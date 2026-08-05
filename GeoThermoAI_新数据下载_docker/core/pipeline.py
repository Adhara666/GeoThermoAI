"""
EasyLST 完整处理流水线（A-08 重写）

修复内容：
    - 固定路径断链：TTRI 原地覆盖 train.csv/validate.csv/test.csv，本模块不再
      声明或读取不存在的 ``*_with_TTRI.csv``；
    - evaluation 固定传入完整30m约束层 + 10m meta（A-05/A-07 两套协议分别调用）；
    - fail-fast：任一步骤失败后不再"默认继续"，立即停止且不启动下游，也不再
      用字符串排序去"捡"旧模型兜底；
    - 新增固定 run_manifest.json：记录每个 stage 的输入签名、完成状态、
      产物路径、行数等，供断点续跑/前端状态查询使用（不允许模型自由命名）。

将所有核心模块串联为统一的自动化流水线:
    预处理(含完整30m约束层) → 数据集划分(空间块+guard buffer) → TTRI(仅train拟合)
    → RF训练 → 测试集预测 → 独立预测评估 → TTRI(预测集) → TCR → LST_final
    → GeoTIFF导出 → 粗尺度闭合评估

支持:
    - 全流程自动执行（fail-fast）
    - 单步执行
    - 随时停止
    - 进度和日志回调
"""

import os
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import manifest as run_manifest
from .data_preprocessing import process_preprocessing
from .split_dataset import split_dataset
from .ttri import compute_ttri_for_splits, compute_ttri_predict, compute_ttri_for_constraint_grid
from .rf_model import train_random_forest, predict_test_set
from .tcr import compute_tcr, MODE_BLOCK_CONSTANT
from .lst_final import compute_lst_final
from .export_geotiff import export_geotiff
from .evaluation import evaluate_independent_prediction, evaluate_coarse_constraint_closure

# ── 日志级别常量 ──────────────────────────────────────────────────────
LOG_INFO = "INFO"
LOG_WARNING = "WARNING"
LOG_ERROR = "ERROR"


class PipelineStageError(RuntimeError):
    """标记某个固定 stage 执行失败；run_full 捕获后立即停止，不启动下游。"""

    def __init__(self, stage: str, original: Exception):
        self.stage = stage
        self.original = original
        super().__init__(f"[{stage}] {original}")


