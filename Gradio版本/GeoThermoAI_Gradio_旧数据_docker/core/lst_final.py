"""
LST最终计算模块（轻包装）

LST_final 已在 TCR 计算阶段完成（LST_final = LST_pred + TCR），
本模块仅做路径确认。
"""

import os
from typing import Dict

import pandas as pd


def compute_lst_final(
    input_csv: str,
    output_path: str,
    chunk_size: int = 500000,
    progress_callback=None,
) -> Dict:
    """
    LST_final 已在 TCR 计算阶段完成，此函数仅做文件验证。

    Args:
        input_csv:    TCR阶段输出的CSV路径（已含LST_final列）
        output_path:  输出CSV路径（直接复制input_csv或验证其存在）
        chunk_size:   兼容参数
        progress_callback: 进度回调

    Returns:
        dict: 包含输出路径和统计信息
    """
    if progress_callback:
        progress_callback("lst_final", 0, "验证 LST_final 数据...")

    if not os.path.isfile(input_csv):
        raise FileNotFoundError(f"TCR输出文件不存在: {input_csv}")

    # 快速统计行数
    total_rows = 0
    total_valid = 0
    for chunk in pd.read_csv(input_csv, chunksize=chunk_size, usecols=["LST_final"]):
        total_rows += len(chunk)
        total_valid += chunk["LST_final"].notna().sum()

    # 如果输出路径与输入不同，复制文件
    if output_path != input_csv:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        pd.read_csv(input_csv, chunksize=chunk_size)
        first = True
        for chunk in pd.read_csv(input_csv, chunksize=chunk_size):
            chunk.to_csv(output_path, mode="w" if first else "a",
                         header=first, index=False, encoding="utf-8-sig")
            first = False

    if progress_callback:
        progress_callback("lst_final", 1.0,
            f"LST_final 验证完成: 共 {total_rows:,} 行, 有效 {total_valid:,} 行")

    return {
        "output_path": output_path if output_path != input_csv else input_csv,
        "total_rows": total_rows,
        "total_valid": total_valid,
    }
