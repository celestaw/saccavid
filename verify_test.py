#!/usr/bin/env python3
"""Verify the saccavid outputs:
1) [60 fps case] the 4-subframe sum of any checked cycle reproduces
   the source frame (what fixation integrates, in RGB code values),
   inside AND outside the cue mask;
2) during a cue, a mask pixel's primary is phase-shifted relative to
   a non-mask pixel in the same sub-frame;
3) [25 fps case] the output duration does NOT stretch (v0 bug: +4.17%
   at 25 fps, +25% at 50 fps) and cycle sums still hold.
Run after:
  python3 make_test_input.py
  python3 saccavid.py encode tests/test_in.mp4 cues.example.json -o tests/test_out_480.mp4
  python3 make_test_input.py 25 2 tests/test_in_25.mp4
  python3 saccavid.py encode tests/test_in_25.mp4 cues.example.json -o tests/test_out_25.mp4
"""
import json
import subprocess
import sys

import numpy as np

sys.path.insert(0, ".")
from saccavid import probe, make_mask  # noqa: E402


def read_all(path):
    info = probe(path)
    w, h = info["width"], info["height"]
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    n = len(raw) // (w * h * 3)
    return info, np.frombuffer(raw, dtype=np.uint8).reshape(n, h, w, 3)


# ---- 60 fps case -----------------------------------------------------
_, src = read_all("tests/test_in.mp4")
out_info, out = read_all("tests/test_out_480.mp4")
assert out_info["fps"] == 480, out_info["fps"]
print(f"[60fps] fps OK: in 60 -> out {out_info['fps']:g}, "
      f"frames {len(src)} -> {len(out)}, duration {out_info['duration']:.2f} s")
assert abs(out_info["duration"] - 3.0) < 0.05

# cycles are 4-aligned; source frame for cycle starting at output
# frame j*8 is src[j] (8 sub-frames per 60 fps source frame)
for j in (10, 60, 130):  # 0.17s (no cue), 1.0s (oval), 2.17s (text)
    cycle = out[j * 8: j * 8 + 4].astype(np.int32)
    diff = np.abs(cycle.sum(axis=0) - src[j].astype(np.int32))
    print(f"[60fps] source frame {j}: max |sum(4 sub) - source| = {diff.max()}")
    assert diff.max() <= 2, "cycle does not integrate back to the source"

cues = json.load(open("cues.example.json"))["cues"]
mask = make_mask(cues[0], 320, 240)
ys, xs = np.where(mask)
cand = [(y, x) for y, x in zip(ys, xs) if 90 < y < 100 and 150 < x < 170]
y, x = cand[0]
sub0 = out[60 * 8]  # cycle-aligned, phase 0
print(f"[60fps] mask pixel ({y},{x}) at sub-frame 0: {sub0[y, x]}  (expect G only)")
print(f"[60fps] grey non-mask pixel (10,160)     : {sub0[10, 160]}  (expect R only)")
assert sub0[y, x][1] > 100 and sub0[y, x][0] == 0
assert sub0[10, 160][0] > 100 and sub0[10, 160][1] == 0

# ---- 25 fps case -----------------------------------------------------
_, src25 = read_all("tests/test_in_25.mp4")
out25_info, out25 = read_all("tests/test_out_25.mp4")
print(f"[25fps] frames {len(src25)} -> {len(out25)}, "
      f"duration {out25_info['duration']:.3f} s (expect ~2.000, "
      f"v0 bug gave 2.083)")
assert abs(out25_info["duration"] - 2.0) < 0.05, "duration stretched!"
assert len(out25) % 4 == 0
# check a few cycles against the cycle-sampled source frame:
# cycle c starts at t=c/120 s, source idx = floor(t*25)
for c in (0, 60, 120, 200):
    t = c / 120.0
    sidx = min(int(t * 25), len(src25) - 1)
    cycle = out25[c * 4: c * 4 + 4].astype(np.int32)
    diff = np.abs(cycle.sum(axis=0) - src25[sidx].astype(np.int32))
    print(f"[25fps] cycle {c} (t={t:.2f}, src {sidx}): max diff = {diff.max()}")
    assert diff.max() <= 2

print("ALL CHECKS PASSED")
