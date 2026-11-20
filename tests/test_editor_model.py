"""Cue compatibility and editing invariants; no Qt or video device required."""

from copy import deepcopy
import json
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from editor_model import CueDocument, ValidationError, snap_time


ROOT = Path(__file__).resolve().parents[1]


def document(**cue_fields):
    cue = {"start": 0.5, "end": 2.0, "nx": 0.5, "ny": 0.5, "size": 70}
    cue.update(cue_fields)
    return CueDocument({"cues": [cue]})


def test_example_roundtrip_exact_content(tmp_path):
    original = json.loads((ROOT / "cues.example.json").read_text())
    doc = CueDocument.load(ROOT / "cues.example.json")
    path = doc.save(tmp_path / "cues.json", duration=3)
    assert CueDocument.load(path).data == original


def test_unknown_fields_and_coordinate_forms_survive_roundtrip(tmp_path):
    data = {"experiment": {"notes": ["日本語", {"version": 2}]},
            "display": {"screen_width_mm": 597, "distance_mm": 530, "future": [1, 2]},
            "cues": [{"start": 0, "end": 1, "x": 10, "ny": 0.4,
                      "size_deg": 2.5, "thickness_deg": 0.3, "custom": {"group": "a"}},
                     {"start": 1, "end": 2, "nx": 0.3, "y": 17, "unknown": None}]}
    doc = CueDocument(data)
    doc.save(tmp_path / "cues.json", duration=3)
    assert CueDocument.load(tmp_path / "cues.json").data == data
    assert json.loads((tmp_path / "cues.json").read_bytes().decode("ascii")) == data
    assert doc.data == data


def test_defaults_and_duplicate_are_independent():
    doc = CueDocument()
    index = doc.add_cue(2)
    assert index == 0
    assert doc.cues[0] == {"start": 2, "end": 3.5, "nx": 0.5, "ny": 0.5,
                           "pattern": "oval", "size": 70, "thickness": 6}
    doc.cues[0]["nested"] = {"labels": [1]}
    duplicate = doc.duplicate(0)
    doc.cues[duplicate]["nested"]["labels"].append(2)
    assert doc.cues[0]["nested"] == {"labels": [1]}
    doc.delete(0)
    assert len(doc.cues) == 1


@pytest.mark.parametrize("cue,field", [
    ({"start": -1}, "start"), ({"start": "bad"}, "start"),
    ({"start": None}, "start"), ({"start": True}, "start"),
    ({"end": 0.5}, "end"), ({"end": 0.1}, "end"),
    ({"end": float("inf")}, "end"), ({"end": float("nan")}, "end"),
    ({"size": 0}, "size"), ({"size": -4}, "size"),
    ({"size": "bad"}, "size"), ({"size_deg": 0}, "size_deg"),
    ({"thickness": 0}, "thickness"), ({"thickness_deg": -1}, "thickness_deg"),
    ({"nx": 1.1}, "nx"), ({"ny": -0.1}, "ny"),
    ({"x": "bad"}, "x"), ({"angle": "bad"}, "angle"),
    ({"pattern": "other"}, "pattern"), ({"font": []}, "font"),
])
def test_invalid_cue_reports_index_and_field(cue, field):
    doc = document(**cue)
    assert any(issue.index == 0 and issue.field == field for issue in doc.validate())


def test_invalid_save_preserves_previous_file(tmp_path):
    path = tmp_path / "cues.json"
    path.write_text("original", encoding="utf-8")
    doc = document(end=4)
    with pytest.raises(ValidationError) as error:
        doc.save(path, duration=3)
    assert error.value.issues[0].index == 0
    assert "動画長" in str(error.value)
    assert path.read_text() == "original"


@pytest.mark.parametrize("data", [None, [], {}, {"cues": {}}, {"cues": [None]}])
def test_malformed_file_is_rejected_in_japanese(tmp_path, data):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    # None is the in-memory default, but JSON null is not a cue document.
    if data is None:
        with pytest.raises(ValueError):
            CueDocument.load(path)
    else:
        with pytest.raises(ValueError):
            CueDocument.load(path)


def test_semantically_invalid_document_can_load_for_repair(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"cues": [{"start": 3, "end": 1}]}')
    doc = CueDocument.load(path)
    assert doc.validate()[0].index == 0
    doc.cues[0]["end"] = 4
    assert doc.validate() == []


