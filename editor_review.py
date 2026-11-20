"""Review actual encoded RGB subframes one complete cycle at a time."""
import math
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QLabel, QVBoxLayout, QSlider,
                               QPushButton, QSpinBox, QHBoxLayout)

from editor_preview import VideoSource
from saccavid import _shift_zero


class ExportReview(QDialog):
    def __init__(self, path, parent=None, slip=3):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.source = VideoSource(path)
        if not math.isclose(self.source.info['fps'], 480) or self.source.frame_count % 4:
            self.source.close()
            raise ValueError('saccavid で書き出した480 fps動画を指定してください。')
        self.setWindowTitle('書き出し確認 • ' + self.source.path.split('/')[-1])
        layout = QVBoxLayout(self)
        self.mode = QComboBox()
        self.mode.addItems(['実フレーム（1枚ずつ）', 'サッカードシミュレーション'])
        layout.addWidget(self.mode)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.picture)
        row = QHBoxLayout()
        self.play = QPushButton('再生')
        self.clock = QLabel()
        self.slip = QSpinBox()
        self.slip.setRange(1, 10)
        self.slip.setValue(slip)
        self.frame_number = QSpinBox()
        self.frame_number.setRange(0, self.source.frame_count - 1)
        self.frame_number.setPrefix('フレーム #')
        row.addWidget(self.frame_number)
        for label, offset in [('◀ 1枚', -1), ('1枚 ▶', 1)]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, step=offset: self.step_frame(step))
            row.addWidget(button)
        row.addWidget(self.play)
        row.addWidget(self.clock)
        row.addStretch()
        row.addWidget(QLabel('ずらし量 [px]'))
        row.addWidget(self.slip)
        layout.addLayout(row)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.source.frame_count - 1)
        layout.addWidget(self.slider)
        self.timer = QTimer(self)
        self.timer.setInterval(17)
        self.timer.timeout.connect(self.advance)
        self.play.clicked.connect(self.toggle)
        self.slider.valueChanged.connect(self.draw)
        self.slip.valueChanged.connect(self.draw)
        self.frame_number.valueChanged.connect(self.slider.setValue)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.change_mode()

    def step_frame(self, offset):
        self.mode.setCurrentIndex(0)
        self.slider.setValue(self.slider.value() + offset)

    def change_mode(self):
        self.timer.stop()
        self.play.setText('再生')
        diagnostic = self.mode.currentIndex() == 1
        self.play.setEnabled(diagnostic)
        self.slip.setEnabled(diagnostic)
        self.draw()

    def toggle(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play.setText('再生')
        else:
            if self.slider.value() == self.slider.maximum():
                self.slider.setValue(0)
            self.timer.start()
            self.play.setText('一時停止')

    def advance(self):
        if self.slider.value() == self.slider.maximum():
            self.toggle()
        else:
            self.slider.setValue(min(self.slider.maximum(), self.slider.value() + 8))

    def draw(self):
        index = self.slider.value()
        self.frame_number.setValue(index)
        try:
            if self.mode.currentIndex() == 0:
                rgb = self.source.read(index)
            else:
                start = (index // 4) * 4
                frames = [_shift_zero(self.source.read(start + k), -k * self.slip.value()) for k in range(4)]
                rgb = np.clip(np.sum(frames, axis=0, dtype=np.uint16), 0, 255).astype(np.uint8)
            h, w = rgb.shape[:2]
            image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888)
            self.picture.setPixmap(QPixmap.fromImage(image).scaled(720, 450, Qt.AspectRatioMode.KeepAspectRatio,
                                                                   Qt.TransformationMode.SmoothTransformation))
            self.clock.setText(f'{index / 480:.6f} 秒  ·  周期内 {index % 4 + 1}/4')
        except Exception as exc:
            self.timer.stop()
            self.play.setText('再生')
            self.picture.setText(f'フレームの読み込みに失敗しました。{exc}')

    def done(self, result):
        self.timer.stop()
        self.source.close()
        super().done(result)
