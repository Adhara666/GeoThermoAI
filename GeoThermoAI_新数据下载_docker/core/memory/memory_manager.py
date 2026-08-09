"""
记忆系统聚合入口（MemoryManager）

职责：
- 按用户初始化（memory 根目录 = data/users/{uid}/memory）
- 幂等播种全局领域知识（knowledge_seed.json 落盘 + ChromaDB global_knowledge）
- 实验写入：experiments.json（精确查询） + ChromaDB project_{id}（语义检索）双写
- 读取：enrich_prompt 注入三段内容（历史经验 RAG / 领域知识 RAG / 历史最佳 JSON）
- 删除级联：delete_conversation / delete_project
- 偏好：preferences.json 键值读写

约定：所有写失败仅告警（logging.warning），绝不抛给 Agent 主流程。
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .rag_store import RAGStore, EmbeddingFunction
from .experiment_log import ExperimentLog
from .knowledge_eval import EVAL_IDS
from .preferences import Preferences
from .session_state import SessionState
from .workflow_experience import WorkflowExperience, record_to_paragraph
from . import seed_data

logger = logging.getLogger(__name__)

# 领域知识检索为空时的兜底条目（保证 Agent 基础领域知识不退化）
_FALLBACK_KNOWLEDGE_IDS = ["K01", "K02", "K03", "K04", "K06", "K07", "K10", "K11", "K12",
                           "K13", "K20", "K21", "K22", "K23", "K24"]

# 按角色定制的检索范围（技术方案 8.4c）。
# 过滤键用 `domain` / `kid` 这类**标量**字段：tags 在 metadata 里存成逗号拼接串，
# `$in` 对它做不了包含匹配。
ROLE_RETRIEVAL: Dict[str, Dict[str, Any]] = {
    "planner": {"knowledge_n": 6, "experience_n": 3, "include_best": True,
                "knowledge_where": None, "experience_where": None,
                "include_workflows": True, "include_preferences": True},
    "data": {"knowledge_n": 5, "experience_n": 2, "include_best": False,
             "knowledge_where": {"domain": "data"}},
    "train": {"knowledge_n": 5, "experience_n": 3, "include_best": True,
              "knowledge_where": {"domain": "model"}},
    "eval": {"knowledge_n": 9, "experience_n": 2, "include_best": False,
             "knowledge_where": {"kid": {"$in": list(EVAL_IDS)}}},
}


class MemoryManager:
    """按用户聚合的记忆读写入口。"""

    def __init__(self, memory_root: str, embedding_model_dir: str = ""):
        self._root = Path(memory_root)
        self._chroma_dir = self._root / "chromadb"
        self._projects_dir = self._root / "projects"
        self._sessions_dir = self._root / "sessions"
        self._rag = RAGStore(str(self._chroma_dir),
                             embedding=EmbeddingFunction(model_dir=embedding_model_dir))

    # ── 目录 / 对象工厂 ────────────────────────────────────────────

    def project_memory_dir(self, project_id: str) -> Path:
        return self._projects_dir / project_id

    def experiment_log(self, project_id: str) -> ExperimentLog:
        return ExperimentLog(str(self.project_memory_dir(project_id) / "experiments.json"))

    def load_used_pairs(self, project_id: str) -> set:
        """该项目历史使用过的影像对 key 集合（升级点 1/12）。

        从 experiments.json 提取每次实验的 pair（landsat_date + sentinel2_date），
        用于「已尝试过的影像对不再作为推荐 / 不再提示换对」。key 形如
        ``20240701_20240702``（Landsat 日期_Sentinel 日期）。
        """
        if not project_id:
            return set()
        keys = set()
        try:
            for rec in self.experiment_log(project_id).all():
                pair = rec.get("pair")
                if not isinstance(pair, dict):
                    continue
                l = str(pair.get("landsat_date") or "").replace("-", "")[:8]
                s = str(pair.get("sentinel2_date") or "").replace("-", "")[:8]
                if l and s:
                    keys.add(f"{l}_{s}")
        except Exception as e:
            logger.warning(f"[memory] 读取已使用配对失败: {e}")
        return keys

    def preferences(self, project_id: str) -> Preferences:
        return Preferences(str(self.project_memory_dir(project_id) / "preferences.json"))

    def session_state(self, conv_id: str, project_id: str = "") -> SessionState:
        """对话级槽位状态（技术方案 8.2）。"""
        return SessionState(str(self._sessions_dir / f"{conv_id}.json"),
                            conv_id=conv_id, project_id=project_id)

    def workflows(self, project_id: str) -> WorkflowExperience:
        """可复用工作流经验（技术方案 8.3）。"""
        return WorkflowExperience(str(self.project_memory_dir(project_id) / "workflows.json"))

    # ── 播种：全局领域知识（按 id 增量） ─────────────────────────

    def ensure_seeded(self) -> None:
        """播种 global_knowledge；knowledge_seed.json 落盘供审计。

        技术方案 8.4a 的两处修复：
        1. 种子文件的落盘条件从「文件不存在」改为「文件不存在 或 schema_version 不一致」，
           升级后老环境的审计文件也会刷新；
        2. `save_knowledge` 改为按 id 增量 upsert，新增条目（E 系列）在老环境也能灌入。
        """
        try:
            os.makedirs(self._root, exist_ok=True)
            seed_path = self._root / "knowledge_seed.json"
            if self._seed_file_outdated(seed_path):
                from ..atomic_io import atomic_write_json
                atomic_write_json(str(seed_path), seed_data.seed_document())
            self._rag.save_knowledge(seed_data.SEED_ITEMS)
        except Exception as e:
            logger.warning(f"[memory] 领域知识播种失败: {e}")

    @staticmethod
    def _seed_file_outdated(seed_path: Path) -> bool:
        if not seed_path.exists():
            return True
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("schema_version") != seed_data.SEED_SCHEMA_VERSION
        except Exception:
            return True

    # ── 写入：实验自动入库 ─────────────────────────────────────────

    def auto_save_experiment(self, project_id: str, record: Dict[str, Any]) -> None:
        """一次实验，两处落库：
        1. experiments.json 追加结构化记录（精确查询）
        2. ChromaDB project_{id} 写入自然语言段落（语义检索，metadata 带检索键）
        """
        if not project_id or not record.get("conv_id"):
            logger.warning(f"[memory] 跳过实验入库（缺 project_id 或 conv_id）")
            return
        try:
            self.experiment_log(project_id).add(record)
        except Exception as e:
            logger.warning(f"[memory] experiments.json 写入失败: {e}")
        try:
            paragraph = self._record_to_paragraph(record)
            metrics = record.get("metrics", {}) or {}
            test_metrics = metrics.get("test", {}) or {}
            r2 = test_metrics.get("R2")
            self._rag.save_experience(
                project_id,
                paragraph,
                metadata={
                    "source_exp": record.get("experiment_id", ""),
                    "source_conv": record.get("conv_id", ""),
                    "region": record.get("region", ""),
                    "model": record.get("model", ""),
                    "r2": r2 if isinstance(r2, (int, float)) else -999.0,
                    "date": (record.get("date_range") or ["", ""])[0],
                    "status": record.get("status", ""),
                },
            )
        except Exception as e:
            logger.warning(f"[memory] ChromaDB 实验入库失败: {e}")

    @staticmethod
    def _record_to_paragraph(record: Dict[str, Any]) -> str:
        """把结构化实验记录组装为自然语言段落（数据清单 2.3 口径）。"""
        parts = []
        region = record.get("region", "?")
        daterange = record.get("date_range") or ["", ""]
        status = record.get("status", "")
        parts.append(f"{daterange[0]} {region} {record.get('model', '?')} 实验（{status}）")

        if status == "failed":
            parts.append(f"失败阶段: {record.get('failure_stage', '?')}；"
                         f"原因: {record.get('failure_message', '?')}")
        metrics = record.get("metrics", {}) or {}
        test = metrics.get("test", {}) or {}
        if test:
            parts.append(f"测试集 R²={test.get('R2')}, RMSE={test.get('RMSE')}K, "
                         f"MAE={test.get('MAE')}K, MB={test.get('MB')}K")
        fi = record.get("feature_importance") or []
        if fi:
            top = sorted(fi, key=lambda x: x.get("importance", 0), reverse=True)[:3]
            parts.append("特征重要性 " + " > ".join(
                f"{t.get('feature')}({t.get('importance'):.2f})" for t in top))
        params = record.get("params", {}) or {}
        if params:
            parts.append(f"生效参数 {json.dumps(params, ensure_ascii=False)}")
        indep = record.get("independent_prediction", {}) or {}
        if indep:
            parts.append(f"独立预测 n={indep.get('n_samples', '?')} R²={indep.get('R2')}")
        closure = record.get("closure", {}) or {}
        if closure:
            cm = closure.get("metrics", {}) or {}
            parts.append(f"粗尺度闭合 MB={cm.get('MB_K')}K, MAE={cm.get('MAE_K')}K")
        df = record.get("data_features", {}) or {}
        if df:
            parts.append(f"样本 {df.get('train_samples', '?')}, 地形 DEMσ={df.get('dem_std')}m")
        return "；".join([p for p in parts if p])

    # ── 读取：prompt 注入 ──────────────────────────────────────────

    def enrich_prompt(self, project_id: str, query: str, n: int = 6) -> str:
        """构建注入文本：领域知识参考 + 当前项目历史经验 + 历史最佳实验。"""
        blocks: List[str] = []

        # 1. 领域知识（RAG 检索 global_knowledge；空则兜底精选种子）
        # 主题条目较多（如 K01/K06 同属 TTRI），n 取 8 保证相关条目能进入注入，
        # 避免"TTRI 是为了解决什么问题"这类查询被不相关条目挤掉
        knowledge = self._rag.search_knowledge(query, n=8)
        if not knowledge:
            knowledge = self._fallback_knowledge(n=n)
        if knowledge:
            lines = []
            for k in knowledge:
                text = (k.get("text") or "").strip()
                lines.append(f"- {text}")
            blocks.append("## 领域知识参考\n" + "\n".join(lines))

        # 2. 当前项目历史经验（RAG 检索 project_{id}）
        if project_id:
            experiences = self._rag.search_for_agent(project_id, query, n=min(n, 3))
            if experiences:
                lines = [f"- {(e.get('text') or '').strip()}" for e in experiences]
                blocks.append(f"## 当前项目历史经验（项目 {project_id}）\n" + "\n".join(lines))

            # 3. 历史最佳实验（JSON 精确查询）
            best = self.experiment_log(project_id).get_best()
            if best:
                test = ((best.get("metrics") or {}).get("test") or {})
                blocks.append(
                    f"## 历史最佳实验\n"
                    f"- {best.get('region', '?')} | {best.get('model', '?')} | "
                    f"R²={test.get('R2')} | 参数 {json.dumps(best.get('params', {}), ensure_ascii=False)}"
                )

        # 4. 结构化精确查询：从 query 提取研究区/时间/影像对日期 → 历史实验指标
        block = self.structured_experiment_block(project_id, query)
        if block:
            blocks.append(block)
        return "\n\n".join(blocks)

    def structured_experiment_block(self, project_id: str, query: str) -> str:
        """结构化查询层：从用户 query 提取研究区/时间/影像对日期，
        精确查历史实验指标并组装为注入文本（不再依赖语义检索猜测）。"""
        if not project_id or not query:
            return ""
        import re as _re

        text = (query or "").strip()
        start = end = ""
        m = _re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            start = f"{year:04d}-{month:02d}-01"
            end = f"{year:04d}-{month:02d}-31"
        else:
            m = _re.search(r"(\d{4})\s*年", text)
            if m:
                year = int(m.group(1))
                start = f"{year:04d}-01-01"
                end = f"{year:04d}-12-31"
        # 影像对日期：8 位 YYYYMMDD（Landsat/Sentinel 同日均按此过滤）
        md = _re.search(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-2]\d|3[01])", text)
        pair_date = md.group(0) if md else ""
        # 研究区：常见地名后缀（省/市/自治州/县/区/镇/乡）
        rm = _re.search(r"[\u4e00-\u9fa5]{2,8}?(?:省|市|自治州|县|区|镇|乡)", text)
        region = rm.group(0) if rm else ""
        if not region and not start and not pair_date:
            return ""
        try:
            hits = self.experiment_log(project_id).query(
                region=region, start=start, end=end,
                landsat_date=pair_date, sentinel2_date=pair_date)
        except Exception as e:
            logger.warning(f"[memory] 结构化查询失败（已忽略）: {e}")
            return ""
        if not hits:
            return ""
        lines = []
        for r in hits[:5]:
            pair = r.get("pair") or {}
            test = (r.get("metrics") or {}).get("test") or {}
            dr = r.get("date_range") or ["", ""]
            lines.append(
                f"- {r.get('region', '?')}（{dr[0]} ~ {dr[1]}）："
                f"测试集 R²={test.get('R2')}，"
                f"影像对 Landsat {pair.get('landsat_date', '?')} + "
                f"Sentinel-2 {pair.get('sentinel2_date', '?')}，"
                f"实验时间 {r.get('timestamp', '?')}")
        return "## 历史实验精确匹配\n" + "\n".join(lines)

    # ── 读取：按角色定制的注入（enrich_prompt 保持原样不动） ──────

    def enrich_for_role(self, project_id: str, role: str, query: str) -> str:
        """按角色定制的记忆注入（技术方案 8.4c）；role 未知时退化为 enrich_prompt。"""
        config = ROLE_RETRIEVAL.get(role)
        if config is None:
            return self.enrich_prompt(project_id, query)

        blocks: List[str] = []

        knowledge = self._rag.search_knowledge(query, n=config["knowledge_n"],
                                               where=config.get("knowledge_where"))
        if not knowledge:
            knowledge = self._fallback_knowledge(n=config["knowledge_n"])
        if knowledge:
            blocks.append("## 领域知识参考\n" + "\n".join(
                f"- {(k.get('text') or '').strip()}" for k in knowledge))

        if project_id:
            experiences = self._rag.search_for_agent(
                project_id, query, n=config["experience_n"],
                where=config.get("experience_where"))
            if experiences:
                blocks.append(f"## 当前项目历史经验（项目 {project_id}）\n" + "\n".join(
                    f"- {(e.get('text') or '').strip()}" for e in experiences))

            if config.get("include_best"):
                best = self.experiment_log(project_id).get_best()
                if best:
                    test = ((best.get("metrics") or {}).get("test") or {})
                    blocks.append(
                        f"## 历史最佳实验\n"
                        f"- {best.get('region', '?')} | {best.get('model', '?')} | "
                        f"R²={test.get('R2')} | 参数 "
                        f"{json.dumps(best.get('params', {}), ensure_ascii=False)}")

            if config.get("include_workflows"):
                block = self._workflow_block(project_id)
                if block:
                    blocks.append(block)

            if config.get("include_preferences"):
                prefs = self.preferences(project_id).all()
                if prefs:
                    blocks.append("## 用户偏好\n" + "\n".join(
                        f"- {k}：{v}" for k, v in prefs.items()))

            # 结构化查询层：精确查历史实验指标（region/时间/影像对日期）
            block = self.structured_experiment_block(project_id, query)
            if block:
                blocks.append(block)

        return "\n\n".join(blocks)

    def _workflow_block(self, project_id: str) -> str:
        try:
            records = self.workflows(project_id).all()
        except Exception:
            return ""
        if not records:
            return ""
        lines = []
        for record in records[-3:]:
            metrics = record.get("metrics") or {}
            lines.append(f"- {record.get('region', '?')} "
                         f"{(record.get('date_range') or ['', ''])[0]}：参数 "
                         f"{json.dumps(record.get('final_params', {}), ensure_ascii=False)}，"
                         f"测试集 R²={metrics.get('test_r2')}")
        return "## 该项目可复用的成功流程\n" + "\n".join(lines)

    # ── 写入：可复用工作流经验（技术方案 8.3） ───────────────────

    def save_workflow(self, project_id: str, record: Dict[str, Any]) -> None:
        """双写 workflows.json + ChromaDB 段落（metadata 带 kind="workflow"）。"""
        if not project_id or not record:
            return
        try:
            self.workflows(project_id).add(record)
        except Exception as e:
            logger.warning(f"[memory] workflows.json 写入失败: {e}")
        try:
            metrics = record.get("metrics") or {}
            self._rag.save_workflow(
                project_id, record_to_paragraph(record),
                metadata={
                    "source_workflow": record.get("workflow_id", ""),
                    "source_exp": record.get("experiment_id", ""),
                    "source_conv": record.get("conv_id", ""),
                    "region": record.get("region", ""),
                    "date": (record.get("date_range") or ["", ""])[0],
                    "test_r2": metrics.get("test_r2") if isinstance(
                        metrics.get("test_r2"), (int, float)) else -999.0,
                    "verdict": record.get("verdict", ""),
                },
            )
        except Exception as e:
            logger.warning(f"[memory] ChromaDB 工作流入库失败: {e}")

    def search_workflows(self, project_id: str, query: str, n: int = 3) -> List[Dict[str, Any]]:
        """只检索可复用工作流段落（按 kind 过滤）。"""
        return self._rag.search_for_agent(project_id, query, n=n,
                                          where={"kind": "workflow"})

    def best_workflow(self, project_id: str, region: str) -> Optional[Dict[str, Any]]:
        """取同区域中测试集 R² 最高的可复用流程（结构化精确查询）。"""
        try:
            return self.workflows(project_id).find_for_region(region)
        except Exception:
            return None

    def _fallback_knowledge(self, n: int = 3) -> List[Dict[str, Any]]:
        """检索为空时的兜底：返回精选种子条目的摘要，保证 Agent 基础领域知识不退化。"""
        items = [i for i in seed_data.SEED_ITEMS if i["id"] in _FALLBACK_KNOWLEDGE_IDS][:n]
        return [{"text": f"[{i['id']}] {i['topic']}：{i['content'][:120]}…",
                 "metadata": {}, "distance": None} for i in items]

    # ── 删除级联 ───────────────────────────────────────────────────

    def delete_conversation(self, project_id: str, conv_id: str) -> None:
        """删除对话：experiments.json 与 workflows.json 删该 conv 记录
        + ChromaDB 删该 conv 条目 + 删该对话的会话槽位状态（技术方案 8.2/8.3 级联约定）。"""
        try:
            self.experiment_log(project_id).delete_by_conv(conv_id)
        except Exception as e:
            logger.warning(f"[memory] 删除对话实验记录失败: {e}")
        try:
            self.workflows(project_id).delete_by_conv(conv_id)
        except Exception as e:
            logger.warning(f"[memory] 删除对话工作流经验失败: {e}")
        self._rag.delete_by_conv(project_id, conv_id)
        self.session_state(conv_id, project_id).delete()

    def delete_project(self, project_id: str) -> None:
        """删除项目：删 memory/projects/{project_id}/ 目录 + ChromaDB Collection
        + 删该项目下所有对话的会话槽位状态。"""
        self._delete_project_sessions(project_id)
        try:
            mem_dir = self.project_memory_dir(project_id)
            if mem_dir.exists():
                shutil.rmtree(mem_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"[memory] 删除项目记忆目录失败: {e}")
        self._rag.delete_project_collection(project_id)

    def _delete_project_sessions(self, project_id: str) -> None:
        """按 session 文件里的 project_id 匹配删除（会话文件按对话而非项目分目录）。"""
        if not self._sessions_dir.exists():
            return
        for path in self._sessions_dir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("project_id") == project_id:
                    os.remove(path)
            except Exception as e:
                logger.warning(f"[memory] 删除项目会话状态失败（已忽略）: {e}")

    # ── 偏好 ───────────────────────────────────────────────────────

    def get_preference(self, project_id: str, key: str, default: Any = None) -> Any:
        return self.preferences(project_id).get(key, default)

    def set_preference(self, project_id: str, key: str, value: Any) -> None:
        try:
            self.preferences(project_id).set(key, value)
        except Exception as e:
            logger.warning(f"[memory] 偏好写入失败: {e}")
