"""Check decoded image identity across texture retirement and partial uploads."""
import time

import cv2
import numpy as np
import pytest

import prepared_rolling as play


def test_refills_recycled_textures_without_changing_pixels(tmp_path, monkeypatch):
    pygame = play.pygame
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    w, h = 80, 32
    # Encode all four offsets, a clear black cycle, a held cycle, and a
    # changing image. A two-cycle window forces refills and re-use.
    frames = []
    rnd = np.random.default_rng(556)
    for cycle in range(14):
        if cycle in (1, 4, 7):
            frames.extend(frames[-4:])
        elif cycle == 5:
            frames.extend([np.zeros((h, w, 3), np.uint8)] * 4)
        else:
            frames.extend(rnd.integers(0, 256, (4,h,w,3), dtype=np.uint8))
    import subprocess
    p = tmp_path/'test.mkv'
    subprocess.run(['ffmpeg','-v','error','-f','rawvideo','-s',f'{w}x{h}',
                    '-pix_fmt','bgr24','-r','480','-i','-','-c:v','ffv1',str(p)],
                   input=b''.join(f.tobytes() for f in frames),check=True)
    info = play.probe(str(p))
    monkeypatch.setattr(play, 'WINDOW', 2)
    pygame.display.init()
    try:
        win = play.Window('test', size=(w,h),hidden=True)
        renderer = play.Renderer(win,accelerated=0,vsync=False)
        with play.cached(p,info) as (cycles, buf):
            ring = play.Ring(renderer,w,h,cycles,buf)
            ring.rows = 5
            for index, expected in enumerate(frames):
                ring.pump(time.perf_counter())  # at most one strip per call
                while not ring.ahead:
                    ring.pump(time.perf_counter())
                ring.present(index%4)
                rgb = pygame.surfarray.array3d(renderer.to_surface()).transpose(1,0,2)
                assert np.array_equal(expected[:,:,::-1],rgb), index
            assert ring.reuses and ring.retired and ring.next_index == len(cycles)
            del ring
        def forbid_build(*args):
            pytest.fail('cache unexpectedly rebuilt')
        monkeypatch.setattr(play, 'build', forbid_build)
        with play.cached(p,info) as (cycles, buf):
            assert len(cycles)*4 == len(frames)
        del renderer
        win.destroy()
    finally:
        pygame.display.quit()
