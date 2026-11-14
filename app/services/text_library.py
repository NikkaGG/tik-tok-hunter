"""Transactional preparation and durable history shared by all accounts."""
import random
from datetime import datetime
from collections import Counter, defaultdict
from fastapi import HTTPException
from sqlalchemy import select, update, text
from app.models import Account, AccountTextSet, Draft, TextSet, TextVariant, TextUse, Video
from app.services import spintax


def normalize(value):
    return " ".join(value.casefold().split())


def begin_write(db):
    # Must precede reads: SQLite serializes writers across API and browser threads.
    db.execute(text("BEGIN IMMEDIATE"))


def release(db, draft_id):
    db.execute(update(TextUse).where(TextUse.draft_id == draft_id, TextUse.state == "reserved").values(state="released"))


def record(db, draft, shape=None):
    normalized = normalize(draft.text)
    repeated = bool(db.scalar(select(TextUse.id).where(TextUse.normalized == normalized,
        TextUse.state.in_(["reserved", "review", "sent"]), TextUse.draft_id != draft.id)))
    release(db, draft.id)
    draft.repeated = int(repeated)
    db.add(TextUse(draft_id=draft.id, account_id=draft.account_id, video_id=draft.video_id,
        text_set_id=draft.text_set_id, variant_id=draft.variant_id,
        normalized=normalized, shape=normalize(shape if shape is not None else draft.text), state="reserved"))
    return repeated


def finish(db, draft, state):
    uses = db.scalars(select(TextUse).where(TextUse.draft_id == draft.id, TextUse.state.in_(["reserved", "review"]))).all()
    for use in uses:
        use.state = state
        use.account_id = draft.account_id
        if state in ("sent", "review"):
            use.sent_at = datetime.now()


def pair_exists(db, video_id, account_id, exclude=None):
    q = select(Draft.id).where(Draft.video_id == video_id, Draft.account_id == account_id,
                              Draft.status.in_(["draft", "sent", "review"]))
    if exclude is not None:
        q = q.where(Draft.id != exclude)
    return bool(db.scalar(q) or db.scalar(select(TextUse.id).where(TextUse.video_id == video_id,
        TextUse.account_id == account_id, TextUse.state.in_(["sent", "review"]))))


def preview(db, video_ids, account_ids, text_set_ids):
    """Read-only estimate. Actual reservations are rechecked in prepare's transaction."""
    videos = set(db.scalars(select(Video.id).where(Video.id.in_(video_ids))))
    accounts = list(db.scalars(select(Account).where(Account.id.in_(account_ids))))
    pools = set(db.scalars(select(TextSet.id).where(TextSet.id.in_(text_set_ids), TextSet.archived == 0)))
    if len(videos) != len(set(video_ids)) or len(accounts) != len(set(account_ids)):
        raise HTTPException(404, "Один из роликов или аккаунтов не найден")
    if len(pools) != len(set(text_set_ids)):
        raise HTTPException(404, "Набор не найден или архивирован")
    variants = list(db.scalars(select(TextVariant).where(TextVariant.text_set_id.in_(pools), TextVariant.archived == 0)))
    pairs = set(db.execute(select(Draft.video_id, Draft.account_id).where(Draft.video_id.in_(videos),
        Draft.account_id.in_(account_ids), Draft.status.in_(["draft", "sent", "review"]))).all())
    pairs.update(db.execute(select(TextUse.video_id, TextUse.account_id).where(TextUse.video_id.in_(videos),
        TextUse.account_id.in_(account_ids), TextUse.state.in_(["sent", "review"]))).all())
    rows, free = [], set()
    for account in accounts:
        usable = variants
        available = {v.id for v in usable}
        duplicates = sum((vid, account.id) in pairs for vid in videos)
        reason = None
        if account.status not in ("active", "working"):
            reason = "Аккаунт приостановлен или ограничен"
        elif not usable:
            reason = "В выбранных наборах нет текстов"
        candidates = len(videos) - duplicates if not reason else 0
        if candidates:
            free.update(available)
        rows.append({"account_id": account.id, "reason": reason, "candidate_count": candidates,
            "duplicate_count": duplicates, "available_variants": len(available)})
    candidates = sum(r["candidate_count"] for r in rows)
    return {"video_count": len(videos), "accounts": rows, "candidate_count": candidates,
            "available_variants": len(free), "maximum_drafts": candidates}


