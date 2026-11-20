"""Repeat four decoded frames from the exported movie with SDL renderer vsync."""
import sys
import time
from statistics import median
import pygame
from pygame._sdl2.video import Window, Renderer, Texture
from editor_preview import VideoSource


def run(path, x, y):
    with VideoSource(path) as source:
        if abs(source.info['fps'] - 480) > .01 or source.frame_count < 4:
            raise ValueError('480 fpsの書き出し動画を指定してください。')
        frames = [source.read(i) for i in range(4)]
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
            textures[len(times) % 4].draw()
            renderer.present()
            times.append(time.perf_counter())
        intervals = [b - a for a, b in zip(times, times[1:])]
        if intervals:
            hz = len(intervals) / sum(intervals)
            print(f'提示呼び出しの平均 {hz:.1f} 回/秒、間隔中央値 {median(intervals)*1000:.2f} ms、'
                  f'3.13 ms超 {sum(dt > 1.5/480 for dt in intervals)}/{len(intervals)} 回。')
        else:
            print('計測前に終了しました。')
        del textures, renderer
        window.destroy()
    finally:
        pygame.display.quit()


if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
