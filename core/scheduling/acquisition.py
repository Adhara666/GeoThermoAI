"""检索、不可变选择、资产获取、本地准备的四个独立执行边界。"""
import copy
import hashlib
import json
import os
from pathlib import Path

from .transfer import fetch_file, file_hash, stable_url
from .worker import write_json


def values(snapshot):
    return {k: v["value"] for k, v in snapshot.get("params", {}).items()}


def region_path(snapshot):
    from core.agent.understanding.slotbook import SlotBook
    entry = SlotBook(snapshot.get("slot_values", {})).get("region")
    detail = entry.get("detail", {})
    path = detail.get("path", "")
    if not path or not Path(path).is_file():
        raise ValueError("运行绑定的研究区文件不存在")
    expected = detail.get("content_hash")
    if expected and file_hash(path) != expected:
        raise ValueError("研究区文件已变化，必须建立新运行")
    return path


def _skill(snapshot):
    from core.skills.builtin.data_acquisition import DataAcquisitionSkill
    skill = DataAcquisitionSkill()
    # 用户凭据执行时读取，不写入冻结快照。禁止扫描其他用户设置作为兜底。
    settings_path = snapshot.get("execution", {}).get("settings_path")
    def source_config():
        if not settings_path:
            return {}
        return json.loads(Path(settings_path).read_text(encoding="utf-8")).get("data_space", {})
    skill._load_dataspace_config = source_config
    return skill


def _clean_item(item):
    item = copy.deepcopy(item)
    for a in item.get("assets", {}).values():
        if a.get("href"):
            a["href"] = stable_url(a["href"])
    item["links"] = []
    return item


def search(spec, report):
    snapshot, stage = spec["snapshot"], Path(spec["staging_dir"])
    config = values(snapshot)
    skill = _skill(snapshot)
    region = region_path(snapshot)
    tr = config.get("time", {})
    result = skill.execute({"region": region, "start_date": tr.get("start"), "end_date": tr.get("end"),
                            "composite": config.get("product_mode"), "cloud_threshold": config.get("cloud_threshold", 30),
                            "output_dir": str(stage / "search")}, report, report)
    if not result.success:
        from .transfer import TransferError
        raise TransferError(result.message)
    found = result.data
    for key in ("landsat_items", "sentinel2_items"):
        found[key] = [_clean_item(i) for i in found.get(key, [])]
    # DEM 检索也属于检索节点，准备节点不会再次访问目录。
    datasets = config.get("datasets") or ["landsat", "sentinel2", "dem"]
    if "dem" in datasets:
        from core.skills.builtin.data_acquisition import _open_catalog, _search_items
        catalog, error = _open_catalog(report)
        if catalog is None:
            raise RuntimeError(f"DEM 目录不可用 {error}")
        found["dem_items"] = [_clean_item(i.to_dict()) for i in _search_items(catalog, report, "DEM", collections=["cop-dem-glo-30"], bbox=skill._parse_region(region), max_items=10)]
    target = stage / "candidates.json"
    write_json(target, found)
    return {"context": {"candidates_path": str(target)}, "message": result.message}