def prepare(db, video_ids, account_ids, text_set_ids):
    begin_write(db)
    videos = db.scalars(select(Video).where(Video.id.in_(video_ids))).all()
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    if len(videos) != len(set(video_ids)) or len(accounts) != len(set(account_ids)):
        raise HTTPException(404, "Один из роликов или аккаунтов не найден")
    pools = set(db.scalars(select(TextSet.id).where(TextSet.id.in_(text_set_ids), TextSet.archived == 0)))
    if len(pools) != len(set(text_set_ids)):
        raise HTTPException(404, "Набор не найден или архивирован")
    created, skipped = [], []
    uses = list(db.scalars(select(TextUse).where(TextUse.state.in_(["reserved", "sent", "review"]))))
    # Index history once instead of repeatedly scanning it for every generated
    # candidate. One write transaction still protects duplicate reservations.
    normalized_history, shape_history = defaultdict(set), defaultdict(set)
    history_times, busy, counts, last = [], set(), Counter(), {}

    def remember(normalized, shape, variant_id, state, timestamp):
        index = len(history_times)
        history_times.append(timestamp)
        normalized_history[normalized].add(index)
        shape_history[shape].add(index)
        if state in ('reserved', 'review'):
            busy.add(normalized)
        counts[variant_id] += 1
        last[variant_id] = max(last.get(variant_id, datetime.min), timestamp)

    for use in uses:
        remember(use.normalized, use.shape, use.variant_id, use.state, use.sent_at or use.created_at)

    def history_for(normalized, shape):
        ids = normalized_history.get(normalized, set()) | shape_history.get(shape, set())
        return len(ids), max((history_times[i] for i in ids), default=datetime.min)

    pairs = set(db.execute(select(Draft.video_id, Draft.account_id).where(
        Draft.video_id.in_(video_ids), Draft.account_id.in_(account_ids),
        Draft.status.in_(['draft', 'sent', 'review']))).all())
    pairs.update(db.execute(select(TextUse.video_id, TextUse.account_id).where(
        TextUse.video_id.in_(video_ids), TextUse.account_id.in_(account_ids),
        TextUse.state.in_(['sent', 'review']))).all())
    assignments = {}
    validated_variants = set()
    finite_shapes = {}
    for account in accounts:
        eligible = pools
        variants = list(db.scalars(select(TextVariant).where(TextVariant.text_set_id.in_(eligible), TextVariant.archived == 0)))
        assignments[account.id] = (eligible, variants)
    pending = []
    # Round-robin across accounts per video, rather than reserving everything for the first account.
    for video in videos:
        variables = spintax.video_variables(video)
        for account in accounts:
            eligible, variants = assignments[account.id]
            reason = None
            if account.status not in ("active", "working"):
                reason = "Аккаунт приостановлен или ограничен"
            elif not eligible or not variants:
                reason = "Нет текстов в выбранных наборах аккаунта"
            elif (video.id, account.id) in pairs:
                reason = "Для этой пары ролик/аккаунт уже есть комментарий"
            if reason:
                skipped.append({"video_id": video.id, "account_id": account.id, "reason": reason})
                continue
            choices = list(variants)
            random.shuffle(choices)
            choices.sort(key=lambda v: (counts[v.id], last.get(v.id, datetime.min)))
            selected = None
            ranked = []
            for variant in choices:
                if variant.id not in validated_variants:
                    spintax.validate(variant.body)
                    validated_variants.add(variant.id)
                    combinations = spintax.count_variants(variant.body)
                    if combinations <= 100:
                        shapes = set()
                        for _ in range(max(100, combinations * 12)):
                            shapes.add(spintax._spin_validated(variant.body))
                            if len(shapes) == combinations:
                                finite_shapes[variant.id] = tuple(shapes)
                                break
                candidates = {}
                seen_shapes = set()
                # Reuse a complete small set of expansions across videos.
                # Larger or ambiguous sets retain the existing random sampling.
                if variant.id in finite_shapes:
                    shapes = list(finite_shapes[variant.id])
                    random.shuffle(shapes)
                else:
                    shapes = (spintax._spin_validated(variant.body) for _ in range(100))
                for shape in shapes:
                    normalized_shape = normalize(shape)
                    if normalized_shape in seen_shapes:
                        continue
                    seen_shapes.add(normalized_shape)
                    rendered = spintax._VAR_RE.sub(lambda m: str(variables.get(m[1], m[0])), shape).strip()
                    if not rendered or len(rendered) > 2000:
                        continue
                    norm = normalize(rendered)
                    history_count, last_use = history_for(norm, normalized_shape)
                    score = (norm in busy, history_count, last_use)
                    candidates[norm] = (score, rendered, shape)
                if candidates:
                    _, rendered, shape = min(candidates.values(), key=lambda v: v[0])
                    history_count, _ = history_for(normalize(rendered), normalize(shape))
                    ranked.append(((normalize(rendered) in busy, bool(history_count), counts[variant.id], last.get(variant.id, datetime.min), history_count), variant, rendered, shape))
            if ranked:
                _, variant, rendered, shape = min(ranked, key=lambda item: item[0])
                selected = variant, rendered, shape
            if not selected:
                skipped.append({"video_id": video.id, "account_id": account.id,
                    "reason": "Не удалось получить непустой текст до 2000 символов из выбранных вариантов"})
                continue
            variant, rendered, shape = selected
            draft = Draft(video_id=video.id, account_id=account.id, text=rendered,
                          text_set_id=variant.text_set_id, variant_id=variant.id)
            normalized, normalized_shape = normalize(rendered), normalize(shape)
            repeated = bool(normalized_history.get(normalized) or shape_history.get(normalized_shape))
            draft.repeated = int(repeated)
            db.add(draft)
            pending.append((draft, video, variant, normalized, normalized_shape, repeated))
            pairs.add((video.id, account.id))
            remember(normalized, normalized_shape, variant.id, 'reserved', datetime.now())
    db.flush()
    for draft, video, variant, normalized, normalized_shape, repeated in pending:
        db.add(TextUse(draft_id=draft.id, account_id=draft.account_id, video_id=video.id,
                       text_set_id=variant.text_set_id, variant_id=variant.id,
                       normalized=normalized, shape=normalized_shape, state='reserved'))
        created.append({"id": draft.id, "account_id": draft.account_id, "video_id": video.id,
                        "text": draft.text, "repeated": repeated, "video_author": video.author,
                        "video_url": video.url, "text_set_id": variant.text_set_id})
    db.commit()
    return {"created": created, "skipped": skipped}
