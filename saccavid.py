#!/usr/bin/env python3
"""
saccavid v0.1 — video Saccaidance encoder (engine only, no GUI yet)

Input : a normal video (e.g. 60 fps)
Cues  : a JSON timeline saying, per cue,
        "from `start` sec to `end` sec, put a saccade-visible symbol
         of type `pattern` at position (x, y)"
Output: a high-frame-rate video (default 480 fps) in which the source
        has been decomposed into a 4-subframe colour cycle, with the
        colour phase shifted inside the cue masks while a cue is active.

Colour decomposition (v0.1, PROVISIONAL):
  This is a provisional scheme based on per-channel RGB separation.
  Whether it matches the Saccaidance paper's / the lab's implementation
  in slot order, black slot, amplitude and phase offset is UNVERIFIED —
  do not claim paper equivalence until the lab recipe is checked.
  For a pixel (R, G, B), the 4 sub-frames show one component at a time:
      non-pattern:  k=0:(R,0,0)  k=1:(0,G,0)  k=2:(0,0,B)  k=3:(0,0,0)
      pattern    :  the same cycle rotated by `delta` steps (default 1)
  Algebraically, IF the source pixel and the mask stay constant over a
  full cycle, the 4 sub-frames sum to (R,G,B) in both regions. That is
  a statement about RGB code values, NOT a proof of perceptual
  invisibility: display response is nonlinear, so code-value averages
  and emitted-light averages differ, and saturation/hue preservation
  on a real panel is not guaranteed. Brightness: each channel is shown
  in only 1 of 4 slots, so its time-averaged emission is ~1/4 (this is
  due to the 1-in-4 duty per channel, not merely the black slot).
  `--gain` multiplies code values; it is NOT calibrated luminance
  compensation, and clipping changes channel ratios (i.e. colour).

Timing model (fixed in v0.1 after Codex review):
  The output clock is the master. Time is divided into cycles of 4
  sub-frames (at 480 fps, one cycle = 1/120 s). The source frame and
  the cue mask are FIXED within each cycle, sampled at the cycle's
  start time. Consequences:
    * total duration never stretches, for any input fps (25/50/59.94…)
    * the per-cycle sum identity above holds exactly
    * cue start/end and source-frame changes are quantised to cycle
      boundaries (error <= 1 cycle); the applied (quantised) cue
      intervals are written to `<output>.applied.json` for the record
  Input is assumed CFR (constant frame rate). VFR sources are not
  supported yet (their PTS is not read by the rawvideo pipe).

IMPORTANT (why the output is not an ordinary video):
  * The encoder writes RGB, losslessly (libx264rgb crf 0, or FFV1).
    A normal YUV 4:2:0 encode would destroy the per-pixel RGB cycle.
  * Ordinary players may drop frames at 480 fps, which breaks the
    integration. Treat the output as *experiment data*; for a real
    study, present it with a vsync-locked player and log dropped
    frames (that player is the next step, not part of v0.1).

Usage:
  python3 saccavid.py encode input.mp4 cues.json -o out.mp4
  python3 saccavid.py encode input.mp4 cues.json -o out.mp4 --delta 2
  python3 saccavid.py check  out.mp4
  python3 saccavid.py preview out.mp4 --mode average -o avg.mp4
  python3 saccavid.py preview out.mp4 --mode saccade -o sim.mp4 --slip 3

Cue JSON:
{
  "display": {"screen_width_mm": 597, "distance_mm": 530},
  "cues": [
    {"start": 1.0, "end": 3.0, "x": 640, "y": 360,
     "pattern": "oval", "size": 220, "thickness": 6},
    {"start": 3.5, "end": 5.0, "nx": 0.25, "ny": 0.5,
     "pattern": "text", "text": "!", "size_deg": 4.0, "font": "/path/font.ttf"}
  ]
}
  x/y   : centre in pixels (or nx/ny as 0..1 fractions of the frame)
  size  : pattern radius / half-size in pixels — or use `size_deg`
          (visual angle, needs the top-level `display` block), and
          `thickness_deg` likewise. px values are display-dependent:
          6 px was the paper's optimum only for its QHD 27" @ 530 mm
          setup (~0.30 deg there); it is not a universal constant.
  pattern: oval | radial | circle | rect | cross | arrow | text
  thickness: line width in px (ignored for filled text)
  angle : arrow direction in degrees (0 = right, -90 = up), default -90
  font  : font file for `text` cues (fix it for experiments; the
          fallback search changes the glyph shape if it changes)
"""

import argparse
import json
import math
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- probes

