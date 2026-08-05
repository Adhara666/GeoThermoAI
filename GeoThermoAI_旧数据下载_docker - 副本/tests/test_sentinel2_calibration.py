import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import planetary_computer
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from core.skills.builtin import sentinel2_calibration as s2cal

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
catalog = Client.open(STAC_URL, headers={"Accept": "application/json"},
                       stac_io=StacApiIO(timeout=60, max_retries=2))

item_id = "S2B_MSIL2A_20240722T025529_R032_T50RKU_20240722T082729"
items = list(catalog.search(collections=["sentinel-2-l2a"], ids=[item_id]).items())
item = items[0]
signed = planetary_computer.sign(item)

logs = []
calib = s2cal.fetch_scene_calibration(signed, log_callback=lambda lvl, msg: logs.append((lvl, msg)))
print("calibration result:", calib)
for l in logs:
    print("LOG:", l)

assert calib["source"] == "per_scene_xml", f"expected real per-scene XML parse, got {calib['source']}"
assert calib["quantification_value"] == 10000.0
for band in ["B02", "B03", "B04", "B08", "B11"]:
    offset = s2cal.offset_for_band(calib, band)
    print(f"  {band}: offset = {offset}")
    assert offset == -1000.0, f"expected -1000 offset for {band}, got {offset}"
print("OK: real per-scene MTD_MSIL2A.xml parsing recovers offset=-1000, quantification=10000 for all 5 spectral bands")

# ---- Test fallback path: simulate an item with no product-metadata asset ----
import copy
broken_item = copy.deepcopy(signed)
broken_item.assets.pop("product-metadata", None)
calib2 = s2cal.fetch_scene_calibration(broken_item, log_callback=lambda lvl, msg: print("LOG2:", lvl, msg))
print("fallback calibration:", calib2)
assert calib2["source"] == "baseline_default_rule"
assert s2cal.offset_for_band(calib2, "B02") == -1000.0  # baseline 05.10 >= 4.0 -> -1000 fallback
print("OK: fallback to baseline-default-rule works and gives same physically-correct answer when XML unavailable")

# ---- Test old-baseline scenario (should give offset=0, no over-correction) ----
old_calib = s2cal.default_calibration_from_baseline("02.09")
print("old baseline (02.09) calibration:", old_calib)
assert s2cal.offset_for_band(old_calib, "B02") == 0.0
print("OK: pre-04.00 baseline correctly gets offset=0 (no blind -1000 subtraction on old products)")

print("ALL SENTINEL2_CALIBRATION TESTS PASSED")
