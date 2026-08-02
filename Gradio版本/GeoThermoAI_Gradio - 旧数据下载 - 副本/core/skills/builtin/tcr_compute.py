"""
TCR 热约束残差修正 Skill

调用 compute_tcr() 计算热约束残差（Thermal Constraint Residual）：
    Phase 1: 加载RF模型 → 逐批预测10m LST_pred → 按30m块聚合 → 计算TCR_30m
    Phase 2: 构建30m TCR规则网格 → 双线性插值到10m → 输出CSV
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class TCRComputeSkill(BaseSkill):
    """TCR热约束残差修正"""

    @property
    def name(self) -> str:
        return "tcr_compute"

    @property
    def group(self) -> str:
        return "tcr_compute"

    @property
    def description(self) -> str:
        return "计算热约束残差（TCR = LST_true_30m - mean(LST_pred_in_30m_block)），通过30m→10m双线性插值校正10m预测结果。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="data_30m_csv",
                type="file_path",
                description="30m全量数据CSV路径（含LST列）",
                required=True,
            ),
            SkillParameter(
                name="meta_30m_json",
                type="file_path",
                description="30m元数据JSON路径（含transform参数）",
                required=True,
            ),
            SkillParameter(
                name="predict_10m_csv",
                type="file_path",
                description="10m预测数据CSV路径（含TTRI列）",
                required=True,
            ),
            SkillParameter(
                name="meta_10m_json",
                type="file_path",
                description="10m元数据JSON路径（含transform参数）",
                required=True,
            ),
            SkillParameter(
                name="model_path",
                type="file_path",
                description="训练好的RF模型.pkl文件路径",
                required=True,
            ),
            SkillParameter(
                name="output_path",
                type="file_path",
                description="输出CSV路径",
                required=True,
            ),
            SkillParameter(
                name="batch_size",
                type="number",
                description="批处理大小，默认 500000",
                required=False,
                default=500000,
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "data_30m_csv": "30m全量数据CSV路径",
            "meta_30m_json": "30m元数据JSON路径",
            "predict_10m_csv": "10m预测数据CSV路径",
            "meta_10m_json": "10m元数据JSON路径",
            "model_path": "RF模型文件路径",
            "output_path": "输出CSV路径",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "predict_with_tcr": "含TCR的10m预测CSV路径",
            "tcr_statistics": "TCR统计信息",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行TCR计算。"""
        data_30m_csv = params.get("data_30m_csv", "")
        meta_30m_json = params.get("meta_30m_json", "")
        predict_10m_csv = params.get("predict_10m_csv", "")
        meta_10m_json = params.get("meta_10m_json", "")
        model_path = params.get("model_path", "")
        output_path = params.get("output_path", "")
        batch_size = params.get("batch_size", 500000)

        for name, val in [
            ("data_30m_csv", data_30m_csv),
            ("meta_30m_json", meta_30m_json),
            ("predict_10m_csv", predict_10m_csv),
            ("meta_10m_json", meta_10m_json),
            ("model_path", model_path),
            ("output_path", output_path),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        try:
            from ...tcr import compute_tcr
        except ImportError:
            return SkillResult(success=False, message="无法导入TCR计算模块")

        if log_callback:
            log_callback("INFO", f"开始TCR计算: 30m数据={data_30m_csv}, 10m预测={predict_10m_csv}")

        try:
            result = compute_tcr(
                data_30m_csv=data_30m_csv,
                meta_30m_json=meta_30m_json,
                predict_10m_csv=predict_10m_csv,
                meta_10m_json=meta_10m_json,
                model_path=model_path,
                output_path=output_path,
                batch_size=batch_size,
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"TCR计算失败: {e}")

        tcr_stats = result.get("tcr_statistics", {})

        result_data = {
            "predict_with_tcr": result.get("output_path", ""),
            "tcr_statistics": tcr_stats,
            "total_valid_10m": result.get("total_valid_10m", 0),
            "total_invalid_10m": result.get("total_invalid_10m", 0),
            "total_seconds": result.get("total_seconds", 0),
        }

        if progress_callback:
            progress_callback("tcr_compute", 1.0, "TCR计算完成")

        return SkillResult(
            success=True,
            message=(
                f"TCR计算完成: mean={tcr_stats.get('mean', 'N/A'):.4f}, "
                f"std={tcr_stats.get('std', 'N/A'):.4f}, "
                f"有效块={tcr_stats.get('n_valid_blocks', 0):,}, "
                f"耗时 {result.get('total_seconds', 0):.1f}s"
            ),
            data=result_data,
            artifacts=[output_path],
        )
