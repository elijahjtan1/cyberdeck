import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import panel_app as app
from panel_tools.runtime import CameraWorker, Clocks, Timelapse, Job, data_home
from panel_tools.network import NetworkHistory, PhotoShare
from panel_tools.updates import Updates
from panel_tools.ui import ToolsUI
from PIL import Image


class RuntimeTest(unittest.TestCase):
    def test_migration_copies_without_deleting_or_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); repo=root/'repo'; home=root/'home'
            repo.mkdir(); home.mkdir()
            old=repo/'cyberdeck-data'; old.mkdir()
            (old/'touch.json').write_text('original')
            target=data_home(None,repo/'panel_app.py',home)
            self.assertEqual((target/'touch.json').read_text(),'original')
            (target/'touch.json').write_text('new')
            self.assertEqual(data_home(None,repo/'panel_app.py',home),target)
            self.assertEqual((target/'touch.json').read_text(),'new')
            self.assertEqual((old/'touch.json').read_text(),'original')
            self.assertEqual(data_home(str(old),repo/'panel_app.py',home),old.resolve())

    def test_clocks_and_no_catchup_timelapse(self):
        clock=Clocks(); clock.toggle(10); clock.toggle(15)
        self.assertEqual(clock.stopwatch(100),5)
        clock.countdown(30,100); clock.tick(129); self.assertFalse(clock.alarm)
        clock.tick(130); self.assertTrue(clock.alarm); self.assertIsNone(clock.deadline)
        lapse=Timelapse(); lapse.interval=5; lapse.duration=60; lapse.start(10)
        self.assertTrue(lapse.due(10)); lapse.completed(22)
        self.assertFalse(lapse.due(26)); self.assertTrue(lapse.due(27))
        lapse.completed(30,'disk full'); self.assertFalse(lapse.active)
        lapse.start(40); self.assertFalse(lapse.due(100))

    def test_camera_owner_serializes_and_does_not_block_ui(self):
        entered=threading.Event(); release=threading.Event(); threads=[]
        class Driver:
            def __init__(self,fps): threads.append(threading.get_ident())
            def frame(self): threads.append(threading.get_ident()); return 'preview'
            def close(self): threads.append(threading.get_ident())
        worker=CameraWorker(Driver,20)
        try:
            deadline=time.monotonic()+2
            while not worker.ready and time.monotonic()<deadline: time.sleep(.005)
            def shot(driver):
                threads.append(threading.get_ident()); entered.set(); release.wait(2); return 'saved'
            self.assertTrue(worker.snapshot(shot)); self.assertTrue(entered.wait(2))
            self.assertFalse(worker.snapshot(shot))
            start=time.monotonic(); worker.frame()
            self.assertLess(time.monotonic()-start,.1)
            release.set(); self.assertEqual(worker.results.get(timeout=2),('saved',None))
        finally: release.set(); worker.close()
        self.assertEqual(len(set(threads)),1)
        self.assertNotEqual(threads[0],threading.get_ident())

    def test_camera_failure_can_retry(self):
        driver=mock.Mock(); driver.frame.return_value=None
        factory=mock.Mock(side_effect=[RuntimeError('disconnected'),driver])
        worker=CameraWorker(factory,20)
        try:
            deadline=time.monotonic()+2
            while not worker.error and time.monotonic()<deadline: time.sleep(.005)
            self.assertFalse(worker.ready); worker.retry()
            while not worker.ready and time.monotonic()<deadline: time.sleep(.005)
            self.assertTrue(worker.ready)
        finally: worker.close()


class NetworkTest(unittest.TestCase):
    def test_rates_ignore_reset_and_new_interface(self):
        def text(rx,tx): return f'eth0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0'
        n=NetworkHistory(); n.sample(1,-60,text(100,200)); n.sample(3,-65,text(300,600))
        self.assertEqual((n.rx,n.tx),(100,200))
        n.sample(4,None,text(0,0)); self.assertEqual((n.rx,n.tx),(0,0))
        self.assertEqual(n.samples[-1][2],None)

    def test_share_allowlist_token_expiry_and_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); photo=root/'a.jpg'; photo.write_bytes(b'photo')
            hidden=root/'secret.txt'; hidden.write_text('secret')
            share=PhotoShare()
            try:
                share.start([photo,hidden],root,host='127.0.0.1',port=0)
                base=f'http://127.0.0.1:{share.port}/'
                self.assertEqual(urlopen(base+share.token+'/0').read(),b'photo')
                for path in ('', 'wrong/',share.token+'/1',share.token+'/../secret.txt'):
                    with self.assertRaises(HTTPError) as error: urlopen(base+path)
                    self.assertEqual(error.exception.code,404)
                self.assertEqual(len(share.files),1)
                share.tick(share.expires+1); self.assertIsNone(share.server)
            finally: share.close()


