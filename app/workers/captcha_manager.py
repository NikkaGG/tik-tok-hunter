"""Manage verification challenges, including an experimental local rotation solver."""
import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class CaptchaError(RuntimeError):
    pass


class CaptchaState(str, Enum):
    DETECTED = 'DETECTED'
    CLASSIFIED = 'CLASSIFIED'
    SOLVING = 'SOLVING'
    AWAITING_USER = 'AWAITING_USER'
    VERIFYING = 'VERIFYING'
    SOLVED = 'SOLVED'
    FAILED = 'FAILED'
    RESUMED = 'RESUMED'


@dataclass(frozen=True)
class CaptchaEvent:
    state: CaptchaState
    kind: str = 'unknown'
    detail: str = ''


# Limit detection to challenge-specific elements; ordinary modals and generic
# "verify" iframes are not sufficient evidence of a CAPTCHA.
SELECTOR = ("#captcha_container:visible, #secsdk-captcha:visible, #captcha-verify-container-main-page:visible, "
    "[id^='secsdk-captcha-']:visible, [class*='captcha_verify_']:visible, "
    "iframe[src*='hcaptcha.com/captcha']:visible, iframe[src*='geetest.com']:visible, "
    "iframe[src*='arkoselabs.com']:visible, iframe[src*='funcaptcha.com']:visible")
SUCCESS_SELECTOR = "[data-e2e='captcha-success']:visible, [class*='captcha_verify_success']:visible"
SEARCH_RESULTS_SELECTOR = 'a[href*="/video/"]:visible'
TYPES = (
    ('hcaptcha', "iframe[src*='hcaptcha.com/captcha']:visible"),
    ('geetest', "iframe[src*='geetest.com']:visible, [class*='geetest_panel']:visible"),
    ('arkose', "iframe[src*='arkoselabs.com']:visible, iframe[src*='funcaptcha.com']:visible"),
    ('voice', "[class*='captcha'][class*='audio']:visible, audio[src]:visible"),
    ('rotate', "[class*='captcha'][class*='whirl']:visible, [class*='captcha'][class*='rotate']:visible"),
    ('slide', "#captcha-verify-image ~ div.cap-absolute > img:visible"),
    ('grid', "[class*='captcha'][class*='grid']:visible"),
    ('shapes', "[class*='captcha'][class*='shape']:visible"),
    ('icon', "[class*='captcha'][class*='icon-select']:visible"),
)


async def classify(page):
    outer = page.locator('#captcha-verify-container-main-page img:not(.cap-absolute):visible')
    inner = page.locator('#captcha-verify-container-main-page img.cap-absolute:visible')
    if await outer.count() == 1 and await inner.count() == 1:
        outer_box, inner_box = await asyncio.gather(outer.first.bounding_box(), inner.first.bounding_box())
        if outer_box and inner_box:
            ow, oh = outer_box['width'], outer_box['height']
            iw, ih = inner_box['width'], inner_box['height']
            if min(ow, oh, iw, ih) >= 40 and abs(ow - oh) <= 4 and abs(iw - ih) <= 4 and .4 < iw / ow < .8:
                return 'rotate'
            if ow > oh * 1.2 and 10 <= iw < ow / 2 and 10 <= ih < oh:
                return 'slide'
    for kind, selector in TYPES:
        if await page.locator(selector).count():
            return kind
    return 'unknown'


