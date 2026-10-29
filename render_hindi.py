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
TRANSITIONS = {}   # name: seconds. Only the short cross-dissolve
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
    """Hindi captions need a Devanagari font: use the installed Noto font, else download Hind Bold."""
    global FONT_PATH
    try:
        from PIL import features
        log('raqm text shaping available:', features.check('raqm'))
    except Exception:  # noqa
        pass
    for cand in ('/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf',
                 '/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf'):
        if os.path.exists(cand):
            FONT_PATH = cand
            return cand
    hind = os.path.join(HERE, 'fonts', 'Hind-Bold.ttf')
    if os.path.exists(hind):
        FONT_PATH = hind
        return hind
    try:
        os.makedirs(os.path.dirname(hind), exist_ok=True)
        r = requests.get('https://raw.githubusercontent.com/google/fonts/main/ofl/hind/Hind-Bold.ttf', timeout=30)
        if r.status_code == 200 and len(r.content) > 50000:
            with open(hind, 'wb') as fh:
                fh.write(r.content)
            FONT_PATH = hind
            return hind
    except Exception as e:  # noqa
        log('font download failed:', str(e)[:100])
    raise RuntimeError('No Devanagari font is available for the Hindi captions')


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
EL_ALIGN = {}


VOICE_NAME = 'Hindi Reel Creator Male'
VOICE_DESC = 'A vibrant young adult male in his mid-20s speaking fluent, modern Hindi. He has a highly energetic reel-creator persona, a crisp and sharp vocal texture, and a naturally punchy, fast-talking delivery style that sounds charismatic and engaging.'
VOICE_SAMPLE = 'दोस्तों, क्या आप जानते हैं कि इस दुनिया में ऐसे कई रहस्य हैं जिनका जवाब आज तक किसी के पास नहीं है? आज की कहानी सुनकर आप हैरान रह जाएंगे, तो बने रहिए आखिर तक!'


def el_post(url, headers, body):
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=180)
    if r.status_code != 200:
        raise RuntimeError('ElevenLabs HTTP ' + str(r.status_code) + ': ' + r.text[:200])
    return r.json()


def ensure_designed_voice(key):
    h = {'xi-api-key': key, 'Content-Type': 'application/json'}
    r = requests.get('https://api.elevenlabs.io/v1/voices', headers=h, timeout=60)
    if r.status_code == 200:
        for v in r.json().get('voices', []):
            if v.get('name') == VOICE_NAME:
                log('using saved designed voice:', v.get('voice_id'))
                return v.get('voice_id')
    log('designing the Hindi reel voice (first run only)')
    try:
        d = el_post('https://api.elevenlabs.io/v1/text-to-voice/design', h,
                    {'voice_description': VOICE_DESC, 'text': VOICE_SAMPLE, 'model_id': 'eleven_multilingual_ttv_v2'})
    except Exception as e:  # noqa
        log('design endpoint failed, trying the older one:', str(e)[:150])
        d = el_post('https://api.elevenlabs.io/v1/text-to-voice/create-previews', h,
                    {'voice_description': VOICE_DESC, 'text': VOICE_SAMPLE})
    gen = d['previews'][0]['generated_voice_id']
    body = {'voice_name': VOICE_NAME, 'voice_description': VOICE_DESC, 'generated_voice_id': gen}
    try:
        v = el_post('https://api.elevenlabs.io/v1/text-to-voice', h, body)
    except Exception as e:  # noqa
        log('create endpoint failed, trying the older one:', str(e)[:150])
        v = el_post('https://api.elevenlabs.io/v1/text-to-voice/create-voice-from-preview', h, body)
    log('designed voice saved:', v.get('voice_id'))
    return v['voice_id']


