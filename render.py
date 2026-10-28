#!/usr/bin/env python3
"""
Stickman Shorts renderer (runs inside GitHub Actions).

n8n sends one JSON payload (the script, scenes, image prompts, art style, mood and a callback URL).
This script then:
  1. makes the Callirrhoe voiceover with Gemini TTS
  2. gets word-level timestamps with Whisper (falls back to an even estimate)
  3. generates one image per scene, trying Hugging Face -> Cloudflare -> Colab (configurable order)
  4. picks a mood-matched music track from the Google Drive folder
  5. draws every frame itself (still images with gentle cross-dissolves, word-by-word captions) and encodes with ffmpeg
  6. mixes voice + music (music ducks under the voice), uploads the mp4 and calls n8n back
"""
import base64
import difflib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import wave
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
CANVAS_W, CANVAS_H = 1242, 2208          # source canvas is larger than the frame so zoom never upscales
TRANSITIONS = {'fade': 0.5, 'dissolve': 0.8}   # name: seconds. Only gentle cross-dissolves, picked at random per cut, never the same twice in a row
WORK = os.environ.get("WORK_DIR", "work")
OUT = os.environ.get("OUT_DIR", "output")
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(HERE, "fonts", "Poppins-Bold.ttf")
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf"
MUSIC_FOLDER_ID = os.environ.get("MUSIC_FOLDER_ID", "1BKMv97oqy2UEkhq3jOSp8yy1kzYFZtEl")
FAKE = os.environ.get("FAKE_MODE") == "1"   # used only for local tests

os.makedirs(WORK, exist_ok=True)
os.makedirs(OUT, exist_ok=True)


def ensure_font():
    """Use the bundled font, else download Poppins Bold, else fall back to DejaVu Bold."""
    global FONT_PATH
    if os.path.exists(FONT_PATH):
        return FONT_PATH
    try:
        os.makedirs(os.path.dirname(FONT_PATH), exist_ok=True)
        r = requests.get(FONT_URL, timeout=30)
        if r.status_code == 200 and len(r.content) > 50_000:
            with open(FONT_PATH, "wb") as fh:
                fh.write(r.content)
            log("downloaded caption font")
            return FONT_PATH
    except Exception as e:  # noqa
        log("font download failed:", str(e)[:100])
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"):
        if os.path.exists(cand):
            FONT_PATH = cand
            return cand
    return None


def log(*a):
    print("[render]", *a, flush=True)


def retry(fn, tries=4, delay=4, what="call"):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            last = e
            log(f"{what} failed (try {i + 1}/{tries}): {str(e)[:200]}")
            time.sleep(delay * (i + 1))
    raise last


# ----------------------------------------------------------------------------- voice
def make_voice(text, path):
    if FAKE:
        # local test: 41 seconds of soft tone bursts that look like speech energy
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=180:duration=41,tremolo=f=4:d=0.9", "-ar", "24000", "-ac", "1", path], check=True)
        return
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Callirrhoe"}}},
        },
    }

    def call():
        r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          data=json.dumps(body), timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini TTS HTTP {r.status_code}: {r.text[:200]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        for p in parts:
            if "inlineData" in p:
                return base64.b64decode(p["inlineData"]["data"])
        raise RuntimeError("Gemini TTS returned no audio")

    pcm = retry(call, tries=6, delay=5, what="Gemini TTS")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)


def audio_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nw=1:nk=1", path]).decode().strip()
    return float(out)


# ----------------------------------------------------------------------------- word timing
def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def whisper_words(path):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("WHISPER_MODEL", "small.en"), device="cpu", compute_type="int8")
    segs, _ = model.transcribe(path, word_timestamps=True, language="en", vad_filter=False)
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append((w.word.strip(), float(w.start), float(w.end)))
    return words


