import asyncio
import errno
import json
from pathlib import Path
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main
from app.jobstore import Job, JobStore, TTL
from app.errors import MediaError


def add_analysis(key):
    main.analyses[key] = {'url': f'https://example.com/{key}.mp4', 'created': time.time(),
                          'metadata': {'title': key, 'qualities': [720]}}


async def waiting_worker(payload, *args, **kwargs):
    if payload['mode'] == 'analyze':
        return {'metadata': {'title': 'new', 'qualities': []}}
    (Path(payload['directory']) / 'source.mp4.part').write_bytes(b'partial-download')
    await asyncio.sleep(120)


def test_two_running_third_queued_and_analysis_still_available(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'worker', waiting_worker)
    with TestClient(main.app, base_url='http://localhost') as client:
        keys = []
        for name in ['first', 'second', 'third']:
            add_analysis(name)
            response = client.post('/api/downloads', json={'analysis_id': name})
            assert response.status_code == 202
            keys.append(response.json()['id'])
        jobs = {j['id']: j for j in client.get('/api/downloads').json()['jobs']}
        assert [jobs[key]['status'] for key in keys] == ['processing', 'processing', 'queued']
        assert jobs[keys[2]]['queue_position'] == 1
        assert all('url' not in j for j in jobs.values())
        assert client.post('/api/analyze', json={'url': 'https://example.com/new'}).status_code == 200
        assert client.post(f'/api/downloads/{keys[0]}/pause').json()['status'] == 'paused'
        assert client.get(f'/api/downloads/{keys[2]}').json()['status'] == 'processing'
        assert (tmp_path / keys[0] / 'source.mp4.part').read_bytes() == b'partial-download'
        assert client.post(f'/api/downloads/{keys[0]}/resume').status_code == 202
        assert client.get(f'/api/downloads/{keys[0]}').json()['queue_position'] == 1


def test_restart_retains_partial_and_completed_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'worker', waiting_worker)
    with TestClient(main.app, base_url='http://localhost') as client:
        add_analysis('interrupted')
        key = client.post('/api/downloads', json={'analysis_id': 'interrupted'}).json()['id']
        assert client.get(f'/api/downloads/{key}').json()['status'] == 'processing'
        complete = Job(uuid4().hex, 'https://example.com/complete', 'complete', None, status='complete', expires_at=time.time() + TTL)
        (tmp_path / complete.id).mkdir()
        (tmp_path / complete.id / 'video.mp4').write_bytes(b'completed-media')
        main.jobs[complete.id] = complete
        main.save(complete)
    with TestClient(main.app, base_url='http://localhost') as client:
        paused = client.get(f'/api/downloads/{key}').json()
        assert paused['status'] == 'paused' and paused['retryable']
        assert (tmp_path / key / 'source.mp4.part').exists()
        assert client.get(f'/api/downloads/{complete.id}/file').content == b'completed-media'
        assert client.post(f'/api/downloads/{key}/resume').status_code == 202
        assert client.delete(f'/api/downloads/{key}/purge').status_code == 204
        assert not (tmp_path / key).exists()
        assert not (tmp_path / f'{key}.json').exists()
        assert client.get(f'/api/downloads/{key}').status_code == 404


def test_retryable_error_keeps_partial_and_nonretryable_error_removes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)

    async def failed_worker(payload, *args, **kwargs):
        (Path(payload['directory']) / 'source.mp4.part').write_bytes(b'partial')
        retryable = 'retryable' in payload['url']
        raise MediaError('network' if retryable else 'protected', 'Safe message', retryable)

    monkeypatch.setattr(main, 'worker', failed_worker)
    with TestClient(main.app, base_url='http://localhost') as client:
        for name, can_retry in [('retryable', True), ('protected', False)]:
            add_analysis(name)
            key = client.post('/api/downloads', json={'analysis_id': name}).json()['id']
            job = client.get(f'/api/downloads/{key}').json()
            assert job['status'] == 'error' and job['retryable'] is can_retry
            assert (tmp_path / key / 'source.mp4.part').exists() is can_retry
            if not can_retry:
                assert client.post(f'/api/downloads/{key}/resume').status_code == 409


