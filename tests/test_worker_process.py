"""Exercise the actual pipe, timeout and process cleanup boundaries, offline."""
import asyncio
import json
import sys

import pytest

from app import diagnostics, main
from app.errors import MediaError


@pytest.fixture
def stub_worker(monkeypatch, tmp_path):
    diagnostics.configure(tmp_path / 'logs')
    original = asyncio.create_subprocess_exec
    processes = []
    def install(script):
        async def spawn(*args, **kwargs):
            assert kwargs['start_new_session'] is True
            process = await original(sys.executable, '-c', 'import sys,json,time\nsys.stdin.readline()\n' + script, **kwargs)
            processes.append(process)
            return process
        monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', spawn)
    return install, processes, tmp_path / 'logs/events.jsonl'


def test_success_drains_large_stderr_and_correlates_log(stub_worker):
    install, processes, log = stub_worker
    install('sys.stderr.write("secret-token=" * 100000)\nsys.stderr.flush()\n'
            'print(json.dumps({"event":"diagnostic","name":"stage_started","fields":{"stage":"remux","url":"secret-url"}}),flush=True)\n'
            'print(json.dumps({"event":"result","size":123}),flush=True)')
    result = asyncio.run(main.worker({'mode': 'download', 'job_id': 'a' * 32}, timeout=5))
    assert result['size'] == 123 and processes[0].returncode == 0
    raw = log.read_text()
    assert 'secret' not in raw
    entries = [json.loads(line) for line in raw.splitlines()]
    assert len({e['operation_id'] for e in entries}) == 1
    assert all(e['job_id'] == 'a' * 32 for e in entries)
    assert next(e for e in entries if e['event'] == 'worker_finished')['stage'] == 'remux'
    assert next(e for e in entries if e['event'] == 'worker_stderr')['stderr_bytes'] > 1000000


@pytest.mark.parametrize('script,code', [
    ('print("secret invalid json",flush=True)', 'worker_protocol'),
    ('print("[]",flush=True)', 'worker_protocol'),
    ('print(json.dumps({"event":[]}),flush=True)', 'worker_protocol'),
    ('print(json.dumps({"event":"diagnostic","name":[],"fields":{}}),flush=True)', 'worker_protocol'),
    ('print(json.dumps({"event":"progress","message":"Safe","percent":"secret"}),flush=True)', 'worker_protocol'),
    ('print(json.dumps({"event":"result"}),flush=True)', 'worker_protocol'),
    ('print(json.dumps({"event":"unknown"}),flush=True)', 'worker_protocol'),
    ('print("x" * 1000000,flush=True)', 'worker_protocol'),
    ('sys.exit(7)', 'worker_stopped'),
    ('sys.exit(0)', 'worker_stopped'),
    ('print(json.dumps({"event":"result","size":123}),flush=True)\nsys.exit(3)', 'worker_stopped'),
    ('print(json.dumps({"event":"error","code":"size_limit","message":"Daha düşük kalite seç.","retryable":False}),flush=True)', 'size_limit'),
])
def test_bad_or_failed_worker_is_actionable_and_reaped(stub_worker, script, code):
    install, processes, log = stub_worker
    install(script)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'download'}, timeout=5))
    assert caught.value.code == code
    assert processes[0].returncode is not None
    assert 'secret' not in log.read_text()
    assert any(json.loads(line).get('code') == code for line in log.read_text().splitlines())


def test_timeout_kills_and_reaps_worker(stub_worker):
    install, processes, log = stub_worker
    install('time.sleep(30)')
    with pytest.raises(TimeoutError):
        asyncio.run(main.worker({'mode': 'download'}, timeout=0.15))
    assert processes[0].returncode == -9
    assert any(json.loads(line).get('code') == 'timeout' for line in log.read_text().splitlines())


def test_cancellation_kills_and_reaps_worker(stub_worker):
    install, processes, log = stub_worker
    install('time.sleep(30)')
    async def cancel():
        task = asyncio.create_task(main.worker({'mode': 'download'}, timeout=5))
        async with asyncio.timeout(3):
            while not processes:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert processes[0].returncode == -9
    assert any(json.loads(line)['event'] == 'worker_cancelled' for line in log.read_text().splitlines())


def test_temporary_disk_budget_stops_worker(stub_worker, tmp_path, monkeypatch):
    install, processes, log = stub_worker
    install('time.sleep(30)')
    directory = tmp_path / 'media'
    directory.mkdir()
    (directory / 'source.part').write_bytes(b'x' * 20)
    monkeypatch.setattr(main, 'MAX_DISK', 10)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'download', 'directory': str(directory)}, timeout=5))
    assert caught.value.code == 'size_limit'
    assert processes[0].returncode == -9
    assert any(json.loads(line).get('basis') == 'temporary_files' for line in log.read_text().splitlines())


def test_spawn_error_is_classified_without_leaking_paths(monkeypatch, tmp_path):
    diagnostics.configure(tmp_path)
    async def failed_spawn(*args, **kwargs):
        raise FileNotFoundError('secret-path')
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', failed_spawn)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'analyze'}))
    assert caught.value.code == 'worker_start_failed' and caught.value.retryable
    assert 'secret-path' not in (tmp_path / 'events.jsonl').read_text()
