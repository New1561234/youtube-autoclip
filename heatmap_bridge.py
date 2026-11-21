"""
Bridge between auto_clipper.py and the yt-heatmap-clipper repo.

Loads yt-heatmap-clipper/run.py as a module and reuses its YouTube
"Most Replayed" (heatmap) parser, so auto_clipper.py can choose clips
from real viewer engagement data and only fall back to audio energy.
"""

import importlib.util
import os
import warnings

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-heatmap-clipper")
RUN_PY = os.path.join(REPO_DIR, "run.py")

_core = None


def _load_core():
    """Import the heatmap clipper's run.py once (as 'yt_heatmap_core')."""
    global _core
    if _core is not None:
        return _core

    if not os.path.exists(RUN_PY):
        raise FileNotFoundError(
            f"yt-heatmap-clipper not found at {REPO_DIR}.\n"
            "Clone it with:\n"
            "  git clone https://github.com/0xACAB666/yt-heatmap-clipper.git"
        )

    spec = importlib.util.spec_from_file_location("yt_heatmap_core", RUN_PY)
    module = importlib.util.module_from_spec(spec)

    # run.py calls warnings.filterwarnings("ignore") at import time;
    # keep that from leaking into auto_clipper.py's own warning behaviour.
    saved_filters = warnings.filters[:]
    try:
        spec.loader.exec_module(module)
    finally:
        warnings.filters[:] = saved_filters

    _core = module
    return _core


def find_heatmap_segments(video_id, min_score=0.40, max_duration=60):
    """Return heatmap segments as dicts: {start, duration, score}, best first."""
    core = _load_core()
    core.MIN_SCORE = min_score
    core.MAX_DURATION = max_duration
    return core.ambil_most_replayed(video_id)


def best_heatmap_segment(video_id, min_score=0.40, max_duration=60):
    """Return the single highest-scoring heatmap segment, or None."""
    segments = find_heatmap_segments(
        video_id, min_score=min_score, max_duration=max_duration
    )
    return segments[0] if segments else None


def get_video_duration(video_id):
    """Return the video duration in seconds (yt-dlp based)."""
    return _load_core().get_duration(video_id)