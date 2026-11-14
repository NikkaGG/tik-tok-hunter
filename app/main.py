"""TikTok Lead Hunter: локальный дашборд.

Запуск:  uvicorn app.main:app --reload --port 8000
Затем откройте http://127.0.0.1:8000
"""
from contextlib import asynccontextmanager
from datetime import datetime, time

from fastapi import Depends, FastAPI, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import STATIC_DIR
from app.database import Base, engine, ensure_schema, get_db
from app.models import Account, Draft, DraftStatus, LogEntry, TaskRun, TaskStatus, Video, TextUse
from app.routers import accounts, drafts, export, settings, tasks, templates, videos, text_sets
from app.schemas import LogOut, StatsOut
from app.services import tasks as task_service, session_monitor
from app.services.logs import log_event


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_schema(engine)
    task_service.recover_interrupted()
    session_monitor.start()
    log_event("system", "Приложение запущено")
    try:
        yield
    finally:
        session_monitor.stop()
        task_service.shutdown()
    log_event("system", "Приложение остановлено")


app = FastAPI(title="TikTok Lead Hunter", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def disable_static_cache(request, call_next):
    # Отключаем браузерный кэш для статики (index.html, app.js), чтобы при обновлении архива всегда открывалась свежая версия
    if not request.url.path.startswith("/api/"):
        request.scope["headers"] = [
            (k, v)
            for k, v in request.scope.get("headers", [])
            if k.lower() not in (b"if-none-match", b"if-modified-since")
        ]
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


for r in (accounts, videos, templates, drafts, tasks, export, settings, text_sets):
    app.include_router(r.router)


def _count_by(db: Session, column) -> dict[str, int]:
    return dict(db.execute(select(column, func.count()).group_by(column)).all())


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/account-monitor")
def account_monitor(db: Session = Depends(get_db)):
    return session_monitor.snapshot(db)


@app.get("/api/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_db)):
    start = datetime.combine(datetime.now().date(), time.min)
    accounts_by_status = _count_by(db, Account.status)
    videos_by_status = _count_by(db, Video.status)
    drafts_by_status = _count_by(db, Draft.status)

    last = db.scalars(select(TaskRun).order_by(TaskRun.id.desc()).limit(1)).first()
    return StatsOut(
        accounts_total=sum(accounts_by_status.values()),
        accounts_by_status=accounts_by_status,
        videos_total=sum(videos_by_status.values()),
        videos_by_status=videos_by_status,
        drafts_pending=drafts_by_status.get(DraftStatus.DRAFT, 0),
        replies_sent_total=db.scalar(select(func.count()).select_from(TextUse).where(TextUse.state == "sent")),
        replies_sent_today=db.scalar(
            select(func.count()).select_from(TextUse).where(TextUse.state == "sent", TextUse.sent_at >= start)
        ),
        tasks_running=db.scalar(select(func.count()).where(TaskRun.status == TaskStatus.RUNNING)),
        last_task=(
            {"id": last.id, "kind": last.kind, "status": last.status, "found": last.found,
             "result": last.result, "error": last.error, "created_at": last.created_at.isoformat(timespec="seconds")}
            if last else None
        ),
    )


@app.get("/api/logs", response_model=list[LogOut])
def logs(
    limit: int = Query(100, ge=1, le=1000),
    level: str | None = None,
    source: str | None = None,
    db: Session = Depends(get_db),
):
    q = select(LogEntry).order_by(LogEntry.id.desc()).limit(limit)
    if level:
        q = q.where(LogEntry.level == level.upper())
    if source:
        q = q.where(LogEntry.source == source)
    return db.scalars(q).all()


# Монтируется последним, чтобы маршруты /api/* имели приоритет.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
