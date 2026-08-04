"""
原子写入与产物写前校验工具（对应审查文档 A-02 / A-08 / 7.2 节）

统一约定：任何"可能被下游依赖、且一旦写坏会被误当成功"的产物，
必须先写到同目录下的固定 ``<final_name>.partial``，经调用方校验通过后，
再用 ``os.replace()`` 原子改名为最终固定文件名。文件名本身仍是工程写死的固定名，
本模块不引入任何动态/自由命名。
"""

import json
import os
from typing import Any, Callable, Tuple


def partial_path(final_path: str) -> str:
    """固定 .partial 后缀命名（不是动态文件名，只是同一固定名加固定后缀）。"""
    return final_path + ".partial"


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def atomic_write_bytes(final_path: str, data: bytes) -> str:
    """写 .partial → fsync → os.replace 原子改名，避免半写文件被下游误读。"""
    _ensure_parent(final_path)
    tmp = partial_path(final_path)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final_path)
    return final_path


def atomic_write_text(final_path: str, text: str, encoding: str = "utf-8") -> str:
    return atomic_write_bytes(final_path, text.encode(encoding))


def _json_default(o: Any) -> Any:
    """兜底 JSON 序列化：numpy 标量/数组不是原生可序列化类型，各调用方本应显式
    转换为 Python 原生类型，这里再加一层防御，避免任何遗漏导致写 manifest/
    metrics JSON 时抛 TypeError 而使整个 stage 被误判为失败。"""
    try:
        import numpy as np

        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    return str(o)


def atomic_write_json(final_path: str, obj: Any, *, ensure_ascii: bool = False, indent: int = 2) -> str:
    text = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=_json_default)
    return atomic_write_text(final_path, text)


def atomic_replace(tmp_path: str, final_path: str) -> str:
    """把已写好的临时文件原子替换为最终文件名。

    .partial 与最终文件必须在同一目录/文件系统才能依赖 os.replace 的原子性；
    若调用方传入了不同目录的临时文件，这里会先搬到目标目录再替换。
    """
    _ensure_parent(final_path)
    tmp_dir = os.path.dirname(os.path.abspath(tmp_path)) or "."
    final_dir = os.path.dirname(os.path.abspath(final_path)) or "."
    if os.path.realpath(tmp_dir) != os.path.realpath(final_dir):
        same_dir_tmp = os.path.join(final_dir, os.path.basename(tmp_path))
        os.replace(tmp_path, same_dir_tmp)
        tmp_path = same_dir_tmp
    os.replace(tmp_path, final_path)
    return final_path


def cleanup_partial(final_path: str) -> None:
    """清理残留的 .partial（成功或失败路径都应调用，属于 tmp/partial 生命周期管理）。"""
    tmp = partial_path(final_path)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def write_verified(
    build_fn: Callable[[str], None],
    final_path: str,
    validator: Callable[[str], Tuple[bool, str]],
) -> str:
    """先在 ``<final_path>.partial`` 上调用 build_fn 写入产物，validator 校验通过后
    原子替换为 final_path；校验失败时删除 partial 并抛异常（A-02：禁止假成功）。

    Args:
        build_fn:  接受输出路径（.partial 路径），执行实际写入。
        final_path: 目标固定文件名（工程写死，不因本函数而改变命名策略）。
        validator: 接受 .partial 路径，返回 (是否通过, 失败原因)；应重新打开文件核查
                   波段数/shape/CRS/transform/nodata/有限值覆盖率等真实状态，
                   而不是信任调用前的假设或"文件存在即成功"。

    Raises:
        RuntimeError: 校验未通过时，不产生（也不保留）正式文件。
    """
    _ensure_parent(final_path)
    tmp = partial_path(final_path)
    cleanup_partial(final_path)
    build_fn(tmp)
    ok, reason = validator(tmp)
    if not ok:
        cleanup_partial(final_path)
        raise RuntimeError(
            f"产物写入后校验未通过，已拒绝生成正式文件 {os.path.basename(final_path)}：{reason}"
        )
    atomic_replace(tmp, final_path)
    return final_path
