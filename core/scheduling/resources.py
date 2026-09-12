"""部署总预算、按尝试预留和新鲜压力采样。只读取小型系统/文件元数据。"""
import math
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

MIB = 1024 ** 2
GIB = 1024 ** 3


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def cgroup_values(root=Path("/sys/fs/cgroup")):
    """检查可见层级的最严格配额，兼容 cgroup v1/v2。"""
    cpu, memory, used = [], [], []
    roots = {Path(root)}
    for line in _read("/proc/self/cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, rel = parts[1], parts[2].lstrip("/")
        bases = [Path(root)] if not controllers else [Path(root) / c for c in controllers.split(",")]
        for base in bases:
            p = base / rel
            while p.is_relative_to(base):
                roots.add(p)
                if p == base:
                    break
                p = p.parent
    roots.update((Path(root) / "memory", Path(root) / "cpu"))
    for p in roots:
        q = _read(p / "cpu.max").split()
        if len(q) == 2 and q[0] != "max" and float(q[1]) > 0:
            cpu.append(float(q[0]) / float(q[1]))
        q, period = _read(p / "cpu.cfs_quota_us"), _read(p / "cpu.cfs_period_us")
        if q and period and float(q) > 0 and float(period) > 0:
            cpu.append(float(q) / float(period))
        for name in ("memory.max", "memory.limit_in_bytes"):
            v = _read(p / name)
            if v and v != "max" and 0 < int(v) < 2 ** 60:
                memory.append(int(v))
                usage = _read(p / ("memory.current" if name == "memory.max" else "memory.usage_in_bytes"))
                if usage:
                    used.append((int(v), int(usage)))
    return cpu, memory, used


def _number(env, name, default, *, integer=False, minimum=0):
    raw = env.get("GTAI_" + name, default)
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= minimum or (integer and value != int(value)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(f"GTAI_{name} 必须为大于 {minimum} 的{'整数' if integer else '有限数值'}") from None
    return int(value) if integer else value


@dataclass(frozen=True)
class Budget:
    cpu: float
    memory: int
    compute_jobs: int = 1
    download_jobs: int = 1
    connections: int = 8
    prefetch: int = 1
    catalog_jobs: int = 2
    model_jobs: int = 2
    disk_margin: int = GIB
    disk_cache: int = 20 * GIB
    timeout: float = 7200
    network_attempts: int = 3
    sources: dict = None

    @classmethod
    def detect(cls, disk_root, env=None):
        env = os.environ if env is None else env
        quotas, limits, _ = cgroup_values()
        cpu = [float(os.cpu_count() or 1), *quotas]
        try:
            cpu.append(float(len(psutil.Process().cpu_affinity())))
        except (AttributeError, psutil.Error):
            pass
        cpu_boundary = min(cpu)
        memory_boundary = min([psutil.virtual_memory().total, *limits])
        cpu_requested = _number(env, "CPU_BUDGET", max(1, cpu_boundary - 1) if cpu_boundary >= 1 else cpu_boundary)
        mem_requested = _number(env, "MEMORY_GIB", memory_boundary / GIB)
        memory_boundary = min(memory_boundary, int(mem_requested * GIB))
        total = shutil.disk_usage(disk_root).total
        return cls(
            cpu=min(cpu_requested, cpu_boundary), memory=int(memory_boundary * .8),
            compute_jobs=_number(env, "COMPUTE_JOBS", 1, integer=True),
            download_jobs=_number(env, "DOWNLOAD_JOBS", 1, integer=True),
            connections=_number(env, "DOWNLOAD_CONNECTIONS", 8, integer=True),
            prefetch=_number(env, "PREFETCH_PACKAGES", 1, integer=True),
            catalog_jobs=_number(env, "CATALOG_JOBS", 2, integer=True),
            model_jobs=_number(env, "MODEL_JOBS", 2, integer=True),
            disk_margin=int(_number(env, "DISK_MARGIN_GIB", min(10, max(1, total * .05 / GIB))) * GIB),
            disk_cache=int(min(total * .1, _number(env, "DISK_CACHE_GIB", 20) * GIB)),
            timeout=_number(env, "NODE_TIMEOUT_SECONDS", 7200),
            network_attempts=_number(env, "NETWORK_ATTEMPTS", 3, integer=True),
            sources={"cpu_detected": cpu, "cpu_requested": cpu_requested,
                     "memory_detected": limits, "physical_memory": psutil.virtual_memory().total,
                     "memory_requested": mem_requested * GIB, "memory_fraction": .8,
                     "reason": "取容器、亲和性与管理员额度的最小值，内存再保留百分之二十余量"})


@dataclass
class Claim:
    kind: str
    cpu: float
    threads: int
    memory: int
    disk: int
    connections: int = 0
    exclusive: bool = False
    basis: dict = None


def kind_for(node_type):
    if node_type == "acquire_asset":
        return "download"
    if node_type == "search_scene":
        return "catalog"
    if node_type in ("select_scene", "train_decision"):
        return "model"
    return "compute"


def estimate(node_type, snapshot, facts, budget, history_peak=0):
    kind = kind_for(node_type)
    params = {k: v["value"] for k, v in snapshot.get("params", {}).items()}
    scale = facts.get("scale", {})
    pixels = int(scale.get("pixels", 0))
    width = int(scale.get("width", 0))
    scenes = max(1, int(scale.get("scenes", 1)))
    bands = max(1, int(scale.get("bands", 5)))
    table_bytes = int(scale.get("table_bytes", 0))
    sample_bytes = int(scale.get("sample_bytes", 0))
    constraint_bytes = int(scale.get("constraint_bytes", 0))
    file_bytes = int(scale.get("file_bytes", 0))
    rf = params.get("rf_params", {})
    trees = int(rf.get("n_estimators", 200))
    depth = int(rf.get("max_depth") or 40)
    basis = {**scale, "node_type": node_type, "n_estimators": trees, "max_depth": depth,
             "history_peak": history_peak, "margin_factor": 1.25}
    unknown = False
    if kind in ("catalog", "model"):
        memory, disk = 384 * MIB, 16 * MIB
    elif kind == "download":
        # 下载真实体量常大于元数据估算（同日期多景、多波段、签名链接
        # 文件大小差异）：给 3 倍裕量；完全未知时按 16GB 预留
        # （不超过磁盘缓存预算）——此前 4GB 下限在大数据集上会撞
        # 预留上限、下载被中途截断（用户实测：两轮均在此失败）。
        asset_bytes = max(0, int(scale.get("asset_bytes", 0)))
        memory = 384 * MIB + budget.connections * MIB
        disk = max(2 * GIB, asset_bytes * 3)
        unknown = not bool(scale.get("asset_size_known", True))
        if unknown or not asset_bytes:
            disk = max(disk, min(16 * GIB, budget.disk_cache))
    else:
        base = 512 * MIB
        if node_type in ("data_check", "prep_check", "ttri_check", "promote_best"):
            memory = base
        elif node_type == "prepare_local":
            # 月度现有路径还有整幅掩膜，Float64 中位数工作区随宽和景数增长。
            memory = base + pixels * 16 + width * 512 * scenes * 8 * 5
        elif node_type == "preprocess_split":
            memory = base + pixels // 9 * (bands * 8 + 32) + width * 512 * (bands * 8 + 48)
        elif node_type == "ttri":
            memory = base + sample_bytes * 3 + constraint_bytes * 2 + min(pixels, 500000) * 160
        elif node_type == "rf_round":
            memory = base + sample_bytes * 4 + trees * min(2 ** min(depth, 25), max(1, int(scale.get("train_rows", 1000000))) * 2) * 80
            # 叶节点的最低样本量限制树的最大节点数。
            leaf = max(1, int(rf.get("min_samples_leaf", 8)))
            memory = min(memory, base + sample_bytes * 4 + trees * max(1, int(scale.get("train_rows", 1000000))) * 160 // leaf)
        else:
            memory = base + constraint_bytes * 2 + min(pixels, 500000) * 256
        disk = max(GIB, file_bytes * (2 if node_type == "ttri" else 1), pixels * 96)
        unknown = not bool(pixels or table_bytes)
        if unknown:
            memory = max(memory, min(4 * GIB, int(budget.memory * .65)))
            basis["conservative_unknown"] = True
    memory = int(max(memory, history_peak * 1.2) * 1.25)
    quota = min(budget.cpu, max(1, math.floor(budget.cpu / budget.compute_jobs))) if kind == "compute" else 0
    connections = max(1, budget.connections // budget.download_jobs) if kind == "download" else 0
    return Claim(kind, quota, max(1, math.floor(quota)), memory, disk, connections, unknown, basis)


def process_memory(pid):
    try:
        p = psutil.Process(pid)
        return sum(x.memory_info().rss for x in [p, *p.children(recursive=True)] if x.is_running())
    except psutil.Error:
        return 0


class ResourceLedger:
    def __init__(self, budget, root):
        self.budget, self.root = budget, Path(root)
        self.claims = {}
        self.measured = {}
        self.written = {}
        self.external_model_requests = 0

    def pressure(self):
        b = self.budget
        _, _, container = cgroup_values()
        rss = psutil.Process().memory_info().rss
        active = sum(max(c.memory, self.measured.get(a, 0)) for a, c in self.claims.items())
        available = psutil.virtual_memory().available
        if container:
            available = min(available, *(max(0, limit - used) for limit, used in container))
        headroom = sum(max(0, c.memory - self.measured.get(a, 0)) for a, c in self.claims.items())
        return {"resident": rss, "reserved_active": active, "available": available,
                "future_growth": headroom, "over_reserved": any(self.measured.get(a, 0) > c.memory for a, c in self.claims.items()),
                "memory_pressure": rss + active > b.memory or available < 128 * MIB,
                "disk_free": shutil.disk_usage(self.root).free}

    def can_fit(self, claim):
        b, p = self.budget, self.pressure()
        counts = {k: sum(c.kind == k for c in self.claims.values()) for k in ("compute", "download", "catalog", "model")}
        counts["model"] += self.external_model_requests
        caps = {"compute": b.compute_jobs, "download": b.download_jobs, "catalog": b.catalog_jobs, "model": b.model_jobs}
        if counts[claim.kind] >= caps[claim.kind]:
            kind_cn = {"compute": "计算", "download": "下载", "catalog": "目录检索", "model": "模型调用"}.get(claim.kind, claim.kind)
            return False, f"等待{kind_cn}作业位置"
        if claim.kind in ("compute", "download") and any(c.exclusive for c in self.claims.values()):
            return False, "另一任务正在独占执行（尚无峰值估计），暂停预取"
        if claim.exclusive and any(c.kind in ("compute", "download") for c in self.claims.values()):
            return False, "为安全起见本节点独占执行，等待其他计算结束"
        if sum(c.cpu for c in self.claims.values()) + claim.cpu > b.cpu + 1e-9:
            return False, "等待 CPU 额度"
        if sum(c.connections for c in self.claims.values()) + claim.connections > b.connections:
            return False, "等待全局连接额度"
        if p["over_reserved"] or p["memory_pressure"]:
            return False, "实测内存超过预留或总压力过高，停止新派发和预取"
        if p["resident"] + p["reserved_active"] + claim.memory > b.memory:
            return False, f"内存不足，需要 {claim.memory} 字节，应用预算 {b.memory} 字节"
        if p["available"] - p["future_growth"] < claim.memory:
            return False, "当前容器或共享主机可用内存不足"
        remaining = sum(max(0, c.disk - self.written.get(a, 0)) for a, c in self.claims.items())
        if p["disk_free"] - b.disk_margin - remaining < claim.disk:
            return False, f"磁盘不足，需要新增 {claim.disk} 字节及紧急余量"
        return True, ""

    def reserve(self, attempt, claim):
        ok, reason = self.can_fit(claim)
        if not ok:
            raise RuntimeError(reason)
        self.claims[attempt] = claim

    def release(self, attempt):
        self.claims.pop(attempt, None)
        self.measured.pop(attempt, None)
        self.written.pop(attempt, None)

    def snapshot(self):
        return {"effective": asdict(self.budget), "pressure": self.pressure(),
                "reservations": {a: {**asdict(c), "measured": self.measured.get(a, 0),
                                      "written": self.written.get(a, 0)} for a, c in self.claims.items()}}
