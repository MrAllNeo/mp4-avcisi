"""ffprobe-based media analysis, replacing ad-hoc ffmpeg-stderr scraping.

Only ever probes a local file that this process already downloaded and owns;
never a remote URL. Output is small (stream/format metadata), so a byte cap
on stdout is a defensive guard, not a real limit in practice.
"""
from dataclasses import dataclass, field
import json
import subprocess

from app.errors import MediaError
from app.media import get_ffprobe

MAX_PROBE_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass
class VideoStreamInfo:
    codec: str | None
    codec_profile: str | None
    width: int | None
    height: int | None
    fps: float | None
    bitrate: int | None
    pixel_format: str | None
    hdr: bool


@dataclass
class AudioStreamInfo:
    codec: str | None
    sample_rate: int | None
    channels: int | None
    bitrate: int | None


@dataclass
class MediaProbeResult:
    container: str
    duration_seconds: float | None
    estimated_size_bytes: int | None
    overall_bitrate: int | None
    protocol: str
    seekable: bool
    video_streams: list[VideoStreamInfo] = field(default_factory=list)
    audio_streams: list[AudioStreamInfo] = field(default_factory=list)
    subtitle_streams: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary_video(self) -> VideoStreamInfo | None:
        return self.video_streams[0] if self.video_streams else None

    @property
    def primary_audio(self) -> AudioStreamInfo | None:
        return self.audio_streams[0] if self.audio_streams else None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fps(stream):
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not raw or raw == "0/0":
        return None
    try:
        numerator, denominator = raw.split("/")
        denominator = float(denominator)
        return round(float(numerator) / denominator, 3) if denominator else None
    except (ValueError, ZeroDivisionError):
        return None


def _is_hdr(stream):
    transfer = (stream.get("color_transfer") or "").lower()
    return transfer in {"smpte2084", "arib-std-b67"}


def parse_probe_json(payload: dict) -> MediaProbeResult:
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video_streams, audio_streams, subtitle_streams = [], [], []
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and stream.get("disposition", {}).get("attached_pic") != 1:
            video_streams.append(VideoStreamInfo(
                codec=stream.get("codec_name"),
                codec_profile=stream.get("profile"),
                width=_to_int(stream.get("width")),
                height=_to_int(stream.get("height")),
                fps=_fps(stream),
                bitrate=_to_int(stream.get("bit_rate")),
                pixel_format=stream.get("pix_fmt"),
                hdr=_is_hdr(stream),
            ))
        elif codec_type == "audio":
            audio_streams.append(AudioStreamInfo(
                codec=stream.get("codec_name"),
                sample_rate=_to_int(stream.get("sample_rate")),
                channels=_to_int(stream.get("channels")),
                bitrate=_to_int(stream.get("bit_rate")),
            ))
        elif codec_type == "subtitle":
            subtitle_streams.append({"codec": stream.get("codec_name")})

    warnings = []
    if not video_streams:
        warnings.append("no_video_stream")
    if not audio_streams:
        warnings.append("no_audio_stream")
    duration = _to_float(fmt.get("duration"))
    if duration is None:
        for stream in streams:
            duration = _to_float(stream.get("duration"))
            if duration is not None:
                break
        else:
            warnings.append("duration_unknown")

    return MediaProbeResult(
        container=fmt.get("format_name") or "unknown",
        duration_seconds=duration,
        estimated_size_bytes=_to_int(fmt.get("size")),
        overall_bitrate=_to_int(fmt.get("bit_rate")),
        protocol="file",
        seekable=True,
        video_streams=video_streams,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
        warnings=warnings,
    )


def run_ffprobe(path, *, ffprobe_binary=None, timeout=30) -> MediaProbeResult:
    ffprobe = ffprobe_binary or get_ffprobe()
    command = [
        ffprobe, "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise MediaError("probe_failed", "Video analiz edilemedi. Dosya bozuk olabilir.") from None
    if result.returncode:
        raise MediaError("probe_failed", "Video analiz edilemedi. Dosya bozuk olabilir.")
    if len(result.stdout) > MAX_PROBE_OUTPUT_BYTES:
        raise MediaError("probe_failed", "Video analiz çıktısı beklenenden büyük.")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise MediaError("probe_failed", "Video analiz edilemedi. Dosya bozuk olabilir.") from None
    if not isinstance(payload, dict):
        raise MediaError("probe_failed", "Video analiz edilemedi. Dosya bozuk olabilir.")
    return parse_probe_json(payload)
