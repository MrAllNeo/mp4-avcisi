"""One media operation per subprocess; stdout is newline-delimited JSON."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit

from app.diagnostics import exception_fields, safe_fields
from app.network import guard_network, validate_url
from app.media import get_ffmpeg, get_ffprobe
from app.errors import MediaError, describe_error
from app.source_trace import trace_requests
from app.probe import run_ffprobe
from app.strategy import ProcessingPlan, Strategy, ffmpeg_args_for, select_strategy
from app.cost import actual_cost

STRATEGY_MESSAGES = {
    Strategy.REMUX: "MP4 dosyası hazırlanıyor…",
    Strategy.MERGE_COPY: "MP4 dosyası hazırlanıyor…",
    Strategy.AUDIO_TRANSCODE: "Ses MP4 uyumlu hâle getiriliyor…",
    Strategy.VIDEO_TRANSCODE: "Video MP4 biçimine dönüştürülüyor…",
    Strategy.FULL_TRANSCODE: "Video MP4 biçimine dönüştürülüyor…",
}
STRATEGY_TIMEOUTS = {
    Strategy.REMUX: 180,
    Strategy.MERGE_COPY: 180,
    Strategy.AUDIO_TRANSCODE: 240,
    Strategy.VIDEO_TRANSCODE: 600,
    Strategy.FULL_TRANSCODE: 600,
}

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024


def configured_limit(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_BYTES = configured_limit("MP4_MAX_BYTES", DEFAULT_MAX_BYTES)
PLAN_FILE = "analysis.json"
COOKIE_FILE = "session.cookies"
BROWSER_DOMAINS = {
    "pornhub.com", "pornhub.net", "pornhub.org", "pornhubpremium.com",
    "xvideos.com", "xvideos2.com", "xvideos.es",
    "eporner.com", "xhamster.com", "xhamster.one", "xhamster.desi",
    "xhms.pro", "xhday.com", "xhvid.com", "brazzers.com",
}


def browser_site(url):
    """Browser impersonation is deliberately limited to supported public sites."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain) for domain in BROWSER_DOMAINS):
        return True
    # xHamster rotates numbered public domains (for example xhamster20.desi).
    return bool(re.fullmatch(r'(?:[^.]+\.)*xhamster\d+\.(?:com|desi)', host))


def private_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_plan(directory):
    path = directory / PLAN_FILE
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise MediaError("analysis_expired", "Video oturumu kullanılamıyor. Bağlantıyı yeniden analiz et.") from None
    if not isinstance(value, dict) or not value.get("webpage_url"):
        raise MediaError("analysis_expired", "Video oturumu kullanılamıyor. Bağlantıyı yeniden analiz et.")
    return value


def proxy_url(proxy):
    return (f"http://{quote(proxy['username'], safe='')}:{quote(proxy['password'], safe='')}@"
            f"{proxy['host']}:{proxy['port']}")


def secure_cookie_file(path):
    path = Path(path)
    if path.is_file() and not path.is_symlink():
        path.chmod(0o600)


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def diagnostic(name, **fields):
    emit(event="diagnostic", name=name, fields=safe_fields(fields))


def stage(name):
    diagnostic("stage_started", stage=name)


def check_size(size, basis, **fields):
    if size is not None and size > MAX_BYTES:
        diagnostic("size_limit", basis=basis, size=size, limit_bytes=MAX_BYTES, **fields)
        gibibyte = 1024 * 1024 * 1024
        label = f"{MAX_BYTES // gibibyte} GB" if MAX_BYTES % gibibyte == 0 else f"{MAX_BYTES // (1024 * 1024)} MB"
        raise MediaError("size_limit", f"Kaynak dosyası {label} sınırını aşıyor. Daha düşük kalite seçip yeniden indir.")


