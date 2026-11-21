"""
YouTube Vertical Clipper Web UI

A local web interface for the YouTube clipper project.
Allows users to paste YouTube URLs and generate vertical clips via a browser.

Features:
- YouTube URL input with auto-extract video ID
- Clip duration setting (default 45s)
- Context-before seconds setting
- Heatmap-based segment detection with audio fallback
- 9:16 vertical clip rendering
- Job status tracking
- Download generated MP4
- Multiple job support without server restart

Run: python app.py
Then open http://127.0.0.1:5000 in Chrome
"""

import os
import sys
import re
import json
import shutil
import uuid
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, abort, redirect

# Add project directory to path
sys.path.insert(0, r'D:\Games\new folder\clip')

# Import clipper logic
from auto_clipper import (
    extract_video_id, download_video, is_file_valid, analyze_audio_energy,
    detect_best_moment, render_vertical_clip, burn_heading_into_clip
)
from heatmap_bridge import find_heatmap_segments, best_heatmap_segment, get_video_duration

# Heading generation (Whisper + Gemini) and live-stream helpers.
import clip_heading
import clip_live

# Load a local .env once at import so GEMINI_API_KEY / GEMINI_MODEL are
# available to every thread without re-reading the file.  Never raises.
try:
    clip_heading.load_env()
except Exception:
    pass

# Optional YouTube upload feature (OAuth + YouTube Data API v3).
# These modules import the Google libraries lazily, so the app still runs if
# the optional dependencies are not installed.
import youtube_auth
import youtube_upload

app = Flask(__name__)

# ===== Job Management =====

JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
MAX_JOBS = 50  # Limit to prevent runaway job accumulation

def ensure_jobs_dir():
    """Ensure the jobs directory exists."""
    if not os.path.exists(JOBS_DIR):
        os.makedirs(JOBS_DIR, exist_ok=True)

def get_job_path(job_id):
    """Get the job-specific temporary directory path."""
    ensure_jobs_dir()
    return os.path.join(JOBS_DIR, f"job_{job_id}")

def cleanup_job_files(job_id):
    """Remove everything a job created under its own directory (recursive).

    Only files inside ``jobs/job_<job_id>/`` are touched, so pre-existing clips
    in ``jobs/`` or ``Clip-video/`` are never affected.  Tolerates a missing or
    already-removed directory.
    """
    job_dir = get_job_path(job_id)
    try:
        if os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
    except OSError:
        pass
    # Remove the now-empty directory (ignore if gone or not empty).
    try:
        os.rmdir(job_dir)
    except OSError:
        pass


# Serializes access to the shared ``Clip-video/full_video.<ext>`` download slot
# used by auto_clipper.download_video (app.py must not modify that module).
_download_lock = threading.Lock()

# Guards the check-and-set of a job's "awaiting_heading" phase so two
# concurrent approvals cannot both pass the phase test.
_approval_lock = threading.Lock()

# Guards reads/writes of per-job mutable state (``job_data`` / ``heading``).
_job_state_lock = threading.RLock()


def _job(job_id):
    """Return the mutable job record, creating a minimal one if needed."""
    job = jobs_status.get(job_id)
    if job is None:
        job = {
            "status": "Queued",
            "message": "",
            "progress": 0,
            "result": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        jobs_status[job_id] = job
    return job


def _set_job_data(job_id, **fields):
    """Merge fields into a job's ``job_data`` dict (additive)."""
    job = _job(job_id)
    data = job.get("job_data")
    if not isinstance(data, dict):
        data = {}
        job["job_data"] = data
    data.update(fields)
    return data


def _set_heading(job_id, **fields):
    """Merge fields into a job's ``heading`` dict (additive)."""
    job = _job(job_id)
    heading = job.get("heading")
    if not isinstance(heading, dict):
        heading = {}
        job["heading"] = heading
    heading.update(fields)
    return heading

# ===== Status Tracking =====

jobs_status = {}  # job_id -> {status, progress, message, result}

# job_id -> upload record. One upload per generated clip (duplicate-safe).
uploads_status = {}


def update_job_status(job_id, status, message="", progress=0, result=None):
    """Update the status of a job."""
    if job_id in jobs_status:
        jobs_status[job_id].update({
            'status': status,
            'message': message,
            'progress': progress,
            'updated_at': datetime.now().isoformat()
        })
        if result is not None:
            jobs_status[job_id]['result'] = result
    else:
        jobs_status[job_id] = {
            'status': status,
            'message': message,
            'progress': progress,
            'result': result,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }

# ===== YouTube URL Handling =====

def parse_youtube_url(url):
    """Extract video ID and validate YouTube URL.

    Returns: (video_id, error_message)
    error_message is None if valid.
    """
    if not url or not url.strip():
        return None, "YouTube URL cannot be empty"

    url = url.strip()

    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)

    # Handle youtu.be short URLs
    if parsed.hostname in ('youtu.be', 'www.youtu.be'):
        video_id = parsed.path[1:] if parsed.path else None
        if '?' in video_id:
            video_id = video_id.split('?')[0]
        if len(video_id) == 11 and all(c.isalnum() or c in '-_' for c in video_id):
            return video_id, None
        return None, "Invalid youtu.be URL format"

    # Handle youtube.com URLs
    if parsed.hostname in ('youtube.com', 'www.youtube.com'):
        if parsed.path == '/watch':
            query_params = parse_qs(parsed.query)
            video_id = query_params.get('v', [None])[0]
            if not video_id and parsed.path.startswith('/shorts/'):
                video_id = parsed.path.split('/')[2]
            if video_id and len(video_id) == 11:
                if all(c.isalnum() or c in '_-' for c in video_id):
                    return video_id, None
                return None, "Invalid video ID in URL"
            if not video_id:
                return None, "Could not extract video ID from watch URL"
            return video_id, None
        if parsed.path.startswith('/shorts/'):
            video_id = parsed.path.split('/')[2]
            if len(video_id) == 11 and all(c.isalnum() or c in '_-' for c in video_id):
                return video_id, None
            return None, "Invalid YouTube Shorts URL format"
        # Live-stream VODs and embeds: /live/<id>, /embed/<id>
        for prefix in ('/live/', '/embed/'):
            if parsed.path.startswith(prefix):
                parts = parsed.path.split('/')
                video_id = parts[2] if len(parts) > 2 else None
                if video_id and len(video_id) == 11 and all(c.isalnum() or c in '_-' for c in video_id):
                    return video_id, None
                return None, "Invalid YouTube video URL format"
        return None, "Unsupported YouTube URL path"

    return None, "Not a valid YouTube URL"


