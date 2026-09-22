from app.admission import (
    CPU_HARD_LIMIT_PERCENT,
    CPU_SOFT_LIMIT_PERCENT,
    MAX_ACTIVE_COST,
    MAX_HEAVY_JOBS,
    MIN_FREE_MEMORY_MB,
    TEMP_DISK_HARD_LIMIT_PERCENT,
    AdmissionController,
    ResourceSnapshot,
)


def controller(snapshot):
    return AdmissionController(snapshot_fn=lambda data_dir: snapshot)


def healthy_snapshot(**overrides):
    defaults = dict(cpu_percent=10.0, available_memory_mb=MIN_FREE_MEMORY_MB * 4, disk_percent=10.0)
    defaults.update(overrides)
    return ResourceSnapshot(**defaults)


class TestAdmissionController:
    def test_admits_light_job_when_resources_are_healthy(self):
        allowed, reason, _ = controller(healthy_snapshot()).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=0)
        assert allowed and reason is None

    def test_rejects_when_cpu_hard_limit_exceeded(self):
        snapshot = healthy_snapshot(cpu_percent=CPU_HARD_LIMIT_PERCENT + 1)
        allowed, reason, _ = controller(snapshot).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=0)
        assert not allowed and reason == "cpu_limit"

    def test_rejects_when_memory_below_minimum(self):
        snapshot = healthy_snapshot(available_memory_mb=MIN_FREE_MEMORY_MB - 1)
        allowed, reason, _ = controller(snapshot).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=0)
        assert not allowed and reason == "memory_limit"

    def test_rejects_when_disk_hard_limit_exceeded(self):
        snapshot = healthy_snapshot(disk_percent=TEMP_DISK_HARD_LIMIT_PERCENT + 1)
        allowed, reason, _ = controller(snapshot).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=0)
        assert not allowed and reason == "disk_limit"

    def test_rejects_heavy_job_over_heavy_slot_cap(self):
        allowed, reason, _ = controller(healthy_snapshot()).can_admit(
            data_dir="/tmp", cost_weight=4, is_heavy=True, active_cost=0, active_heavy_count=MAX_HEAVY_JOBS)
        assert not allowed and reason == "heavy_slot_limit"

    def test_light_job_still_admitted_at_heavy_slot_cap(self):
        allowed, reason, _ = controller(healthy_snapshot()).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=MAX_HEAVY_JOBS)
        assert allowed and reason is None

    def test_rejects_when_cost_budget_would_be_exceeded(self):
        allowed, reason, _ = controller(healthy_snapshot()).can_admit(
            data_dir="/tmp", cost_weight=4, is_heavy=False, active_cost=MAX_ACTIVE_COST - 1, active_heavy_count=0)
        assert not allowed and reason == "cost_budget"

    def test_soft_limit_blocks_new_heavy_jobs_but_not_light_ones(self):
        snapshot = healthy_snapshot(cpu_percent=CPU_SOFT_LIMIT_PERCENT + 1)
        heavy_allowed, heavy_reason, _ = controller(snapshot).can_admit(
            data_dir="/tmp", cost_weight=4, is_heavy=True, active_cost=0, active_heavy_count=0)
        light_allowed, light_reason, _ = controller(snapshot).can_admit(
            data_dir="/tmp", cost_weight=1, is_heavy=False, active_cost=0, active_heavy_count=0)
        assert not heavy_allowed and heavy_reason == "cpu_limit"
        assert light_allowed and light_reason is None
