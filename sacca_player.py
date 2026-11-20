#!/usr/bin/env python3
"""
sacca_player v0 — realtime Saccaidance player (minimal version)

Plays a normal-fps source video + cues JSON (same format as the
saccavid encoder) and decomposes each frame into the 4-subframe
colour cycle AT PRESENTATION TIME, one sub-frame per display refresh.

Why realtime instead of playing the 480 fps file: a normal player
cannot be trusted to present every sub-frame exactly once; here every
flip is ours, vsync-locked, and logged, so dropped refreshes are
visible in the record instead of silently corrupting the stimulus.

Timing model: identical to the encoder v0.1. Time is divided into
cycles of 4 refreshes; the source frame and cue mask are fixed within
a cycle and sampled at the cycle start. At 480 Hz / 60 fps this is
2 cycles per source frame.

Logs (CSV): flip_index, t_flip (s, perf_counter since start),
cycle, phase, src_frame, cue_active, interval_ms, dropped_refreshes
(cumulative estimate: intervals >= 1.75 refresh periods count the
missed periods as dropped).

Audio: the source audio is extracted to a temp WAV and started with
the first flip. v0 logs but does not correct drift; keep stimuli
short and check the summary (actual Hz / drops) after each run.

Usage:
  python3 sacca_player.py video.mp4 cues.json --refresh 480
  python3 sacca_player.py video.mp4 cues.json --windowed --refresh 240
  python3 sacca_player.py video.mp4 cues.json --test-mode
      (--test-mode: headless pacing simulation for development —
       no real display/vsync; verifies sequencing and logging only.
       It says NOTHING about real 480 Hz presentation.)

Keys: ESC / Q quit.
Requirements: pygame, numpy, Pillow, and ffmpeg on PATH.
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

from saccavid import probe, make_mask, decompose  # noqa: E402


def load_cues(path, w, h):
    with open(path) as f:
        spec = json.load(f)
    px_per_deg = None
    if "display" in spec:
        disp = spec["display"]
        pitch_mm = float(disp["screen_width_mm"]) / w
        px_per_deg = (2 * float(disp["distance_mm"])
                      * math.tan(math.radians(0.5)) / pitch_mm)
    return [(float(c["start"]), float(c["end"]),
             make_mask(c, w, h, px_per_deg)) for c in spec["cues"]]


class SourceReader:
    """Sequential CFR source frames from an ffmpeg rawvideo pipe."""

    def __init__(self, path, w, h):
        self.w, self.h = w, h
        self.fb = w * h * 3
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", path, "-vf", "format=rgb24",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE)
        self.idx = -1
        self.frame = None

    def get(self, idx):
        while self.idx < idx:
            raw = self.proc.stdout.read(self.fb)
            if len(raw) < self.fb:
                raise IOError(f"source ended before frame {idx}")
            self.frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                self.h, self.w, 3).copy()
            self.idx += 1
        return self.frame

    def close(self):
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        self.proc.wait()


def extract_audio(path):
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    rc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path,
         "-vn", "-ac", "2", "-ar", "48000", wav]).returncode
    if rc != 0 or os.path.getsize(wav) < 1000:
        try:
            os.unlink(wav)
        except OSError:
            pass
        return None
    return wav


def play(inp, cues_path, refresh=480, fullscreen=True, log_path=None,
         test_mode=False, delta=1, max_seconds=None):
    info = probe(inp)
    w, h, in_fps = info["width"], info["height"], info["fps"]
    total_src = info["nb_frames"] or int(round(info["duration"] * in_fps))
    duration = total_src / in_fps
    if max_seconds:
        duration = min(duration, max_seconds)
    cues = load_cues(cues_path, w, h)
    cycle_dur = 4 / refresh
    total_cycles = int(math.ceil(duration / cycle_dur - 1e-9))

    if test_mode:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.init()
    flags = 0 if test_mode else (pygame.FULLSCREEN if fullscreen else 0)
    screen = pygame.display.set_mode((w, h), flags, vsync=0 if test_mode else 1)
    pygame.display.set_caption("sacca_player")

    wav = None
    if not test_mode:
        wav = extract_audio(inp)
        if wav:
            pygame.mixer.init(frequency=48000)
    reader = SourceReader(inp, w, h)

    log_path = log_path or (inp + ".flips.csv")
    rows = []
    drops = 0
    prev_t = None
    period = 1.0 / refresh
    start = None
    flip_index = 0
    quit_now = False

    def prepare(cyc):
        """Decompose one cycle into 4 sub-frame byte buffers. Runs in
        the producer thread: at 480 Hz a flip slot is 2.08 ms, and a
        full-frame decompose + decode can exceed that, so preparation
        must never happen on the presentation path."""
        t_cycle = cyc * cycle_dur
        src_idx = min(int(t_cycle * in_fps + 1e-6), total_src - 1)
        frame = reader.get(src_idx)
        mask = np.zeros((h, w), dtype=bool)
        active = 0
        for s, e, m in cues:
            if s <= t_cycle < e:
                mask |= m
                active = 1
        bufs = [sub.tobytes() for sub in decompose(frame, mask, delta)]
        return bufs, src_idx, active

    import queue
    import threading
    q = queue.Queue(maxsize=8)

    def produce():
        try:
            for cyc in range(total_cycles):
                q.put((cyc, prepare(cyc)))
        except Exception as exc:  # surfaced in the main loop
            q.put((-1, exc))
        q.put((None, None))

    threading.Thread(target=produce, daemon=True).start()

    for cyc in range(total_cycles):
        tag, payload = q.get()
        if tag == -1:
            raise payload
        bufs, src_idx, active = payload
        surfaces = [pygame.image.frombuffer(b, (w, h), "RGB") for b in bufs]
        for phase in range(4):
            if test_mode:
                # pace by clock to the simulated refresh grid
                target = flip_index * period
                while start is not None and time.perf_counter() - start < target:
                    time.sleep(0.0002)
            screen.blit(surfaces[phase], (0, 0))
            pygame.display.flip()
            now = time.perf_counter()
            if start is None:
                start = now
                if wav:
                    pygame.mixer.Sound(wav).play()
            t_flip = now - start
            interval = None
            if prev_t is not None:
                interval = t_flip - prev_t
                if interval >= 1.75 * period:
                    drops += int(round(interval / period)) - 1
            rows.append([flip_index, f"{t_flip:.6f}", cyc, phase, src_idx,
                         active,
                         f"{interval * 1000:.3f}" if interval else "",
                         drops])
            prev_t = t_flip
            flip_index += 1
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (
                        ev.type == pygame.KEYDOWN
                        and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                    quit_now = True
            if quit_now:
                break
        if quit_now:
            break

    reader.close()
    pygame.quit()
    if wav:
        try:
            os.unlink(wav)
        except OSError:
            pass
    with open(log_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["flip_index", "t_flip_s", "cycle", "phase",
                     "src_frame", "cue_active", "interval_ms",
                     "dropped_refreshes"])
        wr.writerows(rows)
    span = rows[-1][1] if rows else 0
    print(f"flips: {flip_index} over {span} s "
          f"(target refresh {refresh} Hz), dropped refreshes: {drops}")
    print(f"log: {log_path}")
    if test_mode:
        print("NOTE: test-mode — pacing simulated, no real vsync/display. "
              "Sequencing and logging only.")
    return {"flips": flip_index, "drops": drops, "log": log_path}


def main():
    ap = argparse.ArgumentParser(prog="sacca_player")
    ap.add_argument("input")
    ap.add_argument("cues")
    ap.add_argument("--refresh", type=int, default=480,
                    help="display refresh rate to pace/validate against")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--delta", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--log", default=None)
    ap.add_argument("--test-mode", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=None)
    a = ap.parse_args()
    play(a.input, a.cues, refresh=a.refresh, fullscreen=not a.windowed,
         log_path=a.log, test_mode=a.test_mode, delta=a.delta,
         max_seconds=a.max_seconds)


if __name__ == "__main__":
    main()
