"""Reels visuals for render.py (v2 English).

- CaptionTrack: all caption words white, only the word being spoken in ONE highlight colour (picked at random
  from the existing caption palette once per video), captions sit a little higher in the bottom black bar.
- HookOverlay: a short hook line in the top black bar, focus word in red with a soft glow, optional emoji.
- fade_out: smooth fade to black over the last moments of the video.
"""
import math
import os
import random
import re

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

CFG = {}
STATE = {}

HOOK_FONT = ('Bangers-Regular.ttf',
             'https://raw.githubusercontent.com/google/fonts/main/ofl/bangers/Bangers-Regular.ttf')
EMOJI_FONTS = ['/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',
               '/usr/share/fonts/noto/NotoColorEmoji.ttf',
               '/usr/share/fonts/truetype/NotoColorEmoji.ttf']
HOOK_WHITE = (255, 255, 255)
HOOK_ACCENT = (255, 59, 48)
CAPTION_WHITE = (255, 255, 255)
OUTLINE = (0, 0, 0)
CAPTION_CENTER_BELOW_PICTURE = 125   # px below the bottom of the picture window (was the middle of the bar)
FADE_OUT_SECONDS = 0.8
MOTION_AMOUNT = 0.7        # how much of each image's original zoom/pan travel is kept (smaller = slower, gentler)


def configure(w, h, img_top, img_h, caption_font_fn, log_fn, palette, here):
    CFG.update(W=w, H=h, IMG_TOP=img_top, IMG_H=img_h, caption_font=caption_font_fn, log=log_fn,
               palette=palette, HERE=here)


def _log(*a):
    fn = CFG.get('log')
    if fn:
        fn(*a)


def smoother(x):
    x = min(1.0, max(0.0, x))
    return x * x * x * (x * (x * 6 - 15) + 10)


def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def highlight_choices():
    """Colours from the existing caption palette that can stand out on the black bar (black is skipped)."""
    out = []
    for fill, _edge in CFG['palette']:
        lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
        if lum > 60:
            out.append(fill)
    return out or [(255, 214, 0)]


# ----------------------------------------------------------------------------- captions
class CaptionTrack:
    """Short caption lines (max 3 words) in the bottom black bar. All words are white, the word being spoken
    is in the video's one highlight colour."""

    def __init__(self, words, font_size=96):
        fp = CFG['caption_font']()
        self.font = ImageFont.truetype(fp, font_size) if fp else ImageFont.load_default()
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            if len(cur) >= 3 or re.search(r"[.!?,;:]$", w[0]):
                self.chunks.append(cur)
                cur = []
        if cur:
            self.chunks.append(cur)
        self.space = self.font.getlength(" ") * 1.1
        self.pad = 16
        self.stroke = 8
        self.cache = {}
        self.hl = random.choice(highlight_choices())
        W = CFG['W']
        self.layout = []
        for ch in self.chunks:
            widths = [self.font.getlength(w[0]) for w in ch]
            total = sum(widths) + self.space * (len(ch) - 1)
            if total > W - 100:  # two lines when too wide
                half = (len(ch) + 1) // 2
                lines = [list(range(0, half)), list(range(half, len(ch)))]
            else:
                lines = [list(range(len(ch)))]
            self.layout.append((widths, lines))
        _log('caption highlight colour for this video:', self.hl)

    def word_img(self, text, color):
        key = (text, color)
        im = self.cache.get(key)
        if im is None:
            w = int(self.font.getlength(text)) + self.pad * 2
            h = int(self.font.size * 1.5) + self.pad * 2
            im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((self.pad, self.pad), text, font=self.font, fill=color,
                                    stroke_width=self.stroke, stroke_fill=OUTLINE)
            self.cache[key] = im
        return im

    def draw(self, img, t):
        idx = None
        for i, ch in enumerate(self.chunks):
            nxt = self.chunks[i + 1][0][1] if i + 1 < len(self.chunks) else 1e9
            if ch[0][1] - 0.02 <= t < min(nxt, ch[-1][2] + 0.25):
                idx = i
                break
        if idx is None:
            return
        ch = self.chunks[idx]
        widths, lines = self.layout[idx]
        active = 0
        for k, word in enumerate(ch):
            if word[1] - 0.02 <= t:
                active = k
        W = CFG['W']
        bar_top = CFG['IMG_TOP'] + CFG['IMG_H']
        cy0 = bar_top + CAPTION_CENTER_BELOW_PICTURE
        line_h = int(self.font.size * 1.2)
        y0 = cy0 - (len(lines) - 1) * line_h // 2
        if CFG.get('CAP_LAST_Y') is not None:      # square layout: last caption line sits near the bottom of the picture
            y0 = CFG['CAP_LAST_Y'] - (len(lines) - 1) * line_h
        for li, line in enumerate(lines):
            tw = sum(widths[k] for k in line) + self.space * (len(line) - 1)
            x = (W - tw) / 2
            cy = y0 + li * line_h
            for k in line:
                word = ch[k]
                if k == active:
                    im = self.word_img(word[0], self.hl)
                    a = _ease((t - word[1] + 0.02) / 0.16)
                    s = 1 + 0.16 * (1 - a)                  # small settle as the word becomes the spoken one
                    if abs(s - 1) > 0.01:
                        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
                else:
                    im = self.word_img(word[0], CAPTION_WHITE)
                cx = x + widths[k] / 2
                img.paste(im, (int(cx - im.width / 2), int(cy - im.height / 2)), im)
                x += widths[k] + self.space


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
    return CFG['caption_font']()


