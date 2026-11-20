"""Repeat four decoded frames from the exported movie with SDL renderer vsync."""
import hashlib
import sys
import time
from statistics import median
import pygame
from pygame._sdl2.video import Window, Renderer, Texture
from editor_preview import VideoSource


def preload(source, count, start_frame=0):
    # Identical frames can share a texture, but every output slot keeps its place.
    unique = []
    slots = []
    lookup = {}
    size = 0
    for index in range(start_frame, min(start_frame + count, source.frame_count)):
        frame = source.read(index)
        key = hashlib.sha256(memoryview(frame)).digest()
        if key not in lookup:
            size += frame.nbytes
            if size > 256 * 1024**2:
                break
            lookup[key] = len(unique)
            unique.append(frame)
        slots.append(lookup[key])
    slots = slots[:len(slots) // 4 * 4]
    if not slots:
        raise ValueError('完全な1周期を先読みできませんでした。')
    return unique, slots


def run(path, x, y, count=4, seconds=0.):
    with VideoSource(path) as source:
        if abs(source.info['fps'] - 480) > .01 or source.frame_count < 4:
            raise ValueError('480 fpsの書き出し動画を指定してください。')
        start_frame = min(source.frame_count // 4 - 1, max(0, int(seconds * 120))) * 4
        frames, slots = preload(source, count, start_frame)
    pygame.display.init()
    try:
        window = Window('4フレーム確認', size=(320, 180), position=(x, y))
        window.set_fullscreen(desktop=True)
        renderer = Renderer(window, accelerated=1, vsync=True)
        h, w = frames[0].shape[:2]
        renderer.logical_size = (w, h)
        textures = [Texture.from_surface(renderer, pygame.image.frombuffer(f.tobytes(), (w, h), 'RGB')) for f in frames]
        times = []
        start = time.perf_counter()
        while time.perf_counter() - start < 3:
            if any(e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE)
                   for e in pygame.event.get()):
                break
            renderer.clear()
            textures[slots[len(times) % len(slots)]].draw()
            renderer.present()
            times.append(time.perf_counter())
        intervals = [b - a for a, b in zip(times, times[1:])]
        if intervals:
            hz = len(intervals) / sum(intervals)
            print(f'{start_frame/480:.3f} 秒から {len(slots)} 枚（{len(slots)/480:.3f}秒）をループ。提示呼び出しの平均 {hz:.1f} 回/秒、間隔中央値 {median(intervals)*1000:.2f} ms、'
                  f'3.13 ms超 {sum(dt > 1.5/480 for dt in intervals)}/{len(intervals)} 回。')
        else:
            print('計測前に終了しました。')
        del textures, renderer
        window.destroy()
    finally:
        pygame.display.quit()


if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),
        120 if '--moving' in sys.argv[4:] else 4,
        float(sys.argv[5]) if sys.argv[4:5] == ['--at'] else 0.)
