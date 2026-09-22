"""Pure, deterministic processing-strategy selection (no I/O, no subprocess).

Given a probed source and a user preference, decide the cheapest ffmpeg
operation that produces a valid MP4, instead of unconditionally running a
remux-then-full-transcode pass on every download.
"""
from dataclasses import dataclass
from enum import Enum

from app.probe import MediaProbeResult

DEFAULT_MAX_DURATION_SECONDS = 7200

# "fast" (Hızlı/Orijinal): keep the source codec whenever ffmpeg can mux it
# into MP4 at all, even if some players might not support it universally.
FAST_COMPATIBLE_VIDEO_CODECS = {"h264", "hevc", "vp9", "av1", "mpeg4", "vp8"}
FAST_COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "ac3", "eac3", "flac"}

# "compatible" (Uyumlu MP4): only H.264 + AAC/MP3 count as copy-safe; anything
# else is re-encoded to guarantee broad device/browser playback.
STRICT_COMPATIBLE_VIDEO_CODECS = {"h264"}
STRICT_COMPATIBLE_AUDIO_CODECS = {"aac", "mp3"}


class Strategy(str, Enum):
    DIRECT = "DIRECT"
    REMUX = "REMUX"
    MERGE_COPY = "MERGE_COPY"
    AUDIO_TRANSCODE = "AUDIO_TRANSCODE"
    VIDEO_TRANSCODE = "VIDEO_TRANSCODE"
    FULL_TRANSCODE = "FULL_TRANSCODE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ProcessingPlan:
    strategy: Strategy
    reason: str


def select_strategy(
    probe: MediaProbeResult,
    *,
    preference: str = "fast",
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> ProcessingPlan:
    if probe.duration_seconds is not None and probe.duration_seconds > max_duration_seconds:
        return ProcessingPlan(Strategy.REJECT, "duration_limit")

    video = probe.primary_video
    if video is None:
        return ProcessingPlan(Strategy.REJECT, "no_video_stream")

    video_codecs = FAST_COMPATIBLE_VIDEO_CODECS if preference == "fast" else STRICT_COMPATIBLE_VIDEO_CODECS
    audio_codecs = FAST_COMPATIBLE_AUDIO_CODECS if preference == "fast" else STRICT_COMPATIBLE_AUDIO_CODECS

    audio = probe.primary_audio
    video_ok = video.codec in video_codecs
    audio_ok = audio is None or audio.codec in audio_codecs
    # ffprobe reports MP4/MOV/M4A/3GP under one shared demuxer name.
    is_mp4_container = "mp4" in (probe.container or "")

    if video_ok and audio_ok:
        if is_mp4_container:
            return ProcessingPlan(Strategy.DIRECT, "already_compatible_mp4")
        # This pipeline always hands ffprobe an already-merged file (yt-dlp
        # merges separate audio/video streams before we ever probe), so the
        # merge-vs-native distinction can't be recovered here — both land on
        # REMUX, which emits the same "-c copy" plan MERGE_COPY would.
        return ProcessingPlan(Strategy.REMUX, "compatible_streams_wrong_container")
    if video_ok and not audio_ok:
        return ProcessingPlan(Strategy.AUDIO_TRANSCODE, "incompatible_audio_codec")
    if not video_ok and audio_ok:
        return ProcessingPlan(Strategy.VIDEO_TRANSCODE, "incompatible_video_codec")
    return ProcessingPlan(Strategy.FULL_TRANSCODE, "incompatible_video_and_audio")


def ffmpeg_args_for(plan: ProcessingPlan, ffmpeg: str, source, target) -> list[str] | None:
    """Returns None for DIRECT (no ffmpeg invocation needed at all)."""
    if plan.strategy == Strategy.DIRECT:
        return None
    base = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
    ]
    if plan.strategy in (Strategy.REMUX, Strategy.MERGE_COPY):
        return base + ["-c", "copy", "-movflags", "+faststart", str(target)]
    if plan.strategy == Strategy.AUDIO_TRANSCODE:
        return base + ["-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(target)]
    if plan.strategy == Strategy.VIDEO_TRANSCODE:
        return base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                        "-c:a", "copy", "-movflags", "+faststart", str(target)]
    if plan.strategy == Strategy.FULL_TRANSCODE:
        return base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-movflags", "+faststart", str(target)]
    raise ValueError(f"{plan.strategy} için ffmpeg komutu yok.")
