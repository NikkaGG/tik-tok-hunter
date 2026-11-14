import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers.tiktok_search import (
    CaptchaError,
    CommentResult,
    SessionError,
    _check_video_availability,
    _ensure_comments_open,
    _parse_item,
    post_comment_on_video,
)


class TestPlaywrightPost(unittest.IsolatedAsyncioTestCase):
    async def test_batch_reuses_page_and_detaches_response_handler(self):
        from app.workers import tiktok_search as ts
        ctx = MagicMock()
        ctx.new_page = AsyncMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        token = ts.comment_page.set(page)
        try:
            with patch.object(ts, '_has_session', new_callable=AsyncMock, return_value=True), \
                 patch.object(ts, '_raise_if_blocked', new_callable=AsyncMock), \
                 patch.object(ts, '_dismiss_popups', new_callable=AsyncMock), \
                 patch.object(ts, '_check_video_availability', new_callable=AsyncMock, return_value=(False, 'Недоступен')):
                await post_comment_on_video(ctx, 'https://www.tiktok.com/@u/video/1', 'text')
                await post_comment_on_video(ctx, 'https://www.tiktok.com/@u/video/2', 'text')
            ctx.new_page.assert_not_awaited()
            page.close.assert_not_awaited()
            self.assertEqual(page.goto.await_count, 2)
            self.assertEqual(page.remove_listener.call_count, 2)
        finally:
            ts.comment_page.reset(token)

    async def test_session_missing(self):
        ctx = MagicMock()
        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock) as mock_has:
            mock_has.return_value = False
            res = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "text")
            self.assertFalse(res.success)
            self.assertEqual(res.error_code, "session_expired")

    async def test_video_unavailable(self):
        ctx = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        page.on = MagicMock()
        ctx.new_page = AsyncMock(return_value=page)

        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, return_value=(False, "Ролик недоступен")), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=False):

            res = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "text")
            self.assertFalse(res.success)
            self.assertEqual(res.error_code, "page_unavailable")
            self.assertIn("Ролик недоступен", res.message)

    async def test_comments_disabled(self):
        ctx = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        page.on = MagicMock()
        ctx.new_page = AsyncMock(return_value=page)

        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=True):

            res = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "text")
            self.assertFalse(res.success)
            self.assertEqual(res.error_code, "comments_disabled")
            self.assertIn("отключены", res.message)

    async def test_post_success_flow(self):
        ctx = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        handlers = []
        page.on = lambda event, fn: handlers.append(fn)

        editor = MagicMock()
        editor.inner_text = AsyncMock(return_value="")
        editor.focus = AsyncMock()
        editor.evaluate = AsyncMock(return_value="Hello World!")

        post_btn = MagicMock()
        post_btn.get_attribute = AsyncMock(return_value="false")
        post_btn.is_disabled = AsyncMock(return_value=False)
        post_btn.click = AsyncMock()

        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        page.keyboard.press = AsyncMock()

        ctx.new_page = AsyncMock(return_value=page)

        # Mock successful publish response from TikTok API
        async def on_click(**kw):
            mock_resp = MagicMock()
            mock_resp.url = "https://www.tiktok.com/api/comment/publish/"
            mock_resp.json = AsyncMock(return_value={"status_code": 0, "status_msg": ""})
            for h in handlers:
                await h(mock_resp)

        post_btn.click = AsyncMock(side_effect=on_click)

        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._ensure_comments_open", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._find_comment_editor", new_callable=AsyncMock, return_value=editor), \
             patch("app.workers.tiktok_search._find_post_button", new_callable=AsyncMock, return_value=post_btn):

            res = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "Hello World!")
            self.assertTrue(res.success)
            self.assertEqual(res.message, "TikTok принял комментарий. Видимость другим пользователям не проверена")

    async def test_incomplete_comment_is_never_published(self):
        ctx, page, editor = MagicMock(), MagicMock(), MagicMock()
        ctx.new_page = AsyncMock(return_value=page)
        page.goto, page.wait_for_timeout, page.close = AsyncMock(), AsyncMock(), AsyncMock()
        page.keyboard.press, page.keyboard.type = AsyncMock(), AsyncMock()
        editor.focus = AsyncMock()
        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._ensure_comments_open", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._find_comment_editor", new_callable=AsyncMock, return_value=editor), \
             patch("app.workers.tiktok_search._read_editor_text", new_callable=AsyncMock, return_value="partial"), \
             patch("app.workers.tiktok_search._find_post_button", new_callable=AsyncMock) as find_button:
            result = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "Hello World!")
        self.assertEqual(result.error_code, "input_not_found")
        find_button.assert_not_awaited()

    async def test_video_availability_ignores_false_positive_when_video_loaded(self):
        page = MagicMock()
        with patch("app.workers.tiktok_search._has_video_or_comments", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._find_unavailable_text", new_callable=AsyncMock, return_value="Страница не найдена"):
            avail, msg = await _check_video_availability(page)
            self.assertTrue(avail)
            self.assertIsNone(msg)

    async def test_video_availability_recovers_transient_not_found(self):
        page = MagicMock()
        with patch("app.workers.tiktok_search._has_video_or_comments", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._find_unavailable_text", new_callable=AsyncMock, return_value="Страница не найдена"), \
             patch("app.workers.tiktok_search._recover_video_page", new_callable=AsyncMock, return_value=True):
            avail, msg = await _check_video_availability(page)
            self.assertTrue(avail)
            self.assertIsNone(msg)

    async def test_blank_page_is_not_assumed_available(self):
        page = MagicMock()
        with patch("app.workers.tiktok_search._has_video_or_comments", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._find_unavailable_text", new_callable=AsyncMock, return_value=None):
            avail, message = await _check_video_availability(page)
        self.assertIsNone(avail)
        self.assertIn("не загрузилась", message)

    async def test_late_player_is_allowed_to_load(self):
        page = MagicMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        with patch("app.workers.tiktok_search._has_video_or_comments", new_callable=AsyncMock, side_effect=[False, True]), \
             patch("app.workers.tiktok_search._find_unavailable_text", new_callable=AsyncMock, return_value=None):
            avail, message = await _check_video_availability(page)
        self.assertTrue(avail)
        self.assertIsNone(message)
        page.locator.return_value.first.wait_for.assert_awaited_once()

    async def test_cross_region_restriction_reports_cause_and_preserves_draft(self):
        from app.workers.tiktok_search import _availability_error
        page = MagicMock()
        with patch("app.workers.tiktok_search._has_video_or_comments", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._find_unavailable_text", new_callable=AsyncMock, return_value="Видео недоступно"), \
             patch("app.workers.tiktok_search._cross_region_block", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._recover_video_page", new_callable=AsyncMock) as recover:
            avail, message = await _check_video_availability(page)
        self.assertFalse(avail)
        self.assertIn("между регионами", message)
        self.assertEqual(_availability_error(avail, message).error_code, "page_unavailable")
        recover.assert_not_awaited()

    async def test_navigation_failure_does_not_skip_video(self):
        ctx, page = MagicMock(), MagicMock()
        ctx.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock(side_effect=TimeoutError("timeout"))
        page.close = AsyncMock()
        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True):
            result = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "text")
        self.assertEqual(result.error_code, "page_unavailable")

    async def test_unavailable_after_reload_is_not_reported_as_missing_editor(self):
        ctx, page = MagicMock(), MagicMock()
        ctx.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock()
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, side_effect=[(True, None), (False, "Ролик недоступен (Видео недоступно)")]), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._ensure_comments_open", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._find_comment_editor", new_callable=AsyncMock, return_value=None):
            result = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "text")
        self.assertEqual(result.error_code, "page_unavailable")
        self.assertIn("недоступен", result.message)

    def test_only_explicitly_removed_or_private_videos_are_skipped(self):
        from app.workers.tiktok_search import _availability_error
        self.assertEqual(_availability_error(False, "Ролик недоступен (Видео недоступно)").error_code, "page_unavailable")
        self.assertEqual(_availability_error(False, "Ролик недоступен (This video is private)").error_code, "video_unavailable")

    def test_parse_item_video_and_photo_and_author_cleanup(self):
        v1 = _parse_item({"id": "12345", "author": {"uniqueId": "@my_user"}, "desc": "test"})
        self.assertIsNotNone(v1)
        self.assertEqual(v1.author, "my_user")
        self.assertEqual(v1.url, "https://www.tiktok.com/@my_user/video/12345")

        v2 = _parse_item({"id": "67890", "author": "photo_user", "imagePost": {"images": []}})
        self.assertIsNotNone(v2)
        self.assertEqual(v2.author, "photo_user")
        self.assertEqual(v2.url, "https://www.tiktok.com/@photo_user/photo/67890")

    async def test_ensure_comments_open_does_not_toggle_when_already_visible(self):
        page = MagicMock()
        open_loc = MagicMock()
        open_loc.count = AsyncMock(return_value=1)
        icon_loc = MagicMock()
        icon_loc.first.click = AsyncMock()

        def locator_side_effect(sel):
            if "comment-level-1" in sel:
                return open_loc
            return icon_loc

        page.locator = MagicMock(side_effect=locator_side_effect)
        await _ensure_comments_open(page)
        icon_loc.first.click.assert_not_called()

    async def test_post_fails_if_text_remains_in_editor_without_api_confirm(self):
        ctx = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        page.on = MagicMock()
        page.locator = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=0)
        page.get_by_text = MagicMock()
        page.get_by_text.return_value.first.is_visible = AsyncMock(return_value=False)

        editor = MagicMock()
        editor.inner_text = AsyncMock(return_value="Hello World!")
        editor.focus = AsyncMock()
        editor.evaluate = AsyncMock(return_value="Hello World!")

        post_btn = MagicMock()
        post_btn.get_attribute = AsyncMock(return_value="false")
        post_btn.is_disabled = AsyncMock(return_value=False)
        post_btn.click = AsyncMock()

        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        page.keyboard.press = AsyncMock()

        ctx.new_page = AsyncMock(return_value=page)

        with patch("app.workers.tiktok_search._has_session", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.tiktok_search._raise_if_blocked", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._dismiss_popups", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._check_video_availability", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.workers.tiktok_search._check_login_required", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._check_comments_disabled", new_callable=AsyncMock, return_value=False), \
             patch("app.workers.tiktok_search._ensure_comments_open", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search._find_comment_editor", new_callable=AsyncMock, return_value=editor), \
             patch("app.workers.tiktok_search._find_post_button", new_callable=AsyncMock, return_value=post_btn):

            res = await post_comment_on_video(ctx, "https://www.tiktok.com/@u/video/1", "Hello World!")
            self.assertFalse(res.success)
            self.assertEqual(res.error_code, "uncertain")


if __name__ == "__main__":
    unittest.main()
