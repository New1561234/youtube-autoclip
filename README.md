# YouTube Vertical Clipper

A complete local system for clipping YouTube videos into vertical (9:16) comedy clips.

## 📋 Overview

Two codebases work together:

1. **`auto_clipper.py`** - Primary CLI converter (fixed, WebM-based, no corrupted MP4 issues)
2. **`yt-heatmap-clipper/`** - Feature-rich repo with heatmap detection, subtitles, banners
3. **`app.py`** - **NEW**: Flask web UI for browser-based clip generation

## 🚀 Quick Start

### Option 1: Web UI (recommended)
```powershell
# 1. Install dependencies
pip install flask yt-dlp faster-whisper imageio-ffmpeg numpy moviepy requests

# 2. Start the server
python "D:\Games\new folder\clip\app.py"

# 3. Open Chrome at:
http://127.0.0.1:5000
```

### Option 2: CLI (original)
```powershell
python "D:\Games\new folder\clip\auto_clipper.py"
# Follow interactive prompts
```

## 🌐 Web UI Features

| Feature | Details |
|---------|---------|
| **URL formats** | `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/` |
| **Clip duration** | 10-300s (default: 45s) |
| **Context before** | 0-60s extra before selected moment (default: 10s) |
| **Heatmap detection** | YouTube "Most Replayed" data (falls back to audio energy) |
| **Output** | 9:16 vertical MP4 (720x1280) |
| **Subtitles** | Faster-Whisper, Bahasa Indonesia |
| **Top banner** | Optional custom title with semi-transparent box |
| **Split screens** | Default, split-left, split-right facecam layouts |
| **Job tracking** | Live status, per-job folders, auto-cleanup |
| **Download** | Direct MP4 download from local server |

## 🛠️ Installation

```powershell
# Core dependencies
pip install flask yt-dlp faster-whisper imageio-ffmpeg numpy moviepy requests

# Optional: YouTube upload (Google OAuth)
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

# Ensure FFmpeg is available:
# - Windows: Download from https://ffmpeg.org/download.html
# - Or: `pip install imageio-ffmpeg` (used automatically)
```

## 🔧 Project Structure

```
D:\Games\new folder\clip\
├── auto_clipper.py          # Fixed CLI clipper (WebM download, title sanitization)
├── heatmap_bridge.py        # Bridge to yt-heatmap-clipper repo
├── yt-heatmap-clipper/      # Cloned repo: heatmap + subtitles + banners
│   ├── run.py               # Main entry point with heatmap/subtitle/banner features
│   ├── requirements.txt
│   ├── check_setup.py
│   └── clips/               # Output directory
├── app.py                   # Flask web UI (new, browser-based)
├── requirements.txt         # Python dependencies (updated)
├── test_syntax.py           # Syntax validation script
├── templates/               # HTML templates
│   ├── index.html           # Main interactive page
│   └── help.html            # Documentation page
├── jobs/                    # Per-job temp directories (auto-created)
└── *.mp4                    # Original MP4 files (preserved, untouched)
```

## 🔧 Recent Fixes

| Issue | Fix |
|-------|-----|
| Corrupted `full_video.mp4` | Now downloads as WebM (natively readable by MoviePy) |
| Title sanitization | Removes trailing dots from filenames |
| Crop filter | Proper 9:16 vertical: `scale=-2:1280,crop=720:1280:(iw-720)/2:(ih-1280)/2,setsar=1` |
| Audio energy fallback | Works when YouTube heatmap unavailable |
| URL parsing | All formats: watch, youtu.be, Shorts |

## 📦 Dependencies (`requirements.txt`)

```text
flask>=2.3.3          # Web UI
yt-dlp>=2024.01.01    # YouTube download
faster-whisper>=1.0.0 # Subtitle generation
imageio-ffmpeg>=0.4.5 # FFmpeg path fallback
numpy>=1.24.0         # Numerical computing
moviepy>=1.0.3        # Video processing
requests>=2.31.0      # HTTP requests
# Optional YouTube upload:
google-auth>=2.23.0
google-auth-oauthlib>=1.1.0
google-auth-httplib2>=0.1.1
google-api-python-client>=2.100.0
```

## 🎛️ Usage

### Web UI (`http://127.0.0.1:5000`)

1. Paste a YouTube URL
2. Set clip duration (default: 45s, range: 10-300s)
3. Set context-before seconds (default: 10s, range: 0-60s)
4. Click **"Generate Clip"**
5. Watch live status: "Checking heatmap", "Downloading", "Rendering"
6. When complete: **Preview** the video or **Download MP4**

### CLI (`python auto_clipper.py`)

1. Follow interactive prompts
2. Enter YouTube URL
3. Wait for download, analysis, and rendering
4. Clip saved as `{title}_clip.mp4`

### CLI (`python run.py` - heatmap features)

1. Follow interactive prompts (crop mode, subtitles, etc.)
2. Paste YouTube URL
3. System attempts heatmap detection first
4. Falls back to audio energy if no heatmap data
5. Renders 9:16 vertical clip with optional subtitles/banners

## ⚠️ Important Notes

- **Heatmap availability**: Varies by video; newer uploads/live VODs may not have data
- **Audio fallback**: Always works when heatmap is unavailable
- **Bot checks**: YouTube may show "confirm you're not a bot" - optional cookie support
- **Local only**: Server binds to 127.0.0.1 (localhost only, never exposed to internet)
- **No cookies exposed**: Private and secure by design
- **Existing MP4s**: Never touched or deleted by the script
- **Clip duration**: System ensures output ≤ requested duration

## 📁 Output Format

- **Filename**: `{video_id}_clip.mp4` (stored in per-job folder `jobs/job_*_/`)
- **Resolution**: 9:16 vertical (720x1280)
- **Audio**: Included when source has audio track
- **Never overwrites**: Each job gets isolated folder, existing files safe

## 🛡️ Privacy & Safety

- **Server binds to 127.0.0.1** - local only, never internet-exposed
- **No cookies or sensitive data** stored, logged, or transmitted
- **Path traversal protection** on all file endpoints
- **Per-job isolation** - each job gets its own temp folder
- **No arbitrary filesystem access** - only within project directory and job folders
- **FFmpeg/download commands** use safe argument passing (no `shell=True`)

## 📬 Need Help?

- Check the **Help** page at `http://127.0.0.1:5000/help`
- Review the **syntax check** output: `python -m py_compile app.py auto_clipper.py heatmap_bridge.py`
- Issues? The web UI shows clear error messages for common problems
- YouTube changes may affect heatmap availability - this is expected and handled with audio fallback

## 📜 License

This project is for personal use. YouTube's Terms of Service apply to all video processing.