def make_progress_hook():
    last_progress = -1
    last_logged = None

    def progress(data):
        nonlocal last_progress, last_logged
        known = data.get("total_bytes")
        estimate = data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes", 0)
        metrics = {"downloaded_bytes": downloaded, "total_bytes": known, "estimated_bytes": estimate}
        # HLS estimates fluctuate as fragments arrive; they are not a hard limit.
        check_size(downloaded, "downloaded", **metrics)
        check_size(known, "content_length", **metrics)
        total = known or estimate
        percent = round(downloaded / total * 100) if total else None
        now = time.monotonic()
        if last_logged is None or now - last_logged >= 10 or data["status"] == "finished":
            diagnostic("progress", stage="download", percent=percent, **metrics)
            last_logged = now
        if percent != last_progress or data["status"] == "finished":
            emit(event="progress", percent=min(percent, 100) if percent is not None else None,
                 message="Video kaynağı indiriliyor…" if data["status"] != "finished" else "Ses ve görüntü hazırlanıyor…")
            last_progress = percent
    return progress


class SourceLogger:
    def debug(self, message):
        pass

    info = debug

    def warning(self, message):
        text = message.lower()
        hint = ('browser_transport_unavailable' if 'impersonat' in text and 'available' in text
                else 'generic_fallback' if 'generic' in text and 'falling back' in text else 'unknown')
        diagnostic("source_warning", code=describe_error(Exception(message)).code, source_hint=hint)

    def error(self, message):
        diagnostic("source_error", code=describe_error(Exception(message)).code)


def ffmpeg_reason(stderr):
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    for needle, reason in (("no space left", "no_space"), ("invalid data", "invalid_data"),
                           ("not supported", "unsupported_codec"), ("matches no streams", "missing_stream")):
        if needle in text:
            return reason
    return "unknown"


def run_ffmpeg(name, command, *, timeout, check=False):
    stage(name)
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        diagnostic("ffmpeg_failed", stage=name, timeout_seconds=timeout,
                   code=describe_error(exc).code, **exception_fields(exc))
        raise
    diagnostic("ffmpeg_finished", stage=name, returncode=result.returncode,
               elapsed_ms=round((time.monotonic() - started) * 1000),
               stderr_bytes=len(result.stderr), reason=ffmpeg_reason(result.stderr))
    if result.returncode and check:
        if ffmpeg_reason(result.stderr) == "no_space":
            raise MediaError("disk_full", "Diskte yeterli yer yok. Yer açtıktan sonra yeniden dene.", True)
        raise MediaError("conversion_failed", "Video MP4 biçimine dönüştürülemedi. Başka bir kalite dene.")
    return result


def summarize(info):
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = [entry for entry in info.get("entries", []) if entry]
        if not entries:
            raise MediaError("unsupported", "Bu sayfada indirilebilir video bulunamadı.")
        info = entries[0]
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise MediaError("live_unsupported", "Canlı yayınlar henüz desteklenmiyor. Tamamlanmış bir video seç.")
    formats = [f for f in info.get("formats", [info]) if not f.get("has_drm") and f.get("vcodec") != "none"]
    if info.get("has_drm") or not formats:
        raise MediaError("protected" if info.get("has_drm") else "unsupported", "Bu kaynakta korumasız, indirilebilir bir video bulunamadı.")
    heights = sorted({int(f["height"]) for f in formats if f.get("height")}, reverse=True)
    return {
        "title": info.get("title") or "İsimsiz video",
        "duration": info.get("duration"),
        "source": info.get("extractor_key") or info.get("extractor") or "Video kaynağı",
        "qualities": heights,
        "size": info.get("filesize") or info.get("filesize_approx"),
    }


