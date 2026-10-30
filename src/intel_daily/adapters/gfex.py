import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

DEFAULT_QUERY_URL = (
    "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
)
DATA_PAGE_URL = "http://www.gfex.com.cn/gfex/sspzb/sspz.shtml"
DEFAULT_VARIETIES = ("lc",)
PUBLISHER = "广州期货交易所"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20
SEARCH_DAYS = 10
REQUEST_INTERVAL = 1.0
_REPORT_DATE_FORMAT = "%Y%m%d"
_PRICE_FIELDS = (
    ("open", "开"),
    ("high", "高"),
    ("low", "低"),
    ("close", "收"),
    ("clearPrice", "结算"),
)


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _fmt(value: float | None) -> str | None:
    if value is None:
        return None
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _contract_rows(data: list[dict], variety_code: str) -> list[dict]:
    """品种合约行：varietyOrder（请求参数代码，如 lc）匹配 + 有交割月份。

    真值核对：varietyEn 为英文名（Lithium Carbonate/Total/…SubTotal），
    只有 varietyOrder 稳定携带接口参数代码。
    """
    rows = []
    for row in data:
        if str(row.get("varietyOrder")) != variety_code:
            continue
        month = str(row.get("delivMonth") or "").strip()
        if not month:
            continue
        rows.append(row)
    return rows


def _subtotal_row(data: list[dict], variety_code: str) -> dict | None:
    for row in data:
        if str(row.get("varietyOrder")) != variety_code:
            continue
        if "SubTotal" in str(row.get("varietyEn") or "") or "小计" in str(
            row.get("variety") or ""
        ):
            return row
    return None


def _total_row(data: list[dict]) -> dict | None:
    for row in data:
        if "Total" in str(row.get("varietyEn") or "") or str(
            row.get("variety") or ""
        ).strip() == "总计":
            return row
    return None


def _pick_main(rows: list[dict]) -> dict | None:
    best = None
    best_oi = -1.0
    for row in rows:
        oi = _num(row.get("openInterest"))
        if oi is None:
            continue
        if oi > best_oi:
            best_oi = oi
            best = row
    return best


def _format_date(report_date: str) -> str:
    return f"{report_date[0:4]}-{report_date[4:6]}-{report_date[6:8]}"


def _build_title(product_name: str, main_row: dict, trade_date: str) -> str:
    day = _format_date(trade_date)
    settlement = _num(main_row.get("clearPrice"))
    if settlement is not None:
        return f"广期所 {product_name}：主力合约结算价 {_fmt(settlement)}（{day}）"
    close = _num(main_row.get("close"))
    if close is not None:
        return f"广期所 {product_name}：主力合约收盘价 {_fmt(close)}（{day}）"
    return f"广期所 {product_name}（{day}）"


def _build_summary(
    main_row: dict, subtotal_row: dict | None, total_row: dict | None
) -> str:
    month = str(main_row.get("delivMonth") or "")
    tokens = []
    for field, label in _PRICE_FIELDS:
        value = _num(main_row.get(field))
        if value is None:
            continue
        tokens.append(f"{label} {_fmt(value)}")
    if tokens:
        head = f"主力 {month}：{' '.join(tokens)}"
    else:
        head = f"主力 {month}"
    diff = _num(main_row.get("diff"))
    parts = [head]
    if diff is not None:
        parts.append(f"涨跌 {_fmt(diff)}")
    volume = _num(main_row.get("volumn"))
    if volume is not None:
        parts.append(f"成交 {_fmt(volume)} 手")
    oi = _num(main_row.get("openInterest"))
    if oi is not None:
        parts.append(f"持仓 {_fmt(oi)} 手")
    aggregates = []
    if subtotal_row is not None:
        total_volume = _num(subtotal_row.get("volumn"))
        if total_volume is not None:
            aggregates.append(f"品种总成交 {_fmt(total_volume)} 手")
        total_oi = _num(subtotal_row.get("openInterest"))
        if total_oi is not None:
            aggregates.append(f"总持仓 {_fmt(total_oi)} 手")
    elif total_row is not None:
        total_volume = _num(total_row.get("volumn"))
        if total_volume is not None:
            aggregates.append(f"品种总成交 {_fmt(total_volume)} 手")
        total_oi = _num(total_row.get("openInterest"))
        if total_oi is not None:
            aggregates.append(f"总持仓 {_fmt(total_oi)} 手")
    if aggregates:
        parts.append("、".join(aggregates))
    return "；".join(parts) + "。"


