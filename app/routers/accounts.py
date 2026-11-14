import json
import socket
import urllib.request
from datetime import datetime, time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, object_session

from app.database import get_db
from app.models import TextSet, TextUse, AccountTextSet, Account, AccountStatus, Draft, DraftStatus, TaskRun, TaskStatus
from app.schemas import (
    AccountBulkDelete,
    AccountBulkUpdate,
    AccountCreate,
    AccountOut,
    AccountUpdate,
)
from app.services import settings as app_settings
from app.services import tasks, sessions, text_library
from app.services.logs import log_event

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _normalize_account_proxy(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return app_settings.normalize_proxy(raw.strip())
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


def _extract_proxy_host(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    p = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if p.hostname and p.port:
        return f"{p.hostname}:{p.port}"
    netloc = p.netloc.rsplit("@", 1)[-1]
    return netloc or None


def _running_account_ids(db: Session, conn_user: str | None) -> set[int]:
    running_tasks = db.scalars(
        select(TaskRun).where(TaskRun.status == TaskStatus.RUNNING, TaskRun.kind.in_(["search", "auto_comment"]))
    ).all()
    if not running_tasks:
        return set()
    ids: set[int] = set()
    for t in running_tasks:
        try:
            params = json.loads(t.params or "{}")
        except Exception:
            params = {}
        aid = params.get("account_id")
        if aid:
            ids.add(int(aid))
        elif conn_user:
            acc_id = db.scalar(select(Account.id).where(Account.username == conn_user.lower()))
            if acc_id:
                ids.add(acc_id)
    return ids


def _effective_state(acc: Account, replies_today: int, is_running: bool) -> str:
    if acc.status == AccountStatus.BANNED:
        return "banned"
    if acc.status == AccountStatus.WARNING or acc.proxy_status == "error":
        return "warning"
    if acc.status == AccountStatus.WORKING or is_running:
        return "working"
    if acc.status == AccountStatus.PAUSED or (acc.daily_reply_limit > 0 and replies_today >= acc.daily_reply_limit):
        return "resting"
    return "ready"


def _replies_today(db: Session, account_ids: list[int]) -> dict[int, int]:
    if not account_ids:
        return {}
    start = datetime.combine(datetime.now().date(), time.min)
    rows = db.execute(
        select(TextUse.account_id, func.count()).where(TextUse.account_id.in_(account_ids),
            TextUse.state == "sent", TextUse.sent_at >= start).group_by(TextUse.account_id)
    ).all()
    return dict(rows)


def _out(
    acc: Account,
    replies_today: int,
    running_ids: set[int] | None = None,
    conn_user: str | None = None,
) -> AccountOut:
    out = AccountOut.model_validate(acc)
    out.replies_today = replies_today
    db = object_session(acc)
    if db is not None:
        out.text_set_ids = list(db.scalars(select(AccountTextSet.text_set_id).where(AccountTextSet.account_id == acc.id)))
        for task in db.scalars(select(TaskRun).where(TaskRun.status.in_(["queued", "running"])).order_by(TaskRun.id.desc())):
            if json.loads(task.params or "{}").get("account_id") == acc.id:
                if out.current_task_id is None:
                    out.current_task_id = task.id
                    out.current_task_kind = task.kind
                if task.kind in ("check_session", "login") and out.session_task_id is None:
                    out.session_task_id = task.id
                    out.session_task_status = task.status
                    out.session_task_kind = task.kind
    if not sessions.state_path(acc.id).exists():
        out.session_status = "missing"
    effective_proxy = acc.proxy or app_settings.search_proxy()
    out.proxy_host = _extract_proxy_host(effective_proxy)
    is_running = bool(running_ids and acc.id in running_ids)
    out.effective_state = _effective_state(acc, replies_today, is_running)
    out.is_current_session = app_settings.search_account_id() == acc.id
    return out


def _get_or_404(db: Session, account_id: int) -> Account:
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
    return acc


@router.get("", response_model=list[AccountOut])
def list_accounts(status_filter: str | None = None, db: Session = Depends(get_db)):
    q = select(Account).order_by(Account.id.asc())
    if status_filter and status_filter in {s.value for s in AccountStatus}:
        q = q.where(Account.status == status_filter)
    accounts = db.scalars(q).all()
    counts = _replies_today(db, [a.id for a in accounts])
    conn_user = app_settings.connected_username()
    running_ids = _running_account_ids(db, conn_user)
    items = [_out(a, counts.get(a.id, 0), running_ids, conn_user) for a in accounts]
    if status_filter and status_filter not in {s.value for s in AccountStatus}:
        items = [i for i in items if i.effective_state == status_filter]
    return items


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db)):
    acc = _get_or_404(db, account_id)
    conn_user = app_settings.connected_username()
    running_ids = _running_account_ids(db, conn_user)
    return _out(acc, _replies_today(db, [acc.id]).get(acc.id, 0), running_ids, conn_user)


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    text_library.begin_write(db)
    if db.scalar(select(Account).where(Account.username == data.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"@{data.username} уже есть в списке")
    payload = data.model_dump()
    payload["proxy"] = _normalize_account_proxy(payload.get("proxy"))
    if not payload.get("proxy_status"):
        payload["proxy_status"] = "ok"
    acc = Account(**payload)
    db.add(acc)
    db.commit()
    log_event("accounts", f"Добавлен аккаунт @{acc.username}", db=db)
    conn_user = app_settings.connected_username()
    running_ids = _running_account_ids(db, conn_user)
    return _out(acc, 0, running_ids, conn_user)


@router.patch("/{account_id}", response_model=AccountOut)
@tasks.coordinated
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    acc = _get_or_404(db, account_id)
    changes = data.model_dump(exclude_unset=True)
    set_ids = changes.pop("text_set_ids", None)
    if set_ids is not None:
        set_ids = set(set_ids)
        existing = set(db.scalars(select(TextSet.id).where(TextSet.id.in_(set_ids), TextSet.archived == 0)))
        if existing != set_ids:
            raise HTTPException(422, "Один из выбранных наборов недоступен")
        db.execute(delete(AccountTextSet).where(AccountTextSet.account_id == account_id))
        db.add_all(AccountTextSet(account_id=account_id, text_set_id=sid) for sid in set_ids)
    if "username" in changes and changes["username"] is None:
        raise HTTPException(422, "Имя аккаунта не может быть пустым")
    if "username" in changes and changes["username"] != acc.username:
        tasks.ensure_idle(db, account_id)
        if sessions.state_path(account_id).exists():
            acc.session_status = "unchecked"
    if "username" in changes and changes["username"] and changes["username"] != acc.username:
        if db.scalar(select(Account).where(Account.username == changes["username"], Account.id != account_id)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"@{changes['username']} уже есть в списке")
    if "proxy" in changes:
        changes["proxy"] = _normalize_account_proxy(changes["proxy"])
    if (
        "status" in changes
        and changes["status"] in (AccountStatus.ACTIVE, AccountStatus.WORKING, AccountStatus.PAUSED)
        and "proxy_status" not in changes
        and acc.proxy_status == "error"
    ):
        acc.proxy_status = "ok"
    for field, value in changes.items():
        setattr(acc, field, value)
    db.commit()
    if changes:
        log_event("accounts", f"@{acc.username}: изменены поля {', '.join(changes)}", db=db)
    conn_user = app_settings.connected_username()
    running_ids = _running_account_ids(db, conn_user)
    return _out(acc, _replies_today(db, [acc.id]).get(acc.id, 0), running_ids, conn_user)


@router.post("/{account_id}/check-proxy", response_model=AccountOut)
def check_account_proxy(account_id: int, db: Session = Depends(get_db)):
    acc = _get_or_404(db, account_id)
    target_proxy = acc.proxy or app_settings.search_proxy()
    if not target_proxy:
        acc.proxy_status = "none"
        if not acc.proxy_label:
            acc.proxy_label = "Без прокси"
        db.commit()
        conn_user = app_settings.connected_username()
        running_ids = _running_account_ids(db, conn_user)
        return _out(acc, _replies_today(db, [acc.id]).get(acc.id, 0), running_ids, conn_user)

    p = urlparse(target_proxy)
    ok = False
    if p.scheme in ("http", "https"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": target_proxy, "https": target_proxy}))
        try:
            with opener.open("https://api.ipify.org", timeout=6) as r:
                ok = bool(r.read().decode().strip())
        except Exception:
            ok = False
    if not ok and p.hostname and p.port:
        try:
            with socket.create_connection((p.hostname, p.port), timeout=4):
                ok = True
        except Exception:
            ok = False

    if ok:
        acc.proxy_status = "ok"
        if not acc.proxy_label or "недоступен" in acc.proxy_label or "ошибка" in acc.proxy_label:
            base = (acc.proxy_label or "").split("·")[0].strip()
            acc.proxy_label = f"{base} · стабильно" if base and base not in ("недоступен", "ошибка") else "Прокси · стабильно"
        log_event("accounts", f"Прокси @{acc.username} ({_extract_proxy_host(target_proxy)}): доступен", db=db)
    else:
        acc.proxy_status = "error"
        base = (acc.proxy_label or "").split("·")[0].strip()
        acc.proxy_label = f"{base} · недоступен" if base and base not in ("стабильно", "активно") else "Прокси · недоступен"
        log_event("accounts", f"Прокси @{acc.username} ({_extract_proxy_host(target_proxy)}): недоступен", level="WARNING", db=db)

    db.commit()
    conn_user = app_settings.connected_username()
    running_ids = _running_account_ids(db, conn_user)
    return _out(acc, _replies_today(db, [acc.id]).get(acc.id, 0), running_ids, conn_user)


@router.post("/bulk-update")
@tasks.coordinated
def bulk_update_accounts(data: AccountBulkUpdate, db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).where(Account.id.in_(data.ids))).all()
    if not accounts:
        return {"updated": 0}
    norm_proxy = _normalize_account_proxy(data.proxy) if data.proxy is not None else None
    for acc in accounts:
        if data.status is not None:
            acc.status = data.status
            if data.status in (AccountStatus.ACTIVE, AccountStatus.WORKING, AccountStatus.PAUSED) and acc.proxy_status == "error":
                acc.proxy_status = "ok"
        if data.daily_reply_limit is not None:
            acc.daily_reply_limit = data.daily_reply_limit
        if data.display_name is not None:
            acc.display_name = data.display_name or None
        if data.proxy is not None:
            acc.proxy = norm_proxy
        if data.proxy_label is not None:
            acc.proxy_label = data.proxy_label or None
    db.commit()
    log_event("accounts", f"Массовое обновление аккаунтов: {len(accounts)} шт.", db=db)
    return {"updated": len(accounts)}


@router.post("/bulk-delete")
@tasks.coordinated
def bulk_delete_accounts(data: AccountBulkDelete, db: Session = Depends(get_db)):
    for aid in data.ids:
        tasks.ensure_idle(db, aid)
    res = db.execute(delete(Account).where(Account.id.in_(data.ids)))
    db.commit()
    for aid in data.ids:
        sessions.state_path(aid).unlink(missing_ok=True)
    if app_settings.search_account_id() in data.ids:
        app_settings.update_values(search_account_id=None)
    deleted = res.rowcount or 0
    if deleted:
        log_event("accounts", f"Массовое удаление аккаунтов: {deleted} шт.", level="WARNING", db=db)
    return {"deleted": deleted}


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
@tasks.coordinated
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = _get_or_404(db, account_id)
    tasks.ensure_idle(db, account_id)
    username = acc.username
    db.delete(acc)
    db.commit()
    sessions.state_path(account_id).unlink(missing_ok=True)
    if app_settings.search_account_id() == account_id:
        app_settings.update_values(search_account_id=None)
    log_event("accounts", f"Удалён аккаунт @{username}", level="WARNING", db=db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
