"""
审批节点定义、暂停载荷构造、恢复结果解析（技术方案 3.2 / 3.3）

设计要点：
- 现有 `select_pair` 载荷保持不变（前端 PairSelectCard 继续可用），只在每个 pair 上
  增加 `recommended` / `recommend_reason` 两个字段。
- 新增通用 `approval` 载荷：`{type, node, title, summary, options, default_option}`。
- `manual_tune` 的表单字段**从 Skill 的 hyperparameters 动态生成**，不在前端硬编码。
- 恢复值一律在后端做合法性校验与范围截断，绝不信任前端传来的数值。
"""

from typing import Any, Dict, List, Optional, Tuple

from .exec_mode import ExecMode, is_auto

# ── 审批节点 id ────────────────────────────────────────────────────

class Node:
    PLAN_CONFIRM = "plan_confirm"
    PAIR_SELECTION = "pair_selection"
    NO_PAIR = "no_pair"
    DATA_QUALITY = "data_quality"
    TUNING_DECISION = "tuning_decision"
    TUNING_ROUND = "tuning_round"
    FINAL_REPORT = "final_report"


ALL_NODES = (Node.PLAN_CONFIRM, Node.PAIR_SELECTION, Node.NO_PAIR, Node.DATA_QUALITY,
             Node.TUNING_DECISION, Node.TUNING_ROUND, Node.FINAL_REPORT)


# ── 选项 id ────────────────────────────────────────────────────────

class Option:
    START = "start"                 # 开始执行
    EDIT_REQUEST = "edit_request"   # 我要改需求
    AI_TUNE = "ai_tune"             # 让系统继续自动调优
    MANUAL_TUNE = "manual_tune"     # 我自己设置参数
    ACCEPT = "accept"               # 接受当前结果，继续下一步
    NEXT_ROUND = "next_round"       # 继续下一轮
    STOP_TUNING = "stop_tuning"     # 停止调优
    RESELECT_PAIR = "reselect_pair"  # 重新选择影像组合
    REPLAN = "replan"               # 换时间或地区，重新规划
    RELAX_CLOUD = "relax_cloud"     # 放宽云量阈值
    WIDEN_TIME = "widen_time"       # 扩大时间范围
    CHANGE_SOURCE = "change_source"  # 换数据源
    STOP = "stop"                   # 停止
    DONE = "done"                   # 结束
    MORE_ANALYSIS = "more_analysis"  # 做其他分析


# AUTO（完全执行）模式下各节点的默认策略（技术方案 3.2 最后一列）。
# 值为 None 表示该节点在 AUTO 模式下**根本不暂停**（由规则自动决定）。
AUTO_DEFAULT_STRATEGY: Dict[str, Optional[str]] = {
    Node.PLAN_CONFIRM: Option.START,
    Node.PAIR_SELECTION: None,        # 自动选质量得分最高的一组
    Node.NO_PAIR: Option.REPLAN,      # 带原因交规划 Agent replan（≤REPLAN_MAX 次）
    Node.DATA_QUALITY: Option.REPLAN,
    Node.TUNING_DECISION: Option.AI_TUNE,
    Node.TUNING_ROUND: None,          # 按七规则自动决定，不暂停
    Node.FINAL_REPORT: Option.DONE,
}

# 手动调参表单排除的超参：随机种子是可复现性开关，不是调优旋钮
_MANUAL_TUNE_EXCLUDED = {"random_state"}


def should_pause(node: str, exec_mode: str) -> bool:
    """该节点在当前模式下是否需要真的暂停询问用户。

    由我批准模式下所有节点都要停（含 tuning_decision / tuning_round，
    无论精度好坏都要询问并报告结果——技术方案 3.2 的用户明确要求）。
    """
    if not is_auto(exec_mode):
        return True
    return False


def auto_choice(node: str) -> Optional[str]:
    """AUTO 模式下该节点的默认选项 id；None 表示不暂停、由规则自动决定。"""
    return AUTO_DEFAULT_STRATEGY.get(node)


# ── 载荷构造 ───────────────────────────────────────────────────────

def option(option_id: str, label: str, recommended: bool = False,
           hint: str = "", fields: Optional[List[dict]] = None) -> dict:
    item: Dict[str, Any] = {"id": option_id, "label": label}
    if recommended:
        item["recommended"] = True
    if hint:
        item["hint"] = hint
    if fields:
        item["fields"] = list(fields)
    return item


