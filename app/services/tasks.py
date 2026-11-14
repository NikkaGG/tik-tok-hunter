"""Bounded browser scheduler. Waiting accounts never occupy a worker slot."""
import asyncio
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from threading import RLock
from functools import wraps
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload
from fastapi import HTTPException
from app.config import SEARCH_STATE_PATH
from app.database import SessionLocal
from app.models import Account, Draft, DraftStatus, TaskRun, TaskStatus, Video, TextUse
from app.services import settings as app_settings, text_library
from app.services.sessions import browser_binding, state_path, verify_context, connection_path
from app.services.logs import log_event
from app.workers import tiktok_search as ts
from app.workers.captcha_manager import CaptchaState

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="browser")
coordinator = RLock()
_pending = []
_active = {}  # task id -> account id
_loops = {}
_stopping = False

# Settable in data/settings.json; unrelated accounts have independent failures.
LOCAL_VIDEO_ERRORS = {"page_unavailable", "region_restricted", "navigation_failed",
                      "access_denied", "video_unavailable", "comments_disabled",
                      "input_not_found", "button_not_found", "unknown", "api_error"}


def _captcha_progress(task_id, event):
    kinds = {'unknown': 'не определён', 'rotate': 'поворот', 'slide': 'пазл',
             'shapes': 'фигуры', 'icon': 'выбор объектов', 'grid': 'сетка',
             'voice': 'аудио', 'geetest': 'GeeTest', 'arkose': 'Arkose', 'hcaptcha': 'hCaptcha'}
    messages = {
        CaptchaState.DETECTED: 'Обнаружена CAPTCHA',
        CaptchaState.CLASSIFIED: f'Тип CAPTCHA: {kinds.get(event.kind, "не определён")}',
        CaptchaState.SOLVING: f'Попытка локального решения CAPTCHA{f" №{event.detail}" if event.detail else ""}',
        CaptchaState.AWAITING_USER: 'Пройдите CAPTCHA в окне браузера',
        CaptchaState.VERIFYING: 'Проверяется результат CAPTCHA',
        CaptchaState.SOLVED: 'CAPTCHA подтверждена',
        CaptchaState.RESUMED: 'Работа продолжается',
        CaptchaState.FAILED: f'CAPTCHA: {event.detail}',
    }
    message = messages[event.state]
    _set(task_id, result=message)
    log_event('tasks', f'Задача #{task_id}: {message}', level='WARNING' if event.state == CaptchaState.FAILED else 'INFO')


