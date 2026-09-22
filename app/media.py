import os
from pathlib import Path
import shutil


def get_ffmpeg():
    configured = os.environ.get("FFMPEG_BINARY")
    local = Path(__file__).resolve().parent.parent / ".tools" / "bin" / "ffmpeg"
    executable = configured or shutil.which("ffmpeg") or (str(local) if local.is_file() else None)
    if not executable or not os.access(executable, os.X_OK):
        raise ValueError("FFmpeg bulunamadı. FFmpeg'i kur veya FFMPEG_BINARY ile tam yolunu belirt.")
    return executable


def get_ffprobe():
    configured = os.environ.get("FFPROBE_BINARY")
    local = Path(__file__).resolve().parent.parent / ".tools" / "bin" / "ffprobe"
    executable = configured or shutil.which("ffprobe") or (str(local) if local.is_file() else None)
    if not executable or not os.access(executable, os.X_OK):
        raise ValueError("FFprobe bulunamadı. FFmpeg paketini (ffprobe dahil) kur veya FFPROBE_BINARY ile tam yolunu belirt.")
    return executable
