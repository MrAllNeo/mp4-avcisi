"""CPU/RAM/disk-aware admission control for new jobs.

Never touches jobs that are already running — it only decides whether a
*new* job may be accepted, so it can't destabilize in-flight downloads.
psutil.cpu_percent(interval=None) is non-blocking and reports usage since the
previous call; prime_cpu_sampling() must run once at process startup so the
first real check after that has a meaningful baseline.
"""
from dataclasses import dataclass
import os
import shutil

import psutil


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


CPU_SOFT_LIMIT_PERCENT = _env_int("MP4_CPU_SOFT_LIMIT_PERCENT", 75)
CPU_HARD_LIMIT_PERCENT = _env_int("MP4_CPU_HARD_LIMIT_PERCENT", 88)
MIN_FREE_MEMORY_MB = _env_int("MP4_MIN_FREE_MEMORY_MB", 1536)
TEMP_DISK_SOFT_LIMIT_PERCENT = _env_int("MP4_TEMP_DISK_SOFT_LIMIT_PERCENT", 75)
TEMP_DISK_HARD_LIMIT_PERCENT = _env_int("MP4_TEMP_DISK_HARD_LIMIT_PERCENT", 85)
MAX_ACTIVE_COST = _env_int("MP4_MAX_ACTIVE_COST", 8)
MAX_HEAVY_JOBS = _env_int("MP4_MAX_HEAVY_JOBS", 1)

HEAVY_STRATEGIES = frozenset({"VIDEO_TRANSCODE", "FULL_TRANSCODE"})

REASON_MESSAGES = {
    "cpu_limit": "Sunucu şu anda yoğun. Biraz sonra yeniden dene.",
    "memory_limit": "Sunucuda yeterli boş bellek yok. Biraz sonra yeniden dene.",
    "disk_limit": "Sunucuda yeterli geçici disk alanı yok. Biraz sonra yeniden dene.",
    "heavy_slot_limit": "Ağır dönüştürme kapasitesi dolu. Biraz sonra yeniden dene.",
    "cost_budget": "İşlem kapasitesi dolu. Bir işlem tamamlandığında yeniden dene.",
}


def prime_cpu_sampling():
    psutil.cpu_percent(interval=None)


@dataclass
class ResourceSnapshot:
    cpu_percent: float
    available_memory_mb: float
    disk_percent: float


def read_resource_snapshot(data_dir) -> ResourceSnapshot:
    cpu_percent = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    usage = shutil.disk_usage(data_dir)
    disk_percent = (usage.used / usage.total * 100) if usage.total else 0.0
    return ResourceSnapshot(
        cpu_percent=cpu_percent,
        available_memory_mb=memory.available / (1024 * 1024),
        disk_percent=disk_percent,
    )


class AdmissionController:
    """Takes data_dir per call (not stored) so callers can freely repoint their
    data directory (e.g. tests monkeypatching it) without going stale."""

    def __init__(self, *, snapshot_fn=read_resource_snapshot):
        self._snapshot_fn = snapshot_fn

    def snapshot(self, data_dir) -> ResourceSnapshot:
        return self._snapshot_fn(data_dir)

    def can_admit(self, *, data_dir, cost_weight: int, is_heavy: bool, active_cost: int, active_heavy_count: int):
        """Returns (allowed, reason_code, snapshot). reason_code is None when allowed."""
        snapshot = self.snapshot(data_dir)
        if snapshot.cpu_percent >= CPU_HARD_LIMIT_PERCENT:
            return False, "cpu_limit", snapshot
        if snapshot.available_memory_mb < MIN_FREE_MEMORY_MB:
            return False, "memory_limit", snapshot
        if snapshot.disk_percent >= TEMP_DISK_HARD_LIMIT_PERCENT:
            return False, "disk_limit", snapshot
        if is_heavy and active_heavy_count >= MAX_HEAVY_JOBS:
            return False, "heavy_slot_limit", snapshot
        if active_cost + cost_weight > MAX_ACTIVE_COST:
            return False, "cost_budget", snapshot
        # Above the soft limit: still admit light jobs, but stop admitting new
        # heavy (transcode) jobs so existing ones can drain the CPU pressure.
        if is_heavy and snapshot.cpu_percent >= CPU_SOFT_LIMIT_PERCENT:
            return False, "cpu_limit", snapshot
        return True, None, snapshot
