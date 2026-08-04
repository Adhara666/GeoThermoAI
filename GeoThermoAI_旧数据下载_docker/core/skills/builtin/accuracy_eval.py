"""
精度评估 Skill（A-07 / 第6节 重写）

    - 不再把"独立预测精度"和"TCR后回聚合闭合度"混进同一份 JSON：本 Skill 现在
      只负责粗尺度闭合协议（coarse_constraint_closure，A-07 协议二），使用
      完整30m约束层作为参考，明确标注不是独立10m精度、不代表能量/辐射守恒；
      独立预测协议（independent_prediction）已随 rf_model 阶段一并产出
      （因为需要模型对象，而本 Skill 拿不到 model_path）；
    - 彻底删除 5K 阈值判据：不再输出 max_abs_deviation / threshold_K / passed
      字段及"通过/超出"文案（用户确认第5条）；
    - Agent 固定注入的 full_30m_csv/meta_30m_json 仍指向 step2 抽样文件，本
      Skill 按固定命名约定从其所在目录推导完整30m约束层路径，不需要 Agent
      额外注入新参数（core/agent/geo_thermo_agent.py 未修改）。
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ... import manifest as run_manifest


class AccuracyEvalSkill(BaseSkill):
    """精度评估：粗尺度闭合协议（A-07 协议二）"""

    @property
    def name(self) -> str:
        return "accuracy_eval"

    @property
    def group(self) -> str:
        return "accuracy_eval"

    @property
    def description(self) -> str:
        return "使用完整30m约束层作为参考，评估10m最终结果回聚合到30m产品格网的算术均值闭合度（MB/MAE/RMSE），并报告各自完整有效输出范围的低/高端有符号温差；不称为独立10m精度，不代表能量/辐射守恒。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="test_csv", type="file_path", description="（保留兼容参数，本Skill不再使用；独立预测评估已随rf_model阶段产出）", required=False),
            SkillParameter(name="full_30m_csv", type="file_path", description="30m step2抽样CSV路径（仅用于定位同目录下的完整约束层）", required=True),
            SkillParameter(name="predict_csv", type="file_path", description="10m最终结果CSV路径（含row, col, LST_final列）", required=True),
            SkillParameter(name="output_dir", type="file_path", description="输出目录路径", required=True),
            SkillParameter(name="meta_30m_json", type="file_path", description="30m step2元数据JSON路径（同上，仅用于定位）", required=True),
            SkillParameter(name="meta_10m_json", type="file_path", description="10m元数据JSON路径（含transform仿射变换参数）", required=True),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "full_30m_csv": "30m全量数据CSV路径",
            "predict_csv": "10m最终结果CSV路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "closure_metrics": "粗尺度闭合评估结果（A-07协议二）",
            "report_path": "评估报告JSON路径",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行粗尺度闭合评估。"""
        full_30m_csv = params.get("full_30m_csv", "")
        predict_csv = params.get("predict_csv", "")
        output_dir = params.get("output_dir", "")
        meta_10m_json = params.get("meta_10m_json", "")

        for name, val in [
            ("full_30m_csv", full_30m_csv), ("predict_csv", predict_csv),
            ("output_dir", output_dir), ("meta_10m_json", meta_10m_json),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        processed_dir = os.path.dirname(full_30m_csv)
        constraint_csv = params.get("constraint_csv") or os.path.join(processed_dir, "30m_constraint_grid.csv")
        constraint_meta = params.get("constraint_meta") or os.path.join(processed_dir, "30m_constraint_grid_meta.json")

        for label, path in [
            ("完整30m约束层", constraint_csv), ("完整30m约束层元数据", constraint_meta),
            ("10m最终结果", predict_csv), ("10m元数据", meta_10m_json),
        ]:
            if not os.path.isfile(path):
                return SkillResult(success=False, message=f"缺少必需输入文件（{label}）: {path}")

        try:
            from ...evaluation import evaluate_coarse_constraint_closure
        except ImportError:
            return SkillResult(success=False, message="无法导入评估模块")

        if log_callback:
            log_callback("INFO", f"开始粗尺度闭合评估: 完整30m约束层={constraint_csv}, 10m最终结果={predict_csv}")

        try:
            result = evaluate_coarse_constraint_closure(
                constraint_csv=constraint_csv,
                constraint_meta_json=constraint_meta,
                lst_final_csv=predict_csv,
                meta_10m_json=meta_10m_json,
                output_dir=output_dir,
                tcr_mode=params.get("tcr_mode"),
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"粗尺度闭合评估失败: {e}")

        closure = result.get("closure", {})
        value_range = result.get("value_range", {})
        metrics = closure.get("metrics", {})

        result_data = {
            "closure_metrics": result,
            "report_path": result.get("output_path", ""),
        }

        artifacts = [result["output_path"]] if result.get("output_path") else []

        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            run_manifest.record_stage(
                project_root, "accuracy_eval", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"closure": closure, "value_range": value_range},
            )

        if progress_callback:
            progress_callback("accuracy_eval", 1.0, "精度评估完成")

        return SkillResult(
            success=True,
            message=(
                f"粗尺度闭合评估完成: MB={metrics.get('MB_K', 'N/A')}K, "
                f"MAE={metrics.get('MAE_K', 'N/A')}K, RMSE={metrics.get('RMSE_K', 'N/A')}K; "
                f"低端差={value_range.get('low_end_difference_K', 'N/A')}K, "
                f"高端差={value_range.get('high_end_difference_K', 'N/A')}K; "
                f"匹配格数={closure.get('n_matched_cells', 0):,}（不代表能量/辐射守恒，不是独立10m精度）"
            ),
            data=result_data,
            artifacts=artifacts,
        )
