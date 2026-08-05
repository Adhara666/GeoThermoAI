"""
随机森林回归模型模块

包含训练和预测两个函数：
    - train_random_forest: 训练RF模型并评估
    - predict_test_set:    使用训练好的模型对测试集推理

B-02 修复（用户确认第12条）：
    - 参数合并改为"先拷贝默认值，再用白名单校验过的用户参数覆盖"，不再是
      "只要前端传入非空 params 就整体不与默认值合并"，避免 random_state/
      max_features 静默丢失、悄悄回退到 scikit-learn 自身默认值；
    - 不再无条件 n_jobs=-1：按容器 CPU 配额（cgroup v1/v2，兼容宿主机）解析
      实际可用核数并写入生效参数；
    - 评估指标新增 MB（平均偏差），供 A-07 的 independent_prediction 使用。
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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

# 用户可覆盖的参数白名单（B-02：明确包含 random_state/max_features，
# 此前逐阶段包装的白名单只收五个字段、不收 random_state，是丢参数的根因之一）
RF_PARAM_WHITELIST = {
    "n_estimators", "max_depth", "min_samples_split",
    "min_samples_leaf", "max_features", "random_state",
}


def detect_cpu_quota() -> int:
    """检测容器实际可用 CPU 配额（B-02：不再无条件 n_jobs=-1）。

    优先级：cgroup v2 cpu.max → cgroup v1 cpu.cfs_quota_us/cpu.cfs_period_us
    → os.sched_getaffinity（尊重 taskset/affinity）→ os.cpu_count()。
    取以上可得信息的最小值，兜底至少为 1。
    """
    candidates: List[int] = []

    try:
        with open("/sys/fs/cgroup/cpu.max", "r", encoding="utf-8") as f:
            quota_str, period_str = f.read().split()
        if quota_str != "max":
            quota, period = int(quota_str), int(period_str)
            if period > 0:
                candidates.append(max(1, quota // period))
    except Exception:
        pass

    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r", encoding="utf-8") as f:
            quota = int(f.read().strip())
        if quota > 0:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r", encoding="utf-8") as f:
                period = int(f.read().strip())
            if period > 0:
                candidates.append(max(1, quota // period))
    except Exception:
        pass

    try:
        candidates.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass

    cpu_count = os.cpu_count()
    if cpu_count:
        candidates.append(cpu_count)

    if not candidates:
        return 1
    return max(1, min(candidates))


def resolve_n_jobs(requested: Optional[int]) -> int:
    """把用户/默认的 n_jobs 请求解析为不超过容器配额的实际值。"""
    quota = detect_cpu_quota()
    if requested is None or requested == -1 or requested <= 0:
        return quota
    return max(1, min(int(requested), quota))


def merge_rf_params(user_params: Optional[Dict]) -> Dict:
    """先拷贝 DEFAULT_RF_PARAMS，再用白名单校验过的用户参数覆盖（B-02）。

    无论用户传入多少字段，random_state/max_features 等未被用户覆盖的参数都保留
    默认值，不会退回 scikit-learn 自身默认（max_features=1.0, random_state=None）。
    """
    params = DEFAULT_RF_PARAMS.copy()
    if user_params:
        for k, v in user_params.items():
            if k in RF_PARAM_WHITELIST and v is not None:
                params[k] = v
    params["n_jobs"] = resolve_n_jobs(params.get("n_jobs", -1))
    params["verbose"] = 0
    return params


def _validate_columns(df: pd.DataFrame, required_cols: list, dataset_name: str) -> None:
    """校验DataFrame是否包含所有必需的列。"""
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{dataset_name} 缺少必需的列: {missing}. 现有列: {list(df.columns)}"
        )


def _compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """计算R²、RMSE、MAE、MB指标。

    MB（平均偏差，Mean Bias）= mean(y_pred - y_true)；正值表示预测整体偏暖，
    供 A-07 的 independent_prediction 协议使用（此前 rf_model 只算 R2/RMSE/MAE，
    缺 MB 需要另一模块重复计算/混用 TCR 闭合口径）。
    """
    return {
        "R2": round(float(r2_score(y_true, y_pred)), 6),
        "RMSE": round(float(np.sqrt(mean_squared_error(y_true, y_pred))), 6),
        "MAE": round(float(mean_absolute_error(y_true, y_pred)), 6),
        "MB": round(float(np.mean(y_pred - y_true)), 6),
    }


def load_model_and_features(model_path: str) -> Tuple[Any, List[str], Dict]:
    """加载模型 + 特征列表 + 完整 metrics dict，供 tcr.py / evaluation.py 复用，
    避免"加载模型找同名 metrics.json"这段逻辑在多个模块里重复实现。
    """
    model = joblib.load(model_path)
    metrics_path = model_path.replace("_model_", "_metrics_").replace(".pkl", ".json")
    meta: Dict = {}
    if os.path.exists(metrics_path):
        with open(metrics_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    feature_cols = meta.get("features", FEATURE_COLS)
    return model, feature_cols, meta


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
        4. 计算并保存评估指标（R², RMSE, MAE, MB）
        5. 保存模型为.pkl和指标为.json

    Args:
        train_csv:         训练集CSV路径（需包含TTRI列）
        val_csv:           验证集CSV路径（需包含TTRI列）
        output_dir:        输出目录
        params:            随机森林超参数字典（为None则使用默认值；非空时按
                           白名单与默认值合并，不再整体替换默认值）
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含训练和验证的指标、模型路径、特征重要性等
            - metrics: {train: {R2, RMSE, MAE, MB}, val: {...}}
            - model_path: 模型文件路径
            - metrics_path: 指标JSON路径
            - feature_importance: 特征重要性列表
            - features: 特征列列表
            - params: 实际生效的超参数（含 random_state/max_features/n_jobs）
    """
    params = merge_rf_params(params)

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
            f"max_depth={params['max_depth']}, n_jobs={params['n_jobs']})...",
        )

    t0 = time.time()
    model = RandomForestRegressor(**params)
    # scikit-learn 的 fit 是原子阻塞调用，期间无法产生真实进度。
    # 用后台心跳线程定期上报"训练中"状态，避免日志/气泡长时间静默被误认为中断
    #（不改变训练逻辑，只多输出状态日志）。
    _hb_stop = threading.Event()

    def _heartbeat():
        _hb_t0 = time.time()
        while not _hb_stop.wait(15):
            if progress_callback:
                progress_callback(
                    "rf_train", 0.3,
                    f"模型训练中... 已运行 {time.time() - _hb_t0:.0f}s"
                    f"（n_estimators={params['n_estimators']}，树间无进度上报）",
                )

    _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    _hb_thread.start()
    try:
        model.fit(X_train, y_train)
    finally:
        _hb_stop.set()
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
    # 自动编号（文件名仍固定写死这一命名模式；C-03 的"最新文件不可靠"问题
    # 通过 run_manifest.json 精确记录本轮 model_path/metrics_path 解决，
    # 不改变这里的命名策略本身）
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
        "cpu_quota_detected": detect_cpu_quota(),
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
    使用训练好的随机森林模型对测试集进行推理和精度评估（独立预测协议的数据来源，见 A-07）。

    Args:
        test_csv:          测试集CSV路径（需包含TTRI列）
        model_path:        训练好的模型.pkl文件路径
        output_dir:        输出目录
        progress_callback: 进度回调 callback(step_name, percent, message)

    Returns:
        dict: 包含测试集评估指标
            - metrics: {R2, RMSE, MAE, MB}
            - output_path: 结果JSON路径
            - features: 特征列列表
            - params: 模型超参数
    """
    if progress_callback:
        progress_callback("rf_predict_test", 0, "开始加载模型...")

    os.makedirs(output_dir, exist_ok=True)

    # ── 1. 加载模型和元数据 ────────────────────────────────────────────
    model, feature_cols, meta = load_model_and_features(model_path)
    params = meta.get("params", {})

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
            f"推理完成: R²={metrics['R2']:.4f}, RMSE={metrics['RMSE']:.4f}, "
            f"MAE={metrics['MAE']:.4f}, MB={metrics['MB']:.4f}",
        )

    # ── 4. 保存结果 ───────────────────────────────────────────────────
    match = re.search(r"_run(\d+)\.pkl$", model_path)
    run_id = match.group(1) if match else "001"
    output_path = os.path.join(output_dir, f"rf_ttri_predict_run{run_id}.json")

    # 空间范围（供 A-07 的 independent_prediction 报告"样本数和空间范围"）
    spatial_extent = None
    if "row" in df_test.columns and "col" in df_test.columns:
        spatial_extent = {
            "min_row": int(df_test["row"].min()), "max_row": int(df_test["row"].max()),
            "min_col": int(df_test["col"].min()), "max_col": int(df_test["col"].max()),
        }

    output = {
        "task": "predict",
        "model_path": model_path,
        "model_params": params,
        "test_path": test_csv,
        "features": feature_cols,
        "target": TARGET_COL,
        "metrics": metrics,
        "n_samples": int(len(df_test)),
        "spatial_extent_rowcol": spatial_extent,
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
        "n_samples": int(len(df_test)),
        "spatial_extent_rowcol": spatial_extent,
    }
