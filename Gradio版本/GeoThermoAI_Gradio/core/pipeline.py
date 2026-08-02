"""
EasyLST 完整处理流水线

将所有核心模块串联为统一的自动化流水线:
    预处理 → 数据集划分 → TTRI(训练集) → RF训练 → 测试集预测
    → TTRI(预测集) → TCR → LST_final → GeoTIFF导出 → 空间一致性评估

支持:
    - 全流程自动执行
    - 单步执行
    - 随时停止
    - 进度和日志回调
"""

import json
import os
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .data_preprocessing import process_preprocessing
from .split_dataset import split_dataset
from .ttri import compute_ttri_train, compute_ttri_predict
from .rf_model import train_random_forest, predict_test_set
from .tcr import compute_tcr
from .lst_final import compute_lst_final
from .export_geotiff import export_geotiff
from .evaluation import evaluate_spatial_consistency

# ── 日志级别常量 ──────────────────────────────────────────────────────
LOG_INFO = "INFO"
LOG_WARNING = "WARNING"
LOG_ERROR = "ERROR"


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
            {
                "name": "preprocessing",
                "label": "数据预处理",
                "run": self._run_preprocessing,
            },
            {
                "name": "split_dataset",
                "label": "数据集划分",
                "run": self._run_split_dataset,
            },
            {
                "name": "ttri_train",
                "label": "TTRI训练集计算",
                "run": self._run_ttri_train,
            },
            {
                "name": "train_rf",
                "label": "RF模型训练",
                "run": self._run_train_rf,
            },
            {
                "name": "predict_test",
                "label": "测试集预测",
                "run": self._run_predict_test,
            },
            {
                "name": "ttri_predict",
                "label": "TTRI预测集计算",
                "run": self._run_ttri_predict,
            },
            {
                "name": "tcr",
                "label": "TCR计算",
                "run": self._run_tcr,
            },
            {
                "name": "lst_final",
                "label": "LST最终计算",
                "run": self._run_lst_final,
            },
            {
                "name": "export_geotiff",
                "label": "GeoTIFF导出",
                "run": self._run_export_geotiff,
            },
            {
                "name": "evaluation",
                "label": "空间一致性评估",
                "run": self._run_evaluation,
            },
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
            rf_params:       随机森林超参数（可选）
            batch_size:      批处理大小（默认500000）
            chunk_size:      LST_final批处理大小（默认500000）
        """
        self.config.update(kwargs)

    def get_default_paths(self) -> Dict[str, str]:
        """
        根据 output_dir 自动推导各步骤的默认输入/输出路径。

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
            # 预处理输出
            "train_csv": os.path.join(output_dir, "30m_features_step2.csv"),
            "train_meta": os.path.join(output_dir, "30m_features_step2_meta.json"),
            "predict_csv": os.path.join(output_dir, "10m_predict_features.csv"),
            "predict_meta": os.path.join(output_dir, "10m_predict_features_meta.json"),
            # 划分输出
            "train_split": os.path.join(train_dir, "train.csv"),
            "val_split": os.path.join(train_dir, "validate.csv"),
            "test_split": os.path.join(train_dir, "test.csv"),
            # TTRI训练集
            "train_with_ttri": os.path.join(train_dir, "train_with_TTRI.csv"),
            "val_with_ttri": os.path.join(train_dir, "validate_with_TTRI.csv"),
            "test_with_ttri": os.path.join(train_dir, "test_with_TTRI.csv"),
            # RF模型
            "train_results_dir": train_results,
            "test_results_dir": os.path.join(results_dir, "test"),
            # TTRI预测集
            "predict_with_ttri": os.path.join(predict_dir, "predict_10m_with_TTRI.csv"),
            # TCR
            "predict_with_ttri_tcr": os.path.join(predict_dir, "predict_10m_with_TTRI_TCR.csv"),
            # LST final
            "lst_final_csv": os.path.join(predict_results, "rf_10m_predict.csv"),
            # GeoTIFF
            "lst_final_tif": os.path.join(predict_results, "rf_10m_lst_final.tif"),
            # 评估
            "eval_dir": predict_results,
        }

    def run_full(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """
        执行完整流水线（所有步骤按顺序依次执行）。

        Args:
            progress_callback: 进度回调 callback(step_name, percent, message)
            log_callback:      日志回调 callback(level, message)

        Returns:
            dict: 各步骤结果汇总
        """
        self.should_stop = False
        self.results = {}
        total_steps = len(self.steps)

        log_callback(LOG_INFO, f"EasyLST 流水线启动，共 {total_steps} 个步骤")

        for idx, step in enumerate(self.steps):
            if self.should_stop:
                log_callback(LOG_WARNING, "流水线已被用户停止")
                break

            step_name = step["name"]
            step_label = step["label"]

            log_callback(LOG_INFO, f"[{idx + 1}/{total_steps}] 开始: {step_label}")

            # 代理progress_callback以添加步骤编号
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
                # 默认不停止，让调用方决定
                if self.should_stop:
                    break

        log_callback(LOG_INFO, f"EasyLST 流水线结束，完成 {len([r for r in self.results.values() if 'error' not in r])}/{total_steps} 个步骤")
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
        """
        self.should_stop = False

        for step in self.steps:
            if step["name"] == step_name:
                log_callback(LOG_INFO, f"开始执行: {step['label']}")
                result = step["run"](progress_callback, log_callback)
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

    def _run_preprocessing(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行数据预处理步骤。"""
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
        # 更新路径配置
        paths.update({
            "train_csv": result.get("train_csv", ""),
            "train_meta": result.get("train_meta", ""),
            "predict_csv": result.get("predict_csv", ""),
            "predict_meta": result.get("predict_meta", ""),
        })
        return result

    def _run_split_dataset(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行数据集划分步骤。"""
        paths = self.get_default_paths()
        train_dir = os.path.dirname(paths["train_split"])
        result = split_dataset(
            input_csv=paths["train_csv"],
            output_dir=train_dir,
            train_ratio=self.config.get("train_ratio", 0.6),
            val_ratio=self.config.get("val_ratio", 0.2),
            test_ratio=self.config.get("test_ratio", 0.2),
            seed=self.config.get("seed", 42),
            progress_callback=progress_callback,
        )
        return result

    def _run_ttri_train(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行TTRI训练集计算步骤。"""
        paths = self.get_default_paths()
        train_dir = os.path.dirname(paths["train_split"])
        result = compute_ttri_train(
            train_csv=paths["train_split"],
            val_csv=paths["val_split"],
            test_csv=paths["test_split"],
            output_dir=train_dir,
            progress_callback=progress_callback,
        )
        return result

    def _run_train_rf(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行RF模型训练步骤。"""
        paths = self.get_default_paths()
        result = train_random_forest(
            train_csv=paths["train_with_ttri"],
            val_csv=paths["val_with_ttri"],
            output_dir=paths["train_results_dir"],
            params=self.config.get("rf_params", None),
            progress_callback=progress_callback,
        )
        # 保存模型路径供后续步骤使用
        self.config["_model_path"] = result.get("model_path", "")
        return result

    def _run_predict_test(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行测试集预测步骤。"""
        paths = self.get_default_paths()
        model_path = self.config.get("_model_path", "")
        if not model_path:
            # 自动查找最新模型
            train_results_dir = paths["train_results_dir"]
            if os.path.isdir(train_results_dir):
                pkl_files = sorted([
                    f for f in os.listdir(train_results_dir)
                    if f.startswith("rf_ttri_model_") and f.endswith(".pkl")
                ])
                if pkl_files:
                    model_path = os.path.join(train_results_dir, pkl_files[-1])

        result = predict_test_set(
            test_csv=paths["test_with_ttri"],
            model_path=model_path,
            output_dir=paths["test_results_dir"],
            progress_callback=progress_callback,
        )
        return result

    def _run_ttri_predict(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行TTRI预测集计算步骤。"""
        paths = self.get_default_paths()
        result = compute_ttri_predict(
            data_30m_csv=paths["train_csv"],
            predict_10m_csv=paths["predict_csv"],
            output_path=paths["predict_with_ttri"],
            batch_size=self.config.get("batch_size", 500000),
            progress_callback=progress_callback,
        )
        return result

    def _run_tcr(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行TCR计算步骤。"""
        paths = self.get_default_paths()
        model_path = self.config.get("_model_path", "")
        if not model_path:
            # 自动查找最新模型
            train_results_dir = paths["train_results_dir"]
            if os.path.isdir(train_results_dir):
                pkl_files = sorted([
                    f for f in os.listdir(train_results_dir)
                    if f.startswith("rf_ttri_model_") and f.endswith(".pkl")
                ])
                if pkl_files:
                    model_path = os.path.join(train_results_dir, pkl_files[-1])

        result = compute_tcr(
            data_30m_csv=paths["train_csv"],
            meta_30m_json=paths["train_meta"],
            predict_10m_csv=paths["predict_with_ttri"],
            meta_10m_json=paths["predict_meta"],
            model_path=model_path,
            output_path=paths["predict_with_ttri_tcr"],
            batch_size=self.config.get("batch_size", 500000),
            progress_callback=progress_callback,
        )
        return result

    def _run_lst_final(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行LST最终计算步骤。"""
        paths = self.get_default_paths()
        result = compute_lst_final(
            input_csv=paths["predict_with_ttri_tcr"],
            output_path=paths["lst_final_csv"],
            chunk_size=self.config.get("chunk_size", 500000),
            progress_callback=progress_callback,
        )
        return result

    def _run_export_geotiff(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行GeoTIFF导出步骤。"""
        paths = self.get_default_paths()
        result = export_geotiff(
            lst_final_csv=paths["lst_final_csv"],
            meta_10m_json=paths["predict_meta"],
            output_path=paths["lst_final_tif"],
            progress_callback=progress_callback,
        )
        return result

    def _run_evaluation(
        self,
        progress_callback: Callable[[str, float, str], None],
        log_callback: Callable[[str, str], None],
    ) -> Dict:
        """执行空间一致性评估步骤。"""
        paths = self.get_default_paths()
        result = evaluate_spatial_consistency(
            test_csv=paths["test_split"],
            full_30m_csv=paths["train_csv"],
            predict_csv=paths["lst_final_csv"],
            output_dir=paths["eval_dir"],
            progress_callback=progress_callback,
        )
        return result
