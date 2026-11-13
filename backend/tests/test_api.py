"""API 端到端：登录、角色权限、物业站点隔离、账单确认前检查、差额更正、下钻。"""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation

client = TestClient(app)


def _login(username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_and_wrong_password():
    assert client.post("/api/auth/login",
                       json={"username": "finance", "password": "bad"}).status_code == 401
    token = _login("finance", "Finance@123")
    r = client.get("/api/sites", headers=_auth(token))
    assert r.status_code == 200 and len(r.json()) == 2


def _seed_jobs():
    """为两个站点各生成一轮核算，产生核对事项与账单。"""
    db = SessionLocal()
    try:
        for code in ("S01", "S02"):
            site = db.scalar(select(models.Site).where(models.Site.code == code))
            run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
    finally:
        db.close()


def test_property_user_scoped_to_assigned_site():
    _seed_jobs()
    token = _login("property_sh", "Property@123")
    sites = client.get("/api/sites", headers=_auth(token)).json()
    assert {s["code"] for s in sites} == {"S01"}
    # 访问未授权站点被拒
    r = client.get("/api/sites/2", headers=_auth(token))
    assert r.status_code == 403
    # 物业看不到运营/财务的核对事项，只能看 assignee=property 且在授权站点内的条目
    tasks = client.get("/api/tasks", headers=_auth(token)).json()
    assert all(t["assignee"] == "property" for t in tasks)  # S01 无物业事项时为空列表
    token_xj = _login("property_xj", "Property@123")
    tasks_xj = client.get("/api/tasks", headers=_auth(token_xj)).json()
    assert tasks_xj and all(t["assignee"] == "property" for t in tasks_xj)
    kinds = {t["task_kind"] for t in tasks_xj}
    assert "public_confirmation" in kinds and "unexplained_diff" in kinds
    # 物业不能发起核算
    r = client.post("/api/jobs", headers=_auth(token),
                    json={"site_id": 1,
                          "cycle_start": "2026-10-01T00:00:00",
                          "cycle_end": "2026-11-01T00:00:00"})
    assert r.status_code == 403


def test_property_respond_to_own_task_and_cannot_touch_others():
    _seed_jobs()
    db = SessionLocal()
    try:
        task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.assignee == models.ROLE_PROPERTY,
            models.ReviewTask.site_id == 2))
        operator_task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.assignee == models.ROLE_OPERATOR))
    finally:
        db.close()
    token = _login("property_xj", "Property@123")
    r = client.post(f"/api/tasks/{task.id}/respond", headers=_auth(token),
                    json={"response": "物业确认公共照明 320kWh 属实"})
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    r = client.post(f"/api/tasks/{operator_task.id}/respond", headers=_auth(token),
                    json={"response": "越权尝试"})
    assert r.status_code == 403


def test_bill_confirm_precheck_block_and_adjustment_keeps_original():
    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        bill_id = bill.id
        original_unexplained = bill.unexplained_kwh
        site2 = db.scalar(select(models.Site).where(models.Site.code == "S02"))
        job2 = run_reconciliation(db, site2, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        suspended_bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job2.id))
        suspended_id = suspended_bill.id
    finally:
        db.close()

    fin = _auth(_login("finance", "Finance@123"))
    # 挂起账单不允许确认
    r = client.post(f"/api/bills/{suspended_id}/confirm", headers=fin)
    assert r.status_code == 409
    # 正常账单：确认前检查无新修订
    pre = client.get(f"/api/bills/{bill_id}/precheck", headers=fin).json()
    assert pre["new_revision_count"] == 0
    r = client.post(f"/api/bills/{bill_id}/confirm", headers=fin)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    # 重复确认被拒
    assert client.post(f"/api/bills/{bill_id}/confirm", headers=fin).status_code == 409

    # 已确认账单只能差额更正，原值保留
    r = client.post(f"/api/bills/{bill_id}/adjustments", headers=fin, json={
        "component": "unexplained_kwh",
        "corrected_value": "25.0000",
        "reason": "物业补充线损依据，财务复核后登记差额更正",
    })
    assert r.status_code == 200
    adj = r.json()
    assert adj["original_value"] == str(original_unexplained)
    assert adj["delta"] == str(25 - float(original_unexplained))
    db = SessionLocal()
    try:
        bill = db.get(models.Bill, bill_id)
        # 原字段不被替换
        assert bill.unexplained_kwh == original_unexplained
        assert bill.status == models.BILL_ADJUSTED
    finally:
        db.close()


def test_confirm_blocked_when_new_revision_arrives():
    from pathlib import Path
    from app.services.importer import import_file

    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        bill_id = bill.id
        content = (Path(__file__).resolve().parent.parent
                   / "sample_data" / "S01" / "orders_v3_after_adopt.csv").read_bytes()
        import_file(db, site, "order", "orders_v3_after_adopt.csv", content,
                    "tester", "data/test_originals")
        db.commit()
    finally:
        db.close()
    fin = _auth(_login("finance", "Finance@123"))
    pre = client.get(f"/api/bills/{bill_id}/precheck", headers=fin).json()
    assert pre["new_revision_count"] == 1
    r = client.post(f"/api/bills/{bill_id}/confirm", headers=fin)
    assert r.status_code == 409 and "新修订" in r.json()["detail"]