def run():
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
    from yt_dlp.version import __version__
    from yt_dlp.downloader.external import FFmpegFD

    payload = json.loads(sys.stdin.readline())
    diagnostic('engine_ready', engine_version=__version__)
    if payload.get('vpn_proxy'):
        guard_network(proxy=payload['vpn_proxy'])
    else:
        guard_network()
    # No subprocess may retrieve remote media outside the guarded Python sockets.
    def no_external_download(*args, **kwargs):
        raise MediaError("unsupported", "Bu akış türü henüz desteklenmiyor.")
    FFmpegFD.real_download = no_external_download

    url = validate_url(payload["url"])
    mode = payload["mode"]
    directory = Path(payload.get("directory", ".")).resolve()
    ffmpeg = get_ffmpeg()
    ffprobe = get_ffprobe()
    cookie_file = directory / COOKIE_FILE

    options = {
        "quiet": True,
        "logger": SourceLogger(),
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "skip_unavailable_fragments": False,
        "proxy": "",  # Never inherit an environment proxy that could reach private hosts.
        "cachedir": False,
        # Our hook raises a structured error; yt-dlp's max_filesize silently skips.
        "max_downloads": 1,
        "continuedl": True,
        "hls_prefer_native": True,
        "external_downloader": {"default": "native"},
        "ffmpeg_location": ffmpeg,
        "outtmpl": str(directory / "source.%(ext)s"),
        "merge_output_format": "mkv",
        "fixup": "never",  # Our final local MP4 pass handles container repair.
        "postprocessor_args": {"ffmpeg_i": ["-protocol_whitelist", "file,pipe"]},
        "progress_hooks": [make_progress_hook()],
        "restrictfilenames": True,
        "cookiefile": str(cookie_file),
    }
    # These public video sites use browser TLS checks, rotating domains or
    # incomplete certificate chains. Native curl remains scoped to known sites;
    # other URLs keep the guarded Python transport.
    if browser_site(url):
        options["impersonate"] = ImpersonateTarget.from_str("chrome")
        if payload.get("vpn_proxy"):
            # Native curl does not use the Python socket shim. Explicitly route
            # browser traffic through the isolated Gluetun proxy on VPN attempts.
            options["proxy"] = proxy_url(payload["vpn_proxy"])
        diagnostic("browser_transport", enabled=True, route=payload.get("route", "direct"))
    if mode == "analyze":
        stage("extract")
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                trace_requests(downloader, url, diagnostic)
                info = downloader.extract_info(url, download=False)
        finally:
            secure_cookie_file(cookie_file)
        private_file(directory / PLAN_FILE, yt_dlp.YoutubeDL.sanitize_info(info))
        emit(event="result", metadata=summarize(info))
        return

    height = payload.get("height")
    limit = f"[height<={int(height)}]" if height else ""
    # Accept native HTTP/HLS/DASH only. Reject live media and DRM before download.
    protocol = "[protocol~='^(https?|m3u8_native|http_dash_segments)$']"
    options["format"] = f"bv*{limit}{protocol}+ba{protocol}/b{limit}{protocol}"

    def match_filter(info, *, incomplete=False):
        if info.get("is_live") or info.get("has_drm"):
            raise MediaError("protected" if info.get("has_drm") else "live_unsupported", "Canlı veya DRM korumalı yayınlar desteklenmiyor.")
        if (info.get("duration") or 0) > 7200:
            raise MediaError("duration_limit", "İlk sürümde en fazla 2 saatlik videolar destekleniyor.")
        check_size(info.get("filesize"), "content_length")
        return None

    options["match_filter"] = match_filter
    stage("download")
    try:
        plan = load_plan(directory)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                trace_requests(downloader, url, diagnostic)
                try:
                    if plan:
                        diagnostic("analysis_reused", extractor=plan.get("extractor_key") or plan.get("extractor"))
                        downloader.process_ie_result(plan, download=True)
                    else:
                        downloader.extract_info(url, download=True)
                except yt_dlp.utils.MaxDownloadsReached:
                    # yt-dlp signals the one-video limit after a successful download too.
                    pass
        except yt_dlp.utils.DownloadError as exc:
            code = describe_error(exc).code
            if not plan or not browser_site(url) or code not in {"not_found", "access_denied"}:
                raise
            # Signed manifests and media URLs can become invalid immediately
            # after analysis. Refresh once with the same private cookies and
            # browser transport instead of claiming the video vanished.
            diagnostic("analysis_refresh", code=code)
            for partial in directory.glob("source.*"):
                if partial.is_file() and not partial.is_symlink():
                    partial.unlink(missing_ok=True)
            with yt_dlp.YoutubeDL(options) as downloader:
                trace_requests(downloader, url, diagnostic)
                try:
                    downloader.extract_info(url, download=True)
                except yt_dlp.utils.MaxDownloadsReached:
                    pass
    finally:
        secure_cookie_file(cookie_file)
    files = [p for p in directory.glob("source.*") if p.suffix not in {".part", ".ytdl", ".json"}]
    if len(files) != 1:
        raise MediaError("incomplete_download", "Video dosyası tamamlanamadı. Başka bir kalite veya bağlantı dene.", True)
    source = files[0]
    check_size(source.stat().st_size, "source_file")
    stage("probe")
    probe_result = run_ffprobe(source, ffprobe_binary=ffprobe, timeout=30)
    known_video_codecs = {'h264', 'hevc', 'vp9', 'av1', 'vp8', 'mpeg4'}
    known_audio_codecs = {'aac', 'mp3', 'opus', 'vorbis', 'ac3', 'eac3', 'flac', 'dts'}
    video_codec = probe_result.primary_video.codec if probe_result.primary_video else None
    audio_codec = probe_result.primary_audio.codec if probe_result.primary_audio else None
    diagnostic(
        "probe_finished",
        duration=probe_result.duration_seconds,
        video_codec=video_codec if video_codec in known_video_codecs else ("none" if video_codec is None else "other"),
        audio_codec=audio_codec if audio_codec in known_audio_codecs else ("none" if audio_codec is None else "other"),
    )
    if probe_result.duration_seconds is not None and probe_result.duration_seconds > 7200:
        raise MediaError("duration_limit", "İlk sürümde en fazla 2 saatlik videolar destekleniyor.")

    plan = select_strategy(probe_result, preference=payload.get("compatibility", "fast"))
    if plan.strategy == Strategy.REJECT:
        diagnostic("strategy_rejected", strategy_reason=plan.reason)
        raise MediaError("unsupported", "Bu videonun içeriği işlenemedi. Başka bir kalite veya bağlantı dene.")
    diagnostic("strategy_selected", strategy=plan.strategy.value, strategy_reason=plan.reason)

    target = directory / "video.mp4"
    if plan.strategy == Strategy.DIRECT:
        stage("finalize")
        os.replace(source, target)
    else:
        emit(event="progress", percent=None, message=STRATEGY_MESSAGES[plan.strategy])
        args = ffmpeg_args_for(plan, ffmpeg, source, target)
        result = run_ffmpeg(plan.strategy.value.lower(), args, timeout=STRATEGY_TIMEOUTS[plan.strategy])
        if result.returncode and plan.strategy != Strategy.FULL_TRANSCODE:
            # The classified cheap strategy failed in practice (ffmpeg itself
            # rejected it) — full re-encode is the one safety-net fallback.
            emit(event="progress", percent=None, message=STRATEGY_MESSAGES[Strategy.FULL_TRANSCODE])
            fallback = ProcessingPlan(Strategy.FULL_TRANSCODE, "fallback_after_failure")
            run_ffmpeg("transcode", ffmpeg_args_for(fallback, ffmpeg, source, target), check=True, timeout=600)
            plan = fallback
        elif result.returncode:
            raise MediaError("conversion_failed", "Video MP4 biçimine dönüştürülemedi. Başka bir kalite dene.")
        source.unlink()
    stage("finalize")
    if not target.exists() or not target.stat().st_size:
        raise MediaError("conversion_failed", "MP4 oluşturulamadı. Başka bir kalite dene.")
    emit(event="result", size=target.stat().st_size, strategy=plan.strategy.value,
         cost_weight=actual_cost(plan.strategy, probe_result))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Do not expose signed URLs, cookies or remote server responses to clients.
        error = describe_error(exc)
        diagnostic("worker_failed", code=error.code, retryable=error.retryable, **exception_fields(exc))
        emit(event="error", **error.public())
        sys.exit(1)
