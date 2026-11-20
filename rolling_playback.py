"""Keep up to three seconds of output slots resident in GPU textures."""
from collections import deque
from contextlib import contextmanager
import queue
import sys
import threading
import time
from statistics import median

import cv2
import numpy as np
import pygame
from pygame._sdl2.video import Renderer, Texture, Window

from saccavid import probe


WINDOW = 1440
TEXTURE_BUDGET = 4 << 30  # Approximate RGBA8 allocation, excluding driver overhead.
END = object()


@contextmanager
def decoded_slots(path, width, height, expected):
    # A bounded CPU staging queue. Each slot points to an immutable array;
    # repeated frames of the same phase share the exact same array object.
    items = queue.Queue(maxsize=max(4, min(256, (192 << 20) // (4 * width * height))))
    stopped = threading.Event()

    def put(item):
        while not stopped.is_set():
            try:
                items.put(item, timeout=.05)
                return
            except queue.Full:
                pass

    def produce():
        cap = None
        try:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                raise IOError('MP4を開けません。')
            phases = [None] * 4
            index = 0
            while not stopped.is_set():
                ok, frame = cap.read()
                if not ok:
                    if expected and expected != index:
                        raise IOError(f'読み込めたのは {index}/{expected} 枚です。')
                    if index == 0 or index % 4:
                        raise IOError('末尾が完全な4枚周期ではありません。')
                    put(END)
                    break
                if frame.shape != (height, width, 3):
                    raise ValueError('映像の解像度が一致しません。')
                previous = phases[index % 4]
                if previous is None or cv2.norm(frame, previous[0], cv2.NORM_INF) != 0:
                    # Upload-ready pixels. BGRA avoids converting RGB to the
                    # common 32-bit texture format on the display thread.
                    previous = (frame, (index, cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)))
                    phases[index % 4] = previous
                put(previous[1])
                index += 1
        except Exception as exc:
            put(exc)
        finally:
            if cap is not None:
                cap.release()

    thread = threading.Thread(target=produce, daemon=True)
    thread.start()
    try:
        yield items
    finally:
        stopped.set()
        thread.join(timeout=2)


class TextureWindow:
    def __init__(self, width, height, renderer):
        self.size = width, height
        self.limit = max(4, TEXTURE_BUDGET // (width * height * 4))
        self.renderer = renderer
        self.frames = deque()
        self.entries = {}
        self.free = []
        self.peak = 0
        self.uploads = 0

    def add(self, slot):
        # Slot IDs are assigned by the producer; no pixel buffers are retained
        # by the GPU window after upload.
        key, pixels = slot
        entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.limit:
                return False
            surface = pygame.image.frombuffer(pixels, self.size, 'BGRA')
            if self.free:
                texture = self.free.pop()
                texture.update(surface)
            else:
                texture = Texture.from_surface(self.renderer, surface)
            entry = [texture, 0]
            self.entries[key] = entry
            self.uploads += 1
            self.peak = max(self.peak, len(self.entries))
        entry[1] += 1
        self.frames.append(key)
        return True

    def present(self):
        key = self.frames.popleft()
        entry = self.entries[key]
        self.renderer.clear()
        entry[0].draw()
        self.renderer.present()
        entry[1] -= 1
        if not entry[1]:
            self.free.append(entry[0])
            del self.entries[key]


def run(path, x, y):
    info = probe(path)
    if abs(info['fps'] - 480) > .01:
        raise ValueError('480 fps動画を指定してください。')
    w, h = info['width'], info['height']
    pygame.display.init()
    try:
        win = Window('GPU先読み再生', size=(320, 180), position=(x, y))
        win.set_fullscreen(desktop=True)
        renderer = Renderer(win, accelerated=1, vsync=True)
        renderer.logical_size = (w, h)
        ring = TextureWindow(w, h, renderer)
        stamps = []
        pending = None
        eof = False
        started = False
        initial = 0
        stalls = 0
        with decoded_slots(path, w, h, info['nb_frames']) as items:
            while True:
                if any(e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE)
                       for e in pygame.event.get()):
                    break
                # Limit work between two presents. If decoder lags, drain
                # resident slots in order; never drop a phase to catch up.
                for _ in range(4 if started else 32):
                    if eof or len(ring.frames) >= WINDOW:
                        break
                    if pending is None:
                        try:
                            pending = items.get(timeout=0 if started else .01)
                        except queue.Empty:
                            break
                    if pending is END:
                        eof = True
                        pending = None
                        break
                    if isinstance(pending, Exception):
                        raise pending
                    if not ring.add(pending):
                        break
                    pending = None
                if not started:
                    if not eof and len(ring.frames) < WINDOW and len(ring.entries) < ring.limit:
                        continue
                    initial = len(ring.frames)
                    started = True
                if ring.frames:
                    ring.present()
                    stamps.append(time.perf_counter())
                elif eof:
                    break
                else:
                    # Count a decoder wait once per missing slot, not per poll.
                    stalls += 1
                    while pending is None:
                        try:
                            pending = items.get(timeout=.02)
                        except queue.Empty:
                            if pygame.event.peek(pygame.QUIT) or pygame.key.get_pressed()[pygame.K_ESCAPE]:
                                return
                            pygame.event.pump()
        print(f'先読み開始時 {initial} 枚（{initial/480:.2f} 秒）、提示 {len(stamps)} 枚、'
              f'GPU転送 {ring.uploads} 回、バッファ枯渇 {stalls} 回。')
        print(f'テクスチャ最大 {ring.peak} 枚、RGBA換算 {ring.peak*w*h*4/2**30:.2f} GiB。')
        if len(stamps) > 1:
            dt = np.diff(stamps)
            print(f'提示呼び出し平均 {1/dt.mean():.1f} 回/秒、中央値 {median(dt)*1000:.2f} ms、'
                  f'3.13 ms超 {sum(dt > 1.5/480)}/{len(dt)} 回。')
        del ring, renderer
        win.destroy()
    finally:
        pygame.display.quit()


if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
