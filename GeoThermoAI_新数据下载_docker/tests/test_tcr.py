import json
import os
import shutil
import tempfile

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tcr

workdir = tempfile.mkdtemp(prefix='tcr_test_')
print('workdir', workdir)

FEATS = ["R", "G", "B", "NIR", "SWIR1", "NDVI", "NDWI", "NDBI", "TTRI"]

# ---- tiny RF model trained on synthetic data (doesn't need to be accurate) ----
rng = np.random.default_rng(0)
Xtr = rng.uniform(0, 1, size=(200, len(FEATS)))
ytr = 290 + 5 * Xtr[:, 0] - 3 * Xtr[:, -1] + rng.normal(0, 0.1, 200)
model = RandomForestRegressor(n_estimators=20, random_state=0)
model.fit(Xtr, ytr)
model_path = os.path.join(workdir, 'rf_ttri_model_run001.pkl')
joblib.dump(model, model_path)
metrics_path = os.path.join(workdir, 'rf_ttri_metrics_run001.json')
with open(metrics_path, 'w', encoding='utf-8') as f:
    json.dump({"features": FEATS}, f)

# ---- 30m constraint grid: 6x6, exact-aligned 3:1 with 10m grid ----
H30, W30 = 6, 6
r30, c30 = np.meshgrid(np.arange(H30), np.arange(W30), indexing='ij')
r30 = r30.ravel(); c30 = c30.ravel()
lst_true = 295.0 + 0.5 * r30 - 0.3 * c30  # smooth synthetic reference field
constraint_df = pd.DataFrame({'row': r30, 'col': c30, 'LST': lst_true})
constraint_csv = os.path.join(workdir, '30m_constraint_grid.csv')
constraint_df.to_csv(constraint_csv, index=False)

coarse_transform = [30.0, 0.0, 500000.0, 0.0, -30.0, 4000000.0]
constraint_meta = {'height': H30, 'width': W30, 'crs': 'EPSG:32650', 'transform': coarse_transform}
constraint_meta_json = os.path.join(workdir, '30m_constraint_grid_meta.json')
json.dump(constraint_meta, open(constraint_meta_json, 'w', encoding='utf-8'))

# ---- 10m predict grid: exactly 3x denser, same origin ----
fine_transform = [10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0]
H10, W10 = H30 * 3, W30 * 3
predict_meta = {'height': H10, 'width': W10, 'crs': 'EPSG:32650', 'transform': fine_transform}
predict_meta_json = os.path.join(workdir, '10m_predict_features_meta.json')
json.dump(predict_meta, open(predict_meta_json, 'w', encoding='utf-8'))

r10, c10 = np.meshgrid(np.arange(H10), np.arange(W10), indexing='ij')
r10 = r10.ravel(); c10 = c10.ravel()
n10 = len(r10)
rng2 = np.random.default_rng(1)
feat_data = {f: rng2.uniform(0, 1, n10) for f in FEATS}
predict_df = pd.DataFrame({'row': r10, 'col': c10, **feat_data})
predict_csv = os.path.join(workdir, '10m_predict_features.csv')
predict_df.to_csv(predict_csv, index=False)

output_bc = os.path.join(workdir, 'tcr_predict_block_constant.csv')
res_bc = tcr.compute_tcr(
    constraint_csv, constraint_meta_json, predict_csv, predict_meta_json,
    model_path, output_bc, mode='block_constant', batch_size=50,
)
print('block_constant tcr_statistics:', res_bc['tcr_statistics'])
print('block_constant validity:', res_bc['validity'])
assert res_bc['validity']['out_of_grid'] == 0
assert res_bc['validity']['reference_30m_valid_cells'] == H30 * W30

out_bc = pd.read_csv(output_bc)
merged = out_bc.copy()
merged['coarse_row'] = merged['row'] // 3
merged['coarse_col'] = merged['col'] // 3
agg = merged.groupby(['coarse_row', 'coarse_col'])['LST_final'].mean().reset_index()
agg = agg.merge(constraint_df, left_on=['coarse_row', 'coarse_col'], right_on=['row', 'col'], suffixes=('', '_ref'))
closure_err = (agg['LST_final'] - agg['LST']).abs()
print('block_constant closure max abs error (should be ~0, allow 4-decimal rounding):', closure_err.max())
assert closure_err.max() < 1e-3, f'block_constant does not exactly close: {closure_err.max()}'
print('OK: block_constant achieves exact per-cell arithmetic-mean closure')

