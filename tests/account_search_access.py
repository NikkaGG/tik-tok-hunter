"""Check whether a profile's own search results can be opened; never comment."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def inspect(account_id):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
    token = sessions.browser_binding.set(binding)
    try:
        async with ts.open_context(headless=False) as ctx:
            page = await ctx.new_page()
            await page.goto(ts.search_url('standoff 2 читы'), wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(8000)
            if await page.locator(ts._CAPTCHA_SELECTOR).count():
                try:
                    await ts.CaptchaManager(timeout=45, max_attempts=2).handle(page, visible=True)
                    await page.wait_for_timeout(4000)
                except ts.CaptchaError as e:
                    print(json.dumps({'account': binding['username'], 'search': 'captcha', 'error': str(e)}, ensure_ascii=False), flush=True)
                    return
            urls = await page.locator('a[href*="/video/"]').evaluate_all(
                'els => [...new Set(els.map(e => e.href))].slice(0,3)')
            results = []
            for url in urls:
                await page.goto(url, wait_until='domcontentloaded', timeout=45000, referer=f'{ts.TIKTOK}/')
                await page.wait_for_timeout(6000)
                if await page.locator(ts._CAPTCHA_SELECTOR).count():
                    results.append({'status': 'captcha'})
                    break
                status = await page.evaluate('''() => {
                    try {
                        const raw = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                        const v = JSON.parse(raw.textContent).__DEFAULT_SCOPE__['webapp.video-detail'];
                        return {code: v?.statusCode, message: v?.statusMsg, item: !!v?.itemInfo?.itemStruct};
                    } catch { return null; }
                }''')
                results.append({'url': url.split('?')[0], 'data': status, 'page_open': await ts._has_video_or_comments(page)})
            print(json.dumps({'account': binding['username'], 'search_links': len(urls), 'results': results}, ensure_ascii=False), flush=True)
    finally:
        sessions.browser_binding.reset(token)


async def main():
    await asyncio.gather(*(inspect(i) for i in (4, 5, 6)))


if __name__ == '__main__':
    asyncio.run(main())
