"""跨月回补核算：撤贡献不重复加、分段断点续算、修订链挂起、双批次防双撤、
双方复核与分段退回、固定数据集合、旧账不覆盖与应付对平。"""
import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation
from app.services.billing import confirm_bill
from app.services.importer import import_file, promote_blocked_revisions
from app.services import backfill as bf

CYCLE = (datetime(2026, 10, 1), datetime(2026, 11, 1))
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"
BACKFILL_CSV = SAMPLE / "S01" / "backfill"
ORDER_HEADER = ["record_no", "order_no", "device_id", "start_time", "end_time",
                "start_reading", "end_reading", "reported_energy"]


def _site(db, code="S01"):
    return db.scalar(select(models.Site).where(models.Site.code == code))


def _user(db, username):
    return db.scalar(select(models.User).where(models.User.username == username))


def _confirmed_october_bill(db, site):
    job = run_reconciliation(db, site, *CYCLE, "tester")
    db.commit()
    bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
    confirm_bill(db, bill, _user(db, "finance"))
    db.commit()
    return job, bill


def _import(db, site, path, source_type, name=None):
    return import_file(db, site, source_type, name or path.name,
                       path.read_bytes(), "tester", "data/test_originals")


def _write_csv(tmp_path, name, header, rows):
    p = tmp_path / name
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return p


def _run_one_batch(db, site):
    batch = bf.create_backfill_batch(db, site, "tester")
    db.commit()
    bf.run_backfill_batch(db, batch)
    db.commit()
    return batch


def _items(db, batch_id):
    return db.scalars(select(models.BackfillAdjustmentItem).where(
        models.BackfillAdjustmentItem.batch_id == batch_id
    ).order_by(models.BackfillAdjustmentItem.id)).all()


# ---------------- 1. 订单纠正：撤原贡献+计修正值，旧账不覆盖 ----------------

def test_order_correction_withdraws_then_reissues():
    db = SessionLocal()
    try:
        site = _site(db)
        job, bill = _confirmed_october_bill(db, site)
        original_amount = bill.charging_amount
        _import(db, site, SAMPLE / "S01" / "orders_v3_after_adopt.csv", "order")
        db.commit()

        cands = bf.discover_candidates(db, site)
        assert any(c.source_record.source_record_no == "OR-SH-003" for c in cands)
        batch = _run_one_batch(db, site)
        assert batch.status == models.BF_BATCH_COMPLETED

        items = [i for i in _items(db, batch.id)
                 if i.component == models.BF_COMP_CHARGING_AMOUNT]
        assert len(items) == 1
        it = items[0]
        # 25 -> 26 kWh，valley 0.35 => 金额 8.75 -> 9.10
        assert it.energy_before == 25 and it.energy_after == 26
        assert it.amount_delta == pytest.approx(Decimal("0.35"))
        # 旧账金额原样冻结
        db.refresh(bill)
        assert bill.charging_amount == original_amount

        # 原贡献被撤回且只撤回一次，修正贡献生效
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.source_record_no == "OR-SH-003"))
        contribs = db.scalars(select(models.BackfillContribution).where(
            models.BackfillContribution.source_record_id == sr.id,
            models.BackfillContribution.entity_kind == "order")).all()
        assert [c.status for c in contribs].count("withdrawn") == 1
        assert [c.status for c in contribs].count("active") == 1
        # 修订被标记 backfilled，不再被常规作业提升
        rev = db.get(models.RecordRevision, it.to_revision_id)
        assert rev.status == models.REV_BACKFILLED
    finally:
        db.close()


def test_backfilled_revision_is_not_promoted():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, SAMPLE / "S01" / "orders_v3_after_adopt.csv", "order")
        db.commit()
        batch = _run_one_batch(db, site)
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.source_record_no == "OR-SH-003"))
        before_current = sr.current_revision_id
        promoted = promote_blocked_revisions(db, site)
        db.commit()
        assert promoted == 0
        db.refresh(sr)
        assert sr.current_revision_id == before_current
        assert batch.status == models.BF_BATCH_COMPLETED
    finally:
        db.close()


