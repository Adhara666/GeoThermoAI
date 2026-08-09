"""
数据集划分模块（B-01 重写 / 用户确认第8条）

删除像元级随机划分作为主路径：默认且唯一对外暴露的入口改为
**空间块 + guard buffer**（train/test 之间设缓冲带，缓冲依据写入固定
``split_info.json``）。原随机划分实现保留为内部函数
``_split_dataset_pixel_random_legacy``，仅供脚本/调试直接调用，不通过
Skill 参数或前端页面暴露（页面不再提供随机 split 选项）。

空间块划分为单遍流式扫描（O(1) 额外内存 + O(唯一 block 数) 的小型哈希缓存），
block→train/validate/test 的分配由 ``sha256(seed:block_row:block_col)`` 派生的
稳定哈希决定（不依赖 Python 全局 ``random.seed``，避免并发任务互相影响）。
guard buffer 通过检查像元在其 block 内是否邻近"分配到不同数据集的相邻 block"
来判定，纯局部计算，天然可流式处理。
"""

import csv
import hashlib
import math
import os
import random
from typing import Dict, Optional, Tuple

from .atomic_io import atomic_write_json

# 默认值均可通过参数覆盖，并非"先验硬写不可更改"；实际取值与依据写入 split_info.json
# 实现期修订 v1.3：块边长 10→30（实测鄂州 2024-07 数据：100m 缓冲下排除率 73.6%→27.3%，
# 进入样本 ×2.75，实际比例 83/8/8→67/16/17，更接近配置 60/20/20；100m 缓冲依据不变）
DEFAULT_BLOCK_SIZE_PX = 30
# ≈ Landsat Collection 2 ST_B10 (TIRS) 原生约100m热像元支持尺度（USGS Landsat 8/9
# 波段说明），作为默认缓冲带宽度的物理依据，而不是随意拍脑袋的数字；可用 guard_buffer_m 覆盖。
DEFAULT_GUARD_BUFFER_M = 100.0
DEFAULT_PIXEL_SIZE_M = 30.0
DEFAULT_MIN_SAMPLES_PER_SPLIT = 10

SPLIT_INFO_FILENAME = "split_info.json"


def _block_assignment_cache_factory(seed: int, train_ratio: float, val_ratio: float):
    """返回 (assign, cache)：assign(block_row, block_col) -> 'train'/'validate'/'test'，
    结果只由 (seed, block坐标) 决定，纯函数、无需预先枚举全部 block。"""
    cache: Dict[Tuple[int, int], str] = {}

    def assign(block_row: int, block_col: int) -> str:
        key = (block_row, block_col)
        cached = cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256(f"{seed}:{block_row}:{block_col}".encode("utf-8")).digest()
        u = int.from_bytes(digest[:8], "big") / 2 ** 64
        if u < train_ratio:
            label = "train"
        elif u < train_ratio + val_ratio:
            label = "validate"
        else:
            label = "test"
        cache[key] = label
        return label

    return assign, cache


