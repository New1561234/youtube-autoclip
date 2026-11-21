"""Quick syntax check for the clipper project files.

Run: python test_syntax.py
Exits non-zero if any file fails to compile.
"""
import py_compile
import sys

FILES = ["app.py", "auto_clipper.py", "heatmap_bridge.py", "youtube_auth.py", "youtube_upload.py"]

failed = False
for f in FILES:
    try:
        py_compile.compile(f, doraise=True)
        print(f"OK   {f}")
    except py_compile.PyCompileError as e:
        failed = True
        print(f"FAIL {f}: {e}")

sys.exit(1 if failed else 0)