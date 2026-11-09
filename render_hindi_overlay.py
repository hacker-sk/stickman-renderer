"""Reels visuals for render_hindi.py: the same look as the English v2 render (render.py + render_v2_overlay.py),
adapted to the Hindi (Devanagari) text and to the red background with the framed square.

- load_layout: full-screen 9:16 background, the frame lines are detected and the picture window is the whole
  framed square plus a small overscan, so the pictures hide the lines and nothing shows outside the square.
- HookOverlay: Hindi hook above the square (white text, focus word red with glow, black outline).
- CaptionTrack: short caption lines near the bottom of the square, all words white, the spoken word in one highlight colour.
- render_video: English style crossfade with a small zoom, slower and smoother image motion, fade to black at the end.
If the background file is missing or its frame is not found, setup() returns None and render_hindi.py keeps its old layout.
"""
import math
import os
import random
import re
import subprocess

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import render_v2_overlay as ov   # English reference, only smoother() and fade_out() are reused (never modified)

CFG = {}
STATE = {}

BG_FILE = 'reel_background_hindi.png'      # old background, only used if none of the new ones can be loaded
# New backgrounds, one is picked at random for every video. File in assets/ -> outer frame lines
# (left, top, right, bottom) in pixels of the source image (941 x 1672).
BG_CHOICES = {
    'reel_bg_knight.png': (35, 352, 905, 1194),
    'reel_bg_boy_sepia.png': (37, 381, 903, 1175),
    'reel_bg_boy_red.png': (37, 366, 902, 1200),
    'reel_bg_blindfold_blue.png': (36, 368, 905, 1171),
}
OVERSCAN = 4                     # px the pictures extend past the outer edge of the frame lines
CANVAS = 1242                    # square source canvas, larger than the picture window so zoom never upscales
XFADE = 0.3                      # seconds, crossfade with a small zoom between scenes (same as English)
MOTION_AMOUNT = 0.5              # English keeps 0.7 of each zoom/pan travel; Hindi keeps less, so images drift slower
MOTIONS = ['pan_right', 'pan_left', 'slide_up', 'slide_down', 'zoom_in', 'zoom_out', 'focus']
HOOK_FONT = ('Poppins-Black.ttf', 'https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Black.ttf')
HOOK_WHITE = (255, 255, 255)
HOOK_ACCENT = (255, 59, 48)
CAPTION_WHITE = (255, 255, 255)
OUTLINE = (0, 0, 0)
# same highlight colours as the English caption palette (yellow, blue); black is skipped because it is invisible here
HIGHLIGHTS = [(255, 214, 0), (30, 144, 255)]
CAPTION_SIZES = (88, 80, 72, 64, 56)
CAPTION_FROM_BOTTOM = 45        # centre of the last caption line, px above the bottom of the picture window (English: 150)


def _log(*a):
    fn = CFG.get('log')
    if fn:
        fn(*a)


def _font(path, size):
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)   # RAQM shapes Devanagari correctly
    except Exception:  # noqa
        return ImageFont.truetype(path, size)


def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


# ----------------------------------------------------------------------------- background + layout
def _runs(values):
    groups = []
    for v in values:
        if groups and v == groups[-1][1] + 1:
            groups[-1][1] = v
        else:
            groups.append([v, v])
    return groups


def _bright(p):
    return p[0] >= 200 and p[1] >= 190 and p[2] >= 140


def _find_frame(img):
    """(left, top, right, bottom) pixel ranges of the four cream frame lines in the source image."""
    w, h = img.size
    px = img.load()
    xs = range(int(w * 0.15), int(w * 0.85), 3)
    ys = range(int(h * 0.20), int(h * 0.68), 3)
    rows = [y for y in range(h) if sum(1 for x in xs if _bright(px[x, y])) >= 0.6 * len(xs)]
    cols = [x for x in range(w) if sum(1 for y in ys if _bright(px[x, y])) >= 0.6 * len(ys)]
    rg, cg = _runs(rows), _runs(cols)
    if len(rg) != 2 or len(cg) != 2:
        raise ValueError('expected 2 horizontal and 2 vertical frame lines, found %d and %d' % (len(rg), len(cg)))
    return cg[0], rg[0], cg[1], rg[1]


