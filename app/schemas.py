import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import AccountStatus, DraftStatus, VideoStatus

_USERNAME_RE = re.compile(r"^[a-z0-9._]{2,24}$")


# ---------- Аккаунты ----------

class AccountBase(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    status: AccountStatus = AccountStatus.ACTIVE
    daily_reply_limit: int = Field(default=20, ge=0, le=500)
    proxy: str | None = Field(default=None, max_length=256)
    proxy_label: str | None = Field(default=None, max_length=128)
    proxy_status: str = Field(default="ok", max_length=16)
    notes: str | None = None


class AccountCreate(AccountBase):
    username: str

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        v = v.strip().lstrip("@").lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username: 2–24 символа, латиница, цифры, '.' и '_'")
        return v


class AccountUpdate(BaseModel):
    text_set_ids: list[int] | None = Field(default=None, max_length=1000)
    username: str | None = None
    display_name: str | None = Field(default=None, max_length=128)
    status: AccountStatus | None = None
    daily_reply_limit: int | None = Field(default=None, ge=0, le=500)
    proxy: str | None = Field(default=None, max_length=256)
    proxy_label: str | None = Field(default=None, max_length=128)
    proxy_status: str | None = Field(default=None, max_length=16)
    notes: str | None = None

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lstrip("@").lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username: 2–24 символа, латиница, цифры, '.' и '_'")
        return v


class AccountBulkUpdate(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    status: AccountStatus | None = None
    daily_reply_limit: int | None = Field(default=None, ge=0, le=500)
    display_name: str | None = Field(default=None, max_length=128)
    proxy: str | None = Field(default=None, max_length=256)
    proxy_label: str | None = Field(default=None, max_length=128)


class AccountBulkDelete(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


class AccountOut(AccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: datetime
    updated_at: datetime
    replies_today: int = 0
    proxy_host: str | None = None
    effective_state: str = "ready"
    is_current_session: bool = False
    session_status: str = "missing"
    session_checked_at: datetime | None = None
    session_error: str | None = None
    text_set_ids: list[int] = []
    current_task_id: int | None = None
    current_task_kind: str | None = None
    session_task_id: int | None = None
    session_task_status: str | None = None
    session_task_kind: str | None = None


# ---------- Ролики ----------

class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: str
    url: str
    author: str | None
    description: str | None
    views: int | None
    query: str
    status: VideoStatus
    found_at: datetime


class VideoPage(BaseModel):
    total: int
    items: list[VideoOut]


class VideoUpdate(BaseModel):
    status: VideoStatus


class VideoBulkUpdate(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    status: VideoStatus


class VideoBulkDelete(BaseModel):
    ids: list[int] | None = None
    status: VideoStatus | None = None


# ---------- Шаблоны (spintax) ----------

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=2000)


class TemplateOut(TemplateIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class SpinPreviewIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    variables: dict[str, str] = Field(default_factory=dict)
    count: int = Field(default=5, ge=1, le=20)


class SpinPreviewOut(BaseModel):
    variants: list[str]
    total_combinations: int


# ---------- Черновики ответов ----------

class DraftCreate(BaseModel):
    video_id: int
    template_id: int | None = None
    text: str | None = Field(default=None, max_length=2000)
    account_id: int | None = None


class DraftGenerate(BaseModel):
    template_id: int | None = None
    account_ids: list[int] = Field(default_factory=list, max_length=100)
    text_set_ids: list[int] = Field(default_factory=list, max_length=1000)
    video_ids: list[int] | None = None  # None: все «relevant»; повторы проверяются по паре ролик + аккаунт
    account_id: int | None = None


class DraftUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    account_id: int | None = None


class DraftBulkDelete(BaseModel):
    ids: list[int] | None = None
    status: DraftStatus | None = None


class DraftOut(BaseModel):
    id: int
    video_id: int
    video_url: str
    video_author: str | None
    video_description: str | None
    account_id: int | None
    account_username: str | None
    text: str
    status: DraftStatus
    created_at: datetime
    sent_at: datetime | None
    text_set_id: int | None = None
    variant_id: int | None = None
    repeated: bool = False
    result: str | None = None
    task_id: int | None = None
    last_task_id: int | None = None
    error_code: str | None = None


# ---------- Задачи и сессия парсера ----------

class SearchTaskIn(BaseModel):
    account_id: int | None = None
    queries: list[str] = Field(min_length=1, max_length=20)
    limit_per_query: int = Field(default=30, ge=1, le=200)
    visible: bool = False  # открыть окно браузера, чтобы капчу можно было пройти вручную
    template_id: int | None = None  # сразу создать черновики по этому шаблону

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, v: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(q.strip() for q in v if q.strip()))
        if not cleaned:
            raise ValueError("Нужен хотя бы один запрос")
        return cleaned


class AutoCommentTaskIn(BaseModel):
    @model_validator(mode="after")
    def delay_order(self):
        if self.delay_min_s > self.delay_max_s:
            raise ValueError("Минимальная задержка больше максимальной")
        return self

    draft_ids: list[int] | None = None
    account_id: int | None = None
    delay_min_s: int = Field(default=20, ge=3, le=600)
    delay_max_s: int = Field(default=45, ge=3, le=600)
    visible: bool = False
    max_count: int | None = Field(default=None, ge=1, le=500)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    params: dict[str, Any]
    status: str
    found: int
    result: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    @field_validator("params", mode="before")
    @classmethod
    def parse_params(cls, v: Any) -> Any:
        return json.loads(v or "{}") if isinstance(v, str) else v


class SessionInfo(BaseModel):
    state_saved: bool
    account_id: int | None = None
    session_status: str = "missing"
    error: str | None = None
    username: str | None = None  # подключённый аккаунт (по последней успешной проверке)
    saved_at: datetime | None
    proxy_configured: bool
    last_check: TaskOut | None


# ---------- Статистика и логи ----------

class StatsOut(BaseModel):
    accounts_total: int
    accounts_by_status: dict[str, int]
    videos_total: int
    videos_by_status: dict[str, int]
    drafts_pending: int
    replies_sent_total: int
    replies_sent_today: int
    tasks_running: int
    last_task: dict | None


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    level: str
    source: str
    message: str
