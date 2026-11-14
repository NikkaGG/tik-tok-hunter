import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers import tiktok_search as ts


class SearchWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_reuse_one_page_and_close_it_after_search(self):
        context, page = MagicMock(), MagicMock()
        context.new_page = AsyncMock(return_value=page)
        page.close = AsyncMock()
        seen = []

        @asynccontextmanager
        async def open_test_context(**kwargs):
            yield context

        async def search(_ctx, query, _limit, _visible, **kwargs):
            self.assertIs(kwargs['page'], page)
            self.assertEqual(page.close.await_count, 0)
            return [query]

        token = ts.browser_binding.set(None)
        try:
            with patch.object(ts, 'open_context', side_effect=open_test_context), \
                 patch.object(ts, 'search_videos', side_effect=search), \
                 patch.object(ts.asyncio, 'sleep', new_callable=AsyncMock):
                await ts.search_many(['first', 'second'], 1,
                                     lambda query, videos: seen.append((query, videos)),
                                     lambda query, error: self.fail(str(error)), visible=True)
        finally:
            ts.browser_binding.reset(token)
        self.assertEqual(seen, [('first', ['first']), ('second', ['second'])])
        context.new_page.assert_awaited_once()
        page.close.assert_awaited_once()

    async def test_borrowed_page_stays_open_and_listener_is_removed(self):
        context, page = MagicMock(), MagicMock()
        context.new_page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        video = ts.FoundVideo(video_id='123', url='https://example.test/123', author='example')

        async def collect(_page, destination):
            destination[video.video_id] = video

        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock), \
             patch.object(ts, '_recover_error_page', new_callable=AsyncMock), \
             patch.object(ts, '_collect_dom', side_effect=collect), \
             patch.object(ts, '_load_more', new_callable=AsyncMock):
            self.assertEqual(await ts.search_videos(context, 'first', 1, page=page), [video])
        context.new_page.assert_not_awaited()
        page.close.assert_not_awaited()
        page.remove_listener.assert_called_once()
