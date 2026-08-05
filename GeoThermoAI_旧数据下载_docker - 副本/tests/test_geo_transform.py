import math
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import geo_transform as gt

# Wuhan bbox (lon_min, lat_min, lon_max, lat_max), matches review's cited example
wuhan_bbox = [113.7, 29.9, 114.9, 31.3]
utm_epsg = gt.utm_epsg_for_lonlat((wuhan_bbox[0] + wuhan_bbox[2]) / 2, (wuhan_bbox[1] + wuhan_bbox[3]) / 2)
print('Wuhan UTM EPSG:', utm_epsg)
assert utm_epsg == 32650, f'expected EPSG:32650 for Wuhan, got {utm_epsg}'

x1, y1, x2, y2 = gt.bbox_wgs84_to_utm_bounds(wuhan_bbox, utm_epsg)
print(f'Wuhan bbox -> UTM: ({x1:.2f}, {y1:.2f}) - ({x2:.2f}, {y2:.2f})')
assert all(math.isfinite(v) for v in (x1, y1, x2, y2))
assert x2 > x1 and y2 > y1
# review cited approx (181314.76, 3312282.69) - (300130.89, 3464753.66) for two-corner method;
# our densified-boundary method should be in the same ballpark (within a few km)
assert abs(x1 - 181314.76) < 5000
assert abs(y1 - 3312282.69) < 5000
assert abs(x2 - 300130.89) < 5000
assert abs(y2 - 3464753.66) < 5000
print('OK: Wuhan bbox transforms to sane, finite UTM bounds matching review-cited values')

# Beijing bbox
beijing_bbox = [115.4, 39.4, 117.5, 41.1]
utm_epsg_bj = gt.utm_epsg_for_lonlat((beijing_bbox[0] + beijing_bbox[2]) / 2, (beijing_bbox[1] + beijing_bbox[3]) / 2)
x1, y1, x2, y2 = gt.bbox_wgs84_to_utm_bounds(beijing_bbox, utm_epsg_bj)
print(f'Beijing UTM EPSG {utm_epsg_bj}: ({x1:.2f}, {y1:.2f}) - ({x2:.2f}, {y2:.2f})')
assert all(math.isfinite(v) for v in (x1, y1, x2, y2)) and x2 > x1 and y2 > y1
print('OK: Beijing bbox handled correctly')

# Southern hemisphere test (e.g. Sydney, Australia area)
sydney_bbox = [150.5, -34.2, 151.5, -33.5]
utm_epsg_syd = gt.utm_epsg_for_lonlat((sydney_bbox[0] + sydney_bbox[2]) / 2, (sydney_bbox[1] + sydney_bbox[3]) / 2)
print('Sydney UTM EPSG:', utm_epsg_syd)
assert utm_epsg_syd >= 32700, 'southern hemisphere should map to 327xx EPSG range'
x1, y1, x2, y2 = gt.bbox_wgs84_to_utm_bounds(sydney_bbox, utm_epsg_syd)
print(f'Sydney UTM bounds: ({x1:.2f}, {y1:.2f}) - ({x2:.2f}, {y2:.2f})')
assert all(math.isfinite(v) for v in (x1, y1, x2, y2)) and x2 > x1 and y2 > y1
print('OK: southern hemisphere bbox handled correctly (finite, sane UTM zone)')

# Reproduce the ORIGINAL buggy behavior to prove the fix actually matters:
# without SetAxisMappingStrategy, GDAL3's default authority-compliant axis order
# for EPSG:4326 is (lat, lon), so calling TransformPoint(lon, lat) [traditional GIS
# order] against a srs WITHOUT the override produces garbage/inf for many bboxes.
from osgeo import osr
osr.UseExceptions()
srs_wgs84_buggy = osr.SpatialReference()
srs_wgs84_buggy.ImportFromEPSG(4326)
# do NOT set axis mapping strategy -> GDAL3 default authority-compliant (lat, lon)
srs_utm_buggy = osr.SpatialReference()
srs_utm_buggy.ImportFromEPSG(32650)
ct_buggy = osr.CoordinateTransformation(srs_wgs84_buggy, srs_utm_buggy)
try:
    x, y, _z = ct_buggy.TransformPoint(114.3, 30.59)  # old code's argument order: (lon, lat)
    buggy_is_finite = math.isfinite(x) and math.isfinite(y)
except Exception:
    buggy_is_finite = False
print(f'Reproduced OLD buggy call TransformPoint(114.3, 30.59) without axis override -> finite={buggy_is_finite}')
assert not buggy_is_finite, (
    'Expected the ORIGINAL bug (no axis mapping strategy set) to produce a non-finite '
    'result for this call, confirming the fix in geo_transform.py is actually necessary '
    'in this GDAL version -- if this assertion fails, the GDAL version being tested against '
    'may not reproduce the original bug and the regression test should be revisited.'
)
print('OK: confirmed the ORIGINAL bug reproduces non-finite output on this GDAL version, validating the fix is necessary')

print('ALL GEO_TRANSFORM TESTS PASSED')
