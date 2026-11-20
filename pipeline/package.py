"""Finished package written in Claude chat (the old n8n 'Claude Script Input' webhook, now the `package` workflow input).

The package carries everything the renderer needs, so no topic, script or image-prompt step runs, and the preset art styles
(pipeline/styles.py) are NOT used for a package:

{
  "title": "...", "description": "...", "mood": "emotional",          (mood: motivational|emotional|neutral; the older inspiring|mysterious|dramatic|upbeat|calm still work)
  "hashtags": ["..."], "tags": ["..."],                              (optional, same handling as the script step)
  "hook": "opening line shown above the picture"  or  {"line": "...", "focus": "..."},   (optional, default = script opening)
  "art_style": "short name of the look, e.g. Indian Sepia Stickman Cartoon",
                                                                     (optional, only a label passed to the renderer for its fallback image providers; default "illustration".
                                                                      It does not add any style wording: the style must be written inside every image_prompt)
  "voice_style": "delivery style for this script",                    (optional, written for every script; sent after the Iapetus voice description; default style when empty)
  "random_bg": true,                                                 (optional, default false = plain black screen; true = one random artwork background)
  "bgm": true,                                                       (optional, default false = no background music; true = a song picked by the mood)
  "scenes": [{"narration": "Hindi in Devanagari", "image_prompt": "English prompt used as written"}, ...]
}

IMAGE PROMPTS. Every image_prompt is used exactly as written. Nothing is added to it: no style text, no character notes, no
other scene. The image model sees ONE prompt at a time and has no memory, so each prompt must be enough on its own to draw its
picture:
  - the subjects (people, animals, objects) with their complete look: age, face, build, clothing, colours and one distinguishing
    feature, plus expression, pose and action; show a person only when the line needs one;
  - the environment, place and time of day, the composition and where things are placed, the framing and camera angle;
  - the lighting, colours and mood;
  - the complete art style wording (medium, linework, texture, palette, light), the same words in every prompt of the video;
  - no readable text, letters, numbers, logos or watermarks.
CONSISTENCY. When the story has a recurring character, place or object, write its full description again, in the same words, in
EVERY prompt that shows it (same look, same clothing and colours, same distinguishing feature), so all images feel like one
story and one world. When the video has no recurring subject, each prompt is simply a fresh complete picture in the same style.
Never write "same character", "as before", "again" or refer to another image or to the script. Neighbouring prompts differ in
subject, setting, framing and camera angle unless the story needs the same one. Use at most 1900 characters per prompt.

The mood picks the background music (only when bgm is true): one random song named <mood>_NN or neutral_NN in music_hindi/ (see music_picker.py).

Same checks as the n8n 'Claude Package' node.
"""
import json

from . import script

MAX_PROMPT = 1900
MUSIC_MOODS = ('motivational', 'emotional', 'neutral')
_TRUE = ('1', 'true', 'yes', 'y', 'on')


def _s(v):
    return str(v if v is not None else '').strip()


def _flag(v):
    if isinstance(v, bool):
        return v
    return _s(v).lower() in _TRUE


def parse(raw):
    """Returns {'prep', 'scenes', 'hook', 'style_name', 'voice_style', 'random_bg', 'bgm'}; raises RuntimeError with a readable message when the package is unusable."""
    try:
        b = json.loads(raw)
    except ValueError as e:
        raise RuntimeError('package is not valid JSON: ' + str(e)[:120])
    if not isinstance(b, dict):
        raise RuntimeError('package must be a JSON object')
    title, description = _s(b.get('title')), _s(b.get('description'))
    if not title:
        raise RuntimeError('package: title is missing')
    if not description:
        raise RuntimeError('package: description is missing')
    mood = _s(b.get('mood')).lower()
    if mood not in MUSIC_MOODS and mood not in script.MOODS:
        raise RuntimeError('package: mood must be one of ' + ', '.join(MUSIC_MOODS))
    in_scenes = b.get('scenes') if isinstance(b.get('scenes'), list) else []
    if not in_scenes:
        raise RuntimeError('package: scenes list is empty')
    scenes = []
    for i, s in enumerate(in_scenes):
        s = s if isinstance(s, dict) else {}
        n, p = _s(s.get('narration')), _s(s.get('image_prompt'))
        if not n:
            raise RuntimeError('package: scene %d has no narration' % (i + 1))
        if script._has_latin([{'narration': n}]):
            raise RuntimeError('package: scene %d narration has Latin letters; write English words in Devanagari' % (i + 1))
        if len(p) < 40:
            raise RuntimeError('package: scene %d has no usable image_prompt' % (i + 1))
        scenes.append({'narration': n, 'image_prompt': p[:MAX_PROMPT]})
    prep = script.prepare_narration({'title': title, 'description': description, 'hashtags': b.get('hashtags'),
                                     'tags': [_s(t) for t in (b.get('tags') or []) if _s(t)] if isinstance(b.get('tags'), list) else [],
                                     'mood': mood, 'scenes': scenes})
    scenes = [{'narration': _s(s['narration']), 'image_prompt': _s(s['image_prompt'])} for s in prep['scenes']]
    hook = b.get('hook')
    if isinstance(hook, dict):
        hook = {'line': _s(hook.get('line') or hook.get('text')), 'focus': _s(hook.get('focus'))}
    elif isinstance(hook, str):
        hook = _s(hook)
    else:
        hook = None
    return {'prep': prep, 'scenes': scenes, 'hook': hook or None,
            'style_name': _s(b.get('art_style') or b.get('artStyle'))[:80] or 'illustration',
            'voice_style': _s(b.get('voice_style') or b.get('style_instruction') or b.get('gemini_style')),
            'random_bg': _flag(b.get('random_bg')) or _s(b.get('background')).lower() == 'random',
            'bgm': _flag(b.get('bgm')) or _flag(b.get('music'))}
