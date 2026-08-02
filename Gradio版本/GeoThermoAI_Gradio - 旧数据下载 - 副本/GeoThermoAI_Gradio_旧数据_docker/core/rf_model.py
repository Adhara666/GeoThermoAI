"""
随机森林回归模型模块

包含训练和预测两个函数：
    - train_random_forest: 训练RF模型并评估
    - predict_test_set:    使用训练好的模型对测试集推理
"""

import json
import os
import re
import time
from typing import Dict, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


# ── 默认特征列和目标列 ────────────────────────────────────────────────
FEATURE_COLS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI", "TTRI"]
TARGET_COL = "LST"

# ── 默认随机森林超参数 ────────────────────────────────────────────────
DEFAULT_RF_PARAMS = {
    "n_estimators": 200,
    "max_depth": 25,
    "min_samples_split": 16,
    "min_samples_leaf": 8,
    "max_features": 0.5,
    "random_state": 42,
    "n_jobs": -1,
    "verbose": 0,
}


def _validate_columns(df: pd.DataFrame, required_cols: list, dataset_name: str) -> None:
    """校验DataFrame是否包含所有必需的列。"""
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{dataset_name} 缺少必需的列: {missing}. 现有列: {list(df.columns)}"
        )


def _compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """计算R²、RMSE、MAE指标。"""
    return {
        "R2": round(float(r2_score(y_true, y_pred)), 6),
        "RMSE": round(float(np.sqrt(mean_squared_error(y_true, y_pred))), 6),
        "MAE": round(float(mean_absolute_error(y_true, y_pred)), 6),
    }


# ======================================================================
#  训练随机森林模型
# ======================================================================


def train_random_forest(
    train_csv: str,
    val_csv: str,
    output_dir: str,
    params: Optional[Dict] = None,
    progress_callback=None,
) -> Dict:
    """
    训练随机森林回归模型（含TTRI特征）。

    流程:
        1. 加载训练集和验证集数据
        2. 使用指定特征和目标训练随机森林
        3. 对训练集和验证集进行预测
        4. 计算并保存评估指标（R², RMSE, MAE）
        5. 保存模型为.pkl和指标为.json

    Args:
        train_csv:         训练集CSV路径（需包含TTRI列）
        val_csv:           验证集CSV路径（需包含TTRI列）
        output_dir:        输出目录
        params:            随机森林超参数字典（为None则使用默认值）
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含训练和验证的指标、模型路径、特征重要性等
            - metrics: {train: {R2, RMSE, MAE}, val: {R2, RMSE, MAE}}
            - model_path: 模型文件路径
            - metrics_path: 指标JSON路径
            - feature_importance: 特征重要性列表
            - features: 特征列列表
            - params: 使用的超参数
    """
    if params is None:
        params = DEFAULT_RF_PARAMS.copy()
    else:
        # 确保始终使用全核
        params["n_jobs"] = -1
        params["verbose"] = 0

    if progress_callback:
        progress_callback("rf_train", 0, "开始加载数据...")

    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 加载数据 ───────────────────────────────────────────────────
    df_train = pd.read_csv(train_csv)
    df_val = pd.read_csv(val_csv)

    for name, df in [("训练集", df_train), ("验证集", df_val)]:
        _validate_columns(df, FEATURE_COLS + [TARGET_COL], name)

    if progress_callback:
        progress_callback(
            "rf_train", 0.15,
            f"数据加载完成: 训练集 {len(df_train):,}, 验证集 {len(df_val):,}",
        )

    X_train = df_train[FEATURE_COLS].values
    y_train = df_train[TARGET_COL].values
    X_val = df_val[FEATURE_COLS].values
    y_val = df_val[TARGET_COL].values

    # ── 2. 训练模型 ───────────────────────────────────────────────────
    if progress_callback:
        progress_callback(
            "rf_train", 0.2,
            f"开始训练随机森林 (n_estimators={params['n_estimators']}, "
            f"max_depth={params['max_depth']})...",
        )

    t0 = time.time()
    model = RandomForestRegressor(**params)
    model.fit(X_train, y_train)
    train_time = time.time() - t0

    if progress_callback:
        progress_callback("rf_train", 0.6, f"模型训练完成，耗时 {train_time:.1f}s，开始评估...")

    # ── 3. 评估 ───────────────────────────────────────────────────────
    y_train_pred = model.predict(X_train)
    y_val_pred = model.predict(X_val)

    metrics = {}
    metrics["train"] = _compute_metrics(y_train, y_train_pred)
    metrics["val"] = _compute_metrics(y_val, y_val_pred)

    if progress_callback:
        progress_callback(
            "rf_train", 0.75,
            f"评估完成: 训练集 R²={metrics['train']['R2']:.4f}, "
            f"验证集 R²={metrics['val']['R2']:.4f}",
        )

    # ── 4. 保存模型和指标 ─────────────────────────────────────────────
    # 自动编号
    existing = [
        f for f in os.listdir(output_dir)
        if f.startswith("rf_ttri_metrics_") and f.endswith(".json")
    ]
    run_id = len(existing) + 1

    model_path = os.path.join(output_dir, f"rf_ttri_model_run{run_id:03d}.pkl")
    metrics_path = os.path.join(output_dir, f"rf_ttri_metrics_run{run_id:03d}.json")

    joblib.dump(model, model_path)

    output = {
        "model": "RandomForest",
        "params": params,
        "data": {
            "train_path": train_csv,
            "val_path": val_csv,
        },
        "features": FEATURE_COLS,
        "target": TARGET_COL,
        "metrics": metrics,
        "model_path": model_path,
        "train_time_seconds": round(train_time, 2),
    }

    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ── 5. 特征重要性 ─────────────────────────────────────────────────
    importance = model.feature_importances_
    sorted_idx = np.argsort(importance)[::-1]
    feature_importance = [
        {"feature": FEATURE_COLS[i], "importance": round(float(importance[i]), 6)}
        for i in sorted_idx
    ]

    if progress_callback:
        progress_callback("rf_train", 1.0, f"训练完成，模型已保存至: {model_path}")

    return {
        "metrics": metrics,
        "model_path": model_path,
        "metrics_path": metrics_path,
        "feature_importance": feature_importance,
        "features": FEATURE_COLS,
        "params": params,
        "train_time_seconds": round(train_time, 2),
    }


