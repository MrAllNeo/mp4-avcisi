import asyncio
import time

from fastapi.testclient import TestClient
import pytest

from app import main


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA", tmp_path)
    main.jobs.clear()
    main.analyses.clear()
    with TestClient(main.app, base_url="http://localhost") as client:
        yield client


def test_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Videoyu bul" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400


def test_invalid_url_and_cross_origin(client):
    assert client.post("/api/analyze", json={"url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/api/analyze", json={"url": "https://example.com"}, headers={"Origin": "https://attacker.example"}).status_code == 403


def test_analysis_error_is_actionable(client, monkeypatch):
    async def worker(*args, **kwargs):
        raise ValueError("Video bulunamadı.")
    monkeypatch.setattr(main, "worker", worker)
    response = client.post("/api/analyze", json={"url": "https://example.com"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Video bulunamadı."


def test_expired_analysis_and_quality_validation(client):
    main.analyses["expired"] = {"created": time.time() - main.TTL - 1}
    assert client.post("/api/downloads", json={"analysis_id": "expired"}).status_code == 404
    main.analyses["fresh"] = {"created": time.time(), "metadata": {"qualities": [720]}}
    assert client.post("/api/downloads", json={"analysis_id": "fresh", "height": 1080}).status_code == 422


def test_completed_file_response(client, tmp_path):
    job = main.Job("a" * 32, "https://example.com", "My / video", None)
    job.status = "complete"
    main.jobs[job.id] = job
    directory = tmp_path / job.id
    directory.mkdir()
    (directory / "video.mp4").write_bytes(b"test-media")
    response = client.get(f"/api/downloads/{job.id}/file")
    assert response.status_code == 200
    assert response.content == b"test-media"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"] == "video/mp4"


def test_job_cancel_stops_worker_and_removes_files(client, monkeypatch, tmp_path):
    async def worker(*args, **kwargs):
        await asyncio.sleep(120)
    monkeypatch.setattr(main, "worker", worker)
    main.analyses["fresh"] = {"url": "https://example.com", "created": time.time(), "metadata": {"title": "test", "qualities": []}}
    response = client.post("/api/downloads", json={"analysis_id": "fresh"})
    assert response.status_code == 202
    key = response.json()["id"]
    assert client.get(f"/api/downloads/{key}/file").status_code == 409
    response = client.delete(f"/api/downloads/{key}")
    assert response.json()["status"] == "cancelled"
    assert not (tmp_path / key).exists()
