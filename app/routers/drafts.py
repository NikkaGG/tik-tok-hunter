"""Prepared comments, transactional reservations and manual review."""
from datetime import datetime, time
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Account, AccountTextSet, Draft, ReplyTemplate, TextUse, Video
from app.schemas import DraftBulkDelete, DraftCreate, DraftGenerate, DraftOut, DraftUpdate, AutoCommentTaskIn
from app.services import spintax, tasks, text_library as library

router = APIRouter(prefix="/api/drafts", tags=["drafts"])


class PrepareAllIn(BaseModel):
    account_id: int | None = None


@router.post("/prepare-all-assigned")
def prepare_all_assigned(data: PrepareAllIn, db: Session = Depends(get_db)):
    account_query = select(Account).where(Account.status.in_(["active", "working"]))
    if data.account_id:
        account_query = account_query.where(Account.id == data.account_id)
    accounts = list(db.scalars(account_query.order_by(Account.id)))
    video_ids = list(db.scalars(select(Video.id).order_by(Video.id)))
    if not video_ids:
        raise HTTPException(409, "Сначала найдите ролики")
    plans = [(a.id, list(db.scalars(select(AccountTextSet.text_set_id).where(AccountTextSet.account_id == a.id)))) for a in accounts]
    missing = [a.username for a in accounts if not next(sets for aid, sets in plans if aid == a.id)]
    if not accounts or missing:
        raise HTTPException(409, "Назначьте наборы текстов аккаунтам: " + ", ".join('@' + name for name in missing) if missing else "Нет доступных аккаунтов")
    db.rollback()
    created, skipped = 0, 0
    for account_id, sets in plans:
        result = library.prepare(db, video_ids, [account_id], sets)
        created += len(result["created"])
        skipped += len(result["skipped"])
    draft_ids = list(db.scalars(select(Draft.id).where(Draft.status == "draft",
        Draft.account_id.in_([aid for aid, _ in plans]), Draft.video_id.in_(video_ids)).order_by(Draft.id)))
    return {"created": created, "skipped": skipped, "draft_ids": draft_ids}


def _out(d):
    return DraftOut(id=d.id, video_id=d.video_id, video_url=d.video.url,
        video_author=d.video.author, video_description=d.video.description,
        account_id=d.account_id, account_username=d.account.username if d.account else None,
        text=d.text, status=d.status, created_at=d.created_at, sent_at=d.sent_at,
        text_set_id=d.text_set_id, variant_id=d.variant_id, task_id=d.task_id,
        repeated=bool(d.repeated), result=d.result)


def _get_or_404(db, draft_id):
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404, "Черновик не найден")
    return d


def editable(d):
    if d.task_id or d.sending:
        raise HTTPException(409, "Сначала остановите задачу и дождитесь её завершения")
    if d.status != "draft":
        raise HTTPException(409, "Редактировать можно только черновик")


def account_exists(db, aid):
    if aid is not None and not db.get(Account, aid):
        raise HTTPException(404, "Аккаунт не найден")

