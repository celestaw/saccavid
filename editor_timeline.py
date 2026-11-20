"""Cue timeline for the editor; all edits are delegated to CueDocument."""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


class TimelineWidget(QWidget):
    """One row per cue, with a shared time axis and draggable interval edges.

    Put this widget inside a widget-resizable QScrollArea for many cues.
    ``duration=None`` permits editing before a source video has been opened.
    The row separation is only an editing aid, not multiple video tracks.
    """

    selectionChanged = Signal(int)
    seekRequested = Signal(float)
    cueChanged = Signal(int)
    errorOccurred = Signal(str)

    LEFT = 62
    RIGHT = 18
    RULER = 34
    ROW = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document = None
        self.duration = None
        self.time = 0.0
        self.selected = -1
        self.snap = True
        self._drag = None
        self.setMinimumSize(420, 106)
        self.setMouseTracking(True)
        self.setToolTip("ブロックをドラッグ: 移動 / 左右端: 長さ変更 / 目盛り・空白: シーク")

    def set_document(self, doc, duration=None):
        if doc is not self.document:
            self._drag = None
        self.document = doc
        d = _number(duration)
        self.duration = d if d > 0 else None
        count = len(doc.cues) if doc is not None else 0
        self.setMinimumHeight(max(106, self.RULER + count * self.ROW + 16))
        if self.selected >= count:
            self.selected = -1
        self.update()

    def set_time(self, seconds):
        self.time = max(0.0, _number(seconds))
        self.update()

    def set_selected(self, index):
        self.selected = index
        self.update()

    def _span(self):
        if self._drag is not None and "span" in self._drag:
            return self._drag["span"]
        if self.duration is not None:
            return self.duration
        last = max((_number(c.get("end")) for c in self.document.cues),
                   default=0.0) if self.document is not None else 0.0
        return max(3.0, last, self.time)

    def _width(self):
        return max(1, self.width() - self.LEFT - self.RIGHT)

    def _x(self, seconds):
        return self.LEFT + seconds / self._span() * self._width()

    def _time_at(self, x):
        return max(0.0, min(self._span(),
                           (x - self.LEFT) / self._width() * self._span()))

    def _block(self, index):
        cue = self.document.cues[index]
        start = _number(cue.get("start"))
        end = _number(cue.get("end"), start)
        # Even an invalid interval remains visible and selectable for repair.
        x1 = min(self.width() - self.RIGHT - 5, max(self.LEFT, self._x(start)))
        x2 = min(self.width() - self.RIGHT, max(self.LEFT, self._x(end)))
        return QRectF(x1, self.RULER + index * self.ROW + 4,
                      max(5.0, x2 - x1), self.ROW - 8)

    def _hit(self, position):
        if self.document is None:
            return None
        index = int((position.y() - self.RULER) // self.ROW)
        if not 0 <= index < len(self.document.cues):
            return None
        rect = self._block(index)
        if not rect.adjusted(-3, 0, 3, 0).contains(position):
            return None
        left_distance = abs(position.x() - rect.left())
        right_distance = abs(position.x() - rect.right())
        if min(left_distance, right_distance) <= 6:
            edge = "start" if left_distance <= right_distance else "end"
        else:
            edge = "move"
        return index, edge

    def _ticks(self):
        # About 90 logical pixels between labels, with useful decimal steps.
        target = self._span() * 90.0 / self._width()
        exponent = 10 ** math.floor(math.log10(max(target, 1e-9)))
        for factor in (1, 2, 2.5, 5, 10):
            step = exponent * factor
            if step >= target:
                break
        return step

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#18212b"))
        painter.fillRect(0, 0, self.width(), self.RULER, QColor("#25313d"))
        painter.setPen(QColor("#adbfcc"))
        painter.drawText(8, 22, "秒")
        step = self._ticks()
        n = int(self._span() / step) + 1
        for i in range(n):
            t = i * step
            x = self._x(t)
            painter.setPen(QColor("#344452"))
            painter.drawLine(QPointF(x, self.RULER), QPointF(x, self.height()))
            painter.setPen(QColor("#adbfcc"))
            painter.drawText(QPointF(x + 3, 20), f"{t:g}")
        cues = self.document.cues if self.document is not None else []
        invalid = set()
        if self.document is not None:
            invalid = {issue.index for issue in self.document.validate(self.duration)
                       if issue.index is not None}
        for index, cue in enumerate(cues):
            rect = self._block(index)
            painter.setPen(QColor("#adbfcc"))
            painter.drawText(QRectF(4, rect.top(), self.LEFT - 8, rect.height()),
                             Qt.AlignmentFlag.AlignCenter, f"{index + 1}")
            fill = "#963e4b" if index in invalid else "#326c84"
            if index == self.selected:
                fill = "#b6525c" if index in invalid else "#238ea8"
            painter.setBrush(QColor(fill))
            painter.setPen(QPen(QColor("#f0cb77" if index == self.selected else "#6596aa"),
                                2 if index == self.selected else 1))
            painter.drawRoundedRect(rect, 3, 3)
            painter.save()
            painter.setClipRect(rect.adjusted(5, 0, -5, 0))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect.adjusted(8, 0, -8, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             f"{cue.get('pattern', 'oval')}  "
                             f"{_number(cue.get('start')):.3f}–{_number(cue.get('end')):.3f}")
            painter.restore()
            if rect.width() > 14:
                painter.setPen(QPen(QColor("#d5e5e8"), 1))
                for edge_x in (rect.left() + 4, rect.right() - 4):
                    painter.drawLine(QPointF(edge_x, rect.top() + 6),
                                     QPointF(edge_x, rect.bottom() - 6))
        if not cues:
            painter.setPen(QColor("#adbfcc"))
            painter.drawText(QRectF(self.LEFT, self.RULER + 16, self._width(), 36),
                             Qt.AlignmentFlag.AlignCenter,
                             "cue を追加すると、この位置に時間ブロックを表示します")
        head_x = self._x(max(0, min(self.time, self._span())))
        painter.setPen(QPen(QColor("#f4bf59"), 2))
        painter.drawLine(QPointF(head_x, 27), QPointF(head_x, self.height()))
        painter.setBrush(QColor("#f4bf59"))
        painter.drawPolygon(QPolygonF([QPointF(head_x - 5, 25),
                                      QPointF(head_x + 5, 25),
                                      QPointF(head_x, 32)]))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        position = event.position()
        hit = self._hit(position)
        if hit is None:
            self._drag = {"edge": "seek"}
            self.seekRequested.emit(self._time_at(position.x()))
        else:
            index, edge = hit
            cue = self.document.cues[index]
            start = _number(cue.get("start"), float("nan"))
            end = _number(cue.get("end"), float("nan"))
            self.selected = index
            if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                span = self._span()
                self._drag = {"index": index, "edge": edge, "start": start,
                              "end": end, "press_time": self._time_at(position.x()),
                              "span": span}
            else:
                self._drag = None
            self.selectionChanged.emit(index)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        position = event.position()
        if self._drag is None:
            hit = self._hit(position)
            cursor = (Qt.CursorShape.SizeHorCursor if hit and hit[1] != "move"
                      else Qt.CursorShape.OpenHandCursor if hit
                      else Qt.CursorShape.ArrowCursor)
            self.setCursor(cursor)
            return
        drag = self._drag
        current = self._time_at(position.x())
        if drag["edge"] == "seek":
            self.seekRequested.emit(current)
            return
        delta = current - drag["press_time"]
        try:
            if drag["edge"] == "move":
                self.document.move(drag["index"], drag["start"] + delta,
                                   snap=self.snap, duration=self.duration)
            else:
                edge = drag["edge"]
                self.document.resize(drag["index"], edge, drag[edge] + delta,
                                     snap=self.snap, duration=self.duration)
            self.cueChanged.emit(drag["index"])
        except (ValueError, TypeError, IndexError) as exc:
            self.errorOccurred.emit(f"時間を変更できません: {exc}")
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
            self.unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)
