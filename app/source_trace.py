"""Observe yt-dlp requests without changing transport, cookies or headers."""
import hashlib
import secrets
import time
from urllib.parse import urlsplit

from app.errors import describe_error, error_chain, http_status


def resource_kind(url, source, content_type=''):
    target = urlsplit(url)
    path = target.path.lower()
    media_type = content_type.split(';', 1)[0].strip().lower()
    if path.endswith(('.m3u8', '.mpd')) or media_type in {
        'application/vnd.apple.mpegurl', 'application/x-mpegurl', 'application/dash+xml',
    }:
        return 'manifest'
    if path.endswith(('.mp4', '.webm', '.m4a', '.mp3', '.ts', '.m4s')) or media_type.startswith(('video/', 'audio/')):
        return 'media'
    if media_type == 'application/json' or path.endswith('.json'):
        return 'metadata'
    if target._replace(fragment='') == urlsplit(source)._replace(fragment=''):
        return 'source_page'
    if media_type == 'text/html':
        return 'embedded_page'
    return 'other'


def trace_requests(downloader, source, record):
    """One trace per worker; target hashes cannot be correlated across workers.

    Success logs are capped at 40 requests (HLS may make thousands). Failures
    remain visible. Bodies, headers, cookies and URL components are never logged.
    """
    original = downloader.urlopen
    salt = secrets.token_bytes(32)
    sequence = 0
    first_agent = None
    last_failed = False

    def urlopen(request):
        nonlocal sequence, first_agent, last_failed
        sequence += 1
        url = request if isinstance(request, str) else getattr(request, 'url', getattr(request, 'full_url', ''))
        method = getattr(request, 'method', None) or (request.get_method() if hasattr(request, 'get_method') else 'GET')
        headers = {key.lower(): value for key, value in downloader.params.get('http_headers', {}).items()}
        headers.update({key.lower(): value for key, value in getattr(request, 'headers', {}).items()})
        agent = headers.get('user-agent', '')
        if first_agent is None:
            first_agent = agent
        context = {
            'request_number': sequence,
            'target_ref': hashlib.sha256(salt + url.encode()).hexdigest()[:16],
            'method': method,
            'resource': resource_kind(url, source),
            'origin_relation': 'same' if urlsplit(url).netloc == urlsplit(source).netloc else 'other',
            'cookie_count': len(downloader.cookiejar),
            'has_referer': bool(headers.get('referer')),
            'user_agent_changed': agent != first_agent,
        }
        start = time.monotonic()
        try:
            response = original(request)
        except Exception as exc:
            last_failed = True
            status = next((http_status(cause) for cause in error_chain(exc) if http_status(cause)), None)
            # A JSON error body does not prove the requested resource was an API.
            details = {**context, 'http_status': status, 'code': describe_error(exc).code,
                       'elapsed_ms': round((time.monotonic() - start) * 1000)}
            record('request_failed', **details)
            raise
        else:
            if sequence <= 40 or last_failed:
                record('request_finished', **{**context,
                    'resource': resource_kind(url, source, response.headers.get('Content-Type', '')),
                    'http_status': response.status,
                    'redirected': response.url != url,
                    'elapsed_ms': round((time.monotonic() - start) * 1000)})
            last_failed = False
            return response

    downloader.urlopen = urlopen
