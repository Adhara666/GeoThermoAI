import json
import os
import shutil
import tempfile

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import ttri

workdir = tempfile.mkdtemp(prefix='ttri_test_')
print('workdir', workdir)

# ---- synthetic 30m step2 train/val/test with linear ground truth ----
rng = np.random.default_rng(42)
n = 200
true_a, true_b, true_c, true_intercept = 0.05, 0.2, -3.0, 290.0


def make_df(n, seed):
    r = np.random.default_rng(seed)
    dem = r.uniform(0, 500, n)
    slope = r.uniform(0, 30, n)
    aspect = r.uniform(0, 360, n)
    cos_aspect = np.cos(np.deg2rad(aspect))
    noise = r.normal(0, 0.01, n)
    lst = true_intercept + true_a * dem + true_b * slope + true_c * cos_aspect + noise
    df = pd.DataFrame({
        'row': np.arange(n), 'col': np.arange(n),
        'R': r.uniform(0, 1, n), 'G': r.uniform(0, 1, n), 'B': r.uniform(0, 1, n),
        'NIR': r.uniform(0, 1, n), 'SWIR1': r.uniform(0, 1, n),
        'NDVI': r.uniform(-1, 1, n), 'NDWI': r.uniform(-1, 1, n), 'NDBI': r.uniform(-1, 1, n),
        'DEM': dem, 'Slope': slope, 'Aspect': aspect, 'cos(Aspect)': cos_aspect,
        'LST': lst,
    })
    return df


train_csv = os.path.join(workdir, 'train.csv')
val_csv = os.path.join(workdir, 'validate.csv')
test_csv = os.path.join(workdir, 'test.csv')

make_df(n, 1).to_csv(train_csv, index=False)
make_df(80, 2).to_csv(val_csv, index=False)
make_df(80, 3).to_csv(test_csv, index=False)

result = ttri.compute_ttri_for_splits(train_csv, val_csv, test_csv, workdir)
print('fit coefficients:', result['coefficients'])
assert abs(result['coefficients']['coefficients'][0] - true_a) < 0.01
assert abs(result['coefficients']['coefficients'][1] - true_b) < 0.01
assert abs(result['coefficients']['coefficients'][2] - true_c) < 0.05
assert result['coefficients']['r2'] > 0.99
coef_path = result['coefficients_path']
assert os.path.basename(coef_path) == 'ttri_coefficients.json'
assert os.path.isfile(coef_path)
print('OK: fit_ttri_train recovers ground-truth coefficients; ttri_coefficients.json written')

# ---- Anti-leakage check: mutate validate/test LST, TTRI should NOT change ----
df_val_before = pd.read_csv(val_csv)
ttri_before = df_val_before['TTRI'].values.copy()

df_val_mutated = pd.read_csv(val_csv)
df_val_mutated['LST'] = df_val_mutated['LST'] + 999.0  # sabotage the label
df_val_mutated.to_csv(val_csv, index=False)

# Re-apply using SAME coefficients (simulating a second run without refit)
with open(coef_path, encoding='utf-8') as f:
    coef_dict = json.load(f)
ttri.apply_ttri_column(val_csv, coef_dict)
df_val_after = pd.read_csv(val_csv)
assert np.allclose(df_val_after['TTRI'].values, ttri_before, atol=1e-9), 'TTRI changed after LST label was corrupted -> LEAKAGE BUG'
print('OK: mutating validate LST does not change its TTRI (no leakage)')

# ---- Confirm train/validate/test coefficients are literally the SAME hash source ----
df_train = pd.read_csv(train_csv)
df_test = pd.read_csv(test_csv)
# recompute TTRI manually from coefficients and compare
coef_arr = np.array(coef_dict['coefficients'])
manual_train_ttri = df_train[['DEM', 'Slope', 'cos(Aspect)']].values @ coef_arr
assert np.allclose(manual_train_ttri, df_train['TTRI'].values, atol=1e-6)
manual_test_ttri = df_test[['DEM', 'Slope', 'cos(Aspect)']].values @ coef_arr
assert np.allclose(manual_test_ttri, df_test['TTRI'].values, atol=1e-6)
print('OK: train/validate/test TTRI all derive from the identical coefficient set')

# ---- B-04: rank-deficient / ill-conditioned should raise, not silently succeed ----
flat_df = make_df(50, 5)
flat_df['DEM'] = 100.0  # constant -> collinear-ish with intercept, should be flagged
flat_df['Slope'] = 0.0
flat_df['cos(Aspect)'] = 1.0
flat_csv = os.path.join(workdir, 'flat_train.csv')
flat_df.to_csv(flat_csv, index=False)
try:
    ttri.fit_ttri_train(flat_csv, workdir)
    raise SystemExit('expected ValueError for rank-deficient TTRI fit, but succeeded silently')
except ValueError as e:
    print('OK: rank-deficient/degenerate terrain correctly rejected:', str(e)[:80])

