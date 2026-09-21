"""Bounded, private JSON logs. Never serialize URLs, payloads or exception text.

Only the server writes the rotating file. Workers send diagnostics over their
JSON pipe so multiple downloads cannot race during log rotation.
"""
from datetime import datetime, timezone
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import traceback

from app.errors import error_chain, http_status

logger = logging.getLogger('mp4.diagnostics')
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(logging.NullHandler())

CHOICES = {
    'resource': {'source_page', 'embedded_page', 'metadata', 'manifest', 'media', 'other'},
    'method': {'GET', 'HEAD', 'POST', 'PUT', 'OPTIONS', 'DELETE', 'PATCH'},
    'origin_relation': {'same', 'other'},
    'source_hint': {'generic_fallback', 'browser_transport_unavailable', 'unknown'},
    'route': {'direct', 'proton'},
    'stage': {'startup', 'extract', 'download', 'probe', 'remux', 'transcode', 'finalize', 'storage'},
    'mode': {'analyze', 'download'},
    'status': {'queued', 'processing', 'paused', 'cancelled', 'complete', 'error'},
    'basis': {'downloaded', 'content_length', 'source_file', 'temporary_files'},
    'reason': {'no_space', 'invalid_data', 'unsupported_codec', 'missing_stream', 'unknown'},
}
COUNTS = {'downloaded_bytes', 'total_bytes', 'estimated_bytes', 'limit_bytes', 'size',
          'height', 'percent', 'elapsed_ms', 'timeout_seconds', 'returncode', 'stderr_bytes',
          'errno', 'line', 'count', 'request_number', 'cookie_count', 'attempt'}
EVENTS = set('server_started server_stopped job_queued job_started job_finished job_failed '
             'job_expired job_task_failed storage_failed cleanup_failed worker_started '
             'worker_finished worker_failed worker_cancelled worker_stderr worker_protocol_error '
             'stage_started progress size_limit source_warning source_error ffmpeg_finished '
             'ffmpeg_failed legacy_failure vpn_starting vpn_connected vpn_stopped '
             'vpn_cleanup_failed vpn_fallback vpn_unconfigured engine_ready '
             'request_finished request_failed routing_failed browser_transport '
             'analysis_reused'.split())


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')

    def handleError(self, record):
        # A full disk must not turn a useful media error into a logging error,
        # nor let logging's fallback print the original LogRecord.
        print('Diagnostic log unavailable (write/rotation failed).', file=sys.stderr)


def configure(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    handler = PrivateRotatingHandler(root / 'events.jsonl', maxBytes=2 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter('%(message)s'))
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)


def exception_fields(exc):
    # Code locations only: no source lines, locals, command arguments or messages.
    return {
        'exception_type': type(exc).__name__,
        'errno': getattr(exc, 'errno', None),
        'causes': [{'exception_type': type(cause).__name__, 'http_status': http_status(cause)}
                   for cause in error_chain(exc)],
        'stack': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                  for frame in traceback.extract_tb(exc.__traceback__)[-12:]],
    }


def safe_fields(fields):
    result = {}
    for key, value in fields.items():
        if key in CHOICES and isinstance(value, str) and value in CHOICES[key]:
            result[key] = value
        elif key in COUNTS and (type(value) is int or (type(value) is float and math.isfinite(value))):
            result[key] = value
        elif key in {'job_id', 'operation_id', 'request_id'} and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value):
            result[key] = value
        elif key == 'target_ref' and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{16}', value):
            result[key] = value
        elif key == 'engine_version' and isinstance(value, str) and re.fullmatch(r'[0-9.]{1,32}', value):
            result[key] = value
        elif key == 'http_status' and type(value) is int and 100 <= value <= 599:
            result[key] = value
        elif key in {'code', 'exception_type'} and isinstance(value, str) and re.fullmatch(r'[A-Za-z_]{1,64}', value):
            result[key] = value
        elif key in {'retryable', 'redirected', 'has_referer', 'user_agent_changed', 'enabled'} and isinstance(value, bool):
            result[key] = value
        elif key == 'causes' and isinstance(value, list):
            result[key] = [{k: v for k, v in cause.items()
                            if (k == 'http_status' and type(v) is int and 400 <= v <= 599)
                            or (k == 'exception_type' and isinstance(v, str)
                                and re.fullmatch(r'[A-Za-z_]{1,64}', v))}
                           for cause in value[:8] if isinstance(cause, dict)]
        elif key == 'stack' and isinstance(value, list):
            result[key] = [{k: v for k, v in frame.items()
                            if (k == 'line' and type(v) is int) or
                            (k in {'file', 'function'} and isinstance(v, str)
                             and re.fullmatch(r'[\w.<>-]{1,100}', v))}
                           for frame in value[-12:] if isinstance(frame, dict)]
    return result


def record(event, *, exc=None, **fields):
    if not isinstance(event, str) or event not in EVENTS:
        return
    if exc is not None:
        fields.update(exception_fields(exc))
    entry = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **safe_fields(fields)}
    logger.info(json.dumps(entry, ensure_ascii=False, allow_nan=False))
