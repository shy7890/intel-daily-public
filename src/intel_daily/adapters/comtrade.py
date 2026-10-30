import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

DEFAULT_BASE_URL = "https://comtradeapi.un.org"
DEFAULT_URL_TEMPLATE = (
    "https://comtradeapi.un.org/public/v1/preview/C/{freq_code}/HS"
    "?reporterCode={reporter_code}&period={period}&cmdCode={cmd_code}"
    "&partnerCode={partner_code}&flowCode={flow_code}"
)
PUBLISHER = "UN Comtrade"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 15
REQUEST_INTERVAL = 1.0
DEFAULT_REPORTER_CODE = 156
DEFAULT_CMD_CODES = ("850760",)
DEFAULT_PARTNER_CODE = 0
DEFAULT_FLOW_CODE = "X"
# 2026-08-27 实测：public preview 数据集仅发布到 2024 年度，
# 月频（C/M/HS）与 2025+ 年度均为 0 记录 → 只支持年度频。
DEFAULT_FREQ_CODE = "A"
DEFAULT_WINDOW_YEARS = 3

# 公开 preview 接口的描述字段（reporterDesc/cmdDesc/flowDesc）实测为 null，
# 标题素材由以下标签映射提供，未命中时回退到代码本身。
_REPORTER_NAMES = {156: "中国"}
_CMD_LABELS = {"850760": "锂离子电池"}
_FLOW_LABELS = {"X": "出口", "M": "进口"}


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


def _shift_year(value: str, delta: int) -> str:
    return str(int(value) + delta)


def _int_or_default(value, default: int) -> int:
    """reporterCode 等字段来自外部响应，非数值（如 "CN"）必须回退默认值，不得抛异常。"""
    if value is None or isinstance(value, bool):
        return default
    text = str(value).strip()
    for parser in (int, lambda raw: int(float(raw))):
        try:
            return parser(text)
        except (TypeError, ValueError):
            continue
    return default


def _range_years(start: str, end: str) -> list[str]:
    if not start or not end or len(start) != 4 or len(end) != 4:
        return []
    if not (start.isdigit() and end.isdigit()):
        return []
    years = []
    year = int(start)
    while year <= int(end):
        years.append(str(year))
        year += 1
    return years


def _end_year(now: datetime) -> str:
    """年度统计滞后（preview 仅发布到上一年的数据）：默认终点 = now 的上一自然年。"""
    return str(now.astimezone(TIMEZONE).year - 1)


def build_comtrade_api_url(record: dict) -> str:
    """按记录字段复刻可复现的官方 API URL（公开接口返回的记录含全部参数）。"""
    type_code = str(record.get("typeCode") or "C")
    freq_code = str(record.get("freqCode") or "A")
    classification = str(record.get("classificationSearchCode") or "HS")
    query = (
        f"reporterCode={record.get('reporterCode') or DEFAULT_REPORTER_CODE}"
        f"&period={record.get('period') or ''}"
        f"&cmdCode={record.get('cmdCode') or ''}"
        f"&partnerCode={record.get('partnerCode') or DEFAULT_PARTNER_CODE}"
        f"&flowCode={record.get('flowCode') or ''}"
    )
    return (
        f"{DEFAULT_BASE_URL}/public/v1/preview/{type_code}/{freq_code}/"
        f"{classification}?{query}"
    )


