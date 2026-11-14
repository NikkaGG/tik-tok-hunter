"""Local image analysis for a concentric rotation challenge.

This module only estimates alignment. It does not submit an answer or claim
that TikTok accepted one; callers must independently verify the result.
"""
import math
import cv2
import numpy as np
import asyncio


def _ring(image, radius, count=720):
    height, width = image.shape[:2]
    angles = np.arange(count) * (2 * math.pi / count)
    xs = (width - 1) / 2 + radius * np.cos(angles)
    ys = (height - 1) / 2 + radius * np.sin(angles)
    coords_x = xs.astype(np.float32).reshape(1, -1)
    coords_y = ys.astype(np.float32).reshape(1, -1)
    return cv2.remap(image, coords_x, coords_y, cv2.INTER_LINEAR)[0].astype(np.float64)


def estimate_rotation(outer, inner):
    """Return relative seam alignment in degrees and confidence, or None.

    Images must be centered, square and contain the ring and its corresponding
    inner circle at their natural sizes. Ambiguous images are rejected.
    """
    if outer is None or inner is None or len(outer.shape) != 3 or len(inner.shape) != 3:
        return None
    oh, ow = outer.shape[:2]
    ih, iw = inner.shape[:2]
    if min(oh, ow, ih, iw) < 40 or abs(oh - ow) > 3 or abs(ih - iw) > 3 or iw >= ow:
        return None
    radius = min(iw, ih) / 2
    if radius + 3 >= min(oh, ow) / 2:
        return None
    outer = cv2.GaussianBlur(outer[:, :, :3], (3, 3), 0)
    inner = cv2.GaussianBlur(inner[:, :, :3], (3, 3), 0)
    scores = np.zeros(720)
    for inner_delta, outer_delta in ((2, 1), (3, 2), (4, 3)):
        inner_ring = _ring(inner, radius - inner_delta)
        outer_ring = _ring(outer, radius + outer_delta)
        for channel in range(3):
            a, b = inner_ring[:, channel], outer_ring[:, channel]
            a, b = a - a.mean(), b - b.mean()
            norm = np.linalg.norm(a) * np.linalg.norm(b)
            if norm > 1e-6:
                scores += np.fft.irfft(np.conj(np.fft.rfft(a)) * np.fft.rfft(b), n=len(a)) / norm
    best = int(np.argmax(scores))
    excluded = np.ones(len(scores), dtype=bool)
    excluded[(np.arange(best - 16, best + 17) % len(scores))] = False
    spread = float(scores.std())
    confidence = (float(scores[best]) - float(np.max(scores[excluded]))) / max(spread, 1e-9)
    # A sharp peak alone can occur between entirely unrelated images. Require
    # the seam itself to correlate as well as having a distinct best angle.
    if spread < .03 or confidence < .08 or scores[best] / 9 < .35:
        return None
    return best / 2, confidence


async def try_rotation(page):
    """Try one visible, concentric image layout; return False for other types."""
    outer = page.locator('img#captcha-verify-image, img.captcha-verify-image, #captcha-verify-container-main-page img:not(.cap-absolute)').first
    inner = page.locator('img.captcha_verify_img_slide, #captcha-verify-container-main-page img.cap-absolute').first
    handle = page.locator('.secsdk-captcha-drag-icon').first
    track = page.locator('.captcha_verify_slide--slidebar, #secsdk-captcha-drag-wrapper').first
    if not await track.is_visible() and await handle.is_visible():
        track = handle.locator('xpath=../..')
    if not all([await outer.is_visible(), await inner.is_visible(), await handle.is_visible(), await track.is_visible()]):
        return False
    outer_box, inner_box, handle_box, track_box = await asyncio.gather(
        outer.bounding_box(), inner.bounding_box(), handle.bounding_box(), track.bounding_box())
    if not all((outer_box, inner_box, handle_box, track_box)):
        return False
    ow, oh = outer_box['width'], outer_box['height']
    iw, ih = inner_box['width'], inner_box['height']
    if min(ow, oh, iw, ih) < 40 or abs(ow - oh) > 4 or abs(iw - ih) > 4 or not .4 < iw / ow < .8:
        return False
    outer_data, inner_data = await asyncio.gather(outer.screenshot(), inner.screenshot())
    outer_image = cv2.imdecode(np.frombuffer(outer_data, np.uint8), cv2.IMREAD_COLOR)
    inner_image = cv2.imdecode(np.frombuffer(inner_data, np.uint8), cv2.IMREAD_COLOR)
    match = await asyncio.to_thread(estimate_rotation, outer_image, inner_image)
    if match is None:
        return False
    angle, _confidence = match
    travel = track_box['width'] - handle_box['width']
    if not 0 <= angle < 360 or travel <= 0:
        return False
    x = handle_box['x'] + handle_box['width'] / 2
    y = handle_box['y'] + handle_box['height'] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    try:
        await page.mouse.move(x + travel * angle / 360, y, steps=30)
    finally:
        await page.mouse.up()
    return True
