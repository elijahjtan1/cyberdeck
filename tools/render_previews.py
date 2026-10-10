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
    games_screens(clock)


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


def games_screens(clock):
    """Games menu, Tetris and Pong in landscape (top two rows) and portrait (bottom)."""
    def tetris_midgame():
        tetris = app.TetrisApp(best=4200, seed=8)
        tetris.state = 'running'
        game = tetris.game
        rows = ['ZZZZ.SSJJJ', 'TTZ.SSLJJI', 'OTTLLLLOOI', 'OOT.JJJOOI']
        for i, row in enumerate(rows):
            game.board[16+i] = [None if c == '.' else c for c in row]
        game.kind, game.rotation, game.x, game.y = 'T', 1, 2, 6
        game.score, game.lines = 1840, 12
        return tetris

    def pong_rally():
        pong = app.PongApp(wins=3, seed=4)
        pong.state = 'running'
        game = pong.game
        game.player_score, game.cpu_score, game.wait = 4, 2, 0
        game.player, game.cpu = game.width*.4, game.width*.62
        game.ball = [game.width*.55, game.length*.62]
        pong.last = None
        return pong

    def bricks_midgame():
        bricks = app.BrickApp(best=960, seed=2)
        bricks.state = 'running'
        game = bricks.game
        game.bricks -= {(5, 1), (5, 2), (4, 2), (5, 6), (3, 4), (4, 4), (5, 4), (5, 5)}
        game.score, game.lives, game.stuck = 360, 2, False
        game.paddle = game.width*.45
        game.ball = [game.width*.6, game.height*.55]
        bricks.last = None
        return bricks

    def flappy_midgame():
        flappy = app.FlappyApp(best=14, seed=5)
        flappy.state = 'running'
        game = flappy.game
        game.score = 6
        game.pipes = [[game.width*.45, game.height*.45, True], [game.width*.95, game.height*.6, False]]
        game.y, game.vy = game.height*.5, -60
        flappy.last = None
        return flappy

    sheets = []
    for rotation in (0, 90):
        app.set_rotation(rotation)
        try:
            notes = {'snake': 'Best score: 23', 'tetris': 'Best score: 4200', 'pong': 'Games won: 3',
                     'bricks': 'Best score: 960', 'flappy': 'Best score: 14'}
            screens = [app.GamesMenu(lambda: notes).render(0, clock, ()),
                       tetris_midgame().render(0, clock, ()),
                       pong_rally().render(0, clock, ()),
                       bricks_midgame().render(0, clock, ()),
                       flappy_midgame().render(0, clock, ()),
                       app.BrickApp(best=960).render(0, clock, ())]
            sheets.append(screens)
        finally:
            app.set_rotation(0)
    save_sheet(sheets[0], 'games-landscape-preview.png', cols=3)
    save_sheet(sheets[1], 'games-portrait-preview.png', cols=6)
    puzzle_screens(clock)
    home_screens(clock)


def home_screens(clock):
    rows = {'temp': '48.2 C', 'disk_free': '18.4 GiB'}
    notes = {'wifi': 'Wi-Fi -58 dBm', 'camera': 'Live view', 'photos': '23 saved',
             'games': '9 games', 'audio': 'Vol 55%', 'album': 'Slideshow', 'gifs': 'Animations',
             'tools': 'Timers & more', 'quick': 'Shortcuts', 'saver': 'Bunnies'}
    landscape = app.render('home', None, False, 20, rows, '', wall_time=clock, extra={'home': notes})
    app.set_rotation(90)
    try:
        portrait = app.render('home', None, False, 20, rows, '', wall_time=clock, extra={'home': notes})
    finally:
        app.set_rotation(0)
    sheet = Image.new('RGB', (640+8+480, 640), '#05080b')
    sheet.paste(landscape.resize((640, 480), Image.NEAREST), (0, 80))
    sheet.paste(portrait.resize((480, 640), Image.NEAREST), (648, 0))
    out = ROOT / 'docs/previews/home-preview.png'
    sheet.save(out, optimize=True)
    print(out)


def puzzle_screens(clock):
    """Space Invaders, Minesweeper, Solitaire and Sudoku mid-game in both orientations."""
    def screens():
        invaders = app.InvadersApp(best=1200, seed=1)
        invaders.state, invaders.last = 'running', 0
        for step in range(40):
            invaders.render(step*.05, clock, ())
        mines = app.MinesApp(best=95, seed=3)
        mines.state, mines.last = 'running', 0
        mines.game.reveal((2, 2))
        for cell in sorted(mines.game.mines)[:3]:
            mines.game.toggle_flag(cell)
        solitaire = app.SolitaireApp(seed=5)
        solitaire.state, solitaire.last = 'running', 0
        solitaire.game.draw()
        solitaire.game.draw()
        solitaire.selected = ('tab', 6, 6)
        sudoku = app.SudokuApp(best=412, seed=2)
        sudoku.new_game(0)
        sudoku.state, sudoku.last = 'running', 0
        for i, value in enumerate(sudoku.game.solution[:20]):
            if not sudoku.game.given(i):
                sudoku.game.entries[i] = value
        sudoku.game.selected = 40
        return [invaders.render(2.05, clock, ()), mines.render(1, clock, ()),
                solitaire.render(1, clock, ()), sudoku.render(1, clock, ())]
    try:
        save_sheet(screens(), 'puzzles-landscape-preview.png', cols=2)
        app.set_rotation(90)
        save_sheet(screens(), 'puzzles-portrait-preview.png', cols=4)
    finally:
        app.set_rotation(0)


if __name__ == '__main__':
    main()
