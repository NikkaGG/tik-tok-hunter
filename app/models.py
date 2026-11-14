from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountStatus(StrEnum):
    ACTIVE = "active"
    WORKING = "working"  # сейчас в работе
    PAUSED = "paused"
    WARNING = "warning"  # предупреждение или ограничения от платформы
    BANNED = "banned"


class VideoStatus(StrEnum):
    NEW = "new"
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    REPLIED = "replied"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"  # ответ опубликован и отмечен в очереди
    REVIEW = "review"  # результат публикации требует ручной проверки
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Account(Base):
    """TikTok-аккаунт, с которого отправляются ответы: статус, прокси, дневной лимит, заметки."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=AccountStatus.ACTIVE)
    daily_reply_limit: Mapped[int] = mapped_column(Integer, default=20)
    proxy: Mapped[str | None] = mapped_column(String(256), default=None)
    proxy_label: Mapped[str | None] = mapped_column(String(128), default=None)
    proxy_status: Mapped[str] = mapped_column(String(16), default="ok")
    session_status: Mapped[str] = mapped_column(String(16), default="missing")
    session_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    session_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    drafts: Mapped[list["Draft"]] = relationship(back_populates="account")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    views: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(String(128), index=True)  # по какому запросу найдено
    status: Mapped[str] = mapped_column(String(16), default=VideoStatus.NEW, index=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    drafts: Mapped[list["Draft"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)  # spintax: {Привет|Добрый день}, [author]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    text_set_id: Mapped[int | None] = mapped_column(Integer)
    variant_id: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    last_task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    error_code: Mapped[str | None] = mapped_column(String(32))
    sending: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(Text)
    repeated: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=DraftStatus.DRAFT, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    video: Mapped[Video] = relationship(back_populates="drafts")
    account: Mapped[Account | None] = relationship(back_populates="drafts")


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # search | login | check_session
    params: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.QUEUED, index=True)
    found: Mapped[int] = mapped_column(Integer, default=0)  # новых роликов в базе
    result: Mapped[str | None] = mapped_column(Text)  # краткий итог для UI
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    level: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)


class TextSet(Base):
    __tablename__ = "text_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    legacy_template_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    archived: Mapped[int] = mapped_column(Integer, default=0)


class TextVariant(Base):
    __tablename__ = "text_variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    text_set_id: Mapped[int] = mapped_column(ForeignKey("text_sets.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    archived: Mapped[int] = mapped_column(Integer, default=0)


class AccountTextSet(Base):
    __tablename__ = "account_text_sets"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    text_set_id: Mapped[int] = mapped_column(ForeignKey("text_sets.id"), primary_key=True)


class TextUse(Base):
    """Durable ledger, deliberately independent of draft/account deletion."""
    __tablename__ = "text_uses"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int | None] = mapped_column(Integer, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    video_id: Mapped[int] = mapped_column(Integer, index=True)
    text_set_id: Mapped[int | None] = mapped_column(Integer)
    variant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    normalized: Mapped[str] = mapped_column(Text, index=True)
    shape: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), index=True)  # reserved | sent | review | released
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
