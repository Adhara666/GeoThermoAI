# -*- coding: utf-8 -*-
"""边界库：行政区/街道边界的下载、归一（WGS84）与检索。

定位：
  - 为「自然语言研究区」（说地名即建任务）提供边界文件检索；
  - 为「地点/街道/商圈问答」提供面统计所需的边界多边形；
  - 所有入库数据统一为 WGS84（EPSG:4326）GeoJSON。

来源：
  - 国内市/区级：阿里 DataV.GeoAtlas（GCJ-02，入库前转换）；
  - 国内街道级：OSM Overpass（尽力抓取，WGS84）；
  - 海外：geoBoundaries（CC-BY，WGS84）/ GADM（备用）。
"""

from core.boundaries.library import BoundaryLibrary, get_library  # noqa: F401
from core.boundaries.coordconv import gcj02_to_wgs84, wgs84_to_gcj02  # noqa: F401

__all__ = ["BoundaryLibrary", "get_library", "gcj02_to_wgs84", "wgs84_to_gcj02"]
