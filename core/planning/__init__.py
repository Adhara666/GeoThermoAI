# -*- coding: utf-8 -*-
"""计划编译（升级第三阶段）— 能力目录、参数快照、节点图编译器、重规划规则。

依据 docs/agent_task_orchestration_总体技术方案.md §5（从任务草稿编译为节点图）
与 docs/升级方案_通俗解读版.md 9.3（阶段 3：计划编译）。

公开入口：
  - catalog.CAPABILITY_TEMPLATES  能力目录（§5.1：允许目标/必需字段/允许参数/
                                  输入输出产物类型/模板版本/是否产生文件）
  - snapshot.build_snapshot       参数快照（§5.3：进队前冻结，逐项记来源）
  - snapshot.snapshot_hash        快照指纹
  - compiler.compile_task         任务草稿 → 运行 + 节点图落库（未知步骤校验失败）
  - compiler.validate_step_names  步骤名校验（未知步骤返回失败，不静默删除）
  - rebuild.insert_rebuild_nodes  缺输入时显式插入重建节点（§5.5）
  - replan.apply_field_change     改日期/边界/产品方式 → 新任务版本 + 新运行（§5.4）
  - replan.replan_budget          重规划/调优额度的持久读取与消耗
"""

from core.planning.catalog import CAPABILITY_TEMPLATES, NODE_TYPES, TEMPLATE_VERSION
from core.planning.compiler import (
    CompileError,
    compile_task,
    get_run_graph,
    get_run_snapshot,
    validate_step_names,
)
from core.planning.rebuild import (
    REBUILD_EDGES,
    RebuildPermissionError,
    insert_rebuild_nodes,
)
from core.planning.replan import (
    apply_field_change,
    replan_budget,
    start_superseding_run,
)

__all__ = [
    "CAPABILITY_TEMPLATES",
    "NODE_TYPES",
    "TEMPLATE_VERSION",
    "CompileError",
    "compile_task",
    "get_run_graph",
    "get_run_snapshot",
    "validate_step_names",
    "REBUILD_EDGES",
    "RebuildPermissionError",
    "insert_rebuild_nodes",
    "apply_field_change",
    "replan_budget",
    "start_superseding_run",
]
