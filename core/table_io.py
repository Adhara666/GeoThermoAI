# -*- coding: utf-8 -*-
"""
表格文件统一读写辅助（升级：中间产物 CSV → Parquet）

项目中间产物表格统一改为 Parquet（列式 + zstd 压缩，磁盘占用约为 CSV 的
1/5~1/10，读取免文本解析更快）。本模块提供与旧 CSV 语义一致的分块读 /
追加写封装，保证内存占用可控：

- 分块读 ``iter_chunks``：pyarrow.ParquetFile.iter_batches 逐批转 pandas，
  等价旧 ``pd.read_csv(chunksize=...)``，只解压当前批，不整表进内存；
- 追加写 ``TableWriter``：ParquetWriter 支持多次 write_table（每批一个
  row group），等价旧 ``to_csv(mode="a")`` 的分块追加，不用攒全量内存；
- 全量读 ``read_table``：等价旧 ``read_csv(usecols=...)``；
- ``empty_table`` / ``sample_rows``：替代 ``read_csv(nrows=0)`` / ``nrows=5``
  的校验与空表生成。

约定：本项目所有中间产物表均含显式 row/col 列；Parquet 原生支持 null
（读回为 NaN），不再需要旧 CSV 的 ``na_rep=""`` 占位符。
"""

import os
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

# 分块读/写默认批次行数（与各模块旧 CSV chunksize 口径保持一致）
DEFAULT_CHUNK_SIZE = 500000


def read_table(path, columns=None, chunksize=None):
    """读表：columns 列裁剪（等价 read_csv usecols）；chunksize 给定时返回
    分块迭代器（等价 read_csv chunksize 语义）。"""
    if chunksize:
        return iter_chunks(path, columns=columns, batch_size=chunksize)
    return pd.read_parquet(path, columns=columns)


def iter_chunks(path, columns=None, batch_size=DEFAULT_CHUNK_SIZE):
    """分块读取为 DataFrame 迭代器（等价 pd.read_csv(..., chunksize=N)）。

    只解压当前批数据，内存占用与 batch_size 成正比，不整表进内存。
    """
    pf = pq.ParquetFile(path)
    cols = list(columns) if columns is not None else None
    for batch in pf.iter_batches(batch_size=batch_size, columns=cols):
        yield batch.to_pandas()


def read_row_count(path, column=None):
    """快速统计行数（只读单列，避免全量解压）。"""
    pf = pq.ParquetFile(path)
    col = column or pf.schema_arrow.names[0]
    total = 0
    for batch in pf.iter_batches(batch_size=DEFAULT_CHUNK_SIZE, columns=[col]):
        total += batch.num_rows
    return total


def sample_rows(path, n=5, columns=None):
    """读前 n 行用于校验（等价 read_csv(nrows=n)）。"""
    pf = pq.ParquetFile(path)
    if pf.metadata.num_row_groups == 0:
        return empty_table(path, columns)
    batch = pf.read_row_group(0, columns=columns)
    return batch.slice(0, n).to_pandas()


def empty_table(path, columns=None):
    """按文件 schema 返回空表（等价 read_csv(nrows=0)；含完整列）。"""
    pf = pq.ParquetFile(path)
    cols = list(columns) if columns is not None else pf.schema_arrow.names
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})


class TableWriter:
    """Parquet 分块追加写（等价 to_csv(mode="a") 语义，无 CSV 追加兼容问题）。

    首次构造即创建文件并固定 schema，后续每次 write() 追加一个 row group；
    同一文件所有批次必须列一致。close() 前务必保证文件已写完。
    """

    def __init__(self, path, schema=None, compression="zstd"):
        self.path = path
        self._writer = None
        self._schema = schema
        self._compression = compression
        self.wrote_rows = 0

    def _ensure_open(self, first_table: pa.Table):
        if self._writer is None:
            schema = self._schema if self._schema is not None else first_table.schema
            self._writer = pq.ParquetWriter(self.path, schema, compression=self._compression)
        return self._writer

    def write(self, df):
        """写入/追加一个 DataFrame（列顺序与 schema 需一致）。"""
        table = pa.Table.from_pandas(df, preserve_index=False)
        writer = self._ensure_open(table)
        table = table.cast(writer.schema)
        writer.write_table(table)
        self.wrote_rows += len(df)

    def close(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def write_empty(path, schema):
    """写出带 schema 的空 Parquet 文件（等价只有表头的空 CSV）。"""
    empty = pa.Table.from_batches([], schema=schema)
    pq.write_table(empty, path, compression="zstd")


def copy_table(src, dst):
    """复制 Parquet 文件（等价旧 CSV 的整文件复制）。

    直接做文件级复制，避免 read_table + write_table 把整表读入内存
    （10m 全格网 Parquet 数亿行时会产生 GB 级内存峰值）。
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(src, dst)
