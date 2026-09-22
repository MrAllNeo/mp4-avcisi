"""Explainable, rule-based job cost weights (no ML needed for v1)."""
from app.strategy import Strategy


def cost_for_resolution(height: int | None) -> int:
    if not height or height <= 480:
        return 3
    if height <= 720:
        return 4
    if height <= 1080:
        return 6
    return 8


def estimate_cost_from_request(*, height: int | None, duration_seconds: float | None) -> int:
    """Pre-download estimate from yt-dlp metadata alone (no local file yet)."""
    cost = cost_for_resolution(height)
    if duration_seconds and duration_seconds > 3600:
        cost += 2
    return cost


def actual_cost(strategy: Strategy, probe) -> int:
    """Post-probe cost, refining the estimate once the real file is known."""
    if strategy in (Strategy.DIRECT, Strategy.REMUX, Strategy.MERGE_COPY):
        return 1
    if strategy == Strategy.AUDIO_TRANSCODE:
        return 2
    video = probe.primary_video
    cost = cost_for_resolution(video.height if video else None)
    if video and video.fps and video.fps > 50:
        cost += 2
    if video and video.hdr:
        cost += 2
    if probe.duration_seconds and probe.duration_seconds > 3600:
        cost += 2
    return cost
