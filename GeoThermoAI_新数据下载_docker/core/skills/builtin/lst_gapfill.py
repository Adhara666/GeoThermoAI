"""
10m LST 空洞填补（结果后处理）Skill

调用 core.gapfill 的多尺度金字塔 + IDW 算法，对 `lst_export` 导出的
10m 地表温度 GeoTIFF 中因云像元扣除造成的 nodata 空洞做空间重建：

    - 不改变任何无云像元的数值；
    - 填洞值只是空间估计，不参与 TCR / 闭合精度评价；
    - 同时输出空洞掩膜 GeoTIFF（1=估计像元，0=原始有效），供下游区分真实观测与重建值；
    - 输出带日期文件名（升级点 4）：rf_10m_lst_final_filled_{date}.tif / _cloud_mask.tif。
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ... import manifest as run_manifest


class LSTGapFillSkill(BaseSkill):
    """结果后处理（可选）：填充 10m LST 中云像元造成的空洞"""

    @property
    def name(self) -> str:
        return "lst_gapfill"

    @property
    def group(self) -> str:
        return "lst_gapfill"

    @property
    def description(self) -> str:
        return "结果后处理（可选）：对已导出的 10m 地表温度产品做空洞填补（gap filling），填充因云像元扣除造成的 nodata 空洞，得到无空洞的 10m 地表温度产品；只估计空洞像元，不改变无云区数值。当用户要求「无空洞/填洞/空洞填补/结果后处理」的结果时调用。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="input_tif", type="file_path", description="原始 10m LST GeoTIFF 路径（lst_export 产物，含空洞）", required=True),
            SkillParameter(name="output_dir", type="file_path", description="输出目录路径", required=True),
            SkillParameter(name="output_tif", type="file_path", description="填洞后 GeoTIFF 输出路径", required=False),
            SkillParameter(name="output_mask", type="file_path", description="空洞掩膜 GeoTIFF 输出路径（1=估计像元，0=原始有效）", required=False),
            SkillParameter(name="max_level", type="number", description="金字塔最大下采样层数，默认 8", required=False, default=8),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "input_tif": "原始 10m LST GeoTIFF 路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "filled_tif": "填洞后 GeoTIFF 路径",
            "mask_tif": "空洞掩膜 GeoTIFF 路径",
            "stats": "填洞统计信息",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        input_tif = params.get("input_tif", "")
        output_dir = params.get("output_dir", "")
        output_tif = params.get("output_tif", "")
        output_mask = params.get("output_mask", "")
        try:
            max_level = int(params.get("max_level", 8))
        except (TypeError, ValueError):
            max_level = 8

        if not input_tif or not os.path.isfile(input_tif):
            return SkillResult(success=False, message=f"缺少原始 10m LST 影像：{input_tif or '<空>'}")
        if not output_dir:
            return SkillResult(success=False, message="缺少输出目录参数 output_dir")

        # 默认输出命名：与原始产品同目录，带日期前缀 filled
        if not output_tif:
            output_tif = os.path.join(output_dir, "rf_10m_lst_final_filled.tif")
        if not output_mask:
            output_mask = os.path.join(output_dir, "rf_10m_lst_final_filled_cloud_mask.tif")

        if log_callback:
            log_callback("INFO", f"开始结果后处理（空洞填补）: 输入={input_tif}")

        try:
            from ...gapfill import gapfill_lst
        except ImportError:
            return SkillResult(success=False, message="无法导入空洞填补核心模块 core.gapfill")

        try:
            stats = gapfill_lst(
                input_tif=input_tif,
                output_tif=output_tif,
                output_mask_tif=output_mask,
                max_level=max_level,
                progress_callback=lambda pct, msg: (
                    progress_callback("lst_gapfill", pct, msg) if progress_callback else None
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"空洞填补失败: {e}")

        if log_callback:
            log_callback(
                "INFO",
                f"空洞填补完成: 空洞 {stats.get('filled_pixels', 0):,} 像元"
                f"（{stats.get('filled_ratio', 0) * 100:.1f}%），"
                f"输出 {os.path.basename(output_tif)}",
            )

        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            try:
                run_manifest.record_stage(
                    project_root, "postprocess", run_manifest.STATUS_COMPLETED,
                    artifacts={"filled_tif": output_tif, "mask_tif": output_mask},
                    stats={
                        "filled_pixels": stats.get("filled_pixels"),
                        "filled_ratio": stats.get("filled_ratio"),
                        "used_pyramid": stats.get("used_pyramid"),
                        "max_level": stats.get("max_level"),
                    },
                )
            except Exception as e:
                if log_callback:
                    log_callback("WARNING", f"记录结果后处理阶段清单失败（已忽略）: {e}")

        if progress_callback:
            progress_callback("lst_gapfill", 1.0, "结果后处理（空洞填补）完成")

        return SkillResult(
            success=True,
            message=(
                f"结果后处理完成：填补空洞 {stats.get('filled_pixels', 0):,} 个像元"
                f"（占总像元 {stats.get('filled_ratio', 0) * 100:.1f}%），"
                f"填洞后温度范围 {stats.get('after', {}).get('min', 'N/A'):.2f}–"
                f"{stats.get('after', {}).get('max', 'N/A'):.2f} K；"
                f"未改变无云区数值，空洞掩膜已同步输出。"
            ),
            data={
                "filled_tif": output_tif,
                "mask_tif": output_mask,
                "stats": stats,
                "used_pyramid": stats.get("used_pyramid"),
                "filled_pixels": stats.get("filled_pixels"),
                "filled_ratio": stats.get("filled_ratio"),
            },
            artifacts=[output_tif, output_mask],
        )