class UpdatesTest(unittest.TestCase):
    def test_update_rejects_invalid_python_before_mutating(self):
        updates=Updates.__new__(Updates)
        updates.target='new'; updates.branch='main'
        def git(*args, **kwargs):
            if args == ('status','--porcelain'): return ''
            if args[0] == 'symbolic-ref': return 'main'
            if args[0] in ('rev-parse','merge-base'): return 'old'
            if args[0] == 'ls-tree': return 'panel_app.py'
            if args[0] == 'show': return 'def invalid('
            raise AssertionError('Unexpected mutation: '+str(args))
        updates.git=git
        with self.assertRaises(SyntaxError): updates.apply()

    def test_real_fastforward_and_nondestructive_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source'; local=root/'local'; data=root/'data'
            source.mkdir(); data.mkdir()
            def git(folder,*args):
                return subprocess.check_output(['git','-C',str(folder),*args],text=True,stderr=subprocess.DEVNULL).strip()
            git(source,'init','-b','main'); git(source,'config','user.email','test@example.invalid')
            git(source,'config','user.name','Test')
            (source/'app.txt').write_text('one'); git(source,'add','.'); git(source,'commit','-m','one')
            old=git(source,'rev-parse','HEAD')
            git(root,'clone',str(source),str(local))
            (source/'app.txt').write_text('two'); git(source,'commit','-am','two')
            updates=Updates(local,data)
            self.assertIn('Available',updates.check()); updates.apply()
            self.assertEqual((local/'app.txt').read_text(),'two')
            (local/'app.txt').write_text('local work')
            with self.assertRaises(RuntimeError): updates.rollback()
            (local/'app.txt').write_text('two'); updates.rollback()
            self.assertEqual(git(local,'rev-parse','HEAD'),old)
            self.assertEqual((local/'app.txt').read_text(),'one')
            self.assertNotEqual(git(local,'rev-parse','main'),old) # Branch history kept.


class ToolsLayoutTest(unittest.TestCase):
    def test_all_pages_fit_rotations_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Updates,'git',return_value='abc123'):
            data=Path(directory); photos=data/'photos'; photos.mkdir()
            library=app.PhotoLibrary(photos)
            audio=mock.Mock(); audio.state={'volume':35,'backend':'PipeWire'}
            ui=ToolsUI(app,data,data,library,dict(app.DEFAULT_OPTIONS),audio,{})
            ui.zoom_image=Image.new('RGB',(1640,1232),'red')
            pages=('tools','quick','timelapse','timers','network','share','presets','recovery','updates','gnss','zoom')
            try:
                for rotation in (0,90,180,270):
                    app.set_rotation(rotation)
                    for page in pages:
                        ui.page=page
                        frame=ui.render(10,datetime.now(),())
                        self.assertEqual(frame.size,(app.W,app.H))
                        for (x0,y0,x1,y1),action in ui.rects:
                            self.assertTrue(0<=x0<x1<app.W and 0<=y0<y1<app.H,(rotation,page,action))
                    ui.confirm=('Restart?', 'restart',['Confirm restart'])
                    ui.render(10,datetime.now(),())
                    self.assertEqual([a for _,a in ui.rects],['cancel','confirm'])
                    ui.confirm=None
            finally: ui.close(); app.set_rotation(0)

    def test_actions_timers_presets_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Updates,'git',return_value='abc123'):
            data=Path(directory); photos=data/'photos'; photos.mkdir()
            audio=mock.Mock(); audio.state={'volume':45,'muted':True}
            callbacks={'fps':lambda:15,'busy':lambda:False,'preset':mock.Mock(return_value='Applied')}
            ui=ToolsUI(app,data,data,app.PhotoLibrary(photos),dict(app.DEFAULT_OPTIONS),audio,callbacks)
            def tap(action,now=10):
                ui.render(now,datetime.now(),())
                rect=next(r for r,a in ui.rects if a==action)
                return ui.tap((rect[0]+rect[2])/2,(rect[1]+rect[3])/2,now)
            try:
                ui.page='timers'; tap('timer')
                ui.page='gnss'; ui.tick(310,{},None)
                self.assertTrue(ui.clocks.alarm)
                ui.page='presets'; tap('presetsave')
                self.assertFalse(ui.presets_path.exists())
                tap('confirm')
                saved=json.loads(ui.presets_path.read_text())['Camera']
                self.assertEqual((saved['volume'],saved['fps'],saved['muted']),(45,15,True))
                tap('presetapply'); callbacks['preset'].assert_called_once_with(saved)
                tap('presetnext'); tap('presetapply')  # Unsaved preset: factory settings.
                callbacks['preset'].assert_called_with({'options':{'pixel_level':70,'saver_style':'clock',
                                                        'saver_delay':300},'fps':20,'volume':30,'muted':False})
                ui.page='updates'; tap('restart')
                self.assertIsNotNone(ui.confirm); tap('cancel')
                self.assertIsNone(ui.confirm)
            finally: ui.close()


if __name__=='__main__': unittest.main()
