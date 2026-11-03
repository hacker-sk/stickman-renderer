'''Native 16:9 image providers for render_landscape.py.

render_hindi.py's Cloudflare provider uses FLUX.1 schnell, which can only make square images, and its Colab provider asks for a
vertical image. Those squares then got a blurred background. This module replaces both with versions that ask for a real
landscape image (Cloudflare FLUX.2 klein 4B takes width and height). A result that is not wide is rejected so the next
provider in the order is tried instead of putting a square image on the screen.
'''
import base64
import io
import os
import time

import requests
from PIL import Image

WIDE_W, WIDE_H = 1280, 720
MODEL = os.environ.get('CF_WIDE_MODEL') or '@cf/black-forest-labs/flux-2-klein-4b'


def _check_wide(im, who):
    if im.height <= 0 or im.width / float(im.height) < 1.6:
        raise RuntimeError(who + ' returned a ' + str(im.width) + 'x' + str(im.height) + ' image, not landscape')
    return im


def _cf_wide(rh, prompt):
    accts = rh.cf_accounts()
    if not accts:
        raise RuntimeError('Cloudflare keys not set')
    with rh._lock:
        live = [a for a in accts if a[0] not in rh._cf_dead]
        turn = rh._cf_turn[0]
        rh._cf_turn[0] += 1
    if not live:
        raise RuntimeError('Cloudflare HTTP 429: every account is out of its daily allowance')
    for k in range(len(live)):
        label, acct, tok = live[(turn + k) % len(live)]
        url = 'https://api.cloudflare.com/client/v4/accounts/' + acct + '/ai/run/' + MODEL
        form = {'prompt': (None, prompt[:2000]), 'width': (None, str(WIDE_W)), 'height': (None, str(WIDE_H))}
        r = requests.post(url, headers={'Authorization': 'Bearer ' + tok}, files=form, timeout=150)
        if r.status_code in (401, 403, 429):
            body = r.text[:300]
            low = body.lower()
            daily = (r.status_code != 429) or ('daily' in low) or ('4006' in low) or ('neurons' in low)
            rh.log('cloudflare wide ' + label + ' HTTP ' + str(r.status_code) + ': ' + body[:200])
            if daily:
                with rh._lock:
                    if label not in rh._cf_dead:
                        rh._cf_dead.add(label)
                        rh.log('cloudflare ' + label + ' is used up or blocked, switching to the next account')
            else:
                time.sleep(3)
            continue
        if r.status_code != 200:
            raise RuntimeError('Cloudflare wide HTTP ' + str(r.status_code) + ': ' + r.text[:200])
        data = r.json()
        b64 = (data.get('result') or {}).get('image') or data.get('image')
        if not b64:
            raise RuntimeError('Cloudflare returned no image: ' + str(data)[:200])
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        im.load()
        rh.log('cloudflare ' + label + ' made a ' + str(im.width) + 'x' + str(im.height) + ' image')
        return _check_wide(im, 'Cloudflare')
    with rh._lock:
        alive = [x for x in accts if x[0] not in rh._cf_dead]
    if not alive:
        raise RuntimeError('Cloudflare HTTP 429: every account is out of its daily allowance')
    raise RuntimeError('Cloudflare was busy for a moment, will retry')


def _colab_wide(rh, prompt, compact):
    url = rh.get_colab_url()
    if not url:
        raise RuntimeError('Colab URL not set')
    r = requests.post(url + '/generate', json={'prompt': compact or prompt, 'width': 1344, 'height': 768,
                                               'token': os.environ.get('COLAB_TOKEN', '')}, timeout=240)
    if r.status_code != 200:
        raise RuntimeError('Colab HTTP ' + str(r.status_code) + ': ' + r.text[:200])
    im = Image.open(io.BytesIO(r.content))
    im.load()
    return _check_wide(im, 'Colab')


def install(rh):
    def cf(prompt, **_):
        return _cf_wide(rh, prompt)

    def colab(prompt, compact=None, **_):
        return _colab_wide(rh, prompt, compact)

    rh.PROVIDERS['cloudflare'] = cf
    rh.PROVIDERS['colab'] = colab
    rh.log('wide image providers installed: Cloudflare ' + MODEL + ' at ' + str(WIDE_W) + 'x' + str(WIDE_H) + ', Colab 1344x768')
