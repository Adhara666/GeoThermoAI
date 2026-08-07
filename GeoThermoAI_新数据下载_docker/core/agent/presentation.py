"""
气泡文案渲染层（技术方案 9.4）

## 实现红线（不得违反，`tests/test_presentation.py` 逐条断言）

1. 气泡文案一律中文，不出现英文技能名（如 data_acquisition）、变量名（如 train_csv）、
   JSON、文件路径、堆栈。
2. 气泡文案不使用表情符号。状态用中文词表达：开始 / 完成 / 未通过 / 已暂停 / 已停止。
3. 数字保留，但要带中文单位与含义：测试集决定系数 0.87、均方根误差 1.23 开尔文、
   有效像元 4,231,905 个。
4. 一切技术细节（路径、参数字典、进度百分比、原始报错）只走 on_log 进日志面板，
   不进气泡。执行引擎的 `_emit(text, to_log=True)` 机制已支持。
5. 每个阶段的气泡最多三行：一行「第 N 步／共 M 步：中文阶段名」、一行阶段说明、
   一行结果摘要。
6. 任何长文本截断都必须落在句子边界上，禁止 `text[:N]` 硬切——气泡是后端拼好的完整
   字符串，不受模型输出预算限制，出现半截话会被误认为「回复被截断」。
   需要限长时用 `eval_agent._cut_at_sentence` 那样按句号/分号切的方式。

本模块是**阶段中文名的单一来源**：`server._WORKFLOW_LABELS` 与执行引擎的阶段说明
都从这里取，避免同一阶段两处中文名不一致。
"""

import re
from typing import Any, Dict, List, Optional

# ── 阶段中文名与说明（单一来源） ────────────────────────────────────

STAGE_LABELS: Dict[str, str] = {
    "data_acquisition": "数据获取",
    "data_pipeline": "数据预处理",
    "ttri_compute": "地形热响应指数计算",
    "rf_model": "模型训练",
    "tcr_compute": "热约束残差计算",
    "lst_export": "地表温度产品导出",
    "accuracy_eval": "闭合精度校核",
    "ai_assistant": "智能分析",
}

# server 侧工作流面板用的短标签（沿用队友现有面板文案，不改动既有显示）
WORKFLOW_LABELS: Dict[str, str] = {
    "data_acquisition": "数据获取",
    "data_pipeline": "数据预处理",
    "ttri_compute": "TTRI 计算",
    "rf_model": "模型训练",
    "tcr_compute": "TCR 计算",
    "lst_export": "LST 导出",
    "accuracy_eval": "精度评估",
}

# 平移期（P0–P5）沿用的旧阶段说明，与改造前 `geo_thermo_agent._STEP_DESCRIPTIONS`
# 逐字一致，保证 P0 是纯平移。P6 文案改写完成后由 STAGE_DESCRIPTIONS 取代。
LEGACY_STEP_DESCRIPTIONS: Dict[str, str] = {
    "data_acquisition": "下载 Landsat 8/9、Sentinel-2 L2A 与 DEM 影像",
    "data_pipeline": "预处理并划分数据集：生成 30m 训练数据、完整约束层与 10m 预测数据",
    "ttri_compute": "拟合地形校正（TTRI）系数并空间化到 30m/10m 网格",
    "rf_model": "训练随机森林降尺度模型并输出独立精度评价",
    "tcr_compute": "计算地形校正残差（TCR）",
    "lst_export": "计算最终 10m 地表温度并导出 GeoTIFF",
    "accuracy_eval": "粗尺度闭合精度评估",
}

STAGE_DESCRIPTIONS: Dict[str, str] = {
    "data_acquisition": "搜索并下载陆地卫星、哨兵二号与数字高程数据",
    "data_pipeline": "预处理并划分数据集，生成训练数据、约束层与预测格网",
    "ttri_compute": "拟合地形热响应指数并空间化到粗细两套格网",
    "rf_model": "训练降尺度模型并输出独立预测精度",
    "tcr_compute": "计算热约束残差，修正跨尺度系统性偏差",
    "lst_export": "计算最终十米地表温度并导出栅格产品",
    "accuracy_eval": "粗尺度均值闭合校核",
    "ai_assistant": "根据当前结果做智能分析",
}

# 阶段状态的中文词（红线 2：不使用表情符号）
STATUS_WORDS = {
    "running": "开始",
    "completed": "完成",
    "failed": "未通过",
    "paused": "已暂停",
    "aborted": "已停止",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002190-\U000021FF"
    "\U00002300-\U000023FF"
    "\U00002460-\U000024FF"
    "\U000025A0-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)

# 英文技能名 → 中文阶段名（红线 1：气泡里不许出现英文技能名）
_SKILL_NAME_RE = re.compile("|".join(sorted(STAGE_LABELS, key=len, reverse=True)))

# 形如 /a/b/c.tif、D:\a\b、./output/raw 的路径片段。
# 分隔符前必须是「行首、空白或标点」，不能紧跟字母数字：否则 row/col、MB/MAE、
# 训练/验证 这类正常写法会被当成路径吃掉（真实踩过的坑：诊断信息里的
# 「row/col 越界」被替换成「row（详见日志） 越界」，读者完全看不懂）。
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9\u4e00-\u9fff])(?:[A-Za-z]:[\\/][^\s，。；：]*|\.{0,2}[\\/][^\s，。；：]{2,})"
)