def _clean(s, limit):
    s = str(s or '')
    s = s.replace('\u2018', "'").replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')
    s = re.sub(r"[^\x20-\x7E\u00A0-\u00FF\u2013\u2014\u2026]", '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit].strip()


def _norm(w):
    return re.sub(r'[^a-z0-9]', '', w.lower())


def _emoji_image(emoji, target_h):
    if not emoji:
        return None
    path = next((p for p in EMOJI_FONTS if os.path.exists(p)), None)
    if not path:
        return None
    try:
        ef = ImageFont.truetype(path, 109)
        big = Image.new('RGBA', (240, 200), (0, 0, 0, 0))
        ImageDraw.Draw(big).text((10, 10), emoji, font=ef, embedded_color=True)
        box = big.getbbox()
        if not box:
            return None
        big = big.crop(box)
        scale = target_h / float(big.height)
        return big.resize((max(1, int(big.width * scale)), target_h), Image.LANCZOS)
    except Exception as e:  # noqa
        _log('hook emoji skipped:', str(e)[:100])
        return None


class HookOverlay:
    """One hook line (max 2 lines) in the top black bar, drawn once and pasted on every frame."""

    def __init__(self, hook, title=''):
        hook = hook if isinstance(hook, dict) else {}
        line = _clean(hook.get('line') or hook.get('text'), 70)
        if not line:
            line = _clean(title, 60)
        focus = _clean(hook.get('focus'), 40)
        emoji = str(hook.get('emoji') or '').strip()[:8]
        self.img = None
        if not line:
            return
        try:
            self.img = self._build(line, focus, emoji)
            _log('hook:', line, '| focus:', focus, '| emoji:', emoji)
        except Exception as e:  # noqa
            _log('hook skipped:', str(e)[:150])

    def _build(self, line, focus, emoji):
        W, top_h = CFG['W'], (CFG.get('HOOK_H') or CFG['IMG_TOP'])
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
        max_w = CFG.get('HOOK_MAX_W') or (W - 120)
        chosen = None
        for size in range(CFG.get('HOOK_MAX_SIZE', 92), 39, -4):
            font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
            space = size * 0.30
            wid = [font.getlength(t) for t in tokens]
            emo_w = size * 1.05 + space if emoji else 0
            lh = int(size * 1.12)

            def width(a, b, last=False):
                return sum(wid[a:b]) + space * (b - a - 1) + (emo_w if last else 0)

            n = len(tokens)
            if width(0, n, True) <= max_w and lh <= top_h - 40:
                chosen = (font, size, space, wid, [(0, n)], lh)
                break
            best = None
            for cut in range(1, n):
                m = max(width(0, cut), width(cut, n, True))
                if best is None or m < best[0]:
                    best = (m, cut)
            if best and best[0] <= max_w and 2 * lh <= top_h - 24:
                chosen = (font, size, space, wid, [(0, best[1]), (best[1], n)], lh)
                break
        if not chosen:
            raise RuntimeError('hook does not fit')
        font, size, space, wid, spans, lh = chosen
        pad = 40
        layer = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        glow = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        d, gd = ImageDraw.Draw(layer), ImageDraw.Draw(glow)
        block = lh * len(spans)
        y_first = (top_h - block) / 2 + lh / 2 + 4 + CFG.get('HOOK_Y_OFFSET', 0)
        emo = _emoji_image(emoji, int(size * 1.0)) if emoji else None
        for li, (a, b) in enumerate(spans):
            last = li == len(spans) - 1
            tw = sum(wid[a:b]) + space * (b - a - 1)
            if last and emo:
                tw += space + emo.width
            x = (W - tw) / 2
            cy = y_first + li * lh
            for i in range(a, b):
                col = HOOK_ACCENT if accent[i] else HOOK_WHITE
                if accent[i]:
                    gd.text((x, cy), tokens[i], font=font, fill=HOOK_ACCENT + (255,), anchor='lm',
                            stroke_width=6, stroke_fill=HOOK_ACCENT + (255,))
                d.text((x, cy), tokens[i], font=font, fill=col, anchor='lm', stroke_width=5, stroke_fill=OUTLINE)
                x += wid[i] + space
            if last and emo:
                layer.paste(emo, (int(x), int(cy - emo.height / 2)), emo)
        glow = glow.filter(ImageFilter.GaussianBlur(11))
        glow.putalpha(glow.getchannel('A').point(lambda v: int(v * 0.55)))
        out = Image.new('RGBA', (W, top_h), (0, 0, 0, 0))
        out.alpha_composite(glow)
        out.alpha_composite(layer)
        return out

    def draw(self, frame):
        if self.img is not None:
            frame.paste(self.img, (0, 0), self.img)


# ----------------------------------------------------------------------------- end fade
def fade_out(frame, black, t, total):
    start = total - FADE_OUT_SECONDS
    if t <= start:
        return frame
    return Image.blend(frame, black, smoother((t - start) / FADE_OUT_SECONDS))


# ----------------------------------------------------------------------------- background layout + motion
def _line_groups(img):
    """Row ranges (first, last) of the pale-yellow horizontal lines drawn across the background."""
    w, h = img.size
    px = img.load()
    xs = range(0, w, 3)
    need = 0.85 * len(xs)
    rows = []
    for y in range(h):
        n = 0
        for x in xs:
            r, g, b = px[x, y]
            if r >= 235 and g >= 230 and b >= 150:
                n += 1
        if n >= need:
            rows.append(y)
    groups = []
    for y in rows:
        if groups and y == groups[-1][1] + 1:
            groups[-1][1] = y
        else:
            groups.append([y, y])
    return [tuple(g) for g in groups if g[1] - g[0] >= 1]


def load_layout(w, h, path, log_fn=None):
    """Full-screen background with a square picture window marked by two lines. None = keep the old layout."""
    if not os.path.exists(path):
        return None
    try:
        src = Image.open(path).convert('RGB')
        groups = _line_groups(src)
        if len(groups) != 2:
            raise ValueError('expected 2 boundary lines, found %d' % len(groups))
        (t0, t1), (b0, b1) = groups
        scale = max(w / float(src.width), h / float(src.height))
        bg = src.resize((int(round(src.width * scale)), int(round(src.height * scale))), Image.LANCZOS)
        left = max(0, (bg.width - w) // 2)
        bg = bg.crop((left, 0, left + w, h))             # keep the top, trim the bottom if the image is taller
        inner_top = int(math.ceil((t1 + 2) * scale))
        inner_bot = int(math.floor((b0 - 1) * scale))
        inner_h = inner_bot - inner_top
        if inner_h < w:
            raise ValueError('picture window %dpx is smaller than the %dpx square' % (inner_h, w))
        lay = {'bg': bg, 'sq_top': inner_top + (inner_h - w) // 2, 'sq_h': w, 'hook_h': int(math.floor(t0 * scale))}
        if log_fn:
            log_fn('background layout: square %dx%d at y=%d, hook area 0-%d (window %d-%d)' %
                   (w, w, lay['sq_top'], lay['hook_h'], inner_top, inner_bot))
        return lay
    except Exception as e:  # noqa
        if log_fn:
            log_fn('background layout skipped, using the old layout:', str(e)[:150])
        return None


def set_layout(lay):
    """Hook above the square, captions inside the square near its bottom."""
    if not lay:
        return
    CFG.update(HOOK_H=lay['hook_h'], HOOK_MAX_W=CFG['W'] - 340, HOOK_MAX_SIZE=80, HOOK_Y_OFFSET=14,
               CAP_LAST_Y=lay['sq_top'] + lay['sq_h'] - 150)


def slow_plan(plan, amp=None):
    """Same motion, less travel: each zoom/pan range is shrunk around its middle so images drift more gently."""
    amp = MOTION_AMOUNT if amp is None else amp
    z0, z1, fx0, fx1, fy0, fy1 = plan

    def shrink(a, b):
        m = (a + b) / 2.0
        return m + (a - m) * amp, m + (b - m) * amp

    z0, z1 = shrink(z0, z1)
    fx0, fx1 = shrink(fx0, fx1)
    fy0, fy1 = shrink(fy0, fy1)
    return max(1.0, z0), max(1.0, z1), fx0, fx1, fy0, fy1