@router.get("", response_model=list[DraftOut])
def list_drafts(status_filter: str | None = Query("draft", alias="status"),
                account_id: int | None = None, text_set_id: int | None = None,
                limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    q = select(Draft).options(joinedload(Draft.video), joinedload(Draft.account))
    if status_filter:
        q = q.where(Draft.status == status_filter)
    if account_id:
        q = q.where(Draft.account_id == account_id)
    if text_set_id:
        q = q.where(Draft.text_set_id == text_set_id)
    return [_out(d) for d in db.scalars(q.order_by(Draft.id).limit(limit))]


@router.get("/summary")
def draft_summary(account_id: int | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(Draft.account_id, Draft.task_id, func.count()).where(
        Draft.status == 'draft').group_by(Draft.account_id, Draft.task_id)).all()
    available = unassigned = in_progress = 0
    for aid, task_id, count in rows:
        if account_id is None:
            if aid is None:
                unassigned += count if task_id is None else 0
                continue
        elif aid not in (None, account_id):
            continue
        if task_id is None:
            available += count
        else:
            in_progress += count
    return {'available': available, 'unassigned': unassigned, 'in_progress': in_progress}

@router.post("", response_model=DraftOut, status_code=201)
def create_draft(data: DraftCreate, db: Session = Depends(get_db)):
    library.begin_write(db)
    video = db.get(Video, data.video_id)
    if not video:
        raise HTTPException(404, "Ролик не найден")
    account_exists(db, data.account_id)
    if data.account_id and library.pair_exists(db, video.id, data.account_id):
        raise HTTPException(409, "У аккаунта уже есть комментарий под этим роликом")
    if data.text:
        content = data.text
    elif data.template_id:
        tpl = db.get(ReplyTemplate, data.template_id)
        if not tpl:
            raise HTTPException(404, "Шаблон не найден")
        content = spintax.spin(tpl.body, spintax.video_variables(video))
    else:
        raise HTTPException(422, "Укажите text или template_id")
    d = Draft(video_id=video.id, account_id=data.account_id, text=content)
    db.add(d)
    db.flush()
    library.record(db, d)
    db.commit()
    return _out(d)

@router.post("/preview")
def preview_drafts(data: DraftGenerate, db: Session = Depends(get_db)):
    ids = data.video_ids
    if ids is None:
        ids = list(db.scalars(select(Video.id).where(Video.status == "relevant")))
    return library.preview(db, ids, data.account_ids, data.text_set_ids)


@router.post("/generate")
def generate_drafts(data: DraftGenerate, db: Session = Depends(get_db)):
    if not data.account_ids or not data.text_set_ids:
        raise HTTPException(422, "Выберите аккаунты и тематические наборы")
    ids = data.video_ids
    if ids is None:
        ids = list(db.scalars(select(Video.id).where(Video.status == "relevant")))
        db.rollback()
    if not ids:
        return {"created": [], "skipped": []}
    return library.prepare(db, ids, data.account_ids, data.text_set_ids)

@router.patch("/{draft_id}", response_model=DraftOut)
def update_draft(draft_id: int, data: DraftUpdate, db: Session = Depends(get_db)):
    library.begin_write(db)
    d = _get_or_404(db, draft_id)
    editable(d)
    changes = data.model_dump(exclude_unset=True)
    aid = changes.get("account_id", d.account_id)
    account_exists(db, aid)
    if aid and library.pair_exists(db, d.video_id, aid, d.id):
        raise HTTPException(409, "У аккаунта уже есть комментарий под этим роликом")
    if "text" in changes and changes["text"] is None:
        raise HTTPException(422, "Текст не может быть пустым")
    for field, value in changes.items():
        setattr(d, field, value)
    if "text" in changes:
        # A manually rewritten comment is no longer a verbatim library variant.
        d.variant_id = None
    db.flush()
    library.record(db, d)
    db.commit()
    db.expire(d, ["account"])
    return _out(d)


def check_manual(db, d):
    if d.task_id or d.sending or d.status not in ("draft", "review"):
        raise HTTPException(409, "Комментарий занят задачей или уже завершён")
    if not d.account:
        raise HTTPException(409, "Выберите аккаунт, с которого отправлен ответ")
    if d.account.status not in ("active", "working"):
        raise HTTPException(409, "Аккаунт неактивен")
    if library.pair_exists(db, d.video_id, d.account_id, d.id):
        # Its own review record is intentionally ignored for explicit confirmation.
        other = db.scalar(select(TextUse.id).where(TextUse.video_id == d.video_id,
            TextUse.account_id == d.account_id, TextUse.draft_id != d.id,
            TextUse.state.in_(["sent", "review"])))
        duplicate = db.scalar(select(Draft.id).where(Draft.id != d.id, Draft.video_id == d.video_id,
            Draft.account_id == d.account_id, Draft.status.in_(["draft", "sent", "review"])))
        if other or duplicate:
            raise HTTPException(409, "Этот аккаунт уже имеет комментарий под роликом")
    start = datetime.combine(datetime.now().date(), time.min)
    sent = db.scalar(select(func.count()).select_from(TextUse).where(TextUse.account_id == d.account_id,
        TextUse.state.in_(["sent", "review"]), TextUse.sent_at >= start)) or 0
    sent += db.scalar(select(func.count()).select_from(Draft).where(Draft.account_id == d.account_id, Draft.sending == 1)) or 0
    if d.account.daily_reply_limit and sent >= d.account.daily_reply_limit and d.status != "review":
        raise HTTPException(409, "Дневной лимит исчерпан")

@router.post("/{draft_id}/sent", response_model=DraftOut)
def mark_sent(draft_id: int, db: Session = Depends(get_db)):
    library.begin_write(db)
    d = _get_or_404(db, draft_id)
    check_manual(db, d)
    d.status, d.sent_at, d.result = "sent", datetime.now(), "Подтверждено вручную"
    d.video.status = "replied"
    library.finish(db, d, "sent")
    db.commit()
    return _out(d)

@router.post("/{draft_id}/skip", response_model=DraftOut)
def mark_skipped(draft_id: int, db: Session = Depends(get_db)):
    library.begin_write(db)
    d = _get_or_404(db, draft_id)
    editable(d)
    d.status = "skipped"
    library.release(db, d.id)
    db.commit()
    return _out(d)

class ReviewIn(BaseModel):
    published: bool

@router.post("/{draft_id}/resolve", response_model=DraftOut)
def resolve(draft_id: int, data: ReviewIn, db: Session = Depends(get_db)):
    if data.published:
        return mark_sent(draft_id, db)
    library.begin_write(db)
    d = _get_or_404(db, draft_id)
    if d.status != "review" or d.task_id:
        raise HTTPException(409, "Комментарий не ожидает проверки или задача ещё завершается")
    # Keep wording reserved while allowing an explicit retry after manual inspection.
    library.finish(db, d, "reserved")
    d.status, d.result = "draft", "Вручную подтверждено отсутствие публикации"
    db.commit()
    return _out(d)

@router.post("/{draft_id}/auto-comment", status_code=202)
def auto_comment_draft(draft_id: int, visible: bool = False):
    return tasks.submit_batch(AutoCommentTaskIn(draft_ids=[draft_id], visible=visible))

@router.post("/bulk-delete")
def bulk_delete_drafts(data: DraftBulkDelete, db: Session = Depends(get_db)):
    library.begin_write(db)
    q = select(Draft)
    if data.ids is not None:
        q = q.where(Draft.id.in_(data.ids))
    else:
        q = q.where(Draft.status == (data.status or "draft"))
    items = list(db.scalars(q))
    if any(d.task_id or d.sending for d in items):
        raise HTTPException(409, "Часть комментариев находится в работе")
    for d in items:
        library.release(db, d.id)
        db.delete(d)
    db.commit()
    return {"deleted": len(items)}

@router.delete("/{draft_id}", status_code=204)
def delete_draft(draft_id: int, db: Session = Depends(get_db)):
    library.begin_write(db)
    d = _get_or_404(db, draft_id)
    if d.task_id or d.sending:
        raise HTTPException(409, "Комментарий находится в работе")
    library.release(db, d.id)
    db.delete(d)
    db.commit()
