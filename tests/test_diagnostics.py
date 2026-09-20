import json

from app import diagnostics


def test_private_logging_with_code_locations(tmp_path):
    diagnostics.configure(tmp_path / 'logs')
    try:
        raise ValueError('https://user:password@example.com/private?token=secret Authorization: Bearer hidden')
    except ValueError as exc:
        diagnostics.record('job_failed', job_id='a' * 32, code='source_failed', exc=exc,
                           url='https://example.com?token=secret', message=str(exc),
                           headers={'Cookie': 'session=hidden'}, title='private-title',
                           stage='https://example.com/secret')
    path = tmp_path / 'logs/events.jsonl'
    raw = path.read_text()
    entry = json.loads(raw)
    assert entry['job_id'] == 'a' * 32
    assert entry['exception_type'] == 'ValueError'
    assert entry['stack'][-1]['file'] == 'test_diagnostics.py'
    assert entry['stack'][-1]['line'] > 0
    for secret in ['https://', 'password', 'token', 'secret', 'Authorization', 'hidden', 'private-title']:
        assert secret not in raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_rotation_is_bounded_and_keeps_private_permissions(tmp_path):
    diagnostics.configure(tmp_path)
    handler = diagnostics.logger.handlers[0]
    handler.maxBytes = 300
    for i in range(30):
        diagnostics.record('progress', downloaded_bytes=i, total_bytes=100, job_id='b' * 32)
    paths = list(tmp_path.glob('events.jsonl*'))
    assert len(paths) == 4
    for path in paths:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_size <= 300
        assert all(json.loads(line)['event'] == 'progress' for line in path.read_text().splitlines())


def test_logging_write_failure_does_not_hide_original_error(tmp_path, monkeypatch, capsys):
    diagnostics.configure(tmp_path)
    def unavailable(*args):
        raise OSError('disk full secret')
    monkeypatch.setattr(diagnostics.logger.handlers[0], 'shouldRollover', unavailable)
    diagnostics.record('job_failed', code='disk_full')
    stderr = capsys.readouterr().err
    assert 'Diagnostic log unavailable' in stderr
    assert 'secret' not in stderr


def test_nested_http_status_is_logged_without_response_or_url(tmp_path):
    from urllib.error import HTTPError
    from yt_dlp.utils import DownloadError, ExtractorError
    cause = HTTPError('https://example.com?token=secret', 403, 'secret-response', {'Cookie': 'secret'}, None)
    inner = ExtractorError('secret-title', cause=cause)
    outer = DownloadError('secret', exc_info=(type(inner), inner, None))
    diagnostics.configure(tmp_path)
    diagnostics.record('worker_failed', exc=outer)
    raw = (tmp_path / 'events.jsonl').read_text()
    causes = json.loads(raw)['causes']
    assert any(c.get('exception_type') == 'HTTPError' and c.get('http_status') == 403 for c in causes)
    assert 'secret' not in raw and 'https://' not in raw and 'Cookie' not in raw


def test_cause_metadata_rejects_arbitrary_values():
    safe = diagnostics.safe_fields({'causes': [{'exception_type': 'https://secret', 'http_status': 'secret', 'message': 'secret'}]})
    assert safe == {'causes': [{}]}
