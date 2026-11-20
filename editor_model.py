"""Qt-independent, lossless cue document editing.

Existing keys and values are kept until the user explicitly edits them. Loading
accepts semantically invalid cues so the editor can show and repair them; saving
requires a valid document. No rendering or colour-decomposition logic lives here.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Optional


PATTERNS = ("oval", "radial", "circle", "rect", "cross", "arrow", "text")
SNAP_SECONDS = 0.05
MIN_INTERVAL = 0.001


@dataclass(frozen=True)
class ValidationIssue:
    index: Optional[int]
    field: str
    message: str

    def __str__(self):
        prefix = "文書" if self.index is None else "cue {}".format(self.index + 1)
        return "{}: {}".format(prefix, self.message)


class ValidationError(ValueError):
    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


def _number(value):
    """Match the engine's numeric string support, but reject bool/NaN/Inf."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _required_number(value, name):
    result = _number(value)
    if result is None:
        raise ValueError("{} は有限の数値で指定してください。".format(name))
    return result


def snap_time(value, enabled=True):
    """Snap to the nearest 50 ms; exact half-steps round forward."""
    value = _required_number(value, "時刻")
    if enabled:
        value = math.floor(value / SNAP_SECONDS + 0.5 + 1e-10) * SNAP_SECONDS
        return round(value, 9)
    return value


