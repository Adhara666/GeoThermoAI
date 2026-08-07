"""
槽位解析工具（技术方案 4.4）

只做**确定性**解析：研究区文件名匹配、中文时间表达解析。
需要语义判断的部分（中文近似地名、模糊时间澄清）交给规划 Agent 的 LLM 与反问。

不依赖 agent 包内其它模块，避免循环导入。
"""

import calendar
import datetime
import re
from typing import Any, Dict, List, Optional, Sequence

# 时间精度
PRECISION_NONE = "none"      # 完全没解析出时间
PRECISION_YEAR = "year"      # 只到年（必须反问月份）
PRECISION_SEASON = "season"  # 季节等模糊表达（必须反问月份）
PRECISION_MONTH = "month"    # 到月，可执行
PRECISION_DAY = "day"        # 明确起止日期，可执行

_SEASON_WORDS = ("春天", "春季", "夏天", "夏季", "秋天", "秋季", "冬天", "冬季",
                 "上半年", "下半年", "年初", "年末", "最近", "近期")

# 相对年份词
_RELATIVE_YEARS = {"今年": 0, "本年": 0, "去年": -1, "上一年": -1, "前年": -2, "明年": 1}

# ── 中文数字归一化 ─────────────────────────────────────────────────
# 用户常写「24年七月」「二〇二四年十二月」，纯 \d 正则识别不到月份，
# 会退化成「只到年」而反复追问月份。先把中文数字换成阿拉伯数字再解析。
_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 月份：十一/十二/十 要排在个位数之前，否则「十二月」会被切成「十」
_CN_MONTH_RE = re.compile(r"(十[一二]|十|[一二三四五六七八九])(?=\s*月)")
# 年份：连续 2–4 个中文数字且紧跟「年」（「二四年」「二〇二四年」）
_CN_YEAR_RE = re.compile(r"([〇零一二三四五六七八九]{2,4})(?=\s*年)")


def _cn_month_value(token: str) -> int:
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + _CN_DIGIT[token[1]]
    return _CN_DIGIT[token]


def normalize_cn_numerals(text: str) -> str:
    """把「年」「月」前的中文数字换成阿拉伯数字；其它位置不动。"""
    if not text:
        return ""
    out = _CN_YEAR_RE.sub(
        lambda m: "".join(str(_CN_DIGIT[c]) for c in m.group(1)), text)
    return _CN_MONTH_RE.sub(lambda m: str(_cn_month_value(m.group(1))), out)

# 系统可能有数据的最早年份：Sentinel-2A 于 2015 年发射，早于该年份没有可用影像（知识条目 K11）。
# 实现期修订 v1.2：用于拦截「125年」这类明显不合理的年份，避免不加甄别地原样复述反问月份。
MIN_DATA_YEAR = 2015


def year_plausible(year: Optional[int], today: Optional[datetime.date] = None) -> bool:
    """年份是否落在系统可能有数据的合理范围内；空值/超出范围都判为不合理。"""
    if not year:
        return False
    today = today or datetime.date.today()
    try:
        return MIN_DATA_YEAR <= int(year) <= today.year
    except (TypeError, ValueError):
        return False


def match_study_area(candidates: Sequence[Any], name: str) -> Optional[Any]:
    """按用户说的地名在候选研究区文件中匹配：先精确、再包含（双向）。

    candidates 为 `pathlib.Path` 序列。匹配不到或有多个歧义候选时返回 None，
    由调用方决定是反问还是退回「取最新」。
    """
    target = (name or "").strip()
    if not target:
        return None
    stems = [(p, p.stem) for p in candidates]
    for path, stem in stems:
        if stem == target:
            return path
    hits = [path for path, stem in stems if target in stem or stem in target]
    return hits[0] if len(hits) == 1 else None


def match_candidates(candidates: Sequence[Any], name: str) -> List[Any]:
    """返回所有包含匹配的候选（供反问时列出歧义项）。"""
    target = (name or "").strip()
    if not target:
        return []
    return [p for p in candidates if target in p.stem or p.stem in target]


def _month_range(year: int, month: int) -> Dict[str, str]:
    last = calendar.monthrange(year, month)[1]
    return {"start": f"{year:04d}-{month:02d}-01", "end": f"{year:04d}-{month:02d}-{last:02d}"}


def _expand_year(raw_year: str) -> int:
    """两位年份补全为 20xx（「25 年」→ 2025）。"""
    value = int(raw_year)
    return 2000 + value if value < 100 else value