def estimate_words(script_words, duration):
    """Even estimate by letter count, used only when Whisper is not available."""
    weights = [max(2, len(w)) + 2 for w in script_words]
    total = float(sum(weights))
    t = 0.0
    out = []
    usable = duration * 0.97
    for w, wt in zip(script_words, weights):
        d = usable * wt / total
        out.append((w, t, t + d * 0.92))
        t += d
    return out


def align_words(script_words, duration, audio_path):
    """Return [(word, start, end)] using the script's own spelling and Whisper's timing."""
    try:
        if FAKE:
            raise RuntimeError("fake mode")
        heard = whisper_words(audio_path)
        if len(heard) < max(3, len(script_words) // 3):
            raise RuntimeError("Whisper heard too few words")
        a = [norm(w) for w in script_words]
        b = [norm(h[0]) for h in heard]
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        start = [None] * len(a)
        end = [None] * len(a)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                start[blk.a + k] = heard[blk.b + k][1]
                end[blk.a + k] = heard[blk.b + k][2]
        # fill the gaps by interpolating between known anchors
        known = [i for i in range(len(a)) if start[i] is not None]
        if len(known) < len(a) // 3:
            raise RuntimeError("alignment too weak")
        first, last = known[0], known[-1]
        for i in range(0, first):
            start[i] = max(0.0, start[first] - (first - i) * 0.25)
            end[i] = start[i] + 0.22
        for i in range(last + 1, len(a)):
            start[i] = end[last] + (i - last - 1) * 0.28
            end[i] = start[i] + 0.25
        for x, y in zip(known, known[1:]):
            gap = y - x - 1
            for j in range(1, gap + 1):
                f = j / (gap + 1)
                start[x + j] = end[x] + (start[y] - end[x]) * f
                end[x + j] = start[x + j] + max(0.12, (start[y] - end[x]) / (gap + 1) * 0.9)
        out = []
        prev = 0.0
        for i, w in enumerate(script_words):
            s = max(prev, start[i])
            e = max(s + 0.08, end[i])
            out.append((w, s, e))
            prev = s
        log(f"Whisper alignment ok ({len(heard)} heard, {len(known)} matched of {len(a)})")
        return out
    except Exception as e:  # noqa
        log("Using estimated word timing:", str(e)[:150])
        return estimate_words(script_words, duration)


# ----------------------------------------------------------------------------- images
_disabled = {}
_lock = threading.Lock()
_fail_counts = {}


def fit_vertical(img):
    """Make a CANVAS_W x CANVAS_H canvas. Real 9:16 images fill it; other shapes sit on a blurred copy."""
    img = img.convert("RGB")
    ratio = img.width / img.height
    if 0.52 <= ratio <= 0.60:
        return img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    bg = img.copy()
    scale = max(CANVAS_W / bg.width, CANVAS_H / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1), Image.BILINEAR)
    l, t = (bg.width - CANVAS_W) // 2, (bg.height - CANVAS_H) // 2
    bg = bg.crop((l, t, l + CANVAS_W, t + CANVAS_H)).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.35)
    fg_scale = CANVAS_W / img.width
    fg = img.resize((CANVAS_W, int(img.height * fg_scale)), Image.LANCZOS)
    if fg.height > CANVAS_H:
        fg = fg.resize((int(fg.width * CANVAS_H / fg.height), CANVAS_H), Image.LANCZOS)
    bg.paste(fg, ((CANVAS_W - fg.width) // 2, (CANVAS_H - fg.height) // 2))
    return bg


def img_hf(prompt, **_):
    from huggingface_hub import InferenceClient
    client = InferenceClient(api_key=os.environ["HF_TOKEN"])
    model = os.environ.get("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
    return client.text_to_image(prompt, model=model, width=720, height=1280)


def cf_accounts():
    """Cloudflare accounts from the secrets: CF_ACCOUNT_ID / CF_API_TOKEN, then the _2 and _3 pairs."""
    accts = []
    for suffix in ('', '_2', '_3'):
        acct = os.environ.get('CF_ACCOUNT_ID' + suffix, '').strip()
        tok = os.environ.get('CF_API_TOKEN' + suffix, '').strip()
        if acct and tok:
            accts.append(('account ' + (suffix[1:] if suffix else '1'), acct, tok))
    return accts


_cf_dead = set()
_cf_turn = [0]


def img_cloudflare(prompt, **_):
    accts = cf_accounts()
    if not accts:
        raise RuntimeError('Cloudflare keys not set')
    with _lock:
        live = [a for a in accts if a[0] not in _cf_dead]
        turn = _cf_turn[0]
        _cf_turn[0] += 1
    if not live:
        raise RuntimeError('Cloudflare HTTP 429: every account is out of its daily allowance')
    for k in range(len(live)):
        label, acct, tok = live[(turn + k) % len(live)]
        url = f'https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell'
        r = requests.post(url, headers={'Authorization': f'Bearer {tok}'},
                          json={'prompt': prompt[:2000], 'steps': 6}, timeout=120)
        if r.status_code in (401, 403, 429):
            with _lock:
                if label not in _cf_dead:
                    _cf_dead.add(label)
                    log(f'cloudflare {label} is unavailable (HTTP {r.status_code}), switching to the next account')
            continue
        if r.status_code != 200:
            raise RuntimeError(f'Cloudflare HTTP {r.status_code}: {r.text[:200]}')
        data = r.json()
        b64 = (data.get('result') or {}).get('image')
        if not b64:
            raise RuntimeError('Cloudflare returned no image: ' + str(data)[:200])
        log(f'cloudflare {label} made an image')
        return Image.open(io.BytesIO(base64.b64decode(b64)))
    raise RuntimeError('Cloudflare HTTP 429: every account is out of its daily allowance')


def get_colab_url():
    url = os.environ.get("COLAB_URL", "").strip()
    src = os.environ.get("COLAB_URL_SOURCE", "").strip()   # optional raw gist link the notebook keeps updated
    if src:
        try:
            u = requests.get(src, timeout=15).text.strip()
            if u.startswith("http"):
                return u.rstrip("/")
        except Exception as e:  # noqa
            log("Could not read Colab URL source:", str(e)[:100])
    return url.rstrip("/")


def img_colab(prompt, compact=None, **_):
    url = get_colab_url()
    if not url:
        raise RuntimeError("Colab URL not set")
    r = requests.post(url + "/generate", json={"prompt": compact or prompt, "width": 768, "height": 1344,
                                                "token": os.environ.get("COLAB_TOKEN", "")}, timeout=240)
    if r.status_code != 200:
        raise RuntimeError(f"Colab HTTP {r.status_code}: {r.text[:200]}")
    return Image.open(io.BytesIO(r.content))


def img_fake(prompt, idx=0, **_):
    random.seed(idx * 7 + 3)
    im = Image.new("RGB", (768, 1344))
    d = ImageDraw.Draw(im)
    c1 = tuple(random.randint(40, 220) for _ in range(3))
    c2 = tuple(random.randint(40, 220) for _ in range(3))
    for y in range(1344):
        f = y / 1343
        d.line([(0, y), (768, y)], fill=tuple(int(c1[i] * (1 - f) + c2[i] * f) for i in range(3)))
    d.ellipse((200, 500, 568, 868), fill=(250, 250, 250), outline=(20, 20, 20), width=8)
    d.text((330, 640), f"scene {idx + 1}", fill=(0, 0, 0))
    return im


PROVIDERS = {"hf": img_hf, "cloudflare": img_cloudflare, "colab": img_colab}


def generate_image(idx, prompt, compact, order):
    if FAKE:
        # local test of the fallback chain: pretend providers fail unless told otherwise
        pass
    for name in order:
        with _lock:
            if _disabled.get(name):
                continue
        for attempt in range(2):
            try:
                if FAKE:
                    if name in os.environ.get("FAKE_FAIL", "").split(","):
                        raise RuntimeError("forced failure for test")
                    im = img_fake(prompt, idx=idx)
                else:
                    im = PROVIDERS[name](prompt, compact=compact)
                log(f"image {idx + 1}: ok via {name}")
                return im, name
            except Exception as e:  # noqa
                msg = str(e)
                log(f"image {idx + 1}: {name} failed ({attempt + 1}/2): {msg[:160]}")
                hard = any(x in msg for x in ("401", "402", "403", "429", "quota", "credit", "not set", "KeyError"))
                with _lock:
                    _fail_counts[name] = _fail_counts.get(name, 0) + 1
                    if hard or _fail_counts[name] >= 4:
                        _disabled[name] = True
                        log(f"-> switching {name} off for the rest of this run")
                if hard:
                    break
                time.sleep(3)
    return None, None


def make_images(scenes, style_look, style_name, order):
    prefix = (f"{style_look} Same recurring main character design and the same color palette in every image, "
              f"no text, no letters, no watermark, vertical 9:16 composition. Scene: ")
    jobs = []
    for i, s in enumerate(scenes):
        full = prefix + s["image_prompt"]
        scene_short = " ".join(s["image_prompt"].split()[:42])
        compact = f"{style_name} illustration, same recurring character, no text. {scene_short}"
        jobs.append((i, full, compact))
    results = [None] * len(scenes)
    used = [None] * len(scenes)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(generate_image, i, full, compact, order): i for i, full, compact in jobs}
        for f, i in futs.items():
            im, name = f.result()
            results[i], used[i] = im, name
    # repair any gaps by reusing the nearest good image so one bad scene never kills the video
    good = [i for i, r in enumerate(results) if r is not None]
    if not good:
        raise RuntimeError("All three image providers failed for every scene")
    for i in range(len(results)):
        if results[i] is None:
            nearest = min(good, key=lambda g: abs(g - i))
            results[i] = results[nearest]
            used[i] = "reused"
            log(f"image {i + 1}: reused image {nearest + 1}")
    return [fit_vertical(r) for r in results], used


# ----------------------------------------------------------------------------- music
def pick_music(mood):
    try:
        url = f"https://drive.google.com/embeddedfolderview?id={MUSIC_FOLDER_ID}"
        html = requests.get(url, timeout=30).text
        files = re.findall(r'/file/d/([A-Za-z0-9_-]+)/view[\s\S]*?flip-entry-title">([^<]+)<', html)
        if not files:
            raise RuntimeError("no tracks listed")
        pool = [f for f in files if f[1].lower().startswith(mood + "-")] or files
        fid, name = random.choice(pool)
        dl = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
        r = requests.get(dl, timeout=90)
        if r.status_code != 200 or len(r.content) < 100_000:
            raise RuntimeError("download failed")
        p = os.path.join(WORK, "music.mp3")
        with open(p, "wb") as fh:
            fh.write(r.content)
        log("music:", name)
        return p, name
    except Exception as e:  # noqa
        log("No music this time:", str(e)[:150])
        return None, ""


def mix_audio(voice, music, total, out):
    fade_out_start = max(0.0, total - 2.0)
    if music:
        fc = (
            f"[0:a]apad=pad_dur=3,atrim=0:{total:.3f},asetpts=N/SR/TB,asplit=2[v1][v2];"
            f"[1:a]volume=0.30,atrim=0:{total:.3f},asetpts=N/SR/TB,"
            f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:.3f}:d=2[m];"
            f"[m][v1]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450:makeup=1[duck];"
            f"[v2][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.95[a]"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", voice, "-stream_loop", "-1", "-i", music,
               "-filter_complex", fc, "-map", "[a]", "-ar", "44100", "-ac", "2", out]
    else:
        fc = f"[0:a]apad=pad_dur=3,atrim=0:{total:.3f},loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.95[a]"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", voice, "-filter_complex", fc,
               "-map", "[a]", "-ar", "44100", "-ac", "2", out]
    subprocess.run(cmd, check=True)


