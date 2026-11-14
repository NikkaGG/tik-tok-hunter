"""Run the real input path, but deliberately stop before the publish button."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models import Account, Draft
from app.services import sessions, settings
from app.workers import tiktok_search as ts


async def main():
    with SessionLocal() as db:
        draft = db.get(Draft, 1467)
        account = db.get(Account, draft.account_id)
        binding = {'path': sessions.state_path(account.id), 'username': account.username,
                   'proxy': account.proxy or settings.search_proxy()}
        url, content = draft.video.url, draft.text
    token = sessions.browser_binding.set(binding)
    original = ts._find_post_button

    async def inspect(page):
        button = await original(page)
        result = await page.evaluate('''() => ({
          editables: Array.from(document.querySelectorAll('[contenteditable=true], textarea')).map(e => ({
            text: e.value ?? e.innerText, role: e.getAttribute('role'), visible: !!(e.offsetWidth || e.offsetHeight)
          })),
          active: {tag: document.activeElement?.tagName, role: document.activeElement?.getAttribute('role'),
                   editable: document.activeElement?.getAttribute('contenteditable')},
          post: Array.from(document.querySelectorAll('[data-e2e="comment-post"]')).map(e => ({
            tag: e.tagName, disabled: e.disabled, ariaDisabled: e.getAttribute('aria-disabled'),
            html: e.outerHTML.slice(0, 800)
          }))
        })''')
        print('inspection=', json.dumps(result, ensure_ascii=False), flush=True)
        print('button_found=', button is not None, flush=True)
        await page.screenshot(path='data/test-runs/comment-before-post.png')
        return None  # Never submit during inspection.

    ts._find_post_button = inspect
    try:
        async with ts.open_context(headless=False) as ctx:
            ts.comment_visible.set(True)
            await sessions.verify_context(ctx, binding['username'])
            result = await ts.post_comment_on_video(ctx, url, content, visible=True)
            print('result=', result, flush=True)
    finally:
        ts._find_post_button = original
        sessions.browser_binding.reset(token)


if __name__ == '__main__':
    asyncio.run(main())
