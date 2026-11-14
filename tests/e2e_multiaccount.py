"""Browser → API → SQLite smoke test. All TikTok operations are fakes."""
import os
import asyncio
import json
import sys
import uuid
import time
import threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN = ROOT / 'data' / 'test-runs' / uuid.uuid4().hex
RUN.mkdir(parents=True)
os.environ['TTLH_DATA_DIR'] = str(RUN)
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, AsyncMock
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app.main import app
from app.database import ensure_schema, SessionLocal
from app.models import Video, Account
from datetime import datetime
from app.services import sessions, tasks
from app.workers import tiktok_search as ts

session_health = {}
health_calls = []

async def fake_check():
    binding = sessions.browser_binding.get()
    if binding['username']:
        health_calls.append(binding['username'])
        await asyncio.sleep(.6)
        health = session_health.get(binding['username'], 'active')
        if health == 'expired': return False, 'Test: session expired', None
        if health == 'network': raise RuntimeError('Test: network unavailable')
        return True, 'Test session verified', binding['username']
    state = json.loads(binding['path'].read_text())
    sid = next(c['value'] for c in state['cookies'] if c['name'] == 'sessionid')
    if sid.startswith('c'):
        return False, 'Test: invalid session', None
    return True, 'Test session verified', {'a': 'smoke_one', 'b': 'smoke_two', 'd': 'smoke_three'}[sid[0]]

async def fake_info(ctx):
    return {'data': {'username': sessions.browser_binding.get()['username']}}

@asynccontextmanager
async def fake_context(**kw):
    ctx = MagicMock()
    ctx.storage_state = AsyncMock()
    yield ctx

async def fake_post(ctx, video_url, comment_text, visible=False):
    return ts.CommentResult(True, 'Test publication confirmed')

async def fake_login():
    sessions.browser_binding.get()['path'].write_text(json.dumps({'cookies': [{'name': 'sessionid', 'value': 'd' * 32}], 'origins': []}))
    return 'Session saved'

ts.login_interactive = fake_login
ts.check_session = fake_check
ts._account_info = fake_info
ts.open_context = fake_context
ts.post_comment_on_video = fake_post
ensure_schema()
with SessionLocal() as db:
    db.add(Video(video_id='smoke-video', url='https://example.test/video/1', author='writer', query='#test', status='relevant'))
    db.commit()
config = uvicorn.Config(app, host='127.0.0.1', port=8765, log_level='warning')
server = uvicorn.Server(config)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
for _ in range(100):
    if server.started: break
    time.sleep(.05)
