import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from yt_dlp.networking import Request

from app.diagnostics import safe_fields
from app.source_trace import trace_requests


def downloader(response=None):
    return SimpleNamespace(params={'http_headers': {'User-Agent': 'test-agent'}}, cookiejar=[],
                           urlopen=lambda req: response or SimpleNamespace(status=200, url=req.url, headers={}))


def test_trace_preserves_request_response_and_redacts_private_values():
    url = 'https://example.com/video.mp4?token=secret'
    response = SimpleNamespace(status=200, url=url, headers={'Content-Type': 'video/mp4', 'Set-Cookie': 'secret'})
    client = downloader(response)
    events, seen = [], []
    client.urlopen = lambda req: (seen.append(req) or response)
    trace_requests(client, url, lambda event, **fields: events.append({'event': event, **safe_fields(fields)}))
    request = Request(url, headers={'Cookie': 'secret', 'Authorization': 'secret', 'Referer': 'https://private.example/secret'})
    assert client.urlopen(request) is response and seen == [request]
    assert request.headers['Cookie'] == 'secret'
    assert events[0]['resource'] == 'media' and events[0]['has_referer']
    raw = json.dumps(events)
    assert not any(secret in raw for secret in ['secret', 'example.com', 'private.example', 'https://'])


def test_head_success_does_not_hide_get_failure_or_read_error_body():
    url = 'https://example.com/page?token=secret'
    client = downloader()
    events = []
    body = io.BytesIO(b'secret-response-body')
    error = HTTPError(url, 403, 'secret', {}, body)
    def respond(req):
        if req.method == 'HEAD':
            return SimpleNamespace(status=200, url=url, headers={'Content-Type': 'text/html'})
        raise error
    client.urlopen = respond
    trace_requests(client, url, lambda event, **fields: events.append({'event': event, **safe_fields(fields)}))
    assert client.urlopen(Request(url, method='HEAD')).status == 200
    with pytest.raises(HTTPError):
        client.urlopen(Request(url))
    assert [(e['method'], e['http_status']) for e in events] == [('HEAD', 200), ('GET', 403)]
    assert events[-1]['resource'] == 'source_page'
    assert events[0]['target_ref'] == events[1]['target_ref']
    assert body.tell() == 0 and 'secret' not in json.dumps(events)


def test_success_trace_is_bounded():
    client, events = downloader(), []
    trace_requests(client, 'https://example.com', lambda event, **fields: events.append(fields))
    for i in range(100):
        client.urlopen(Request(f'https://example.com/{i}.ts'))
    assert len(events) == 40


def test_success_after_failure_is_reported_even_after_normal_log_cap():
    client, events = downloader(), []
    count = 0
    def respond(req):
        nonlocal count
        count += 1
        if count == 41:
            raise HTTPError(req.url, 503, 'secret', {}, None)
        return SimpleNamespace(status=200, url=req.url, headers={})
    client.urlopen = respond
    trace_requests(client, 'https://example.com', lambda event, **fields: events.append(event))
    for _ in range(40):
        client.urlopen(Request('https://example.com/segment.ts'))
    with pytest.raises(HTTPError):
        client.urlopen(Request('https://example.com/segment.ts'))
    client.urlopen(Request('https://example.com/segment.ts'))
    assert events[-2:] == ['request_failed', 'request_finished']


def test_target_hashes_are_private_to_each_trace():
    refs = []
    for _ in range(2):
        client = downloader()
        trace_requests(client, 'https://example.com', lambda event, **fields: refs.append(fields['target_ref']))
        client.urlopen(Request('https://example.com'))
    assert refs[0] != refs[1]
