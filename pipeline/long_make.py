#!/usr/bin/env python3
"""Long landscape video, MAKE step. Nothing is generated here: no images, no voice, no music, no transitions, no pan/zoom.

Inputs: long_meta.json + long_voice.wav (from the prepare job) and scene_NNN.<ext> images (from the Flow job, NNN = prompt number).
Every image is shown from the start of its first scene to the start of the next different image, over the voiceover.
A scene without its own image_prompt keeps the previous image. If ANY image is missing the step fails and lists them (nothing is
faked); the Flow job keeps what it made in the cache, so the next run only makes the missing ones.
Writes output/video.mp4 (1920x1080), output/metadata.json (title, description) and output/result.json (video_urls from the free temp host).
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

W, H = 1920, 1080


def log(*a):
    print('[long_make]', *a, flush=True)


def set_output(key, value):
    p = os.environ.get('GITHUB_OUTPUT')
    if p:
        with open(p, 'a', encoding='utf-8') as fh:
            fh.write(key + '=' + str(value) + '\n')


def probe(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height', '-show_entries', 'format=duration',
                        '-of', 'json', path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('ffprobe failed: ' + r.stderr[:200])
    return json.loads(r.stdout)


def fit(src, dst):
    from PIL import Image
    im = Image.open(src).convert('RGB')
    s = min(W / im.width, H / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    canvas = Image.new('RGB', (W, H), (0, 0, 0))
    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    canvas.save(dst, 'JPEG', quality=92)


def upload(path):
    """The free temporary host the Shorts renderer uses (render_hindi.upload_video). Best effort: [] when it fails."""
    try:
        from pipeline.long_prepare import load_rh
        res = load_rh().upload_video(path)
    except Exception as e:  # noqa
        log('::warning::temporary host upload not done: ' + str(e)[:200])
        return []
    if isinstance(res, dict):
        res = list(res.values())
    if isinstance(res, str):
        res = [res]
    return [str(u) for u in (res or []) if str(u).startswith('http')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--meta', required=True)
    ap.add_argument('--voice', required=True)
    ap.add_argument('--images', required=True)
    ap.add_argument('--out', default='output')
    a = ap.parse_args()
    with open(a.meta, encoding='utf-8') as fh:
        meta = json.load(fh)
    imgs = {}
    for dp, _dn, fn in os.walk(a.images):
        for f in fn:
            m = re.match(r'^scene_(\d+)\.(png|jpe?g|webp)$', f, re.I)
            p = os.path.join(dp, f)
            if m and os.path.getsize(p) >= 10000:
                imgs[int(m.group(1))] = p
    need = [it['prompt_no'] for it in meta['items']]
    miss = [p for p in need if p not in imgs]
    if miss:
        raise RuntimeError(str(len(miss)) + ' of ' + str(len(need)) + ' images are missing (prompt numbers ' + ','.join(map(str, miss[:60])) +
                           '). Run the workflow again: the Flow job continues from its cache.')
    total = float(probe(a.voice)['format']['duration'])
    n, starts = meta['scene_count'], meta['starts']
    item_p = dict((it['index'], it['prompt_no']) for it in meta['items'])
    per, cur = [], need[0]
    for i in range(n):
        if i in item_p:
            cur = item_p[i]
        per.append(cur)
    segs = []
    for i in range(n):
        if not segs or segs[-1][0] != per[i]:
            segs.append([per[i], 0.0 if not segs else starts[i], 0.0])
    for k in range(len(segs)):
        segs[k][2] = segs[k + 1][1] if k + 1 < len(segs) else total

    os.makedirs(a.out, exist_ok=True)
    work = os.path.join(a.out, '_work')
    os.makedirs(work, exist_ok=True)
    lines = []
    for p, s, e in segs:
        dst = os.path.join(work, 'img_%03d.jpg' % p)
        if not os.path.isfile(dst):
            fit(imgs[p], dst)
        lines.append("file '" + os.path.abspath(dst) + "'\nduration " + ('%.3f' % max(0.1, e - s)))
    lines.append("file '" + os.path.abspath(os.path.join(work, 'img_%03d.jpg' % segs[-1][0])) + "'")
    lst = os.path.join(work, 'list.txt')
    with open(lst, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    video = os.path.join(a.out, 'video.mp4')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-i', a.voice, '-map', '0:v', '-map', '1:a',
                    '-vf', 'format=yuv420p', '-r', '24', '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'stillimage', '-crf', '22',
                    '-c:a', 'aac', '-b:a', '192k', '-t', '%.3f' % total, '-movflags', '+faststart', video], check=True)
    info = probe(video)
    vs = [s for s in info['streams'] if s.get('codec_type') == 'video']
    dur = float(info['format']['duration'])
    if not vs or not any(s.get('codec_type') == 'audio' for s in info['streams']):
        raise RuntimeError('video has no video or no audio stream')
    if int(vs[0]['width']) <= int(vs[0]['height']):
        raise RuntimeError('video is not landscape')
    if abs(dur - total) > 3:
        raise RuntimeError('video is ' + str(round(dur, 1)) + 's but the voice is ' + str(round(total, 1)) + 's')
    size_mb = round(os.path.getsize(video) / 1048576, 1)
    log('video ready: ' + str(round(dur / 60, 1)) + ' min, ' + str(size_mb) + ' MB, ' + str(len(segs)) + ' images')
    with open(os.path.join(a.out, 'metadata.json'), 'w', encoding='utf-8') as fh:
        json.dump({'title': meta.get('title') or '', 'description': meta.get('description') or ''}, fh, ensure_ascii=False, indent=1)
    urls = upload(video)
    with open(os.path.join(a.out, 'result.json'), 'w', encoding='utf-8') as fh:
        json.dump({'status': 'success', 'video_urls': urls, 'duration': round(dur, 1), 'size_mb': size_mb}, fh, indent=1)
    set_output('video_ready', 'true')
    sm = os.environ.get('GITHUB_STEP_SUMMARY')
    if sm:
        with open(sm, 'a', encoding='utf-8') as fh:
            fh.write('## Long landscape video\n- ' + str(round(dur / 60, 1)) + ' min, ' + str(size_mb) + ' MB, ' + str(len(segs)) + ' images\n- Hosted: ' +
                     (', '.join(urls) or 'none (artifact and release copy only)') + '\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa
        print('::error::long make failed: ' + str(e)[:400], flush=True)
        sys.exit(1)
