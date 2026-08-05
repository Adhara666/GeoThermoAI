"""
数据预处理+数据集划分 Skill

整合数据预处理和数据集划分两个步骤：
    1. 调用 process_preprocessing() 生成30m训练特征CSV（step2）、完整30m约束层
       （A-05）和10m预测特征CSV
    2. 调用 split_dataset() 按空间块 + guard buffer 划分训练/验证/测试集（B-01）
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ... import manifest as run_manifest
from ...intermediate_cleanup import cleanup_stage


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
        return "将 Landsat/Sentinel-2/DEM 栅格数据进行预处理（对齐、掩膜、光谱指数），生成30m训练CSV、完整30m约束层和10m预测CSV，并按空间块+guard buffer划分训练/验证/测试集。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="landsat_path", type="file_path", description="Landsat L2 ST_B10 栅格路径", required=True),
            SkillParameter(name="sentinel2_path", type="file_path", description="Sentinel-2 L2A 多光谱栅格路径", required=True),
            SkillParameter(name="qa_path", type="file_path", description="Landsat QA_PIXEL 栅格路径", required=True),
            SkillParameter(name="scl_path", type="file_path", description="Sentinel-2 SCL 栅格路径", required=True),
            SkillParameter(name="dem_path", type="file_path", description="DEM 栅格路径", required=True),
            SkillParameter(name="output_dir", type="file_path", description="输出目录路径", required=True),
            SkillParameter(name="train_ratio", type="number", description="训练集比例", required=False, default=0.6),
            SkillParameter(name="val_ratio", type="number", description="验证集比例", required=False, default=0.2),
            SkillParameter(name="test_ratio", type="number", description="测试集比例", required=False, default=0.2),
            SkillParameter(name="seed", type="number", description="随机种子（仅用于派生空间块哈希分配）", required=False, default=42),
            SkillParameter(name="block_size_px", type="number", description="空间块边长（像元数）", required=False, default=10),
            SkillParameter(name="guard_buffer_m", type="number", description="train/test 缓冲带宽度（米）", required=False, default=100.0),
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
            "constraint_csv": "完整30m约束层CSV路径",
            "constraint_meta": "完整30m约束层元数据JSON路径",
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
        block_size_px = params.get("block_size_px", 10)
        guard_buffer_m = params.get("guard_buffer_m", 100.0)

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

        # ── 步骤1: 数据预处理（含完整30m约束层，A-05）───────────────
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
            return SkillResult(success=False, message=f"数据预处理失败: {e}")

        train_csv = prep_result.get("train_csv", "")
        predict_csv = prep_result.get("predict_csv", "")
        train_meta = prep_result.get("train_meta", "")
        predict_meta = prep_result.get("predict_meta", "")
        constraint_csv = prep_result.get("constraint_csv", "")
        constraint_meta = prep_result.get("constraint_meta", "")

        if log_callback:
            log_callback("INFO", f"预处理完成: 训练CSV={train_csv}, 完整约束层={constraint_csv}, 预测CSV={predict_csv}")

        # ── 步骤2: 数据集划分（空间块 + guard buffer，B-01）──────────────
        if log_callback:
            log_callback("INFO", "开始数据集划分（空间块 + guard buffer）...")

        train_dir = output_dir

        try:
            split_result = split_dataset(
                input_csv=train_csv,
                output_dir=train_dir,
                train_ratio=train_ratio,
                val_ratio=val_ratio,
                test_ratio=test_ratio,
                seed=seed,
                block_size_px=block_size_px,
                guard_buffer_m=guard_buffer_m,
                progress_callback=lambda sn, pct, msg: _step_progress(
                    "data_pipeline", 0.6 + pct * 0.4, f"[划分] {msg}"
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"数据集划分失败: {e}")

        result_data = {
            "train_csv": os.path.join(train_dir, "train.csv"),
            "val_csv": os.path.join(train_dir, "validate.csv"),
            "test_csv": os.path.join(train_dir, "test.csv"),
            "train_meta": train_meta,
            "predict_meta": predict_meta,
            "predict_csv": predict_csv,
            "constraint_csv": constraint_csv,
            "constraint_meta": constraint_meta,
            "split_info": split_result.get("split_info", {}),
            "split_stats": split_result,
            "train_rows": prep_result.get("train_rows", 0),
            "constraint_rows": prep_result.get("constraint_rows", 0),
            "predict_valid_pixels": prep_result.get("predict_valid_pixels", 0),
        }

        artifacts = [
            result_data["train_csv"], result_data["val_csv"], result_data["test_csv"],
            train_meta, predict_meta, predict_csv, constraint_csv, constraint_meta,
        ]

        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            run_manifest.record_stage(
                project_root, "data_pipeline", run_manifest.STATUS_COMPLETED,
                artifacts={k: v for k, v in result_data.items() if isinstance(v, str) and v},
                stats={
                    "train_rows": result_data["train_rows"],
                    "constraint_rows": result_data["constraint_rows"],
                    "predict_valid_pixels": result_data["predict_valid_pixels"],
                    "split_info": result_data["split_info"],
                },
            )
            # 划分完成后，对齐栅格中间产物与 step2 抽样 CSV 不再被下游读取，立即清理
            cleanup_stage(project_root, "data_pipeline")

        if progress_callback:
            progress_callback("data_pipeline", 1.0, "数据预处理+划分完成")

        return SkillResult(
            success=True,
            message=(
                f"预处理完成: 训练(step2) {prep_result.get('train_rows', 0):,} 行, "
                f"完整约束层 {prep_result.get('constraint_rows', 0):,} 行, "
                f"预测 {prep_result.get('predict_valid_pixels', 0):,} 有效像素; "
                f"空间块划分: 训练 {split_result.get('train', {}).get('count', 0):,}, "
                f"验证 {split_result.get('validate', {}).get('count', 0):,}, "
                f"测试 {split_result.get('test', {}).get('count', 0):,}"
            ),
            data=result_data,
            artifacts=artifacts,
        )