def parse_gfex_payload(
    payload: dict,
    trade_date: str,
    fetched_at: datetime,
    varieties: tuple[str, ...] = DEFAULT_VARIETIES,
) -> list[SourceItem]:
    """解析广期所日行情响应；仅品种主力合约产出、每条一次。

    公开接口无官方文档；字段名来自 2026-08-27 真实响应
    （open/high/low/close/clearPrice/lastClear/diff/volumn/openInterest/delivMonth）。
    """
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    total_row = _total_row(data)
    items: list[SourceItem] = []
    for variety_en in varieties:
        rows = _contract_rows(data, variety_en)
        main_row = _pick_main(rows)
        if main_row is None:
            continue
        month = str(main_row.get("delivMonth") or "")
        product_name = str(rows[0].get("variety") or variety_en)
        subtotal_row = _subtotal_row(data, variety_en)
        published_at = datetime.strptime(trade_date, _REPORT_DATE_FORMAT).replace(
            tzinfo=TIMEZONE
        )
        items.append(
            SourceItem(
                external_id=f"{trade_date}-{variety_en}-{month}",
                title=_build_title(product_name, main_row, trade_date),
                publisher=PUBLISHER,
                published_at=published_at,
                original_url=DATA_PAGE_URL,
                summary=_build_summary(main_row, subtotal_row, total_row),
                keywords=(product_name,),
                fetched_at=fetched_at,
            )
        )
    return items


class GfexAdapter:
    """广期所行情 JSON 适配器（免登录公开接口，http 而非 https）。

    cursor 为 trade_date 的 YYYYMMDD；从昨日（上海时区）起向后回退最多
    search_days 个交易日，取第一份 code=0 且含合约行（盘后全量）的响应；
    仅当 trade_date 大于 cursor 时产出新增。
    """

    def __init__(
        self,
        query_url: str = DEFAULT_QUERY_URL,
        varieties: tuple[str, ...] = DEFAULT_VARIETIES,
        search_days: int = SEARCH_DAYS,
        request_interval: float = REQUEST_INTERVAL,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.query_url = query_url
        self.varieties = tuple(varieties)
        self.search_days = search_days
        self.request_interval = request_interval
        self.user_agent = user_agent

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        yesterday = now.astimezone(TIMEZONE).date() - timedelta(days=1)
        last_failure: str | None = None
        for offset in range(self.search_days):
            trade_date = (yesterday - timedelta(days=offset)).strftime(
                _REPORT_DATE_FORMAT
            )
            marker = (cursor or "").strip()
            if marker and trade_date <= marker:
                return FetchBatch(items=[], new_cursor=marker)
            items: list[SourceItem] = []
            for index, variety in enumerate(self.varieties):
                if index:
                    time.sleep(self.request_interval)
                payload, failure = self._request_day(trade_date, variety)
                if payload is None:
                    last_failure = failure or trade_date
                    break
                items.extend(
                    parse_gfex_payload(
                        payload,
                        trade_date=trade_date,
                        fetched_at=now,
                        varieties=(variety,),
                    )
                )
            if not items and last_failure:
                continue
            if items:
                return FetchBatch(
                    items=items, new_cursor=trade_date if items else marker
                )
            last_failure = f"{trade_date} 无合约行（可能盘中未全量）"
        raise SourceFetchError(
            f"广期所行情接口不可达: {last_failure or '连续请求失败'}"
        )

    def _request_day(
        self, trade_date: str, variety: str
    ) -> tuple[dict | None, str | None]:
        form = urllib.parse.urlencode(
            {
                "trade_date": trade_date,
                "trade_type": "0",
                "variety": variety,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.query_url,
            data=form,
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, "404"
            raise SourceFetchError(f"广期所行情接口返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            return None, str(exc)
        if status != 200:
            return None, f"status {status}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "非法 JSON"
        if not isinstance(payload, dict):
            return None, "非法 JSON 结构"
        if str(payload.get("code")) != "0":
            return None, str(payload.get("msg") or "业务失败")
        data = payload.get("data")
        if not isinstance(data, list) or not any(
            str(row.get("delivMonth") or "").strip() for row in data
        ):
            return None, "无合约行（盘中未全量）"
        return payload, None
