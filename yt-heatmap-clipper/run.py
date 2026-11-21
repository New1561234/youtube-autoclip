import os
import re
import json
import sys
import subprocess
import requests
import shutil
from urllib.parse import urlparse, parse_qs
import warnings

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# FFmpeg discovery
# ---------------------------------------------------------------------------
def get_ffmpeg_exe():
    """
    Locate an FFmpeg executable.
    Prefers one on PATH, then falls back to imageio-ffmpeg's bundled build.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = get_ffmpeg_exe()

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OUTPUT_DIR = "clips"       # Directory where generated clips are saved
MAX_DURATION = 60          # Maximum duration (seconds) of a single clip
MIN_SCORE = 0.40           # Minimum heatmap intensity score to be considered viral
MAX_CLIPS = 10             # Maximum number of clips generated per video
PADDING = 10               # Extra seconds kept before/after each detected segment

OUTPUT_WIDTH = 1080        # Short-form canvas width  (TikTok / Reels / Shorts)
OUTPUT_HEIGHT = 1920       # Short-form canvas height

# Split layouts: top (main content) + bottom (facecam) must equal OUTPUT_HEIGHT
TOP_HEIGHT = 1600
BOTTOM_HEIGHT = 320

USE_SUBTITLE = True        # Enable auto subtitles using Faster-Whisper
WHISPER_MODEL = "small"    # tiny, base, small, medium, large

# --- Top topic / hook header banner ---------------------------------------
HEADER_FONT_SIZE = 52
HEADER_Y = 180                                          # top safe zone
HEADER_BOX = "box=1:boxcolor=black@0.8:boxborderw=24"
HEADER_MAX_CHARS = 26                                   # per line
HEADER_MAX_LINES = 2

# --- Colour pop ------------------------------------------------------------
COLOR_EQ = "eq=contrast=1.08:saturation=1.18:brightness=0.02"

# --- Dynamic punch-in zoom -------------------------------------------------
PUNCH_ZOOM_MAX = 1.08          # zoom from 1.00x up to 1.08x
PUNCH_ZOOM_PERIOD = 7.0        # seconds per zoom cycle

# --- Viral pop-up subtitle style ------------------------------------------
# ffmpeg converts SRT to ASS with PlayResY=288, so force_style values are in
# 288p units and are scaled up to the 1080x1920 canvas.  MarginV is therefore
# converted from the requested 320 px safe-zone offset.
SUBTITLE_MARGIN_PX = 320
SUBTITLE_MARGIN_V = round(SUBTITLE_MARGIN_PX * 288 / OUTPUT_HEIGHT)   # 48
SUBTITLE_STYLE = (
    "FontName=Arial,FontSize=24,Bold=1,"
    "PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=3,Shadow=1,Alignment=2,"
    f"MarginV={SUBTITLE_MARGIN_V}"
)


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------
def extract_video_id(url):
    """
    Extract the YouTube video ID from a given URL.
    Supports standard YouTube URLs, shortened URLs, and Shorts URLs.
    """
    parsed = urlparse(url)

    if parsed.hostname in ("youtu.be", "www.youtu.be"):
        return parsed.path[1:]

    if parsed.hostname in ("youtube.com", "www.youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/"):
            return parse_qs(parsed.query).get("v", [None])[0] or parsed.path.split("/")[2]

    return None


def get_model_size(model):
    """Get the approximate size of a Whisper model."""
    sizes = {
        "tiny": "75 MB",
        "base": "142 MB",
        "small": "466 MB",
        "medium": "1.5 GB",
        "large-v1": "2.9 GB",
        "large-v2": "2.9 GB",
        "large-v3": "2.9 GB",
    }
    return sizes.get(model, "unknown size")


def cek_dependensi(install_whisper=False):
    """
    Ensure required dependencies are available.
    Updates yt-dlp only when it is missing and checks FFmpeg availability.
    """
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("📦 Installing yt-dlp...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if install_whisper:
        try:
            import faster_whisper  # noqa: F401
            print("✅ Faster-Whisper package installed.")

            cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
            model_name = f"faster-whisper-{WHISPER_MODEL}"
            model_cached = False
            if os.path.exists(cache_dir):
                try:
                    model_cached = any(
                        model_name in item.lower() for item in os.listdir(cache_dir)
                    )
                except Exception:
                    pass

            if model_cached:
                print(f"✅ Model '{WHISPER_MODEL}' already cached and ready.\n")
            else:
                print(f"⚠️  Model '{WHISPER_MODEL}' not found in cache.")
                print(f"   📥 Will auto-download ~{get_model_size(WHISPER_MODEL)} on first transcribe.\n")
        except ImportError:
            print("📦 Installing Faster-Whisper package...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "faster-whisper"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("✅ Faster-Whisper package installed successfully.\n")

    if not FFMPEG:
        print("FFmpeg not found. Please install FFmpeg and ensure it is in PATH.")
        sys.exit(1)


def ambil_most_replayed(video_id):
    """
    Fetch and parse YouTube 'Most Replayed' heatmap data.
    Returns a list of high-engagement segments.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {"User-Agent": "Mozilla/5.0"}

    print("Reading YouTube heatmap data...")

    try:
        html = requests.get(url, headers=headers, timeout=20).text
    except Exception:
        return []

    match = re.search(
        r'"markers":\s*(\[.*?\])\s*,\s*"?markersMetadata"?',
        html,
        re.DOTALL,
    )
    if not match:
        return []

    try:
        markers = json.loads(match.group(1).replace('\\"', '"'))
    except Exception:
        return []

    results = []
    for marker in markers:
        if "heatMarkerRenderer" in marker:
            marker = marker["heatMarkerRenderer"]
        try:
            score = float(marker.get("intensityScoreNormalized", 0))
            if score >= MIN_SCORE:
                results.append({
                    "start": float(marker["startMillis"]) / 1000,
                    "duration": min(float(marker["durationMillis"]) / 1000, MAX_DURATION),
                    "score": score,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def get_duration(video_id):
    """Retrieve the total duration of a YouTube video in seconds."""
    url = f"https://youtu.be/{video_id}"
    try:
        res = run_ytdlp(["--get-duration"], url, timeout=60)
        if res is None:
            return 3600

        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not lines:
            return 3600

        value = lines[-1]

        # yt-dlp prints whole seconds; older versions printed MM:SS / HH:MM:SS
        if value.isdigit():
            return int(value)

        time_parts = value.split(":")
        if len(time_parts) == 2:
            return int(time_parts[0]) * 60 + int(time_parts[1])
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
    except Exception:
        pass

    return 3600


def get_video_title(video_id):
    """Fetch the YouTube video title once (used for the header and filename)."""
    url = f"https://youtu.be/{video_id}"
    try:
        res = run_ytdlp(["--quiet", "--no-warnings", "--dump-json"], url, timeout=60)
        if res is not None and res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout).get("title") or "unknown_video"
    except Exception:
        pass
    return "unknown_video"


# ---------------------------------------------------------------------------
# RMS audio-energy fallback (used when a video has no heatmap data)
# ---------------------------------------------------------------------------
def analyze_audio_energy_fallback(media_file, sample_rate=11025, window_size=2.0):
    """
    Fallback RMS audio-energy peak detector using MoviePy.
    Returns the start time (seconds) of the loudest window, or 0 on failure.
    Works with audio-only files as well as video files.
    """
    try:
        from moviepy import AudioFileClip
        import numpy as np

        audio = AudioFileClip(media_file)
        arr = np.array(audio.to_soundarray(fps=sample_rate))
        audio.close()

        if arr.ndim > 1:
            arr = np.mean(arr, axis=1)

        win = int(window_size * sample_rate)
        if arr.size < win * 2 or win <= 0:
            print("   Audio too short for analysis; defaulting to 0s start.")
            return 0

        best_t, best_energy = 0.0, -1.0
        for i in range(0, arr.size - win + 1, win):
            segment = arr[i:i + win]
            energy = float(np.sqrt(np.mean(segment * segment)))
            if energy > best_energy:
                best_energy = energy
                best_t = i / sample_rate

        print(f"   Audio analysis complete. Best moment at ~{int(best_t)}s "
              f"(energy={best_energy:.3f})")
        return int(best_t)
    except Exception as e:
        print(f"   Audio energy analysis failed: {e}")
        return 0


def download_audio_for_analysis(video_id):
    """Download only the audio track (small + fast) for energy analysis."""
    url = f"https://youtu.be/{video_id}"
    result = run_ytdlp(
        [
            "--quiet", "--no-warnings",
            "--ffmpeg-location", FFMPEG or "ffmpeg",
            "-f", "bestaudio/best",
            "-o", "temp_energy.%(ext)s",
        ],
        url,
        timeout=300,
    )
    if result is None or result.returncode != 0:
        if is_bot_error(result):
            print_bot_help()
        return None

    for ext in ("webm", "m4a", "opus", "mp3", "aac", "flac", "wav"):
        candidate = f"temp_energy.{ext}"
        if os.path.exists(candidate):
            return candidate
    for name in os.listdir("."):
        if name.startswith("temp_energy.") and not name.endswith(".part"):
            return name
    return None


# ---------------------------------------------------------------------------
# Faster-Whisper subtitle generation
# ---------------------------------------------------------------------------
def _patch_av_open():
    """
    PyAV 19 removed av.open(..., metadata_errors=...), which faster-whisper
    still passes. Wrap av.open so that kwarg is dropped on PyAV builds that
    no longer accept it.
    """
    try:
        import av
    except Exception:
        return

    if getattr(av.open, "_yf_compat", False):
        return

    original = av.open

    def open_compat(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except TypeError as exc:
            if "metadata_errors" in str(exc):
                kwargs.pop("metadata_errors", None)
                return original(*args, **kwargs)
            raise

    open_compat._yf_compat = True
    av.open = open_compat


def format_timestamp(seconds):
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_subtitle(video_file, subtitle_file):
    """
    Generate an SRT subtitle file using Faster-Whisper for the given video.
    Text is upper-cased for modern short-form captions.
    Returns True if at least one cue was written.
    """
    try:
        from faster_whisper import WhisperModel

        _patch_av_open()

        print(f"  Loading Faster-Whisper model '{WHISPER_MODEL}'...")
        print(f"  (If this is first time, downloading ~{get_model_size(WHISPER_MODEL)}...)")
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

        print("  ✅ Model loaded. Transcribing audio...")
        segments, info = model.transcribe(video_file, language="id")

        print("  Generating subtitle file...")
        cue_count = 0
        with open(subtitle_file, "w", encoding="utf-8") as f:
            for segment in segments:
                text = " ".join(segment.text.strip().upper().split())
                if not text:
                    continue
                f.write(f"{cue_count + 1}\n")
                f.write(f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}\n")
                f.write(f"{text}\n\n")
                cue_count += 1

        if cue_count == 0:
            # Music-only section: an empty .srt makes the subtitles filter fail.
            print("  No speech detected, skipping subtitle burn.")
            try:
                os.remove(subtitle_file)
            except OSError:
                pass
            return False

        return True
    except Exception as e:
        print(f"  Failed to generate subtitle: {e}")
        return False


# ---------------------------------------------------------------------------
# Header banner + filter graph
# ---------------------------------------------------------------------------
def clean_header_text(text):
    """Collapse whitespace and trim a header/hook string."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def wrap_header_text(text, max_chars=HEADER_MAX_CHARS, max_lines=HEADER_MAX_LINES):
    """
    Wrap text to at most `max_lines` lines of ~`max_chars` characters.
    Overflow is folded into the last line with an ellipsis.
    """
    words = clean_header_text(text).split()
    if not words:
        return ""

    lines = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)

    if len(lines) <= max_lines:
        return "\n".join(lines)

    head = lines[:max_lines - 1]
    tail = " ".join(lines[max_lines - 1:])
    if len(tail) > max_chars:
        tail = tail[:max_chars - 1].rstrip() + "…"
    return "\n".join(head + [tail])


def escape_drawtext(text):
    """
    Escape text for an FFmpeg drawtext value.
    `:` starts a new option, `'` ends a quoted value, and `\\` is an escape
    character, so each one has to be neutralised before rendering.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "''")
    return "'" + text + "'"


def get_bold_font_path():
    """Return a bold system font path, or None to let FFmpeg pick one."""
    for path in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]:
        if os.path.exists(path):
            return path
    return None


