"""
结果解读提示词

报告的数字、评级、闭合口径句全部由系统确定性渲染（见 eval_agent.assemble_report），
LLM 只负责生成「关键特征与局限」一节的 1~3 句**定性**说明。因此提示词禁止出现
任何数字/指标数值——数字不经过 LLM 之手，E-R1 数字把关自然不需要了。
E01–E09 全文由 `memory.enrich_for_role(project_id, "eval", query)` 注入；
记忆不可用时用 `knowledge_eval.eval_knowledge_text()` 兜底，保证口径不退化。
"""

EVAL_IDENTITY = """你是地表温度降尺度领域的研究人员，正在为一次已完成的生产任务撰写结果说明。
你只负责：为「关键特征与局限」一节写 1~3 句定性说明，基于给定的真实结果作概括。
你禁止：重算或引用任何指标数值（决定系数、误差、像元数、温度值、占比等一律不写）；
        使用「能量守恒」「辐射守恒」「产品不可用」「产品质量差」这类说法；
        因10m与30m的极值差贬低产品；
        出现文件路径、变量名、英文技能名、表情符号。"""


EVAL_QUALITATIVE_PROMPT = """{identity}

## 必须遵守的领域约定
{knowledge}

## 本次实际结果（只能用于概括判断，不得引用其中的任何数值）
{facts}

## 写作要求
- 只写「关键特征与局限」这一节的内容，不写章节标题，共 1~3 句
- 每句都是定性概括：可以谈贡献最大的特征、分辨率尺度效应、云掩膜/空洞区域的局限、
  决定系数无法计算时的原因（若上方事实清单提到）等
- 全文不得出现任何指标数值（决定系数、误差、像元数、温度值、占比等一律不写）；
  需要举例时不写具体数值；分辨率单位一律写 10m/30m，不要写「十米/三十米」
- 不得使用「能量守恒」「辐射守恒」「产品不可用」「产品质量差」等措辞
- 10m产品极值范围比30m宽时，按领域约定 E03 说明这是分辨率提升的正常表现，
  不得据此贬低产品
- 只用中文，以句号结尾，不出现文件路径、变量名、英文技能名、表情符号
"""


def qualitative_prompt(knowledge: str, facts: str, expected_grade: str) -> str:
    return EVAL_QUALITATIVE_PROMPT.format(identity=EVAL_IDENTITY, knowledge=knowledge,
                                          facts=facts, expected_grade=expected_grade)
