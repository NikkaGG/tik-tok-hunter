import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import asynccontextmanager, ExitStack
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from app.database import Base, ensure_schema
from app.models import Account, AccountTextSet, Draft, TextSet, TextVariant, TextUse, Video, TaskRun
from app.services import text_library as library, tasks, sessions
from app.schemas import AutoCommentTaskIn, DraftUpdate, DraftBulkDelete
from app.routers import drafts as router
from app.workers.tiktok_search import CommentResult, SessionError


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / ".tmp"
        test_root.mkdir(exist_ok=True)
        import uuid, shutil
        self.root = test_root / uuid.uuid4().hex
        self.root.mkdir()
        def cleanup():
            assert self.root.resolve().is_relative_to(test_root.resolve())
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.engine = create_engine(f'sqlite:///{self.root / "test.db"}', connect_args={'check_same_thread': False, 'timeout': 30})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.db = self.factory()
        self.addCleanup(self.db.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tasks, 'SessionLocal', self.factory))
        self.stack.enter_context(patch.object(tasks, 'log_event'))
        self.stack.enter_context(patch.object(sessions, 'SESSION_DIR', self.root))
        self.stack.enter_context(patch.object(tasks.app_settings, 'search_proxy', return_value=None))
        self.stack.enter_context(patch.object(tasks.app_settings, 'browser_limit', return_value=3))
        self.accounts = [Account(username='account_a'), Account(username='account_b')]
        self.videos = [Video(video_id=f'v{i}', url=f'https://example.test/{i}', query='#topic', author='writer') for i in range(4)]
        self.db.add_all(self.accounts + self.videos)
        self.db.commit()
        for a in self.accounts:
            sessions.state_path(a.id).write_text('{"cookies":[],"origins":[]}')
        self.pool = TextSet(name='Topic')
        self.db.add(self.pool)
        self.db.flush()
        self.db.add_all([TextVariant(text_set_id=self.pool.id, body=b) for b in ['First [author]', 'Second', 'Third', 'Fourth']])
        self.db.add_all([AccountTextSet(account_id=a.id, text_set_id=self.pool.id) for a in self.accounts])
        self.db.commit()

    def prepare(self, videos=None, accounts=None):
        return library.prepare(self.db, videos or [self.videos[0].id], accounts or [a.id for a in self.accounts], [self.pool.id])

    def test_same_video_two_accounts_and_duplicate_pair(self):
        result = self.prepare()
        self.assertEqual(len(result['created']), 2)
        self.assertNotEqual(result['created'][0]['text'], result['created'][1]['text'])
        again = self.prepare()
        self.assertEqual(again['created'], [])
        self.assertEqual(len(again['skipped']), 2)

    def test_bulk_preparation_keeps_unique_pairs_and_history(self):
        accounts = self.accounts + [Account(username='account_c'), Account(username='account_d')]
        videos = self.videos + [Video(video_id=f'bulk{i}', url=f'https://example.test/bulk/{i}',
                                      query='#topic', author=f'author{i}') for i in range(76)]
        self.db.add_all(accounts[2:] + videos[4:])
        self.db.commit()
        account_ids, video_ids = [a.id for a in accounts], [v.id for v in videos]
        result = library.prepare(self.db, video_ids, account_ids, [self.pool.id])
        self.assertEqual(len(result['created']), 320)
        pairs = {(d['video_id'], d['account_id']) for d in result['created']}
        self.assertEqual(len(pairs), 320)
        self.assertEqual(len(list(self.db.scalars(select(TextUse).where(TextUse.state == 'reserved')))), 320)
        self.assertTrue(any(d['repeated'] for d in result['created']))
        again = library.prepare(self.db, video_ids, account_ids, [self.pool.id])
        self.assertEqual(len(again['created']), 0)
        self.assertEqual(len(again['skipped']), 320)

    def test_preparation_preview_explains_missing_assignment_without_writes(self):
        self.db.query(AccountTextSet).filter(AccountTextSet.account_id == self.accounts[1].id).delete()
        self.db.commit()
        result = library.preview(self.db, [v.id for v in self.videos],
            [a.id for a in self.accounts], [self.pool.id])
        self.assertEqual(result['video_count'], 4)
        self.assertEqual(result['maximum_drafts'], 8)
        row = next(a for a in result['accounts'] if a['account_id'] == self.accounts[1].id)
        self.assertIsNone(row['reason'])
        self.assertEqual(row['candidate_count'], 4)
        self.assertEqual(list(self.db.scalars(select(Draft))), [])
        self.assertEqual(list(self.db.scalars(select(TextUse))), [])

    def test_preparation_preview_capacity_and_duplicate_pairs(self):
        result = library.preview(self.db, [v.id for v in self.videos],
            [a.id for a in self.accounts], [self.pool.id])
        self.assertEqual(result['candidate_count'], 8)
        self.assertEqual(result['maximum_drafts'], 8)
        self.db.rollback()
        self.prepare()
        result = library.preview(self.db, [v.id for v in self.videos],
            [a.id for a in self.accounts], [self.pool.id])
        self.assertEqual(sum(a['duplicate_count'] for a in result['accounts']), 2)
        self.assertEqual(result['candidate_count'], 6)
        self.assertEqual(result['maximum_drafts'], 6)

    def test_preparation_preview_busy_variants_and_empty_scope(self):
        self.prepare([v.id for v in self.videos])
        result = library.preview(self.db, [v.id for v in self.videos],
            [a.id for a in self.accounts], [self.pool.id])
        self.assertEqual(result['maximum_drafts'], 0)
        self.assertTrue(all(a['reason'] is None for a in result['accounts']))
        result = library.preview(self.db, [], [], [])
        self.assertEqual(result['video_count'], 0)
        self.assertEqual(result['maximum_drafts'], 0)

    def test_concurrent_preparation_reserves_globally(self):
        def generate(aid):
            with self.factory() as db:
                return library.prepare(db, [self.videos[0].id], [aid], [self.pool.id])['created'][0]['text']
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(generate, [a.id for a in self.accounts]))
        self.assertEqual(len(set(results)), 2)

    def test_exhaustion_and_release(self):
        result = self.prepare([v.id for v in self.videos])
        self.assertEqual(len(result['created']), 8)
        self.assertFalse(result['skipped'])
        self.assertTrue(any(d['repeated'] for d in result['created']))
        did = result['created'][0]['id']
        video_id = result['created'][0]['video_id']
        router.delete_draft(did, self.db)
        # Released variants become available; old draft IDs are never reused.
        result = self.prepare([video_id])
        self.assertEqual(len(result['created']), 1)
        self.assertNotEqual(result['created'][0]['id'], did)

    def test_history_survives_deletion_and_prevents_pair_retry(self):
        did = self.prepare(accounts=[self.accounts[0].id])['created'][0]['id']
        router.mark_sent(did, self.db)
        router.delete_draft(did, self.db)
        self.assertEqual(self.prepare(accounts=[self.accounts[0].id])['created'], [])
        self.assertEqual(self.db.scalar(select(TextUse.state).where(TextUse.draft_id == did)), 'sent')

    def test_lru_repeat_and_pending_drafts_count(self):
        self.prepare([v.id for v in self.videos], [self.accounts[0].id])
        ids = list(self.db.scalars(select(Draft.id)))
        self.db.rollback()
        for did in ids:
            router.mark_sent(did, self.db)
        extra = Video(video_id='extra', url='https://example.test/extra', query='topic', author='writer')
        self.db.add(extra)
        self.db.commit()
        result = self.prepare([extra.id], [self.accounts[0].id])
        self.assertEqual(result['created'][0]['text'], self.db.get(Draft, ids[0]).text)
        self.assertTrue(result['created'][0]['repeated'])

    def test_manual_edit_collision_is_atomic(self):
        created = self.prepare()['created']
        router.update_draft(created[0]['id'], DraftUpdate(text=created[1]['text']), self.db)
        self.assertEqual(self.db.get(Draft, created[0]['id']).text, created[1]['text'])
        self.assertTrue(self.db.get(Draft, created[0]['id']).repeated)

    def test_multiple_sets_only_intersection(self):
        pool = TextSet(name='Other')
        self.db.add(pool)
        self.db.flush()
        self.db.add(TextVariant(text_set_id=pool.id, body='Other text'))
        self.db.add(AccountTextSet(account_id=self.accounts[0].id, text_set_id=pool.id))
        self.db.commit()
        result = library.prepare(self.db, [self.videos[0].id], [a.id for a in self.accounts], [pool.id])
        self.assertEqual(len(result['created']), 2)
        self.assertEqual(result['created'][0]['text'], 'Other text')
        self.assertEqual(len(result['skipped']), 0)

    def test_prepare_all_assigned_includes_existing_and_new_drafts(self):
        first = self.prepare([self.videos[0].id], [self.accounts[0].id])['created'][0]
        result = router.prepare_all_assigned(router.PrepareAllIn(), self.db)
        self.assertEqual(result['created'], 7)
        self.assertIn(first['id'], result['draft_ids'])
        self.assertEqual(len(result['draft_ids']), 8)

    def test_search_all_distributes_queries_between_accounts(self):
        from app.routers.tasks import start_search_all
        from app.schemas import SearchTaskIn
        with patch.object(tasks, 'submit', side_effect=lambda kind, params: type('Job', (), {'id': params['account_id']})()) as submit:
            response = start_search_all(SearchTaskIn(queries=['one', 'two', 'three']), self.db)
        self.assertEqual(response['accounts_used'], 2)
        self.assertEqual(submit.call_args_list[0].args[1]['queries'], ['one', 'three'])
        self.assertEqual(submit.call_args_list[1].args[1]['queries'], ['two'])

    def test_batch_claims_split_and_no_double_start(self):
        made = self.prepare()['created']
        with patch.object(tasks, 'enqueue') as enqueue:
            result = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[d['id'] for d in made]))
            self.assertEqual(len(result['tasks']), 2)
            self.assertEqual(enqueue.call_count, 2)
            with self.assertRaises(HTTPException):
                tasks.submit_batch(AutoCommentTaskIn(draft_ids=[d['id'] for d in made]))
        self.db.expire_all()
        self.assertTrue(self.db.get(Draft, made[0]['id']).task_id)

    def test_unassigned_requires_explicit_account(self):
        d = Draft(video_id=self.videos[0].id, text='Legacy comment')
        self.db.add(d)
        self.db.commit()
        with self.assertRaises(HTTPException):
            tasks.submit_batch(AutoCommentTaskIn(draft_ids=[d.id]))
        with patch.object(tasks, 'enqueue'):
            result = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[d.id], account_id=self.accounts[0].id))
        self.assertEqual(len(result['tasks']), 1)

    def test_all_assigned_skips_legacy_unassigned_draft(self):
        legacy = Draft(video_id=self.videos[1].id, text='Legacy comment')
        self.db.add(legacy)
        self.db.commit()
        made = self.prepare()['created']
        with patch.object(tasks, 'enqueue') as enqueue:
            result = tasks.submit_batch(AutoCommentTaskIn())
        self.assertEqual(result['skipped_unassigned'], 1)
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual(enqueue.call_count, 2)
        self.db.expire_all()
        self.assertIsNone(self.db.get(Draft, legacy.id).account_id)
        self.assertIsNone(self.db.get(Draft, legacy.id).task_id)
        self.assertTrue(all(self.db.get(Draft, d['id']).task_id for d in made))

    def test_draft_summary_counts_all_and_account_scope(self):
        legacy = Draft(video_id=self.videos[1].id, text='Legacy comment')
        self.db.add(legacy)
        self.db.commit()
        self.prepare()
        result = router.draft_summary(db=self.db)
        self.assertEqual(result, {'available': 2, 'unassigned': 1, 'in_progress': 0})
        result = router.draft_summary(account_id=self.accounts[0].id, db=self.db)
        self.assertEqual(result, {'available': 2, 'unassigned': 0, 'in_progress': 0})

    def test_recovery_preserves_uncertain_publication(self):
        made = self.prepare(accounts=[self.accounts[0].id])['created'][0]
        d = self.db.get(Draft, made['id'])
        job = TaskRun(kind='auto_comment', status='running')
        self.db.add(job)
        self.db.flush()
        d.task_id, d.sending = job.id, 1
        self.db.commit()
        tasks.recover_interrupted()
        self.db.expire_all()
        d = self.db.get(Draft, d.id)
        self.assertEqual(d.status, 'review')
        self.assertIsNone(d.task_id)
        self.assertEqual(self.db.scalar(select(TextUse.state).where(TextUse.draft_id == d.id)), 'review')
        self.db.rollback()
        router.resolve(d.id, router.ReviewIn(published=False), self.db)
        self.assertEqual(self.db.get(Draft, d.id).status, 'draft')

    def test_zero_limit_manual_and_worker_uncertainty(self):
        self.accounts[0].daily_reply_limit = 0
        self.db.commit()
        did = self.prepare(accounts=[self.accounts[0].id])['created'][0]['id']
        router.mark_sent(did, self.db)
        self.assertEqual(self.db.get(Draft, did).status, 'sent')
        self.db.rollback()
        did = self.prepare([self.videos[1].id], [self.accounts[0].id])['created'][0]['id']
        with patch.object(tasks, 'enqueue'):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[did]))['tasks'][0]
        @asynccontextmanager
        async def ctx(**kw):
            mock = MagicMock()
            mock.storage_state = AsyncMock()
            yield mock
        with patch.object(tasks.ts, 'open_context', ctx), patch.object(tasks, 'verify_context', new_callable=AsyncMock), \
             patch.object(tasks.ts, 'post_comment_on_video', new_callable=AsyncMock, return_value=CommentResult(False, 'Uncertain', 'uncertain')):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Draft, did).status, 'review')
        self.assertIsNone(self.db.get(Draft, did).task_id)
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'failed')

    def test_identity_mismatch_never_posts(self):
        did = self.prepare(accounts=[self.accounts[0].id])['created'][0]['id']
        with patch.object(tasks, 'enqueue'):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[did]))['tasks'][0]
        @asynccontextmanager
        async def ctx(**kw):
            mock = MagicMock()
            mock.storage_state = AsyncMock()
            yield mock
        with patch.object(tasks.ts, 'open_context', ctx), \
             patch.object(tasks, 'verify_context', new_callable=AsyncMock, side_effect=SessionError('Mismatch')), \
             patch.object(tasks.ts, 'post_comment_on_video', new_callable=AsyncMock) as post:
            tasks._execute(job.id)
            post.assert_not_awaited()
        self.db.expire_all()
        self.assertEqual(self.db.get(Draft, did).status, 'draft')
        self.assertEqual(self.db.get(Account, self.accounts[0].id).session_status, 'error')

    def test_identity_verification_requires_tiktok_response(self):
        async def check():
            with patch.object(tasks.ts, '_account_info', new_callable=AsyncMock, return_value={'data': {'username': 'account_b'}}):
                with self.assertRaises(SessionError):
                    await sessions.verify_context(MagicMock(), 'account_a')
            with patch.object(tasks.ts, '_account_info', new_callable=AsyncMock, return_value=None):
                with self.assertRaises(SessionError):
                    await sessions.verify_context(MagicMock(), 'account_a')
        asyncio.run(check())

    def test_scheduler_skips_busy_accounts_and_honors_limit(self):
        class Executor:
            def __init__(self): self.started = []
            def submit(self, fn, tid):
                self.started.append(tid)
                return Future()
        executor = Executor()
        with tasks.coordinator, patch.object(tasks, '_pending', [(1, 10), (2, 10), (3, 20), (4, 30), (5, 40)]), \
             patch.object(tasks, '_active', {}), patch.object(tasks, '_executor', executor), patch.object(tasks, '_stopping', False):
            tasks._dispatch()
            self.assertEqual(executor.started, [1, 3, 4])
            self.assertEqual(tasks._pending, [(2, 10), (5, 40)])
            tasks._completed(3)
            self.assertEqual(executor.started, [1, 3, 4, 5])
            tasks._completed(1)
            self.assertEqual(executor.started[-1], 2)

    def test_account_operations_block_queued_and_active(self):
        from app.routers.accounts import delete_account, update_account
        from app.schemas import AccountUpdate
        aid = self.accounts[0].id
        self.db.add(TaskRun(kind='search', status='queued', params=json.dumps({'account_id': aid})))
        self.db.commit()
        with self.assertRaises(HTTPException):
            delete_account(aid, self.db)
        with self.assertRaises(HTTPException):
            update_account(aid, AccountUpdate(username='renamed'), self.db)
        self.db.rollback()

    def test_round_robin_distributes_small_library(self):
        made = self.prepare([v.id for v in self.videos])['created']
        counts = {a.id: sum(d['account_id'] == a.id for d in made) for a in self.accounts}
        self.assertEqual(list(counts.values()), [4, 4])

    def test_duplicate_text_across_sets_is_not_reserved_twice(self):
        for v in self.db.scalars(select(TextVariant)):
            v.archived = 1
        other = TextSet(name='Duplicate pool')
        self.db.add(other)
        self.db.flush()
        for pool in (self.pool, other):
            self.db.add(TextVariant(text_set_id=pool.id, body='  IDENTICAL    comment  '))
        self.db.add(AccountTextSet(account_id=self.accounts[1].id, text_set_id=other.id))
        self.db.commit()
        result = library.prepare(self.db, [self.videos[0].id], [a.id for a in self.accounts], [self.pool.id, other.id])
        self.assertEqual(len(result['created']), 2)
        self.assertTrue(result['created'][1]['repeated'])
        self.assertEqual(len(result['skipped']), 0)

    def test_spintax_prefers_unused_shape_across_authors(self):
        variants = list(self.db.scalars(select(TextVariant)))
        for v in variants:
            v.archived = 1
        self.db.add(TextVariant(text_set_id=self.pool.id, body='{Hello|Welcome} [author]'))
        self.db.commit()
        first = self.prepare(accounts=[self.accounts[0].id])['created'][0]
        router.mark_sent(first['id'], self.db)
        self.videos[1].author = 'other_author'
        self.db.commit()
        second = self.prepare([self.videos[1].id], [self.accounts[0].id])['created'][0]
        self.assertNotEqual(first['text'].split()[0], second['text'].split()[0])
        self.assertFalse(second['repeated'])

    def test_worker_limit_stops_before_publication(self):
        aid = self.accounts[0].id
        self.accounts[0].daily_reply_limit = 1
        self.db.commit()
        first = self.prepare(accounts=[aid])['created'][0]
        router.mark_sent(first['id'], self.db)
        second = self.prepare([self.videos[1].id], [aid])['created'][0]
        with patch.object(tasks, 'enqueue'):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[second['id']]))['tasks'][0]
        @asynccontextmanager
        async def ctx(**kw):
            yield MagicMock()
        with patch.object(tasks.ts, 'open_context', ctx), patch.object(tasks, 'verify_context', new_callable=AsyncMock), \
             patch.object(tasks.ts, 'post_comment_on_video', new_callable=AsyncMock) as post:
            tasks._execute(job.id)
            post.assert_not_awaited()
        self.db.expire_all()
        self.assertEqual(self.db.get(Draft, second['id']).status, 'draft')
        self.assertIn('лимит', self.db.get(TaskRun, job.id).error)

    def test_cancel_inflight_preserves_review_and_closes_context(self):
        from threading import Event
        entered, closed = Event(), Event()
        did = self.prepare(accounts=[self.accounts[0].id])['created'][0]['id']
        with patch.object(tasks, 'enqueue'):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[did]))['tasks'][0]
        @asynccontextmanager
        async def ctx(**kw):
            try:
                yield MagicMock()
            finally:
                closed.set()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(tasks.ts, 'open_context', ctx), patch.object(tasks, 'verify_context', new_callable=AsyncMock), \
             patch.object(tasks.ts, 'post_comment_on_video', pending), \
             patch.object(tasks, '_active', {job.id: self.accounts[0].id}), patch.object(tasks, '_pending', []):
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(tasks._execute, job.id)
                self.assertTrue(entered.wait(3))
                self.assertTrue(tasks.cancel(job.id))
                future.result(timeout=3)
        self.assertTrue(closed.is_set())
        self.db.expire_all()
        self.assertEqual(self.db.get(Draft, did).status, 'review')
        self.assertIsNone(self.db.get(Draft, did).task_id)
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'cancelled')

    def test_claimed_draft_cannot_be_edited_or_deleted(self):
        did = self.prepare(accounts=[self.accounts[0].id])['created'][0]['id']
        with patch.object(tasks, 'enqueue'):
            tasks.submit_batch(AutoCommentTaskIn(draft_ids=[did]))
        with self.assertRaises(HTTPException):
            router.update_draft(did, DraftUpdate(text='Changed'), self.db)
        self.db.rollback()
        with self.assertRaises(HTTPException):
            router.delete_draft(did, self.db)
        self.db.rollback()

    def test_contexts_use_separate_cookie_files_and_proxies(self):
        ts = tasks.ts
        captured = []
        class PW:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args): pass
            @property
            def chromium(self): return self
            async def launch(self, **kw):
                browser = MagicMock()
                async def new_context(**args):
                    captured.append((kw['proxy'], args['storage_state']))
                    ctx = MagicMock()
                    ctx.add_init_script = AsyncMock()
                    return ctx
                browser.new_context = new_context
                browser.close = AsyncMock()
                return browser
        async def one(aid, proxy):
            token = sessions.browser_binding.set({'path': sessions.state_path(aid), 'username': 'user', 'proxy': proxy})
            try:
                await asyncio.sleep(0)
                async with ts.open_context(): pass
            finally:
                sessions.browser_binding.reset(token)
        async def run():
            await asyncio.gather(one(1, 'http://host1:8001'), one(2, 'http://host2:8002'))
        with patch.object(ts, 'async_playwright', PW), patch.object(ts.app_settings, 'use_system_chrome', return_value=False):
            asyncio.run(run())
        self.assertEqual({(p['server'], path) for p, path in captured}, {
            ('http://host1:8001', str(sessions.state_path(1))),
            ('http://host2:8002', str(sessions.state_path(2)))})

    def test_connect_discovers_username_without_existing_account(self):
        from app.routers.tasks import connect_account, ConnectIn
        with patch.object(tasks, 'enqueue'), patch.object(tasks.app_settings, 'search_account_id', return_value=None), \
             patch.object(tasks.app_settings, 'update_values') as settings_update:
            job = connect_account(ConnectIn(method='import', data='z' * 32))
            token = json.loads(job.params)['connection_token']
            self.assertNotIn('z' * 32, job.params)
            self.assertTrue(sessions.connection_path(token).exists())
            with patch.object(tasks.ts, 'check_session', new_callable=AsyncMock, return_value=(True, 'OK', 'discovered_user')):
                tasks._execute(job.id)
            settings_update.assert_called_once()
        self.db.expire_all()
        account = self.db.scalar(select(Account).where(Account.username == 'discovered_user'))
        self.assertIsNotNone(account)
        self.assertEqual(account.session_status, 'active')
        self.assertTrue(sessions.state_path(account.id).exists())
        self.assertFalse(sessions.connection_path(token).exists())
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'done')
        self.assertEqual(json.loads(self.db.get(TaskRun, job.id).params)['account_id'], account.id)

    def test_connect_failure_creates_no_account_and_removes_candidate(self):
        from app.routers.tasks import connect_account, ConnectIn
        with patch.object(tasks, 'enqueue'):
            job = connect_account(ConnectIn(method='import', data='z' * 32))
        token = json.loads(job.params)['connection_token']
        with patch.object(tasks.ts, 'check_session', new_callable=AsyncMock, return_value=(False, 'Invalid session', None)):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(len(list(self.db.scalars(select(Account)))), 2)
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'failed')
        self.assertFalse(sessions.connection_path(token).exists())

    def test_connect_duplicate_preserves_original_session(self):
        from app.routers.tasks import connect_account, ConnectIn
        original = sessions.state_path(self.accounts[0].id).read_text()
        with patch.object(tasks, 'enqueue'):
            job = connect_account(ConnectIn(method='import', data='z' * 32))
        with patch.object(tasks.ts, 'check_session', new_callable=AsyncMock, return_value=(True, 'OK', self.accounts[0].username)):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(sessions.state_path(self.accounts[0].id).read_text(), original)
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'failed')
        self.assertIn('уже добавлен', self.db.get(TaskRun, job.id).error)

    def test_cancel_queued_connection_removes_secret_file(self):
        from app.routers.tasks import connect_account, ConnectIn
        with patch.object(tasks, 'enqueue'):
            job = connect_account(ConnectIn(method='import', data='z' * 32))
        token = json.loads(job.params)['connection_token']
        self.assertTrue(tasks.cancel(job.id))
        self.assertFalse(sessions.connection_path(token).exists())

    def test_interrupted_connection_cleans_up_without_resuming(self):
        from app.routers.tasks import connect_account, ConnectIn
        with patch.object(tasks, 'enqueue'):
            job = connect_account(ConnectIn(method='import', data='z' * 32))
        token = json.loads(job.params)['connection_token']
        tasks.recover_interrupted()
        self.db.expire_all()
        self.assertEqual(self.db.get(TaskRun, job.id).status, 'failed')
        self.assertFalse(sessions.connection_path(token).exists())

    def test_health_check_queues_behind_work_and_deduplicates(self):
        from app.routers.tasks import start_check
        aid = self.accounts[0].id
        self.db.add(TaskRun(kind='auto_comment', status='running', params=json.dumps({'account_id': aid})))
        self.db.commit()
        with patch.object(tasks, 'enqueue') as enqueue:
            first = start_check(aid, self.db)
            second = start_check(aid, self.db)
            self.assertEqual(first.id, second.id)
            self.assertEqual(first.status, 'queued')
            enqueue.assert_called_once()

    def test_health_check_metadata_does_not_claim_account_is_sending(self):
        from app.routers.accounts import get_account, _running_account_ids
        aid = self.accounts[0].id
        self.db.add(TaskRun(kind='check_session', status='running', params=json.dumps({'account_id': aid})))
        self.db.commit()
        result = get_account(aid, self.db)
        self.assertIsNotNone(result.session_task_id)
        self.assertEqual(result.session_task_kind, 'check_session')
        self.assertEqual(result.session_task_status, 'running')
        self.assertEqual(result.current_task_kind, 'check_session')
        self.assertEqual(result.effective_state, 'ready')
        self.assertNotIn(aid, _running_account_ids(self.db, None))

    def test_network_check_error_is_not_reported_as_expired(self):
        from app.routers.tasks import start_check
        self.accounts[0].session_status = 'active'
        self.db.commit()
        with patch.object(tasks, 'enqueue'):
            job = start_check(self.accounts[0].id, self.db)
        with patch.object(tasks.ts, 'check_session', new_callable=AsyncMock, side_effect=RuntimeError('Network unavailable')):
            tasks._execute(job.id)
        self.db.expire_all()
        account = self.db.get(Account, self.accounts[0].id)
        self.assertEqual(account.session_status, 'error')
        self.assertEqual(account.session_error, 'Network unavailable')
        self.assertIsNotNone(account.session_checked_at)

    def test_rejected_session_is_marked_expired(self):
        from app.routers.tasks import start_check
        with patch.object(tasks, 'enqueue'):
            job = start_check(self.accounts[0].id, self.db)
        with patch.object(tasks.ts, 'check_session', new_callable=AsyncMock, return_value=(False, 'Session expired', None)):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Account, self.accounts[0].id).session_status, 'expired')

    def test_editor_updates_general_settings_and_sets_atomically(self):
        from app.routers.accounts import update_account
        from app.schemas import AccountUpdate
        result = update_account(self.accounts[0].id, AccountUpdate(daily_reply_limit=35, text_set_ids=[]), self.db)
        self.assertEqual(result.daily_reply_limit, 35)
        self.assertEqual(result.text_set_ids, [])
        with self.assertRaises(HTTPException):
            update_account(self.accounts[0].id, AccountUpdate(daily_reply_limit=40, text_set_ids=[99999]), self.db)
        self.db.rollback()
        self.assertEqual(self.db.get(Account, self.accounts[0].id).daily_reply_limit, 35)

    def test_inherited_proxy_keeps_editable_fields_separate_from_effective_host(self):
        from app.routers.accounts import get_account
        with patch.object(tasks.app_settings, 'search_proxy', return_value='http://global.test:8080'):
            result = get_account(self.accounts[0].id, self.db)
        self.assertEqual(result.proxy_host, 'global.test:8080')
        self.assertIsNone(result.proxy)
        self.assertIsNone(result.proxy_label)

    def test_monitor_sorts_problems_and_does_not_expose_credentials(self):
        from app.services import session_monitor as monitor
        now = datetime.now()
        self.accounts[0].session_status = 'active'
        self.accounts[0].session_checked_at = now
        self.accounts[0].proxy = 'http://login:secret@example.test:8000'
        self.accounts[1].session_status = 'expired'
        other = Account(username='network_error', session_status='error', session_error='Timeout', session_checked_at=now)
        self.db.add(other)
        self.db.commit()
        sessions.state_path(other.id).write_text('{}')
        with patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15):
            result = monitor.snapshot(self.db, now)
        self.assertEqual([a['tone'] for a in result['accounts']], ['red', 'yellow', 'green'])
        self.assertEqual(result['counts'], {'red':1,'yellow':1,'green':1})
        self.assertNotIn('secret', str(result))
        self.assertNotIn('proxy', result['accounts'][0])

    def test_monitor_paused_account_can_still_be_green(self):
        from app.services import session_monitor as monitor
        a = self.accounts[0]
        a.status, a.session_status, a.session_checked_at = 'paused', 'active', datetime.now()
        self.db.commit()
        with patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15):
            row = next(v for v in monitor.snapshot(self.db)['accounts'] if v['id'] == a.id)
        self.assertEqual(row['tone'], 'green')
        self.assertEqual(row['activity'], 'На паузе')

    def test_monitor_stale_confirmation_is_yellow_and_missing_file_is_red(self):
        from app.services import session_monitor as monitor
        from datetime import timedelta
        a = self.accounts[0]
        a.session_status, a.session_checked_at = 'active', datetime.now() - timedelta(minutes=31)
        self.db.commit()
        with patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15):
            self.assertEqual(monitor.health(a, datetime.now(), 15), ('yellow', 'Давно не проверялся'))
            sessions.state_path(a.id).unlink()
            self.assertEqual(monitor.health(a, datetime.now(), 15), ('red', 'Не подключён'))

    def test_monitor_due_checks_are_deduplicated_and_use_normal_queue(self):
        from app.services import session_monitor as monitor
        from datetime import timedelta
        from threading import Event
        now = datetime.now()
        for a in self.accounts:
            a.session_status, a.session_checked_at = 'active', now - timedelta(minutes=16)
        self.accounts[1].status = 'paused'
        self.db.add(TaskRun(kind='auto_comment', status='running', params=json.dumps({'account_id':self.accounts[0].id})))
        self.db.commit()
        with patch.object(monitor, 'SessionLocal', self.factory), patch.object(monitor, '_stop', Event()), \
             patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15), patch.object(tasks, 'enqueue') as enqueue:
            created = monitor.enqueue_due(now)
            self.assertEqual(len(created), 2)
            self.assertEqual(monitor.enqueue_due(now), [])
            self.assertEqual(enqueue.call_count, 2)
        self.db.expire_all()
        for tid in created:
            job = self.db.get(TaskRun, tid)
            self.assertEqual(job.status, 'queued')
            self.assertTrue(json.loads(job.params)['automatic'])

    def test_monitor_disabled_and_recent_failures_do_not_spam_checks(self):
        from app.services import session_monitor as monitor
        from threading import Event
        for a in self.accounts:
            a.session_status, a.session_checked_at = 'error', datetime.now()
        self.db.commit()
        with patch.object(monitor, 'SessionLocal', self.factory), patch.object(monitor, '_stop', Event()), \
             patch.object(monitor.settings, 'monitor_interval_minutes', return_value=0), patch.object(tasks, 'enqueue') as enqueue:
            self.assertEqual(monitor.enqueue_due(), [])
            enqueue.assert_not_called()
        with patch.object(monitor, 'SessionLocal', self.factory), patch.object(monitor, '_stop', Event()), \
             patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15), patch.object(tasks, 'enqueue') as enqueue:
            self.assertEqual(monitor.enqueue_due(), [])
            enqueue.assert_not_called()

    def test_monitor_skips_unconnected_and_banned_accounts(self):
        from app.services import session_monitor as monitor
        from threading import Event
        self.accounts[0].status = 'banned'
        sessions.state_path(self.accounts[1].id).unlink()
        self.db.commit()
        with patch.object(monitor, 'SessionLocal', self.factory), patch.object(monitor, '_stop', Event()), \
             patch.object(monitor.settings, 'monitor_interval_minutes', return_value=15), patch.object(tasks, 'enqueue') as enqueue:
            self.assertEqual(monitor.enqueue_due(), [])
            enqueue.assert_not_called()

    def test_monitor_pending_check_keeps_last_confirmed_color(self):
        from app.services import session_monitor as monitor
        a = self.accounts[0]
        a.session_status, a.session_checked_at = 'active', datetime.now()
        self.db.add(TaskRun(kind='check_session', status='queued', params=json.dumps({'account_id':a.id})))
        self.db.commit()
        row = next(r for r in monitor.snapshot(self.db)['accounts'] if r['id'] == a.id)
        self.assertEqual(row['tone'], 'green')
        self.assertTrue(row['checking'])
        self.assertEqual(row['activity'], 'В очереди')

    def test_monitor_handles_old_malformed_task_params(self):
        from app.services import session_monitor as monitor
        self.db.add(TaskRun(kind='check_session', status='running', params='invalid JSON'))
        self.db.commit()
        self.assertEqual(len(monitor.snapshot(self.db)['accounts']), 2)

    def test_publication_session_errors_immediately_update_monitor(self):
        from app.services import session_monitor as monitor
        aid = self.accounts[0].id
        @asynccontextmanager
        async def ctx(**kw):
            yield MagicMock()
        for index, (code, status, tone) in enumerate([
            ('session_expired', 'expired', 'red'), ('captcha', 'error', 'yellow'), ('rate_limit', 'error', 'yellow')
        ]):
            account = self.db.get(Account, aid)
            account.session_status = 'active'
            self.db.commit()
            did = self.prepare([self.videos[index].id], [aid])['created'][0]['id']
            with patch.object(tasks, 'enqueue'):
                job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=[did]))['tasks'][0]
            with patch.object(tasks.ts, 'open_context', ctx), patch.object(tasks, 'verify_context', new_callable=AsyncMock), \
                 patch.object(tasks.ts, 'post_comment_on_video', new_callable=AsyncMock, return_value=CommentResult(False, code, code)):
                tasks._execute(job.id)
            self.db.expire_all()
            self.assertEqual(self.db.get(Account, aid).session_status, status)
            row = next(v for v in monitor.snapshot(self.db)['accounts'] if v['id'] == aid)
            self.assertEqual(row['tone'], tone)

    def test_temporary_video_unavailability_preserves_draft_and_continues_batch(self):
        aid = self.accounts[0].id
        dids = [self.prepare([video.id], [aid])['created'][0]['id'] for video in self.videos[:2]]
        with patch.object(tasks, 'enqueue'):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=dids))['tasks'][0]

        @asynccontextmanager
        async def ctx(**kw):
            yield MagicMock()

        with patch.object(tasks.ts, 'open_context', ctx), patch.object(tasks, 'verify_context', new_callable=AsyncMock), \
             patch.object(tasks.ts, 'post_comment_on_video', new_callable=AsyncMock,
                          return_value=CommentResult(False, 'Видео недоступно', 'page_unavailable')) as post:
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(post.await_count, 2)
        self.assertEqual(tasks.task_summary(self.db, job.id)["errors"], 2)
        for did in dids:
            draft = self.db.get(Draft, did)
            self.assertEqual(draft.status, 'draft')
            self.assertFalse(draft.sending)


    def test_mixed_local_errors_continue_and_summary_survives_restart(self):
        aid = self.accounts[0].id
        dids = [self.prepare([v.id], [aid])["created"][0]["id"] for v in self.videos[:3]]
        with patch.object(tasks, "enqueue"):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=dids))["tasks"][0]

        @asynccontextmanager
        async def ctx(**kwargs):
            yield MagicMock()

        outcomes = [
            CommentResult(False, "Ограничено между регионами (код 10204)", "region_restricted"),
            CommentResult(True, "TikTok принял комментарий"),
            CommentResult(False, "HTTP 503 after retries", "navigation_failed"),
        ]
        with patch.object(tasks.ts, "open_context", ctx), \
             patch.object(tasks, "verify_context", new_callable=AsyncMock), \
             patch.object(tasks.ts, "post_comment_on_video", new_callable=AsyncMock, side_effect=outcomes) as post, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(post.await_count, 3)
        self.assertEqual(self.db.get(TaskRun, job.id).status, "partial")
        self.assertEqual([self.db.get(Draft, d).status for d in dids], ["draft", "sent", "draft"])
        self.assertEqual([self.db.get(Draft, d).error_code for d in dids], ["region_restricted", None, "navigation_failed"])
        expected = dict(published=1, skipped=0, errors=2, review=0, unprocessed=0, total=3)
        self.assertEqual(tasks.task_summary(self.db, job.id), expected)
        from app.routers import tasks as task_router
        self.assertEqual(task_router.get_task_summary(job.id, self.db), expected)
        tasks.recover_interrupted()
        with self.factory() as reopened:
            self.assertEqual(tasks.task_summary(reopened, job.id), expected)
            self.assertEqual(reopened.get(Draft, dids[0]).status, "draft")

        # Generic automatic runs must not retry a documented regional block.
        with patch.object(tasks, "enqueue"):
            next_job = tasks.submit_batch(AutoCommentTaskIn(account_id=aid))["tasks"][0]
        self.assertEqual(tasks.task_summary(self.db, next_job.id)["total"], 1)
        self.assertEqual(self.db.get(Draft, dids[0]).last_task_id, job.id)

    def test_account_network_threshold_stops_only_this_account(self):
        aid, other = self.accounts[0].id, self.accounts[1].id
        first = [self.prepare([v.id], [aid])["created"][0]["id"] for v in self.videos[:3]]
        second = self.prepare([self.videos[3].id], [other])["created"][0]["id"]
        with patch.object(tasks, "enqueue"):
            jobs = tasks.submit_batch(AutoCommentTaskIn(draft_ids=first + [second]))["tasks"]
        job_by_account = {j.params["account_id"]: j for j in jobs}

        @asynccontextmanager
        async def ctx(**kwargs):
            yield MagicMock()

        with patch.object(tasks.ts, "open_context", ctx), \
             patch.object(tasks, "verify_context", new_callable=AsyncMock), \
             patch.object(tasks.app_settings, "network_failure_threshold", return_value=2), \
             patch.object(tasks.ts, "post_comment_on_video", new_callable=AsyncMock,
                          side_effect=[CommentResult(False, "HTTP 503", "navigation_failed")] * 2 +
                                      [CommentResult(True, "OK")]) as post, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            tasks._execute(job_by_account[aid].id)
            tasks._execute(job_by_account[other].id)
        self.db.expire_all()
        self.assertEqual(post.await_count, 3)
        self.assertEqual(self.db.get(TaskRun, job_by_account[aid].id).status, "failed")
        self.assertEqual(tasks.task_summary(self.db, job_by_account[aid].id)["unprocessed"], 1)
        self.assertEqual(tasks.task_summary(self.db, job_by_account[aid].id)["errors"], 2)
        self.assertEqual(self.db.get(TaskRun, job_by_account[other].id).status, "done")
        self.assertEqual(self.db.get(Draft, second).status, "sent")
        self.assertTrue(sessions.state_path(other).exists())

    def test_uncertain_after_click_requires_manual_review_and_blocks_resend(self):
        aid = self.accounts[0].id
        dids = [self.prepare([v.id], [aid])["created"][0]["id"] for v in self.videos[:2]]
        with patch.object(tasks, "enqueue"):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=dids))["tasks"][0]

        @asynccontextmanager
        async def ctx(**kwargs):
            yield MagicMock()

        with patch.object(tasks.ts, "open_context", ctx), \
             patch.object(tasks, "verify_context", new_callable=AsyncMock), \
             patch.object(tasks.ts, "post_comment_on_video", new_callable=AsyncMock,
                          return_value=CommentResult(False, "After click: uncertain", "uncertain")) as post:
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(post.await_count, 1)
        self.assertEqual(self.db.get(Draft, dids[0]).status, "review")
        self.assertEqual(self.db.get(Draft, dids[1]).status, "draft")
        self.assertEqual(tasks.task_summary(self.db, job.id)["review"], 1)
        self.assertEqual(tasks.task_summary(self.db, job.id)["unprocessed"], 1)
        tasks.recover_interrupted()
        self.assertEqual(self.db.get(Draft, dids[0]).status, "review")
        with patch.object(tasks, "enqueue"):
            with self.assertRaises(HTTPException):
                tasks.submit_batch(AutoCommentTaskIn(draft_ids=[dids[0]]))

    def test_permanent_video_failure_is_skipped_without_losing_next_comment(self):
        aid = self.accounts[0].id
        dids = [self.prepare([v.id], [aid])["created"][0]["id"] for v in self.videos[:2]]
        with patch.object(tasks, "enqueue"):
            job = tasks.submit_batch(AutoCommentTaskIn(draft_ids=dids))["tasks"][0]

        @asynccontextmanager
        async def ctx(**kwargs):
            yield MagicMock()

        with patch.object(tasks.ts, "open_context", ctx), \
             patch.object(tasks, "verify_context", new_callable=AsyncMock), \
             patch.object(tasks.ts, "post_comment_on_video", new_callable=AsyncMock,
                          side_effect=[CommentResult(False, "HTTP 404", "video_unavailable"),
                                       CommentResult(True, "OK")]), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            tasks._execute(job.id)
        self.db.expire_all()
        self.assertEqual(self.db.get(TaskRun, job.id).status, "partial")
        self.assertEqual([self.db.get(Draft, d).status for d in dids], ["skipped", "sent"])
        self.assertEqual(tasks.task_summary(self.db, job.id)["skipped"], 1)

    def test_migration_is_idempotent_and_backs_up(self):
        path = self.root / 'legacy.db'
        old = create_engine(f'sqlite:///{path}')
        self.addCleanup(old.dispose)
        with old.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE accounts (id INTEGER PRIMARY KEY, username VARCHAR(64), display_name VARCHAR(128), status VARCHAR(16), daily_reply_limit INTEGER, notes TEXT, created_at DATETIME, updated_at DATETIME)')
            conn.exec_driver_sql("INSERT INTO accounts (id,username,status,daily_reply_limit) VALUES (1,'legacy','active',20)")
            conn.exec_driver_sql('CREATE TABLE reply_templates (id INTEGER PRIMARY KEY, name VARCHAR(128), body TEXT, created_at DATETIME)')
            conn.exec_driver_sql("INSERT INTO reply_templates (id,name,body) VALUES (1,'Legacy','Hello')")
        ensure_schema(old)
        ensure_schema(old)
        with sessionmaker(old)() as db:
            self.assertEqual(len(list(db.scalars(select(TextSet)))), 1)
            self.assertEqual(db.get(Account, 1).username, 'legacy')
        self.assertEqual(len(list((self.root / 'backups').glob('*/app.db'))), 1)


if __name__ == '__main__':
    unittest.main()
