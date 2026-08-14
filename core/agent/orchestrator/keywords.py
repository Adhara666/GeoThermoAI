"""
关键词单一来源（I1 改进）

之前散落在 4 处的关键词列表统一到本文件，防止修改时遗漏导致行为漂移：
- geo_thermo_agent.py: _POSTPROCESS_KEYWORDS / _FULLWORKFLOW_MARKERS / _is_advisory_request
- planner_agent.py: _POSTPROCESS_KEYWORDS / _POSTPROCESS_SOFT_KEYWORDS / _FULLWORKFLOW_MARKERS / _FALLBACK_TASK_KEYWORDS
- role_flow.py: _detect_acquisition_mode_hint 内联关键词

所有模块从本文件 import，不再各自定义。
"""

import re

# ── 结果后处理 ──────────────────────────────────────────────────

# 明确的后处理关键词：命中时无论是否有已有结果，都判 postprocess
POSTPROCESS_KEYWORDS = (
    "无空洞", "空洞填补", "填补空洞", "空洞填充",
    "填洞", "补洞", "去空洞", "结果后处理", "gapfill", "gap fill",
)

# 模糊后处理关键词：仅在已有结果时才判 postprocess
# 用户跑完全流程后说"继续"、"处理一下"等，大概率是对已有结果做后处理
POSTPROCESS_SOFT_KEYWORDS = (
    "继续", "处理一下", "处理下", "补全", "修复",
    "完善", "填上", "补上", "补一下", "填一下",
    "后处理", "postprocess", "搞一下", "弄一下",
    "帮我处理", "接着处理", "继续处理",
)

# 全流程标记：命中时不算单独的后处理请求
FULLWORKFLOW_MARKERS = (
    "全流程", "一键", "跑完全流程", "执行全流程", "跑全流程", "做全流程",
)

# 新任务信号：模糊关键词命中时，如果同时含这些词则不判 postprocess
NEW_TASK_SIGNALS = (
    "下载", "训练", "生成产品", "重新跑", "再跑",
    "新项目", "换个", "另外",
)

# ── 咨询/问答判定 ──────────────────────────────────────────────

# 咨询关键词：旧路径 _is_advisory_request 用
ADVISORY_KEYWORDS = ("推荐", "参数", "原理", "是什么")

# 疑问句标记
QUESTION_MARKS = ("？", "?", "吗")
QUESTION_WORDS = ("为什么", "原因", "什么是", "是什么")

# ── 任务意图兜底关键词 ─────────────────────────────────────────

# LLM 不可用时的关键词兜底：命中任一即判 task
FALLBACK_TASK_KEYWORDS = (
    "处理", "训练", "下载", "执行", "运行", "生成",
    "全流程", "一键", "开始", "计算", "导出", "评估",
)

# ── 纠错/修正意图 ──────────────────────────────────────────────

# 用户纠正上一轮的理解（"不是武汉""改成9月""不对"）：
# 命中时 intent 不应判为 unclear（应延续 task/modify 修正）；
# 「不是X/不要X」中的 X 是被否定的内容，不得作为新槽位提取；
# 本轮明确给出的新槽位覆盖旧值。
CORRECTION_MARKERS = (
    "不是", "不对", "错了", "不是那个", "改成", "换成",
    "改为", "换个", "纠正", "重来", "说错了",
)


def is_correction_request(text: str) -> bool:
    """是否为纠错/修正请求（修正上一轮的理解）。"""
    text = (text or "").strip()
    if not text:
        return False
    return any(kw in text for kw in CORRECTION_MARKERS)


def negation_slot_terms(text: str) -> list:
    """提取「不是X/不要X/不用X」中 X（被否定的内容），供槽位排除。

    例：「不是武汉，是九江」→ ['武汉']；「不要7月，改成8月」→ ['7月']。
    只匹配否定词后紧跟的一小段（≤10 字，不含标点），避免误伤正常句子。
    """
    terms = []
    text = text or ""
    for marker in ("不是", "不要", "不用", "并非"):
        idx = 0
        while True:
            i = text.find(marker, idx)
            if i < 0:
                break
            m = re.match(r"\s*([^\s，。,.！？!?；;：:、]{1,10})",
                         text[i + len(marker):])
            if m:
                terms.append(m.group(1))
            idx = i + len(marker) + 1
    return terms

# ── 部分流程判定 ───────────────────────────────────────────────

# 部分流程关键词：命中且不含全流程标记时判 partial
PARTIAL_KEYWORDS = (
    "下载", "搜索影像", "搜索数据", "找数据", "找找数据", "找影像",
    "获取数据", "拉取", "爬取",
    "预处理", "清洗", "裁剪", "配准", "分割",
    "训练模型", "建模", "拟合", "调参", "调优",
    "导出", "输出产品",
    "评估", "精度验证",
)

# 全流程/产品标记：含这些词时不判 partial（即使同时含下载/预处理等词）
TASK_PRODUCT_MARKERS = (
    "全流程", "降尺度", "地表温度产品", "生成产品", "跑全流程",
    "做全流程", "执行全流程", "一键",
)


