from app.cost import actual_cost, cost_for_resolution, estimate_cost_from_request
from app.probe import AudioStreamInfo, MediaProbeResult, VideoStreamInfo
from app.strategy import Strategy


def probe_with(height=720, fps=30.0, hdr=False, duration=120.0):
    return MediaProbeResult(
        container="mov,mp4,m4a,3gp,3g2,mj2", duration_seconds=duration,
        estimated_size_bytes=1_000, overall_bitrate=1_000, protocol="file", seekable=True,
        video_streams=[VideoStreamInfo(codec="h264", codec_profile=None, width=1280, height=height,
                                        fps=fps, bitrate=1_000, pixel_format="yuv420p", hdr=hdr)],
        audio_streams=[AudioStreamInfo(codec="aac", sample_rate=44100, channels=2, bitrate=128_000)],
    )


class TestCostForResolution:
    def test_tiers(self):
        assert cost_for_resolution(360) == 3
        assert cost_for_resolution(480) == 3
        assert cost_for_resolution(720) == 4
        assert cost_for_resolution(1080) == 6
        assert cost_for_resolution(2160) == 8
        assert cost_for_resolution(None) == 3


class TestEstimateCostFromRequest:
    def test_short_video_uses_resolution_tier(self):
        assert estimate_cost_from_request(height=720, duration_seconds=300) == 4

    def test_long_video_adds_penalty(self):
        assert estimate_cost_from_request(height=720, duration_seconds=4000) == 6


class TestActualCost:
    def test_direct_and_remux_are_cheap(self):
        assert actual_cost(Strategy.DIRECT, probe_with()) == 1
        assert actual_cost(Strategy.REMUX, probe_with()) == 1

    def test_audio_transcode_is_fixed_low_cost(self):
        assert actual_cost(Strategy.AUDIO_TRANSCODE, probe_with(height=2160)) == 2

    def test_video_transcode_scales_with_resolution(self):
        assert actual_cost(Strategy.VIDEO_TRANSCODE, probe_with(height=1080)) == 6

    def test_high_fps_and_hdr_add_cost(self):
        base = actual_cost(Strategy.FULL_TRANSCODE, probe_with(height=1080, fps=30))
        high_fps = actual_cost(Strategy.FULL_TRANSCODE, probe_with(height=1080, fps=60))
        hdr = actual_cost(Strategy.FULL_TRANSCODE, probe_with(height=1080, hdr=True))
        assert high_fps == base + 2
        assert hdr == base + 2

    def test_long_duration_adds_cost(self):
        base = actual_cost(Strategy.FULL_TRANSCODE, probe_with(duration=1000))
        long = actual_cost(Strategy.FULL_TRANSCODE, probe_with(duration=4000))
        assert long == base + 2
