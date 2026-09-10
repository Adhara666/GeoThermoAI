# -*- coding: utf-8 -*-
"""理解层 — 以消息接收时间为锚的时间解析（升级第二阶段：理解层）。

依据升级方案 9.2 第 3 条与总体技术方案 §4.2：

  复用现有 `roles/slots.parse_time_expression()`，但

  1. **以消息接收时间为锚点**（不是进程当前时间），并把锚点日期、时区
     和原表达一起存下来——重试、跨天恢复不重新解释（最终版 §4.3）；
  2. **单日与明确区间保持原范围**，不擅自补成整月；
  3. 「最近」「夏天」「去年夏天」等无法唯一确定时**先问具体时间**，
     不猜月份。

本模块不修改 `slots.py`（科学/解析规则单一来源），只在其外层加锚点与
「单日不扩月」这两条程序判定。
"""

import datetime
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.agent.roles import slots as slot_utils

# 「YYYY-M-D」「YYYY年M月D日」这类**单个完整日期**：必须保持单日，
# 不能被下游的「年+月 → 整月区间」规则暗扩成整月（9.2 第 3 条）
_SINGLE_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?(?!\s*[-/~至到])"
)

# 出现这些词说明用户在说一个区间/多日范围，此时不做「单日」判定
_RANGE_HINTS = ("到", "至", "~", "—", "－", "-之间", "之间", "起", "以来")


@dataclass(frozen=True)
class TimeResolution:
    """一次时间解析结果。`ok=True` 才允许进入执行链。"""

    ok: bool
    start: str = ""
    end: str = ""
    precision: str = slot_utils.PRECISION_NONE
    year: Optional[int] = None
    month: Optional[int] = None
    raw: str = ""
    anchor_date: str = ""
    tz_offset: float = 0.0
    relative: bool = False       # 原表达是相对时间（去年/上个月/最近N天）
    reason: str = ""             # ok=False 时的追问理由（中文，给用户看）

    def to_slot_value(self) -> Dict[str, Any]:
        """存入任务槽位的值形状（绝对起止 + 原表达 + 锚点）。"""
        return {
            "start": self.start, "end": self.end, "precision": self.precision,
            "year": self.year, "month": self.month, "raw": self.raw,
            "anchor_date": self.anchor_date, "tz_offset": self.tz_offset,
            "relative": self.relative,
        }


def anchor_from(received_at: datetime.datetime,
                tz_offset_hours: float) -> datetime.date:
    """把消息接收时刻（UTC）换算成用户本地日期，作为相对时间的锚点。

    锚点一旦定下就随槽位一起存档；后续重试/跨天恢复复用它，
    「去年七月」不会因为跨年而漂移（最终版 §4.3「相对时间」行）。
    """
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=datetime.timezone.utc)
    local = received_at.astimezone(
        datetime.timezone(datetime.timedelta(hours=float(tz_offset_hours or 0.0)))
    )
    return local.date()


def _is_relative(text: str) -> bool:
    return any(w in text for w in
               ("今年", "本年", "去年", "上一年", "前年", "明年", "上个月", "上月",
                "本月", "这个月", "当月", "最近", "近期", "个月前", "前"))


def _single_date(text: str) -> Optional[datetime.date]:
    """文本是否只给了一个明确单日（不带区间提示词）。"""
    if any(h in text for h in _RANGE_HINTS):
        return None
    matches = _SINGLE_DATE_RE.findall(text)
    if len(matches) != 1:
        return None
    year, month, day = (int(x) for x in matches[0])
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def resolve(expression: str, *, anchor_date: datetime.date,
            tz_offset: float = 0.0) -> TimeResolution:
    """解析一段时间表达；返回绝对起止或「需要追问」的理由。"""
    raw = (expression or "").strip()
    if not raw:
        return TimeResolution(ok=False, raw=raw,
                              anchor_date=anchor_date.isoformat(),
                              tz_offset=tz_offset,
                              reason="没有说时间范围")

    normalized = slot_utils.normalize_cn_numerals(raw)
    relative = _is_relative(normalized)
    base = dict(anchor_date=anchor_date.isoformat(), tz_offset=tz_offset,
                raw=raw, relative=relative)

    # 1) 单日：保持原范围，绝不扩成整月（9.2 第 3 条）
    day = _single_date(normalized)
    if day is not None:
        if day > anchor_date:
            return TimeResolution(ok=False, reason=f"{day.isoformat()} 还没有到，"
                                                   f"没有可用影像", **base)
        if day.year < slot_utils.MIN_DATA_YEAR:
            return TimeResolution(
                ok=False, reason=f"{day.year} 年早于系统覆盖范围"
                                 f"（{slot_utils.MIN_DATA_YEAR} 年起）", **base)
        return TimeResolution(ok=True, start=day.isoformat(), end=day.isoformat(),
                              precision=slot_utils.PRECISION_DAY,
                              year=day.year, month=day.month, **base)

    parsed = slot_utils.parse_time_expression(raw, today=anchor_date)
    precision = str(parsed.get("precision") or slot_utils.PRECISION_NONE)
    year, month = parsed.get("year"), parsed.get("month")

    if not slot_utils.is_executable(precision):
        return TimeResolution(ok=False, precision=precision, year=year,
                              month=month,
                              reason=_ask_reason(precision, year, month, raw),
                              **base)

    start, end = str(parsed.get("start") or ""), str(parsed.get("end") or "")
    invalid = slot_utils.time_range_valid(start, end, today=anchor_date)
    if invalid:
        return TimeResolution(ok=False, precision=precision, year=year,
                              month=month, reason=invalid, **base)
    return TimeResolution(ok=True, start=start, end=end, precision=precision,
                          year=year, month=month, **base)


def _ask_reason(precision: str, year: Optional[int], month: Optional[int],
                raw: str) -> str:
    """追问理由：说清楚缺什么，不复述明显异常的年份（沿用 slots 的合理性判据）。"""
    if year and not slot_utils.year_plausible(year):
        return (f"「{raw}」不像一个能下载到影像的年份"
                f"（系统数据从 {slot_utils.MIN_DATA_YEAR} 年前后开始覆盖）")
    if precision == slot_utils.PRECISION_SEASON:
        return f"「{raw}」有多种理解，需要一个能唯一确定的时间"
    if year and not month:
        return f"只说了 {year} 年，还需要具体月份或日期范围"
    if month and not year:
        return f"只说了 {month} 月，还需要哪一年"
    return "没有解析出可执行的时间范围"


def is_whole_month(start: str, end: str) -> bool:
    """起止是否正好覆盖一个完整自然月（决定要不要问配对/月度合成）。"""
    import calendar

    if len(start) < 10 or len(end) < 10 or start[:7] != end[:7]:
        return False
    try:
        year, month = int(start[:4]), int(start[5:7])
        last = calendar.monthrange(year, month)[1]
    except (ValueError, IndexError):
        return False
    return start[8:10] == "01" and int(end[8:10]) == last
