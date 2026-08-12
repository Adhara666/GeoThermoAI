"""
TCR 热约束残差修正 Skill

    - 30m 参考为完整 30m 约束层 30m_constraint_grid.parquet；Agent 固定注入的
      data_30m_csv 参数仍指向 30m_features_step2.parquet，仅用于定位约束层所在
      目录（即预处理阶段的 processed_dir），本 Skill 按固定命名约定自动推导
      完整约束层路径，不需要 Agent 额外注入新参数；
    - 细→粗映射使用 core.grid_mapping 的仿射逆变换；
    - 默认 TCR 模式为 block_constant，可选 smooth_recentered（实验性，附加诊断）。
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ... import manifest as run_manifest
from ...intermediate_cleanup import cleanup_stage
from ...stage_rebuild import ensure_stage_inputs


def _derive_constraint_paths(params: Dict[str, Any]) -> Dict[str, str]:
    """从 Agent 固定注入的 data_30m_csv/meta_30m_json（指向 step2 抽样文件）
    推导出同目录下的完整30m约束层固定文件名。"""
    legacy_csv = params.get("data_30m_csv", "")
    processed_dir = os.path.dirname(legacy_csv) if legacy_csv else ""
    return {
        "constraint_csv": params.get("constraint_csv")
                          or (os.path.join(processed_dir, "30m_constraint_grid.parquet") if processed_dir else ""),
        "constraint_meta": params.get("constraint_meta_json") or params.get("constraint_meta")
                           or (os.path.join(processed_dir, "30m_constraint_grid_meta.json") if processed_dir else ""),
    }


class TCRComputeSkill(BaseSkill):
    """TCR热约束残差修正（默认 block_constant，可选 smooth_recentered）"""

    @property
    def name(self) -> str:
        return "tcr_compute"

    @property
    def group(self) -> str:
        return "tcr_compute"

    @property
    def description(self) -> str:
        return "计算热约束残差（TCR = LST_true_30m - mean(LST_pred_in_30m_cell)），基于完整30m约束层与统一仿射映射，将10m预测结果按30m产品格网算术均值闭合修正。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="data_30m_csv", type="file_path", description="30m step2抽样Parquet路径（仅用于定位同目录下的完整约束层，不直接作为TCR参考）", required=True),
            SkillParameter(name="meta_30m_json", type="file_path", description="30m step2元数据JSON路径（同上，仅用于定位）", required=True),
            SkillParameter(name="predict_10m_csv", type="file_path", description="10m预测数据Parquet路径（含TTRI列）", required=True),
            SkillParameter(name="meta_10m_json", type="file_path", description="10m元数据JSON路径", required=True),
            SkillParameter(name="model_path", type="file_path", description="训练好的RF模型.pkl文件路径", required=True),
            SkillParameter(name="output_path", type="file_path", description="输出Parquet路径", required=True),
            SkillParameter(name="tcr_mode", type="string", description="TCR模式：block_constant（默认）或 smooth_recentered（实验性，附加诊断）", required=False, default="block_constant", choices=["block_constant", "smooth_recentered"]),
            SkillParameter(name="batch_size", type="number", description="批处理大小，默认 500000", required=False, default=500000),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "data_30m_csv": "30m全量数据Parquet路径",
            "meta_30m_json": "30m元数据JSON路径",
            "predict_10m_csv": "10m预测数据Parquet路径",
            "meta_10m_json": "10m元数据JSON路径",
            "model_path": "RF模型文件路径",
            "output_path": "输出Parquet路径",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "predict_with_tcr": "含TCR的10m预测Parquet路径",
            "tcr_statistics": "TCR统计信息",
            "validity": "有效性诊断",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行TCR计算。"""
        predict_10m_csv = params.get("predict_10m_csv", "")
        meta_10m_json = params.get("meta_10m_json", "")
        model_path = params.get("model_path", "")
        output_path = params.get("output_path", "")
        tcr_mode = params.get("tcr_mode", "block_constant")
        batch_size = params.get("batch_size", 500000)

        for name, val in [
            ("data_30m_csv", params.get("data_30m_csv", "")),
            ("meta_30m_json", params.get("meta_30m_json", "")),
            ("predict_10m_csv", predict_10m_csv),
            ("meta_10m_json", meta_10m_json),
            ("model_path", model_path),
            ("output_path", output_path),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        # 单步重跑支持：输入被阶段清理删除时自动重建（失败则明确报错，不静默）
        project_root = run_manifest.project_root_from_stage_output_dir(os.path.dirname(output_path) or output_dir)
        if project_root:
            try:
                ensure_stage_inputs(project_root, "tcr_compute", log_callback=log_callback)
            except Exception as e:
                return SkillResult(success=False, message=f"重建输入失败: {e}")

        derived = _derive_constraint_paths(params)
        constraint_csv = derived["constraint_csv"]
        constraint_meta = derived["constraint_meta"]

        for label, path in [
            ("完整30m约束层", constraint_csv), ("完整30m约束层元数据", constraint_meta),
        ]:
            if not path or not os.path.isfile(path):
                return SkillResult(
                    success=False,
                    message=f"缺少必需输入文件（{label}）: {path or '<无法推导路径>'}；"
                            f"请确认已成功完成 data_pipeline 阶段",
                )

        try:
            from ...tcr import compute_tcr
        except ImportError:
            return SkillResult(success=False, message="无法导入TCR计算模块")

        if log_callback:
            log_callback("INFO", f"开始TCR计算（模式={tcr_mode}）: 完整30m约束层={constraint_csv}, 10m预测={predict_10m_csv}")

        try:
            result = compute_tcr(
                constraint_csv=constraint_csv,
                constraint_meta_json=constraint_meta,
                predict_10m_csv=predict_10m_csv,
                meta_10m_json=meta_10m_json,
                model_path=model_path,
                output_path=output_path,
                mode=tcr_mode,
                batch_size=batch_size,
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"TCR计算失败: {e}")

        tcr_stats = result.get("tcr_statistics", {})
        validity = result.get("validity", {})

        result_data = {
            "predict_with_tcr": result.get("output_path", ""),
            "tcr_statistics": tcr_stats,
            "validity": validity,
            "mode": result.get("mode", tcr_mode),
            "grid_ratio_diagnostics": result.get("grid_ratio_diagnostics", {}),
            "smooth_recentered_diagnostics": result.get("smooth_recentered_diagnostics"),
            "total_valid_10m": result.get("total_valid_10m", 0),
            "total_invalid_10m": result.get("total_invalid_10m", 0),
            "total_seconds": result.get("total_seconds", 0),
        }

        project_root = run_manifest.project_root_from_stage_output_dir(os.path.dirname(output_path) or ".")
        if project_root:
            run_manifest.record_stage(
                project_root, "tcr_compute", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"tcr_statistics": tcr_stats, "validity": validity, "mode": result.get("mode")},
            )
            # TCR 计算完成后，10m 预测特征 Parquet（含 TTRI 列）已无下游消费方，立即清理
            cleanup_stage(project_root, "tcr_compute")

        if progress_callback:
            progress_callback("tcr_compute", 1.0, "TCR计算完成")

        return SkillResult(
            success=True,
            message=(
                f"TCR计算完成（{result.get('mode', tcr_mode)}）: mean={tcr_stats.get('mean', 'N/A'):.4f}K, "
                f"std={tcr_stats.get('std', 'N/A'):.4f}K, 有效格={tcr_stats.get('n_valid_blocks', 0):,}, "
                f"耗时 {result.get('total_seconds', 0):.1f}s；不代表辐射或能量守恒"
            ),
            data=result_data,
            artifacts=[output_path],
        )
