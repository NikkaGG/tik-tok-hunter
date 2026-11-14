"""Read-only access comparison for a small set of already saved videos."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account, Video
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def inspect(account_id, video_ids):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
        videos = [(v.id, v.url) for v in (db.get(Video, i) for i in video_ids) if v]
    token = sessions.browser_binding.set(binding)
    results = []
    try:
        async with ts.open_context(headless=False) as ctx:
            page = await ctx.new_page()
            for video_id, url in videos:
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=45000, referer=f'{ts.TIKTOK}/')
                    await page.wait_for_timeout(6000)
                    if await page.locator(ts._CAPTCHA_SELECTOR).count():
                        results.append({'video_id': video_id, 'result': 'captcha'})
                        break
                    data = await page.evaluate('''() => {
                        try {
                            const raw = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                            const v = JSON.parse(raw.textContent).__DEFAULT_SCOPE__['webapp.video-detail'];
                            return {statusCode: v?.statusCode, statusMsg: v?.statusMsg, hasItem: !!v?.itemInfo?.itemStruct};
                        } catch { return null; }
                    }''')
                    results.append({'video_id': video_id, 'data': data,
                                    'page_open': await ts._has_video_or_comments(page)})
                except Exception as e:
                    results.append({'video_id': video_id, 'error': type(e).__name__})
            print(json.dumps({'account': binding['username'], 'results': results}, ensure_ascii=False), flush=True)
    finally:
        sessions.browser_binding.reset(token)


async def main():
    await asyncio.gather(*(inspect(i, [536, 525, 500, 475]) for i in (4, 5, 6)))


if __name__ == '__main__':
    asyncio.run(main())
