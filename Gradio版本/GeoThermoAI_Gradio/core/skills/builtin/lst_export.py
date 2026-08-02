"""
最终LST计算 + 导出 GeoTIFF Skill

调用 compute_lst_final() 和 export_geotiff()：
    1. 计算 LST_final = LST_pred + TCR
    2. 将结果导出为带地理参考的GeoTIFF影像
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class LSTExportSkill(BaseSkill):
    """最终LST计算 + 导出GeoTIFF"""

    @property
    def name(self) -> str:
        return "lst_export"

    @property
    def group(self) -> str:
        return "lst_export"

    @property
    def description(self) -> str:
        return "计算 LST_final = LST_pred + TCR，并将10m地表温度结果导出为带地理参考的GeoTIFF栅格影像。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="input_csv",
                type="file_path",
                description="含LST_pred和TCR列的CSV路径",
                required=True,
            ),
            SkillParameter(
                name="meta_10m_json",
                type="file_path",
                description="10m元数据JSON路径（含height, width, transform, crs）",
                required=True,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="chunk_size",
                type="number",
                description="批处理大小，默认 500000",
                required=False,
                default=500000,
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "input_csv": "含LST_pred和TCR列的CSV路径",
            "meta_10m_json": "10m元数据JSON路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "tif_path": "输出GeoTIFF文件路径",
            "stats": "影像统计信息",
            "total_rows": "总行数",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行LST最终计算和GeoTIFF导出。"""
        input_csv = params.get("input_csv", "")
        meta_10m_json = params.get("meta_10m_json", "")
        output_dir = params.get("output_dir", "")
        chunk_size = params.get("chunk_size", 500000)

        for name, val in [
            ("input_csv", input_csv),
            ("meta_10m_json", meta_10m_json),
            ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        try:
            from ...lst_final import compute_lst_final
            from ...export_geotiff import export_geotiff
        except ImportError:
            return SkillResult(success=False, message="无法导入LST计算/导出模块")

        results_dir = output_dir

        # ── 步骤1: 计算LST_final ────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始计算 LST_final = LST_pred + TCR...")

        lst_final_csv = os.path.join(results_dir, "rf_10m_predict.csv")

        try:
            lst_result = compute_lst_final(
                input_csv=input_csv,
                output_path=lst_final_csv,
                chunk_size=chunk_size,
                progress_callback=lambda sn, pct, msg: (
                    progress_callback(sn, pct * 0.5, f"[LST计算] {msg}") if progress_callback else None
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"LST最终计算失败: {e}")

        if log_callback:
            log_callback("INFO", f"LST计算完成: {lst_result.get('total_rows', 0):,} 行")

        # ── 步骤2: 导出GeoTIFF ──────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始导出GeoTIFF...")

        tif_path = os.path.join(results_dir, "rf_10m_lst_final.tif")

        try:
            tif_result = export_geotiff(
                lst_final_csv=lst_final_csv,
                meta_10m_json=meta_10m_json,
                output_path=tif_path,
                progress_callback=lambda sn, pct, msg: (
                    progress_callback(sn, 0.5 + pct * 0.5, f"[导出] {msg}") if progress_callback else None
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"GeoTIFF导出失败: {e}")

        stats = tif_result.get("stats", {})

        result_data = {
            "tif_path": tif_result.get("output_path", ""),
            "stats": stats,
            "total_rows": lst_result.get("total_rows", 0),
            "total_valid": lst_result.get("total_valid", 0),
            "image_size": tif_result.get("image_size", {}),
            "file_size_mb": tif_result.get("file_size_mb", 0),
            "lst_final_csv": lst_final_csv,
        }

        artifacts = [tif_path, lst_final_csv]

        if progress_callback:
            progress_callback("lst_export", 1.0, "LST计算+导出完成")

        return SkillResult(
            success=True,
            message=(
                f"GeoTIFF导出完成: {tif_result.get('image_size', {}).get('height', 0)}×"
                f"{tif_result.get('image_size', {}).get('width', 0)} 像素, "
                f"min={stats.get('min', 'N/A')}, max={stats.get('max', 'N/A')}, "
                f"有效率={stats.get('valid_percent', 0):.1f}%, "
                f"文件大小={tif_result.get('file_size_mb', 0):.2f}MB"
            ),
            data=result_data,
            artifacts=artifacts,
        )