def make_voice_elevenlabs(text, path):
    if FAKE:
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=180:duration=41,tremolo=f=4:d=0.9', '-ar', '24000', '-ac', '1', path], check=True)
        return
    key = os.environ['ELEVENLABS_API_KEY']
    voice_id = ensure_designed_voice(key)
    url = 'https://api.elevenlabs.io/v1/text-to-speech/' + voice_id + '/with-timestamps?output_format=mp3_44100_128'
    body = {'text': text, 'model_id': 'eleven_multilingual_v2',
            'voice_settings': {'stability': 0.35, 'similarity_boost': 0.8, 'style': 0.5, 'use_speaker_boost': True, 'speed': 1.1}}
    h = {'xi-api-key': key, 'Content-Type': 'application/json'}
    data = None
    last = ''
    for i in range(3):
        r = requests.post(url, headers=h, data=json.dumps(body), timeout=180)
        if r.status_code == 200:
            data = r.json()
            break
        last = 'ElevenLabs HTTP ' + str(r.status_code) + ': ' + r.text[:200]
        log(last)
        if r.status_code in (401, 402, 403, 404, 422):
            break
        time.sleep(4 * (i + 1))
    if data is None:
        raise RuntimeError(last)
    mp3 = path + '.mp3'
    with open(mp3, 'wb') as fh:
        fh.write(base64.b64decode(data['audio_base64']))
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', mp3, '-ar', '24000', '-ac', '1', path], check=True)
    EL_ALIGN.update(data.get('alignment') or data.get('normalized_alignment') or {})


def make_voice_fish(text, path):
    payload = json.loads(os.environ['PAYLOAD'])
    key = os.environ['FISH_API_KEY']
    body = {'text': text, 'format': 'mp3', 'mp3_bitrate': 128, 'latency': 'normal', 'prosody': {'speed': 1.1, 'volume': 0}}
    ref = payload.get('fish_voice_id') or ''
    if ref:
        body['reference_id'] = ref
    models = ['', payload.get('fish_model') or 's2.1-pro-free']

    def call_with(model):
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        if model:
            headers['model'] = model
        r = requests.post('https://api.fish.audio/v1/tts', headers=headers, data=json.dumps(body), timeout=180)
        if r.status_code != 200:
            raise RuntimeError('Fish Audio HTTP ' + str(r.status_code) + ': ' + r.text[:200])
        return r.content

    audio = None
    last = None
    for model in models:
        try:
            audio = retry(lambda: call_with(model), tries=2, delay=4, what='Fish Audio TTS (' + (model or 'default model') + ')')
            log('Fish Audio voice made with model:', model or 'default')
            break
        except Exception as e:  # noqa
            last = e
            log('Fish Audio model failed:', model or 'default', str(e)[:150])
    if audio is None:
        raise last
    mp3 = path + '.mp3'
    with open(mp3, 'wb') as fh:
        fh.write(audio)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', mp3, '-ar', '24000', '-ac', '1', path], check=True)
    EL_ALIGN.clear()


def make_voice(text, path):
    try:
        make_voice_elevenlabs(text, path)
        log('voice made by ElevenLabs')
    except Exception as e:  # noqa
        log('ElevenLabs failed, switching to Fish Audio:', str(e)[:200])
        make_voice_fish(text, path)
        log('voice made by Fish Audio')


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


def el_words(script_words):
    """Word timing from the ElevenLabs character timestamps (exact, no Whisper needed)."""
    try:
        chars = EL_ALIGN.get('characters') or []
        st = EL_ALIGN.get('character_start_times_seconds') or []
        en = EL_ALIGN.get('character_end_times_seconds') or []
        if not chars or len(chars) != len(st) or len(chars) != len(en):
            return None
        if ''.join(''.join(chars).split()) != ''.join(script_words):
            return None
        out = []
        i = 0
        n = len(chars)
        for w in script_words:
            while i < n and chars[i].isspace():
                i += 1
            if i >= n:
                return None
            s = i
            i = min(n, i + len(w))
            out.append((w, float(st[s]), float(en[i - 1])))
        return out
    except Exception:  # noqa
        return None


def whisper_heard(audio_path):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get('WHISPER_MODEL', 'small'), device='cpu', compute_type='int8')
    segs, _ = model.transcribe(audio_path, word_timestamps=True, language='hi', vad_filter=False,
                               condition_on_previous_text=False)
    out = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                out.append((t, float(w.start), float(w.end)))
    return out