# ----------------------------------------------------------------------------- frames
class CaptionTrack:
    """Groups words into short lines (max 4 words) and draws them with the current word highlighted."""

    def __init__(self, words, font_size=84):
        fp = ensure_font()
        self.font = ImageFont.truetype(fp, font_size) if fp else ImageFont.load_default()
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            text = w[0]
            if len(cur) >= 4 or re.search(r"[.!?,;:]$", text):
                self.chunks.append(cur)
                cur = []
        if cur:
            self.chunks.append(cur)
        self.space = self.font.getlength(" ")
        self.layout = []
        for ch in self.chunks:
            widths = [self.font.getlength(w[0]) for w in ch]
            total = sum(widths) + self.space * (len(ch) - 1)
            if total > W - 120:  # two lines when too wide
                half = (len(ch) + 1) // 2
                lines = [list(range(0, half)), list(range(half, len(ch)))]
            else:
                lines = [list(range(len(ch)))]
            self.layout.append((widths, lines))

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
        d = ImageDraw.Draw(img)
        line_h = int(self.font.size * 1.25)
        y0 = int(H * 0.66) - (len(lines) - 1) * line_h // 2
        for li, line in enumerate(lines):
            tw = sum(widths[k] for k in line) + self.space * (len(line) - 1)
            x = (W - tw) / 2
            y = y0 + li * line_h
            for k in line:
                word = ch[k]
                active = word[1] - 0.03 <= t < word[2] + 0.05
                done = t >= word[2] + 0.05
                color = (255, 230, 0) if active else (255, 255, 255)
                if not (active or done):
                    color = (255, 255, 255)
                d.text((x, y), word[0], font=self.font, fill=color, stroke_width=8, stroke_fill=(0, 0, 0))
                x += widths[k] + self.space


def ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def smoother(x):
    x = min(1.0, max(0.0, x))
    return x * x * x * (x * (x * 6 - 15) + 10)


def transition_frame(kind, old, new, p):
    """One frame of a gentle cross-dissolve from image old to image new (p goes 0 to 1). No movement, no dark dip."""
    if kind == 'fade':
        return Image.blend(old, new, ease(p))
    return Image.blend(old, new, smoother(p))


def render_video(canvases, bounds, captions, total, audio_path, out_path):
    nframes = int(math.ceil(total * FPS))
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}',
           '-r', str(FPS), '-i', '-', '-i', audio_path, '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', os.environ.get('X264_PRESET', 'veryfast'), '-crf', '20',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-shortest', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = len(canvases)
    # images stay completely still; only a short cross-fade between scenes
    statics = [c.resize((W, H), Image.LANCZOS) for c in canvases]
    kinds = []
    prev = None
    for _ in range(max(0, n - 1)):
        kind = random.choice([x for x in TRANSITIONS if x != prev])
        kinds.append(kind)
        prev = kind
    log('transitions:', kinds)
    for f in range(nframes):
        t = f / FPS
        k = 0
        while k + 1 < n and t >= bounds[k + 1]:
            k += 1
        start, end = bounds[k], bounds[k + 1]
        frame = None
        if k + 1 < n:
            d = TRANSITIONS[kinds[k]]
            if t > end - d / 2:
                frame = transition_frame(kinds[k], statics[k], statics[k + 1], (t - (end - d / 2)) / d)
        if frame is None and k > 0:
            d = TRANSITIONS[kinds[k - 1]]
            if t < start + d / 2:
                frame = transition_frame(kinds[k - 1], statics[k - 1], statics[k], (t - (start - d / 2)) / d)
        if frame is None:
            frame = statics[k].copy()
        captions.draw(frame, t)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg video encode failed')


