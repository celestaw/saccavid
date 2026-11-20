"""Prepare lossless RGB layers once, then refill a three-second GPU ring in strips."""
from collections import deque
from contextlib import contextmanager
import json
import mmap
import os
import shutil
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time

import cv2
import numpy as np
import pygame
from pygame._sdl2.video import Renderer, Texture, Window

from rolling_playback import decoded_slots, END
from saccavid import probe


MAGIC = b'SACCLYR1'
WINDOW = 360  # four-slot cycles; 3 seconds at 480 Hz
CANCEL = threading.Event()
CAP = 4 << 30


def identity(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def read_index(stream, source_id, w, h, n):
    stream.seek(-16, os.SEEK_END)
    size, magic = struct.unpack('<Q8s', stream.read(16))
    if magic != MAGIC or size > 64 << 20:
        raise ValueError('invalid cache footer')
    stream.seek(-16-size, os.SEEK_END)
    data_size = stream.tell()
    record = json.loads(stream.read(size))
    if (record['source'] != source_id or record['shape'] != [w, h]
            or data_size != record['layers'] * w*h*4
            or not record['cycles'] or (n and len(record['cycles'])*4 != n)):
        raise ValueError('stale or incomplete cache')
    if any(len(ids) != 4 or any(type(i) is not int or not -1 <= i < record['layers']
                                for i in ids) for ids in record['cycles']):
        raise ValueError('invalid layer reference')
    return record


def build(stream, path, info):
    """Each offset layer packs RGB values from three different encoded slots.
    Selecting one component/layer/refresh recovers even arbitrary input bytes.
    Persist only layers with nonzero contributions; zero is the cleared screen.
    """
    w, h = info['width'], info['height']
    alpha = np.full((h, w), 255, np.uint8)
    zero = np.zeros_like(alpha)
    layer_ids = {}
    cycles = []
    source_id = identity(path)
    previous = [None]*4
    cycle = []
    prev_key = None
    last_progress = 0.
    with decoded_slots(str(path), w, h, info['nb_frames']) as queue:
        while True:
            if CANCEL.is_set():
                raise InterruptedError("準備をキャンセルしました。")
            item = queue.get()
            if item is END:
                break
            if isinstance(item, Exception):
                raise item
            phase = len(cycle)
            key, bgra = item
            if previous[phase] is None or previous[phase][0] != key:
                planes = cv2.split(bgra)[:3]
                previous[phase] = (key, [p if cv2.countNonZero(p) else None for p in planes])
            cycle.append(previous[phase])
            if len(cycle) != 4:
                continue
            keys = tuple(v[0] for v in cycle)
            if keys != prev_key:
                ids = []
                for offset in range(4):
                    channels = []
                    signature = []
                    for bgr in range(3):
                        k, planes = cycle[(2-bgr-offset) % 4]
                        plane = planes[bgr]
                        channels.append(zero if plane is None else plane)
                        signature.append(-1 if plane is None else k)
                    sig = tuple(signature)
                    if sig == (-1, -1, -1):
                        ids.append(-1)
                        continue
                    if sig not in layer_ids:
                        layer_ids[sig] = len(layer_ids)
                        stream.write(memoryview(cv2.merge(channels + [alpha])))
                    ids.append(layer_ids[sig])
                prev_key = keys
            cycles.append(ids)
            cycle.clear()
            if time.monotonic() - last_progress > 1:
                if shutil.disk_usage(path.parent).free < (512 << 20):
                    raise OSError('キャッシュ用の空き容量が不足しました。')
                print(f'準備中 {len(cycles)*4}/{info["nb_frames"] or "?"} 枚', flush=True)
                last_progress = time.monotonic()
    if cycle or not cycles or identity(path) != source_id:
        raise ValueError('準備中に動画が変更されたか、不完全な周期が含まれています。')
    trailer = json.dumps(dict(source=source_id, shape=[w,h], layers=len(layer_ids),
                              cycles=cycles), separators=(',',':')).encode()
    stream.write(trailer)
    stream.write(struct.pack('<Q8s', len(trailer), MAGIC))


@contextmanager
def cached(path, info):
    path = Path(path)
    cache = path.with_name(path.name + '.sacca-cache')
    def index(stream):
        return read_index(stream, identity(path), info['width'], info['height'], info['nb_frames'])
    try:
        with cache.open('rb') as stream:
            index(stream)
    except (FileNotFoundError, ValueError, KeyError, struct.error, OSError):
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache.parent, prefix='.sacca-prep-', delete=False) as out:
                temp = Path(out.name)
                build(out, path, info)
            os.replace(temp, cache)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    with cache.open('rb') as stream:
        record = index(stream)
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as pixels:
            yield record['cycles'], pixels


