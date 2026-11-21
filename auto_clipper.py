"""
Auto-cut and auto-edit YouTube videos into vertical (9:16) comedy clips.

Features:
- Prompts for YouTube URL at startup
- Extracts video ID from the URL
- Downloads video with yt-dlp CLI
- Detects the best moment from YouTube "Most Replayed" heatmap data
  (via the yt-heatmap-clipper repo), falling back to audio energy analysis
- Renders as 9:16 vertical clip (720x1280) with smart center-preserving crop
- Cleans up the full video after clipping
- Uses YouTube video title for unique output filename
"""

import subprocess
import os
import re
import shutil
import tempfile
import textwrap
import warnings
import sys
from collections import deque

import numpy as np
from moviepy import VideoFileClip

from heatmap_bridge import best_heatmap_segment

HEATMAP_PADDING = 10
MAX_DOWNLOAD_HEIGHT = 1080

# Folder (inside this clip directory) where downloaded videos are stored.
CLIP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Clip-video")
CLIP_VIDEO_BASENAME = "full_video"

COOKIES_FILENAME = "cookies.txt"
_COOKIE_STRATEGY = None


def find_cookies_file():
    """Look for cookies.txt next to this script and in the current directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, COOKIES_FILENAME),
                 os.path.join(os.getcwd(), COOKIES_FILENAME)):
        if os.path.isfile(path):
            return path
    return None


def installed_browsers():
    """Return the browsers yt-dlp could read cookies from on this machine."""
    if os.name == "nt":
        candidates = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ],
            "edge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
            "firefox": [
                r"C:\Program Files\Mozilla Firefox\firefox.exe",
                r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
            ],
            "brave": [
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            ],
        }
        return [n for n, paths in candidates.items() if any(os.path.exists(p) for p in paths)]
    return [n for n in ("chrome", "edge", "firefox", "brave") if shutil.which(n)]


def ytdlp_cookie_strategies():
    """Ordered cookie argument sets to try when calling yt-dlp."""
    strategies = []
    cookies = find_cookies_file()
    if cookies:
        strategies.append(["--cookies", cookies])
    strategies.extend(["--cookies-from-browser", b] for b in installed_browsers())
    strategies.append([])          # last resort: no cookies
    return strategies


def is_bot_error(result):
    """True when yt-dlp failed because of YouTube's anti-bot check."""
    if result is None:
        return False
    blob = ((result.stderr or "") + (result.stdout or "")).lower()
    return "not a bot" in blob or "sign in to confirm" in blob


def print_bot_help():
    """Explain how to fix YouTube's 'confirm you're not a bot' block."""
    here = os.path.dirname(os.path.abspath(__file__))
    print()
    print("YouTube blocked this request: \"Sign in to confirm you're not a bot\".")
    print("This is an IP-level anti-bot check, not a bug in the script.")
    print()
    print("Fix it by giving yt-dlp your YouTube cookies:")
    print("  1. Install the 'Get cookies.txt LOCALLY' extension in Chrome or Edge.")
    print("  2. Open https://www.youtube.com while logged in, then click the extension.")
    print(f"  3. Save the file as: {os.path.join(here, COOKIES_FILENAME)}")
    print()
    print("The script picks it up automatically on the next run.")
    print("Alternatively, set the YTDLP_PROXY environment variable to a working proxy.")


