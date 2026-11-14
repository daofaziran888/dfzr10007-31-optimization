"""分析告警统一判定标准：增长阈值、静默周期、生效范围与触发判定的唯一入口。

告警规则登记、启用校验、触发窗口重算、指标异动归因页展示四条链路都只能走
这里的解析函数，避免同一监控指标在不同页面各算一套口径：

* 增长阈值统一折算成百分数，允许 ``15``、``15%``、``15 个百分点`` 等写法，
  允许区间 ``(0%, 100%]``，越界即拒绝；
* 静默周期统一折算成秒，允许 ``1800``、``1800秒``、``30分钟``、``1小时`` 等写法，
  允许区间 ``[1, 3600]`` 秒（上限 1 小时），越界即拒绝进入生效状态；
* 生效范围支持 ``全天`` / ``7x24`` / ``工作日 09:00-18:00`` 等写法，时间边界
  必须落在 ``00:00-24:00`` 内且结束晚于开始；
* 触发口径保持统一：实际增长 ``>=`` 增长阈值即触发，无法计算的不触发。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

# 静默周期上限：1 小时。超过该上限的静默配置不允许进入生效状态。
MAX_SILENCE_SECONDS = 3600
MIN_SILENCE_SECONDS = 1
# 增长阈值上限：100%。超过该上限的阈值按非法配置处理。
MAX_GROWTH_THRESHOLD_PERCENT = 100.0
MIN_GROWTH_THRESHOLD_PERCENT = 0.0

DEFAULT_SCOPE = "全天"
DEFAULT_SILENCE_SECONDS = 1800
DEFAULT_GROWTH_THRESHOLD = 15.0


class StandardError(ValueError):
    """统一判定标准不通过时抛出，消息可直接回给调用方展示。"""


def pick(values: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按给定别名顺序取第一个有值的字段，兼容中英文两套入参命名。"""
    for key in keys:
        if key in values and values[key] is not None and str(values[key]).strip() != "":
            return values[key]
    return default


def normalize_text(value: Any) -> str:
    """把指标 / 条件文本压成稳定的归并键：去空白、去全角空格、转小写。"""
    return re.sub(r"\s+", "", str(value or "").replace("　", " ")).lower()


# ---------------------------------------------------------------- 增长阈值
_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:%|％|个百分点)?")


def parse_growth_threshold(raw: Any, *, enforce_limit: bool = True) -> tuple[float, float]:
    """把增长阈值统一折算成 ``(百分数, 小数)`` 二元组。

    ``enforce_limit=False`` 只校验能否解析，供草稿保存使用；进入生效状态前必须
    以默认的 ``enforce_limit=True`` 再校验一次上限。
    """
    if isinstance(raw, bool):
        raise StandardError("增长阈值必须是数值或百分位文本，不允许保存")
    if isinstance(raw, (int, float)):
        percent = float(raw)
    elif isinstance(raw, str) and raw.strip():
        match = _PERCENT_RE.search(raw.strip())
        if match is None:
            raise StandardError(f"增长阈值「{raw}」无法解析为数值，不允许保存")
        percent = float(match.group(1))
    else:
        raise StandardError("增长阈值为空或类型无法识别，不允许保存")

    if percent <= MIN_GROWTH_THRESHOLD_PERCENT:
        raise StandardError(f"增长阈值 {percent:g}% 必须大于 0%，不允许保存")
    if enforce_limit and percent > MAX_GROWTH_THRESHOLD_PERCENT:
        raise StandardError(
            f"增长阈值 {percent:g}% 超出允许范围 (0%, {MAX_GROWTH_THRESHOLD_PERCENT:g}%]，"
            "拒绝进入生效状态"
        )
    return percent, percent / 100.0


def threshold_text(percent: float) -> str:
    return f"{percent:g}%"


# ---------------------------------------------------------------- 静默周期
_SILENCE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(秒|s|sec|second|分钟|min|m|小时|h|hour)?", re.I)
_SILENCE_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1, "second": 1,
    "分钟": 60, "min": 60, "m": 60,
    "小时": 3600, "h": 3600, "hour": 3600,
}


def parse_silence(raw: Any, *, enforce_limit: bool = True) -> int:
    """把静默周期统一折算成秒；不带单位按秒处理，超过上限直接拒绝。

    ``enforce_limit=False`` 只校验能否解析，供草稿登记使用；进入生效状态前必须
    以默认的 ``enforce_limit=True`` 再校验一次上限。
    """
    if isinstance(raw, bool):
        raise StandardError("静默周期必须是秒数或带单位的时间文本，不允许保存")
    if isinstance(raw, (int, float)):
        seconds = float(raw)
    elif isinstance(raw, str) and raw.strip():
        match = _SILENCE_RE.fullmatch(raw.strip())
        if match is None:
            raise StandardError(f"静默周期「{raw}」无法解析，不允许保存")
        value = float(match.group(1))
        unit = (match.group(2) or "秒").lower()
        seconds = value * _SILENCE_UNIT_SECONDS.get(unit, 1)
    else:
        raise StandardError("静默周期为空或类型无法识别，不允许保存")

    if not seconds.is_integer():
        raise StandardError("静默周期必须能折算成整秒，不允许保存")
    seconds = int(seconds)
    if seconds < MIN_SILENCE_SECONDS:
        raise StandardError(f"静默周期必须至少 {MIN_SILENCE_SECONDS} 秒，不允许保存")
    if enforce_limit and seconds > MAX_SILENCE_SECONDS:
        raise StandardError(
            f"静默周期 {seconds} 秒超出允许上限 {MAX_SILENCE_SECONDS} 秒，"
            "拒绝进入生效状态"
        )
    return seconds


