"""Test core.pipeline.EasyLSTPipeline end-to-end with synthetic rasters (rewrite)."""
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import Affine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_DIR = tempfile.mkdtemp(prefix='easylst_test_')
RAW_DIR = os.path.join(PROJECT_DIR, "raw")
os.makedirs(RAW_DIR, exist_ok=True)
print("PROJECT_DIR =", PROJECT_DIR)

CRS = "EPSG:32650"
H30, W30 = 36, 36
H10, W10 = H30 * 3, W30 * 3
ORIGIN_X, ORIGIN_Y = 500000.0, 3400000.0
transform_30m = Affine(30.0, 0.0, ORIGIN_X, 0.0, -30.0, ORIGIN_Y)
transform_10m = Affine(10.0, 0.0, ORIGIN_X, 0.0, -10.0, ORIGIN_Y)


def write_raster(path, array, transform, dtype, nodata=None, count=1):
    if array.ndim == 2:
        array = array[None, ...]
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[1], width=array.shape[2],
                        count=count, dtype=dtype, crs=CRS, transform=transform, nodata=nodata) as dst:
        for i in range(count):
            dst.write(array[i], i + 1)


rng = np.random.default_rng(11)
row_idx, col_idx = np.meshgrid(np.arange(H30), np.arange(W30), indexing="ij")
dem = (60.0 + 1.5 * row_idx + 0.7 * col_idx + 12.0 * np.sin(row_idx / 5.0) * np.cos(col_idx / 4.0)).astype(np.float32)
write_raster(os.path.join(RAW_DIR, "dem.tif"), dem, transform_30m, "float32")

true_temp_k = 300.0 - 0.008 * dem + rng.normal(0, 0.25, size=dem.shape).astype(np.float32)
lst_dn = ((true_temp_k - 149.0) / 0.00341802).astype(np.float32)
write_raster(os.path.join(RAW_DIR, "landsat_lst.tif"), lst_dn, transform_30m, "float32", nodata=0.0)

qa_pixel = np.zeros((H30, W30), dtype=np.uint16)
write_raster(os.path.join(RAW_DIR, "landsat_qa_pixel.tif"), qa_pixel, transform_30m, "uint16")

s2_bands = np.stack([rng.uniform(800, 1800, size=(H10, W10)).astype(np.float32) for _ in range(5)])
write_raster(os.path.join(RAW_DIR, "sentinel2_bands.tif"), s2_bands, transform_10m, "float32", nodata=-9999.0, count=5)

scl = np.full((H10, W10), 4, dtype=np.uint8)
write_raster(os.path.join(RAW_DIR, "sentinel2_scl.tif"), scl, transform_10m, "uint8")

print("Synthetic raw rasters written.")

from core.pipeline import EasyLSTPipeline

pipeline = EasyLSTPipeline()
output_dir = os.path.join(PROJECT_DIR, "output")
pipeline.configure(
    landsat_path=os.path.join(RAW_DIR, "landsat_lst.tif"),
    sentinel2_path=os.path.join(RAW_DIR, "sentinel2_bands.tif"),
    qa_path=os.path.join(RAW_DIR, "landsat_qa_pixel.tif"),
    scl_path=os.path.join(RAW_DIR, "sentinel2_scl.tif"),
    dem_path=os.path.join(RAW_DIR, "dem.tif"),
    output_dir=output_dir,
    train_ratio=0.6, val_ratio=0.2, test_ratio=0.2, seed=42,
    block_size_px=6, guard_buffer_m=30.0,
    rf_params={"n_estimators": 40, "max_depth": 8},
    tcr_mode="block_constant",
)

progress_log = []
message_log = []


def pcb(step, pct, msg):
    progress_log.append((step, pct))


def lcb(level, msg):
    message_log.append((level, msg))
    if level == "ERROR":
        print(f"  [{level}] {msg}")


results = pipeline.run_full(pcb, lcb)

print()
print("Steps executed:", list(results.keys()))
failed_steps = [k for k, v in results.items() if isinstance(v, dict) and "error" in v]
print("Failed steps:", failed_steps)
assert not failed_steps, f"pipeline had failing steps: {failed_steps}"

expected_steps = [
    "preprocessing", "split_dataset", "ttri_train", "train_rf", "predict_test",
    "ttri_predict", "tcr", "lst_final", "export_geotiff", "evaluate_closure",
]
for step in expected_steps:
    assert step in results, f"expected step {step} did not run"
print("OK: all 10 EasyLSTPipeline steps completed successfully (fail-fast, no silent skip)")

# Verify run_manifest.json was written at output_dir root and covers all steps
from core import manifest as run_manifest
manifest_data = run_manifest.load_manifest(output_dir)
print("run_manifest stages:", list(manifest_data["stages"].keys()))
for step in expected_steps:
    assert manifest_data["stages"].get(step, {}).get("status") == "completed"
print("OK: run_manifest.json correctly tracked all EasyLSTPipeline stages")

# Sanity-check key fixed-name outputs exist
paths = pipeline.get_default_paths()
for key in ["train_split", "val_split", "test_split", "ttri_coefficients", "constraint_csv", "lst_final_csv", "lst_final_tif"]:
    assert os.path.isfile(paths[key]), f"expected fixed output missing: {key} -> {paths[key]}"
print("OK: all fixed-name output files exist on disk")

# Verify NO *_with_TTRI.parquet files were created (fix: old broken path names should not exist)
for bad_name in ["train_with_TTRI.parquet", "validate_with_TTRI.parquet", "test_with_TTRI.parquet"]:
    bad_path = os.path.join(os.path.dirname(paths["train_split"]), bad_name)
    assert not os.path.exists(bad_path), f"stale broken filename should not exist: {bad_path}"
print("OK: no stale *_with_TTRI.parquet broken filenames created")

# ---- Test fail-fast: intentionally break preprocessing input, verify downstream is skipped ----
pipeline2 = EasyLSTPipeline()
output_dir2 = os.path.join(PROJECT_DIR, "output_fail")
pipeline2.configure(
    landsat_path="/nonexistent/landsat.tif",
    sentinel2_path=os.path.join(RAW_DIR, "sentinel2_bands.tif"),
    qa_path=os.path.join(RAW_DIR, "landsat_qa_pixel.tif"),
    scl_path=os.path.join(RAW_DIR, "sentinel2_scl.tif"),
    dem_path=os.path.join(RAW_DIR, "dem.tif"),
    output_dir=output_dir2,
)
results2 = pipeline2.run_full(pcb, lcb)
print()
print("Fail-fast test: steps attempted =", list(results2.keys()))
assert list(results2.keys()) == ["preprocessing"], (
    f"expected ONLY 'preprocessing' to be attempted after it fails (fail-fast), "
    f"got: {list(results2.keys())}"
)
assert "error" in results2["preprocessing"]
manifest2 = run_manifest.load_manifest(output_dir2)
assert manifest2["stages"]["preprocessing"]["status"] == "failed"
assert manifest2["stages"]["split_dataset"]["status"] == "skipped_upstream"
assert manifest2["stages"]["evaluate_closure"]["status"] == "skipped_upstream"
print("OK: fail-fast correctly stopped after first failure; ALL downstream steps marked skipped_upstream in manifest")

shutil.rmtree(PROJECT_DIR, ignore_errors=True)
print()
print("ALL EASYLSTPIPELINE TESTS PASSED")
