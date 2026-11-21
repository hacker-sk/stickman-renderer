#!/usr/bin/env python3
"""Long landscape video, PREPARE step (voice + scene timing + tagged Flow prompts). Reuses pipeline/flow_images.py.

The Shorts voice maker is capped at 178 s per call, so a long script is voiced in LONG chunks of consecutive scenes: about
LONG_CHUNK_CHARS letters each (default 2000, roughly 2 to 2.5 minutes of speech), which keeps the number of voice calls small.
If a chunk turns out too long for one call (or fails 3 times) it is split in half automatically and both halves are made, so a big
chunk never blocks the run. Every chunk is a separate wav kept in <out>/voice_chunks under a hash of its text, so a new run of the same
package (cache restored by the workflow) only makes the chunks that are still missing. Scene start times come from the forced alignment
of each chunk (same code as Shorts), chunk start times from the real chunk lengths.

Writes into <out> (default flow-batch/long):
  long_flow.json   the tagged Flow prompts ('[s01] ...'), only scenes that have an image_prompt
  long_meta.json   scene_count, items (scene index -> prompt number), starts (start second of EVERY scene), voice_seconds, title, description
  long_voice.wav   the whole voiceover
Exit code 1 on any failure (there is no fallback voice for a long video).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import flow_images as fl  # noqa: E402

CHUNK_CHARS = int(os.environ.get('LONG_CHUNK_CHARS') or 2000)
VOICE_NAME = 'long_voice.wav'
META_NAME = 'long_meta.json'
PROMPTS_NAME = 'long_flow.json'


def log(*a):
    print('[long_prepare]', *a, flush=True)


def load_rh(style=''):
    """The render_hindi module, set up exactly like pipeline/flow_images.make_voice_file does (also used by long_make for the upload)."""
    work = os.path.abspath('flow_work')
    os.makedirs(work, exist_ok=True)
    os.environ['WORK_DIR'] = work
    os.environ['OUT_DIR'] = os.path.abspath('flow_out')
    try:
        from pipeline import voice_patch as _vp
        voice = _vp.voice_payload(style)
    except Exception:  # noqa
        voice = {'language': 'hi', 'gemini_voice': 'Iapetus',
                 'gemini_models': ['gemini-3.8-flash-tts', 'gemini-2.5-flash-preview-tts'],
                 'voice_id': 'ibbx9zDYGvLgtYzRbqqG', 'fish_voice_id': '6366ea69a97d41ada9eb855a64e56fba', 'fish_model': 's2.1-pro-free'}
    os.environ['PAYLOAD'] = json.dumps(voice, ensure_ascii=False)
    stub = types.ModuleType('render_hindi_overlay')
    stub.STATE = {}
    sys.modules.setdefault('render_hindi_overlay', stub)
    import render_hindi as rh
    try:
        from pipeline import voice_patch
        voice_patch.apply_voice(rh)
    except Exception as e:  # noqa
        log('voice description patch not installed: ' + str(e)[:150])
    return rh


def make_chunks(scenes):
    chunks, cur, n = [], [], 0
    for i, s in enumerate(scenes):
        t = len(str(s.get('narration') or ''))
        if cur and n + t > CHUNK_CHARS:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += t
    if cur:
        chunks.append(cur)
    return chunks


def do_chunk(idxs, scenes, style, vdir, state):
    """List of (wav path or None, seconds, [start second of each scene inside it], scene indexes). One entry, or two or more when split."""
    sub = [scenes[i] for i in idxs]
    text = fl.narration_of(sub)
    if not text.strip():
        return [(None, 0.0, [0.0] * len(sub), idxs)]
    key = hashlib.sha256((style + '|' + text).encode('utf-8')).hexdigest()[:16]
    wav, js = os.path.join(vdir, key + '.wav'), os.path.join(vdir, key + '.json')
    if os.path.isfile(wav) and os.path.isfile(js):
        try:
            with open(js, encoding='utf-8') as fh:
                d = json.load(fh)
            if len(d['bounds']) == len(sub) and os.path.getsize(wav) > 10000:
                log('chunk ' + key + ' reused from cache (' + str(round(d['dur'], 1)) + 's)')
                return [(wav, float(d['dur']), [float(b) for b in d['bounds']], idxs)]
        except Exception:  # noqa
            pass
    last = None
    dur = 0.0
    for attempt in range(1, 4):
        try:
            if state['rh'] is None:
                dur, state['rh'] = fl.make_voice_file(text, wav, style)
            else:
                state['rh'].make_voice(text, wav)
                dur = state['rh'].audio_duration(wav)
                if dur > state['rh'].MAX_VOICE_SECONDS:
                    raise RuntimeError('chunk voice is ' + str(int(dur)) + 's, over the limit')
            last = None
            break
        except Exception as e:  # noqa
            last = e
            log('chunk voice attempt ' + str(attempt) + ' failed: ' + str(e)[:200])
            if 'limit' in str(e).lower() and len(idxs) > 1:
                break  # too long for one call: no point retrying, split below
            time.sleep(10 * attempt)
    if last is not None:
        if len(idxs) > 1:
            h = len(idxs) // 2
            log('splitting a chunk of ' + str(len(idxs)) + ' scenes into ' + str(h) + ' + ' + str(len(idxs) - h))
            return do_chunk(idxs[:h], scenes, style, vdir, state) + do_chunk(idxs[h:], scenes, style, vdir, state)
        raise RuntimeError('voice failed after 3 tries: ' + str(last)[:200])
    try:
        _w, bounds, aligner = fl.real_timing(state['rh'], sub, wav, dur)
    except Exception as e:  # noqa
        log('timing of the chunk failed, estimated from its length: ' + str(e)[:150])
        st = fl.scene_starts(sub, dur)
        bounds, aligner = [st[k] for k in range(len(sub))], 'estimate'
    with open(js, 'w', encoding='utf-8') as fh:
        json.dump({'dur': dur, 'bounds': [round(b, 3) for b in bounds], 'aligner': aligner}, fh)
    log('chunk ' + key + ' made (' + str(round(dur, 1)) + 's, timing ' + aligner + ')')
    return [(wav, dur, [float(b) for b in bounds], idxs)]


def probe_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--package', required=True)
    ap.add_argument('--out', default='flow-batch/long')
    a = ap.parse_args()
    with open(a.package, encoding='utf-8') as fh:
        raw = fh.read().strip()
    scenes = fl.load_scenes(raw)
    items = [i for i, s in enumerate(scenes) if str(s.get('image_prompt') or '').strip()]
    if not scenes or not items or not fl.narration_of(scenes).strip():
        raise RuntimeError('package has no scenes, no image prompts or no narration')
    style = fl.voice_style_of(raw)
    out = os.path.abspath(a.out)
    vdir = os.path.join(out, 'voice_chunks')
    os.makedirs(vdir, exist_ok=True)
    chunks = make_chunks(scenes)
    log(str(len(scenes)) + ' scenes, ' + str(len(items)) + ' images, ' + str(len(chunks)) + ' voice chunks of about ' + str(CHUNK_CHARS) + ' letters')
    state = {'rh': None}
    starts = [0.0] * len(scenes)
    offset, wavs = 0.0, []
    for idxs in chunks:
        for wav, dur, bounds, ix in do_chunk(idxs, scenes, style, vdir, state):
            for k, i in enumerate(ix):
                starts[i] = offset + bounds[k]
            if wav:
                wavs.append(wav)
            offset += dur
    voice_out = os.path.join(out, VOICE_NAME)
    if len(wavs) == 1:
        shutil.copyfile(wavs[0], voice_out)
    else:
        lst = os.path.join(out, 'voice_list.txt')
        with open(lst, 'w', encoding='utf-8') as fh:
            for w in wavs:
                fh.write("file '" + w.replace("'", "'\\''") + "'\n")
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', voice_out], check=True)
    total = probe_duration(voice_out)
    if abs(total - offset) > 2:
        log('::warning::joined voice is ' + str(round(total, 1)) + 's, chunk lengths add up to ' + str(round(offset, 1)) + 's')

    prompts = ['[s%02d] %s' % (p, str(scenes[i]['image_prompt']).strip()) for p, i in enumerate(items, 1)]
    with open(os.path.join(out, PROMPTS_NAME), 'w', encoding='utf-8') as fh:
        json.dump(prompts, fh, ensure_ascii=False, indent=1)

    title, description = '', ''
    try:
        from pipeline import package
        prep = package.parse(raw)['prep']
        title, description = str(prep.get('title') or ''), str(prep.get('description') or '')
    except Exception:  # noqa
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                title, description = str(d.get('title') or ''), str(d.get('description') or '')
        except Exception:  # noqa
            pass
    meta = {'scene_count': len(scenes), 'narration_sha': fl.sha(fl.narration_of(scenes)),
            'items': [{'index': i, 'prompt_no': p} for p, i in enumerate(items, 1)],
            'starts': [round(s, 2) for s in starts], 'voice_seconds': round(total, 2), 'title': title, 'description': description}
    with open(os.path.join(out, META_NAME), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    log('done: voice ' + str(round(total, 1)) + 's in ' + str(len(wavs)) + ' calls, ' + str(len(prompts)) + ' Flow prompts')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa
        print('::error::long prepare failed: ' + str(e)[:300], flush=True)
        sys.exit(1)