def load_layout(w, h, path, log_fn=None, frame=None):
    """Full-screen background plus the picture window. None means: keep the old layout."""
    if not os.path.exists(path):
        if log_fn:
            log_fn('background layout skipped, file not found:', path)
        return None
    try:
        src = Image.open(path).convert('RGB')
        if frame:                                        # known frame position of the new backgrounds, no detection needed
            left_l, top_l, right_l, bottom_l = (frame[0],) * 2, (frame[1],) * 2, (frame[2],) * 2, (frame[3],) * 2
        else:
            left_l, top_l, right_l, bottom_l = _find_frame(src)
        scale = max(w / float(src.width), h / float(src.height))
        bg = src.resize((int(round(src.width * scale)), int(round(src.height * scale))), Image.LANCZOS)
        off_x = max(0, (bg.width - w) // 2)
        bg = bg.crop((off_x, 0, off_x + w, h))           # keep the top, trim the bottom if the image is taller
        x0 = int(math.floor(left_l[0] * scale)) - off_x - OVERSCAN
        x1 = int(math.ceil((right_l[1] + 1) * scale)) - off_x + OVERSCAN
        y0 = int(math.floor(top_l[0] * scale)) - OVERSCAN
        y1 = int(math.ceil((bottom_l[1] + 1) * scale)) + OVERSCAN
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        x0, x1 = 0, w                                    # full width: the picture band runs edge to edge, no side margins
        y0 = max(0, y1 - w)                              # taller than the drawn frame: a true 1:1 square (w x w), grows upward, bottom edge stays
        if x1 - x0 < w * 0.6 or y1 - y0 < h * 0.3:
            raise ValueError('frame looks wrong: %d,%d to %d,%d' % (x0, y0, x1, y1))
        lay = {'bg': bg, 'box': (x0, y0, x1, y1), 'pw': x1 - x0, 'ph': y1 - y0, 'hook_h': y0}
        if log_fn:
            log_fn('background layout: picture window x %d-%d, y %d-%d (%dx%d), hook area 0-%d' %
                   (x0, x1, y0, y1, x1 - x0, y1 - y0, y0))
        return lay
    except Exception as e:  # noqa
        if log_fn:
            log_fn('background layout skipped, using the old layout:', str(e)[:150])
        return None


def fit_square(img, size=None):
    """Square canvas for the picture window. Images are generated 1:1; any other shape is cropped (never stretched)."""
    size = size or CANVAS
    img = img.convert('RGB')
    if img.width != img.height:
        s = min(img.width, img.height)
        left = (img.width - s) // 2
        top = int((img.height - s) * 0.4)                # a little above the centre, faces and subjects sit higher
        img = img.crop((left, top, left + s, top + s))
    return img.resize((size, size), Image.LANCZOS)


# ----------------------------------------------------------------------------- hook
def _hook_font_path():
    fname, url = HOOK_FONT
    p = os.path.join(CFG['HERE'], 'fonts', fname)
    if os.path.exists(p):
        return p
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 20_000:
            with open(p, 'wb') as fh:
                fh.write(r.content)
            return p
    except Exception as e:  # noqa
        _log('hook font download failed:', str(e)[:100])
    return CFG['font']()


def _clean(s, limit):
    s = str(s or '')
    s = s.replace('\u2018', "'").replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')
    s = re.sub(r'[^\u0900-\u097F\u200c\u200d\x20-\x7E\u2013\u2014\u2026]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'[\u0964,;:\s]+$', '', s)                # no trailing danda or comma on a heading
    return s[:limit].strip()


def _norm(w):
    return re.sub(r'[^\u0900-\u097Fa-z0-9]', '', w.lower())


def hook_text(payload):
    """The hook: an explicit payload hook if there is one, else the opening phrase of the script (the scroll stopper)."""
    h = payload.get('hook')
    if isinstance(h, dict):
        line, focus = h.get('line') or h.get('text') or '', h.get('focus') or ''
    else:
        line, focus = (h if isinstance(h, str) else ''), ''
    if not _clean(line, 90):
        scenes = payload.get('scenes') or []
        line = str(scenes[0].get('narration', '')) if scenes else ''
        if len(line.split()) < 3 and len(scenes) > 1:
            line += ' ' + str(scenes[1].get('narration', ''))
        line = ' '.join(line.split()[:9])
    if not _clean(line, 90):
        line = payload.get('title', '')
    return _clean(line, 90), _clean(focus, 40)


class HookOverlay:
    """One hook line (max 2 lines) above the square, drawn once and pasted on every frame."""

    def __init__(self, line, focus=''):
        self.img = None
        if not line:
            return
        try:
            self.img = self._build(line, focus)
            _log('hook:', line, '| focus:', focus)
        except Exception as e:  # noqa
            _log('hook skipped:', str(e)[:150])

    def _build(self, line, focus):
        W, top_h = CFG['W'], CFG['HOOK_H']
        fp = _hook_font_path()
        tokens = line.split(' ')
        accent = [False] * len(tokens)
        f_words = [_norm(x) for x in focus.split() if _norm(x)]
        if f_words:
            n = len(f_words)
            for i in range(len(tokens) - n + 1):
                if [_norm(x) for x in tokens[i:i + n]] == f_words:
                    for j in range(i, i + n):
                        accent[j] = True
                    break
        if not any(accent):  # no usable focus: use the longest word so the hook always has an accent
            best = max(range(len(tokens)), key=lambda i: len(_norm(tokens[i])))
            accent[best] = True
        max_w = CFG['PW'] - 90
        chosen = None
        for size in range(CFG.get('HOOK_MAX_SIZE', 78), 35, -3):
            font = _font(fp, size)
            space = size * 0.30
            wid = [font.getlength(t) for t in tokens]
            lh = int(size * 1.3)

            def width(a, b):
                return sum(wid[a:b]) + space * (b - a - 1)

            n = len(tokens)
            if width(0, n) <= max_w and lh <= top_h - 40:
                chosen = (font, size, space, wid, [(0, n)], lh)
                break
            best = None
            for cut in range(1, n):
                m = max(width(0, cut), width(cut, n))
                if best is None or m < best[0]:
                    best = (m, cut)
            if best and best[0] <= max_w and 2 * lh <= top_h - 24:
                chosen = (font, size, space, wid, [(0, best[1]), (best[1], n)], lh)
                break
        if not chosen:
            raise RuntimeError('hook does not fit')
        font, size, space, wid, spans, lh = chosen
        layer = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        glow = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        d, gd = ImageDraw.Draw(layer), ImageDraw.Draw(glow)
        block = lh * len(spans)
        y_first = (top_h - block) / 2 + lh / 2 + 4 + CFG.get('HOOK_Y_OFFSET', 0)
        for li, (a, b) in enumerate(spans):
            tw = sum(wid[a:b]) + space * (b - a - 1)
            x = (W - tw) / 2
            cy = y_first + li * lh
            for i in range(a, b):
                col = HOOK_ACCENT if accent[i] else HOOK_WHITE
                if accent[i]:
                    gd.text((x, cy), tokens[i], font=font, fill=HOOK_ACCENT + (255,), anchor='lm',
                            stroke_width=6, stroke_fill=HOOK_ACCENT + (255,))
                d.text((x, cy), tokens[i], font=font, fill=col, anchor='lm', stroke_width=5, stroke_fill=OUTLINE)
                x += wid[i] + space
        glow = glow.filter(ImageFilter.GaussianBlur(11))
        glow.putalpha(glow.getchannel('A').point(lambda v: int(v * 0.55)))
        out = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        out.alpha_composite(glow)
        out.alpha_composite(layer)
        return out

    def draw(self, frame):
        if self.img is not None:
            frame.paste(self.img, (0, 0), self.img)


# ----------------------------------------------------------------------------- captions
def _font_kind(text):
    '''Which font a caption word needs: the Devanagari font has no Latin letters, so English words came out as empty boxes.'''
    has_dev = any(0x0900 <= ord(c) <= 0x097F for c in text)
    has_lat = any(c.isascii() and c.isalpha() for c in text)
    if has_lat and has_dev:
        return 'mixed'
    return 'latin' if has_lat else 'dev'


def _latin_font_path():
    for p in ('/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf', '/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        if os.path.exists(p):
            return p
    return _hook_font_path()


class CaptionTrack:
    """Short caption lines (max 3 words) near the bottom of the square. All words are white, the word being spoken
    is in the video's one highlight colour. Every line is fitted inside the picture window, never outside it."""

    def __init__(self, words):
        self.fp = CFG['font']()
        self.fps = {'dev': self.fp, 'latin': _latin_font_path(), 'mixed': _hook_font_path()}   # mixed words use Poppins (has both scripts)
        self.fonts = {}
        self.cache = {}
        self.stroke = 8
        self.pad = 16
        self.max_w = CFG['PW'] - 110
        self.hl = random.choice(HIGHLIGHTS)
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            if len(cur) >= 3 or re.search(r'[.!?,;:\u0964]$', w[0]):
                self.chunks.append(cur)
                cur = []
        if cur:
            self.chunks.append(cur)
        self.layout = [self._fit(ch) for ch in self.chunks]
        _log('caption highlight colour for this video:', self.hl)

    def _f(self, size, kind='dev'):
        key = (size, kind)
        f = self.fonts.get(key)
        if f is None:
            f = self.fonts[key] = _font(self.fps[kind], size)
        return f

    def _fw(self, size, text):
        return self._f(size, _font_kind(text))

    def _fit(self, ch):
        n = len(ch)
        for size in CAPTION_SIZES:
            f = self._f(size)
            space = f.getlength(' ') * 1.1
            widths = [self._fw(size, w[0]).getlength(w[0]) for w in ch]

            def fits(line):
                return sum(widths[k] for k in line) + space * (len(line) - 1) <= self.max_w

            one = list(range(n))
            if fits(one):
                return size, space, widths, [one]
            if n > 1:
                half = (n + 1) // 2
                two = [list(range(0, half)), list(range(half, n))]
                if all(fits(l) for l in two):
                    return size, space, widths, two
        size = CAPTION_SIZES[-1]
        f = self._f(size)
        return size, f.getlength(' ') * 1.1, [self._fw(size, w[0]).getlength(w[0]) for w in ch], [[k] for k in range(n)]

    def word_img(self, text, color, size):
        key = (text, color, size)
        im = self.cache.get(key)
        if im is None:
            f = self._fw(size, text)
            w = int(f.getlength(text)) + self.pad * 2 + self.stroke
            h = int(size * 1.9) + self.pad * 2
            im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((self.pad + self.stroke // 2, self.pad + int(size * 1.2)), text, font=f, fill=color, anchor='ls',
                                    stroke_width=self.stroke, stroke_fill=OUTLINE)
            self.cache[key] = im
        return im

    def draw(self, img, t):
        idx = None
        for i, ch in enumerate(self.chunks):
            nxt = self.chunks[i + 1][0][1] if i + 1 < len(self.chunks) else 1e9
            if ch[0][1] - 0.02 <= t < min(nxt, ch[-1][2] + 0.9):
                idx = i
                break
        if idx is None:
            return
        ch = self.chunks[idx]
        size, space, widths, lines = self.layout[idx]
        active = 0
        for k, word in enumerate(ch):
            if word[1] - 0.02 <= t:
                active = k
        W = CFG['W']
        line_h = int(size * 1.3)
        last_y = CFG['CAP_LAST_Y']
        y0 = last_y - (len(lines) - 1) * line_h
        for li, line in enumerate(lines):
            tw = sum(widths[k] for k in line) + space * (len(line) - 1)
            x = (W - tw) / 2
            cy = y0 + li * line_h
            for k in line:
                word = ch[k]
                if k == active:
                    im = self.word_img(word[0], self.hl, size)
                    a = _ease((t - word[1] + 0.02) / 0.16)
                    s = 1 + 0.16 * (1 - a)               # small settle as the word becomes the spoken one
                    if abs(s - 1) > 0.01:
                        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
                else:
                    im = self.word_img(word[0], CAPTION_WHITE, size)
                cx = x + widths[k] / 2
                img.paste(im, (int(cx - im.width / 2), int(cy - im.height / 2)), im)
                x += widths[k] + space


# ----------------------------------------------------------------------------- motion + video
def motion_plan(kind):
    """(zoom start, zoom end, x start, x end, y start, y end): the same plans as the English render."""
    r = random.random
    if kind == 'pan_right':
        return (1.15, 1.15, 0.05, 0.95, 0.4 + r() * 0.2, 0.4 + r() * 0.2)
    if kind == 'pan_left':
        return (1.15, 1.15, 0.95, 0.05, 0.4 + r() * 0.2, 0.4 + r() * 0.2)
    if kind == 'slide_up':
        return (1.06, 1.06, 0.5, 0.5, 0.30, 0.70)
    if kind == 'slide_down':
        return (1.06, 1.06, 0.5, 0.5, 0.70, 0.30)
    if kind == 'zoom_in':
        return (1.0, 1.16, 0.5, 0.5, 0.5, 0.5)
    if kind == 'zoom_out':
        return (1.16, 1.0, 0.5, 0.5, 0.5, 0.5)
    tx = random.choice([0.15, 0.85])                     # 'focus': zoom toward an off-centre spot
    return (1.0, 1.2, 0.5, tx, 0.5, 0.35 + r() * 0.3)


def slow_plan(plan, amp=None):
    """Each zoom/pan range is shrunk around its middle so the picture drifts more gently (English keeps 0.7)."""
    amp = MOTION_AMOUNT if amp is None else amp
    z0, z1, fx0, fx1, fy0, fy1 = plan

    def shrink(a, b):
        m = (a + b) / 2.0
        return m + (a - m) * amp, m + (b - m) * amp

    z0, z1 = shrink(z0, z1)
    fx0, fx1 = shrink(fx0, fx1)
    fy0, fy1 = shrink(fy0, fy1)
    return max(1.0, z0), max(1.0, z1), fx0, fx1, fy0, fy1


def view_frame(canvas, plan, u, extra=1.0):
    """The picture window (pw x ph) cut from the square canvas at motion progress u (0..1). The cut always stays
    inside the canvas, so the picture is never stretched and never shows an empty edge."""
    pw, ph = CFG['PW'], CFG['PH']
    z0, z1, fx0, fx1, fy0, fy1 = slow_plan(plan)
    e = 0.35 * u + 0.65 * _ease(u)                       # a little more easing than English (0.5 / 0.5): smoother start and end
    z = (z0 + (z1 - z0) * e) * extra
    fx = fx0 + (fx1 - fx0) * e
    fy = fy0 + (fy1 - fy0) * e
    cw, ch = float(canvas.width), float(canvas.height)
    bh = ch / z
    bw = bh * pw / float(ph)
    if bw > cw:
        bw = cw
        bh = bw * ph / float(pw)
    left = fx * (cw - bw)
    top = fy * (ch - bh)
    return canvas.resize((pw, ph), Image.BICUBIC, box=(left, top, left + bw, top + bh))


def render_video(canvases, bounds, captions, total, audio_path, out_path, fps, log_fn=None):
    W, H = CFG['W'], CFG['H']
    lay = STATE['layout']
    x0, y0 = lay['box'][0], lay['box'][1]
    nframes = int(math.ceil(total * fps))
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '%dx%d' % (W, H),
           '-r', str(fps), '-i', '-', '-i', audio_path, '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', os.environ.get('X264_PRESET', 'veryfast'), '-crf', '20',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-shortest', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = len(canvases)
    plans = []
    prev = None
    for _ in range(n):
        kind = random.choice([m for m in MOTIONS if m != prev])
        plans.append((kind,) + motion_plan(kind))
        prev = kind
    if log_fn:
        log_fn('motions:', [p[0] for p in plans])
    half = XFADE / 2.0
    shift = half - 0.05                      # the fade starts 0.05 s before the first word of the phrase, never earlier
    bounds = [bounds[0]] + [x + shift for x in bounds[1:-1]] + [bounds[-1]]
    black = Image.new('RGB', (W, H), (0, 0, 0))
    hook = STATE.get('hook')

    def view(k, t, extra=1.0):
        a = bounds[k] - (half if k > 0 else 0.0)
        b = bounds[k + 1] + (half if k + 1 < n else 0.0)
        u = (t - a) / max(0.1, b - a)
        return view_frame(canvases[k], plans[k][1:], min(1.0, max(0.0, u)), extra)

    for f in range(nframes):
        t = f / float(fps)
        k = 0
        while k + 1 < n and t >= bounds[k + 1]:
            k += 1
        if k + 1 < n and t > bounds[k + 1] - half:
            p = ov.smoother((t - (bounds[k + 1] - half)) / XFADE)
            pic = Image.blend(view(k, t, 1 + 0.07 * p), view(k + 1, t, 1 + 0.07 * (1 - p)), p)
        elif k > 0 and t < bounds[k] + half:
            p = ov.smoother((t - (bounds[k] - half)) / XFADE)
            pic = Image.blend(view(k - 1, t, 1 + 0.07 * p), view(k, t, 1 + 0.07 * (1 - p)), p)
        else:
            pic = view(k, t)
        frame = lay['bg'].copy()
        frame.paste(pic, (x0, y0))
        if hook is not None:
            hook.draw(frame)
        captions.draw(frame, t)
        frame = ov.fade_out(frame, black, t, total)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg video encode failed')


# ----------------------------------------------------------------------------- entry point
def setup(payload, w, h, here, font_fn, log_fn):
    """Load the background layout and build the hook. Returns the layout, or None to keep the old layout."""
    CFG.update(W=w, H=h, HERE=here, font=font_fn, log=log_fn)
    lay = None
    names = list(BG_CHOICES)
    random.shuffle(names)                                # a different background for every video
    for name in names:
        lay = load_layout(w, h, os.path.join(here, 'assets', name), log_fn, BG_CHOICES[name])
        if lay:
            if log_fn:
                log_fn('background picked:', name)
            break
    if not lay:                                          # none of the new files usable: old background as before
        lay = load_layout(w, h, os.path.join(here, 'assets', BG_FILE), log_fn)
    STATE['layout'] = lay
    STATE['hook'] = None
    if not lay:
        return None
    CFG.update(PW=lay['pw'], PH=lay['ph'], HOOK_H=lay['hook_h'], HOOK_Y_OFFSET=14,
               CAP_LAST_Y=lay['box'][3] - CAPTION_FROM_BOTTOM)
    line, focus = hook_text(payload)
    STATE['hook'] = HookOverlay(line, focus)
    return lay