def build_drawtext_filter(text):
    """Build the drawtext filter for the top hook banner."""
    font = get_bold_font_path()
    if font:
        font_esc = font.replace("\\", "/").replace(":", "\\:")
        font_part = f"fontfile='{font_esc}':"
    else:
        font_part = ""

    return (
        f"drawtext=expansion=none:text={escape_drawtext(text)}:{font_part}"
        f"fontsize={HEADER_FONT_SIZE}:fontcolor=white:"
        f"{HEADER_BOX}:x=(w-text_w)/2:y={HEADER_Y}"
    )


def get_video_dimensions(path):
    """Return (width, height) of a media file using FFmpeg."""
    try:
        res = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            capture_output=True, text=True, timeout=30,
        )
        blob = (res.stderr or "") + (res.stdout or "")
        m = re.search(r"(\d{2,5})x(\d{2,5})", blob)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


def build_filter_complex(crop_mode, video_size, subtitle_path=None, header_text=None):
    """
    Build the FFmpeg filter_complex that turns a 16:9 source into a
    1080x1920 short-form clip with layout, colour pop, punch-in zoom,
    top hook banner and (optional) burned-in subtitles.
    """
    iw, ih = video_size
    fg_h = max(2, int(round(OUTPUT_WIDTH * ih / iw / 2)) * 2)
    needs_tail = bool(header_text or subtitle_path)
    base_label = "vbase" if needs_tail else "out"

    zoom = f"1+{(PUNCH_ZOOM_MAX - 1) / 2:.4f}*(1-cos(2*PI*t/{PUNCH_ZOOM_PERIOD}))"

    if crop_mode == "blurred":
        # Background: fill 1080x1920 + heavy blur.
        # Foreground: full-width 16:9 frame, colour-popped, periodic punch-in.
        base = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},boxblur=25:5[bgb];"
            f"[fg]scale={OUTPUT_WIDTH}:{fg_h},{COLOR_EQ}[fgc];"
            f"[fgc]scale=w='trunc({OUTPUT_WIDTH}*({zoom})/2)*2':"
            f"h='trunc({fg_h}*({zoom})/2)*2':eval=frame,"
            f"crop={OUTPUT_WIDTH}:{fg_h}:(iw-{OUTPUT_WIDTH})/2:(ih-{fg_h})/2[fgz];"
            f"[bgb][fgz]overlay=(W-w)/2:(H-h)/2,setsar=1[{base_label}]"
        )
    elif crop_mode == "split_left":
        base = (
            f"[0:v]scale=-2:{OUTPUT_HEIGHT}[scaled];"
            "[scaled]split=2[s1][s2];"
            f"[s1]crop={OUTPUT_WIDTH}:{TOP_HEIGHT}:(iw-{OUTPUT_WIDTH})/2:(ih-{OUTPUT_HEIGHT})/2[top];"
            f"[s2]crop={OUTPUT_WIDTH}:{BOTTOM_HEIGHT}:0:ih-{BOTTOM_HEIGHT}[bottom];"
            f"[top][bottom]vstack=inputs=2,{COLOR_EQ},setsar=1[{base_label}]"
        )
    elif crop_mode == "split_right":
        base = (
            f"[0:v]scale=-2:{OUTPUT_HEIGHT}[scaled];"
            "[scaled]split=2[s1][s2];"
            f"[s1]crop={OUTPUT_WIDTH}:{TOP_HEIGHT}:(iw-{OUTPUT_WIDTH})/2:(ih-{OUTPUT_HEIGHT})/2[top];"
            f"[s2]crop={OUTPUT_WIDTH}:{BOTTOM_HEIGHT}:iw-{OUTPUT_WIDTH}:ih-{BOTTOM_HEIGHT}[bottom];"
            f"[top][bottom]vstack=inputs=2,{COLOR_EQ},setsar=1[{base_label}]"
        )
    else:
        # Default center crop, scaled up to the 1080x1920 canvas.
        base = (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},{COLOR_EQ},setsar=1[{base_label}]"
        )

    parts = [base]
    tail = "vbase"

    if header_text:
        parts.append(f"[{tail}]{build_drawtext_filter(header_text)}[vhead]")
        tail = "vhead"

    if subtitle_path:
        sub_esc = os.path.abspath(subtitle_path).replace("\\", "/").replace(":", "\\:")
        parts.append(
            f"[{tail}]subtitles='{sub_esc}':force_style='{SUBTITLE_STYLE}'[out]"
        )
    elif needs_tail:
        # Header only: still need an explicit [out] label for -map.
        parts.append(f"[{tail}]null[out]")

    return ";".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def sanitize_title(title):
    """Sanitize a YouTube video title for use in filenames."""
    safe = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()
    safe = re.sub(r"\s+", "_", safe)[:50].strip("_")
    return safe or "clip"


