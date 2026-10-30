#!/usr/bin/env python3
"""
Landscape (16:9) long-form Hindi video renderer (runs inside GitHub Actions).

n8n commits one JSON job file (script split into scenes + image prompts) to jobs/ and starts this workflow.
Steps: Gemini male voiceover made in parts -> word timing (Whisper, part by part) -> images (same providers as
render_hindi.py, wide size) -> music -> frames with captions + ffmpeg -> upload -> callback to n8n.
"""
import base64
import bisect
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

import render_hindi as rh

W, H = 1920, 1080
FPS = 24
WORK = rh.WORK
OUT = rh.OUT
log = rh.log
PCM_RATE = 24000
GAP_SEC = 0.35
CHUNK_WORDS = 95


# ----------------------------------------------------------------------------- voice (Gemini TTS, in parts)
def tts_chunk(text, voice, models, key):
    last = ''
    for model in models:
        url = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent'
        body = {'contents': [{'parts': [{'text': text}]}],
                'generationConfig': {'responseModalities': ['AUDIO'],
                                     'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice}}}}}
        for attempt in range(4):
            try:
                r = requests.post(url, headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                                  data=json.dumps(body), timeout=300)
                if r.status_code == 200:
                    data = r.json()
                    for cand in data.get('candidates') or []:
                        for p in (cand.get('content') or {}).get('parts') or []:
                            inl = p.get('inlineData') or p.get('inline_data')
                            if inl and inl.get('data'):
                                pcm = base64.b64decode(inl['data'])
                                pcm = pcm[:len(pcm) // 2 * 2]
                                if len(pcm) > PCM_RATE:
                                    return pcm
                    last = 'Gemini TTS returned no audio'
                else:
                    last = 'Gemini TTS HTTP ' + str(r.status_code) + ': ' + r.text[:160]
                    if r.status_code in (400, 401, 403, 404):
                        log(model + ' ' + last)
                        break
            except Exception as e:  # noqa
                last = 'Gemini TTS error ' + str(e)[:120]
            log(model + ' ' + last + ' (try ' + str(attempt + 1) + '/4)')
            time.sleep((25 if '429' in last else 8) * (attempt + 1))
    raise RuntimeError(last)


def make_voice(scenes, payload, path):
    key = os.environ['GEMINI_API_KEY']
    voice = payload.get('tts_voice') or 'Charon'
    models = []
    for m in [payload.get('tts_model'), 'gemini-2.5-flash-preview-tts', 'gemini-2.5-pro-preview-tts']:
        if m and m not in models:
            models.append(m)
    chunks = []
    cur = []
    cw = 0
    for i, s in enumerate(scenes):
        n = len(s['narration'].split())
        if cur and cw + n > CHUNK_WORDS:
            chunks.append(cur)
            cur = []
            cw = 0
        cur.append(i)
        cw += n
    if cur:
        chunks.append(cur)
    log('voice: ' + str(len(chunks)) + ' parts, voice ' + voice + ', models ' + ','.join(models))
    pcm_all = bytearray()
    gap = bytes(int(PCM_RATE * GAP_SEC) * 2)
    ranges = []
    for ci, idxs in enumerate(chunks):
        text = ' '.join(scenes[i]['narration'].strip() for i in idxs)
        log('voice part ' + str(ci + 1) + '/' + str(len(chunks)) + ' (' + str(len(text.split())) + ' words)')
        pcm = tts_chunk(text, voice, models, key)
        t0 = len(pcm_all) / 2.0 / PCM_RATE
        pcm_all.extend(pcm)
        t1 = len(pcm_all) / 2.0 / PCM_RATE
        ranges.append((idxs, t0, t1))
        pcm_all.extend(gap)
        time.sleep(3)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(PCM_RATE)
        wf.writeframes(bytes(pcm_all))
    return ranges


def align_parts(scenes, ranges, voice_path):
    """Word timing: Whisper on the whole voice, then every part is mapped on its own time range so there is no drift."""
    heard = None
    try:
        heard = rh.whisper_heard(voice_path)
        log('Whisper heard ' + str(len(heard)) + ' words')
    except Exception as e:  # noqa
        log('Whisper not available, using estimated timing: ' + str(e)[:150])
    words = []
    for idxs, t0, t1 in ranges:
        cw = ' '.join(scenes[i]['narration'].strip() for i in idxs).split()
        got = None
        if heard:
            hs = [h for h in heard if t0 - 0.3 <= h[1] < t1 + 0.1]
            if len(hs) >= 3:
                try:
                    got = rh.map_by_chars(cw, hs)
                except Exception:  # noqa
                    got = None
        if got is None:
            got = [(w, s + t0, e + t0) for (w, s, e) in rh.estimate_words(cw, max(1.0, t1 - t0))]
        words.extend(got)
    return words


def scene_bounds(scenes, words, sil, total):
    bounds = [0.0]
    wi = 0
    for s in scenes[:-1]:
        wi += len(s['narration'].split())
        b = words[min(wi, len(words) - 1)][1] - 0.05
        cand = [m for m in sil if abs(m - b) <= 0.4]
        if cand:
            b = min(cand, key=lambda m: abs(m - b))
        bounds.append(max(bounds[-1] + 1.0, b))
    bounds.append(total)
    return bounds


# ----------------------------------------------------------------------------- images (wide)
def fit_wide(img):
    img = img.convert('RGB')
    ratio = img.width / float(img.height)
    if 1.65 <= ratio <= 1.9:
        return img.resize((W, H), Image.LANCZOS)
    bg = img.copy()
    scale = max(W / float(bg.width), H / float(bg.height))
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1), Image.BILINEAR)
    l, t = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((l, t, l + W, t + H)).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new('RGB', bg.size, (0, 0, 0)), 0.35)
    s = H / float(img.height)
    fg = img.resize((max(1, int(img.width * s)), H), Image.LANCZOS)
    if fg.width > W:
        fg = img.resize((W, max(1, int(img.height * W / float(img.width)))), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg


def img_hfspace_wide(prompt, **_):
    toks = rh.hf_tokens()
    if not toks:
        raise RuntimeError('Hugging Face tokens not set')
    space = os.environ.get('HF_SPACE') or 'black-forest-labs/FLUX.1-schnell'
    last = ''
    for rnd in range(3):
        with rh._lock:
            live = [t for t in toks if t[0] not in rh._hf_dead]
            turn = rh._hf_turn[0]
            rh._hf_turn[0] += 1
        if not live:
            raise RuntimeError('Hugging Face ZeroGPU quota: every account is used up for today')
        for k in range(len(live)):
            label, tok = live[(turn + k) % len(live)]
            with rh._lock:
                lk = rh._hf_locks.setdefault(label, threading.Lock())
            if not lk.acquire(timeout=240):
                continue
            job = None
            try:
                client = rh.hf_client(label, tok, space)
                job = client.submit(prompt=prompt[:1500], seed=0, randomize_seed=True, width=1280, height=720,
                                    num_inference_steps=4, api_name='/infer')
                res = job.result(timeout=200)
                path = res[0] if isinstance(res, (list, tuple)) else res
                if isinstance(path, dict):
                    path = path.get('path') or path.get('url')
                im = Image.open(path)
                im.load()
                log(label + ' (ZeroGPU) made a wide image')
                return im
            except Exception as e:  # noqa
                msg = str(e)
                low = msg.lower()
                try:
                    if job is not None:
                        job.cancel()
                except Exception:  # noqa
                    pass
                gone = ('quota' in low) or ('unauthorized' in low) or ('401' in msg) or ('403' in msg)
                if gone:
                    with rh._lock:
                        if label not in rh._hf_dead:
                            rh._hf_dead.add(label)
                            log(label + ' is used up or blocked (' + msg[:110] + '), switching to the next account')
                    continue
                last = msg[:200]
                log(label + ' queue busy or failed, trying another account: ' + last[:120])
                with rh._lock:
                    rh._hf_clients.pop(label + '|' + space, None)
                continue
            finally:
                lk.release()
        time.sleep(8 * (rnd + 1))
    log('Hugging Face gave no image this time, last problem: ' + last[:150])
    raise RuntimeError('Hugging Face queue was busy or failed, falling back to the next image source')


def img_pollinations_wide(prompt, **_):
    url = ('https://image.pollinations.ai/prompt/' + requests.utils.quote(prompt[:1500]) +
           '?width=1280&height=720&model=flux&nologo=true&private=true&seed=' + str(random.randint(1, 999999)))
    last = ''
    with rh._poll_lock:
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


def make_images(scenes, style_look, style_name, order):
    rh.PROVIDERS['hfspace'] = img_hfspace_wide
    rh.PROVIDERS['pollinations'] = img_pollinations_wide
    prefix = (style_look + ' Same recurring main character design and the same color palette in every image, '
              'no text, no letters, no watermark, wide cinematic 16:9 landscape composition with the subject near the centre. Scene: ')
    jobs = []
    for i, s in enumerate(scenes):
        full = prefix + s['image_prompt']
        short = ' '.join(s['image_prompt'].split()[:42])
        compact = style_name + ' illustration, same recurring character, no text, wide 16:9. ' + short
        jobs.append((i, full, compact))
    results = [None] * len(scenes)
    used = [None] * len(scenes)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [(i, ex.submit(rh.generate_image, i, full, compact, order)) for i, full, compact in jobs]
        for i, f in futs:
            im, name = f.result()
            results[i], used[i] = im, name
    good = [i for i, r in enumerate(results) if r is not None]
    if not good:
        raise RuntimeError('All image providers failed for every scene')
    for i in range(len(results)):
        if results[i] is None:
            nearest = min(good, key=lambda g: abs(g - i))
            results[i] = results[nearest]
            used[i] = 'reused'
            log('image ' + str(i + 1) + ': reused image ' + str(nearest + 1))
    return [fit_wide(r) for r in results], used


# ----------------------------------------------------------------------------- captions + video
class WideCaptions:
    def __init__(self, words, size=60):
        fp = rh.ensure_font()
        self.font = ImageFont.truetype(fp, size, layout_engine=ImageFont.Layout.RAQM)
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            if len(cur) >= 6 or re.search(r'[.!?,;:।]$', w[0]):
                self.chunks.append(cur)
                cur = []
        if cur:
            self.chunks.append(cur)
        self.space = self.font.getlength(' ')
        self.layout = []
        for ch in self.chunks:
            widths = [self.font.getlength(w[0]) for w in ch]
            total = sum(widths) + self.space * (len(ch) - 1)
            if total > W - 260:
                half = (len(ch) + 1) // 2
                lines = [list(range(0, half)), list(range(half, len(ch)))]
            else:
                lines = [list(range(len(ch)))]
            self.layout.append((widths, lines))
        self.starts = [ch[0][1] - 0.02 for ch in self.chunks]

    def state(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return None
        ch = self.chunks[i]
        nxt = self.starts[i + 1] if i + 1 < len(self.chunks) else 1e9
        if t >= min(nxt, ch[-1][2] + 0.3):
            return None
        active = -1
        for k, w in enumerate(ch):
            if w[1] - 0.03 <= t < w[2] + 0.05:
                active = k
                break
        return (i, active)

    def draw(self, img, st):
        if st is None:
            return
        i, active = st
        ch = self.chunks[i]
        widths, lines = self.layout[i]
        d = ImageDraw.Draw(img)
        line_h = int(self.font.size * 1.3)
        y0 = int(H * 0.80) - (len(lines) - 1) * line_h // 2
        for li, line in enumerate(lines):
            tw = sum(widths[k] for k in line) + self.space * (len(line) - 1)
            x = (W - tw) / 2
            y = y0 + li * line_h
            for k in line:
                color = (255, 230, 0) if k == active else (255, 255, 255)
                d.text((x, y), ch[k][0], font=self.font, fill=color, stroke_width=6, stroke_fill=(0, 0, 0))
                x += widths[k] + self.space


def render_video(canvases, bounds, caps, total, audio_path, out_path):
    nframes = int(math.ceil(total * FPS))
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', str(W) + 'x' + str(H),
           '-r', str(FPS), '-i', '-', '-i', audio_path, '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', os.environ.get('X264_PRESET', 'veryfast'), '-tune', 'stillimage', '-crf', '21',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-shortest', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = len(canvases)
    log('encoding ' + str(nframes) + ' frames, hard cuts between scenes')
    k = 0
    cache_key = None
    cache_bytes = b''
    for f in range(nframes):
        t = f / float(FPS)
        while k + 1 < n and t >= bounds[k + 1]:
            k += 1
        st = caps.state(t)
        key = (k, st)
        if key != cache_key:
            frame = canvases[k].copy()
            caps.draw(frame, st)
            cache_bytes = frame.tobytes()
            cache_key = key
        proc.stdin.write(cache_bytes)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg video encode failed')


# ----------------------------------------------------------------------------- main
def load_payload():
    with open(os.environ['JOB_FILE'], 'r', encoding='utf-8') as fh:
        return json.load(fh)


def main():
    cb = ''
    try:
        payload = load_payload()
        cb = payload.get('callback_url', '')
    except Exception as e:  # noqa
        log('Could not read the job file: ' + str(e)[:150])
        payload = {}
    if '--notify-failure' in sys.argv:
        rh.callback(cb, {'status': 'error', 'message': 'The GitHub landscape render job failed. Open the run log in GitHub Actions.'})
        return
    try:
        scenes = payload['scenes']
        order = [x.strip() for x in (payload.get('image_order') or os.environ.get('HINDI_IMAGE_ORDER') or 'hfspace,cloudflare,pollinations').split(',') if x.strip()]
        if 'pollinations' not in order:
            order.append('pollinations')
        log('scenes: ' + str(len(scenes)) + ', provider order: ' + str(order))

        voice = os.path.join(WORK, 'voice.wav')
        ranges = make_voice(scenes, payload, voice)
        dur = rh.audio_duration(voice)
        log('voice ' + str(round(dur, 1)) + 's')
        if dur > 660:
            raise RuntimeError('Voiceover is ' + str(int(dur)) + 's, over the 11 minute limit')

        words = align_parts(scenes, ranges, voice)
        sil = rh.find_silences(voice)
        total = dur + 1.5
        bounds = scene_bounds(scenes, words, sil, total)

        canvases, used = make_images(scenes, payload.get('style_look', ''), payload.get('art_style', 'illustration'), order)
        music_path, music_name = rh.pick_music(payload.get('mood', 'inspiring'))
        mixed = os.path.join(WORK, 'mix.wav')
        rh.mix_audio(voice, music_path, total, mixed)

        out_path = os.path.join(OUT, 'video.mp4')
        render_video(canvases, bounds, WideCaptions(words), total, mixed, out_path)
        size = os.path.getsize(out_path) / 1e6
        log('video done ' + str(round(total, 1)) + 's ' + str(round(size, 1)) + 'MB')

        urls = rh.upload_video(out_path)
        result = {'status': 'success', 'video_urls': urls, 'duration': total, 'music': music_name,
                  'images_used': used, 'title': payload.get('title', ''), 'size_mb': round(size, 1)}
        with open(os.path.join(OUT, 'result.json'), 'w') as fh:
            json.dump(result, fh)
        if not urls:
            result['status'] = 'error'
            result['message'] = 'Video rendered but every upload host failed. It is saved as the GitHub release asset.'
        rh.callback(cb, result)
    except Exception as e:  # noqa
        traceback.print_exc()
        rh.callback(cb, {'status': 'error', 'message': str(e)[:300]})
        sys.exit(1)


if __name__ == '__main__':
    main()