class EasyLSTPipeline:
    """
    EasyLST 完整处理流水线。

    使用示例:
        pipeline = EasyLSTPipeline()
        pipeline.configure(
            landsat_path="...",
            sentinel2_path="...",
            qa_path="...",
            scl_path="...",
            dem_path="...",
            output_dir="...",
            ...
        )
        pipeline.run_full(progress_callback, log_callback)
    """

    def __init__(self):
        """初始化流水线。"""
        self.config: Dict[str, Any] = {}
        self.should_stop = False
        self.current_step_name = ""
        self.results: Dict[str, Any] = {}

        # ── 定义流水线步骤清单 ────────────────────────────────────────
        self.steps: List[Dict[str, Any]] = [
            {"name": "preprocessing", "label": "数据预处理（含完整30m约束层）", "run": self._run_preprocessing},
            {"name": "split_dataset", "label": "数据集划分（空间块+guard buffer）", "run": self._run_split_dataset},
            {"name": "ttri_train", "label": "TTRI训练集计算（仅train拟合一次）", "run": self._run_ttri_train},
            {"name": "train_rf", "label": "RF模型训练", "run": self._run_train_rf},
            {"name": "predict_test", "label": "测试集预测", "run": self._run_predict_test},
            {"name": "evaluate_independent", "label": "独立预测评估", "run": self._run_evaluate_independent},
            {"name": "ttri_predict", "label": "TTRI预测集计算", "run": self._run_ttri_predict},
            {"name": "tcr", "label": "TCR计算", "run": self._run_tcr},
            {"name": "lst_final", "label": "LST最终计算", "run": self._run_lst_final},
            {"name": "export_geotiff", "label": "GeoTIFF导出", "run": self._run_export_geotiff},
            {"name": "evaluate_closure", "label": "粗尺度闭合评估", "run": self._run_evaluate_closure},
        ]

    # ==================================================================
    #  公开API
    # ==================================================================

    def configure(self, **kwargs) -> None:
        """
        配置流水线参数。

        常用参数:
            landsat_path:    Landsat L2 ST_B10栅格路径
            sentinel2_path:  Sentinel-2 L2A多光谱栅格路径
            qa_path:         Landsat QA_PIXEL栅格路径
            scl_path:        Sentinel-2 SCL栅格路径
            dem_path:        DEM栅格路径
            output_dir:      总输出目录
            train_ratio:     训练集比例（默认0.6）
            val_ratio:       验证集比例（默认0.2）
            test_ratio:      测试集比例（默认0.2）
            seed:            随机种子（默认42）
            block_size_px:   空间块划分的块边长像元数
            guard_buffer_m:  train/test 缓冲带宽度（米）
            rf_params:       随机森林超参数（可选）
            tcr_mode:        TCR模式，"block_constant"（默认）或 "smooth_recentered"
            batch_size:      批处理大小（默认500000）
            chunk_size:      LST_final批处理大小（默认500000）
        """
        self.config.update(kwargs)

    def get_default_paths(self) -> Dict[str, str]:
        """
        根据 output_dir 自动推导各步骤的默认输入/输出路径（固定命名，不变）。

        Returns:
            dict: 路径字典
        """
        output_dir = self.config.get("output_dir", ".")
        train_dir = os.path.join(output_dir, "for_train")
        predict_dir = os.path.join(output_dir, "for_predict_10m")
        results_dir = os.path.join(output_dir, "results")
        train_results = os.path.join(results_dir, "train")
        predict_results = os.path.join(results_dir, "predict_10m")

        return {
            "output_dir": output_dir,
            # 预处理输出
            "train_csv": os.path.join(output_dir, "30m_features_step2.csv"),
            "train_meta": os.path.join(output_dir, "30m_features_step2_meta.json"),
            "constraint_csv": os.path.join(output_dir, "30m_constraint_grid.csv"),
            "constraint_meta": os.path.join(output_dir, "30m_constraint_grid_meta.json"),
            "predict_csv": os.path.join(output_dir, "10m_predict_features.csv"),
            "predict_meta": os.path.join(output_dir, "10m_predict_features_meta.json"),
            # 划分输出（固定文件名，A-04 修复后直接原地含 TTRI 列，不再有 *_with_TTRI.csv）
            "train_split": os.path.join(train_dir, "train.csv"),
            "val_split": os.path.join(train_dir, "validate.csv"),
            "test_split": os.path.join(train_dir, "test.csv"),
            "split_info": os.path.join(train_dir, "split_info.json"),
            "ttri_coefficients": os.path.join(train_dir, "ttri_coefficients.json"),
            # RF模型
            "train_results_dir": train_results,
            "test_results_dir": os.path.join(results_dir, "test"),
            # TTRI预测集（原地新增TTRI列）
            "predict_with_ttri": os.path.join(predict_dir, "10m_predict_features.csv"),
            # TCR 中间产物（固定名；lst_export 完成后由 manifest 标记为"可清理的重复产物"）
            "tcr_output": os.path.join(predict_dir, "tcr_predict.csv"),
            # LST final
            "lst_final_csv": os.path.join(predict_results, "rf_10m_predict.csv"),
            # GeoTIFF
            "lst_final_tif": os.path.join(predict_results, "rf_10m_lst_final.tif"),
            # 评估（两套协议，各自固定文件名，见 evaluation.py）
            "eval_dir": predict_results,
        }

    def run_full(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """
        执行完整流水线（fail-fast：任一步骤失败立即停止，不启动下游，不复用旧产物）。

        Args:
            progress_callback: 进度回调 callback(step_name, percent, message)
            log_callback:      日志回调 callback(level, message)

        Returns:
            dict: 各步骤结果汇总；若某步骤失败，self.results[step_name] 含 error/traceback，
                  且该步骤之后的所有步骤不会出现在 self.results 中（未执行）
        """
        self.should_stop = False
        self.results = {}
        total_steps = len(self.steps)
        output_dir = self.config.get("output_dir", ".")

        log_callback(LOG_INFO, f"EasyLST 流水线启动，共 {total_steps} 个步骤（fail-fast 模式）")

        for idx, step in enumerate(self.steps):
            if self.should_stop:
                log_callback(LOG_WARNING, "流水线已被用户停止")
                break

            step_name = step["name"]
            step_label = step["label"]

            log_callback(LOG_INFO, f"[{idx + 1}/{total_steps}] 开始: {step_label}")
            if output_dir and output_dir != ".":
                run_manifest.record_stage(output_dir, step_name, run_manifest.STATUS_RUNNING)

            def step_progress(step_name_inner, percent, message):
                actual_percent = (idx + percent) / total_steps
                progress_callback(step_name_inner, actual_percent, f"[{step_label}] {message}")

            try:
                result = step["run"](step_progress, log_callback)
                self.results[step_name] = result
                log_callback(LOG_INFO, f"[{idx + 1}/{total_steps}] 完成: {step_label}")
            except Exception as exc:
                tb = traceback.format_exc()
                log_callback(LOG_ERROR, f"[{idx + 1}/{total_steps}] 失败: {step_label}\n{tb}")
                self.results[step_name] = {"error": str(exc), "traceback": tb}
                if output_dir and output_dir != ".":
                    run_manifest.record_stage(
                        output_dir, step_name, run_manifest.STATUS_FAILED, error=str(exc)
                    )
                    run_manifest.mark_downstream_skipped(
                        output_dir, step_name, stage_order=[s["name"] for s in self.steps]
                    )
                log_callback(
                    LOG_ERROR,
                    f"流水线在 [{step_label}] 失败后已停止（fail-fast），"
                    f"后续 {total_steps - idx - 1} 个步骤未执行，不会复用旧模型/旧产物兜底",
                )
                break

        completed = len([r for r in self.results.values() if "error" not in r])
        log_callback(LOG_INFO, f"EasyLST 流水线结束，完成 {completed}/{total_steps} 个步骤")
        return self.results

    def run_step(
        self,
        step_name: str,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Any:
        """
        执行单个步骤。

        Args:
            step_name:        步骤名称
            progress_callback: 进度回调
            log_callback:      日志回调

        Returns:
            该步骤的返回值

        Raises:
            ValueError: 步骤名称无效
            Exception:  步骤本身失败时原样抛出（fail-fast；调用方决定如何处理）
        """
        self.should_stop = False
        output_dir = self.config.get("output_dir", ".")

        for step in self.steps:
            if step["name"] == step_name:
                log_callback(LOG_INFO, f"开始执行: {step['label']}")
                if output_dir and output_dir != ".":
                    run_manifest.record_stage(output_dir, step_name, run_manifest.STATUS_RUNNING)
                try:
                    result = step["run"](progress_callback, log_callback)
                except Exception as exc:
                    if output_dir and output_dir != ".":
                        run_manifest.record_stage(
                            output_dir, step_name, run_manifest.STATUS_FAILED, error=str(exc)
                        )
                    raise
                self.results[step_name] = result
                log_callback(LOG_INFO, f"完成: {step['label']}")
                return result

        raise ValueError(f"无效的步骤名称: {step_name}，可用步骤: {[s['name'] for s in self.steps]}")

    def stop(self) -> None:
        """请求停止流水线。"""
        self.should_stop = True

    def get_step_names(self) -> List[str]:
        """获取所有可用步骤名称。"""
        return [s["name"] for s in self.steps]

    # ==================================================================
    #  各步骤实现
    # ==================================================================

    def _run_preprocessing(self, progress_callback, log_callback) -> Dict:
        """执行数据预处理步骤（含完整30m约束层，A-05）。"""
        paths = self.get_default_paths()
        result = process_preprocessing(
            landsat_path=self.config.get("landsat_path", ""),
            sentinel2_path=self.config.get("sentinel2_path", ""),
            qa_path=self.config.get("qa_path", ""),
            scl_path=self.config.get("scl_path", ""),
            dem_path=self.config.get("dem_path", ""),
            output_dir=self.config.get("output_dir", "."),
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "preprocessing", run_manifest.STATUS_COMPLETED,
                artifacts={
                    "train_csv": result.get("train_csv", ""),
                    "train_meta": result.get("train_meta", ""),
                    "constraint_csv": result.get("constraint_csv", ""),
                    "constraint_meta": result.get("constraint_meta", ""),
                    "predict_csv": result.get("predict_csv", ""),
                    "predict_meta": result.get("predict_meta", ""),
                },
                stats={
                    "train_rows": result.get("train_rows", 0),
                    "constraint_rows": result.get("constraint_rows", 0),
                    "predict_valid_pixels": result.get("predict_valid_pixels", 0),
                },
            )
        return result

    def _run_split_dataset(self, progress_callback, log_callback) -> Dict:
        """执行数据集划分步骤（空间块 + guard buffer，B-01）。"""
        paths = self.get_default_paths()
        train_dir = os.path.dirname(paths["train_split"])
        result = split_dataset(
            input_csv=paths["train_csv"],
            output_dir=train_dir,
            train_ratio=self.config.get("train_ratio", 0.6),
            val_ratio=self.config.get("val_ratio", 0.2),
            test_ratio=self.config.get("test_ratio", 0.2),
            seed=self.config.get("seed", 42),
            block_size_px=self.config.get("block_size_px", 10),
            guard_buffer_m=self.config.get("guard_buffer_m", 100.0),
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "split_dataset", run_manifest.STATUS_COMPLETED,
                artifacts={"train": paths["train_split"], "validate": paths["val_split"], "test": paths["test_split"]},
                stats=result.get("split_info", {}),
            )
        return result

    def _run_ttri_train(self, progress_callback, log_callback) -> Dict:
        """执行TTRI计算步骤：仅 train 拟合一次，train/validate/test 复用同一组系数（A-04）。"""
        paths = self.get_default_paths()
        train_dir = os.path.dirname(paths["train_split"])
        result = compute_ttri_for_splits(
            train_csv=paths["train_split"],
            val_csv=paths["val_split"],
            test_csv=paths["test_split"],
            output_dir=train_dir,
            progress_callback=progress_callback,
        )
        # 用同一组系数在完整30m约束层上计算 TTRI（供 TCR / 闭合评价的空间化插值使用）
        compute_ttri_for_constraint_grid(paths["constraint_csv"], result["coefficients_path"])
        self.config["_ttri_coefficients_path"] = result["coefficients_path"]

        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "ttri_train", run_manifest.STATUS_COMPLETED,
                artifacts={"coefficients": result["coefficients_path"], **result["output_files"]},
                stats=result["coefficients"],
            )
        return result

    def _run_train_rf(self, progress_callback, log_callback) -> Dict:
        """执行RF模型训练步骤。"""
        paths = self.get_default_paths()
        result = train_random_forest(
            train_csv=paths["train_split"],
            val_csv=paths["val_split"],
            output_dir=paths["train_results_dir"],
            params=self.config.get("rf_params", None),
            progress_callback=progress_callback,
        )
        self.config["_model_path"] = result.get("model_path", "")
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "train_rf", run_manifest.STATUS_COMPLETED,
                artifacts={"model_path": result.get("model_path", ""), "metrics_path": result.get("metrics_path", "")},
                stats={"metrics": result.get("metrics", {}), "params": result.get("params", {})},
            )
        return result

    def _run_predict_test(self, progress_callback, log_callback) -> Dict:
        """执行测试集预测步骤（fail-fast：不再自动查找"最新"旧模型兜底）。"""
        paths = self.get_default_paths()
        model_path = self.config.get("_model_path", "")
        if not model_path:
            raise PipelineStageError(
                "predict_test",
                RuntimeError(
                    "上游 RF 训练未成功产出模型路径，拒绝自动搜索目录中的旧模型兜底继续执行"
                ),
            )

        result = predict_test_set(
            test_csv=paths["test_split"],
            model_path=model_path,
            output_dir=paths["test_results_dir"],
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "predict_test", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"metrics": result.get("metrics", {})},
            )
        return result

    def _run_evaluate_independent(self, progress_callback, log_callback) -> Dict:
        """独立预测评估（A-07 协议一）。"""
        paths = self.get_default_paths()
        model_path = self.config.get("_model_path", "")
        if not model_path:
            raise PipelineStageError("evaluate_independent", RuntimeError("上游 RF 模型不存在，无法评估"))

        split_info = None
        try:
            import json

            with open(paths["split_info"], "r", encoding="utf-8") as f:
                split_info = json.load(f)
        except Exception:
            pass

        result = evaluate_independent_prediction(
            test_csv=paths["test_split"],
            model_path=model_path,
            output_dir=paths["eval_dir"],
            split_info=split_info,
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "evaluate_independent", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"metrics": result.get("metrics", {})},
            )
        return result

    def _run_ttri_predict(self, progress_callback, log_callback) -> Dict:
        """执行TTRI预测集计算步骤（基于完整30m约束层的统一仿射映射插值，A-05/A-06）。"""
        paths = self.get_default_paths()
        coefficients_path = self.config.get("_ttri_coefficients_path") or paths["ttri_coefficients"]
        result = compute_ttri_predict(
            constraint_csv=paths["constraint_csv"],
            constraint_meta_json=paths["constraint_meta"],
            predict_10m_csv=paths["predict_csv"],
            predict_10m_meta_json=paths["predict_meta"],
            coefficients=coefficients_path,
            output_path=paths["predict_with_ttri"],
            batch_size=self.config.get("batch_size", 500000),
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "ttri_predict", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": paths["predict_with_ttri"]},
                stats={"total_valid": result.get("total_valid", 0), "out_of_grid": result.get("out_of_grid", 0)},
            )
        return result

    def _run_tcr(self, progress_callback, log_callback) -> Dict:
        """执行TCR计算步骤（A-06：默认 block_constant，统一仿射映射）。"""
        paths = self.get_default_paths()
        model_path = self.config.get("_model_path", "")
        if not model_path:
            raise PipelineStageError("tcr", RuntimeError("上游 RF 模型不存在，拒绝自动搜索旧模型兜底"))

        result = compute_tcr(
            constraint_csv=paths["constraint_csv"],
            constraint_meta_json=paths["constraint_meta"],
            predict_10m_csv=paths["predict_with_ttri"],
            meta_10m_json=paths["predict_meta"],
            model_path=model_path,
            output_path=paths["tcr_output"],
            mode=self.config.get("tcr_mode", MODE_BLOCK_CONSTANT),
            batch_size=self.config.get("batch_size", 500000),
            progress_callback=progress_callback,
        )
        self.config["_tcr_mode_used"] = result.get("mode")
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "tcr", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"tcr_statistics": result.get("tcr_statistics", {}), "validity": result.get("validity", {})},
            )
        return result

    def _run_lst_final(self, progress_callback, log_callback) -> Dict:
        """执行LST最终计算步骤。"""
        paths = self.get_default_paths()
        result = compute_lst_final(
            input_csv=paths["tcr_output"],
            output_path=paths["lst_final_csv"],
            chunk_size=self.config.get("chunk_size", 500000),
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "lst_final", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"total_rows": result.get("total_rows", 0), "total_valid": result.get("total_valid", 0)},
            )
        return result

    def _run_export_geotiff(self, progress_callback, log_callback) -> Dict:
        """执行GeoTIFF导出步骤（B-07：严格按 row,col 写入）。"""
        paths = self.get_default_paths()
        result = export_geotiff(
            lst_final_csv=paths["lst_final_csv"],
            meta_10m_json=paths["predict_meta"],
            output_path=paths["lst_final_tif"],
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "export_geotiff", run_manifest.STATUS_COMPLETED,
                artifacts={"tif_path": result.get("output_path", ""), "csv_path": paths["lst_final_csv"]},
                stats={"stats": result.get("stats", {}), "image_size": result.get("image_size", {})},
            )
        return result

    def _run_evaluate_closure(self, progress_callback, log_callback) -> Dict:
        """粗尺度闭合评估（A-07 协议二）。"""
        paths = self.get_default_paths()
        result = evaluate_coarse_constraint_closure(
            constraint_csv=paths["constraint_csv"],
            constraint_meta_json=paths["constraint_meta"],
            lst_final_csv=paths["lst_final_csv"],
            meta_10m_json=paths["predict_meta"],
            output_dir=paths["eval_dir"],
            tcr_mode=self.config.get("_tcr_mode_used"),
            progress_callback=progress_callback,
        )
        output_dir = self.config.get("output_dir", ".")
        if output_dir and output_dir != ".":
            run_manifest.record_stage(
                output_dir, "evaluate_closure", run_manifest.STATUS_COMPLETED,
                artifacts={"output_path": result.get("output_path", "")},
                stats={"closure": result.get("closure", {}), "value_range": result.get("value_range", {})},
            )
        return result
