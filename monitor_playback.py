"""Play an exported movie on the chosen desktop screen."""
import shutil

from PySide6.QtCore import QProcess
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
        play = buttons.addButton('この画面で再生', QDialogButtonBox.ButtonRole.ActionRole)
        play.clicked.connect(self.start)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def describe(self):
        hz = self.screens[self.choice.currentIndex()].refreshRate()
        message = f'元のMP4を再生します（{self.fps:g} fps）。Esc で終了。'
        if hz + 0.5 < self.fps:
            message += f' 現在は {hz:g} Hz のため、全フレームの等速提示はできません。'
        self.message.setText(message)

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
