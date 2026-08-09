"""
ChromaDB 向量记忆封装（RAG 层）

职责：
- 按用户持久化（PersistentClient(path=data/users/{uid}/memory/chromadb)）
- `project_{id}` Collection：项目历史实验段落（写入/检索/按对话删除）
- `global_knowledge` Collection：领域知识（幂等播种/检索）
- 自定义 embedding：优先 bge-small-zh-v1.5（ONNX，中文最优），
  模型缺失时回退 ChromaDB 内置 ONNX 嵌入（保证零配置可用）

约定：写入失败只向上抛给调用方记录告警，绝不影响 Agent 主流程。
"""

import os
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# BGE 官方检索惯例：查询文本加前缀，效果更稳定（见升级方案第五节）
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

# 默认 bge ONNX 模型目录（Dockerfile 构建期预下载；可用 BGE_MODEL_DIR 覆盖）
_DEFAULT_BGE_DIR = os.path.join("models", "bge-small-zh-v1.5")

_METADATA_NUMERIC_TYPES = (int, float, bool)


class _BGE_ONNXEmbedding:
    """bge-small-zh-v1.5 ONNX 编码器：AutoTokenizer + onnxruntime，均值池化 + L2 归一化。

    依赖 onnxruntime + transformers（仅用 tokenizer，无需 torch）。
    """

    def __init__(self, model_dir: str):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model_file = next(
            (os.path.join(root, f) for root, _, files in os.walk(model_dir)
             for f in files if f.endswith(".onnx")),
            os.path.join(model_dir, "model.onnx"),
        )
        self._session = ort.InferenceSession(
            model_file, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def encode(self, texts: List[str], is_query: bool = False) -> List[List[float]]:
        import numpy as np

        batch = []
        for text in texts:
            tokens = self._tokenizer(
                text, padding=True, truncation=True, max_length=512, return_tensors="np"
            )
            feeds = {k: v for k, v in tokens.items() if k in self._input_names}
            outputs = self._session.run(None, feeds)
            last_hidden = outputs[0]  # (1, seq, hidden)
            mask = feeds.get("attention_mask")
            if mask is None:
                mask = np.ones((last_hidden.shape[0], last_hidden.shape[1]), dtype=np.float32)
            mask = mask.astype(np.float32)
            summed = (last_hidden * mask[..., None]).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            vec = summed / counts[..., None]
            norm = np.linalg.norm(vec, axis=1, keepdims=True)
            vec = vec / np.clip(norm, 1e-9, None)
            batch.append(vec[0].tolist())
        return batch


class EmbeddingFunction:
    """统一 embedding 适配器（满足 chromadb embedding_function 协议）。

    - 配置了 bge 模型目录 → 用 bge（中文最优），查询时带 BGE 前缀；
    - 否则 → 回退 ChromaDB 内置 ONNX 嵌入（零配置可用，中文效果一般）。
    """

    def __init__(self, model_dir: str = ""):
        self._bge: Optional[_BGE_ONNXEmbedding] = None
        self._fallback = None
        # 候选目录：显式传入 > 环境变量 > 默认相对目录（Docker 内 WORKDIR=/app 可命中预下载模型）
        dirs = [d for d in (model_dir, os.environ.get("BGE_MODEL_DIR", ""), _DEFAULT_BGE_DIR) if d]
        for d in dirs:
            # 空目录/缺核心文件（onnx 或 tokenizer）视为未配置，直接跳过，
            # 避免"预下载失败留下空目录"被误判为有模型后加载失败
            if os.path.isdir(d) and any(
                f.endswith((".onnx", ".bin", ".safetensors")) or f in ("tokenizer.json", "tokenizer_config.json")
                for f in os.listdir(d)
            ):
                try:
                    self._bge = _BGE_ONNXEmbedding(d)
                    logger.info(f"[memory] 使用 bge-small-zh-v1.5 ONNX 嵌入: {d}")
                    break
                except Exception as e:
                    logger.warning(f"[memory] bge 加载失败，回退内置嵌入: {e}")
        if self._bge is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            self._fallback = DefaultEmbeddingFunction()
            logger.info("[memory] 使用 ChromaDB 内置 ONNX 嵌入（未配置 bge 模型）")

    def __call__(self, input: List[str]) -> List[List[float]]:
        """chromadb 协议：对 documents 编码（不加查询前缀）。"""
        texts = [t if isinstance(t, str) else str(t) for t in input]
        if self._bge is not None:
            return self._bge.encode(texts, is_query=False)
        return self._fallback(texts)

    def name(self) -> str:
        """chromadb 1.x 协议要求：embedding 函数名。"""
        if self._bge is not None:
            return "bge-small-zh-v1.5"
        return "chroma-default-onnx"

    def encode_query(self, texts: List[str]) -> List[List[float]]:
        """对查询文本编码（bge 模式自动加检索前缀）。"""
        texts = [t if isinstance(t, str) else str(t) for t in texts]
        if self._bge is not None:
            return self._bge.encode([BGE_QUERY_PREFIX + t for t in texts], is_query=True)
        return self._fallback(texts)

    def embed_query(self, input: List[str]) -> List[List[float]]:
        """chromadb 1.x 协议：query 编码（bge 模式带检索前缀）。"""
        return self.encode_query(input)

    def embed_documents(self, input: List[str]) -> List[List[float]]:
        """chromadb 1.x 协议：documents 编码（不加前缀）。"""
        return self(input)

    @property
    def is_bge(self) -> bool:
        return self._bge is not None


def _clean_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """清理 metadata：只保留 ChromaDB 支持的标量类型，过滤 None/复杂对象。"""
    cleaned = {}
    for k, v in meta.items():
        if isinstance(v, str):
            cleaned[k] = v
        elif isinstance(v, _METADATA_NUMERIC_TYPES):
            cleaned[k] = v
    return cleaned


class RAGStore:
    """ChromaDB 持久化封装：项目经验 + 全局领域知识。"""

    def __init__(self, persist_dir: str, embedding: Optional[EmbeddingFunction] = None):
        import chromadb

        self._persist_dir = persist_dir
        os.makedirs(persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._embedding = embedding or EmbeddingFunction()
        # 记忆加固：ChromaDB 写入/删除/检索的全局读写锁（可重入）。
        # get→add / get→delete 等多步操作必须原子执行，避免并行时
        # 「一个对话在写入经验、另一个对话在删除/检索」的竞态。
        self._lock = threading.RLock()

    # ── Collection 管理 ────────────────────────────────────────────

    def _collection(self, name: str):
        return self._client.get_or_create_collection(
            name=name,
            embedding_function=self._embedding,
            metadata={"hnsw:space": "cosine"},
        )

    def project_collection(self, project_id: str):
        return self._collection(f"project_{project_id}")

    def global_collection(self):
        return self._collection("global_knowledge")

    def delete_project_collection(self, project_id: str) -> None:
        with self._lock:
            try:
                self._client.delete_collection(f"project_{project_id}")
            except Exception as e:
                logger.warning(f"[memory] 删除 Collection project_{project_id} 失败: {e}")

    # ── 写入 ───────────────────────────────────────────────────────

    def save_experience(self, project_id: str, text: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """写入一条项目实验段落（metadata 需含唯一 source_conv + 检索键）。

        统一补 `kind="experiment"`，供规划 Agent 按 kind 区分实验与可复用工作流。
        """
        if not text:
            return
        meta = _clean_metadata({"kind": "experiment", **(metadata or {})})
        col = self.project_collection(project_id)
        doc_id = str(meta.get("source_exp", "")) or f"exp_{len(text)}"
        with self._lock:
            try:
                col.add(ids=[doc_id], documents=[text], metadatas=[meta])
            except Exception as e:
                logger.warning(f"[memory] ChromaDB 写入实验段落失败: {e}")

    def save_workflow(self, project_id: str, text: str,
                      metadata: Optional[Dict[str, Any]] = None) -> None:
        """写入一条可复用工作流段落（technical 方案 8.3），metadata 带 kind="workflow"。"""
        if not text:
            return
        meta = _clean_metadata({"kind": "workflow", **(metadata or {})})
        col = self.project_collection(project_id)
        doc_id = str(meta.get("source_workflow", "")) or f"wf_{len(text)}"
        with self._lock:
            try:
                col.add(ids=[doc_id], documents=[text], metadatas=[meta])
            except Exception as e:
                logger.warning(f"[memory] ChromaDB 写入工作流段落失败: {e}")

    def save_knowledge(self, items: List[Dict[str, Any]]) -> None:
        """按 id 增量播种领域知识（技术方案 8.4a）。

        改造前的判据是「collection 非空就整体跳过」，导致新增条目（如 E 系列）
        在老用户环境永远灌不进去。改为逐条按 id 比对：已有的不动（避免重复写入与
        内容漂移），只补缺失的。
        """
        col = self.global_collection()
        with self._lock:
            try:
                existing = set(col.get(include=[]).get("ids", []))
            except Exception as e:
                logger.warning(f"[memory] 读取已有领域知识 id 失败（按空处理）: {e}")
                existing = set()

            ids, docs, metas = [], [], []
            for item in items:
                kid = item.get("id", "")
                if not kid or kid in existing:
                    continue
                ids.append(kid)
                docs.append(item.get("content", ""))
                metas.append(_clean_metadata({
                    "topic": item.get("topic", ""),
                    "tags": ",".join(item.get("tags", [])),
                    "kid": kid,
                    # domain 是标量字段，可直接用于 metadata where 过滤
                    # （tags 存成逗号拼接串，$in 对它做不了包含匹配）
                    "domain": item.get("domain", ""),
                    "kind": "knowledge",
                }))
            if ids:
                try:
                    col.add(ids=ids, documents=docs, metadatas=metas)
                except Exception as e:
                    logger.warning(f"[memory] 领域知识播种失败: {e}")

    # ── 检索 ───────────────────────────────────────────────────────

    def _query_collection(self, col, query: str, n: int,
                          where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """向量检索，可选 metadata 过滤（技术方案 8.4b）。

        `where` 语法在不同 ChromaDB 版本上有差异，过滤查询异常时自动退回无过滤查询
        （与现有「检索失败返回空列表」的容错风格一致，但不因过滤语法把结果清空）。
        """
        with self._lock:
            try:
                total = col.count()
                if total == 0:
                    return []
                res = self._query(col, query, min(n, total), where)
                if res is None and where:
                    logger.warning("[memory] metadata 过滤查询失败，退回无过滤查询")
                    res = self._query(col, query, min(n, total), None)
                if res is None:
                    return []
            except Exception as e:
                logger.warning(f"[memory] 向量检索失败: {e}")
                return []
            items = []
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for i, doc in enumerate(docs):
                items.append({
                    "text": doc,
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                })
            return items

    def _query(self, col, query: str, n: int, where: Optional[Dict[str, Any]]):
        """执行一次 query；失败返回 None 由调用方决定是否退回无过滤。"""
        kwargs: Dict[str, Any] = {"n_results": n}
        if where:
            kwargs["where"] = where
        try:
            if self._embedding.is_bge:
                return col.query(query_embeddings=self._embedding.encode_query([query]),
                                 **kwargs)
            return col.query(query_texts=[query], **kwargs)
        except Exception as e:
            logger.warning(f"[memory] 检索执行失败（where={where}）: {e}")
            return None

    def search_for_agent(self, project_id: str, query: str, n: int = 3,
                         where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """项目经验检索；where 为 None 时行为与改造前完全一致。"""
        return self._query_collection(self.project_collection(project_id), query, n, where)

    def search_knowledge(self, query: str, n: int = 3,
                         where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """领域知识检索；where 为 None 时行为与改造前完全一致。"""
        return self._query_collection(self.global_collection(), query, n, where)

    # ── 删除 ───────────────────────────────────────────────────────

    def delete_by_conv(self, project_id: str, conv_id: str) -> None:
        with self._lock:
            try:
                col = self.project_collection(project_id)
                got = col.get(where={"source_conv": conv_id})
                ids = got.get("ids", [])
                if ids:
                    col.delete(ids=ids)
            except Exception as e:
                logger.warning(f"[memory] 删除对话记忆失败: {e}")

    def count(self, project_id: str = "", knowledge: bool = False) -> int:
        with self._lock:
            try:
                col = self.global_collection() if knowledge else self.project_collection(project_id)
                return col.count()
            except Exception:
                return 0