# ---------------- 2. 累计读数纠正：只重算相邻区间，不整段重加 ----------------

def test_cumulative_reading_correction_only_recomputes_adjacent_spans():
    db = SessionLocal()
    try:
        site = _site(db)
        job, bill = _confirmed_october_bill(db, site)
        master_before = bill.master_total_kwh
        _import(db, site, BACKFILL_CSV / "01_corrected_master_reading.csv", "master_reading")
        db.commit()

        batch = _run_one_batch(db, site)
        assert batch.status == models.BF_BATCH_COMPLETED
        master_items = [i for i in _items(db, batch.id)
                        if i.component == models.BF_COMP_MASTER]
        # 10-15 读数 6280->6290：前一区间 +10、后一区间 -10，合计守恒
        deltas = sorted(i.energy_delta for i in master_items)
        assert deltas == [Decimal("-10.0000"), Decimal("10.0000")]
        assert sum(i.energy_delta for i in master_items) == 0
        # 联动的未解释差额同向修正
        unexp = [i for i in _items(db, batch.id)
                 if i.component == models.BF_COMP_UNEXPLAINED_KWH]
        assert sorted(i.energy_delta for i in unexp) == [Decimal("-10.0000"), Decimal("10.0000")]
        # 旧账总表电量绝不被改写
        db.refresh(bill)
        assert bill.master_total_kwh == master_before
    finally:
        db.close()


# ---------------- 3. 迟到订单：从 0 计修正值 ----------------

def test_late_order_is_added_not_duplicated():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        db.commit()

        cands = bf.discover_candidates(db, site)
        late = [c for c in cands if c.change_kind == bf.CHANGE_LATE]
        assert len(late) == 1 and late[0].source_record.source_record_no == "OR-SH-LATE1"

        batch = _run_one_batch(db, site)
        items = [i for i in _items(db, batch.id)
                 if i.component == models.BF_COMP_CHARGING_AMOUNT]
        assert len(items) == 1
        it = items[0]
        assert it.energy_before == 0 and it.energy_after == 20
        # 13:00-14:00 flat 0.75（10-15 前三季度电价）=> 15 元
        assert it.amount_delta == pytest.approx(Decimal("15.0000"))

        # 再跑一次候选发现：同一修订不会重复进批
        assert bf.discover_candidates(db, site) == []
    finally:
        db.close()


# ---------------- 4. 撤销重复记录：撤出原贡献，修正值为 0 ----------------

def test_revocation_withdraws_contribution_to_zero():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "03_revoke_duplicate_order.csv", "order")
        db.commit()

        batch = _run_one_batch(db, site)
        items = [i for i in _items(db, batch.id)
                 if i.component == models.BF_COMP_CHARGING_AMOUNT]
        # O-1002 跨 peak/valley 各 10kWh，撤销后两个时段 after 均为 0
        assert len(items) == 2
        assert all(i.energy_after == 0 for i in items)
        assert sum(i.amount_delta for i in items) == pytest.approx(Decimal("-14.0000"))
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.source_record_no == "OR-SH-002"))
        active = db.scalars(select(models.BackfillContribution).where(
            models.BackfillContribution.source_record_id == sr.id,
            models.BackfillContribution.status == bf.CONTRIB_ACTIVE)).all()
        assert active == []
    finally:
        db.close()


# ---------------- 5. 电价更正：电量不变，仅金额与舍入变化 ----------------

def test_tariff_correction_keeps_energy_changes_price_only():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "04_tariff_correction.csv", "tariff")
        db.commit()
        batch = _run_one_batch(db, site)
        items = _items(db, batch.id)
        assert items, "电价更正必须产出调整项"
        assert all(i.energy_delta == 0 for i in items)
        assert all(i.component == models.BF_COMP_TARIFF for i in items)
        assert any(i.amount_delta != 0 for i in items)
        # 舍入变化单独留字段
        assert all(i.rounding_delta is not None for i in items)
    finally:
        db.close()


