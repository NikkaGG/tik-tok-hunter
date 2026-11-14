"""Read back the submitted test comment without editing or submitting anything."""
import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account, Draft
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def main(guest=False):
    with SessionLocal() as db:
        d = db.get(Draft, 1467)
        account = db.get(Account, d.account_id)
        url, content = d.video.url, d.text
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
    token = sessions.browser_binding.set(binding)
    try:
        async with ts.open_context(headless=False, use_state=not guest) as ctx:
            page = await ctx.new_page()
            comment_pages = []

            async def on_response(response):
                if '/api/comment/list/' not in response.url:
                    return
                try:
                    payload = await response.json()
                    comments = payload.get('comments') or []
                    comment_pages.append({
                        'count': len(comments), 'has_more': payload.get('has_more'),
                        'cursor': payload.get('cursor'),
                        'text_found': any(c.get('text') == content for c in comments),
                        'author_found': any((c.get('user') or {}).get('unique_id', '').casefold() == binding['username'].casefold() for c in comments),
                    })
                except Exception:
                    pass

            page.on('response', on_response)
            await page.goto(url, wait_until='domcontentloaded', timeout=45000, referer=f'{ts.TIKTOK}/')
            await page.wait_for_timeout(8000)
            await ts._raise_if_blocked(page, visible=True)
            await ts._ensure_comments_open(page)
            if guest:
                tab = page.get_by_text('Комментарии', exact=True).first
                if await tab.is_visible():
                    await tab.click()
            await page.wait_for_timeout(5000)
            await ts._raise_if_blocked(page, visible=True)
            matches = page.get_by_text(content, exact=False)
            if guest:
                await page.mouse.move(1200, 650)
                for _ in range(40):
                    if await page.locator(ts._CAPTCHA_SELECTOR).count():
                        print('guest_captcha_detected', flush=True)
                        try:
                            await ts.CaptchaManager(timeout=45, max_attempts=2).handle(page, visible=True)
                            print('guest_captcha_passed', flush=True)
                        except ts.CaptchaError as e:
                            print('guest_captcha_blocked=', str(e), flush=True)
                            break
                    if await matches.count():
                        break
                    await page.mouse.wheel(0, 650)
                    await page.wait_for_timeout(800)
                    if comment_pages and comment_pages[-1]['has_more'] == 0:
                        await page.wait_for_timeout(1000)
                        break
            print('matching_text_count=', await matches.count(), flush=True)
            print('challenge_at_end=', await page.locator(ts._CAPTCHA_SELECTOR).count(), flush=True)
            print('comment_pages=', comment_pages, flush=True)
            for i in range(min(await matches.count(), 3)):
                loc = matches.nth(i)
                print('visible=', await loc.is_visible(), 'context=',
                      (await loc.evaluate("el => el.parentElement?.parentElement?.innerText || ''"))[:500], flush=True)
            await page.screenshot(path='data/test-runs/comment-readback-guest.png' if guest else 'data/test-runs/comment-readback.png')
    finally:
        sessions.browser_binding.reset(token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--guest', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.guest))
