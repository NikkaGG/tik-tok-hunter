"""Read-only live diagnostic: open one search and capture its visible state."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account
from app.services import sessions, settings
from app.workers import tiktok_search as ts
from app.workers.captcha_manager import CaptchaManager, CaptchaError


async def main(account_id, query, output, solve=False, homepage=False):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            raise RuntimeError('Аккаунт не найден')
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
    if not binding['path'].exists():
        raise RuntimeError('Сессия отсутствует')
    token = sessions.browser_binding.set(binding)
    try:
        async with ts.open_context(headless=False) as ctx:
            page = await ctx.new_page()
            await page.goto(ts.TIKTOK if homepage else ts.search_url(query), wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(7000)
            await page.screenshot(path=output)
            print('visible_challenges=', await page.locator(ts._CAPTCHA_SELECTOR).count())
            print('video_links=', await page.locator('a[href*="/video/"]').count())
            for frame in page.frames:
                print('frame=', frame.name, 'url_host=', __import__('urllib.parse', fromlist=['urlparse']).urlparse(frame.url).hostname)
                print('elements=', await frame.evaluate("""() => Array.from(document.querySelectorAll('img, canvas, [role=slider], [class*=captcha], [id*=captcha]')).filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0, 35).map(e => ({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,160),rect:[Math.round(e.getBoundingClientRect().width),Math.round(e.getBoundingClientRect().height)]}))"""))
                print('challenge_structure=', await frame.evaluate("""() => { const root=document.querySelector('#captcha-verify-container-main-page'); if(!root)return null; return Array.from(root.querySelectorAll('*')).filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).map(e=>({tag:e.tagName,id:e.id,cls:String(e.className).slice(0,160),rect:[Math.round(e.getBoundingClientRect().width),Math.round(e.getBoundingClientRect().height)]})); }"""))
            if solve:
                async def response_info(response):
                    from urllib.parse import urlparse
                    parsed = urlparse(response.url)
                    if 'captcha' not in parsed.path.lower():
                        return
                    try:
                        data = await response.json()
                        safe = {'type': type(data).__name__, 'keys': list(data.keys()) if isinstance(data, dict) else []}
                        if isinstance(data, dict):
                            safe.update({key: data.get(key) for key in ('code', 'success', 'message', 'msg', 'status', 'status_code') if key in data})
                            if isinstance(data.get('data'), dict):
                                safe['data_keys'] = list(data['data'].keys())
                                safe['data_result'] = {key: data['data'].get(key) for key in ('code', 'success', 'message', 'msg', 'status', 'status_code') if key in data['data']}
                    except Exception as exc:
                        safe = {'error': str(exc)[:250]}
                    print('captcha_response=', parsed.hostname, parsed.path, response.status, safe, flush=True)
                page.on('response', response_info)
                try:
                    result = await CaptchaManager(timeout=12, callback=lambda event: print('event=', event, flush=True)).handle(page, visible=True)
                    print('handler_result=', result, flush=True)
                except CaptchaError as exc:
                    print('handler_error=', str(exc), flush=True)
                await page.screenshot(path=output.replace('.png', '-after.png'))
                print('after_video_links=', await page.locator('a[href*="/video/"]').count(), flush=True)
    finally:
        sessions.browser_binding.reset(token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', type=int, required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--solve', action='store_true')
    parser.add_argument('--homepage', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.account_id, args.query, args.output, args.solve, args.homepage))
