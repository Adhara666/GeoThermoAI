# -*- coding: utf-8 -*-
"""
进程内存归还工具（单一来源）

流程中的大量小分配（RF 树节点数组、pandas/pyarrow 分块缓冲）被 glibc malloc
的 arena 保留在进程地址空间：Python 侧 `del` + gc 后 RSS 不会回落（实测
150k×32KB 小分配释放后 RSS 保持 4.7GB 不降），但可继续复用。主动调
`malloc_trim(0)` 可把完全空闲的堆页归还系统（实测 4.7GB → 32MB），让内存
读数在步骤边界回落，降低长流程（数据预处理 → TTRI → RF 调优 → TCR）期间
的峰值 RSS，避免大研究区在 WSL2 配额下被 OOM 杀（exit 137）。

仅对 glibc（Linux 容器）生效，其他平台/加载失败静默跳过，不影响主流程。
"""

import ctypes
import gc


def release_rss_memory() -> None:
    """把进程空闲堆归还操作系统；任何失败都静默跳过。"""
    try:
        gc.collect()
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass
