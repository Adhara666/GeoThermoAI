"""
项目状态扫描（轻量版）

把当前对话工作区里已有的三层状态——已下载影像、已训练模型、已生成产物——
拼成一段简短的自然语言摘要，注入各执行 Agent 的 LLM prompt，让它们在做局部
决策时能看到项目全局（复用已有、提示重复、对比历史），而不是每次都当"新手"。

只读扫描：任何异常（路径不存在、IO 错误）一律返回空串，绝不影响主流程。

目录结构约定（见 executor.pair_dirs / rf_model）：
- 影像对：{project_dir}/pairs/L{landsat_date}_S{sentinel2_date}/
- 模型：  {project_dir}/results/train/rf_ttri_model_run{id:03d}.pkl
- 产物：  {project_dir}/pairs/*/results/rf_10m_lst_final_{date}.tif
"""

import pathlib
import re


def scan_project_state(project_dir: str, max_items: int = 3) -> str:
    """扫描 {project_dir} 下已有的影像/模型/产物，返回中文摘要；无产物返回空串。

    max_items：每层最多列出的数量（超出显示总数即可，避免 prompt 过长）。
    """
    base = pathlib.Path(project_dir) if project_dir else None
    if base is None or not base.is_dir():
        return ""
    try:
        parts = []
        pairs = _scan_pairs(base)
        if pairs:
            parts.append(f"已下载 {len(pairs)} 个影像对（最近：{_pair_label(pairs[0])}）")
        models = _scan_models(base)
        if models:
            parts.append(f"已训练模型 {len(models)} 份（最新：{models[0].name}）")
        products = _scan_products(base)
        if products:
            dates = "、".join(_product_date(p.name) for p in products[:max_items])
            parts.append(f"已生成 {len(products)} 份 10m 地表温度产品"
                         f"（最近影像日期：{dates}）")
        if not parts:
            return ""
        return ("【当前项目已有】" + "；".join(parts)
                + "。若新任务与已有影像/模型/产品的时间或区域重叠，"
                  "优先考虑复用而非重复生成。")
    except OSError:
        return ""


def derive_project_dir(*dirs: str) -> str:
    """从工作目录（如 raw_dir / processed_dir）反推项目根目录。

    目录形如 {project}/pairs/L*_S*/raw 或 {project}/raw 或 {project}/processed，
    取含 "pairs" 段之前的路径；无 "pairs" 段时取父目录。返回空串表示推不出。
    """
    for d in dirs:
        if not d:
            continue
        p = pathlib.Path(d)
        try:
            i = p.parts.index("pairs")
        except ValueError:
            i = -1
        if i > 0:
            return str(pathlib.Path(*p.parts[:i]))
        if i == -1 and p.parent and str(p.parent) != str(p):
            return str(p.parent)
    return ""


def _scan_pairs(base: pathlib.Path) -> list:
    """按 mtime 从新到旧列出影像对目录。"""
    try:
        return sorted(base.glob("pairs/L*_S*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _scan_models(base: pathlib.Path) -> list:
    """最新模型 pkl（优先规范位置 results/train/，失败回退递归扫描）。"""
    try:
        files = sorted(base.glob("results/train/rf_ttri_model_run*.pkl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    if not files:
        try:
            files = sorted(base.rglob("rf_ttri_model_run*.pkl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
    return files


def _scan_products(base: pathlib.Path) -> list:
    """最新 10m LST 产物（排除 _filled 填洞产物与 _cloud_mask 掩膜）。"""
    try:
        files = sorted(base.glob("pairs/*/results/rf_10m_lst_final_*.tif"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    if not files:
        try:
            files = sorted(base.rglob("rf_10m_lst_final_*.tif"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
    return [p for p in files
            if "_filled" not in p.name and "_cloud_mask" not in p.name]


def _pair_label(pair_dir: pathlib.Path) -> str:
    """把影像对目录名 L20240721_S20240722 显示为「2024-07-21 / 2024-07-22」。"""
    m = re.match(r"L(\d{8})_S(\d{8})", pair_dir.name)
    if m:
        return f"{_fmt(m.group(1))} / {_fmt(m.group(2))}"
    return pair_dir.name


def _product_date(name: str) -> str:
    m = re.search(r"_final_(\d{8})\.tif$", name)
    return _fmt(m.group(1)) if m else name


def _fmt(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
