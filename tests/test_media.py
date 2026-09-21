"""Real yt-dlp + FFmpeg against tiny deterministic local media fixtures.

Network guard is disabled only within these tests so the loopback fixture can
be fetched. Production workers keep their socket guard; test_network covers it.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from app import worker
from app.media import get_ffmpeg
from app.errors import MediaError


class RangeHandler(SimpleHTTPRequestHandler):
    ranges = []
    session_requests = []
    session_page_requests = 0

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/session.html':
            type(self).session_page_requests += 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Set-Cookie', 'media_session=fixture-secret; Path=/; HttpOnly')
            self.end_headers()
            self.wfile.write(b'<html><title>Session fixture</title><video src="/session.mp4"></video></html>')
            return
        if path == '/session.mp4':
            type(self).session_requests.append(dict(self.headers))
            expected_referrer = f'http://{self.headers["Host"]}/session.html'
            if 'media_session=fixture-secret' not in self.headers.get('Cookie', '') or self.headers.get('Referer') != expected_referrer:
                self.send_error(403)
                return
            self.path = '/sample.mp4'
        requested_range = self.headers.get('Range')
        if urlsplit(self.path).path == '/sample.mp4' and requested_range:
            self.ranges.append(requested_range)
            content = (Path(self.directory) / 'sample.mp4').read_bytes()
            start = int(requested_range.removeprefix('bytes=').split('-')[0])
            self.send_response(206)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Range', f'bytes {start}-{len(content) - 1}/{len(content)}')
            self.send_header('Content-Length', str(len(content) - start))
            self.end_headers()
            self.wfile.write(content[start:])
        else:
            super().do_GET()


@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    ffmpeg = get_ffmpeg()
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=128x72:r=10:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(root / "sample.mp4")], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-i", str(root / "sample.mp4"), "-c", "copy", "-f", "hls",
                    "-hls_time", "1", "-hls_playlist_type", "vod", str(root / "sample.m3u8")], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-i", str(root / "sample.mp4"), "-c", "copy", "-f", "dash",
                    "-seg_duration", "1", str(root / "sample.mpd")], check=True)
    (root / "index.html").write_text('<html><title>Embedded sample</title><video controls src="sample.mp4"></video></html>')
    handler = partial(RangeHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("source", ["sample.mp4", "index.html", "sample.m3u8", "sample.mpd"])
def test_real_media_is_downloaded_merged_and_decodable(media_server, source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "guard_network", lambda: None)
    monkeypatch.setattr(worker, "validate_url", lambda url: url)
    payload = {"mode": "download", "url": f"{media_server}/{source}", "directory": str(tmp_path)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    worker.run()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "result"
    target = tmp_path / "video.mp4"
    assert target.read_bytes()[4:8] == b"ftyp"
    # Decode both streams; this catches truncated downloads and lost audio.
    subprocess.run([get_ffmpeg(), "-v", "error", "-i", str(target),
                    "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], check=True, capture_output=True)
    assert events[-1]["size"] == target.stat().st_size


def test_partial_download_resumes_with_http_range(media_server, tmp_path, monkeypatch, capsys):
    with urlopen(f'{media_server}/sample.mp4') as response:
        original = response.read()
    (tmp_path / 'source.mp4.part').write_bytes(original[:1024])
    RangeHandler.ranges.clear()
    monkeypatch.setattr(worker, 'guard_network', lambda: None)
    monkeypatch.setattr(worker, 'validate_url', lambda url: url)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download', 'url': f'{media_server}/sample.mp4', 'directory': str(tmp_path),
    })))
    worker.run()
    assert 'bytes=1024-' in RangeHandler.ranges
    target = tmp_path / 'video.mp4'
    assert target.read_bytes()[4:8] == b'ftyp'
    subprocess.run([get_ffmpeg(), '-v', 'error', '-i', str(target), '-map', '0:v:0',
                    '-map', '0:a:0', '-f', 'null', '-'], check=True, capture_output=True)


def test_public_page_session_and_referrer_survive_real_download(media_server, tmp_path, monkeypatch, capsys):
    RangeHandler.session_requests.clear()
    monkeypatch.setattr(worker, 'guard_network', lambda: None)
    monkeypatch.setattr(worker, 'validate_url', lambda url: url)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download', 'url': f'{media_server}/session.html', 'directory': str(tmp_path),
    })))
    worker.run()
    assert RangeHandler.session_requests
    assert len({r['User-Agent'] for r in RangeHandler.session_requests}) == 1
    raw = capsys.readouterr().out
    assert 'fixture-secret' not in raw and 'session.html' not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    media = [e['fields'] for e in events if e.get('name') == 'request_finished' and e['fields']['resource'] == 'media']
    assert media and all(e['cookie_count'] >= 1 and e['has_referer'] for e in media)
    assert all(not e['user_agent_changed'] for e in media)
    assert (tmp_path / 'video.mp4').read_bytes()[4:8] == b'ftyp'


def test_analysis_plan_reuses_session_without_refetching_source(media_server, tmp_path, monkeypatch, capsys):
    analysis = tmp_path / 'analysis'
    download = tmp_path / 'download'
    analysis.mkdir()
    download.mkdir()
    RangeHandler.session_requests.clear()
    RangeHandler.session_page_requests = 0
    monkeypatch.setattr(worker, 'guard_network', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'validate_url', lambda url: url)

    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'analyze', 'url': f'{media_server}/session.html', 'directory': str(analysis),
    })))
    worker.run()
    assert RangeHandler.session_page_requests == 1
    assert (analysis / worker.PLAN_FILE).stat().st_mode & 0o777 == 0o600
    assert (analysis / worker.COOKIE_FILE).stat().st_mode & 0o777 == 0o600

    for name in (worker.PLAN_FILE, worker.COOKIE_FILE):
        shutil.copyfile(analysis / name, download / name)
        (download / name).chmod(0o600)
    capsys.readouterr()
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download', 'url': f'{media_server}/session.html', 'directory': str(download),
    })))
    worker.run()

    assert RangeHandler.session_page_requests == 1
    assert RangeHandler.session_requests
    assert 'media_session=fixture-secret' in RangeHandler.session_requests[-1].get('Cookie', '')
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event.get('name') == 'analysis_reused' for event in events)
    assert (download / 'video.mp4').read_bytes()[4:8] == b'ftyp'


@pytest.mark.parametrize('source', ['sample.mp4', 'sample.m3u8'])
def test_real_download_size_limit_is_not_a_silent_skip(media_server, source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, 'guard_network', lambda: None)
    monkeypatch.setattr(worker, 'validate_url', lambda url: url)
    monkeypatch.setattr(worker, 'MAX_BYTES', 1024)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'mode': 'download', 'url': f'{media_server}/{source}', 'directory': str(tmp_path),
    })))
    with pytest.raises(MediaError) as caught:
        worker.run()
    assert caught.value.code == 'size_limit'
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e.get('name') == 'size_limit' for e in events)
    assert not any(e['event'] == 'result' for e in events)
    assert not (tmp_path / 'video.mp4').exists()
