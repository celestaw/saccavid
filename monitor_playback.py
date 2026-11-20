"""Play an exported movie on the chosen desktop screen."""
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QLabel, QMessageBox, QPushButton, QDoubleSpinBox, QHBoxLayout, QVBoxLayout)
from saccavid import probe


class MonitorPlayback(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = str(path)
        info = probe(self.path)
        self.fps = info['fps']
        self.setWindowTitle('動画をモニターに表示')
        layout = QVBoxLayout(self)
        self.screens = QApplication.screens()
        self.choice = QComboBox()
        for screen in self.screens:
            self.choice.addItem(f'{screen.name()}  ·  {screen.refreshRate():g} Hz')
        layout.addWidget(self.choice)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.stop_job = QPushButton('準備・再生をキャンセル')
        self.stop_job.clicked.connect(lambda: self.worker.write(b'cancel\n'))
        self.stop_job.hide()
        layout.addWidget(self.stop_job)
        self.choice.currentIndexChanged.connect(self.describe)
        self.choice.setCurrentIndex(len(self.screens) - 1)
        self.describe()
        line = QHBoxLayout()
        self.sample_time = QDoubleSpinBox()
        self.sample_time.setRange(0, max(0, info['duration']-4/self.fps))
        self.sample_time.setDecimals(3)
        self.sample_time.setSuffix(' 秒')
        line.addWidget(self.sample_time)
        sample = QPushButton('指定時刻の4枚で同期確認')
        sample.clicked.connect(lambda: self.launch_worker('cycle_check.py', '--at', str(self.sample_time.value())))
        line.addWidget(sample)
        self.sample_button = sample
        layout.addLayout(line)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        play = buttons.addButton('通常再生（ffplay）', QDialogButtonBox.ButtonRole.ActionRole)
        play.clicked.connect(self.start)
        movie = buttons.addButton('同期再生（映像のみ）', QDialogButtonBox.ButtonRole.ActionRole)
        movie.clicked.connect(lambda: self.launch_worker('movie_sync.py'))
        rolling = buttons.addButton('GPU先読み再生（事前展開・映像のみ）', QDialogButtonBox.ButtonRole.ActionRole)
        rolling.clicked.connect(lambda: self.launch_worker('prepared_rolling.py'))
        self.check = buttons.addButton('先頭4枚で同期確認（3秒）', QDialogButtonBox.ButtonRole.ActionRole)
        self.check.clicked.connect(lambda: self.launch_worker('cycle_check.py'))
        moving = buttons.addButton('動画冒頭をGPU先読み（3秒）', QDialogButtonBox.ButtonRole.ActionRole)
        moving.clicked.connect(lambda: self.launch_worker('cycle_check.py', '--moving'))
        buttons.rejected.connect(self.reject)
        self.buttons = buttons
        layout.addWidget(buttons)

    def describe(self):
        hz = self.screens[self.choice.currentIndex()].refreshRate()
        message = f'元のMP4を再生します（{self.fps:g} fps）。Esc で終了。'
        if hz + 0.5 < self.fps:
            message += f' 現在は {hz:g} Hz のため、全フレームの等速提示はできません。'
        self.message.setText(message + "\nGPU先読み再生は初回に動画の隣へ .sacca-cache を作成します（素材により数GB以上）。")

    def launch_worker(self, script, *options):
        screen = self.screens[self.choice.currentIndex()]
        if screen not in QApplication.screens():
            QMessageBox.warning(self, '画面が切断されました', '画面選択を開き直してください。')
            return
        center = screen.geometry().center()
        self.worker = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONIOENCODING', 'utf-8')
        env.insert('PYGAME_HIDE_SUPPORT_PROMPT', '1')
        self.worker.setProcessEnvironment(env)
        self.worker.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.worker.finished.connect(self.check_finished)
        self.worker.errorOccurred.connect(self.check_error)
        self.buttons.setEnabled(False)
        self.sample_button.setEnabled(False)
        self.choice.setEnabled(False)
        self.summary = []
        self.output = b''
        if script == 'prepared_rolling.py':
            self.message.setText('初回は動画の隣に再生キャッシュ（数GB以上）を作成し、準備後に再生します。')
            self.worker.readyReadStandardOutput.connect(self.read_progress)
            self.stop_job.show()
        self.worker.start(sys.executable, [str(Path(__file__).with_name(script)),
                          self.path, str(center.x()-160), str(center.y()-90), *options])

    def read_progress(self):
        self.output += bytes(self.worker.readAllStandardOutput())
        lines = self.output.split(b'\n')
        self.output = lines.pop()
        for raw in lines:
            message = raw.decode('utf-8', 'replace').strip()
            if message.startswith('準備中'):
                self.message.setText(message)
            elif message:
                self.summary.append(message)

    def reject(self):
        if not self.buttons.isEnabled():
            if self.stop_job.isVisible():
                self.worker.write(b'cancel\n')
            return
        super().reject()

    def reset_controls(self):
        self.buttons.setEnabled(True)
        self.sample_button.setEnabled(True)
        self.choice.setEnabled(True)
        self.stop_job.hide()

    def check_error(self, error):
        self.reset_controls()
        self.message.setText(self.worker.errorString())

    def check_finished(self, code, status):
        self.reset_controls()
        self.read_progress()
        self.message.setText(('\n'.join(self.summary) + self.output.decode('utf-8', 'replace')).strip()
                             or f'確認終了（コード {code}）')

    def start(self):
        screen = self.screens[self.choice.currentIndex()]
        if screen not in QApplication.screens():
            QMessageBox.warning(self, '画面が切断されました', '画面選択を開き直してください。')
            return
        rect = screen.geometry()
        program = shutil.which('ffplay')
        args = ['-autoexit', '-noborder', '-left', str(rect.x()), '-top', str(rect.y()),
                '-x', str(rect.width()), '-y', str(rect.height()), '-i', self.path]
        if not program or not QProcess.startDetached(program, args)[0]:
            QMessageBox.warning(self, '再生できません', 'ffplay を起動できませんでした。')
            return
        self.accept()