def split_dataset(
    input_csv: str,
    output_dir: str,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    test_ratio: float = 0.2,
    seed: int = 42,
    block_size_px: int = DEFAULT_BLOCK_SIZE_PX,
    guard_buffer_m: float = DEFAULT_GUARD_BUFFER_M,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    min_samples_per_split: int = DEFAULT_MIN_SAMPLES_PER_SPLIT,
    progress_callback=None,
) -> Dict:
    """空间块 + guard buffer 划分（对外唯一入口；固定输出文件名 train.csv/validate.csv/test.csv 不变）。

    Args:
        input_csv:            输入CSV文件路径（须含 row, col 列）
        output_dir:           输出目录路径
        train_ratio/val_ratio/test_ratio: 划分比例，须为 [0,1] 内有限数且和为1
        seed:                 随机种子（仅用于派生稳定哈希，不修改任何全局状态）
        block_size_px:        空间块边长（像元数）
        guard_buffer_m:       train/test 缓冲带宽度（米）；默认值见 DEFAULT_GUARD_BUFFER_M
                              的物理依据说明，可自由覆盖
        pixel_size_m:         输入CSV的像元分辨率（米），用于把 guard_buffer_m 换算为像元数
        min_samples_per_split: 三个数据集的最小样本数下限，划分后不达标则拒绝产出

    Returns:
        dict: {train:{count,ratio}, validate:{...}, test:{...}, split_info:{...}}

    Raises:
        FileNotFoundError / ValueError: 输入非法或划分后样本数不足
    """
    for name, val in (("train_ratio", train_ratio), ("val_ratio", val_ratio), ("test_ratio", test_ratio)):
        if not math.isfinite(val) or not (0.0 <= val <= 1.0):
            raise ValueError(f"{name}={val} 不是 [0,1] 内的有限数值")
    total_ratio = train_ratio + val_ratio + test_ratio
    if abs(total_ratio - 1.0) > 1e-9:
        raise ValueError(f"比例之和必须为1.0，当前为 {total_ratio}")
    if not os.path.isfile(input_csv):
        raise FileNotFoundError(f"输入文件不存在: {input_csv}")
    if block_size_px <= 0:
        raise ValueError(f"block_size_px 必须为正整数，当前为 {block_size_px}")
    if guard_buffer_m < 0:
        raise ValueError(f"guard_buffer_m 不能为负数，当前为 {guard_buffer_m}")

    os.makedirs(output_dir, exist_ok=True)
    guard_buffer_px = max(0, int(math.ceil(guard_buffer_m / pixel_size_m))) if pixel_size_m > 0 else 0
    if guard_buffer_px * 2 >= block_size_px:
        raise ValueError(
            f"guard_buffer_px({guard_buffer_px}) 相对 block_size_px({block_size_px}) 过大，"
            f"会吞掉整个 block；请减小 guard_buffer_m 或增大 block_size_px"
        )

    assign, _cache = _block_assignment_cache_factory(seed, train_ratio, val_ratio)

    def resolve_label(row: int, col: int) -> Optional[str]:
        block_row, block_col = row // block_size_px, col // block_size_px
        local_row, local_col = row % block_size_px, col % block_size_px
        own = assign(block_row, block_col)

        near_top = local_row < guard_buffer_px
        near_bottom = local_row >= block_size_px - guard_buffer_px
        near_left = local_col < guard_buffer_px
        near_right = local_col >= block_size_px - guard_buffer_px

        offsets = []
        if near_top:
            offsets.append((-1, 0))
        if near_bottom:
            offsets.append((1, 0))
        if near_left:
            offsets.append((0, -1))
        if near_right:
            offsets.append((0, 1))
        if near_top and near_left:
            offsets.append((-1, -1))
        if near_top and near_right:
            offsets.append((-1, 1))
        if near_bottom and near_left:
            offsets.append((1, -1))
        if near_bottom and near_right:
            offsets.append((1, 1))

        for dr, dc in offsets:
            if assign(block_row + dr, block_col + dc) != own:
                return None  # 落在缓冲带内：train/test 之间的样本都不使用
        return own

    output_paths = {
        "train": os.path.join(output_dir, "train.csv"),
        "validate": os.path.join(output_dir, "validate.csv"),
        "test": os.path.join(output_dir, "test.csv"),
    }
    counters = {"train": 0, "validate": 0, "test": 0, "buffer_excluded": 0}
    total_lines = 0
    row_bounds = {"min_row": None, "max_row": None, "min_col": None, "max_col": None}

    if progress_callback:
        progress_callback("split_dataset", 0, "开始空间块 + guard buffer 划分...")

    partial_paths = {k: p + ".partial" for k, p in output_paths.items()}
    try:
        with open(input_csv, "r", encoding="utf-8", newline="") as infile:
            reader = csv.reader(infile)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("CSV文件为空，缺少表头")
            try:
                row_pos = header.index("row")
                col_pos = header.index("col")
            except ValueError:
                raise ValueError("输入 CSV 缺少 row/col 列，无法进行空间块划分")

            files = {}
            try:
                for key, path in partial_paths.items():
                    f = open(path, "w", encoding="utf-8", newline="")
                    writer = csv.writer(f)
                    writer.writerow(header)
                    files[key] = {"file": f, "writer": writer}

                for row_vals in reader:
                    total_lines += 1
                    r = int(row_vals[row_pos])
                    c = int(row_vals[col_pos])
                    if row_bounds["min_row"] is None or r < row_bounds["min_row"]:
                        row_bounds["min_row"] = r
                    if row_bounds["max_row"] is None or r > row_bounds["max_row"]:
                        row_bounds["max_row"] = r
                    if row_bounds["min_col"] is None or c < row_bounds["min_col"]:
                        row_bounds["min_col"] = c
                    if row_bounds["max_col"] is None or c > row_bounds["max_col"]:
                        row_bounds["max_col"] = c

                    label = resolve_label(r, c)
                    if label is None:
                        counters["buffer_excluded"] += 1
                        continue
                    files[label]["writer"].writerow(row_vals)
                    counters[label] += 1

                    if total_lines % 500000 == 0 and progress_callback:
                        progress_callback(
                            "split_dataset", min(total_lines / 10000000, 0.95),
                            f"已处理 {total_lines:,} 行...",
                        )
            finally:
                for fobj in files.values():
                    fobj["file"].close()
    except PermissionError:
        for p in partial_paths.values():
            if os.path.exists(p):
                os.remove(p)
        raise PermissionError(f"无权限写入输出目录: {output_dir}")
    except UnicodeDecodeError:
        for p in partial_paths.values():
            if os.path.exists(p):
                os.remove(p)
        raise ValueError("文件编码错误，请检查输入文件编码")
    except Exception:
        for p in partial_paths.values():
            if os.path.exists(p):
                os.remove(p)
        raise

    for key in ("train", "validate", "test"):
        if counters[key] < min_samples_per_split:
            for p in partial_paths.values():
                if os.path.exists(p):
                    os.remove(p)
            raise ValueError(
                f"{key} 集划分后仅 {counters[key]} 行（< 最小要求 {min_samples_per_split}），"
                f"guard buffer 或 block_size 设置可能过大，已拒绝产出不可用的划分结果"
            )

    for key, path in output_paths.items():
        os.replace(partial_paths[key], path)

    actual_total = sum(counters[k] for k in ("train", "validate", "test"))
    stats = {}
    for key in ("train", "validate", "test"):
        count = counters[key]
        ratio = count / actual_total if actual_total > 0 else 0
        stats[key] = {"count": count, "ratio": ratio}

    split_info = {
        "schema_version": 1,
        "method": "spatial_block_guard_buffer",
        "seed": seed,
        "block_size_px": block_size_px,
        "guard_buffer_m": guard_buffer_m,
        "guard_buffer_px": guard_buffer_px,
        "pixel_size_m": pixel_size_m,
        "guard_buffer_justification": (
            f"默认 {DEFAULT_GUARD_BUFFER_M:g}m 取自 Landsat Collection 2 ST_B10 (TIRS) 原生"
            f"约100m热像元支持尺度（USGS Landsat 8/9 波段说明），非任意拍脑袋数值；"
            f"可通过 guard_buffer_m 参数覆盖，本次实际取值见本文件顶层字段"
        ),
        "counts": counters,
        "ratios": {k: stats[k]["ratio"] for k in stats},
        "row_col_bounds": row_bounds,
        "total_input_rows": total_lines,
    }
    atomic_write_json(os.path.join(output_dir, SPLIT_INFO_FILENAME), split_info)

    if progress_callback:
        progress_callback(
            "split_dataset", 1.0,
            f"空间块划分完成: 训练集 {counters['train']:,}, 验证集 {counters['validate']:,}, "
            f"测试集 {counters['test']:,}, 缓冲带排除 {counters['buffer_excluded']:,}",
        )

    return {**stats, "split_info": split_info}