def select(spec):
    from core.agent.roles.data_agent import rank_pairs
    from core.agent.orchestrator.exec_mode import is_auto
    config = values(spec["snapshot"])
    candidates = json.loads(Path(spec["context"]["candidates_path"]).read_text(encoding="utf-8"))
    mode = config.get("product_mode", "pair")
    capability = json.loads(spec["node"]["frozen_inputs"])["capability"]
    answer = json.loads(spec["node"]["params"]).get("execution_answer")
    pair = None
    if mode != "monthly" and capability != "download_subset":
        pairs = rank_pairs(candidates.get("image_pairs", []))
        if not pairs:
            raise ValueError("没有符合原配对规则的候选，需要修改任务后重试")
        if answer:
            pair = next((p for i, p in enumerate(pairs) if str(i + 1) == str(answer.get("option_id"))), None)
            if pair is None:
                raise ValueError("选择不在已保存候选内")
        elif not is_auto(config.get("exec_mode")):
            return {"question": {"prompt": "请选择本次影像配对", "candidates": [
                {"id": str(i + 1), "label": f"L{p['landsat_date']} S{p['sentinel2_date']}", "value": p}
                for i, p in enumerate(pairs)]}}
        else:
            pair = pairs[0]
    groups = {"landsat": candidates.get("landsat_items", []), "sentinel2": candidates.get("sentinel2_items", []), "dem": candidates.get("dem_items", [])}
    if pair:
        sat = pair.get("landsat_satellite", "")
        def satellite(i):
            name = i["id"].lower()
            return not sat or (sat == "L8" and (name.startswith("lc08") or "landsat_8" in name)) or (sat == "L9" and (name.startswith("lc09") or "landsat_9" in name)) or sat not in ("L8", "L9")
        groups["landsat"] = [i for i in groups["landsat"] if i["properties"].get("datetime", "").startswith(pair["landsat_date"]) and satellite(i)]
        groups["sentinel2"] = [i for i in groups["sentinel2"] if i["properties"].get("datetime", "").startswith(pair["sentinel2_date"])]
    requested = config.get("datasets") or ["landsat", "sentinel2", "dem"]
    groups = {k: v for k, v in groups.items() if k in requested}
    if any(not items for items in groups.values()):
        raise ValueError("已选数据集合缺少必需场景")
    selection = {"mode": mode, "pair": pair, "scene_ids": {k: [i["id"] for i in v] for k, v in groups.items()},
                 "geometry_hash": file_hash(region_path(spec["snapshot"])), "time": config.get("time"), "datasets": requested}
    digest = hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()
    selection["id"] = digest
    target = Path(spec["staging_dir"]) / "selection.json"
    write_json(target, {"selection": selection, "groups": groups})
    # 源长度未知时保守预留，后续探测会严格检查预留上限。
    required_names = {"lwir11", "qa_pixel", "B02", "B03", "B04", "B08", "B11", "SCL", "data", "product-metadata", "product_metadata"}
    sizes = [int(a.get("file:size", 0) or 0) for items in groups.values() for i in items
             for name, a in i.get("assets", {}).items() if name in required_names]
    asset_bytes = sum(sizes)
    known = all(size > 0 for size in sizes) if sizes else False
    return {"selection": selection, "context": {"selection_path": str(target), "scale": {
        "scenes": sum(map(len, groups.values())), "asset_bytes": asset_bytes,
        "asset_size_known": known}}}


def acquire(spec, report):
    import pystac
    from core.skills.builtin import sentinel2_calibration as s2cal
    selected = json.loads(Path(spec["context"]["selection_path"]).read_text(encoding="utf-8"))
    skill, stage = _skill(spec["snapshot"]), Path(spec["staging_dir"])
    cfg = skill._load_dataspace_config()
    bands = {"landsat": ("lwir11", "qa_pixel"), "sentinel2": ("B02", "B03", "B04", "B08", "B11", "SCL"), "dem": ("data",)}
    local, assets, written = {}, [], 0
    for group, items in selected["groups"].items():
        local[group] = []
        for document in items:
            item = pystac.Item.from_dict(document)
            out = copy.deepcopy(document)
            out["assets"] = {}
            out["properties"]["gtai:local_assets"] = True
            signed = None
            for band in (*bands[group], *(("product-metadata",) if group == "sentinel2" else ())):
                asset = skill._find_ds_asset(item.assets, band)
                if band == "product-metadata":
                    asset = item.assets.get(band) or item.assets.get("product_metadata")
                if asset is None:
                    if band == "product-metadata":
                        continue
                    raise ValueError(f"必需资产缺失 {item.id}/{band}")
                url = skill._ds_https_url(asset.href)
                headers = None
                if "eodata.dataspace.copernicus.eu" in url:
                    if cfg.get("s3_key") and cfg.get("s3_secret"):
                        headers = skill._sign_s3_headers(cfg["s3_key"], cfg["s3_secret"], "GET", "eodata.dataspace.copernicus.eu", url.split("eodata.dataspace.copernicus.eu", 1)[1])
                    else:
                        headers = {"Authorization": "Bearer " + skill._get_dataspace_token(cfg)}
                else:
                    signed = signed or skill._sign_item(item)
                    if signed is None:
                        raise RuntimeError("资产签名失败")
                    sa = signed.assets.get(band) or signed.assets.get("product_metadata")
                    url = sa.href
                identity = {"collection": item.collection_id, "scene": item.id, "band": band,
                            "source": stable_url(asset.href), "version": item.properties.get("updated"),
                            "geometry": selected["selection"]["geometry_hash"]}
                key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                target = stage / "assets" / (key + (".xml" if band == "product-metadata" else ".tif"))
                # 重试复用此前已校验分片的位置；同一运行同资产只有一个 OS 锁写入者。
                cache_root = Path(spec.get("cache_root") or (Path(spec["staging_dir"]).parent.parent / "acquiring"))
                # 缓存按用户和项目隔离，文件锁与完成描述共同保证同一资产单写者。
                scope = f"{spec['node'].get('user_id','default')}_{spec['node'].get('project_id','default')}"
                cache = cache_root / scope / (key + target.suffix)
                expected_size = int(asset.extra_fields.get("file:size", 0) or 0)
                cache_limit = int(spec.get("cache_limit", 0) or 0)
                current_cache = sum(p.stat().st_size for p in cache_root.glob("**/*") if p.is_file()) if cache_root.exists() else 0
                if cache_limit and (expected_size > cache_limit or current_cache + expected_size > cache_limit):
                    cache = target
                from . import transfer
                transfer._disk_limit = max(0, spec["claim"]["disk"] - written)
                descriptor = fetch_file(url, cache, headers=headers, workers=spec["claim"]["connections"], progress=report, label=f"{item.id}/{band}")
                written += descriptor["size"]
                assets.append({**identity, **descriptor, "required": band != "product-metadata"})
                out["assets"][band] = {**asset.to_dict(), "href": descriptor["path"]}
            if group == "sentinel2":
                meta = out["assets"].get("product-metadata")
                calibration = None
                if meta:
                    parsed = s2cal.parse_boa_offsets_from_xml(Path(meta["href"]).read_text(encoding="utf-8"))
                    if parsed["quantification_value"] and parsed["offsets_by_band_id"]:
                        calibration = {"item_id": item.id, "processing_baseline": item.properties.get("s2:processing_baseline"), **parsed, "source": "per_scene_xml"}
                if calibration is None:
                    calibration = {"item_id": item.id, **s2cal.default_calibration_from_baseline(item.properties.get("s2:processing_baseline"))}
                out["properties"]["gtai:calibration"] = calibration
            local[group].append(out)
    target = stage / "assets.json"
    write_json(target, {"selection": selected["selection"], "groups": local, "assets": assets})
    return {"context": {"assets_path": str(target), "scale": {**spec["context"].get("scale", {}), "file_bytes": written}}}