# ----------------------------------------------------------------------------- upload + callback
def upload_video(path):
    urls = []
    try:
        with open(path, "rb") as fh:
            r = requests.post("https://uguu.se/upload?output=text", files={"files[]": ("video.mp4", fh, "video/mp4")},
                              timeout=300)
        u = r.text.strip()
        if r.status_code == 200 and u.startswith("http"):
            urls.append(u)
            log("uploaded to uguu:", u)
    except Exception as e:  # noqa
        log("uguu upload failed:", str(e)[:150])
    try:
        with open(path, "rb") as fh:
            r = requests.post("https://catbox.moe/user/api.php", data={"reqtype": "fileupload"},
                              files={"fileToUpload": ("video.mp4", fh, "video/mp4")}, timeout=300)
        u = r.text.strip()
        if r.status_code == 200 and u.startswith("http"):
            urls.append(u)
            log("uploaded to catbox:", u)
    except Exception as e:  # noqa
        log("catbox upload failed:", str(e)[:150])
    return urls


def callback(url, body):
    if not url:
        log("No callback URL; result:", json.dumps(body)[:300])
        return
    for i in range(5):
        try:
            r = requests.post(url, json=body, timeout=30)
            log("callback status", r.status_code)
            if r.status_code < 300:
                return
        except Exception as e:  # noqa
            log("callback error", str(e)[:150])
        time.sleep(5 * (i + 1))


