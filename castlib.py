"""castlib.py - minimal Google Cast (CASTV2) client. Pure Python, no hard dependencies.

Does what `catt scan / cast / volume / stop` does, but talks to the TV directly,
so the app needs neither Termux nor catt.
Optional packages: casttube (YouTube links), yt-dlp (other web-page links).
"""
import itertools
import json
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

NS_CONN = 'urn:x-cast:com.google.cast.tp.connection'
NS_HB = 'urn:x-cast:com.google.cast.tp.heartbeat'
NS_RECV = 'urn:x-cast:com.google.cast.receiver'
NS_MEDIA = 'urn:x-cast:com.google.cast.media'
NS_YT = 'urn:x-cast:com.google.youtube.mdx'
APP = 'CC1AD845'      # Default Media Receiver
YT_APP = '233637DE'   # YouTube

EXT = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/mp4',
    '.webm': 'video/webm', '.mkv': 'video/x-matroska',
    '.m3u8': 'application/x-mpegURL', '.mpd': 'application/dash+xml',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.flac': 'audio/flac',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
}


# ---------- tiny protobuf codec for CastMessage ----------
def _vi(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(tag, s):
    s = s.encode()
    return bytes([tag]) + _vi(len(s)) + s


def enc(src, dst, ns, payload):
    body = (b'\x08\x00' + _ld(0x12, src) + _ld(0x1A, dst) + _ld(0x22, ns)
            + b'\x28\x00' + _ld(0x32, payload))
    return struct.pack('>I', len(body)) + body


def _rv(buf, i):
    n = s = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            return n, i


def dec(buf):
    i, f = 0, {}
    while i < len(buf):
        k, i = _rv(buf, i)
        wt = k & 7
        if wt == 0:
            v, i = _rv(buf, i)
        elif wt == 2:
            n, i = _rv(buf, i)
            v, i = buf[i:i + n], i + n
        elif wt == 1:
            i += 8
            continue
        elif wt == 5:
            i += 4
            continue
        else:
            break
        f[k >> 3] = v
    return f


# ---------- Cast connection ----------
class Cast:
    def __init__(self, host, port=8009):
        self.host, self.port = host, port
        self.sock = None
        self.alive = False
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.ev = threading.Event()
        self.yev = threading.Event()
        self.want = APP          # receiver app we are currently talking to
        self.transport = self.session = self.msid = self.vol = self.screen_id = None
        self.last = ''
        self.cb = lambda state: None

    def connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.host, self.port), 6)
        self.sock = ctx.wrap_socket(raw)
        self.sock.settimeout(10)
        self.alive = True
        self._raw(NS_CONN, {'type': 'CONNECT'})
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._beat, daemon=True).start()
        self._send(NS_RECV, {'type': 'GET_STATUS'})

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass

    def _raw(self, ns, d, dst='receiver-0'):
        data = enc('sender-0', dst, ns, json.dumps(d))
        with self.lock:
            self.sock.sendall(data)

    def _send(self, ns, d, dst='receiver-0'):
        d['requestId'] = next(self.ids)
        self._raw(ns, d, dst)

    def _beat(self):
        while self.alive:
            try:
                self._raw(NS_HB, {'type': 'PING'})
            except Exception:
                self.alive = False
                return
            time.sleep(5)

    def _recvn(self, n):
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf

    def _read(self):
        try:
            while self.alive:
                if not self.sock.pending() and not select.select([self.sock], [], [], 1)[0]:
                    continue
                n = struct.unpack('>I', self._recvn(4))[0]
                self._on(dec(self._recvn(n)))
        except Exception:
            pass
        self.alive = False

    def _on(self, m):
        try:
            d = json.loads(m.get(6, b'{}').decode() or '{}')
        except Exception:
            return
        t = d.get('type')
        if t == 'PING':
            self._raw(NS_HB, {'type': 'PONG'})
        elif t == 'CLOSE':
            if m.get(2, b'') == b'receiver-0':
                self.alive = False
            else:
                self.transport = None
        elif t == 'RECEIVER_STATUS':
            st = d.get('status') or {}
            v = st.get('volume') or {}
            if 'level' in v:
                self.vol = v['level']
            app = next((a for a in st.get('applications', []) if a.get('appId') == self.want), None)
            self.transport = app.get('transportId') if app else None
            self.session = app.get('sessionId') if app else None
            self.ev.set()
        elif t == 'mdxSessionStatus':
            sid = (d.get('data') or {}).get('screenId')
            if sid:
                self.screen_id = sid
                self.yev.set()
        elif t == 'MEDIA_STATUS':
            for s in d.get('status', []):
                self.msid = s.get('mediaSessionId', self.msid)
                ps = 'ERROR' if s.get('idleReason') == 'ERROR' else s.get('playerState')
                if ps and ps != self.last:
                    self.last = ps
                    self.cb(ps)
        elif t in ('LOAD_FAILED', 'LOAD_CANCELLED', 'INVALID_REQUEST'):
            self.cb('ERR ' + t + ' (TV cannot play this link/format)')

    # ----- actions -----
    def _launch(self, app_id):
        self.want = app_id
        self.transport = self.session = self.msid = None
        self.last = ''
        self.ev.clear()
        self._send(NS_RECV, {'type': 'LAUNCH', 'appId': app_id})
        t0 = time.time()
        while not self.transport and self.alive and time.time() - t0 < 15:
            self.ev.wait(1)
            self.ev.clear()
        if not self.transport:
            raise RuntimeError('TV did not start the player. Is it awake and on the same Wi-Fi?')
        self._raw(NS_CONN, {'type': 'CONNECT'}, self.transport)

    def play(self, url, ctype):
        if ctype == 'youtube':
            return self._play_youtube(url)
        self._launch(APP)
        self._send(NS_MEDIA, {
            'type': 'LOAD', 'sessionId': self.session, 'autoplay': True, 'currentTime': 0,
            'media': {'contentId': url, 'contentType': ctype, 'streamType': 'BUFFERED'},
        }, self.transport)

    def _play_youtube(self, vid):
        try:
            from casttube import YouTubeSession
        except ImportError:
            raise RuntimeError('YouTube links need the casttube package (Pydroid: Menu > Pip > casttube > Install)')
        self.screen_id = None
        self.yev.clear()
        self._launch(YT_APP)
        for _ in range(8):  # the YouTube app needs a moment before it answers
            if not self.transport:
                break
            self._send(NS_YT, {'type': 'getMdxSessionStatus'}, self.transport)
            if self.yev.wait(2):
                break
        if not self.screen_id:
            raise RuntimeError('YouTube app on the TV did not respond. Open YouTube on the TV once, then retry.')
        YouTubeSession(screen_id=self.screen_id).play_video(vid)

    def media(self, typ):
        if not self.transport or self.msid is None:
            raise RuntimeError('pause/resume only works for direct links, not YouTube' if self.want == YT_APP
                               else 'nothing is playing on this TV')
        self._send(NS_MEDIA, {'type': typ, 'mediaSessionId': self.msid}, self.transport)

    def stop(self):
        if self.transport and self.msid is not None:
            self._send(NS_MEDIA, {'type': 'STOP', 'mediaSessionId': self.msid}, self.transport)
        if self.session:
            self._send(NS_RECV, {'type': 'STOP', 'sessionId': self.session})

    def volume(self, level):
        level = round(max(0.0, min(1.0, level)), 2)
        self._send(NS_RECV, {'type': 'SET_VOLUME', 'volume': {'level': level}})

    def mute(self, on):
        self._send(NS_RECV, {'type': 'SET_VOLUME', 'volume': {'muted': bool(on)}})