def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames",
         "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    st = info["streams"][0]
    num, den = st["avg_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    if fps <= 0:
        num, den = st["r_frame_rate"].split("/")
        fps = float(num) / float(den)
    nb = st.get("nb_frames")
    return {"width": int(st["width"]), "height": int(st["height"]),
            "fps": fps, "duration": float(info["format"]["duration"]),
            "nb_frames": int(nb) if nb and nb.isdigit() else None}


# ---------------------------------------------------------------- masks

def _font(size, path=None):
    candidates = [path] if path else []
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"]
    for p in candidates:
        if p:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    print("warning: no TrueType font found, using PIL default "
          "(glyph shape will differ)", file=sys.stderr)
    return ImageFont.load_default()


def make_mask(cue, w, h, px_per_deg=None):
    """Binary mask (H, W) bool for one cue, in the cue's local geometry."""
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    cx = cue.get("nx", None)
    cy = cue.get("ny", None)
    x = float(cx) * w if cx is not None else float(cue.get("x", w / 2))
    y = float(cy) * h if cy is not None else float(cue.get("y", h / 2))

    def _deg_or_px(deg_key, px_key, default):
        if deg_key in cue:
            if px_per_deg is None:
                raise ValueError(f"{deg_key} needs a top-level 'display' "
                                 "block (screen_width_mm, distance_mm)")
            return float(cue[deg_key]) * px_per_deg
        return float(cue.get(px_key, default))

    size = _deg_or_px("size_deg", "size", 160)
    th = max(1, int(round(_deg_or_px("thickness_deg", "thickness", 6))))
    pat = cue.get("pattern", "oval")

    if pat == "oval":  # concentric ovals, denser toward the centre
        for frac in (1.0, 0.72, 0.48, 0.28):
            r = size * frac
            ry = r * 0.78
            d.ellipse([x - r, y - ry, x + r, y + ry], outline=255, width=th)
    elif pat == "circle":
        d.ellipse([x - size, y - size, x + size, y + size], outline=255, width=th)
    elif pat == "radial":  # spokes radiating from the centre
        for a in range(0, 360, 15):
            rad = math.radians(a)
            d.line([x, y, x + size * math.cos(rad), y + size * math.sin(rad)],
                   fill=255, width=th)
        d.ellipse([x - th * 2, y - th * 2, x + th * 2, y + th * 2], fill=255)
    elif pat == "rect":
        d.rectangle([x - size, y - size * 0.75, x + size, y + size * 0.75],
                    outline=255, width=th)
    elif pat == "cross":
        d.line([x - size, y, x + size, y], fill=255, width=th)
        d.line([x, y - size, x, y + size], fill=255, width=th)
    elif pat == "arrow":
        ang = math.radians(float(cue.get("angle", -90)))
        ux, uy = math.cos(ang), math.sin(ang)
        px, py = -uy, ux
        tip = (x + ux * size, y + uy * size)
        base = (x - ux * size, y - uy * size)
        d.line([base, tip], fill=255, width=th)
        for s in (0.55, -0.55):
            hx = tip[0] - ux * size * 0.45 + px * size * s
            hy = tip[1] - uy * size * 0.45 + py * size * s
            d.line([tip, (hx, hy)], fill=255, width=th)
    elif pat == "text":  # a filled symbol / glyph as the mask
        txt = str(cue.get("text", "*"))
        f = _font(int(size * 2), cue.get("font"))
        d.text((x, y), txt, fill=255, font=f, anchor="mm")
    else:
        raise ValueError(f"unknown pattern: {pat}")
    return np.asarray(img) > 0


# ---------------------------------------------------------------- encode

def decompose(frame, mask, delta=1):
    """One source frame + mask -> the 4 sub-frames of one cycle."""
    chans = [frame[:, :, 0], frame[:, :, 1], frame[:, :, 2],
             np.zeros_like(frame[:, :, 0])]

    def primary_frame(p):
        out = np.zeros_like(frame)
        if p < 3:
            out[:, :, p] = chans[p]
        return out

    subs = []
    for k in range(4):
        # Non-pattern pixels run the cycle at phase k; pattern pixels
        # run the SAME cycle shifted by `delta`. The 4-frame sum is
        # identical in both regions, while at any instant the primaries
        # differ across the mask boundary — the fringe a saccade reveals.
        f_plain = primary_frame(k)
        f_pat = primary_frame((k + delta) % 4)
        subs.append(np.where(mask[:, :, None], f_pat, f_plain))
    return subs


def _validate(inp_fps, out_fps, gain, cues):
    if not (math.isfinite(inp_fps) and inp_fps > 0):
        raise ValueError(f"bad input fps: {inp_fps}")
    if out_fps % 4 != 0 or out_fps < 4:
        raise ValueError("--out-fps must be a positive multiple of 4")
    if not (math.isfinite(gain) and gain > 0):
        raise ValueError("--gain must be a positive number")
    for c in cues:
        s, e = float(c["start"]), float(c["end"])
        if not (math.isfinite(s) and math.isfinite(e) and 0 <= s < e):
            raise ValueError(f"bad cue interval: {c}")
        if float(c.get("size", 160)) <= 0:
            raise ValueError(f"bad cue size: {c}")


def encode(inp, cues_path, outp, out_fps=480, gain=1.0, codec="x264rgb",
           delta=1):
    info = probe(inp)
    w, h, in_fps = info["width"], info["height"], info["fps"]
    with open(cues_path) as f:
        spec = json.load(f)
    cues = spec["cues"]
    _validate(in_fps, out_fps, gain, cues)
    if not (1 <= delta <= 3):
        raise ValueError("--delta must be 1, 2 or 3")

    px_per_deg = None
    if "display" in spec:
        disp = spec["display"]
        pitch_mm = float(disp["screen_width_mm"]) / w
        px_per_deg = (2 * float(disp["distance_mm"])
                      * math.tan(math.radians(0.5)) / pitch_mm)
    masks = [(float(c["start"]), float(c["end"]),
              make_mask(c, w, h, px_per_deg)) for c in cues]

    total_src = info["nb_frames"] or int(round(info["duration"] * in_fps))
    total_duration = total_src / in_fps
    cycle_dur = 4 / out_fps
    total_cycles = int(math.ceil(total_duration / cycle_dur - 1e-9))
    total_out = total_cycles * 4

    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", inp, "-vf", "format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE)
    enc_cmd = ["ffmpeg", "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", str(out_fps), "-i", "-",
               "-i", inp,  # second input, only for its audio track
               "-map", "0:v:0", "-map", "1:a:0?", "-shortest"]
    if codec == "ffv1":
        enc_cmd += ["-c:v", "ffv1", "-pix_fmt", "rgb24"]
    else:
        enc_cmd += ["-c:v", "libx264rgb", "-crf", "0", "-preset", "medium",
                    "-pix_fmt", "rgb24"]
    enc_cmd += ["-c:a", "copy", outp]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    frame_bytes = w * h * 3
    cur_idx = -1
    cur_frame = None

    def read_src(idx):
        """Advance the decoder stream until frame `idx` is current."""
        nonlocal cur_idx, cur_frame
        while cur_idx < idx:
            raw = dec.stdout.read(frame_bytes)
            if len(raw) == 0:
                raise IOError(f"decoder EOF before source frame {idx}")
            if len(raw) < frame_bytes:
                raise IOError("partial source frame from decoder "
                              f"({len(raw)} of {frame_bytes} bytes)")
            cur_frame = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3).copy()
            cur_idx += 1
        return cur_frame

    try:
        for cyc in range(total_cycles):
            t = cyc * cycle_dur
            src_idx = min(int(t * in_fps + 1e-6), total_src - 1)
            frame = read_src(src_idx)
            if gain != 1.0:
                frame = np.clip(frame.astype(np.float32) * gain,
                                0, 255).astype(np.uint8)
            mask = np.zeros((h, w), dtype=bool)
            for s, e, m in masks:
                if s <= t < e:
                    mask |= m
            for sub in decompose(frame, mask, delta):
                enc.stdin.write(sub.tobytes())
    finally:
        try:
            enc.stdin.close()
        except BrokenPipeError:
            pass
        dec.stdout.close()
        dec_rc = dec.wait()
        enc_rc = enc.wait()
    if dec_rc != 0:
        raise SystemExit(f"decoder failed (ffmpeg rc={dec_rc})")
    if enc_rc != 0:
        raise SystemExit(f"encoder failed (ffmpeg rc={enc_rc})")

    # record what was actually applied (cycle-quantised cue intervals)
    applied = []
    for c, (s, e, _m) in zip(cues, masks):
        a_s = math.ceil(s / cycle_dur - 1e-9) * cycle_dur
        a_e = math.ceil(e / cycle_dur - 1e-9) * cycle_dur
        applied.append({**c, "applied_start": round(a_s, 4),
                        "applied_end": round(a_e, 4)})
    sidecar = {"source": inp, "source_fps": in_fps,
               "source_frames": total_src, "out_fps": out_fps,
               "out_frames": total_out, "delta": delta, "gain": gain,
               "cycle_ms": round(cycle_dur * 1000, 3), "cues": applied}
    with open(outp + ".applied.json", "w") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)
    print(f"source: {w}x{h} @ {in_fps:g} fps, {total_src} frames "
          f"({total_duration:.3f} s)")
    print(f"output: {outp} @ {out_fps} fps, {total_out} sub-frames "
          f"({total_out / out_fps:.3f} s), delta={delta}")
    print(f"applied-cue record: {outp}.applied.json")


