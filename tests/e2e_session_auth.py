"""Real Chromium + real session checks; every network request is intercepted.

Only synthetic credentials. No connections or publications to TikTok.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import session_import, sessions
from app.workers import tiktok_search as ts


async def main():
    run = ROOT / 'data' / 'test-runs' / uuid4().hex
    run.mkdir(parents=True)
    original = ts.open_context
    expired = {'message': 'error', 'data': {'error_code': 13, 'name': 'session_expired'}}
    valid = {'message': 'success', 'data': {'username': 'test_identity'}}
    for scenario in ('fallback', 'expired', 'unknown', 'mismatch'):
        path = run / (scenario + '.json')
        path.write_text(json.dumps({'cookies': session_import.parse(
            'sessionid=' + 'a' * 32 + '; store-idc=test-region; ttwid=test-device'), 'origins': []}))
        before = path.read_bytes()
        calls = []

        async def route_request(route):
            request = route.request
            url = urlparse(request.url)
            assert url.hostname == 'www.tiktok.com'
            if url.path == '/passport/web/account/info/':
                headers = await request.all_headers()
                assert 'sessionid=' + 'a' * 32 in headers.get('cookie', '')
                assert 'store-idc=test-region' in headers.get('cookie', '')
                assert 'ttwid=test-device' in headers.get('cookie', '')
                aid = int(parse_qs(url.query)['aid'][0])
                calls.append(aid)
                if scenario == 'unknown':
                    await route.fulfill(status=503, body='Unavailable')
                else:
                    body = expired if scenario == 'expired' or (scenario == 'fallback' and aid == 1988) else valid
                    await route.fulfill(json=body)
            else:
                await route.fulfill(content_type='text/html', body='<html><body>Test home</body></html>')

        @asynccontextmanager
        async def context():
            async with original() as ctx:
                await ctx.route('**/*', route_request)
                yield ctx

        token = sessions.browser_binding.set({'path': path, 'proxy': None,
            'username': 'wrong_identity' if scenario == 'mismatch' else None})
        try:
            with patch.object(ts, 'open_context', context), \
                 patch.object(ts.app_settings, 'use_system_chrome', return_value=False):
                try:
                    result = await ts.check_session()
                except ts.SessionExpiredError:
                    assert scenario == 'expired'
                except ts.SessionError:
                    assert scenario in ('unknown', 'mismatch')
                else:
                    assert scenario == 'fallback'
                    assert result == (True, 'Сессия активна: @test_identity', 'test_identity')
                assert calls == ([1988] if scenario == 'mismatch' else [1988, 1459])
                if scenario != 'fallback':
                    assert path.read_bytes() == before
        finally:
            sessions.browser_binding.reset(token)
        print(scenario + ': OK')
    print('Real browser auth transport: OK (all network intercepted)')


if __name__ == '__main__':
    asyncio.run(main())