# ---------- discovery ----------
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))  # no packet is sent, just picks the Wi-Fi interface
        return s.getsockname()[0]
    finally:
        s.close()


def dev_name(host):
    try:
        url = 'http://%s:8008/setup/eureka_info?params=name' % host
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.load(r).get('name') or host
    except Exception:
        return host


def scan(port=8009):
    """Sweep the phone's own /24 subnet for Cast receivers. Returns [(ip, name)]."""
    base = local_ip().rsplit('.', 1)[0]

    def probe_host(i):
        h = '%s.%d' % (base, i)
        try:
            socket.create_connection((h, port), 0.9).close()
        except OSError:
            return None
        return h, dev_name(h)

    with ThreadPoolExecutor(64) as ex:
        return [r for r in ex.map(probe_host, range(1, 255)) if r]


# ---------- link -> (what to send, content type) ----------
def yt_id(url):
    """11-char YouTube video id from watch / youtu.be / shorts / embed links, else None."""
    u = urlparse(url)
    h = (u.hostname or '').lower()
    vid = ''
    if h == 'youtu.be':
        vid = u.path.strip('/').split('/')[0]
    elif h.endswith('youtube.com') or h.endswith('youtube-nocookie.com'):
        vid = (parse_qs(u.query).get('v') or [''])[0]
        if not vid:
            m = re.match(r'/(?:shorts|embed|live|v)/([^/?#]+)', u.path)
            vid = m.group(1) if m else ''
    return vid if re.fullmatch(r'[\w-]{11}', vid) else None


def probe(url):
    """Content-Type the server reports (HEAD, then a 1-byte GET), or None."""
    for method, extra in (('HEAD', {}), ('GET', {'Range': 'bytes=0-0'})):
        try:
            hd = {'User-Agent': 'Mozilla/5.0'}
            hd.update(extra)
            rq = urllib.request.Request(url, method=method, headers=hd)
            with urllib.request.urlopen(rq, timeout=6) as r:
                return r.headers.get_content_type()
        except Exception:
            continue
    return None


def resolve(url):
    vid = yt_id(url)
    if vid:
        return vid, 'youtube'
    p = urlparse(url).path.lower()
    for e, t in EXT.items():
        if p.endswith(e):
            return url, t
    ct = probe(url)
    if ct == 'image/jpg':
        ct = 'image/jpeg'
    if ct and (ct.startswith(('video/', 'audio/', 'image/')) or 'mpegurl' in ct):
        return url, ct
    if ct == 'application/octet-stream':
        return url, 'video/mp4'
    try:
        import yt_dlp  # optional: page links (Facebook, Vimeo, ...)
    except ImportError:
        if ct is None:  # the phone got no answer from the server; let the TV try itself
            return url, 'video/mp4'
        raise RuntimeError('this link is a web page, not a video file. Install yt-dlp '
                           '(Pydroid: Menu > Pip > yt-dlp) or paste a direct .mp4 / .m3u8 link')
    with yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True, 'format': 'best[ext=mp4]/best'}) as y:
        info = y.extract_info(url, download=False)
    u = info.get('url')
    if not u:
        raise RuntimeError('could not extract a playable stream from this page')
    if '.m3u8' in u or str(info.get('protocol', '')).startswith('m3u8'):
        return u, 'application/x-mpegURL'
    return u, 'video/mp4'
