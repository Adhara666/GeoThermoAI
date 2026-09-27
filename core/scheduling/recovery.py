"""失败自愈：诊断器 + 带适用域的策略库 + 经验结晶（"任务管家"核心）。

设计原则（与用户确认）：
- **第一次靠脑子，第二次靠经验**：规则/经验未命中时叫醒对口角色的 LLM
  诊断长尾错误；自愈结果写入经验卡，同类下次直接命中（LLM 负责长尾、
  规则负责高频，越用越快）。
- **执行动作白名单**：retry / retry_cloud_plus / escalate——自愈只动
  方法层参数（云量放宽），**永远不动科学设定**（区域/时间/产品定义）。
- **经验带适用域**：条件标签（region/error_class/node_type/month），
  同域可自动执行，**跨域只降级为参考提示**（交 LLM 判断），防张冠李戴。
- **有界 + 必播报**：每节点自愈次数有上限；每次动作都写事件与日志。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# ── 动作白名单（自愈只允许这三种，绝不改科学设定）─────────────────
ALLOWED_ACTIONS = ("retry", "retry_cloud_plus", "escalate")

# 每节点自愈次数上限（有界）
MAX_HEAL_PER_NODE = 2
# 放宽云量的步长（70 → 90 → 110，步进 20；上限 110）
CLOUD_STEP = 20
CLOUD_MAX = 110

# ── 错误分类器（规则）：(节点类型, error_kind, 错误文本) → 错误类 ────
ERROR_CLASSES = ("no_scene", "no_pair", "asset_missing", "network",
                 "resource", "data_quality", "unknown")

_CLASS_LABELS = {
    "no_scene": "影像检索无合格候选（云量阈值过严等）",
    "no_pair": "配对候选为空（时差/云量规则过严）",
    "asset_missing": "输入资产缺失或不可读",
    "network": "网络传输异常",
    "resource": "资源预算超限",
    "data_quality": "数据质量校验不通过",
    "unknown": "未归类的执行错误",
}

# 内置策略：scope=global 可跨域自动执行；scope=regional 仅同域自动
_BUILTIN_POLICY: Dict[str, Dict[str, Any]] = {
    "network": {"scope": "global", "action": "retry",
                "reason": "网络传输异常多为瞬态，自动重试"},
    "resource": {"scope": "global", "action": "escalate",
                 "reason": "资源预算超限不可自动改参，转人工建议"},
    "no_scene": {"scope": "global", "action": "retry_cloud_plus",
                 "reason": "影像检索无候选，按安全步进放宽云量阈值重试（方法论级）"},
    "no_pair": {"scope": "global", "action": "retry_cloud_plus",
                "reason": "配对候选为空，按安全步进放宽云量阈值重试（方法论级）"},
    "data_quality": {"scope": "regional", "action": "escalate",
                     "reason": "数据质量类失败建议人工核查"},
    "asset_missing": {"scope": "global", "action": "retry",
                      "reason": "资产缺失可能是清理竞态，先原样重试"},
    "unknown": {"scope": "global", "action": "escalate",
                "reason": "未归类错误转人工诊断"},
}

# 节点类型 → 对口角色（诊断意见由"谁的孩子谁抱"）
_NODE_ROLE = {
    "search_scene": "data", "select_scene": "data", "acquire_asset": "data",
    "prepare_local": "data", "data_check": "data", "preprocess_split": "data",
    "prep_check": "data", "ttri": "data", "ttri_check": "data",
    "gapfill": "data", "rebuild": "data",
    "rf_round": "train", "train_decision": "train", "promote_best": "train",
    "tcr": "train", "export": "train",
    "closure_eval": "eval",
}


def role_for(node_type: str) -> str:
    """节点失败时对口的诊断角色。"""
    return _NODE_ROLE.get(str(node_type or ""), "data")


def classify(node_type: str, error_kind: str, error_text: str) -> str:
    """错误分类器：粗粒度归类，供经验匹配与规则命中（不追求精细）。"""
    t = f"{error_text or ''} {error_kind or ''}"
    if error_kind == "resource" or any(k in t for k in
                                       ("内存", "磁盘", "预算", "GiB")):
        return "resource"
    if "配对规则的候选" in t or "配对候选" in t:
        return "no_pair"
    if node_type in ("search_scene", "select_scene") and any(
            k in t for k in ("云量", "候选", "没有符合", "无合格", "阈值")):
        return "no_scene"
    if any(k in t for k in ("不存在", "缺失", "不可读")):
        return "asset_missing"
    if error_kind in ("network", "transfer") or any(
            k in t for k in ("网络", "超时", "连接", "下载", "transfer")):
        return "network"
    if any(k in t for k in ("质量", "校验", "检查未通过")):
        return "data_quality"
    # 兜底：数据/训练/评估节点的通用执行失败按 asset_missing 处置（先重试）
    if node_type in ("search_scene", "select_scene", "acquire_asset",
                     "prepare_local", "data_check"):
        return "asset_missing"
    return "unknown"


def class_label(error_class: str, zh: bool = True) -> str:
    if zh:
        return _CLASS_LABELS.get(error_class, error_class)
    return error_class


def same_region(a: str, b: str) -> bool:
    """区域同域判定（保守：互为子串才算同域，如"武汉市_市"与"武汉市"）。"""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return False
    return a == b or a in b or b in a


# ── 经验库（项目级 recovery.json：带条件标签 + 置信度）─────────────
class ExperienceStore:
    """自愈经验卡读写（每项目一份；条件标签防张冠李戴）。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("[recovery] 经验库读取失败（按空处理）: %s", e)
            return []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, records)

    def all(self) -> List[Dict[str, Any]]:
        return self._load()

    def add_pending(self, *, region: str, error_class: str, node_type: str,
                    action: str, params: Optional[dict], node_id: str,
                    month: str = "") -> None:
        """自愈动作执行时登记一条待观察经验（outcome=pending）。"""
        with self._lock:
            records = self._load()
            records.append({
                "region": str(region or ""),
                "month": str(month or ""),
                "error_class": error_class,
                "node_type": str(node_type or ""),
                "action": action,
                "params": dict(params or {}),
                "node_id": str(node_id or ""),
                "outcome": "pending",
                "confidence": 0,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._save(records)

    def resolve(self, node_id: str, outcome: str) -> None:
        """节点重跑结果回填：成功 → 置信 +1；再次同类失败 → 记一次败。"""
        with self._lock:
            records = self._load()
            changed = False
            for r in records:
                if r.get("node_id") == node_id and r.get("outcome") == "pending":
                    r["outcome"] = outcome
                    if outcome == "success":
                        r["confidence"] = int(r.get("confidence") or 0) + 1
                    changed = True
            if changed:
                self._save(records)

    def match(self, *, region: str, error_class: str,
              node_type: str) -> Optional[Dict[str, Any]]:
        """条件匹配：同域 + 同类错误 + 同节点类型；取最新一条非 pending。"""
        best = None
        for r in self._load():
            if str(r.get("error_class")) != error_class:
                continue
            if str(r.get("node_type")) != str(node_type or ""):
                continue
            if r.get("outcome") == "pending":
                continue
            same = same_region(region, r.get("region", ""))
            if not same:
                continue  # 跨域不自动执行（防张冠李戴）
            if best is None or int(r.get("confidence") or 0) > int(best.get("confidence") or 0):
                best = r
        return best


# ── 诊断器（规则/经验优先，未命中标记 need_llm）──────────────────
def diagnose(*, region: str, node_type: str, error_kind: str, error_text: str,
             experiences: ExperienceStore,
             month: str = "") -> Dict[str, Any]:
    """诊断一次失败，返回结构化处置建议。

    返回字段：
      error_class  错误类
      action       建议动作（白名单内；未命中为 escalate+need_llm）
      params       动作参数（如云量覆盖值）
      reason       人话理由
      source       rule | experience | llm-needed
      need_llm     是否需要叫醒角色（规则/经验未命中）
      role         对口诊断角色
    """
    cls = classify(node_type, error_kind, error_text)
    out: Dict[str, Any] = {
        "error_class": cls, "action": "escalate", "params": {},
        "reason": "", "source": "rule", "need_llm": False,
        "role": role_for(node_type),
    }
    # 1) 经验优先（同域 + 置信 ≥1）——“第二次靠经验”
    hit = experiences.match(region=region, error_class=cls, node_type=node_type)
    if hit and int(hit.get("confidence") or 0) >= 1 \
            and str(hit.get("action")) in ALLOWED_ACTIONS:
        out.update(action=str(hit["action"]), params=dict(hit.get("params") or {}),
                   source="experience",
                   reason=f"同类情况此前用过「{hit['action']}」成功"
                          f"（{hit.get('confidence')} 次）")
        return out
    # 2) 内置规则（global 直接可用；regional 需…… 这里同为"方法论"动作，
    #    云量放宽只影响检索阈值，不改变科学设定，同域语义由经验层把关）
    rule = _BUILTIN_POLICY.get(cls)
    if rule:
        out.update(action=rule["action"], reason=rule["reason"], source="rule")
        if rule["action"] == "retry_cloud_plus":
            out["params"] = {"cloud_step": CLOUD_STEP}
        if rule["action"] == "escalate":
            out["need_llm"] = True  # 规则说不自动 → 交给角色诊断给建议
        return out
    # 3) 未命中 → 需要角色（第一次靠脑子）
    out["need_llm"] = True
    out["source"] = "llm-needed"
    return out


# ── LLM 诊断（对口角色；输出被白名单与轻校验约束）─────────────────
def llm_diagnose(assistant, *, role: str, node_type: str, error_text: str,
                 error_class: str, attempts: int = 1,
                 region: str = "", time_range: str = "",
                 reference: Optional[dict] = None,
                 allow_cloud: bool = True) -> Optional[Dict[str, Any]]:
    """叫醒对口角色做一次结构化诊断（失败/超时返回 None，调用方走 escalate）。

    输出 JSON：{"root_cause": str, "action": 白名单内, "reason": str}；
    action 不在白名单或缺失 → 返回 None（绝不执行未授权动作）。
    """
    if assistant is None:
        return None
    try:
        from ..agent.roles.prompts import recovery as prompts
    except Exception:  # noqa: BLE001
        return None
    ref = ""
    if reference:
        ref = ("跨区域参考（仅供参考，不保证适用）："
               f"区域 {reference.get('region')} 曾用「{reference.get('action')}」")
    candidates = ["retry", "escalate"] + (["retry_cloud_plus"] if allow_cloud else [])
    try:
        text = assistant.ask(
            prompts.diagnose_prompt(role=role, node_type=node_type,
                                    error_class=error_class,
                                    error_text=str(error_text or "")[:600],
                                    attempts=attempts, region=region,
                                    time_range=time_range, reference=ref,
                                    candidates=candidates))
    except Exception as e:  # noqa: BLE001
        logger.warning("[recovery] LLM 诊断失败（转人工建议）: %s", e)
        return None
    text = str(text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action") or "")
    if action not in candidates:
        return None
    return {"root_cause": str(parsed.get("root_cause") or "").strip(),
            "action": action,
            "reason": str(parsed.get("reason") or "").strip()}


def cloud_override(current: Any, step: int = CLOUD_STEP) -> int:
    """计算放宽后的云量阈值（有封顶，返回整数）。"""
    try:
        cur = int(round(float(current)))
    except (TypeError, ValueError):
        cur = 30
    return min(CLOUD_MAX, max(cur + step, step + 30))
