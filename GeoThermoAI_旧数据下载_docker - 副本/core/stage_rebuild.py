# -*- coding: utf-8 -*-
"""
下游步骤单步重跑的输入自愈模块

全流程中，中间产物会在各自阶段完成后被清理（见 intermediate_cleanup）。
当用户单独重跑某个下游 skill（如只重跑 accuracy_eval）时，其输入可能已被
清理删除。本模块提供 ensure_stage_inputs(project_dir, stage)，在输入缺失时
按依赖链从保留文件重建，使单步重跑可用。

重建只依赖全流程后仍保留的文件：
    - raw/ 原始影像（Landsat/Sentinel-2/QA/SCL/DEM，可选 ST_QA）
    - results/train/rf_ttri_model_run*.pkl（RF 模型）
    - processed/ttri_coefficients.json（TTRI 系数，缺则重新拟合）
    - 各 *_meta.json、split_info.json（KB 级）

依赖链（有向）：
    data_pipeline → ttri_compute → rf_model
                            ↘ tcr_compute → lst_export → accuracy_eval
                            ↘ accuracy_eval

fail-fast 语义保持：重建失败会抛出异常，由调用方（skill）捕获并返回失败，
不静默跳过，也不影响正常全流程执行（全流程时输入必然存在，不会触发重建）。
"""

import glob
import json
import os
from typing import Callable, List, Optional

from .atomic_io import atomic_replace
from .data_preprocessing import process_preprocessing
from .split_dataset import split_dataset
from .ttri import compute_ttri_for_splits, compute_ttri_predict, compute_ttri_for_constraint_grid
from .tcr import compute_tcr, MODE_BLOCK_CONSTANT
from .intermediate_cleanup import cleanup_stage

# 重建时使用的默认划分参数（split_info.json 存在时优先读其中的实际取值）
_DEFAULT_SPLIT_PARAMS = {
    "train_ratio": 0.6,
    "val_ratio": 0.2,
    "test_ratio": 0.2,
    "seed": 42,
    "block_size_px": 10,
    "guard_buffer_m": 100.0,
}
_BATCH_SIZE = 500000


def _log(cb: Optional[Callable], msg: str) -> None:
    if cb:
        try:
            cb("INFO", msg)
        except Exception:
            pass


def _progress(cb: Optional[Callable]):
    """把各 core 步骤的 (step, percent, message) 转发为日志"""
    if not cb:
        return None
    return lambda s, p, m: cb("INFO", f"[重建] {m}")


