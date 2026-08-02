"""
数据预处理+数据集划分 Skill

整合数据预处理和数据集划分两个步骤：
    1. 调用 process_preprocessing() 生成30m训练特征CSV和10m预测特征CSV
    2. 调用 split_dataset() 将30m数据划分为训练/验证/测试集
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class DataPipelineSkill(BaseSkill):
    """数据预处理 + 数据集划分：对齐栅格、计算指数、生成CSV并划分数据集"""

    @property
    def name(self) -> str:
        return "data_pipeline"

    @property
    def group(self) -> str:
        return "data_process"

    @property
    def description(self) -> str:
        return "将 Landsat/Sentinel-2/DEM 栅格数据进行预处理（对齐、掩膜、光谱指数），生成30m训练CSV和10m预测CSV，并按比例划分训练/验证/测试集。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="landsat_path",
                type="file_path",
                description="Landsat L2 ST_B10 栅格路径",
                required=True,
            ),
            SkillParameter(
                name="sentinel2_path",
                type="file_path",
                description="Sentinel-2 L2A 多光谱栅格路径",
                required=True,
            ),
            SkillParameter(
                name="qa_path",
                type="file_path",
                description="Landsat QA_PIXEL 栅格路径",
                required=True,
            ),
            SkillParameter(
                name="scl_path",
                type="file_path",
                description="Sentinel-2 SCL 栅格路径",
                required=True,
            ),
            SkillParameter(
                name="dem_path",
                type="file_path",
                description="DEM 栅格路径",
                required=True,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="train_ratio",
                type="number",
                description="训练集比例",
                required=False,
                default=0.6,
            ),
            SkillParameter(
                name="val_ratio",
                type="number",
                description="验证集比例",
                required=False,
                default=0.2,
            ),
            SkillParameter(
                name="test_ratio",
                type="number",
                description="测试集比例",
                required=False,
                default=0.2,
            ),
            SkillParameter(
                name="seed",
                type="number",
                description="随机种子",
                required=False,
                default=42,
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "landsat_path": "Landsat ST_B10 栅格路径",
            "sentinel2_path": "Sentinel-2 L2A 多光谱栅格路径",
            "qa_path": "Landsat QA_PIXEL 栅格路径",
            "scl_path": "Sentinel-2 SCL 栅格路径",
            "dem_path": "DEM 栅格路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "train_csv": "训练集CSV路径",
            "val_csv": "验证集CSV路径",
            "test_csv": "测试集CSV路径",
            "train_meta": "30m训练元数据JSON路径",
            "predict_meta": "10m预测元数据JSON路径",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行数据预处理和数据集划分。"""
        landsat_path = params.get("landsat_path", "")
        sentinel2_path = params.get("sentinel2_path", "")
        qa_path = params.get("qa_path", "")
        scl_path = params.get("scl_path", "")
        dem_path = params.get("dem_path", "")
        output_dir = params.get("output_dir", "")
        train_ratio = params.get("train_ratio", 0.6)
        val_ratio = params.get("val_ratio", 0.2)
        test_ratio = params.get("test_ratio", 0.2)
        seed = params.get("seed", 42)

        # 参数校验
        for name, val in [
            ("landsat_path", landsat_path),
            ("sentinel2_path", sentinel2_path),
            ("qa_path", qa_path),
            ("scl_path", scl_path),
            ("dem_path", dem_path),
            ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        try:
            from ...data_preprocessing import process_preprocessing
            from ...split_dataset import split_dataset
        except ImportError:
            return SkillResult(
                success=False,
                message="无法导入核心预处理模块，请检查项目结构",
            )

        def _step_progress(step_name, percent, message):
            if progress_callback:
                progress_callback(step_name, percent, message)

        # ── 步骤1: 数据预处理 ────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始数据预处理...")

        try:
            prep_result = process_preprocessing(
                landsat_path=landsat_path,
                sentinel2_path=sentinel2_path,
                qa_path=qa_path,
                scl_path=scl_path,
                dem_path=dem_path,
                output_dir=output_dir,
                progress_callback=lambda sn, pct, msg: _step_progress(
                    "data_pipeline", pct * 0.6, f"[预处理] {msg}"
                ),
            )
        except Exception as e:
            return SkillResult(
                success=False,
                message=f"数据预处理失败: {e}",
            )

        train_csv = prep_result.get("train_csv", "")
        predict_csv = prep_result.get("predict_csv", "")
        train_meta = prep_result.get("train_meta", "")
        predict_meta = prep_result.get("predict_meta", "")

        if log_callback:
            log_callback("INFO", f"预处理完成: 训练CSV={train_csv}, 预测CSV={predict_csv}")

        # ── 步骤2: 数据集划分 ────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始数据集划分...")

        train_dir = output_dir

        try:
            split_result = split_dataset(
                input_csv=train_csv,
                output_dir=train_dir,
                train_ratio=train_ratio,
                val_ratio=val_ratio,
                test_ratio=test_ratio,
                seed=seed,
                progress_callback=lambda sn, pct, msg: _step_progress(
                    "data_pipeline", 0.6 + pct * 0.4, f"[划分] {msg}"
                ),
            )
        except Exception as e:
            return SkillResult(
                success=False,
                message=f"数据集划分失败: {e}",
            )

        result_data = {
            "train_csv": os.path.join(train_dir, "train.csv"),
            "val_csv": os.path.join(train_dir, "validate.csv"),
            "test_csv": os.path.join(train_dir, "test.csv"),
            "train_meta": train_meta,
            "predict_meta": predict_meta,
            "predict_csv": predict_csv,
            "split_stats": split_result,
            "train_rows": prep_result.get("train_rows", 0),
            "predict_valid_pixels": prep_result.get("predict_valid_pixels", 0),
        }

        artifacts = [
            result_data["train_csv"],
            result_data["val_csv"],
            result_data["test_csv"],
            train_meta,
            predict_meta,
            predict_csv,
        ]

        if progress_callback:
            progress_callback("data_pipeline", 1.0, "数据预处理+划分完成")

        return SkillResult(
            success=True,
            message=(
                f"预处理完成: 训练 {prep_result.get('train_rows', 0):,} 行, "
                f"预测 {prep_result.get('predict_valid_pixels', 0):,} 有效像素; "
                f"划分完成: 训练 {split_result.get('train', {}).get('count', 0):,}, "
                f"验证 {split_result.get('validate', {}).get('count', 0):,}, "
                f"测试 {split_result.get('test', {}).get('count', 0):,}"
            ),
            data=result_data,
            artifacts=artifacts,
        )
