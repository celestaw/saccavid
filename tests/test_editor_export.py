"""Export publication and cancellation must not damage existing user files."""

import json
from pathlib import Path
import sys
import threading

import pytest

import editor_export
import saccavid
from editor_export import run_export


def test_success_publishes_video_and_record(tmp_path, monkeypatch):
    def encode(source, cues, destination, delta, gain):
        Path(destination).write_bytes(b"encoded movie")
        Path(destination + ".applied.json").write_text(json.dumps({
            "source": source, "delta": delta, "gain": gain,
        }))

    monkeypatch.setattr(saccavid, "encode", encode)
    output = tmp_path / "movie.mp4"
    assert run_export(tmp_path / "source.mp4", tmp_path / "cues.json",
                      output, delta=2, gain=1.5) == 0
    assert output.read_bytes() == b"encoded movie"
    record = json.loads(Path(str(output) + ".applied.json").read_text())
    assert record["delta"] == 2 and record["gain"] == 1.5
    assert not list(tmp_path.glob(".sacca-encode-*"))


def test_failed_encode_preserves_old_output(tmp_path, monkeypatch):
    def encode(source, cues, destination, **kwargs):
        Path(destination).write_bytes(b"partial")
        raise IOError("decoder failed")

    monkeypatch.setattr(saccavid, "encode", encode)
    output = tmp_path / "movie.mp4"
    output.write_bytes(b"old movie")
    sidecar = Path(str(output) + ".applied.json")
    sidecar.write_text("old record")
    with pytest.raises(IOError, match="decoder failed"):
        run_export(tmp_path / "source.mp4", tmp_path / "cues.json",
                   output, overwrite=True)
    assert output.read_bytes() == b"old movie"
    assert sidecar.read_text() == "old record"
    assert not list(tmp_path.glob(".sacca-encode-*"))


def test_cancel_reaps_children_and_removes_partial_output(tmp_path, monkeypatch):
    ready = threading.Event()
    cancel = threading.Event()
    children = []

    def encode(source, cues, destination, **kwargs):
        Path(destination).write_bytes(b"partial")
        child = saccavid.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        children.append(child)
        ready.set()
        child.wait()

    def request_cancel():
        if ready.wait(5):
            cancel.set()

    monkeypatch.setattr(saccavid, "encode", encode)
    requester = threading.Thread(target=request_cancel)
    requester.start()
    output = tmp_path / "movie.mp4"
    assert run_export(tmp_path / "source.mp4", tmp_path / "cues.json",
                      output, cancel_event=cancel) == 130
    requester.join(timeout=5)
    assert children and all(child.poll() is not None for child in children)
    assert not output.exists()
    assert not Path(str(output) + ".applied.json").exists()
    assert not list(tmp_path.glob(".sacca-encode-*"))


def test_refuses_existing_output_without_overwrite(tmp_path):
    output = tmp_path / "movie.mp4"
    output.write_bytes(b"old movie")
    with pytest.raises(FileExistsError):
        run_export(tmp_path / "source.mp4", tmp_path / "cues.json", output)
    assert output.read_bytes() == b"old movie"


def test_refuses_overwriting_source_even_with_overwrite(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    with pytest.raises(ValueError, match="別"):
        run_export(source, tmp_path / "cues.json", source, overwrite=True)
    assert source.read_bytes() == b"source"


@pytest.mark.parametrize("existing", [False, True])
def test_sidecar_publish_failure_rolls_back_both_files(tmp_path, monkeypatch, existing):
    def encode(source, cues, destination, **kwargs):
        Path(destination).write_bytes(b"new movie")
        Path(destination + ".applied.json").write_text('{"new": true}')

    output = tmp_path / "movie.mp4"
    sidecar = Path(str(output) + ".applied.json")
    if existing:
        output.write_bytes(b"old movie")
        sidecar.write_text("old record")
    replace = editor_export.os.replace

    def fail_sidecar_publish(source, destination):
        if Path(source).name.startswith(".sacca-encode-") and Path(destination) == sidecar:
            raise PermissionError("simulated sidecar publication failure")
        replace(source, destination)

    monkeypatch.setattr(saccavid, "encode", encode)
    monkeypatch.setattr(editor_export.os, "replace", fail_sidecar_publish)
    with pytest.raises(PermissionError, match="publication failure"):
        run_export(tmp_path / "source.mp4", tmp_path / "cues.json",
                   output, overwrite=existing)
    if existing:
        assert output.read_bytes() == b"old movie"
        assert sidecar.read_text() == "old record"
    else:
        assert not output.exists() and not sidecar.exists()
    assert not list(tmp_path.glob(".sacca-*"))


def test_failed_rollback_keeps_recoverable_backup(tmp_path, monkeypatch):
    def encode(source, cues, destination, **kwargs):
        Path(destination).write_bytes(b"new movie")
        Path(destination + ".applied.json").write_text('{"new": true}')

    output = tmp_path / "movie.mp4"
    sidecar = Path(str(output) + ".applied.json")
    output.write_bytes(b"old movie")
    sidecar.write_text("old record")
    replace = editor_export.os.replace

    def fail_sidecar_destination(source, destination):
        if Path(destination) == sidecar:
            raise PermissionError("simulated persistent filesystem failure")
        replace(source, destination)

    monkeypatch.setattr(saccavid, "encode", encode)
    monkeypatch.setattr(editor_export.os, "replace", fail_sidecar_destination)
    with pytest.raises(IOError, match="バックアップ"):
        run_export(tmp_path / "source.mp4", tmp_path / "cues.json",
                   output, overwrite=True)
    assert output.read_bytes() == b"old movie"
    backups = list(tmp_path.glob(".sacca-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "old record"
    assert not list(tmp_path.glob(".sacca-encode-*"))


@pytest.mark.parametrize("audio_seconds", [0.15, 1.5])
def test_audio_length_does_not_truncate_or_extend_gui_movie(tmp_path, audio_seconds):
    import subprocess
    source = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=size=64x48:rate=30:duration=1", "-f", "lavfi", "-i",
                    f"sine=duration={audio_seconds}", "-c:v", "libx264",
                    "-c:a", "aac", str(source)], check=True)
    cues = tmp_path / "cues.json"
    cues.write_text('{"cues":[]}')
    output = tmp_path / "output.mp4"
    assert run_export(source, cues, output) == 0
    info = saccavid.probe(str(output))
    assert info["nb_frames"] == 480 and info["fps"] == 480
    assert abs(info["duration"] - 1) < 0.05
