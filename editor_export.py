"""Portable, cancellable supervisor around the unchanged saccavid encoder.

The GUI sends ``cancel\n`` to stdin. All encoder children, including ffprobe,
are tracked so cancellation cannot leave an ffmpeg process behind. Generated
files stay under temporary names until encoding has completed successfully.
"""

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import saccavid


class ExportCancelled(Exception):
    pass


class TrackedProcesses:
    """Subprocess proxy scoped to the encoder module, never a global patch."""

    def __init__(self):
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._children = []
        self.video_seconds = None
        self.encoder = None

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, *args, **kwargs):
        with self._lock:
            if self.cancelled.is_set():
                raise ExportCancelled("エンコードをキャンセルしました")
            cmd = args[0] if args else []
            monitor = (isinstance(cmd, list) and cmd[:1] == ["ffmpeg"]
                       and kwargs.get("stdin") == subprocess.PIPE)
            if monitor:
                cmd = list(cmd)
                if "-shortest" in cmd and self.video_seconds is not None:
                    index = cmd.index("-shortest")
                    cmd[index:index + 1] = ["-t", f"{self.video_seconds:.9f}"]
                args = ([cmd[0], "-stats_period", "0.2", "-progress", "pipe:1", *cmd[1:]], *args[1:])
                kwargs["stdout"] = subprocess.PIPE
            process = subprocess.Popen(*args, **kwargs)
            if monitor:
                self.encoder = process
                threading.Thread(target=self._read_progress, args=(process,), daemon=True).start()
            self._children.append(process)
            return process

    @staticmethod
    def _read_progress(process):
        with process.stdout as stream:
            for line in stream:
                key, _, value = line.strip().partition(b"=")
                if key == b"frame" and value.strip().isdigit():
                    print(f"SACCA_FRAME {int(value)}", flush=True)

    def run(self, *args, input=None, capture_output=False, timeout=None,
            check=False, **kwargs):
        # Equivalent to subprocess.run for the public options used by probe().
        if input is not None:
            if "stdin" in kwargs:
                raise ValueError("stdin and input arguments may not both be used")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if "stdout" in kwargs or "stderr" in kwargs:
                raise ValueError("capture_output conflicts with stdout/stderr")
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process = self.Popen(*args, **kwargs)
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode,
                                             stdout, stderr)
        if check:
            result.check_returncode()
        return result

    def stop(self):
        """Stop and reap every tracked direct child, on Windows and POSIX."""
        self.cancelled.set()
        with self._lock:
            children = list(self._children)
        for process in children:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + 2.0
        for process in children:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()


def _publish_pair(movie, target, record, sidecar):
    """Publish both outputs, restoring the previous pair after an I/O error.

    Each rename is atomic, but a two-file transaction cannot be atomic across
    sudden process/system termination. Backups are retained and reported when
    the filesystem also prevents rollback, rather than deleting recoverable data.
    """
    backups = {}
    published = []
    try:
        for destination in (target, sidecar):
            if destination.exists():
                if not destination.is_file():
                    raise ValueError("出力先には通常のファイルを指定してください")
                fd, backup_name = tempfile.mkstemp(prefix=".sacca-backup-",
                                                    suffix=".bak", dir=destination.parent)
                os.close(fd)
                backup = Path(backup_name)
                try:
                    os.replace(destination, backup)
                except BaseException:
                    backup.unlink()
                    raise
                backups[destination] = backup
        for source, destination in ((movie, target), (record, sidecar)):
            os.replace(source, destination)
            published.append(destination)
    except BaseException as original:
        restore_errors = []
        for destination in reversed(published):
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                restore_errors.append(str(exc))
        for destination, backup in reversed(list(backups.items())):
            try:
                os.replace(backup, destination)
            except OSError as exc:
                restore_errors.append(f"{backup} → {destination}: {exc}")
        if restore_errors:
            raise IOError("出力の復旧が完了しませんでした。残ったバックアップを保持しています: "
                          + "; ".join(restore_errors)) from original
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink()
            except OSError as exc:
                print(f"書き出しは成功しましたがバックアップを削除できません: {backup}: {exc}",
                      file=sys.stderr, flush=True)


