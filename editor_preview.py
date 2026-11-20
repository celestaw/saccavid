"""Normal-fps editing previews; diagnostics do not predict actual perception.

Mask geometry and colour decomposition come exclusively from the existing
encoder. No presentation window, audio, or 480 Hz playback is started here.
"""

import math
from pathlib import Path

import cv2
import numpy as np

from saccavid import _shift_zero, decompose, make_mask, probe


def _number(value, name, positive=False):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} は数値で指定してください") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "有限の正の数" if positive else "有限の数"
        raise ValueError(f"{name} は{qualifier}で指定してください")
    return number


def px_per_degree(spec, width):
    """Match the encoder/player's small-angle pixel conversion exactly."""
    if "display" not in spec:
        return None
    display = spec["display"]
    if not isinstance(display, dict):
        raise ValueError("display は画面幅と視距離を含むオブジェクトにしてください")
    screen = _number(display.get("screen_width_mm"), "画面幅 (mm)", True)
    distance = _number(display.get("distance_mm"), "視距離 (mm)", True)
    pitch_mm = screen / width
    return 2 * distance * math.tan(math.radians(0.5)) / pitch_mm


def active_mask(spec, time, width, height):
    """Union of active cues, using the engine's half-open [start, end) rule."""
    time = _number(time, "現在時刻")
    if width <= 0 or height <= 0:
        raise ValueError("動画の幅と高さは正の値である必要があります")
    if not isinstance(spec, dict) or not isinstance(spec.get("cues"), list):
        raise ValueError("cues 配列を含む JSON オブジェクトが必要です")
    scale = px_per_degree(spec, width)
    result = np.zeros((height, width), dtype=bool)
    for index, cue in enumerate(spec["cues"]):
        try:
            if not isinstance(cue, dict):
                raise ValueError("cue は JSON オブジェクトにしてください")
            start = _number(cue.get("start"), "start")
            end = _number(cue.get("end"), "end")
            if not 0 <= start < end:
                raise ValueError("0 ≦ start < end を満たす必要があります")
            if start <= time < end:
                for key in ("size", "size_deg", "thickness", "thickness_deg"):
                    if key in cue:
                        _number(cue[key], key, True)
                result |= make_mask(cue, width, height, scale)
        except Exception as exc:
            raise ValueError(f"cue {index + 1} のマスクを生成できません: {exc}") from exc
    return result


def render_preview(frame, spec, time, mode="normal", overlay=True, slip=3, delta=1):
    """Return an independent RGB uint8 image for an ordinary-fps preview.

    Fixation is the arithmetic four-slot mean (sum / 4). Saccade is a
    zero-filled shifted sum, clipped to byte range, as in the existing
    diagnostic preview. Neither represents calibrated emitted luminance.
    The optional editing overlay is applied after the diagnostic image.
    """
    if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
            or frame.ndim != 3 or frame.shape[2] != 3
            or frame.shape[0] <= 0 or frame.shape[1] <= 0):
        raise ValueError("プレビューには RGB uint8 の動画フレームが必要です")
    if mode not in ("normal", "fixation", "saccade"):
        raise ValueError(f"不明なプレビューモード: {mode}")
    if (isinstance(delta, bool) or not isinstance(delta, (int, np.integer))
            or delta not in (1, 2, 3)):
        raise ValueError("位相差 delta は 1〜3 の整数にしてください")
    if isinstance(slip, bool) or not isinstance(slip, (int, np.integer)) or not 1 <= slip <= 10:
        raise ValueError("slip は 1〜10 px の整数にしてください")
    height, width = frame.shape[:2]
    mask = active_mask(spec, time, width, height)
    if mode == "normal":
        image = frame.copy()
    else:
        subframes = decompose(frame, mask, delta=int(delta))
        if mode == "fixation":
            image = (np.sum(subframes, axis=0, dtype=np.int32) / 4).astype(np.uint8)
        else:
            total = np.zeros(frame.shape, dtype=np.int32)
            for index, subframe in enumerate(subframes):
                total += _shift_zero(subframe, -index * int(slip))
            image = np.clip(total, 0, 255).astype(np.uint8)
    if overlay:
        colour = np.array([255, 176, 40], dtype=np.float32)
        image[mask] = (image[mask].astype(np.float32) * 0.55 + colour * 0.45).astype(np.uint8)
    return np.ascontiguousarray(image)


class VideoSource:
    """CFR frame reader backed by OpenCV; metadata comes from ffprobe.

    Sequential reads avoid costly seeks. Random seeks and repeated reads
    are frame indexed. The returned array is RGB, never OpenCV's BGR.
    """

    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        self.capture = None
        self._last_index = -1
        self._last_frame = None
        try:
            self.info = probe(self.path)
            fps = _number(self.info["fps"], "動画 fps", True)
            _number(self.info["duration"], "動画長", True)
            if self.info["width"] <= 0 or self.info["height"] <= 0:
                raise ValueError("動画の幅または高さが不正です")
            self.frame_count = self.info.get("nb_frames") or int(round(self.info["duration"] * fps))
            if self.frame_count <= 0:
                raise ValueError("読み込める動画フレームがありません")
            # probe reports container duration, which may include longer audio.
            # The encoder and player use source frame count / fps instead.
            self.info["container_duration"] = self.info["duration"]
            self.info["duration"] = self.frame_count / fps
            self.capture = cv2.VideoCapture(self.path)
            if not self.capture.isOpened():
                raise ValueError("OpenCV で動画を開けません。コーデックを確認してください")
        except Exception as exc:
            self.close()
            raise ValueError(f"動画を開けません: {exc}") from exc

    def read(self, index):
        if self.capture is None:
            raise ValueError("動画は閉じられています")
        if (isinstance(index, bool) or not isinstance(index, (int, np.integer))
                or not 0 <= index < self.frame_count):
            raise ValueError(f"フレーム番号は 0〜{self.frame_count - 1} の整数にしてください")
        index = int(index)
        if index == self._last_index and self._last_frame is not None:
            return self._last_frame.copy()
        if index != self._last_index + 1:
            if not self.capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                raise ValueError(f"フレーム {index} へシークできません")
        success, bgr = self.capture.read()
        if not success or bgr is None:
            # A retry must seek again, because a failed decode may advance.
            self._last_index = -2
            self._last_frame = None
            raise ValueError(f"フレーム {index} を読み込めません")
        if bgr.shape[:2] != (self.info["height"], self.info["width"]):
            self._last_index = -2
            self._last_frame = None
            raise ValueError("デコード画像と ffprobe の解像度が一致しません。動画の回転情報を確認してください")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        self._last_index, self._last_frame = index, rgb
        return rgb.copy()

    def close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self._last_frame = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
