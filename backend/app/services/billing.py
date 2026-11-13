"""账单确认与差额更正。"""
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .timeutil import money, quantize


def check_new_revisions(db: Session, job: models.ReconciliationJob) -> list[models.RecordRevision]:
    """确认前检查：作业冻结之后依据是否出现新修订。

    包括两类：
    1. 来源记录当前生效版本已不同于快照版本（下一轮提升后的修订）；
    2. 作业采用后到达、被标记 blocked_adopted 的更正（留待下一轮，同样阻止本轮确认）。

    已由跨月回补批次处理（backfilled）的修订不在此列：它们的差额已通过回补调整项
    单列结算，不能再作为“未处理修订”永久阻塞后续账单确认。
    """
    snapshot = db.scalars(
        select(models.JobInput).where(models.JobInput.job_id == job.id)
    ).all()
    new_rev_ids: set[int] = set()
    for snap in snapshot:
        if snap.revision_id is None:
            continue
        base_rev = db.get(models.RecordRevision, snap.revision_id)
        if base_rev is None:
            continue
        sr = db.get(models.SourceRecord, base_rev.source_record_id)
        if sr.current_revision_id and sr.current_revision_id != snap.revision_id:
            cur = db.get(models.RecordRevision, sr.current_revision_id)
            if cur is not None and cur.status != models.REV_BACKFILLED:
                new_rev_ids.add(sr.current_revision_id)
        later = db.scalars(
            select(models.RecordRevision).where(
                models.RecordRevision.source_record_id == sr.id,
                models.RecordRevision.id > snap.revision_id,
                models.RecordRevision.status != models.REV_BACKFILLED,
            )
        ).all()
        for rev in later:
            new_rev_ids.add(rev.id)
    return [db.get(models.RecordRevision, rid) for rid in sorted(new_rev_ids)]


def confirm_bill(db: Session, bill: models.Bill, user: models.User) -> models.Bill:
    if bill.status == models.BILL_CONFIRMED:
        raise HTTPException(status.HTTP_409_CONFLICT, "账单已确认")
    job = db.get(models.ReconciliationJob, bill.job_id)
    new_revs = check_new_revisions(db, job)
    if new_revs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"依据在作业冻结后出现 {len(new_revs)} 条新修订，请先发起下一轮核算再确认",
        )
    if bill.status == models.BILL_SUSPENDED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "账单处于挂起状态（未解释差额超阈值或存在挂起计量段），不得确认",
        )
    bill.status = models.BILL_CONFIRMED
    bill.confirmed_at = datetime.utcnow()
    bill.confirmed_by = user.username
    db.flush()
    # 新账单确认即把同站点已确认未关联的回补调整项自动单列带入后续结算；
    # 放在服务层保证任何确认入口（API/脚本/测试）行为一致。
    from .backfill import auto_link_on_settlement
    auto_link_on_settlement(db, bill)
    db.flush()
    return bill


def add_adjustment(
    db: Session,
    bill: models.Bill,
    component: str,
    corrected_value,
    reason: str,
    user: models.User,
) -> models.BillAdjustment:
    """已确认账单只允许差额更正：保留原值，以更正行体现 delta，不直接替换原字段。"""
    if bill.status not in (models.BILL_CONFIRMED, models.BILL_ADJUSTED):
        raise HTTPException(status.HTTP_409_CONFLICT, "只有已确认账单可以登记差额更正")
    field_map = {
        "charging_kwh": bill.charging_kwh,
        "estimated_kwh": bill.estimated_kwh,
        "public_kwh": bill.public_kwh,
        "unexplained_kwh": bill.unexplained_kwh,
        "charging_amount": bill.charging_amount,
        "public_amount": bill.public_amount,
    }
    if component not in field_map:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知更正成分 {component}")
    original = field_map[component]
    corrected = quantize(corrected_value, 4)
    adj = models.BillAdjustment(
        bill_id=bill.id,
        source_job_id=bill.job_id,
        component=component,
        original_value=original,
        corrected_value=corrected,
        delta=quantize(corrected - original, 4),
        reason=reason,
        created_by=user.username,
    )
    db.add(adj)
    bill.status = models.BILL_ADJUSTED
    db.flush()
    return adj
