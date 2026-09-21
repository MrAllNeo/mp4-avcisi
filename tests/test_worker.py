import json
import io
import subprocess

import pytest

from app import worker
from app.errors import MediaError


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize('url', [
    'https://www.eporner.com/video-test/example/',
    'https://de.xhamster.com/videos/example-test',
    'https://xhamster20.desi/videos/example-test',
    'https://members.brazzers.com/video/example',
])
def test_public_browser_sites_use_hardened_transport(url):
    assert worker.browser_site(url)


@pytest.mark.parametrize('url', [
    'https://noteporner.com/video/test',
    'https://xhamster20.example/videos/test',
    'https://brazzers.com.attacker.example/video/test',
])
def test_similar_domains_do_not_get_browser_transport(url):
    assert not worker.browser_site(url)


@pytest.mark.parametrize('known', [None, worker.MAX_BYTES])
def test_inaccurate_estimate_does_not_reject_small_download(known, capsys):
    hook = worker.make_progress_hook()
    hook({'status': 'downloading', 'downloaded_bytes': 1024,
          'total_bytes': known, 'total_bytes_estimate': worker.MAX_BYTES * 10})
    emitted = events(capsys)
    assert not any(e.get('name') == 'size_limit' for e in emitted)
    details = next(e['fields'] for e in emitted if e.get('name') == 'progress')
    assert details['estimated_bytes'] == worker.MAX_BYTES * 10
    assert emitted[-1]['event'] == 'progress'


@pytest.mark.parametrize('field,basis', [('downloaded_bytes', 'downloaded'), ('total_bytes', 'content_length')])
def test_actual_size_limit_has_actionable_error_and_exact_diagnostics(field, basis, capsys):
    data = {'status': 'downloading', 'downloaded_bytes': 0, field: worker.MAX_BYTES + 1}
    with pytest.raises(MediaError) as caught:
        worker.make_progress_hook()(data)
    assert caught.value.code == 'size_limit'
    assert not caught.value.retryable
    assert 'Daha düşük kalite' in caught.value.message
    details = events(capsys)[-1]['fields']
    assert details['basis'] == basis
    assert details['size'] == worker.MAX_BYTES + 1
    assert details['limit_bytes'] == worker.MAX_BYTES


def test_exact_limit_allowed_and_progress_is_throttled(monkeypatch, capsys):
    monkeypatch.setattr(worker.time, 'monotonic', lambda: 5)
    hook = worker.make_progress_hook()
    for size in [0, 1, worker.MAX_BYTES]:
        hook({'status': 'downloading', 'downloaded_bytes': size, 'total_bytes': worker.MAX_BYTES})
    hook({'status': 'finished', 'downloaded_bytes': worker.MAX_BYTES, 'total_bytes': worker.MAX_BYTES})
    emitted = events(capsys)
    assert len([e for e in emitted if e.get('name') == 'progress']) == 2
    assert emitted[-1]['percent'] == 100


@pytest.mark.parametrize('stage,stderr,code,retryable', [
    ('transcode', b'Invalid data https://secret.example?token=hidden', 'conversion_failed', False),
    ('transcode', b'No space left on device /private/secret', 'disk_full', True),
])
def test_ffmpeg_failure_is_classified_without_stderr_leak(stage, stderr, code, retryable, monkeypatch, capsys):
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, b'', stderr))
    with pytest.raises(MediaError) as caught:
        worker.run_ffmpeg(stage, ['ffmpeg', '-i', 'private-source'], timeout=1, check=True)
    assert caught.value.code == code and caught.value.retryable is retryable
    raw = capsys.readouterr().out
    assert 'secret' not in raw and 'hidden' not in raw and 'private-source' not in raw
    fields = json.loads(raw.splitlines()[-1])['fields']
    assert fields['returncode'] == 1 and fields['stage'] == stage


def test_remux_failure_allows_transcode_fallback(monkeypatch, capsys):
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, b'', b'not supported'))
    result = worker.run_ffmpeg('remux', ['ffmpeg'], timeout=1)
    assert result.returncode == 1
    assert events(capsys)[-1]['fields']['reason'] == 'unsupported_codec'