def silence_text(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


# ---------------------------------------------------------------- 生效范围
_DAY_SCOPE_WORDS = {"全天", "7x24", "7*24", "24小时", "每日", "每天"}
_SCOPE_RE = re.compile(
    r"(工作日|每个工作日|每天|每日|全天)?\s*"
    r"(\d{1,2}):(\d{2})\s*[-~～至]\s*(\d{1,2}):(\d{2})"
)
_WEEKDAY_WORDS = {"工作日", "每个工作日"}


@dataclass(frozen=True)
class Scope:
    """解析后的生效范围：分钟粒度半开区间 + 允许的星期集合。"""

    raw: str
    normalized: str
    start_minute: int
    end_minute: int
    weekdays: tuple[int, ...] | None  # 0=周一 ... 6=周日；None 表示每天都生效

    def allows(self, moment: datetime) -> bool:
        if self.weekdays is not None and moment.weekday() not in self.weekdays:
            return False
        minute = moment.hour * 60 + moment.minute
        return self.start_minute <= minute < self.end_minute


def parse_scope(raw: Any) -> Scope:
    """解析生效范围；无法解析或时间边界越界时拒绝。"""
    text = DEFAULT_SCOPE if raw is None or str(raw).strip() == "" else str(raw).strip()
    if text in _DAY_SCOPE_WORDS:
        return Scope(text, "全天", 0, 24 * 60, None)

    match = _SCOPE_RE.fullmatch(text)
    if match is None:
        raise StandardError(
            f"生效范围「{text}」无法解析，允许写法：全天 / 工作日 09:00-18:00 / 09:00-18:00"
        )
    prefix, start_hour, start_minute, end_hour, end_minute = match.groups()
    start_h = int(start_hour)
    start_m = int(start_minute)
    end_h = int(end_hour)
    end_m = int(end_minute)

    if not (0 <= start_h <= 24 and 0 <= end_h <= 24):
        raise StandardError(f"生效范围「{text}」的时间小时数超出 00:00-24:00，拒绝进入生效状态")
    if not (0 <= start_m < 60 and 0 <= end_m < 60):
        raise StandardError(f"生效范围「{text}」的分钟数超出 00-59，拒绝进入生效状态")
    if start_h == 24 and start_m != 0:
        raise StandardError(f"生效范围「{text}」的起始时间超出 24:00，拒绝进入生效状态")
    if end_h == 24 and end_m != 0:
        raise StandardError(f"生效范围「{text}」的结束时间超出 24:00，拒绝进入生效状态")

    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if end <= start:
        raise StandardError(f"生效范围「{text}」的结束时间必须晚于开始时间，拒绝进入生效状态")

    weekdays = tuple(range(0, 5)) if prefix in _WEEKDAY_WORDS else None
    normalized = f"{'工作日 ' if weekdays is not None else ''}{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
    return Scope(text, normalized, start, end, weekdays)


# ---------------------------------------------------------------- 告警条件
_OPERATOR_RE = re.compile(r"(>=|≥|>|<=|≤|<|=)\s*(-?\d+(?:\.\d+)?)\s*(?:%|％|个百分点)?\s*$")
_METRIC_NOISE = (
    "同环比增长率", "环比增长率", "同比增长率", "增长率", "增长阈值",
    "告警条件", "告警", "条件",
)


def parse_condition(raw: Any) -> dict[str, Any] | None:
    """把告警条件解析成 ``{metric, operator, threshold_percent}``。

    解析不出比较运算符或阈值时返回 ``None``；启用规则时由调用方拒绝进入生效状态，
    历史脏数据在读取链路里只展示、不参与触发。
    """
    condition = "" if raw is None else str(raw).strip()
    match = _OPERATOR_RE.search(condition)
    if match is None:
        return None
    operator = match.group(1).replace("≥", ">=").replace("≤", "<=")
    try:
        percent, _ratio = parse_growth_threshold(match.group(2))
    except StandardError:
        return None
    prefix = condition[: match.start()].strip()
    for noise in _METRIC_NOISE:
        prefix = prefix.replace(noise, " ").strip()
    return {
        "metric": prefix,
        "operator": operator,
        "threshold_percent": percent,
        "condition": condition,
    }


def rebuild_condition(raw: Any, percent: float) -> str:
    """把告警条件末尾的阈值改写成新的百分数，保留指标与比较符。"""
    condition = "" if raw is None else str(raw).strip()
    match = _OPERATOR_RE.search(condition)
    if match is None:
        return f"{condition} >= {threshold_text(percent)}".strip()
    return f"{condition[: match.start()].strip()} {match.group(1)} {threshold_text(percent)}".strip()


# ---------------------------------------------------------------- 时间与判定
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def parse_datetime(raw: Any, *, default: datetime | None = None) -> datetime:
    """解析事件时间；无法识别时退回默认值（调用方一般传当前时间）。"""
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    if isinstance(raw, date):
        return datetime.combine(raw, time())
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("T", " ")
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            pass
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    if default is not None:
        return default.replace(tzinfo=None)
    raise StandardError(f"时间「{raw}」无法解析")


def format_datetime(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def growth_value(raw: Any) -> float | None:
    """解析事件的实际增长幅度，统一成百分数；无法识别时返回 ``None``。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        match = _PERCENT_RE.search(raw.strip())
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def breached(actual_percent: float | None, threshold_percent: float | None) -> bool:
    """统一触发口径：实际增长达到或超过阈值即触发。"""
    if actual_percent is None or threshold_percent is None:
        return False
    return actual_percent >= threshold_percent


def scope_label(scope: Any) -> str:
    if isinstance(scope, Scope):
        return scope.normalized
    try:
        return parse_scope(scope).normalized
    except StandardError:
        return str(scope)