def cleanup_temp_files(index):
    """Remove all temporary files produced while rendering clip `index`."""
    for path in (f"temp_{index}.mp4", f"temp_{index}.srt"):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def proses_satu_clip(
    video_id,
    item,
    index,
    total_duration,
    crop_mode="default",
    use_subtitle=False,
    header_text="",
    safe_title="clip",
):
    """
    Download, render and export a single vertical short-form clip.

    Args:
        crop_mode: "default", "split_left", "split_right" or "blurred"
        use_subtitle: generate and burn Faster-Whisper subtitles
        header_text: optional top hook banner text
        safe_title: sanitized video title used in the output filename
    """
    start = max(0.0, float(item["start"]) - PADDING)
    end = min(float(item["start"]) + float(item["duration"]) + PADDING, total_duration)
    if end - start < 3:
        return False

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    temp_file = f"temp_{index}.mp4"
    subtitle_file = f"temp_{index}.srt"
    output_file = os.path.join(OUTPUT_DIR, f"{safe_title}_clip_{index}.mp4")

    print(
        f"[Clip {index}] Processing segment "
        f"({int(start)}s - {int(end)}s, padding {PADDING}s)"
    )

    try:
        # 1. Download only the required range.
        result = run_ytdlp(
            [
                "--force-ipv4",
                "--quiet", "--no-warnings",
                "--ffmpeg-location", FFMPEG or "ffmpeg",
                "--downloader", "ffmpeg",
                "--downloader-args",
                f"ffmpeg_i:-ss {int(start)} -to {int(end)} -hide_banner -loglevel error",
                "-f",
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "-o", temp_file,
            ],
            f"https://youtu.be/{video_id}",
            timeout=600,
        )

        if result is None or result.returncode != 0:
            if is_bot_error(result):
                print_bot_help()
            print("  Failed to download video segment.")
            return False

        if not os.path.exists(temp_file):
            print("  Failed to download video segment.")
            return False

        # 2. Transcribe the segment (audio is identical to the final clip).
        subtitle_path = None
        if use_subtitle:
            print("  Generating subtitle...")
            if generate_subtitle(temp_file, subtitle_file):
                subtitle_path = subtitle_file

        # 3. Render everything in a single encode.
        header = wrap_header_text(header_text) if header_text else ""
        video_size = get_video_dimensions(temp_file)
        fc = build_filter_complex(crop_mode, video_size, subtitle_path, header)

        print(f"  Rendering {crop_mode} 1080x1920 clip...")
        cmd_render = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", temp_file,
            "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            output_file,
        ]
        subprocess.run(cmd_render, check=True, capture_output=True, text=True)

        print(f"  Clip successfully generated: {output_file}")
        return True

    except subprocess.CalledProcessError as e:
        print("  Failed to generate this clip.")
        print("  Error details:", (e.stderr or e.stdout or "")[:500])
        return False
    except Exception as e:
        print("  Failed to generate this clip.")
        print("  Error:", str(e))
        return False
    finally:
        cleanup_temp_files(index)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    """Main entry point of the application."""
    print("\n=== Crop Mode ===")
    print("1. Default (center crop)")
    print("2. Split 1 (top: center, bottom: bottom-left (facecam))")
    print("3. Split 2 (top: center, bottom: bottom-right (facecam))")
    print("4. Blurred Background + Full Frame (recommended)")

    modes = {
        "1": ("default", "Default center crop"),
        "2": ("split_left", "Split crop (bottom-left facecam)"),
        "3": ("split_right", "Split crop (bottom-right facecam)"),
        "4": ("blurred", "Blurred background + full frame"),
    }

    while True:
        choice = input("\nSelect crop mode (1-4) [4]: ").strip() or "4"
        if choice in modes:
            crop_mode, crop_desc = modes[choice]
            break
        print("Invalid choice. Please enter 1, 2, 3 or 4.")

    print(f"Selected: {crop_desc}")

    print("\n=== Auto Subtitle ===")
    print(f"Available model: {WHISPER_MODEL} (~{get_model_size(WHISPER_MODEL)})")
    subtitle_choice = input("Add auto subtitle using Faster-Whisper? (y/n): ").strip().lower()
    use_subtitle = subtitle_choice in ["y", "yes"]

    if use_subtitle:
        print(f"✅ Subtitle enabled (Model: {WHISPER_MODEL})")
    else:
        print("❌ Subtitle disabled")

    print("\n=== Top Topic / Hook Header ===")
    print("Optional short hook shown at the top of every clip.")
    print("Leave blank to automatically use the YouTube video title.")
    header_input = input("Top topic / hook text: ").strip()

    cek_dependenci(install_whisper=use_subtitle)

    link = input("\nLink YT: ").strip()
    video_id = extract_video_id(link)

    if not video_id:
        print("Invalid YouTube link.")
        return

    heatmap_data = ambil_most_replayed(video_id)

    if not heatmap_data:
        print("No high-engagement segments found from heatmap.")
        print("  Trying audio-energy peak detector fallback...")
        audio_file = download_audio_for_analysis(video_id)
        best_start = 0
        if audio_file:
            try:
                best_start = analyze_audio_energy_fallback(audio_file)
            finally:
                try:
                    os.remove(audio_file)
                except OSError:
                    pass
        else:
            print("  Failed to download audio for analysis.")
        heatmap_data = [{"start": best_start, "duration": MAX_DURATION, "score": 0}]

    total_duration = get_duration(video_id)
    video_title = get_video_title(video_id)
    safe_title = sanitize_title(video_title)
    header_text = clean_header_text(header_input) or clean_header_text(video_title)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"\nFound {len(heatmap_data)} segment(s) to process.")
    print(f"Video title: {video_title}")
    print(f"Using crop mode: {crop_desc}")
    print(f"Header text: {header_text}")

    success_count = 0
    attempts = 0
    for item in heatmap_data:
        if success_count >= MAX_CLIPS or attempts >= MAX_CLIPS:
            break
        attempts += 1

        item = dict(item)
        item["start"] = max(0.0, min(float(item["start"]), max(0.0, total_duration - 1)))
        item["duration"] = max(
            1.0,
            min(float(item["duration"]), total_duration - item["start"]),
        )

        if proses_satu_clip(
            video_id,
            item,
            success_count + 1,
            total_duration,
            crop_mode,
            use_subtitle,
            header_text,
            safe_title,
        ):
            success_count += 1

    print(
        f"\nFinished processing. "
        f"{success_count} clip(s) successfully saved to '{OUTPUT_DIR}'."
    )


if __name__ == "__main__":
    main()