def test_ffmpeg_timeout_keeps_stage_and_code(monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(['ffmpeg', 'secret-path'], 1, stderr=b'secret-token')
    monkeypatch.setattr(worker.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_ffmpeg('transcode', ['ffmpeg'], timeout=1, check=True)
    raw = capsys.readouterr().out
    assert 'secret' not in raw
    fields = json.loads(raw.splitlines()[-1])['fields']
    assert fields['stage'] == 'transcode' and fields['code'] == 'timeout'


@pytest.mark.parametrize('transcode_ok', [True, False])
def test_pipeline_falls_back_to_transcode_and_only_reports_valid_result(tmp_path, monkeypatch, capsys, transcode_ok):
    import yt_dlp
    class Downloader:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            (tmp_path / 'source.webm').write_bytes(b'fixture')
            return {'title': 'fixture'}
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', Downloader)
    monkeypatch.setattr(worker, 'trace_requests', lambda *args: None)
    monkeypatch.setattr(worker, 'guard_network', lambda: None)
    monkeypatch.setattr(worker, 'get_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download', 'url': 'https://example.com/sample', 'directory': str(tmp_path),
    })))
    commands = []
    def ffmpeg(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, b'', b'Duration: 00:00:02.00')
        if len(commands) == 2:
            (tmp_path / 'video.mp4').write_bytes(b'broken-remux')
            return subprocess.CompletedProcess(command, 1, b'', b'codec not supported')
        assert command[command.index('-c:v') + 1] == 'libx264'
        assert '-y' in command and command[command.index('-c:a') + 1] == 'aac'
        if transcode_ok:
            (tmp_path / 'video.mp4').write_bytes(b'converted-fixture')
        return subprocess.CompletedProcess(command, 0 if transcode_ok else 1, b'', b'')
    monkeypatch.setattr(worker.subprocess, 'run', ffmpeg)
    if transcode_ok:
        worker.run()
        assert not (tmp_path / 'source.webm').exists()
    else:
        with pytest.raises(MediaError, match='dönüştürülemedi'):
            worker.run()
        assert (tmp_path / 'source.webm').exists()
    emitted = events(capsys)
    assert len(commands) == 3
    assert any(e.get('fields', {}).get('stage') == 'transcode' for e in emitted)
    assert any(e['event'] == 'result' for e in emitted) is transcode_ok


def test_stale_browser_manifest_is_refreshed_once(tmp_path, monkeypatch, capsys):
    import yt_dlp

    worker.private_file(tmp_path / worker.PLAN_FILE, {
        'webpage_url': 'https://www.pornhub.com/view_video.php?viewkey=test',
        'extractor': 'PornHub',
    })
    calls = []

    class Downloader:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def process_ie_result(self, plan, download):
            calls.append('cached')
            (tmp_path / 'source.mp4.part').write_bytes(b'stale')
            raise yt_dlp.utils.DownloadError('HTTP Error 410: Gone')
        def extract_info(self, url, download):
            calls.append('fresh')
            (tmp_path / 'source.mp4').write_bytes(b'fixture')
            return {'title': 'fixture'}

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', Downloader)
    monkeypatch.setattr(worker, 'trace_requests', lambda *args: None)
    monkeypatch.setattr(worker, 'guard_network', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'get_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download',
        'url': 'https://www.pornhub.com/view_video.php?viewkey=test',
        'directory': str(tmp_path),
    })))

    commands = []
    def ffmpeg(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, b'', b'Duration: 00:00:02.00')
        (tmp_path / 'video.mp4').write_bytes(b'mp4')
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(worker.subprocess, 'run', ffmpeg)

    worker.run()

    emitted = events(capsys)
    assert calls == ['cached', 'fresh']
    assert not (tmp_path / 'source.mp4.part').exists()
    assert any(event.get('name') == 'analysis_refresh' for event in emitted)
    assert any(event.get('event') == 'result' for event in emitted)