assert server.started
try:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception:
            browser = pw.chromium.launch(headless=True, channel='chrome')
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        failures = []
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.on('response', lambda r: failures.append(f'{r.status}: {r.url}') if '/api/' in r.url and r.status >= 400 else None)
        page.goto('http://127.0.0.1:8765', wait_until='networkidle')
        # The first account is added from an empty app; no account selection exists in the dialog.
        page.locator('#btn-login').click()
        expect(page.locator('#account-auth')).to_be_visible()
        assert page.locator('#account-auth select').count() == 0
        assert page.locator('#set-session-card').count() == 0
        page.screenshot(path=str(RUN / 'add-account.png'), full_page=True)
        page.locator('#account-auth-submit').click()
        expect(page.locator('#account-auth-error')).to_have_text('Вставьте sessionid или экспорт cookies')
        page.locator('#account-auth-cookies').fill('c' * 32)
        page.locator('#account-auth-submit').click()
        expect(page.locator('#account-auth-error')).to_contain_text('Test: invalid session')
        assert page.request.get('http://127.0.0.1:8765/api/accounts').json() == []
        for index, prefix in enumerate(('a', 'b')):
            if index:
                page.locator('#btn-acc-add').click()
            page.locator('#account-auth-cookies').fill(prefix * 32)
            page.locator('#account-auth-submit').click()
            expect(page.locator('#account-auth-success')).to_be_visible()
            page.locator('#account-auth-done').click()
            expect(page.locator('#account-auth')).to_be_hidden()
        accounts = page.request.get('http://127.0.0.1:8765/api/accounts').json()
        assert len(accounts) == 2
        page.locator('[data-tab=templates]').click()
        page.locator('#mx-set-name').fill('Coffee topic')
        page.locator('#mx-variants textarea').fill('First useful comment [author]')
        page.locator('#mx-add-variant').click()
        page.locator('#mx-variants textarea').nth(1).fill('Second useful comment')
        page.locator('#mx-set-form button:not([type])').click()
        expect(page.locator('#mx-set-list')).to_contain_text('Coffee topic')
        page.locator('[data-tab=accounts]').click()
        assert page.locator('#mx-account').count() == 0
        # Session health is checked directly beside the account, with results polled locally.
        first_id = accounts[0]['id']
        first_row = page.locator(f'#accounts-body tr[data-id="{first_id}"]')
        session_health['smoke_one'] = 'expired'
        first_row.locator('[data-acc-check-session]').click()
        expect(first_row.locator('[data-acc-check-session]')).to_be_disabled()
        expect(first_row).to_contain_text('Сессия истекла')
        expect(page.locator('#side-session')).to_contain_text('Сессия истекла')
        session_health['smoke_one'] = 'network'
        first_row.locator('[data-acc-check-session]').click()
        expect(first_row).to_contain_text('Ошибка проверки')
        expect(first_row).to_contain_text('Test: network unavailable')
        expect(page.locator('#side-session')).to_contain_text('Ошибка проверки')
        session_health['smoke_one'] = 'active'
        first_row.locator('[data-acc-check-session]').click()
        expect(first_row.locator('.acc-session-badge')).to_have_text('Активна')
        expect(first_row.locator('.acc-session-time')).to_contain_text('сегодня')
        # Editing an account must preserve inheritance of a global proxy.
        proxy_response = page.request.put('http://127.0.0.1:8765/api/settings/proxy', data={'proxy':'127.0.0.1:9999','scheme':'http'})
        assert proxy_response.ok
        page.locator('#btn-acc-refresh').click()
        expect(page.locator('#toast')).to_have_text('Список аккаунтов обновлён')
        for account in accounts:
            page.locator(f'#accounts-body [data-acc-edit="{account["id"]}"]').click()
            expect(page.locator('#account-modal-form [name=proxy]')).to_have_value('')
            page.locator('#account-modal-form [name=daily_reply_limit]').fill('35')
            page.locator('#account-editor-tab-sets').click()
            page.locator('#account-editor-set-list input').check()
            page.locator('#account-modal-form button[type=submit]').click()
            expect(page.locator('#modal-account')).to_be_hidden()
        saved_accounts = page.request.get('http://127.0.0.1:8765/api/accounts').json()
        assert all(a['proxy'] is None and a['proxy_label'] is None and a['daily_reply_limit'] == 35 for a in saved_accounts)
        page.request.put('http://127.0.0.1:8765/api/settings/proxy', data={'proxy':'','scheme':'http'})
        expect(first_row).to_contain_text('Coffee topic')
        page.screenshot(path=str(RUN / 'accounts.png'), full_page=True)
        page.locator(f'#accounts-body [data-acc-edit="{first_id}"]').click()
        page.screenshot(path=str(RUN / 'account-general.png'), full_page=True)
        page.locator('#account-editor-tab-session').click()
        expect(page.locator('#account-editor-session-status')).to_contain_text('Активна')
        expect(page.locator('#account-editor-cookies')).to_be_hidden()
        page.screenshot(path=str(RUN / 'account-settings.png'), full_page=True)
        page.locator('#btn-close-acc-modal').click()
        # Filters and selection survive status refreshes.
        first_row.locator('[data-acc-check]').check()
        expect(page.locator('#acc-bulk-bar')).to_be_visible()
        previous_health_calls = len(health_calls)
        page.wait_for_timeout(2200)
        assert len(health_calls) == previous_health_calls, 'Local refresh must not contact TikTok'
        expect(first_row.locator('[data-acc-check]')).to_be_checked()
        page.locator('#acc-clear-selection').click()
        expect(page.locator('#acc-bulk-bar')).to_be_hidden()
        page.set_viewport_size({'width': 420, 'height': 900})
        page.screenshot(path=str(RUN / 'accounts-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Horizontal page overflow'
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('[data-tab=videos]').click()
        # Reproduce a large scope: 83 videos, only two available variants.
        with SessionLocal() as db:
            db.add_all([Video(video_id=f'prepare-{i}', url=f'https://example.test/video/{i + 2}',
                author=f'writer_{i}', query='#test', status='relevant') for i in range(82)])
            db.commit()
        # Missing set assignment is explained before generation, and fixed inline.
        second_id = accounts[1]['id']
        set_id = page.request.get('http://127.0.0.1:8765/api/text-sets').json()[0]['id']
        page.request.put(f'http://127.0.0.1:8765/api/text-sets/accounts/{second_id}', data={'text_set_ids': []})
        page.locator('#btn-generate').click()
        expect(page.locator('#v-template')).to_be_hidden()
        expect(page.locator('#mx-prepare-submit')).to_be_disabled()
        page.locator('#mx-prepare-sets input').check()
        page.locator(f'#prepare-account-{second_id}').check()
        expect(page.locator('#prepare-estimate')).to_contain_text('не назначены')
        expect(page.locator('#mx-prepare-submit')).to_be_disabled()
        assert page.request.get('http://127.0.0.1:8765/api/drafts?status=draft').json() == []
        page.screenshot(path=str(RUN / 'prepare-missing-assignment.png'), full_page=True)
        page.locator(f'[data-prepare-assign="{second_id}"]').click()
        expect(page.locator('#mx-prepare-submit')).to_be_enabled()
        assert set_id in next(a for a in page.request.get('http://127.0.0.1:8765/api/accounts').json() if a['id'] == second_id)['text_set_ids']
        page.set_viewport_size({'width':420, 'height':900})
        page.screenshot(path=str(RUN / 'prepare-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        expect(page.locator('#mx-prepare-submit')).to_be_in_viewport()
        page.set_viewport_size({'width':1440, 'height':1000})
        page.locator('#mx-prepare-accounts input').nth(0).check()
        page.locator('#mx-prepare-accounts input').nth(1).check()
        page.locator('#mx-prepare-sets input').check()
        expect(page.locator('#prepare-estimate')).to_contain_text('Роликов: 83. Аккаунтов: 2.')
        expect(page.locator('#prepare-estimate')).to_contain_text('вариантов не хватает')
        expect(page.locator('#mx-prepare-submit')).to_be_enabled()
        page.screenshot(path=str(RUN / 'prepare-ready.png'), full_page=True)
        page.locator('#mx-prepare-submit').click()
        expect(page.locator('#mx-prepare-result')).to_contain_text('Создано: 2')
        expect(page.locator('#mx-prepare-result')).to_contain_text('Пропущено: 164')
        expect(page.locator('#mx-prepare-result .prepare-note.warning')).to_have_count(2)
        expect(page.locator('#mx-prepare-result')).not_to_contain_text('аккаунт #')
        drafts = page.request.get('http://127.0.0.1:8765/api/drafts?status=draft').json()
        assert len(drafts) == 2 and drafts[0]['text'] != drafts[1]['text']
        page.screenshot(path=str(RUN / 'prepared.png'), full_page=True)
        page.locator('#mx-open-queue').click()
        expect(page.locator('#drafts-list [data-id]')).to_have_count(2)
        page.locator('#btn-open-ac-modal').click()
        expect(page.locator('#ac-drafts-count')).to_have_text('2')
        page.locator('#ac-submit-btn').click()
        expect(page.locator('#modal-autocomment')).to_be_hidden()
        for _ in range(100):
            result = page.request.get('http://127.0.0.1:8765/api/drafts?status=sent').json()
            if len(result) == 2: break
            page.wait_for_timeout(100)
        assert len(result) == 2, result
        page.locator('#d-status').select_option('sent')
        expect(page.locator('#drafts-list [data-id]')).to_have_count(2)
        page.screenshot(path=str(RUN / 'sent.png'), full_page=True)
        # Browser login is the second method of the same focused add dialog.
        page.locator('[data-tab=accounts]').click()
        page.locator('#btn-acc-add').click()
        page.locator('#account-auth-browser-tab').click()
        expect(page.locator('#account-auth-cookies')).to_be_hidden()
        page.locator('#account-auth-submit').click()
        expect(page.locator('#account-auth-success-text')).to_contain_text('@smoke_three')
        page.locator('#account-auth-done').click()
        assert len(page.request.get('http://127.0.0.1:8765/api/accounts').json()) == 3
        # The sidebar monitors all accounts, independently of the account used for search.
        expect(page.locator('#side-session [data-monitor-account]')).to_have_count(3)
        page.locator(f'#side-session [data-monitor-account="{first_id}"]').click()
        expect(page.locator('#account-editor-session')).to_be_visible()
        expect(page.locator('#acc-modal-title')).to_contain_text('@smoke_one')
        page.locator('#btn-close-acc-modal').click()
        page.locator('#side-session [data-monitor-settings]').click()
        expect(page.locator('#monitor-interval')).to_have_value('15')
        page.locator('#monitor-enabled').uncheck()
        page.locator('#monitor-settings button[type=submit], #monitor-settings button:not([type])').click()
        expect(page.locator('#side-session')).to_contain_text('Автопроверка выключена')
        assert page.request.get('http://127.0.0.1:8765/api/settings').json()['monitor_interval_minutes'] == 0
        page.locator('#monitor-enabled').check()
        page.locator('#monitor-interval').fill('30')
        page.locator('#monitor-settings button[type=submit], #monitor-settings button:not([type])').click()
        expect(page.locator('#side-session')).to_contain_text('каждые 30 мин')
        page.locator('#monitor-interval').fill('15')
        page.locator('#monitor-settings button[type=submit], #monitor-settings button:not([type])').click()
        expect(page.locator('#side-session')).to_contain_text('каждые 15 мин')
        with SessionLocal() as db:
            for name, status, paused in [('attention_account','error',False),('expired_account','expired',False),
                                         ('paused_account','active',True),('unchecked_account','unchecked',False),
                                         ('disconnected_account','missing',False)]:
                account = Account(username=name, session_status=status, session_checked_at=datetime.now(),
                                  status='paused' if paused else 'active', session_error='Test network timeout' if status == 'error' else None)
                db.add(account)
                db.flush()
                if status != 'missing': sessions.state_path(account.id).write_text('{"cookies":[],"origins":[]}')
            db.commit()
        # Refreshing local data does not run a TikTok check.
        page.locator('[data-tab=accounts]').click()
        page.locator('#btn-acc-refresh').click()
        expect(page.locator('#side-session [data-monitor-account]')).to_have_count(5)
        page.locator('#side-session [data-monitor-expand]').click()
        expect(page.locator('#side-session [data-monitor-account]')).to_have_count(8)
        tones = page.locator('#side-session [data-monitor-account]').evaluate_all('(nodes) => nodes.map(n => n.dataset.tone)')
        assert tones == ['red','red','yellow','yellow','green','green','green','green'], tones
        assert page.locator('.monitor-list').evaluate('(el) => el.scrollHeight > el.clientHeight')
        page.screenshot(path=str(RUN / 'account-monitor.png'), full_page=True)
        page.locator('#side-session [data-monitor-expand]').click()
        expect(page.locator('#side-session [data-monitor-account]')).to_have_count(5)
        assert page.request.get('http://127.0.0.1:8765/api/settings').json()['search_account_id'] == first_id
        page.set_viewport_size({'width':420,'height':900})
        expect(page.locator('#hdr-session')).to_have_text('TikTok · 4/8')
        page.locator('#hdr-session').click()
        expect(page.locator('#page-title')).to_have_text('Аккаунты TikTok')
        page.screenshot(path=str(RUN / 'monitor-mobile.png'), full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        # Exercise every new page and the old overview against the migrated API.
        for tab in ('settings', 'search', 'accounts', 'dashboard', 'templates'):
            page.locator(f'[data-tab={tab}]').click()
            page.wait_for_timeout(100)
        assert not errors, errors
        assert not failures, failures
        print('PASS: account flows + sidebar monitor colors/order/expansion/navigation + interval settings + mobile summary')
        print('Artifacts:', RUN)
        browser.close()
except Exception:
    if 'page' in locals():
        try:
            page.screenshot(path=str(RUN / 'failure.png'), full_page=True)
            print('Browser errors:', errors)
            print('Artifacts:', RUN)
        except Exception:
            pass
    raise
finally:
    server.should_exit = True
    thread.join(10)