def parse_comtrade_payload(
    payload: dict, requested_period: str, fetched_at: datetime
) -> list[SourceItem]:
    """解析 UN Comtrade 公共预览 API 响应；每条合法 record 产出一条统计值条目。

    合法 record：有 cmdCode/flowCode + 数值型 primaryValue（无值的行排除，
    例如部分聚合行仅含估算标志）；描述字段缺失一律回退标签映射，
    不得为 null 描述编造标题素材。external_id=f"{cmdCode}-{period}-{flowCode}"。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    items: list[SourceItem] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        primary_value = _num(row.get("primaryValue"))
        if primary_value is None:
            continue
        cmd_code = str(row.get("cmdCode") or "").strip()
        flow_code = str(row.get("flowCode") or "").strip()
        if not cmd_code or not flow_code:
            continue
        period = str(row.get("period") or "").strip() or requested_period
        reporter_code = _int_or_default(
            row.get("reporterCode"), DEFAULT_REPORTER_CODE
        )
        reporter = (
            str(row.get("reporterDesc") or "").strip()
            or _REPORTER_NAMES.get(reporter_code, str(reporter_code))
        )
        label = (
            str(row.get("cmdDesc") or "").strip()
            or _CMD_LABELS.get(cmd_code, cmd_code)
        )
        flow_label = (
            str(row.get("flowDesc") or "").strip()
            or _FLOW_LABELS.get(flow_code, "值")
        )
        items.append(
            SourceItem(
                external_id=f"{cmd_code}-{period}-{flow_code}",
                title=f"{reporter} {period} {label}（HS {cmd_code}）{flow_label}额",
                publisher=PUBLISHER,
                published_at=fetched_at,
                original_url=build_comtrade_api_url(row),
                summary=f"{flow_label}额 {primary_value:,.0f} USD",
                keywords=(label,),
                fetched_at=fetched_at,
            )
        )
    return items


class ComtradeAdapter:
    """UN Comtrade 公开预览 API 适配器（官方一手贸易统计，GET JSON，仅年度频）。

    preview 数据集滞后：2026-08-27 实测仅发布到 2024 年度，月频无记录 →
    cursor 为已拉取的最新年度令牌（YYYY，如 "2024"）；窗口 = 最近 window_years
    个年度（默认终点 = now 的上一自然年），逐年度、逐 cmdCode 请求（默认
    3 个年度 × 1 个 HS 码，request_interval=1.0s 适配官方 ≤1 req/s 限速）。
    无数据的年度自动跳过；new_cursor 只推进到“有记录的最新年度”，无数据则
    保持旧 cursor（避免停在无数据的年份）。HTTP >=400（含 429）→ SourceFetchError。
    """

    def __init__(
        self,
        url_template: str = DEFAULT_URL_TEMPLATE,
        reporter_code: int = DEFAULT_REPORTER_CODE,
        cmd_codes: tuple[str, ...] = DEFAULT_CMD_CODES,
        period_start: str | None = None,
        request_interval: float = REQUEST_INTERVAL,
        timeout: int = REQUEST_TIMEOUT,
        partner_code: int = DEFAULT_PARTNER_CODE,
        flow_code: str = DEFAULT_FLOW_CODE,
        freq_code: str = DEFAULT_FREQ_CODE,
        window_years: int = DEFAULT_WINDOW_YEARS,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.url_template = url_template
        self.reporter_code = reporter_code
        self.cmd_codes = tuple(cmd_codes)
        self.period_start = period_start
        self.request_interval = request_interval
        self.timeout = timeout
        self.partner_code = partner_code
        self.flow_code = flow_code
        self.freq_code = freq_code
        self.window_years = window_years
        self.user_agent = user_agent

    def _window_periods(self, cursor: str | None, now: datetime) -> list[str]:
        end = _end_year(now)
        start = self.period_start or _shift_year(end, -(self.window_years - 1))
        marker = (cursor or "").strip()
        if marker and not marker.isdigit():
            # cursor 被写坏（不是年度令牌）：不猜窗口，抛错由上层记该源 failed。
            raise SourceFetchError(f"Comtrade cursor 非年度令牌: {marker!r}")
        if marker:
            if marker >= end:
                return []
            start = max(start, _shift_year(marker, 1))
        return _range_years(start, end)

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        periods = self._window_periods(cursor, now)
        marker = (cursor or "").strip()
        if not periods:
            return FetchBatch(items=[], new_cursor=marker)
        items: list[SourceItem] = []
        last_with_items = None
        sent_any = False
        for period in periods:
            for cmd_code in self.cmd_codes:
                if sent_any:
                    time.sleep(self.request_interval)
                sent_any = True
                payload = self._request_period(period, cmd_code)
                period_items = parse_comtrade_payload(
                    payload, requested_period=period, fetched_at=now
                )
                if period_items:
                    last_with_items = period
                items.extend(period_items)
        return FetchBatch(
            items=items,
            new_cursor=last_with_items or marker,
        )

    def _request_period(self, period: str, cmd_code: str) -> dict:
        url = self.url_template.format(
            freq_code=self.freq_code,
            reporter_code=self.reporter_code,
            period=period,
            cmd_code=cmd_code,
            partner_code=self.partner_code,
            flow_code=self.flow_code,
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raise SourceFetchError(f"Comtrade API 返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"Comtrade API 不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"Comtrade API 返回 {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceFetchError(f"Comtrade API 返回非法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceFetchError("Comtrade API 返回非法 JSON 结构")
        error = str(payload.get("error") or "").strip()
        if error:
            raise SourceFetchError(f"Comtrade API 返回错误: {error}")
        return payload
