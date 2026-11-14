from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # API и воркер пишут параллельно
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema(target_engine=engine) -> None:
    """Создаёт таблицы и мягко добавляет новые колонки в существующую БД SQLite."""
    # Back up an existing pre-multiaccount database before altering its schema.
    import sqlite3
    from contextlib import closing
    import shutil
    from pathlib import Path
    from datetime import datetime
    db_file = target_engine.url.database
    if db_file and db_file != ":memory:" and Path(db_file).exists():
        with closing(sqlite3.connect(db_file)) as source:
            old = {r[1] for r in source.execute("PRAGMA table_info(accounts)")}
            if old and "session_status" not in old:
                backup = Path(db_file).parent / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-migration")
                backup.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(backup / "app.db")) as target:
                    source.backup(target)
                for relative in ("settings.json", "session/search_state.json"):
                    src = Path(db_file).parent / relative
                    if src.exists():
                        shutil.copy2(src, backup / src.name)
    Base.metadata.create_all(target_engine)
    with target_engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(accounts)").fetchall()}
        if cols:
            if "proxy" not in cols:
                conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN proxy VARCHAR(256)")
            if "proxy_label" not in cols:
                conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN proxy_label VARCHAR(128)")
            if "proxy_status" not in cols:
                conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN proxy_status VARCHAR(16) DEFAULT 'ok'")

    with target_engine.begin() as conn:
        for table, additions in {
            "accounts": {"session_status": "VARCHAR(16) DEFAULT 'missing'", "session_checked_at": "DATETIME", "session_error": "TEXT"},
            "drafts": {"text_set_id": "INTEGER", "variant_id": "INTEGER", "task_id": "INTEGER", "last_task_id": "INTEGER", "error_code": "VARCHAR(32)", "sending": "INTEGER DEFAULT 0", "result": "TEXT", "repeated": "INTEGER DEFAULT 0"},
        }.items():
            columns = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, definition in additions.items():
                if name not in columns:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app.models import ReplyTemplate, TextSet, TextVariant, Draft, TextUse
    with Session(target_engine) as db:
        for template in db.scalars(select(ReplyTemplate)):
            if not db.scalar(select(TextSet.id).where(TextSet.legacy_template_id == template.id)):
                pool = TextSet(name=template.name, legacy_template_id=template.id)
                db.add(pool)
                db.flush()
                db.add(TextVariant(text_set_id=pool.id, body=template.body))
        for draft in db.scalars(select(Draft).where(Draft.status.in_(["draft", "sent", "review"]))):
            if not db.scalar(select(TextUse.id).where(TextUse.draft_id == draft.id)):
                norm = " ".join(draft.text.casefold().split())
                db.add(TextUse(draft_id=draft.id, account_id=draft.account_id, video_id=draft.video_id,
                               normalized=norm, shape=norm, state={"draft": "reserved"}.get(draft.status, draft.status), sent_at=draft.sent_at))
        db.commit()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@event.listens_for(Session, "before_flush")
def _durable_ids(db, flush_context, instances):
    from sqlalchemy import select, func
    from app.models import Account, Video, Draft, TextUse
    for model, history_column in ((Account, TextUse.account_id), (Video, TextUse.video_id), (Draft, TextUse.draft_id)):
        items = [obj for obj in db.new if isinstance(obj, model) and obj.id is None]
        if items:
            highest = max(db.scalar(select(func.max(model.id))) or 0,
                          db.scalar(select(func.max(history_column))) or 0)
            for offset, item in enumerate(items, 1):
                item.id = highest + offset
