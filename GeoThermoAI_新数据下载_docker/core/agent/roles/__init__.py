"""
四个角色 Agent（技术方案 2.2）

- base_role：带角色提示词的 LLM 调用 + JSON 三级解析 + 按角色的记忆注入
- slots：确定性槽位解析（研究区文件匹配、中文时间表达）
- planner_agent：规划（意图判定、多轮补全、出 plan、轻反思）
- data_agent：数据下载与预处理（质量评分、推荐配对、轻反思）
- train_agent：训练与调优（七规则主反思）
- eval_agent：结果生成与评估（基于记忆先验解读、表述把关）

四个角色共用一个 LLM 客户端，不引入任何多智能体框架。
本模块**不在导入期加载各角色**，避免与 geo_thermo_agent 形成循环导入。
"""

from . import slots
from .base_role import RoleAgent, extract_json

__all__ = ["RoleAgent", "extract_json", "slots"]
