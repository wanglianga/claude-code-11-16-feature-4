"""核算引擎。

核心原则：
- 全部时间先转站点时区，再按电价生效边界/分时边界拆成同价区间，读数差值与订单用电在相同范围比较。
- 读数回退先查设备更换记录；无依据则整段挂起，不生成负电量。
- 订单电量只能来自真实读数差值或设备上报值（标注估算）；缺起止读数且无上报值列为待补，
  绝不按持续时间编造实际电量。持续时间仅用于把“已知总电量”拆分到跨时段各段，拆分结果标注估算。
- 作业开始即冻结输入（JobInput 快照 + adopted 标记），执行期间到达的更正自动 blocked，留待下一轮。
- 保留充电用电、经确认的公共负荷与未解释差额三个成分，不做按订单比例摊平；
  未解释差额超阈值则账单挂起，不自动归零。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from .importer import promote_blocked_revisions
from .tariff_svc import price_at_utc, tariff_boundaries
from .timeutil import (
    duration_seconds,
    local_to_utc,
    merge_boundaries,
    money,
    quantize,
    utc_to_local,
)

settings = get_settings()
ZERO = Decimal("0")
Q4 = settings.energy_scale


@dataclass
class Span:
    start: datetime
    end: datetime
    energy: Decimal | None          # None=挂起，不可量化
    status: str
    note: str | None = None


# ---------------- 读数序列 -> 计量段（含回退/换表处理） ----------------

def build_spans(
    db: Session,
    site_id: int,
    tz_name: str,
    meter: models.Meter,
    readings: list[models.MeterReading],
    cycle_start: datetime,
    cycle_end: datetime,
    task_list: list[models.ReviewTask],
) -> list[tuple[Span, models.MeterReading, models.MeterReading]]:
    readings = sorted(readings, key=lambda r: r.reading_ts)
    replacements = sorted(
        db.scalars(
            select(models.DeviceReplacement).where(
                models.DeviceReplacement.site_id == site_id,
                models.DeviceReplacement.meter_id == meter.id,
            )
        ).all(),
        key=lambda r: r.effective_ts,
    )
    out: list[tuple[Span, models.MeterReading, models.MeterReading]] = []
    for prev, cur in zip(readings, readings[1:]):
        if not (cur.reading_ts > cycle_start and prev.reading_ts < cycle_end):
            continue
        t1, t2 = max(prev.reading_ts, cycle_start), min(cur.reading_ts, cycle_end)
        delta = cur.reading_kwh - prev.reading_kwh
        if delta >= 0:
            # 裁剪到周期边界时按时间比例折算（完整段通常不触发）
            full = duration_seconds(prev.reading_ts, cur.reading_ts)
            part = duration_seconds(t1, t2)
            energy = delta if full == part else quantize(delta * Decimal(str(part / full)), Q4)
            out.append((Span(t1, t2, energy, models.BASIS_MEASURED), prev, cur))
            continue
        # 读数回退：先查设备更换记录
        rep = next(
            (
                r
                for r in replacements
                if prev.reading_ts <= r.effective_ts <= cur.reading_ts
            ),
            None,
        )
        if rep is None:
            task_list.append(
                models.ReviewTask(
                    site_id=site_id,
                    task_kind="meter_rollback",
                    title=f"电表 {meter.meter_no} 读数回退且无更换依据",
                    detail=(
                        f"{utc_to_local(prev.reading_ts, tz_name)} 读数 {prev.reading_kwh} -> "
                        f"{utc_to_local(cur.reading_ts, tz_name)} 读数 {cur.reading_kwh} 出现回退，"
                        f"未找到设备更换记录，整段挂起，不生成负电量。"
                    ),
                    assignee=models.ROLE_OPERATOR,
                )
            )
            out.append((Span(t1, t2, None, models.BASIS_SUSPENDED,
                             "读数回退且无设备更换依据，整段挂起"), prev, cur))
            continue
        # 换表桥接：旧表尾读数 -> 新表起读数
        if rep.old_final_reading is None or rep.new_initial_reading is None:
            task_list.append(
                models.ReviewTask(
                    site_id=site_id,
                    task_kind="meter_rollback",
                    title=f"电表 {meter.meter_no} 换表记录缺少首尾读数",
                    detail=(
                        f"换表时间 {utc_to_local(rep.effective_ts, tz_name)} 的记录缺少旧表尾读数或新表起读数，"
                        f"无法桥接，整段挂起。依据备注：{rep.evidence_note}"
                    ),
                    assignee=models.ROLE_OPERATOR,
                )
            )
            out.append((Span(t1, t2, None, models.BASIS_SUSPENDED, "换表记录缺少首尾读数"), prev, cur))
            continue
        part_a = rep.old_final_reading - prev.reading_kwh
        part_b = cur.reading_kwh - rep.new_initial_reading
        if part_a < 0 or part_b < 0:
            out.append((Span(t1, t2, None, models.BASIS_SUSPENDED,
                             "换表桥接出现负差值，挂起待人工核对"), prev, cur))
            continue
        out.append((
            Span(
                t1,
                t2,
                quantize(part_a + part_b, Q4),
                models.BASIS_MEASURED,
                f"换表桥接（依据：{rep.evidence_note}）",
            ),
            prev,
            cur,
        ))
    return out


def clip_span_energy(span: Span, t1: datetime, t2: datetime) -> Decimal | None:
    lo, hi = max(span.start, t1), min(span.end, t2)
    if lo >= hi or span.energy is None:
        return None if span.energy is None else ZERO
    full = duration_seconds(span.start, span.end)
    part = duration_seconds(lo, hi)
    if part == full:
        return span.energy
    return quantize(span.energy * Decimal(str(part / full)), Q4)


# ---------------- 订单拆分与电量认定 ----------------

@dataclass
class OrderPiece:
    start: datetime
    end: datetime
    energy: Decimal | None
    amount: Decimal | None
    segment_name: str
    price: Decimal
    basis: str
    note: str | None


def determine_order_energy(order: models.ChargingOrder) -> tuple[Decimal | None, str, str | None]:
    """返回 (总电量, 依据状态, 说明)。无任何真实来源时返回 None=待补。"""
    if order.start_reading_kwh is not None and order.end_reading_kwh is not None:
        delta = order.end_reading_kwh - order.start_reading_kwh
        if delta < 0:
            return None, models.BASIS_PENDING, "起止读数差值为负，待人工核对"
        return quantize(delta, Q4), models.BASIS_MEASURED, None
    if order.reported_energy_kwh and order.reported_energy_kwh > 0:
        return (
            quantize(order.reported_energy_kwh, Q4),
            models.BASIS_ESTIMATED,
            "缺少起止读数，采用设备上报电量，标注估算",
        )
    return None, models.BASIS_PENDING, "缺少起止读数且无上报电量，列为待补，不按持续时间编造"


def split_order(
    db: Session,
    site: models.Site,
    order: models.ChargingOrder,
    boundaries: list[datetime],
    device_readings: dict[int, list[models.MeterReading]],
    meter_by_device: dict[str, models.Meter],
    tariff_ids: set[int] | None = None,
    price_resolver=None,
) -> list[OrderPiece]:
    """price_resolver(ts_utc) -> (segment_name, price)：回补可用修正后电价计价；
    缺省沿用冻结电价版本查询。"""
    def _price(ts):
        if price_resolver is not None:
            return price_resolver(ts)
        return price_at_utc(db, site.id, site.timezone, ts, tariff_ids)

    pieces_time = merge_boundaries(order.start_ts, order.end_ts, boundaries)
    total, basis, note = determine_order_energy(order)
    result: list[OrderPiece] = []
    if total is None:
        for t1, t2 in pieces_time:
            seg_name, price = _price(t1 + (t2 - t1) / 2)
            result.append(OrderPiece(t1, t2, None, None, seg_name, price,
                                     models.BASIS_PENDING, note))
        return result

    # 是否具备各分时边界的中间计量值
    meter = meter_by_device.get(order.device_id)
    internal = [b for b in boundaries if order.start_ts < b < order.end_ts]
    measured_split = False
    if meter and internal and order.start_reading_kwh is not None and order.end_reading_kwh is not None:
        rmap = {r.reading_ts: r.reading_kwh for r in device_readings.get(meter.id, [])}
        measured_split = all(b in rmap for b in internal)

    energies: list[Decimal]
    piece_basis = basis
    split_note = note
    if measured_split:
        rmap = {r.reading_ts: r.reading_kwh for r in device_readings[meter.id]}
        anchors = [(order.start_ts, order.start_reading_kwh)]
        anchors += [(b, rmap[b]) for b in internal]
        anchors.append((order.end_ts, order.end_reading_kwh))
        energies = [
            quantize(anchors[i + 1][1] - anchors[i][1], Q4)
            for i in range(len(anchors) - 1)
        ]
        piece_basis = models.BASIS_MEASURED
    else:
        if internal:
            piece_basis = models.BASIS_ESTIMATED
            split_note = "跨电价时段且缺少中间计量值，按持续时间拆分已知总电量，各段标注估算"
        weights = [Decimal(str(duration_seconds(t1, t2))) for t1, t2 in pieces_time]
        wsum = sum(weights, ZERO)
        energies = [quantize(total * w / wsum, Q4) for w in weights]
        # 尾差并入最后一段，保证拆分合计等于已知总电量
        energies[-1] = quantize(total - sum(energies[:-1], ZERO), Q4)

    for (t1, t2), energy in zip(pieces_time, energies):
        seg_name, price = _price(t1 + (t2 - t1) / 2)
        amount = money(energy * price, settings.money_scale)
        result.append(
            OrderPiece(t1, t2, energy, amount, seg_name, price, piece_basis, split_note)
        )
    return result


# ---------------- 主流程 ----------------

def run_reconciliation(
    db: Session,
    site: models.Site,
    cycle_start_local: datetime,
    cycle_end_local: datetime,
    username: str,
    rerun_of_job_id: int | None = None,
) -> models.ReconciliationJob:
    # 新一轮：先提升上轮被阻塞的修订（重跑沿用旧输入，不提升）
    if rerun_of_job_id is None:
        promote_blocked_revisions(db, site)

    start_utc = local_to_utc(cycle_start_local, site.timezone)
    end_utc = local_to_utc(cycle_end_local, site.timezone)

    source_job = (
        db.get(models.ReconciliationJob, rerun_of_job_id)
        if rerun_of_job_id
        else None
    )
    job = models.ReconciliationJob(
        site_id=site.id,
        cycle_start=start_utc,
        cycle_end=end_utc,
        cycle_start_local=cycle_start_local,
        cycle_end_local=cycle_end_local,
        status=models.JOB_RUNNING,
        threshold_pct=site.diff_threshold_pct,
        rerun_of_job_id=rerun_of_job_id,
    )
    db.add(job)
    db.flush()

    tasks: list[models.ReviewTask] = []

    # ---- 1. 冻结输入：重跑沿用源作业快照；首轮选取当前生效实体并打 adopted 标记 ----
    if source_job is not None:
        snapshots = db.scalars(
            select(models.JobInput).where(models.JobInput.job_id == source_job.id)
        ).all()
        for snap in snapshots:
            db.add(models.JobInput(
                job_id=job.id, input_kind=snap.input_kind,
                entity_id=snap.entity_id, revision_id=snap.revision_id,
            ))
        adopted_readings = db.scalars(
            select(models.MeterReading).where(
                models.MeterReading.adopted_by_job_id == source_job.id
            )
        ).all()
        adopted_orders = db.scalars(
            select(models.ChargingOrder).where(
                models.ChargingOrder.adopted_by_job_id == source_job.id
            )
        ).all()
        adopted_tariffs = db.scalars(
            select(models.Tariff).where(models.Tariff.adopted_by_job_id == source_job.id)
        ).all()
        adopted_public = db.scalars(
            select(models.PublicLoad).where(models.PublicLoad.adopted_by_job_id == source_job.id)
        ).all()
    else:
        adopted_readings = db.scalars(
            select(models.MeterReading).where(
                models.MeterReading.site_id == site.id,
                models.MeterReading.status == models.ENTITY_ACTIVE,
                models.MeterReading.reading_ts >= start_utc,
                models.MeterReading.reading_ts <= end_utc,
            )
        ).all()
        adopted_orders = db.scalars(
            select(models.ChargingOrder).where(
                models.ChargingOrder.site_id == site.id,
                models.ChargingOrder.status == models.ENTITY_ACTIVE,
                models.ChargingOrder.start_ts < end_utc,
                models.ChargingOrder.end_ts > start_utc,
            )
        ).all()
        adopted_tariffs = db.scalars(
            select(models.Tariff).where(
                models.Tariff.site_id == site.id,
                models.Tariff.status == models.ENTITY_ACTIVE,
            )
        ).all()
        adopted_public = db.scalars(
            select(models.PublicLoad).where(
                models.PublicLoad.site_id == site.id,
                models.PublicLoad.status == models.ENTITY_ACTIVE,
                models.PublicLoad.start_ts < end_utc,
                models.PublicLoad.end_ts > start_utc,
            )
        ).all()
        for r in adopted_readings:
            r.adopted_by_job_id = job.id
            db.add(models.JobInput(job_id=job.id, input_kind="reading",
                                   entity_id=r.id, revision_id=r.revision_id))
        for o in adopted_orders:
            o.adopted_by_job_id = job.id
            db.add(models.JobInput(job_id=job.id, input_kind="order",
                                   entity_id=o.id, revision_id=o.revision_id))
        for t in adopted_tariffs:
            t.adopted_by_job_id = job.id
            db.add(models.JobInput(job_id=job.id, input_kind="tariff",
                                   entity_id=t.id, revision_id=t.revision_id))
        for p in adopted_public:
            p.adopted_by_job_id = job.id
            db.add(models.JobInput(job_id=job.id, input_kind="public_load",
                                   entity_id=p.id, revision_id=p.revision_id))
        for rep in db.scalars(
            select(models.DeviceReplacement).where(models.DeviceReplacement.site_id == site.id)
        ).all():
            db.add(models.JobInput(job_id=job.id, input_kind="replacement",
                                   entity_id=rep.id, revision_id=None))
    db.flush()

    # ---- 2. 总表计量区间 ----
    master_meters = db.scalars(
        select(models.Meter).where(
            models.Meter.site_id == site.id, models.Meter.meter_kind == "master"
        )
    ).all()
    device_meters = db.scalars(
        select(models.Meter).where(
            models.Meter.site_id == site.id, models.Meter.meter_kind == "device"
        )
    ).all()

    master_intervals: list[tuple[models.Meter, Span, models.MeterReading, models.MeterReading]] = []
    for mm in master_meters:
        mrs = sorted(
            [r for r in adopted_readings if r.meter_id == mm.id],
            key=lambda r: r.reading_ts,
        )
        built = build_spans(db, site.id, site.timezone, mm, mrs, start_utc, end_utc, tasks)
        for span, prev, cur in built:
            master_intervals.append((mm, span, prev, cur))
    master_intervals.sort(key=lambda x: x[1].start)

    # ---- 3. 设备表计量段 ----
    device_spans: dict[int, list[Span]] = {}
    device_readings: dict[int, list[models.MeterReading]] = {}
    for dm in device_meters:
        drs = sorted(
            [r for r in adopted_readings if r.meter_id == dm.id],
            key=lambda r: r.reading_ts,
        )
        device_readings[dm.id] = drs
        device_spans[dm.id] = [
            span for span, _, _ in build_spans(
                db, site.id, site.timezone, dm, drs, start_utc, end_utc, tasks
            )
        ]
    meter_by_device = {dm.device_id: dm for dm in device_meters if dm.device_id}

    # ---- 4. 电价边界 + 订单拆分（仅使用本轮冻结的电价版本） ----
    frozen_tariff_ids = {t.id for t in adopted_tariffs}
    boundaries = tariff_boundaries(
        db, site.id, site.timezone, start_utc, end_utc, frozen_tariff_ids
    )
    conflict_order_ids = {
        c.order_a_id for c in db.scalars(
            select(models.OrderConflict).where(
                models.OrderConflict.site_id == site.id,
                models.OrderConflict.status == models.CONFLICT_OPEN,
            )
        ).all()
    } | {
        c.order_b_id for c in db.scalars(
            select(models.OrderConflict).where(
                models.OrderConflict.site_id == site.id,
                models.OrderConflict.status == models.CONFLICT_OPEN,
            )
        ).all()
    }

    valid_orders = [o for o in adopted_orders if o.id not in conflict_order_ids]
    order_pieces: dict[int, list[OrderPiece]] = {}
    bill_charging_amount = ZERO
    sum_charging = ZERO
    sum_estimated = ZERO
    pending_orders: set[int] = set()
    for order in valid_orders:
        pieces = split_order(
            db, site, order, boundaries, device_readings, meter_by_device,
            frozen_tariff_ids,
        )
        order_pieces[order.id] = pieces
        for p in pieces:
            if p.basis == models.BASIS_PENDING:
                pending_orders.add(order.id)
            elif p.basis == models.BASIS_ESTIMATED:
                sum_estimated += p.energy or ZERO
                bill_charging_amount += p.amount or ZERO
            else:
                sum_charging += p.energy or ZERO
                bill_charging_amount += p.amount or ZERO
        # 上报电量与实测电量偏差核对事项
        total, basis, _ = determine_order_energy(order)
        if (
            basis == models.BASIS_MEASURED
            and order.reported_energy_kwh
            and total is not None
        ):
            diff = abs(total - order.reported_energy_kwh)
            if diff > max(Decimal("1"), total * Decimal("0.01")):
                tasks.append(models.ReviewTask(
                    site_id=site.id, job_id=job.id,
                    task_kind="order_meter_diff",
                    title=f"订单 {order.order_no} 读数电量与上报电量偏差",
                    detail=(
                        f"读数差值 {total} kWh，设备上报 {order.reported_energy_kwh} kWh，"
                        f"偏差 {quantize(diff, Q4)} kWh，以读数差值为准。"
                    ),
                    assignee=models.ROLE_OPERATOR,
                ))
        if basis == models.BASIS_ESTIMATED:
            tasks.append(models.ReviewTask(
                site_id=site.id, job_id=job.id,
                task_kind="estimated_item",
                title=f"订单 {order.order_no} 含估算电量",
                detail=(
                    f"订单 {order.order_no} 缺少起止读数或中间计量值，"
                    f"采用上报电量/按时长拆分，金额 {sum(p.amount or ZERO for p in pieces)} 元为估算项。"
                ),
                assignee=models.ROLE_FINANCE,
            ))
        if basis == models.BASIS_PENDING or any(p.basis == models.BASIS_PENDING for p in pieces):
            tasks.append(models.ReviewTask(
                site_id=site.id, job_id=job.id,
                task_kind="missing_reading",
                title=f"订单 {order.order_no} 缺少计量依据",
                detail=(
                    f"订单 {order.order_no} 缺少起止读数且无可用上报电量，列为待补，"
                    f"本周期不计电量不计金额，不得按持续时间编造。"
                ),
                assignee=models.ROLE_OPERATOR,
            ))

    # ---- 5. 逐总表区间汇总成分并落库 ----
    bill_master_total = ZERO
    bill_public = ZERO
    bill_unexplained = ZERO
    bill_suspended = ZERO
    bill_device_meter = ZERO
    has_suspended_interval = False
    unconfirmed_public_tasked: set[int] = set()
    intervals_out: list[models.ReconciliationInterval] = []

    for mm, span, prev_read, cur_read in master_intervals:
        t1, t2 = span.start, span.end
        rec = models.ReconciliationInterval(
            job_id=job.id, site_id=site.id,
            start_ts=t1, end_ts=t2,
            start_ts_local=utc_to_local(t1, site.timezone),
            end_ts_local=utc_to_local(t2, site.timezone),
            master_reading_start=prev_read.reading_kwh,
            master_reading_end=cur_read.reading_kwh,
        )
        if span.status == models.BASIS_SUSPENDED or span.energy is None:
            rec.master_energy_kwh = None
            rec.basis_status = models.BASIS_SUSPENDED
            rec.note = span.note or "总表区间挂起"
            has_suspended_interval = True
            db.add(rec)
            intervals_out.append(rec)
            continue

        master_energy = quantize(span.energy, Q4)
        rec.master_energy_kwh = master_energy
        bill_master_total += master_energy

        # 设备表电量（相同范围比较）
        device_energy = ZERO
        interval_suspended = False
        for dm in device_meters:
            for sp in device_spans.get(dm.id, []):
                if sp.end <= t1 or sp.start >= t2:
                    continue
                if sp.energy is None:
                    interval_suspended = True
                    continue
                clip = clip_span_energy(sp, t1, t2)
                if clip:
                    device_energy += clip
        device_energy = quantize(device_energy, Q4)
        rec.device_meter_kwh = device_energy
        bill_device_meter += device_energy

        # 订单电量（按持续时间裁剪到本区间，仅用于同范围比较）
        charging = ZERO
        estimated = ZERO
        pending = ZERO
        for order in valid_orders:
            full = duration_seconds(order.start_ts, order.end_ts)
            part = duration_seconds(max(order.start_ts, t1), min(order.end_ts, t2))
            if part <= 0:
                continue
            ratio = Decimal(str(part / full))
            for p in order_pieces.get(order.id, []):
                e = p.energy
                if e is None:
                    continue
                alloc = quantize(e * ratio, Q4)
                if p.basis == models.BASIS_ESTIMATED:
                    estimated += alloc
                else:
                    charging += alloc
        rec.charging_kwh = quantize(charging, Q4)
        rec.estimated_kwh = quantize(estimated, Q4)
        rec.pending_kwh = pending
        rec.order_meter_diff_kwh = quantize(
            device_energy - rec.charging_kwh - rec.estimated_kwh, Q4
        )

        # 经确认的公共负荷（未确认的生成核对事项，不计入；同一负荷每轮只生成一条）
        public_energy = ZERO
        for pl in adopted_public:
            if pl.end_ts <= t1 or pl.start_ts >= t2:
                continue
            if not pl.confirmed:
                if pl.id not in unconfirmed_public_tasked:
                    unconfirmed_public_tasked.add(pl.id)
                    tasks.append(models.ReviewTask(
                        site_id=site.id, job_id=job.id,
                        task_kind="public_confirmation",
                        title=f"公共负荷 {pl.load_name} 尚未经双方确认",
                        detail=(
                            f"时段 {utc_to_local(pl.start_ts, site.timezone)} ~ "
                            f"{utc_to_local(pl.end_ts, site.timezone)}，电量 {pl.energy_kwh} kWh，"
                            f"确认前不计入账单，不得直接摊给客户充电。"
                        ),
                        assignee=models.ROLE_PROPERTY,
                    ))
                continue
            full = duration_seconds(pl.start_ts, pl.end_ts)
            part = duration_seconds(max(pl.start_ts, t1), min(pl.end_ts, t2))
            public_energy += quantize(pl.energy_kwh * Decimal(str(part / full)), Q4)
        public_energy = quantize(public_energy, Q4)
        rec.public_kwh = public_energy
        bill_public += public_energy

        # 未解释差额：总表 - 设备表 - 公共负荷；挂起段不参与，不做按订单摊平
        unexplained = quantize(master_energy - device_energy - public_energy, Q4)
        rec.unexplained_kwh = unexplained
        bill_unexplained += unexplained

        if interval_suspended:
            rec.basis_status = models.BASIS_SUSPENDED
            rec.note = "区间内存在挂起的设备计量段，该部分电量未计入，待补依据"
            has_suspended_interval = True
        elif rec.estimated_kwh > 0:
            rec.basis_status = models.BASIS_ESTIMATED
        else:
            rec.basis_status = models.BASIS_MEASURED
        db.add(rec)
        intervals_out.append(rec)

    db.flush()

    # ---- 6. 订单拆分段落库（关联所在区间） ----
    for order in valid_orders:
        for p in order_pieces.get(order.id, []):
            interval_id = next(
                (iv.id for iv in intervals_out
                 if iv.start_ts <= p.start < iv.end_ts),
                None,
            )
            db.add(models.OrderIntervalSplit(
                job_id=job.id, order_id=order.id,
                reconciliation_interval_id=interval_id,
                start_ts=p.start, end_ts=p.end,
                start_ts_local=utc_to_local(p.start, site.timezone),
                end_ts_local=utc_to_local(p.end, site.timezone),
                segment_name=p.segment_name, price_per_kwh=p.price,
                energy_kwh=p.energy, amount=p.amount,
                basis_status=p.basis, note=p.note,
            ))

    # ---- 7. 阈值判定与账单 ----
    bill_charging_amount = quantize(bill_charging_amount, settings.money_scale)
    bill_public_amount = ZERO
    for pl in adopted_public:
        if not pl.confirmed:
            continue
        mid = pl.start_ts + (pl.end_ts - pl.start_ts) / 2
        _, price = price_at_utc(db, site.id, site.timezone, mid, frozen_tariff_ids)
        bill_public_amount += money(pl.energy_kwh * price, settings.money_scale)
    bill_public_amount = quantize(bill_public_amount, settings.money_scale)

    unexplained_pct = (
        abs(bill_unexplained) / bill_master_total
        if bill_master_total > 0
        else ZERO
    )
    over_threshold = quantize(unexplained_pct, 4) > quantize(site.diff_threshold_pct, 4)

    bill_no = f"BILL-{site.code}-{cycle_start_local.strftime('%Y%m')}-{job.id:04d}"
    bill = models.Bill(
        site_id=site.id, job_id=job.id, bill_no=bill_no,
        master_total_kwh=quantize(bill_master_total, Q4),
        charging_kwh=quantize(sum_charging, Q4),
        estimated_kwh=quantize(sum_estimated, Q4),
        pending_kwh=ZERO,
        pending_order_count=len(pending_orders),
        public_kwh=quantize(bill_public, Q4),
        unexplained_kwh=quantize(bill_unexplained, Q4),
        suspended_kwh=bill_suspended,
        charging_amount=bill_charging_amount,
        public_amount=bill_public_amount,
        status=models.BILL_DRAFT,
    )
    if over_threshold:
        bill.status = models.BILL_SUSPENDED
        job.status = models.JOB_SUSPENDED
        job.message = (
            f"未解释差额 {quantize(bill_unexplained, Q4)} kWh 占总表 "
            f"{quantize(unexplained_pct * 100, 2)}%，超过约定阈值 "
            f"{quantize(site.diff_threshold_pct * 100, 2)}%，暂停确认"
        )
        tasks.append(models.ReviewTask(
            site_id=site.id, job_id=job.id,
            task_kind="unexplained_diff",
            title="未解释差额超出约定范围，账单暂停确认",
            detail=job.message,
            assignee=models.ROLE_PROPERTY,
        ))
    elif has_suspended_interval:
        bill.status = models.BILL_SUSPENDED
        job.status = models.JOB_SUSPENDED
        job.message = "存在挂起计量段（回退无依据/缺首尾读数），补齐依据前暂停确认"
    else:
        job.status = models.JOB_COMPLETED
        job.message = "核算完成"
    job.finished_at = datetime.utcnow()
    db.add(bill)

    for t in tasks:
        db.add(t)
    db.flush()
    return job
