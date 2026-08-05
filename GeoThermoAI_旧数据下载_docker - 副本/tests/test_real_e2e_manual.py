"""
TRUE end-to-end test with REAL Planetary Computer data, exercising the exact
same skill-call chain (and Agent-style parameter injection quirks) as the
live server-driven workflow: data_acquisition -> data_pipeline -> ttri_compute
-> rf_model -> tcr_compute -> lst_export -> accuracy_eval.
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_DIR = tempfile.mkdtemp(prefix='real_e2e_')
RAW_DIR = os.path.join(PROJECT_DIR, "raw")
PROCESSED_DIR = os.path.join(PROJECT_DIR, "processed")
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")
for d in (RAW_DIR, PROCESSED_DIR, RESULTS_DIR):
    os.makedirs(d, exist_ok=True)

print("PROJECT_DIR =", PROJECT_DIR)
t0 = time.time()


def pcb(name, pct, msg):
    pass


def lcb(level, msg):
    if level in ("ERROR",):
        print(f'  [{level}] {msg}')


# ======================================================================
#  Stage 0: data_acquisition (real Planetary Computer download)
# ======================================================================
from core.skills.builtin.data_acquisition import DataAcquisitionSkill

acq = DataAcquisitionSkill()
# Slightly larger AOI than the earlier acquisition-only test, for more realistic sample counts
bbox = "114.28,30.52,114.40,30.64"  # ~12km x 12km near Wuhan
search_result = acq.execute({
    "region": bbox, "start_date": "2024-06-01", "end_date": "2024-08-31",
    "output_dir": RAW_DIR, "cloud_threshold": 60, "dem_source": "copernicus",
}, progress_callback=pcb, log_callback=lcb)
assert search_result.success, search_result.message
pairs = search_result.data.get("image_pairs", [])
print(f"[{time.time()-t0:.0f}s] Found {len(pairs)} candidate pairs")
assert len(pairs) > 0
# pick the pair with the lowest combined cloud cover for a cleaner test
selected = min(pairs, key=lambda p: (p.get("landsat_cloud_cover", 100) or 100) + (p.get("sentinel2_cloud_cover", 100) or 100))
print("Selected pair:", selected["landsat_date"], selected["landsat_satellite"], "+", selected["sentinel2_date"],
      "clouds:", selected.get("landsat_cloud_cover"), selected.get("sentinel2_cloud_cover"))

download_result = acq.execute({
    "region": bbox, "start_date": "2024-06-01", "end_date": "2024-08-31",
    "output_dir": RAW_DIR, "cloud_threshold": 60, "dem_source": "copernicus",
    "selected_pair": selected, "fetch_st_qa": True,
}, progress_callback=pcb, log_callback=lcb)
assert download_result.success, download_result.message
print(f"[{time.time()-t0:.0f}s] STAGE data_acquisition OK:", download_result.message)
acq_data = download_result.data

# ======================================================================
#  Stage 1: data_pipeline
# ======================================================================
from core.skills.builtin.data_pipeline import DataPipelineSkill

result = DataPipelineSkill().execute({
    "landsat_path": acq_data["landsat_path"], "qa_path": acq_data["qa_path"],
    "sentinel2_path": acq_data["sentinel2_path"], "scl_path": acq_data["scl_path"],
    "dem_path": acq_data["dem_path"], "st_qa_path": acq_data.get("st_qa_path") or None,
    "output_dir": PROCESSED_DIR,
    "train_ratio": 0.6, "val_ratio": 0.2, "test_ratio": 0.2, "seed": 42,
    "block_size_px": 10, "guard_buffer_m": 90.0,
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE data_pipeline OK:", result.message)
print("  has_st_qa =", result.data["has_st_qa"], "constraint_rows =", result.data["constraint_rows"])
assert result.data["constraint_rows"] > 0
assert result.data["train_rows"] > 0

# ======================================================================
#  Stage 2: ttri_compute
# ======================================================================
from core.skills.builtin.ttri_compute import TTRIComputeSkill

result = TTRIComputeSkill().execute({
    "train_csv": os.path.join(PROCESSED_DIR, "train.csv"),
    "val_csv": os.path.join(PROCESSED_DIR, "validate.csv"),
    "test_csv": os.path.join(PROCESSED_DIR, "test.csv"),
    "output_dir": PROCESSED_DIR,
    "data_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),
    "predict_10m_csv": os.path.join(PROCESSED_DIR, "10m_predict_features.csv"),
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE ttri_compute OK:", result.message)

# ======================================================================
#  Stage 3: rf_model
# ======================================================================
from core.skills.builtin.rf_model import RFModelSkill

result = RFModelSkill().execute({
    "train_csv": os.path.join(PROCESSED_DIR, "train.csv"),
    "val_csv": os.path.join(PROCESSED_DIR, "validate.csv"),
    "test_csv": os.path.join(PROCESSED_DIR, "test.csv"),
    "output_dir": RESULTS_DIR,
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE rf_model OK:", result.message)
model_path = result.data["model_path"]
with open(result.data["independent_prediction_path"], encoding="utf-8") as f:
    print("  independent_prediction:", json.load(f)["metrics"])

# ======================================================================
#  Stage 4: tcr_compute
# ======================================================================
from core.skills.builtin.tcr_compute import TCRComputeSkill

tcr_output = os.path.join(RESULTS_DIR, "tcr_result.csv")
result = TCRComputeSkill().execute({
    "data_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),
    "meta_30m_json": os.path.join(PROCESSED_DIR, "30m_features_step2_meta.json"),
    "predict_10m_csv": os.path.join(PROCESSED_DIR, "10m_predict_features.csv"),
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
    "model_path": model_path, "output_path": tcr_output,
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE tcr_compute OK:", result.message)
print("  validity:", result.data["validity"])
print("  grid_ratio_diagnostics:", result.data["grid_ratio_diagnostics"])

# ======================================================================
#  Stage 5: lst_export
# ======================================================================
from core.skills.builtin.lst_export import LSTExportSkill

result = LSTExportSkill().execute({
    "input_csv": tcr_output,
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
    "output_dir": RESULTS_DIR,
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE lst_export OK:", result.message)
tif_path = result.data["tif_path"]

# ======================================================================
#  Stage 6: accuracy_eval
# ======================================================================
from core.skills.builtin.accuracy_eval import AccuracyEvalSkill

result = AccuracyEvalSkill().execute({
    "full_30m_csv": os.path.join(PROCESSED_DIR, "30m_features_step2.csv"),
    "predict_csv": tcr_output, "output_dir": RESULTS_DIR,
    "meta_30m_json": os.path.join(PROCESSED_DIR, "30m_features_step2_meta.json"),
    "meta_10m_json": os.path.join(PROCESSED_DIR, "10m_predict_features_meta.json"),
}, progress_callback=pcb, log_callback=lcb)
assert result.success, result.message
print(f"[{time.time()-t0:.0f}s] STAGE accuracy_eval OK:", result.message)
with open(result.data["report_path"], encoding="utf-8") as f:
    closure_json = json.load(f)
print("  value_range:", closure_json["value_range"])
raw_text = json.dumps(closure_json)
for forbidden in ["max_abs_deviation", "threshold_K", '"passed"']:
    assert forbidden not in raw_text
print("  OK: no 5K-era fields in real-data closure JSON")

# ======================================================================
#  Final checks
# ======================================================================
from core import manifest as run_manifest
manifest_data = run_manifest.load_manifest(PROJECT_DIR)
print()
print("Final run_manifest.json stages:", {k: v["status"] for k, v in manifest_data["stages"].items()})
for stage in ["data_pipeline", "ttri_compute", "rf_model", "tcr_compute", "lst_export", "accuracy_eval"]:
    assert manifest_data["stages"][stage]["status"] == "completed"

import subprocess
du = subprocess.run(["du", "-sh", PROJECT_DIR], capture_output=True, text=True)
print("Project dir size:", du.stdout.strip())

print()
print(f"TOTAL ELAPSED: {time.time()-t0:.0f}s")
print("ALL REAL-DATA END-TO-END TESTS PASSED")
print("PROJECT_DIR (kept for inspection):", PROJECT_DIR)
