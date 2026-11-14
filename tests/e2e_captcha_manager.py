"""Browser integration test with a synthetic challenge; no TikTok traffic."""
import asyncio
import base64
import sys
from pathlib import Path
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from app.workers.captcha_manager import CaptchaError, CaptchaManager as RealCaptchaManager, CaptchaState


class CaptchaManager(RealCaptchaManager):
    def __init__(self, **kwargs):
        kwargs.setdefault('readiness_timeout', .15)
        super().__init__(**kwargs)


HTML = '''<!doctype html><html><body>
<div id="captcha_container"><p>Test challenge</p>
<button id="pass" onclick="complete()">Complete test</button>
<button id="dismiss" onclick="document.getElementById('captcha_container').remove()">Dismiss</button>
</div><script>
async function complete() {
  await fetch('/captcha/verify', {method: 'POST'});
  document.getElementById('captcha_container').remove();
}
</script></body></html>'''


def rotation_fixture():
    rng = np.random.default_rng(4)
    base = cv2.GaussianBlur(rng.integers(0, 256, (347, 347, 3), dtype=np.uint8), (0, 0), 5)
    inner = cv2.warpAffine(base[68:279, 68:279], cv2.getRotationMatrix2D((105, 105), 55, 1), (211, 211))
    def data(image):
        ok, png = cv2.imencode('.png', image)
        assert ok
        return 'data:image/png;base64,' + base64.b64encode(png.tobytes()).decode()
    return f'''<!doctype html><html><body>
    <div id="captcha_container" style="position:relative;width:347px;height:400px">
      <img id="captcha-verify-image" src="{data(base)}" style="width:347px;height:347px">
      <img class="captcha_verify_img_slide" src="{data(inner)}" style="position:absolute;left:68px;top:68px;width:211px;height:211px;border-radius:50%">
      <div class="captcha_verify_slide--slidebar" style="width:340px;height:30px;background:#ddd">
        <div class="secsdk-captcha-drag-icon" style="width:20px;height:30px;background:blue"></div>
      </div>
    </div>
    <script>
      let start = 0;
      document.querySelector('.secsdk-captcha-drag-icon').onmousedown = e => start = e.clientX;
      document.onmouseup = async e => {{
        if (Math.abs((e.clientX-start) - (320 * 55 / 360)) < 4) {{
          await fetch('/captcha/verify', {{method:'POST'}});
          document.getElementById('captcha_container').remove();
        }}
      }};
    </script></body></html>'''


def modern_rotation_fixture():
    html = rotation_fixture().replace('captcha_container', 'captcha-verify-container-main-page')
    html = html.replace('id="captcha-verify-image"', 'class="cap-outer"')
    html = html.replace('class="captcha_verify_img_slide"', 'class="cap-absolute"')
    html = html.replace('class="captcha_verify_slide--slidebar"', 'class="new-slider-track"')
    html = html.replace('<div class="secsdk-captcha-drag-icon"', '<div><div class="secsdk-captcha-drag-icon"')
    html = html.replace('background:blue"></div>', 'background:blue"></div></div>')
    html = html.replace("fetch('/captcha/verify',", "fetch('/captcha/verify?encrypted=1',")
    html = html.replace("document.getElementById('captcha-verify-container-main-page').remove();", "document.getElementById('captcha-verify-container-main-page').remove(); document.body.insertAdjacentHTML('beforeend', '<a href=\"/@author/video/123\">video</a>');")
    return html


def delayed_rotation_fixture():
    html = modern_rotation_fixture()
    html = html.replace('<img class="cap-outer"', '<img class="cap-outer" hidden')
    html = html.replace('<img class="cap-absolute"', '<img class="cap-absolute" hidden')
    return html.replace('</body>', "<script>setTimeout(() => document.querySelectorAll('img[hidden]').forEach(e => e.hidden = false), 400)</script></body>")


def retry_rotation_fixture():
    html = rotation_fixture().replace('let start = 0;', 'let start = 0; let attempts = 0;')
    return html.replace("if (Math.abs((e.clientX-start) - (320 * 55 / 360)) < 4) {",
                        "if (Math.abs((e.clientX-start) - (320 * 55 / 360)) < 4) { "
                        "if (++attempts === 1) { document.querySelector('.captcha_verify_img_slide').src += '#retry'; return; }")


def always_refresh_fixture():
    html = rotation_fixture().replace('let start = 0;', 'let start = 0; let attempts = 0;')
    return html.replace("if (Math.abs((e.clientX-start) - (320 * 55 / 360)) < 4) {",
                        "if (Math.abs((e.clientX-start) - (320 * 55 / 360)) < 4) { "
                        "document.querySelector('.captcha_verify_img_slide').src += '#retry' + (++attempts); return;")


