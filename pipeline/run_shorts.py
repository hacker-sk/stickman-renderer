#!/usr/bin/env python3
"""Hindi portrait Shorts orchestrator (GitHub Actions). Replaces the n8n runtime.

Pipeline: input -> topic -> Hindi script -> image prompts -> render (existing render_hindi.py, same job)
-> output validation -> Buffer -> topic history -> Actions summary.

Inputs (environment, set by .github/workflows/shorts.yml):
  IDEA, SCRIPT          manual idea / finished script (both empty = scheduled run, a topic is picked automatically)
  PACKAGE               finished package JSON from Claude chat (title, description, mood, hook, scenes with narration +
                        image_prompt, see pipeline/package.py); skips topic, script and image-prompt steps. Its image prompts are
                        complete on their own (style wording and recurring characters included), so no preset style is used for it.
                        It can also carry voice_style (delivery style of this script), random_bg (true = random artwork background,
                        default plain black), bgm (true = background music, default none) and thumbnail_prompt (portrait thumbnail,
                        see pipeline/thumb_prompt.py)
  SCRIPT_MODE           auto | truth | story   (scheduled runs only)
  ART_STYLE             scheduled, idea and script runs only (not used for a package):
                        random (default) | indian_storybook | sketchbook_doodle | whimsical_indian | editorial_pencil |
                        stickman_sepia | village_storybook
  IMAGE_PROVIDER_ORDER  optional, copied to HINDI_IMAGE_ORDER for the renderer
  MUSIC_VOLUME          optional, level of the background song (default 0.14, see music_picker.py)
  THUMB_DIR             optional, folder with the thumbnail image the flow job made (artifact flow-thumb). The renderer (pipeline/thumb_frame.py,
                        installed by run_hindi.py) adds it as a 0.3 second silent last frame and writes its position to output/thumbnail.json;
                        that position is sent to Buffer as the thumbnail offset (metadata.thumbnailOffset) of the Instagram post

Voice: Gemini prebuilt voice Iapetus. The voice payload below (pipeline/voice_patch.py voice_payload) is what Gemini TTS gets, in BOTH places where
the voiceover is made (the prepare job through pipeline/flow_images.py, and the make job through the render job file). It holds the voice
description plus the delivery style written for the script (package field voice_style; the default style when it has none). When prepare
made the voice, the make job reuses it (and its word timing) instead of making it again.
Look and sound: pipeline/look_patch.py (installed by run_hindi.py) reads random_bg and bgm from the job file.
"""
import json
import os
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import buffer, image_prompts, package, script, styles, topics, voice_patch  # noqa: E402

OUT_DIR = os.environ.get('OUT_DIR', 'output')
WORK_DIR = os.environ.get('WORK_DIR', 'work')
RENDERER = os.environ.get('RENDER_SCRIPT', os.path.join(ROOT, 'render_hindi.py'))
RENDER_TIMEOUT = 80 * 60
VOICE = voice_patch.voice_payload()     # default style; build_job() adds the style written for the script


def log(*a):
    print('[run_shorts]', *a, flush=True)


def env(name):
    return os.environ.get(name, '').strip()