def build(node: str, title: str, summary: str, options: List[dict],
          default_option: str = "") -> dict:
    """组装通用审批载荷。default_option 缺省时取第一个 recommended，否则第一项。"""
    if not options:
        raise ValueError("审批载荷至少需要一个选项")
    if not default_option:
        recommended = [o["id"] for o in options if o.get("recommended")]
        default_option = recommended[0] if recommended else options[0]["id"]
    return {
        "type": "approval",
        "node": node,
        "title": title,
        "summary": summary,
        "options": options,
        "default_option": default_option,
    }


def hyperparameter_fields(skill: Any) -> List[dict]:
    """从 Skill 的 hyperparameters 动态生成表单字段（技术方案 3.3）。"""
    fields: List[dict] = []
    for hp in getattr(skill, "hyperparameters", []) or []:
        name = getattr(hp, "name", "")
        if not name or name in _MANUAL_TUNE_EXCLUDED:
            continue
        field = {
            "name": name,
            "label": getattr(hp, "label", name),
            "type": getattr(hp, "type", "number"),
            "default": getattr(hp, "default", None),
        }
        for attr in ("min", "max", "step"):
            value = getattr(hp, attr, None)
            if value is not None:
                field[attr] = value
        description = getattr(hp, "description", "")
        if description:
            field["description"] = description
        options = getattr(hp, "options", None)
        if options:
            field["options"] = list(options)
        fields.append(field)
    return fields


def build_plan_confirm(goal: str, summary: str = "") -> dict:
    return build(
        Node.PLAN_CONFIRM,
        title="执行方案已就绪，请确认是否开始",
        summary=summary or goal,
        options=[
            option(Option.START, "开始执行", recommended=True,
                   hint="按上面的方案依次执行各个步骤"),
            option(Option.EDIT_REQUEST, "我要改需求",
                   hint="回到对话，重新说明研究区、时间或产品要求"),
        ],
    )


def build_no_pair(summary: str) -> dict:
    return build(
        Node.NO_PAIR,
        title="没有找到合格的影像组合，请选择下一步",
        summary=summary,
        options=[
            option(Option.RELAX_CLOUD, "放宽云量要求", recommended=True,
                   hint="允许云量更高的影像进入候选"),
            option(Option.WIDEN_TIME, "扩大时间范围",
                   hint="向前后各延长搜索窗口"),
            option(Option.CHANGE_SOURCE, "更换数据源",
                   hint="改用另一个数据平台重新搜索"),
            option(Option.REPLAN, "换时间或地区，重新规划"),
            option(Option.STOP, "先停下来"),
        ],
    )


def build_data_quality(summary: str) -> dict:
    return build(
        Node.DATA_QUALITY,
        title="数据检查未通过，请选择下一步",
        summary=summary,
        options=[
            option(Option.RESELECT_PAIR, "重新选择影像组合", recommended=True,
                   hint="回到影像组合选择，换一组云量更低的重跑"),
            option(Option.CHANGE_SOURCE, "更换数据源"),
            option(Option.REPLAN, "换时间或地区，重新规划"),
            # v1.2 新增：检查规则本身也可能有误判，用户比系统更清楚这批数据能不能用
            option(Option.ACCEPT, "我接受现状，继续执行",
                   hint="忽略本次检查未通过的提示，直接进入模型训练"),
            option(Option.STOP, "先停下来"),
        ],
    )


def build_tuning_decision(summary: str, fields: Optional[List[dict]] = None,
                          max_rounds: int = 5) -> dict:
    return build(
        Node.TUNING_DECISION,
        title="模型训练完成，请选择下一步",
        summary=summary,
        options=[
            option(Option.AI_TUNE, "让系统继续自动调优", recommended=True,
                   hint=f"在硬性规则约束下最多再训练 {max_rounds} 轮，自动选取误差最小的一轮"),
            option(Option.MANUAL_TUNE, "我自己设置参数", fields=fields or []),
            option(Option.ACCEPT, "接受当前结果，继续下一步"),
            option(Option.RESELECT_PAIR, "重新选择影像组合"),
            option(Option.REPLAN, "换时间或地区，重新规划"),
        ],
    )