def coordinated(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with coordinator:
            return fn(*args, **kwargs)
    return wrapped


def account_busy(db, account_id):
    if account_id in _active.values():
        return True
    return any(json.loads(t.params or "{}").get("account_id") == account_id for t in
        db.scalars(select(TaskRun).where(TaskRun.status.in_(["queued", "running"]))))


def ensure_idle(db, account_id):
    if account_busy(db, account_id):
        raise HTTPException(409, "У аккаунта есть активная или ожидающая задача")


def _dispatch():
    # Called only with coordinator held. Enforce both total and per-account limits.
    if _stopping:
        return
    for item in list(_pending):
        if len(_active) >= app_settings.browser_limit():
            break
        task_id, account_id = item
        if account_id is not None and account_id in _active.values():
            continue
        _pending.remove(item)
        _active[task_id] = account_id
        future = _executor.submit(_execute, task_id)
        future.add_done_callback(lambda f, tid=task_id: _completed(tid))


def _completed(task_id):
    with coordinator:
        _active.pop(task_id, None)
        _loops.pop(task_id, None)
        _dispatch()


@coordinated
def submit(kind, params=None):
    params = dict(params or {})
    with SessionLocal() as db:
        task = TaskRun(kind=kind, params=json.dumps(params, ensure_ascii=False))
        db.add(task)
        db.commit()
        db.refresh(task)
    enqueue(task.id, params.get("account_id"))
    return task


@coordinated
def request_session_check(account_id, *, automatic=False):
    with SessionLocal() as db:
        if not db.get(Account, account_id):
            raise HTTPException(404, "Аккаунт не найден")
        for pending in db.scalars(select(TaskRun).where(TaskRun.kind.in_(["check_session", "login"]),
                                  TaskRun.status.in_(["queued", "running"]))):
            if json.loads(pending.params or "{}").get("account_id") == account_id:
                if pending.kind == "check_session":
                    return pending
                raise HTTPException(409, "Дождитесь окончания входа в этот аккаунт")
    return submit("check_session", {"account_id": account_id, "automatic": automatic})


def enqueue(task_id, account_id):
    with coordinator:
        _pending.append((task_id, account_id))
        _dispatch()


def recover_interrupted():
    with SessionLocal() as db:
        connection_ids = list(db.scalars(select(TaskRun.id).where(TaskRun.kind == "connect", TaskRun.status.in_(["queued", "running"]))))
        db.execute(update(TaskRun).where(TaskRun.status.in_(["queued", "running"])).values(
            status="failed", error="Прервано перезапуском приложения", finished_at=datetime.now()))
        for d in db.scalars(select(Draft).where(Draft.task_id.is_not(None))):
            if d.sending:
                d.status, d.result, d.error_code = "review", "Прервано во время публикации. Проверьте вручную", "uncertain"
                text_library.finish(db, d, "review")
            d.sending, d.task_id = 0, None
        db.commit()
    for tid in connection_ids:
        _cleanup_connection(tid)


def shutdown():
    global _stopping
    with coordinator:
        _stopping = True
        ids = list(_active)
        ids.extend(tid for tid, _ in _pending)
    for tid in ids:
        cancel(tid)
    _executor.shutdown(wait=False, cancel_futures=True)


@coordinated
def cancel(task_id):
    with SessionLocal() as db:
        task = db.get(TaskRun, task_id)
        if not task or task.status not in ("queued", "running"):
            return False
        task.status, task.error, task.finished_at = "cancelled", "Отменено пользователем", datetime.now()
        if task_id not in _active:
            db.execute(update(Draft).where(Draft.task_id == task_id).values(task_id=None))
        db.commit()
    _pending[:] = [p for p in _pending if p[0] != task_id]
    if task_id not in _active:
        _cleanup_connection(task_id)
    if task_id in _loops:
        loop, coro_task = _loops[task_id]
        loop.call_soon_threadsafe(coro_task.cancel)
    _dispatch()
    return True


def is_cancelled(task_id):
    with SessionLocal() as db:
        task = db.get(TaskRun, task_id)
        return not task or task.status == "cancelled"


def _set(task_id, **fields):
    with SessionLocal() as db:
        db.execute(update(TaskRun).where(TaskRun.id == task_id, TaskRun.status != "cancelled").values(**fields))
        db.commit()


def task_summary(db, task_id):
    """Recalculate task counts from durable draft records, including after restarts."""
    counts = {"published": 0, "skipped": 0, "errors": 0, "review": 0, "unprocessed": 0}
    for state, code in db.execute(select(Draft.status, Draft.error_code).where(Draft.last_task_id == task_id)):
        if state == "sent":
            counts["published"] += 1
        elif state == "skipped":
            counts["skipped"] += 1
        elif state == "review":
            counts["review"] += 1
        elif code:
            counts["errors"] += 1
        else:
            counts["unprocessed"] += 1
    counts["total"] = sum(counts.values())
    return counts


def task_summary_text(counts):
    return (f"Отправлено: {counts['published']}, пропущено: {counts['skipped']}, "
            f"ошибок: {counts['errors']}, проверить: {counts['review']}, "
            f"не обработано: {counts['unprocessed']}")


def persisted_summary(task_id):
    with SessionLocal() as db:
        return task_summary(db, task_id)


def _run_coro(coro, task_id=None):
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    coro_task = loop.create_task(coro)
    if task_id is not None:
        with coordinator:
            _loops[task_id] = (loop, coro_task)
            if is_cancelled(task_id):
                coro_task.cancel()
    try:
        return loop.run_until_complete(coro_task)
    finally:
        with coordinator:
            _loops.pop(task_id, None)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _execute(task_id):
    token = None
    candidate_path = None
    account_id = None
    kind = None
    try:
        if is_cancelled(task_id):
            return
        with SessionLocal() as db:
            task = db.get(TaskRun, task_id)
            kind, params = task.kind, json.loads(task.params)
            account_id = params.get("account_id")
            account = db.get(Account, account_id) if account_id else None
            if kind not in ("test_proxy", "connect") and account is None:
                raise ts.SessionError("Выберите аккаунт для задачи")
            if account:
                path = state_path(account.id)
                # Legacy file is a candidate only; it is never assigned without identity verification.
                legacy = not path.exists() and SEARCH_STATE_PATH.exists() and kind == "check_session"
                if legacy:
                    import shutil
                    candidate_path = path.with_suffix(".candidate.json")
                    shutil.copy2(SEARCH_STATE_PATH, candidate_path)
                token = browser_binding.set({"path": candidate_path if legacy else path,
                    "proxy": account.proxy or app_settings.search_proxy(), "username": account.username})
            if kind == "connect":
                token = browser_binding.set({"path": connection_path(params["connection_token"]),
                    "proxy": app_settings.search_proxy(), "username": None})
        _set(task_id, status="running")
        if kind == "connect":
            result, found = _run_connect(task_id, params)
        elif kind == "auto_comment":
            result, found = _run_auto_comment(task_id, params)
        elif kind == "search":
            result, found = _run_search(task_id, params)
        elif kind in ("login", "check_session"):
            if kind == "login":
                _run_coro(ts.login_interactive(), task_id)
            ok, result, username = _run_coro(ts.check_session(), task_id)
            if not ok:
                raise ts.SessionExpiredError(result)
            if not username:
                raise ts.SessionError("TikTok не подтвердил имя аккаунта. Повторите проверку")
            binding = browser_binding.get()
            if username.casefold() != binding["username"].casefold():
                raise ts.SessionError("Сессия принадлежит другому аккаунту")
            if candidate_path:
                candidate_path.replace(state_path(account_id))
                if SEARCH_STATE_PATH.exists():
                    SEARCH_STATE_PATH.replace(SEARCH_STATE_PATH.with_name("legacy_state.migrated.json"))
            with SessionLocal() as db:
                account = db.get(Account, account_id)
                account.session_status, account.session_error = "active", None
                account.session_checked_at = datetime.now()
                db.commit()
            found = 1
        elif kind == "test_proxy":
            result, found = _run_coro(ts.test_proxy(), task_id), 0
        else:
            raise ValueError("Неизвестная задача")
        if kind == "auto_comment":
            summary = persisted_summary(task_id)
            result = task_summary_text(summary)
            final_status = "partial" if any(summary[k] for k in ("skipped", "errors", "review", "unprocessed")) else "done"
        else:
            final_status = "done"
        _set(task_id, status=final_status, result=result, found=found, finished_at=datetime.now())
    except BaseException as e:
        if not isinstance(e, (Exception, asyncio.CancelledError)):
            raise
        message = str(e) or "Операция отменена"
        if account_id and (isinstance(e, (ts.SessionError, ts.CaptchaError)) or kind in ("check_session", "login")):
            with SessionLocal() as db:
                account = db.get(Account, account_id)
                if account:
                    account.session_status = "expired" if isinstance(e, ts.SessionExpiredError) else "error"
                    account.session_error = message
                    account.session_checked_at = datetime.now()
                    db.commit()
        if kind == "auto_comment":
            message += " · " + task_summary_text(persisted_summary(task_id))
        _set(task_id, status="failed", error=message, finished_at=datetime.now())
        log_event("tasks", f"Задача #{task_id}: {message}", level="WARNING")
    finally:
        with SessionLocal() as db:
            for d in db.scalars(select(Draft).where(Draft.task_id == task_id)):
                if d.sending:
                    d.status, d.result, d.error_code = "review", "Результат не подтверждён. Проверьте вручную", "uncertain"
                    text_library.finish(db, d, "review")
                d.sending, d.task_id = 0, None
            db.commit()
        _cleanup_connection(task_id)
        if candidate_path:
            candidate_path.unlink(missing_ok=True)
        if token is not None:
            browser_binding.reset(token)


def _cleanup_connection(task_id):
    with SessionLocal() as db:
        task = db.get(TaskRun, task_id)
        if task and task.kind == "connect":
            token = json.loads(task.params).get("connection_token")
            if token:
                connection_path(token).unlink(missing_ok=True)


def _run_connect(task_id, params):
    """Discover identity before creating an account; never replace a live session."""
    from app.schemas import AccountCreate
    if params["method"] == "login":
        _run_coro(ts.login_interactive(), task_id)
    ok, message, username = _run_coro(ts.check_session(), task_id)
    if not ok or not username:
        raise ts.SessionError(message or "TikTok не подтвердил аккаунт")
    username = AccountCreate(username=username).username
    path = browser_binding.get()["path"]
    if not path.exists():
        raise ts.SessionError("Сессия не сохранена. Повторите вход")
    promoted = None
    # Atomic with account edits, disconnect and task submission; discovery itself needs no account lock.
    with coordinator, SessionLocal() as db:
        if is_cancelled(task_id):
            raise RuntimeError("Подключение отменено")
        text_library.begin_write(db)
        account = db.scalar(select(Account).where(Account.username == username))
        if account:
            ensure_idle(db, account.id)
            if state_path(account.id).exists():
                raise RuntimeError(f"@{username} уже добавлен. Переподключите его в настройках этого аккаунта")
        else:
            account = Account(username=username)
            db.add(account)
            db.flush()
        account.session_status, account.session_error = "active", None
        account.session_checked_at = datetime.now()
        task = db.get(TaskRun, task_id)
        updated = dict(params, account_id=account.id, username=username)
        task.params = json.dumps(updated, ensure_ascii=False)
        task.status, task.result, task.found = "done", f"Аккаунт @{username} добавлен", 1
        task.finished_at = datetime.now()
        try:
            promoted = state_path(account.id)
            path.replace(promoted)
            db.commit()
        except BaseException:
            if promoted and promoted.exists():
                promoted.replace(path)
            raise
        if task_id in _active:
            _active[task_id] = account.id
        if not app_settings.search_account_id():
            app_settings.update_values(search_account_id=account.id)
    return f"Аккаунт @{username} добавлен", 1


def _save_videos(query: str, videos: list[ts.FoundVideo]) -> list[int]:
    """Сохраняет новые ролики, возвращает их id в базе."""
    if not videos:
        return []
    with SessionLocal() as db:
        text_library.begin_write(db)
        existing = set(db.scalars(select(Video.video_id).where(Video.video_id.in_([v.video_id for v in videos]))))
        new = [
            Video(video_id=v.video_id, url=v.url, author=v.author, description=v.description,
                  views=v.views, query=query)
            for v in videos if v.video_id not in existing
        ]
        db.add_all(new)
        db.commit()
        return [v.id for v in new]


def _run_search(task_id: int, params: dict) -> tuple[str, int]:
    queries, limit = params["queries"], params.get("limit_per_query", 30)
    visible = params.get("visible", False)
    template_id = params.get("template_id")
    stats = {"seen": 0, "new": 0, "drafts": 0, "errors": 0, "last_error": ""}

    def on_results(query: str, videos: list[ts.FoundVideo]) -> None:
        new_ids = _save_videos(query, videos)
        stats["seen"] += len(videos)
        stats["new"] += len(new_ids)

        _set(task_id, found=stats["new"])
        log_event("search", f"«{query}»: найдено {len(videos)}, новых {len(new_ids)}")

    def on_error(query: str, e: Exception) -> None:
        stats["errors"] += 1
        stats["last_error"] = str(e)
        level = "WARNING" if isinstance(e, ts.EmptyResultError) else "ERROR"
        log_event("search", f"«{query}»: {e}", level=level)

    # По логам: с тем же аккаунтом без прокси поиск работал, а через прокси TikTok отдавал пустую выдачу.
    proxy_hint = (" Включён прокси: TikTok часто не отдаёт через него выдачу поиска (например, если сессия "
                  "из другой страны). Отключите прокси в «Настройках» и повторите, для поиска он не обязателен."
                  if app_settings.search_proxy() else "")
    async def search_with_status():
        token = ts.captcha_status.set(lambda event: _captcha_progress(task_id, event))
        visible_token = ts.captcha_visible.set(visible)
        try:
            await ts.search_many(queries, limit, on_results, on_error, visible=visible)
        finally:
            ts.captcha_visible.reset(visible_token)
            ts.captcha_status.reset(token)
    try:
        _run_coro(search_with_status(), task_id)
    except ts.CaptchaError as e:
        raise ts.CaptchaError(f"{e}{proxy_hint}") from e
    if stats["errors"] == len(queries):
        raise RuntimeError((stats["last_error"] if len(queries) == 1 else
                            f"Ни один запрос не дал результатов. Последняя ошибка: {stats['last_error']}") + proxy_hint)
    result = f"Запросов: {len(queries)}, найдено: {stats['seen']}, новых: {stats['new']}"
    if template_id:
        result += f", черновиков: {stats['drafts']}"
    if stats["errors"]:
        result += f", ошибок: {stats['errors']}"
    return result, stats["new"]



def _run_auto_comment(task_id, params):
    account_id = params["account_id"]
    visible = params.get("visible", False)
    binding = browser_binding.get()
    if not binding["path"].exists():
        raise ts.SessionError("У аккаунта нет сессии. Подключите его")
    sent, skipped, errors, review, consecutive_network = 0, 0, 0, 0, 0

    async def process():
        nonlocal sent, skipped, errors, review, consecutive_network
        _set(task_id, result="Запуск браузера")
        async with ts.open_context(headless=not visible) as ctx:
            ts.comment_visible.set(visible)
            ts.captcha_status.set(lambda event: _captcha_progress(task_id, event))
            _set(task_id, result="Открывается TikTok и проверяется аккаунт")
            await verify_context(ctx, binding["username"])
            ts.session_verified.set(True)
            with SessionLocal() as db:
                ids = list(db.scalars(select(Draft.id).where(Draft.task_id == task_id).order_by(Draft.id)))
            for index, draft_id in enumerate(ids):
                if is_cancelled(task_id):
                    break
                _set(task_id, result=f"Ролик {index + 1} из {len(ids)} · отправлено: {sent}, пропущено: {skipped}, ошибок: {errors}, проверить: {review}, осталось: {len(ids) - index}")
                # Verify immediately before every publication, including after captcha/login changes.
                ts.session_verified.set(False)
                await verify_context(ctx, binding["username"])
                ts.session_verified.set(True)
                with SessionLocal() as db:
                    text_library.begin_write(db)
                    d, account = db.get(Draft, draft_id), db.get(Account, account_id)
                    if not d or d.task_id != task_id or d.status != "draft" or d.account_id != account_id:
                        continue
                    if account.status not in ("active", "working"):
                        raise RuntimeError("Аккаунт приостановлен или ограничен")
                    start = datetime.combine(datetime.now().date(), time.min)
                    # Durable history also counts deleted comments and unresolved publications.
                    used = db.scalar(select(func.count()).select_from(TextUse).where(
                        TextUse.account_id == account_id,
                        ((TextUse.state == "sent") & (TextUse.sent_at >= start)) |
                        ((TextUse.state == "review") & (TextUse.sent_at >= start)))) or 0
                    if account.daily_reply_limit and used >= account.daily_reply_limit:
                        raise RuntimeError("Дневной лимит аккаунта исчерпан")
                    if text_library.pair_exists(db, d.video_id, account_id, d.id):
                        raise RuntimeError("Под этим роликом аккаунт уже публиковал комментарий")
                    d.sending = 1
                    url, content = d.video.url, d.text
                    db.commit()
                result = await ts.post_comment_on_video(ctx, url, content, visible=visible)
                with SessionLocal() as db:
                    d = db.get(Draft, draft_id)
                    d.sending, d.result = 0, result.message
                    d.error_code = None if result.success else (result.error_code or "unknown")
                    if result.success:
                        d.status, d.sent_at = "sent", datetime.now()
                        d.video.status = "replied"
                        text_library.finish(db, d, "sent")
                        sent += 1
                    elif result.error_code == "uncertain":
                        d.status = "review"
                        review += 1
                        text_library.finish(db, d, "review")
                    elif result.error_code in ("video_unavailable", "comments_disabled"):
                        d.status = "skipped"
                        text_library.release(db, d.id)
                        skipped += 1
                    else:
                        errors += 1
                    db.commit()
                _set(task_id, found=sent, result=task_summary_text(persisted_summary(task_id)))
                if result.error_code == "navigation_failed":
                    consecutive_network += 1
                else:
                    consecutive_network = 0
                if consecutive_network >= app_settings.network_failure_threshold():
                    raise ts.SessionError(f"Сетевая ошибка у {consecutive_network} разных роликов подряд. Проверьте подключение и прокси аккаунта")
                if not result.success and result.error_code == "session_expired":
                    raise ts.SessionExpiredError(result.message)
                if not result.success and result.error_code in ("captcha", "rate_limit"):
                    raise ts.SessionError(result.message)
                if not result.success and result.error_code not in LOCAL_VIDEO_ERRORS:
                    raise RuntimeError(result.message)
                if index < len(ids) - 1:
                    _set(task_id, result=f"Пауза перед следующим роликом · отправлено: {sent}, пропущено: {skipped}")
                    await asyncio.sleep(random.uniform(params.get("delay_min_s", 20), params.get("delay_max_s", 45)))
    _run_coro(process(), task_id)
    return f"Отправлено: {sent}, пропущено: {skipped}, ошибок: {errors}, проверить: {review}", sent


@coordinated
def submit_batch(data):
    from app.schemas import TaskOut
    with SessionLocal() as db:
        text_library.begin_write(db)
        q = select(Draft).where(Draft.status == "draft")
        skipped_unassigned = 0
        skipped_claimed = 0
        if data.draft_ids is None:
            # "All assigned accounts" means exactly that; a legacy unassigned
            # draft must not block the rest or silently change ownership.
            if not data.account_id:
                skipped_unassigned = db.scalar(select(func.count()).select_from(Draft).where(
                    Draft.status == 'draft', Draft.account_id.is_(None), Draft.task_id.is_(None))) or 0
                q = q.where(Draft.account_id.is_not(None))
            claimed = select(func.count()).select_from(Draft).where(Draft.status == 'draft', Draft.task_id.is_not(None))
            if data.account_id:
                claimed = claimed.where((Draft.account_id == data.account_id) | Draft.account_id.is_(None))
            else:
                claimed = claimed.where(Draft.account_id.is_not(None))
            skipped_claimed = db.scalar(claimed) or 0
            q = q.where(Draft.task_id.is_(None))
            # A proven regional restriction does not change without user intervention.
            q = q.where((Draft.error_code.is_(None)) | (Draft.error_code != "region_restricted"))
        if data.draft_ids is not None:
            q = q.where(Draft.id.in_(data.draft_ids))
        if data.account_id:
            q = q.where((Draft.account_id == data.account_id) | Draft.account_id.is_(None))
        drafts = list(db.scalars(q.order_by(Draft.id)))
        if data.max_count:
            drafts = drafts[:data.max_count]
        if not drafts:
            raise HTTPException(409, "Нет доступных черновиков с назначенным аккаунтом" if skipped_unassigned else "Нет доступных черновиков для отправки")
        if any(d.task_id for d in drafts):
            raise HTTPException(409, "Часть черновиков уже находится в очереди")
        groups = {}
        pairs = set()
        for d in drafts:
            aid = d.account_id or data.account_id
            if not aid:
                raise HTTPException(409, "Назначьте аккаунты всем выбранным черновикам")
            account = db.get(Account, aid)
            if not account or account.status not in ("active", "working"):
                raise HTTPException(409, "Выбран неактивный аккаунт")
            if not state_path(aid).exists():
                raise HTTPException(409, f"Подключите сессию @{account.username}")
            if text_library.pair_exists(db, d.video_id, aid, d.id):
                raise HTTPException(409, "У аккаунта уже есть комментарий под выбранным роликом")
            if (d.video_id, aid) in pairs:
                raise HTTPException(409, "В выборе повторяется пара ролик/аккаунт")
            pairs.add((d.video_id, aid))
            d.account_id = aid
            groups.setdefault(aid, []).append(d)
        jobs = []
        for aid, items in groups.items():
            params = data.model_dump()
            params.update(account_id=aid, draft_ids=[d.id for d in items])
            task = TaskRun(kind="auto_comment", params=json.dumps(params), result=f"В очереди: {len(items)} комментариев")
            db.add(task)
            db.flush()
            for d in items:
                d.task_id = task.id
                d.last_task_id = task.id
                d.error_code = None
                if not db.scalar(select(TextUse.id).where(TextUse.draft_id == d.id, TextUse.state == "reserved")):
                    text_library.record(db, d)
                db.execute(update(TextUse).where(TextUse.draft_id == d.id, TextUse.state == "reserved").values(account_id=aid))
            jobs.append((task.id, aid))
        db.commit()
        outputs = [TaskOut.model_validate(db.get(TaskRun, tid)) for tid, _ in jobs]
    for tid, aid in jobs:
        enqueue(tid, aid)
    return {"tasks": outputs, "skipped_unassigned": skipped_unassigned, "skipped_in_progress": skipped_claimed}
