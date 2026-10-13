#!/usr/bin/env python3
"""Render sample screens (illustrative data, no hardware) into docs/previews/."""
import sys
import threading
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
    history_screens(clock, rows)
    planner_screens(clock)
    reader_screens(clock)


def planner_screens(clock):
    """PLANNER app: month, day, new-entry and time views with sample entries."""
    import tempfile
    from datetime import date
    from panel_tools.planner import PlannerApp

    sheets = []
    with tempfile.TemporaryDirectory() as folder:
        page = PlannerApp(app, Path(folder), lambda: 'Synchronized')
        for day, text, time in ((9, 'Check the antenna mast', '10:00'), (9, 'Charge the power bank', ''),
                                (9, 'Radio net', '19:30'), (14, 'Market', '09:00'), (21, 'Hike', '')):
            page.planner.add(date(2026, 10, day), text, time)
        for rotation in (0, 90):
            app.set_rotation(rotation)
            try:
                screens = []
                for view in ('month', 'day', 'type', 'time'):
                    page.view, page.day, page.text, page.time = view, clock.date(), 'Refill water', '20:00'
                    screens.append(page.render(0, clock, ()))
                sheets.append(screens)
            finally:
                app.set_rotation(0)
    save_sheet(sheets[0], 'planner-landscape-preview.png', cols=4)
    save_sheet(sheets[1], 'planner-portrait-preview.png', cols=4)


def reader_screens(clock):
    """READER app: keyboard, results and an article page, from a canned backend."""
    from panel_tools.reader import ReaderApp

    results = [{'title': 'Amateur radio', 'url': 'http://127.0.0.1:8080/content/wiki/A/Amateur_radio',
                'source': 'kiwix'},
               {'title': 'Antenna (radio)', 'url': 'http://127.0.0.1:8080/content/wiki/A/Antenna', 'source': 'kiwix'},
               {'title': 'Shortwave radio', 'url': 'http://127.0.0.1:8080/content/wiki/A/Shortwave', 'source': 'kiwix'},
               {'title': 'antenna-notes.txt', 'url': '/api/library/antenna-notes.txt', 'source': 'local'}]
    paragraphs = ['Amateur radio, also known as ham radio, is the use of the radio frequency spectrum '
                  'for purposes of non-commercial exchange of messages, wireless experimentation, '
                  'self-training, private recreation, radiosport, contesting, and emergency communications.',
                  'The term "amateur" is used to specify a person who has an interest in radio '
                  'electrical phenomena with no direct monetary or other similar reward.',
                  'Radio amateurs use a variety of voice, text, image, and data communications modes '
                  'and have access to frequency allocations throughout the RF spectrum.']
    sheets = []
    for rotation in (0, 90):
        app.set_rotation(rotation)
        try:
            page = ReaderApp(app, 'http://127.0.0.1:8765')
            page.query = 'ham radio'
            screens = [page.render(0, clock, ())]
            page.view, page.results = 'results', results
            screens.append(page.render(0, clock, ()))
            page.view, page.title, page.paragraphs, page.truncated = 'article', 'Amateur radio', paragraphs, True
            screens.append(page.render(0, clock, ()))
            sheets.append(screens)
        finally:
            app.set_rotation(0)
    save_sheet(sheets[0], 'reader-landscape-preview.png', cols=3)
    save_sheet(sheets[1], 'reader-portrait-preview.png', cols=3)


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
    saver = app.BunnySaver({**options, 'bunny_count': 8}, lambda n: 'Saved.', seed=11)
    for step in range(150):  # Let some bunnies settle into snacks and naps first.
        saver.render(step*.1, clock, ())
    for step in range(150, 230):
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
    ask_screens(clock)
    bunny_breeds()


def home_screens(clock):
    rows = {'temp': '48.2 C', 'disk_free': '18.4 GiB'}
    notes = {'wifi': 'Wi-Fi -58 dBm', 'reader': 'Reading: AC and DC',
             'planner': 'Next: 19:30 Radio net', 'games': '9 games', 'audio': 'Vol 55%',
             'sos': 'Beacon, torch', 'apps': 'Album, GIFs', 'tools': 'Timers & more',
             'quick': 'Shortcuts', 'ask': 'Offline answers'}
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


def ask_screens(clock):
    """ASK page: keyboard, waiting for the first word, and a finished answer (sample text)."""
    from panel_tools.ask import AskApp

    def answer(question, base_url, opener=None, timeout=None):
        yield {'event': 'passages', 'model': 'qwen3:0.6b', 'passages': [{'n': 1}, {'n': 2}]}
        yield {'event': 'done', 'found': True, 'uncited': False, 'cited': [1], 'warnings': [],
               'answer': 'Most leaks come from a worn seal or O-ring around the water tank '
                         'or the carafe lid [1]. Check the gasket under the filter holder and '
                         'replace it if it is cracked or hard [1].',
               'sources': [{'n': 1, 'title': 'Coffee makers', 'source': 'kiwix'}]}

    sheets = []
    for rotation in (0, 90):
        app.set_rotation(rotation)
        try:
            times = iter((100, 144))
            page = AskApp(app, 'http://127.0.0.1:8765', events=answer, clock=lambda: next(times))
            page.question = 'why does my coffee maker leak'
            typing = page.render(0, clock, ())
            page.ask('Why does my coffee maker leak?')
            page.thread.join()
            done = page.render(0, clock, ())
            waiting = AskApp(app, 'http://127.0.0.1:8765', clock=lambda: 118)
            waiting.view, waiting.asked, waiting.stage = 'answer', page.asked, 'reading'
            waiting.started, waiting.passages, waiting.model = 100, 2, 'qwen3:0.6b'
            hold = threading.Event()  # A live worker, so the page shows STOP.
            waiting.thread = threading.Thread(target=hold.wait)
            waiting.thread.start()
            sheets.append([typing, waiting.render(0, clock, ()), done])
            hold.set()
        finally:
            app.set_rotation(0)
    save_sheet(sheets[0], 'ask-landscape-preview.png', cols=3)
    save_sheet(sheets[1], 'ask-portrait-preview.png', cols=3)


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



