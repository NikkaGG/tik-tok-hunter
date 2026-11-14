"""Per-account state paths and identity verification. No cookies in API responses."""
from contextvars import ContextVar
from datetime import datetime
import json
from fastapi import HTTPException
from app.config import SESSION_DIR, SEARCH_STATE_PATH
from app.models import Account

browser_binding = ContextVar("browser_binding", default=None)


def state_path(account_id):
    return SESSION_DIR / f"account_{int(account_id)}.json"


def require_account(db, account_id):
    if not account_id:
        raise HTTPException(409, "Сначала выберите аккаунт")
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Аккаунт не найден")
    return account


def info(account):
    path = state_path(account.id)
    return {"account_id": account.id, "username": account.username,
            "state_saved": path.exists(), "status": account.session_status,
            "checked_at": account.session_checked_at, "error": account.session_error,
            "saved_at": datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else None}


def write_cookies(account_id, cookies):
    path = state_path(account_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    temp.replace(path)


async def verify_context(ctx, expected, page=None):
    from app.workers import tiktok_search as ts
    data = await ts._account_info(ctx, page=page) if page is not None else await ts._account_info(ctx)
    username = ((data or {}).get("data") or {}).get("username")
    if not username or (data or {}).get("message") == "error":
        raise ts.SessionError("TikTok не подтвердил личность аккаунта. Проверьте сессию")
    if username.lstrip("@").casefold() != expected.lstrip("@").casefold():
        raise ts.SessionError(f"Несовпадение сессии: ожидался @{expected}, TikTok вернул @{username}")
    return username


def connection_path(token):
    from uuid import UUID
    return SESSION_DIR / f"connect_{UUID(token).hex}.json"
