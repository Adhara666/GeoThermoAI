# -*- coding: utf-8 -*-
"""地理问答引擎：地点/街道/商圈提问 → 对 LST 结果做统计 → 结构化回答。

模块：
  - geocode：地名 → 坐标（高德/Nominatim；本地边界库优先）
  - stats：点缓冲/多边形统计 + 覆盖范围守卫
  - service：目标解析与回答编排（本模块对外唯一入口 answer_geo_query）
"""

from core.geoqa.service import answer_geo_query, identify_place  # noqa: F401

__all__ = ["answer_geo_query", "identify_place"]
