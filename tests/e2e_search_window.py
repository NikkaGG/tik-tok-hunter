"""Exercise multiple searches in one browser window without TikTok traffic."""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from app.workers import tiktok_search as ts


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        opened, closed, found, errors = [], [], [], []
        ctx.on('page', lambda page: (opened.append(page), page.on('close', lambda: closed.append(page))))

        async def mock_site(route):
            q = parse_qs(urlparse(route.request.url).query).get('q', [''])[0]
            video_id = '123' if q == 'first' else '456'
            await route.fulfill(status=200, content_type='text/html', body=f'<a href="/@author/video/{video_id}">video</a>')

        await ctx.route('**/*', mock_site)

        @asynccontextmanager
        async def use_context(**kwargs):
            yield ctx

        def save(query, videos):
            assert len(opened) == 1 and not opened[0].is_closed()
            found.append((query, videos[0].video_id))

        token = ts.browser_binding.set(None)
        try:
            with patch.object(ts, 'open_context', side_effect=use_context), patch.object(ts.random, 'uniform', return_value=0):
                await ts.search_many(['first', 'second'], 1, save, lambda query, error: errors.append(str(error)), visible=True)
        finally:
            ts.browser_binding.reset(token)
        assert not errors, errors
        assert found == [('first', '123'), ('second', '456')], found
        assert len(opened) == 1 and closed == opened
        await browser.close()
    print('Two searches reused one browser page; page closed after the task')


if __name__ == '__main__':
    asyncio.run(main())
