"""
记忆系统模块（RAG + 结构化混合存储）

- rag_store：ChromaDB 向量存储（项目经验 + 全局领域知识）
- experiment_log：experiments.json 精确查询层
- preferences：preferences.json 偏好键值
- session_state：对话级槽位状态（多轮补全的落盘依据）
- workflow_experience：可复用工作流经验（"靠谱流程"写回记忆）
- knowledge_eval：E 系列评估先验知识（防止 AI 乱说的依据）
- memory_manager：聚合入口（写入/注入/删除级联/播种）
"""

from .memory_manager import MemoryManager, ROLE_RETRIEVAL
from .rag_store import RAGStore, EmbeddingFunction
from .experiment_log import ExperimentLog
from .knowledge_eval import EVAL_SEED_ITEMS
from .preferences import Preferences
from .session_state import SessionState
from .workflow_experience import WORKFLOW_MIN_R2, WorkflowExperience
from .seed_data import SEED_ITEMS

__all__ = [
    "MemoryManager",
    "ROLE_RETRIEVAL",
    "RAGStore",
    "EmbeddingFunction",
    "ExperimentLog",
    "Preferences",
    "SessionState",
    "WorkflowExperience",
    "WORKFLOW_MIN_R2",
    "SEED_ITEMS",
    "EVAL_SEED_ITEMS",
]
