"""Active-livestream helpers for the clip pipeline.

Detects whether a YouTube URL points at an active (or recent) live stream and
captures a bounded recent portion of that stream.  Only the standard library and
yt-dlp are required; ffmpeg comes from imageio_ffmpeg.

Public API:
    LiveError
    extract_video_id(url) -> str | None
    normalize_watch_url(url) -> str
    is_live_url(url) -> bool
    get_live_status(url) -> dict
    select_live_window(captured_seconds, clip_duration) -> tuple[float, float]
    capture_live_segment(url, duration, output_path, *, max_wait=None, progress_cb=None) -> dict
    describe_live_limitations() -> str
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import auto_clipper
from auto_clipper import get_ffmpeg_exe, run_ytdlp, ytdlp_cookie_strategies

__all__ = [
    "LiveError",
    "extract_video_id",
    "normalize_watch_url",
    "is_live_url",
    "get_live_status",
    "select_live_window",
    "capture_live_segment",
    "describe_live_limitations",
]

DEFAULT_MAX_WAIT = 30
MIN_CAPTURE_SECONDS = 10
MAX_CAPTURE_SECONDS = 600
HARD_MIN_SECONDS = 1
HARD_MAX_SECONDS = 3600
STATUS_TIMEOUT = 45

_ID = r"([A-Za-z0-9_-]{11})"
_PATTERNS = (
    re.compile(r"youtube\.com/watch\?(?:[^#]*&)?v=" + _ID),
    re.compile(r"youtu\.be/" + _ID),
    re.compile(r"youtube\.com/(?:embed|v|shorts|live)/" + _ID),
    re.compile(r"youtube-nocookie\.com/embed/" + _ID),
)
_LIVE_URL_RE = re.compile(
    r"/live(?:/|$)|/embed/|/live_chat|[?&]live(?:=|&|$)|[?&]feature=live",
    re.IGNORECASE,
)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%\s+of")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

_CREATE_FLAGS = 0
if os.name == "nt":
    _CREATE_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class LiveError(RuntimeError):
    """Raised when a live-stream lookup or capture cannot be completed."""


def _log(text: str) -> None:
    try:
        print(text)
    except Exception:
        pass


def _report(progress_cb, fraction, message):
    if progress_cb is None:
        return
    try:
        progress_cb(max(0.0, min(1.0, float(fraction))), str(message))
    except Exception:
        pass


def extract_video_id(url: str):
    """Best-effort extraction of the 11-character YouTube video id."""
    if not isinstance(url, str) or not url:
        return None
    candidate = url.strip()
    for pattern in _PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    match = re.search(r"[?&]v=" + _ID, candidate)
    if match:
        return match.group(1)
    match = re.search(_ID, candidate)
    if match:
        return match.group(1)
    return None


def normalize_watch_url(url: str) -> str:
    """Return the canonical watch URL, or raise LiveError."""
    video_id = extract_video_id(url)
    if not video_id:
        raise LiveError(f"Could not extract a video id from: {url!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


def is_live_url(url: str) -> bool:
    """Cheap, offline check for live-ish URL shapes."""
    if not isinstance(url, str) or not url:
        return False
    return bool(_LIVE_URL_RE.search(url))


def _ffmpeg_exe():
    try:
        return get_ffmpeg_exe()
    except Exception:
        return None


def _probe_duration(path: str):
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg or not os.path.isfile(path):
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except Exception:
        return None
    blob = (result.stderr or "") + (result.stdout or "")
    match = _DURATION_RE.search(blob)
    if not match:
        return None
    try:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
    except Exception:
        return None
    return hours * 3600.0 + minutes * 60.0 + seconds


def get_live_status(url: str) -> dict:
    """Ask yt-dlp about the live state of ``url``.  Never raises."""
    info = {
        "is_live": False,
        "status": "unknown",
        "was_live": False,
        "title": None,
        "duration": None,
        "concurrent_view_count": None,
        "raw_live_status": None,
        "url": url if isinstance(url, str) else "",
        "video_id": None,
    }
    try:
        video_id = extract_video_id(url)
        info["video_id"] = video_id
        if video_id:
            info["url"] = f"https://www.youtube.com/watch?v={video_id}"

        ffmpeg = _ffmpeg_exe()
        args = [
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--skip-download",
            "--dump-single-json",
        ]
        if ffmpeg:
            args += ["--ffmpeg-location", ffmpeg]

        result = run_ytdlp(args, url, timeout=STATUS_TIMEOUT)
        if result is None:
            info["error"] = "yt-dlp timed out"
            return info
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            reason = stderr.splitlines()[-1] if stderr else ""
            info["error"] = f"yt-dlp exited {result.returncode}: {reason[:160]}"
            return info

        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        info["error"] = f"could not parse yt-dlp json: {exc}"
        return info
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    try:
        raw_status = payload.get("live_status")
        is_live_flag = bool(payload.get("is_live"))
        was_live_flag = bool(payload.get("was_live"))
        info["raw_live_status"] = raw_status if isinstance(raw_status, str) else None
        info["is_live"] = bool(is_live_flag or raw_status == "is_live")
        info["was_live"] = bool(
            was_live_flag or raw_status in ("post_live", "was_live")
        )
        title = payload.get("title")
        info["title"] = title if isinstance(title, str) else None

        duration = payload.get("duration")
        try:
            info["duration"] = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            info["duration"] = None

        viewers = payload.get("concurrent_view_count")
        try:
            info["concurrent_view_count"] = int(viewers) if viewers is not None else None
        except (TypeError, ValueError):
            info["concurrent_view_count"] = None

        if raw_status == "is_live" or is_live_flag:
            info["status"] = "live"
        elif raw_status == "is_upcoming":
            info["status"] = "upcoming"
        elif raw_status in ("post_live", "was_live") or was_live_flag:
            info["status"] = "replay"
        elif not raw_status and not is_live_flag and not was_live_flag:
            info["status"] = "vod"
        else:
            info["status"] = "unknown"
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def select_live_window(captured_seconds: float, clip_duration: int):
    """Return (start, end) covering the LAST clip_duration seconds."""
    try:
        captured = max(0.0, float(captured_seconds))
    except (TypeError, ValueError):
        captured = 0.0
    try:
        wanted = max(0.0, float(clip_duration))
    except (TypeError, ValueError):
        wanted = 0.0
    end = captured
    start = max(0.0, captured - wanted)
    return (start, end)


_CAPTURE_COOKIE_ARGS = None


def _probe_cookie_args(url, extra):
    args = [
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--simulate",
        "--print",
        "%(id)s",
    ]
    args += _proxy_args()
    cmd = [sys.executable, "-m", "yt_dlp"] + args + list(extra) + [url]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=25
        )
    except Exception:
        return False
    return result.returncode == 0


def _cookie_args(url):
    """Pick a cookie source once per process, then reuse it."""
    global _CAPTURE_COOKIE_ARGS
    if _CAPTURE_COOKIE_ARGS is not None:
        return list(_CAPTURE_COOKIE_ARGS)
    cached = getattr(auto_clipper, "_COOKIE_STRATEGY", None)
    if cached is not None:
        _CAPTURE_COOKIE_ARGS = list(cached)
        return list(_CAPTURE_COOKIE_ARGS)
    try:
        strategies = ytdlp_cookie_strategies()
    except Exception:
        strategies = []
    for extra in strategies:
        if _probe_cookie_args(url, extra):
            _CAPTURE_COOKIE_ARGS = list(extra)
            return list(_CAPTURE_COOKIE_ARGS)
    _CAPTURE_COOKIE_ARGS = []
    return []


def _proxy_args():
    proxy = os.environ.get("YTDLP_PROXY")
    return ["--proxy", proxy] if proxy else []


def _kill_process_tree(proc, pid):
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _tail(text: str, limit: int = 200) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]


def _pump(stream, sink: queue.Queue, state: dict) -> None:
    try:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip()
            if not line:
                continue
            state["tail"] = line
            match = _PCT_RE.search(line)
            if match:
                try:
                    state["frac"] = float(match.group(1)) / 100.0
                except ValueError:
                    pass
            sink.put(line)
    except Exception:
        pass
    finally:
        try:
            sink.put(None)
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


def capture_live_segment(
    url: str,
    duration: int,
    output_path: str,
    *,
    max_wait: int = None,
    progress_cb=None,
) -> dict:
    """Capture up to ``duration`` seconds of the current live edge of ``url``."""
    if not isinstance(url, str) or not url.strip():
        raise LiveError("A live stream URL is required.")
    if not isinstance(output_path, str) or not output_path.strip():
        raise LiveError("An output path is required.")

    try:
        requested = float(duration)
    except (TypeError, ValueError):
        raise LiveError("duration must be a number of seconds.")
    if requested != requested:
        raise LiveError("duration must be a number of seconds.")
    if requested < HARD_MIN_SECONDS or requested > HARD_MAX_SECONDS:
        raise LiveError(
            f"duration must be between {HARD_MIN_SECONDS} and {HARD_MAX_SECONDS} seconds."
        )
    requested = int(max(MIN_CAPTURE_SECONDS, min(MAX_CAPTURE_SECONDS, requested)))

    if max_wait is None:
        max_wait = DEFAULT_MAX_WAIT
    try:
        max_wait = float(max_wait)
    except (TypeError, ValueError):
        raise LiveError("max_wait must be a number of seconds.")
    if max_wait < 0:
        raise LiveError("max_wait cannot be negative.")

    output_path = os.path.abspath(output_path)
    parent = os.path.dirname(output_path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:
            raise LiveError(f"Could not create output directory: {exc}")

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise LiveError("ffmpeg is not available; cannot capture a live segment.")

    status = get_live_status(url)
    if status.get("status") in ("vod", "upcoming", "replay") and not status.get("is_live"):
        label = status.get("status")
        raise LiveError(f"Stream is not currently live (status: {label}).")

    ffmpeg_arg = f"ffmpeg_i:-t {requested}"
    args = [
        "--no-playlist",
        "--no-part",
        "--no-mtime",
        "--no-warnings",
        "--newline",
        "--ffmpeg-location",
        ffmpeg,
        "-f",
        "bestvideo[height<=1080]+bestaudio/best",
        "--downloader",
        "ffmpeg",
        "--downloader-args",
        ffmpeg_arg,
        "--merge-output-format",
        "mp4",
        "--retries",
        "2",
        "-o",
        output_path,
    ]
    args += _proxy_args()
    args += _cookie_args(url)

    cmd = [sys.executable, "-m", "yt_dlp"] + args + [url]

    total_budget = requested + max_wait
    deadline = time.monotonic() + total_budget
    started = time.monotonic()

    sink: queue.Queue = queue.Queue()
    state = {"frac": 0.0, "tail": ""}

    _report(progress_cb, 0.0, f"Connecting to live stream (up to {requested}s)...")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_CREATE_FLAGS,
            **({} if os.name == "nt" else {"start_new_session": True}),
        )
    except Exception as exc:
        raise LiveError(f"Could not start yt-dlp: {exc}")

    reader = threading.Thread(
        target=_pump, args=(proc.stdout, sink, state), daemon=True
    )
    reader.start()

    reader_done = threading.Event()
    last_report = 0.0

    def _reap(wait_for):
        try:
            proc.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            return False
        return True

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        remaining = deadline - time.monotonic()
        try:
            line = sink.get(timeout=max(0.05, min(0.25, remaining)))
        except queue.Empty:
            continue
        if line is None:
            reader_done.set()
            _reap(1.0)
            continue
        now = time.monotonic()
        if now - last_report >= 0.5:
            last_report = now
            fraction = state["frac"]
            if fraction <= 0.0:
                fraction = min(0.98, (now - started) / total_budget)
            _report(progress_cb, fraction, line)

    timed_out = proc.poll() is None
    if timed_out:
        _kill_process_tree(proc, proc.pid)
        if not _reap(10.0):
            _kill_process_tree(proc, proc.pid)
            _reap(5.0)

    returncode = proc.returncode

    for _ in range(500):
        try:
            leftover = sink.get_nowait()
        except queue.Empty:
            break
        if leftover is None:
            break
        state["tail"] = leftover

    try:
        reader.join(timeout=5)
    except Exception:
        pass

    probed = _probe_duration(output_path)
    has_output = os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    if probed:
        captured = max(0.0, min(float(probed), float(requested)))
    elif has_output:
        captured = float(requested)
    else:
        captured = 0.0
    approx = probed is None and has_output

    if timed_out:
        if not has_output:
            raise LiveError(
                "Hard time limit reached before any video was written: "
                f"{_tail(state.get('tail') or '')}"
            )
        return {
            "ok": True,
            "path": output_path,
            "captured_seconds": captured,
            "timed_out": True,
            "note": (
                f"Hard time limit reached after {total_budget:.0f}s; "
                f"kept the {captured:.1f}s already written"
                + (" (duration could not be probed)." if approx else ".")
            ),
        }

    if returncode != 0:
        detail = _tail(state.get("tail") or "")
        raise LiveError(f"yt-dlp capture failed (exit {returncode}): {detail}")

    if not has_output:
        raise LiveError("yt-dlp reported success but produced no output file.")

    if captured + 0.5 < requested:
        note = (
            f"Stream ended before the requested {requested}s; "
            f"captured {captured:.1f}s."
        )
    else:
        note = f"Captured {captured:.1f}s of the live stream."
        if approx:
            note += " (duration could not be probed)"
    return {
        "ok": True,
        "path": output_path,
        "captured_seconds": captured,
        "timed_out": False,
        "note": note,
    }


def describe_live_limitations() -> str:
    """Honest, short description of what live capture can and cannot do."""
    return (
        "Live capture grabs the most recent part of an active stream, so the clip "
        "always ends at the moment you press record. If the range you want sits "
        "further back than the requested duration, the stream has to have been "
        "recorded for at least that long first. Some streams expose no DVR or "
        "seekable formats, in which case nothing can be captured. Automatic format "
        "selection is a fallback, not a guarantee of the best moment, and private, "
        "members-only or otherwise restricted streams may fail outright. A stream "
        "can also end mid-capture, which returns a shorter clip rather than an "
        "error."
    )
