import io, os, sys

root = r"D:\Games\new folder\clip"
src = os.path.join(root, "app.py")

with io.open(src, "r", encoding="utf-8-sig") as fh:
    text = fh.read()
lines = text.split("\n")

def block(name):
    with io.open(os.path.join(root, "_blk_%s.txt" % name), "r", encoding="utf-8") as fh:
        return fh.read().rstrip("\n")

def check(lineno, needle):
    actual = lines[lineno - 1]
    if needle not in actual:
        raise SystemExit("boundary mismatch at line %d: %r does not contain %r"
                         % (lineno, actual, needle))

check(21, "import os")
check(40, "heatmap_bridge")
check(65, "def cleanup_job_files")
check(84, "pass")
check(189, "def generate_clip_job")
check(344, "ERROR:")
check(405, '@app.route("/generate"')
check(450, "job_id")
check(716, "Health & Misc")
check(730, "Cache policy")
check(741, "")

ops = [
    (730, 741, "prefix"),
    (405, 450, "generate"),
    (189, 344, "genjob"),
    (65, 84, "cleanup"),
    (21, 40, "imports"),
]

# insertion before line 716
insert_at = 716

for start, end, name in ops:
    lines[start - 1:end] = block(name).split("\n")

lines[insert_at - 1:insert_at - 1] = block("routes").split("\n")

out = "\n".join(lines)
with io.open(src, "w", encoding="utf-8", newline="") as fh:
    fh.write(out)
print("patched; new line count:", len(lines))