"""Exercise the real auth decision path, mocking only browser transport."""
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import session_import, sessions
from app.workers import tiktok_search as ts


EXPIRED = {'message': 'error', 'data': {'name': 'session_expired', 'error_code': 13}}
VALID = {'message': 'success', 'data': {'username': 'verified_user'}}


class SessionAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_context_saves_state_after_failure_without_relaunch(self):
        with patch.object(ts.app_settings, 'use_system_chrome', return_value=False):
            path, temporary = MagicMock(), MagicMock()
            path.exists.return_value = True
            path.with_suffix.return_value = temporary
            ctx = MagicMock()
            ctx.add_init_script = AsyncMock()
            ctx.storage_state = AsyncMock()
            browser = MagicMock()
            browser.new_context = AsyncMock(return_value=ctx)
            browser.close = AsyncMock()
            pw = MagicMock()
            pw.chromium.launch = AsyncMock(return_value=browser)
            @asynccontextmanager
            async def playwright():
                yield pw
            token = sessions.browser_binding.set({'path': path, 'username': 'verified', 'proxy': None})
            try:
                with patch.object(ts, 'async_playwright', playwright):
                    with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                        async with ts.open_context():
                            ts.session_verified.set(True)
                            raise RuntimeError('interrupted')
                temporary.replace.assert_called_once_with(path)
                browser.close.assert_awaited_once()
                pw.chromium.launch.assert_awaited_once()
            finally:
                sessions.browser_binding.reset(token)

    def context(self, responses):
        page = MagicMock()
        page.url = ts.TIKTOK + '/'
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(side_effect=responses)
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        ctx = MagicMock()
        ctx.pages = [page]
        ctx.new_page = AsyncMock(return_value=page)
        ctx.cookies = AsyncMock(return_value=[{'name': 'sessionid', 'value': 'test-only'}])
        ctx.storage_state = AsyncMock()
        return ctx, page

    async def test_refusal_from_one_app_is_not_expiry(self):
        ctx, page = self.context([EXPIRED, VALID])
        self.assertEqual(await ts._account_info(ctx), VALID)
        self.assertEqual([call.args[1] for call in page.evaluate.await_args_list], [1988, 1459])
        ctx.request.get.assert_not_called()

    async def test_web_app_success_needs_no_fallback(self):
        ctx, page = self.context([VALID])
        self.assertEqual(await ts._account_info(ctx), VALID)
        self.assertEqual(page.evaluate.await_count, 1)

    async def test_both_explicit_refusals(self):
        ctx, _ = self.context([EXPIRED, EXPIRED])
        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock):
            with self.assertRaises(ts.SessionExpiredError) as error:
                await ts._account_info(ctx)
        self.assertIn('все cookies', str(error.exception))

    async def test_network_failure_after_one_refusal_is_unknown(self):
        ctx, _ = self.context([EXPIRED, RuntimeError('Network')])
        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock):
            self.assertIsNone(await ts._account_info(ctx))

    async def test_public_profile_or_error_username_is_not_identity(self):
        for response in ({'userInfo': {'user': {'uniqueId': 'public_author'}}},
                         {'message': 'error', 'data': {'username': 'untrusted'}},
                         {'message': 'success', 'data': None}):
            ctx, _ = self.context([response, response])
            with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock):
                self.assertIsNone(await ts._account_info(ctx))

    async def test_captcha_is_not_expired(self):
        ctx, _ = self.context([None, None])
        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock,
                          side_effect=ts.CaptchaError('captcha')):
            with self.assertRaises(ts.CaptchaError):
                await ts._account_info(ctx)

    async def test_temporary_page_is_closed_on_error(self):
        ctx, page = self.context([EXPIRED, EXPIRED])
        ctx.pages = []
        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock):
            with self.assertRaises(ts.SessionExpiredError):
                await ts._account_info(ctx)
        page.goto.assert_awaited_once()
        page.close.assert_awaited_once()

    async def test_check_saves_only_confirmed_matching_identity(self):
        for responses, expected, error in (([EXPIRED, VALID], 'verified_user', None),
                                           ([VALID], 'other_user', ts.SessionError),
                                           ([None, None], 'verified_user', ts.SessionError)):
            ctx, _ = self.context(responses)
            @asynccontextmanager
            async def context():
                yield ctx
            token = sessions.browser_binding.set({'path': Path(__file__), 'username': expected})
            try:
                with patch.object(ts, 'open_context', context), \
                     patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock):
                    if error:
                        with self.assertRaises(error):
                            await ts.check_session()
                        ctx.storage_state.assert_not_awaited()
                    else:
                        self.assertEqual((await ts.check_session())[2], 'verified_user')
                        ctx.storage_state.assert_awaited_once()
            finally:
                sessions.browser_binding.reset(token)


class SessionImportTests(unittest.TestCase):
    def test_bare_cookie_attributes(self):
        cookies = {c['name']: c for c in session_import.parse('a' * 32)}
        self.assertTrue(cookies['sessionid']['httpOnly'])
        self.assertEqual(cookies['sessionid_ss']['sameSite'], 'None')
        self.assertTrue(cookies['sessionid_ss']['secure'])

    def test_full_header_retains_region_and_device(self):
        cookies = {c['name']: c for c in session_import.parse(
            'Cookie: sessionid=' + 'b' * 32 + '; store-idc=test-region; ttwid=test-device')}
        self.assertEqual(cookies['store-idc']['value'], 'test-region')
        self.assertEqual(cookies['ttwid']['value'], 'test-device')

    def test_full_export_preserves_attributes(self):
        import json
        original = {'name': 'sessionid', 'value': 'c' * 32, 'domain': '.tiktok.com',
                    'httpOnly': True, 'secure': True, 'sameSite': 'Strict', 'expires': 2000000000}
        cookie = session_import.parse(json.dumps([original]))[0]
        for key, value in original.items():
            self.assertEqual(cookie[key], value)

    def test_foreign_domain_is_not_tiktok(self):
        import json
        with self.assertRaises(ValueError):
            session_import.parse(json.dumps([{'name': 'sessionid', 'value': 'x',
                                             'domain': 'tiktok.com.example.test'}]))
