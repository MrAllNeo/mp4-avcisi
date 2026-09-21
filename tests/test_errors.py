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
    ('Redirection detected; the video may be deleted or require login', 'access_denied', False),
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


def wrapped(cause):
    from yt_dlp.utils import DownloadError, ExtractorError
    inner = ExtractorError('Source failed at https://example.com?token=secret', cause=cause)
    return DownloadError('Video lookup failed', exc_info=(type(inner), inner, None))


def test_saved_ytdlp_http_cause_is_classified():
    from urllib.error import HTTPError
    error = wrapped(HTTPError('https://example.com?token=secret', 403, 'Denied', {}, None))
    result = describe_error(error)
    assert result.code == 'access_denied'
    assert 'secret' not in result.message


def test_parser_failure_has_distinct_code():
    import json
    assert describe_error(wrapped(json.JSONDecodeError('secret', 'secret', 0))).code == 'source_parse'
    assert describe_error(Exception('Unable to extract video URL from secret')).code == 'source_parse'


def test_nested_tls_error_is_distinct_and_does_not_disable_validation():
    import ssl
    assert describe_error(wrapped(ssl.SSLCertVerificationError('secret'))).code == 'tls_failed'


def test_hidden_access_control_takes_priority_over_parser_failure():
    from yt_dlp.utils import DownloadError
    error = DownloadError('Unable to extract video', exc_info=(Exception, Exception('CAPTCHA required'), None))
    assert describe_error(error).code == 'bot_blocked'


def test_exception_cycles_are_bounded():
    from app.errors import error_chain
    error = Exception('secret')
    error.__cause__ = error
    assert list(error_chain(error)) == [error]
    assert describe_error(error).code == 'source_failed'
