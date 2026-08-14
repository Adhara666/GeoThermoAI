"""
LST最终计算模块（轻包装）

LST_final 已在 TCR 计算阶段完成（LST_final = LST_pred + TCR），
本模块仅做路径确认。
"""

import os
from typing import Dict

from .table_io import copy_table, iter_chunks


def compute_lst_final(
    input_csv: str,
    output_path: str,
    chunk_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """
    LST_final 已在 TCR 计算阶段完成，此函数仅做文件验证。

    Args:
        input_csv:    TCR阶段输出的Parquet路径（已含LST_final列）
        output_path:  输出Parquet路径（直接复制input_csv或验证其存在）
        chunk_size:   兼容参数
        progress_callback: 进度回调

    Returns:
        dict: 包含输出路径和统计信息
    """
    if progress_callback:
        progress_callback("lst_final", 0, "验证 LST_final 数据...")

    if not os.path.isfile(input_csv):
        raise FileNotFoundError(f"TCR输出文件不存在: {input_csv}")

    # 快速统计行数（只读单列，避免全量解压）
    total_rows = 0
    total_valid = 0
    for chunk in iter_chunks(input_csv, columns=["LST_final"], batch_size=chunk_size):
        total_rows += len(chunk)
        total_valid += chunk["LST_final"].notna().sum()

    # 如果输出路径与输入不同，复制文件（Parquet 整文件复制）
    if output_path != input_csv:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        copy_table(input_csv, output_path)

    if progress_callback:
        progress_callback("lst_final", 1.0,
            f"LST_final 验证完成: 共 {total_rows:,} 行, 有效 {total_valid:,} 行")

    return {
        "output_path": output_path if output_path != input_csv else input_csv,
        # 显式转 Python 原生 int：pandas .sum() 返回 numpy.int64，不做转换会在下游
        # json.dumps()（如 run_manifest.json 写入）时抛 TypeError（本轮联调发现）。
        "total_rows": int(total_rows),
        "total_valid": int(total_valid),
    }