class CueDocument:
    def __init__(self, data=None):
        if data is None:
            data = {"cues": []}
        if not isinstance(data, dict):
            raise ValueError("JSON の最上位はオブジェクトにしてください。")
        if "cues" not in data or not isinstance(data["cues"], list):
            raise ValueError("JSON の cues は配列にしてください。")
        for i, cue in enumerate(data["cues"]):
            if not isinstance(cue, dict):
                raise ValueError("cue {} はオブジェクトにしてください。".format(i + 1))
        self.data = deepcopy(data)

    @property
    def cues(self):
        return self.data["cues"]

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("cues JSON を読み込めません: {}".format(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("JSON の最上位はオブジェクトにしてください。")
        return cls(data)

    def validate(self, duration=None):
        issues = []

        def report(index, field, message):
            issues.append(ValidationIssue(index, field, message))

        length = None
        if duration is not None:
            length = _number(duration)
            if length is None or length <= 0:
                report(None, "duration", "動画の長さは 0 より大きい有限の秒数が必要です。")
                length = None

        display = self.data.get("display")
        valid_display = False
        if "display" in self.data:
            if not isinstance(display, dict):
                report(None, "display", "display は画面幅と視距離を含むオブジェクトにしてください。")
            else:
                valid_display = True
                for key, label in (("screen_width_mm", "画面幅"), ("distance_mm", "視距離")):
                    value = _number(display.get(key))
                    if value is None or value <= 0:
                        report(None, "display." + key, "{} (mm) は 0 より大きい有限値にしてください。".format(label))
                        valid_display = False

        for i, cue in enumerate(self.cues):
            start, end = _number(cue.get("start")), _number(cue.get("end"))
            if start is None:
                report(i, "start", "開始時刻 start は有限の秒数が必要です。")
            elif start < 0:
                report(i, "start", "開始時刻 start は 0 秒以上にしてください。")
            if end is None:
                report(i, "end", "終了時刻 end は有限の秒数が必要です。")
            elif start is not None and end <= start:
                report(i, "end", "終了時刻 end は開始時刻 start より後にしてください。")
            if end is not None and length is not None and end > length + 1e-9:
                report(i, "end", "終了時刻 {:.3f} 秒が動画長 {:.3f} 秒を超えています。".format(end, length))

            if cue.get("pattern", "oval") not in PATTERNS:
                report(i, "pattern", "pattern は oval / radial / circle / rect / cross / arrow / text から選んでください。")
            for key in ("x", "y", "nx", "ny"):
                if key not in cue:
                    continue
                # The engine treats null normalized coordinates as absent.
                if key in ("nx", "ny") and cue[key] is None:
                    continue
                value = _number(cue[key])
                if value is None:
                    report(i, key, "位置 {} は有限の数値にしてください。".format(key))
                elif key in ("nx", "ny") and not 0 <= value <= 1:
                    report(i, key, "位置 {} は 0〜1 の範囲にしてください。".format(key))

            for key in ("size", "size_deg", "thickness", "thickness_deg"):
                if key not in cue:
                    continue
                value = _number(cue[key])
                if value is None or value <= 0:
                    report(i, key, "{} は 0 より大きい有限値にしてください。".format(key))
                if key.endswith("_deg") and not valid_display:
                    report(i, key, "{} には有効な display の画面幅・視距離が必要です。".format(key))

            if "angle" in cue and _number(cue["angle"]) is None:
                report(i, "angle", "angle は有限の角度にしてください。")
            if "font" in cue and cue["font"] is not None and not isinstance(cue["font"], str):
                report(i, "font", "font はフォントファイルのパス文字列にしてください。")

        try:
            json.dumps(self.data, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            report(None, "json", "JSON に保存できない値（NaN、無限大など）が含まれています。")
        return issues

    def save(self, path, duration=None):
        issues = self.validate(duration)
        if issues:
            raise ValidationError(issues)
        destination = Path(path)
        # Serialize first and replace atomically: a failed write never truncates
        # the user's last valid cue document.
        # Both existing consumers use open() without an explicit encoding.
        # Escaping Unicode keeps Japanese text/font paths portable to Windows
        # locales while preserving their decoded JSON values exactly.
        text = json.dumps(self.data, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix="." + destination.name + ".", suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
            os.replace(str(temporary), str(destination))
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return destination

    def add_cue(self, time):
        time = _required_number(time, "開始時刻")
        if time < 0:
            raise ValueError("開始時刻は 0 秒以上にしてください。")
        self.cues.append({"start": time, "end": time + 1.5, "nx": 0.5, "ny": 0.5,
                          "pattern": "oval", "size": 70, "thickness": 6})
        return len(self.cues) - 1

    def duplicate(self, index):
        self.cues.append(deepcopy(self.cues[index]))
        return len(self.cues) - 1

    def delete(self, index):
        del self.cues[index]

    def _interval(self, index):
        cue = self.cues[index]
        start = _required_number(cue.get("start"), "開始時刻")
        end = _required_number(cue.get("end"), "終了時刻")
        if not 0 <= start < end:
            raise ValueError("時間をドラッグする前に start / end の不正値を修正してください。")
        return start, end

    def move(self, index, start, snap=False, duration=None):
        previous_start, previous_end = self._interval(index)
        width = previous_end - previous_start
        start = max(0.0, snap_time(start, snap))
        if duration is not None:
            duration = _required_number(duration, "動画長")
            if duration < width:
                raise ValueError("cue の長さが動画長を超えています。先に終了時刻を修正してください。")
            start = min(start, duration - width)
        end = start + width
        self.cues[index].update(start=start, end=end)
        return start, end

    def resize(self, index, edge, time, snap=False, duration=None):
        start, end = self._interval(index)
        time = snap_time(time, snap)
        if edge == "start":
            start = max(0.0, min(time, end - min(MIN_INTERVAL, (end - start) / 2)))
        elif edge == "end":
            minimum = start + MIN_INTERVAL
            if duration is not None:
                duration = _required_number(duration, "動画長")
                if duration < minimum:
                    raise ValueError("開始時刻が動画の末尾に近すぎます。先に開始時刻を修正してください。")
                time = min(time, duration)
            end = max(minimum, time)
        else:
            raise ValueError("伸縮する端は start または end で指定してください。")
        self.cues[index].update(start=start, end=end)
        return start, end

    def position(self, index, width, height):
        width, height = self._dimensions(width, height)
        cue = self.cues[index]
        result = []
        for px, normalized, dimension in (("x", "nx", width), ("y", "ny", height)):
            if cue.get(normalized) is not None:
                result.append(_required_number(cue[normalized], normalized))
            else:
                result.append(_required_number(cue.get(px, dimension / 2), px) / dimension)
        return tuple(result)

    @staticmethod
    def _dimensions(width, height):
        width = _required_number(width, "動画の幅")
        height = _required_number(height, "動画の高さ")
        if width <= 0 or height <= 0:
            raise ValueError("動画の幅・高さは 0 より大きい値にしてください。")
        return width, height

    def set_position(self, index, nx, ny, width, height):
        width, height = self._dimensions(width, height)
        nx, ny = _required_number(nx, "nx"), _required_number(ny, "ny")
        if not 0 <= nx <= 1 or not 0 <= ny <= 1:
            raise ValueError("位置 nx / ny は 0〜1 の範囲にしてください。")
        cue = self.cues[index]
        for px, normalized, value, dimension in (("x", "nx", nx, width), ("y", "ny", ny, height)):
            if cue.get(normalized) is not None or px not in cue:
                cue[normalized] = value
            else:
                cue[px] = value * dimension

    def set_coordinate_mode(self, index, normalized, width, height):
        """Explicit conversion, unlike normal editing, removes shadowed keys."""
        nx, ny = self.position(index, width, height)
        cue = self.cues[index]
        if normalized:
            cue.update(nx=nx, ny=ny)
            cue.pop("x", None)
            cue.pop("y", None)
        else:
            cue.update(x=nx * width, y=ny * height)
            cue.pop("nx", None)
            cue.pop("ny", None)

    def set_display(self, width_mm, distance_mm):
        width_mm = _required_number(width_mm, "画面幅")
        distance_mm = _required_number(distance_mm, "視距離")
        if width_mm <= 0 or distance_mm <= 0:
            raise ValueError("画面幅と視距離は 0 より大きい値にしてください。")
        if not isinstance(self.data.get("display"), dict):
            self.data["display"] = {}
        self.data["display"].update(screen_width_mm=width_mm, distance_mm=distance_mm)

    def px_per_degree(self, width):
        if "display" not in self.data:
            return None
        display = self.data["display"]
        if not isinstance(display, dict):
            raise ValueError("display はオブジェクトにしてください。")
        width = _required_number(width, "動画の幅")
        screen = _required_number(display.get("screen_width_mm"), "画面幅")
        distance = _required_number(display.get("distance_mm"), "視距離")
        if min(width, screen, distance) <= 0:
            raise ValueError("動画幅・画面幅・視距離は 0 より大きい値にしてください。")
        return 2 * distance * math.tan(math.radians(0.5)) / (screen / width)

    def active_cues(self, time):
        time = _required_number(time, "現在時刻")
        return [cue for cue in self.cues
                if _number(cue.get("start")) is not None and _number(cue.get("end")) is not None
                and float(cue["start"]) <= time < float(cue["end"])]
