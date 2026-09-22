import asyncio

from fastapi.testclient import TestClient
import pytest

from app import main, objectstore
from app.jobstore import Job

R2_ENV = {
    'MP4_R2_ENDPOINT': 'https://account.r2.cloudflarestorage.com',
    'MP4_R2_BUCKET': 'downloads',
    'MP4_R2_ACCESS_KEY_ID': 'key',
    'MP4_R2_SECRET_ACCESS_KEY': 'secret',
}


@pytest.fixture(autouse=True)
def clean_client_cache():
    objectstore.reset_client()
    yield
    objectstore.reset_client()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA", tmp_path)
    main.jobs.clear()
    main.analyses.clear()
    with TestClient(main.app, base_url="http://localhost") as client:
        yield client


def configure(monkeypatch, **overrides):
    values = {**R2_ENV, **overrides}
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def complete_job(client, *, offloaded, write_file=True):
    job = Job(id='a' * 32, url='https://example.com/v', title='Örnek Video', height=720)
    job.status, job.size, job.percent = 'complete', 1024, 100
    job.offloaded = offloaded
    main.jobs[job.id] = job
    if write_file:
        directory = main.DATA / job.id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'video.mp4').write_bytes(b'video-bytes')
    return job


class TestConfiguration:
    def test_not_configured_without_environment(self, monkeypatch):
        configure(monkeypatch, **{name: None for name in R2_ENV})
        assert objectstore.is_configured() is False

    def test_configured_when_every_setting_is_present(self, monkeypatch):
        configure(monkeypatch)
        assert objectstore.is_configured() is True

    def test_a_single_missing_setting_disables_the_offload(self, monkeypatch):
        configure(monkeypatch, MP4_R2_BUCKET=None)
        assert objectstore.is_configured() is False

    def test_url_ttl_is_clamped_to_a_sane_window(self, monkeypatch):
        configure(monkeypatch)
        monkeypatch.setenv('MP4_R2_URL_TTL', '1')
        assert objectstore.url_ttl() == 60
        monkeypatch.setenv('MP4_R2_URL_TTL', '999999999')
        assert objectstore.url_ttl() == 604800
        monkeypatch.setenv('MP4_R2_URL_TTL', 'not-a-number')
        assert objectstore.url_ttl() == objectstore.DEFAULT_URL_TTL

    def test_object_key_is_namespaced_per_job(self):
        assert objectstore.object_key('abc') == 'downloads/abc/video.mp4'


class TestDownloadEndpoint:
    def test_offloaded_job_redirects_to_a_presigned_url(self, client, monkeypatch):
        configure(monkeypatch)
        job = complete_job(client, offloaded=True)
        monkeypatch.setattr(
            objectstore, 'presigned_url', lambda job_id, filename: f'https://cdn.example/{job_id}?n={filename}'
        )

        response = client.get(f'/api/downloads/{job.id}/file', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'].startswith('https://cdn.example/')

    def test_local_file_is_served_when_the_offload_is_off(self, client, monkeypatch):
        configure(monkeypatch, **{name: None for name in R2_ENV})
        job = complete_job(client, offloaded=False)

        response = client.get(f'/api/downloads/{job.id}/file', follow_redirects=False)
        assert response.status_code == 200
        assert response.content == b'video-bytes'

    def test_signing_failure_falls_back_to_the_local_file(self, client, monkeypatch):
        configure(monkeypatch)
        job = complete_job(client, offloaded=True)

        def explode(job_id, filename):
            raise objectstore.ObjectStoreError('imza üretilemedi')

        monkeypatch.setattr(objectstore, 'presigned_url', explode)

        response = client.get(f'/api/downloads/{job.id}/file', follow_redirects=False)
        assert response.status_code == 200
        assert response.content == b'video-bytes'

    def test_missing_local_file_without_offload_is_reported_as_gone(self, client, monkeypatch):
        configure(monkeypatch, **{name: None for name in R2_ENV})
        job = complete_job(client, offloaded=False, write_file=False)

        response = client.get(f'/api/downloads/{job.id}/file', follow_redirects=False)
        assert response.status_code == 410

    def test_incomplete_job_is_refused(self, client):
        job = complete_job(client, offloaded=False)
        job.status = 'processing'
        response = client.get(f'/api/downloads/{job.id}/file', follow_redirects=False)
        assert response.status_code == 409


class TestOffloadResult:
    def test_skipped_when_not_configured(self, monkeypatch, tmp_path):
        configure(monkeypatch, **{name: None for name in R2_ENV})
        path = tmp_path / 'video.mp4'
        path.write_bytes(b'x')
        job = Job(id='b' * 32, url='https://example.com/v', title='T', height=720)
        assert asyncio.run(main.offload_result(job, path)) is False

    def test_missing_file_is_not_uploaded(self, monkeypatch, tmp_path):
        configure(monkeypatch)
        job = Job(id='c' * 32, url='https://example.com/v', title='T', height=720)
        assert asyncio.run(main.offload_result(job, tmp_path / 'absent.mp4')) is False

    def test_upload_failure_leaves_the_job_serving_locally(self, monkeypatch, tmp_path):
        configure(monkeypatch)
        path = tmp_path / 'video.mp4'
        path.write_bytes(b'x')

        def explode(local_path, job_id):
            raise objectstore.ObjectStoreError('yükleme başarısız')

        monkeypatch.setattr(objectstore, 'upload', explode)
        job = Job(id='d' * 32, url='https://example.com/v', title='T', height=720)
        assert asyncio.run(main.offload_result(job, path)) is False

    def test_successful_upload_marks_the_job(self, monkeypatch, tmp_path):
        configure(monkeypatch)
        path = tmp_path / 'video.mp4'
        path.write_bytes(b'x')
        uploaded = []
        monkeypatch.setattr(objectstore, 'upload', lambda p, job_id: uploaded.append(job_id))

        job = Job(id='e' * 32, url='https://example.com/v', title='T', height=720)
        assert asyncio.run(main.offload_result(job, path)) is True
        assert uploaded == ['e' * 32]


class TestDelete:
    def test_delete_is_a_noop_when_unconfigured(self, monkeypatch):
        configure(monkeypatch, **{name: None for name in R2_ENV})
        objectstore.delete('f' * 32)  # must not raise

    def test_delete_swallows_backend_errors(self, monkeypatch):
        configure(monkeypatch)

        class Boom:
            def delete_object(self, **kwargs):
                raise objectstore.ClientError({'Error': {}}, 'DeleteObject')

        monkeypatch.setattr(objectstore, '_get_client', lambda: Boom())
        objectstore.delete('f' * 32)  # must not raise
