"""
评估轻反思：表述把关 E-R1 – E-R7

报告的数字、评级、闭合口径句全部由系统确定性渲染，LLM 只写不含数字的定性短句，
只需过 `check_disallowed` / `check_extreme_negativity` 两项轻校验（见 eval_agent）。

本模块保留 E-R1 – E-R7 全套规则与确定性修复函数作为**已测试的工具库**：
- 数字出处（E-R1）、空指标（E-R3）、评级一致（E-R5）、口径（E-R6）、结构（E-R7）
  用于需要校验「带数字的自由文本」的场景；
- `repair_draft` 等确定性修复可在自由文本必须保留时兜底修正。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .result import Action, ReflectionResult
from .train_rules import grade

RULES = {
    "E-R1": "文本里出现的每个指标数值都要能在结果文件里找到对应值",
    "E-R2": "不得使用禁用表述",
    "E-R3": "指标为空时不得编造数值，必须复述为空的原因",
    "E-R4": "不得仅凭十米与三十米的极值差下负面结论",
    "E-R5": "评级词必须与按分档算出的评级一致",
    "E-R6": "不得把闭合指标描述为精度",
    "E-R7": "报告必须结构完整、不得停在半句话（防止生成预算耗尽后原样输出半截稿）",
}

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
# 注意不能放裸「差」：它是「温差」「差异」「差值」等合法领域词的一部分，
# 极值句里写「低端温差 -11.74 K」会被误判成负面结论。
# 负面判断用复合词表达，覆盖「偏差明显/较大/大」「质量差/表现差/精度差」等。
NEGATIVE_WORDS = ("不好", "不理想", "有问题", "不可靠", "不可用",
                  "偏差大", "偏差较大", "偏差明显", "质量差", "表现差",
                  "精度差", "很差", "较差")

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
      test_metrics / train_metrics / closure / lst_stats
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

    # 热约束残差统计（列为评估要读的来源之一）：
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
    否则编号里的数字会被当成编造的决定系数。
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


_SENTENCE_SEPS = ("。", "；", "，", "\n")


def _group_for(text: str, start: int) -> Optional[str]:
    """数值前 _KEYWORD_WINDOW 个字符内最靠近的指标关键词所属分组。

    窗口按**句内**截断：只取从最近一个句子分隔符（。；，换行）到数字前的片段，
    避免把上一句的指标关键词（如「占比 82.0%。影像 15,477 行」里的「占比」）
    误归到当前数字上——那样会把合法的行列号、像元数误判成编造。
    """
    window_start = max(0, start - _KEYWORD_WINDOW)
    window = text[window_start:start]
    for sep in _SENTENCE_SEPS:
        pos = window.rfind(sep)
        if pos >= 0:
            window = window[pos + 1:]
            break
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
            # 附上数字前文片段（最近几个字），方便 LLM 重写时精确定位到出错的那处
            start = max(0, match.start() - 12)
            context = text[start:match.start()].strip()
            context = context[-10:] if len(context) > 10 else context
            problems.append(f"文本里的{group_label(group)} {raw}"
                            f"（…{context}）与结果文件不一致")
    return problems


def group_label(group: str) -> str:
    return {"r2": "决定系数", "rmse": "均方根误差", "mae": "平均绝对误差",
            "mb": "平均偏差", "coverage": "覆盖率", "count": "计数"}.get(group, group)


# 显式否定词：禁用表述出现在否定语境里是允许的。
# 要求正文必须写「不是 10 米精度，也不代表能量守恒」，
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


# ══════════════════════════════════════════════════════════════════
# 确定性修复（根治降级）
#
# 背景：E-R1 ~ E-R7 是「防 AI 乱说」的关卡，命中即打回重写。但 LLM 对
# 数字的幻觉（如把 0.73 写成 0.75）与结构偏差（漏掉"产品概况"小节）即使
# 重写两次也未必改对，导致大量流程降级为模板报告。
#
# 因此把 E-R1 / E-R2 / E-R6 / E-R7 四类**可机械修复**的违规改为确定性
# 修复（数字对齐、禁用词替换、口径句替换、结构补齐），修复后一般直接通过；
# 重写只兜底"编造了远离真实值的数字"这类无法自动修的情形。
# ══════════════════════════════════════════════════════════════════

# E-R2 禁用表述的安全替换（保持语句通顺、不引入新错误）。
# 空字符串表示直接删除该词（能量守恒/辐射守恒只在被否定时合法，
# 裸出现属于模型乱说，删掉比保留更安全）。
_DISALLOWED_REPLACEMENTS = {
    "产品不可用": "产品存在数据空洞",
    "产品质量差": "产品存在数据空洞",
    "10m精度": "精度",
    "10 米精度": "精度",
    "十米精度": "精度",
    "独立10m精度": "精度",
    "独立十米精度": "精度",
    "完全准确": "总体一致",
    "零误差": "误差很小",
    "能量守恒": "",
    "辐射守恒": "",
}

# E-R7 缺失小节的兜底句
_SECTION_FALLBACK = {
    "产品概况": "以下为本次产品的基本信息与总体情况概述。",
    "模型精度": "各项精度指标以本次实际结果中的数值为准。",
    "闭合": "闭合情况见本次实际结果中的闭合校核数值。",
    "局限": "总体而言，本次产品的关键特征与局限需结合研究区与天气条件理解。",
}

# E-R1 修复的最大相对偏差：写错但接近真实值才修正，远离真实值的
# 编造数字（如把样本数编成 12）不做替换，交给重写/降级，避免越修越错。
_REPAIR_RELATIVE_TOL = 0.1


def repair_numbers(text: str, allowed: Dict[str, List[float]]) -> str:
    """E-R1 兜底：把带指标关键词但数值对不上允许值表的数字改成最近的真实值。

    只修复「偏差 ≤ 10% 相对误差」的近似数字（如 0.75 → 0.73）；
    偏差过大视为模型编造，不猜（保持原样，由后续重写/降级处理）。
    """
    body = text or ""
    fixes: List[Tuple[int, int, str]] = []
    for match in _NUMBER_RE.finditer(body):
        raw = match.group(0)
        group = _group_for(body, match.start())
        if group is None:
            continue
        value = _parse_number(raw)
        if value is None:
            continue
        candidates = allowed.get(group) or []
        if not candidates:
            continue
        digits = _decimals(raw)
        if any(abs(value - c) <= 0.5 * (10 ** -digits) + 1e-9 for c in candidates):
            continue
        best = min(candidates, key=lambda c: abs(value - c))
        if best == 0:
            # 真实值本身是 0（闭合 MB/MAE/RMSE、热约束残差均值常恰为 0.00）时，
            # 「偏差 ≤ 10%」的判据对 0 无意义。LLM 常把 0.00 幻觉成 0.01~0.05
            # 这类小数字：偏差不大就修回 0；偏差过大（如 0.5）可能是另一个指标
            # 的真实值，不猜。这是根治「闭合 0.00 被幻觉成 0.05 后 E-R1 必挂」的修复。
            if abs(value) <= 0.05:
                repl = f"{0:.{digits}f}" if digits else "0"
                fixes.append((match.start(), match.end(), repl))
            continue
        if abs(value - best) > _REPAIR_RELATIVE_TOL * abs(best):
            continue
        repl = f"{best:.{digits}f}" if digits else f"{int(round(best))}"
        fixes.append((match.start(), match.end(), repl))
    for start, end, repl in reversed(fixes):
        body = body[:start] + repl + body[end:]
    return body


def repair_disallowed(text: str, closure_ok: bool) -> str:
    """E-R2 兜底：把非否定语境下的禁用表述替换为安全说法。

    与 check_disallowed 的豁免一致：出现在显式否定语境（"不是…"）里的
    禁用词是允许的口径说明，不做替换。
    """
    body = text or ""
    words = list(DISALLOWED_ALWAYS)
    if closure_ok:
        words += list(DISALLOWED_WHEN_CLOSURE_OK)
    for word in words:
        if word not in body:
            continue
        repl = _DISALLOWED_REPLACEMENTS.get(word)
        if repl is None:
            continue
        pieces: List[str] = []
        start = 0
        cursor = 0
        while True:
            idx = body.find(word, cursor)
            if idx < 0:
                break
            if _negated_before(body, idx):
                cursor = idx + len(word)
                continue
            pieces.append(body[start:idx] + repl)
            cursor = idx + len(word)
            start = cursor
        if pieces:
            body = "".join(pieces) + body[start:]
    return body


def repair_closure_wording(text: str) -> str:
    """E-R6 兜底：把「闭合…精度」的非否定表述替换为「闭合校核结果」。

    只替换命中的子串、保留句子其余内容（真实数字不会因此被丢掉），
    与被修数字一起构成最终稿。
    """
    body = text or ""
    pattern = re.compile(r"闭合.{0,20}?精度|精度.{0,20}?闭合")
    parts: List[str] = []
    cursor = 0
    for match in pattern.finditer(body):
        if any(neg in match.group(0) for neg in NEGATION_WORDS):
            continue
        parts.append(body[cursor:match.start()])
        parts.append("闭合校核结果")
        cursor = match.end()
    if not parts:
        return body
    parts.append(body[cursor:])
    return "".join(parts)


def repair_structure(text: str) -> str:
    """E-R7 兜底：补缺失小节、补齐句尾标点，避免生成预算耗尽的半截稿。"""
    body = (text or "").strip()
    if not body:
        return body
    if "产品概况" not in body:
        body = "产品概况\n\n" + _SECTION_FALLBACK["产品概况"] + "\n\n" + body
    for section in REQUIRED_SECTIONS:
        if section == "产品概况" or section in body:
            continue
        body = body.rstrip() + "\n\n" + section + "\n\n" + _SECTION_FALLBACK.get(section, "")
    if not body.endswith(SENTENCE_ENDINGS):
        body = body.rstrip() + "。"
    return body


def repair_grade(text: str, expected: str) -> str:
    """E-R5 兜底：把文本里与分档不一致的评级词统一替换为 expected。

    评级词是封闭集合（优秀/良好/合格/偏低），模型写错评级（如把「偏低」写成
    「合格」）没有机械修复手段就会连续重写失败而降级。这里确定性替换：
    只要文本里出现非 expected 的评级词就改成 expected；expected 为「未知」
    （决定系数缺失）时不改动。
    """
    if not expected or expected == "未知":
        return text or ""
    body = text or ""
    for word in GRADE_WORDS:
        if word == expected or word not in body:
            continue
        body = body.replace(word, expected)
    return body


# E-R4 确定性修复：把「以极值差为依据的负面结论」句替换为领域约定 E03 的标准表述。
# 触发条件与 check_extreme_negativity 一致（闭合正常 + 同句出现极值词与负面词）。
# 替换目标是**整句**：负面句里的数字/措辞本身是违规的一部分，保句不保措辞，
# 避免「越修越错」。E03 表述不含负面判断词，替换后可通过 E-R4 复查。
_EXTREME_NEGATIVE_REPLACEMENT = (
    "十米产品的最小值更低、最大值更高，是分辨率提升的预期表现"
    "（混合像元分解与尺度效应），不是产品质量问题"
)


def repair_extreme_negativity(text: str, closure_ok: bool) -> str:
    """E-R4 兜底：把极值差负面结论句替换为 E03 的中性标准表述。"""
    if not closure_ok or not text:
        return text or ""
    parts = re.split(r"(?<=[。；\n])", text)
    out: List[str] = []
    for sentence in parts:
        if any(word in sentence for word in EXTREME_WORDS) and \
                any(word in sentence for word in NEGATIVE_WORDS):
            out.append(_EXTREME_NEGATIVE_REPLACEMENT + "。")
        else:
            out.append(sentence)
    return "".join(out)


def repair_draft(text: str, bundle: Optional[dict] = None) -> str:
    """确定性修复：数字对齐 → 禁用词替换 → 极值负面句中性化 → 闭合口径修复 →
    结构补齐 → 评级对齐。

    修复后通常直接通过全部规则；仅当仍不过（如编造了远离真实值的数字）
    才进入重写/降级流程。
    """
    if not text:
        return text
    bundle = bundle or {}
    allowed = build_allowed_values(bundle)
    closure = bundle.get("closure") or {}
    closure_ok = closure_is_normal(closure.get("metrics"))
    text = repair_numbers(text, allowed)
    text = repair_disallowed(text, closure_ok)
    text = repair_extreme_negativity(text, closure_ok)
    text = repair_closure_wording(text)
    text = repair_structure(text)
    text = repair_grade(text, grade((bundle.get("test_metrics") or {}).get("R2")))
    return text