class Ring:
    def __init__(self, renderer, width, height, cycles, pixels):
        self.renderer = renderer
        self.w, self.h = width, height
        self.bytes = width*height*4
        self.rows = max(1, (1 << 20)//(width*4))
        self.limit = max(8, CAP//self.bytes)
        self.cycles, self.pixels = cycles, pixels
        self.ahead = deque()
        self.next_index = 0
        self.resident = {}  # layer -> [texture, queued uses]
        self.free = []
        self.pending = None
        self.uploads = self.reuses = self.retired = 0

    def pump(self, deadline):
        # Queue whole cycles only after all of their textures are ready.
        while len(self.ahead) < WINDOW and self.next_index < len(self.cycles):
            ids = self.cycles[self.next_index]
            for layer in ids:
                if layer < 0 or layer in self.resident:
                    continue
                if self.pending is None:
                    if self.free:
                        tex = self.free.pop()
                        self.reuses += 1
                    elif len(self.resident) < self.limit:
                        tex = Texture(self.renderer, (self.w,self.h), streaming=True)
                        tex.blend_mode = pygame.BLENDMODE_ADD
                    else:
                        return
                    self.pending = (layer, tex, 0)
                current, tex, row = self.pending
                assert current == layer
                while row < self.h:
                    rows = min(self.rows, self.h-row)
                    off = layer*self.bytes + row*self.w*4
                    chunk = memoryview(self.pixels)[off:off+rows*self.w*4]
                    surface = pygame.image.frombuffer(chunk,(self.w,rows),'BGRA')
                    tex.update(surface, (0,row,self.w,rows))
                    row += rows
                    if time.perf_counter() >= deadline and row < self.h:
                        self.pending = (layer,tex,row)
                        return
                self.pending = None
                self.resident[layer] = [tex,0]
                self.uploads += 1
                if time.perf_counter() >= deadline:
                    return
            for layer in ids:
                if layer >= 0:
                    self.resident[layer][1] += 1
            self.ahead.append(ids)
            self.next_index += 1
            if time.perf_counter() >= deadline:
                return

    def present(self, phase):
        ids = self.ahead[0]
        self.renderer.draw_color = (0,0,0,255)
        self.renderer.clear()
        for offset,layer in enumerate(ids):
            color = (phase+offset)%4
            if layer >= 0 and color < 3:
                tex = self.resident[layer][0]
                tex.color = ((255,0,0),(0,255,0),(0,0,255))[color]
                tex.draw()
        self.renderer.present()
        if phase == 3:
            self.ahead.popleft()
            for layer in ids:
                if layer < 0:
                    continue
                entry = self.resident[layer]
                entry[1] -= 1
                if not entry[1] and not (self.next_index < len(self.cycles) and
                                          layer in self.cycles[self.next_index]):
                    self.free.append(entry[0])
                    del self.resident[layer]
                    self.retired += 1


def quit_requested():
    return CANCEL.is_set() or any(e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE)
               for e in pygame.event.get())


def run(path, x, y):
    info = probe(path)
    if abs(info['fps']-480) > .01:
        raise ValueError('480 fps動画を選択してください。')
    with cached(path, info) as (cycles, pixels):
        pygame.display.init()
        try:
            win = Window('GPU先読み再生', size=(320,180), position=(x,y))
            win.set_fullscreen(desktop=True)
            renderer = Renderer(win, accelerated=1, vsync=True)
            renderer.logical_size = (info['width'], info['height'])
            ring = Ring(renderer,*renderer.logical_size,cycles,pixels)
            while (len(ring.ahead) < min(WINDOW,len(cycles)) and len(ring.resident)<ring.limit):
                if quit_requested():
                    return
                ring.pump(time.perf_counter()+.01)
            initial = ring.next_index
            stamps = []
            misses = 0
            first_miss = None
            for i in range(len(cycles)*4):
                if quit_requested():
                    break
                ring.pump(time.perf_counter()+.00055)
                if not ring.ahead:
                    misses += 1
                    if first_miss is None:
                        first_miss = i / 480
                    while not ring.ahead:
                        if quit_requested():
                            return
                        ring.pump(time.perf_counter()+.01)
                ring.present(i%4)
                stamps.append(time.perf_counter())
            print(f'初期 {initial*4} 枚、再生開始後に補充 {(ring.next_index-initial)*4} 枚、提示 {len(stamps)} 枚。'
                  f' 使用済み解放 {ring.retired} 回、GPU領域再利用 {ring.reuses} 回、枯渇 {misses} 回。')
            if first_miss is not None:
                print(f'初回枯渇は動画時刻 {first_miss:.3f} 秒。')
            if len(stamps)>1:
                dt=np.diff(stamps)
                print(f'提示平均 {1/dt.mean():.1f} 回/秒、3.13 ms超 {np.count_nonzero(dt>1.5/480)} 回。')
            del ring, renderer
            win.destroy()
        finally:
            pygame.display.quit()


if __name__ == '__main__':
    def listen():
        if os.read(sys.stdin.fileno(), 32).strip() == b'cancel':
            CANCEL.set()
    threading.Thread(target=listen, daemon=True).start()
    try:
        run(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    except InterruptedError as exc:
        print(exc)