# 形如 train_csv、meta_30m_json 的变量名（含下划线的纯 ASCII 标识符）
_VARNAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")


def strip_emoji(text: str) -> str:
    """移除表情符号与装饰性符号（红线 2）。"""
    if not text:
        return ""
    return _EMOJI_RE.sub("", text)


def sanitize(text: str) -> str:
    """把任意来源文本收敛到气泡可用形态：去表情、去路径、技能名中文化、去变量名。

    只用于兜底（没有结构化数据可用时），正常路径应走 `summarize`。
    """
    if not text:
        return ""
    out = strip_emoji(str(text))
    out = _PATH_RE.sub("（详见日志）", out)
    out = _SKILL_NAME_RE.sub(lambda m: STAGE_LABELS.get(m.group(0), m.group(0)), out)
    out = _VARNAME_RE.sub("相关数据", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# ── 数字格式化（红线 3） ────────────────────────────────────────────

def fmt_count(value: Any) -> str:
    """整数千分位；不可解析时返回「未知」。"""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "未知"


def fmt_num(value: Any, digits: int = 2, missing: str = "未计算") -> str:
    """浮点数定点格式化；None/非数值返回 missing。"""
    if value is None:
        return missing
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return missing


def fmt_percent(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "未知"


def stage_label(skill_name: str) -> str:
    return STAGE_LABELS.get(skill_name, "处理步骤")


def stage_description(skill_name: str) -> str:
    return STAGE_DESCRIPTIONS.get(skill_name, "")


def step_header(index: int, total: int, skill_name: str) -> str:
    """阶段开始的气泡头（红线 5：第一行 + 第二行说明）。

    使用全角斜线「／」而非 ASCII 斜线，避免与文件路径分隔符混淆。
    """
    head = f"**第 {index} 步／共 {total} 步：{stage_label(skill_name)}**"
    desc = stage_description(skill_name)
    return head + ("\n" + desc + "\n" if desc else "\n")


# ── 各 Skill 结果摘要（红线 3、5） ──────────────────────────────────

def _split_counts(data: Dict[str, Any]) -> Dict[str, Any]:
    stats = data.get("split_stats") or {}
    return {
        "train": (stats.get("train") or {}).get("count"),
        "validate": (stats.get("validate") or {}).get("count"),
        "test": (stats.get("test") or {}).get("count"),
    }


def _summarize_data_acquisition(data: Dict[str, Any]) -> str:
    pairs = data.get("image_pairs") or []
    if pairs:
        return f"影像检索完成：找到 {fmt_count(len(pairs))} 组可用的影像组合"
    if data.get("landsat_path"):
        return "影像下载完成：陆地卫星地表温度、哨兵二号多光谱与数字高程数据均已就位"
    return "影像检索完成"


def _summarize_data_pipeline(data: Dict[str, Any]) -> str:
    c = _split_counts(data)
    if c["train"] is not None:
        return (f"数据准备完成：训练样本 {fmt_count(c['train'])} 个，"
                f"验证 {fmt_count(c['validate'])} 个，测试 {fmt_count(c['test'])} 个")
    return f"数据准备完成：训练样本 {fmt_count(data.get('train_rows'))} 个"


def _summarize_ttri(data: Dict[str, Any]) -> str:
    coef = data.get("coefficients") or {}
    r2 = coef.get("r2") if isinstance(coef, dict) else None
    valid = data.get("total_valid")
    parts = ["地形热响应指数计算完成"]
    detail = []
    if r2 is not None:
        detail.append(f"地形拟合决定系数 {fmt_num(r2)}")
    if valid is not None:
        detail.append(f"十米有效格点 {fmt_count(valid)} 个")
    return parts[0] + ("：" + "，".join(detail) if detail else "")


def _summarize_rf_model(data: Dict[str, Any]) -> str:
    test = data.get("test_metrics") or {}
    return (f"模型训练完成：测试集决定系数 {fmt_num(test.get('R2'))}，"
            f"均方根误差 {fmt_num(test.get('RMSE'))} 开尔文")


def _summarize_tcr(data: Dict[str, Any]) -> str:
    stats = data.get("tcr_statistics") or {}
    return (f"热约束残差计算完成：平均残差 {fmt_num(stats.get('mean'))} 开尔文，"
            f"有效格网 {fmt_count(stats.get('n_valid_blocks'))} 个")


def _summarize_lst_export(data: Dict[str, Any]) -> str:
    size = data.get("image_size") or {}
    stats = data.get("stats") or {}
    return (f"地表温度产品导出完成：影像 {fmt_count(size.get('height'))} 行 × "
            f"{fmt_count(size.get('width'))} 列，有效像元占比 "
            f"{fmt_percent(stats.get('valid_percent'))}")


def _summarize_accuracy_eval(data: Dict[str, Any]) -> str:
    full = data.get("closure_metrics") or {}
    closure = full.get("closure") or {}
    metrics = closure.get("metrics") or {}
    return (f"闭合校核完成：平均偏差 {fmt_num(metrics.get('MB_K'))} 开尔文，"
            f"平均绝对误差 {fmt_num(metrics.get('MAE_K'))} 开尔文，共比对 "
            f"{fmt_count(closure.get('n_matched_cells'))} 个格网"
            f"（这是均值闭合校核，不是十米独立精度）")


_SUMMARIZERS = {
    "data_acquisition": _summarize_data_acquisition,
    "data_pipeline": _summarize_data_pipeline,
    "ttri_compute": _summarize_ttri,
    "rf_model": _summarize_rf_model,
    "tcr_compute": _summarize_tcr,
    "lst_export": _summarize_lst_export,
    "accuracy_eval": _summarize_accuracy_eval,
    "ai_assistant": lambda data: "智能分析完成",
}


def summarize(skill_name: str, result: Any) -> str:
    """把 SkillResult 转写为一行中文结果摘要（红线 1、2、3）。

    优先读结构化 `result.data`；失败时退回对原始 message 做 `sanitize`。
    """
    success = bool(getattr(result, "success", True))
    message = getattr(result, "message", "") or ""
    data = getattr(result, "data", None)
    data = data if isinstance(data, dict) else {}

    if not success:
        return f"{stage_label(skill_name)}未通过：{sanitize(message) or '未给出原因'}"

    fn = _SUMMARIZERS.get(skill_name)
    if fn is None:
        return f"{stage_label(skill_name)}完成"
    try:
        return fn(data)
    except Exception:
        return f"{stage_label(skill_name)}完成：{sanitize(message)}"


# ── 其它气泡文案（执行引擎与角色 Agent 共用） ──────────────────────

def study_area_loaded(name: str) -> str:
    """已载入研究区（去掉扩展名，只给中文可读名）。"""
    clean = str(name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for ext in (".geojson", ".json", ".shp", ".kml", ".gpkg"):
        if clean.lower().endswith(ext):
            clean = clean[: -len(ext)]
            break
    return f"已载入研究区：{clean}\n"


def planning_started() -> str:
    return "正在理解你的需求并规划执行步骤\n"


def planning_retry() -> str:
    return "规划结果需要修正，正在重新整理\n"


def planning_fallback() -> str:
    return "改用标准全流程继续执行\n"


def plan_ready(step_count: int) -> str:
    return f"执行方案已确定，共 {step_count} 步\n"


def plan_completed_by_safety_net() -> str:
    return "已补全为完整流程\n"


def tuning_params_suggesting() -> str:
    return "正在根据数据特征推荐训练参数\n"


def pairs_found(count: int) -> str:
    return f"找到 {fmt_count(count)} 组可用的影像组合\n"


def pair_auto_selected(index: int, reason: str = "") -> str:
    tail = f"：{reason}" if reason else ""
    return f"已自动选择第 {index} 组{tail}\n"


def download_started() -> str:
    return "**开始下载所选影像**\n"


def waiting_for_user() -> str:
    return "已暂停，等待你的选择\n"


def skill_missing(skill_name: str) -> str:
    return f"该步骤（{stage_label(skill_name)}）暂不可用，已跳过\n"


def no_pair_reason(detail: Dict[str, Any]) -> str:
    """无合格配对时说清「搜到了什么、为什么都不合格」（技术方案 5.2）。"""
    detail = detail or {}
    lines = [
        f"没有找到符合条件的影像组合：陆地卫星 {fmt_count(detail.get('landsat_count'))} 景，"
        f"哨兵二号 {fmt_count(detail.get('sentinel_count'))} 景。"
    ]
    reasons: List[str] = []
    if detail.get("rejected_by_coverage"):
        reasons.append(f"覆盖不足被淘汰 {fmt_count(detail['rejected_by_coverage'])} 组")
    if detail.get("rejected_by_time_diff"):
        reasons.append(f"成像时间相差过大被淘汰 {fmt_count(detail['rejected_by_time_diff'])} 组")
    if detail.get("cloud_threshold") is not None:
        reasons.append(f"当前云量阈值为 {fmt_count(detail['cloud_threshold'])}")
    if reasons:
        lines.append("原因：" + "；".join(reasons) + "。")
    return "\n".join(lines) + "\n"


def rule_note(rule_id: str, text: str) -> str:
    """规则覆盖 LLM 决策时的可追溯标注（技术方案 13.2）。"""
    return f"[规则] {rule_id} {text}"
