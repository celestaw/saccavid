"""Present decoded MP4 frames in order and report decode starvation separately."""
import queue
import threading
import time
from statistics import median

import cv2
import pygame
from pygame._sdl2.video import Window, Renderer, Texture

from saccavid import probe


def present_movie(path, x, y):
    info = probe(path)
    if abs(info['fps'] - 480) > .01:
        raise ValueError('480 fps動画を選択してください。')
    w, h = info['width'], info['height']
    capacity = max(2, min(120, (192 << 20) // (w * h * 3)))
    frames = queue.Queue(capacity)
    stop = threading.Event()
    ready = threading.Event()
    end = object()

    def enqueue(value):
        while not stop.is_set():
            try:
                frames.put(value, timeout=.05)
                if frames.full() or value is end or isinstance(value, Exception):
                    ready.set()
                return
            except queue.Full:
                pass

    def decode():
        cap = None
        try:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                raise IOError('動画を開けません。')
            index = 0
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    if info['nb_frames'] and index != info['nb_frames']:
                        raise IOError(f'予定 {info["nb_frames"]} 枚のうち {index} 枚で読み込み終了')
                    enqueue(end)
                    break
                index += 1
                enqueue(frame)
        except Exception as exc:
            enqueue(exc)
        finally:
            if cap is not None:
                cap.release()
            ready.set()

    producer = threading.Thread(target=decode, daemon=True)
    producer.start()
    times = []
    waits = 0
    waited = 0.
    try:
        ready.wait()
        pygame.display.init()
        window = Window('同期再生（映像のみ）', size=(320, 180), position=(x, y))
        window.set_fullscreen(desktop=True)
        renderer = Renderer(window, accelerated=1, vsync=True)
        renderer.logical_size = (w, h)
        texture = None
        while True:
            if any(e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE)
                   for e in pygame.event.get()):
                break
            try:
                frame = frames.get_nowait()
            except queue.Empty:
                start = time.perf_counter()
                try:
                    frame = frames.get(timeout=.02)
                except queue.Empty:
                    frame = None
                waited += time.perf_counter() - start
                waits += 1
                if frame is None:
                    continue
            if frame is end:
                break
            if isinstance(frame, Exception):
                raise frame
            surface = pygame.image.frombuffer(frame, (w, h), 'BGR')
            if texture is None:
                texture = Texture.from_surface(renderer, surface)
            else:
                texture.update(surface)
            renderer.clear()
            texture.draw()
            renderer.present()
            times.append(time.perf_counter())
        del texture, renderer
        window.destroy()
    finally:
        stop.set()
        producer.join(timeout=2)
        pygame.display.quit()
    print(f'提示 {len(times)} 枚、デコード待ち合計 {waited:.3f} 秒（待機 {waits} 回）。')
    intervals = [b-a for a,b in zip(times,times[1:])]
    if intervals:
        print(f'提示呼び出し平均 {len(intervals)/sum(intervals):.1f} 回/秒、中央値 {median(intervals)*1000:.2f} ms、'
              f'3.13 ms超 {sum(t > 1.5/480 for t in intervals)}/{len(intervals)} 回。')


if __name__ == '__main__':
    import sys
    present_movie(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
