"""Play an exported movie on the chosen desktop screen."""
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QLabel, QMessageBox, QVBoxLayout)
from saccavid import probe


class MonitorPlayback(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.fps = probe(self.path)['fps']
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
        self.choice.currentIndexChanged.connect(self.describe)
        self.choice.setCurrentIndex(len(self.screens) - 1)
        self.describe()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        play = buttons.addButton('通常再生（ffplay）', QDialogButtonBox.ButtonRole.ActionRole)
        play.clicked.connect(self.start)
        movie = buttons.addButton('同期再生（映像のみ）', QDialogButtonBox.ButtonRole.ActionRole)
        movie.clicked.connect(lambda: self.launch_worker('movie_sync.py'))
        self.check = buttons.addButton('先頭4枚で同期確認（3秒）', QDialogButtonBox.ButtonRole.ActionRole)
        self.check.clicked.connect(lambda: self.launch_worker('cycle_check.py'))
        moving = buttons.addButton('動画冒頭をGPU先読み（3秒）', QDialogButtonBox.ButtonRole.ActionRole)
        moving.clicked.connect(lambda: self.launch_worker('cycle_check.py', '--moving'))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def describe(self):
        hz = self.screens[self.choice.currentIndex()].refreshRate()
        message = f'元のMP4を再生します（{self.fps:g} fps）。Esc で終了。'
        if hz + 0.5 < self.fps:
            message += f' 現在は {hz:g} Hz のため、全フレームの等速提示はできません。'
        self.message.setText(message)

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
        self.setEnabled(False)
        self.worker.start(sys.executable, [str(Path(__file__).with_name(script)),
                          self.path, str(center.x()-160), str(center.y()-90), *options])

    def check_error(self, error):
        self.setEnabled(True)
        self.message.setText(self.worker.errorString())

    def check_finished(self, code, status):
        self.setEnabled(True)
        self.message.setText(bytes(self.worker.readAllStandardOutput()).decode('utf-8', 'replace').strip()
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
