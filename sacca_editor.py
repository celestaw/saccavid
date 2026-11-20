#!/usr/bin/env python3
"""Normal-fps cue authoring; diagnostic previews are not physical presentation."""
import argparse
import math
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, Signal, QProcess, QProcessEnvironment
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QFormLayout, QSplitter, QComboBox, QDoubleSpinBox, QSpinBox,
    QLineEdit, QCheckBox, QSlider, QScrollArea, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QPlainTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QGroupBox,
)

from monitor_playback import MonitorPlayback
from editor_review import ExportReview
from editor_model import CueDocument
from editor_preview import VideoSource, render_preview
from editor_timeline import TimelineWidget

ROOT = Path(__file__).resolve().parent
PATTERNS = ('oval', 'radial', 'circle', 'rect', 'cross', 'arrow', 'text')


def number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def command_text(args):
    """Quote every Windows argument for PowerShell; POSIX shells elsewhere."""
    if sys.platform == 'win32':
        return '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in args)
    return shlex.join(args)


class PreviewWidget(QWidget):
    positionChanged = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(360, 230)
        self.image = QImage()
        self.marker = None
        self.target = QRectF()
        self.dragging = False

    def set_frame(self, rgb, marker=None):
        self.image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                            QImage.Format.Format_RGB888).copy()
        self.marker = marker
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#131820'))
        if self.image.isNull():
            painter.setPen(QColor('#c3cedc'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '動画を開いてキューを配置')
            return
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        self.target = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        painter.drawImage(self.target, self.image)
        if self.marker is not None:
            x = self.target.left() + self.marker[0] * w
            y = self.target.top() + self.marker[1] * h
            painter.setPen(QPen(QColor('#fff3a1'), 2))
            painter.drawEllipse(QPointF(x, y), 9, 9)
            painter.drawLine(QPointF(x - 15, y), QPointF(x + 15, y))
            painter.drawLine(QPointF(x, y - 15), QPointF(x, y + 15))

    def _position(self, pos):
        if self.target.isEmpty() or self.marker is None:
            return
        nx = min(1., max(0., (pos.x() - self.target.left()) / self.target.width()))
        ny = min(1., max(0., (pos.y() - self.target.top()) / self.target.height()))
        self.positionChanged.emit(nx, ny)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.target.contains(event.position()):
            self.dragging = True
            self._position(event.position())

    def mouseMoveEvent(self, event):
        if self.dragging:
            self._position(event.position())

    def mouseReleaseEvent(self, event):
        self.dragging = False


class EditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.doc = CueDocument()
        self.source = None
        self.video_path = None
        self.cues_path = None
        self.index = -1
        self.frame_index = 0
        self.current_time = 0.0
        self.frame = None
        self.dirty = False
        self.loading = False
        self.process = None
        self.cancelled = False
        self.last_error = None
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._tick)
        self.resize(1200, 900)
        self._build_ui()
        self.refresh_document()

    def _build_ui(self):
        toolbar = self.addToolBar('ファイルと書き出し')
        for title, fn, shortcut in [
            ('動画を開く', self.choose_video, 'Ctrl+O'),
            ('cues 読込', self.choose_cues, 'Ctrl+L'),
            ('cues 保存', self.save_cues, 'Ctrl+S'),
            ('名前を付けて保存', lambda: self.save_cues(True), 'Ctrl+Shift+S'),
            ('書き出して確認', self.encode, None),
            ('実画面で再生', self.choose_monitor_playback, None),
            ('書き出した動画を開く', self.choose_review, None),
            ('プレイヤーコマンド', self.copy_player_command, None),
        ]:
            action = QAction(title, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, f=fn: f())
            toolbar.addAction(action)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.info_label = QLabel('動画未選択 • 通常fpsの編集プレビュー')
        layout.addWidget(self.info_label)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.preview = PreviewWidget()
        self.preview.positionChanged.connect(self.set_position)
        split.addWidget(self.preview)
        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        props_scroll.setMinimumWidth(360)
        props_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        props = QWidget()
        props_layout = QVBoxLayout(props)
        group = QGroupBox('選択中の cue')
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fields = {}
        for key, title in [('start', '開始 [秒]'), ('end', '終了 [秒]')]:
            self._spin_field(form, key, title, -1e6, 1e6, .05)
        self.pattern = QComboBox()
        self.pattern.addItems(PATTERNS)
        self.pattern.currentTextChanged.connect(lambda text: self.set_field('pattern', text))
        form.addRow('パターン', self.pattern)
        self.coords = QComboBox()
        self.coords.addItems(['比率 nx / ny', 'ピクセル x / y', '混在（読込値を保持）'])
        self.coords.currentIndexChanged.connect(self.change_coordinates)
        form.addRow('座標形式', self.coords)
        self._spin_field(form, 'pos_x', '横位置', -1e6, 1e6, .01)
        self._spin_field(form, 'pos_y', '縦位置', -1e6, 1e6, .01)
        for key, title, default in [('size', 'サイズ', 70), ('thickness', '線幅', 6)]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            spin = QDoubleSpinBox()
            spin.setDecimals(4)
            spin.setRange(-1e6, 1e6)
            spin.setValue(default)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda v, k=key: self.set_dimension(k, v))
            unit = QComboBox()
            unit.addItems(['px', 'deg'])
            unit.currentIndexChanged.connect(lambda i, k=key: self.change_unit(k, i))
            self.fields[key] = spin
            self.fields[key + '_unit'] = unit
            row_layout.addWidget(spin)
            row_layout.addWidget(unit)
            form.addRow(title, row)
        self._spin_field(form, 'angle', '矢印の角度 [度]', -36000, 36000, 1)
        for key, title in [('text', '文字'), ('font', 'フォントファイル')]:
            entry = QLineEdit()
            entry.editingFinished.connect(lambda k=key, w=entry: self.set_field(k, w.text()))
            self.fields[key] = entry
            form.addRow(title, entry)
        font_button = QPushButton('フォントを選択…')
        font_button.clicked.connect(self.choose_font)
        self.font_button = font_button
        form.addRow(font_button)
        props_layout.addWidget(group)
        self.cue_group = group
        actions = QHBoxLayout()
        for title, fn in [('cue 追加', self.add_cue), ('複製', self.duplicate_cue), ('削除', self.delete_cue)]:
            button = QPushButton(title)
            button.clicked.connect(fn)
            actions.addWidget(button)
        display_group = QGroupBox('表示条件（視角の換算用）')
        display_form = QFormLayout(display_group)
        self.display_enabled = QCheckBox('display ブロックを保存')
        self.display_enabled.toggled.connect(self.change_display)
        display_form.addRow(self.display_enabled)
        for key, title, default in [('screen_width_mm', '画面幅 [mm]', 597), ('distance_mm', '視距離 [mm]', 530)]:
            spin = QDoubleSpinBox()
            spin.setRange(0, 1e6)
            spin.setDecimals(3)
            spin.setValue(default)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self.change_display)
            self.fields[key] = spin
            display_form.addRow(title, spin)
        props_layout.addWidget(display_group)
        export_group = QGroupBox('書き出し条件')
        export_form = QFormLayout(export_group)
        export_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.delta = QSpinBox()
        self.delta.setRange(1, 3)
        self.delta.valueChanged.connect(self.render_frame)
        export_form.addRow('位相差 Δ（診断にも適用）', self.delta)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(.001, 100)
        self.gain.setDecimals(3)
        self.gain.setValue(1)
        export_form.addRow('gain（エンコードのみ）', self.gain)
        self.cancel_button = QPushButton('エンコードをキャンセル')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_encode)
        self.export_progress = QProgressBar()
        self.export_progress.setMinimumWidth(240)
        self.export_progress.hide()
        self.statusBar().addPermanentWidget(self.export_progress)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.cancel_button.hide()
        props_layout.addWidget(export_group)
        props_layout.addStretch()
        props_scroll.setWidget(props)
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(props_scroll)
        sidebar_layout.addLayout(actions)
        split.addWidget(sidebar)
        split.setSizes([820, 380])
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 5)
        controls = QHBoxLayout()
        self.play_button = QPushButton('再生')
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)
        for title, offset in [('◀ 1f', -1), ('1f ▶', 1)]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, step=offset: self.step_frame(step))
            controls.addWidget(button)
        self.time_label = QLabel('0.000 / 0.000 秒')
        controls.addWidget(self.time_label)
        self.mode = QComboBox()
        self.mode.addItems(['通常', '注視シミュレーション（診断用）', 'サッカードシミュレーション（診断用）'])
        self.mode.currentIndexChanged.connect(self.render_frame)
        controls.addWidget(self.mode)
        self.overlay = QCheckBox('マスク重畳')
        self.overlay.setChecked(True)
        self.overlay.toggled.connect(self.render_frame)
        controls.addWidget(self.overlay)
        controls.addWidget(QLabel('slip'))
        self.slip = QSlider(Qt.Orientation.Horizontal)
        self.slip.setRange(1, 10)
        self.slip.setValue(3)
        self.slip.setMaximumWidth(100)
        self.slip.valueChanged.connect(self.render_frame)
        controls.addWidget(self.slip)
        self.slip_label = QLabel('3 px')
        self.slip.valueChanged.connect(lambda value: self.slip_label.setText(f'{value} px'))
        controls.addWidget(self.slip_label)
        layout.addLayout(controls)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.valueChanged.connect(self.seek_index)
        self.seek_slider.sliderPressed.connect(self.pause)
        layout.addWidget(self.seek_slider)
        timeline_header = QHBoxLayout()
        timeline_header.addWidget(QLabel('タイムライン • 本体で移動、両端で開始／終了を変更'))
        self.snap_check = QCheckBox('0.05 秒にスナップ')
        self.snap_check.setChecked(True)
        timeline_header.addStretch()
        timeline_header.addWidget(self.snap_check)
        layout.addLayout(timeline_header)
        self.timeline = TimelineWidget()
        self.snap_check.toggled.connect(lambda enabled: setattr(self.timeline, 'snap', enabled))
        self.timeline.selectionChanged.connect(self.select_cue)
        self.timeline.seekRequested.connect(self.seek_time)
        self.timeline.cueChanged.connect(self.timeline_changed)
        self.timeline.errorOccurred.connect(self.report)
        timeline_scroll = QScrollArea()
        timeline_scroll.setWidgetResizable(True)
        timeline_scroll.setWidget(self.timeline)
        timeline_scroll.setMinimumHeight(105)
        layout.addWidget(timeline_scroll, 1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['cue', '開始 [秒]', '終了 [秒]', 'パターン', '検証'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.table_selected)
        self.table.cellDoubleClicked.connect(lambda row, col: self.seek_time(number(self.doc.cues[row].get('start'))))
        self.table.setMaximumHeight(145)
        layout.addWidget(self.table)
        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setMaximumHeight(85)
        layout.addWidget(self.validation_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(600)
        self.log.setMaximumHeight(90)
        layout.addWidget(self.log)
        note = QLabel('RGB分離による暫定方式。原論文／研究室実装との一致は未確認。診断画像は実際の知覚や480Hz提示を保証しません。')
        note.setWordWrap(True)
        layout.addWidget(note)

    def _spin_field(self, form, key, title, low, high, step):
        spin = QDoubleSpinBox()
        spin.setDecimals(4 if key.startswith('pos_') else 3)
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda value, k=key: self.set_field(k, value))
        self.fields[key] = spin
        form.addRow(title, spin)

    @property
    def duration(self):
        return self.source.info['duration'] if self.source else None

    def report(self, message):
        self.log.appendPlainText(str(message))
        self.statusBar().showMessage(str(message), 10000)

    def fail(self, context, exc):
        self.report(f'{context}: {exc}')

    def _title(self):
        name = self.cues_path.name if self.cues_path else '未保存の cues'
        self.setWindowTitle(f'{"* " if self.dirty else ""}{name} — saccavid キューエディタ')

    def changed(self):
        self.dirty = True
        self.refresh_document()

    def refresh_document(self):
        self._title()
        issues = self.doc.validate(duration=self.duration)
        by_index = {}
        for issue in issues:
            by_index.setdefault(issue.index, []).append(issue.message)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.doc.cues))
        for i, cue in enumerate(self.doc.cues):
            values = [str(i + 1), str(cue.get('start', '')), str(cue.get('end', '')),
                      str(cue.get('pattern', 'oval')), ' / '.join(by_index.get(i, []))]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if i in by_index:
                    item.setBackground(QColor('#713333'))
                    item.setForeground(QColor('white'))
                self.table.setItem(i, col, item)
        if 0 <= self.index < len(self.doc.cues):
            self.table.selectRow(self.index)
        self.table.blockSignals(False)
        message = '\n'.join(f'{"表示条件" if i.index is None else "cue " + str(i.index + 1)}: {i.message}' for i in issues)
        self.validation_label.setText(message[:1800] if issues else '検証 OK' + ('（動画長は動画読込後に検査）' if not self.source else ''))
        self.validation_label.setStyleSheet('color: #d85a50;' if issues else '')
        self.timeline.set_document(self.doc, self.duration)
        self.timeline.set_selected(self.index)
        self.timeline.set_time(self.current_time)
        self.load_properties()
        self.render_frame()

    def table_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.select_cue(row)

    def select_cue(self, index):
        self.index = index if 0 <= index < len(self.doc.cues) else -1
        self.table.blockSignals(True)
        if self.index >= 0:
            self.table.selectRow(self.index)
        self.table.blockSignals(False)
        self.timeline.set_selected(self.index)
        self.load_properties()
        self.render_frame()

    def selected(self):
        return self.doc.cues[self.index] if 0 <= self.index < len(self.doc.cues) else None

    def load_properties(self):
        self.loading = True
        try:
            cue = self.selected()
            self.cue_group.setEnabled(cue is not None)
            if cue is not None:
                for key, default in [('start', 0), ('end', 1.5), ('angle', -90)]:
                    self.fields[key].setValue(number(cue.get(key), default))
                self.pattern.setCurrentIndex(self.pattern.findText(str(cue.get('pattern', 'oval'))))
                # make_mask resolves coordinates independently for each axis.
                nx = cue.get('nx') is not None or 'x' not in cue
                ny = cue.get('ny') is not None or 'y' not in cue
                self.coords.setCurrentIndex(0 if nx and ny else (1 if not nx and not ny else 2))
                width = self.source.info['width'] if self.source else 1
                height = self.source.info['height'] if self.source else 1
                self.fields['pos_x'].setValue(number(cue.get('nx' if nx else 'x'), .5 if nx else width / 2))
                self.fields['pos_y'].setValue(number(cue.get('ny' if ny else 'y'), .5 if ny else height / 2))
                self.fields['pos_x'].setSuffix(' nx' if nx else ' px')
                self.fields['pos_y'].setSuffix(' ny' if ny else ' px')
                for key, default in [('size', 160), ('thickness', 6)]:
                    deg = key + '_deg' in cue
                    self.fields[key + '_unit'].setCurrentIndex(int(deg))
                    self.fields[key].setValue(number(cue.get(key + '_deg' if deg else key), default))
                for key, default in [('text', '*'), ('font', '')]:
                    self.fields[key].setText(str(cue.get(key, default)))
                pattern = cue.get('pattern', 'oval')
                self.fields['angle'].setEnabled(pattern == 'arrow')
                self.fields['text'].setEnabled(pattern == 'text')
                self.fields['font'].setEnabled(pattern == 'text')
                self.font_button.setEnabled(pattern == 'text')
                self.fields['thickness'].setEnabled(pattern != 'text')
                self.fields['thickness_unit'].setEnabled(pattern != 'text')
            display = self.doc.data.get('display')
            self.display_enabled.setChecked(display is not None)
            for key, default in [('screen_width_mm', 597), ('distance_mm', 530)]:
                self.fields[key].setEnabled(display is not None)
                if isinstance(display, dict):
                    self.fields[key].setValue(number(display.get(key), default))
        finally:
            self.loading = False

    def set_field(self, key, value):
        if self.loading or self.selected() is None:
            return
        cue = self.selected()
        if key.startswith('pos_'):
            axis = key[-1]
            normalized = cue.get('n' + axis) is not None or axis not in cue
            key = 'n' + axis if normalized else axis
        cue[key] = value
        self.changed()

    def set_dimension(self, key, value):
        if self.loading or self.selected() is None:
            return
        field = key + '_deg' if self.fields[key + '_unit'].currentIndex() else key
        self.set_field(field, value)

    def change_unit(self, key, index):
        if self.loading or self.selected() is None:
            return
        cue = self.selected()
        target = key + '_deg' if index else key
        other = key if index else key + '_deg'
        try:
            if not self.source:
                raise ValueError('単位変換の前に動画と表示条件を設定してください')
            ppd = self.doc.px_per_degree(self.source.info['width'])
            if not ppd:
                raise ValueError('deg の使用には display の画面幅と視距離が必要です')
            current = number(cue.get(other), 160 if key == 'size' else 6)
            cue[target] = current / ppd if index else current * ppd
            cue.pop(other, None)
            self.changed()
        except Exception as exc:
            self.fail('単位変換できません', exc)
            self.load_properties()

    def change_coordinates(self, index):
        if self.loading or self.selected() is None or index == 2:
            return
        try:
            if not self.source:
                raise ValueError('座標形式の変換には動画を開いてください')
            self.doc.set_coordinate_mode(self.index, index == 0, self.source.info['width'], self.source.info['height'])
            self.changed()
        except Exception as exc:
            self.fail('座標変換できません', exc)
            self.load_properties()

    def change_display(self, *args):
        if self.loading:
            return
        if self.display_enabled.isChecked():
            # Keep temporary invalid edits visible and let validate() explain
            # them; do not raise out of a Qt signal handler or erase extra keys.
            if not isinstance(self.doc.data.get('display'), dict):
                self.doc.data['display'] = {}
            self.doc.data['display'].update(
                screen_width_mm=self.fields['screen_width_mm'].value(),
                distance_mm=self.fields['distance_mm'].value())
        else:
            self.doc.data.pop('display', None)
        self.changed()

    def choose_font(self):
        path, _ = QFileDialog.getOpenFileName(self, 'フォントを選択', '', 'Fonts (*.ttf *.otf *.ttc);;すべて (*)')
        if path:
            self.set_field('font', path)

    def set_position(self, nx, ny):
        if not self.source or self.selected() is None:
            return
        try:
            self.doc.set_position(self.index, nx, ny, self.source.info['width'], self.source.info['height'])
            self.changed()
        except Exception as exc:
            self.fail('位置を変更できません', exc)

    def add_cue(self):
        self.index = self.doc.add_cue(self.current_time)
        self.changed()

    def duplicate_cue(self):
        if self.selected() is not None:
            self.index = self.doc.duplicate(self.index)
            self.changed()

    def delete_cue(self):
        if self.selected() is not None:
            self.doc.delete(self.index)
            self.index = min(self.index, len(self.doc.cues) - 1)
            self.changed()

    def timeline_changed(self, index):
        self.index = index
        self.changed()

    def choose_video(self):
        path, _ = QFileDialog.getOpenFileName(self, '素材動画を開く', '', '動画 (*.mp4 *.mov *.mkv *.avi *.webm);;すべて (*)')
        if path:
            try:
                self.open_video(path)
            except Exception as exc:
                self.fail('動画を開けません', exc)

    def open_video(self, path):
        # Read successfully before replacing the current source.
        source = VideoSource(str(Path(path).resolve()))
        try:
            frame = source.read(0)
        except Exception:
            source.close()
            raise
        self.pause()
        if self.source:
            self.source.close()
        self.source = source
        self.video_path = Path(path).resolve()
        self.frame = frame
        self.frame_index = 0
        self.current_time = 0
        info = source.info
        self.info_label.setText(f'{self.video_path.name} • {info["width"]} × {info["height"]} • {info["fps"]:.3f} fps • {info["duration"]:.3f} 秒')
        self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, source.frame_count - 1)
        self.seek_slider.setValue(0)
        self.seek_slider.blockSignals(False)
        self.refresh_document()
        self.update_time()

    def confirm_discard(self):
        if not self.dirty:
            return True
        answer = QMessageBox.question(self, '未保存の変更', '現在の cues の変更を保存しますか？',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_cues()
        return answer == QMessageBox.StandardButton.Discard

    def choose_cues(self):
        path, _ = QFileDialog.getOpenFileName(self, 'cues を読み込み', '', 'JSON (*.json);;すべて (*)')
        if path and self.confirm_discard():
            try:
                self.open_cues(path)
            except Exception as exc:
                self.fail('cues を読み込めません', exc)

    def open_cues(self, path):
        doc = CueDocument.load(path)
        self.pause()
        self.doc = doc
        self.cues_path = Path(path).resolve()
        self.index = 0 if doc.cues else -1
        self.dirty = False
        self.refresh_document()

    def save_cues(self, save_as=False):
        issues = self.doc.validate(duration=self.duration)
        if issues:
            self.refresh_document()
            first = next((issue.index for issue in issues if issue.index is not None), None)
            if first is not None:
                self.select_cue(first)
            self.report('保存できません。赤く表示された cue と表示条件を修正してください。')
            for issue in issues:
                self.report(f'{"表示条件" if issue.index is None else "cue " + str(issue.index + 1)}: {issue.message}')
            return False
        path = self.cues_path
        if path is None or save_as:
            name, _ = QFileDialog.getSaveFileName(self, 'cues を保存', str(path or ROOT / 'cues.json'), 'JSON (*.json)')
            if not name:
                return False
            path = Path(name)
        try:
            self.doc.save(path, duration=self.duration)
            self.cues_path = path.resolve()
            self.dirty = False
            self._title()
            self.report(f'保存しました: {path}')
            return True
        except Exception as exc:
            self.fail('保存できません', exc)
            return False

    def seek_time(self, seconds):
        self.pause()
        if self.source:
            self.seek_index(int(max(0., seconds) * self.source.info['fps'] + 1e-7))

    def seek_index(self, index):
        if not self.source:
            return
        index = max(0, min(int(index), self.source.frame_count - 1))
        try:
            if self.frame is None or index != self.frame_index:
                self.frame = self.source.read(index)
            self.frame_index = index
            self.current_time = index / self.source.info['fps']
            if self.timer.isActive():
                self.play_epoch = time.monotonic() - self.current_time
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(index)
            self.seek_slider.blockSignals(False)
            self.update_time()
            self.render_frame()
        except Exception as exc:
            self.pause()
            self.fail('フレームを読めません', exc)

    def update_time(self):
        self.time_label.setText(f'{self.current_time:.3f} / {self.duration or 0:.3f} 秒')
        self.timeline.set_time(self.current_time)

    def step_frame(self, step):
        self.pause()
        self.seek_index(self.frame_index + step)

    def toggle_play(self):
        if self.timer.isActive():
            self.pause()
        elif self.source:
            if self.frame_index >= self.source.frame_count - 1:
                self.seek_index(0)
            self.play_epoch = time.monotonic() - self.current_time
            self.timer.start()
            self.play_button.setText('一時停止')

    def pause(self):
        self.timer.stop()
        if hasattr(self, 'play_button'):
            self.play_button.setText('再生')

    def _tick(self):
        if not self.source:
            return
        epoch = self.play_epoch
        index = int((time.monotonic() - epoch) * self.source.info['fps'])
        if index >= self.source.frame_count:
            self.seek_index(self.source.frame_count - 1)
            self.pause()
        elif index != self.frame_index:
            self.seek_index(index)
            self.play_epoch = epoch

    def render_frame(self, *args):
        if self.frame is None:
            return
        try:
            rgb = render_preview(self.frame, self.doc.data, self.current_time,
                mode=('normal', 'fixation', 'saccade')[self.mode.currentIndex()],
                overlay=self.overlay.isChecked(), slip=self.slip.value(), delta=self.delta.value())
            self.last_error = None
        except Exception as exc:
            message = f'プレビューを生成できません: {exc}'
            if message != self.last_error:
                self.report(message)
                self.last_error = message
            rgb = self.frame
        marker = None
        if self.selected() is not None:
            try:
                marker = self.doc.position(self.index, self.frame.shape[1], self.frame.shape[0])
                if not all(math.isfinite(v) for v in marker):
                    marker = None
            except (TypeError, ValueError, KeyError):
                pass
        self.preview.set_frame(rgb, marker)

    def ensure_export_ready(self):
        if not self.video_path:
            self.report('先に素材動画を開いてください。')
            return False
        if self.doc.validate(duration=self.duration):
            self.save_cues()
            return False
        if self.dirty or not self.cues_path:
            if not self.save_cues():
                return False
        return True

    def copy_player_command(self):
        if not self.ensure_export_ready():
            return
        args = [sys.executable, str(ROOT / 'sacca_player.py'), str(self.video_path), str(self.cues_path),
                '--refresh', '480', '--delta', str(self.delta.value())]
        command = command_text(args)
        QApplication.clipboard().setText(command)
        self.report('プレイヤーコマンドをコピーしました（実行はしていません）:\n' + command)

    def encode(self):
        if self.process is not None:
            self.report('エンコードは既に実行中です。')
            return
        if not self.ensure_export_ready():
            return
        path, _ = QFileDialog.getSaveFileName(self, 'エンコードの出力先', str(self.video_path.with_name(self.video_path.stem + '_480.mp4')), 'MP4 (*.mp4);;Matroska (*.mkv)')
        if not path:
            return
        if Path(path).resolve() in (self.video_path.resolve(), self.cues_path.resolve()):
            self.report('素材動画や cues 自体を出力先にはできません。')
            return
        target = Path(path)
        sidecar = Path(str(target) + '.applied.json')
        if sidecar.exists() and not target.exists():
            answer = QMessageBox.question(self, '適用記録の上書き',
                f'既存の適用記録も置き換えますか？\n{sidecar}',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.start_encode(path, overwrite=target.exists() or sidecar.exists())

    def choose_monitor_playback(self):
        path, _ = QFileDialog.getOpenFileName(self, '実画面で再生する動画', str(getattr(self, 'export_path', ROOT)), '動画 (*.mp4 *.mkv *.mov)')
        if path:
            try:
                MonitorPlayback(path, self).exec()
            except Exception as exc:
                self.fail('再生画面を開けません', exc)

    def choose_review(self):
        path, _ = QFileDialog.getOpenFileName(self, '書き出した動画を開く', '', '動画 (*.mp4 *.mkv *.mov)')
        if path:
            self.review_export(path)

    def review_export(self, path):
        try:
            dialog = ExportReview(path, self, self.slip.value())
            dialog.show()
        except Exception as exc:
            self.fail('書き出し結果を開けません', exc)

    def start_encode(self, path, overwrite=False):
        self.export_path = Path(path).resolve()
        self.total_output_frames = 4 * math.ceil(self.duration * 120 - 1e-9)
        self.process_buffer = b''
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_progress.setFormat('準備中  0%')
        self.export_progress.show()
        self.cancel_button.show()
        self.pause()
        args = [str(self.video_path), str(self.cues_path), '-o', str(Path(path).resolve()),
                '--delta', str(self.delta.value()), '--gain', str(self.gain.value())]
        self.report(command_text([sys.executable, str(ROOT / 'saccavid.py'), 'encode'] + args))
        if overwrite:
            args.append('--overwrite')
        self.process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(environment)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.readyReadStandardOutput.connect(self.read_process)
        self.process.finished.connect(self.encode_finished)
        self.process.errorOccurred.connect(self.encode_error)
        self.cancelled = False
        self.cancel_button.setEnabled(True)
        self.process.start(sys.executable, ['-u', str(ROOT / 'editor_export.py')] + args)

    def read_process(self):
        if self.process:
            self.process_buffer += bytes(self.process.readAllStandardOutput())
            lines = self.process_buffer.split(b'\n')
            self.process_buffer = lines.pop()
            for line in lines:
                message = line.decode('utf-8', errors='replace').rstrip()
                key, _, count = message.partition(' ')
                if key == 'SACCA_FRAME' and count.isdecimal():
                    if not self.cancelled:
                        percent = min(99, int(count) * 100 // self.total_output_frames)
                        self.export_progress.setValue(percent)
                        self.export_progress.setFormat(f'書き出し中  {percent}%')
                elif message:
                    self.log.appendPlainText(message)

    def encode_error(self, error):
        self.report(f'エンコードプロセスのエラー: {self.process.errorString() if self.process else error}')
        if error == QProcess.ProcessError.FailedToStart:
            self.encode_finished(-1, QProcess.ExitStatus.CrashExit)

    def cancel_encode(self):
        if self.process:
            self.cancelled = True
            self.process.write(b'cancel\n')
            self.cancel_button.setEnabled(False)
            self.export_progress.setFormat('キャンセル処理中')
            self.report('キャンセル処理中です…')

    def encode_finished(self, code, status):
        self.read_process()
        self.report('エンコードをキャンセルしました。' if self.cancelled else
                    ('エンコードが完了しました。' if code == 0 else f'エンコードに失敗しました（終了コード {code}）。'))
        if self.process:
            self.process.deleteLater()
        self.process = None
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        if code == 0 and not self.cancelled:
            self.export_progress.setValue(100)
            self.export_progress.setFormat('書き出し完了  100%')
            self.review_export(self.export_path)
        else:
            self.export_progress.setFormat('キャンセル済み' if self.cancelled else '書き出し失敗')

    def closeEvent(self, event):
        if self.process:
            self.cancel_encode()
            self.report('終了前にエンコードを停止しています。停止後にウィンドウを閉じてください。')
            event.ignore()
            return
        if not self.confirm_discard():
            event.ignore()
            return
        self.pause()
        if self.source:
            self.source.close()
        event.accept()


def smoke_test(video, cues):
    """Exercise the same GUI rendering path without showing any window."""
    window = EditorWindow()
    try:
        window.open_video(video)
        window.open_cues(cues)
        issues = window.doc.validate(duration=window.duration)
        if issues:
            raise ValueError('; '.join(issue.message for issue in issues))
        t = float(window.doc.cues[0]['start']) if window.doc.cues else 0.
        window.seek_index(min(window.source.frame_count - 1, int(t * window.source.info['fps'] + 1e-8)))
        # Cues may be shorter than one source-frame interval. Sample the
        # appropriate source frame, but evaluate the mask at the exact cue time.
        window.current_time = t
        window.update_time()
        window.render_frame()
        if window.last_error:
            raise ValueError(window.last_error)
        window.preview.resize(640, 480)
        window.preview.grab()  # Render QWidget + QImage offscreen too.
        with tempfile.TemporaryDirectory(prefix='saccavid-smoke-') as directory:
            path = Path(directory) / 'roundtrip.json'
            window.doc.save(path, duration=window.duration)
            if CueDocument.load(path).data != window.doc.data:
                raise ValueError('JSON の再読込結果が一致しません')
        print('SMOKE OK')
        return 0
    except Exception as exc:
        print(f'SMOKE FAILED: {exc}', file=sys.stderr)
        return 1
    finally:
        window.dirty = False
        window.close()


def main():
    parser = argparse.ArgumentParser(description='saccavid キュー編集ツール（診断用プレビュー）')
    parser.add_argument('--smoke-test', nargs=2, metavar=('VIDEO', 'CUES'))
    parser.add_argument('video', nargs='?')
    parser.add_argument('cues', nargs='?')
    args = parser.parse_args()
    if args.smoke_test:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QApplication([sys.argv[0]])
    if args.smoke_test:
        return smoke_test(*args.smoke_test)
    window = EditorWindow()
    for path, loader, label in [(args.video, window.open_video, '動画を開けません'), (args.cues, window.open_cues, 'cues を読み込めません')]:
        if path:
            try:
                loader(path)
            except Exception as exc:
                window.fail(label, exc)
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