def test_bom_and_numeric_strings_are_supported(tmp_path):
    path = tmp_path / "bom.json"
    path.write_text('{"cues": [{"start": "0", "end": "1", "size": "70"}]}', encoding="utf-8-sig")
    assert CueDocument.load(path).validate() == []


def test_move_snap_bounds_and_duration_preservation():
    doc = document()
    assert doc.move(0, 0.526, snap=True) == pytest.approx((0.55, 2.05))
    assert doc.move(0, -5) == pytest.approx((0, 1.5))
    assert doc.move(0, 9, duration=3) == pytest.approx((1.5, 3))
    assert doc.move(0, 0.513, snap=False) == pytest.approx((0.513, 2.013))
    before = deepcopy(doc.data)
    with pytest.raises(ValueError):
        doc.move(0, 0, duration=1)
    assert doc.data == before


def test_resize_preserves_other_endpoint_and_prevents_crossing():
    doc = document()
    assert doc.resize(0, "start", 0.776, snap=True) == pytest.approx((0.8, 2))
    assert doc.resize(0, "end", 2.226, snap=True) == pytest.approx((0.8, 2.25))
    start, end = doc.resize(0, "start", 9)
    assert 0 <= start < end == 2.25
    start, end = doc.resize(0, "end", -1)
    assert end > start
    assert doc.resize(0, "end", 10, duration=3)[1] == 3


def test_half_open_active_intervals():
    doc = document()
    assert doc.active_cues(0.499) == []
    assert doc.active_cues(0.5) == doc.cues
    assert doc.active_cues(1.999) == doc.cues
    assert doc.active_cues(2) == []


def test_coordinate_edit_preserves_mixed_formats_and_shadowed_values():
    doc = CueDocument({"cues": [{"start": 0, "end": 1, "x": 10, "ny": 0.2,
                                 "y": 99, "size_deg": 1, "custom": "keep"}]})
    assert doc.position(0, 100, 200) == (0.1, 0.2)
    doc.set_position(0, 0.3, 0.4, 100, 200)
    cue = doc.cues[0]
    assert cue["x"] == 30 and cue["ny"] == 0.4
    assert "nx" not in cue and cue["y"] == 99
    assert cue["custom"] == "keep" and cue["size_deg"] == 1


def test_coordinate_conversion_is_explicit_and_reversible():
    doc = document(x=9, y=8)
    doc.set_coordinate_mode(0, False, 320, 240)
    assert "nx" not in doc.cues[0] and "ny" not in doc.cues[0]
    assert doc.cues[0]["x"] == 160 and doc.cues[0]["y"] == 120
    doc.set_coordinate_mode(0, True, 320, 240)
    assert "x" not in doc.cues[0] and "y" not in doc.cues[0]
    assert doc.position(0, 1920, 1080) == (0.5, 0.5)


def test_null_normalized_coordinate_uses_pixel_fallback():
    doc = document(nx=None, x=80)
    assert doc.position(0, 320, 240) == (0.25, 0.5)
    doc.set_position(0, 0.5, 0.6, 320, 240)
    assert doc.cues[0]["nx"] is None and doc.cues[0]["x"] == 160
    assert doc.validate() == []


def test_display_units_validation_and_unknown_fields_preserved():
    doc = document(size_deg=2, thickness_deg=0.3)
    assert {issue.field for issue in doc.validate()} == {"size_deg", "thickness_deg"}
    assert doc.px_per_degree(320) is None
    doc.data["display"] = {"note": "keep"}
    doc.set_display(597, 530)
    assert doc.data["display"]["note"] == "keep"
    assert doc.validate() == []
    assert doc.px_per_degree(320) == pytest.approx(2 * 530 * math.tan(math.radians(0.5)) / (597 / 320))


def test_invalid_display_and_unknown_nonfinite_are_rejected():
    doc = document()
    doc.data["display"] = {"screen_width_mm": 0, "distance_mm": "bad"}
    assert len(doc.validate()) == 2
    doc.data.pop("display")
    doc.data["future"] = {"number": float("nan")}
    assert doc.validate()[0].field == "json"


def test_snap_precision():
    assert snap_time(0.525) == 0.55
    assert snap_time(0.511) == 0.5
    assert snap_time(0.511, enabled=False) == 0.511


def test_constructor_copies_input():
    data = {"cues": [{"start": 0, "end": 1, "extension": []}]}
    doc = CueDocument(data)
    doc.cues[0]["extension"].append(1)
    assert data["cues"][0]["extension"] == []