def build_tuning_round(summary: str, is_last_round: bool = False) -> dict:
    options = [option(Option.ACCEPT, "接受本轮结果", recommended=is_last_round)]
    if not is_last_round:
        options.append(option(Option.NEXT_ROUND, "继续下一轮", recommended=True,
                              hint="按规则给出的方向再调一次参数并重训"))
    options.append(option(Option.STOP_TUNING, "停止调优，取目前最好的一轮"))
    return build(Node.TUNING_ROUND, title="本轮调优完成，请选择下一步",
                 summary=summary, options=options)


def build_final_report(summary: str) -> dict:
    return build(
        Node.FINAL_REPORT,
        title="全流程已完成",
        summary=summary,
        options=[
            option(Option.DONE, "结束", recommended=True),
            option(Option.MORE_ANALYSIS, "做其他分析",
                   hint="回到对话，继续提出新的分析需求"),
        ],
    )


# ── 载荷与恢复值校验 ───────────────────────────────────────────────

def validate_payload(payload: Any) -> List[str]:
    """校验审批载荷 schema 合法性（`test_exec_mode_approval.py` 逐条断言）。"""
    issues: List[str] = []
    if not isinstance(payload, dict):
        return ["审批载荷必须是对象"]
    if payload.get("type") != "approval":
        issues.append("type 必须是 approval")
    if payload.get("node") not in ALL_NODES:
        issues.append(f"未知的审批节点：{payload.get('node')}")
    for key in ("title", "summary"):
        if not isinstance(payload.get(key), str):
            issues.append(f"{key} 必须是字符串")
    options = payload.get("options")
    if not isinstance(options, list) or not options:
        issues.append("options 必须是非空数组")
        return issues
    ids = []
    for i, item in enumerate(options):
        if not isinstance(item, dict):
            issues.append(f"第 {i + 1} 个选项不是对象")
            continue
        if not item.get("id"):
            issues.append(f"第 {i + 1} 个选项缺少 id")
        if not item.get("label"):
            issues.append(f"第 {i + 1} 个选项缺少 label")
        ids.append(item.get("id"))
        for field in item.get("fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                issues.append(f"选项 {item.get('id')} 的表单字段缺少 name")
    if len(set(ids)) != len(ids):
        issues.append("选项 id 重复")
    if payload.get("default_option") not in ids:
        issues.append("default_option 必须是 options 中的某个 id")
    return issues


def find_option(payload: dict, option_id: str) -> Optional[dict]:
    for item in (payload or {}).get("options") or []:
        if item.get("id") == option_id:
            return item
    return None


def _clamp_field(field: dict, raw: Any) -> Any:
    """按字段声明截断到安全区间；不可解析时回落默认值。"""
    default = field.get("default")
    if field.get("type") == "boolean":
        return bool(raw)
    if field.get("type") == "select":
        allowed = field.get("options") or []
        return raw if raw in allowed else default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    low, high = field.get("min"), field.get("max")
    if low is not None:
        value = max(float(low), value)
    if high is not None:
        value = min(float(high), value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(round(value))
    return value


def sanitize_values(payload: dict, option_id: str, values: Any) -> Dict[str, Any]:
    """按所选选项的字段声明清洗用户输入：只保留声明过的字段并做范围截断。"""
    chosen = find_option(payload, option_id)
    fields = (chosen or {}).get("fields") or []
    raw = values if isinstance(values, dict) else {}
    cleaned: Dict[str, Any] = {}
    for field in fields:
        name = field.get("name")
        if not name:
            continue
        cleaned[name] = _clamp_field(field, raw.get(name, field.get("default")))
    return cleaned


def parse_resume(payload: dict, resume: Any) -> Tuple[Optional[dict], str]:
    """解析前端恢复载荷，返回 (结果, 错误信息)。

    结果形状：`{"option_id": "...", "values": {...}}`；非法选项返回错误信息。
    """
    if not isinstance(payload, dict):
        return None, "没有待处理的选择，请重新发送指令"
    resume = resume if isinstance(resume, dict) else {}
    option_id = str(resume.get("option_id") or "")
    if find_option(payload, option_id) is None:
        return None, "无效的选项"
    return {
        "option_id": option_id,
        "values": sanitize_values(payload, option_id, resume.get("values")),
    }, ""


def auto_resume(payload: dict) -> Dict[str, Any]:
    """AUTO 模式下不暂停，直接按 default_option 组装一份等效的恢复结果。"""
    option_id = payload.get("default_option") or ""
    return {
        "option_id": option_id,
        "values": sanitize_values(payload, option_id, None),
    }
