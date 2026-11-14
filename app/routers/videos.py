from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Draft, DraftStatus, Video, VideoStatus
from app.schemas import VideoBulkDelete, VideoBulkUpdate, VideoOut, VideoPage, VideoUpdate
from app.services.logs import log_event
from app.services import text_library

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=VideoPage)
def list_videos(
    status_filter: VideoStatus | None = Query(None, alias="status"),
    query: str | None = None,
    q: str | None = Query(None, description="Поиск по автору и описанию"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    base = select(Video)
    if status_filter:
        base = base.where(Video.status == status_filter)
    if query:
        base = base.where(Video.query == query)
    if q:
        like = f"%{q}%"
        base = base.where(or_(Video.author.ilike(like), Video.description.ilike(like)))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    items = db.scalars(base.order_by(Video.found_at.desc(), Video.id.desc()).limit(limit).offset(offset)).all()
    return VideoPage(total=total, items=items)


@router.get("/queries", response_model=list[str])
def list_queries(db: Session = Depends(get_db)):
    return db.scalars(select(Video.query).distinct().order_by(Video.query)).all()


@router.patch("/{video_id}", response_model=VideoOut)
def update_video(video_id: int, data: VideoUpdate, db: Session = Depends(get_db)):
    text_library.begin_write(db)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ролик не найден")
    _guard(db, [video_id])
    video.status = data.status
    if data.status == VideoStatus.IRRELEVANT:
        _release(db, [video_id])
        db.execute(delete(Draft).where(Draft.video_id == video_id, Draft.status == DraftStatus.DRAFT))
    db.commit()
    return video


@router.post("/bulk-status")
def bulk_status(data: VideoBulkUpdate, db: Session = Depends(get_db)):
    text_library.begin_write(db)
    _guard(db, data.ids)
    res = db.execute(update(Video).where(Video.id.in_(data.ids)).values(status=data.status))
    if data.status == VideoStatus.IRRELEVANT:
        _release(db, data.ids)
        db.execute(delete(Draft).where(Draft.video_id.in_(data.ids), Draft.status == DraftStatus.DRAFT))
    db.commit()
    log_event("videos", f"Статус «{data.status}» у {res.rowcount} роликов", db=db)
    return {"updated": res.rowcount}


@router.post("/bulk-delete")
def bulk_delete_videos(data: VideoBulkDelete, db: Session = Depends(get_db)):
    text_library.begin_write(db)
    q = select(Video.id)
    if data.ids:
        q = q.where(Video.id.in_(data.ids))
    elif data.status:
        q = q.where(Video.status == data.status)
    ids = list(db.scalars(q))
    _guard(db, ids)
    _release(db, ids)
    db.execute(delete(Video).where(Video.id.in_(ids)))
    db.commit()
    return {"deleted": len(ids)}


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(video_id: int, db: Session = Depends(get_db)):
    text_library.begin_write(db)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ролик не найден")
    _guard(db, [video_id])
    _release(db, [video_id])
    db.delete(video)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _guard(db, ids):
    if db.scalar(select(Draft.id).where(Draft.video_id.in_(ids), Draft.task_id.is_not(None))):
        raise HTTPException(409, "У ролика есть комментарий в работе")


def _release(db, ids):
    for draft_id in db.scalars(select(Draft.id).where(Draft.video_id.in_(ids), Draft.status == "draft")):
        text_library.release(db, draft_id)
