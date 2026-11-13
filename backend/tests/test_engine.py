"""核算引擎：时区/电价边界拆分、估算/待补、回退挂起、换表桥接、
重复/冲突订单、未解释差额、阈值挂起、重跑冻结输入、金额精度。"""
from datetime import datetime

from sqlalchemy import select

from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation

CYCLE = (datetime(2026, 10, 1), datetime(2026, 11, 1))


def _site(db, code):
    return db.scalar(select(models.Site).where(models.Site.code == code))


def _order(db, site_id, order_no):
    return db.scalar(select(models.ChargingOrder).where(
        models.ChargingOrder.site_id == site_id,
        models.ChargingOrder.order_no == order_no))


def _new_job(db, site):
    job = run_reconciliation(db, site, *CYCLE, "pytest")
    db.commit()
    return job


def test_timezone_boundary_split_with_measured_midpoint():
    db = SessionLocal()
    try:
        site = _site(db, "S01")
        job = _new_job(db, site)
        order = _order(db, site.id, "O-1001")
        splits = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id,
            models.OrderIntervalSplit.order_id == order.id,
        ).order_by(models.OrderIntervalSplit.start_ts)).all()
        # 09:00-10:00 peak、10:00-11:00 flat，按 10:00 中间计量值实测拆分
        assert [s.segment_name for s in splits] == ["peak", "flat"]
        assert [s.energy_kwh for s in splits] == [15, 15]
        assert [s.basis_status for s in splits] == ["measured", "measured"]
        assert float(splits[0].price_per_kwh) == 1.05
        assert float(splits[1].price_per_kwh) == 0.75
        assert float(splits[0].amount) == 15.75
        assert float(splits[1].amount) == 11.25
        # 本地时间展示正确（UTC 存储 + Asia/Shanghai 转换）
        assert splits[0].start_ts_local.hour == 9
    finally:
        db.close()


def test_estimated_and_pending_items_are_labeled_not_fabricated():
    db = SessionLocal()
    try:
        site = _site(db, "S02")
        job = _new_job(db, site)
        # O-2002 跨 peak/flat 且无中间计量：按时长拆分已知上报电量，标注估算
        o2002 = _order(db, site.id, "O-2002")
        splits = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id,
            models.OrderIntervalSplit.order_id == o2002.id)).all()
        assert len(splits) == 2
        assert all(s.basis_status == models.BASIS_ESTIMATED for s in splits)
        assert sum(s.energy_kwh for s in splits) == 50  # 合计等于上报总量，不凭空编数
        # O-2004 无起止读数且上报为 0：待补，电量金额均为 None
        o2004 = _order(db, "x") if False else _order(db, site.id, "O-2004")
        pending = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id,
            models.OrderIntervalSplit.order_id == o2004.id)).all()
        assert all(s.basis_status == models.BASIS_PENDING for s in pending)
        assert all(s.energy_kwh is None and s.amount is None for s in pending)
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        assert bill.pending_order_count >= 1
    finally:
        db.close()


def test_rollback_without_replacement_suspends_and_never_negative():
    db = SessionLocal()
    try:
        site = _site(db, "S02")
        job = _new_job(db, site)
        intervals = db.scalars(select(models.ReconciliationInterval).where(
            models.ReconciliationInterval.job_id == job.id)).all()
        # 任何落库电量都不允许为负
        for iv in intervals:
            for col in ("master_energy_kwh", "device_meter_kwh", "charging_kwh",
                        "estimated_kwh", "public_kwh", "unexplained_kwh"):
                val = getattr(iv, col)
                assert val is None or val >= 0
        # M-E 回退段对应区间挂起
        assert any(iv.basis_status == models.BASIS_SUSPENDED for iv in intervals)
        task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.task_kind == "meter_rollback",
            models.ReviewTask.site_id == site.id))
        assert task is not None and "M-E" in task.title
    finally:
        db.close()


def test_replacement_bridge_counts_full_energy():
    db = SessionLocal()
    try:
        site = _site(db, "S02")
        job = _new_job(db, site)
        meter_c = db.scalar(select(models.Meter).where(
            models.Meter.site_id == site.id, models.Meter.meter_no == "M-C"))
        # 换表桥接：(800-0)+(900-50)=1650，不因读数从 800 掉到 50 产生负值
        spans_total = sum(
            iv.device_meter_kwh for iv in db.scalars(
                select(models.ReconciliationInterval).where(
                    models.ReconciliationInterval.job_id == job.id))
        )
        # C=1650, D=1200, E 仅首段 100（次段挂起不计）
        assert spans_total == 2950
        assert meter_c is not None
    finally:
        db.close()


