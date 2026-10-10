#!/usr/bin/env python3
"""Render sample screens (illustrative data, no hardware) into docs/previews/."""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import panel_app as app  # noqa: E402


def scene(size=(320, 240)):
    w, h = size
    image = Image.new('RGB', size, '#8ab4c0')
    d = ImageDraw.Draw(image)
    d.polygon([(w*.0, h*.65), (w*.28, h*.25), (w*.55, h*.65)], fill='#355566')
    d.polygon([(w*.4, h*.65), (w*.7, h*.33), (w*1., h*.65)], fill='#4b6878')
    d.rectangle((0, h*.65, w, h), fill='#3f7a62')
    return image


def main():
    clock = datetime(2026, 10, 9, 19, 24, 7).astimezone()
    rows = {'cpu': 18, 'temp': '77.1 C', 'ram': '1.1/3.7 GiB', 'ram_percent': 30,
            'disk_free': '18.4 GiB', 'disk_used_percent': 38, 'uptime': '2h 16m'}
    photo = {'image': scene((640, 480)), 'size': (3280, 2464), 'index': 4, 'count': 7,
             'label': '2026/10/09 19:12:33'}
    thumb = scene((60, 45))
    common = dict(options={**app.DEFAULT_OPTIONS, 'snap_timer': 3}, wall_time=clock)
    screens = [
        app.render('camera', scene(), False, 20, rows, 'Self-timer running | CANCEL stops it',
                   cam={'thumb': thumb, 'countdown': 2}, **common),
        app.render('camera', scene(), False, 20, rows, 'Saved 20261009-192407  3280x2464',
                   cam={'thumb': thumb, 'saved': '3280x2464'}, **common),
        app.render('photos', None, False, 20, rows, '', photo=photo, **common),
        app.render('photodelete', None, False, 20, rows, '', photo=photo, **common),
        app.render('camera', scene(), False, 20, rows, 'Live | SNAP saves a photo',
                   cam={'thumb': thumb}, alerts=[('warning', 'WARM 77 C')], **common),
        app.render('status', None, False, 20, rows, '', (), 0,
                   alerts=[('critical', 'LOW VOLTAGE'), ('warning', 'LOW STORAGE 0.8 GB')],
                   **common),
        app.render('exit', None, False, 20, rows, '', **common),
        app.render('shutdown', None, False, 20, rows, '', **common),
        app.render('options', None, False, 20, rows, '', **common),
        app.render('poweroff', None, False, 20, rows, '', **common),
    ]
    save_sheet(screens, 'camera-photos-power-preview.png')
    fun_screens(clock, rows)
    portrait_screens(clock, rows)


