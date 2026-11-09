"""CAST//OPS - cast a link to one or both Chromecast TVs. Kivy UI, standalone (no Termux)."""
import re
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty
from kivy.resources import resource_find
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup

import castlib

FONT = resource_find('data/fonts/RobotoMono-Regular.ttf') or 'Roboto'
BG = (0.02, 0.03, 0.02, 1)
GREEN = (0.22, 1.0, 0.08, 1)
AMBER = (1.0, 0.69, 0.0, 1)
RED = (1.0, 0.23, 0.19, 1)
DIM = (0.12, 0.42, 0.12, 1)
OLIVE = (0.05, 0.09, 0.04, 1)


def ui(fn, *a):
    """Run fn(*a) on the UI thread."""
    Clock.schedule_once(lambda dt: fn(*a), 0)


def lbl(text='', color=GREEN, **kw):
    kw.setdefault('halign', 'left')
    kw.setdefault('valign', 'middle')
    w = Label(text=text, color=color, font_name=FONT, **kw)
    w.bind(size=lambda o, s: setattr(o, 'text_size', s))
    return w


def field(hint, **kw):
    return TextInput(
        hint_text=hint, multiline=False, font_name=FONT, font_size=sp(14),
        foreground_color=GREEN, hint_text_color=(0.2, 0.5, 0.2, 1), cursor_color=GREEN,
        background_normal='', background_active='', background_color=(0.04, 0.09, 0.04, 1),
        padding=[dp(10), dp(14), dp(10), dp(10)], **kw)


class Neon(ButtonBehavior, Label):
    """Flat outlined button; fills solid while pressed (or switched on if toggle=True)."""
    active = BooleanProperty(False)

    def __init__(self, col=GREEN, toggle=False, **kw):
        kw.setdefault('font_name', FONT)
        kw.setdefault('bold', True)
        kw.setdefault('halign', 'center')
        kw.setdefault('valign', 'middle')
        super().__init__(**kw)
        self.col, self.toggle = col, toggle
        with self.canvas.before:
            self._bg = Color(*col[:3], 0.1)
            self._rect = Rectangle()
            Color(*col)
            self._ln = Line(width=1.2)
        self.bind(pos=self._draw, size=self._draw, state=self._draw, active=self._draw)
        self.bind(size=lambda o, s: setattr(o, 'text_size', (s[0] - dp(8), s[1])))
        self._draw()

    def on_release(self):
        if self.toggle:
            self.active = not self.active

    def _draw(self, *_):
        hot = self.state == 'down' or self.active
        self._bg.rgba = (*self.col[:3], 0.9 if hot else 0.1)
        self._rect.pos, self._rect.size = self.pos, self.size
        self._ln.rectangle = (self.x, self.y, self.width, self.height)
        self.color = (0, 0, 0, 1) if hot else self.col


class Panel(BoxLayout):
    def __init__(self, **kw):
        super().__init__(**kw)
        with self.canvas.before:
            Color(*OLIVE)
            self._r = Rectangle()
            Color(*DIM)
            self._l = Line(width=1)
        self.bind(pos=self._u, size=self._u)

    def _u(self, *_):
        self._r.pos, self._r.size = self.pos, self.size
        self._l.rectangle = (self.x, self.y, self.width, self.height)