def map_by_chars(script_words, heard):
    """Place every script word on the real speech timeline using what Whisper heard (pauses and pace included)."""
    if len(heard) < 3:
        raise RuntimeError('Whisper heard too few words')
    pos = [0]
    for h in heard:
        pos.append(pos[-1] + len(h[0]))
    lw = float(pos[-1])
    sw = [len(w) for w in script_words]
    ls = float(sum(sw))

    def time_at(x, is_start):
        x = min(max(x, 0.0), lw)
        for i in range(len(heard)):
            inside = (x < pos[i + 1]) if is_start else (x <= pos[i + 1])
            if inside or i == len(heard) - 1:
                a = pos[i]
                b = pos[i + 1]
                frac = 0.0 if b == a else min(1.0, max(0.0, (x - a) / (b - a)))
                return heard[i][1] + frac * (heard[i][2] - heard[i][1])
        return heard[-1][2]

    out = []
    c = 0.0
    for w, n in zip(script_words, sw):
        s = time_at(c / ls * lw, True)
        e = time_at((c + n) / ls * lw, False)
        c += n
        out.append((w, s, max(e, s + 0.12)))
    return out


def find_silences(audio_path):
    """Middle points of the real pauses in the voice, used to cut images at natural breaks."""
    try:
        r = subprocess.run(['ffmpeg', '-i', audio_path, '-af', 'silencedetect=noise=-32dB:d=0.1', '-f', 'null', '-'],
                           capture_output=True, text=True)
        starts = []
        mids = []
        for line in r.stderr.splitlines():
            if 'silence_start:' in line:
                starts.append(float(line.split('silence_start:')[1].strip().split()[0]))
            elif 'silence_end:' in line and starts:
                e = float(line.split('silence_end:')[1].strip().split()[0])
                mids.append((starts.pop() + e) / 2.0)
        return mids
    except Exception:  # noqa
        return []


def align_words(script_words, duration, audio_path):
    got = el_words(script_words)
    if got:
        log('ElevenLabs word timing ok (' + str(len(got)) + ' words)')
        return got
    try:
        heard = whisper_heard(audio_path)
        log('Whisper heard ' + str(len(heard)) + ' words, mapping the script onto them')
        return map_by_chars(script_words, heard)
    except Exception as e:  # noqa
        log('Using estimated word timing:', str(e)[:150])
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
    for suffix in ('', '_2', '_3', '_4', '_5', '_6'):
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
                          json={'prompt': prompt[:2000], 'steps': 4}, timeout=120)
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


_poll_lock = threading.Lock()


def img_pollinations(prompt, **_):
    """Free fallback that needs no key. One request at a time, retried a few times."""
    url = ('https://image.pollinations.ai/prompt/' + requests.utils.quote(prompt[:1500]) +
           '?width=720&height=1280&model=flux&nologo=true&private=true&seed=' + str(random.randint(1, 999999)))
    last = ''
    with _poll_lock:
        for i in range(4):
            try:
                r = requests.get(url, timeout=150)
                if r.status_code == 200 and len(r.content) > 5000:
                    return Image.open(io.BytesIO(r.content))
                last = 'Pollinations HTTP ' + str(r.status_code)
            except Exception as e:  # noqa
                last = 'Pollinations error ' + str(e)[:100]
            time.sleep(6 * (i + 1))
    raise RuntimeError(last)


# Hugging Face ZeroGPU Spaces through gradio_client. One free ZeroGPU quota per account (HF_TOKEN, HF_TOKEN_2 ... HF_TOKEN_6)
def hf_tokens():
    toks = []
    for suffix in ('', '_2', '_3', '_4', '_5', '_6'):
        t = os.environ.get('HF_TOKEN' + suffix, '').strip()
        if t:
            toks.append(('hf account ' + (suffix[1:] if suffix else '1'), t))
    return toks


_hf_dead = set()
_hf_turn = [0]
_hf_clients = {}
_hf_locks = {}


def hf_client(label, tok, space):
    key = label + '|' + space
    with _lock:
        c = _hf_clients.get(key)
    if c is not None:
        return c
    from gradio_client import Client
    try:
        c = Client(space, token=tok, verbose=False)
    except TypeError:
        c = Client(space, hf_token=tok, verbose=False)
    with _lock:
        _hf_clients[key] = c
    return c


