"""
Full end-to-end integration test using SYNTHETIC rasters, but exercising the
EXACT skill-call chain and SKILL_PATHS-style path derivation that
core/agent/geo_thermo_agent.py uses in the live server-driven flow.

This validates (without touching agent code, without needing new network
downloads since data_acquisition.py was already validated separately with
REAL Planetary Computer data):
    - data_pipeline (preprocessing + spatial-block split)
    - ttri_compute  (single-fit TTRI + unified affine interpolation)
    - rf_model      (param-merged RF + independent_prediction.json)
    - tcr_compute   (block_constant TCR via unified affine mapping)
    - lst_export    (row/col-correct GeoTIFF export)
    - accuracy_eval (coarse_constraint_closure.json, no 5K fields)
    - run_manifest.json accumulation across all 6 stages
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import Affine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_DIR = tempfile.mkdtemp(prefix='full_pipeline_')
RAW_DIR = os.path.join(PROJECT_DIR, "raw")
PROCESSED_DIR = os.path.join(PROJECT_DIR, "processed")
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")
for d in (RAW_DIR, PROCESSED_DIR, RESULTS_DIR):
    os.makedirs(d, exist_ok=True)

print("PROJECT_DIR =", PROJECT_DIR)

CRS = "EPSG:32650"
H30, W30 = 40, 40  # 40x40 @ 30m = 1200m x 1200m
H10, W10 = H30 * 3, W30 * 3  # exact 3x for a clean deterministic check
ORIGIN_X, ORIGIN_Y = 500000.0, 3400000.0

transform_30m = Affine(30.0, 0.0, ORIGIN_X, 0.0, -30.0, ORIGIN_Y)
transform_10m = Affine(10.0, 0.0, ORIGIN_X, 0.0, -10.0, ORIGIN_Y)


def write_raster(path, array, transform, dtype, nodata=None, count=1):
    if array.ndim == 2:
        array = array[None, ...]
    with rasterio.open(
        path, "w", driver="GTiff", height=array.shape[1], width=array.shape[2],
        count=count, dtype=dtype, crs=CRS, transform=transform, nodata=nodata,
    ) as dst:
        for i in range(count):
            dst.write(array[i], i + 1)


rng = np.random.default_rng(7)

# ---- synthetic DEM: undulating terrain (30m grid) -- NOT a plane, so slope/aspect
# actually vary spatially (a perfectly planar DEM gives constant slope/aspect
# everywhere, which makes the TTRI regression matrix rank-deficient by construction;
# real terrain always has some curvature, so this is more representative anyway) ----
row_idx, col_idx = np.meshgrid(np.arange(H30), np.arange(W30), indexing="ij")
dem = (
    50.0
    + 2.0 * row_idx + 1.0 * col_idx
    + 15.0 * np.sin(row_idx / 6.0) * np.cos(col_idx / 5.0)
    + 8.0 * np.sin(col_idx / 3.7)
).astype(np.float32)
write_raster(os.path.join(RAW_DIR, "dem.tif"), dem, transform_30m, "float32")

# ---- synthetic Landsat LST (DN space): temperature depends on DEM (terrain effect) + noise ----
true_temp_k = 305.0 - 0.01 * dem + rng.normal(0, 0.3, size=dem.shape).astype(np.float32)
lst_dn = ((true_temp_k - 149.0) / 0.00341802).astype(np.float32)
write_raster(os.path.join(RAW_DIR, "landsat_lst.tif"), lst_dn, transform_30m, "float32", nodata=0.0)

# ---- synthetic QA_PIXEL: all clear (bits 0-4 = 0) ----
qa_pixel = np.zeros((H30, W30), dtype=np.uint16)
write_raster(os.path.join(RAW_DIR, "landsat_qa_pixel.tif"), qa_pixel, transform_30m, "uint16")

# ---- synthetic Sentinel-2 bands (10m grid): 5 bands, B02..B11 (already "corrected" reflectance*10000) ----
s2_bands = np.stack([
    rng.uniform(800, 1800, size=(H10, W10)).astype(np.float32) for _ in range(5)
])
write_raster(os.path.join(RAW_DIR, "sentinel2_bands.tif"), s2_bands, transform_10m, "float32",
             nodata=-9999.0, count=5)

# ---- synthetic SCL (10m grid): all class 4 (vegetation, valid) ----
scl = np.full((H10, W10), 4, dtype=np.uint8)
write_raster(os.path.join(RAW_DIR, "sentinel2_scl.tif"), scl, transform_10m, "uint8")

print("Synthetic raw rasters written.")

# ======================================================================
#  Stage 1: data_pipeline  (mimics Agent SKILL_PATHS injection)
# ======================================================================
from core.skills.builtin.data_pipeline import DataPipelineSkill

logs = []


def pcb(name, pct, msg):
    pass  # keep output short


def lcb(level, msg):
    logs.append((level, msg))
    if level in ("ERROR",):
        print(f'  [{level}] {msg}')


skill = DataPipelineSkill()
result = skill.execute({
    "landsat_path": os.path.join(RAW_DIR, "landsat_lst.tif"),
    "qa_path": os.path.join(RAW_DIR, "landsat_qa_pixel.tif"),
    "sentinel2_path": os.path.join(RAW_DIR, "sentinel2_bands.tif"),
    "scl_path": os.path.join(RAW_DIR, "sentinel2_scl.tif"),
    "dem_path": os.path.join(RAW_DIR, "dem.tif"),
    "output_dir": PROCESSED_DIR,
    "train_ratio": 0.6, "val_ratio": 0.2, "test_ratio": 0.2, "seed": 42,
    "block_size_px": 8, "guard_buffer_m": 30.0,
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 1 data_pipeline success=", result.success, "|", result.message)
assert result.success, result.message
assert os.path.isfile(result.data["constraint_csv"])
assert os.path.isfile(os.path.join(PROCESSED_DIR, "train.csv"))
assert os.path.isfile(os.path.join(PROCESSED_DIR, "split_info.json"))

import pandas as pd
train_df = pd.read_csv(os.path.join(PROCESSED_DIR, "train.csv"))
print(f"  train rows={len(train_df)}, constraint rows={result.data['constraint_rows']}")

# ======================================================================
#  Stage 2: ttri_compute  (mimics Agent's fixed data_30m_csv/predict_10m_csv injection;
#  constraint_csv/meta must be auto-derived from output_dir, NOT explicitly passed)
# ======================================================================
from core.skills.builtin.ttri_compute import TTRIComputeSkill

skill = TTRIComputeSkill()
result = skill.execute({
    "train_csv": os.path.join(PROCESSED_DIR, "train.csv"),
    "val_csv": os.path.join(PROCESSED_DIR, "validate.csv"),
    "test_csv": os.path.join(PROCESSED_DIR, "test.csv"),
    "output_dir": PROCESSED_DIR,  # Agent injects this; constraint_csv/meta must be auto-derived
    "data_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),  # legacy param Agent injects, unused now
    "predict_10m_csv": os.path.join(PROCESSED_DIR, "10m_predict_features.csv"),
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 2 ttri_compute success=", result.success, "|", result.message)
assert result.success, result.message
assert os.path.isfile(result.data["coefficients_path"])
assert os.path.basename(result.data["coefficients_path"]) == "ttri_coefficients.json"

with open(result.data["coefficients_path"], encoding="utf-8") as f:
    coef_json = json.load(f)
print("  TTRI coefficients:", coef_json["coefficients"], "r2=", coef_json["r2"])
# ground truth was temp = 305 - 0.01*DEM -> a(DEM) should be close to -0.01
assert abs(coef_json["coefficients"][0] - (-0.01)) < 0.01, f"unexpected TTRI DEM coefficient: {coef_json['coefficients']}"

predict_df = pd.read_csv(os.path.join(PROCESSED_DIR, "10m_predict_features.csv"))
assert "TTRI" in predict_df.columns
print(f"  10m predict rows={len(predict_df)}, TTRI valid={predict_df['TTRI'].notna().sum()}")
assert predict_df["TTRI"].notna().sum() > 0

# ======================================================================
#  Stage 3: rf_model  (produces model + independent_prediction.json)
# ======================================================================
from core.skills.builtin.rf_model import RFModelSkill

skill = RFModelSkill()
result = skill.execute({
    "train_csv": os.path.join(PROCESSED_DIR, "train.csv"),
    "val_csv": os.path.join(PROCESSED_DIR, "validate.csv"),
    "test_csv": os.path.join(PROCESSED_DIR, "test.csv"),
    "output_dir": RESULTS_DIR,
    "n_estimators": 50,  # small/fast for test
    "max_depth": 10,
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 3 rf_model success=", result.success, "|", result.message)
assert result.success, result.message
assert os.path.isfile(result.data["model_path"])
assert os.path.isfile(result.data["independent_prediction_path"])
with open(result.data["independent_prediction_path"], encoding="utf-8") as f:
    indep = json.load(f)
print("  independent_prediction protocol:", indep["protocol"], "metrics:", indep["metrics"])
assert indep["protocol"] == "independent_prediction"
assert "MB_K" in indep["metrics"]
assert indep["split_method"] == "spatial_block_guard_buffer"
# random_state should have been correctly applied (not silently dropped, B-02)
assert result.data["params"]["random_state"] == 42
assert result.data["params"]["max_features"] == 0.5
print("  RF effective params (random_state/max_features preserved):", result.data["params"])

model_path = result.data["model_path"]

# ======================================================================
#  Stage 4: tcr_compute  (Agent injects legacy data_30m_csv/meta_30m_json pointing
#  to step2 files; constraint_csv/meta must be auto-derived from their directory)
# ======================================================================
from core.skills.builtin.tcr_compute import TCRComputeSkill

tcr_output = os.path.join(RESULTS_DIR, "tcr_result.csv")
skill = TCRComputeSkill()
result = skill.execute({
    "data_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),  # legacy, Agent-injected
    "meta_30m_json": os.path.join(PROCESSED_DIR, "30m_features_step2_meta.json"),  # legacy, Agent-injected
    "predict_10m_csv": os.path.join(PROCESSED_DIR, "10m_predict_features.csv"),
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
    "model_path": model_path,
    "output_path": tcr_output,
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 4 tcr_compute success=", result.success, "|", result.message)
assert result.success, result.message
assert result.data["mode"] == "block_constant"
assert os.path.isfile(tcr_output)
print("  TCR validity:", result.data["validity"])
assert result.data["validity"]["out_of_grid"] >= 0

# Verify block_constant closure property holds on THIS real pipeline run too
tcr_df = pd.read_csv(tcr_output)
constraint_df = pd.read_csv(os.path.join(PROCESSED_DIR, "30m_constraint_grid.csv"))
tcr_df["c_row"] = tcr_df["row"] // 3
tcr_df["c_col"] = tcr_df["col"] // 3
agg = tcr_df.groupby(["c_row", "c_col"])["LST_final"].mean().reset_index()
merged = agg.merge(constraint_df, left_on=["c_row", "c_col"], right_on=["row", "col"], suffixes=("", "_ref"))
closure_err = (merged["LST_final"] - merged["LST"]).abs()
print(f"  closure check on real pipeline run: max abs error = {closure_err.max():.6f} K (n={len(merged)})")
assert closure_err.max() < 0.01

# ======================================================================
#  Stage 5: lst_export  (row/col-correct GeoTIFF export)
# ======================================================================
from core.skills.builtin.lst_export import LSTExportSkill

skill = LSTExportSkill()
result = skill.execute({
    "input_csv": tcr_output,
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
    "output_dir": RESULTS_DIR,
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 5 lst_export success=", result.success, "|", result.message)
assert result.success, result.message
tif_path = result.data["tif_path"]
assert os.path.isfile(tif_path)

from osgeo import gdal
ds = gdal.Open(tif_path)
print(f"  GeoTIFF: {ds.RasterXSize}x{ds.RasterYSize}, band desc={ds.GetRasterBand(1).GetDescription()}")
assert ds.RasterXSize == W10 and ds.RasterYSize == H10
band_arr = ds.GetRasterBand(1).ReadAsArray()
# spot check a few known (row,col) positions against the CSV values directly (B-07 row/col correctness)
# 注：lst_export 完成后 rf_10m_predict.csv 已被中间产物清理删除，此处改读输入源 tcr_result.csv（LST_final 同源）
final_df = pd.read_csv(tcr_output)
sample_rows = final_df.dropna(subset=["LST_final"]).sample(20, random_state=1)
for _, r in sample_rows.iterrows():
    csv_val = r["LST_final"]
    tif_val = band_arr[int(r["row"]), int(r["col"])]
    assert abs(csv_val - tif_val) < 0.01, f"row/col mismatch at ({r['row']},{r['col']}): csv={csv_val} tif={tif_val}"
ds = None
print("  OK: GeoTIFF pixel values match CSV row/col exactly at 20 random sample points")

# ======================================================================
#  Stage 6: accuracy_eval  (coarse_constraint_closure, no 5K fields)
# ======================================================================
from core.skills.builtin.accuracy_eval import AccuracyEvalSkill

skill = AccuracyEvalSkill()
result = skill.execute({
    "full_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),  # legacy, Agent-injected
    "predict_csv": tcr_output,
    "output_dir": RESULTS_DIR,
    "meta_30m_json": os.path.join(PROCESSED_DIR, "30m_features_step2_meta.json"),  # legacy, Agent-injected
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
}, progress_callback=pcb, log_callback=lcb)
print("STAGE 6 accuracy_eval success=", result.success, "|", result.message)
assert result.success, result.message

report_path = result.data["report_path"]
with open(report_path, encoding="utf-8") as f:
    closure_json = json.load(f)
print("  closure JSON top-level keys:", list(closure_json.keys()))
assert closure_json["protocol"] == "coarse_constraint_closure"
raw_text = json.dumps(closure_json)
for forbidden in ["max_abs_deviation", "threshold_K", '"passed"', "5K", "通过", "超出"]:
    assert forbidden not in raw_text, f"forbidden 5K-era field/text '{forbidden}' still present in closure JSON"
print("  OK: no threshold_K/max_abs_deviation/passed/通过/超出 anywhere in closure JSON")
assert "low_end_difference_K" in closure_json["value_range"]
assert "high_end_difference_K" in closure_json["value_range"]
print("  value_range:", closure_json["value_range"])
assert "common_valid_footprint_diagnostic" in closure_json
print("  common_valid_footprint_diagnostic (backend-only):", closure_json["common_valid_footprint_diagnostic"])

# ======================================================================
#  Verify run_manifest.json accumulated all 6 stages correctly at PROJECT_DIR root
# ======================================================================
from core import manifest as run_manifest

manifest_data = run_manifest.load_manifest(PROJECT_DIR)
print()
print("run_manifest.json stages:", list(manifest_data["stages"].keys()))
for stage in ["data_pipeline", "ttri_compute", "rf_model", "tcr_compute", "lst_export", "accuracy_eval"]:
    assert stage in manifest_data["stages"], f"stage {stage} missing from run_manifest.json"
    assert manifest_data["stages"][stage]["status"] == "completed", f"stage {stage} not marked completed"
print("OK: run_manifest.json correctly accumulated all 6 stages at PROJECT_DIR root")
assert os.path.isfile(os.path.join(PROJECT_DIR, "run_manifest.json"))

shutil.rmtree(PROJECT_DIR, ignore_errors=True)
print()
print("ALL FULL-PIPELINE INTEGRATION TESTS PASSED")
