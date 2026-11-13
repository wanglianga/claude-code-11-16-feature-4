"""跨月回补：批次、分段进度、调整项、双方复核、后续账单关联。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, get_current_user, require_roles
from .. import models, schemas
from ..services import backfill as bf

router = APIRouter(prefix="/api/backfill", tags=["backfill"])


def _load_batch(db: Session, batch_id: int, user: models.User) -> models.BackfillBatch:
    batch = db.get(models.BackfillBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "回补批次不存在")
    ensure_site_access(db, user, batch.site_id)
    return batch


def _load_item(db: Session, item_id: int, user: models.User) -> models.BackfillAdjustmentItem:
    item = db.get(models.BackfillAdjustmentItem, item_id)
    if item is None:
        raise HTTPException(404, "回补调整项不存在")
    ensure_site_access(db, user, item.site_id)
    return item


@router.get("/candidates")
def candidates(site_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(
                   models.ROLE_FINANCE, models.ROLE_OPERATOR))):
    """预览发起后将被冻结进下一批的更正候选（已确认月份相关）。"""
    ensure_site_access(db, user, site_id)
    site = db.get(models.Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    out = []
    for c in bf.discover_candidates(db, site):
        out.append({
            "source_record_id": c.source_record.id,
            "source_type": c.source_record.source_type,
            "source_record_no": c.source_record.source_record_no,
            "revision_id": c.revision.id,
            "revision_no": c.revision.revision_no,
            "change_kind": c.change_kind,
        })
    return out


@router.post("/batches", response_model=schemas.BackfillBatchOut)
def create_batch(body: schemas.BackfillBatchCreateIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    ensure_site_access(db, user, body.site_id)
    site = db.get(models.Site, body.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    try:
        batch = bf.create_backfill_batch(db, site, user.username, body.reason_note)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"创建回补批次失败: {exc}")
    db.refresh(batch)
    return batch


@router.post("/batches/{batch_id}/run", response_model=schemas.BackfillBatchOut)
def run_batch(batch_id: int, db: Session = Depends(get_db),
              user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    batch = _load_batch(db, batch_id, user)
    try:
        bf.run_backfill_batch(db, batch)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"回补执行失败: {exc}")
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=list[schemas.BackfillBatchOut])
def list_batches(site_id: int | None = None, db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(
                     models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    stmt = select(models.BackfillBatch).order_by(models.BackfillBatch.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.BackfillBatch.site_id == site_id)
    batches = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        from ..deps import user_site_ids
        allowed = user_site_ids(db, user)
        batches = [b for b in batches if b.site_id in allowed]
    return batches


@router.get("/batches/{batch_id}", response_model=schemas.BackfillBatchOut)
def get_batch(batch_id: int, db: Session = Depends(get_db),
              user: models.User = Depends(require_roles(
                  models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    return _load_batch(db, batch_id, user)


@router.get("/batches/{batch_id}/segments", response_model=list[schemas.BackfillSegmentOut])
def batch_segments(batch_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(
                       models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    _load_batch(db, batch_id, user)
    return db.scalars(select(models.BackfillSegment).where(
        models.BackfillSegment.batch_id == batch_id
    ).order_by(models.BackfillSegment.id)).all()


@router.get("/batches/{batch_id}/items", response_model=list[schemas.BackfillItemOut])
def batch_items(batch_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(require_roles(
                    models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    _load_batch(db, batch_id, user)
    return db.scalars(select(models.BackfillAdjustmentItem).where(
        models.BackfillAdjustmentItem.batch_id == batch_id
    ).order_by(models.BackfillAdjustmentItem.id)).all()


@router.get("/items/{item_id}", response_model=schemas.BackfillItemOut)
def get_item(item_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    return _load_item(db, item_id, user)


@router.post("/items/{item_id}/reviews", response_model=schemas.BackfillItemOut)
def review_item(item_id: int, body: schemas.BackfillReviewIn,
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    item = _load_item(db, item_id, user)
    # 物业只能处理授权站点；财务可确认本站点调整项
    if user.role == models.ROLE_PROPERTY:
        from ..deps import user_site_ids
        if item.site_id not in user_site_ids(db, user):
            raise HTTPException(403, "该调整项不在你的核对授权范围内")
    elif user.role != models.ROLE_FINANCE:
        raise HTTPException(403, "只有物业与财务可以复核回补调整项")
    try:
        bf.review_item(db, item, user, body.action, body.comment)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"复核失败: {exc}")
    db.refresh(item)
    return item


@router.get("/items/{item_id}/reviews", response_model=list[schemas.BackfillReviewOut])
def item_reviews(item_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    _load_item(db, item_id, user)
    return db.scalars(select(models.BackfillReview).where(
        models.BackfillReview.item_id == item_id
    ).order_by(models.BackfillReview.id)).all()


@router.post("/items/{item_id}/link", response_model=schemas.BackfillLinkOut)
def link_item(item_id: int, body: schemas.BackfillLinkIn,
              db: Session = Depends(get_db),
              user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    item = _load_item(db, item_id, user)
    settlement = db.get(models.Bill, body.settlement_bill_id)
    if settlement is None:
        raise HTTPException(404, "后续结算账单不存在")
    if settlement.site_id != item.site_id:
        raise HTTPException(400, "调整项与结算账单站点不一致")
    try:
        link = bf.link_to_settlement_bill(db, item, settlement, user)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"关联失败: {exc}")
    db.refresh(link)
    return link


@router.get("/items/{item_id}/links", response_model=list[schemas.BackfillLinkOut])
def item_links(item_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    _load_item(db, item_id, user)
    return db.scalars(select(models.BackfillBillLink).where(
        models.BackfillBillLink.item_id == item_id
    ).order_by(models.BackfillBillLink.id)).all()