def slide_fixture(start=0):
    bg = np.full((160, 320, 3), 180, np.uint8)
    cv2.circle(bg, (205, 85), 20, (30, 30, 30), -1)
    piece = np.zeros((50, 50, 4), np.uint8)
    cv2.circle(piece, (25, 25), 20, (120, 120, 120, 255), -1)
    def data(image):
        ok, png = cv2.imencode('.png', image)
        assert ok
        return 'data:image/png;base64,' + base64.b64encode(png.tobytes()).decode()
    return f'''<!doctype html><html><body>
    <div id="captcha_container" style="position:relative;width:320px;height:200px">
      <img id="captcha-verify-image" src="{data(bg)}" style="width:320px;height:160px">
      <div class="cap-absolute" style="position:absolute;top:60px;left:{start}px"><img class="captcha_verify_img_slide" src="{data(piece)}" style="width:50px;height:50px"></div>
      <div class="captcha_verify_slide--slidebar" style="width:320px;height:30px;background:#ddd"><div class="secsdk-captcha-drag-icon" style="width:20px;height:30px;background:blue"></div></div>
    </div><script>
      let start = 0;
      document.querySelector('.secsdk-captcha-drag-icon').onmousedown = e => start = e.clientX;
      document.onmouseup = async e => {{
        if (Math.abs((e.clientX-start) - (300 * (180 - {start}) / 320)) < 4) {{
          await fetch('/captcha/verify', {{method:'POST'}});
          document.getElementById('captcha_container').remove();
        }}
      }};
    </script></body></html>'''