# ---------------- 6. 修订链不连续：挂起，不用最新行猜历史 ----------------

def test_broken_revision_chain_suspends_segment():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        # 先产生 rev2（85->86），再导入声明 replaces=1 的 rev3，链断裂
        _import(db, site, SAMPLE / "S01" / "orders_v3_after_adopt.csv", "order")
        _import(db, site, BACKFILL_CSV / "05_broken_chain_order.csv", "order")
        db.commit()

        batch = _run_one_batch(db, site)
        assert batch.status == models.BF_BATCH_SUSPENDED
        seg = db.scalar(select(models.BackfillSegment).where(
            models.BackfillSegment.batch_id == batch.id))
        assert seg.status == models.BF_SEG_SUSPENDED and seg.suspended_reason
        sus = [i for i in _items(db, batch.id) if i.status == models.BF_ADJ_SUSPENDED]
        assert sus and "承接版本" in sus[0].suspended_reason
        # 挂起项不产生可结算金额
        assert all(i.amount_delta == 0 for i in sus)

        # 续算：保持挂起且不产生重复调整项/重复核对事项
        bf.run_backfill_batch(db, batch)
        db.commit()
        again = _items(db, batch.id)
        assert len(again) == len(_items(db, batch.id))
        tasks = db.scalars(select(models.ReviewTask).where(
            models.ReviewTask.task_kind == "backfill_suspended")).all()
        assert len(tasks) == 1
    finally:
        db.close()


# ---------------- 7. 分段进度：失败只重算未完成分段 ----------------

def test_segment_checkpoint_resumes_only_unfinished(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "01_corrected_master_reading.csv", "master_reading")
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        _import(db, site, BACKFILL_CSV / "03_revoke_duplicate_order.csv", "order")
        db.commit()

        batch = bf.create_backfill_batch(db, site, "tester")
        db.commit()
        assert batch.segment_total == 3

        calls = {"n": 0}
        real = bf._process_segment

        def flaky(db_, site_, batch_, seg_, candidates_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated segment failure")
            return real(db_, site_, batch_, seg_, candidates_)

        monkeypatch.setattr(bf, "_process_segment", flaky)
        bf.run_backfill_batch(db, batch)
        db.commit()
        assert batch.status == models.BF_BATCH_FAILED
        segs = db.scalars(select(models.BackfillSegment).where(
            models.BackfillSegment.batch_id == batch.id)).all()
        failed = [s for s in segs if s.status == models.BF_SEG_FAILED]
        done = [s for s in segs if s.status == models.BF_SEG_DONE]
        assert len(failed) == 1 and len(done) == 2

        monkeypatch.undo()
        bf.run_backfill_batch(db, batch)
        db.commit()
        assert batch.status == models.BF_BATCH_COMPLETED
        segs = db.scalars(select(models.BackfillSegment).where(
            models.BackfillSegment.batch_id == batch.id)).all()
        # 失败段重试两次，其余段只执行一次
        retried = [s for s in segs if s.attempts == 2]
        once = [s for s in segs if s.attempts == 1]
        assert len(retried) == 1 and len(once) == 2
    finally:
        db.close()


# ---------------- 8. 双批次同贡献：后者看到前者生效记录，不双撤 ----------------

def test_second_batch_builds_on_first_without_double_withdraw(tmp_path):
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        p1 = _write_csv(tmp_path, "c1.csv", ORDER_HEADER,
                        [["OR-SH-003", "O-1003", "DEV-A", "2026-10-05 02:00",
                          "2026-10-05 04:00", "60", "86", "26"]])
        _import(db, site, p1, "order", "c1.csv")
        db.commit()
        b1 = _run_one_batch(db, site)

        p2 = _write_csv(tmp_path, "c2.csv", ORDER_HEADER,
                        [["OR-SH-003", "O-1003", "DEV-A", "2026-10-05 02:00",
                          "2026-10-05 04:00", "60", "88", "28"]])
        _import(db, site, p2, "order", "c2.csv")
        db.commit()
        cands = bf.discover_candidates(db, site)
        assert len(cands) == 1 and cands[0].revision.revision_no == 3
        b2 = _run_one_batch(db, site)

        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.source_record_no == "OR-SH-003"))
        rows = db.scalars(select(models.BackfillContribution).where(
            models.BackfillContribution.source_record_id == sr.id,
            models.BackfillContribution.entity_kind == "order")).all()
        # 每个电价段当前只有一条 active；每条历史行只被一个批次撤回
        by_seg = {}
        for r in rows:
            by_seg.setdefault(r.segment_name, []).append(r)
        for seg_name, rs in by_seg.items():
            assert sum(1 for r in rs if r.status == "active") == 1
            batches_used = {r.withdrawn_by_batch_id for r in rs if r.status == "withdrawn"}
            assert len(batches_used) == len([r for r in rs if r.status == "withdrawn"])
        # 第二批的 before 必须是第一批的 after（26 -> 28），而非原始 25
        it = [i for i in _items(db, b2.id) if i.component == models.BF_COMP_CHARGING_AMOUNT][0]
        assert it.energy_before == 26 and it.energy_after == 28
        assert b1.id != b2.id
    finally:
        db.close()