# ===== Clip Generation Logic =====

def get_yt_duration(video_id):
    """Get YouTube video duration using yt-dlp."""
    try:
        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--quiet', '--no-warnings',
            '--get-duration', f'https://youtu.be/{video_id}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except Exception:
        pass
    return 3600  # Fallback default

def generate_clip_job(job_id, youtube_url, clip_duration=45, context_before=10,
                      heading_mode="none", heading_text="", source="auto"):
    """Background pipeline: download/capture -> base clip -> (heading) -> final.

    Phases:
        download -> heading -> awaiting_heading (optional park) -> complete

    ``phase == "awaiting_heading"`` is NOT terminal: the job waits there for the
    user to approve or edit the heading via ``POST /heading/<job_id>``.
    """
    job_dir = get_job_path(job_id)
    os.makedirs(job_dir, exist_ok=True)

    if heading_mode not in ("ai", "manual", "none"):
        heading_mode = "none"
    if source not in ("video", "live", "auto"):
        source = "auto"
    if not isinstance(heading_text, str):
        heading_text = ""

    try:
        # ---- Step 1: parse & validate URL -------------------------------
        update_job_status(job_id, "Parsing URL", "Validating YouTube URL...", 5)

        video_id, error = parse_youtube_url(youtube_url)
        if error:
            update_job_status(job_id, "Error", f"Invalid URL: {error}", 0)
            return

        update_job_status(job_id, "URL parsed", f"Video ID: {video_id}", 10)

        _set_job_data(
            job_id,
            heading_mode=heading_mode,
            heading_text=heading_text,
            approved_heading=None,
            approved=False,
            phase="download",
            live=False,
            video_id=video_id,
            youtube_url=youtube_url,
            clip_duration=clip_duration,
            context_before=context_before,
        )
        _set_heading(
            job_id,
            status="pending",
            suggested="",
            language=None,
            message="",
            source="none",
        )

        # ---- Step 2: decide live vs. regular video ----------------------
        live_mode = False
        live_info = None
        if source == "live":
            live_mode = True
        elif source == "auto":
            if clip_live.is_live_url(youtube_url):
                live_mode = True
            else:
                update_job_status(job_id, "Checking live",
                                  "Checking whether this is a live stream...", 12)
                live_info = clip_live.get_live_status(youtube_url)
                st = (live_info or {}).get("status")
                if st == "live":
                    live_mode = True
                elif st == "upcoming":
                    update_job_status(
                        job_id, "Error",
                        "This stream is scheduled but has not started yet; "
                        "try again once it is live.", 0)
                    return
                elif st in ("vod", "replay"):
                    live_mode = False
                elif st == "unknown":
                    detail = ((live_info or {}).get("error")
                              or "could not determine stream status")
                    update_job_status(job_id, "Error",
                                      f"Could not determine stream status: {detail}", 0)
                    return

        video_title = ""
        total_duration = None
        live_note = None

        if live_mode:
            # ---- Live capture path (never touches the heatmap) ----------
            update_job_status(job_id, "Checking live",
                              "Checking live stream status...", 14)
            if live_info is None:
                live_info = clip_live.get_live_status(youtube_url)
            status = (live_info or {}).get("status")
            if status == "upcoming":
                update_job_status(
                    job_id, "Error",
                    "This stream is scheduled but has not started yet; "
                    "try again once it is live.", 0)
                return
            if status == "unknown":
                detail = ((live_info or {}).get("error")
                          or "could not determine stream status")
                update_job_status(job_id, "Error",
                                  f"Could not determine stream status: {detail}", 0)
                return
            if status in ("replay", "vod"):
                # Not actually live any more: fall back to the video workflow.
                live_mode = False
            else:
                # Hard cap: never request more than 600s in one capture.
                cap_seconds = max(10, int(clip_duration) + int(context_before))
                cap_seconds = min(600, cap_seconds)
                capture_path = os.path.join(job_dir, "live_capture.mp4")

                def _cap_progress(frac, msg):
                    pct = 18 + int(max(0.0, min(1.0, frac)) * 7)
                    update_job_status(job_id, "Capturing live",
                                      str(msg)[:160], pct)

                update_job_status(
                    job_id, "Capturing live",
                    "Capturing the most recent %ds of the live stream..."
                    % cap_seconds, 18)
                try:
                    cap = clip_live.capture_live_segment(
                        youtube_url, cap_seconds, capture_path,
                        progress_cb=_cap_progress)
                except clip_live.LiveError as e:
                    update_job_status(job_id, "Error",
                                      f"Live capture failed: {str(e)[:140]}", 0)
                    return
                except Exception as e:
                    update_job_status(job_id, "Error",
                                      f"Live capture failed: {str(e)[:140]}", 0)
                    return

                video_path = cap.get("path") or capture_path
                captured = float(cap.get("captured_seconds") or 0.0)
                total_duration = captured
                best_start, best_end = clip_live.select_live_window(
                    captured, clip_duration)
                video_title = (live_info or {}).get("title") or "Live stream"
                live_note = clip_live.describe_live_limitations()
                _set_job_data(
                    job_id, live=True, live_capture=capture_path,
                    live_status=status, live_captured=captured)
                update_job_status(
                    job_id, "Live captured",
                    "Captured %.1fs; using the last %ds"
                    % (captured, clip_duration), 26)
                print(f"[{job_id}] Live captured {captured:.1f}s -> {video_path}")

        if not live_mode:
            # ---- Regular video path -------------------------------------
            update_job_status(job_id, "Downloading video",
                              "Downloading from YouTube...", 15)
            try:
                with _download_lock:
                    video_path, video_title = download_video(youtube_url)
                    # Move the shared download into this job's own dir under a
                    # unique name so concurrent jobs never collide.
                    src_ext = os.path.splitext(video_path)[1] or ".mp4"
                    src_path = os.path.join(job_dir, f"src_{video_id}{src_ext}")
                    try:
                        shutil.move(video_path, src_path)
                        video_path = src_path
                    except Exception:
                        try:
                            shutil.copyfile(video_path, src_path)
                            video_path = src_path
                        except Exception:
                            pass
                _set_job_data(job_id, live=False)
                update_job_status(job_id, "Video downloaded",
                                  f"Video: {str(video_title)[:30]}...", 25)
            except Exception as e:
                update_job_status(job_id, "Download error",
                                  f"Failed to download video: {str(e)[:80]}", 0)
                return

            # Duration (only meaningful for non-live videos).
            total_duration = get_yt_duration(video_id)
            update_job_status(job_id, "Duration fetched",
                              f"Video duration: {total_duration}s", 20)

            # ---- Heatmap lookup (video only) -------------------------
            update_job_status(job_id, "Checking heatmap",
                              "Fetching YouTube Most Replayed data...", 30)
            heatmap_segments = None
            try:
                heatmap_segments = find_heatmap_segments(
                    video_id, min_score=0.40,
                    max_duration=clip_duration + context_before)
                update_job_status(job_id, "Heatmap found",
                                  f"{len(heatmap_segments)} segment(s) found", 35)
            except Exception as e:
                print(f"Heatmap lookup error: {e}")
                update_job_status(job_id, "Heatmap unavailable",
                                  "Will use audio energy fallback", 35)

            best_start = None
            best_end = None
            if heatmap_segments:
                segment = heatmap_segments[0]  # already sorted by score
                raw_start = segment.get('start', 0)
                raw_duration = segment.get('duration', clip_duration)
                start = max(0, raw_start - context_before)
                end = min(raw_start + raw_duration + context_before, total_duration)
                start = max(0, end - raw_duration - context_before)
                best_start = start
                best_end = end
                update_job_status(job_id, "Heatmap selected",
                                  f"Heatmap segment at {segment.get('start', 0)}s "
                                  f"(score: {segment.get('score', 0):.2f})", 40)
            else:
                update_job_status(job_id, "Audio analysis",
                                  "Analyzing audio energy...", 40)
                try:
                    # moviepy 2.2.1's VideoFileClip has NO ``timeout`` kwarg;
                    # detect_best_moment already returns the real total_duration,
                    # so no throwaway clip is needed here.
                    best_start, best_end, total_duration = detect_best_moment(
                        video_path, clip_duration, video_id)
                    update_job_status(job_id, "Audio analysis complete",
                                      f"Best moment at ~{best_start}s", 45)
                except Exception as e:
                    update_job_status(job_id, "Audio analysis failed",
                                      f"Could not analyze audio: {str(e)[:60]}", 0)
                    return

        # ---- Step 3: validate the chosen segment ------------------------
        if best_start is None or best_end is None:
            update_job_status(job_id, "Error", "Could not determine a clip segment", 0)
            return
        if best_end - best_start < 3:
            update_job_status(job_id, "Error",
                              "Selected segment is shorter than 3 seconds", 0)
            return

        # ---- Step 4: render the base clip (no heading) ------------------
        output_filename = f"{video_id}_clip.mp4"
        output_path = os.path.join(job_dir, output_filename)
        base_path = os.path.join(job_dir, f"base_{video_id}.mp4")

        update_job_status(job_id, "Rendering clip",
                          f"Rendering {int(best_start)}s\u2013{int(best_end)}s...", 55)
        try:
            render_vertical_clip(video_path, int(best_start), int(best_end), base_path)
            update_job_status(job_id, "Clip rendered", "Encoding complete", 90)
        except subprocess.CalledProcessError as e:
            update_job_status(job_id, "Render error",
                              f"ffmpeg failed: {str(e)[:80]}", 0)
            return
        except Exception as e:
            update_job_status(job_id, "Render error",
                              f"Could not render clip: {str(e)[:80]}", 0)
            return

        if not is_file_valid(base_path):
            update_job_status(job_id, "Render error",
                              "Output file was not produced correctly", 0)
            return

        base_duration = int(round(best_end - best_start))
        _set_job_data(
            job_id,
            phase="heading",
            base_path=base_path,
            base_duration=base_duration,
            output_path=output_path,
            output_filename=output_filename,
            video_title=video_title,
            total_duration=total_duration,
            live_note=live_note,
        )

        # ---- Step 5: heading modes --------------------------------------
        if heading_mode == "none":
            _set_heading(job_id, status="skipped", source="none",
                         message="Heading disabled")
            _finalize_job(job_id)
            return

        if heading_mode == "manual":
            clean = clip_heading.sanitize_heading(heading_text) if heading_text else ""
            _set_heading(job_id, status="ready", suggested=clean,
                         source="manual", language=None,
                         message="Review the heading, then approve it")
            _park_for_heading(
                job_id, clean, 92,
                "Review the heading and approve it to finish the clip.")
            return

        # ---- heading_mode == "ai" ---------------------------------------
        if not clip_heading.is_configured():
            _set_heading(job_id, status="error", suggested="", source="ai",
                         message="GEMINI_API_KEY is not configured; "
                                 "enter a heading manually")
            _park_for_heading(
                job_id, "", 92,
                "AI heading unavailable (GEMINI_API_KEY is not configured). "
                "Enter a heading to finish the clip.")
            return

        update_job_status(job_id, "Transcribing", "Transcribing clip audio...", 93)
        try:
            transcript = clip_heading.transcribe_audio(base_path)
        except Exception as e:
            msg = str(e)[:200]
            _set_heading(job_id, status="error", suggested="", source="ai",
                         message=msg)
            _park_for_heading(
                job_id, "", 92,
                f"AI transcription failed: {str(e)[:120]}. "
                "Enter a heading to finish the clip.")
            return

        transcript_text = (transcript or {}).get("text", "")
        transcript_lang = (transcript or {}).get("language")
        if not str(transcript_text).strip():
            _set_heading(job_id, status="error", suggested="", source="ai",
                         language=transcript_lang,
                         message="No speech was detected in this clip")
            _park_for_heading(
                job_id, "", 92,
                "No speech was detected in this clip. "
                "Enter a heading to finish it.")
            return

        update_job_status(job_id, "Generating heading",
                          "Generating a heading with AI...", 95)
        try:
            generated = clip_heading.generate_heading(
                transcript_text, language=transcript_lang)
        except Exception as e:
            msg = str(e)[:200]
            _set_heading(job_id, status="error", suggested="", source="ai",
                         language=transcript_lang, message=msg)
            _park_for_heading(
                job_id, "", 92,
                f"AI heading failed: {str(e)[:120]}. "
                "Enter a heading to finish the clip.")
            return

        suggested = clip_heading.sanitize_heading(
            (generated or {}).get("heading", ""))
        if not suggested:
            _set_heading(job_id, status="error", suggested="", source="ai",
                         language=transcript_lang,
                         message="AI returned an empty heading")
            _park_for_heading(
                job_id, "", 96,
                "AI returned an empty heading. Enter a heading to finish the clip.")
            return

        _set_heading(job_id, status="ready", suggested=suggested, source="ai",
                     language=transcript_lang,
                     message="Review the suggested heading")
        _set_job_data(job_id, transcript_language=transcript_lang)
        _park_for_heading(
            job_id, suggested, 96,
            "Review the suggested heading, then approve it.")

    except Exception as e:
        # Never let a background thread die silently.
        update_job_status(job_id, "Error",
                          f"Unexpected error: {str(e)[:120]}", 0)
        print(f"[{job_id}] ERROR:", e)


