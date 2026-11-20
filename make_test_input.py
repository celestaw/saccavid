#!/usr/bin/env python3
"""Generate a small synthetic source video for testing saccavid.
Usage: python3 make_test_input.py [fps] [seconds] [output]
Defaults: 60 fps, 3 s, tests/test_in.mp4.
Content: mid-grey background, a moving white box and static colour
patches (so R, G and B channels all carry signal)."""
import subprocess
import sys

import numpy as np

FPS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
DUR = int(sys.argv[2]) if len(sys.argv) > 2 else 3
OUT = sys.argv[3] if len(sys.argv) > 3 else "tests/test_in.mp4"
W, H = 320, 240
p = subprocess.Popen(
    ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
     "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
     "-c:v", "libx264", "-crf", "5", "-pix_fmt", "yuv420p", OUT],
    stdin=subprocess.PIPE)
for i in range(FPS * DUR):
    fr = np.full((H, W, 3), 128, dtype=np.uint8)          # mid grey
    fr[20:80, 20:110] = (200, 60, 60)                     # red patch
    fr[150:210, 200:290] = (60, 80, 200)                  # blue patch
    x = 20 + (i * 2) % 220                                # moving white box
    fr[100:140, x:x + 40] = 230
    p.stdin.write(fr.tobytes())
p.stdin.close()
p.wait()
print(f"{OUT} written ({FPS} fps, {DUR} s)")
