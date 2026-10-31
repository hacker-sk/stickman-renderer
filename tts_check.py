import json
import os
import sys

STYLE = 'crisp, punchy, confident, steady medium pace, high energy'
os.environ['PAYLOAD'] = json.dumps({'gemini_voice': 'designed', 'gemini_style': STYLE,
                                    'gemini_models': ['gemini-3.8-flash-tts', 'gemini-2.5-flash-preview-tts']})
import render_hindi as rh  # noqa: E402
import render_landscape as rl  # noqa: E402

SCENES = [
    {'narration': 'दोस्तों, क्या आप जानते हैं कि हर इंसान के अंदर एक अनोखी ताकत छुपी होती है?'},
    {'narration': 'बस फर्क इतना है कि कुछ लोग उसे पहचान लेते हैं, और कुछ पूरी ज़िंदगी डर में निकाल देते हैं।'},
    {'narration': 'इसलिए आज से डरना छोड़ो, छोटा सा कदम उठाओ, और खुद पर भरोसा रखो।'},
    {'narration': 'याद रखना, जो आज मुश्किल लगता है, वही कल तुम्हारी सबसे बड़ी जीत बनेगा।'},
]
FAILS = []


def check(name, ok, detail=''):
    print('CHECK ' + ('PASS' if ok else 'FAIL') + ' | ' + name + ' | ' + str(detail), flush=True)
    if not ok:
        FAILS.append(name)


def timing(tag, words, dur, voice):
    starts = [w[1] for w in words]
    ends = [w[2] for w in words]
    total_words = sum(len(s['narration'].split()) for s in SCENES)
    check(tag + ': every script word has a time', len(words) == total_words, len(words))
    check(tag + ': word starts never go backwards', all(b >= a for a, b in zip(starts, starts[1:])))
    check(tag + ': first word starts within 1.5s', starts[0] <= 1.5, round(starts[0], 2))
    check(tag + ': last word ends inside the audio', ends[-1] <= dur + 0.3, str(round(ends[-1], 2)) + ' of ' + str(round(dur, 2)))
    gaps = [b - a for a, b in zip(ends, starts[1:])]
    worst = max(gaps) if gaps else 0
    check(tag + ': no caption gap longer than 2.5s', worst <= 2.5, round(worst, 2))
    first_words = []
    wi = 0
    for s in SCENES[:-1]:
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
    bounds.append(dur + 0.9)
    print('SCENE BOUNDS ' + tag + ' ' + str([round(x, 2) for x in bounds]), flush=True)
    check(tag + ': scene cuts in order, every scene at least 0.8s', all(b - a >= 0.8 for a, b in zip(bounds, bounds[1:])))


narration = ' '.join(s['narration'] for s in SCENES)
script_words = narration.split()
v1 = os.path.join(rh.WORK, 'check_portrait.wav')
rh.make_voice_gemini(narration, v1)
d1 = rh.audio_duration(v1)
print('PORTRAIT voice seconds ' + str(round(d1, 2)) + ' for ' + str(len(script_words)) + ' words', flush=True)
check('portrait: voice fits the 58s limit', d1 <= 58, round(d1, 1))
check('portrait: natural pace (1.5 to 4.5 words per second)', 1.5 <= len(script_words) / d1 <= 4.5, round(len(script_words) / d1, 2))
w1 = rh.align_words(script_words, d1, v1)
timing('portrait', w1, d1, v1)

payload = {'tts_voice': 'designed', 'tts_style': STYLE, 'tts_chunk_words': 20, 'tts_pause_seconds': 3,
           'tts_models': ['gemini-3.8-flash-tts', 'gemini-2.5-flash-preview-tts']}
v2 = os.path.join(rh.WORK, 'check_landscape.wav')
chunks = rl.make_voice_gemini(SCENES, payload, v2)
d2 = rh.audio_duration(v2)
print('LANDSCAPE voice seconds ' + str(round(d2, 2)) + ' in ' + str(len(chunks)) + ' parts', flush=True)
check('landscape: voice made in several parts', len(chunks) >= 2, len(chunks))
check('landscape: part timeline matches the real audio length', abs(chunks[-1][2] - d2) <= 0.15,
      str(round(chunks[-1][2], 2)) + ' vs ' + str(round(d2, 2)))
w2 = rl.align_by_chunks(SCENES, chunks, v2)
timing('landscape', w2, d2, v2)

print('ALL CHECKS PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS), flush=True)
sys.exit(1 if FAILS else 0)
