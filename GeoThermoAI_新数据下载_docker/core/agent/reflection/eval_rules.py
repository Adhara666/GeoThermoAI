"""
评估轻反思：表述把关 E-R1 – E-R6（技术方案 7.3）

**这是评估 Agent 的核心价值：防止 AI 乱说。** 确定性规则优先，命中即打回重写。
两次重写仍不过 → 降级为模板化报告（纯指标表 + 固定口径说明），
绝不输出未通过检查的文案。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .result import Action, ReflectionResult
from .train_rules import grade

# 解读文本重写上限
EVAL_REWRITE_MAX = 2

RULES = {
    "E-R1": "文本里出现的每个指标数值都要能在结果文件里找到对应值",
    "E-R2": "不得使用禁用表述",
    "E-R3": "指标为空时不得编造数值，必须复述为空的原因",
    "E-R4": "不得仅凭十米与三十米的极值差下负面结论",
    "E-R5": "评级词必须与按分档算出的评级一致",
    "E-R6": "不得把闭合指标描述为精度",
    "E-R7": "报告必须结构完整、不得停在半句话（防止生成预算耗尽后原样输出半截稿）",
}

# 面向用户的规则短名。报告里说明「为什么降级」时用这些中文名而不是 E-Rx 编号：
# 一是编号对用户没有信息量，二是「E-R2」含子串「R2」会被 E-R1 当成决定系数关键词。
RULE_LABELS = {
    "E-R1": "数字出处",
    "E-R2": "禁用表述",
    "E-R3": "空指标不得编造",
    "E-R4": "极值差不得作为负面依据",
    "E-R5": "评级一致性",
    "E-R6": "口径不得混用",
    "E-R7": "结构完整",
}


def rule_labels(rule_hits: Any) -> List[str]:
    """把命中的规则编号转成面向用户的中文短名。"""
    return [RULE_LABELS.get(r, r) for r in (rule_hits or ())]

# E-R7：报告必备小节（对应领域知识 E09）
REQUIRED_SECTIONS = ("产品概况", "模型精度", "闭合", "局限")

# E-R7：合法的句子收尾符号
SENTENCE_ENDINGS = ("。", "！", "？", "…", "」", "）", ".", "!", "?")

# E-R7：正文长度下限（低于此值视为没写完）
MIN_REPORT_CHARS = 80

# E-R2 禁用词表。后两项只在闭合指标正常时禁止出现。
DISALLOWED_ALWAYS = ("能量守恒", "辐射守恒", "10m精度", "10 米精度", "十米精度",
                     "独立10m精度", "独立十米精度", "完全准确", "零误差")
DISALLOWED_WHEN_CLOSURE_OK = ("产品不可用", "产品质量差")

# E-R4 极值词 + 负面判断词
EXTREME_WORDS = ("最大值", "最小值", "值域", "极值", "端点")
NEGATIVE_WORDS = ("差", "不好", "不理想", "有问题", "不可靠", "不可用", "偏差大")

# E-R5 评级词
GRADE_WORDS = ("优秀", "良好", "合格", "偏低")

# E-R1 指标关键词分组（数值前若出现这些词，就必须能在允许值表里找到）
METRIC_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "r2": ("决定系数", "R²", "R2", "拟合度"),
    "rmse": ("均方根误差", "RMSE"),
    "mae": ("平均绝对误差", "MAE"),
    "mb": ("平均偏差", "MB", "整体偏差"),
    "coverage": ("覆盖率", "覆盖比例", "占比", "百分比"),
    "count": ("像元", "格网", "样本", "匹配", "景"),
}

# 数值前向后查找关键词的窗口（字符数）
_KEYWORD_WINDOW = 14

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# E-R4 闭合指标「正常」的判据（E04：block_constant 模式下 MB 应接近 0）
CLOSURE_MB_NORMAL = 0.5
CLOSURE_MAE_NORMAL = 1.0


def closure_is_normal(closure_metrics: Optional[dict]) -> bool:
    """闭合指标是否在正常区间（E04）。拿不到指标时按「未知」处理，返回 False。"""
    metrics = closure_metrics or {}
    try:
        mb = abs(float(metrics.get("MB_K")))
        mae = abs(float(metrics.get("MAE_K")))
    except (TypeError, ValueError):
        return False
    return mb <= CLOSURE_MB_NORMAL and mae <= CLOSURE_MAE_NORMAL


def build_allowed_values(bundle: Dict[str, Any]) -> Dict[str, List[float]]:
    """从真实结果里组装「允许值表」（E-R1 的比对依据）。

    bundle 形状（缺项自动跳过）：
      test_metrics / train_metrics / independent_prediction / closure / lst_stats
    """
    allowed: Dict[str, List[float]] = {k: [] for k in METRIC_KEYWORDS}

    def push(group: str, value: Any) -> None:
        try:
            allowed[group].append(float(value))
        except (TypeError, ValueError):
            pass

    for source in (bundle.get("test_metrics"), bundle.get("train_metrics")):
        source = source or {}
        push("r2", source.get("R2"))
        push("rmse", source.get("RMSE"))
        push("mae", source.get("MAE"))
        push("mb", source.get("MB"))

    # 独立预测结果可能是「指标在 metrics 子字典里」的原始形状，两种都要认，
    # 否则文本里写对了的独立预测数值会被 E-R1 误判成编造
    indep = dict(bundle.get("independent_prediction") or {})
    nested = indep.get("metrics")
    if isinstance(nested, dict):
        for key, value in nested.items():
            indep.setdefault(key, value)
    push("r2", indep.get("R2"))
    push("rmse", indep.get("RMSE_K"))
    push("mae", indep.get("MAE_K"))
    push("mb", indep.get("MB_K"))
    push("count", indep.get("n_samples"))

    closure = bundle.get("closure") or {}
    closure_metrics = closure.get("metrics") or {}
    push("r2", closure_metrics.get("R2"))
    push("rmse", closure_metrics.get("RMSE_K"))
    push("mae", closure_metrics.get("MAE_K"))
    push("mb", closure_metrics.get("MB_K"))
    push("count", closure.get("n_matched_cells"))
    ratio = closure.get("coverage_ratio")
    push("coverage", ratio)
    try:
        push("coverage", float(ratio) * 100.0)   # 允许写成百分数
    except (TypeError, ValueError):
        pass

    stats = bundle.get("lst_stats") or {}
    push("count", stats.get("total_valid"))
    push("coverage", stats.get("valid_percent"))
    size = bundle.get("image_size") or {}
    push("count", size.get("height"))
    push("count", size.get("width"))

    # 热约束残差统计（技术方案 7.1 列为评估要读的来源之一）：
    # 事实清单里给了这几个数，允许值表就必须收录，否则写对了也会被判成编造
    tcr = bundle.get("tcr_statistics") or {}
    push("mb", tcr.get("mean"))
    push("rmse", tcr.get("std"))
    push("count", tcr.get("n_valid_blocks"))

    return allowed


def _parse_number(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _decimals(raw: str) -> int:
    return len(raw.split(".")[1]) if "." in raw else 0


def _last_keyword_pos(window: str, keyword: str) -> int:
    """window 中该关键词最后一次**合法**出现的位置；找不到返回 -1。

    「R2」会作为子串出现在规则编号「E-R2」里，那不是指标关键词，必须排除，
    否则编号里的数字会被当成编造的决定系数（真实踩过的坑）。
    """
    end = len(window)
    while True:
        pos = window.rfind(keyword, 0, end)
        if pos < 0:
            return -1
        prev = window[pos - 1] if pos > 0 else ""
        if not (keyword in ("R2", "R²") and prev == "-"):
            return pos
        end = pos


def _group_for(text: str, start: int) -> Optional[str]:
    """数值前 _KEYWORD_WINDOW 个字符内最靠近的指标关键词所属分组。"""
    window = text[max(0, start - _KEYWORD_WINDOW):start]
    best_group, best_pos = None, -1
    for group, keywords in METRIC_KEYWORDS.items():
        for kw in keywords:
            pos = _last_keyword_pos(window, kw)
            if pos > best_pos:
                best_group, best_pos = group, pos
    return best_group if best_pos >= 0 else None


def check_numbers(text: str, allowed: Dict[str, List[float]]) -> List[str]:
    """E-R1：逐个核对文本里带指标关键词的数值。"""
    problems: List[str] = []
    for match in _NUMBER_RE.finditer(text or ""):
        raw = match.group(0)
        group = _group_for(text, match.start())
        if group is None:
            continue                       # 日期、分辨率等无指标关键词的数字不参与核对
        value = _parse_number(raw)
        if value is None:
            continue
        candidates = allowed.get(group) or []
        if not candidates:
            problems.append(f"文本提到{group_label(group)} {raw}，但结果文件里没有该指标")
            continue
        digits = _decimals(raw)
        tolerance = 0.5 * (10 ** -digits) + 1e-9
        if not any(abs(value - c) <= tolerance for c in candidates):
            problems.append(f"文本里的{group_label(group)} {raw} 与结果文件不一致")
    return problems


def group_label(group: str) -> str:
    return {"r2": "决定系数", "rmse": "均方根误差", "mae": "平均绝对误差",
            "mb": "平均偏差", "coverage": "覆盖率", "count": "计数"}.get(group, group)


# 显式否定词：禁用表述出现在否定语境里是允许的。
# 技术方案 7.4 要求正文必须写「不是 10 米精度，也不代表能量守恒」，
# 若把这些词一律硬禁，必需的口径说明反而写不出来。
NEGATION_WORDS = ("不是", "不代表", "并非", "而非", "不等于", "不能说", "无法保证",
                  "不宣称", "不意味")

# 判断否定词的回看窗口（字符数）
_NEGATION_WINDOW = 12


def _negated_before(text: str, index: int) -> bool:
    window = text[max(0, index - _NEGATION_WINDOW):index]
    return any(word in window for word in NEGATION_WORDS)


def _bare_occurrence(text: str, word: str) -> bool:
    """该禁用词是否存在「非否定语境」的出现。"""
    start = 0
    body = text or ""
    while True:
        index = body.find(word, start)
        if index < 0:
            return False
        if not _negated_before(body, index):
            return True
        start = index + len(word)


def check_disallowed(text: str, closure_ok: bool) -> List[str]:
    """E-R2：禁用表述（显式否定语境除外）。"""
    problems = [f"使用了禁用表述「{word}」" for word in DISALLOWED_ALWAYS
                if _bare_occurrence(text, word)]
    if closure_ok:
        problems += [f"闭合指标正常却使用了「{word}」" for word in DISALLOWED_WHEN_CLOSURE_OK
                     if _bare_occurrence(text, word)]
    return problems


def check_null_metric(text: str, r2_is_null: bool, r2_null_reason: str) -> List[str]:
    """E-R3：指标为空时不得编造数值，必须复述原因。"""
    if not r2_is_null:
        return []
    problems = []
    for match in _NUMBER_RE.finditer(text or ""):
        if _group_for(text, match.start()) == "r2":
            problems.append("决定系数无法计算时，文本仍给出了具体数值")
            break
    if r2_null_reason and r2_null_reason not in (text or ""):
        problems.append("决定系数无法计算，但文本没有说明原因")
    return problems


def check_extreme_negativity(text: str, closure_ok: bool) -> List[str]:
    """E-R4：极值差不得作为负面结论依据（E03）。"""
    if not closure_ok:
        return []
    body = text or ""
    if not any(word in body for word in EXTREME_WORDS):
        return []
    for sentence in re.split(r"[。；\n]", body):
        if any(word in sentence for word in EXTREME_WORDS) and \
                any(word in sentence for word in NEGATIVE_WORDS):
            return ["闭合指标正常时，仍以十米与三十米的极值差为依据下了负面结论"]
    return []


def check_grade(text: str, expected: str) -> List[str]:
    """E-R5：评级词必须与分档一致。"""
    if expected == "未知":
        return []
    wrong = [word for word in GRADE_WORDS if word in (text or "") and word != expected]
    if wrong:
        return [f"文本评级为「{wrong[0]}」，与按分档算出的「{expected}」不一致"]
    return []


def check_closure_wording(text: str) -> List[str]:
    """E-R6：不得把闭合指标描述为精度。

    两条豁免：
    1. 「这是均值闭合校核，不是十米独立精度」这类**显式否定**的说明是允许的；
    2. 邻近判定只在**同一句内**进行（不跨句号、分号、换行），
       否则「闭合平均偏差 0.05 开尔文；模型精度见独立预测协议」这种分句陈述会被误杀。
    """
    for sentence in re.split(r"[。；\n]", text or ""):
        for match in re.finditer(r"闭合.{0,20}?精度|精度.{0,20}?闭合", sentence):
            if any(neg in match.group(0) for neg in NEGATION_WORDS):
                continue
            return ["把闭合指标描述成了精度，两者口径不同"]
    return []


def check_structure(text: str) -> List[str]:
    """E-R7：报告结构完整且没有停在半句话。

    大模型的生成预算（`max_tokens`）耗尽时会在任意位置停下，且不带任何标记。
    E-R1 ~ E-R6 只查数字与用词，查不出「没写完」，因此单列这条：
    缺小节或末尾不是句子收尾符号，一律判为未完成，走重写/降级流程。
    """
    body = (text or "").strip()
    problems: List[str] = []
    if len(body) < MIN_REPORT_CHARS:
        problems.append(f"正文只有 {len(body)} 字，明显没有写完")
        return problems
    missing = [s for s in REQUIRED_SECTIONS if s not in body]
    if missing:
        problems.append("缺少必备小节：" + "、".join(missing))
    if not body.endswith(SENTENCE_ENDINGS):
        problems.append(f"末尾停在半句话（结尾为「{body[-12:]}」），可能是生成预算耗尽被切断")
    return problems


def check(text: str, *, bundle: Optional[dict] = None,
          expected_grade: Optional[str] = None,
          require_structure: bool = False) -> ReflectionResult:
    """跑完 E-R1 ~ E-R7，返回反思结论。

    bundle 为真实结果集合（`build_allowed_values` 的入参）。
    未通过时 `action=REWRITE`，`violations` 就是回灌给 LLM 的「修改要求」。

    `require_structure` 控制是否跑 E-R7：正式出报告的路径必须传 True；
    针对单条规则的单元测试传 False，这样短句片段不会被结构检查干扰。
    """
    bundle = bundle or {}
    allowed = build_allowed_values(bundle)
    closure = bundle.get("closure") or {}
    closure_ok = closure_is_normal(closure.get("metrics"))

    test_r2 = (bundle.get("test_metrics") or {}).get("R2")
    expected = expected_grade if expected_grade is not None else grade(test_r2)
    r2_is_null = test_r2 is None and bool(bundle.get("r2_null_reason"))

    checks = [
        ("E-R1", check_numbers(text, allowed)),
        ("E-R2", check_disallowed(text, closure_ok)),
        ("E-R3", check_null_metric(text, r2_is_null,
                                   str(bundle.get("r2_null_reason") or ""))),
        ("E-R4", check_extreme_negativity(text, closure_ok)),
        ("E-R5", check_grade(text, expected)),
        ("E-R6", check_closure_wording(text)),
        ("E-R7", check_structure(text) if require_structure else []),
    ]

    rule_hits: List[str] = []
    violations: List[str] = []
    for rule_id, problems in checks:
        if problems:
            rule_hits.append(rule_id)
            violations.extend(problems)

    if not rule_hits:
        return ReflectionResult.passed(note="结果解读通过表述检查")
    return ReflectionResult(ok=False, action=Action.REWRITE,
                            note="结果解读未通过表述检查，需要重写",
                            violations=violations, rule_hits=rule_hits,
                            data={"expected_grade": expected})
