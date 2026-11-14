import asyncio
import json
import unittest
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Account,
    AccountStatus,
    Draft,
    DraftStatus,
    ReplyTemplate,
    TaskRun,
    TaskStatus,
    Video,
    VideoStatus,
)
from app.schemas import AutoCommentTaskIn
from app.workers.tiktok_search import CommentResult


class TestAutoComment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create an in-memory SQLite database for testing
        cls.engine = create_engine("sqlite:///:memory:")
        cls.SessionLocal = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        log_patch = patch("app.services.tasks.log_event")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_schema_validation(self):
        """Test AutoCommentTaskIn schema validations."""
        data = AutoCommentTaskIn(
            draft_ids=[1, 2, 3],
            delay_min_s=15,
            delay_max_s=30,
            visible=True,
            max_count=5,
        )
        self.assertEqual(data.draft_ids, [1, 2, 3])
        self.assertEqual(data.delay_min_s, 15)
        self.assertEqual(data.delay_max_s, 30)
        self.assertTrue(data.visible)
        self.assertEqual(data.max_count, 5)

        # Default values
        defaults = AutoCommentTaskIn()
        self.assertIsNone(defaults.draft_ids)
        self.assertEqual(defaults.delay_min_s, 20)
        self.assertEqual(defaults.delay_max_s, 45)
        self.assertFalse(defaults.visible)

    def test_comment_result_dataclass(self):
        """Test CommentResult attributes and error codes."""
        ok = CommentResult(success=True, message="Комментарий успешно опубликован")
        self.assertTrue(ok.success)
        self.assertIsNone(ok.error_code)

        err = CommentResult(
            success=False,
            message="Комментарии отключены автором",
            error_code="comments_disabled",
        )
        self.assertFalse(err.success)
        self.assertEqual(err.error_code, "comments_disabled")

    def test_task_status_enum(self):
        """Ensure CANCELLED is in TaskStatus."""
        self.assertEqual(TaskStatus.CANCELLED, "cancelled")
        self.assertIn("cancelled", [s.value for s in TaskStatus])

    def test_task_cancellation_logic(self):
        """Test task cancel and is_cancelled functions."""
        from app.services import tasks

        with patch("app.services.tasks.SessionLocal", self.SessionLocal):
            task = TaskRun(kind="auto_comment", status=TaskStatus.QUEUED)
            self.db.add(task)
            self.db.commit()
            self.db.refresh(task)

            self.assertFalse(tasks.is_cancelled(task.id))
            res = tasks.cancel(task.id)
            self.assertTrue(res)
            self.assertTrue(tasks.is_cancelled(task.id))

            # Database row updated
            self.db.refresh(task)
            self.assertEqual(task.status, TaskStatus.CANCELLED)
            self.assertEqual(task.error, "Отменено пользователем")

    def test_router_endpoints(self):
        """Test start_auto_comment and cancel_task router functions."""
        from app.routers import drafts as drafts_router
        from app.routers import tasks as tasks_router

        with patch("app.routers.tasks.tasks.submit_batch", return_value={"tasks": []}) as batch, \
             patch("app.routers.tasks.tasks.cancel", return_value=True):
            data = AutoCommentTaskIn(draft_ids=[10])
            self.assertEqual(tasks_router.start_auto_comment(data), {"tasks": []})
            batch.assert_called_once_with(data)
            self.assertTrue(tasks_router.cancel_task(1)["ok"])

    def test_single_draft_auto_comment_router(self):
        from app.routers import drafts as router
        with patch("app.routers.drafts.tasks.submit_batch", return_value={"tasks": []}) as batch:
            self.assertEqual(router.auto_comment_draft(7, visible=True), {"tasks": []})
            self.assertEqual(batch.call_args.args[0].draft_ids, [7])
            self.assertTrue(batch.call_args.args[0].visible)

    def test_run_auto_comment_worker_flow(self):
        """Test _run_auto_comment batch processing flow with mocked Playwright."""
        from app.services import tasks

        # Setup test data: Account, Video, Drafts
        acc = Account(username="test_bot", status=AccountStatus.ACTIVE, daily_reply_limit=10)
        v1 = Video(video_id="v1", url="https://www.tiktok.com/@user1/video/111", query="#tag")
        v2 = Video(video_id="v2", url="https://www.tiktok.com/@user2/video/222", query="#tag")
        v3 = Video(video_id="v3", url="https://www.tiktok.com/@user3/video/333", query="#tag")
        self.db.add_all([acc, v1, v2, v3])
        self.db.commit()

        d1 = Draft(task_id=99, video_id=v1.id, text="Comment 1", status=DraftStatus.DRAFT, account_id=acc.id)
        d2 = Draft(task_id=99, video_id=v2.id, text="Comment 2", status=DraftStatus.DRAFT, account_id=acc.id)
        d3 = Draft(task_id=99, video_id=v3.id, text="Comment 3", status=DraftStatus.DRAFT, account_id=acc.id)
        self.db.add_all([d1, d2, d3])
        self.db.commit()

        # Task in DB
        task = TaskRun(id=99, kind="auto_comment", status=TaskStatus.RUNNING)
        self.db.add(task)
        self.db.commit()

        # Mock post_comment_on_video to return success for d1, disabled for d2, error for d3
        async def mock_post(ctx, url, text, visible=False):
            if "111" in url:
                return CommentResult(success=True, message="Опубликовано")
            elif "222" in url:
                return CommentResult(success=False, message="Отключены", error_code="comments_disabled")
            else:
                return CommentResult(success=False, message="Ошибка", error_code="unknown")

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_open_context(headless=True):
            mock = MagicMock()
            mock.storage_state = AsyncMock()
            yield mock

        mock_path = MagicMock()
        mock_path.exists.return_value = True

        from app.services.sessions import browser_binding
        token = browser_binding.set({"path": mock_path, "username": acc.username, "proxy": None})
        self.addCleanup(browser_binding.reset, token)
        with patch("app.services.tasks.SessionLocal", self.SessionLocal), \
             patch("app.services.tasks.verify_context", new_callable=AsyncMock), \
             patch("app.workers.tiktok_search.open_context", mock_open_context), \
             patch("app.workers.tiktok_search.post_comment_on_video", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result, count = tasks._run_auto_comment(99, {"account_id": acc.id, "delay_min_s": 1, "delay_max_s": 1})
            self.assertEqual(count, 1)
            self.assertIn("ошибок: 1", result)

            # Check DB state
            self.db.refresh(d1)
            self.db.refresh(d2)
            self.db.refresh(d3)
            self.db.refresh(v1)

            self.assertEqual(d1.status, DraftStatus.SENT)
            self.assertIsNotNone(d1.sent_at)
            self.assertEqual(v1.status, VideoStatus.REPLIED)

            self.assertEqual(d2.status, DraftStatus.SKIPPED)
            self.assertEqual(d3.status, DraftStatus.DRAFT)  # Error leaves draft for retry

    def test_account_manager_states_and_proxies(self):
        """Test Account Manager effective states, proxy host extraction, and bulk operations."""
        from app.routers import accounts as acc_router
        from app.schemas import AccountBulkDelete, AccountBulkUpdate, AccountCreate, AccountUpdate

        with patch("app.services.logs.SessionLocal", self.SessionLocal):
            # 1. Create ready account with proxy
            a1 = acc_router.create_account(
                AccountCreate(
                    username="@home.finds_kz",
                    display_name="Товары для дома",
                    status=AccountStatus.ACTIVE,
                    daily_reply_limit=20,
                    proxy="185.92.43.18:8001:user1:pass1",
                    proxy_label="Казахстан · стабильно",
                    notes="Работает по новому списку",
                ),
                db=self.db,
            )
            self.assertEqual(a1.username, "home.finds_kz")
            self.assertEqual(a1.proxy_host, "185.92.43.18:8001")
            self.assertEqual(a1.effective_state, "ready")

            # 2. Working account
            a2 = acc_router.create_account(
                AccountCreate(
                    username="@daily.tech_kz",
                    display_name="Техника",
                    status=AccountStatus.WORKING,
                    daily_reply_limit=25,
                    proxy="91.203.17.204:3000",
                    proxy_label="Германия · активно",
                ),
                db=self.db,
            )
            self.assertEqual(a2.effective_state, "working")

            # 3. Resting account (when daily_reply_limit reached or paused)
            a3 = acc_router.create_account(
                AccountCreate(
                    username="@cozy.corner_kz",
                    display_name="Интерьер",
                    status=AccountStatus.PAUSED,
                    daily_reply_limit=20,
                    proxy="37.18.55.90:8080",
                ),
                db=self.db,
            )
            self.assertEqual(a3.effective_state, "resting")

            # 4. Warning account (when proxy_status == 'error')
            a4 = acc_router.create_account(
                AccountCreate(
                    username="@beauty.notes_kz",
                    display_name="Косметика",
                    status=AccountStatus.ACTIVE,
                    daily_reply_limit=20,
                    proxy="104.28.61.11:9090",
                    proxy_label="Россия · недоступен",
                    proxy_status="error",
                ),
                db=self.db,
            )
            self.assertEqual(a4.effective_state, "warning")

            # Switching status to ACTIVE clears proxy_status error automatically
            a4_fixed = acc_router.update_account(
                a4.id,
                AccountUpdate(status=AccountStatus.ACTIVE),
                db=self.db,
            )
            self.assertEqual(a4_fixed.effective_state, "ready")
            self.assertEqual(a4_fixed.proxy_status, "ok")

            # 5. Bulk update & bulk delete
            res_upd = acc_router.bulk_update_accounts(
                AccountBulkUpdate(ids=[a1.id, a2.id], daily_reply_limit=30),
                db=self.db,
            )
            self.assertEqual(res_upd["updated"], 2)
            self.assertEqual(acc_router.get_account(a1.id, db=self.db).daily_reply_limit, 30)

            res_del = acc_router.bulk_delete_accounts(
                AccountBulkDelete(ids=[a3.id]),
                db=self.db,
            )
            self.assertEqual(res_del["deleted"], 1)
            all_accs = acc_router.list_accounts(db=self.db)
            self.assertEqual(len(all_accs), 3)


if __name__ == "__main__":
    unittest.main()
