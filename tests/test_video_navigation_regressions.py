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
                listeners["response"](response)
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
