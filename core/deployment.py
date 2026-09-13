# -*- coding: utf-8 -*-
"""Deployment paths and bounded runtime settings.

The application used to treat ``WORKSPACE_ROOT`` as both the project disk and
the home of accounts/state.  Phase 7 gives small durable metadata a dedicated
data root while retaining a separately configurable large project root.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "deployment.json"
NETWORK_FS_TYPES = {
    "9p", "afs", "cifs", "davfs", "fuse.sshfs", "gcsfuse", "glusterfs",
    "lustre", "nfs", "nfs4", "smb3", "sshfs",
}


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"部署配置无法读取：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"部署配置必须是 JSON 对象：{path}")
    return value


def _number(value: Any, name: str, *, integer: bool = False,
            minimum: float = 0, allow_none: bool = False) -> Optional[float]:
    if value in (None, "") and allow_none:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是有限数值") from None
    if not math.isfinite(number) or number <= minimum:
        raise ValueError(f"{name} 必须大于 {minimum}")
    if integer and number != int(number):
        raise ValueError(f"{name} 必须是整数")
    return int(number) if integer else number


def _configured(config: dict, section: str, key: str, default: Any) -> Any:
    values = config.get(section, {})
    return values.get(key, default) if isinstance(values, dict) else default


def _path_value(env: Mapping[str, str], env_name: str, configured: Any,
                default: Path) -> Path:
    raw = str(env.get(env_name, "") or configured or "").strip()
    return Path(raw).expanduser() if raw else default


def _mount_fs_type(path: Path) -> str:
    """Best-effort Linux mount type lookup for the SQLite local-disk guard."""
    if os.name == "nt":
        if str(path).startswith(("\\\\", "//")):
            return "unc"
        try:
            import ctypes
            anchor = str(path.resolve().anchor or path.resolve())
            # Win32 DRIVE_REMOTE = 4. Mapped drive letters must be rejected just
            # like UNC paths because SQLite rollback locks require local semantics.
            if int(ctypes.windll.kernel32.GetDriveTypeW(anchor)) == 4:
                return "windows_remote"
        except Exception:
            pass
        return ""
    mounts = Path("/proc/mounts")
    if not mounts.is_file():
        return ""
    resolved = str(path.resolve())
    best = ("", "")
    try:
        for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mountpoint = parts[1].replace("\\040", " ")
            if (resolved == mountpoint or resolved.startswith(mountpoint.rstrip("/") + "/")) \
                    and len(mountpoint) > len(best[0]):
                best = (mountpoint, parts[2].lower())
    except OSError:
        return ""
    return best[1]


def _assert_writable_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".gtai-write-probe-", dir=str(path))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise RuntimeError(f"{label}不可写：{path}：{exc}") from exc


def _copy_tree_missing(source: Path, target: Path) -> int:
    """Copy legacy metadata without replacing anything already in the new root."""
    copied = 0
    if not source.is_dir():
        return copied
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        # Project rasters can be very large and their recorded absolute paths remain
        # readable.  Only account/control metadata is adopted automatically.
        if "workspace" in rel.parts:
            continue
        destination = target / rel
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            copied += 1
    return copied


@dataclass(frozen=True)
class DeploymentConfig:
    project_root: Path
    data_root: Path
    workspace_root: Path
    temp_root: Path
    state_db: Path
    user_cache_capacity: int
    user_cache_ttl_seconds: float
    search_cache_entries: int
    search_cache_ttl_seconds: float
    memory_writeback_attempts: int
    memory_writeback_base_seconds: float
    memory_writeback_poll_seconds: float
    budget_values: dict
    source_path: Path = DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, *, project_root: Path = PROJECT_ROOT,
             env: Optional[Mapping[str, str]] = None,
             config_path: Optional[Path] = None) -> "DeploymentConfig":
        env = os.environ if env is None else env
        path = Path(config_path or env.get("GTAI_DEPLOYMENT_CONFIG", "")
                    or (Path(project_root) / "config" / "deployment.json"))
        config = _load_json(path)

        default_data = Path(project_root) / "data"
        data_root = _path_value(env, "GTAI_DATA_ROOT",
                                _configured(config, "paths", "data_root", ""),
                                default_data)
        workspace_root = _path_value(
            env, "WORKSPACE_ROOT", _configured(config, "paths", "workspace_root", ""),
            data_root / "users")
        temp_root = _path_value(env, "GTAI_TEMP_ROOT",
                                _configured(config, "paths", "temp_root", ""),
                                data_root / "tmp")
        state_db = _path_value(env, "GTAI_STATE_DB",
                               _configured(config, "paths", "state_db", ""),
                               data_root / "state_kernel" / "ledger.sqlite3")

        def val(section, key, env_name, default, *, integer=False, minimum=0):
            raw = env.get(env_name, _configured(config, section, key, default))
            return _number(raw, env_name, integer=integer, minimum=minimum)

        memory = {
            "user_cache_capacity": val("memory", "user_cache_capacity",
                                       "GTAI_USER_CACHE_CAPACITY", 8, integer=True),
            "user_cache_ttl_seconds": val("memory", "user_cache_ttl_seconds",
                                          "GTAI_USER_CACHE_TTL_SECONDS", 1800),
            "memory_writeback_attempts": val("memory", "writeback_attempts",
                                             "GTAI_MEMORY_WRITEBACK_ATTEMPTS", 4,
                                             integer=True),
            "memory_writeback_base_seconds": val("memory", "writeback_base_seconds",
                                                 "GTAI_MEMORY_WRITEBACK_BASE_SECONDS", 2),
            "memory_writeback_poll_seconds": val("memory", "writeback_poll_seconds",
                                                 "GTAI_MEMORY_WRITEBACK_POLL_SECONDS", 1),
        }
        cache = {
            "search_cache_entries": val("cache", "search_entries",
                                        "GTAI_SEARCH_CACHE_ENTRIES", 64, integer=True),
            "search_cache_ttl_seconds": val("cache", "search_ttl_seconds",
                                            "GTAI_SEARCH_CACHE_TTL_SECONDS", 600),
        }

        budget_specs = {
            "GTAI_COMPUTE_JOBS": ("compute_jobs", 1, True),
            "GTAI_DOWNLOAD_JOBS": ("download_jobs", 1, True),
            "GTAI_DOWNLOAD_CONNECTIONS": ("download_connections", 8, True),
            "GTAI_PREFETCH_PACKAGES": ("prefetch_packages", 1, True),
            "GTAI_CATALOG_JOBS": ("catalog_jobs", 2, True),
            "GTAI_MODEL_JOBS": ("model_jobs", 2, True),
            "GTAI_DISK_MARGIN_GIB": ("disk_margin_gib", None, False),
            "GTAI_DISK_CACHE_GIB": ("disk_cache_gib", 20, False),
            "GTAI_NODE_TIMEOUT_SECONDS": ("node_timeout_seconds", 7200, False),
            "GTAI_NETWORK_ATTEMPTS": ("network_attempts", 3, True),
            "GTAI_CPU_BUDGET": ("cpu_budget", None, False),
            "GTAI_MEMORY_GIB": ("memory_gib", None, False),
        }
        budget_values = {}
        for env_name, (key, default, integer) in budget_specs.items():
            raw = env.get(env_name, _configured(config, "budget", key, default))
            if raw in (None, ""):
                continue
            budget_values[env_name] = _number(raw, env_name, integer=integer)

        if budget_values.get("GTAI_DOWNLOAD_CONNECTIONS", 8) \
                < budget_values.get("GTAI_DOWNLOAD_JOBS", 1):
            raise ValueError("GTAI_DOWNLOAD_CONNECTIONS 不能小于 GTAI_DOWNLOAD_JOBS")

        return cls(project_root=Path(project_root).resolve(),
                   data_root=data_root.resolve(), workspace_root=workspace_root.resolve(),
                   temp_root=temp_root.resolve(), state_db=state_db.resolve(),
                   budget_values=budget_values, source_path=path,
                   **memory, **cache)

    @property
    def users_root(self) -> Path:
        return self.data_root / "users"

    def prepare(self, *, adopt_legacy: bool = True) -> dict:
        """Validate durable roots before the scheduler is allowed to start."""
        _assert_writable_directory(self.data_root, "应用持久数据根")
        _assert_writable_directory(self.users_root, "账号数据目录")
        _assert_writable_directory(self.workspace_root, "项目数据根")
        _assert_writable_directory(self.temp_root, "暂存根")
        _assert_writable_directory(self.state_db.parent, "SQLite 数据库目录")
        fs_type = _mount_fs_type(self.state_db.parent)
        if fs_type in NETWORK_FS_TYPES or fs_type in ("unc", "windows_remote"):
            raise RuntimeError(
                f"SQLite 必须位于单机本地持久文件系统，当前目录为 {fs_type or '网络路径'}："
                f"{self.state_db.parent}")

        copied = 0
        legacy_root = self.project_root / "data"
        if adopt_legacy and legacy_root.resolve() != self.data_root.resolve() \
                and legacy_root.exists():
            old_secret = legacy_root / ".jwt_secret"
            new_secret = self.data_root / ".jwt_secret"
            if old_secret.is_file() and not new_secret.exists():
                shutil.copy2(old_secret, new_secret)
                copied += 1
            copied += _copy_tree_missing(legacy_root / "users", self.users_root)
        return {"ok": True, "legacy_metadata_files_copied": copied,
                "sqlite_filesystem": fs_type or "unknown"}

    def apply_process_defaults(self) -> None:
        """Expose validated defaults to lazily imported/spawned worker modules."""
        defaults = {
            "GTAI_DATA_ROOT": self.data_root,
            "WORKSPACE_ROOT": self.workspace_root,
            "GTAI_TEMP_ROOT": self.temp_root,
            "GTAI_STATE_DB": self.state_db,
            "GTAI_SEARCH_CACHE_ENTRIES": self.search_cache_entries,
            "GTAI_SEARCH_CACHE_TTL_SECONDS": self.search_cache_ttl_seconds,
        }
        # Export the validated, absolute effective values.  ``setdefault`` is
        # not sufficient here: an environment value may have been relative or
        # a caller may have loaded an explicit environment mapping for a child
        # process.  Spawned workers must see exactly the paths we validated.
        for name, value in defaults.items():
            os.environ[name] = str(value)
        # Python tempfile in spawn workers reads platform temp variables rather
        # than GTAI_TEMP_ROOT. Do not replace an administrator's explicit choice.
        for name in (("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)):
            os.environ.setdefault(name, str(self.temp_root))

    def budget_environment(self, env: Optional[Mapping[str, str]] = None) -> dict:
        merged = dict(os.environ if env is None else env)
        for name, value in self.budget_values.items():
            merged.setdefault(name, str(value))
        return merged

    def public_summary(self) -> dict:
        return {
            "data_root": str(self.data_root),
            "workspace_root": str(self.workspace_root),
            "temp_root": str(self.temp_root),
            "state_db": str(self.state_db),
            "user_cache_capacity": self.user_cache_capacity,
            "user_cache_ttl_seconds": self.user_cache_ttl_seconds,
            "search_cache_entries": self.search_cache_entries,
            "search_cache_ttl_seconds": self.search_cache_ttl_seconds,
            "memory_writeback_attempts": self.memory_writeback_attempts,
            "config": str(self.source_path),
        }


_DEFAULT: Optional[DeploymentConfig] = None


def current() -> DeploymentConfig:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DeploymentConfig.load()
    return _DEFAULT


def reset_for_tests() -> None:
    global _DEFAULT
    _DEFAULT = None
