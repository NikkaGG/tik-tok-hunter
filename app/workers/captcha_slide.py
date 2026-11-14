"""Experimental local recognition of a transparent puzzle-piece challenge."""
import asyncio
import base64
from urllib.parse import urlparse
import cv2
import numpy as np

HOSTS = ('tiktok.com', 'tiktokv.com', 'tiktokcdn.com', 'tiktokcdn-us.com',
         'byteimg.com', 'ibyteimg.com', 'ibytedtos.com', 'muscdn.com', 'byteoversea.com')


def find_gap(background, piece, top):
    if background is None or piece is None or piece.ndim != 3 or piece.shape[2] != 4:
        return None
    h, w = background.shape[:2]
    ph, pw = piece.shape[:2]
    if not (10 <= pw < w // 2 and 10 <= ph < h and 1 <= top < h - ph):
        return None
    alpha = piece[:, :, 3]
    mask = (alpha > 128).astype(np.uint8) * 255
    if np.count_nonzero(mask) < 80 or np.count_nonzero(mask == 0) < 30:
        return None
    outline = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), dtype=np.uint8))
    edges = cv2.Canny(background[:, :, :3], 60, 150)
    lo, hi = max(0, top - 3), min(h - ph, top + 3)
    region = edges[lo:hi + ph + 1, :]
    scores = cv2.matchTemplate(region, outline, cv2.TM_CCORR_NORMED)
    scores = np.nan_to_num(scores, nan=0, posinf=0, neginf=0)
    scores[:, :max(pw // 2, 1)] = 0
    _, best, _, (x, _y) = cv2.minMaxLoc(scores)
    other = scores.copy()
    other[:, max(0, x - pw // 2):min(other.shape[1], x + pw // 2 + 1)] = 0
    second = float(other.max())
    if best < .20 or best - second < .025:
        return None
    return x / w


async def _image(page, locator):
    src = await locator.get_attribute('src')
    if not src:
        return None
    if src.startswith('data:image/'):
        if len(src) > 7_000_000:
            return None
        try:
            data = base64.b64decode(src.split(',', 1)[1], validate=True)
        except (ValueError, IndexError):
            return None
    else:
        src = await locator.evaluate('(el) => el.currentSrc || el.src')
        url = urlparse(src)
        host = (url.hostname or '').lower()
        if url.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in HOSTS):
            return None
        response = await page.context.request.get(src, timeout=5000, max_redirects=0)
        if not response.ok or not response.headers.get('content-type', '').lower().startswith('image/'):
            return None
        data = await response.body()
    if len(data) > 5_000_000:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)


async def try_slide(page):
    background = page.locator('img#captcha-verify-image, img.captcha-verify-image, #captcha-verify-container-main-page img:not(.cap-absolute)').first
    piece = page.locator('img.captcha_verify_img_slide, #captcha-verify-image ~ div.cap-absolute > img, #captcha-verify-container-main-page img.cap-absolute').first
    handle = page.locator('.secsdk-captcha-drag-icon').first
    track = page.locator('.captcha_verify_slide--slidebar, #secsdk-captcha-drag-wrapper').first
    if not await track.is_visible() and await handle.is_visible():
        track = handle.locator('xpath=../..')
    if not all([await background.is_visible(), await piece.is_visible(), await handle.is_visible(), await track.is_visible()]):
        return False
    bg_box, piece_box, handle_box, track_box = await asyncio.gather(
        background.bounding_box(), piece.bounding_box(), handle.bounding_box(), track.bounding_box())
    if not all((bg_box, piece_box, handle_box, track_box)) or bg_box['width'] < bg_box['height'] * 1.2:
        return False
    bg, fragment = await asyncio.gather(_image(page, background), _image(page, piece))
    if bg is None or fragment is None:
        return False
    target_width = round(piece_box['width'] * bg.shape[1] / bg_box['width'])
    target_height = round(piece_box['height'] * bg.shape[0] / bg_box['height'])
    if not (10 <= target_width < bg.shape[1] // 2 and 10 <= target_height < bg.shape[0]):
        return False
    fragment = cv2.resize(fragment, (target_width, target_height), interpolation=cv2.INTER_AREA)
    top = round((piece_box['y'] - bg_box['y']) * bg.shape[0] / bg_box['height'])
    fraction = await asyncio.to_thread(find_gap, bg, fragment, top)
    if fraction is None:
        return False
    # The piece need not start at the left edge of the image. The slider moves
    # it by the gap offset relative to its current position.
    fraction -= (piece_box['x'] - bg_box['x']) / bg_box['width']
    if not 0.05 < fraction < .95:
        return False
    travel = track_box['width'] - handle_box['width']
    if travel <= 0:
        return False
    x, y = handle_box['x'] + handle_box['width'] / 2, handle_box['y'] + handle_box['height'] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    try:
        await page.mouse.move(x + travel * fraction, y, steps=30)
    finally:
        await page.mouse.up()
    return True
