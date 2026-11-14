"""Playwright-парсер поиска TikTok и модуль автоматической отправки комментариев.

В приложении планировщик привязывает браузер к сессии и прокси конкретного
аккаунта через browser_binding. CLI использует отдельный прежний файл
data/session/search_state.json для ручной отладки.

CLI для отладки без веб-интерфейса:
    python -m app.workers.tiktok_search login
    python -m app.workers.tiktok_search check
    python -m app.workers.tiktok_search search "#coffee" "кофейня москва" [--visible]
    python -m app.workers.tiktok_search post "https://www.tiktok.com/@user/video/123" "Привет фулл тут gjrjgi" [--visible]
"""
import asyncio
import json
import logging
import random
import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from playwright.async_api import BrowserContext, Locator, Page, Response, async_playwright

from app.config import SEARCH_STATE_PATH
from app.services.sessions import browser_binding, verify_context
from app.services import settings as app_settings
from app.workers.captcha_manager import CaptchaError, CaptchaManager, SELECTOR as _CAPTCHA_SELECTOR

TIKTOK = "https://www.tiktok.com"
_VIDEO_HREF_RE = re.compile(r"/@([^/?#]+)/(?:video|photo)/(\d+)")
CAPTCHA_WAIT_S = 180  # сколько ждать, пока человек пройдёт капчу в видимом окне
comment_page = ContextVar("comment_page", default=None)
captcha_status = ContextVar("captcha_status", default=None)
comment_visible = ContextVar("comment_visible", default=None)
captcha_visible = ContextVar("captcha_visible", default=False)
session_verified = ContextVar("session_verified", default=False)
_API_MARKERS = ("/api/search/", "/api/challenge/item_list")


class SessionError(RuntimeError):
    pass


class SessionExpiredError(SessionError):
    """TikTok explicitly rejected the session; distinguish it from network failures."""
    pass


class BlockedError(CaptchaError):
    """TikTok показал «Страница недоступна» вместо выдачи. Как и капча, останавливает поиск целиком."""


class EmptyResultError(RuntimeError):
    pass


@dataclass
class FoundVideo:
    video_id: str
    url: str
    author: str | None
    description: str | None = None
    views: int | None = None


def proxy_settings(proxy_url: str | None) -> dict | None:
    """'http://user:pass@host:port' / 'socks5://host:port' → формат Playwright."""
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    if not p.scheme or not p.hostname or not p.port:
        raise ValueError(f"Некорректный прокси: {proxy_url!r}")
    settings = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        settings["username"] = unquote(p.username)
        settings["password"] = unquote(p.password or "")
    return settings


