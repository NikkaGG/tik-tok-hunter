"""Regressions for failed TikTok video navigation; never send real comments."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers import tiktok_search as ts


class NavigationRegressions(unittest.IsolatedAsyncioTestCase):
    def make_page(self, statuses):
        page = MagicMock()
        page.url = "https://www.tiktok.com/@test/video/1"
        page.close = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        listeners = {}
        page.on.side_effect = lambda name, fn: listeners.__setitem__(name, fn)

        async def goto(url, **kwargs):
            status = statuses.pop(0)
            response = MagicMock()
            response.status = status
            response.url = url
            response.headers = {"Retry-After": "0"}
            response.request.is_navigation_request.return_value = True
            response.request.frame = page.main_frame
            if "response" in listeners:
                await listeners["response"](response)
            if status >= 400:
                raise RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            return response
        page.goto = AsyncMock(side_effect=goto)
        return page

    async def run_post(self, page):
        ctx = MagicMock()
        ctx.new_page = AsyncMock(return_value=page)
        with patch.object(ts, "_has_session", new_callable=AsyncMock, return_value=True), \
             patch.object(ts, "_raise_if_blocked", new_callable=AsyncMock), \
             patch.object(ts, "_dismiss_popups", new_callable=AsyncMock), \
             patch.object(ts, "_check_video_availability", new_callable=AsyncMock, return_value=(False, "Недоступен")):
            return await ts.post_comment_on_video(ctx, "https://www.tiktok.com/@test/video/1", "test")

    async def test_5xx_http_failure_recovers_in_same_page(self):
        page = self.make_page([503, 200])
        with patch.object(ts.asyncio, "sleep", new_callable=AsyncMock):
            result = await self.run_post(page)
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(result.error_code, "page_unavailable")
        page.close.assert_awaited_once()

    async def test_404_http_failure_is_not_retried(self):
        page = self.make_page([404])
        result = await self.run_post(page)
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(result.error_code, "video_unavailable")
        page.close.assert_awaited_once()

    async def test_retries_are_bounded_and_draft_is_not_published(self):
        page = self.make_page([503, 503, 503])
        with patch.object(ts.asyncio, "sleep", new_callable=AsyncMock):
            result = await self.run_post(page)
        self.assertEqual(page.goto.await_count, 3)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "navigation_failed")
        self.assertIn("503", result.message)
        page.close.assert_awaited_once()

    async def test_successful_navigation_does_not_retry(self):
        page = self.make_page([200])
        result = await self.run_post(page)
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(result.error_code, "page_unavailable")

    async def test_429_rate_limit_is_retried_once(self):
        page = self.make_page([429, 200])
        with patch.object(ts.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.run_post(page)
        self.assertEqual(page.goto.await_count, 2)
        self.assertGreaterEqual(sleep.await_count, 1)
        self.assertEqual(result.error_code, "page_unavailable")

    async def test_authentication_error_ends_navigation_without_retry(self):
        page = self.make_page([401])
        result = await self.run_post(page)
        self.assertEqual(result.error_code, "session_expired")
        self.assertEqual(page.goto.await_count, 1)

    async def test_access_denied_ends_navigation_without_retry(self):
        page = self.make_page([403])
        result = await self.run_post(page)
        self.assertEqual(result.error_code, "access_denied")
        self.assertEqual(page.goto.await_count, 1)

    async def test_410_gone_does_not_retry(self):
        page = self.make_page([410])
        result = await self.run_post(page)
        self.assertEqual(result.error_code, "video_unavailable")
        self.assertEqual(page.goto.await_count, 1)

    async def test_unknown_transport_failure_is_bounded(self):
        ctx, page = MagicMock(), MagicMock()
        page.url = "chrome-error://chromewebdata/"
        ctx.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE"))
        page.close = AsyncMock()
        with patch.object(ts, "_has_session", new_callable=AsyncMock, return_value=True), \
             patch.object(ts.asyncio, "sleep", new_callable=AsyncMock):
            result = await ts.post_comment_on_video(ctx, "https://www.tiktok.com/@test/video/1", "test")
        self.assertEqual(result.error_code, "navigation_failed")
        self.assertEqual(page.goto.await_count, ts.VIDEO_NAV_MAX_ATTEMPTS)
        self.assertIn("HTTP ?", result.message)
        page.close.assert_awaited_once()

    async def test_structured_10204_requires_both_code_and_marker(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                async def put(code, message):
                    import json
                    payload = json.dumps({"__DEFAULT_SCOPE__": {
                        "webapp.video-detail": {"statusCode": code, "statusMsg": message}
                    }})
                    await page.set_content(
                        '<main><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                        + payload + '</script></main>')
                await put(10204, "ru_cross_border_block")
                self.assertTrue(await ts._cross_region_block(page))
                await put(10204, "unrelated server response")
                self.assertFalse(await ts._cross_region_block(page))
                detail = await ts._video_detail_status(page)
                self.assertEqual(detail["statusCode"], 10204)
                self.assertEqual(detail["statusMsg"], "unrelated server response")
                await put(10203, "ru_cross_border_block")
                self.assertFalse(await ts._cross_region_block(page))
                await page.set_content('<main>Ошибка в рекомендациях: Видео недоступно</main>')
                self.assertFalse(await ts._cross_region_block(page))
                self.assertIsNone(await ts._find_unavailable_text(page))
            finally:
                await browser.close()

