#!/usr/bin/env python3
'''Landscape (16:9) long-form Hindi video renderer, runs in GitHub Actions.

n8n commits a job JSON file (script split into scenes with image prompts) to the repo and dispatches this workflow with its path.
Steps: Gemini male voice in parts -> Whisper word timing per part -> 16:9 images (same providers as render_hindi.py) ->
hard-cut video with Hindi captions -> upload -> callback to n8n. Reuses helpers from render_hindi.py.
'''
import base64
import json
import math
import os
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
log = rh.log


# ----------------------------------------------------------------------------- voice (Gemini, in parts)
def gemini_tts_chunk(text, voice, model, key):
    url = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent'
    body = {'contents': [{'parts': [{'text': text}]}],
            'generationConfig': {'responseModalities': ['AUDIO'],
                                 'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice}}}}}
    r = requests.post(url, headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                      data=json.dumps(body), timeout=300)
    if r.status_code != 200:
        raise RuntimeError('Gemini TTS HTTP ' + str(r.status_code) + ': ' + r.text[:200])
    data = r.json()
    cands = data.get('candidates') or [{}]
    parts = ((cands[0].get('content') or {}).get('parts')) or []
    for p in parts:
        inline = p.get('inlineData') or p.get('inline_data')
        if inline and inline.get('data'):
            return base64.b64decode(inline['data'])
    raise RuntimeError('Gemini TTS returned no audio: ' + json.dumps(data)[:200])


def split_chunks(scenes, max_words):
    chunks = []
    cur = []
    count = 0
    for i, s in enumerate(scenes):
        n = len(s['narration'].split())
        if cur and count + n > max_words:
            chunks.append(cur)
            cur = []
            count = 0
        cur.append(i)
        count += n
    if cur:
        chunks.append(cur)
    return chunks


def make_voice_gemini(scenes, payload, path):
    key = os.environ['GEMINI_API_KEY']
    voice = payload.get('tts_voice') or 'Charon'
    models = payload.get('tts_models') or ['gemini-2.5-flash-preview-tts', 'gemini-2.5-pro-preview-tts']
    max_words = int(payload.get('tts_chunk_words') or 180)
    pause = float(payload.get('tts_pause_seconds') or 21)
    style = str(payload.get('tts_style') or '').strip()
    groups = split_chunks(scenes, max_words)
    log('voice: ' + str(len(groups)) + ' parts, voice ' + voice)
    pcm_all = bytearray()
    gap = bytes(int(24000 * 0.35) * 2)
    out = []
    for ci, idxs in enumerate(groups):
        text = ' '.join(scenes[i]['narration'].strip() for i in idxs)
        nwords = len(text.split())
        pcm = None
        for attempt in range(6):
            model = models[min(attempt // 3, len(models) - 1)]
            try:
                pcm = gemini_tts_chunk((style + ' ' + text) if style else text, voice, model, key)
                secs = len(pcm) / 48000.0
                if secs < nwords * 0.18:
                    raise RuntimeError('audio too short (' + str(round(secs, 1)) + 's for ' + str(nwords) + ' words)')
                break
            except Exception as e:  # noqa
                msg = str(e)
                pcm = None
                log('voice part ' + str(ci + 1) + ' try ' + str(attempt + 1) + ' failed (' + model + '): ' + msg[:160])
                time.sleep(65 if '429' in msg else 8 * (attempt + 1))
        if pcm is None:
            raise RuntimeError('Gemini voice failed for part ' + str(ci + 1) + ' of ' + str(len(groups)))
        if ci > 0:
            pcm_all.extend(gap)
        t0 = len(pcm_all) / 48000.0
        pcm_all.extend(pcm)
        t1 = len(pcm_all) / 48000.0
        out.append((idxs, t0, t1))
        log('voice part ' + str(ci + 1) + '/' + str(len(groups)) + ' ok, ' + str(round(t1 - t0, 1)) + 's')
        if ci + 1 < len(groups):
            time.sleep(pause)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(bytes(pcm_all))
    return out


def align_by_chunks(scenes, chunks, voice):
    heard = None
    try:
        heard = rh.whisper_heard(voice)
        log('Whisper heard ' + str(len(heard)) + ' words')
    except Exception as e:  # noqa
        log('Whisper not available, using estimated word timing: ' + str(e)[:150])
    words = []
    for idxs, t0, t1 in chunks:
        cw = ' '.join(scenes[i]['narration'].strip() for i in idxs).split()
        got = None
        if heard:
            hw = [h for h in heard if t0 - 0.3 <= h[1] < t1 + 0.3]
            try:
                got = rh.map_by_chars(cw, hw)
            except Exception as e:  # noqa
                log('part timing fell back to an estimate: ' + str(e)[:100])
        if not got:
            got = [(w, a + t0, b + t0) for (w, a, b) in rh.estimate_words(cw, t1 - t0)]
        words.extend(got)
    fixed = []
    prev = 0.0
    for w, a, b in words:
        a = max(a, prev)
        b = max(b, a + 0.1)
        fixed.append((w, a, b))
        prev = a
    return fixed


# ----------------------------------------------------------------------------- wide images
def fit_wide(img):
    img = img.convert('RGB')
    ratio = img.width / img.height
    if 1.70 <= ratio <= 1.86:
        return img.resize((W, H), Image.LANCZOS)
    bg = img.copy()
    scale = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1), Image.BILINEAR)
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new('RGB', bg.size, (0, 0, 0)), 0.35)
    fs = H / img.height
    fg = img.resize((max(1, int(img.width * fs)), H), Image.LANCZOS)
    if fg.width > W:
        fg = fg.resize((W, max(1, int(fg.height * W / fg.width))), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg


def img_poll_wide(prompt, **_):
    url = ('https://image.pollinations.ai/prompt/' + requests.utils.quote(prompt[:1500]) +
           '?width=1280&height=720&model=flux&nologo=true&private=true&seed=' + str(int(time.time() * 1000) % 999983))
    last = ''
    with rh._poll_lock:
        for i in range(4):
            try:
                r = requests.get(url, timeout=150)
                if r.status_code == 200 and len(r.content) > 5000:
                    return Image.open(__import__('io').BytesIO(r.content))
                last = 'Pollinations HTTP ' + str(r.status_code)
            except Exception as e:  # noqa
                last = 'Pollinations error ' + str(e)[:100]
            time.sleep(6 * (i + 1))
    raise RuntimeError(last)


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
                if ('quota' in low) or ('unauthorized' in low) or ('401' in msg) or ('403' in msg):
                    with rh._lock:
                        rh._hf_dead.add(label)
                    log(label + ' is used up or blocked, switching to the next account')
                    continue
                last = msg[:200]
                with rh._lock:
                    rh._hf_clients.pop(label + '|' + space, None)
                continue
            finally:
                lk.release()
        time.sleep(8 * (rnd + 1))
    log('Hugging Face gave no image this time: ' + last[:150])
    raise RuntimeError('Hugging Face queue was busy or failed, falling back to the next image source')


def make_images_wide(scenes, style_look, style_name, order):
    prefix = (style_look + ' Same recurring main character design and the same color palette in every image, '
              'no text, no letters, no watermark, wide cinematic 16:9 landscape composition. Scene: ')
    jobs = []
    for i, s in enumerate(scenes):
        full = prefix + s['image_prompt']
        short = ' '.join(s['image_prompt'].split()[:42])
        compact = style_name + ' illustration, same recurring character, no text, wide 16:9. ' + short
        jobs.append((i, full, compact))
    results = [None] * len(scenes)
    used = [None] * len(scenes)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(rh.generate_image, i, full, compact, order): i for i, full, compact in jobs}
        for f, i in futs.items():
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
    def __init__(self, words, font_size=62):
        fp = rh.ensure_font()
        self.font = ImageFont.truetype(fp, font_size, layout_engine=ImageFont.Layout.RAQM)
        self.chunks = []
        cur = []
        for w in words:
            cur.append(w)
            if len(cur) >= 7 or re.search(r'[.!?,;:।]$', w[0]):
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
        self.ptr = 0

    def key(self, t):
        n = len(self.chunks)
        if n == 0:
            return None
        while self.ptr + 1 < n and t >= self.chunks[self.ptr + 1][0][1] - 0.02:
            self.ptr += 1
        ch = self.chunks[self.ptr]
        if not (ch[0][1] - 0.02 <= t < ch[-1][2] + 0.25):
            return None
        act = -1
        for k, wd in enumerate(ch):
            if wd[1] - 0.03 <= t < wd[2] + 0.05:
                act = k
        return (self.ptr, act)

    def draw(self, img, key):
        if key is None:
            return
        idx, act = key
        ch = self.chunks[idx]
        widths, lines = self.layout[idx]
        d = ImageDraw.Draw(img)
        line_h = int(self.font.size * 1.3)
        y0 = int(H * 0.80) - (len(lines) - 1) * line_h // 2
        for li, line in enumerate(lines):
            tw = sum(widths[k] for k in line) + self.space * (len(line) - 1)
            x = (W - tw) / 2
            y = y0 + li * line_h
            for k in line:
                color = (255, 230, 0) if k == act else (255, 255, 255)
                d.text((x, y), ch[k][0], font=self.font, fill=color, stroke_width=6, stroke_fill=(0, 0, 0))
                x += widths[k] + self.space


def render_video(canvases, bounds, captions, total, audio_path, out_path):
    nframes = int(math.ceil(total * FPS))
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', str(W) + 'x' + str(H),
           '-r', str(FPS), '-i', '-', '-i', audio_path, '-map', '0:v', '-map', '1:a',
           '-c:v', 'libx264', '-preset', os.environ.get('X264_PRESET', 'veryfast'), '-crf', '21',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-shortest', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = len(canvases)
    log('encoding ' + str(nframes) + ' frames, hard cuts between scenes')
    k = 0
    last_key = None
    last_bytes = None
    for f in range(nframes):
        t = f / FPS
        while k + 1 < n and t >= bounds[k + 1]:
            k += 1
        ck = captions.key(t)
        key = (k, ck)
        if key != last_key:
            frame = canvases[k].copy()
            captions.draw(frame, ck)
            last_bytes = frame.tobytes()
            last_key = key
        proc.stdin.write(last_bytes)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg video encode failed')


# ----------------------------------------------------------------------------- main
def main():
    job = os.environ['JOB_PATH']
    with open(job, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    cb = payload.get('callback_url', '')
    if '--notify-failure' in sys.argv:
        rh.callback(cb, {'status': 'error', 'message': 'The GitHub landscape render job failed. Open the run log in GitHub Actions.'})
        return
    try:
        scenes = payload['scenes']
        order = [x.strip() for x in (os.environ.get('HINDI_IMAGE_ORDER') or 'hfspace,cloudflare,pollinations').split(',') if x.strip()]
        if 'pollinations' not in order:
            order.append('pollinations')
        rh.PROVIDERS['hfspace'] = img_hfspace_wide
        rh.PROVIDERS['pollinations'] = img_poll_wide
        log('scenes: ' + str(len(scenes)) + ', provider order: ' + str(order))

        voice = os.path.join(rh.WORK, 'voice.wav')
        chunks = make_voice_gemini(scenes, payload, voice)
        dur = rh.audio_duration(voice)
        log('voice total ' + str(round(dur, 1)) + 's')
        if dur > 780:
            raise RuntimeError('Voiceover is ' + str(int(dur)) + 's, over the 13 minute limit')

        words = align_by_chunks(scenes, chunks, voice)

        first_words = []
        wi = 0
        for s in scenes[:-1]:
            wi += len(s['narration'].split())
            first_words.append(wi)
        sil = rh.find_silences(voice)
        bounds = [0.0]
        for wi in first_words:
            b = words[min(wi, len(words) - 1)][1] - 0.04
            cand = [m for m in sil if abs(m - b) <= 0.35]
            if cand:
                b = min(cand, key=lambda m: abs(m - b))
            bounds.append(max(bounds[-1] + 0.8, b))
        total = dur + 1.2
        bounds.append(total)

        canvases, used = make_images_wide(scenes, payload.get('style_look', ''), payload.get('art_style', 'illustration'), order)
        music_path, music_name = rh.pick_music(payload.get('mood', 'calm'))
        mixed = os.path.join(rh.WORK, 'mix.wav')
        rh.mix_audio(voice, music_path, total, mixed)

        out_path = os.path.join(rh.OUT, 'video.mp4')
        render_video(canvases, bounds, WideCaptions(words), total, mixed, out_path)
        size = os.path.getsize(out_path) / 1e6
        log('video done ' + str(round(total, 1)) + 's ' + str(round(size, 1)) + 'MB')

        urls = rh.upload_video(out_path)
        result = {'status': 'success', 'video_urls': urls, 'duration': total, 'music': music_name,
                  'images_used': used, 'title': payload.get('title', ''), 'size_mb': round(size, 1)}
        with open(os.path.join(rh.OUT, 'result.json'), 'w') as fh:
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