def _park_for_heading(job_id, suggested, progress, message):
    """Mark the job as waiting for the user to approve/edit a heading."""
    _set_job_data(job_id, phase="awaiting_heading")
    update_job_status(job_id, "Needs heading", message, progress)
    print(f"[{job_id}] Parked for heading approval: {message}")


def _finalize_job(job_id):
    """Render (or copy) the final clip from the base clip.

    Called from the worker thread when heading_mode == "none", or from the
    approval route (via a background thread) once a heading is approved.
    """
    job = _job(job_id)
    data = job.get("job_data") or {}
    heading = job.get("heading") or {}
    mode = data.get("heading_mode", "none")

    base_path = data.get("base_path")
    output_path = data.get("output_path")
    if not base_path or not os.path.isfile(base_path):
        update_job_status(job_id, "Error",
                          "Base clip is missing; cannot finalize", 0)
        return
    if not output_path:
        update_job_status(job_id, "Error", "Output path is missing", 0)
        return

    approved = bool(data.get("approved"))
    if approved:
        final_heading = data.get("approved_heading") or None
        heading_source = "manual" if final_heading else "none"
    else:
        final_heading = heading.get("suggested") or None
        if mode == "none":
            final_heading = None
        heading_source = heading.get("source") or "none"

    update_job_status(job_id, "Finalizing",
                      "Burning in the heading..." if final_heading
                      else "Finalizing clip...", 97)

    try:
        if final_heading:
            render_vertical_clip(
                base_path, 0, int(data.get("base_duration") or 0),
                output_path, heading=final_heading)
        else:
            # No heading: copy the base render instead of re-encoding.
            shutil.copyfile(base_path, output_path)
    except subprocess.CalledProcessError as e:
        update_job_status(job_id, "Error",
                          f"ffmpeg failed while finalizing: {str(e)[:80]}", 0)
        return
    except Exception as e:
        update_job_status(job_id, "Error",
                          f"Could not finalize clip: {str(e)[:80]}", 0)
        return

    if not is_file_valid(output_path):
        update_job_status(job_id, "Error",
                          "Final output file was not produced correctly", 0)
        return

    clip_len = int(data.get("base_duration") or 0)
    result = {
        "filename": data.get("output_filename"),
        "clip_duration": clip_len,
        "video_title": data.get("video_title", ""),
        "heading": final_heading,
        "heading_source": heading_source,
        "heading_language": heading.get("language"),
        "transcript_lang": data.get("transcript_language"),
        "live": bool(data.get("live")),
        "live_note": data.get("live_note"),
    }
    _set_job_data(job_id, phase="complete", final_heading=final_heading)
    update_job_status(job_id, "Complete", "Clip ready", 100, result=result)
    print(f"[{job_id}] Done: {output_path} (heading={final_heading!r})")


