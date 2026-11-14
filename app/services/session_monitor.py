"""Local multi-account health snapshot and non-interrupting periodic checks."""
from datetime import datetime, timedelta
from threading import Event, Thread
import json
from fastapi import HTTPException
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Account, TaskRun
from app.services import sessions, tasks, settings
from app.services.logs import log_event

_stop = Event()
_thread = None


def _account_id(job):
    try:
        value = json.loads(job.params or "{}").get("account_id")
        return int(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def health(account, now, interval):
    if account.status == "banned":
        return "red", "Аккаунт отмечен как заблокированный"
    if not sessions.state_path(account.id).exists() or account.session_status == "missing":
        return "red", "Не подключён"
    if account.session_status == "expired":
        return "red", "Сессия истекла"
    if account.session_status == "error":
        return "yellow", "Ошибка проверки: " + (account.session_error or "повторите проверку сессии")
    if account.proxy_status == "error":
        return "yellow", "Проблема с прокси"
    if account.status == "warning":
        return "yellow", "Аккаунт требует внимания"
    if account.session_status != "active" or not account.session_checked_at:
        return "yellow", "Сессия ещё не проверена"
    if now - account.session_checked_at > timedelta(minutes=2 * (interval or 15)):
        return "yellow", "Давно не проверялся"
    return "green", "Сессия активна"


def snapshot(db, now=None):
    now = now or datetime.now()
    interval = settings.monitor_interval_minutes()
    accounts = list(db.scalars(select(Account).order_by(Account.id)))
    pending = {}
    for job in db.scalars(select(TaskRun).where(TaskRun.status.in_(["queued", "running"])).order_by(TaskRun.id)):
        aid = _account_id(job)
        if aid is not None:
            pending.setdefault(aid, []).append(job)
    items = []
    for account in accounts:
        tone, reason = health(account, now, interval)
        jobs = pending.get(account.id, [])
        running = next((j for j in jobs if j.status == "running"), None)
        activity = "На паузе" if account.status == "paused" else None
        if running:
            activity = {"auto_comment": "Отправляет", "search": "Поиск роликов", "login": "Ожидает входа",
                        "check_session": "Проверяется"}.get(running.kind, "В работе")
        elif jobs:
            activity = "В очереди"
        check = next((j for j in jobs if j.kind in ("check_session", "login")), None)
        items.append({"id": account.id, "username": account.username, "tone": tone, "reason": reason,
                      "checked_at": account.session_checked_at, "activity": activity, "detail": account.session_error,
                      "checking": bool(check), "paused": account.status == "paused"})
    rank = {"red": 0, "yellow": 1, "green": 2}
    items.sort(key=lambda a: (rank[a["tone"]], a["id"]))
    return {"accounts": items, "counts": {c: sum(a["tone"] == c for a in items) for c in rank},
            "interval_minutes": interval, "updated_at": now}


def enqueue_due(now=None):
    """At most one queued check per account; ordinary account jobs retain their order."""
    now = now or datetime.now()
    interval = settings.monitor_interval_minutes()
    if interval == 0:
        return []
    created = []
    with tasks.coordinator, SessionLocal() as db:
        latest = {}
        busy_checks = set()
        for job in db.scalars(select(TaskRun).where(TaskRun.kind.in_(["check_session", "login"]))):
            aid = _account_id(job)
            if aid is not None:
                latest[aid] = max(latest.get(aid, datetime.min), job.created_at)
                if job.status in ("queued", "running"):
                    busy_checks.add(aid)
        for account in list(db.scalars(select(Account).order_by(Account.id))):
            if _stop.is_set() or tasks._stopping:
                break
            if account.id in busy_checks or account.status == "banned" or not sessions.state_path(account.id).exists():
                continue
            last_attempt = max(account.session_checked_at or datetime.min, latest.get(account.id, datetime.min))
            if now - last_attempt < timedelta(minutes=interval):
                continue
            try:
                job = tasks.request_session_check(account.id, automatic=True)
                created.append(job.id)
            except HTTPException:
                continue
    return created


def _run():
    while not _stop.wait(30):
        try:
            enqueue_due()
        except Exception as error:
            log_event("session", f"Монитор аккаунтов: {error}", level="WARNING")


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = Thread(target=_run, name="account-monitor", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
