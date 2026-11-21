"""Faster-Whisper transcription and Gemini heading generation for clips.

Public API:
    HeadingError, is_configured, load_env,
    transcribe_audio, generate_heading, build_prompt, sanitize_heading.

Heavy/optional dependencies (faster_whisper, google.genai) are imported lazily
inside functions so importing this module is cheap and never fails.
"""

import os
import re
import shutil
import subprocess
import tempfile

__all__ = [
    "HeadingError",
    "is_configured",
    "load_env",
    "transcribe_audio",
    "generate_heading",
    "build_prompt",
    "sanitize_heading",
]

_ENV_LOADED = False


class HeadingError(RuntimeError):
    """Raised when transcription or heading generation fails."""


def load_env() -> None:
    """Load a local .env into os.environ without overriding existing vars.

    Idempotent; safe to call repeatedly.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        from dotenv import load_dotenv

        # Look for .env in this module's directory (the project dir).
        here = os.path.dirname(os.path.abspath(__file__))
        load_dotenv(os.path.join(here, ".env"), override=False)
    except Exception:
        # dotenv missing or unreadable: continue without it.
        pass


def _get_api_key():
    load_env()
    return os.environ.get("GEMINI_API_KEY", "")


def is_configured() -> bool:
    """Return True iff a non-empty GEMINI_API_KEY is available."""
    key = _get_api_key()
    return bool(key and key.strip())

# ---------------------------------------------------------------------------
# PyAV compatibility shim (replicated from yt-heatmap-clipper/run.py)
# ---------------------------------------------------------------------------
def _patch_av_open() -> None:
    """PyAV 19 removed av.open(..., metadata_errors=...).

    faster-whisper still passes that kwarg. Wrap av.open so the kwarg is
    dropped on PyAV builds that no longer accept it.
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


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------
def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _notify(progress_cb, fraction, message):
    if progress_cb is None:
        return
    try:
        progress_cb(float(fraction), str(message))
    except Exception:
        pass


def transcribe_audio(video_path, *, model_size=None, progress_cb=None):
    """Transcribe a clip's audio with Faster-Whisper.

    Returns {"text", "language", "language_probability", "segments",
             "duration", "ok": True}. Language is auto-detected by Whisper
    (no ``language=`` kwarg is passed). Raises HeadingError on failure.
    """
    try:
        from faster_whisper import WhisperModel
    except Exception:
        raise HeadingError("faster-whisper is not installed")

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise HeadingError("ffmpeg executable not found")

    if not os.path.isfile(video_path):
        raise HeadingError("video file not found: %s" % os.path.basename(video_path))

    model_name = model_size or os.environ.get("WHISPER_MODEL") or "base"

    _notify(progress_cb, 0.0, "Preparing audio")
    tmp_dir = tempfile.mkdtemp(prefix="clip_head_")
    wav_path = os.path.join(tmp_dir, "audio.wav")
    try:
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    video_path,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    wav_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            raise HeadingError("ffmpeg audio extraction failed: %s" % exc)

        _notify(progress_cb, 0.15, "Loading Whisper model '%s'" % model_name)
        try:
            _patch_av_open()
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        except Exception as exc:
            raise HeadingError("failed to load Whisper model '%s': %s" % (model_name, exc))

        _notify(progress_cb, 0.3, "Transcribing")
        try:
            # NOTE: deliberately no language= kwarg; Whisper auto-detects.
            segments, info = model.transcribe(
                wav_path,
                beam_size=1,
                vad_filter=True,
            )
        except Exception as exc:
            raise HeadingError("transcription failed: %s" % exc)

        _notify(progress_cb, 0.85, "Collecting transcript")
        texts = []
        segment_count = 0
        try:
            for seg in segments:
                segment_count += 1
                chunk = (seg.text or "").strip()
                if chunk:
                    texts.append(chunk)
        except Exception as exc:
            raise HeadingError("transcription failed while reading segments: %s" % exc)

        duration = getattr(info, "duration", None)
        language = getattr(info, "language", None)
        lang_prob = getattr(info, "language_probability", None)

        _notify(progress_cb, 1.0, "Done")
        return {
            "text": " ".join(texts).strip(),
            "language": language,
            "language_probability": lang_prob,
            "segments": segment_count,
            "duration": duration,
            "ok": True,
        }
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Heading generation via Gemini
# ---------------------------------------------------------------------------
def build_prompt(transcript, language=None):
    """Build the prompt sent to Gemini for a short heading."""
    truncated = (transcript or "")[:12000]
    lang_line = ""
    if language and str(language).strip():
        lang_line = (
            "The transcript language is '%s' (ISO code). "
            "Reply in that same language.\n" % str(language).strip()
        )
    return (
        "You write short headings for video clips based on their transcript.\n"
        "Read the transcript below and produce ONE short, punchy, factual heading "
        "that describes the actual spoken content.\n"
        "Rules:\n"
        "- The heading must reflect what is actually said; do not invent facts.\n"
        "- Reply in the same language as the transcript.\n"
        "- Keep it concise (at most 70 characters).\n"
        "- No hashtags, no emoji spam, no clickbait claims.\n"
        "- Do not wrap the heading in quotes or backticks.\n"
        "- Reply with the heading text only, nothing else.\n"
        "%s"
        "Transcript:\n\"\"\"\n%s\n\"\"\"\n"
    ) % (lang_line, truncated)


def generate_heading(transcript, *, language=None, max_chars=70):
    """Generate a short heading from a transcript using Gemini.

    Returns {"heading", "model", "ok": True}. Raises HeadingError on failure.
    Never leaks the API key.
    """
    key = _get_api_key()
    if not (key and key.strip()):
        raise HeadingError("GEMINI_API_KEY is not set")

    try:
        from google import genai
        from google.genai import types
    except Exception:
        raise HeadingError("google-genai is not installed")

    model_name = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    prompt = build_prompt(transcript, language=language)

    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=128,
            ),
        )
    except Exception as exc:
        # Do not include the API key in the error message.
        raise HeadingError("Gemini request failed: %s" % _safe_exc(exc))

    text = getattr(response, "text", None)
    if text is None:
        # Some SDKs return .candidates; try a fallback.
        try:
            text = response.candidates[0].content.parts[0].text
        except Exception:
            text = None
    if not text or not str(text).strip():
        raise HeadingError("Gemini returned an empty heading")

    heading = sanitize_heading(str(text), max_chars=max_chars)
    if not heading:
        raise HeadingError("Gemini returned an unusable heading")

    return {"heading": heading, "model": model_name, "ok": True}


def _safe_exc(exc):
    """Return an exception string with any API-key-like token redacted."""
    msg = str(exc)
    key = _get_api_key()
    if key and key in msg:
        msg = msg.replace(key, "***")
    return msg


# ---------------------------------------------------------------------------
# Heading sanitization
# ---------------------------------------------------------------------------
def sanitize_heading(text, max_chars=70):
    """Clean a heading: strip, collapse whitespace, drop control chars, trim."""
    if not text:
        return ""
    # Remove control characters (except tab/newline which we then collapse).
    cleaned = "".join(
        ch for ch in str(text) if ch == "\t" or ch == "\n" or ord(ch) >= 32
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip wrapping quotes / backticks (possibly repeated).
    cleaned = cleaned.strip(" \t\"'`")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    # Trim to max_chars on a word boundary.
    truncated = cleaned[:max_chars]
    if " " in truncated:
        truncated = truncated[: truncated.rfind(" ")]
    return truncated.strip(" \t\"'`-")