def run_ytdlp(args, url, timeout=600):
    """
    Run yt-dlp, trying each available cookie source until one succeeds.
    The working source is cached for the rest of the session.
    """
    global _COOKIE_STRATEGY
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        args = ["--proxy", proxy] + args
    strategies = ([_COOKIE_STRATEGY] if _COOKIE_STRATEGY is not None
                  else ytdlp_cookie_strategies())
    cmd = [sys.executable, "-m", "yt_dlp"] + args
    last = None
    for extra in strategies:
        try:
            result = subprocess.run(cmd + extra + [url], capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = None
            continue
        last = result
        if result.returncode == 0:
            _COOKIE_STRATEGY = extra
            return result
    return last


def get_ffmpeg_exe():
    """Get the bundled ffmpeg path."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from a URL."""
    m = re.search(r"(?:v=|\/v\/)([^&\n\?]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be\/([^&\n\?]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com\/shorts\/([^&\n\?]{11})", url)
    if m:
        return m.group(1)
    raise ValueError("Invalid YouTube URL — could not extract video ID.")


def is_file_valid(filepath: str) -> bool:
    """Check if a video file is valid and readable by MoviePy."""
    try:
        clip = VideoFileClip(filepath)
        duration = clip.duration
        clip.close()
        return bool(duration and duration > 0)
    except Exception:
        return False


def download_video(url: str) -> tuple[str, str]:
    """Download a YouTube video using yt-dlp CLI into the Clip-video folder."""
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    os.makedirs(CLIP_DIR, exist_ok=True)
    output_template = os.path.join(CLIP_DIR, f"{CLIP_VIDEO_BASENAME}.%(ext)s")

    print(f"1. Downloading video: {url}")
    print(f"   Saving into: {CLIP_DIR}")

    # Download best video + best audio separately as WebM.
    # MoviePy can read WebM natively, avoiding the corrupted MP4 merge issue
    # that occurred with --merge-output-format mp4.
    args = [
        '--ffmpeg-location', ffmpeg_exe,
        '--quiet', '--no-warnings',
        # Live streams: download from the start of the broadcast instead of the
        # live edge. Ignored for regular (already-uploaded) videos.
        '--live-from-start',
        '--output', output_template,
        '--format',
        f'bestvideo[height<={MAX_DOWNLOAD_HEIGHT}]+bestaudio/'
        f'best[height<={MAX_DOWNLOAD_HEIGHT}]/best',
    ]

    result = run_ytdlp(args, url, timeout=600)

    if result is None:
        raise RuntimeError("yt-dlp timed out while downloading")
    if result.returncode != 0:
        if is_bot_error(result):
            print_bot_help()
        raise RuntimeError(f"yt-dlp download failed: {(result.stderr or '')[:200]}")

    # Extract video title
    video_title = "unknown_video"
    try:
        info_result = run_ytdlp(
            ['--ffmpeg-location', ffmpeg_exe, '--quiet', '--no-warnings',
             '--dump-json'],
            url, timeout=60,
        )
        if info_result is not None and info_result.returncode == 0:
            import json
            try:
                info = json.loads(info_result.stdout)
                video_title = info.get('title', 'unknown_video')
            except Exception:
                pass
    except Exception:
        pass

    safe_title = "".join(c for c in video_title if c.isalnum() or c in (' ', '-', '_')).rstrip()
    safe_title = safe_title.replace(' ', '_')[:50]
    # Remove trailing dots that can result from sanitization
    safe_title = safe_title.rstrip('.')

    print(f"   Downloaded title: {video_title}")

    # Find the downloaded file
    downloaded_file = None
    for ext in ['webm', 'mp4', 'mkv']:
        candidate = os.path.join(CLIP_DIR, f"{CLIP_VIDEO_BASENAME}.{ext}")
        if os.path.exists(candidate):
            downloaded_file = candidate
            break

    if downloaded_file is None:
        # List what's in the download folder
        for f in os.listdir(CLIP_DIR):
            if f.startswith(CLIP_VIDEO_BASENAME) and not f.endswith('.part'):
                downloaded_file = os.path.join(CLIP_DIR, f)
                break

    if downloaded_file is None:
        raise RuntimeError("Downloaded file not found after yt-dlp execution")

    # Verify file is valid for MoviePy
    if not is_file_valid(downloaded_file):
        print(f"   Warning: {downloaded_file} downloaded but MoviePy read issue")

    print("   Download complete.")
    return downloaded_file, safe_title


def analyze_audio_energy(
    video_clip: VideoFileClip,
    sample_rate: int = 11025,
    window_size: float = 2.0,
    threshold_quantile: float = 0.85,
) -> int:
    """Analyze audio to find the funniest/loudest moment."""
    total_duration = int(video_clip.duration)
    if total_duration < 30:
        warnings.warn("Video too short for analysis; defaulting to 0s start.")
        return 0
    audio = video_clip.audio
    if audio is None:
        warnings.warn("No audio track found; defaulting to 0s start.")
        return 0
    energies = deque(maxlen=int(total_duration / window_size) + 1)
    step = max(1, int(window_size))
    for t in range(0, int(total_duration) - step, step):
        end = min(t + window_size, total_duration)
        sub_audio = audio.subclipped(t, end)
        arr = np.array(sub_audio.to_soundarray(fps=sample_rate))
        if arr.ndim > 1:
            arr = np.mean(arr, axis=1)
        energy = float(np.sqrt(np.mean(arr ** 2)))
        energies.append((t, energy))
    if not energies:
        return 0
    energy_vals = np.array([e[1] for e in energies])
    threshold = np.quantile(energy_vals, threshold_quantile)
    best_t, best_energy = max(energies, key=lambda item: item[1])
    best_start = max(0, min(int(best_t), max(0, total_duration - 1)))
    print(f"   Audio analysis complete. Best moment at ~{best_start}s (energy={best_energy:.3f}, threshold={threshold:.3f})")
    return best_start


def detect_best_moment(
    video_path: str,
    clip_duration: int = 45,
    video_id: str = None,
) -> tuple:
    """Find the best moment, preferring YouTube heatmap data over audio energy."""
    print("2. Looking for the most replayed / loudest moment...")
    video = VideoFileClip(video_path)
    total_duration = int(video.duration)

    if video_id:
        try:
            segment = best_heatmap_segment(video_id, max_duration=clip_duration)
        except Exception as exc:
            print(f"   Heatmap lookup failed ({exc}); falling back to audio analysis.")
            segment = None

        if segment:
            start = max(0, int(segment["start"]) - HEATMAP_PADDING)
            end = min(
                int(segment["start"] + segment["duration"]) + HEATMAP_PADDING,
                total_duration,
            )
            if end - start < clip_duration:
                end = min(start + clip_duration, total_duration)
            if end - start >= 5:
                print(
                    f"   Heatmap match: score={segment['score']:.2f} "
                    f"at {int(segment['start'])}s"
                )
                video.close()
                return start, end, total_duration

        print("   No usable heatmap data; falling back to audio energy analysis.")

    best_start = analyze_audio_energy(video)
    best_end = min(best_start + clip_duration, total_duration)
    video.close()
    return best_start, best_end, total_duration


DEFAULT_HEADING_STYLE = {
    "font_size": 54,
    "max_chars": 26,
    "max_lines": 2,
    "box_opacity": 0.55,
    "y": 120,
    "font_file": None,
}

_HEADING_TEMP_DIRS = []


def _resolve_heading_style(style):
    """Merge a partial heading style dict with the module defaults."""
    resolved = dict(DEFAULT_HEADING_STYLE)
    if style:
        for key, value in style.items():
            if value is not None:
                resolved[key] = value
    resolved["font_size"] = int(resolved["font_size"])
    resolved["max_chars"] = max(1, int(resolved["max_chars"]))
    resolved["max_lines"] = max(1, int(resolved["max_lines"]))
    resolved["box_opacity"] = float(resolved["box_opacity"])
    resolved["y"] = int(resolved["y"])
    return resolved


def find_font_path(explicit: str = None) -> str | None:
    """Resolve a bold sans-serif TTF for burned-in headings, or None."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    env_font = os.environ.get("CLIPPER_FONT")
    if env_font and os.path.isfile(env_font):
        return env_font
    for candidate in (
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\seguisb.ttf",
    ):
        if os.path.isfile(candidate):
            return candidate
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(fonts_dir):
        try:
            for name in sorted(os.listdir(fonts_dir)):
                if name.lower().endswith(".ttf"):
                    return os.path.join(fonts_dir, name)
        except OSError:
            pass
    return None


def _wrap_heading_text(text, max_chars, max_lines):
    """Wrap heading text to max_chars per line, capped at max_lines lines."""
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return []
    lines = textwrap.wrap(
        cleaned,
        width=max(1, max_chars),
        break_long_words=True,
        break_on_hyphens=False,
    ) or [cleaned]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        if len(tail) > max_chars - 1:
            tail = tail[: max_chars - 1]
        lines[-1] = tail.rstrip() + "\u2026"
    return lines


def _filter_path(value):
    """Escape a Windows path for an FFmpeg filter argument (colon escaped)."""
    return str(value).replace("\\", "/").replace(":", "\\\\:").replace("'", "")


def build_drawtext_filter(text, style=None) -> str:
    """Build a drawtext filter that burns wrapped heading text into a frame.

    The wrapped text is written to a temp file and referenced with textfile=,
    which avoids FFmpeg's fragile inline text escaping entirely. The temp
    directory is removed after rendering by _cleanup_heading_tempfiles().
    """
    resolved = _resolve_heading_style(style)
    font_path = find_font_path(resolved.get("font_file"))
    if not font_path:
        raise ValueError(
            "no usable font found for the heading (set CLIPPER_FONT or pass "
            "heading_style={'font_file': ...})"
        )
    lines = _wrap_heading_text(text, resolved["max_chars"], resolved["max_lines"])
    if not lines:
        return ""
    tmp_dir = tempfile.mkdtemp(prefix="clipper_heading_")
    _HEADING_TEMP_DIRS.append(tmp_dir)
    text_path = os.path.join(tmp_dir, "heading.txt")
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    parts = [
        "fontfile=" + _filter_path(font_path),
        "textfile=" + _filter_path(text_path),
        "fontsize=%d" % resolved["font_size"],
        "fontcolor=white",
        "box=1",
        "boxcolor=black@%s" % resolved["box_opacity"],
        "boxborderw=22",
        "x=(w-text_w)/2",
        "y=%d" % resolved["y"],
        "line_spacing=10",
        "expansion=none",
    ]
    return "drawtext=" + ":".join(parts)


def _cleanup_heading_tempfiles():
    """Delete temp dirs created for heading text files."""
    while _HEADING_TEMP_DIRS:
        tmp_dir = _HEADING_TEMP_DIRS.pop()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _apply_heading_filter(crop_filter, heading, heading_style):
    """Return the full -vf string, or just crop_filter if heading is unusable."""
    if not heading or not str(heading).strip():
        return crop_filter
    try:
        drawtext_filter = build_drawtext_filter(heading, heading_style)
    except Exception as exc:
        print(
            f"   WARNING: heading could not be rendered ({exc}); "
            f"rendering without a heading."
        )
        return crop_filter
    if not drawtext_filter:
        return crop_filter
    print("   Burned-in heading enabled.")
    return crop_filter + "," + drawtext_filter


def render_vertical_clip(
    video_path: str,
    best_start: int,
    best_end: int,
    output_path: str = "tmkoc_viral_clip.mp4",
    heading: str = None,
    heading_style: dict = None,
) -> str:
    """Render a 9:16 (720x1280) vertical clip with a center crop.

    When ``heading`` is a non-empty string it is burned in near the top of the
    frame. Passing ``heading=None`` (the default) keeps the original behaviour
    and produces the same ffmpeg command as before.
    """
    ffmpeg_exe = get_ffmpeg_exe()
    print(f"3. Rendering 9:16 vertical clip from {best_start}s to {best_end}s...")
    # Scale to width 1280 (height scales automatically to maintain aspect ratio),
    # then center-crop to 720x1280 (9:16 aspect ratio).
    crop_filter = "scale=-2:1280,crop=720:1280:(iw-720)/2:(ih-1280)/2,setsar=1"
    print(f"   Using crop filter: {crop_filter}")
    try:
        video_filter = _apply_heading_filter(crop_filter, heading, heading_style)
        cmd = [
            ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(best_start), "-to", str(best_end),
            "-i", video_path, "-vf", video_filter,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", output_path,
        ]
        subprocess.run(cmd, check=True)
    finally:
        _cleanup_heading_tempfiles()
    return output_path


def burn_heading_into_clip(
    source_clip_path: str,
    output_path: str,
    heading: str,
    heading_style: dict = None,
) -> str:
    """Re-encode an existing clip with a burned-in heading.

    The whole source clip is re-encoded (no trimming) and the source file is
    never modified or deleted; only ``output_path`` is written.
    """
    ffmpeg_exe = get_ffmpeg_exe()
    if not os.path.isfile(source_clip_path):
        raise FileNotFoundError(f"source clip not found: {source_clip_path}")
    print(f"   Re-rendering with heading: {source_clip_path} -> {output_path}")
    crop_filter = "scale=-2:1280,crop=720:1280:(iw-720)/2:(ih-1280)/2,setsar=1"
    try:
        video_filter = _apply_heading_filter(crop_filter, heading, heading_style)
        cmd = [
            ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
            "-i", source_clip_path, "-vf", video_filter,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", output_path,
        ]
        subprocess.run(cmd, check=True)
    finally:
        _cleanup_heading_tempfiles()
    return output_path

def auto_clip_video(url: str, clip_duration: int = 45) -> str:
    """Full pipeline: download → analyze → render → cleanup."""
    video_id = extract_video_id(url)
    video_path, video_title = download_video(url)

    try:
        best_start, best_end, total_duration = detect_best_moment(
            video_path, clip_duration, video_id
        )
        safe_title = video_title or "clip"
        output_path = render_vertical_clip(
            video_path, best_start, best_end, f"{safe_title}_clip.mp4"
        )
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
            print("4. Removed full video file.")
        for leftover in [f for f in os.listdir(CLIP_DIR) if f.startswith(CLIP_VIDEO_BASENAME) and f.endswith(".part")]:
            try:
                os.remove(os.path.join(CLIP_DIR, leftover))
            except OSError:
                pass

    print(f"DONE! Your vertical clip is saved as: {output_path}")
    return output_path


if __name__ == "__main__":
    print("Auto Clipper - YouTube to Vertical Comedy Clip Converter")
    print("=" * 50)
    url = input("Enter YouTube URL: ").strip()
    try:
        video_id = extract_video_id(url)
        print(f"   Video ID: {video_id}")
    except ValueError as e:
        print(f"   Error: {e}")
        print("   Supported formats:")
        print("   - youtube.com/watch?v=VIDEO_ID")
        print("   - youtu.be/VIDEO_ID")
        print("   - youtube.com/shorts/VIDEO_ID")
        exit(1)
    try:
        result = auto_clip_video(url)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nError: {exc}")
        sys.exit(1)
    print(f"\nClip ready at: {result}")