# ---------------------------------------------------------------- check

def _read_frames(path, n_max):
    info = probe(path)
    w, h = info["width"], info["height"]
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fb = w * h * 3
    frames = []
    while len(frames) < n_max:
        raw = p.stdout.read(fb)
        if len(raw) < fb:
            break
        frames.append(np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3).copy())
    p.stdout.close()
    p.kill()
    return info, frames


def check(path):
    info, frames = _read_frames(path, 8)
    print(f"{path}: {info['width']}x{info['height']} @ {info['fps']:g} fps, "
          f"duration {info['duration']:.2f} s")
    if len(frames) < 4:
        print("not enough frames to check a full colour cycle")
        return
    avg = np.mean([f.astype(np.float32) for f in frames[:4]], axis=0)
    for i, f in enumerate(frames[:4]):
        nz = [int((f[:, :, c] > 0).sum()) for c in range(3)]
        print(f"  sub-frame {i}: non-zero px per channel R/G/B = {nz}")
    print(f"  4-frame average: mean RGB = {avg.mean(axis=(0, 1)).round(1)} "
          f"(x4 = integrated colour)")


# ---------------------------------------------------------------- preview

def _shift_zero(img, dx):
    """Shift an image horizontally by dx px, zero-filling (no wrap)."""
    if dx == 0:
        return img
    out = np.zeros_like(img)
    if dx > 0:
        out[:, dx:] = img[:, :-dx]
    else:
        out[:, :dx] = img[:, -dx:]
    return out


