"""
角色 Agent 基类（技术方案 2.5 / 附录 A）

四个角色共用一个 LLM 客户端（`GeoThermoAI_Assistant`），不引入任何多智能体框架。
本基类只负责三件事：
1. 带角色提示词调用 LLM（低温度、可解析 JSON）；
2. JSON 三级解析兜底（与现有 `_parse_plan` 同一套策略，此处是单一来源）；
3. 按角色定制的记忆注入（`enrich_for_role`，不可用时退回 `enrich_prompt`）。

角色提示词必须包含四段（附录 A 检查清单）：你是谁 / 只负责什么 / 禁止做什么 / 输出格式。
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 角色提示词的四段结构检查（`test_planner_agent_synthetic.py` 断言）
REQUIRED_PROMPT_SECTIONS = ("你是", "你只负责", "你禁止", "输出")


def _strip_trailing_commas(snippet: str) -> str:
    """去掉对象/数组收尾前多余的逗号（`{"a": 1,}` → `{"a": 1}`），
    应对模型输出的「近似 JSON」常见笔误（实现期修订 v1.2）。"""
    return re.sub(r",(\s*[}\]])", r"\1", snippet)


def _iter_balanced_objects(text: str):
    """从文本**末尾往前**找所有「花括号配对」候选片段，正确跳过字符串内部的花括号。

    模型经常把真正的答案放在推理说明之后，说明文字里也可能出现无关的花括号
    （举例子、贴一段参考 JSON）；从后往前扫、优先命中离结尾最近的合法候选，
    比朴素的 `find('{')...rfind('}')` 更不容易被前置说明文字里的花括号带偏。
    """
    ends = [i for i, ch in enumerate(text) if ch == "}"]
    for end in reversed(ends):
        depth = 0
        in_string = False
        escape = False
        start = None
        i = end
        while i >= 0:
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "}":
                    depth += 1
                elif ch == "{":
                    depth -= 1
                    if depth == 0:
                        start = i
                        break
            i -= 1
        if start is not None:
            yield text[start:end + 1]


def _try_parse_dict(candidate: str) -> Optional[dict]:
    for text in (candidate, _strip_trailing_commas(candidate)):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_json(response: str) -> Optional[dict]:
    """从 LLM 响应中提取 JSON 对象（四级兜底：直接解析 → 代码块 → 花括号配对回扫 →
    首尾大括号截取；每一级都追加一次「去尾随逗号」重试）。

    这是全项目 JSON 解析的单一来源；`GeoThermoAgent._parse_plan` 也委托到这里。
    实现期修订 v1.2：原三级兜底只覆盖模型输出「干净」JSON 的理想情况，对「推理前言里
    夹带花括号」「尾随逗号」两类常见的不严格 JSON 输出没有兜底，新增两级应对。
    """
    text = (response or "").strip()
    if not text:
        return None

    parsed = _try_parse_dict(text)
    if parsed is not None:
        return parsed

    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        parsed = _try_parse_dict(m.group(1).strip())
        if parsed is not None:
            return parsed

    for candidate in _iter_balanced_objects(text):
        parsed = _try_parse_dict(candidate)
        if parsed is not None:
            return parsed

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = _try_parse_dict(text[start:end + 1])
        if parsed is not None:
            return parsed

    return None


def is_api_failure(response: str) -> bool:
    """LLM 调用是否失败（`GeoThermoAI_Assistant` 用返回字符串前缀表示失败）。"""
    text = (response or "").lstrip()
    return text.startswith("API调用失败") or text.startswith("API流式调用失败") \
        or text.startswith("未检测到LLM模型配置")


class RoleAgent:
    """带角色提示词与记忆注入的 LLM 调用封装。"""

    role = "base"
    role_name = "角色"

    def __init__(self, assistant, memory_manager=None, project_id: str = "",
                 on_log=None):
        self.assistant = assistant
        self.memory = memory_manager
        self.project_id = project_id
        self._on_log = on_log

    # ── 日志 ───────────────────────────────────────────────────────

    def log(self, message: str) -> None:
        """技术细节只进日志面板，不进气泡（气泡红线 4）。"""
        if self._on_log:
            try:
                self._on_log(f"  [{self.role}] {message}\n")
            except Exception:
                pass

    # ── 记忆注入 ───────────────────────────────────────────────────

    def memory_block(self, query: str) -> str:
        """按角色定制的记忆注入；失败一律返回空串，绝不影响主流程。"""
        if self.memory is None or not self.project_id:
            return ""
        try:
            enrich_for_role = getattr(self.memory, "enrich_for_role", None)
            if callable(enrich_for_role):
                return enrich_for_role(self.project_id, self.role, query) or ""
            return self.memory.enrich_prompt(self.project_id, query) or ""
        except Exception as e:
            logger.warning(f"[{self.role}] 记忆注入失败（已忽略）: {e}")
            return ""

    # ── LLM 调用 ───────────────────────────────────────────────────

    def call_text(self, system_prompt: str, user_content: str,
                  temperature: float = 0.2, max_tokens: int = 2048,
                  history: Optional[List[dict]] = None) -> str:
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for item in history or []:
            role = "assistant" if item.get("role") in ("ai", "assistant") else "user"
            content = (item.get("content") or "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_content})
        try:
            return self.assistant._call_api(messages, temperature=temperature,
                                            max_tokens=max_tokens)
        except Exception as e:
            logger.warning(f"[{self.role}] LLM 调用异常: {e}")
            return f"API调用失败: {e}"

    def call_json(self, system_prompt: str, user_content: str,
                  temperature: float = 0.0, max_tokens: int = 1200,
                  history: Optional[List[dict]] = None,
                  retry_once: bool = True) -> Optional[dict]:
        """调用 LLM 并解析为 JSON 对象；解析失败时用更严格的提示重试一次。

        返回 None 表示「LLM 不可用或输出无法解析」，由调用方走确定性兜底。
        """
        response = self.call_text(system_prompt, user_content, temperature=temperature,
                                  max_tokens=max_tokens, history=history)
        if is_api_failure(response):
            self.log(f"LLM 不可用：{response[:120]}")
            return None
        parsed = extract_json(response)
        if parsed is not None or not retry_once:
            if parsed is None:
                self.log("LLM 输出无法解析为 JSON")
            return parsed

        strict = system_prompt + "\n\n## 强制要求\n只输出一个 JSON 对象，不要任何解释文字、标题或代码块标记。"
        response = self.call_text(strict, user_content, temperature=0.0,
                                  max_tokens=max_tokens, history=history)
        if is_api_failure(response):
            return None
        parsed = extract_json(response)
        if parsed is None:
            self.log("重试后仍无法解析 JSON，转确定性兜底")
        return parsed