def _split_dataset_pixel_random_legacy(
    input_csv: str,
    output_dir: str,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    test_ratio: float = 0.2,
    seed: int = 42,
    progress_callback=None,
) -> Dict:
    """保留原有像元级随机划分实现，仅供内部调试/脚本直接调用，不通过 Skill 参数或
    前端暴露（B-01 / 用户确认第8条："页面不再提供随机 split 选项"）。

    与旧实现的唯一差异：改用局部 ``random.Random(seed)`` 实例而非全局
    ``random.seed()``，避免并发任务之间通过进程级随机状态互相影响。
    """
    ratios = {"train": train_ratio, "validate": val_ratio, "test": test_ratio}
    total_ratio = sum(ratios.values())
    if abs(total_ratio - 1.0) > 1e-9:
        raise ValueError(f"比例之和必须为1.0，当前为 {total_ratio}")
    if not os.path.isfile(input_csv):
        raise FileNotFoundError(f"输入文件不存在: {input_csv}")

    os.makedirs(output_dir, exist_ok=True)
    rng = random.Random(seed)

    output_paths = {
        "train": os.path.join(output_dir, "train.csv"),
        "validate": os.path.join(output_dir, "validate.csv"),
        "test": os.path.join(output_dir, "test.csv"),
    }
    counters = {"train": 0, "validate": 0, "test": 0}
    total_lines = 0

    if progress_callback:
        progress_callback("split_dataset", 0, "开始数据划分（快速随机，仅调试用途）...")

    with open(input_csv, "r", encoding="utf-8", newline="") as infile:
        reader = csv.reader(infile)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError("CSV文件为空，缺少表头")

        files = {}
        try:
            for key, path in output_paths.items():
                f = open(path, "w", encoding="utf-8", newline="")
                writer = csv.writer(f)
                writer.writerow(header)
                files[key] = {"file": f, "writer": writer}

            for row in reader:
                total_lines += 1
                rand_val = rng.random()
                if rand_val < train_ratio:
                    files["train"]["writer"].writerow(row)
                    counters["train"] += 1
                elif rand_val < train_ratio + val_ratio:
                    files["validate"]["writer"].writerow(row)
                    counters["validate"] += 1
                else:
                    files["test"]["writer"].writerow(row)
                    counters["test"] += 1
        finally:
            for fobj in files.values():
                fobj["file"].close()

    actual_total = sum(counters.values())
    stats = {}
    for key in ["train", "validate", "test"]:
        count = counters[key]
        ratio = count / actual_total if actual_total > 0 else 0
        stats[key] = {"count": count, "ratio": ratio}

    if progress_callback:
        progress_callback(
            "split_dataset", 1.0,
            f"划分完成: 训练集 {counters['train']:,}, "
            f"验证集 {counters['validate']:,}, "
            f"测试集 {counters['test']:,}",
        )

    return stats
