"""Offline browser smoke test for a hidden element preceding a visible player."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from app.workers.tiktok_search import _has_video_or_comments


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('''
          <div data-e2e="comment-input" style="display:none">hidden</div>
          <div data-e2e="comment-icon" style="width:50px;height:50px">comments</div>
        ''')
        assert await _has_video_or_comments(page)
        await page.locator('[data-e2e="comment-icon"]').evaluate("el => el.style.display = 'none'")
        assert not await _has_video_or_comments(page)
        await browser.close()
    print('visible player detection: OK')


if __name__ == '__main__':
    asyncio.run(main())
