# -*- coding: utf-8 -*-
"""边界库下载工具。

用法（容器内执行）：
  python3 /app/scripts/fetch_boundaries.py cn city 420100
      下载「市级 adcode」的城市 + 全部区县（DataV，含 GCJ-02→WGS84 转换）
  python3 /app/scripts/fetch_boundaries.py cn streets 洪山区 --city 武汉市 --limit 0
      抓取某区/市范围内全部街道边界（OSM，尽力抓取，无几何的街道自动跳过）
  python3 /app/scripts/fetch_boundaries.py global USA 2
      下载某国某层级边界（geoBoundaries，懒加载大文件）
  python3 /app/scripts/fetch_boundaries.py list
      查看当前边界库索引概况
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from core.boundaries.library import get_library  # noqa: E402
from core.boundaries import fetcher  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="GeoThermoAI 边界库下载工具")
    sub = ap.add_subparsers(dest="kind", required=True)

    p1 = sub.add_parser("cn", help="国内（DataV 市区 / OSM 街道）")
    p1.add_argument("mode", choices=["city", "streets"])
    p1.add_argument("code", help="city 模式=市级 adcode；streets 模式=区/市名")
    p1.add_argument("--city", default="", help="streets 模式下入库上级城市名")
    p1.add_argument("--parent", default="", help="city 模式的上级省名（可选）")
    p1.add_argument("--limit", type=int, default=0, help="streets 抓取上限（0=全部）")
    p1.add_argument("--pause", type=float, default=2.0, help="街道查询间隔秒")

    p2 = sub.add_parser("global", help="海外（geoBoundaries）")
    p2.add_argument("iso", help="ISO3 国家码，如 USA/FRA/GBR")
    p2.add_argument("adm", type=int, help="层级数字，如 2/3/4/5")

    sub.add_parser("list", help="索引概况")

    args = ap.parse_args()
    lib = get_library()

    if args.kind == "list":
        data = lib.load()
        print("数据根目录:", lib.root)
        print("国内条目:", len(data["entries"]))
        for e in data["entries"][:20]:
            print("  [%s] %s (%s) -> %s"
                  % (e["level"], e["name"], e.get("parent") or "-", e["path"]))
        if len(data["entries"]) > 20:
            print("  ... 共", len(data["entries"]), "条")
        print("海外文件:", data["global_files"])
        return 0

    if args.kind == "cn" and args.mode == "city":
        out = fetcher.fetch_cn_city(args.code, lib, parent=args.parent)
        print("城市:", out["city"])
        print("区县(%d):" % len(out["districts"]), "、".join(out["districts"]))
        return 0

    if args.kind == "cn" and args.mode == "streets":
        out = fetcher.fetch_cn_streets(args.code, lib, city_name=args.city,
                                       pause=args.pause, limit=args.limit)
        print("范围:", out["area"], "| OSM 候选街道:", out["candidates"])
        print("成功入库(%d):" % len(out["saved"]), "、".join(out["saved"]))
        print("跳过(%d):" % len(out["skipped"]), "、".join(out["skipped"][:10]))
        return 0

    if args.kind == "global":
        out = fetcher.fetch_global(args.iso, args.adm, lib)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
