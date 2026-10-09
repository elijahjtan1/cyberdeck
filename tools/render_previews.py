#!/usr/bin/env python3
"""Render sample screens (illustrative data, no hardware) into docs/previews/."""
import sys
from datetime import datetime
from pathlib import Path

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
    cols, gap, scale = 2, 8, 2
    rows_n = (len(screens)+cols-1)//cols
    sheet = Image.new('RGB', (cols*(640+gap)-gap, rows_n*(480+gap)-gap), '#05080b')
    for i, screen in enumerate(screens):
        big = screen.resize((320*scale, 240*scale), Image.NEAREST)
        sheet.paste(big, ((i % cols)*(640+gap), (i//cols)*(480+gap)))
    out = ROOT / 'docs/previews/camera-photos-power-preview.png'
    sheet.save(out, optimize=True)
    print(out)


if __name__ == '__main__':
    main()
