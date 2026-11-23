#!/usr/bin/env python3
"""Long landscape video, PREPARE step (voice + scene timing + tagged Flow prompts). Reuses pipeline/flow_images.py.

The Shorts voice code limits one voice call to 178 s (render_hindi.MAX_VOICE_SECONDS, and it speeds the audio up to fit that). For the
long video this script raises that limit on the loaded module only (LONG_MAX_VOICE_SECONDS, default 900), so render_hindi.py and the
Shorts workflows are untouched, and voices the script in BIG chunks of consecutive scenes: about LONG_CHUNK_CHARS letters each
(default 6000, roughly 6 to 7 minutes of speech), so a 20 minute video needs only a few voice calls.
Safety nets: a chunk whose audio is far too short for its text (cut off by the TTS) or too long, or that fails 3 times, is split in
half automatically and both halves are made. Every chunk is a separate wav kept in <out>/voice_chunks under a hash of its text, so a new
run of the same package (cache restored by the workflow) only makes the chunks that are still missing. Scene start times come from the
forced alignment of each chunk (same code as Shorts; estimated from the length if that fails), chunk start times from the real chunk lengths.

LANGUAGE (LONG_LANGUAGE, set by the workflow from the package field "language", the run input or the repo variable): en (default) or hi.
  en  English video. Package read by parse_package, English alignment models, voice Gemini Puck with the reel-narrator style
      (LONG_GEMINI_VOICE / LONG_GEMINI_STYLE).
  hi  Hindi video. The narration may be Devanagari OR Hindi in English letters (Hinglish), or a mix. Voice: Gemini Iapetus with the Hindi
      voice description + delivery style of the Shorts (pipeline/voice_patch.py); for Hinglish text the style also tells the voice to read it
      as native Hindi. Override with LONG_HI_GEMINI_VOICE / LONG_HI_GEMINI_STYLE. Timing: WhisperX large-v3-turbo, language hi, Hindi
      wav2vec2 aligner (theainerd/Wav2Vec2-large-xlsr-hindi). Devanagari text: forced alignment of the known words. Hinglish text (the Hindi
      aligner cannot read Latin letters): the voice is transcribed by WhisperX (hi) and the script words are spread over the heard words.
The Shorts code is never edited; the language switch only changes this process.

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
import re
import shutil
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import flow_images as fl  # noqa: E402

CHUNK_CHARS = int(os.environ.get('LONG_CHUNK_CHARS') or 6000)
MAX_VOICE = float(os.environ.get('LONG_MAX_VOICE_SECONDS') or 900)
MIN_CHARS_PER_SECOND = 30.0  # faster than any real speech: audio shorter than text/30 means the TTS cut the text off
VOICE_NAME = 'long_voice.wav'
META_NAME = 'long_meta.json'
PROMPTS_NAME = 'long_flow.json'
MAX_PROMPT = 1900

# Language of the long video: en (default) or hi. Decides the package reader, the voice and the alignment models.
LANG = (os.environ.get('LONG_LANGUAGE') or '').strip().lower() or 'en'
if LANG.startswith('hi'):
    LANG = 'hi'

if LANG == 'hi':
    # Hindi: Iapetus + the Shorts Hindi description/style unless overridden ('' style = the Hindi default built in hindi_style()).
    LONG_VOICE = (os.environ.get('LONG_HI_GEMINI_VOICE') or '').strip() or 'Iapetus'
    LONG_STYLE = ' '.join((os.environ.get('LONG_HI_GEMINI_STYLE') or '').split())
else:
    # English: Puck + the reel-narrator style (same as the code-video-shorts reels).
    LONG_VOICE = (os.environ.get('LONG_GEMINI_VOICE') or '').strip() or 'Puck'
    LONG_STYLE = ' '.join((os.environ.get('LONG_GEMINI_STYLE') or '').split()) or (
        'an engaging young guy narrating a viral social media reel: upbeat, fast-paced, hyped and curious, friendly, with a punchy hook')

SCRIPT = {'latin': False}  # set in main(): True when the narration is Hindi written in English letters

HI_LATIN_NOTE = ('The script is Hindi written in English letters (Hinglish): read it as natural spoken Hindi with a native Hindi accent '
                 'and Hindi pronunciation, never as English.')
HI_GENERIC_STYLE = ('Adapt emotional delivery naturally to the script: engaging for stories, heartfelt for emotion, inspiring for motivation. '
                    'Keep a steady, natural pace with brief pauses between complete thoughts, and emphasize key words through vocal '
                    'projection and pitch without slowing down.')
HI_FALLBACK_DESC = ('Smooth, clear Indian male voice in his early 30s with a natural, native Hindi accent. Balanced lower-middle baritone '
                    'timbre with warm chest resonance, close-mic studio proximity, and high, forward broadcast loudness.')


def log(*a):
    print('[long_prepare]', *a, flush=True)


def is_latin(text):
    """True when most letters of the text are Latin (Hindi written in English letters)."""
    letters = [c for c in str(text) if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isascii()) / float(len(letters)) > 0.5


# ----------------------------------------------------------------------------- package (any language)
def parse_package(raw):
    """Package reader for the long video (the Shorts package.parse is Devanagari only). Same JSON shape: title, description, optional
    hashtags and voice_style, scenes with narration + image_prompt. Narration is used as written (English, Devanagari Hindi or Hindi in
    English letters); only a missing final full stop is added. A scene without an image_prompt is allowed (it keeps the previous image).
    Hashtags are added to the description only when it does not already contain them.
    Returns {'scenes', 'voice_style', 'title', 'description'}."""
    try:
        b = json.loads(raw)
    except ValueError as e:
        raise RuntimeError('package is not valid JSON: ' + str(e)[:120])
    if not isinstance(b, dict):
        raise RuntimeError('package must be a JSON object')
    title = ' '.join(str(b.get('title') or '').split())
    description = str(b.get('description') or '').strip()
    if not title:
        raise RuntimeError('package: title is missing')
    if not description:
        raise RuntimeError('package: description is missing')
    in_scenes = b.get('scenes') if isinstance(b.get('scenes'), list) else []
    if not in_scenes:
        raise RuntimeError('package: scenes list is empty')
    scenes = []
    for i, s in enumerate(in_scenes):
        s = s if isinstance(s, dict) else {}
        n = ' '.join(str(s.get('narration') or '').split())
        p = str(s.get('image_prompt') or '').strip()
        if not n:
            raise RuntimeError('package: scene %d has no narration' % (i + 1))
        scenes.append({'narration': n, 'image_prompt': p[:MAX_PROMPT]})
    last = scenes[-1]['narration']
    if not re.search(r'[.!?\u2026\u0964\u0965,;:]["\')\]\u201d\u2019]*$', last):
        scenes[-1]['narration'] = last + ('\u0964' if re.search(r'[\u0900-\u097F]', last) else '.')
    tags, seen = [], set()
    low_desc = description.lower()
    for h in (b.get('hashtags') if isinstance(b.get('hashtags'), list) else [])[:10]:
        t = re.sub(r'[^\w]', '', str(h or ''))
        if 2 <= len(t) <= 40 and t.lower() not in seen:
            seen.add(t.lower())
            if not re.search('#' + re.escape(t.lower()) + r'(?!\w)', low_desc):
                tags.append('#' + t)
    if tags:
        description += '\n\n' + ' '.join(tags)
    style = str(b.get('voice_style') or b.get('style_instruction') or b.get('gemini_style') or '').strip()
    return {'scenes': scenes, 'voice_style': style, 'title': title[:100], 'description': description}


parse_english = parse_package  # old name


def apply_language(rh):
    """Point the alignment code at the language of the long video, on the loaded modules of THIS process only (Shorts stay Hindi).
    render_hindi.whisperx_heard / whisper_heard_fw and align_forced.forced_words are written for Hindi, which is exactly what a Hindi
    long video needs, so for hi nothing is replaced. For any other language they are replaced by the same code with the language
    (and its default aligner model) swapped in."""
    if LANG == 'hi':
        log('language hi: WhisperX large-v3-turbo (hi) with the Hindi wav2vec2 aligner, faster-whisper hi as fallback')
        return
    from pipeline import align_forced
    orig_forced = align_forced.forced_words

    def forced_words(script_words, dur, voice_path, language=LANG, log=print):
        return orig_forced(script_words, dur, voice_path, language=language, log=log)

    align_forced.forced_words = forced_words

    def whisperx_heard(audio_path, script_text=''):
        import whisperx
        audio = whisperx.load_audio(audio_path)
        opts = {'condition_on_previous_text': False, 'beam_size': 5, 'initial_prompt': (script_text or None)}
        model = whisperx.load_model(os.environ.get('WHISPERX_MODEL', 'large-v3-turbo'), 'cpu', compute_type='int8', language=LANG, asr_options=opts)
        result = model.transcribe(audio, batch_size=8, language=LANG)
        del model
        align_model, meta = whisperx.load_align_model(language_code=LANG, device='cpu')
        aligned = whisperx.align(result['segments'], align_model, meta, audio, 'cpu', return_char_alignments=False)
        out = []
        for w in aligned.get('word_segments') or []:
            t = str(w.get('word') or '').strip()
            if t and w.get('start') is not None and w.get('end') is not None:
                out.append((t, float(w['start']), float(w['end'])))
        if len(out) < 3:
            raise RuntimeError('WhisperX alignment returned too few words')
        return out

    def whisper_heard_fw(audio_path, script_text=''):
        from faster_whisper import WhisperModel
        model = WhisperModel(os.environ.get('WHISPER_MODEL', 'medium'), device='cpu', compute_type='int8')
        segs, _ = model.transcribe(audio_path, word_timestamps=True, language=LANG, vad_filter=False,
                                   condition_on_previous_text=False, beam_size=5, initial_prompt=(script_text or None),
                                   no_speech_threshold=None)
        out = []
        for s in segs:
            for w in (s.words or []):
                t = w.word.strip()
                if t:
                    out.append((t, float(w.start), float(w.end)))
        return out

    rh.whisperx_heard = whisperx_heard
    rh.whisper_heard_fw = whisper_heard_fw
    log('language ' + LANG + ': forced alignment and the Whisper fallback use the ' + LANG + ' models')


def hindi_style(style):
    """Gemini style text of a Hindi long video: the Shorts Hindi voice description + delivery style (package voice_style, else
    LONG_HI_GEMINI_STYLE, else the Shorts default). For Hinglish text the Devanagari wording is dropped and a note says to read it as Hindi."""
    s = ' '.join(str(style or '').split()) or LONG_STYLE
    try:
        from pipeline import voice_patch as vp
    except Exception:  # noqa
        vp = None
    if SCRIPT['latin']:
        desc = (vp.DESC if vp else HI_FALLBACK_DESC).replace(' and crisp Devanagari articulation', '')
        return desc + ' ' + HI_LATIN_NOTE + ' ' + (s or HI_GENERIC_STYLE)
    if vp:
        return vp.style_for(s)
    return HI_FALLBACK_DESC + ' ' + (s or HI_GENERIC_STYLE)


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
    # long video voice (replaces the Shorts voice for this process only). A voice_style written in the package wins over the default style.
    voice['language'] = LANG
    voice['gemini_voice'] = LONG_VOICE
    if LANG == 'hi':
        voice['gemini_style'] = hindi_style(style)
    else:
        voice['gemini_style'] = ' '.join(str(style or '').split()) or LONG_STYLE
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


def get_rh(state, style):
    if state['rh'] is None:
        rh = load_rh(style)
        # render_hindi reads MAX_VOICE_SECONDS at call time, so this lifts the Shorts limit for this process only
        rh.MAX_VOICE_SECONDS = MAX_VOICE
        apply_language(rh)
        state['rh'] = rh
    return state['rh']


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


def latin_timing(rh, scenes, voice_path, dur):
    """Timing for Hindi written in English letters. The Hindi aligner only knows Devanagari, so the voice is transcribed by WhisperX (hi)
    and the script words are spread over the heard words (script word k -> heard word k*m/n), which keeps the real pauses and speed.
    Scene starts snap to real pauses like Shorts. Returns (words, bounds, aligner) like fl.real_timing."""
    script_words = fl.narration_of(scenes).split()
    heard = rh.whisper_heard(voice_path, '')
    if len(heard) < 3:
        raise RuntimeError('Whisper heard too few words')
    n, m = len(script_words), len(heard)
    words = []
    for k, w in enumerate(script_words):
        h = heard[min(m - 1, k * m // n)]
        words.append((w, float(h[1]), float(h[2])))
    firsts, wi = [], 0
    for s in scenes[:-1]:
        wi += len(str(s.get('narration') or '').split())
        firsts.append(wi)
    sil = rh.find_silences(voice_path)
    bounds = [0.0]
    for wi in firsts:
        b = words[min(wi, n - 1)][1]
        cand = [x for x in sil if abs(x - b) <= 0.1]
        if cand:
            b = min(cand, key=lambda x: abs(x - b))
        bounds.append(max(bounds[-1] + 0.8, b))
    return words, bounds, 'whisper-hi'


def chunk_timing(rh, sub, wav, dur):
    if LANG == 'hi' and is_latin(fl.narration_of(sub)):
        return latin_timing(rh, sub, wav, dur)
    return fl.real_timing(rh, sub, wav, dur)


def do_chunk(idxs, scenes, style, vdir, state):
    """List of (wav path or None, seconds, [start second of each scene inside it], scene indexes). One entry, or two or more when split."""
    sub = [scenes[i] for i in idxs]
    text = fl.narration_of(sub)
    if not text.strip():
        return [(None, 0.0, [0.0] * len(sub), idxs)]
    key = hashlib.sha256((LANG + '|' + LONG_VOICE + '|' + LONG_STYLE + '|' + style + '|' + str(SCRIPT['latin']) + '|' + text).encode('utf-8')).hexdigest()[:16]
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
            rh = get_rh(state, style)
            rh.make_voice(text, wav)
            dur = rh.audio_duration(wav)
            if dur > MAX_VOICE:
                raise RuntimeError('chunk voice is ' + str(int(dur)) + 's, over the limit')
            if dur < len(text) / MIN_CHARS_PER_SECOND:
                raise RuntimeError('chunk voice is only ' + str(int(dur)) + 's for ' + str(len(text)) + ' letters, the TTS cut it off, limit of the model')
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
        _w, bounds, aligner = chunk_timing(state['rh'], sub, wav, dur)
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
    pk = parse_package(raw)
    scenes, style = pk['scenes'], pk['voice_style']
    SCRIPT['latin'] = LANG == 'hi' and is_latin(fl.narration_of(scenes))
    items = [i for i, s in enumerate(scenes) if str(s.get('image_prompt') or '').strip()]
    if not scenes or not items or not fl.narration_of(scenes).strip():
        raise RuntimeError('package has no scenes, no image prompts or no narration')
    out = os.path.abspath(a.out)
    vdir = os.path.join(out, 'voice_chunks')
    os.makedirs(vdir, exist_ok=True)
    chunks = make_chunks(scenes)
    log(str(len(scenes)) + ' scenes, ' + str(len(items)) + ' images, ' + str(len(chunks)) + ' voice chunks of about ' + str(CHUNK_CHARS) + ' letters')
    log('language: ' + LANG + (' (Hindi in English letters)' if SCRIPT['latin'] else '') + ', voice: ' + LONG_VOICE + ', style: ' +
        (' '.join(style.split()) or LONG_STYLE or '(Hindi default)') + (' (from the package)' if style else ' (default)'))
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

    meta = {'scene_count': len(scenes), 'narration_sha': fl.sha(fl.narration_of(scenes)),
            'items': [{'index': i, 'prompt_no': p} for p, i in enumerate(items, 1)],
            'starts': [round(s, 2) for s in starts], 'voice_seconds': round(total, 2), 'title': pk['title'], 'description': pk['description']}
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
