from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import TextSet, TextVariant, AccountTextSet
from app.services import spintax
from app.services.sessions import require_account

router = APIRouter(prefix="/api/text-sets", tags=["text sets"])

class VariantIn(BaseModel):
    id: int | None = None
    body: str = Field(min_length=1, max_length=2000)

class SetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    variants: list[VariantIn] = Field(min_length=1, max_length=1000)

class Assignment(BaseModel):
    text_set_ids: list[int] = Field(max_length=1000)


def out(db, pool):
    return {"id": pool.id, "name": pool.name, "variants": [
        {"id": v.id, "body": v.body} for v in db.scalars(select(TextVariant).where(
            TextVariant.text_set_id == pool.id, TextVariant.archived == 0).order_by(TextVariant.id))]}

@router.get("")
def listing(db: Session = Depends(get_db)):
    return [out(db, p) for p in db.scalars(select(TextSet).where(TextSet.archived == 0).order_by(TextSet.name))]


def save_pool(data, db, pool=None):
    try:
        for v in data.variants:
            spintax.validate(v.body)
            if not v.body.strip():
                raise ValueError("Пустой текст")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if pool is None:
        pool = TextSet(name=data.name)
        db.add(pool)
        db.flush()
    old = {v.id: v for v in db.scalars(select(TextVariant).where(TextVariant.text_set_id == pool.id))}
    ids = [v.id for v in data.variants if v.id is not None]
    if len(ids) != len(set(ids)) or any(i not in old for i in ids):
        raise HTTPException(422, "Некорректные ID вариантов")
    pool.name = data.name
    for v in old.values():
        v.archived = 1
    for item in data.variants:
        if item.id in old and old[item.id].body == item.body:
            old[item.id].archived = 0
        else:
            db.add(TextVariant(text_set_id=pool.id, body=item.body))
    db.commit()
    return out(db, pool)

@router.post("", status_code=201)
def create(data: SetIn, db: Session = Depends(get_db)):
    return save_pool(data, db)

@router.put("/{pool_id}")
def edit(pool_id: int, data: SetIn, db: Session = Depends(get_db)):
    pool = db.get(TextSet, pool_id)
    if not pool or pool.archived:
        raise HTTPException(404, "Набор не найден")
    return save_pool(data, db, pool)

@router.delete("/{pool_id}", status_code=204)
def archive(pool_id: int, db: Session = Depends(get_db)):
    pool = db.get(TextSet, pool_id)
    if not pool:
        raise HTTPException(404, "Набор не найден")
    pool.archived = 1
    db.commit()

@router.put("/accounts/{account_id}")
def assign(account_id: int, data: Assignment, db: Session = Depends(get_db)):
    require_account(db, account_id)
    ids = set(data.text_set_ids)
    existing = set(db.scalars(select(TextSet.id).where(TextSet.id.in_(ids), TextSet.archived == 0)))
    if ids != existing:
        raise HTTPException(404, "Набор не найден")
    db.execute(delete(AccountTextSet).where(AccountTextSet.account_id == account_id))
    db.add_all([AccountTextSet(account_id=account_id, text_set_id=i) for i in ids])
    db.commit()
    return {"text_set_ids": sorted(ids)}