# ---- Full 30m constraint grid + dense TTRI grid + fine-grid bilinear interpolation ----
# Build a tiny 6x6 "30m constraint grid" (all valid) with a KNOWN linear DEM field so the
# TTRI grid itself is perfectly linear -> bilinear interpolation should reduce to the exact
# analytic value everywhere (this isolates "did the affine math get applied correctly").
H30, W30 = 6, 6
rows30, cols30 = np.meshgrid(np.arange(H30), np.arange(W30), indexing='ij')
rows30 = rows30.ravel()
cols30 = cols30.ravel()
dem30 = 10.0 * rows30 + 5.0 * cols30  # perfectly bilinear-representable
slope30 = np.zeros_like(dem30, dtype=float)
cosaspect30 = np.ones_like(dem30, dtype=float)
lst30 = true_intercept + true_a * dem30 + true_b * slope30 + true_c * cosaspect30
constraint_df = pd.DataFrame({
    'row': rows30, 'col': cols30, 'LST': lst30,
    'DEM': dem30, 'Slope': slope30, 'cos(Aspect)': cosaspect30,
})
constraint_csv = os.path.join(workdir, '30m_constraint_grid.csv')
constraint_df.to_csv(constraint_csv, index=False)

# 30m transform: origin (500000, 4000000), 30m pixel, north-up
coarse_transform = [30.0, 0.0, 500000.0, 0.0, -30.0, 4000000.0]
constraint_meta = {
    'height': H30, 'width': W30, 'crs': 'EPSG:32650', 'transform': coarse_transform,
}
constraint_meta_json = os.path.join(workdir, '30m_constraint_grid_meta.json')
with open(constraint_meta_json, 'w', encoding='utf-8') as f:
    json.dump(constraint_meta, f)

# fine 10m grid EXACTLY 3x denser, sharing the same origin (ideal aligned case)
fine_transform = [10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0]
H10, W10 = H30 * 3, W30 * 3
predict_meta = {'height': H10, 'width': W10, 'crs': 'EPSG:32650', 'transform': fine_transform}
predict_meta_json = os.path.join(workdir, '10m_predict_features_meta.json')
with open(predict_meta_json, 'w', encoding='utf-8') as f:
    json.dump(predict_meta, f)

rows10, cols10 = np.meshgrid(np.arange(H10), np.arange(W10), indexing='ij')
rows10 = rows10.ravel()
cols10 = cols10.ravel()
predict_df = pd.DataFrame({
    'row': rows10, 'col': cols10,
    'R': 0.1, 'G': 0.1, 'B': 0.1, 'NIR': 0.2, 'SWIR1': 0.2,
    'NDVI': 0.1, 'NDWI': 0.1, 'NDBI': 0.1,
})
predict_csv = os.path.join(workdir, '10m_predict_features.csv')
predict_df.to_csv(predict_csv, index=False)

ttri_predict_out = os.path.join(workdir, '10m_predict_with_ttri.csv')
res = ttri.compute_ttri_predict(
    constraint_csv, constraint_meta_json, predict_csv, predict_meta_json,
    coef_dict, ttri_predict_out, batch_size=100000,
)
print('compute_ttri_predict result:', {k: v for k, v in res.items() if k != 'grid_ratio_diagnostics'})
assert res['grid_ratio_diagnostics']['fast_path_eligible'] is True, 'exact aligned 3:1 grid should be flagged fast_path_eligible'

out_df = pd.read_csv(ttri_predict_out)
# analytic expected TTRI at fine pixel (row,col): its center maps to real-world (x,y),
# then to continuous 30m coords; because DEM is perfectly bilinear (linear in row/col),
# the interpolated DEM at any fine pixel center should equal the analytic DEM value there.
# NOTE: dem30 was defined directly as 10*row_index + 5*col_index (i.e. the raw 30m
# GRID INDEX is the coordinate plugged into the bilinear field), so the "array index
# coordinate" that interpolate_grid_to_fine() feeds into RegularGridInterpolator is
# EXACTLY the coordinate to plug into 10*x + 5*y -- no further "+0.5" pixel-center
# re-adjustment should be added on top (that was double-counting the center offset).
fine_center_row_in_30m_units = (out_df['row'].values + 0.5) / 3.0 - 0.5
fine_center_col_in_30m_units = (out_df['col'].values + 0.5) / 3.0 - 0.5
expected_dem = 10.0 * fine_center_row_in_30m_units + 5.0 * fine_center_col_in_30m_units
# Use the ACTUAL fitted coefficients (coef_dict), not the synthetic ground-truth
# true_a/b/c -- fit_ttri_train() only recovers them approximately from noisy data,
# so the correct oracle for "did interpolation reproduce the exact linear field
# built from THESE coefficients" must use the same coefficients that were applied.
fitted_a, fitted_b, fitted_c = coef_dict['coefficients']
expected_ttri = fitted_a * expected_dem + fitted_b * 0.0 + fitted_c * 1.0
valid = np.isfinite(out_df['TTRI'].values)
# edge pixels near the boundary can be NaN (outside interpolation support); interior must match closely
interior = valid & (out_df['row'] >= 1) & (out_df['row'] < H10 - 1) & (out_df['col'] >= 1) & (out_df['col'] < W10 - 1)
assert interior.sum() > 0
diff = np.abs(out_df.loc[interior, 'TTRI'].values - expected_ttri[interior])
print('max interior TTRI interpolation error:', diff.max())
assert diff.max() < 1e-6, f'bilinear interpolation via unified affine mapping does not match analytic value, max diff={diff.max()}'
print('OK: unified grid_mapping affine + bilinear interpolation matches analytic TTRI field exactly on aligned 3:1 grid')

shutil.rmtree(workdir, ignore_errors=True)
print('ALL TTRI TESTS PASSED')