def _load_split_params(project_dir: str) -> dict:
    """优先从保留的 split_info.json 读取实际划分参数，避免重建结果与原来不一致"""
    try:
        path = os.path.join(project_dir, "processed", "split_info.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
            merged = dict(_DEFAULT_SPLIT_PARAMS)
            for k in merged:
                if info.get(k) is not None:
                    merged[k] = info[k]
            return merged
    except Exception:
        pass
    return dict(_DEFAULT_SPLIT_PARAMS)


def _ensure_raw(project_dir: str) -> None:
    raw = os.path.join(project_dir, "raw")
    need = ["landsat_lst.tif", "sentinel2_bands.tif", "landsat_qa_pixel.tif",
            "sentinel2_scl.tif", "dem.tif"]
    missing = [f for f in need if not os.path.isfile(os.path.join(raw, f))]
    if missing:
        raise RuntimeError(f"原始影像缺失，无法重建中间产物（缺: {missing}）")


def _rebuild_processed(project_dir: str, log_callback=None) -> None:
    """重建预处理 + 数据集划分产物（不含 TTRI 列）。"""
    _ensure_raw(project_dir)
    raw = os.path.join(project_dir, "raw")
    pdir = os.path.join(project_dir, "processed")
    os.makedirs(pdir, exist_ok=True)
    _log(log_callback, "重建预处理与数据集划分产物（原始影像仍保留）...")
    st_qa = os.path.join(raw, "landsat_st_qa.tif")
    process_preprocessing(
        landsat_path=os.path.join(raw, "landsat_lst.tif"),
        sentinel2_path=os.path.join(raw, "sentinel2_bands.tif"),
        qa_path=os.path.join(raw, "landsat_qa_pixel.tif"),
        scl_path=os.path.join(raw, "sentinel2_scl.tif"),
        dem_path=os.path.join(raw, "dem.tif"),
        output_dir=pdir,
        st_qa_path=st_qa if os.path.isfile(st_qa) else None,
        progress_callback=_progress(log_callback),
    )
    sp = _load_split_params(project_dir)
    split_dataset(
        input_csv=os.path.join(pdir, "30m_features_step2.csv"),
        output_dir=pdir,
        train_ratio=sp["train_ratio"], val_ratio=sp["val_ratio"], test_ratio=sp["test_ratio"],
        seed=sp["seed"], block_size_px=sp["block_size_px"], guard_buffer_m=sp["guard_buffer_m"],
        progress_callback=_progress(log_callback),
    )
    # 与 data_pipeline skill 行为一致：划分完成后立即清理对齐栅格
    cleanup_stage(project_dir, "data_pipeline")


def _rebuild_ttri_split(project_dir: str, log_callback=None) -> str:
    """在 train/val/test 上拟合 TTRI 系数并原地加 TTRI 列，同时给约束层加 TTRI。
    返回 coefficients 路径。"""
    pdir = os.path.join(project_dir, "processed")
    _log(log_callback, "重建 TTRI 系数与 train/validate/test TTRI 列...")
    result = compute_ttri_for_splits(
        train_csv=os.path.join(pdir, "train.csv"),
        val_csv=os.path.join(pdir, "validate.csv"),
        test_csv=os.path.join(pdir, "test.csv"),
        output_dir=pdir,
        progress_callback=_progress(log_callback),
    )
    compute_ttri_for_constraint_grid(
        os.path.join(pdir, "30m_constraint_grid.csv"), result["coefficients_path"]
    )
    return result["coefficients_path"]


def _rebuild_ttri_predict(project_dir: str, log_callback=None) -> None:
    """确保 TTRI 系数存在，并给约束层与 10m 预测特征加 TTRI（供 TCR 使用）。"""
    pdir = os.path.join(project_dir, "processed")
    coef = os.path.join(pdir, "ttri_coefficients.json")
    if not os.path.isfile(coef):
        coef = _rebuild_ttri_split(project_dir, log_callback)
    else:
        compute_ttri_for_constraint_grid(os.path.join(pdir, "30m_constraint_grid.csv"), coef)
    predict_csv = os.path.join(pdir, "10m_predict_features.csv")
    if not os.path.isfile(predict_csv):
        raise RuntimeError("缺少 10m 预测特征 CSV，无法重建 TTRI 空间化")
    tmp = predict_csv + ".ttri_tmp"
    compute_ttri_predict(
        constraint_csv=os.path.join(pdir, "30m_constraint_grid.csv"),
        constraint_meta_json=os.path.join(pdir, "30m_constraint_grid_meta.json"),
        predict_10m_csv=predict_csv,
        predict_10m_meta_json=os.path.join(pdir, "10m_predict_features_meta.json"),
        coefficients=coef,
        output_path=tmp,
        batch_size=_BATCH_SIZE,
        progress_callback=_progress(log_callback),
    )
    if os.path.isfile(tmp):
        atomic_replace(tmp, predict_csv)


def _latest_model(project_dir: str) -> str:
    """从 results/ 下找最新的 RF 模型 .pkl（模型为保留文件，全流程后必然存在）"""
    for pattern in (
        os.path.join(project_dir, "results", "train", "*_model_*.pkl"),
        os.path.join(project_dir, "results", "**", "*_model_*.pkl"),
    ):
        models = sorted(glob.glob(pattern, recursive=True),
                        key=lambda p: os.path.getmtime(p), reverse=True)
        if models:
            return models[0]
    return ""


def _tcr_mode_from_manifest(project_dir: str) -> str:
    """从 run_manifest.json 读取上次 tcr_compute 实际使用的模式"""
    try:
        path = os.path.join(project_dir, "run_manifest.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f).get("stages", {}).get("tcr_compute", {})
            mode = entry.get("stats", {}).get("mode")
            if mode:
                return mode
    except Exception:
        pass
    return MODE_BLOCK_CONSTANT


def _rebuild_tcr(project_dir: str, log_callback=None) -> None:
    """重建 tcr_result.csv（含上游：预处理/划分/TTRI 空间化）。"""
    pdir = os.path.join(project_dir, "processed")
    rdir = os.path.join(project_dir, "results")
    _ensure_processed(project_dir, [
        "30m_constraint_grid.csv", "30m_constraint_grid_meta.json",
        "10m_predict_features.csv", "10m_predict_features_meta.json",
    ], log_callback)
    _rebuild_ttri_predict(project_dir, log_callback)
    model_path = _latest_model(project_dir)
    if not model_path:
        raise RuntimeError("未找到 RF 模型（results/train/*_model_*.pkl），无法重建 TCR")
    os.makedirs(rdir, exist_ok=True)
    _log(log_callback, "重建 TCR 中间产物（tcr_result.csv）...")
    compute_tcr(
        constraint_csv=os.path.join(pdir, "30m_constraint_grid.csv"),
        constraint_meta_json=os.path.join(pdir, "30m_constraint_grid_meta.json"),
        predict_10m_csv=os.path.join(pdir, "10m_predict_features.csv"),
        meta_10m_json=os.path.join(pdir, "10m_predict_features_meta.json"),
        model_path=model_path,
        output_path=os.path.join(rdir, "tcr_result.csv"),
        mode=_tcr_mode_from_manifest(project_dir),
        batch_size=_BATCH_SIZE,
        progress_callback=_progress(log_callback),
    )


def _ensure_processed(project_dir: str, need: List[str], log_callback=None) -> bool:
    """确保 processed/ 下 need 列出的文件存在；缺失时重建预处理+划分产物。
    返回是否发生了重建。"""
    pdir = os.path.join(project_dir, "processed")
    missing = [f for f in need if not os.path.isfile(os.path.join(pdir, f))]
    if missing:
        _rebuild_processed(project_dir, log_callback)
        return True
    return False


def ensure_stage_inputs(project_dir: str, stage: str, log_callback=None) -> None:
    """确保 stage 的输入文件存在；缺失时自动重建。重建失败抛异常。"""
    if not stage or not project_dir or not os.path.isdir(project_dir):
        return
    pdir = os.path.join(project_dir, "processed")
    rdir = os.path.join(project_dir, "results")

    if stage == "data_pipeline":
        # 输入为 raw/ 影像（保留），缺失时由 skill 自身报错
        return

    if stage == "ttri_compute":
        _ensure_processed(project_dir, [
            "train.csv", "validate.csv", "test.csv",
            "30m_constraint_grid.csv", "30m_constraint_grid_meta.json",
            "10m_predict_features.csv", "10m_predict_features_meta.json",
            "30m_features_step2_meta.json",
        ], log_callback)
        return

    if stage == "rf_model":
        rebuilt = _ensure_processed(project_dir, [
            "train.csv", "validate.csv", "test.csv",
        ], log_callback)
        # 重建后的划分 CSV 不含 TTRI 列，必须重新拟合系数并原地加列
        if rebuilt or not os.path.isfile(os.path.join(pdir, "ttri_coefficients.json")):
            _rebuild_ttri_split(project_dir, log_callback)
        return

    if stage == "tcr_compute":
        _ensure_processed(project_dir, [
            "30m_constraint_grid.csv", "30m_constraint_grid_meta.json",
            "10m_predict_features.csv", "10m_predict_features_meta.json",
        ], log_callback)
        _rebuild_ttri_predict(project_dir, log_callback)
        if not _latest_model(project_dir):
            raise RuntimeError("未找到 RF 模型（results/train/*_model_*.pkl），无法重建 TCR")
        return

    if stage == "lst_export":
        if not os.path.isfile(os.path.join(rdir, "tcr_result.csv")):
            _rebuild_tcr(project_dir, log_callback)
        return

    if stage == "accuracy_eval":
        _ensure_processed(project_dir, [
            "30m_constraint_grid.csv", "30m_constraint_grid_meta.json",
            "10m_predict_features_meta.json",
        ], log_callback)
        if not os.path.isfile(os.path.join(rdir, "tcr_result.csv")):
            _rebuild_tcr(project_dir, log_callback)
        return