def test_drill_down_from_interval_to_splits_and_entities():
    _seed_jobs()
    fin = _auth(_login("finance", "Finance@123"))
    jobs = client.get("/api/jobs?site_id=1", headers=fin).json()
    job_id = jobs[0]["id"]
    intervals = client.get(f"/api/jobs/{job_id}/intervals", headers=fin).json()
    assert intervals and "master_reading_start" in intervals[0]
    splits = client.get(f"/api/jobs/{job_id}/splits", headers=fin).json()
    assert splits and all("segment_name" in s for s in splits)
    readings = client.get("/api/entities/readings?site_id=1", headers=fin).json()
    assert readings and "meter_no" in readings[0]
    orders = client.get("/api/entities/orders?site_id=1", headers=fin).json()
    assert len(orders) >= 9
    files = client.get("/api/imports/files?site_id=1", headers=fin).json()
    rows = client.get(f"/api/imports/files/{files[0]['id']}/rows", headers=fin).json()
    assert rows and "raw_line" in rows[0] and "line_no" in rows[0]


def test_operator_resolves_conflict():
    op = _auth(_login("operator", "Operator@123"))
    conflicts = client.get("/api/conflicts?site_id=2", headers=op).json()
    open_conflicts = [c for c in conflicts if c["status"] == "open"]
    assert open_conflicts
    c = open_conflicts[0]
    r = client.post(f"/api/conflicts/{c['id']}/resolve", headers=op, json={
        "resolved_order_id": c["order_a_id"],
        "resolution_note": "核对设备日志确认 A 订单真实",
    })
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    # 物业无权处理冲突
    prop = _auth(_login("property_xj", "Property@123"))
    r2 = client.post(f"/api/conflicts/{c['id']}/resolve", headers=prop, json={
        "resolved_order_id": c["order_a_id"], "resolution_note": "x"})
    assert r2.status_code == 403


def test_backfill_batch_http_flow_and_role_guard():
    """回补全链路 HTTP：候选预览 -> 建批执行 -> 分段 -> 物业复核 -> 财务确认 -> 应付视图。"""
    from pathlib import Path
    from app.services.importer import import_file

    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        # 回补只面向已确认月份，先确认 10 月账单
        from app.services.billing import confirm_bill
        confirm_bill(db, bill, db.scalar(select(models.User).where(
            models.User.username == "finance")))
        db.commit()
        bill_id = bill.id
        content = (Path(__file__).resolve().parent.parent
                   / "sample_data" / "S01" / "backfill" / "02_late_order.csv").read_bytes()
        import_file(db, site, "order", "late.csv", content, "tester", "data/test_originals")
        db.commit()
    finally:
        db.close()

    fin = _auth(_login("finance", "Finance@123"))
    prop = _auth(_login("property_sh", "Property@123"))

    # 物业不能创建批次
    assert client.post("/api/backfill/batches", headers=prop,
                       json={"site_id": 1}).status_code == 403
    # 候选预览
    cands = client.get("/api/backfill/candidates?site_id=1", headers=fin).json()
    assert any(c["change_kind"] == "late" for c in cands)
    # 创建并执行
    r = client.post("/api/backfill/batches", headers=fin,
                    json={"site_id": 1, "reason_note": "http 回补"})
    assert r.status_code == 200
    batch_id = r.json()["id"]
    r = client.post(f"/api/backfill/batches/{batch_id}/run", headers=fin)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    # 分段进度
    segs = client.get(f"/api/backfill/batches/{batch_id}/segments", headers=fin).json()
    assert segs and all(s["status"] == "done" for s in segs)
    # 调整项
    items = client.get(f"/api/backfill/batches/{batch_id}/items", headers=fin).json()
    assert items and any(i["amount_delta"] == "15.0000" for i in items)
    item_id = items[0]["id"]
    # 财务不能跳过物业直接确认
    assert client.post(f"/api/backfill/items/{item_id}/reviews", headers=fin,
                       json={"action": "confirm"}).status_code == 409
    # 物业复核 -> 财务确认
    assert client.post(f"/api/backfill/items/{item_id}/reviews", headers=prop,
                       json={"action": "acknowledge", "comment": "核对无误"}).status_code == 200
    r = client.post(f"/api/backfill/items/{item_id}/reviews", headers=fin,
                    json={"action": "confirm"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    # 双方复核分别留痕
    reviews = client.get(f"/api/backfill/items/{item_id}/reviews", headers=fin).json()
    assert {rv["review_party"] for rv in reviews} == {"property", "finance"}
    # 应付视图同时引用旧账与回补项
    pv = client.get(f"/api/bills/{bill_id}/payable", headers=fin).json()
    assert pv["backfill_confirmed_count"] >= 1
    assert pv["backfill_confirmed_delta"] == "15.0000"
    assert float(pv["current_payable_amount"]) == (
        float(pv["original_charging_amount"])
        + float(pv["original_public_amount"])
        + float(pv["manual_adjustment_delta"])
        + float(pv["backfill_confirmed_delta"]))
    # 账单下钻能看到回补项
    bf_items = client.get(f"/api/bills/{bill_id}/backfill-items", headers=fin).json()
    assert len(bf_items) >= 1