def prepare(spec, report):
    import pystac
    skill = _skill(spec["snapshot"])
    config = values(spec["snapshot"])
    bundle = json.loads(Path(spec["context"]["assets_path"]).read_text(encoding="utf-8"))
    groups = {k: [pystac.Item.from_dict(i) for i in items] for k, items in bundle["groups"].items()}
    region = region_path(spec["snapshot"])
    bbox = skill._parse_region(region)
    target = Path(spec["staging_dir"]) / "raw"
    target.mkdir()
    pair = bundle["selection"].get("pair")
    monthly = bundle["selection"]["mode"] == "monthly"
    paths = {}
    if monthly:
        result = skill._download_monthly(groups.get("landsat", []), groups.get("sentinel2", []), output_dir=str(target), bbox=bbox,
                                          study_area_geojson=region, start_date=config["time"]["start"], end_date=config["time"]["end"],
                                          progress_callback=report, log_callback=report)
        if not result.get("ok"):
            raise ValueError(result.get("message", "月度本地准备失败"))
        paths.update(result["output_paths"])
    operations = [] if monthly else [
        ("landsat", "lwir11", "landsat_path", "landsat_lst", 30),
        ("landsat", "qa_pixel", "qa_path", "landsat_qa_pixel", 30),
        ("sentinel2", ["B02", "B03", "B04", "B08", "B11"], "sentinel2_path", "sentinel2_bands", 10),
        ("sentinel2", "SCL", "scl_path", "sentinel2_scl", 20)]
    operations.append(("dem", "data", "dem_path", "dem", 30))
    for group, bands, name, stem, resolution in operations:
        if group not in groups:
            continue
        date = (pair or {}).get("landsat_date" if group == "landsat" else "sentinel2_date", "").replace("-", "")
        path = target / (stem + ("_" + date if date and group != "dem" else "") + ".tif")
        provenance = skill._download_composite(groups[group], bands, str(path), bbox, resolution, report, report, (0, 1), "prepare_local",
                                                study_area_geojson=region, apply_s2_calibration=name == "sentinel2_path")
        paths[name] = str(path)
        if provenance:
            write_json(target / "sentinel2_provenance.json", {"scenes": provenance})
    return {"context": {"raw_paths": paths, "raw_dir": str(target), "input_type": "complete_lst" if len(paths) >= 5 else "download_subset"}}