def set_output(key, value):
    path = env('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(key + '=' + str(value) + '\n')


def summary(lines):
    path = env('GITHUB_STEP_SUMMARY')
    text = '\n'.join(lines) + '\n'
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(text)
    else:
        print(text)


def build_job(prep, scenes, style_key, style, mood, hook=None, opts=None):
    """Render payload. The renderer reads it from the job file (payload.job_path).

    Same keys as n8n's 'Build Render Payload' (no style_look: the image prompts already carry the style),
    plus an optional hook for packages that bring their own. opts is the parsed package (voice_style, random_bg, bgm);
    without a package the defaults apply: default voice style, plain black background, no music."""
    opts = opts or {}
    job = voice_patch.voice_payload(opts.get('voice_style'))
    job.update({'title': prep['title'], 'mood': mood, 'art_style': style['name'], 'scenes': scenes,
                'random_bg': bool(opts.get('random_bg')), 'bgm': bool(opts.get('bgm'))})
    if hook:
        job['hook'] = hook
    return job


def run_renderer(job):
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    for name in ('video.mp4', 'result.json', 'thumbnail.json'):
        p = os.path.join(OUT_DIR, name)
        if os.path.exists(p):
            os.remove(p)
    job_path = os.path.abspath(os.path.join(WORK_DIR, 'job.json'))
    with open(job_path, 'w', encoding='utf-8') as fh:
        json.dump(job, fh, ensure_ascii=False)
    child = dict(os.environ)
    child['PAYLOAD'] = json.dumps({'job_path': job_path})
    child.setdefault('WORK_DIR', WORK_DIR)
    child.setdefault('OUT_DIR', OUT_DIR)
    if env('IMAGE_PROVIDER_ORDER') and not env('HINDI_IMAGE_ORDER'):
        child['HINDI_IMAGE_ORDER'] = env('IMAGE_PROVIDER_ORDER')
    # run_hindi.py installs the mood based music picker and then runs the renderer chosen above
    child['RENDER_TARGET'] = RENDERER
    log('starting the renderer (' + str(len(job['scenes'])) + ' scenes, mood ' + str(job.get('mood')) + ')')
    try:
        rc = subprocess.run([sys.executable, os.path.join(ROOT, 'run_hindi.py')], cwd=ROOT, env=child, timeout=RENDER_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        raise RuntimeError('The renderer ran longer than ' + str(RENDER_TIMEOUT // 60) + ' minutes and was stopped')
    if rc != 0:
        raise RuntimeError('The renderer failed with exit code ' + str(rc) + ', see the "Make the video" log above')


def validate_output():
    """The video must exist, be a real portrait video with sound, and result.json must report success and a URL."""
    video = os.path.join(OUT_DIR, 'video.mp4')
    if not os.path.exists(video) or os.path.getsize(video) < 100000:
        raise RuntimeError('output/video.mp4 is missing or too small')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height', '-show_entries',
                            'format=duration', '-of', 'json', video], capture_output=True, text=True)
    if probe.returncode != 0:
        raise RuntimeError('ffprobe could not read output/video.mp4: ' + probe.stderr[:200])
    info = json.loads(probe.stdout)
    streams = info.get('streams') or []
    vs = [s for s in streams if s.get('codec_type') == 'video']
    if not vs or not any(s.get('codec_type') == 'audio' for s in streams):
        raise RuntimeError('output/video.mp4 has no video or no audio stream')
    if int(vs[0].get('height') or 0) <= int(vs[0].get('width') or 0):
        raise RuntimeError('output/video.mp4 is not portrait (' + str(vs[0].get('width')) + 'x' + str(vs[0].get('height')) + ')')
    duration = float((info.get('format') or {}).get('duration') or 0)
    if not 5 <= duration <= 180:
        raise RuntimeError('video duration ' + str(round(duration, 1)) + 's is outside 5 to 180 seconds')
    rp = os.path.join(OUT_DIR, 'result.json')
    if not os.path.exists(rp):
        raise RuntimeError('output/result.json is missing')
    with open(rp, encoding='utf-8') as fh:
        result = json.load(fh)
    if result.get('status') != 'success':
        raise RuntimeError('Renderer reported: ' + str(result.get('message') or result.get('status')))
    urls = [u for u in (result.get('video_urls') or []) if str(u).startswith('http')]
    if not urls:
        raise RuntimeError('No public video URL (every upload host failed); the video is only in the release copy')
    return result, urls, duration


def thumbnail_offset_ms():
    """Position (ms) of the thumbnail end frame the renderer added (output/thumbnail.json), or 0 when there is none. Never raises:
    no thumbnail is not a failed run."""
    try:
        with open(os.path.join(OUT_DIR, 'thumbnail.json'), encoding='utf-8') as fh:
            return max(0, int(json.load(fh).get('offset_ms') or 0))
    except Exception:  # noqa
        return 0


def publish_to_buffer(urls, title, description, thumb_offset=0):
    """Try each hosted URL until YouTube or Instagram accepts the post."""
    last = None
    for url in urls:
        out = buffer.publish(url, title, description, thumb_offset)
        last = out
        if out['post_ids'].get('YouTube') or out['post_ids'].get('Instagram'):
            out['video_url'] = url
            return out
        log('no required channel accepted ' + url + ', trying the next host')
    return last


def main():
    started = time.time()
    lines = ['## Hindi Shorts run']
    try:
        idea, own_script = env('IDEA'), env('SCRIPT')
        ctx, pkg, hook, topic_text = None, None, None, ''
        if env('PACKAGE'):
            pkg = package.parse(env('PACKAGE'))
            hook = pkg['hook']
        if pkg:
            # A package brings complete image prompts (style wording and recurring characters included): no preset style is used.
            style_key, style = None, {'name': pkg['style_name']}
        else:
            style_key, style = styles.get_style(env('ART_STYLE') or None)
        if pkg:
            lines.append('- Source: finished package from chat (no topic, script or image-prompt step)')
        elif idea or own_script:
            topic_text, provided = script.parse_manual(idea, own_script)
            lines.append('- Source: manual ' + ('script' if provided else 'idea'))
        else:
            mode = env('SCRIPT_MODE')
            ctx = topics.choose_context(mode if mode in ('truth', 'story') else None)
            topic_text, angle = topics.generate_topic(ctx)
            provided = False
            lines += ['- Source: scheduled, niche **' + ctx['niche'] + '**, ' + ctx['scriptMode'] + ' mode, ' + ctx['shape'] + ' shape']
        if pkg:
            prep, scenes = pkg['prep'], pkg['scenes']
            mood = prep['mood']
        else:
            log('topic: ' + topic_text[:80].replace('\n', ' '))
            data = script.write_script(topic_text, provided, ctx)
            prep = script.prepare_narration(data)
            if not prep['scenes']:
                raise RuntimeError('The script has no scenes')
            mood = prep['mood'] if prep['mood'] in script.MOODS else 'emotional'
            chunks = script.number_chunks(prep, style_key)
            scenes, _story = image_prompts.write_image_prompts(prep, chunks, style_key)
        lines += ['- Title: ' + prep['title'], '- Style: ' + style['name'] + ', mood ' + mood + ', ' + str(len(scenes)) + ' scenes']
        lines.append('- Voice: Iapetus, ' + ('own delivery style from the package' if pkg and pkg.get('voice_style') else 'default delivery style') +
                     ' | Background: ' + ('random artwork' if pkg and pkg.get('random_bg') else 'plain black') +
                     ' | Music: ' + ('on (' + mood + ')' if pkg and pkg.get('bgm') else 'off'))

        run_renderer(build_job(prep, scenes, style_key, style, mood, hook, pkg))
        result, urls, duration = validate_output()
        set_output('video_ready', 'true')
        lines += ['- Video: ' + str(round(duration, 1)) + 's, ' + str(result.get('size_mb')) + ' MB', '- Hosted: ' + ', '.join(urls)]

        offset = thumbnail_offset_ms()
        lines.append('- Thumbnail: ' + (('end frame added, Instagram thumbnail offset ' + str(offset) + ' ms') if offset
                                         else 'none (no thumbnail image, so the video has no end frame)'))
        out = publish_to_buffer(urls, prep['title'], prep['description'], offset)
        scheduled = sorted((out or {}).get('post_ids') or {})
        lines += ['- Buffer scheduled: ' + (', '.join(scheduled) or 'nothing')]
        for w in (out or {}).get('warnings') or []:
            lines.append('- Warning: ' + w)
        if scheduled and not pkg:  # a package brings its own idea; n8n never recorded those either
            topics.record_used(topic_text, ctx['niche'] if ctx else 'manual')
            set_output('state_changed', 'true')
            lines.append('- Topic history updated')
        errors = (out or {}).get('errors') or []
        if not scheduled or errors:
            raise RuntimeError('Buffer publishing problem: ' + ('; '.join(errors) or 'no channel accepted the video'))
        lines.append('- Done in ' + str(int(time.time() - started)) + 's')
        summary(lines)
        return 0
    except Exception as e:  # noqa
        traceback.print_exc()
        lines += ['', '**FAILED:** ' + buffer.redact(e)[:500]]
        summary(lines)
        return 1


if __name__ == '__main__':
    sys.exit(main())
