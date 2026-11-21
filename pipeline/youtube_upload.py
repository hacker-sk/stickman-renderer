#!/usr/bin/env python3
"""Upload output/video.mp4 to YouTube with the official Data API v3 (resumable upload). Used by the Long Landscape Video workflow.

Secrets (env): YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN  (refresh token needs the youtube.upload scope).
Optional env: YT_PRIVACY (private | unlisted | public, default private), YT_CATEGORY (default 22), YT_LANGUAGE (default hi),
              YT_MADE_FOR_KIDS (true/false, default false), YT_TAGS (comma separated).
Title and description come from output/metadata.json. If a secret is missing the step ends with a notice and exit code 0 (nothing uploaded).
Writes output/youtube.json with the video id and link. Note: a Google Cloud project that has not passed the YouTube API audit can only
upload videos as private, whatever YT_PRIVACY says.
"""
import argparse
import json
import os
import sys
import time

import requests

CHUNK = 32 * 1024 * 1024  # multiple of 256 KB


def log(*a):
    print('[youtube]', *a, flush=True)


def access_token():
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': os.environ['YT_CLIENT_ID'], 'client_secret': os.environ['YT_CLIENT_SECRET'],
        'refresh_token': os.environ['YT_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}, timeout=60)
    if r.status_code != 200:
        hint = ''
        if 'invalid_grant' in r.text:
            hint = ' (refresh token expired or revoked: if the consent screen is in Testing mode tokens last 7 days, publish it and make a new token)'
        raise RuntimeError('could not get an access token: ' + r.text[:200] + hint)
    return r.json()['access_token']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--video', default='output/video.mp4')
    ap.add_argument('--meta', default='output/metadata.json')
    ap.add_argument('--out', default='output/youtube.json')
    a = ap.parse_args()
    missing = [k for k in ('YT_CLIENT_ID', 'YT_CLIENT_SECRET', 'YT_REFRESH_TOKEN') if not os.environ.get(k)]
    if missing:
        print('::notice::YouTube upload skipped, secret(s) not set: ' + ', '.join(missing))
        return 0
    meta = {}
    try:
        with open(a.meta, encoding='utf-8') as fh:
            meta = json.load(fh)
    except Exception:  # noqa
        pass
    title = (str(meta.get('title') or '').strip() or 'Video')[:100]
    desc = str(meta.get('description') or '')
    while len(desc.encode('utf-8')) > 4900:
        desc = desc[:-100]
    privacy = (os.environ.get('YT_PRIVACY') or 'private').strip().lower()
    if privacy not in ('private', 'unlisted', 'public'):
        privacy = 'private'
    tags = [t.strip() for t in (os.environ.get('YT_TAGS') or '').split(',') if t.strip()]
    snippet = {'title': title, 'description': desc, 'categoryId': (os.environ.get('YT_CATEGORY') or '22').strip(),
               'defaultLanguage': (os.environ.get('YT_LANGUAGE') or 'hi').strip(), 'defaultAudioLanguage': (os.environ.get('YT_LANGUAGE') or 'hi').strip()}
    if tags:
        snippet['tags'] = tags
    body = {'snippet': snippet, 'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': (os.environ.get('YT_MADE_FOR_KIDS') or '').lower() == 'true'}}
    size = os.path.getsize(a.video)
    token = access_token()
    auth = {'Authorization': 'Bearer ' + token}
    r = requests.post('https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
                      headers=dict(auth, **{'Content-Type': 'application/json; charset=UTF-8', 'X-Upload-Content-Type': 'video/mp4',
                                            'X-Upload-Content-Length': str(size)}),
                      data=json.dumps(body).encode('utf-8'), timeout=60)
    if r.status_code != 200 or not r.headers.get('Location'):
        raise RuntimeError('could not start the upload: ' + str(r.status_code) + ' ' + r.text[:300])
    url = r.headers['Location']
    log('uploading ' + str(round(size / 1048576, 1)) + ' MB as "' + title + '" (' + privacy + ')')
    pos, fails, result = 0, 0, None
    with open(a.video, 'rb') as fh:
        while result is None:
            fh.seek(pos)
            data = fh.read(CHUNK)
            end = pos + len(data) - 1
            try:
                r = requests.put(url, data=data, timeout=600, headers={'Content-Length': str(len(data)), 'Content-Type': 'video/mp4',
                                                                       'Content-Range': 'bytes %d-%d/%d' % (pos, end, size)})
            except requests.RequestException as e:
                r = None
                log('network error: ' + str(e)[:120])
            if r is not None and r.status_code in (200, 201):
                result = r.json()
            elif r is not None and r.status_code == 308:
                rng = r.headers.get('Range')
                pos = int(rng.split('-')[1]) + 1 if rng else 0
                fails = 0
                log('sent ' + str(round(pos / 1048576)) + ' of ' + str(round(size / 1048576)) + ' MB')
            elif r is None or r.status_code >= 500:
                fails += 1
                if fails > 8:
                    raise RuntimeError('upload keeps failing')
                time.sleep(min(60, 5 * fails))
                try:  # ask the server how much it already has
                    q = requests.put(url, headers={'Content-Length': '0', 'Content-Range': 'bytes */%d' % size}, timeout=60)
                    if q.status_code == 308:
                        rng = q.headers.get('Range')
                        pos = int(rng.split('-')[1]) + 1 if rng else 0
                    elif q.status_code in (200, 201):
                        result = q.json()
                except requests.RequestException:
                    pass
            else:
                raise RuntimeError('upload refused: ' + str(r.status_code) + ' ' + r.text[:300])
    vid = result.get('id', '')
    link = 'https://youtu.be/' + vid
    log('done: ' + link + ' (' + privacy + ')')
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump({'video_id': vid, 'url': link, 'privacy': privacy}, fh, indent=1)
    sm = os.environ.get('GITHUB_STEP_SUMMARY')
    if sm:
        with open(sm, 'a', encoding='utf-8') as fh:
            fh.write('### YouTube\n' + link + ' (' + privacy + ')\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa
        print('::error::YouTube upload failed: ' + str(e)[:400], flush=True)
        sys.exit(1)
