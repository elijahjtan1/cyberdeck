"""Render the new tools without Pi hardware. Sample network figures are illustrative."""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image, ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import panel_app as app
from panel_tools.ui import ToolsUI
from panel_tools.updates import Updates


def main():
    # Windows review uses matching font sizes instead of PIL's tiny bitmap fallback.
    # The Pi itself continues to use DejaVu Sans from the original app.
    windows_font=Path('C:/Windows/Fonts/arial.ttf')
    if windows_font.exists():
        app.FONT=ImageFont.truetype(str(windows_font),15)
        app.SMALL=ImageFont.truetype(str(windows_font),12)
        app.TINY=ImageFont.truetype(str(windows_font),10)
    target=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'docs'/'previews'
    target.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary, patch.object(Updates,'git',return_value='8251f19'):
        data=Path(temporary); photos=data/'photos'; photos.mkdir()
        audio=Mock(); audio.state={'volume':35,'backend':'PipeWire'}
        ui=ToolsUI(app,data,ROOT,app.PhotoLibrary(photos),dict(app.DEFAULT_OPTIONS),audio,{})
        ui.rows={'ip':'wlan0 192.168.1.42','wifi':'wlan0: -58 dBm'}
        ui.network.samples.extend((3000+i%10*1200,500+i%4*400,-70+i%15) for i in range(120))
        ui.network.rx,ui.network.tx=12000,2400
        ui.network.available=True
        try:
            for rotation in (0,90):
                app.set_rotation(rotation)
                pages=('tools','quick','timelapse','timers','network','presets','recovery','updates','gnss')
                canvas=Image.new('RGB',(app.W*3,app.H*3),'#080e14')
                for i,page in enumerate(pages):
                    ui.page=page
                    canvas.paste(ui.render(10,datetime(2026,10,10,12,30),()),(i%3*app.W,i//3*app.H))
                canvas.save(target/f'tools-{rotation}.png')
            # Sun & moon: landscape without and with a saved location; portrait with one,
            # then with the clock not synced.
            ui.page,sheet,x='sky',Image.new('RGB',(320*2+240*2+18,320),'#080e14'),0
            when=datetime(2026,10,21,21,0,tzinfo=timezone(timedelta(hours=1)))  # London summer time
            london='{"lat": 51.51, "lon": -0.13}'
            for rotation,location,sync in ((0,None,'Synchronized'),(0,london,'Synchronized'),
                                           (90,london,'Synchronized'),(90,london,'Not synchronized')):
                app.set_rotation(rotation)
                ui.rows={**ui.rows,'sync':sync}
                path=data/'location.json'
                path.unlink(missing_ok=True)
                if location: path.write_text(location)
                sheet.paste(ui.render(10,when,()),(x,0)); x+=app.W+6
            sheet.save(target/'sky-preview.png')
        finally:
            ui.close(); app.set_rotation(0)


if __name__=='__main__': main()
