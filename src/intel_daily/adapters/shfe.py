import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

DEFAULT_DATA_BASE_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/"
DEFAULT_PRODUCTS = ("ni_f", "sn_f")
PUBLISHER = "上海期货交易所"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20
SEARCH_DAYS = 10
_UPDATE_TIME_FORMAT = "%Y%m%d %H:%M:%S"
_REPORT_DATE_FORMAT = "%Y%m%d"
_SUBTOTAL_MONTHS = ("", "小计")
_PRICE_FIELDS = (
    ("OPENPRICE", "开"),
    ("HIGHESTPRICE", "高"),
    ("LOWESTPRICE", "低"),
    ("CLOSEPRICE", "收"),
    ("SETTLEMENTPRICE", "结算"),
    ("PRESETTLEMENTPRICE", "昨结"),
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


def _month_of(row: dict) -> str:
    return str(row.get("DELIVERYMONTH") or "").strip()


def _is_subtotal_row(row: dict) -> bool:
    return _month_of(row) in _SUBTOTAL_MONTHS


def _pick_main_row(rows: list[dict]) -> dict | None:
    best = None
    best_oi = -1.0
    for row in rows:
        if _is_subtotal_row(row):
            continue
        oi = _num(row.get("OPENINTEREST"))
        if oi is None:
            continue
        if oi > best_oi:
            best_oi = oi
            best = row
    return best


def _find_subtotal_row(rows: list[dict]) -> dict | None:
    for row in rows:
        if _is_subtotal_row(row):
            return row
    return None


def _format_report_date(report_date: str) -> str:
    return f"{report_date[0:4]}-{report_date[4:6]}-{report_date[6:8]}"


def _resolve_published_at(payload: dict, report_date: str) -> datetime:
    raw_update = str(payload.get("update_date") or "")
    if raw_update:
        try:
            return datetime.strptime(
                raw_update.strip(), _UPDATE_TIME_FORMAT
            ).replace(tzinfo=TIMEZONE)
        except ValueError:
            pass
    try:
        return datetime.strptime(report_date, _REPORT_DATE_FORMAT).replace(
            tzinfo=TIMEZONE
        )
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=TIMEZONE)


def _build_title(product_name: str, main_row: dict, report_date: str) -> str:
    day = _format_report_date(report_date)
    settlement = _num(main_row.get("SETTLEMENTPRICE"))
    if settlement is not None:
        return f"沪期货 {product_name}：主力合约结算价 {_fmt(settlement)}（{day}）"
    close = _num(main_row.get("CLOSEPRICE"))
    if close is not None:
        return f"沪期货 {product_name}：主力合约收盘价 {_fmt(close)}（{day}）"
    return f"沪期货 {product_name}（{day}）"


def _build_summary(main_row: dict, subtotal_row: dict | None) -> str:
    month = _month_of(main_row)
    price_tokens = []
    for field, label in _PRICE_FIELDS:
        value = _num(main_row.get(field))
        if value is None:
            continue
        price_tokens.append(f"{label} {_fmt(value)}")
    head = f"主力 {month}：{' '.join(price_tokens)}" if price_tokens else f"主力 {month}"
    parts = [head]
    volume = _num(main_row.get("VOLUME"))
    if volume is not None:
        parts.append(f"成交 {_fmt(volume)} 手")
    oi = _num(main_row.get("OPENINTEREST"))
    if oi is not None:
        chg = _num(main_row.get("OPENINTERESTCHG"))
        if chg is not None:
            parts.append(f"持仓 {_fmt(oi)}（{int(chg):+d}）手")
        else:
            parts.append(f"持仓 {_fmt(oi)} 手")
    if subtotal_row is not None:
        total_tokens = []
        total_volume = _num(subtotal_row.get("VOLUME"))
        if total_volume is not None:
            total_tokens.append(f"品种总成交 {_fmt(total_volume)} 手")
        total_oi = _num(subtotal_row.get("OPENINTEREST"))
        if total_oi is not None:
            total_tokens.append(f"总持仓 {_fmt(total_oi)} 手")
        if total_tokens:
            parts.append("、".join(total_tokens))
    return "；".join(parts) + "。"


def parse_shfe_payload(
    payload: dict,
    dat_url: str,
    fetched_at: datetime,
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
) -> list[SourceItem]:
    report_date = str(payload.get("report_date") or "")
    if len(report_date) != 8:
        return []
    try:
        datetime.strptime(report_date, _REPORT_DATE_FORMAT)
    except ValueError:
        return []
    published_at = _resolve_published_at(payload, report_date)
    rows = payload.get("o_curinstrument") or []
    if not isinstance(rows, list):
        return []

    items: list[SourceItem] = []
    for product_id in products:
        product_rows = [
            row for row in rows if str(row.get("PRODUCTID")) == product_id
        ]
        main_row = _pick_main_row(product_rows)
        if main_row is None:
            continue
        subtotal_row = _find_subtotal_row(product_rows)
        month = _month_of(main_row)
        product_name = str(main_row.get("PRODUCTNAME") or product_id)
        items.append(
            SourceItem(
                external_id=f"{report_date}-{product_id}-{month}",
                title=_build_title(product_name, main_row, report_date),
                publisher=PUBLISHER,
                published_at=published_at,
                original_url=dat_url,
                summary=_build_summary(main_row, subtotal_row),
                keywords=(product_name,),
                fetched_at=fetched_at,
            )
        )
    return items


class ShfeAdapter:
    """上期所每日行情 .dat 适配器（免费公开 JSON，免登录）。

    cursor 为 report_date 的 YYYYMMDD；从 now 的上海日期向后回退最多
    search_days 个日历日，取第一份可解析且合约列表非空的 kxYYYYMMDD.dat；
    仅当文件 report_date 大于 cursor 时产出新增。
    """

    def __init__(
        self,
        data_base_url: str = DEFAULT_DATA_BASE_URL,
        products: tuple[str, ...] = DEFAULT_PRODUCTS,
        search_days: int = SEARCH_DAYS,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.data_base_url = data_base_url
        self.products = tuple(products)
        self.search_days = search_days
        self.user_agent = user_agent

    def _day_url(self, day: datetime.date) -> str:
        base = (
            self.data_base_url
            if self.data_base_url.endswith("/")
            else self.data_base_url + "/"
        )
        return f"{base}kx{day.strftime(_REPORT_DATE_FORMAT)}.dat"

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        yesterday = now.astimezone(TIMEZONE).date() - timedelta(days=1)
        last_failure: str | None = None
        for offset in range(self.search_days):
            url = self._day_url(yesterday - timedelta(days=offset))
            payload, failure = self._request_day(url)
            if payload is None:
                last_failure = failure or url
                continue
            dat_url = url
            report_date = str(payload.get("report_date") or "")
            items = parse_shfe_payload(
                payload, dat_url=dat_url, fetched_at=now, products=self.products
            )
            marker = (cursor or "").strip()
            if marker and report_date <= marker:
                return FetchBatch(items=[], new_cursor=marker)
            return FetchBatch(
                items=items,
                new_cursor=report_date if items else (cursor or ""),
            )
        raise SourceFetchError(
            f"上期所每日行情接口不可达: {last_failure or '连续请求失败'}"
        )

    def _request_day(self, url: str) -> tuple[dict | None, str | None]:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, "404"
            raise SourceFetchError(f"上期所每日行情接口返回 {exc.code}") from exc
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
        if not payload.get("o_curinstrument"):
            return None, "空合约列表"
        report_date = str(payload.get("report_date") or "")
        if len(report_date) != 8:
            return None, "report_date 缺失"
        return payload, None