# ======================================================================
#  对测试集进行预测
# ======================================================================


def predict_test_set(
    test_csv: str,
    model_path: str,
    output_dir: str,
    progress_callback=None,
) -> Dict:
    """
    使用训练好的随机森林模型对测试集进行推理和精度评估。

    Args:
        test_csv:          测试集CSV路径（需包含TTRI列）
        model_path:        训练好的模型.pkl文件路径
        output_dir:        输出目录
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含测试集评估指标
            - metrics: {R2, RMSE, MAE}
            - output_path: 结果JSON路径
            - features: 特征列列表
            - params: 模型超参数
    """
    if progress_callback:
        progress_callback("rf_predict_test", 0, "开始加载模型...")

    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 加载模型和元数据 ────────────────────────────────────────────
    model = joblib.load(model_path)

    # 尝试找到对应的metrics.json
    metrics_path = model_path.replace("_model_", "_metrics_").replace(".pkl", ".json")
    if os.path.exists(metrics_path):
        with open(metrics_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        feature_cols = meta.get("features", FEATURE_COLS)
        params = meta.get("params", {})
    else:
        feature_cols = FEATURE_COLS
        params = {}

    if progress_callback:
        progress_callback(
            "rf_predict_test", 0.3,
            f"模型加载完成，特征 ({len(feature_cols)}): {feature_cols}",
        )

    # ── 2. 加载测试集 ─────────────────────────────────────────────────
    df_test = pd.read_csv(test_csv)
    _validate_columns(df_test, feature_cols + [TARGET_COL], "测试集")

    X_test = df_test[feature_cols].values
    y_test = df_test[TARGET_COL].values

    if progress_callback:
        progress_callback(
            "rf_predict_test", 0.5,
            f"数据加载完成: {len(df_test):,} 行",
        )

    # ── 3. 推理 ───────────────────────────────────────────────────────
    y_pred = model.predict(X_test)
    metrics = _compute_metrics(y_test, y_pred)

    if progress_callback:
        progress_callback(
            "rf_predict_test", 0.8,
            f"推理完成: R²={metrics['R2']:.4f}, RMSE={metrics['RMSE']:.4f}, MAE={metrics['MAE']:.4f}",
        )

    # ── 4. 保存结果 ───────────────────────────────────────────────────
    match = re.search(r"_run(\d+)\.pkl$", model_path)
    run_id = match.group(1) if match else "001"
    output_path = os.path.join(output_dir, f"rf_ttri_predict_run{run_id}.json")

    output = {
        "task": "predict",
        "model_path": model_path,
        "model_params": params,
        "test_path": test_csv,
        "features": feature_cols,
        "target": TARGET_COL,
        "metrics": metrics,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    if progress_callback:
        progress_callback("rf_predict_test", 1.0, f"结果已保存至: {output_path}")

    return {
        "metrics": metrics,
        "output_path": output_path,
        "features": feature_cols,
        "params": params,
    }
