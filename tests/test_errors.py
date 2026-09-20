import pytest
import errno
import subprocess

from app.errors import describe_error


@pytest.mark.parametrize('detail, code, retryable', [
    ('HTTP Error 403: Forbidden', 'access_denied', False),
    ('HTTP Error 404: not found', 'not_found', False),
    ('HTTP Error 429: Too Many Requests', 'rate_limited', True),
    ('HTTP Error 503', 'network', True),
    ('Login required', 'authentication', False),
    ('Unsupported URL', 'unsupported', False),
    ('DRM protected', 'protected', False),
    ('Connection reset by peer', 'network', True),
])
def test_source_errors_are_actionable_and_redacted(detail, code, retryable):
    result = describe_error(Exception(f'{detail}: https://example.com/video?token=secret'))
    assert result.code == code
    assert result.retryable is retryable
    assert 'secret' not in result.message
    assert 'https://' not in result.message


@pytest.mark.parametrize('error,code,retryable', [
    (TimeoutError(), 'timeout', True),
    (subprocess.TimeoutExpired(['ffmpeg', 'secret'], 30), 'timeout', True),
    (OSError(errno.ENOSPC, 'secret'), 'disk_full', True),
    (OSError(errno.EDQUOT, 'secret'), 'disk_full', True),
    (PermissionError('secret'), 'storage_permission', True),
    (subprocess.CalledProcessError(1, ['ffmpeg', 'secret']), 'conversion_failed', False),
    (ValueError('Parser failed at https://example.com?token=secret'), 'source_failed', True),
])
def test_internal_errors_are_classified_without_raw_messages(error, code, retryable):
    result = describe_error(error)
    assert result.code == code and result.retryable is retryable
    assert 'secret' not in result.message and 'https://' not in result.message
