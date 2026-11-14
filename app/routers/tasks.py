from datetime import datetime
from typing import Literal
from uuid import uuid4
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Account, TaskRun
from app.schemas import AutoCommentTaskIn, SearchTaskIn, SessionInfo, TaskOut
from app.services import sessions, session_import, tasks
from app.services import settings as app_settings

router = APIRouter(prefix="/api", tags=["tasks"])

class SessionImportIn(BaseModel):
    data: str = Field(min_length=1, max_length=200_000)


def selected(db, account_id=None):
    return sessions.require_account(db, account_id or app_settings.search_account_id())

def _task_out(db, task):
    item = TaskOut.model_validate(task)
    if task.kind == "auto_comment":
        item.summary = tasks.task_summary(db, task.id)
    return item


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)):
    return [_task_out(db, t) for t in db.scalars(select(TaskRun).order_by(TaskRun.id.desc()).limit(limit))]

@router.post("/tasks/search", response_model=TaskOut, status_code=202)
@tasks.coordinated
def start_search(data: SearchTaskIn, db: Session = Depends(get_db)):
    account = selected(db, data.account_id)
    params = data.model_dump()
    params["account_id"] = account.id
    # Text preparation is an explicit operation after search.
    params["template_id"] = None
    return tasks.submit("search", params)


@router.post("/tasks/search-all", status_code=202)
@tasks.coordinated
def start_search_all(data: SearchTaskIn, db: Session = Depends(get_db)):
    accounts = [a for a in db.scalars(select(Account).where(Account.status.in_(["active", "working"])).order_by(Account.id))
                if sessions.state_path(a.id).exists()]
    if not accounts:
        raise HTTPException(409, "Нет доступных аккаунтов с подключённой сессией")
    groups = [[] for _ in accounts]
    for index, query in enumerate(data.queries):
        groups[index % len(accounts)].append(query)
    result = []
    for account, queries in zip(accounts, groups):
        if queries:
            params = data.model_dump()
            params.update(account_id=account.id, queries=queries, template_id=None)
            result.append(tasks.submit("search", params).id)
    return {"task_ids": result, "accounts_used": len(result), "available_accounts": len(accounts)}

@router.post("/tasks/login", response_model=TaskOut, status_code=202)
@tasks.coordinated
def start_login(account_id: int | None = None, db: Session = Depends(get_db)):
    account = selected(db, account_id)
    tasks.ensure_idle(db, account.id)
    return tasks.submit("login", {"account_id": account.id})

@router.post("/tasks/check-session", response_model=TaskOut, status_code=202)
@tasks.coordinated
def start_check(account_id: int | None = None, db: Session = Depends(get_db)):
    account = selected(db, account_id)
    return tasks.request_session_check(account.id)

@router.post("/tasks/auto-comment", status_code=202)
def start_auto_comment(data: AutoCommentTaskIn):
    return tasks.submit_batch(data)

@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int):
    if not tasks.cancel(task_id):
        raise HTTPException(409, "Задача уже завершена")
    return {"ok": True, "task_id": task_id}

@router.get("/session", response_model=SessionInfo)
def session_info(account_id: int | None = None, db: Session = Depends(get_db)):
    aid = account_id or app_settings.search_account_id()
    # The overview remains usable before the first account has been created.
    if not aid:
        return SessionInfo(state_saved=False, saved_at=None, proxy_configured=bool(app_settings.search_proxy()), last_check=None)
    account = selected(db, aid)
    recent = [t for t in db.scalars(select(TaskRun).where(TaskRun.kind.in_(["login", "check_session"])).order_by(TaskRun.id.desc()))
              if __import__("json").loads(t.params or "{}").get("account_id") == account.id]
    state = sessions.info(account)
    return SessionInfo(account_id=account.id, state_saved=state["state_saved"], username=account.username,
        saved_at=state["saved_at"], proxy_configured=bool(account.proxy or app_settings.search_proxy()),
        last_check=recent[0] if recent else None, session_status=account.session_status, error=account.session_error)

@router.post("/session/import", response_model=TaskOut, status_code=202)
@tasks.coordinated
def import_session(data: SessionImportIn, account_id: int | None = None, db: Session = Depends(get_db)):
    account = selected(db, account_id)
    tasks.ensure_idle(db, account.id)
    try:
        cookies = session_import.parse(data.data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    sessions.write_cookies(account.id, cookies)
    account.session_status, account.session_error = "unchecked", None
    db.commit()
    return tasks.submit("check_session", {"account_id": account.id})

@router.delete("/session", status_code=204)
@tasks.coordinated
def clear_session(account_id: int | None = None, db: Session = Depends(get_db)):
    account = selected(db, account_id)
    tasks.ensure_idle(db, account.id)
    sessions.state_path(account.id).unlink(missing_ok=True)
    account.session_status, account.session_error = "missing", None
    db.commit()

@router.get("/accounts/{account_id}/session")
def account_session(account_id: int, db: Session = Depends(get_db)):
    return sessions.info(selected(db, account_id))

@router.post("/accounts/{account_id}/session/import", response_model=TaskOut, status_code=202)
def account_import(account_id: int, data: SessionImportIn, db: Session = Depends(get_db)):
    return import_session(data, account_id, db)

@router.post("/accounts/{account_id}/session/login", response_model=TaskOut, status_code=202)
def account_login(account_id: int, db: Session = Depends(get_db)):
    return start_login(account_id, db)

@router.post("/accounts/{account_id}/session/check", response_model=TaskOut, status_code=202)
def account_check(account_id: int, db: Session = Depends(get_db)):
    return start_check(account_id, db)

@router.delete("/accounts/{account_id}/session", status_code=204)
def account_disconnect(account_id: int, db: Session = Depends(get_db)):
    return clear_session(account_id, db)


class ConnectIn(BaseModel):
    method: Literal["import", "login"]
    data: str | None = Field(default=None, max_length=200_000)

@router.post("/accounts/connect", response_model=TaskOut, status_code=202)
@tasks.coordinated
def connect_account(data: ConnectIn):
    cookies = None
    if data.method == "import":
        if not data.data or not data.data.strip():
            raise HTTPException(422, "Вставьте sessionid или экспорт cookies")
        try:
            cookies = session_import.parse(data.data)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    token = uuid4().hex
    path = sessions.connection_path(token)
    if cookies is not None:
        path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    try:
        return tasks.submit("connect", {"method": data.method, "connection_token": token})
    except BaseException:
        path.unlink(missing_ok=True)
        raise

@router.get("/tasks/{task_id}/summary")
def get_task_summary(task_id: int, db: Session = Depends(get_db)):
    if not db.get(TaskRun, task_id):
        raise HTTPException(404, "Задача не найдена")
    return tasks.task_summary(db, task_id)


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(TaskRun, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return _task_out(db, task)