def modern_slide_fixture():
    html = slide_fixture(start=40).replace('captcha_container', 'captcha-verify-container-main-page')
    html = html.replace('id="captcha-verify-image"', 'class="cap-outer"')
    html = html.replace('class="captcha_verify_img_slide"', 'class="cap-absolute"')
    html = html.replace('class="captcha_verify_slide--slidebar"', 'class="new-slider-track"')
    html = html.replace('<div class="secsdk-captcha-drag-icon"', '<div><div class="secsdk-captcha-drag-icon"')
    return html.replace('background:blue"></div>', 'background:blue"></div></div>')


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        async def fake(route):
            if '/captcha/verify' in route.request.url:
                body = ('{"edata":"encrypted","success":false}' if 'rejected=1' in route.request.url else
                        '{"edata":"encrypted"}' if 'encrypted=1' in route.request.url else '{"success":true}')
                await route.fulfill(status=200, content_type='application/json', body=body)
            elif '/search/video?q=tiktok' in route.request.url:
                await route.fulfill(status=200, content_type='text/html', body='<a href="/@test/video/123">video</a>')
            elif '/search/delayed' in route.request.url:
                await route.fulfill(status=200, content_type='text/html', body=delayed_rotation_fixture())
            elif '/search' in route.request.url:
                await route.fulfill(status=200, content_type='text/html', body=modern_rotation_fixture())
            elif route.request.url.endswith('/rotation'):
                await route.fulfill(status=200, content_type='text/html', body=rotation_fixture())
            elif route.request.url.endswith('/retry-rotation'):
                await route.fulfill(status=200, content_type='text/html', body=retry_rotation_fixture())
            elif route.request.url.endswith('/rejected-rotation'):
                await route.fulfill(status=200, content_type='text/html', body=rotation_fixture().replace('320 * 55 / 360', '320 * 240 / 360'))
            elif route.request.url.endswith('/always-refresh'):
                await route.fulfill(status=200, content_type='text/html', body=always_refresh_fixture())
            elif route.request.url.endswith('/home-rotation'):
                await route.fulfill(status=200, content_type='text/html', body=modern_rotation_fixture())
            elif route.request.url.endswith('/home-rejected'):
                await route.fulfill(status=200, content_type='text/html', body=modern_rotation_fixture().replace('?encrypted=1', '?rejected=1'))
            elif route.request.url.endswith('/slide'):
                await route.fulfill(status=200, content_type='text/html', body=slide_fixture())
            elif route.request.url.endswith('/shifted-slide'):
                await route.fulfill(status=200, content_type='text/html', body=slide_fixture(start=40))
            elif route.request.url.endswith('/modern-slide'):
                await route.fulfill(status=200, content_type='text/html', body=modern_slide_fixture())
            else:
                await route.fulfill(status=200, content_type='text/html', body=HTML)
        await ctx.route('**/*', fake)
        page = await ctx.new_page()
        await page.goto('https://www.tiktok.com/test')
        events = []
        waiting = asyncio.Event()
        def status(event):
            events.append(event.state)
            if event.state == CaptchaState.AWAITING_USER:
                waiting.set()
        pending = asyncio.create_task(CaptchaManager(timeout=10, poll=.05, callback=status).handle(page, visible=True))
        await asyncio.wait_for(waiting.wait(), timeout=3)
        assert not pending.done(), 'Task must wait while challenge is visible'
        await page.locator('#pass').click()
        assert await asyncio.wait_for(pending, timeout=5)
        assert events == [CaptchaState.DETECTED, CaptchaState.CLASSIFIED, CaptchaState.SOLVING, CaptchaState.AWAITING_USER,
                          CaptchaState.VERIFYING, CaptchaState.SOLVED, CaptchaState.RESUMED], events
        await page.goto('https://www.tiktok.com/test')
        waiting.clear()
        pending = asyncio.create_task(CaptchaManager(timeout=10, poll=.05, confirmation_timeout=.2,
                                                     callback=status).handle(page, visible=True))
        await asyncio.wait_for(waiting.wait(), timeout=3)
        await page.locator('#dismiss').click()
        try:
            await asyncio.wait_for(pending, timeout=5)
            raise AssertionError('Dismissed challenge incorrectly counted as solved')
        except CaptchaError:
            pass
        await page.goto('https://www.tiktok.com/rotation')
        automatic_events = []
        automatic = await CaptchaManager(timeout=3, poll=.05, callback=automatic_events.append).handle(page, visible=True)
        assert automatic and automatic_events[-1].state == CaptchaState.RESUMED, automatic_events
        await page.goto('https://www.tiktok.com/retry-rotation')
        retry_events = []
        retry = await CaptchaManager(timeout=2, poll=.05, retry_window=.5, callback=retry_events.append).handle(page, visible=True)
        assert retry and sum(e.state == CaptchaState.SOLVING for e in retry_events) == 2, retry_events
        await page.goto('https://www.tiktok.com/rejected-rotation')
        rejected_events = []
        try:
            await CaptchaManager(timeout=.2, poll=.05, retry_window=.2, callback=rejected_events.append).handle(page, visible=True)
            raise AssertionError('Unchanged rejected challenge was incorrectly accepted')
        except CaptchaError:
            assert sum(e.state == CaptchaState.SOLVING for e in rejected_events) == 1, rejected_events
            assert not any(e.state == CaptchaState.SOLVED for e in rejected_events), rejected_events
        await page.goto('https://www.tiktok.com/always-refresh')
        capped_events = []
        try:
            await CaptchaManager(timeout=.2, poll=.05, retry_window=.5, callback=capped_events.append).handle(page, visible=True)
            raise AssertionError('Repeatedly rejected challenges were incorrectly accepted')
        except CaptchaError:
            assert sum(e.state == CaptchaState.SOLVING for e in capped_events) == 3, capped_events
            assert not any(e.state == CaptchaState.SOLVED for e in capped_events), capped_events
        await page.goto('https://www.tiktok.com/search/video?q=test')
        modern_events = []
        modern = await CaptchaManager(timeout=3, poll=.05, callback=modern_events.append).handle(page, visible=True)
        assert modern and modern_events[-1].state == CaptchaState.RESUMED, modern_events
        assert await page.locator('a[href*="/video/"]').count() == 1
        await page.goto('https://www.tiktok.com/home-rotation')
        homepage_events = []
        homepage = await CaptchaManager(timeout=3, poll=.05, callback=homepage_events.append).handle(page, visible=True)
        assert homepage and homepage_events[-1].state == CaptchaState.RESUMED, homepage_events
        await page.goto('https://www.tiktok.com/home-rejected')
        homepage_failed = []
        try:
            await CaptchaManager(timeout=3, poll=.05, confirmation_timeout=.2, callback=homepage_failed.append).handle(page, visible=True)
            raise AssertionError('Explicit CAPTCHA rejection incorrectly accepted')
        except CaptchaError:
            assert not any(e.state == CaptchaState.SOLVED for e in homepage_failed), homepage_failed
        await page.goto('https://www.tiktok.com/search/delayed')
        delayed_events = []
        delayed = await CaptchaManager(timeout=3, poll=.05, readiness_timeout=2, callback=delayed_events.append).handle(page, visible=True)
        assert delayed and delayed_events[-1].state == CaptchaState.RESUMED, delayed_events
        assert any(e.state == CaptchaState.CLASSIFIED and e.kind == 'rotate' for e in delayed_events), delayed_events
        await page.goto('https://www.tiktok.com/slide')
        slide_events = []
        slide = await CaptchaManager(timeout=3, poll=.05, callback=slide_events.append).handle(page, visible=True)
        assert slide and slide_events[-1].state == CaptchaState.RESUMED, slide_events
        await page.goto('https://www.tiktok.com/shifted-slide')
        shifted_events = []
        shifted = await CaptchaManager(timeout=3, poll=.05, callback=shifted_events.append).handle(page, visible=True)
        assert shifted and shifted_events[-1].state == CaptchaState.RESUMED, shifted_events
        await page.goto('https://www.tiktok.com/modern-slide')
        modern_slide_events = []
        modern_slide = await CaptchaManager(timeout=3, poll=.05, callback=modern_slide_events.append).handle(page, visible=True)
        assert modern_slide and modern_slide_events[-1].state == CaptchaState.RESUMED, modern_slide_events
        assert any(e.state == CaptchaState.CLASSIFIED and e.kind == 'slide' for e in modern_slide_events)
        await browser.close()
    print('Synthetic browser CAPTCHA: manual success, rejected dismissal, rotation, encrypted-response continuation and puzzle with shifted start passed')


if __name__ == '__main__':
    asyncio.run(main())
