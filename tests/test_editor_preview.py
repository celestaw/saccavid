"""Regression checks for shared geometry, diagnostic math and RGB decoding."""

import math
import subprocess

import numpy as np
import pytest

import editor_preview
from editor_preview import VideoSource, active_mask, px_per_degree, render_preview
from saccavid import _shift_zero, decompose, make_mask


def cue(**changes):
    value = dict(start=0.5, end=1.5, nx=0.5, ny=0.5,
                 pattern="oval", size=10, thickness=2)
    value.update(changes)
    return value


@pytest.mark.parametrize("pattern", ["oval", "radial", "circle", "rect", "cross", "arrow", "text"])
def test_each_pattern_uses_engine_mask(pattern):
    item = cue(pattern=pattern, text="!")
    np.testing.assert_array_equal(active_mask({"cues": [item]}, 0.5, 80, 60),
                                  make_mask(item, 80, 60))


def test_active_intervals_and_union():
    first, second = cue(nx=0.2), cue(nx=0.8, start=1, end=2)
    spec = {"cues": [first, second]}
    assert not active_mask(spec, 0.49, 80, 60).any()
    np.testing.assert_array_equal(active_mask(spec, 1, 80, 60),
                                  make_mask(first, 80, 60) | make_mask(second, 80, 60))
    np.testing.assert_array_equal(active_mask(spec, 1.5, 80, 60), make_mask(second, 80, 60))
    assert not active_mask(spec, 2, 80, 60).any()


def test_degree_mask_conversion_matches_encoder():
    spec = {"display": {"screen_width_mm": 597, "distance_mm": 530},
            "cues": [cue(size_deg=4, thickness_deg=0.3)]}
    expected_scale = 2 * 530 * math.tan(math.radians(0.5)) / (597 / 320)
    assert px_per_degree(spec, 320) == expected_scale
    np.testing.assert_array_equal(active_mask(spec, 0.8, 320, 240),
                                  make_mask(spec["cues"][0], 320, 240, expected_scale))


def test_normal_overlay_and_frame_not_mutated():
    frame = np.full((60, 80, 3), [100, 80, 40], dtype=np.uint8)
    original = frame.copy()
    spec = {"cues": [cue()]}
    mask = make_mask(spec["cues"][0], 80, 60)
    np.testing.assert_array_equal(render_preview(frame, spec, 1, overlay=False), frame)
    output = render_preview(frame, spec, 1)
    np.testing.assert_array_equal(output[~mask], frame[~mask])
    assert np.all(np.any(output[mask] != frame[mask], axis=1))
    np.testing.assert_array_equal(frame, original)