def parse_time_expression(raw: str, today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """解析中文时间表达，返回 {start, end, precision, year, month, raw}。

    只有 precision 为 month/day 时才允许放行执行（规则 P2）；
    year/season/none 一律需要反问确认月份。

    年与月**分别独立抽取**再组合，不走「先匹配年+月、匹配不上就只当年份」的级联：
    级联会让「去年七月」这类同时带相对年与月份的表达丢掉月份，反复追问。
    """
    original = (raw or "").strip()
    text = normalize_cn_numerals(original)
    today = today or datetime.date.today()
    out: Dict[str, Any] = {"start": "", "end": "", "precision": PRECISION_NONE,
                           "year": None, "month": None, "raw": original}
    if not text:
        return out

    # 1. 明确起止日期：2025-07-01 到 2025-07-31
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\D+?(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        g = [int(x) for x in m.groups()]
        out.update(start=f"{g[0]:04d}-{g[1]:02d}-{g[2]:02d}",
                   end=f"{g[3]:04d}-{g[4]:02d}-{g[5]:02d}",
                   precision=PRECISION_DAY, year=g[0], month=g[1])
        return out

    year, month = _extract_year(text, today), _extract_month(text)

    # 2025-07 这种「年-月」写法既无「年」也无「月」字，单独识别
    if year is None or month is None:
        m = re.search(r"(20\d{2})[-/](\d{1,2})(?!\d)", text)
        if m and 1 <= int(m.group(2)) <= 12:
            year = year or int(m.group(1))
            month = month or int(m.group(2))

    out["year"], out["month"] = year, month
    if year and month:
        out.update(_month_range(int(year), int(month)), precision=PRECISION_MONTH)
        return out

    has_season = any(s in text for s in _SEASON_WORDS)
    if year or month:
        # 年、月只给了一个，仍需反问补齐
        out["precision"] = PRECISION_SEASON if has_season else PRECISION_YEAR
    elif has_season:
        out["precision"] = PRECISION_SEASON
    return out


def _extract_year(text: str, today: datetime.date) -> Optional[int]:
    """抽取年份：绝对年（2025年 / 25年 / 2025）或相对年（今年 / 去年 / 前年）。"""
    m = re.search(r"(\d{2,4})\s*年", text)
    if m:
        return _expand_year(m.group(1))
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", text)
    if m:
        return int(m.group(1))
    for word, delta in _RELATIVE_YEARS.items():
        if word in text:
            return today.year + delta
    return None


def _extract_month(text: str) -> Optional[int]:
    """抽取月份：7月 / 七月（已归一化为 7月）。"""
    m = re.search(r"(?<!\d)(\d{1,2})\s*月", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def merge_time_parts(year: Optional[int], month: Optional[int]) -> Dict[str, Any]:
    """把分散在多轮里的年、月合并成可执行的时间范围。"""
    if year and month and 1 <= month <= 12:
        return {**_month_range(int(year), int(month)), "precision": PRECISION_MONTH,
                "year": int(year), "month": int(month)}
    return {"start": "", "end": "", "precision": PRECISION_YEAR if year else PRECISION_NONE,
            "year": year, "month": month}


def is_executable(precision: str) -> bool:
    return precision in (PRECISION_MONTH, PRECISION_DAY)


def time_range_valid(start: str, end: str, today: Optional[datetime.date] = None) -> str:
    """校验时间范围（规则 P2）；合法返回空串，非法返回中文原因。

    实现期修订 v1.2：补下界校验。此前只查「起始<=结束」「不晚于今天」，若用户把年和月
    一次说全但年份手误（如「125年7月」），会被 `merge_time_parts` 判为可执行（月份齐全）
    却生成 `start_date="0125-07-01"` 这种荒谬日期，一路带进 data_acquisition 才崩溃。
    """
    today = today or datetime.date.today()
    try:
        s = datetime.date.fromisoformat(start)
        e = datetime.date.fromisoformat(end)
    except (TypeError, ValueError):
        return "时间范围无法解析"
    if s.year < MIN_DATA_YEAR:
        return f"起始时间早于 {MIN_DATA_YEAR} 年，系统没有这么早的可用影像"
    if s > e:
        return "开始时间晚于结束时间"
    if s > today:
        return "开始时间晚于今天，还没有可用影像"
    return ""


def describe_range(start: str, end: str) -> str:
    """把时间范围转成中文可读描述（气泡用）。"""
    if not start or not end:
        return "时间范围未确定"
    if start[:7] == end[:7]:
        return f"{start[:4]} 年 {int(start[5:7])} 月"
    return f"{start} 到 {end}"