def _finalize_worker(job_id):
    """Background wrapper so the approval HTTP call returns immediately."""
    try:
        _finalize_job(job_id)
    except Exception as e:
        update_job_status(job_id, "Error",
                          f"Could not finalize clip: {str(e)[:80]}", 0)


# ===== Job pruning =====

def prune_old_jobs():
    """Keep at most MAX_JOBS completed jobs in memory to avoid leaks."""
    # Snapshot first: another thread may mutate jobs_status while we iterate.
    try:
# ===== Heading approval / regeneration & live status =====

@app.route("/heading/options")
def heading_options():
    """Report which heading modes are available and how they behave."""
    return jsonify({
        "modes": ["ai", "manual", "none"],
        "ai_available": bool(clip_heading.is_configured()),
        "default_model": os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash",
        "privacy_note": (
            "In AI mode the clip's audio is transcribed on this machine with "
            "Whisper; only the resulting transcript text is sent to Google "
            "Gemini to write a heading. The GEMINI_API_KEY is never returned "
            "by this app or included in any response."
        ),
        "limits_note": clip_live.describe_live_limitations(),
    })


@app.route("/heading/<job_id>", methods=["POST"])
def submit_heading(job_id):
    """Approve (or edit) a heading for a job parked at 'Needs heading'.

    Body: {"heading": str, "use_heading": bool}
    """
    job = jobs_status.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job ID"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}

    with _approval_lock:
        data_state = job.get("job_data") or {}
        if data_state.get("phase") != "awaiting_heading":
            return jsonify({"error": "Job is not waiting for a heading"}), 409

        use_heading = bool(data.get("use_heading"))
        if use_heading:
            heading = clip_heading.sanitize_heading(data.get("heading") or "")
            if not heading:
                return jsonify({"error": "Heading cannot be empty"}), 400
            data_state["approved_heading"] = heading
        else:
            heading = ""
            data_state["approved_heading"] = ""

        data_state["approved"] = True
        data_state["phase"] = "finalizing"
        job["job_data"] = data_state

        heading_state = job.get("heading")
        if not isinstance(heading_state, dict):
            heading_state = {}
        heading_state["status"] = "approved" if use_heading else "skipped"
        if use_heading:
            heading_state["suggested"] = heading
        job["heading"] = heading_state

    threading.Thread(target=_finalize_worker, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/regenerate-heading/<job_id>", methods=["POST"])
def regenerate_heading(job_id):
    """Re-render an already complete clip with a new heading."""
    job = jobs_status.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job ID"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    heading = clip_heading.sanitize_heading(data.get("heading") or "")
    if not heading:
        return jsonify({"error": "Heading cannot be empty"}), 400

    job_data = job.get("job_data") or {}
    base_path = job_data.get("base_path")
    if not base_path or not os.path.isfile(base_path):
        return jsonify({
            "error": "No base clip is available for this job; "
                     "regeneration needs the original unheaded render"
        }), 409

    threading.Thread(
        target=_regenerate_worker,
        args=(job_id, heading),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


def _regenerate_worker(job_id, heading):
    """Background worker: burn a new heading onto the existing base clip."""
    job = jobs_status.get(job_id)
    if job is None:
        return
    job_data = job.get("job_data") or {}
    base_path = job_data.get("base_path")
    output_path = job_data.get("output_path")
    if not base_path or not os.path.isfile(base_path) or not output_path:
        return

    job_dir = os.path.dirname(output_path)
    tmp_path = os.path.join(job_dir, "regen_%s.mp4" % uuid.uuid4().hex)
    try:
        # Render to a temp name first so the existing output is never lost if
        # this render fails.
        burn_heading_into_clip(base_path, tmp_path, heading)
        if not is_file_valid(tmp_path):
            raise RuntimeError("regenerated clip was not produced correctly")
        os.replace(tmp_path, output_path)

        job_data["approved_heading"] = heading
        job_data["approved"] = True
        job_data["final_heading"] = heading
        job["job_data"] = job_data

        heading_state = job.get("heading")
        if not isinstance(heading_state, dict):
            heading_state = {}
        heading_state["status"] = "approved"
        heading_state["suggested"] = heading
        heading_state["source"] = "manual"
        job["heading"] = heading_state

        result = job.get("result")
        if isinstance(result, dict):
            result["heading"] = heading
            result["heading_source"] = "manual"
        update_job_status(job_id, "Complete",
                          "Heading regenerated", 100, result=result)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        update_job_status(job_id, "Complete",
                          f"Heading regeneration failed: {str(e)[:80]}", 100,
                          result=job.get("result"))


@app.route("/live-status")
def live_status():
    """Report whether a URL points at an active / upcoming / replay stream."""
    url = request.args.get("url", "")
    if not isinstance(url, str) or not url.strip():
        return jsonify({"ok": False, "error": "Missing 'url' query parameter"})
    try:
        info = clip_live.get_live_status(url.strip())
    except clip_live.LiveError as e:
        return jsonify({"ok": False, "error": str(e)[:200]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})
    return jsonify({"ok": True, "live": info})
        items = list(jobs_status.items())
    except RuntimeError:
        return
    if len(items) <= MAX_JOBS:
        return
    # Sort by updated_at (oldest first) and drop the oldest completed ones.
    ordered = sorted(
        items,
        key=lambda kv: (kv[1] or {}).get("updated_at", ""),
    )
    excess = len(items) - MAX_JOBS
    removed = 0
    for job_id, meta in ordered:
        if removed >= excess:
            break
        meta = meta or {}
        if meta.get("status") in ("Complete", "Error"):
            cleanup_job_files(job_id)
            jobs_status.pop(job_id, None)
            uploads_status.pop(job_id, None)
            removed += 1


# ===== Flask Routes =====

@app.route("/")
def index():
    """Serve the main page."""
    return render_template("index.html")


@app.route("/help")
def help_page():
    """Serve the help page."""
    return render_template("help.html")


@app.route("/jobs")
def list_jobs():
    """Return a summary of all tracked jobs."""
    # Return a serialisable copy (avoid mutating while iterating).
    summary = {}
    # Snapshot so a concurrent job update cannot break iteration.
    for job_id, meta in list(jobs_status.items()):
        summary[job_id] = {
            "status": meta.get("status"),
            "message": meta.get("message", ""),
            "progress": meta.get("progress", 0),
        }
    return jsonify({"jobs": summary})


@app.route("/generate", methods=["POST"])
def generate():
    """Start a background clip-generation job."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    raw_url = data.get("youtube_url")
    youtube_url = raw_url.strip() if isinstance(raw_url, str) else ""

    video_id, error = parse_youtube_url(youtube_url)
    if error:
        return jsonify({"error": error}), 400

    # Validate / clamp numeric settings.
    try:
        clip_duration = int(data.get("clip_duration", 45))
    except (TypeError, ValueError):
        clip_duration = 45
    try:
        context_before = int(data.get("context_before", 10))
    except (TypeError, ValueError):
        context_before = 10

    clip_duration = max(10, min(300, clip_duration))
    context_before = max(0, min(60, context_before))

    # Heading mode: one of "ai" | "manual" | "none" (default "none").
    heading_mode = data.get("heading_mode", "none")
    if not isinstance(heading_mode, str) or heading_mode.lower() not in ("ai", "manual", "none"):
        heading_mode = "none"
    else:
        heading_mode = heading_mode.lower()
    heading_text = data.get("heading_text", "")
    if not isinstance(heading_text, str):
        heading_text = ""

    # Source resolution: "video" | "live" | "auto" (default "auto").
    source = data.get("source", "auto")
    if not isinstance(source, str) or source.lower() not in ("video", "live", "auto"):
        source = "auto"
    else:
        source = source.lower()

    job_id = uuid.uuid4().hex
    jobs_status[job_id] = {
        "status": "Queued",
        "message": "Job queued",
        "progress": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "result": None,
        "heading": None,
        "job_data": None,
    }

    thread = threading.Thread(
        target=generate_clip_job,
        args=(job_id, youtube_url, clip_duration, context_before,
              heading_mode, heading_text, source),
        daemon=True,
    )
    thread.start()

    prune_old_jobs()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    """Return the live status of a job."""
    meta = jobs_status.get(job_id)
    if meta is None:
        return jsonify({"status": "Error", "message": "Unknown job ID"}), 404

    payload = {
        "status": meta.get("status"),
        "message": meta.get("message", ""),
        "progress": meta.get("progress", 0),
        "result": meta.get("result"),
    }
    return jsonify(payload)


@app.route("/download/<job_id>/<path:filename>")
def download(job_id, filename):
    """Serve a generated clip for preview / download."""
    job_dir = get_job_path(job_id)
    # Prevent path traversal: only serve files that live in the job dir.
    safe_name = os.path.basename(filename)
    target = os.path.join(job_dir, safe_name)
    if not os.path.isfile(target):
        abort(404)
    # ?download=1 forces a browser download; default stays inline preview.
    as_attachment = request.args.get("download", "").lower() in ("1", "true", "yes")
    return send_from_directory(
        job_dir, safe_name,
        as_attachment=as_attachment,
        download_name=safe_name,
    )


# ===== YouTube Upload Feature =====

# Matches files this app generates: <11-char video id>_clip.mp4
GENERATED_CLIP_RE = re.compile(r"^[A-Za-z0-9_-]{11}_clip\.mp4$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Client-side safe error codes for the OAuth redirect (no secrets, no detail).
YT_REDIRECT_ERRORS = {
    "not_configured": "not_configured",
    "access_denied": "access_denied",
    "invalid_state": "invalid_state",
    "invalid_callback": "invalid_callback",
    "token_exchange_failed": "token_exchange_failed",
    "flow_init_failed": "flow_init_failed",
    "bad_client_secret": "bad_client_secret",
    "not_installed": "not_installed",
    "service_build_failed": "service_build_failed",
    "channel_lookup_failed": "channel_lookup_failed",
}


def _yt_error_code(exc):
    """Map an exception to a short, safe redirect/query error code."""
    raw = exc.args[0] if getattr(exc, "args", None) else "error"
    return YT_REDIRECT_ERRORS.get(raw, "error")


def _set_upload(job_id, **fields):
    """Merge fields into a job's upload record and stamp updated_at."""
    rec = uploads_status.get(job_id)
    if rec is None:
        rec = {"created_at": datetime.now().isoformat()}
        uploads_status[job_id] = rec
    rec.update(fields)
    rec["updated_at"] = datetime.now().isoformat()
    return rec


def _upload_error(job_id, message):
    _set_upload(job_id, status="Error", error=message, message=message)


def _run_upload(job_id, file_path, title, description, privacy):
    """Background thread: perform the resumable upload and record progress."""

    def on_progress(pct):
        _set_upload(job_id, progress=int(pct), message="Uploading\u2026 %d%%" % int(pct))

    try:
        creds = youtube_auth.load_credentials()
        if not creds:
            _upload_error(job_id, "not_connected")
            return
        result = youtube_upload.upload_video(
            creds, file_path, title, description, privacy, progress_cb=on_progress
        )
        _set_upload(
            job_id,
            status="Complete",
            progress=100,
            message="Upload complete",
            video_id=result.get("video_id"),
            url=result.get("url"),
            error=None,
        )
    except youtube_upload.YouTubeUploadError as exc:
        _upload_error(job_id, str(exc.args[0]) if exc.args else "upload_failed")
    except Exception:
        # Never leak internals to the browser or logs with secrets.
        _upload_error(job_id, "upload_failed")


@app.route("/youtube/status")
def youtube_status():
    """Report whether a Google account is connected and which channel it is."""
    data = {"configured": False, "connected": False, "channel": None, "error": None}
    try:
        data["configured"] = youtube_auth.is_configured()
        if data["configured"]:
            creds = youtube_auth.load_credentials()
            if creds:
                data["connected"] = True
                try:
                    data["channel"] = youtube_auth.get_connected_channel(creds)
                except Exception:
                    # Token is valid enough to be connected even if the channel
                    # name lookup fails (e.g. transient API error).
                    data["channel"] = None
    except youtube_auth.YouTubeAuthError:
        data["error"] = "not_installed"
    return jsonify(data)


@app.route("/youtube/connect")
def youtube_connect():
    """Redirect the browser to Google's OAuth consent screen."""
    try:
        auth_url, _state = youtube_auth.start_oauth_flow()
    except youtube_auth.YouTubeAuthError as exc:
        return redirect("/?youtube_error=" + _yt_error_code(exc))
    return redirect(auth_url)


@app.route("/youtube/oauth2callback")
def youtube_oauth2callback():
    """OAuth redirect target: exchange the code for tokens, then return home."""
    if request.args.get("error"):
        return redirect("/?youtube_error=access_denied")
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    try:
        youtube_auth.complete_oauth_flow(state, code)
    except youtube_auth.YouTubeAuthError as exc:
        return redirect("/?youtube_error=" + _yt_error_code(exc))
    return redirect("/?youtube=connected")


@app.route("/youtube/disconnect", methods=["POST"])
def youtube_disconnect():
    """Disconnect the current Google account (deletes the stored token)."""
    youtube_auth.clear_credentials()
    return jsonify({"ok": True})


@app.route("/youtube/upload", methods=["POST"])
def youtube_upload_start():
    """Start uploading a clip this app generated.

    Only files produced by this app (matched against the job's own record) can
    be uploaded. Requires explicit confirmation and rejects duplicate uploads.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_request"}), 400

    job_id = data.get("job_id")
    filename = data.get("filename")
    title = (data.get("title") or "").strip() if isinstance(data.get("title"), str) else ""
    description = data.get("description")
    if not isinstance(description, str):
        description = ""
    privacy = data.get("privacy") or "private"
    confirm = data.get("confirm") is True

    # --- validation -----------------------------------------------------
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        return jsonify({"error": "invalid_job"}), 400

    job = jobs_status.get(job_id)
    if not job or job.get("status") != "Complete":
        return jsonify({"error": "clip_not_ready"}), 400
    expected_name = (job.get("result") or {}).get("filename")
    if not filename or filename != expected_name or not GENERATED_CLIP_RE.match(filename or ""):
        # Only files this app produced for this job may be uploaded.
        return jsonify({"error": "invalid_file"}), 400

    if not confirm:
        return jsonify({"error": "confirm_required"}), 400

    if not title:
        return jsonify({"error": "title_required"}), 400
    if len(title) > 100:
        return jsonify({"error": "title_too_long"}), 400
    if not isinstance(description, str) or len(description) > 5000:
        return jsonify({"error": "description_too_long"}), 400
    if privacy not in youtube_upload.ALLOWED_PRIVACY:
        privacy = "private"

    if not youtube_auth.is_configured():
        return jsonify({"error": "not_configured"}), 400
    try:
        if not youtube_auth.load_credentials():
            return jsonify({"error": "not_connected"}), 401
    except youtube_auth.YouTubeAuthError:
        return jsonify({"error": "not_installed"}), 400

    # --- duplicate prevention ------------------------------------------
    existing = uploads_status.get(job_id)
    if existing and existing.get("status") in ("Uploading", "Complete"):
        return jsonify({
            "job_id": job_id,
            "status": existing.get("status"),
            "duplicate": True,
            "url": existing.get("url"),
        })

    # --- resolve the file safely inside the job directory --------------
    job_dir = get_job_path(job_id)
    file_path = os.path.join(job_dir, os.path.basename(filename))
    if not os.path.isfile(file_path):
        return jsonify({"error": "file_missing"}), 404

    _set_upload(
        job_id,
        status="Uploading",
        progress=0,
        message="Starting upload\u2026",
        video_id=None,
        url=None,
        error=None,
    )

    thread = threading.Thread(
        target=_run_upload,
        args=(job_id, file_path, title, description, privacy),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "Uploading"})


@app.route("/youtube/upload/status/<job_id>")
def youtube_upload_status(job_id):
    """Return the live status of a clip's upload."""
    rec = uploads_status.get(job_id)
    if rec is None:
        return jsonify({"status": "None"}), 404
    payload = {
        "status": rec.get("status"),
        "progress": rec.get("progress", 0),
        "message": rec.get("message", ""),
        "video_id": rec.get("video_id"),
        "url": rec.get("url"),
        "error": rec.get("error"),
    }
    return jsonify(payload)


# ===== Health & Misc =====

@app.route("/health")
def health():
    """Lightweight liveness probe (no side effects)."""
    return jsonify({"status": "ok"})


@app.route("/favicon.ico")
def favicon():
    """Empty response so browsers stop logging noisy 404s."""
    return "", 204


# ===== Cache policy & JSON error handling =====

# Polling endpoints must never be cached.
NO_STORE_PREFIXES = (
    "/status/", "/jobs/", "/generate", "/youtube/",
    "/heading/", "/regenerate-heading/", "/live-status",
)
# Paths that should answer with JSON instead of Flask HTML error pages.
API_PREFIXES = (
    "/status/", "/jobs/", "/generate", "/download/", "/youtube/",
    "/heading/", "/regenerate-heading/", "/live-status",
)


def is_api_path(path):
    """True if the path belongs to the JSON API surface."""
    return path == "/jobs" or path.startswith(API_PREFIXES)

@app.after_request
def add_cache_headers(response):
    """Disable caching for polling endpoints; leave other caching intact."""
    path = request.path
    if path == "/jobs" or path.startswith(NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(404)
def handle_404(e):
    """JSON 404 for API paths, default HTML 404 for page routes."""
    if is_api_path(request.path):
        return jsonify({"error": "Not found"}), 404
    return e.get_response()


@app.errorhandler(405)
def handle_405(e):
    """JSON 405 for API paths, default HTML 405 for page routes."""
    if is_api_path(request.path):
        return jsonify({"error": "Method not allowed"}), 405
    return e.get_response()


@app.errorhandler(500)
def handle_500(e):
    """Log and return JSON 500 for API paths, default HTML otherwise."""
    app.logger.error("Internal server error on %s: %s", request.path, e)
    if is_api_path(request.path):
        return jsonify({"error": "Internal server error"}), 500
    return e.get_response()


if __name__ == "__main__":
    ensure_jobs_dir()
    print("YouTube Vertical Clipper - Web UI")
    print("=" * 50)
    print("Open your browser at: http://127.0.0.1:5000")
    # debug=False and threaded=True so background jobs run concurrently
    # without the Werkzeug reloader spawning duplicate processes.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)