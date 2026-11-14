"""Compare page evidence without saving cookies, request headers, or sending comments."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account, Video
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def run(account_id, video_id, check_app):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        video = db.get(Video, video_id)
        binding = {"path": sessions.state_path(account.id), "username": account.username,
                   "proxy": account.proxy or settings.search_proxy()}
        url = video.url
    token = sessions.browser_binding.set(binding)
    try:
        async with ts.open_context(headless=False) as ctx:
            page = await ctx.new_page()
            responses = []

            async def capture(resp):
                if '/api/item/detail/' not in resp.url:
                    return
                data = None
                try:
                    body = await resp.json()
                    data = {k: body.get(k) for k in ('statusCode', 'status_code', 'statusMsg', 'status_msg') if k in body}
                except Exception:
                    pass
                responses.append({'endpoint': '/api/item/detail/', 'http_status': resp.status, 'status': data})

            page.on('response', capture)
            response = await page.goto(url, wait_until='domcontentloaded', timeout=45000, referer=f'{ts.TIKTOK}/')
            await page.wait_for_timeout(12000)
            embedded = await page.evaluate('''() => {
                const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                if (!el) return null;
                try {
                    const scope = JSON.parse(el.textContent).__DEFAULT_SCOPE__ || {};
                    return Object.fromEntries(Object.entries(scope).filter(([k]) => k.includes('video-detail')).map(([k,v]) => [k, {
                        statusCode: v.statusCode, statusMsg: v.statusMsg, hasItem: !!v.itemInfo?.itemStruct
                    }]));
                } catch { return null; }
            }''')
            cookies = await ctx.cookies(ts.TIKTOK)
            names = sorted(c['name'] for c in cookies)
            country = {c['name']: c['value'] for c in cookies if c['name'] in ('store-country-code', 'store-country-code-src')}
            print(json.dumps({'account': binding['username'], 'host': urlparse(page.url).hostname,
                'http_status': response.status if response else None,
                'challenge': await page.locator(ts._CAPTCHA_SELECTOR).count(),
                'video_or_comments': await ts._has_video_or_comments(page),
                'unavailable': await ts._find_unavailable_text(page), 'embedded': embedded,
                'responses': responses, 'cookie_names': names, 'country_cookies': country}, ensure_ascii=False))
            if check_app:
                available, message = await ts._check_video_availability(page)
                print('app_availability=', available, 'message=', message, flush=True)
    finally:
        sessions.browser_binding.reset(token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', type=int, required=True)
    parser.add_argument('--video-id', type=int, required=True)
    parser.add_argument('--check-app', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.account_id, args.video_id, args.check_app))