def is_verification_endpoint(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return (host == 'tiktok.com' or host.endswith('.tiktok.com') or host.endswith('.tiktokv.com')
            or host.endswith('.tiktokw.eu')) and parsed.path == '/captcha/verify'


def verified_response(url, data):
    """Accept only an explicit result from TikTok's verification endpoint."""
    if not is_verification_endpoint(url):
        return False
    if not isinstance(data, dict):
        return False
    if data.get('success') is False:
        return False
    return data.get('success') is True or (type(data.get('code')) is int and data['code'] == 0)


async def confirm_search_access(page, timeout=8):
    """Corroborate an encrypted reply with a read-only search in the same session."""
    probe = None
    try:
        probe = await page.context.new_page()
        await probe.goto('https://www.tiktok.com/search/video?q=tiktok',
                         wait_until='domcontentloaded', timeout=timeout * 1000)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            parsed = urlparse(probe.url)
            if parsed.hostname != 'www.tiktok.com' or parsed.path.startswith('/login'):
                return False
            if await probe.locator(SELECTOR).count():
                return False
            if await probe.locator(SEARCH_RESULTS_SELECTOR).count():
                # The page may render cached results before a challenge appears.
                await asyncio.sleep(.8)
                return (not await probe.locator(SELECTOR).count()
                        and bool(await probe.locator(SEARCH_RESULTS_SELECTOR).count()))
            await asyncio.sleep(.25)
        return False
    except Exception:
        return False
    finally:
        if probe is not None:
            await probe.close()


class CaptchaManager:
    def __init__(self, *, timeout=180, poll=0.5, confirmation_timeout=5, callback=None,
                 max_attempts=3, retry_window=5, readiness_timeout=5):
        self.timeout, self.poll, self.confirmation_timeout = timeout, poll, confirmation_timeout
        self.max_attempts, self.retry_window = max_attempts, retry_window
        self.readiness_timeout = readiness_timeout
        self.callback = callback

    def emit(self, state, kind, detail=''):
        if self.callback:
            self.callback(CaptchaEvent(state, kind, detail))

    @staticmethod
    async def _image_signature(page):
        try:
            return tuple(await page.locator(
                '#captcha_container img, #secsdk-captcha img, #captcha-verify-container-main-page img'
            ).evaluate_all("els => els.map(e => e.currentSrc || e.src).filter(Boolean)")) or None
        except Exception:
            return None

    async def handle(self, page, *, visible):
        challenge = page.locator(SELECTOR)
        if not await challenge.count():
            return False
        kind = 'unknown'
        self.emit(CaptchaState.DETECTED, kind)
        verified = asyncio.Event()
        verification_response = asyncio.Event()
        automatic_attempted = False
        probe_attempted = False
        search_was_empty = (urlparse(page.url).hostname == 'www.tiktok.com'
                            and urlparse(page.url).path.startswith('/search')
                            and not await page.locator(SEARCH_RESULTS_SELECTOR).count())

        async def on_response(response):
            try:
                if not is_verification_endpoint(response.url) or not response.ok:
                    return
                data = await response.json()
                if verified_response(response.url, data):
                    verified.set()
                elif (isinstance(data, dict) and data.get('edata')
                      and data.get('success') is not False
                      and ('code' not in data or data['code'] == 0)):
                    # Only the encrypted response format observed on TikTok is
                    # eligible for corroboration by newly loaded results.
                    verification_response.set()
            except Exception:
                pass

        page.on('response', on_response)
        try:
            kind = await classify(page)
            # The container can appear before its images. Give the widget time
            # to render before deciding that its layout is unsupported.
            ready_deadline = asyncio.get_running_loop().time() + self.readiness_timeout
            while kind == 'unknown' and asyncio.get_running_loop().time() < ready_deadline and await challenge.count():
                await asyncio.sleep(self.poll)
                kind = await classify(page)
            self.emit(CaptchaState.CLASSIFIED, kind)
            # Retry only when the site replaces the challenge images. Never
            # repeat guesses against an unchanged image.
            if kind in ('rotate', 'slide', 'unknown'):
                try:
                    for attempt in range(self.max_attempts):
                        signature = await self._image_signature(page)
                        self.emit(CaptchaState.SOLVING, kind, str(attempt + 1))
                        attempted = False
                        if kind in ('rotate', 'unknown'):
                            from app.workers.captcha_rotation import try_rotation
                            attempted = await asyncio.wait_for(try_rotation(page), timeout=12)
                        if not attempted and kind in ('slide', 'unknown'):
                            from app.workers.captcha_slide import try_slide
                            attempted = await asyncio.wait_for(try_slide(page), timeout=12)
                        automatic_attempted = automatic_attempted or attempted
                        if not attempted:
                            break
                        deadline = asyncio.get_running_loop().time() + self.retry_window
                        changed = False
                        while await challenge.count() and asyncio.get_running_loop().time() < deadline:
                            new_signature = await self._image_signature(page)
                            if signature and new_signature and signature != new_signature:
                                changed = True
                                break
                            await asyncio.sleep(self.poll)
                        if not changed:
                            break
                        kind = await classify(page)
                        if kind not in ('rotate', 'slide', 'unknown'):
                            break
                except Exception:
                    # Unknown layout or unavailable dependency: keep manual fallback.
                    logging.getLogger('ttlh').warning('Ошибка локальной обработки CAPTCHA', exc_info=True)
            still_visible = bool(await challenge.count())
            if still_visible and not visible:
                raise CaptchaError('TikTok запросил CAPTCHA. Запустите задачу с видимым окном и пройдите проверку')
            if still_visible:
                self.emit(CaptchaState.AWAITING_USER, kind)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            while await challenge.count():
                if page.is_closed():
                    raise CaptchaError('Окно закрыто до прохождения CAPTCHA')
                if loop.time() >= deadline:
                    raise CaptchaError(f'CAPTCHA не пройдена за {self.timeout} с')
                await asyncio.sleep(self.poll)
            self.emit(CaptchaState.VERIFYING, kind)
            deadline = loop.time() + self.confirmation_timeout
            while not verified.is_set():
                if page.is_closed():
                    raise CaptchaError('Окно закрыто до подтверждения CAPTCHA')
                if await challenge.count():
                    raise CaptchaError('Проверка появилась снова. Пройдите CAPTCHA и повторите задачу')
                if await page.locator(SUCCESS_SELECTOR).count():
                    verified.set()
                    break
                # Newer TikTok verification replies are encrypted. A successful
                # HTTP response alone is insufficient: on a previously empty
                # search page, newly loaded results corroborate continuation.
                if (search_was_empty and verification_response.is_set()
                        and await page.locator(SEARCH_RESULTS_SELECTOR).count()):
                    verified.set()
                    break
                # Home and video pages do not necessarily gain search results.
                # After our own attempt, corroborate an encrypted reply by
                # opening a read-only search with the same browser session.
                if (not search_was_empty and not probe_attempted and automatic_attempted
                        and verification_response.is_set()
                        and urlparse(page.url).hostname == 'www.tiktok.com'):
                    probe_attempted = True
                    if await confirm_search_access(page) and not await challenge.count():
                        verified.set()
                        break
                if loop.time() >= deadline:
                    raise CaptchaError('Окно CAPTCHA закрыто, но успех не подтверждён. Проверьте страницу и повторите задачу')
                await asyncio.sleep(self.poll)
            self.emit(CaptchaState.SOLVED, kind)
            self.emit(CaptchaState.RESUMED, kind)
            return True
        except asyncio.CancelledError:
            self.emit(CaptchaState.FAILED, kind, 'Задача отменена')
            raise
        except Exception as exc:
            self.emit(CaptchaState.FAILED, kind, str(exc) or 'Ошибка проверки')
            if isinstance(exc, CaptchaError):
                raise
            raise CaptchaError('Не удалось отследить CAPTCHA. Проверьте окно браузера') from exc
        finally:
            page.remove_listener('response', on_response)
