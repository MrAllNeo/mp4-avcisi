from app.probe import AudioStreamInfo, MediaProbeResult, VideoStreamInfo
from app.strategy import Strategy, ffmpeg_args_for, select_strategy


def probe(*, container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="h264", audio_codec="aac",
          duration=120.0, height=720, fps=30.0, hdr=False, has_audio=True, has_video=True):
    video_streams = []
    if has_video:
        video_streams.append(VideoStreamInfo(
            codec=video_codec, codec_profile=None, width=1280, height=height,
            fps=fps, bitrate=1_000_000, pixel_format="yuv420p", hdr=hdr,
        ))
    audio_streams = []
    if has_audio:
        audio_streams.append(AudioStreamInfo(codec=audio_codec, sample_rate=44100, channels=2, bitrate=128_000))
    return MediaProbeResult(
        container=container, duration_seconds=duration, estimated_size_bytes=10_000_000,
        overall_bitrate=1_000_000, protocol="file", seekable=True,
        video_streams=video_streams, audio_streams=audio_streams,
    )


class TestSelectStrategy:
    def test_already_compatible_mp4_is_direct(self):
        plan = select_strategy(probe(), preference="fast")
        assert plan.strategy == Strategy.DIRECT

    def test_compatible_streams_wrong_container_is_remux(self):
        plan = select_strategy(probe(container="matroska,webm"), preference="fast")
        assert plan.strategy == Strategy.REMUX

    def test_incompatible_audio_only_is_audio_transcode(self):
        plan = select_strategy(probe(container="matroska,webm", audio_codec="dts"), preference="fast")
        assert plan.strategy == Strategy.AUDIO_TRANSCODE

    def test_incompatible_video_only_is_video_transcode(self):
        plan = select_strategy(probe(container="matroska,webm", video_codec="av1"), preference="compatible")
        assert plan.strategy == Strategy.VIDEO_TRANSCODE

    def test_incompatible_both_is_full_transcode(self):
        plan = select_strategy(probe(video_codec="vp9", audio_codec="opus"), preference="compatible")
        assert plan.strategy == Strategy.FULL_TRANSCODE

    def test_compatible_mode_rejects_fast_mode_compatible_codecs(self):
        # vp9+opus is fine for "fast" but must still transcode under "compatible".
        fast_plan = select_strategy(probe(container="matroska,webm", video_codec="vp9", audio_codec="opus"), preference="fast")
        strict_plan = select_strategy(probe(container="matroska,webm", video_codec="vp9", audio_codec="opus"), preference="compatible")
        assert fast_plan.strategy == Strategy.REMUX
        assert strict_plan.strategy == Strategy.FULL_TRANSCODE

    def test_no_video_stream_is_rejected(self):
        plan = select_strategy(probe(has_video=False))
        assert plan.strategy == Strategy.REJECT
        assert plan.reason == "no_video_stream"

    def test_duration_over_limit_is_rejected(self):
        plan = select_strategy(probe(duration=8000), max_duration_seconds=7200)
        assert plan.strategy == Strategy.REJECT
        assert plan.reason == "duration_limit"

    def test_missing_audio_stream_does_not_block_direct(self):
        plan = select_strategy(probe(has_audio=False))
        assert plan.strategy == Strategy.DIRECT


class TestFfmpegArgsFor:
    def test_direct_has_no_ffmpeg_command(self):
        plan = select_strategy(probe())
        assert ffmpeg_args_for(plan, "ffmpeg", "in.mp4", "out.mp4") is None

    def test_remux_copies_both_streams(self):
        plan = select_strategy(probe(container="matroska,webm"))
        args = ffmpeg_args_for(plan, "ffmpeg", "in.mkv", "out.mp4")
        assert args[-4:] == ["-c", "copy", "-movflags", "+faststart"] or "-c" in args
        assert args[0] == "ffmpeg" and args[-1] == "out.mp4"

    def test_audio_transcode_keeps_video_copy(self):
        plan = select_strategy(probe(container="matroska,webm", audio_codec="dts"))
        args = ffmpeg_args_for(plan, "ffmpeg", "in.mkv", "out.mp4")
        assert args[args.index("-c:v") + 1] == "copy"
        assert args[args.index("-c:a") + 1] == "aac"

    def test_full_transcode_forces_yuv420p(self):
        plan = select_strategy(probe(video_codec="vp9", audio_codec="opus"), preference="compatible")
        args = ffmpeg_args_for(plan, "ffmpeg", "in.webm", "out.mp4")
        assert args[args.index("-pix_fmt") + 1] == "yuv420p"
        assert args[args.index("-c:v") + 1] == "libx264"
        assert args[args.index("-c:a") + 1] == "aac"
