"""
GeoThermoAI 智能助手模块

支持 DeepSeek / Kimi / OpenAI / Anthropic 多模型 API，
提供参数推荐、结果诊断、报告生成等功能。
"""

import json
import threading
from typing import Dict, List, Callable

import requests


class GeoThermoAI_Assistant:
    """AI 助手 - 支持多模型 API"""

    SUPPORTED_MODELS = {
        'deepseek': 'https://api.deepseek.com/v1/chat/completions',
        'kimi': 'https://api.moonshot.cn/v1/chat/completions',
        'openai': 'https://api.openai.com/v1/chat/completions',
    }

    def __init__(self, model_type='deepseek', api_key='', api_base_url='', model_id='', api_format='openai'):
        self.model_type = model_type
        self.api_key = api_key
        self.api_base_url = api_base_url.rstrip('/')
        self.model_id = model_id
        self.api_format = api_format
        self.current_model_name = 'RF'  # 当前工作面板选择的模型名称

        # 构建可读的模型显示名称（用于系统提示词）
        model_display = model_id or model_type or 'deepseek'
        self.model_display_name = model_display

        self.system_prompt = self._build_system_prompt()

    def set_current_model(self, model_name: str):
        """设置当前工作面板选择的模型名称，更新系统提示词"""
        self.current_model_name = model_name or 'RF'
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self):
        """构建系统提示词（仅含角色身份与格式规则；领域知识由记忆系统动态注入）

        设计原则：算法公式/输入数据/数据来源等专业领域知识不再硬编码在此处，
        统一由记忆系统（core/memory 种子 + RAG 检索）在 ask/ask_stream 时注入，
        为将来动态 Agent 角色切换做准备（角色提示词可配置化，领域知识单一来源）。
        """
        return f"""你是GeoThermoAI智能助手，专注于地表温度降尺度处理。

## 关于你的身份（严格遵守）
- 你是GeoThermoAI，一个负责进行地表温度降尺度处理的AI助手
- 你底层使用的是 **{self.model_display_name}** 大语言模型
- 如果用户问你"你是什么模型"/"你的底层是什么"，回答时请准确说明底层模型为 {self.model_display_name}
- 不要提及任何与当前配置模型无关的其他模型名称
- 当前工作面板使用的降尺度模型：{self.current_model_name}

## 领域知识（以注入内容为准）
- 关于本系统的算法原理（TTRI/TCR/降尺度）、输入数据、数据来源等专业领域知识，
  一律以对话中注入的"当前软件状态/记忆"内容为准，未注入的部分请谨慎基于常识回答，严禁编造
- 数据来源硬性约束（不可违背）：本系统遥感数据来自 Microsoft Planetary Computer
  与 Copernicus Data Space（通过 STAC API 自动搜索下载，国内可直连）；
  **禁止回答 Google Earth Engine（GEE）或其他本系统未使用的数据平台**
- 用户询问"数据来自什么平台/数据源/数据来源"时，按上述说明如实回答

输出：10m分辨率地表温度产品

请用简洁专业的语言回答用户问题。当用户询问算法实现细节时，请基于当前使用的{self.current_model_name}模型来回答。
重要：必须使用以下完整名称，不要使用简称或缩写替代：
- TTRI 必须写为"地形热响应指数（TTRI）"
- TCR 必须写为"热约束残差（TCR）"
不要写成"地形特征分析"或"残差空间化"等非标准名称。
数学公式请使用 LaTeX 语法，行内公式用 $...$ 包裹，块级公式用 $$...$$ 包裹。
格式规则：
- 列表必须使用无序列表（用 - 开头），禁止使用编号列表（如 1. 2. 3.）
- 禁止输出任何 markdown 标记符号（如 **、##、###、*、` 等），需要强调时用引号「」或直接自然语言表达即可
- 段落之间用空行分隔，保持内容紧凑"""

    def ask(self, question: str, context: dict = None, prior_messages: list = None) -> str:
        """向AI提问，支持传入历史消息"""
        # 检查是否配置了 LLM
        if not self.api_key and not self.api_base_url:
            return "未检测到LLM模型配置，请先在设置中配置 API 密钥或自定义请求地址。"

        messages = [
            {"role": "system", "content": self.system_prompt}
        ]

        if context:
            context_str = self._format_context(context)
            messages.append({
                "role": "system",
                "content": f"当前软件状态：{context_str}"
            })

        # 加入历史对话
        if prior_messages:
            for msg in prior_messages:
                role = "assistant" if msg.get("role") == "ai" else msg.get("role", "user")
                messages.append({"role": role, "content": msg.get("content", "")})

        messages.append({"role": "user", "content": question})

        return self._call_api(messages)

    def ask_stream(
        self,
        question: str,
        on_token: Callable[[str], None],
        context: dict = None,
        prior_messages: list = None,
        on_thinking: Callable[[str], None] = None,
    ) -> str:
        """流式向AI提问，每次收到 token 回调 on_token(累计内容)

        on_thinking（可选）：模型输出思考过程（如 DeepSeek 的 reasoning_content）
        时，以累计思考内容回调，供前端以可折叠形式展示。
        """
        if not self.api_key and not self.api_base_url:
            error = "未检测到LLM模型配置，请先在设置中配置 API 密钥或自定义请求地址。"
            on_token(error)
            return error

        messages = [{"role": "system", "content": self.system_prompt}]

        if context:
            context_str = self._format_context(context)
            messages.append({
                "role": "system",
                "content": f"当前软件状态：{context_str}",
            })

        if prior_messages:
            for msg in prior_messages:
                role = "assistant" if msg.get("role") == "ai" else msg.get("role", "user")
                messages.append({"role": role, "content": msg.get("content", "")})

        messages.append({"role": "user", "content": question})

        if self.api_format == "anthropic":
            # Anthropic 暂不支持流式，降级为非流式
            result = self._call_anthropic_api(messages)
            on_token(result)
            return result
        else:
            return self._call_openai_api_stream(messages, on_token, on_thinking=on_thinking)

    def _call_api(self, messages: List[Dict], **overrides) -> str:
        """调用大模型API，overrides 可覆盖 max_tokens/temperature 等参数。
        支持 on_thinking 回调（透传非流式响应中的 reasoning_content）。"""
        if self.api_format == 'anthropic':
            return self._call_anthropic_api(messages)
        else:
            return self._call_openai_api(messages, **overrides)

    def _call_openai_api(self, messages: List[Dict], **overrides) -> str:
        """调用 OpenAI 兼容格式 API

        on_thinking（可选）：非流式响应（如规划 Agent 出 plan 前的 LLM 调用）
        携带 reasoning_content 时回调，让规划/反思等同步阶段的思考也能实时展示
        。
        """
        on_thinking = overrides.get("on_thinking")
        if self.api_base_url:
            api_url = f"{self.api_base_url}/chat/completions"
        else:
            api_url = self.SUPPORTED_MODELS.get(self.model_type)
            if not api_url:
                return f"不支持的模型类型: {self.model_type}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model_id if self.model_id else self._get_model_name(),
            "messages": messages,
            "temperature": overrides.get("temperature", 0.7),
            "max_tokens": overrides.get("max_tokens", 8192)
        }
        # DeepSeek-V4 系列思考模式默认开启：reasoning_content 计入 max_tokens，
        # 对「单步结构化输出」类任务（如 eval 结果解读）会占满预算导致正文为空而降级。
        # 调用方（如 eval_agent）可传 thinking={"type": "disabled"} 关闭该次调用的思考链。
        thinking = overrides.get("thinking")
        if thinking is not None:
            payload["thinking"] = thinking

        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            message = response.json()['choices'][0]['message']
            thinking = message.get("reasoning_content") or ""
            if thinking and on_thinking:
                on_thinking(thinking)
            return message.get("content", "")
        except Exception as e:
            return f"API调用失败: {str(e)}"

    def _call_openai_api_stream(
        self, messages: List[Dict], on_token: Callable[[str], None],
        on_thinking: Callable[[str], None] = None,
    ) -> str:
        """流式调用 OpenAI 兼容格式 API，每次收到 token 回调 on_token(累计内容)

        on_thinking：模型输出 reasoning_content（DeepSeek 等思考模型）时，
        以累计思考内容回调（前端可折叠展示思考过程）。
        """
        if self.api_base_url:
            api_url = f"{self.api_base_url}/chat/completions"
        else:
            api_url = self.SUPPORTED_MODELS.get(self.model_type)
            if not api_url:
                return f"不支持的模型类型: {self.model_type}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model_id if self.model_id else self._get_model_name(),
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 8192,
            "stream": True,
        }

        full_content = ""
        full_thinking = ""
        try:
            response = requests.post(
                api_url, headers=headers, json=payload, stream=True, timeout=120
            )
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    line_text = line.decode("utf-8").strip()
                    if line_text.startswith("data: "):
                        data = line_text[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            # 思考过程：DeepSeek 等模型用 reasoning_content 字段输出
                            thinking = delta.get("reasoning_content") or ""
                            if thinking and on_thinking:
                                full_thinking += thinking
                                on_thinking(full_thinking)
                            content = delta.get("content") or ""
                            if content:
                                full_content += content
                                on_token(full_content)
                        except json.JSONDecodeError:
                            pass
            return full_content
        except Exception as e:
            error_msg = f"API流式调用失败: {str(e)}"
            on_token(error_msg)
            return error_msg

    def _call_anthropic_api(self, messages: List[Dict]) -> str:
        """调用 Anthropic Messages 格式 API"""
        if self.api_base_url:
            api_url = f"{self.api_base_url}/v1/messages"
        else:
            api_url = "https://api.anthropic.com/v1/messages"

        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }

        # Anthropic 不支持 system role，需要单独提取
        system_content = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_content += msg["content"] + "\n\n"
            else:
                chat_messages.append(msg)

        payload = {
            "model": self.model_id if self.model_id else self._get_model_name(),
            "messages": chat_messages,
            "max_tokens": 8192,
        }

        if system_content.strip():
            payload["system"] = system_content.strip()

        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            return data['content'][0]['text']
        except Exception as e:
            return f"API调用失败: {str(e)}"

    def _get_model_name(self) -> str:
        """获取模型名称"""
        model_names = {
            'deepseek': 'deepseek-chat',
            'kimi': 'moonshot-v1-8k',
            'openai': 'gpt-3.5-turbo'
        }
        return model_names.get(self.model_type, 'deepseek-chat')

    def _format_context(self, context: dict) -> str:
        """格式化上下文信息"""
        return json.dumps(context, ensure_ascii=False, indent=2)