def img_hfspace(prompt, **_):
    toks = hf_tokens()
    if not toks:
        raise RuntimeError('Hugging Face tokens not set')
    space = os.environ.get('HF_SPACE') or 'black-forest-labs/FLUX.1-schnell'
    with _lock:
        live = [t for t in toks if t[0] not in _hf_dead]
        turn = _hf_turn[0]
        _hf_turn[0] += 1
    if not live:
        raise RuntimeError('Hugging Face ZeroGPU quota: every account is used up for today')
    for k in range(len(live)):
        label, tok = live[(turn + k) % len(live)]
        with _lock:
            lk = _hf_locks.setdefault(label, threading.Lock())
        with lk:
            try:
                client = hf_client(label, tok, space)
                res = client.predict(prompt=prompt[:1500], seed=0, randomize_seed=True, width=720, height=1280,
                                     num_inference_steps=4, api_name='/infer')
                path = res[0] if isinstance(res, (list, tuple)) else res
                if isinstance(path, dict):
                    path = path.get('path') or path.get('url')
                im = Image.open(path)
                im.load()
                log(label + ' (ZeroGPU) made an image')
                return im
            except Exception as e:  # noqa
                msg = str(e)
                low = msg.lower()
                gone = ('quota' in low) or ('unauthorized' in low) or ('401' in msg) or ('403' in msg) or ('429' in msg)
                if gone:
                    with _lock:
                        if label not in _hf_dead:
                            _hf_dead.add(label)
                            log(label + ' is unavailable (' + msg[:110] + '), switching to the next account')
                    continue
                raise RuntimeError('Hugging Face Space error: ' + msg[:200])
    raise RuntimeError('Hugging Face ZeroGPU quota: every account is used up for today')


PROVIDERS = {'hf': img_hf, 'hfspace': img_hfspace, 'cloudflare': img_cloudflare, 'colab': img_colab, 'pollinations': img_pollinations}


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
                    if hard or _fail_counts[name] >= 6:
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
    with ThreadPoolExecutor(max_workers=6) as ex:
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
        folder = os.path.join(HERE, 'music_hindi')
        files = []
        if os.path.isdir(folder):
            for n in sorted(os.listdir(folder)):
                if n.lower().endswith(('.mp3', '.wav', '.m4a', '.ogg')):
                    files.append(n)
        if not files:
            raise RuntimeError('no music files in the music_hindi folder')
        pool = [n for n in files if n.lower().startswith(mood + '-')] or files
        name = random.choice(pool)
        log('music:', name)
        return os.path.join(folder, name), name
    except Exception as e:  # noqa
        log('No music this time:', str(e)[:150])
        return None, ''


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

    def __init__(self, words, font_size=88):
        fp = ensure_font()
        self.font = ImageFont.truetype(fp, font_size, layout_engine=ImageFont.Layout.RAQM) if fp else ImageFont.load_default()
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            text = w[0]
            if len(cur) >= 4 or re.search(r"[.!?,;:।]$", text):
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
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', str(W) + 'x' + str(H),
           '-r', str(FPS), '-i', '-', '-i', audio_path, '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', os.environ.get('X264_PRESET', 'veryfast'), '-crf', '20',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-shortest', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = len(canvases)
    statics = [c.resize((W, H), Image.LANCZOS) for c in canvases]
    log('hard cuts between scenes, no transitions')
    for f in range(nframes):
        t = f / FPS
        k = 0
        while k + 1 < n and t >= bounds[k + 1]:
            k += 1
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
        order = [x.strip() for x in (os.environ.get('HINDI_IMAGE_ORDER') or 'hfspace,cloudflare,pollinations').split(',') if x.strip()]
        log("provider order:", order)

        voice = os.path.join(WORK, "voice.wav")
        if 'pollinations' not in order:
            order.append('pollinations')
        make_voice(narration, voice)
        dur = audio_duration(voice)
        log(f"voice {dur:.1f}s")
        if dur > 58:
            raise RuntimeError(f"Voiceover is {dur:.0f}s, over the 58 second limit")

        words = align_words(script_words, dur, voice)

        # scene boundaries from the first word of each scene, snapped to the nearest real pause
        first_words = []
        wi = 0
        for s in scenes[:-1]:
            wi += len(s['narration'].split())
            first_words.append(wi)
        sil = find_silences(voice)
        bounds = [0.0]
        for wi in first_words:
            b = words[min(wi, len(words) - 1)][1] - 0.04
            cand = [m for m in sil if abs(m - b) <= 0.35]
            if cand:
                b = min(cand, key=lambda m: abs(m - b))
            bounds.append(max(bounds[-1] + 0.8, b))
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
