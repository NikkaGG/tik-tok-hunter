"""Inspect one existing video in an account session; never enter or submit text."""
import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account, Draft, Video
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def main(account_id, draft_id, video_id, output, check_editor):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if draft_id is not None:
            draft = db.get(Draft, draft_id)
            if not draft or draft.account_id != account_id:
                raise RuntimeError('Черновик не найден')
            url = draft.video.url
        else:
            video = db.get(Video, video_id)
            if not video:
                raise RuntimeError('Ролик не найден')
            url = video.url
        if not account:
            raise RuntimeError('Аккаунт не найден')
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
    token = sessions.browser_binding.set(binding)
    try:
        async with ts.open_context(headless=False) as ctx:
            page = await ctx.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=45000, referer=f'{ts.TIKTOK}/')
            await page.wait_for_timeout(7000)
            if check_editor and await ts._has_video_or_comments(page):
                await ts._ensure_comments_open(page)
                editor = await ts._find_comment_editor(page)
                print('editor_found=', editor is not None)
                await page.wait_for_timeout(2000)
            await page.screenshot(path=output)
            print('host=', urlparse(page.url).hostname, 'path_type=', '/video/' in urlparse(page.url).path)
            print('challenge=', await page.locator(ts._CAPTCHA_SELECTOR).count())
            print('video_or_comments=', await ts._has_video_or_comments(page))
            print('unavailable=', await ts._find_unavailable_text(page))
            print('visible_text=', (await page.locator('body').inner_text())[:1000].replace('\n', ' | '))
            print('inputs=', await page.evaluate("""() => Array.from(document.querySelectorAll('video, textarea, [contenteditable], [role=textbox], [data-e2e*=comment]')).filter(e=>e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0,30).map(e=>({tag:e.tagName,role:e.getAttribute('role'),editable:e.getAttribute('contenteditable'),e2e:e.getAttribute('data-e2e'),cls:String(e.className).slice(0,100)}))"""))
    finally:
        sessions.browser_binding.reset(token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', type=int, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--draft-id', type=int)
    source.add_argument('--video-id', type=int)
    parser.add_argument('--output', required=True)
    parser.add_argument('--check-editor', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.account_id, args.draft_id, args.video_id, args.output, args.check_editor))
