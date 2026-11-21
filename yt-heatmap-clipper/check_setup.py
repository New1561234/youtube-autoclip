import os
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def get_ffmpeg_exe():
    """
    Locate FFmpeg: PATH first, then imageio-ffmpeg's bundled build.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def check_ffmpeg():
    """Check if FFmpeg is installed and executable."""
    exe = get_ffmpeg_exe()
    if not exe:
        print("❌ FFmpeg NOT found (not on PATH and imageio-ffmpeg unavailable).")
        print("   -> Install FFmpeg, or: pip install imageio-ffmpeg")
        return False
    try:
        subprocess.run(
            [exe, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        print("✅ FFmpeg is installed and recognized.")
        return True
    except Exception:
        print("❌ FFmpeg was found but could not be executed.")
        return False


def check_library(package_name, import_name=None):
    """Check if a specific Python library is installed."""
    if import_name is None:
        import_name = package_name

    try:
        __import__(import_name)
        print(f"✅ Library '{package_name}' is installed.")
        return True
    except ImportError:
        print(f"⚠️  Library '{package_name}' is NOT installed.")
        return False


def main():
    print("--- 🩺 Checking System Environment ---\n")

    ffmpeg_ok = check_ffmpeg()

    # (pip package name, import name)
    packages = [
        ("requests", "requests"),
        ("yt-dlp", "yt_dlp"),
        ("faster-whisper", "faster_whisper"),
        ("moviepy", "moviepy"),
        ("numpy", "numpy"),
        ("imageio-ffmpeg", "imageio_ffmpeg"),
    ]

    all_packages_ok = True
    for pkg_name, import_name in packages:
        if not check_library(pkg_name, import_name):
            all_packages_ok = False

    print("\n" + "=" * 40)

    if ffmpeg_ok and all_packages_ok:
        print("🎉 GREAT! Your system is ready.")
        print("   You can now run: python run.py")
    else:
        print("❌ SETUP INCOMPLETE.")
        if not ffmpeg_ok:
            print("   - You need to install FFmpeg (or imageio-ffmpeg).")
        if not all_packages_ok:
            print("   - You need to install Python dependencies.")
            print("     Run: pip install -r requirements.txt")


if __name__ == "__main__":
    main()