output_sr = os.path.join(workdir, 'tcr_predict_smooth_recentered.csv')
res_sr = tcr.compute_tcr(
    constraint_csv, constraint_meta_json, predict_csv, predict_meta_json,
    model_path, output_sr, mode='smooth_recentered', batch_size=50,
)
print('smooth_recentered diagnostics:', json.dumps(res_sr['smooth_recentered_diagnostics'], indent=2))
post_err = res_sr['smooth_recentered_diagnostics']['post_recenter']['cell_mean_abs_error_K']['max']
print('smooth_recentered post-recenter max cell mean abs error:', post_err)
assert post_err is not None and post_err < 1e-3, 'smooth_recentered failed to close after recentering'

out_sr = pd.read_csv(output_sr)
merged_sr = out_sr.copy()
merged_sr['coarse_row'] = merged_sr['row'] // 3
merged_sr['coarse_col'] = merged_sr['col'] // 3
agg_sr = merged_sr.groupby(['coarse_row', 'coarse_col'])['LST_final'].mean().reset_index()
agg_sr = agg_sr.merge(constraint_df, left_on=['coarse_row', 'coarse_col'], right_on=['row', 'col'], suffixes=('', '_ref'))
closure_err_sr = (agg_sr['LST_final'] - agg_sr['LST']).abs()
print('smooth_recentered closure max abs error:', closure_err_sr.max())
assert closure_err_sr.max() < 1e-3, f'smooth_recentered does not close: {closure_err_sr.max()}'

# smooth_recentered should NOT be perfectly block-flat within a cell (i.e. some within-cell variation)
within_cell_std = merged_sr.groupby(['coarse_row', 'coarse_col'])['TCR'].std().dropna()
print('smooth_recentered within-cell TCR std (sample):', within_cell_std.head().to_dict())
assert (within_cell_std > 1e-6).any(), 'smooth_recentered TCR is flat within cells, no smoothing effect detected'
print('OK: smooth_recentered closes exactly AND preserves within-cell smooth variation')

# Sanity: block_constant TCR should be EXACTLY flat within each cell (by construction)
within_cell_std_bc = merged.groupby(['coarse_row', 'coarse_col'])['TCR'].std(ddof=0).dropna()
print('block_constant within-cell TCR std (should be ~0):', within_cell_std_bc.max())
assert within_cell_std_bc.max() < 1e-6
print('OK: block_constant TCR is exactly constant within each 30m cell')

# ---- Non-exact ratio grid: verify no crash and out_of_grid tracked, no KDTree anchor blow-up ----
fine_transform2 = [9.5, 0.0, 500000.0, 0.0, -9.5, 4000000.0]  # slightly mismatched, not exactly 3x
predict_meta2 = {'height': H10, 'width': W10, 'crs': 'EPSG:32650', 'transform': fine_transform2}
predict_meta_json2 = os.path.join(workdir, '10m_predict_features_meta2.json')
json.dump(predict_meta2, open(predict_meta_json2, 'w', encoding='utf-8'))
output_bc2 = os.path.join(workdir, 'tcr_predict_mismatched.csv')
res_bc2 = tcr.compute_tcr(
    constraint_csv, constraint_meta_json, predict_csv, predict_meta_json2,
    model_path, output_bc2, mode='block_constant', batch_size=50,
)
print('mismatched-grid diagnostics:', res_bc2['grid_ratio_diagnostics'])
assert res_bc2['grid_ratio_diagnostics']['fast_path_eligible'] is False
print('OK: non-exact-ratio grid handled without crash; correctly flagged as not fast-path-eligible')

shutil.rmtree(workdir, ignore_errors=True)
print('ALL TCR TESTS PASSED')
