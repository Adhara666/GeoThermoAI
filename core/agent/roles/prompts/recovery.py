"""失败自愈诊断提示词（对口角色的一次性结构化诊断）。

只输出 JSON：{"root_cause", "action", "reason"}；
action 必须来自候选动作白名单（由调用方注入）；
人物设定：以对口角色的职责视角做判断（Data=数据质量、Train=训练、
Eval=评估），不写代码、不改科学设定。
"""


def diagnose_prompt(*, role: str, node_type: str, error_class: str,
                    error_text: str, attempts: int = 1, region: str = "",
                    time_range: str = "", reference: str = "",
                    candidates: list = None) -> str:
    role_names = {"data": "Data·数据与质检", "train": "Train·训练与温度生成",
                  "eval": "Eval·评估与复核"}
    role_name = role_names.get(role, role)
    cand = "、".join(f"`{c}`" for c in (candidates or ["retry", "escalate"]))
    ref_line = f"\n已知参考：{reference}" if reference else ""
    return f"""你是一名地表温度降尺度系统的执行诊断员，现在以「{role_name}」角色的
职责视角，为一次节点执行失败给出简短诊断。

背景：
- 失败节点：{node_type}
- 错误分类：{error_class}
- 错误信息：{error_text}
- 已尝试次数：{attempts}
- 研究区：{region or "（未记录）"}
- 时间范围：{time_range or "（未记录）"}{ref_line}

候选处置动作（只能从这些里选，含义如下）：
- `retry`：原样重试（适用于网络抖动、瞬态故障）
- `retry_cloud_plus`：放宽影像检索云量阈值后重试（适用于"找不到合格影像/配对"；只放宽阈值，不改研究区与时间）
- `escalate`：不自动处置，转交用户决定（适用于需要改科学设定或情况不明）

要求：
1. 先用一两句给出根因判断（root_cause）；
2. 再从候选中选择最合适的一个（action）；
3. 用一句人话说明为什么（reason）。
严格只输出如下 JSON（不要 markdown 代码块、不要多余文字）：
{{"root_cause": "...", "action": "{ (candidates or ['retry'])[0] }", "reason": "..."}}
（action 取值仅限：{cand}）"""
