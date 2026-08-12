"""
EasyLST 核心模块

30m→10m 地表温度降尺度 (Land Surface Temperature Downscaling)

本模块将现有的独立脚本整合为统一的 API，支持回调函数以便 GUI 集成。
"""

from .data_preprocessing import process_preprocessing
from .split_dataset import split_dataset
from .ttri import compute_ttri_for_splits, compute_ttri_predict, fit_ttri_train
from .rf_model import train_random_forest, predict_test_set
from .tcr import compute_tcr
from .lst_final import compute_lst_final
from .export_geotiff import export_geotiff
from .evaluation import evaluate_coarse_constraint_closure
from .pipeline import EasyLSTPipeline
from .ai_assistant import GeoThermoAI_Assistant

__all__ = [
    "process_preprocessing",
    "split_dataset",
    "fit_ttri_train",
    "compute_ttri_for_splits",
    "compute_ttri_predict",
    "train_random_forest",
    "predict_test_set",
    "compute_tcr",
    "compute_lst_final",
    "export_geotiff",
    "evaluate_coarse_constraint_closure",
    "EasyLSTPipeline",
    "GeoThermoAI_Assistant",
]

__version__ = "1.0.0"