# ---------------- 9. 固定数据集合：批次启动后的新更正进下一批 ----------------

def test_batch_freezes_dataset_new_correction_goes_next_batch(tmp_path):
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        db.commit()
        batch = bf.create_backfill_batch(db, site, "tester")
        db.commit()
        frozen_sr = {row.source_record_id for row in db.scalars(
            select(models.BackfillBatchInput).where(
                models.BackfillBatchInput.batch_id == batch.id)).all()}

        # 批次创建之后才到达的更正，不在本批固定集合内
        p = _write_csv(tmp_path, "later.csv", ORDER_HEADER,
                       [["OR-SH-004", "O-1004", "DEV-A", "2026-10-10 14:00",
                         "2026-10-10 16:00", "120", "151", "31"]])
        _import(db, site, p, "order", "later.csv")
        db.commit()
        bf.run_backfill_batch(db, batch)
        db.commit()
        touched_sr = {i.source_record_id for i in _items(db, batch.id)}
        assert touched_sr <= frozen_sr

        next_cands = bf.discover_candidates(db, site)
        assert any(c.source_record.source_record_no == "OR-SH-004" for c in next_cands)
    finally:
        db.close()


# ---------------- 10. 双方复核：物业否认只退回本调整，不阻断其他项 ----------------

def test_property_reject_isolates_single_item():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        _import(db, site, BACKFILL_CSV / "03_revoke_duplicate_order.csv", "order")
        db.commit()
        batch = _run_one_batch(db, site)
        items = _items(db, batch.id)
        assert len(items) >= 2
        prop = _user(db, "property_sh")
        fin = _user(db, "finance")

        target = items[0]
        bf.review_item(db, target, prop, "reject", "物业否认该段计量依据")
        db.commit()
        db.refresh(target)
        assert target.status == models.BF_ADJ_REJECTED

        # 其余调整项仍可走完整复核流程，不受退回影响
        for other in items[1:]:
            bf.review_item(db, other, prop, "acknowledge", "核对无误")
            bf.review_item(db, other, fin, "confirm", "入结算")
        db.commit()

        bill = db.get(models.Bill, items[0].bill_id)
        view = bf.bill_payable_view(db, bill)
        assert view["backfill_rejected_count"] == 1
        assert view["backfill_confirmed_count"] == len(items) - 1
        # 退回项金额不进应付
        confirmed_sum = sum(i.amount_delta for i in items[1:])
        assert view["backfill_confirmed_delta"] == pytest.approx(confirmed_sum)
        # 复核留痕双方分别保存
        reviews = db.scalars(select(models.BackfillReview).where(
            models.BackfillReview.item_id == target.id)).all()
        assert [r.review_party for r in reviews] == ["property"]
    finally:
        db.close()


