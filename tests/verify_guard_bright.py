# -*- coding: utf-8 -*-
"""验证防刷屏熔断 + S2 提亮拉伸（可删除）。"""
import io
import math
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from core.web_app import _guard_stream_text  # noqa: E402
from core.visualization import LayerVisualizer  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


# 1) 复读熔断（行级：整段说明反复换行重复——用户实测场景）
piece = "配对模式：系统在指定的时间范围内为当前任务分析所有可获取的卫星影像，" \
        "逐对处理，保留单日细节。"
spam = (piece + "\n") * 300
guarded = _guard_stream_text(spam)
check("复读文本被熔断（大幅缩短且带提示）",
      len(guarded) < 2000 and "已自动截断" in guarded,
      f"原长 {len(spam)} → 截后 {len(guarded)}")

# 变体复读（数字/标点微变，绕过行级判重）→ 硬长度上限兜底
vary = "".join(
    f"Landsat: 2025-07-{20 + i % 5} (云量 {15.8 + i * 0.1:.1f}%, L9)\n"
    f"Sentinel-2: 2025-07-{20 + i % 5} (云量 13.2%)\n"
    f"成像时差: {i % 3} 天\n"
    for i in range(200))
guarded_v = _guard_stream_text(vary)
check("变体复读被硬长度上限截断（≤3040）",
      len(guarded_v) <= 3040 and "截断" in guarded_v,
      f"原长 {len(vary)} → 截后 {len(guarded_v)}")

# 正常长文本（无复读、无换行）不误伤
normal = "".join(f"第{i}步：执行数据处理并检查结果是否满足要求。" for i in range(90))
out = _guard_stream_text(normal)
check("正常编号序列不被复读熔断误伤",
      "检测到重复" not in out, f"len={len(out)}")

# 正常多行步骤列表（行内容各异）不误伤
steps = "\n".join(f"步骤 {i}：处理数据集 {i} 并保存检查点。" for i in range(60))
out3 = _guard_stream_text(steps)
check("正常多行列表不误伤", "检测到重复" not in out3, f"len={len(out3)}")

# 超长截断
huge = "很长内容" * 5000
out2 = _guard_stream_text(huge)
check("超长输出被长度上限截断", len(out2) <= 3000 + 40 and "截断" in out2)

# 2) S2 提亮：同瓦片渲染亮度应较 2/98 拉伸更亮
PROD = ("/app/data/state_kernel/executions/runs/"
        "4d661355cf924003a612a2592ab86477/committed/preprocess_split/"
        "attempt-1/Aligned_S2_30m.tif")
import rasterio  # noqa: E402
p = Path(PROD)
with rasterio.open(p) as src:
    cx, cy = (src.bounds.left + src.bounds.right) / 2, \
             (src.bounds.bottom + src.bounds.top) / 2
    from rasterio.warp import transform as _wt
    lon, lat = _wt(src.crs, "EPSG:4326", [cx], [cy])
lon, lat = lon[0], lat[0]
n = 2 ** 13
x = int((lon + 180) / 360 * n)
lat_r = math.radians(lat)
y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
LayerVisualizer._render_tile_cached.cache_clear()
LayerVisualizer._STATS_CACHE.clear()
png = LayerVisualizer._render_tile_cached("sentinel_rgb", str(p),
                                          p.stat().st_mtime, 13, x, y)
img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
m = img.sum(axis=2) > 10
bright = img[..., :3][m].mean()
print(f"  S2 提亮后平均亮度: {bright:.0f}（修复前约 63）")
check("S2 RGB 亮度提升（≥75）", bright >= 75, f"{bright:.0f}")

print(f"\n结果：{ok} 项通过，{fail} 项失败")
sys.exit(1 if fail else 0)
