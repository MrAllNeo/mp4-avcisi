import subprocess

import pytest

from app.errors import MediaError
from app.media import get_ffmpeg
from app.probe import run_ffprobe


@pytest.fixture(scope="module")
def sample_mp4(tmp_path_factory):
    root = tmp_path_factory.mktemp("probe-fixtures")
    path = root / "sample.mp4"
    ffmpeg = get_ffmpeg()
    subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=320x240:r=30:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path


@pytest.fixture(scope="module")
def sample_webm(tmp_path_factory, sample_mp4):
    root = tmp_path_factory.mktemp("probe-fixtures-webm")
    path = root / "sample.webm"
    ffmpeg = get_ffmpeg()
    subprocess.run([
        ffmpeg, "-v", "error", "-i", str(sample_mp4), "-c:v", "libvpx-vp9", "-c:a", "libopus", str(path),
    ], check=True)
    return path


class TestRunFfprobe:
    def test_probes_real_h264_aac_mp4(self, sample_mp4):
        result = run_ffprobe(sample_mp4)
        assert "mp4" in result.container
        assert result.duration_seconds == pytest.approx(1.0, abs=0.3)
        assert result.primary_video.codec == "h264"
        assert result.primary_video.width == 320
        assert result.primary_video.height == 240
        assert result.primary_audio.codec == "aac"
        assert not result.warnings

    def test_probes_real_vp9_opus_webm(self, sample_webm):
        result = run_ffprobe(sample_webm)
        assert result.primary_video.codec == "vp9"
        assert result.primary_audio.codec == "opus"

    def test_missing_file_raises_media_error(self, tmp_path):
        with pytest.raises(MediaError) as exc_info:
            run_ffprobe(tmp_path / "does-not-exist.mp4")
        assert exc_info.value.code == "probe_failed"

    def test_corrupt_file_raises_media_error(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a real video file")
        with pytest.raises(MediaError) as exc_info:
            run_ffprobe(broken)
        assert exc_info.value.code == "probe_failed"

    def test_timeout_raises_media_error(self, sample_mp4, monkeypatch):
        def slow_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=kwargs.get("timeout", 1))
        monkeypatch.setattr("app.probe.subprocess.run", slow_run)
        with pytest.raises(MediaError) as exc_info:
            run_ffprobe(sample_mp4, timeout=1)
        assert exc_info.value.code == "probe_failed"
