import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers.captcha_manager import CaptchaError, CaptchaManager as RealCaptchaManager, CaptchaState, SELECTOR, SUCCESS_SELECTOR, SEARCH_RESULTS_SELECTOR, verified_response
from app.workers import tiktok_search as ts


class CaptchaManager(RealCaptchaManager):
    def __init__(self, **kwargs):
        kwargs.setdefault('readiness_timeout', 0)
        super().__init__(**kwargs)


class CaptchaManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('app.workers.captcha_rotation.try_rotation', new_callable=AsyncMock, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch('app.workers.captcha_slide.try_slide', new_callable=AsyncMock, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, counts, success=False):
        page = MagicMock()
        challenge = MagicMock()
        challenge.count = AsyncMock(side_effect=counts)
        success_marker = MagicMock()
        success_marker.count = AsyncMock(return_value=int(success))
        other = MagicMock()
        other.count = AsyncMock(return_value=0)
        page.locator.side_effect = lambda selector: challenge if selector == SELECTOR else success_marker if selector == SUCCESS_SELECTOR else other
        page.is_closed.return_value = False
        page.url = 'https://www.tiktok.com/'
        return page

    async def test_no_challenge_does_not_install_listener(self):
        page = self.page([0])
        self.assertFalse(await CaptchaManager().handle(page, visible=True))
        page.on.assert_not_called()

    async def test_manual_success_emits_all_states_and_does_not_touch_mouse(self):
        page = self.page([1, 1, 1, 0, 0], success=True)
        events = []
        with patch('app.workers.captcha_manager.asyncio.sleep', new_callable=AsyncMock):
            self.assertTrue(await CaptchaManager(callback=events.append).handle(page, visible=True))
        self.assertEqual([e.state for e in events], [CaptchaState.DETECTED, CaptchaState.CLASSIFIED,
            CaptchaState.SOLVING, CaptchaState.AWAITING_USER, CaptchaState.VERIFYING, CaptchaState.SOLVED, CaptchaState.RESUMED])
        page.mouse.down.assert_not_called()
        page.remove_listener.assert_called_once()

    async def test_dismissed_without_success_is_not_solved(self):
        page = self.page([1, 1, 0, 0])
        events = []
        with self.assertRaisesRegex(CaptchaError, 'успех не подтверждён'):
            await CaptchaManager(callback=events.append, confirmation_timeout=0).handle(page, visible=True)
        self.assertEqual(events[-1].state, CaptchaState.FAILED)
        self.assertNotIn(CaptchaState.SOLVED, [e.state for e in events])

    async def test_verified_tiktok_response_allows_resume(self):
        page = self.page([1, 1, 1, 0])
        events = []
        response = MagicMock()
        response.url = 'https://www.tiktok.com/captcha/verify'
        response.ok = True
        response.json = AsyncMock(return_value={'success': True})
        async def verify_while_waiting(_):
            await page.on.call_args.args[1](response)
        with patch('app.workers.captcha_manager.asyncio.sleep', side_effect=verify_while_waiting):
            self.assertTrue(await CaptchaManager(callback=events.append).handle(page, visible=True))
        self.assertEqual(events[-1].state, CaptchaState.RESUMED)

    async def test_encrypted_response_requires_new_search_results(self):
        scenarios = (({'edata': 'encrypted'}, 1, True),
                     ({'edata': 'encrypted'}, 0, False),
                     ({'edata': 'encrypted', 'success': False}, 1, False),
                     ({'edata': 'encrypted', 'code': 1}, 1, False),
                     ({'success': False}, 1, False),
                     ({'unexpected': 'value'}, 1, False))
        for payload, results_after, expected in scenarios:
            page = self.page([1, 0, 0, 0, 0])
            page.url = 'https://www.tiktok.com/search/video?q=test'
            old_locator = page.locator.side_effect
            results = MagicMock()
            results.count = AsyncMock(side_effect=[0, results_after])
            page.locator.side_effect = lambda selector: results if selector == SEARCH_RESULTS_SELECTOR else old_locator(selector)
            response = MagicMock()
            response.url = 'https://verification-sg.tiktok.com/captcha/verify'
            response.ok = True
            response.json = AsyncMock(return_value=payload)
            async def attempt(_page):
                await page.on.call_args.args[1](response)
                return True
            with patch('app.workers.captcha_rotation.try_rotation', side_effect=attempt):
                manager = CaptchaManager(confirmation_timeout=0)
                if expected:
                    self.assertTrue(await manager.handle(page, visible=True))
                else:
                    with self.assertRaises(CaptchaError):
                        await manager.handle(page, visible=True)

    async def test_homepage_encrypted_reply_needs_successful_readonly_probe(self):
        for probe_result in (True, False):
            page = self.page([1, 0, 0, 0, 0, 0])
            page.url = 'https://www.tiktok.com/'
            response = MagicMock()
            response.url = 'https://verification-sg.tiktok.com/captcha/verify'
            response.ok = True
            response.json = AsyncMock(return_value={'edata': 'encrypted'})

            async def attempt(_page):
                await page.on.call_args.args[1](response)
                return True

            with patch('app.workers.captcha_rotation.try_rotation', side_effect=attempt), \
                 patch('app.workers.captcha_manager.confirm_search_access', new_callable=AsyncMock, return_value=probe_result) as probe:
                manager = CaptchaManager(confirmation_timeout=0)
                if probe_result:
                    self.assertTrue(await manager.handle(page, visible=True))
                else:
                    with self.assertRaises(CaptchaError):
                        await manager.handle(page, visible=True)
                probe.assert_awaited_once_with(page)

    async def test_independent_pages_can_succeed_and_fail_separately(self):
        successful = self.page([1, 1, 0, 0], success=True)
        unsuccessful = self.page([1, 1, 0, 0])
        ok, failed = await asyncio.gather(
            CaptchaManager().handle(successful, visible=True),
            CaptchaManager(confirmation_timeout=0).handle(unsuccessful, visible=True),
            return_exceptions=True)
        self.assertTrue(ok)
        self.assertIsInstance(failed, CaptchaError)

    async def test_timeout_and_headless_fail_without_interaction(self):
        for visible in (False, True):
            page = self.page([1, 1, 1])
            with self.assertRaises(CaptchaError):
                await CaptchaManager(timeout=0).handle(page, visible=visible)
            page.mouse.down.assert_not_called()

    async def test_cancel_removes_listener_and_records_failure(self):
        page = self.page([1, 1, 1])
        events = []
        with patch('app.workers.captcha_manager.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await CaptchaManager(callback=events.append).handle(page, visible=True)
        self.assertEqual(events[-1].state, CaptchaState.FAILED)
        page.remove_listener.assert_called_once()

    async def test_search_preserves_partial_results_on_captcha_failure(self):
        ctx, page = MagicMock(), MagicMock()
        ctx.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        found = ts.FoundVideo(video_id='123', url='https://example.test/123', author='example')
        async def collect(_page, destination):
            destination[found.video_id] = found
        saved = []
        with patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock, side_effect=[None, CaptchaError('captcha')]), \
             patch.object(ts, '_recover_error_page', new_callable=AsyncMock), \
             patch.object(ts, '_collect_dom', side_effect=collect):
            with self.assertRaises(CaptchaError):
                await ts.search_videos(ctx, '#topic', on_partial=lambda query, videos: saved.extend(videos))
        self.assertEqual(saved, [found])
        page.close.assert_awaited_once()

    def test_only_explicit_tiktok_verify_success_is_trusted(self):
        self.assertTrue(verified_response('https://www.tiktok.com/captcha/verify', {'code': 0}))
        self.assertFalse(verified_response('https://example.com/captcha/verify', {'code': 0}))
        self.assertFalse(verified_response('https://www.tiktok.com/captcha/verify', {'success': False, 'code': 1}))
        self.assertFalse(verified_response('https://www.tiktok.com/captcha/verify', {'code': False}))
