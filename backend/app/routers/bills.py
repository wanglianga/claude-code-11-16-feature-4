from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, require_roles
from .. import models, schemas
from ..services.billing import add_adjustment, check_new_revisions, confirm_bill
from ..services import backfill as bf

router = APIRouter(prefix="/api/bills", tags=["bills"])


@router.get("", response_model=list[schemas.BillOut])
def list_bills(site_id: int | None = None, db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(
                   models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    stmt = select(models.Bill).order_by(models.Bill.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.Bill.site_id == site_id)
    bills = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        bills = [b for b in bills if b.site_id in allowed]
    return bills


def _load_bill(db, bill_id, user):
    bill = db.get(models.Bill, bill_id)
    if bill is None:
        raise HTTPException(404, "账单不存在")
    ensure_site_access(db, user, bill.site_id)
    return bill


@router.get("/{bill_id}", response_model=schemas.BillOut)
def get_bill(bill_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(require_roles(
                 models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    return _load_bill(db, bill_id, user)


@router.get("/{bill_id}/precheck")
def precheck(bill_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(require_roles(
                 models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    """确认前检查依据是否有新修订，并回显挂起原因。"""
    bill = _load_bill(db, bill_id, user)
    job = db.get(models.ReconciliationJob, bill.job_id)
    new_revs = check_new_revisions(db, job)
    return {
        "bill_status": bill.status,
        "new_revision_count": len(new_revs),
        "new_revisions": [
            {"id": r.id, "revision_no": r.revision_no, "status": r.status} for r in new_revs
        ],
        "job_message": job.message,
    }


@router.post("/{bill_id}/confirm", response_model=schemas.BillOut)
def confirm(bill_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    bill = _load_bill(db, bill_id, user)
    # confirm_bill 内部会自动把同站点已确认回补项关联到本后续结算账单，
    # 关联留痕保存在 BackfillBillLink.note，旧账本身金额不变。
    confirm_bill(db, bill, user)
    db.commit()
    db.refresh(bill)
    return bill


@router.get("/{bill_id}/payable", response_model=schemas.BackfillPayableOut)
def payable(bill_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(require_roles(
                models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    """当前应付：旧账金额 + 人工差额更正 + 已确认回补调整项（旧账不覆盖）。"""
    bill = _load_bill(db, bill_id, user)
    return bf.bill_payable_view(db, bill)


@router.get("/{bill_id}/backfill-items", response_model=list[schemas.BackfillItemOut])
def bill_backfill_items(bill_id: int, db: Session = Depends(get_db),
                        user: models.User = Depends(require_roles(
                            models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    bill = _load_bill(db, bill_id, user)
    return db.scalars(
        select(models.BackfillAdjustmentItem)
        .where(models.BackfillAdjustmentItem.bill_id == bill.id)
        .order_by(models.BackfillAdjustmentItem.id)
    ).all()


@router.post("/{bill_id}/adjustments", response_model=schemas.AdjustmentOut)
def create_adjustment(bill_id: int, body: schemas.AdjustmentIn,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    bill = _load_bill(db, bill_id, user)
    adj = add_adjustment(db, bill, body.component, body.corrected_value, body.reason, user)
    db.commit()
    db.refresh(adj)
    return adj


@router.get("/{bill_id}/adjustments", response_model=list[schemas.AdjustmentOut])
def list_adjustments(bill_id: int, db: Session = Depends(get_db),
                     user: models.User = Depends(require_roles(
                         models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    bill = _load_bill(db, bill_id, user)
    return db.scalars(
        select(models.BillAdjustment)
        .where(models.BillAdjustment.bill_id == bill.id)
        .order_by(models.BillAdjustment.id)
    ).all()