@asynccontextmanager
async def open_context(
    headless: bool = True,
    use_state: bool = True,
    proxy_override: str | None = None,
) -> AsyncIterator[BrowserContext]:
    async with async_playwright() as pw:
        channel = "chrome" if app_settings.use_system_chrome() else None
        binding = browser_binding.get()
        state_path = binding["path"] if binding else SEARCH_STATE_PATH
        effective_proxy = binding["proxy"] if binding else (proxy_override if proxy_override is not None else app_settings.search_proxy())
        try:
            browser = await pw.chromium.launch(
                headless=headless,
                proxy=proxy_settings(effective_proxy),
                channel=channel,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            if channel and "is not found" in str(e):
                raise RuntimeError(
                    "Google Chrome не найден. Установите его или снимите галочку "
                    "«Использовать установленный Google Chrome» в «Настройках»"
                ) from e
            raise
        ctx = None
        verified_token = session_verified.set(False)
        try:
            state = str(state_path) if use_state and state_path.exists() else None
            ctx = await browser.new_context(
                storage_state=state,
                locale="ru-RU",
                viewport={"width": 1366, "height": 860},
            )
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            ctx.set_default_timeout(30_000)
            yield ctx
        finally:
            try:
                if ctx is not None and session_verified.get() and binding:
                    temporary = state_path.with_suffix('.state.tmp')
                    try:
                        await ctx.storage_state(path=str(temporary))
                        temporary.replace(state_path)
                    except Exception:
                        logging.getLogger('ttlh').warning('Не удалось сохранить проверенную сессию браузера')
                    finally:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
            finally:
                session_verified.reset(verified_token)
                await browser.close()


async def _has_session(ctx: BrowserContext) -> bool:
    return any(c["name"] == "sessionid" and c["value"] for c in await ctx.cookies(TIKTOK))


# ---------- Сессия ----------

async def login_interactive(timeout_s: int = 300) -> str:
    """Открывает видимое окно браузера. Пользователь входит сам, после входа сессия сохраняется."""
    async with open_context(headless=False, use_state=False) as ctx:
        page = await ctx.new_page()
        await page.goto(f"{TIKTOK}/login", wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if page.is_closed():
                raise SessionError("Окно браузера закрыто до входа. Можно перенести сессию из своего браузера: «Настройки» → способ 1")
            if await _has_session(ctx):
                await page.wait_for_timeout(3000)  # даём дописать остальные куки
                await ctx.storage_state(path=str(browser_binding.get()["path"] if browser_binding.get() else SEARCH_STATE_PATH))
                return "Сессия сохранена"
            await asyncio.sleep(2)
    raise SessionError(
        f"Вход не выполнен за {timeout_s} с. Если окно не пускает (например, вход через Google), "
        "перенесите сессию из своего браузера: «Настройки» → способ 1"
    )


async def _account_info(ctx: BrowserContext, page: Page | None = None) -> dict | None:
    """Ask Passport inside the live website, with its cookies and request hooks.

    APIRequestContext bypasses the web client's request initialization. A refusal
    from that separate client is not proof that an imported browser session died.
    Never infer identity from a public profile or from the presence of cookies.
    """
    if page is None:
        page = next((p for p in ctx.pages if not p.is_closed()
                     and urlparse(p.url).hostname == "www.tiktok.com"), None)
    owned = page is None
    if owned:
        page = await ctx.new_page()
        if comment_visible.get() is not None:
            comment_page.set(page)
    try:
        if owned or urlparse(page.url).hostname != "www.tiktok.com":
            await page.goto(TIKTOK, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
        if comment_visible.get() is not None or captcha_visible.get():
            await _raise_if_blocked(page, bool(comment_visible.get() or captcha_visible.get()))
        rejected = 0
        # Web app first; retain the previous Passport app ID as a compatibility fallback.
        for aid in (1988, 1459):
            try:
                result = await page.evaluate("""async aid => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 15000);
                    try {
                        const params = new URLSearchParams({aid: String(aid),
                            app_name: 'tiktok_web', app_language: 'ru',
                            device_platform: 'web_pc', cookie_enabled: 'true'});
                        const response = await fetch('/passport/web/account/info/?' + params,
                            {credentials: 'include', cache: 'no-store', signal: controller.signal});
                        if (!response.ok) return null;
                        return await response.json();
                    } catch { return null; }
                    finally { clearTimeout(timer); }
                }""", aid)
            except Exception:
                result = None
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            username = data.get("username")
            if result.get("message") == "success" and isinstance(username, str) and username.strip():
                return result
            if result.get("message") == "error" and (
                data.get("name") == "session_expired" or data.get("error_code") == 13
            ):
                rejected += 1
        await _raise_if_blocked(page, visible=bool(comment_visible.get() or captcha_visible.get()))
        if rejected == 2:
            raise SessionExpiredError(
                "TikTok отклонил вход в браузере программы. Если в обычном браузере аккаунт работает, "
                "перенесите все cookies TikTok: одного sessionid может быть недостаточно. "
                "Либо используйте «Войти в браузере»."
            )
        return None
    finally:
        if owned and comment_visible.get() is None:
            await page.close()


async def check_session() -> tuple[bool, str, str | None]:
    """(сессия действует, текст для UI, ник аккаунта, если TikTok его отдал)."""
    if not (browser_binding.get()["path"] if browser_binding.get() else SEARCH_STATE_PATH).exists():
        return False, "Сессии нет: войдите или импортируйте куки", None
    async with open_context() as ctx:
        page = await ctx.new_page()
        await page.goto(TIKTOK, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if not await _has_session(ctx):
            return False, "Сессия истекла: войдите заново", None

        info = await _account_info(ctx)
        username = ((info or {}).get("data") or {}).get("username")
        if not username or (info or {}).get("message") == "error":
            raise SessionError("Не удалось подтвердить аккаунт: TikTok не ответил на проверку входа. "
                               "Это не подтверждает истечение сессии. Повторите проверку; "
                               "при импорте попробуйте перенести все cookies TikTok вместо одного sessionid.")
        expected = (browser_binding.get() or {}).get("username")
        if expected and username.lstrip("@").casefold() != expected.lstrip("@").casefold():
            raise SessionError(f"Несовпадение сессии: ожидался @{expected}, TikTok вернул @{username}")
        # Do not overwrite the saved state on a failed check or identity mismatch.
        await ctx.storage_state(path=str(browser_binding.get()["path"] if browser_binding.get() else SEARCH_STATE_PATH))
        return True, f"Сессия активна: @{username}", username


def _direct_proxy_ip(proxy: str) -> str | None:
    """IP через прокси без браузера (только HTTP-прокси). None, если не получилось."""
    if not proxy.startswith(("http://", "https://")):
        return None
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open("https://api.ipify.org", timeout=20) as r:
            return r.read().decode().strip() or None
    except Exception:
        return None


def _explain_proxy_error(err: str, proxy: str | None) -> str:
    """Коды ошибок Chromium → понятный текст."""
    host = urlparse(proxy).netloc.rsplit("@", 1)[-1] if proxy else "прокси"
    if "ERR_PROXY_CONNECTION_FAILED" in err or "ERR_CONNECTION_REFUSED" in err or "ERR_CONNECTION_TIMED_OUT" in err:
        return (f"Компьютер не может подключиться к серверу прокси {host}. Логин и пароль тут ни при чём: "
                "соединение не доходит до прокси. Проверьте, не мешают ли VPN, антивирус или провайдер, "
                "или попросите у продавца другой сервер или порт.")
    if "ERR_TUNNEL_CONNECTION_FAILED" in err or "407" in err or "ERR_PROXY_AUTH" in err:
        return "Прокси отклонил подключение: проверьте логин и пароль или не закончился ли тариф."
    if "ERR_NAME_NOT_RESOLVED" in err:
        return f"Адрес прокси {host} не найден. Проверьте, нет ли опечатки."
    if "ERR_SOCKS_CONNECTION_FAILED" in err:
        return "SOCKS5-прокси не отвечает. Проверьте тип прокси: возможно, это HTTP."
    return f"Прокси не отвечает: {err.splitlines()[0]}"


async def test_proxy() -> str:
    """Через какой IP ходит парсер и открывается ли TikTok."""
    proxy = app_settings.search_proxy()
    async with open_context(use_state=False) as ctx:
        page = await ctx.new_page()
        try:
            await page.goto("https://api.ipify.org?format=json", timeout=30_000)
            ip = json.loads(await page.inner_text("body"))["ip"]
        except Exception as e:
            msg = _explain_proxy_error(str(e), proxy)
            # Проверяем прокси без браузера: если так он работает, браузер блокирует что-то на компьютере.
            direct_ip = await asyncio.to_thread(_direct_proxy_ip, proxy) if proxy else None
            if direct_ip:
                msg = (f"Прокси рабочий (напрямую отвечает с IP {direct_ip}), но браузер панели не может к нему "
                       "подключиться. Обычно это антивирус или фаервол. Включите ниже галочку "
                       "«Использовать установленный Google Chrome» или добавьте в исключения антивируса "
                       r"папку %LOCALAPPDATA%\ms-playwright")
            raise RuntimeError(msg) from e
        try:
            resp = await page.goto(TIKTOK, wait_until="domcontentloaded", timeout=45_000)
            tiktok = "открывается" if resp and resp.ok else f"ответ {resp.status if resp else 'пустой'}"
        except Exception:
            tiktok = "не открывается"
    via = f"через прокси {app_settings.mask_proxy(proxy)}" if proxy else "без прокси"
    browser = "Google Chrome" if app_settings.use_system_chrome() else "встроенный браузер"
    return f"IP {ip} ({via}, {browser}), TikTok {tiktok}"


# ---------- Поиск ----------

def search_url(query: str) -> str:
    q = query.strip()
    if q.startswith("#"):
        return f"{TIKTOK}/tag/{quote(q[1:])}"
    return f"{TIKTOK}/search/video?q={quote(q)}"


def _parse_item(item: dict) -> FoundVideo | None:
    vid = str(item.get("id") or "")
    author_raw = item.get("author")
    author = (
        author_raw.get("uniqueId")
        if isinstance(author_raw, dict)
        else (str(author_raw) if isinstance(author_raw, str) else None)
    )
    if author:
        author = unquote(author).strip().lstrip("@")
    if not vid.isdigit() or not author:
        return None
    kind = "photo" if item.get("imagePost") else "video"
    return FoundVideo(
        video_id=vid,
        url=f"{TIKTOK}/@{author}/{kind}/{vid}",
        author=author,
        description=item.get("desc") or None,
        views=(item.get("stats") or {}).get("playCount"),
    )


def _extract_items(data: dict) -> list[dict]:
    items = list(data.get("item_list") or data.get("itemList") or [])
    for row in data.get("data") or []:  # /api/search/...: [{"type": 1, "item": {...}}]
        if isinstance(row, dict) and isinstance(row.get("item"), dict):
            items.append(row["item"])
    return items


async def _raise_if_blocked(page: Page, visible: bool) -> None:
    if "/login" in page.url:
        raise SessionError("TikTok требует вход: нажмите «Войти» на дашборде")
    manager = CaptchaManager(timeout=CAPTCHA_WAIT_S, callback=captcha_status.get())
    await manager.handle(page, visible=visible)

_ERROR_PAGE_RE = re.compile(
    r"Страница недоступна|Не удалось найти эту страницу|Страница не найдена|"
    r"Что-то пошло не так|Page not available|Couldn't find this page|Page not found|Something went wrong",
    re.I,
)
_RETRY_SELECTOR = (
    'button:has-text("Попробовать"), button:has-text("Обновить"), '
    'button:has-text("Try again"), button:has-text("Refresh")'
)


async def _error_page_shown(page: Page) -> bool:
    try:
        if await page.locator('a[href*="/video/"], a[href*="/photo/"]').count() > 0:
            return False
        return await page.get_by_text(_ERROR_PAGE_RE).first.is_visible()
    except Exception:
        return False


async def _recover_error_page(page: Page, visible: bool) -> None:
    """TikTok иногда отвечает «Страница недоступна». Пробуем до 3 раз нажать «Попробовать ещё раз»
    или перезагрузить страницу; в видимом окне даём человеку разобраться самому."""
    if not await _error_page_shown(page):
        return
    for _ in range(3):
        try:
            await page.locator(_RETRY_SELECTOR).first.click(timeout=5000)
        except Exception:
            await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(random.uniform(3000, 5000))
        if not await _error_page_shown(page):
            return
    if visible:
        try:
            await page.locator('a[href*="/video/"], a[href*="/photo/"]').first.wait_for(timeout=CAPTCHA_WAIT_S * 1000)
            return
        except Exception:
            pass
    raise BlockedError(
        "TikTok не показал выдачу («Страница недоступна»). Обычно причина одна из двух: "
        "1) TikTok не подключён или сессия истекла (проверьте плашку в «Настройках»); "
        "2) TikTok временно ограничил этот IP: попробуйте позже или другой прокси. "
        "В режиме «окно» можно нажать «Попробовать ещё раз» самому, парсер подождёт."
    )


async def _collect_dom(page: Page, found: dict[str, FoundVideo]) -> None:
    hrefs = await page.eval_on_selector_all('a[href*="/video/"], a[href*="/photo/"]', "els => els.map(e => e.href)")
    for href in hrefs:
        if m := _VIDEO_HREF_RE.search(href):
            author, vid = m.groups()
            author = unquote(author).strip().lstrip("@")
            kind = "photo" if "/photo/" in href else "video"
            if author and vid.isdigit():
                found.setdefault(vid, FoundVideo(video_id=vid, url=f"{TIKTOK}/@{author}/{kind}/{vid}", author=author))


_LOAD_MORE_SELECTOR = (
    '[data-e2e="search-load-more"], button:has-text("Загрузить ещё"), '
    'button:has-text("Загрузить еще"), button:has-text("Load more")'
)
_MAX_STALE = 5  # сколько подгрузок подряд без новых роликов терпим, прежде чем остановиться


async def _load_more(page: Page) -> None:
    """Следующая порция выдачи. На странице поиска TikTok подгружает ролики кнопкой
    «Загрузить ещё», на странице хэштега прокруткой, поэтому пробуем оба способа."""
    btn = page.locator(_LOAD_MORE_SELECTOR).first
    try:
        if await btn.is_visible():
            await btn.scroll_into_view_if_needed(timeout=3000)
            await btn.click(timeout=5000)
            return
    except Exception:
        pass
    try:
        await page.locator('a[href*="/video/"]').last.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    # Колесо крутит тот блок, над которым курсор, поэтому ставим курсор в центр выдачи.
    vp = page.viewport_size or {"width": 1366, "height": 860}
    await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
    await page.mouse.wheel(0, 3000)


async def search_videos(ctx: BrowserContext, query: str, limit: int = 30, visible: bool = False,
                        on_partial: Callable[[str, list[FoundVideo]], None] | None = None,
                        page: Page | None = None) -> list[FoundVideo]:
    found: dict[str, FoundVideo] = {}

    async def on_response(resp: Response) -> None:
        if not any(m in resp.url for m in _API_MARKERS):
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for item in _extract_items(data):
            if v := _parse_item(item):
                found[v.video_id] = v

    owned = page is None
    if owned:
        page = await ctx.new_page()
    page.on("response", on_response)
    try:
        await page.goto(search_url(query), wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(random.uniform(2000, 3500))
        await _raise_if_blocked(page, visible)
        await _recover_error_page(page, visible)

        stale = 0
        while len(found) < limit and stale < _MAX_STALE:
            before = len(found)
            await _collect_dom(page, found)
            await _raise_if_blocked(page, visible)
            await _load_more(page)
            await page.wait_for_timeout(random.uniform(2000, 3500))  # ждём подгрузку выдачи
            await _raise_if_blocked(page, visible)
            await _recover_error_page(page, visible)
            await _collect_dom(page, found)
            stale = stale + 1 if len(found) == before else 0
        if not found:
            raise EmptyResultError(
                "0 роликов: TikTok не отдал выдачу. Проверьте подключение TikTok в «Настройках» "
                "или запустите поиск в видимом окне"
            )
        return list(found.values())[:limit]
    except BaseException:
        if found and on_partial:
            on_partial(query, list(found.values())[:limit])
        raise
    finally:
        page.remove_listener("response", on_response)
        if owned:
            await page.close()


async def search_many(
    queries: list[str],
    limit: int,
    on_results: Callable[[str, list[FoundVideo]], None],
    on_error: Callable[[str, Exception], None],
    visible: bool = False,
) -> None:
    async with open_context(headless=not visible) as ctx:
        page = await ctx.new_page()
        try:
            if browser_binding.get():
                await verify_context(ctx, browser_binding.get()["username"], page=page)
                session_verified.set(True)
            for i, query in enumerate(queries):
                if i:
                    await asyncio.sleep(random.uniform(3, 6))
                try:
                    on_results(query, await search_videos(ctx, query, limit, visible, on_partial=on_results, page=page))
                except (SessionError, CaptchaError):
                    raise
                except Exception as e:
                    on_error(query, e)
        finally:
            await page.close()


# ---------- Автоматическая отправка комментария ----------

@dataclass
class CommentResult:
    success: bool
    message: str
    error_code: str | None = None  # comments_disabled | video_unavailable | session_expired | captcha | rate_limit | input_not_found | button_not_found | unknown


async def _dismiss_popups(page: Page) -> None:
    """Закрывает только безопасные баннеры куки и гостевых подсказок,
    не трогая модальные окна капчи и панель комментариев."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Разрешить все")',
        'button:has-text("Allow all cookies")',
        'button:has-text("Принять все")',
        'button:has-text("Continue as guest")',
        'button:has-text("Продолжить как гость")',
        'button:has-text("Not now")',
        'button:has-text("Не сейчас")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click(timeout=1000)
                await page.wait_for_timeout(200)
        except Exception:
            pass


async def _clear_blocking_non_captcha_modal(page: Page) -> None:
    """Вызывается только тогда, когда обычный клик по полю комментария перехвачен
    посторонним оверлеем (.TUXModal-overlay). Не трогает капчу и не скрывает порталы комментариев."""
    try:
        if await page.locator(_CAPTCHA_SELECTOR).count() > 0:
            return
        overlay = page.locator(".TUXModal-overlay")
        if await overlay.count() > 0 and await overlay.first.is_visible():
            await page.evaluate(
                """() => {
                    document.querySelectorAll('.TUXModal-overlay').forEach(el => {
                        const hasCaptcha = el.querySelector('[id*="captcha"], [class*="captcha" i], iframe[src*="captcha"], iframe[src*="verify"]');
                        const hasComments = el.querySelector('[data-e2e*="comment"], [class*="Comment" i]');
                        if (!hasCaptcha && !hasComments) {
                            el.style.pointerEvents = 'none';
                        }
                    });
                }"""
            )
    except Exception:
        pass


_VIDEO_OR_COMMENTS_SELECTOR = (
    '[data-e2e="comment-input"], [data-e2e="comment-text"], '
    '[data-e2e="comment-icon"], [data-e2e="browse-comment-icon"], '
    '[data-e2e="comment-disabled"], [data-e2e="browse-video"], '
    '[data-e2e="detail-video"], div[class*="DivCommentInputContainer"], '
    'div[contenteditable="true"][role="textbox"]'
)
_VISIBLE_VIDEO_OR_COMMENTS_SELECTOR = f":is({_VIDEO_OR_COMMENTS_SELECTOR}):visible"

_UNAVAILABLE_TEXTS = (
    "Видео недоступно",
    "Видео сейчас недоступно",
    "Video currently unavailable",
    "This video is private",
    "This video has been removed",
    "Видео удалено",
    "Страница недоступна",
    "Не удалось найти эту страницу",
    "Страница не найдена",
    "Page not available",
    "Couldn't find this page",
    "Page not found",
    "Что-то пошло не так",
    "Something went wrong",
)


async def _has_video_or_comments(page: Page) -> bool:
    """Проверяет, отрисовался ли плеер ролика или блок комментариев."""
    try:
        loc = page.locator(_VISIBLE_VIDEO_OR_COMMENTS_SELECTOR).first
        return await loc.is_visible(timeout=500)
    except Exception:
        return False


async def _find_unavailable_text(page: Page) -> str | None:
    for t in _UNAVAILABLE_TEXTS:
        try:
            if await page.get_by_text(t, exact=False).first.is_visible(timeout=300):
                return t
        except Exception:
            pass
    return None


async def _cross_region_block(page: Page) -> bool:
    """Read TikTok's page status only; never change the account or its cookies."""
    try:
        return bool(await page.evaluate("""() => {
            try {
                const node = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                if (!node) return false;
                const data = JSON.parse(node.textContent).__DEFAULT_SCOPE__?.['webapp.video-detail'];
                return data?.statusCode === 10204 &&
                    typeof data.statusMsg === 'string' && data.statusMsg.includes('ru_cross_border_block');
            } catch { return false; }
        }"""))
    except Exception:
        return False


async def _recover_video_page(page: Page) -> bool:
    """Пробует до 3 раз восстановить страницу ролика, если TikTok временно показал
    «Страница не найдена» / «Страница недоступна» / «Видео сейчас недоступно»."""
    orig_url = page.url or ""
    for attempt in range(3):
        try:
            retry_btn = page.locator(_RETRY_SELECTOR).first
            if await retry_btn.is_visible(timeout=800):
                await retry_btn.click(timeout=3000)
            elif attempt == 0:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
            elif attempt == 1 and orig_url.startswith("http"):
                sep = "&" if "?" in orig_url else "?"
                webapp_url = f"{orig_url}{sep}is_from_webapp=1&sender_device=pc"
                await page.goto(webapp_url, wait_until="domcontentloaded", timeout=45_000, referer=f"{TIKTOK}/")
            elif attempt == 2 and "/video/" in orig_url:
                photo_url = orig_url.replace("/video/", "/photo/")
                await page.goto(photo_url, wait_until="domcontentloaded", timeout=45_000, referer=f"{TIKTOK}/")
            else:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass

        await page.wait_for_timeout(random.uniform(2500, 4000))
        await _dismiss_popups(page)
        if await _has_video_or_comments(page):
            return True
    return False


async def _check_video_availability(page: Page) -> tuple[bool | None, str | None]:
    """Проверяет, доступно ли видео для просмотра, с защитой от ложных срабатываний
    и автоматической перезагрузкой при временном сбое TikTok («Страница не найдена»)."""
    if await _has_video_or_comments(page):
        return True, None

    matched = await _find_unavailable_text(page)
    if not matched:
        # TikTok may render its shell first and insert the player several seconds
        # later. Give the actual content a bounded chance to appear.
        try:
            await page.locator(_VISIBLE_VIDEO_OR_COMMENTS_SELECTOR).first.wait_for(state="visible", timeout=12_000)
        except Exception:
            pass
        if await _has_video_or_comments(page):
            return True, None
        matched = await _find_unavailable_text(page)
        if not matched:
            return None, "Страница ролика не загрузилась: видео и комментарии не появились"

    if await _cross_region_block(page):
        return False, "TikTok ограничил просмотр этого ролика между регионами для данного аккаунта (код 10204)"

    if await _recover_video_page(page):
        return True, None

    matched_after = await _find_unavailable_text(page)
    if not matched_after:
        return None, "Страница ролика не загрузилась после повторных попыток"
    return False, f"Ролик недоступен ({matched_after})"


def _availability_error(avail: bool | None, message: str | None) -> CommentResult | None:
    if avail is True:
        return None
    message = message or "Не удалось проверить доступность ролика"
    # Generic TikTok unavailable pages can be temporary or account-specific.
    # Keep the draft for retry unless TikTok explicitly says it was removed/private.
    permanent = any(text in message.lower() for text in (
        "this video is private", "this video has been removed", "видео удалено",
    ))
    if avail is False and permanent:
        return CommentResult(False, message, "video_unavailable")
    return CommentResult(False, f"{message}. Черновик сохранён; проверьте доступ к ролику для этого аккаунта и повторите запуск", "page_unavailable")


async def _check_comments_disabled(page: Page) -> bool:
    """Проверяет, отключены ли комментарии автором."""
    indicators = [
        '[data-e2e="comment-disabled"]',
        'div:has-text("Комментарии отключены")',
        'div:has-text("Comments are turned off")',
        'div:has-text("Comments have been disabled")',
        'p:has-text("Комментарии отключены")',
        'p:has-text("Comments are turned off")',
        'span:has-text("Комментарии отключены")',
        'span:has-text("Comments are turned off")',
    ]
    for sel in indicators:
        try:
            if await page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    return False


async def _check_login_required(page: Page) -> bool:
    """Проверяет, требует ли страница авторизацию."""
    if "/login" in page.url:
        return True
    login_indicators = [
        '[data-e2e="login-modal"]',
        'div[id*="login-modal"]',
        'div:has-text("Войдите, чтобы оставлять комментарии")',
        'div:has-text("Log in to comment")',
    ]
    for sel in login_indicators:
        try:
            if await page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    return False


_COMMENTS_OPEN_SELECTORS = (
    '[data-e2e="comment-input"]:visible, '
    '[data-e2e="comment-text"]:visible, '
    'div[class*="DivCommentInputContainer"]:visible, '
    'div[class*="DraftEditor-root"]:visible, '
    'div[contenteditable="true"][role="textbox"]:visible, '
    '[data-e2e="comment-level-1"]:visible, '
    '[data-e2e="comment-list"]:visible, '
    '[data-e2e="browse-comment-list"]:visible, '
    'div[class*="DivCommentListContainer"]:visible, '
    'div[class*="DivCommentMain"]:visible'
)


async def _ensure_comments_open(page: Page) -> None:
    """Если секция комментариев уже открыта (видно поле ввода или список чужих комментариев),
    ничего не нажимаем, чтобы не свернуть комментарии и не переключить вкладку."""
    try:
        if await page.locator(_COMMENTS_OPEN_SELECTORS).count() > 0:
            return
        if await page.locator('[data-e2e="comment-input"], div[class*="DivCommentInputContainer"]').first.is_visible():
            return
    except Exception:
        pass

    # Если открыта соседняя вкладка «Видео автора», переключаемся на вкладку «Комментарии»
    comment_tabs = [
        '[data-e2e="comment-tab"]',
        'div[role="tab"]:has-text("Комментарии")',
        'div[role="tab"]:has-text("Comments")',
        'button[role="tab"]:has-text("Комментарии")',
        'button[role="tab"]:has-text("Comments")',
    ]
    for sel in comment_tabs:
        try:
            tab = page.locator(sel).first
            if await tab.is_visible(timeout=400):
                await tab.click(timeout=1500)
                await page.wait_for_timeout(random.uniform(500, 900))
                if await page.locator(_COMMENTS_OPEN_SELECTORS).count() > 0:
                    return
        except Exception:
            pass

    comment_icons = [
        '[data-e2e="comment-icon"]',
        '[data-e2e="browse-comment-icon"]',
        'span[data-e2e="comment-icon"]',
        'button[aria-label*="комментар" i]',
        'button[aria-label*="comment" i]',
    ]
    for sel in comment_icons:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                try:
                    await loc.click(timeout=1500)
                except Exception:
                    await loc.click(force=True, timeout=1500)
                await page.wait_for_timeout(random.uniform(700, 1200))
                return
        except Exception:
            pass


async def _find_comment_editor(page: Page) -> Locator | None:
    """Находит и активирует поле ввода комментария."""
    await _dismiss_popups(page)

    # 1. Если есть контейнер-обёртка поля ввода, кликаем по нему для активации Draft.js
    containers = [
        '[data-e2e="comment-input"]',
        '[data-e2e="comment-text"]',
        'div[class*="DivCommentInputContainer"]',
        'div[class*="DraftEditor-editorContainer"]',
        'div[class*="DraftEditor-root"]',
        '[data-e2e="comment-text-area"]',
    ]
    for sel in containers:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                await loc.scroll_into_view_if_needed(timeout=2000)
                try:
                    await loc.click(timeout=1500)
                except Exception:
                    await _clear_blocking_non_captcha_modal(page)
                    await loc.click(force=True, timeout=1500)
                await page.wait_for_timeout(random.uniform(300, 600))
                break
        except Exception:
            pass

    # 2. Ищем сам редактируемый элемент
    editable_selectors = [
        'div[class*="notranslate"][contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        '[data-e2e="comment-text-area"]',
        'div[role="textbox"]',
        'textarea[placeholder*="комментар" i]',
        'textarea[placeholder*="comment" i]',
        'textarea',
    ]
    for sel in editable_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                try:
                    await loc.click(timeout=1500)
                except Exception:
                    await _clear_blocking_non_captcha_modal(page)
                    await loc.click(force=True, timeout=1500)
                try:
                    await loc.focus()
                except Exception:
                    pass
                return loc
        except Exception:
            pass
    return None


async def _find_post_button(page: Page) -> Locator | None:
    """Находит кнопку отправки комментария."""
    post_selectors = [
        '[data-e2e="comment-post"]',
        'div[data-e2e="comment-post"]',
        'button[data-e2e="comment-post"]',
        '[data-e2e="comment-post-button"]',
        'div[data-e2e="comment-post-button"]',
        'button:has-text("Опубликовать")',
        'button:has-text("Post")',
        'div[role="button"]:has-text("Опубликовать")',
        'div[role="button"]:has-text("Post")',
        'div[class*="DivPostButton"]',
        'button[type="submit"]',
    ]
    for sel in post_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                return loc
        except Exception:
            pass
    return None


async def _read_editor_text(editor: Locator) -> str:
    return await editor.evaluate("el => 'value' in el ? el.value : el.innerText")


async def post_comment_on_video(
    ctx: BrowserContext,
    video_url: str,
    comment_text: str,
    visible: bool = False,
) -> CommentResult:
    """Переходит по ссылке ролика, вводит текст комментария и публикует его."""
    if not await _has_session(ctx):
        return CommentResult(
            success=False,
            message="Сессия TikTok недействительна или отсутствует. Подключите аккаунт в «Настройках»",
            error_code="session_expired",
        )

    page = comment_page.get()
    owned = page is None
    if owned:
        page = await ctx.new_page()
    attempted = False
    try:
        # Перехватываем ответы TikTok API на публикацию комментариев
        api_response: dict = {"caught": False, "status_code": None, "status_msg": None}

        async def on_response(resp: Response) -> None:
            if any(marker in resp.url for marker in ("/comment/publish/", "/api/comment/publish", "/node/share/comment/post")):
                try:
                    data = await resp.json()
                    api_response["caught"] = True
                    api_response["status_code"] = data.get("status_code")
                    api_response["status_msg"] = (
                        data.get("status_msg")
                        or data.get("message")
                        or data.get("status_message")
                    )
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=45_000, referer=f"{TIKTOK}/")
        except Exception as e:
            return CommentResult(
                success=False,
                message=f"Не удалось открыть страницу ролика: {e}",
                error_code="page_unavailable",
            )

        await page.wait_for_timeout(random.uniform(2000, 3500))
        # Проверка до взаимодействия с любыми элементами страницы.
        try:
            await _raise_if_blocked(page, visible)
        except CaptchaError as e:
            return CommentResult(success=False, message=str(e), error_code="captcha")
        except SessionError as e:
            return CommentResult(success=False, message=str(e), error_code="session_expired")
        await _dismiss_popups(page)

        # Проверка доступности видео (включая авто-перезагрузку при ложном «Страница не найдена»)
        avail, avail_msg = await _check_video_availability(page)
        availability_error = _availability_error(avail, avail_msg)
        if availability_error:
            return availability_error

        # Проверка требования входа
        if await _check_login_required(page):
            return CommentResult(
                success=False,
                message="TikTok требует повторный вход в аккаунт",
                error_code="session_expired",
            )

        # Проверка отключенных комментариев
        if await _check_comments_disabled(page):
            return CommentResult(
                success=False,
                message="Комментарии к ролику отключены автором",
                error_code="comments_disabled",
            )

        # Убедимся, что секция комментариев раскрыта
        await _ensure_comments_open(page)
        await page.wait_for_timeout(random.uniform(500, 1000))
        await _dismiss_popups(page)

        # Ищем поле ввода (с одной перезагрузкой страницы, если секция не успела прогрузиться)
        editor = await _find_comment_editor(page)
        if not editor and not await _check_comments_disabled(page):
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(random.uniform(2500, 3500))
                await _dismiss_popups(page)
                await _raise_if_blocked(page, visible)
                avail, avail_msg = await _check_video_availability(page)
                availability_error = _availability_error(avail, avail_msg)
                if availability_error:
                    return availability_error
                await _ensure_comments_open(page)
                await page.wait_for_timeout(random.uniform(500, 1000))
                editor = await _find_comment_editor(page)
            except CaptchaError as e:
                return CommentResult(False, str(e), "captcha")
            except SessionError as e:
                return CommentResult(False, str(e), "session_expired")
            except Exception as e:
                return CommentResult(False, f"Не удалось перезагрузить страницу ролика: {e}", "page_unavailable")

        if not editor:
            # Повторно проверяем отключены ли комментарии
            if await _check_comments_disabled(page):
                return CommentResult(
                    success=False,
                    message="Комментарии к ролику отключены автором",
                    error_code="comments_disabled",
                )
            return CommentResult(
                success=False,
                message="Поле ввода комментария не найдено на странице",
                error_code="input_not_found",
            )

        # Очищаем поле (если было что-то введено)
        try:
            await page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # Имитируем естественный ввод текста человеком
        for char in comment_text:
            try:
                await _raise_if_blocked(page, visible)
            except CaptchaError as e:
                return CommentResult(False, str(e), "captcha")
            # A verification challenge may steal focus from the editor.
            await editor.focus()
            await page.keyboard.type(char, delay=random.uniform(25, 60))
            if char in (" ", ",", ".", "!", "?", "\n") and random.random() < 0.2:
                await asyncio.sleep(random.uniform(0.1, 0.3))

        await page.wait_for_timeout(random.uniform(800, 1500))

        # Ищем кнопку отправки
        try:
            await _raise_if_blocked(page, visible)
        except CaptchaError as e:
            return CommentResult(False, str(e), "captcha")
        try:
            typed_text = await _read_editor_text(editor)
        except Exception:
            return CommentResult(False, "Не удалось проверить текст в поле комментария", "input_not_found")
        if typed_text.replace("\r\n", "\n").strip() != comment_text.replace("\r\n", "\n").strip():
            return CommentResult(False, "Текст комментария введён не полностью; черновик сохранён", "input_not_found")
        post_btn = await _find_post_button(page)
        if not post_btn:
            return CommentResult(False, "Кнопка отправки не найдена", "button_not_found")
        if await post_btn.is_disabled() or await post_btn.get_attribute("aria-disabled") == "true":
            await page.wait_for_timeout(2000)
            if await post_btn.is_disabled() or await post_btn.get_attribute("aria-disabled") == "true":
                return CommentResult(False, "Кнопка отправки недоступна", "button_not_found")
        attempted = True
        await post_btn.click(timeout=3000)

        # Ждём подтверждения публикации (до 8 секунд), отслеживая возможное появление капчи
        for _ in range(16):
            if api_response["caught"]:
                break
            try:
                if await page.locator(_CAPTCHA_SELECTOR).count() > 0:
                    await _raise_if_blocked(page, visible)
                    # После прохождения капчи даём TikTok завершить запрос публикации
                    for _ in range(10):
                        if api_response["caught"]:
                            break
                        await page.wait_for_timeout(500)
                    break
            except CaptchaError as e:
                return CommentResult(
                    success=False,
                    message=f"Капча после отправки: {e}",
                    error_code="uncertain",
                )
            except Exception:
                pass
            await page.wait_for_timeout(500)

        # Проверка после отправки на появление капчи
        try:
            await _raise_if_blocked(page, visible)
        except CaptchaError as e:
            return CommentResult(
                success=False,
                message=f"Капча после отправки: {e}",
                error_code="uncertain",
            )

        # Анализ ответа API
        if api_response["caught"]:
            sc = api_response["status_code"]
            msg = api_response["status_msg"] or ""
            if sc is None:
                return CommentResult(False, "Ответ TikTok не содержит подтверждения результата. Проверьте вручную", "uncertain")
            if sc == 0:
                await page.wait_for_timeout(1500)
                return CommentResult(success=True, message="TikTok принял комментарий. Видимость другим пользователям не проверена")

            # Известные коды ошибок TikTok
            if any(w in msg.lower() for w in ("captcha", "verify", "проверк")):
                error_code = "captcha"
            elif any(w in msg.lower() for w in ("fast", "часто", "limit", "block", "restrict")):
                error_code = "rate_limit"
            else:
                error_code = "api_error"
            return CommentResult(
                success=False,
                message=f"TikTok отклонил публикацию: {msg or f'код {sc}'}",
                error_code=error_code,
            )

        # Если ответ API не перехвачен, проверяем DOM на наличие ошибок/успеха
        toast_error_texts = [
            "Не удалось опубликовать",
            "Couldn't post",
            "Слишком часто",
            "Too fast",
            "Слишком быстро",
            "Действие заблокировано",
        ]
        for t in toast_error_texts:
            try:
                if await page.get_by_text(t, exact=False).first.is_visible(timeout=400):
                    return CommentResult(
                        success=False,
                        message=f"TikTok показал предупреждение: «{t}»",
                        error_code="rate_limit",
                    )
            except Exception:
                pass

        return CommentResult(success=False, message="Результат публикации не подтверждён. Проверьте комментарии вручную", error_code="uncertain")

    except Exception as e:
        msg = str(e) or type(e).__name__
        return CommentResult(
            success=False,
            message=f"Ошибка автоматической отправки: {msg}",
            error_code="uncertain" if attempted else "unknown",
        )
    finally:
        page.remove_listener("response", on_response)
        if owned:
            await page.close()


# ---------- CLI ----------

async def _cli(argv: list[str]) -> None:
    cmd, *args = argv or ["help"]
    if cmd == "login":
        print(await login_interactive())
    elif cmd == "check":
        print((await check_session())[1])  # noqa
    elif cmd == "search" and args:
        await search_many(
            [a for a in args if a != "--visible"], 20, visible="--visible" in args,
            on_results=lambda q, vs: [print(q, v.url, v.views, (v.description or "")[:60]) for v in vs],
            on_error=lambda q, e: print(f"{q}: ошибка {e}"),
        )
    elif cmd == "post" and len(args) >= 2:
        url = args[0]
        text = " ".join([a for a in args[1:] if a != "--visible"])
        visible = "--visible" in args
        async with open_context(headless=not visible) as ctx:
            result = await post_comment_on_video(ctx, url, text, visible=visible)
            print(f"[{'УСПЕХ' if result.success else 'ОШИБКА'}] {result.message}")
    else:
        print(__doc__)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(_cli(sys.argv[1:]))