def test_queue_cap_and_duplicate_submission(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'worker', waiting_worker)
    monkeypatch.setattr(main, 'MAX_PENDING', 3)
    with TestClient(main.app, base_url='http://localhost') as client:
        for i in range(3):
            add_analysis(str(i))
            assert client.post('/api/downloads', json={'analysis_id': str(i)}).status_code == 202
        add_analysis('overflow')
        assert client.post('/api/downloads', json={'analysis_id': 'overflow'}).status_code == 429
        assert client.post('/api/downloads', json={'analysis_id': '0'}).status_code == 202
        assert len(client.get('/api/downloads').json()['jobs']) == 3


def test_expiry_is_based_on_finish_and_applies_before_download(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    with TestClient(main.app, base_url='http://localhost') as client:
        job = Job(uuid4().hex, 'https://example.com/video', 'done', None, created=time.time() - 2 * TTL)
        main.jobs[job.id] = job
        (tmp_path / job.id).mkdir()
        (tmp_path / job.id / 'video.mp4').write_bytes(b'media')
        main.finish(job, 'complete', 'Ready')
        main.cleanup_expired()
        assert client.get(f'/api/downloads/{job.id}/file').status_code == 200
        job.expires_at = time.time() - 1
        assert client.get(f'/api/downloads/{job.id}/file').status_code == 404
        assert not (tmp_path / job.id).exists()
        assert not (tmp_path / f'{job.id}.json').exists()


def test_store_recovers_crash_and_cleans_corrupt_orphaned_records(tmp_path):
    store = JobStore(tmp_path)
    running = Job(uuid4().hex, 'https://example.com/video?token=secret', 'partial', None, status='processing')
    store.save(running)
    (tmp_path / running.id).mkdir()
    (tmp_path / running.id / 'source.mp4.part').write_bytes(b'partial')
    orphan, corrupt = uuid4().hex, uuid4().hex
    (tmp_path / orphan).mkdir()
    (tmp_path / f'{corrupt}.json').write_text('{invalid-json')
    loaded = store.load()
    assert loaded[running.id].status == 'paused'
    assert loaded[running.id].retryable
    assert (tmp_path / running.id / 'source.mp4.part').exists()
    assert not (tmp_path / orphan).exists()
    assert not (tmp_path / f'{corrupt}.json').exists()
    assert (tmp_path / f'{running.id}.json').stat().st_mode & 0o777 == 0o600
    assert 'url' not in loaded[running.id].public()


def test_size_error_reaches_api_record_and_log(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    async def oversized(payload, *args, **kwargs):
        (Path(payload['directory']) / 'source.part').write_bytes(b'partial')
        raise MediaError('size_limit', 'Kaynak 500 MB sınırını aşıyor. Daha düşük kalite seç.')
    monkeypatch.setattr(main, 'worker', oversized)
    with TestClient(main.app, base_url='http://localhost') as client:
        add_analysis('large')
        key = client.post('/api/downloads', json={'analysis_id': 'large'}).json()['id']
        job = client.get(f'/api/downloads/{key}').json()
        assert job['status'] == 'error' and job['code'] == 'size_limit'
        assert not job['retryable'] and not (tmp_path / key).exists()
        assert client.get(f'/api/downloads/{key}/file').status_code == 409
        assert json.loads((tmp_path / f'{key}.json').read_text())['code'] == 'size_limit'
        entries = [json.loads(line) for line in (tmp_path / 'logs/events.jsonl').read_text().splitlines()]
        assert any(e['event'] == 'job_failed' and e['job_id'] == key and e['code'] == 'size_limit' for e in entries)


def test_full_disk_does_not_leave_queued_job_or_unhandled_task(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    with TestClient(main.app, base_url='http://localhost') as client:
        def full_disk(*args, **kwargs):
            raise OSError(errno.ENOSPC, 'private-path')
        monkeypatch.setattr(JobStore, 'save', full_disk)
        add_analysis('full')
        response = client.post('/api/downloads', json={'analysis_id': 'full'})
        assert response.status_code == 422 and response.json()['code'] == 'disk_full'
        job = client.get('/api/downloads').json()['jobs'][0]
        assert job['status'] == 'error' and job['retryable']
        assert not main.tasks
        assert 'private-path' not in (tmp_path / 'logs/events.jsonl').read_text()