# ----------------------------------------------------------------------------- main
def main():
    payload = json.loads(os.environ["PAYLOAD"])
    cb = payload.get("callback_url", "")
    if "--notify-failure" in sys.argv:
        callback(cb, {"status": "error", "message": "The GitHub render job failed. Open the run log in GitHub Actions."})
        return
    try:
        scenes = payload["scenes"]
        narration = " ".join(s["narration"].strip() for s in scenes)
        script_words = narration.split()
        order = [x.strip() for x in os.environ.get("IMAGE_PROVIDER_ORDER", "hf,cloudflare,colab").split(",") if x.strip()]
        log("provider order:", order)

        voice = os.path.join(WORK, "voice.wav")
        make_voice(narration, voice)
        dur = audio_duration(voice)
        log(f"voice {dur:.1f}s")
        if dur > 58:
            raise RuntimeError(f"Voiceover is {dur:.0f}s, over the 58 second limit")

        words = align_words(script_words, dur, voice)

        # scene boundaries from the first word of each scene
        bounds = [0.0]
        wi = 0
        for s in scenes[:-1]:
            wi += len(s["narration"].split())
            bounds.append(max(bounds[-1] + 0.8, words[min(wi, len(words) - 1)][1] - 0.05))
        total = dur + 0.9
        bounds.append(total)

        canvases, used = make_images(scenes, payload.get("style_look", ""), payload.get("art_style", "illustration"), order)
        music_path, music_name = pick_music(payload.get("mood", "inspiring"))
        mixed = os.path.join(WORK, "mix.wav")
        mix_audio(voice, music_path, total, mixed)

        out_path = os.path.join(OUT, "video.mp4")
        render_video(canvases, bounds, CaptionTrack(words), total, mixed, out_path)
        size = os.path.getsize(out_path) / 1e6
        log(f"video done {total:.1f}s {size:.1f}MB")

        urls = [] if FAKE else upload_video(out_path)
        result = {"status": "success", "video_urls": urls, "duration": total, "music": music_name,
                  "images_used": used, "title": payload.get("title", ""), "size_mb": round(size, 1)}
        with open(os.path.join(OUT, "result.json"), "w") as fh:
            json.dump(result, fh)
        if not urls and not FAKE:
            result["status"] = "error"
            result["message"] = "Video rendered but every upload host failed. It is saved as the GitHub release asset."
        callback(cb, result)
    except Exception as e:  # noqa
        traceback.print_exc()
        callback(cb, {"status": "error", "message": str(e)[:300]})
        sys.exit(1)


if __name__ == "__main__":
    main()
