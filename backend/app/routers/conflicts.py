from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, require_roles
from .. import models, schemas

router = APIRouter(prefix="/api/conflicts", tags=["order-conflicts"])


@router.get("", response_model=list[schemas.ConflictOut])
def list_conflicts(site_id: int | None = None, db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(
                       models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    stmt = select(models.OrderConflict).order_by(models.OrderConflict.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.OrderConflict.site_id == site_id)
    conflicts = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        conflicts = [c for c in conflicts if c.site_id in allowed]
    return conflicts


@router.post("/{conflict_id}/resolve", response_model=schemas.ConflictOut)
def resolve_conflict(conflict_id: int, body: schemas.ConflictResolveIn,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(require_roles(models.ROLE_OPERATOR))):
    conflict = db.get(models.OrderConflict, conflict_id)
    if conflict is None:
        raise HTTPException(404, "冲突不存在")
    if conflict.status == models.CONFLICT_RESOLVED:
        raise HTTPException(409, "冲突已处理")
    if body.resolved_order_id not in (conflict.order_a_id, conflict.order_b_id):
        raise HTTPException(400, "只能选择冲突中的两张订单之一")
    winner = db.get(models.ChargingOrder, body.resolved_order_id)
    loser_id = conflict.order_b_id if winner.id == conflict.order_a_id else conflict.order_a_id
    loser = db.get(models.ChargingOrder, loser_id)
    winner.conflict_id = None
    loser.status = models.ENTITY_SUPERSEDED
    loser.conflict_id = None
    conflict.status = models.CONFLICT_RESOLVED
    conflict.resolved_order_id = winner.id
    conflict.resolution_note = body.resolution_note
    conflict.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(conflict)
    return conflict
