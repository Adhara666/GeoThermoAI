"""
精度评估 Skill

调用 evaluate_spatial_consistency() 执行空间一致性评估：
    - 将10m LST_final聚合到30m位置，与测试集对比计算MB/MAE/RMSE
    - 统计值域范围，验证偏差 < 5K
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class AccuracyEvalSkill(BaseSkill):
    """精度评估：空间一致性 + 值域范围分析"""

    @property
    def name(self) -> str:
        return "accuracy_eval"

    @property
    def group(self) -> str:
        return "accuracy_eval"

    @property
    def description(self) -> str:
        return "将10m降尺度结果聚合到30m网格，与测试集对比计算空间一致性指标（MB/MAE/RMSE），并分析值域偏差是否满足<5K阈值。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="test_csv",
                type="file_path",
                description="30m测试集CSV路径（含row, col, LST列）",
                required=True,
            ),
            SkillParameter(
                name="full_30m_csv",
                type="file_path",
                description="30m全量数据CSV路径（用于LST范围扫描）",
                required=True,
            ),
            SkillParameter(
                name="predict_csv",
                type="file_path",
                description="10m预测结果CSV路径（含row, col, LST_final列）",
                required=True,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="meta_30m_json",
                type="file_path",
                description="30m元数据JSON路径（含transform仿射变换参数）",
                required=True,
            ),
            SkillParameter(
                name="meta_10m_json",
                type="file_path",
                description="10m元数据JSON路径（含transform仿射变换参数）",
                required=True,
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "test_csv": "30m测试集CSV路径",
            "full_30m_csv": "30m全量数据CSV路径",
            "predict_csv": "10m预测结果CSV路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "metrics": "评估指标（空间一致性+值域范围）",
            "report_path": "评估报告JSON路径",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行精度评估。"""
        test_csv = params.get("test_csv", "")
        full_30m_csv = params.get("full_30m_csv", "")
        predict_csv = params.get("predict_csv", "")
        output_dir = params.get("output_dir", "")
        meta_30m_json = params.get("meta_30m_json", "")
        meta_10m_json = params.get("meta_10m_json", "")

        for name, val in [
            ("test_csv", test_csv),
            ("full_30m_csv", full_30m_csv),
            ("predict_csv", predict_csv),
            ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        if not meta_30m_json or not meta_10m_json:
            return SkillResult(
                success=False,
                message="参数 meta_30m_json 和 meta_10m_json 不能为空（需要元数据文件来读取仿射变换参数）",
            )

        try:
            from ...evaluation import evaluate_spatial_consistency
        except ImportError:
            return SkillResult(success=False, message="无法导入评估模块")

        if log_callback:
            log_callback("INFO", f"开始精度评估: 测试集={test_csv}, 预测结果={predict_csv}")

        try:
            result = evaluate_spatial_consistency(
                test_csv=test_csv,
                full_30m_csv=full_30m_csv,
                predict_csv=predict_csv,
                output_dir=output_dir,
                meta_30m_json=meta_30m_json,
                meta_10m_json=meta_10m_json,
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"精度评估失败: {e}")

        sc = result.get("spatial_consistency", {})
        vr = result.get("value_range", {})
        metrics = sc.get("metrics", {})
        deviation = vr.get("deviation", {})

        result_data = {
            "metrics": result,
            "report_path": result.get("output_path", ""),
        }

        artifacts = []
        if result.get("output_path"):
            artifacts.append(result["output_path"])

        if progress_callback:
            progress_callback("accuracy_eval", 1.0, "精度评估完成")

        passed_str = "通过" if deviation.get("passed", False) else "未通过"

        return SkillResult(
            success=True,
            message=(
                f"空间一致性: MB={metrics.get('MB', 'N/A')}, "
                f"MAE={metrics.get('MAE', 'N/A')}, "
                f"RMSE={metrics.get('RMSE', 'N/A')}; "
                f"值域偏差={deviation.get('max_abs_deviation', 'N/A')}K ({passed_str}), "
                f"匹配像素={sc.get('n_matched', 0):,}"
            ),
            data=result_data,
            artifacts=artifacts,
        )
