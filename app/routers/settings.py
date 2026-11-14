from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import TaskRun
from app.schemas import TaskOut
from app.services import settings as app_settings
from app.services import tasks
from app.services.logs import log_event

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    search_proxy: str | None
    proxy_source: Literal["ui", "env"] | None
    use_system_chrome: bool
    last_proxy_test: TaskOut | None
    monitor_interval_minutes: int = 15
    browser_limit: int = 3
    search_account_id: int | None = None


class BrowserIn(BaseModel):
    use_system_chrome: bool


class ProxyIn(BaseModel):
    proxy: str = Field(default="", max_length=500)  # пусто = без прокси
    scheme: Literal["http", "https", "socks5"] = "http"


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    last = db.scalars(select(TaskRun).where(TaskRun.kind == "test_proxy").order_by(TaskRun.id.desc()).limit(1)).first()
    return SettingsOut(
        search_proxy=app_settings.search_proxy(),
        proxy_source=app_settings.proxy_source(),
        use_system_chrome=app_settings.use_system_chrome(),
        last_proxy_test=last,
        monitor_interval_minutes=app_settings.monitor_interval_minutes(),
        browser_limit=app_settings.browser_limit(),
        search_account_id=app_settings.search_account_id(),
    )


@router.put("/proxy", response_model=SettingsOut)
def set_proxy(data: ProxyIn, db: Session = Depends(get_db)):
    value = None
    if data.proxy.strip():
        try:
            value = app_settings.normalize_proxy(data.proxy, data.scheme)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    app_settings.set_search_proxy(value)
    log_event("settings", f"Прокси: {app_settings.mask_proxy(value) or 'отключён'}", db=db)
    return get_settings(db)


@router.put("/browser", response_model=SettingsOut)
def set_browser(data: BrowserIn, db: Session = Depends(get_db)):
    app_settings.set_use_system_chrome(data.use_system_chrome)
    log_event("settings", "Браузер: " + ("Google Chrome" if data.use_system_chrome else "встроенный Chromium"), db=db)
    return get_settings(db)


@router.post("/proxy/test", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
def test_proxy():
    return tasks.submit("test_proxy")


class ExecutionIn(BaseModel):
    browser_limit: int = Field(ge=1, le=8)
    search_account_id: int | None = None

@router.put("/execution", response_model=SettingsOut)
def execution(data: ExecutionIn, db: Session = Depends(get_db)):
    if data.search_account_id is not None:
        from app.services.sessions import require_account
        require_account(db, data.search_account_id)
    app_settings.update_values(browser_limit=data.browser_limit, search_account_id=data.search_account_id)
    with tasks.coordinator:
        tasks._dispatch()
    return get_settings(db)


class MonitorIn(BaseModel):
    interval_minutes: int = Field(ge=0, le=1440)

@router.put("/monitor", response_model=SettingsOut)
def set_monitor(data: MonitorIn, db: Session = Depends(get_db)):
    app_settings.update_values(monitor_interval_minutes=data.interval_minutes)
    return get_settings(db)