def test_finance_confirm_requires_property_review():
    db = SessionLocal()
    try:
        from fastapi import HTTPException
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        db.commit()
        batch = _run_one_batch(db, site)
        it = _items(db, batch.id)[0]
        fin = _user(db, "finance")
        with pytest.raises(HTTPException):
            bf.review_item(db, it, fin, "confirm", None)
    finally:
        db.close()


# ---------------- 11. 应付对平：旧账+人工更正+回补项，找不到来源不进结算 ----------------

def test_payable_view_reconciles_and_unknown_amount_never_settles():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        db.commit()
        batch = _run_one_batch(db, site)
        bill = db.scalar(select(models.Bill).where(
            models.Bill.id == _items(db, batch.id)[0].bill_id))
        prop = _user(db, "property_sh")
        fin = _user(db, "finance")
        for it in _items(db, batch.id):
            bf.review_item(db, it, prop, "acknowledge", None)
            bf.review_item(db, it, fin, "confirm", None)
        db.commit()

        view = bf.bill_payable_view(db, bill)
        expected = (bill.charging_amount + bill.public_amount
                    + Decimal("15.0000"))
        assert view["current_payable_amount"] == pytest.approx(expected)
        assert view["backfill_pending_count"] == 0

        # 挂起项永远不进入应付：构造断链批次
        _import(db, site, SAMPLE / "S01" / "orders_v3_after_adopt.csv", "order")
        _import(db, site, BACKFILL_CSV / "05_broken_chain_order.csv", "order")
        db.commit()
        suspended_batch = _run_one_batch(db, site)
        assert suspended_batch.status == models.BF_BATCH_SUSPENDED
        view2 = bf.bill_payable_view(db, bill)
        assert view2["current_payable_amount"] == view["current_payable_amount"]
    finally:
        db.close()


def test_backfilled_revision_does_not_block_later_bill_confirmation():
    """回补已处理的修订不再作为“未处理新修订”阻塞后续月份账单确认。"""
    from app.services.billing import check_new_revisions, confirm_bill

    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, SAMPLE / "S01" / "orders_v3_after_adopt.csv", "order")
        db.commit()
        _run_one_batch(db, site)

        # 11 月新作业采用当前仍生效的原版本；回补修订已 backfilled，预检查应放行
        nov_job = run_reconciliation(db, site, datetime(2026, 11, 1),
                                     datetime(2026, 12, 1), "tester")
        db.commit()
        assert check_new_revisions(db, nov_job) == []
        nov_bill = db.scalar(select(models.Bill).where(models.Bill.job_id == nov_job.id))
        if nov_bill.status != models.BILL_SUSPENDED:
            confirm_bill(db, nov_bill, _user(db, "finance"))
            db.commit()
            assert nov_bill.status == models.BILL_CONFIRMED
    finally:
        db.close()


# ---------------- 12. 后续账单关联单独保存 ----------------

def test_settlement_bill_link_is_separate_from_original():
    db = SessionLocal()
    try:
        site = _site(db)
        _confirmed_october_bill(db, site)
        _import(db, site, BACKFILL_CSV / "02_late_order.csv", "order")
        db.commit()
        batch = _run_one_batch(db, site)
        items = _items(db, batch.id)
        prop = _user(db, "property_sh")
        fin = _user(db, "finance")
        for it in items:
            bf.review_item(db, it, prop, "acknowledge", None)
            bf.review_item(db, it, fin, "confirm", None)
        db.commit()

        # 11 月新账单确认时自动关联已确认回补项
        nov_job = run_reconciliation(db, site, datetime(2026, 11, 1),
                                     datetime(2026, 12, 1), "tester")
        db.commit()
        nov_bill = db.scalar(select(models.Bill).where(models.Bill.job_id == nov_job.id))
        if nov_bill.status != models.BILL_SUSPENDED:
            confirm_bill(db, nov_bill, fin)
            db.commit()
            links = db.scalars(select(models.BackfillBillLink).where(
                models.BackfillBillLink.settlement_bill_id == nov_bill.id)).all()
            assert len(links) == len(items)
            assert all(l.original_bill_id != nov_bill.id for l in links)
    finally:
        db.close()
