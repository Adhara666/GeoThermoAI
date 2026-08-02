"""
数据集划分模块

将大型CSV文件按指定比例随机划分为训练集、验证集和测试集。

采用单遍扫描 + 逐行随机分配策略，时间复杂度O(n)，空间复杂度O(1)，
适用于GB级别的大型CSV文件。
"""

import csv
import os
import random
from typing import Dict, Optional


def split_dataset(
    input_csv: str,
    output_dir: str,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    test_ratio: float = 0.2,
    seed: int = 42,
    progress_callback=None,
) -> Dict:
    """
    将大型CSV文件按指定比例随机划分为训练集、验证集和测试集。

    Args:
        input_csv:      输入CSV文件路径
        output_dir:     输出目录路径
        train_ratio:    训练集比例（默认0.6）
        val_ratio:      验证集比例（默认0.2）
        test_ratio:     测试集比例（默认0.2）
        seed:           随机种子（默认42，确保可复现）
        progress_callback: 进度回调函数 callback(step_name, percent, message)

    Returns:
        dict: 包含各数据集样本数量和占比的统计信息

    Raises:
        FileNotFoundError: 输入文件不存在
        ValueError:        比例参数不合法
    """
    # ── 参数校验 ──────────────────────────────────────────────────────
    ratios = {"train": train_ratio, "validate": val_ratio, "test": test_ratio}
    total_ratio = sum(ratios.values())
    if abs(total_ratio - 1.0) > 1e-9:
        raise ValueError(f"比例之和必须为1.0，当前为 {total_ratio}")

    if not os.path.isfile(input_csv):
        raise FileNotFoundError(f"输入文件不存在: {input_csv}")

    os.makedirs(output_dir, exist_ok=True)

    random.seed(seed)

    # ── 输出文件路径 ──────────────────────────────────────────────────
    train_path = os.path.join(output_dir, "train.csv")
    val_path = os.path.join(output_dir, "validate.csv")
    test_path = os.path.join(output_dir, "test.csv")

    output_paths = {
        "train": train_path,
        "validate": val_path,
        "test": test_path,
    }

    counters = {"train": 0, "validate": 0, "test": 0}
    total_lines = 0

    if progress_callback:
        progress_callback("split_dataset", 0, "开始数据划分...")

    try:
        with open(input_csv, "r", encoding="utf-8", newline="") as infile:
            reader = csv.reader(infile)

            # 读取表头
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("CSV文件为空，缺少表头")

            # 打开输出文件
            files = {}
            try:
                for key, path in output_paths.items():
                    f = open(path, "w", encoding="utf-8", newline="")
                    writer = csv.writer(f)
                    writer.writerow(header)
                    files[key] = {"file": f, "writer": writer}

                # 单遍扫描逐行分配
                for row in reader:
                    total_lines += 1

                    rand_val = random.random()
                    if rand_val < train_ratio:
                        files["train"]["writer"].writerow(row)
                        counters["train"] += 1
                    elif rand_val < train_ratio + val_ratio:
                        files["validate"]["writer"].writerow(row)
                        counters["validate"] += 1
                    else:
                        files["test"]["writer"].writerow(row)
                        counters["test"] += 1

                    # 每50万行报告一次进度
                    if total_lines % 500000 == 0 and progress_callback:
                        progress_callback(
                            "split_dataset",
                            min(total_lines / 10000000, 0.95),  # 进度估计
                            f"已处理 {total_lines:,} 行...",
                        )

            finally:
                for key, fobj in files.items():
                    fobj["file"].close()

    except PermissionError:
        raise PermissionError(f"无权限写入输出目录: {output_dir}")
    except UnicodeDecodeError:
        raise ValueError(f"文件编码错误，请检查输入文件编码")

    # ── 汇总统计 ──────────────────────────────────────────────────────
    actual_total = sum(counters.values())
    stats = {}
    for key in ["train", "validate", "test"]:
        count = counters[key]
        ratio = count / actual_total if actual_total > 0 else 0
        stats[key] = {"count": count, "ratio": ratio}

    if progress_callback:
        progress_callback(
            "split_dataset",
            1.0,
            f"划分完成: 训练集 {counters['train']:,}, "
            f"验证集 {counters['validate']:,}, "
            f"测试集 {counters['test']:,}",
        )

    return stats