def save_sheet(screens, name, cols=2, gap=8, scale=2):
    w, h = screens[0].width*scale, screens[0].height*scale
    rows_n = (len(screens)+cols-1)//cols
    sheet = Image.new('RGB', (cols*(w+gap)-gap, rows_n*(h+gap)-gap), '#05080b')
    for i, screen in enumerate(screens):
        big = screen.resize((w, h), Image.NEAREST)
        sheet.paste(big, ((i % cols)*(w+gap), (i//cols)*(h+gap)))
    out = ROOT / 'docs/previews' / name
    sheet.save(out, optimize=True)
    print(out)


def fun_screens(clock, rows):
    import tempfile
    options = {**app.DEFAULT_OPTIONS, 'saver_style': 'bunnies', 'bunny_count': 6}
    capture = app.AudioCapture(which=lambda name: None)
    capture.source, capture.status = 'output', 'Listening via parec'
    t = np.arange(app.VIS_CHUNK) / app.VIS_RATE
    capture.feed((sum(np.sin(2*np.pi*f*t)*a for f, a in ((110, 9000), (440, 5000), (2500, 1500)))
                  + np.random.default_rng(1).normal(0, 600, len(t))).astype(np.int16))
    audio_state = {'backend': 'PipeWire', 'volume': 55, 'muted': False, 'player': 'Playing',
                   'title': 'Sample Artist - Sample Track'}
    common = dict(options=options, wall_time=clock)
    notes = {'audio': 'Volume, media, visualizer', 'snake': 'Best score: 12',
             'album': '7 pictures | slideshow', 'gifs': '3 GIFs | tap to play'}
    snake = app.SnakeApp(best=12, seed=5)
    snake.start(0)
    snake.game.body = app.deque([(12, 7), (11, 7), (10, 7), (10, 6), (9, 6), (8, 6), (8, 5)])
    snake.game.food, snake.game.score = (17, 4), 9
    bunnies = app.BunnySaver(options, lambda n: 'Saved.', seed=7)
    for step in range(40):
        bunnies.render(step*.1, clock, ())
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        scene((640, 480)).save(folder / 'photo-20261009-191233-000000.jpg')
        show = app.Slideshow(options, folder, folder / 'none', app.PhotoLibrary(folder))
        album = app.AlbumApp(show, lambda key: 'Saved.')
        album.overlay_until = 99
        album_image = album.render(1, clock, ())
    screens = [
        app.render('apps', None, False, 20, rows, '', extra={'notes': notes}, **common),
        app.render('audio', None, False, 20, rows, '',
                   extra={'audio': audio_state, 'capture': capture}, **common),
        snake.render(0, clock, ()),
        bunnies.render(4.1, clock, ())[0],
        app.ClockSaver(options).render(0, clock, ())[0],
        album_image,
        app.render('savers', None, False, 20, rows, '', **common),
        app.render('saverstyle', None, False, 20, rows, '', **common),
    ]
    save_sheet(screens, 'apps-savers-preview.png')
    frames = []
    saver = app.BunnySaver({**options, 'bunny_count': 6}, lambda n: 'Saved.', seed=11)
    for step in range(60):
        frames.append(saver.render(step*.1, clock, ())[0].quantize(64))
    frames[0].save(ROOT / 'docs/previews/bunnies.gif', save_all=True, append_images=frames[1:],
                   duration=100, loop=0, optimize=True)


def portrait_screens(clock, rows):
    """The same pages with OPTIONS -> Screen at 90 degrees clockwise, shown upright."""
    app.set_rotation(90)
    try:
        options = {**app.DEFAULT_OPTIONS, 'snap_timer': 3, 'saver_style': 'bunnies'}
        common = dict(options=options, wall_time=clock)
        photo = {'image': scene((640, 480)), 'size': (3280, 2464), 'index': 4, 'count': 7,
                 'label': '2026/10/09 19:12:33'}
        capture = app.AudioCapture(which=lambda name: None)
        capture.source, capture.status = 'output', 'Listening via parec'
        t = np.arange(app.VIS_CHUNK) / app.VIS_RATE
        capture.feed((np.sin(2*np.pi*220*t)*9000 + np.sin(2*np.pi*1800*t)*3000).astype(np.int16))
        notes = {'audio': 'Volume, media, visualizer', 'snake': 'Best score: 12',
                 'album': '7 pictures | slideshow', 'gifs': '3 GIFs | tap to play'}
        snake = app.SnakeApp(best=12, seed=5)
        snake.start(0)
        snake.game.body = app.deque([(9, 10), (8, 10), (7, 10), (7, 9), (6, 9)])
        snake.game.food, snake.game.score = (12, 5), 6
        bunnies = app.BunnySaver({**options, 'bunny_count': 6}, lambda n: 'Saved.', seed=7)
        for step in range(40):
            bunnies.render(step*.1, clock, ())
        screens = [
            app.render('camera', scene(), False, 20, rows, 'Live | SNAP saves a photo',
                       cam={'thumb': scene((60, 45))}, **common),
            app.render('photos', None, False, 20, rows, '', photo=photo, **common),
            app.render('status', None, False, 20, rows, '', (), 0,
                       alerts=[('warning', 'WARM 77 C')], **common),
            app.render('options', None, False, 20, rows, '', **common),
            app.render('apps', None, False, 20, rows, '', extra={'notes': notes}, **common),
            app.render('audio', None, False, 20, rows, '',
                       extra={'audio': {'volume': 55, 'player': 'Playing',
                                        'title': 'Sample Artist - Sample Track'},
                              'capture': capture}, **common),
            snake.render(0, clock, ()),
            bunnies.render(4.1, clock, ())[0],
            app.render('exit', None, False, 20, rows, '', **common),
            app.ClockSaver(options).render(0, clock, ())[0],
        ]
        save_sheet(screens, 'portrait-preview.png', cols=5)
        # What actually goes to the panel: the canvas turned 90 degrees clockwise.
        save_sheet([app.physical_image(screens[0])], 'portrait-on-panel.png', cols=1)
    finally:
        app.set_rotation(0)


if __name__ == '__main__':
    main()