@pytest.mark.parametrize("delta", [1, 2, 3])
def test_fixation_is_quarter_source_without_overflow(delta):
    rng = np.random.default_rng(41)
    frame = rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)
    output = render_preview(frame, {"cues": [cue()]}, 1,
                            mode="fixation", overlay=False, delta=delta)
    np.testing.assert_array_equal(output, frame // 4)


def test_saccade_matches_engine_shift_and_has_no_wrap():
    frame = np.zeros((4, 40, 3), dtype=np.uint8)
    frame[:, 0, 1:] = 255
    output = render_preview(frame, {"cues": []}, 0,
                            mode="saccade", overlay=False, slip=3)
    assert not output.any()  # Green and blue move off the left edge, never to the right.
    frame[:] = np.arange(40, dtype=np.uint8)[None, :, None] * 6
    spec = {"cues": [cue(start=0, size=5, ny=0.5)]}
    mask = active_mask(spec, 0, 40, 4)
    expected = sum(_shift_zero(sub.astype(np.int32), -k * 3)
                   for k, sub in enumerate(decompose(frame, mask)))
    np.testing.assert_array_equal(render_preview(frame, spec, 0, "saccade", False),
                                  np.clip(expected, 0, 255).astype(np.uint8))


@pytest.mark.parametrize("bad", [cue(end=0.2), cue(size=0), cue(size=float("nan")), cue(size_deg=2)])
def test_invalid_cues_raise_useful_error(bad):
    with pytest.raises(ValueError, match="cue 1"):
        active_mask({"cues": [bad]}, 1, 80, 60)


def test_bad_display_and_preview_arguments():
    with pytest.raises(ValueError, match="画面幅"):
        active_mask({"display": {"screen_width_mm": 0, "distance_mm": 530}, "cues": []}, 0, 80, 60)
    frame = np.zeros((4, 40, 3), dtype=np.uint8)
    for kwargs in ({"mode": "unknown"}, {"slip": 0}, {"slip": 1.5}, {"delta": 0}, {"delta": 1.0}):
        with pytest.raises(ValueError):
            render_preview(frame, {"cues": []}, 0, **kwargs)


class FakeCapture:
    def __init__(self, path):
        self.position = 0
        self.seeks = []
        self.reads = 0
        self.released = False

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.seeks.append(value)
        self.position = value
        return True

    def read(self):
        self.reads += 1
        bgr = np.full((2, 3, 3), [10, 20, 200 + self.position], dtype=np.uint8)
        self.position += 1
        return True, bgr

    def release(self):
        self.released = True


def test_video_rgb_random_sequential_and_cached_reads(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_preview, "probe", lambda path: dict(width=3, height=2, fps=10.0, duration=1.0, nb_frames=10))
    monkeypatch.setattr(editor_preview.cv2, "VideoCapture", FakeCapture)
    source = VideoSource(tmp_path / "example.mp4")
    capture = source.capture
    assert source.frame_count == 10
    assert source.read(0)[0, 0].tolist() == [200, 20, 10]
    assert source.read(1)[0, 0].tolist() == [201, 20, 10]
    assert capture.seeks == []
    assert source.read(5)[0, 0].tolist() == [205, 20, 10]
    assert capture.seeks == [5]
    output = source.read(5)
    output[:] = 0
    assert source.read(5)[0, 0].tolist() == [205, 20, 10]
    assert capture.reads == 3
    assert source.read(2)[0, 0].tolist() == [202, 20, 10]
    assert capture.seeks == [5, 2]
    with pytest.raises(ValueError, match="フレーム番号"):
        source.read(10)
    source.close()
    assert capture.released
    with pytest.raises(ValueError, match="閉じられて"):
        source.read(0)


def test_video_open_failure_releases_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_preview, "probe", lambda path: dict(width=3, height=2, fps=10.0, duration=1.0, nb_frames=10))
    capture = FakeCapture("ignored")
    monkeypatch.setattr(capture, "isOpened", lambda: False)
    monkeypatch.setattr(editor_preview.cv2, "VideoCapture", lambda path: capture)
    with pytest.raises(ValueError, match="動画を開けません"):
        VideoSource(tmp_path / "bad.mp4")
    assert capture.released


def test_video_duration_matches_encoder_when_container_is_longer(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_preview, "probe", lambda path: dict(width=3, height=2, fps=10.0, duration=2.0, nb_frames=10))
    monkeypatch.setattr(editor_preview.cv2, "VideoCapture", FakeCapture)
    with VideoSource(tmp_path / "long-audio.mp4") as source:
        assert source.info["duration"] == 1.0
        assert source.info["container_duration"] == 2.0
        assert source.frame_count == 10


def test_real_video_decode_rgb_and_seek(tmp_path):
    frames = np.stack([np.full((6, 8, 3), colour, dtype=np.uint8)
                       for colour in ([200, 20, 10], [15, 180, 60], [7, 45, 240])])
    path = tmp_path / "colours.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", "8x6", "-r", "10", "-i", "-", "-c:v", "ffv1",
                    "-pix_fmt", "bgr0", str(path)],
                   input=frames.tobytes(), capture_output=True, check=True)
    with VideoSource(path) as source:
        assert source.frame_count == 3
        assert source.info["fps"] == 10
        for index in (0, 1, 2, 0, 2):
            np.testing.assert_array_equal(source.read(index), frames[index])
