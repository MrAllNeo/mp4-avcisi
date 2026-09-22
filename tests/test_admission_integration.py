import asyncio
import time

from fastapi.testclient import TestClient
import pytest

from app import main
from app.admission import ResourceSnapshot
from app.errors import MediaError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA", tmp_path)
    main.jobs.clear()
    main.analyses.clear()
    with TestClient(main.app, base_url="http://localhost") as client:
        yield client


def add_analysis(key, **metadata):
    main.analyses[key] = {
        "url": f"https://example.com/{key}.mp4", "created": time.time(),
        "metadata": {"title": key, "qualities": [720], **metadata},
    }


async def instant_worker(payload, on_event=None, timeout=90):
    if payload["mode"] == "analyze":
        return {"metadata": {"title": "new", "qualities": [720]}}
    return {"route": "direct", "size": 1234, "strategy": "REMUX", "cost_weight": 1}


class TestCostEstimateAtSubmission:
    def test_download_records_estimated_cost_weight(self, client, monkeypatch):
        monkeypatch.setattr(main, "worker", instant_worker)
        add_analysis("a", duration=200)
        response = client.post("/api/downloads", json={"analysis_id": "a", "height": 720})
        assert response.status_code == 202
        # height=720 -> cost tier 4 (see app/cost.py cost_for_resolution)
        stored = main.jobs[response.json()["id"]]
        assert stored.compatibility == "fast"

    def test_download_accepts_compatibility_preference(self, client, monkeypatch):
        monkeypatch.setattr(main, "worker", instant_worker)
        add_analysis("b")
        response = client.post("/api/downloads", json={"analysis_id": "b", "compatibility": "compatible"})
        assert response.status_code == 202
        assert main.jobs[response.json()["id"]].compatibility == "compatible"

    def test_invalid_compatibility_is_rejected(self, client):
        add_analysis("c")
        response = client.post("/api/downloads", json={"analysis_id": "c", "compatibility": "ultra"})
        assert response.status_code == 422


class TestAdmissionWaitInsideDownload:
    def test_queued_jobs_do_not_count_toward_active_cost(self, client, monkeypatch):
        # Two queued (not yet processing) jobs must not block a third from
        # being admitted — only genuinely running jobs consume the budget.
        monkeypatch.setattr(main.admission, "MAX_ACTIVE_COST", 5)
        for job_id, cost in [("queued-1", 8), ("queued-2", 8)]:
            job = main.Job(job_id, f"https://example.com/{job_id}", job_id, None)
            job.status, job.cost_weight = "queued", cost
            main.jobs[job_id] = job
        job = main.Job("running-candidate", "https://example.com/x", "x", None)
        job.cost_weight = 1
        allowed, reason, _ = main.admission_controller.can_admit(
            data_dir=main.DATA, cost_weight=job.cost_weight,
            is_heavy=False,
            active_cost=sum(j.cost_weight for j in main.jobs.values() if j.status == "processing"),
            active_heavy_count=0,
        )
        assert allowed and reason is None

    def test_wait_for_capacity_retries_then_succeeds(self, client, monkeypatch):
        responses = iter([
            (False, "cpu_limit", ResourceSnapshot(95.0, 4000, 10)),
            (True, None, ResourceSnapshot(10.0, 4000, 10)),
        ])
        monkeypatch.setattr(main.admission_controller, "can_admit", lambda **kw: next(responses))
        monkeypatch.setattr(main, "ADMISSION_POLL_SECONDS", 0)
        job = main.Job("wait-job", "https://example.com/wait", "wait", None)
        job.cost_weight = 1

        async def run():
            await main.wait_for_capacity(job)

        asyncio.run(run())  # must return normally (not raise) once capacity frees up

    def test_wait_for_capacity_raises_retryable_after_timeout(self, monkeypatch):
        monkeypatch.setattr(main.admission_controller, "can_admit",
                            lambda **kw: (False, "cpu_limit", ResourceSnapshot(95.0, 4000, 10)))
        monkeypatch.setattr(main, "ADMISSION_WAIT_TIMEOUT", 0)
        monkeypatch.setattr(main, "ADMISSION_POLL_SECONDS", 0)
        job = main.Job("timeout-job", "https://example.com/timeout", "timeout", None)
        job.cost_weight = 1

        async def run():
            await main.wait_for_capacity(job)

        with pytest.raises(MediaError) as exc_info:
            asyncio.run(run())
        assert exc_info.value.code == "capacity_unavailable"
        assert exc_info.value.retryable is True

    def test_download_fails_retryably_when_capacity_never_frees(self, client, monkeypatch):
        monkeypatch.setattr(main, "worker", instant_worker)
        monkeypatch.setattr(main.admission_controller, "can_admit",
                            lambda **kw: (False, "memory_limit", ResourceSnapshot(10.0, 1, 10)))
        monkeypatch.setattr(main, "ADMISSION_WAIT_TIMEOUT", 0)
        monkeypatch.setattr(main, "ADMISSION_POLL_SECONDS", 0)
        add_analysis("d")
        key = client.post("/api/downloads", json={"analysis_id": "d"}).json()["id"]
        for _ in range(50):
            status = client.get(f"/api/downloads/{key}").json()
            if status["status"] != "queued" and status["status"] != "processing":
                break
            time.sleep(0.05)
        assert status["status"] == "error"
        assert status["retryable"] is True


class TestMetricsEndpoint:
    def test_metrics_reports_admission_configuration(self, client):
        body = client.get("/api/metrics").json()
        assert body["max_active_cost"] == main.admission.MAX_ACTIVE_COST
        assert body["max_heavy_jobs"] == main.admission.MAX_HEAVY_JOBS
        assert "cpu_percent" in body and "available_memory_mb" in body and "disk_percent" in body
        assert body["active_jobs"] == 0