def is_partial_request(text: str) -> bool:
    """是否为部分流程请求（只下载/只预处理等，不含全流程意图）。

    判定逻辑：含部分流程关键词，且不含全流程/产品标记，且不是疑问句。
    """
    text = (text or "").strip()
    if not text:
        return False
    if any(m in text for m in TASK_PRODUCT_MARKERS):
        return False
    # 疑问句不判 partial（如"下载的数据源是什么？"是 qa 不是 partial）
    if text.endswith(QUESTION_MARKS) or any(w in text for w in QUESTION_WORDS):
        return False
    return any(kw in text for kw in PARTIAL_KEYWORDS)


# 续接关键词：partial 完成后用户说"继续后续流程"等，表示要跑剩余步骤
CONTINUATION_KEYWORDS = (
    "继续后续流程", "继续后续", "接着往下", "继续往下",
    "继续流程", "接着跑", "继续跑", "往下跑",
    "继续后面的", "接着后续",
)


def is_continuation_request(text: str) -> bool:
    """是否为 partial 完成后的续接请求（继续后续流程）。"""
    text = (text or "").strip()
    if not text:
        return False
    return any(kw in text for kw in CONTINUATION_KEYWORDS)


# ── 影像获取模式 ───────────────────────────────────────────────

# 月度合成模式关键词
MONTHLY_COMPOSITE_KEYWORDS = (
    "月度合成", "月合成", "按月合成", "月度模式", "合成模式",
)

# 配对模式关键词
PAIR_MODE_KEYWORDS = (
    "配对模式", "影像配对", "逐对", "按对", "配对",
)

# ── 城市bbox（I3：可配置，默认4城市） ──────────────────────────

CITY_BBOX = {
    "武汉": "113.7,29.9,114.9,31.3",
    "北京": "115.4,39.4,117.5,41.1",
    "上海": "120.8,30.6,122.2,31.9",
    "广州": "112.8,22.8,114.0,23.8",
}

# 默认bbox（找不到城市时用）
DEFAULT_BBOX = "113.7,29.9,114.9,31.3"

# ── feature 中文标签（I5：统一来源） ───────────────────────────

FEATURE_LABELS = {
    "NDVI": "植被指数",
    "NDBI": "建筑指数",
    "NDWI": "水体指数",
    "DEM": "高程",
    "TTRI": "地形热响应指数",
    "NIR": "近红外",
    "SWIR1": "短波红外",
    "R": "红光",
    "G": "绿光",
    "B": "蓝光",
}


def is_postprocess_request(text: str) -> bool:
    """是否**单独提出**的结果后处理请求（明确关键词命中）。

    疑问句/全流程/顺带提到/新任务信号 均不命中。
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.endswith(QUESTION_MARKS) or any(w in text for w in QUESTION_WORDS):
        return False
    if any(m in text for m in FULLWORKFLOW_MARKERS):
        return False
    if "包括" in text and any(kw in text for kw in POSTPROCESS_KEYWORDS):
        return False
    # 含新任务信号（重新跑/再跑/换个等）时不判 postprocess——
    # 用户明确要求重跑全流程，即使提到"无空洞"也是全流程的一部分
    if any(kw in text for kw in NEW_TASK_SIGNALS):
        return False
    if any(kw in text for kw in POSTPROCESS_KEYWORDS):
        return True
    # 兜底组合：「空洞」+ 动作词
    if "空洞" in text and any(w in text for w in ("填补", "填充", "处理",
                                                   "去掉", "去除", "补上", "生成")):
        return True
    return False


def is_postprocess_soft_request(text: str) -> bool:
    """模糊后处理请求判断（仅在已有结果时才使用）。

    排除疑问句、全流程标记、新任务信号。
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.endswith(QUESTION_MARKS) or any(w in text for w in QUESTION_WORDS):
        return False
    if any(m in text for m in FULLWORKFLOW_MARKERS):
        return False
    if any(kw in text for kw in NEW_TASK_SIGNALS):
        return False
    return any(kw in text for kw in POSTPROCESS_SOFT_KEYWORDS)


def is_advisory_request(text: str) -> bool:
    """是否为咨询/问答请求（旧路径用）。

    问句（含疑问词/以？结尾）默认视为咨询；
    含明确执行意图词时仍走计划。
    """
    text = text or ""
    if any(kw in text for kw in ADVISORY_KEYWORDS[:3]):  # 推荐/参数/原理
        return True
    if "是什么" in text:
        return True
    if (("什么" in text) or text.endswith("？") or text.endswith("?")) \
            and not any(kw in text for kw in FULLWORKFLOW_MARKERS):
        return True
    return False


def detect_acquisition_mode_hint(text: str) -> str:
    """用户指令里是否已明确指定影像获取方式。

    月度词优先于配对词。都没提到返回空串。
    """
    text = text or ""
    if any(w in text for w in MONTHLY_COMPOSITE_KEYWORDS):
        return "monthly"
    if any(w in text for w in PAIR_MODE_KEYWORDS):
        return "pair"
    return ""


def guess_city_bbox(text: str) -> str:
    """从用户输入中猜测城市bbox（旧路径兜底用）。

    命中城市名返回对应bbox，否则返回默认（武汉）。
    """
    for city, bbox in CITY_BBOX.items():
        if city in (text or ""):
            return bbox
    return DEFAULT_BBOX