class CastOps(App):
    def build(self):
        self.title = 'CAST//OPS'
        Window.clearcolor = BG
        Window.softinput_mode = 'below_target'
        self.tvs, self.casts, self.lines = {}, {}, []
        self.cl = threading.Lock()
        self.status, self._b, self._vt, self.busy = 'READY', True, None, False

        root = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(8))

        self.hdr = lbl(markup=True, size_hint_y=None, height=dp(56))
        root.add_widget(self.hdr)

        # scan + manual IP
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        scan_b = Neon(text='SCAN', size_hint_x=.26)
        scan_b.bind(on_release=self.scan)
        self.manual = field('manual IP: 192.168.1.20', size_hint_x=.5)
        add_b = Neon(text='ADD', col=AMBER, size_hint_x=.24)
        add_b.bind(on_release=self.add_manual)
        for w in (scan_b, self.manual, add_b):
            row.add_widget(w)
        root.add_widget(row)

        # targets
        tp = Panel(orientation='vertical', padding=dp(8), spacing=dp(4), size_hint_y=.34)
        tp.add_widget(lbl('// TARGETS  (tap to select)', AMBER, size_hint_y=None,
                          height=dp(18), font_size=sp(12)))
        self.grid = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter('height'))
        self.empty = lbl('No TVs yet. Press SCAN (phone and TV on the same Wi-Fi).', DIM,
                         size_hint_y=None, height=dp(48), font_size=sp(12))
        self.grid.add_widget(self.empty)
        sv = ScrollView(bar_width=dp(3), bar_color=GREEN)
        sv.add_widget(self.grid)
        tp.add_widget(sv)
        root.add_widget(tp)

        # link
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        self.link = field('paste video link (.mp4 / .m3u8 / .mp3)', size_hint_x=.74,
                          input_type='url')
        paste_b = Neon(text='PASTE', size_hint_x=.26)
        paste_b.bind(on_release=self.paste)
        row.add_widget(self.link)
        row.add_widget(paste_b)
        root.add_widget(row)

        # volume
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.vlab = lbl('VOL 40%', AMBER, size_hint_x=.24, font_size=sp(13))
        self.vol = Slider(min=0, max=100, value=40, step=1, value_track=True,
                          value_track_color=GREEN, cursor_size=(dp(26), dp(26)))
        self.vol.bind(value=self.on_vol)
        mute = Neon(text='MUTE', col=AMBER, toggle=True, size_hint_x=.24)
        mute.bind(active=lambda w, v: self.each(lambda c: c.mute(v)))
        for w in (self.vlab, self.vol, mute):
            row.add_widget(w)
        root.add_widget(row)

        # start + transport
        go = Neon(text='START CAST', size_hint_y=None, height=dp(60), font_size=sp(20))
        go.bind(on_release=self.start)
        root.add_widget(go)
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        pause_b = Neon(text='PAUSE', col=AMBER)
        pause_b.bind(on_release=lambda *_: self.each(lambda c: c.media('PAUSE')))
        resume_b = Neon(text='RESUME')
        resume_b.bind(on_release=lambda *_: self.each(lambda c: c.media('PLAY')))
        stop_b = Neon(text='STOP', col=RED)
        stop_b.bind(on_release=lambda *_: self.each(lambda c: c.stop()))
        for w in (pause_b, resume_b, stop_b):
            row.add_widget(w)
        root.add_widget(row)

        # log
        lp = Panel(orientation='vertical', padding=dp(8), spacing=dp(4), size_hint_y=.26)
        lp.add_widget(lbl('// LOG', AMBER, size_hint_y=None, height=dp(18), font_size=sp(12)))
        self.lsv = ScrollView(bar_width=dp(3), bar_color=GREEN)
        self.logl = Label(text='', markup=True, font_name=FONT, font_size=sp(11), color=GREEN,
                          halign='left', valign='top', size_hint_y=None)
        self.logl.bind(width=lambda o, v: setattr(o, 'text_size', (v, None)),
                       texture_size=lambda o, s: setattr(o, 'height', s[1]))
        self.lsv.add_widget(self.logl)
        lp.add_widget(self.lsv)
        root.add_widget(lp)

        self._hdr()
        Clock.schedule_interval(self._blink, .6)
        return root

    def on_pause(self):
        return True

    def on_stop(self):
        for c in list(self.casts.values()):
            c.close()

    # ----- header / log -----
    def _hdr(self):
        self.hdr.text = ('[b][size=24sp]>_ CAST//OPS[/size][/b]\n'
                         '[color=ffb000]STATUS: %s%s[/color]' % (self.status, '_' if self._b else ' '))

    def _blink(self, dt):
        self._b = not self._b
        self._hdr()

    def set_status(self, s):
        self.status = s
        self._hdr()

    def log(self, msg):
        ui(self._log, msg)

    def _log(self, msg):
        c = 'ff4040' if 'ERR' in msg else '39ff14'
        self.lines.append('[color=4a6b4a]%s[/color] [color=%s]%s[/color]'
                          % (time.strftime('%H:%M:%S'), c, escape_markup(msg)))
        del self.lines[:-60]
        self.logl.text = '\n'.join(self.lines)
        Clock.schedule_once(lambda dt: setattr(self.lsv, 'scroll_y', 0), .1)

    # ----- targets -----
    def selected(self):
        return [ip for ip, d in self.tvs.items() if d['btn'].active]

    def add_tv(self, ip, name):
        d = self.tvs.get(ip)
        if d:
            d['name'] = name
            d['upd']()
            return
        b = Neon(toggle=True, size_hint_y=None, height=dp(56), font_size=sp(14))
        d = {'name': name, 'btn': b}

        def upd(*_):
            b.text = '%s %s\n%s' % ('[X]' if b.active else '[ ]', d['name'], ip)
        d['upd'] = upd
        b.bind(active=upd)
        upd()
        self.tvs[ip] = d
        if self.empty.parent:
            self.grid.remove_widget(self.empty)
        self.grid.add_widget(b)

    def scan(self, *_):
        if self.busy:
            return
        self.busy = True
        self.set_status('SCANNING')
        self.log('sweeping local network for Cast devices ...')
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            res = castlib.scan()
            ui(self.found, res)
        except Exception as e:
            self.log('ERR scan failed: %s. Check Wi-Fi is on.' % e)
        finally:
            self.busy = False
            ui(self.set_status, 'READY')

    def found(self, res):
        for ip, name in res:
            self.add_tv(ip, name)
        if res:
            self.log('%d device(s) found. Tap the ones to use.' % len(res))
        else:
            self.log('ERR no devices found. Same Wi-Fi as the TVs? VPN off? Try ADD with the IP.')

    def add_manual(self, *_):
        ip = self.manual.text.strip()
        if not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', ip):
            return self.log('ERR enter an IP like 192.168.1.20')
        self.manual.text = ''
        threading.Thread(target=lambda: ui(self.add_tv, ip, castlib.dev_name(ip)),
                         daemon=True).start()

    # ----- link -----
    def paste(self, *_):
        try:
            t = (Clipboard.paste() or '').strip()
        except Exception:
            t = ''
        if t:
            self.link.text = t
        else:
            self.log('ERR clipboard is empty. Copy a link first.')

    # ----- cast control -----
    def cast(self, ip):
        with self.cl:
            c = self.casts.get(ip)
            if not c or not c.alive:
                c = castlib.Cast(ip)
                c.cb = lambda s, ip=ip: self.log('%s :: %s' % (ip, s))
                c.connect()
                self.casts[ip] = c
            return c

    def _do(self, ip, fn):
        try:
            fn(self.cast(ip))
        except Exception as e:
            self.log('ERR %s :: %s' % (ip, e))

    def each(self, fn):
        sel = self.selected()
        if not sel:
            return self.log('ERR select at least one TV first')
        for ip in sel:
            threading.Thread(target=self._do, args=(ip, fn), daemon=True).start()

    def start(self, *_):
        url = self.link.text.strip()
        if not url.lower().startswith(('http://', 'https://')):
            return self.log('ERR paste a full link starting with http:// or https://')
        if not self.selected():
            return self.log('ERR select at least one TV first')
        self.log('casting to %d TV(s) ...' % len(self.selected()))
        for ip in self.selected():
            threading.Thread(target=self._play, args=(ip, url), daemon=True).start()

    def _play(self, ip, url):
        try:
            direct, ct = castlib.resolve(url)
            self.cast(ip).play(direct, ct)
            self.log('%s :: sent (%s)' % (ip, ct))
        except Exception as e:
            self.log('ERR %s :: %s' % (ip, e))

    def on_vol(self, w, v):
        self.vlab.text = 'VOL %d%%' % v
        if self._vt:
            self._vt.cancel()
        self._vt = Clock.schedule_once(lambda dt: self.each(lambda c: c.volume(v / 100)), .25)


if __name__ == '__main__':
    CastOps().run()
