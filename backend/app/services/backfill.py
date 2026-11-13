"""跨月回补核算引擎。

设计原则（对应业务约束）：
1. 已确认账单金额永不覆盖：回补只产出调整项（delta），旧账原值冻结，应付同时引用旧账与调整项。
2. 先定位更正行曾参与的计量区间、电价段与账单，再“撤原贡献 + 计修正值”，
   绝不能把补来的累计读数整段再加一次——累计读数只重算受影响相邻 span 的差值。
3. 批次发起即冻结更正数据集合（BackfillBatchInput）；批次启动后新到的更正进入下一批。
4. 作业按受影响区间分段（BackfillSegment）保存进度，失败只重算未完成分段。
5. 同源修订按来源顺序收齐：缺少中间版本（或 replaces 链不连续）则该段挂起，
   不用最新一行猜测前面发生过什么。
6. 原贡献台账（BackfillContribution）保证每条贡献只撤回一次；后一批次看到前一批次的生效记录，
   在其基础上再撤再计，防止两个批次同时撤掉两遍。
7. 原始事件、原计算与回补差额逐项对平：before 必须与旧账留痕（split/interval/台账）一致，
   找不到来源的金额挂起，不进入结算；读数、电价、舍入变化分别落字段说明。
8. 调整项、双方复核、后续账单关联分别建表保存；物业否认某段依据只退回该调整，
   不阻断其他无争议月份。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from .importer import is_revocation_payload
from .reconciliation import build_spans, clip_span_energy, split_order
from .tariff_svc import segment_for_hour
from .timeutil import (
    duration_seconds,
    money,
    parse_local_datetime,
    quantize,
    utc_to_local,
)

settings = get_settings()
ZERO = Decimal("0")
Q4 = settings.energy_scale

CHANGE_LATE = "late"
CHANGE_CORRECTION = "correction"
CHANGE_REVOCATION = "revocation"

CONTRIB_ACTIVE = "active"
CONTRIB_WITHDRAWN = "withdrawn"


@dataclass
class Candidate:
    """回补候选：一个来源记录的目标修订 + 变更类别。"""
    source_record: models.SourceRecord
    revision: models.RecordRevision
    change_kind: str
    payload: dict


# ================= 候选发现 =================

def _confirmed_bills(db: Session, site_id: int) -> list[models.Bill]:
    return db.scalars(
        select(models.Bill).where(
            models.Bill.site_id == site_id,
            models.Bill.status.in_([models.BILL_CONFIRMED, models.BILL_ADJUSTED]),
        )
    ).all()


def _bill_for_event(db: Session, site_id: int, event_ts: datetime) -> models.Bill | None:
    for bill in _confirmed_bills(db, site_id):
        job = db.get(models.ReconciliationJob, bill.job_id)
        if job.cycle_start <= event_ts < job.cycle_end:
            return bill
    return None


def _already_batched(db: Session, site_id: int) -> tuple[set[int], set[int]]:
    """返回 (已入批来源记录集合, 已入批修订版本集合)。

    同一来源记录可以在不同批次携带不同修订版本；只排除已经冻结进某批次的具体版本，
    不能按来源记录一刀切，否则后续批次永远无法处理同一记录的新更正。
    """
    rows = db.execute(
        select(models.BackfillBatchInput.source_record_id,
               models.BackfillBatchInput.revision_id)
        .join(models.BackfillBatch,
              models.BackfillBatch.id == models.BackfillBatchInput.batch_id)
        .where(models.BackfillBatch.site_id == site_id)
    ).all()
    return {r[0] for r in rows}, {r[1] for r in rows}


def _event_ts(entity) -> datetime | None:
    for attr in ("start_ts", "reading_ts", "effective_date"):
        val = getattr(entity, attr, None)
        if val is not None:
            return val if isinstance(val, datetime) else datetime(val.year, val.month, val.day)
    return None


def _payload_event_ts(payload: dict, source_type: str, tz_name: str) -> datetime | None:
    field = {
        "order": "start_time",
        "master_reading": "reading_time",
        "device_reading": "reading_time",
        "public_load": "start_time",
        "tariff": "effective_date",
    }.get(source_type)
    if not field or not payload.get(field):
        return None
    text = str(payload[field])
    if len(text) == 10:
        return parse_local_datetime(text + " 00:00", tz_name)
    return parse_local_datetime(text, tz_name)


def discover_candidates(db: Session, site: models.Site) -> list[Candidate]:
    """发现可回补候选：
    A. 作业采用后到达、被 blocked_adopted 的更正/撤销修订，且影响已确认账单；
    B. 已生效但从未被作业采用的迟到实体（新记录号），业务时间落在已确认账单周期内。
    未确认月份的修订不在此列——它们走常规下一轮核算（promote 提升）。
    """
    _batched_sr_ids, batched_rev_ids = _already_batched(db, site.id)
    out: list[Candidate] = []

    blocked = db.scalars(
        select(models.RecordRevision)
        .join(models.SourceRecord,
              models.SourceRecord.id == models.RecordRevision.source_record_id)
        .where(
            models.SourceRecord.site_id == site.id,
            models.RecordRevision.status.in_([
                models.REV_BLOCKED_ADOPTED, models.REV_BACKFILLED]),
        ).order_by(models.RecordRevision.id)
    ).all()
    for rev in blocked:
        sr = db.get(models.SourceRecord, rev.source_record_id)
        if rev.id in batched_rev_ids:
            continue
        payload = json.loads(rev.payload_json)
        change_kind = CHANGE_REVOCATION if is_revocation_payload(payload) else CHANGE_CORRECTION
        if _candidate_bills(db, site.id, sr):
            out.append(Candidate(sr, rev, change_kind, payload))

    for model in (models.ChargingOrder, models.MeterReading,
                  models.PublicLoad, models.Tariff):
        for entity in db.scalars(
            select(model).where(
                model.site_id == site.id,
                model.status == models.ENTITY_ACTIVE,
                model.adopted_by_job_id.is_(None),
                model.source_record_id.is_not(None),
            )
        ).all():
            sr = db.get(models.SourceRecord, entity.source_record_id)
            if sr is None or entity.revision_id in batched_rev_ids:
                continue
            rev = db.get(models.RecordRevision, entity.revision_id)
            if rev is None or rev.status == models.REV_BACKFILLED:
                continue
            event_ts = _event_ts(entity)
            if event_ts and _bill_for_event(db, site.id, event_ts):
                out.append(Candidate(sr, rev, CHANGE_LATE, json.loads(rev.payload_json)))

    dedup: dict[int, Candidate] = {}
    for c in out:
        dedup[c.source_record.id] = c
    return sorted(dedup.values(), key=lambda c: c.revision.id)


def _candidate_bills(db: Session, site_id: int, sr: models.SourceRecord) -> list[models.Bill]:
    bills: dict[int, models.Bill] = {}
    for model in (models.MeterReading, models.ChargingOrder,
                  models.PublicLoad, models.Tariff):
        for entity in db.scalars(
            select(model).where(
                model.source_record_id == sr.id,
                model.adopted_by_job_id.is_not(None),
            )
        ).all():
            bill = db.scalar(select(models.Bill).where(
                models.Bill.job_id == entity.adopted_by_job_id))
            if bill and bill.site_id == site_id and bill.status in (
                models.BILL_CONFIRMED, models.BILL_ADJUSTED
            ):
                bills[bill.id] = bill
    return list(bills.values())


# ================= 同源修订链收齐校验 =================

def check_revision_chain(db: Session, sr: models.SourceRecord,
                         target_rev: models.RecordRevision,
                         base_rev_no: int | None) -> dict:
    """base_rev_no+1 .. target_rev_no 必须连续存在；声明了 replaces_revision_no 时必须衔接。
    缺中间版本即判定不收齐，调用方必须挂起而不是猜测。"""
    revs = {r.revision_no: r for r in db.scalars(
        select(models.RecordRevision).where(
            models.RecordRevision.source_record_id == sr.id,
            models.RecordRevision.revision_no <= target_rev.revision_no,
        )
    ).all()}
    missing: list[int] = []
    broken_chain: list[str] = []
    if base_rev_no is not None:
        for no in range(base_rev_no + 1, target_rev.revision_no + 1):
            if no not in revs:
                missing.append(no)
        prev_no = base_rev_no
        for no in range(base_rev_no + 1, target_rev.revision_no + 1):
            rev = revs.get(no)
            if rev is None:
                continue
            declared = json.loads(rev.payload_json).get("_replaces_revision_no")
            if declared is not None and declared != prev_no:
                broken_chain.append(
                    f"版本 {no} 声明承接版本 {declared}，实际前序应为版本 {prev_no}")
            prev_no = no
    return {
        "complete": not missing and not broken_chain,
        "missing": missing,
        "broken_chain": broken_chain,
        "base_rev_no": base_rev_no,
        "target_rev_no": target_rev.revision_no,
    }


# ================= 批次创建与分段规划 =================

def create_backfill_batch(db: Session, site: models.Site, username: str,
                          reason_note: str | None = None) -> models.BackfillBatch:
    candidates = discover_candidates(db, site)
    if not candidates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "没有可回补的更正数据（已确认月份无待处理修订/迟到记录）")
    batch = models.BackfillBatch(
        site_id=site.id,
        batch_no=f"BF-{site.code}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        status=models.BF_BATCH_PENDING,
        reason_note=reason_note,
        created_by=username,
    )
    db.add(batch)
    db.flush()
    for c in candidates:
        db.add(models.BackfillBatchInput(
            batch_id=batch.id,
            source_record_id=c.source_record.id,
            revision_id=c.revision.id,
            source_type=c.source_record.source_type,
            source_record_no=c.source_record.source_record_no,
            change_kind=c.change_kind,
        ))
    db.flush()
    _plan_segments(db, site, batch, candidates)
    db.flush()
    return batch


def _adopted_entities_for_sr(db: Session, sr_id: int):
    found = []
    for model in (models.MeterReading, models.ChargingOrder,
                  models.PublicLoad, models.Tariff):
        found.extend(db.scalars(select(model).where(
            model.source_record_id == sr_id,
            model.adopted_by_job_id.is_not(None),
        )).all())
    return found


def _intervals_of_bill(db: Session, bill: models.Bill) -> list[models.ReconciliationInterval]:
    return db.scalars(
        select(models.ReconciliationInterval).where(
            models.ReconciliationInterval.job_id == bill.job_id
        ).order_by(models.ReconciliationInterval.start_ts)
    ).all()


def _locate_intervals(db: Session, site: models.Site, c: Candidate
                      ) -> list[tuple[models.Bill, models.ReconciliationInterval]]:
    """定位更正行曾参与的区间与账单。"""
    out: list[tuple[models.Bill, models.ReconciliationInterval]] = []
    seen: set[int] = set()
    entities = _adopted_entities_for_sr(db, c.source_record.id)

    if not entities:
        event_ts = _payload_event_ts(c.payload, c.source_record.source_type, site.timezone)
        bill = _bill_for_event(db, site.id, event_ts) if event_ts else None
        if bill is not None:
            for iv in _intervals_of_bill(db, bill):
                if event_ts and iv.start_ts <= event_ts < iv.end_ts:
                    out.append((bill, iv))
        return out

    for entity in entities:
        bill = db.scalar(select(models.Bill).where(
            models.Bill.job_id == entity.adopted_by_job_id))
        if bill is None or bill.status not in (models.BILL_CONFIRMED, models.BILL_ADJUSTED):
            continue
        intervals = _intervals_of_bill(db, bill)
        if isinstance(entity, models.ChargingOrder):
            split_iv_ids = {s.reconciliation_interval_id for s in db.scalars(
                select(models.OrderIntervalSplit).where(
                    models.OrderIntervalSplit.job_id == bill.job_id,
                    models.OrderIntervalSplit.order_id == entity.id,
                )).all() if s.reconciliation_interval_id is not None}
            for iv in intervals:
                if iv.id in split_iv_ids and iv.id not in seen:
                    seen.add(iv.id)
                    out.append((bill, iv))
        elif isinstance(entity, models.MeterReading):
            # 累计读数只影响以该读数为边界的相邻计量区间，不整段重加
            for iv in intervals:
                if iv.start_ts <= entity.reading_ts <= iv.end_ts and iv.id not in seen:
                    seen.add(iv.id)
                    out.append((bill, iv))
        elif isinstance(entity, models.PublicLoad):
            for iv in intervals:
                if entity.start_ts < iv.end_ts and iv.start_ts < entity.end_ts and iv.id not in seen:
                    seen.add(iv.id)
                    out.append((bill, iv))
        elif isinstance(entity, models.Tariff):
            for iv in intervals:
                if iv.id not in seen:
                    seen.add(iv.id)
                    out.append((bill, iv))
    return out


def _plan_segments(db: Session, site: models.Site, batch: models.BackfillBatch,
                   candidates: list[Candidate]) -> None:
    planned: set[int] = set()
    for c in candidates:
        for bill, interval in _locate_intervals(db, site, c):
            if interval.id in planned:
                continue
            planned.add(interval.id)
            db.add(models.BackfillSegment(
                batch_id=batch.id, site_id=site.id,
                bill_id=bill.id, job_id=bill.job_id,
                reconciliation_interval_id=interval.id,
                start_ts=interval.start_ts, end_ts=interval.end_ts,
                start_ts_local=interval.start_ts_local,
                end_ts_local=interval.end_ts_local,
                status=models.BF_SEG_PENDING,
            ))
    batch.segment_total = len(planned)


# ================= 原贡献台账（防双撤） =================

def ensure_ledger_seeded(db: Session, bill: models.Bill) -> None:
    """从已确认账单的留痕幂等播种原贡献台账。"""
    exists = db.scalar(
        select(models.BackfillContribution.id).where(
            models.BackfillContribution.bill_id == bill.id).limit(1))
    if exists:
        return
    job = db.get(models.ReconciliationJob, bill.job_id)
    site = db.get(models.Site, bill.site_id)

    # 订单电价段贡献
    for sp in db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id)).all():
        order = db.get(models.ChargingOrder, sp.order_id)
        if order is None or sp.energy_kwh is None:
            continue
        db.add(models.BackfillContribution(
            site_id=bill.site_id, source_record_id=order.source_record_id,
            revision_id=order.revision_id, entity_kind="order",
            entity_id=order.id, job_id=job.id, bill_id=bill.id,
            reconciliation_interval_id=sp.reconciliation_interval_id,
            order_split_id=sp.id, segment_name=sp.segment_name,
            price_per_kwh=sp.price_per_kwh,
            energy_kwh=sp.energy_kwh or ZERO, amount=sp.amount or ZERO,
            status=CONTRIB_ACTIVE))

    # 经确认公共负荷贡献（按区间时长裁剪）
    for pl in db.scalars(select(models.PublicLoad).where(
            models.PublicLoad.adopted_by_job_id == job.id)).all():
        if not pl.confirmed:
            continue
        for iv in _intervals_of_bill(db, bill):
            full = duration_seconds(pl.start_ts, pl.end_ts)
            part = duration_seconds(max(pl.start_ts, iv.start_ts),
                                    min(pl.end_ts, iv.end_ts))
            if part <= 0:
                continue
            energy = quantize(pl.energy_kwh * Decimal(str(part / full)), Q4)
            mid = pl.start_ts + (pl.end_ts - pl.start_ts) / 2
            _, price = _frozen_price(db, site, job, mid)
            db.add(models.BackfillContribution(
                site_id=bill.site_id, source_record_id=pl.source_record_id or 0,
                revision_id=pl.revision_id, entity_kind="public_load",
                entity_id=pl.id, job_id=job.id, bill_id=bill.id,
                reconciliation_interval_id=iv.id, segment_name=None,
                price_per_kwh=price, energy_kwh=energy,
                amount=money(energy * price, settings.money_scale),
                status=CONTRIB_ACTIVE))
    # 说明：读数类贡献的撤回防护由分段调整项幂等（同段同源只产出一次）+
    # build_spans 重算保证，不在台账重复登记，避免累计读数被当作整段贡献再加一次。
    db.flush()


def _frozen_tariff_ids(db: Session, job: models.ReconciliationJob) -> set[int]:
    return {s.entity_id for s in db.scalars(select(models.JobInput).where(
        models.JobInput.job_id == job.id,
        models.JobInput.input_kind == "tariff")).all()}


def _frozen_price(db: Session, site: models.Site, job: models.ReconciliationJob,
                  ts_utc: datetime):
    from .tariff_svc import price_at_utc
    return price_at_utc(db, site.id, site.timezone, ts_utc, _frozen_tariff_ids(db, job))


def _latest_contributions(db: Session, sr_id: int, interval_id: int,
                          entity_kind: str) -> list[models.BackfillContribution]:
    """同一贡献在多批次后的最新状态：订单/电价类按电价时段分组取最新行
    （后批次据此在前批次生效记录上再撤再计，不会把已撤回行当作当前贡献）。"""
    rows = db.scalars(select(models.BackfillContribution).where(
        models.BackfillContribution.source_record_id == sr_id,
        models.BackfillContribution.reconciliation_interval_id == interval_id,
        models.BackfillContribution.entity_kind == entity_kind,
    ).order_by(models.BackfillContribution.id)).all()
    latest: dict[str | None, models.BackfillContribution] = {}
    for r in rows:
        latest[r.segment_name] = r
    return list(latest.values())


# ================= 批次执行（分段进度 + 断点续算） =================

def run_backfill_batch(db: Session, batch: models.BackfillBatch) -> models.BackfillBatch:
    site = db.get(models.Site, batch.site_id)
    if batch.status == models.BF_BATCH_RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "批次正在执行中")
    batch.status = models.BF_BATCH_RUNNING
    batch.started_at = batch.started_at or datetime.utcnow()
    db.flush()

    inputs = db.scalars(select(models.BackfillBatchInput).where(
        models.BackfillBatchInput.batch_id == batch.id)).all()
    candidates: list[Candidate] = []
    for inp in inputs:
        rev = db.get(models.RecordRevision, inp.revision_id)
        sr = db.get(models.SourceRecord, inp.source_record_id)
        candidates.append(Candidate(sr, rev, inp.change_kind, json.loads(rev.payload_json)))

    segments = db.scalars(select(models.BackfillSegment).where(
        models.BackfillSegment.batch_id == batch.id,
        models.BackfillSegment.status.in_([
            models.BF_SEG_PENDING, models.BF_SEG_FAILED, models.BF_SEG_SUSPENDED]),
    ).order_by(models.BackfillSegment.id)).all()

    any_failed = False
    any_suspended = False
    for seg in segments:
        seg.attempts += 1
        seg.status = models.BF_SEG_RUNNING
        db.flush()  # 分段进度检查点：running 状态先落库
        try:
            # 每段独立 SAVEPOINT：本段失败只回滚本段，已完成段结果保留
            with db.begin_nested():
                suspended_reason = _process_segment(db, site, batch, seg, candidates)
            if suspended_reason:
                seg.status = models.BF_SEG_SUSPENDED
                seg.suspended_reason = suspended_reason
                any_suspended = True
            else:
                seg.status = models.BF_SEG_DONE
                seg.suspended_reason = None
            seg.last_error = None
            seg.processed_at = datetime.utcnow()
            db.flush()
        except Exception as exc:  # 本段失败不影响其他段，续算只重算未完成段
            seg.status = models.BF_SEG_FAILED
            seg.last_error = str(exc)[:500]
            seg.suspended_reason = None
            db.flush()
            any_failed = True

    batch.segment_done = _count_segments(db, batch.id, models.BF_SEG_DONE)
    batch.segment_suspended = _count_segments(db, batch.id, models.BF_SEG_SUSPENDED)
    batch.segment_failed = _count_segments(db, batch.id, models.BF_SEG_FAILED)
    if any_failed:
        batch.status = models.BF_BATCH_FAILED
        batch.message = "存在执行失败分段，修正后可续算（已完成分段不重算）"
    elif any_suspended:
        batch.status = models.BF_BATCH_SUSPENDED
        batch.message = "存在挂起分段（同源修订未收齐），补齐中间版本后续算"
    else:
        batch.status = models.BF_BATCH_COMPLETED
        batch.message = "全部受影响区间回补完成，调整项待双方复核"
    batch.finished_at = datetime.utcnow()
    _mark_revisions_backfilled(db, batch)
    db.flush()
    return batch


def _count_segments(db: Session, batch_id: int, st: str) -> int:
    return len(db.scalars(select(models.BackfillSegment).where(
        models.BackfillSegment.batch_id == batch_id,
        models.BackfillSegment.status == st)).all())


def _mark_revisions_backfilled(db: Session, batch: models.BackfillBatch) -> None:
    """已产出有效调整项（非挂起）的修订标记 backfilled，不再被常规作业提升。"""
    item_rev_ids = set(db.scalars(
        select(models.BackfillAdjustmentItem.to_revision_id).where(
            models.BackfillAdjustmentItem.batch_id == batch.id,
            models.BackfillAdjustmentItem.status != models.BF_ADJ_SUSPENDED,
            models.BackfillAdjustmentItem.to_revision_id.is_not(None))).all())
    for rev_id in item_rev_ids:
        rev = db.get(models.RecordRevision, rev_id)
        if rev is not None and rev.status == models.REV_BLOCKED_ADOPTED:
            rev.status = models.REV_BACKFILLED


# ================= 单段处理 =================

def _process_segment(db: Session, site: models.Site, batch: models.BackfillBatch,
                     seg: models.BackfillSegment,
                     candidates: list[Candidate]) -> str | None:
    bill = db.get(models.Bill, seg.bill_id)
    ensure_ledger_seeded(db, bill)
    interval = db.get(models.ReconciliationInterval, seg.reconciliation_interval_id)
    suspension: list[str] = []

    relevant = [c for c in candidates
                if _candidate_touches_interval(db, site, c, interval.id)]
    if not relevant:
        return None

    for c in relevant:
        # 幂等：该段该来源已产出非挂起结果则不重复处理（断点续算/防双撤）
        existing = db.scalars(select(models.BackfillAdjustmentItem).where(
            models.BackfillAdjustmentItem.segment_id == seg.id,
            models.BackfillAdjustmentItem.source_record_id == c.source_record.id,
        )).all()
        live = [i for i in existing if i.status != models.BF_ADJ_SUSPENDED]
        if live:
            continue
        for stale in [i for i in existing if i.status == models.BF_ADJ_SUSPENDED]:
            db.delete(stale)
        db.flush()

        base_rev_no = _base_revision_no(db, bill, c)
        chain = check_revision_chain(db, c.source_record, c.revision, base_rev_no)
        if not chain["complete"]:
            reason = (
                f"同源修订未收齐：缺版本 {chain['missing']}；{'; '.join(chain['broken_chain'])}。"
                f"已收到版本 {chain['target_rev_no']}，挂起等待中间版本，不使用最新行推断历史。"
            )
            _suspend_item(db, batch, seg, bill, c, reason)
            suspension.append(reason)
            exists_task = db.scalar(select(models.ReviewTask.id).where(
                models.ReviewTask.site_id == site.id,
                models.ReviewTask.task_kind == "backfill_suspended",
                models.ReviewTask.title.like(
                    f"回补 {c.source_record.source_record_no}%")).limit(1))
            if not exists_task:
                db.add(models.ReviewTask(
                    site_id=site.id, job_id=bill.job_id,
                    task_kind="backfill_suspended",
                    title=f"回补 {c.source_record.source_record_no} 修订链不完整，分段挂起",
                    detail=reason, assignee=models.ROLE_OPERATOR))
            continue

        stype = c.source_record.source_type
        seg_reason = None
        if stype == "order":
            seg_reason = _process_order_change(db, site, batch, seg, bill, interval, c, base_rev_no)
        elif stype in ("master_reading", "device_reading"):
            seg_reason = _process_reading_change(db, site, batch, seg, bill, interval, c)
        elif stype == "public_load":
            seg_reason = _process_public_change(db, site, batch, seg, bill, interval, c)
        elif stype == "tariff":
            seg_reason = _process_tariff_change(db, site, batch, seg, bill, interval, c)
        if seg_reason:
            suspension.append(seg_reason)
    db.flush()
    return "；".join(suspension) if suspension else None


def _candidate_touches_interval(db: Session, site: models.Site, c: Candidate,
                                interval_id: int) -> bool:
    return any(iv.id == interval_id for _bill, iv in _locate_intervals(db, site, c))


def _base_revision_no(db: Session, bill: models.Bill, c: Candidate) -> int | None:
    """旧作业对该来源记录实际采用的版本号；迟到记录无采用历史 => None。"""
    if c.change_kind == CHANGE_LATE:
        return None
    snap = db.scalars(select(models.JobInput).where(
        models.JobInput.job_id == bill.job_id,
        models.JobInput.revision_id.is_not(None))).all()
    rev_ids = {s.revision_id for s in snap}
    if not rev_ids:
        return None
    revs = db.scalars(select(models.RecordRevision).where(
        models.RecordRevision.source_record_id == c.source_record.id,
        models.RecordRevision.id.in_(rev_ids),
    )).all()
    if not revs:
        return None
    return max(r.revision_no for r in revs)


def _suspend_item(db, batch, seg, bill, c: Candidate, reason: str) -> None:
    db.add(models.BackfillAdjustmentItem(
        batch_id=batch.id, segment_id=seg.id, site_id=bill.site_id, bill_id=bill.id,
        source_job_id=bill.job_id, source_record_id=c.source_record.id,
        from_revision_id=None, to_revision_id=c.revision.id,
        change_kind=c.change_kind, reason_kind=_reason_kind(c),
        component=models.BF_COMP_ROUNDING,
        detail_json=json.dumps({"suspended": True, "payload": c.payload},
                               ensure_ascii=False),
        reason_text=reason, status=models.BF_ADJ_SUSPENDED,
        suspended_reason=reason, created_by=batch.created_by))


def _reason_kind(c: Candidate) -> str:
    if c.change_kind == CHANGE_LATE:
        return models.BF_REASON_LATE_ORDER
    if c.change_kind == CHANGE_REVOCATION:
        return models.BF_REASON_DUPLICATE_REVOKED
    return {
        "order": models.BF_REASON_CORRECTED_READING,
        "master_reading": models.BF_REASON_CORRECTED_READING,
        "device_reading": models.BF_REASON_CORRECTED_READING,
        "public_load": models.BF_REASON_PUBLIC_CORRECTION,
        "tariff": models.BF_REASON_TARIFF_CORRECTION,
    }.get(c.source_record.source_type, models.BF_REASON_CORRECTED_READING)


# ================= 订单类更正（迟到/纠正/撤销） =================

def _transient_order(payload: dict, site: models.Site):
    start = parse_local_datetime(payload["start_time"], site.timezone)
    end = parse_local_datetime(payload["end_time"], site.timezone)
    if end <= start:
        raise ValueError("订单结束时间必须晚于开始时间")
    sr = payload.get("start_reading")
    er = payload.get("end_reading")
    return SimpleNamespace(
        order_no=payload.get("order_no"),
        device_id=payload["device_id"],
        start_ts=start, end_ts=end,
        start_ts_local=utc_to_local(start, site.timezone),
        end_ts_local=utc_to_local(end, site.timezone),
        start_reading_kwh=Decimal(str(sr)) if sr not in (None, "") else None,
        end_reading_kwh=Decimal(str(er)) if er not in (None, "") else None,
        reported_energy_kwh=Decimal(str(payload["reported_energy"])),
    )


def _job_context(db: Session, bill: models.Bill):
    job = db.get(models.ReconciliationJob, bill.job_id)
    site = db.get(models.Site, bill.site_id)
    snapshot_ids = {s.entity_id for s in db.scalars(select(models.JobInput).where(
        models.JobInput.job_id == job.id,
        models.JobInput.input_kind == "reading")).all()}
    if snapshot_ids:
        readings = db.scalars(select(models.MeterReading).where(
            models.MeterReading.id.in_(snapshot_ids))).all()
    else:
        readings = []
    device_readings: dict[int, list[models.MeterReading]] = {}
    for r in readings:
        device_readings.setdefault(r.meter_id, []).append(r)
    meters = db.scalars(select(models.Meter).where(
        models.Meter.site_id == site.id)).all()
    meter_by_device = {m.device_id: m for m in meters if m.device_id}
    from .tariff_svc import tariff_boundaries
    boundaries = tariff_boundaries(
        db, site.id, site.timezone, job.cycle_start, job.cycle_end,
        _frozen_tariff_ids(db, job))
    return job, site, device_readings, meter_by_device, boundaries


def _clip_pieces(pieces, t1, t2) -> list:
    out = []
    for p in pieces:
        full = duration_seconds(p.start, p.end)
        part = duration_seconds(max(p.start, t1), min(p.end, t2))
        if part <= 0 or p.energy is None:
            continue
        ratio = Decimal(str(part / full))
        energy = quantize(p.energy * ratio, Q4)
        out.append(SimpleNamespace(
            segment_name=p.segment_name, price=p.price, energy=energy,
            amount=money(energy * p.price, settings.money_scale),
            basis=p.basis, note=p.note))
    return out


def _aggregate(pieces) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for p in pieces:
        a = agg.setdefault(p.segment_name, {
            "energy": ZERO, "amount": ZERO, "price": p.price,
            "raw_amount": ZERO, "basis": p.basis, "notes": set()})
        a["energy"] = quantize(a["energy"] + p.energy, Q4)
        a["amount"] = quantize(a["amount"] + p.amount, settings.money_scale)
        a["raw_amount"] = quantize(a["raw_amount"] + p.energy * p.price,
                                   settings.money_scale)
        if p.note:
            a["notes"].add(p.note)
    return agg


def _process_order_change(db, site, batch, seg, bill, interval, c, base_rev_no):
    job, site, device_readings, meter_by_device, boundaries = _job_context(db, bill)
    before_rows = _latest_contributions(db, c.source_record.id, interval.id, "order")
    before_agg = _aggregate([SimpleNamespace(
        segment_name=r.segment_name, price=r.price_per_kwh,
        energy=r.energy_kwh, amount=r.amount, basis=None, note=None)
        for r in before_rows])

    if c.change_kind == CHANGE_REVOCATION:
        after_agg, after_pieces = {}, []
        new_order = None
    else:
        new_order = _transient_order(c.payload, site)
        pieces = split_order(db, site, new_order, boundaries,
                             device_readings, meter_by_device,
                             _frozen_tariff_ids(db, job))
        after_pieces = _clip_pieces(pieces, interval.start_ts, interval.end_ts)
        after_agg = _aggregate(after_pieces)

    segments = sorted(set(before_agg) | set(after_agg))
    for seg_name in segments:
        b = before_agg.get(seg_name, {"energy": ZERO, "amount": ZERO,
                                      "raw_amount": ZERO, "price": None, "notes": set()})
        a = after_agg.get(seg_name, {"energy": ZERO, "amount": ZERO,
                                     "raw_amount": ZERO, "price": None, "notes": set()})
        rounding = quantize(
            (a["amount"] - a["raw_amount"]) - (b["amount"] - b["raw_amount"]),
            settings.money_scale)
        item = models.BackfillAdjustmentItem(
            batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
            source_job_id=job.id, source_record_id=c.source_record.id,
            from_revision_id=_adopted_revision_id(db, bill, c),
            to_revision_id=None if c.change_kind == CHANGE_REVOCATION else c.revision.id,
            change_kind=c.change_kind, reason_kind=_reason_kind(c),
            component=models.BF_COMP_CHARGING_AMOUNT, segment_name=seg_name,
            price_before=b["price"], price_after=a["price"],
            energy_before=b["energy"], energy_after=a["energy"],
            energy_delta=quantize(a["energy"] - b["energy"], Q4),
            amount_before=b["amount"], amount_after=a["amount"],
            amount_delta=quantize(a["amount"] - b["amount"], settings.money_scale),
            rounding_delta=rounding,
            basis_before=_basis_of(db, before_rows, seg_name),
            basis_after=a.get("basis"),
            detail_json=json.dumps({
                "order_no": getattr(new_order, "order_no", None)
                              or _adopted_order_no(db, c),
                "interval_local": [interval.start_ts_local.isoformat(),
                                   interval.end_ts_local.isoformat()],
                "before": {"energy": str(b["energy"]), "amount": str(b["amount"]),
                           "price": str(b["price"]) if b["price"] is not None else None},
                "after": {"energy": str(a["energy"]), "amount": str(a["amount"]),
                          "price": str(a["price"]) if a["price"] is not None else None},
                "notes": sorted(a.get("notes", set())),
                "rule": "撤原贡献+计修正值；累计读数不整段重加，仅按订单真实读数差值/上报值重算",
            }, ensure_ascii=False),
            reason_text=_default_reason(c, seg_name),
            status=models.BF_ADJ_PENDING, created_by=batch.created_by)
        db.add(item)
        db.flush()
        _withdraw_and_reissue_order(db, batch, item, before_rows, after_pieces,
                                    seg_name, c, bill, job, interval)
        _link_item(db, batch, item, bill)


def _withdraw_and_reissue_order(db, batch, item, before_rows, after_pieces,
                                seg_name, c, bill, job, interval):
    for r in before_rows:
        if r.segment_name != seg_name or r.status != CONTRIB_ACTIVE:
            continue
        r.status = CONTRIB_WITHDRAWN
        r.withdrawn_by_batch_id = batch.id
        r.withdrawn_by_item_id = item.id
    if c.change_kind == CHANGE_REVOCATION:
        return
    for p in [p for p in after_pieces if p.segment_name == seg_name]:
        db.add(models.BackfillContribution(
            site_id=bill.site_id, source_record_id=c.source_record.id,
            revision_id=c.revision.id, entity_kind="order",
            entity_id=0, job_id=job.id, bill_id=bill.id,
            reconciliation_interval_id=interval.id, order_split_id=None,
            segment_name=p.segment_name, price_per_kwh=p.price,
            energy_kwh=p.energy, amount=p.amount, status=CONTRIB_ACTIVE,
            supersedes_contribution_id=None,
            withdrawn_by_batch_id=None))


def _adopted_revision_id(db, bill, c: Candidate) -> int | None:
    rows = db.scalars(select(models.JobInput).where(
        models.JobInput.job_id == bill.job_id,
        models.JobInput.revision_id.is_not(None))).all()
    src_rev_ids = {s.revision_id for s in rows}
    if not src_rev_ids:
        return None
    revs = db.scalars(select(models.RecordRevision).where(
        models.RecordRevision.source_record_id == c.source_record.id,
        models.RecordRevision.id.in_(src_rev_ids))).all()
    return max((r.id for r in revs), default=None)


def _adopted_order_no(db, c: Candidate) -> str | None:
    order = db.scalar(select(models.ChargingOrder).where(
        models.ChargingOrder.source_record_id == c.source_record.id))
    return order.order_no if order else None


def _basis_of(db, rows, seg_name: str) -> str | None:
    """从原电价段留痕取回该时段的依据状态（measured/estimated）。"""
    for r in rows:
        if r.segment_name != seg_name or r.order_split_id is None:
            continue
        sp = db.get(models.OrderIntervalSplit, r.order_split_id)
        if sp is not None:
            return sp.basis_status
    return None


# ================= 读数类更正 =================

def _process_reading_change(db, site, batch, seg, bill, interval, c):
    job, site, device_readings, meter_by_device, boundaries = _job_context(db, bill)
    old_entity = _adopted_entity(db, c.source_record.id, bill.job_id, models.MeterReading)
    if old_entity is None:
        raise ValueError("读数更正找不到旧作业采用的读数来源")
    meter = db.get(models.Meter, old_entity.meter_id)
    readings = sorted(device_readings.get(meter.id, []), key=lambda r: r.reading_ts)

    def spans_with(replace_value=None, drop=False):
        seq = []
        for r in readings:
            if r.id == old_entity.id and drop:
                continue
            if r.id == old_entity.id and replace_value is not None:
                seq.append(SimpleNamespace(
                    id=r.id, reading_ts=r.reading_ts,
                    reading_kwh=replace_value, source_record_id=r.source_record_id,
                    revision_id=r.revision_id))
            else:
                seq.append(r)
        tasks: list[models.ReviewTask] = []
        return build_spans(db, site.id, site.timezone, meter, seq,
                           job.cycle_start, job.cycle_end, tasks), tasks

    new_value = None if c.change_kind == CHANGE_REVOCATION else Decimal(
        str(c.payload["reading_kwh"]))
    before_spans, _ = spans_with()
    after_spans, after_tasks = spans_with(
        replace_value=new_value, drop=c.change_kind == CHANGE_REVOCATION)

    before_energy = _span_energy_in(before_spans, interval)
    after_energy = _span_energy_in(after_spans, interval)
    if after_energy is None:
        reason = "修正后相邻计量区间出现回退/挂起且无换表依据，不产生负电量，分段挂起"
        _suspend_item(db, batch, seg, bill, c, reason)
        return reason

    component = (models.BF_COMP_MASTER if meter.meter_kind == "master"
                 else models.BF_COMP_CHARGING_KWH)
    energy_delta = quantize(after_energy - (before_energy or ZERO), Q4)
    item = models.BackfillAdjustmentItem(
        batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
        source_job_id=job.id, source_record_id=c.source_record.id,
        from_revision_id=old_entity.revision_id,
        to_revision_id=None if c.change_kind == CHANGE_REVOCATION else c.revision.id,
        change_kind=c.change_kind, reason_kind=_reason_kind(c),
        component=component,
        energy_before=before_energy or ZERO, energy_after=after_energy,
        energy_delta=energy_delta,
        detail_json=json.dumps({
            "meter_no": meter.meter_no,
            "reading_ts_local": old_entity.reading_ts_local.isoformat(),
            "reading_before": str(old_entity.reading_kwh),
            "reading_after": None if new_value is None else str(new_value),
            "span_energy_before": str(before_energy),
            "span_energy_after": str(after_energy),
            "rule": "仅重算以该累计读数为边界的相邻区间差值，不把累计读数整段再加一次",
        }, ensure_ascii=False),
        reason_text=_default_reason(c, meter.meter_no),
        status=models.BF_ADJ_PENDING, created_by=batch.created_by)
    db.add(item)
    db.flush()
    _link_item(db, batch, item, bill)

    # 恒等对平：总表变动 => 未解释差额同向变动；设备表变动 => 未解释差额反向变动
    if energy_delta != ZERO:
        unexplained_delta = energy_delta if meter.meter_kind == "master" else -energy_delta
        unexp_item = models.BackfillAdjustmentItem(
            batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
            source_job_id=job.id, source_record_id=c.source_record.id,
            from_revision_id=old_entity.revision_id,
            to_revision_id=c.revision.id,
            change_kind=c.change_kind, reason_kind=_reason_kind(c),
            component=models.BF_COMP_UNEXPLAINED_KWH,
            energy_before=ZERO, energy_after=ZERO,
            energy_delta=unexplained_delta,
            detail_json=json.dumps({
                "identity": "未解释差额 = 总表电量 - 设备表电量 - 公共负荷",
                "driven_by_component": component,
            }, ensure_ascii=False),
            reason_text=f"由 {component} 更正联动的未解释差额修正",
            status=models.BF_ADJ_PENDING, created_by=batch.created_by)
        db.add(unexp_item)
        db.flush()
        _link_item(db, batch, unexp_item, bill)

    # 设备读数同时是订单实测拆分锚点时，重算受影响订单的电价段金额
    if meter.meter_kind == "device":
        _reanchor_orders_for_reading(db, site, batch, seg, bill, interval, c,
                                     old_entity, new_value, device_readings,
                                     meter_by_device, boundaries, job)
    # 读数类不写贡献台账：调整项的分段幂等（同段同源一次）即防双撤，
    # 且避免把累计读数按 span 整段重复登记。
    db.flush()


def _span_energy_in(spans, interval):
    total = ZERO
    found = False
    for span, _prev, _cur in spans:
        if span.end <= interval.start_ts or span.start >= interval.end_ts:
            continue
        if span.energy is None:
            return None
        clip = clip_span_energy(span, interval.start_ts, interval.end_ts)
        if clip:
            total = quantize(total + clip, Q4)
            found = True
    return total if found else ZERO


def _reanchor_orders_for_reading(db, site, batch, seg, bill, interval, c,
                                 old_reading, new_value, device_readings,
                                 meter_by_device, boundaries, job):
    device_id = db.get(models.Meter, old_reading.meter_id).device_id
    if not device_id:
        return
    orders = db.scalars(select(models.ChargingOrder).where(
        models.ChargingOrder.site_id == site.id,
        models.ChargingOrder.device_id == device_id,
        models.ChargingOrder.adopted_by_job_id == job.id,
    ).order_by(models.ChargingOrder.start_ts)).all()
    adjusted_readings = {mid: [SimpleNamespace(
        id=r.id, reading_ts=r.reading_ts, reading_kwh=(
            new_value if r.id == old_reading.id else r.reading_kwh),
        source_record_id=r.source_record_id, revision_id=r.revision_id)
        for r in rs] for mid, rs in device_readings.items()}
    for order in orders:
        if not (order.start_ts < interval.end_ts and order.end_ts > interval.start_ts):
            continue
        if not (order.start_ts < old_reading.reading_ts < order.end_ts):
            continue
        before_pieces = [SimpleNamespace(
            segment_name=r.segment_name, price=r.price_per_kwh,
            energy=r.energy_kwh, amount=r.amount, basis=None, note=None)
            for r in _latest_contributions(db, order.source_record_id,
                                           interval.id, "order")]
        after_pieces = _clip_pieces(split_order(
            db, site, order, boundaries, adjusted_readings, meter_by_device,
            _frozen_tariff_ids(db, job)), interval.start_ts, interval.end_ts)
        b_agg, a_agg = _aggregate(before_pieces), _aggregate(after_pieces)
        for seg_name in sorted(set(b_agg) | set(a_agg)):
            b = b_agg.get(seg_name, {"energy": ZERO, "amount": ZERO, "raw_amount": ZERO})
            a = a_agg.get(seg_name, {"energy": ZERO, "amount": ZERO, "raw_amount": ZERO})
            if a["amount"] == b["amount"] and a["energy"] == b["energy"]:
                continue
            re_item = models.BackfillAdjustmentItem(
                batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
                source_job_id=job.id, source_record_id=c.source_record.id,
                from_revision_id=old_reading.revision_id, to_revision_id=c.revision.id,
                change_kind=c.change_kind, reason_kind=_reason_kind(c),
                component=models.BF_COMP_CHARGING_AMOUNT, segment_name=seg_name,
                price_before=b.get("price"), price_after=a.get("price"),
                energy_before=b["energy"], energy_after=a["energy"],
                energy_delta=quantize(a["energy"] - b["energy"], Q4),
                amount_before=b["amount"], amount_after=a["amount"],
                amount_delta=quantize(a["amount"] - b["amount"], settings.money_scale),
                rounding_delta=quantize(
                    (a["amount"] - a["raw_amount"]) - (b["amount"] - b["raw_amount"]),
                    settings.money_scale),
                detail_json=json.dumps({
                    "order_no": order.order_no,
                    "anchor_reading_local": old_reading.reading_ts_local.isoformat(),
                    "rule": "设备读数锚点纠正后重算实测拆分，订单总电量守恒，仅分时段间迁移",
                }, ensure_ascii=False),
                reason_text=f"读数锚点 {old_reading.reading_ts_local} 纠正联动订单 {order.order_no} 重拆分",
                status=models.BF_ADJ_PENDING, created_by=batch.created_by)
            db.add(re_item)
            db.flush()
            _link_item(db, batch, re_item, bill)


# ================= 公共负荷更正 =================

def _adopted_entity(db, sr_id: int, job_id: int, model):
    return db.scalar(select(model).where(
        model.source_record_id == sr_id,
        model.adopted_by_job_id == job_id))


def _process_public_change(db, site, batch, seg, bill, interval, c):
    job = db.get(models.ReconciliationJob, bill.job_id)
    old = _adopted_entity(db, c.source_record.id, job.id, models.PublicLoad)
    before_rows = [r for r in _latest_contributions(
        db, c.source_record.id, interval.id, "public_load")]
    before_energy = quantize(sum((r.energy_kwh for r in before_rows), ZERO), Q4)
    before_amount = quantize(sum((r.amount for r in before_rows), ZERO), settings.money_scale)
    price = before_rows[0].price_per_kwh if before_rows else None

    if c.change_kind == CHANGE_REVOCATION:
        after_energy, after_amount = ZERO, ZERO
    else:
        payload = c.payload
        s = parse_local_datetime(payload["start_time"], site.timezone)
        e = parse_local_datetime(payload["end_time"], site.timezone)
        total = Decimal(str(payload["energy_kwh"]))
        full = duration_seconds(s, e)
        part = duration_seconds(max(s, interval.start_ts), min(e, interval.end_ts))
        after_energy = quantize(total * Decimal(str(part / full)), Q4) if part > 0 else ZERO
        if price is None:
            mid = s + (e - s) / 2
            _, price = _frozen_price(db, site, job, mid)
        after_amount = money(after_energy * price, settings.money_scale)

    item = models.BackfillAdjustmentItem(
        batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
        source_job_id=job.id, source_record_id=c.source_record.id,
        from_revision_id=old.revision_id if old else None,
        to_revision_id=None if c.change_kind == CHANGE_REVOCATION else c.revision.id,
        change_kind=c.change_kind, reason_kind=_reason_kind(c),
        component=models.BF_COMP_PUBLIC_KWH,
        energy_before=before_energy, energy_after=after_energy,
        energy_delta=quantize(after_energy - before_energy, Q4),
        price_before=price, price_after=price,
        amount_before=before_amount, amount_after=after_amount,
        amount_delta=quantize(after_amount - before_amount, settings.money_scale),
        detail_json=json.dumps({
            "load_name": payload.get("load_name") if c.change_kind != CHANGE_REVOCATION
                         else (old.load_name if old else None),
            "before": {"energy": str(before_energy), "amount": str(before_amount)},
            "after": {"energy": str(after_energy), "amount": str(after_amount)},
        }, ensure_ascii=False),
        reason_text=_default_reason(c, "公共负荷"),
        status=models.BF_ADJ_PENDING, created_by=batch.created_by)
    db.add(item)
    db.flush()
    _link_item(db, batch, item, bill)
    for r in before_rows:
        if r.status == CONTRIB_ACTIVE:
            r.status = CONTRIB_WITHDRAWN
            r.withdrawn_by_batch_id = batch.id
            r.withdrawn_by_item_id = item.id
    if c.change_kind != CHANGE_REVOCATION and after_energy > ZERO:
        db.add(models.BackfillContribution(
            site_id=bill.site_id, source_record_id=c.source_record.id,
            revision_id=c.revision.id, entity_kind="public_load",
            entity_id=0, job_id=job.id, bill_id=bill.id,
            reconciliation_interval_id=interval.id, price_per_kwh=price,
            energy_kwh=after_energy, amount=after_amount, status=CONTRIB_ACTIVE))
    db.flush()


# ================= 电价更正 =================

def _tariff_override_resolver(db, site, job, c: Candidate):
    """用修正电价 payload 替换同源冻结电价版本，其余仍用冻结版本。"""
    frozen = db.scalars(select(models.Tariff).where(
        models.Tariff.id.in_(_frozen_tariff_ids(db, job)))).all()
    old_tariff = db.scalar(select(models.Tariff).where(
        models.Tariff.source_record_id == c.source_record.id,
        models.Tariff.adopted_by_job_id == job.id))
    versions = []
    for t in frozen:
        if old_tariff is not None and t.id == old_tariff.id:
            continue
        versions.append((t.effective_date, t.end_date, t))
    segs = c.payload["segments"]
    eff = datetime.strptime(segs[0]["effective_date"], "%Y-%m-%d").date()
    end = (datetime.strptime(segs[0]["end_date"], "%Y-%m-%d").date()
           if segs[0].get("end_date") else None)
    transient = SimpleNamespace(effective_date=eff, end_date=end, segments=[
        SimpleNamespace(segment_name=r["segment_name"], start_hour=int(r["start_hour"]),
                        end_hour=int(r["end_hour"]),
                        price_per_kwh=Decimal(str(r["price"]))) for r in segs])
    versions.append((eff, end, transient))

    def resolve(ts_utc: datetime):
        local_dt = utc_to_local(ts_utc, site.timezone)
        day = local_dt.date()
        candidates_v = [v for v in versions
                        if v[0] <= day and (v[1] is None or day < v[1])]
        if not candidates_v:
            raise ValueError(f"修正后电价在 {day} 无生效版本")
        tariff = max(candidates_v, key=lambda v: v[0])[2]
        seg = segment_for_hour(tariff, local_dt.hour)
        return seg.segment_name, seg.price_per_kwh
    return resolve, old_tariff


def _process_tariff_change(db, site, batch, seg, bill, interval, c):
    job = db.get(models.ReconciliationJob, bill.job_id)
    if c.change_kind == CHANGE_REVOCATION:
        raise ValueError("电价版本不允许撤销，只能以新版本更正")
    resolver, old_tariff = _tariff_override_resolver(db, site, job, c)
    rows = [r for r in db.scalars(select(models.BackfillContribution).where(
        models.BackfillContribution.bill_id == bill.id,
        models.BackfillContribution.reconciliation_interval_id == interval.id,
        models.BackfillContribution.status == CONTRIB_ACTIVE,
        models.BackfillContribution.entity_kind.in_(["order", "public_load"]))).all()]
    for r in rows:
        mid = interval.start_ts + (interval.end_ts - interval.start_ts) / 2
        if r.order_split_id is not None:
            sp = db.get(models.OrderIntervalSplit, r.order_split_id)
            mid = sp.start_ts + (sp.end_ts - sp.start_ts) / 2
        new_seg, new_price = resolver(mid)
        if new_price == r.price_per_kwh:
            continue
        raw_after = money(r.energy_kwh * new_price, settings.money_scale)
        amount_delta = quantize(raw_after - r.amount, settings.money_scale)
        rounding = quantize(raw_after - money(r.energy_kwh * new_price,
                                               settings.money_scale), settings.money_scale)
        item = models.BackfillAdjustmentItem(
            batch_id=batch.id, segment_id=seg.id, site_id=site.id, bill_id=bill.id,
            source_job_id=job.id, source_record_id=c.source_record.id,
            from_revision_id=old_tariff.revision_id if old_tariff else None,
            to_revision_id=c.revision.id,
            change_kind=c.change_kind, reason_kind=models.BF_REASON_TARIFF_CORRECTION,
            component=models.BF_COMP_TARIFF, segment_name=new_seg,
            price_before=r.price_per_kwh, price_after=new_price,
            energy_before=r.energy_kwh, energy_after=r.energy_kwh,
            energy_delta=ZERO,
            amount_before=r.amount, amount_after=raw_after,
            amount_delta=amount_delta, rounding_delta=rounding,
            detail_json=json.dumps({
                "contribution_id": r.id, "entity_kind": r.entity_kind,
                "energy_unchanged": True,
                "rule": "电量不变，仅按修正后电价重计价，舍入尾差单独落 rounding_delta",
            }, ensure_ascii=False),
            reason_text=_default_reason(c, new_seg),
            status=models.BF_ADJ_PENDING, created_by=batch.created_by)
        db.add(item)
        db.flush()
        _link_item(db, batch, item, bill)
        r.status = CONTRIB_WITHDRAWN
        r.withdrawn_by_batch_id = batch.id
        r.withdrawn_by_item_id = item.id
        db.add(models.BackfillContribution(
            site_id=bill.site_id, source_record_id=c.source_record.id,
            revision_id=c.revision.id, entity_kind=r.entity_kind,
            entity_id=r.entity_id, job_id=job.id, bill_id=bill.id,
            reconciliation_interval_id=interval.id, order_split_id=r.order_split_id,
            segment_name=new_seg, price_per_kwh=new_price,
            energy_kwh=r.energy_kwh, amount=raw_after, status=CONTRIB_ACTIVE,
            supersedes_contribution_id=r.id))
    db.flush()


# ================= 调整项链接、复核、结算关联 =================

def _link_item(db, batch, item: models.BackfillAdjustmentItem,
               bill: models.Bill) -> None:
    db.add(models.BackfillBillLink(
        site_id=bill.site_id, batch_id=batch.id, item_id=item.id,
        original_bill_id=bill.id, linked_amount=item.amount_delta,
        status="pending"))


def _default_reason(c: Candidate, where: str) -> str:
    kind = {
        CHANGE_LATE: "迟到数据补计入账",
        CHANGE_CORRECTION: "依据纠正后撤原贡献并计修正值",
        CHANGE_REVOCATION: "重复/误报记录撤销，撤出原贡献",
    }[c.change_kind]
    return f"{c.source_record.source_type}:{c.source_record.source_record_no}（{kind}，位置：{where}）"


def review_item(db: Session, item: models.BackfillAdjustmentItem, user: models.User,
                action: str, comment: str | None) -> models.BackfillAdjustmentItem:
    """双方复核分别保存；物业否认只退回本调整，不影响同批次其他调整项。"""
    if item.status == models.BF_ADJ_SUSPENDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "挂起调整项尚不能复核")
    party = models.BF_REVIEW_PROPERTY if user.role == models.ROLE_PROPERTY \
        else models.BF_REVIEW_FINANCE
    if party == models.BF_REVIEW_PROPERTY:
        if action == "reject":
            item.status = models.BF_ADJ_REJECTED
            item.rejected_at = datetime.utcnow()
            item.rejected_by = user.username
            item.reject_note = comment
        elif action == "acknowledge":
            item.status = models.BF_ADJ_PROPERTY
            item.property_reviewed_at = datetime.utcnow()
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "物业动作只能是 acknowledge/reject")
    else:
        if action != "confirm":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "财务动作只能是 confirm")
        if item.status != models.BF_ADJ_PROPERTY:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "财务确认前必须先由物业复核通过")
        item.status = models.BF_ADJ_CONFIRMED
        item.confirmed_at = datetime.utcnow()
        item.confirmed_by = user.username
    db.add(models.BackfillReview(
        item_id=item.id, review_party=party, action=action,
        comment=comment, reviewer=user.username))
    db.flush()
    return item


def link_to_settlement_bill(db: Session, item: models.BackfillAdjustmentItem,
                            settlement_bill: models.Bill,
                            user: models.User) -> models.BackfillBillLink:
    if item.status != models.BF_ADJ_CONFIRMED:
        raise HTTPException(status.HTTP_409_CONFLICT, "只有财务已确认的调整项可关联后续账单")
    link = db.scalar(select(models.BackfillBillLink).where(
        models.BackfillBillLink.item_id == item.id))
    if link is None:
        link = models.BackfillBillLink(
            site_id=item.site_id, batch_id=item.batch_id, item_id=item.id,
            original_bill_id=item.bill_id, linked_amount=item.amount_delta)
        db.add(link)
    link.settlement_bill_id = settlement_bill.id
    link.status = "linked"
    link.note = f"由 {user.username} 关联到后续结算账单 {settlement_bill.bill_no}"
    db.flush()
    return link


def auto_link_on_settlement(db: Session, settlement_bill: models.Bill) -> int:
    """新账单确认时，把同站点已确认未关联的回补项自动单列带入后续结算。"""
    items = db.scalars(select(models.BackfillAdjustmentItem).where(
        models.BackfillAdjustmentItem.site_id == settlement_bill.site_id,
        models.BackfillAdjustmentItem.status == models.BF_ADJ_CONFIRMED,
    )).all()
    count = 0
    for item in items:
        link = db.scalar(select(models.BackfillBillLink).where(
            models.BackfillBillLink.item_id == item.id))
        if link is None:
            db.add(models.BackfillBillLink(
                site_id=item.site_id, batch_id=item.batch_id, item_id=item.id,
                original_bill_id=item.bill_id,
                settlement_bill_id=settlement_bill.id,
                linked_amount=item.amount_delta, status="linked",
                note=f"账单 {settlement_bill.bill_no} 确认时自动带入"))
            count += 1
        elif link.settlement_bill_id is None:
            link.settlement_bill_id = settlement_bill.id
            link.status = "linked"
            link.note = f"账单 {settlement_bill.bill_no} 确认时自动带入"
            count += 1
    db.flush()
    return count


def bill_payable_view(db: Session, bill: models.Bill) -> dict:
    """当前应付必须同时引用旧账与调整项，不能只凭月份查一个看似完整的总数。"""
    links = db.scalars(select(models.BackfillBillLink).where(
        models.BackfillBillLink.original_bill_id == bill.id)).all()
    items = [db.get(models.BackfillAdjustmentItem, l.item_id) for l in links]
    confirmed = [i for i in items if i and i.status == models.BF_ADJ_CONFIRMED]
    rejected = [i for i in items if i and i.status == models.BF_ADJ_REJECTED]
    pending = [i for i in items if i and i.status in (
        models.BF_ADJ_PENDING, models.BF_ADJ_PROPERTY, models.BF_ADJ_SUSPENDED)]
    backfill_delta = quantize(sum((i.amount_delta for i in confirmed), ZERO),
                              settings.money_scale)
    manual_delta = quantize(sum((a.delta for a in db.scalars(
        select(models.BillAdjustment).where(
            models.BillAdjustment.bill_id == bill.id,
            models.BillAdjustment.component.in_(
                ["charging_amount", "public_amount"]))).all()), ZERO),
        settings.money_scale)
    return {
        "bill_id": bill.id,
        "bill_no": bill.bill_no,
        "original_charging_amount": bill.charging_amount,
        "original_public_amount": bill.public_amount,
        "manual_adjustment_delta": manual_delta,
        "backfill_confirmed_delta": backfill_delta,
        "current_payable_amount": quantize(
            bill.charging_amount + bill.public_amount
            + manual_delta + backfill_delta, settings.money_scale),
        "backfill_item_total": len(items),
        "backfill_confirmed_count": len(confirmed),
        "backfill_pending_count": len(pending),
        "backfill_rejected_count": len(rejected),
        "note": "旧账金额冻结不覆盖；应付=原账单+人工差额更正+已确认回补调整项",
    }
