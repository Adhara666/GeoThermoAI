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
        """构建系统提示词 - 注入领域知识，根据当前模型动态生成"""
        return f"""你是GeoThermoAI智能助手，专注于地表温度降尺度处理。

## 关于你的身份（严格遵守）
- 你是GeoThermoAI，一个地表温度降尺度处理软件的内置AI助手
- 你底层使用的是 **{self.model_display_name}** 大语言模型
- 如果用户问你"你是什么模型"/"你的底层是什么"，回答时请准确说明底层模型为 {self.model_display_name}
- 不要提及任何与当前配置模型无关的其他模型名称

核心算法：
1. TTRI（地形热响应指数）：基于DEM数据建立地形与温度的回归关系
   公式: $TTRI = a \\cdot DEM + b \\cdot Slope + c \\cdot \\cos(Aspect)$
2. TCR（热约束残差）：捕捉30m尺度下的温度残差并空间化到10m
   公式: $TCR_{{30m}} = LST_{{true,30m}} - \\overline{{LST_{{pred,30m\\_block}}}}$
3. 温度重建模型：融合多光谱、地形、TTRI、TCR特征进行10m LST预测
   特征列: [R, G, B, NIR, SWIR1, NDVI, NDWI, NDBI, TTRI]
   当前使用的模型: {self.current_model_name}

输入数据：
- Landsat 8/9 L2级ST_B10（30m地表温度）
- Sentinel-2 L2A多光谱（10m: B2/B3/B4/B8/B11/B12）
- Copernicus DEM（30m数字高程模型）
- QA_PIXEL / SCL 质量掩膜

输出：10m分辨率地表温度产品

请用简洁专业的语言回答用户问题。当用户询问算法实现细节时，请基于当前使用的{self.current_model_name}模型来回答。
重要：必须使用以下完整名称，不要使用简称或缩写替代：
- TTRI 必须写为"地形热响应指数（TTRI）"
- TCR 必须写为"热约束残差（TCR）"
不要写成"地形特征分析"或"残差空间化"等非标准名称。
数学公式请使用 LaTeX 语法，行内公式用 $...$ 包裹，块级公式用 $$...$$ 包裹。
格式规则：
- 列表必须使用无序列表（用 - 开头），禁止使用编号列表（如 1. 2. 3.）
- 禁止输出 ### 等 markdown 标记符作为纯文本，标题直接用 **粗体** 表示即可
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
    ) -> str:
        """流式向AI提问，每次收到 token 回调 on_token(累计内容)"""
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
            return self._call_openai_api_stream(messages, on_token)

    def _call_api(self, messages: List[Dict], **overrides) -> str:
        """调用大模型API，overrides 可覆盖 max_tokens/temperature 等参数"""
        if self.api_format == 'anthropic':
            return self._call_anthropic_api(messages)
        else:
            return self._call_openai_api(messages, **overrides)

    def _call_openai_api(self, messages: List[Dict], **overrides) -> str:
        """调用 OpenAI 兼容格式 API"""
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

        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            return f"API调用失败: {str(e)}"

    def _call_openai_api_stream(
        self, messages: List[Dict], on_token: Callable[[str], None]
    ) -> str:
        """流式调用 OpenAI 兼容格式 API，每次收到 token 回调 on_token(累计内容)"""
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
                            content = (
                                chunk.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content", "")
                            )
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
