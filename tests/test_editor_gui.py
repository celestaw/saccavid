"""Offscreen editor integration tests; never run the presentation player."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import shlex
import sys
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from editor_model import CueDocument
import sacca_editor
from sacca_editor import EditorWindow, smoke_test


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "tests" / "test_in.mp4"


@pytest.fixture(scope="session")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def editor(app, monkeypatch):
    if not VIDEO.exists():
        pytest.fail("Run python make_test_input.py before the GUI integration tests")
    # No test may accidentally enter a modal native file dialog.
    def forbidden_dialog(*args, **kwargs):
        raise AssertionError("Unexpected file dialog")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", forbidden_dialog)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", forbidden_dialog)
    window = EditorWindow()
    window.open_video(VIDEO)
    window.open_cues(ROOT / "cues.example.json")
    yield window
    window.pause()
    window.dirty = False
    window.close()
    window.deleteLater()
    app.processEvents()


def image_pixels(image):
    """QImage RGB888 with possible row padding -> independent RGB ndarray."""
    array = np.frombuffer(image.constBits(), dtype=np.uint8)
    rows = array.reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy()


def test_startup_does_not_play_or_launch_external_process(editor):
    assert editor.process is None
    assert not editor.timer.isActive()
    assert editor.frame_index == 0
    assert editor.source.info["fps"] == 60
    assert editor.frame.shape == (240, 320, 3)
    assert "320 × 240" in editor.info_label.text()
    assert not editor.preview.image.isNull()


def test_invalid_interval_is_highlighted_and_save_leaves_file_untouched(editor, tmp_path):
    path = tmp_path / "cues.json"
    path.write_text("unchanged", encoding="utf-8")
    editor.cues_path = path
    editor.select_cue(0)
    editor.fields["end"].setValue(0.5)
    assert "開始時刻" in editor.validation_label.text()
    assert editor.table.item(0, 0).background().color().name() == "#713333"
    assert not editor.save_cues()
    assert editor.index == 0
    assert path.read_text(encoding="utf-8") == "unchanged"
    assert "保存できません" in editor.log.toPlainText()
    editor.fields["end"].setValue(4)
    assert "動画長" in editor.validation_label.text()


def test_zero_display_can_be_edited_without_uncaught_exception(editor):
    editor.doc.data["display"] = {"screen_width_mm": 597, "distance_mm": 530, "note": "keep"}
    editor.doc.cues[0]["size_deg"] = 2
    editor.refresh_document()
    editor.fields["screen_width_mm"].blockSignals(True)
    editor.fields["screen_width_mm"].setValue(0)
    editor.fields["screen_width_mm"].blockSignals(False)
    editor.change_display()  # Regression: model.set_display used to raise here.
    assert editor.doc.data["display"]["screen_width_mm"] == 0
    assert editor.doc.data["display"]["note"] == "keep"
    assert "画面幅" in editor.validation_label.text()
    assert not editor.save_cues()
    assert not editor.preview.image.isNull()
    editor.fields["screen_width_mm"].setValue(597)
    assert editor.doc.validate(duration=3) == []


def test_mixed_coordinate_edit_and_preview_drag_keep_formats(editor):
    editor.doc = CueDocument({"cues": [{"start": 0, "end": 1, "x": 80, "ny": 0.2,
                                        "future": {"condition": "a"}}]})
    editor.index = 0
    editor.refresh_document()
    assert editor.coords.currentIndex() == 2
    assert editor.fields["pos_x"].value() == 80
    assert editor.fields["pos_y"].value() == 0.2
    editor.fields["pos_x"].setValue(96)
    editor.set_position(0.25, 0.75)
    assert editor.doc.cues[0]["x"] == 80
    assert editor.doc.cues[0]["ny"] == 0.75
    assert "nx" not in editor.doc.cues[0] and "y" not in editor.doc.cues[0]
    assert editor.doc.cues[0]["future"] == {"condition": "a"}
    editor.preview.resize(640, 500)
    editor.preview.grab()  # Computes the letterboxed image target.
    target = editor.preview.target
    editor.preview._position(QPointF(target.left() + target.width() * 0.4,
                                    target.top() + target.height() * 0.6))
    assert editor.doc.position(0, 320, 240) == pytest.approx((0.4, 0.6))


def test_property_change_and_save_keep_extensions_and_degrees(editor, tmp_path):
    editor.doc = CueDocument({"metadata": {"group": "日本語"},
                             "display": {"screen_width_mm": 597, "distance_mm": 530, "extra": 7},
                             "cues": [{"start": 0, "end": 1, "nx": 0.3, "y": 90,
                                       "size_deg": 2, "thickness_deg": 0.2,
                                       "future": {"array": [1, 2]}}]})
    editor.index = 0
    editor.refresh_document()
    editor.pattern.setCurrentText("arrow")
    assert editor.fields["angle"].isEnabled()
    assert not editor.fields["text"].isEnabled()
    editor.fields["angle"].setValue(45)
    editor.fields["size"].setValue(3)
    editor.cues_path = tmp_path / "saved.json"
    assert editor.save_cues()
    saved = CueDocument.load(editor.cues_path).data
    assert saved == editor.doc.data
    assert saved["metadata"] == {"group": "日本語"}
    assert saved["display"]["extra"] == 7
    assert saved["cues"][0]["future"] == {"array": [1, 2]}
    assert saved["cues"][0]["size_deg"] == 3
    assert "size" not in saved["cues"][0]
    assert "y" in saved["cues"][0] and "ny" not in saved["cues"][0]
    editor.pattern.setCurrentText("text")
    assert editor.fields["text"].isEnabled()
    assert editor.fields["font"].isEnabled()
    assert not editor.fields["angle"].isEnabled()
    assert not editor.fields["thickness"].isEnabled()


def test_frame_step_seek_and_playback_clock(editor, monkeypatch):
    editor.seek_time(1)
    assert editor.frame_index == 60
    editor.step_frame(1)
    assert editor.frame_index == 61
    assert editor.current_time == pytest.approx(61 / 60)
    editor.step_frame(-1)
    assert editor.frame_index == 60
    assert editor.time_label.text().startswith("1.000")
    now = [100.0]
    monkeypatch.setattr(sacca_editor.time, "monotonic", lambda: now[0])
    editor.toggle_play()
    epoch = editor.play_epoch
    now[0] += 0.25
    editor._tick()
    assert editor.frame_index == 75
    assert editor.play_epoch == epoch
    now[0] += 0.25
    editor._tick()
    assert editor.frame_index == 90
    now[0] += 5
    editor._tick()
    assert editor.frame_index == editor.source.frame_count - 1
    assert not editor.timer.isActive()


def test_qt_timer_advances_preview_and_pause_stops_it(editor):
    editor.toggle_play()
    QTest.qWait(140)
    assert editor.frame_index > 0
    editor.pause()
    stopped = editor.frame_index
    QTest.qWait(40)
    assert editor.frame_index == stopped


def test_preview_modes_and_overlay_render_distinct_diagnostic_images(editor):
    editor.seek_time(1)
    editor.overlay.setChecked(False)
    editor.mode.setCurrentIndex(0)
    np.testing.assert_array_equal(image_pixels(editor.preview.image), editor.frame)
    editor.mode.setCurrentIndex(1)
    np.testing.assert_array_equal(image_pixels(editor.preview.image), (editor.frame.astype(np.int32) / 4).astype(np.uint8))
    editor.mode.setCurrentIndex(2)
    shifted = image_pixels(editor.preview.image)
    assert not np.array_equal(shifted, editor.frame)
    editor.slip.setValue(8)
    assert not np.array_equal(image_pixels(editor.preview.image), shifted)
    editor.mode.setCurrentIndex(0)
    editor.overlay.setChecked(True)
    assert not np.array_equal(image_pixels(editor.preview.image), editor.frame)
    assert editor.last_error is None


def test_player_command_only_copies_saved_arguments(editor, tmp_path, monkeypatch):
    path = tmp_path / "cues with space.json"
    editor.cues_path = path
    editor.dirty = True
    editor.delta.setValue(3)
    def forbidden_start(*args, **kwargs):
        raise AssertionError("Player command must not start a process")
    monkeypatch.setattr(sacca_editor.QProcess, "start", forbidden_start)
    editor.copy_player_command()
    command = QApplication.clipboard().text()
    args = shlex.split(command) if sys.platform != "win32" else None
    if args is not None:
        assert args == [sys.executable, str(ROOT / "sacca_player.py"), str(VIDEO), str(path),
                        "--refresh", "480", "--delta", "3"]
    assert path.exists()
    assert editor.process is None
    assert not editor.timer.isActive()


def test_smoke_uses_real_video_and_roundtrips(app, capsys):
    assert smoke_test(str(VIDEO), str(ROOT / "cues.example.json")) == 0
    assert "SMOKE OK" in capsys.readouterr().out


def test_smoke_renders_short_cue_at_exact_start(app, tmp_path, monkeypatch):
    path = tmp_path / "short.json"
    CueDocument({"cues": [{"start": 0.501, "end": 0.502, "nx": 0.5, "ny": 0.5,
                            "pattern": "circle", "size": 30, "thickness": 4}]}).save(path)
    original_render = sacca_editor.render_preview
    rendered = []

    def record_render(frame, spec, cue_time, **kwargs):
        result = original_render(frame, spec, cue_time, **kwargs)
        rendered.append((cue_time, not np.array_equal(frame, result)))
        return result

    monkeypatch.setattr(sacca_editor, "render_preview", record_render)
    assert smoke_test(str(VIDEO), str(path)) == 0
    # Neither adjacent 60fps frame timestamp lies in [0.501, 0.502).
    # Smoke must use the cue's exact timestamp for mask evaluation.
    assert rendered[-1] == (0.501, True)


def test_saved_json_loads_in_existing_player_without_presentation(editor, tmp_path, monkeypatch):
    from editor_preview import active_mask
    import sacca_player

    def forbidden_presentation(*args, **kwargs):
        raise AssertionError("Loading cues must not initialize presentation")

    monkeypatch.setattr(sacca_player.pygame, "init", forbidden_presentation)
    monkeypatch.setattr(sacca_player.pygame.display, "set_mode", forbidden_presentation)
    editor.doc.data["metadata"] = {"label": "日本語を保持"}
    editor.doc.set_display(597, 530)
    editor.doc.cues.append({"start": 2.6, "end": 2.9, "x": 80, "ny": 0.3,
                             "pattern": "rect", "size_deg": 3, "thickness_deg": 0.2,
                             "future": [1, 2]})
    editor.refresh_document()
    editor.cues_path = tmp_path / "player compatible.json"
    assert editor.save_cues()
    loaded = sacca_player.load_cues(str(editor.cues_path), 320, 240)
    assert len(loaded) == len(editor.doc.cues)
    for (start, end, player_mask), cue in zip(loaded, editor.doc.cues):
        assert (start, end) == (cue["start"], cue["end"])
        # These intervals do not overlap, so the GUI's active union is exactly
        # the player's individual saved mask, including mixed coords and deg.
        np.testing.assert_array_equal(player_mask,
                                      active_mask(editor.doc.data, start, 320, 240))
    assert CueDocument.load(editor.cues_path).data == editor.doc.data
    assert not sacca_player.pygame.get_init()


def test_invalid_loaded_values_stay_repairable(editor, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"cues": [{"start": "bad", "end": 2, "size": 0}]}))
    editor.open_cues(path)
    assert "開始時刻" in editor.validation_label.text()
    assert editor.last_error is not None
    editor.set_field("start", 0)
    editor.set_dimension("size", 70)
    assert editor.doc.validate(duration=3) == []
    assert editor.last_error is None
    assert editor.save_cues()


def wait_for_encode(window, seconds=20):
    deadline = time.monotonic() + seconds
    while window.process is not None and time.monotonic() < deadline:
        QTest.qWait(20)
    assert window.process is None, window.log.toPlainText()


def test_gui_encode_runs_existing_engine_and_streams_log(editor, tmp_path):
    editor.cues_path = tmp_path / "saved cues.json"
    assert editor.save_cues()
    output = tmp_path / "encoded output.mp4"
    editor.delta.setValue(2)
    editor.gain.setValue(0.75)
    editor.start_encode(str(output))
    assert editor.cancel_button.isEnabled()
    wait_for_encode(editor)
    assert output.stat().st_size > 0
    record = json.loads(Path(str(output) + ".applied.json").read_text())
    assert record["delta"] == 2 and record["gain"] == 0.75
    assert record["source_frames"] == 180 and record["out_frames"] == 1440
    assert "ENCODE OK" in editor.log.toPlainText()
    assert "エンコードが完了しました" in editor.log.toPlainText()
    assert not editor.cancel_button.isEnabled()


def test_gui_encode_cancel_preserves_existing_destination(editor, tmp_path):
    editor.cues_path = tmp_path / "saved.json"
    assert editor.save_cues()
    output = tmp_path / "preserved.mp4"
    output.write_bytes(b"original movie")
    record = Path(str(output) + ".applied.json")
    record.write_bytes(b"original sidecar")
    editor.start_encode(str(output), overwrite=True)
    assert editor.process.waitForStarted(2000)
    editor.cancel_encode()
    wait_for_encode(editor)
    assert output.read_bytes() == b"original movie"
    assert record.read_bytes() == b"original sidecar"
    assert "エンコードをキャンセルしました" in editor.log.toPlainText()
    assert not editor.cancel_button.isEnabled()
    assert not list(tmp_path.glob(".sacca-encode-*"))