def bunny_breeds():
    """Every coat as a standard and a lop bunny, then the Flemish giant coats, labelled."""
    from PIL import ImageDraw
    coats = list(app.BUNNY_COATS)
    cell, scale, label = 48, 2, 16
    rows = [('Standard', 'standard', coats, 2), ('Lop', 'lop', coats, 2),
            ('Lionhead', 'lionhead', coats, 2),
            ('Rex', 'rex', list(app.BUNNY_BREEDS['rex'][3]), 2),
            ('Flemish giant', 'standard', list(app.BUNNY_BREEDS['giant'][3]), 3)]
    height = cell*scale + label
    sheet = Image.new('RGB', (len(coats)*cell*scale, len(rows)*height), (118, 176, 236))
    d = ImageDraw.Draw(sheet)
    for r, (title, shape, names, size) in enumerate(rows):
        top = r*height
        d.text((4, top+2), title, fill='#123', font=app.SMALL)
        for c, coat in enumerate(names):
            sit = app.bunny_look(shape, coat, size)[0]
            big = sit.resize((sit.width*scale, sit.height*scale), Image.NEAREST)
            x = c*cell*scale + (cell*scale - big.width)//2
            sheet.paste(big, (x, top + cell*scale - big.height), big)
            d.text((c*cell*scale + cell*scale//2, top + cell*scale + label//2), coat,
                   fill='#123', anchor='mm', font=app.TINY)
    out = ROOT / 'docs/previews/bunny-breeds.png'
    sheet.save(out, optimize=True)
    print(out)
    bunny_life()


def bunny_life():
    """A staged moment: snacking on carrots and hay, napping, grooming, a binky and the
    aftermath."""
    from datetime import datetime
    saver = app.BunnySaver({**app.DEFAULT_OPTIONS, 'bunny_count': 8}, lambda n: '', seed=2)
    cast = [(('standard', 'white', 2), 'eat', 'carrot', 1, 30, 176),
            (('lop', 'harlequin', 2), 'sleep', None, -1, 95, 186),
            (('lionhead', 'castor', 2), 'eat', 'hay', 1, 120, 200),
            (('rex', 'blue', 2), 'binky', None, -1, 205, 178),
            (('standard', 'sable', 3), 'sleep', None, 1, 170, 204),
            (('lionhead', 'white', 2), 'eat', 'carrot', -1, 290, 194),
            (('rex', 'lilac', 2), 'sleep', None, -1, 255, 172),
            (('lop', 'cinnamon', 2), 'groom', None, 1, 60, 206)]
    for bunny, (look, activity, food, facing, x, ground) in zip(saver.bunnies, cast):
        bunny.look, bunny.breed = look, 'giant' if look[2] == 3 else look[0]
        bunny.half = saver.poses_for(look)[0]['sit'].width // 2
        bunny.x, bunny.ground, bunny.direction, bunny.hop_start = x, ground, facing, None
        bunny.activity, bunny.food = activity, food
        bunny.activity_start, bunny.activity_until, bunny.rest_until = 0, 12, 99
        if activity == 'binky':  # Caught on the way up, body tilted into the leap.
            bunny.activity_start, bunny.activity_until = 3.83, 4.73
    saver.pellets = [[225, 181, 3], [228, 182, 3], [222, 183, 3]]
    saver.last = 3.9
    image = saver.render(4.1, datetime(2026, 10, 10, 15, 30), ())[0]
    out = ROOT / 'docs/previews/bunny-life.png'
    image.resize((640, 480), Image.NEAREST).save(out, optimize=True)
    print(out)


def history_screens(clock, rows):
    """STATUS -> HISTORY over a made-up day: warm afternoon, a few AI answers and one dip."""
    now, seconds = 100_000., 86400
    rng = np.random.default_rng(4)
    samples = []
    for i in range(240):
        t = now - seconds + i*seconds/240
        busy = 1. if 140 <= i < 150 or 200 <= i < 206 else 0.
        cpu = 6 + 4*rng.random() + 70*busy
        temp = 48 + 8*np.sin(np.pi*i/240) + 14*busy + rng.random()
        samples.append([t, cpu, temp, 30., 0, 0])
    samples[90:96] = []  # Backend restarted: a gap in the lines.
    events = [{'kind': 'ai', 'detail': '', 'start': now-seconds+140*360, 'end': now-seconds+150*360},
              {'kind': 'ai', 'detail': '', 'start': now-seconds+200*360, 'end': now-seconds+206*360},
              {'kind': 'search', 'detail': '', 'start': now-seconds+120*360, 'end': now-seconds+121*360},
              {'kind': 'camera', 'detail': '', 'start': now-seconds+60*360, 'end': now-seconds+60*360},
              {'kind': 'undervoltage', 'detail': '', 'start': now-seconds+203*360,
               'end': now-seconds+204*360}]
    data = {'now': now, 'seconds': seconds, 'samples': samples, 'events': events}
    online = {**rows, 'backend_online': True, 'history_range': seconds, 'history': data}
    common = dict(options=app.DEFAULT_OPTIONS, wall_time=clock)
    screens = [app.render('history', None, False, 20, {**online, 'busy': ('ai',)}, '', **common),
               app.render('history', None, False, 20, {**rows, 'backend_online': False}, '', **common)]
    save_sheet(screens, 'history-preview.png')


if __name__ == '__main__':
    main()
