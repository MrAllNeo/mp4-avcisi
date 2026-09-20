"""One media operation per subprocess; stdout is newline-delimited JSON."""

import json
from pathlib import Path
import re
import subprocess
import sys
import time

from app.diagnostics import exception_fields, safe_fields
from app.network import guard_network, validate_url
from app.media import get_ffmpeg
from app.errors import MediaError, describe_error

MAX_BYTES = 500 * 1024 * 1024


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def diagnostic(name, **fields):
    emit(event="diagnostic", name=name, fields=safe_fields(fields))


def stage(name):
    diagnostic("stage_started", stage=name)


def check_size(size, basis, **fields):
    if size is not None and size > MAX_BYTES:
        diagnostic("size_limit", basis=basis, size=size, limit_bytes=MAX_BYTES, **fields)
        raise MediaError("size_limit", "Kaynak dosyası 500 MB sınırını aşıyor. Daha düşük kalite seçip yeniden indir.")


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
        diagnostic("source_warning", code=describe_error(Exception(message)).code)

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
    from yt_dlp.downloader.external import FFmpegFD

    guard_network()
    # No subprocess may retrieve remote media outside the guarded Python sockets.
    def no_external_download(*args, **kwargs):
        raise MediaError("unsupported", "Bu akış türü henüz desteklenmiyor.")
    FFmpegFD.real_download = no_external_download

    payload = json.loads(sys.stdin.readline())
    url = validate_url(payload["url"])
    mode = payload["mode"]
    directory = Path(payload.get("directory", ".")).resolve()
    ffmpeg = get_ffmpeg()

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
    }
    if mode == "analyze":
        stage("extract")
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
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
    with yt_dlp.YoutubeDL(options) as downloader:
        try:
            downloader.extract_info(url, download=True)
        except yt_dlp.utils.MaxDownloadsReached:
            # yt-dlp signals the one-video limit after a successful download too.
            pass
    files = [p for p in directory.glob("source.*") if p.suffix not in {".part", ".ytdl", ".json"}]
    if len(files) != 1:
        raise MediaError("incomplete_download", "Video dosyası tamamlanamadı. Başka bir kalite veya bağlantı dene.", True)
    source = files[0]
    check_size(source.stat().st_size, "source_file")
    probe = run_ffmpeg("probe", [ffmpeg, "-nostdin", "-hide_banner", "-protocol_whitelist", "file,pipe",
                            "-i", str(source)], timeout=30)
    duration = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if duration:
        hours, minutes, seconds = map(float, duration.groups())
        if hours * 3600 + minutes * 60 + seconds > 7200:
            raise MediaError("duration_limit", "İlk sürümde en fazla 2 saatlik videolar destekleniyor.")
    emit(event="progress", percent=None, message="MP4 dosyası hazırlanıyor…")
    target = directory / "video.mp4"
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    result = run_ffmpeg("remux", base + ["-c", "copy", "-movflags", "+faststart", str(target)], timeout=180)
    if result.returncode:
        emit(event="progress", percent=None, message="Video MP4 biçimine dönüştürülüyor…")
        run_ffmpeg("transcode", base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                              "-c:a", "aac", "-movflags", "+faststart", str(target)],
                       check=True, timeout=600)
    stage("finalize")
    if not target.exists() or not target.stat().st_size:
        raise MediaError("conversion_failed", "MP4 oluşturulamadı. Başka bir kalite dene.")
    source.unlink()
    emit(event="result", size=target.stat().st_size)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Do not expose signed URLs, cookies or remote server responses to clients.
        error = describe_error(exc)
        diagnostic("worker_failed", code=error.code, retryable=error.retryable, **exception_fields(exc))
        emit(event="error", **error.public())
        sys.exit(1)