def preview(path, mode, outp, slip=3):
    """Diagnostic previews for ordinary monitors. `average` shows the
    cycle-integrated image rescaled to source brightness (a diagnostic,
    NOT the photometric brightness a viewer would see). `saccade`
    integrates one cycle while the image slips `slip` px per sub-frame
    (zero-filled edges, no wrap-around). Neither proves perception."""
    info = probe(path)
    w, h, fps = info["width"], info["height"], info["fps"]
    out_fps = 60
    if fps % out_fps != 0:
        raise ValueError(f"preview needs fps divisible by {out_fps}, "
                         f"got {fps:g}")
    per = int(fps // out_fps)  # sub-frames per preview frame (480->8)
    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{w}x{h}", "-r", str(out_fps), "-i", "-",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", outp],
        stdin=subprocess.PIPE)
    fb = w * h * 3
    group = []
    n_out = 0
    while True:
        raw = dec.stdout.read(fb)
        if len(raw) < fb:
            break
        group.append(np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3).astype(np.int32))
        if len(group) == per:
            if mode == "average":
                # integrate ALL cycles in the window, rescale to source
                fr = np.clip(np.sum(group, axis=0) / (per / 4), 0, 255)
            else:
                g = group[:4]  # one full cycle
                acc = np.zeros((h, w, 3), dtype=np.int32)
                for k, f in enumerate(g):
                    acc += _shift_zero(f, -k * slip)
                fr = np.clip(acc, 0, 255)
            enc.stdin.write(fr.astype(np.uint8).tobytes())
            n_out += 1
            group = []
    dec.stdout.close()
    dec.wait()
    enc.stdin.close()
    enc_rc = enc.wait()
    if enc_rc != 0:
        raise SystemExit(f"preview encoder failed (ffmpeg rc={enc_rc})")
    print(f"preview ({mode}): {outp}, {n_out} frames @ {out_fps} fps")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="saccavid")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode")
    e.add_argument("input")
    e.add_argument("cues")
    e.add_argument("-o", "--output", required=True)
    e.add_argument("--out-fps", type=int, default=480)
    e.add_argument("--gain", type=float, default=1.0)
    e.add_argument("--delta", type=int, default=1, choices=[1, 2, 3])
    e.add_argument("--codec", choices=["x264rgb", "ffv1"], default="x264rgb")

    c = sub.add_parser("check")
    c.add_argument("input")

    p = sub.add_parser("preview")
    p.add_argument("input")
    p.add_argument("--mode", choices=["average", "saccade"], default="average")
    p.add_argument("--slip", type=int, default=3)
    p.add_argument("-o", "--output", required=True)

    a = ap.parse_args()
    if a.cmd == "encode":
        encode(a.input, a.cues, a.output, a.out_fps, a.gain, a.codec, a.delta)
    elif a.cmd == "check":
        check(a.input)
    elif a.cmd == "preview":
        preview(a.input, a.mode, a.output, a.slip)


if __name__ == "__main__":
    main()