def run_export(inp, cues, output, delta=1, gain=1.0, overwrite=False,
               cancel_event=None):
    """Encode a file and return 0, or 130 after a cancellation.

    Encode failures raise their original exception. The existing destination
    remains untouched during a failed or cancelled encode.
    """
    source = Path(inp).resolve()
    cue_path = Path(cues).resolve()
    target = Path(output).resolve()
    sidecar = Path(str(target) + ".applied.json")
    if target in (source, cue_path) or sidecar in (source, cue_path):
        raise ValueError("出力先は入力動画・cue JSON と別の場所にしてください")
    if not overwrite and (target.exists() or sidecar.exists()):
        raise FileExistsError("出力先が存在します。別名を選ぶか上書きを明示してください")
    if not target.parent.is_dir():
        raise ValueError("出力先のフォルダが存在しません")
    if target.suffix.lower() not in (".mp4", ".mkv", ".mov"):
        raise ValueError("出力拡張子は .mp4 / .mkv / .mov を指定してください")
    cancel_event = cancel_event or threading.Event()
    tracker = TrackedProcesses()
    fd, temporary_name = tempfile.mkstemp(prefix=".sacca-encode-",
                                          suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    temporary_sidecar = Path(temporary_name + ".applied.json")
    original_subprocess = saccavid.subprocess
    done = threading.Event()
    failures = []

    original_probe = saccavid.probe

    def probe_source(path):
        info = original_probe(path)
        fps = info["fps"]
        frames = info["nb_frames"] or round(info["duration"] * fps)
        # Match the encoder's complete four-subframe cycles at 480 fps.
        tracker.video_seconds = math.ceil(frames / fps * 120 - 1e-9) / 120
        return info

    def encode():
        try:
            saccavid.encode(str(source), str(cue_path), str(temporary),
                            delta=delta, gain=gain)
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    saccavid.probe = probe_source
    saccavid.subprocess = tracker
    worker = threading.Thread(target=encode, daemon=True)
    try:
        print("ENCODE START: " + str(target), flush=True)
        worker.start()
        while not done.wait(0.05):
            if cancel_event.is_set():
                tracker.stop()
                worker.join(timeout=3.0)
                return 130
        if cancel_event.is_set():
            return 130
        if failures:
            if isinstance(failures[0], BrokenPipeError) and tracker.encoder is not None:
                code = tracker.encoder.poll()
                if code == 0:
                    detail = "映像の受け取りが予定より早く終了しました"
                elif code is not None and code < 0:
                    detail = f"OSのシグナル {-code} で停止しました"
                else:
                    detail = f"終了コード {code} で停止しました"
                raise IOError(f"書き出し側のFFmpegが{detail}。直前のログを確認してください。") from failures[0]
            raise failures[0]
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise IOError("エンコーダが動画を生成しませんでした")
        # Read before publishing: an incomplete/malformed applied record is a
        # failed export, even when the encoded movie itself is nonempty.
        with temporary_sidecar.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if tracker.encoder is not None:
            actual = original_probe(str(temporary))["nb_frames"]
            expected = record["out_frames"]
            if actual is not None and actual != expected:
                raise IOError(f"出力映像のフレーム数が不足または超過しています（実際 {actual}、予定 {expected}）。")
        if cancel_event.is_set():
            return 130
        if not overwrite and (target.exists() or sidecar.exists()):
            raise FileExistsError("書き出し中に同名ファイルが作成されました")
        _publish_pair(temporary, target, temporary_sidecar, sidecar)
        print("ENCODE OK: " + str(target), flush=True)
        return 0
    finally:
        tracker.stop()
        if worker.ident is not None:
            worker.join(timeout=3.0)
        saccavid.probe = original_probe
        saccavid.subprocess = original_subprocess
        for path in (temporary, temporary_sidecar):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("cues")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--delta", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    cancel = threading.Event()

    def listen():
        for line in sys.stdin:
            if line.strip().lower() == "cancel":
                cancel.set()
                return

    threading.Thread(target=listen, daemon=True).start()
    try:
        result = run_export(args.input, args.cues, args.output, args.delta,
                            args.gain, args.overwrite, cancel)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("エンコードを中断しました", file=sys.stderr, flush=True)
            return 130
        print(f"エンコードに失敗しました: {exc}", file=sys.stderr, flush=True)
        return 1
    if result == 130:
        print("エンコードをキャンセルしました（出力先は変更していません）", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
