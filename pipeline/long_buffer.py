#!/usr/bin/env python3
"""Long landscape video: schedule it on YouTube through Buffer (separate Buffer account/channel per language).

The workflow passes BUFFER_TOKEN and BUFFER_YOUTUBE_CHANNEL_ID of the language of the video (Hindi: BUFFER_LONG_HI_TOKEN +
variable BUFFER_LONG_HI_YOUTUBE_CHANNEL_ID; English: BUFFER_LONG_EN_TOKEN + BUFFER_LONG_EN_YOUTUBE_CHANNEL_ID). Nothing set = skipped.
The Shorts publisher (pipeline/buffer.py, BUFFER_TOKEN secret) is only imported for its helpers and is not changed.
Buffer needs a public video link: the first link of output/result.json (the free temporary host of the make job). No link = skipped.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline import buffer  # noqa: E402


def main():
    token = os.environ.get('BUFFER_TOKEN', '').strip()
    cid = os.environ.get('BUFFER_YOUTUBE_CHANNEL_ID', '').strip()
    lang = os.environ.get('LONG_LANGUAGE', 'en')
    if not token or not cid:
        print('[long_buffer] Buffer skipped: no Buffer token / YouTube channel id set for language ' + lang, flush=True)
        return 0
    out = os.environ.get('OUT_DIR', 'output')
    with open(os.path.join(out, 'metadata.json'), encoding='utf-8') as fh:
        meta = json.load(fh)
    try:
        with open(os.path.join(out, 'result.json'), encoding='utf-8') as fh:
            urls = [u for u in (json.load(fh).get('video_urls') or []) if str(u).startswith('http')]
    except Exception:  # noqa
        urls = []
    if not urls:
        print('[long_buffer] Buffer skipped: the video has no public link (the temporary host did not take it)', flush=True)
        return 0
    title = str(meta.get('title') or '')[:100]
    desc = str(meta.get('description') or '')
    m = 'youtube: { title: ' + json.dumps(title, ensure_ascii=False) + ', categoryId: "22" }'
    pid = buffer._create_post(token, cid, desc, buffer.due_at(60), urls[0], m)
    print('[long_buffer] YouTube post scheduled on Buffer (' + lang + '), id ' + str(pid), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa
        print('::error::long Buffer post failed: ' + buffer.redact(e)[:300], flush=True)
        sys.exit(1)
