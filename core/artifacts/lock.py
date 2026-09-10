# -*- coding: utf-8 -*-
"""产物与恢复（升级第五阶段）— 应用实例锁。

依据总体技术方案 §11.1：运行服务必须先取得数据根上的应用实例锁，
并检查数据库结构版本。锁用操作系统文件锁或同等互斥机制，单纯判断
锁文件存在不够；重复启动一个写入相同任务库的调度器应明确失败。

跨平台：Linux/容器用 fcntl.flock；Windows 用 msvcrt.locking。
锁句柄由调用进程持有，进程退出即释放（不存在"锁文件存在"的误判）。
"""

import os
from pathlib import Path
from typing import Optional


class InstanceLock:
    """数据根上的排他实例锁；acquire 失败说明已有服务在写同一任务库。"""

    def __init__(self, data_root: Path):
        self.path = Path(data_root) / ".gtai_instance.lock"
        self._fh: Optional[Any] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except (OSError, BlockingIOError):
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()}\n".encode("utf-8"))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise RuntimeError(
                f"已有服务实例持有数据根锁：{self.path}（重复启动被拒绝）"
            )
        return self

    def __exit__(self, *exc) -> None:
        self.release()