def test_duplicate_order_counted_once_and_conflict_excluded_until_resolved():
    db = SessionLocal()
    try:
        site = _site(db, "S02")
        job = _new_job(db, site)
        # 重复上报：O-2006D 被停用，只有 O-2006 进拆分
        keep = _order(db, site.id, "O-2006")
        drop = _order(db, site.id, "O-2006D")
        assert drop.status == models.ENTITY_SUPERSEDED
        kept_splits = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id,
            models.OrderIntervalSplit.order_id == keep.id)).all()
        assert len(kept_splits) == 1
        # 冲突两张订单在运营确认前都不计入
        conflict = db.scalar(select(models.OrderConflict).where(
            models.OrderConflict.site_id == site.id,
            models.OrderConflict.status == models.CONFLICT_OPEN))
        winner = db.get(models.ChargingOrder, conflict.order_a_id)
        before = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job.id,
            models.OrderIntervalSplit.order_id.in_([conflict.order_a_id,
                                                    conflict.order_b_id]))).all()
        assert before == []
        # 运营确认有效订单后，下一轮计入
        loser = db.get(models.ChargingOrder, conflict.order_b_id)
        winner.conflict_id = None
        loser.status = models.ENTITY_SUPERSEDED
        loser.conflict_id = None
        conflict.status = models.CONFLICT_RESOLVED
        conflict.resolved_order_id = winner.id
        conflict.resolution_note = "运营核对工单后确认 A 有效"
        db.commit()
        job2 = _new_job(db, site)
        after = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job2.id,
            models.OrderIntervalSplit.order_id == winner.id)).all()
        assert len(after) >= 1
    finally:
        db.close()


def test_unexplained_diff_kept_separate_and_threshold_suspends_bill():
    db = SessionLocal()
    try:
        site = _site(db, "S02")
        job = _new_job(db, site)
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        # 未确认公共负荷不得计入
        assert bill.public_kwh == 0
        # 未解释差额独立保留：3200 - 2950 - 0 = 250，未摊给充电
        assert bill.unexplained_kwh == 250
        assert bill.status == models.BILL_SUSPENDED
        assert "阈值" in (job.message or "")

        site1 = _site(db, "S01")
        job1 = _new_job(db, site1)
        bill1 = db.scalar(select(models.Bill).where(models.Bill.job_id == job1.id))
        # 已确认公共照明计入；小差额在 2% 阈值内，账单可进入确认流程
        assert bill1.public_kwh > 0
        assert bill1.status == models.BILL_DRAFT
        # 区间恒等式：总表 = 设备表 + 公共 + 未解释（逐段）
        for iv in db.scalars(select(models.ReconciliationInterval).where(
                models.ReconciliationInterval.job_id == job1.id)):
            assert iv.master_energy_kwh == (
                iv.device_meter_kwh + iv.public_kwh + iv.unexplained_kwh
            )
    finally:
        db.close()


def test_rerun_uses_frozen_inputs_and_amount_precision():
    db = SessionLocal()
    try:
        site = _site(db, "S01")
        job1 = _new_job(db, site)
        job2 = run_reconciliation(db, site, *CYCLE, "pytest", rerun_of_job_id=job1.id)
        db.commit()
        snap1 = {(s.input_kind, s.entity_id, s.revision_id)
                 for s in db.scalars(select(models.JobInput).where(
                     models.JobInput.job_id == job1.id))}
        snap2 = {(s.input_kind, s.entity_id, s.revision_id)
                 for s in db.scalars(select(models.JobInput).where(
                     models.JobInput.job_id == job2.id))}
        assert snap1 == snap2  # 重跑沿用完全相同的输入
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job1.id))
        # 金额 4 位小数精度；拆分金额合计与账单充电金额一致
        splits = db.scalars(select(models.OrderIntervalSplit).where(
            models.OrderIntervalSplit.job_id == job1.id,
            models.OrderIntervalSplit.amount.is_not(None))).all()
        total = sum((s.amount or 0) for s in splits)
        assert total == bill.charging_amount
        for s in splits:
            assert s.amount == s.amount.quantize(__import__("decimal").Decimal("0.0001"))
    finally:
        db.close()


def test_rerun_ignores_tariff_imported_after_freeze(tmp_path):
    """作业后导入的新电价版本，重跑必须仍按冻结的旧电价计价。"""
    import csv
    from app.services.importer import import_file

    db = SessionLocal()
    try:
        site = _site(db, "S01")
        job1 = _new_job(db, site)
        before = {s.id: float(s.price_per_kwh) for s in db.scalars(
            select(models.OrderIntervalSplit).where(
                models.OrderIntervalSplit.job_id == job1.id))}

        # 作业后导入一套全新高价电价（同号修订会被 blocked；新记录号则成为新增版本）
        p = tmp_path / "tariff_new.csv"
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["record_no", "tariff_name", "effective_date", "end_date",
                        "segment_name", "start_hour", "end_hour", "price"])
            for seg, sh, eh, price in [
                ("valley", 0, 6, 9.99), ("peak", 6, 10, 9.99), ("flat", 10, 18, 9.99),
                ("peak", 18, 22, 9.99), ("valley", 22, 24, 9.99)]:
                w.writerow(["TAR-SH-NEW", "作业后新电价", "2026-10-01", "", seg, sh, eh, price])
        import_file(db, site, "tariff", "tariff_new.csv", p.read_bytes(),
                    "pytest", "data/test_originals")
        db.commit()

        job2 = run_reconciliation(db, site, *CYCLE, "pytest", rerun_of_job_id=job1.id)
        db.commit()
        after = {s.id: float(s.price_per_kwh) for s in db.scalars(
            select(models.OrderIntervalSplit).where(
                models.OrderIntervalSplit.job_id == job2.id))}
        # 重跑单价集合与首轮完全一致，未出现 9.99 的新价格
        assert 9.99 not in after.values()
        assert sorted(before.values()) == sorted(after.values())
    finally:
        db.close()
