"""Small-screen tools, kept separate from the camera/saver rendering code."""
import json
import math
import re
import shutil
from PIL import Image, ImageDraw, ImageOps
from .runtime import Clocks, Timelapse, Job, atomic_json
from .network import NetworkHistory, PhotoShare
from .updates import Updates
from . import sky


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'


class ToolsUI:
    delay = .2
    def __init__(self, api, data, repo, library, options, audio, callbacks):
        self.a, self.data, self.library = api, data, library
        self.options, self.audio, self.callbacks = options, audio, callbacks
        self.page, self.menu = 'tools', 0
        self.message = ''
        self.rects = []
        self.clocks, self.lapse = Clocks(), Timelapse()
        self.network, self.share = NetworkHistory(), PhotoShare()
        self.job = Job()
        self.updates = Updates(repo, data, api.user_owns)
        self.rows, self.camera = {}, None
        self.next_network = 0
        self.timer_seconds = 300
        self.selected = set()
        self.share_index = 0
        self.preset_index = 0
        self.presets_path = data/'presets.json'
        try:
            self.presets = json.loads(self.presets_path.read_text())
            if not isinstance(self.presets, dict):
                self.presets = {}
        except (OSError, ValueError):
            self.presets = {}
        self.zoom_image = None
        self.zoom_cache = None
        self.zoom = 1
        self.cx = self.cy = .5
        self.confirm = None

    def tick(self, now, rows, camera):
        self.rows, self.camera = rows, camera
        self.clocks.tick(now)
        self.share.tick(now)
        if now >= self.next_network:
            match = re.search(r'(-?\d+)\s*dBm', rows.get('wifi', ''))
            self.network.sample(now, float(match[1]) if match else None)
            self.next_network = now+1
        result = self.job.consume()
        if result:
            value, error = result
            self.message = error or str(value)
            if error:
                print('Panel tool failed:', error, flush=True)
        if self.lapse.due(now) and not self.callbacks['busy']():
            if not self.callbacks['snap'](True):
                self.lapse.completed(now, 'Camera unavailable; timelapse stopped')

    def open_zoom(self, path):
        # Decode on the generic job thread; never decode full-res on every redraw.
        if self.job.busy or self.job.done:
            self.message = 'Wait for the current operation'
            return False
        self.zoom_image = None
        self.zoom, self.cx, self.cy, self.page = 1, .5, .5, 'zoom'
        def load():
            with Image.open(path) as image:
                self.zoom_image = ImageOps.exif_transpose(image).convert('RGB')
            return 'Use +/- and arrows to inspect'
        self.job.start(load)
        return True

    def render(self, now, wall_time, alerts):
        a = self.a
        image = Image.new('RGB', (a.W, a.H), a.BG)
        d = ImageDraw.Draw(image)
        self.rects = []
        title = f'PHOTO {self.zoom:g}x' if self.page == 'zoom' else self.page.upper()
        a.centred(d, 12, title, a.FONT, a.ACCENT)
        if alerts:
            a.draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        bottom = a.H-40
        def line(y, text, colour=None):
            a.centred(d, y, text, a.SMALL, colour or a.TEXT)
        def button(rect, label, action, active=False):
            a.button(d, rect, label, active=active)
            self.rects.append((rect, action))
        def grid(items, top=70, end=None):
            for rect, (label, action) in zip(a.grid_rects(len(items), top, end or bottom-5, max_pitch=64), items):
                button(rect, label, action)
        def graph(values, rect, colours):
            x0,y0,x1,y1 = rect
            d.rectangle(rect, fill='#0b1218')
            maximum = max([v for series in values for v in series if v is not None] or [1]) or 1
            for series, colour in zip(values, colours):
                points = []
                for i, v in enumerate(series):
                    if v is None:
                        if len(points)>1: d.line(points, fill=colour, width=2)
                        points=[]
                    else:
                        points.append((x0+i*(x1-x0)/max(1,len(series)-1), y1-(y1-y0)*v/maximum))
                if len(points)>1: d.line(points, fill=colour, width=2)
        if self.page == 'tools':
            line(38, wall_time.strftime('%Y/%m/%d  %H:%M:%S'))
            items = [('Quick controls','go:quick'), ('Timelapse','go:timelapse'),
                     ('Timers','go:timers'), ('Network','go:network'), ('Photo sharing','go:share'),
                     ('Presets','go:presets'), ('Recovery','go:recovery'), ('Updates','go:updates'),
                     ('GNSS','go:gnss'), ('Sun & moon','go:sky')]
            grid(items[self.menu*6:self.menu*6+6], 56, bottom-38)
            button((8,bottom-37,a.W-9,bottom-8), 'MORE >' if not self.menu else '< FIRST PAGE', 'more')
        elif self.page == 'quick':
            volume = self.audio.state.get('volume')
            line(40, f"Volume: {volume if volume is not None else '--'}%"+
                 (' / muted' if self.audio.state.get('muted') else ''))
            grid([('VOL -','audio:down'), ('VOL +','audio:up'), ('Mute','audio:mute'),
                  ('Take photo','snap'), ('Screensaver','saver'), ('Shut down...','shutdown')], 58)
        elif self.page == 'timelapse':
            lapse = self.lapse
            line(39, f'{lapse.saved} saved / up to {lapse.total} shots')
            remaining = max(0,lapse.started+lapse.duration-now) if lapse.active else lapse.duration
            line(58, f'{duration(remaining)} remaining' if lapse.active else 'Saves individual full-size JPEGs')
            sizes = [p.stat().st_size for p in self.library.items[-8:] if p.exists()]
            estimate = (sum(sizes)/len(sizes) if sizes else 5*2**20)*lapse.total/2**20
            free = shutil.disk_usage(self.data).free / 2**30
            line(77, f'Est. ~{estimate:.0f} MB / free {free:.1f} GB')
            width = a.W-16
            d.rectangle((8,90,a.W-8,98), fill=a.PANEL)
            d.rectangle((8,90,8+width*min(1,lapse.saved/lapse.total),98),fill=a.ACCENT)
            grid([(f'Interval {lapse.interval}s','interval'), (f'Length {lapse.duration//60}m','length'),
                  ('STOP' if lapse.active else 'START','lapse')], 112, bottom-30)
            line(bottom-19, lapse.error or ('Capturing in background' if lapse.active else 'Ready'), a.WARN)
        elif self.page == 'timers':
            line(42, 'Stopwatch '+duration(self.clocks.stopwatch(now)))
            line(64, 'Countdown '+duration(self.clocks.deadline-now if self.clocks.deadline else self.timer_seconds))
            grid([('Pause / Start','watch'), ('Reset watch','resetwatch'),
                  (f'Length {self.timer_seconds//60}m','timerlength'),
                  ('Cancel timer' if self.clocks.deadline else 'Start timer','timer'),
                  ('Dismiss alert','ack')], 85)
        elif self.page == 'network':
            line(38, self.rows.get('ip','No address'))
            line(57, self.rows.get('wifi','Wi-Fi unavailable'))
            line(76, f'Down {self.network.rx/1024:.1f} / Up {self.network.tx/1024:.1f} KiB/s'
                 if self.network.available else 'Network counters unavailable')
            samples = list(self.network.samples)
            mid = (bottom+98)//2
            graph([[v[0] for v in samples],[v[1] for v in samples]], (8,90,a.W-9,mid-8),[a.ACCENT,a.WARN])
            line(mid+4, 'Traffic: green down / orange up',a.MUTED)
            graph([[None if v[2] is None else max(0,min(70,v[2]+100)) for v in samples]],
                  (8,mid+18,a.W-9,bottom-23),['#70aaff'])
            line(bottom-11,'Wi-Fi signal / last 120 seconds',a.MUTED)
        elif self.page == 'share':
            if self.share.server:
                address = self.rows.get('ip','').split()
                ip = next((s for s in address if re.fullmatch(r'\d+\.\d+\.\d+\.\d+',s)), '<Pi-IP>')
                line(43, f'{len(self.share.files)} photos / {math.ceil((self.share.expires-now)/60)} min left')
                line(67, 'Open on the same trusted Wi-Fi:')
                line(91, f'http://{ip}:{self.share.port}/', a.ACCENT)
                line(111, self.share.token+'/', a.ACCENT)
                grid([('Stop sharing','stopshare')], 142)
            else:
                items = self.library.items
                if items:
                    self.share_index %= len(items)
                    path = items[self.share_index]
                    line(39, f'Photo {self.share_index+1}/{len(items)} / {len(self.selected)} selected')
                    line(58, self.a.photo_label(path))
                    grid([('< Older','shareprev'), ('Newer >','sharenext'),
                          ('Deselect' if path in self.selected else 'Select photo','select'),
                          ('Start sharing','startshare')], 78, bottom-33)
                else:
                    line(60,'Take a photo first')
                line(bottom-21,'Private LAN only / 10 min / read-only',a.MUTED)
        elif self.page == 'presets':
            name = ('Camera','Desk','Quiet')[self.preset_index]
            line(43,name+(' / saved' if name in self.presets else ' / factory defaults'))
            line(64,'Pixel dim, idle, camera, volume')
            grid([('Next preset','presetnext'), ('Apply preset','presetapply'),
                  ('Save current...','presetsave')],86)
        elif self.page == 'recovery':
            line(41,'Camera: '+('ready' if self.camera and self.camera.ready else 'unavailable'))
            line(61,self.camera.error if self.camera else 'Camera disabled')
            line(81,self.audio.state.get('backend','Audio unavailable'))
            grid([('Retry camera','retrycamera'), ('Retry audio','retryaudio')],104,bottom-40)
        elif self.page == 'updates':
            line(40,'Running: '+self.updates.running)
            line(60,'Explicit check / no automatic updates')
            grid([('Check GitHub','check'), ('Update...','update'),
                  ('Restart app...','restart'), ('Roll back...','rollback')],80,bottom-43)
        elif self.page == 'gnss':
            line(48,'NO RECEIVER CONNECTED',a.WARN)
            for y,text in zip(range(79,174,23),('Position: --','Satellites: -- / Fix: --',
                                               'Speed: -- / Altitude: --','Waiting for your GNSS module')):
                line(y,text)
        elif self.page == 'sky':
            # Read on each draw: the file is tiny and editing it takes effect at once.
            info = sky.sky_lines(wall_time, sky.load_location(self.data/'location.json'),
                                 self.rows.get('sync', 'Unknown'))
            line(38, info['clock'] or info['where'], a.WARN if info['clock'] else a.MUTED)
            rows = info['sun'] or ['Sun times need a position:',
                                   'save it in cyberdeck-data/location.json']
            if info['clock']:  # The warning takes the top line; the position moves down.
                rows = [info['where']] + rows
            for y, text in zip(range(56, 132, 19), rows):
                line(y, text, a.MUTED if text == info['where'] else None if info['sun'] else a.WARN)
            moon_y = max(126 if info['clock'] else 122, (bottom+120)//2-26)
            # The southern hemisphere sees the moon upside down: lit from the left while waxing.
            self.draw_moon(d, (a.W//2, moon_y+16), 15, info['lit'],
                           info['waxing'] != info.get('south', False))
            line(moon_y+40, info['moon'])
            line(moon_y+59, info['next'], a.MUTED)
        elif self.page == 'zoom':
            source = self.zoom_image
            if source:
                vw,vh=a.W,bottom-76
                scale=min(vw/source.width,vh/source.height)*self.zoom
                cw,ch=min(source.width,vw/scale),min(source.height,vh/scale)
                left=max(0,min(source.width-cw,self.cx*source.width-cw/2))
                top=max(0,min(source.height-ch,self.cy*source.height-ch/2))
                key = (id(source), vw, vh, self.zoom, self.cx, self.cy)
                if self.zoom_cache is None or self.zoom_cache[0] != key:
                    crop=source.crop((int(left),int(top),int(left+cw),int(top+ch)))
                    self.zoom_cache = (key, ImageOps.contain(crop,(vw,vh)))
                view = self.zoom_cache[1]
                image.paste(view,((vw-view.width)//2,27+(vh-view.height)//2))
            else:
                line(60,'Loading full-size photo...')
            for rect,(label,action) in zip(a.row_rects(6,bottom-37,bottom-6),
                                         [('-','zoomout'),('+','zoomin'),('<','left'),('>','right'),('^','up'),('v','down')]):
                button(rect,label,action)
        if self.page in ('updates','recovery','presets','share','quick'):
            line(bottom-20, 'Working...' if self.job.busy else self.message,a.WARN)
        if self.page == 'zoom' and self.zoom_image is None and not self.job.busy:
            line(90, self.message, a.WARN)
        button((6,a.H-34,a.W//2-4,a.H-5),'BACK','back')
        button((a.W//2+4,a.H-34,a.W-7,a.H-5),'CAMERA','camera')
        if self.confirm:
            # Confirmation entirely replaces earlier hotspots.
            self.rects=[]
            d.rectangle((0,25,a.W-1,a.H-1),fill=a.BG)
            line(62,self.confirm[0],a.WARN)
            for i,text in enumerate(self.confirm[2]): line(89+i*19,text)
            button((8,a.H-65,a.W//2-5,a.H-15),'CANCEL','cancel')
            button((a.W//2+5,a.H-65,a.W-9,a.H-15),'CONFIRM','confirm')
        return image

    def draw_moon(self, d, centre, radius, lit, right):
        """Moon disc with `lit` of it lit, from the right side when `right` is true."""
        cx, cy = centre
        d.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill='#26323c')
        for dy in range(-radius, radius+1):
            half = math.sqrt(max(0, radius*radius - dy*dy))
            edge = half*(1-2*lit)  # Terminator: the lit part is beyond it.
            x0, x1 = (cx+edge, cx+half) if right else (cx-half, cx-edge)
            if x1 > x0:
                d.line((x0, cy+dy, x1, cy+dy), fill='#e8e4d0')

    def tap(self,x,y,now):
        action = next((action for (x0,y0,x1,y1),action in reversed(self.rects)
                       if x0<=x<=x1 and y0<=y<=y1),None)
        if not action: return
        if action=='cancel': self.confirm=None; return
        if action=='confirm':
            action=self.confirm[1]
            self.confirm=None
        elif action in ('update','rollback','restart','presetsave'):
            descriptions = {'update':('Install checked update?', ['Saves rollback revision.', 'Restart separately to load it.']),
                            'rollback':('Restore previous code?', ['Keeps photos and settings.', 'Leaves Git in detached mode.']),
                            'restart':('Restart this app?', ['Stops timers and timelapse.', 'Camera will reconnect.']),
                            'presetsave':('Replace this preset?', ['Stores your current settings.'])}
            title,lines=descriptions[action]
            self.confirm=(title,action,lines); return
        if action.startswith('go:'):
            self.page=action[3:]; self.message=''
        elif action=='more': self.menu=1-self.menu
        elif action=='back':
            if self.page=='tools': return 'exit'
            if self.page=='zoom':
                self.zoom_image = self.zoom_cache = None
                self.page='tools'
                return 'photos'
            self.page='tools'
        elif action in ('camera','shutdown','saver'):
            if self.page == 'zoom':
                self.zoom_image = self.zoom_cache = None
            return action
        elif action.startswith('audio:'): self.audio.request(action[6:])
        elif action=='snap':
            self.message='Capturing...' if self.callbacks['snap'](False) else 'Camera busy or unavailable'
        elif action=='watch': self.clocks.toggle(now)
        elif action=='resetwatch': self.clocks.started=None; self.clocks.elapsed=0
        elif action=='timerlength': self.timer_seconds={60:300,300:600,600:1500,1500:3600,3600:60}[self.timer_seconds]
        elif action=='timer':
            if self.clocks.deadline: self.clocks.deadline=None
            else: self.clocks.countdown(self.timer_seconds,now)
        elif action=='ack': self.clocks.alarm=False
        elif action=='interval' and not self.lapse.active:
            self.lapse.interval={5:10,10:30,30:60,60:5}[self.lapse.interval]
        elif action=='length' and not self.lapse.active:
            self.lapse.duration={60:300,300:900,900:1800,1800:3600,3600:60}[self.lapse.duration]
        elif action=='lapse':
            if self.lapse.active: self.lapse.active=False
            elif self.camera and self.camera.ready and not self.callbacks['busy'](): self.lapse.start(now)
            else: self.lapse.error='Camera busy or unavailable'
        elif action=='shareprev': self.share_index=max(0,self.share_index-1)
        elif action=='sharenext': self.share_index=min(len(self.library.items)-1,self.share_index+1)
        elif action=='select' and self.library.items:
            path=self.library.items[self.share_index % len(self.library.items)]
            if path in self.selected: self.selected.remove(path)
            else: self.selected.add(path)
        elif action=='startshare':
            try:
                self.share.start(sorted(self.selected),self.library.folder)
                self.message='Session enabled'
            except (OSError,RuntimeError) as exc: self.message=str(exc)
        elif action=='stopshare': self.share.close(); self.message='Sharing stopped'
        elif action=='presetnext': self.preset_index=(self.preset_index+1)%3
        elif action in ('presetapply','presetsave'):
            name=('Camera','Desk','Quiet')[self.preset_index]
            if action=='presetsave':
                value={'options':dict(self.options),'fps':self.callbacks['fps'](),
                       'volume':self.audio.state.get('volume'),'muted':self.audio.state.get('muted',False)}
                try:
                    updated={**self.presets,name:value}
                    atomic_json(self.presets_path,updated,self.a.user_owns)
                    self.presets=updated; self.message='Preset saved'
                except OSError as exc: self.message=str(exc)
            else:
                defaults={'Camera':{'pixel_level':100,'saver_style':'off'},
                          'Desk':{'pixel_level':70,'saver_style':'clock','saver_delay':300},
                          'Quiet':{'pixel_level':40,'saver_style':'black','saver_delay':30}}
                self.message=self.callbacks['preset'](self.presets.get(name,{'options':defaults[name],
                                                     'fps':20,'volume':30,'muted':name=='Quiet'}))
        elif action=='retrycamera': self.callbacks['retry'](); self.message='Reconnecting camera...'
        elif action=='retryaudio': self.audio.request('detect'); self.message='Checking audio again...'
        elif action in ('check','update','rollback'):
            fn={'check':self.updates.check,'update':self.updates.apply,'rollback':self.updates.rollback}[action]
            if not self.job.start(fn): self.message='Wait for the current operation'
        elif action=='restart':
            if not self.job.busy and not self.callbacks['busy'](): return 'restart'
            self.message='Wait for the current operation'
        elif action in ('zoomin','zoomout'): self.zoom=max(1,min(64,self.zoom*(2 if action=='zoomin' else .5)))
        elif action in ('left','right','up','down'):
            dx,dy={'left':(-1,0),'right':(1,0),'up':(0,-1),'down':(0,1)}[action]
            self.cx=max(0,min(1,self.cx+dx*.25/self.zoom))
            self.cy=max(0,min(1,self.cy+dy*.25/self.zoom))

    def close(self):
        self.share.close()
        # Never restart/exit midway through a repository mutation.
        if self.job.thread and self.job.thread.is_alive():
            self.job.thread.join()
