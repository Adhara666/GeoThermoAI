"""
EasyLST 核心模块

30m→10m 地表温度降尺度 (Land Surface Temperature Downscaling)

本模块将现有的独立脚本整合为统一的 API，支持回调函数以便 GUI 集成。

科学算法子模块（含 GDAL/numpy/sklearn 等重组件依赖）改为惰性导入：
`from core import EasyLSTPipeline` 等用法保持不变（PEP 562 __getattr__），
但仅使用轻量子包（如 core.state_kernel，纯标准库）时不再级联加载整个
科学算法链。子模块导入行为与导出符号名不变，算法实现零改动。
"""

from importlib import import_module
from typing import Any

# 旧版 __init__ 直接重导出的符号 → 来源子模块（惰性加载）
_LAZY_EXPORTS = {
    "process_preprocessing": ".data_preprocessing",
    "split_dataset": ".split_dataset",
    "compute_ttri_for_splits": ".ttri",
    "compute_ttri_predict": ".ttri",
    "fit_ttri_train": ".ttri",
    "train_random_forest": ".rf_model",
    "predict_test_set": ".rf_model",
    "compute_tcr": ".tcr",
    "compute_lst_final": ".lst_final",
    "export_geotiff": ".export_geotiff",
    "evaluate_coarse_constraint_closure": ".evaluation",
    "EasyLSTPipeline": ".pipeline",
    "GeoThermoAI_Assistant": ".ai_assistant",
}

__all__ = list(_LAZY_EXPORTS)

__version__ = "1.0.0"


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'core' has no attribute '{name}'")
    value = getattr(import_module(target, __name__), name)
    globals()[name] = value  # 缓存，后续访问不再触发 import
    return value


def __dir__() -> list:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
