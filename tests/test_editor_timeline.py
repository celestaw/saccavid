"""Real Qt mouse interactions for the timeline, without a visible desktop."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from editor_model import CueDocument
from editor_timeline import TimelineWidget


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def timeline(application):
    doc = CueDocument({"cues": [{"start": .5, "end": 1.5, "nx": .5, "ny": .5,
                                "pattern": "oval", "size": 70, "thickness": 6}]})
    widget = TimelineWidget()
    widget.resize(1080, 150)  # 1000 px time axis, 3 ms per pixel.
    widget.set_document(doc, 3.0)
    widget.show()
    application.processEvents()
    yield widget, doc
    widget.close()
    widget.deleteLater()
    application.processEvents()


def point(widget, seconds, row=0):
    return QPoint(round(widget._x(seconds)),
                  widget.RULER + row * widget.ROW + widget.ROW // 2)


def drag(widget, start, end):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)


def test_body_drag_selects_and_preserves_duration(timeline):
    widget, doc = timeline
    selections, changes = [], []
    widget.selectionChanged.connect(selections.append)
    widget.cueChanged.connect(changes.append)
    drag(widget, point(widget, 1), point(widget, 1.2))
    assert selections == [0] and changes == [0]
    assert doc.cues[0]["start"] == pytest.approx(.7)
    assert doc.cues[0]["end"] == pytest.approx(1.7)


def test_both_resize_edges(timeline):
    widget, doc = timeline
    drag(widget, point(widget, .5), point(widget, .8))
    assert doc.cues[0]["start"] == pytest.approx(.8)
    assert doc.cues[0]["end"] == pytest.approx(1.5)
    drag(widget, point(widget, 1.5), point(widget, 1.2))
    assert doc.cues[0]["start"] == pytest.approx(.8)
    assert doc.cues[0]["end"] == pytest.approx(1.2)


@pytest.mark.parametrize("snap, expected", [(True, .6), (False, .623)])
def test_snap_toggle_changes_drag_precision(timeline, snap, expected):
    widget, doc = timeline
    widget.snap = snap
    start = point(widget, 1)
    drag(widget, start, start + QPoint(41, 0))
    assert doc.cues[0]["start"] == pytest.approx(expected)
    assert doc.cues[0]["end"] - doc.cues[0]["start"] == pytest.approx(1)


def test_body_drag_clamps_to_video_bounds(timeline):
    widget, doc = timeline
    drag(widget, point(widget, 1), QPoint(-100, point(widget, 1).y()))
    assert doc.cues[0]["start"] == 0
    assert doc.cues[0]["end"] == 1
    drag(widget, point(widget, .5), QPoint(widget.width() + 100, point(widget, .5).y()))
    assert doc.cues[0]["start"] == 2
    assert doc.cues[0]["end"] == 3


def test_resizing_cannot_invert_interval(timeline):
    widget, doc = timeline
    drag(widget, point(widget, 1.5), point(widget, 0))
    assert doc.cues[0]["start"] == .5
    assert .5 < doc.cues[0]["end"] <= .50100001


def test_ruler_click_and_drag_emit_seek(timeline):
    widget, _ = timeline
    seeks = []
    widget.seekRequested.connect(seeks.append)
    start = QPoint(round(widget._x(1.2)), 10)
    end = QPoint(round(widget._x(1.8)), 10)
    drag(widget, start, end)
    assert seeks[0] == pytest.approx(1.2, abs=.002)
    assert seeks[-1] == pytest.approx(1.8, abs=.002)


def test_invalid_cue_remains_selectable_without_dragging(timeline, application):
    widget, doc = timeline
    doc.cues[0].update(start="invalid", end=float("nan"))
    widget.set_document(doc, 3)
    selections = []
    widget.selectionChanged.connect(selections.append)
    application.processEvents()
    assert not widget.grab().isNull()
    start = QPoint(widget.LEFT + 2, widget.RULER + widget.ROW // 2)
    drag(widget, start, start + QPoint(100, 0))
    assert selections == [0]
    assert doc.cues[0]["start"] == "invalid"


def test_model_drag_failure_is_reported_without_crash(timeline):
    widget, doc = timeline
    doc.cues[0]["end"] = 4.0  # Wider than the 3-second video.
    errors = []
    widget.errorOccurred.connect(errors.append)
    drag(widget, point(widget, 1.2), point(widget, 1.4))
    assert errors and "動画長" in errors[0]
    assert doc.cues[0]["start"] == .5 and doc.cues[0]["end"] == 4


def test_parent_refresh_during_drag_keeps_absolute_origin(timeline):
    widget, doc = timeline
    changes = []

    def refresh(index):
        changes.append(index)
        widget.set_document(doc, 3)
        widget.set_selected(index)

    widget.cueChanged.connect(refresh)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=point(widget, 1))
    QTest.mouseMove(widget, point(widget, 1.2))
    QTest.mouseMove(widget, point(widget, 1.4))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=point(widget, 1.4))
    assert len(changes) == 2
    assert doc.cues[0]["start"] == pytest.approx(.9)
    assert doc.cues[0]["end"] == pytest.approx(1.9)
