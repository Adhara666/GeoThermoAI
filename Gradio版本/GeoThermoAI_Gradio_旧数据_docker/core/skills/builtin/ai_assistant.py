"""
AI 智能助手 Skill

调用LLM API提供以下功能：
    - 参数推荐: 根据数据特征推荐最优超参数
    - 结果诊断: 分析评估结果，给出改进建议
    - 报告生成: 基于流水线结果生成自然语言技术报告
"""

import json
import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class AIAssistantSkill(BaseSkill):
    """AI智能助手：参数推荐、结果诊断、报告生成"""

    @property
    def name(self) -> str:
        return "ai_assistant"

    @property
    def group(self) -> str:
        return "ai_assist"

    @property
    def description(self) -> str:
        return "调用LLM API进行参数推荐（根据数据特征优化超参数）、结果诊断（分析评估指标并给出改进建议）和技术报告生成。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="mode",
                type="string",
                description="助手模式: 'recommend'（参数推荐）、'diagnose'（结果诊断）、'report'（报告生成）",
                required=True,
                choices=["recommend", "diagnose", "report"],
            ),
            SkillParameter(
                name="context",
                type="string",
                description="上下文JSON字符串，包含当前步骤的数据信息（如评估指标、数据统计量等）",
                required=True,
            ),
            SkillParameter(
                name="api_key",
                type="string",
                description="LLM API密钥（可选，也可通过环境变量 LLM_API_KEY 设置）",
                required=False,
            ),
            SkillParameter(
                name="model_type",
                type="string",
                description="LLM模型类型",
                required=False,
                default="gpt-4o-mini",
                choices=["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo", "deepseek-chat"],
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "mode": "助手模式（recommend/diagnose/report）",
            "context": "上下文信息",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "result": "LLM返回的文本结果",
            "data": "结构化数据（如推荐参数、诊断结论等）",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行AI助手功能。"""
        mode = params.get("mode", "")
        context = params.get("context", "")
        api_key = params.get("api_key", "") or os.environ.get("LLM_API_KEY", "")
        model_type = params.get("model_type", "gpt-4o-mini")

        if not mode:
            return SkillResult(success=False, message="参数 mode 不能为空")
        if not context:
            return SkillResult(success=False, message="参数 context 不能为空")

        # 解析上下文
        if isinstance(context, str):
            try:
                ctx_data = json.loads(context)
            except json.JSONDecodeError:
                ctx_data = {"raw": context}
        else:
            ctx_data = context

        if progress_callback:
            progress_callback("ai_assistant", 0.1, f"准备AI请求 (模式={mode}, 模型={model_type})")

        # ── 构建Prompt ───────────────────────────────────────────────
        system_prompt, user_prompt = self._build_prompts(mode, ctx_data)

        if log_callback:
            log_callback("INFO", f"AI助手模式: {mode}, 模型: {model_type}")

        # ── 调用LLM API ──────────────────────────────────────────────
        if progress_callback:
            progress_callback("ai_assistant", 0.3, "调用LLM API...")

        try:
            llm_result = self._call_llm(
                api_key=api_key,
                model_type=model_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as e:
            return SkillResult(
                success=False,
                message=f"LLM API调用失败: {e}",
            )

        if progress_callback:
            progress_callback("ai_assistant", 0.8, "解析LLM返回结果...")

        # ── 解析结构化结果 ───────────────────────────────────────────
        structured_data = self._parse_structured_result(llm_result, mode)

        if progress_callback:
            progress_callback("ai_assistant", 1.0, "AI助手完成")

        return SkillResult(
            success=True,
            message=f"AI助手 ({mode}) 完成",
            data={
                "result": llm_result,
                "data": structured_data,
            },
        )

    @staticmethod
    def _build_prompts(mode: str, ctx_data: dict):
        """根据模式构建System和User Prompt。"""

        if mode == "recommend":
            system = (
                "你是一个地表温度降尺度专家。根据用户提供的数据特征和统计信息，"
                "推荐最优的随机森林超参数。请以JSON格式返回推荐参数。"
            )
            user = (
                f"以下是当前数据集的信息:\n{json.dumps(ctx_data, ensure_ascii=False, indent=2)}\n\n"
                "请推荐随机森林模型的超参数，包括: n_estimators, max_depth, min_samples_split, "
                "min_samples_leaf, max_features。\n"
                "请以JSON格式返回，包含 recommended_params 和 reasoning 两个字段。"
            )

        elif mode == "diagnose":
            system = (
                "你是一个地表温度降尺度精度分析专家。根据用户提供的评估指标和数据统计信息，"
                "诊断模型性能并给出改进建议。"
            )
            user = (
                f"以下是评估结果:\n{json.dumps(ctx_data, ensure_ascii=False, indent=2)}\n\n"
                "请分析:\n"
                "1. 模型精度是否达标\n"
                "2. 空间一致性指标解读\n"
                "3. 值域范围是否合理\n"
                "4. 可能的改进方向\n"
                "请以JSON格式返回，包含 diagnosis, severity, suggestions 三个字段。"
            )

        elif mode == "report":
            system = (
                "你是一个遥感技术报告撰写专家。根据用户提供的流水线执行结果，"
                "生成一份结构清晰的技术报告。"
            )
            user = (
                f"以下是流水线完整结果:\n{json.dumps(ctx_data, ensure_ascii=False, indent=2)}\n\n"
                "请生成一份技术报告，包含:\n"
                "1. 数据概况\n"
                "2. 方法说明\n"
                "3. 模型性能\n"
                "4. 精度评估\n"
                "5. 结论与建议\n"
                "请以JSON格式返回，包含 title, summary, sections 三个字段。"
            )

        else:
            system = "你是一个AI助手。"
            user = f"请处理以下请求:\n{json.dumps(ctx_data, ensure_ascii=False, indent=2)}"

        return system, user

    @staticmethod
    def _call_llm(api_key: str, model_type: str, system_prompt: str, user_prompt: str) -> str:
        """调用LLM API。支持OpenAI兼容接口。"""
        try:
            from openai import OpenAI
        except ImportError:
            # 无openai库时使用模拟
            return json.dumps({
                "note": "未安装openai库，请运行 pip install openai",
                "mode_hint": "使用 --api_key 参数或设置 LLM_API_KEY 环境变量",
            }, ensure_ascii=False, indent=2)

        if not api_key:
            return json.dumps({
                "note": "未提供API密钥，请通过 api_key 参数或 LLM_API_KEY 环境变量设置",
            }, ensure_ascii=False, indent=2)

        # 根据模型类型选择base_url
        if "deepseek" in model_type:
            base_url = "https://api.deepseek.com/v1"
        else:
            base_url = "https://api.openai.com/v1"

        client = OpenAI(api_key=api_key, base_url=base_url)

        response = client.chat.completions.create(
            model=model_type,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )

        return response.choices[0].message.content

    @staticmethod
    def _parse_structured_result(llm_text: str, mode: str) -> dict:
        """尝试从LLM返回中解析JSON结构化数据。"""
        try:
            # 尝试直接解析
            return json.loads(llm_text)
        except json.JSONDecodeError:
            pass

        # 尝试提取JSON代码块
        import re
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", llm_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # 无法解析时返回原始文本
        return {"raw_text": llm_text}
