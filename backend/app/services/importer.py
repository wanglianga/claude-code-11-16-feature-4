"""导入服务。

规则：
1. 原始文件落盘并计算 sha256，逐行保存原始文本与物理行号（原件索引）。
2. 按 (站点, 来源类型, 来源记录号) 识别业务记录；同号同内容=重复，不新增版本。
3. 同号内容变化=修订：原数据未被账单采用时新版本生效、旧版本置 superseded；
   已被核算作业采用时新版本记 blocked_adopted，绝不覆盖已采用数据，留待下一轮。
4. 单行解析失败仅回滚该行 SAVEPOINT，原件行以 rejected 留痕，不影响其他行。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .. import models
from ..models import (
    ENTITY_ACTIVE,
    ENTITY_REVOKED,
    ENTITY_SUPERSEDED,
    REV_APPLIED,
    REV_BLOCKED_ADOPTED,
    REV_DUPLICATE,
)
from .timeutil import parse_local_datetime, utc_to_local


class ImportError_(Exception):
    pass


# 可选的修订链控制列：
# - is_revocation=true 表示该行是对同号记录的撤销（迟到的重复/误报撤回）
# - replaces_revision_no 显式声明本版本承接的来源版本号，用于回补按来源顺序收齐校验
REVOCATION_FIELD = "is_revocation"
REPLACES_FIELD = "replaces_revision_no"


def augment_chain_fields(clean: dict) -> dict:
    """把可选的撤销/版本链字段纳入 payload（缺省即为普通修订）。"""
    clean = dict(clean)
    clean["_is_revocation"] = b(clean.get(REVOCATION_FIELD, "false"))
    replaces = (clean.get(REPLACES_FIELD) or "").strip()
    clean["_replaces_revision_no"] = int(replaces) if replaces else None
    return clean


def is_revocation_payload(payload: dict) -> bool:
    return bool(payload.get("_is_revocation"))


def d(value) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ImportError_(f"非法数值: {value!r}") from exc


def b(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "已确认"}


def canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class RowResult:
    line_no: int
    source_record_no: str | None
    outcome: str
    note: str | None = None


def _get_or_create_meter(db: Session, site_id: int, meter_no: str, kind: str, device_id):
    meter = db.scalar(
        select(models.Meter).where(
            models.Meter.site_id == site_id, models.Meter.meter_no == meter_no
        )
    )
    if meter is None:
        meter = models.Meter(
            site_id=site_id, meter_no=meter_no, meter_kind=kind, device_id=device_id
        )
        db.add(meter)
        db.flush()
    return meter


def _get_or_create_source_record(db: Session, site_id: int, source_type: str, record_no: str):
    sr = db.scalar(
        select(models.SourceRecord).where(
            models.SourceRecord.site_id == site_id,
            models.SourceRecord.source_type == source_type,
            models.SourceRecord.source_record_no == record_no,
        )
    )
    if sr is None:
        sr = models.SourceRecord(
            site_id=site_id, source_type=source_type, source_record_no=record_no
        )
        db.add(sr)
        db.flush()
    return sr


def _is_adopted(db: Session, sr: models.SourceRecord) -> bool:
    for model in (
        models.MeterReading,
        models.ChargingOrder,
        models.Tariff,
        models.PublicLoad,
    ):
        stmt = select(model.id).where(
            model.source_record_id == sr.id,
            model.status == ENTITY_ACTIVE,
            model.adopted_by_job_id.is_not(None),
        )
        if db.scalar(stmt) is not None:
            return True
    return False


def _register_revision(
    db: Session,
    sr: models.SourceRecord,
    payload: dict,
    import_row: models.ImportRow,
) -> tuple[models.RecordRevision, str]:
    """返回 (修订版本, 结果类型)：applied / duplicate / blocked_adopted。"""
    content_hash = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
    if sr.current_revision_id is not None:
        current = db.get(models.RecordRevision, sr.current_revision_id)
        if current.content_hash == content_hash:
            import_row.outcome = REV_DUPLICATE
            return current, REV_DUPLICATE
        # 版本号按该来源记录已有最大版本单调递增；blocked 修订不推进 current 指针，
        # 不能用 current.revision_no+1，否则连续 blocked 会同号，回补链校验将失效。
        max_no = db.scalar(
            select(func.max(models.RecordRevision.revision_no)).where(
                models.RecordRevision.source_record_id == sr.id
            )
        ) or 0
        next_no = max_no + 1
        if _is_adopted(db, sr):
            rev = models.RecordRevision(
                source_record_id=sr.id,
                revision_no=next_no,
                status=REV_BLOCKED_ADOPTED,
                content_hash=content_hash,
                payload_json=canonical(payload),
                import_row_id=import_row.id,
            )
            db.add(rev)
            db.flush()
            import_row.outcome = REV_BLOCKED_ADOPTED
            import_row.note = "原版本已被核算作业采用，修订保留至下一轮"
            return rev, REV_BLOCKED_ADOPTED
    else:
        next_no = 1

    rev = models.RecordRevision(
        source_record_id=sr.id,
        revision_no=next_no,
        status=REV_APPLIED,
        content_hash=content_hash,
        payload_json=canonical(payload),
        import_row_id=import_row.id,
    )
    db.add(rev)
    db.flush()
    sr.current_revision_id = rev.id
    import_row.outcome = REV_APPLIED
    return rev, REV_APPLIED


def _supersede(db: Session, model, source_record_id: int) -> None:
    rows = db.scalars(
        select(model).where(
            model.source_record_id == source_record_id,
            model.status == ENTITY_ACTIVE,
        )
    ).all()
    for row in rows:
        row.status = ENTITY_SUPERSEDED


def _revoke_active(db: Session, model, source_record_id: int) -> None:
    """撤销：同源生效实体标记 revoked（区别于普通修订的 superseded），不新增生效实体。"""
    rows = db.scalars(
        select(model).where(
            model.source_record_id == source_record_id,
            model.status == ENTITY_ACTIVE,
        )
    ).all()
    for row in rows:
        row.status = ENTITY_REVOKED


# ---------- 按来源类型把 payload 落地为实体 ----------

def _apply_reading(db: Session, site: models.Site, payload: dict, sr, rev, kind: str):
    if rev.status != REV_APPLIED:
        return
    _supersede(db, models.MeterReading, sr.id)
    meter = _get_or_create_meter(
        db, site.id, payload["meter_no"], kind, payload.get("device_id")
    )
    ts_utc = parse_local_datetime(payload["reading_time"], site.timezone)
    db.add(
        models.MeterReading(
            site_id=site.id,
            meter_id=meter.id,
            reading_ts=ts_utc,
            reading_ts_local=utc_to_local(ts_utc, site.timezone),
            reading_kwh=d(payload["reading_kwh"]),
            is_correction=b(payload.get("is_correction", "false")),
            status=ENTITY_ACTIVE,
            source_record_id=sr.id,
            revision_id=rev.id,
        )
    )


def _detect_order_conflicts(db: Session, site_id: int, order: models.ChargingOrder) -> None:
    """同设备时段重叠：完全同时段=重复上报（只计一次）；部分重叠=冲突交运营确认。"""
    others = db.scalars(
        select(models.ChargingOrder).where(
            models.ChargingOrder.site_id == site_id,
            models.ChargingOrder.device_id == order.device_id,
            models.ChargingOrder.id != order.id,
            models.ChargingOrder.status == ENTITY_ACTIVE,
            models.ChargingOrder.conflict_id.is_(None),
        )
    ).all()
    for other in others:
        if order.start_ts < other.end_ts and other.start_ts < order.end_ts:
            exact = order.start_ts == other.start_ts and order.end_ts == other.end_ts
            if exact:
                order.status = ENTITY_SUPERSEDED
                db.add(
                    models.ReviewTask(
                        site_id=site_id,
                        task_kind="order_duplicate",
                        title=f"设备 {order.device_id} 重复上报订单",
                        detail=(
                            f"订单 {order.order_no} 与 {other.order_no} 占用完全相同的设备时段，"
                            f"按重复上报只计 {other.order_no} 一次。"
                        ),
                        assignee=models.ROLE_OPERATOR,
                    )
                )
                return
            conflict = models.OrderConflict(
                site_id=site_id,
                device_id=order.device_id,
                order_a_id=other.id,
                order_b_id=order.id,
            )
            db.add(conflict)
            db.flush()
            other.conflict_id = conflict.id
            order.conflict_id = conflict.id
            db.add(
                models.ReviewTask(
                    site_id=site_id,
                    task_kind="order_conflict",
                    title=f"设备 {order.device_id} 订单时段冲突",
                    detail=(
                        f"订单 {other.order_no} 与 {order.order_no} 声称占用同一设备的重叠时段，"
                        f"待运营确认有效订单；确认前两条均不计入核算。"
                    ),
                    assignee=models.ROLE_OPERATOR,
                )
            )


def _apply_order(db: Session, site: models.Site, payload: dict, sr, rev):
    if rev.status != REV_APPLIED:
        return
    _supersede(db, models.ChargingOrder, sr.id)
    start_utc = parse_local_datetime(payload["start_time"], site.timezone)
    end_utc = parse_local_datetime(payload["end_time"], site.timezone)
    if end_utc <= start_utc:
        raise ImportError_(f"订单 {payload.get('order_no')} 结束时间必须晚于开始时间")
    start_reading = payload.get("start_reading")
    end_reading = payload.get("end_reading")
    order = models.ChargingOrder(
        site_id=site.id,
        order_no=payload["order_no"],
        device_id=payload["device_id"],
        start_ts=start_utc,
        end_ts=end_utc,
        start_ts_local=utc_to_local(start_utc, site.timezone),
        end_ts_local=utc_to_local(end_utc, site.timezone),
        start_reading_kwh=d(start_reading) if start_reading not in (None, "") else None,
        end_reading_kwh=d(end_reading) if end_reading not in (None, "") else None,
        reported_energy_kwh=d(payload["reported_energy"]),
        status=ENTITY_ACTIVE,
        source_record_id=sr.id,
        revision_id=rev.id,
    )
    db.add(order)
    db.flush()
    _detect_order_conflicts(db, site.id, order)


def _apply_tariff_payload(db: Session, site: models.Site, payload: dict, sr, rev):
    if rev.status != REV_APPLIED:
        return
    _supersede(db, models.Tariff, sr.id)
    rows = sorted(payload["segments"], key=lambda r: int(r["start_hour"]))
    head = rows[0]
    tariff = models.Tariff(
        site_id=site.id,
        name=head["tariff_name"],
        effective_date=datetime.strptime(head["effective_date"], "%Y-%m-%d").date(),
        end_date=(
            datetime.strptime(head["end_date"], "%Y-%m-%d").date()
            if head.get("end_date")
            else None
        ),
        status=ENTITY_ACTIVE,
        source_record_id=sr.id,
        revision_id=rev.id,
    )
    for r in rows:
        tariff.segments.append(
            models.TariffSegment(
                segment_name=r["segment_name"],
                start_hour=int(r["start_hour"]),
                end_hour=int(r["end_hour"]),
                price_per_kwh=d(r["price"]),
            )
        )
    db.add(tariff)


def _apply_public_load(db: Session, site: models.Site, payload: dict, sr, rev):
    if rev.status != REV_APPLIED:
        return
    _supersede(db, models.PublicLoad, sr.id)
    db.add(
        models.PublicLoad(
            site_id=site.id,
            load_name=payload["load_name"],
            start_ts=parse_local_datetime(payload["start_time"], site.timezone),
            end_ts=parse_local_datetime(payload["end_time"], site.timezone),
            energy_kwh=d(payload["energy_kwh"]),
            confirmed=b(payload.get("confirmed", "false")),
            status=ENTITY_ACTIVE,
            source_record_id=sr.id,
            revision_id=rev.id,
        )
    )


def _apply_replacement(db: Session, site: models.Site, payload: dict, sr, rev):
    if rev.status != REV_APPLIED:
        return
    meter = _get_or_create_meter(db, site.id, payload["meter_no"], "device", None)
    old = payload.get("old_final_reading")
    new = payload.get("new_initial_reading")
    db.add(
        models.DeviceReplacement(
            site_id=site.id,
            meter_id=meter.id,
            effective_ts=parse_local_datetime(payload["effective_time"], site.timezone),
            old_final_reading=d(old) if old not in (None, "") else None,
            new_initial_reading=d(new) if new not in (None, "") else None,
            evidence_note=payload.get("evidence_note", ""),
            source_record_id=sr.id,
        )
    )


def apply_payload(db: Session, site: models.Site, source_type: str, payload: dict, sr, rev) -> None:
    # 撤销类修订：不落地新生效实体，只把同源生效行标记 revoked
    if is_revocation_payload(payload):
        model_map = {
            "master_reading": models.MeterReading,
            "device_reading": models.MeterReading,
            "order": models.ChargingOrder,
            "public_load": models.PublicLoad,
            "tariff": models.Tariff,
        }
        model = model_map.get(source_type)
        if model is None:
            raise ImportError_(f"来源类型 {source_type} 不支持撤销")
        _revoke_active(db, model, sr.id)
        return
    if source_type == "master_reading":
        _apply_reading(db, site, payload, sr, rev, "master")
    elif source_type == "device_reading":
        _apply_reading(db, site, payload, sr, rev, "device")
    elif source_type == "order":
        _apply_order(db, site, payload, sr, rev)
    elif source_type == "public_load":
        _apply_public_load(db, site, payload, sr, rev)
    elif source_type == "replacement":
        _apply_replacement(db, site, payload, sr, rev)
    elif source_type == "tariff":
        _apply_tariff_payload(db, site, payload, sr, rev)
    else:
        raise ImportError_(f"未知来源类型 {source_type}")


def promote_blocked_revisions(db: Session, site: models.Site) -> int:
    """新一轮核算开始前，把上轮被阻塞的修订提升为生效版本（沿用相同来源记录号）。

    已被跨月回补批次处理（backfilled）的修订不再提升，避免与回补调整重复计入。
    """
    blocked = db.scalars(
        select(models.RecordRevision)
        .join(models.SourceRecord, models.SourceRecord.id == models.RecordRevision.source_record_id)
        .where(
            models.SourceRecord.site_id == site.id,
            models.RecordRevision.status == REV_BLOCKED_ADOPTED,
        )
    ).all()
    promoted = 0
    for rev in blocked:
        sr = db.get(models.SourceRecord, rev.source_record_id)
        payload = json.loads(rev.payload_json)
        rev.status = REV_APPLIED
        sr.current_revision_id = rev.id
        import_row = db.get(models.ImportRow, rev.import_row_id)
        import_row.outcome = REV_APPLIED
        import_row.note = "修订已在新一轮核算前提升生效"
        apply_payload(db, site, sr.source_type, payload, sr, rev)
        promoted += 1
    if promoted:
        db.flush()
    return promoted


# ---------- 主入口 ----------

SPECS = {
    "master_reading": {
        "required": {"record_no", "meter_no", "reading_time", "reading_kwh"}
    },
    "device_reading": {
        "required": {"record_no", "meter_no", "reading_time", "reading_kwh"}
    },
    "order": {
        "required": {"record_no", "order_no", "device_id", "start_time", "end_time", "reported_energy"}
    },
    "public_load": {
        "required": {"record_no", "load_name", "start_time", "end_time", "energy_kwh"}
    },
    "replacement": {
        "required": {"record_no", "meter_no", "effective_time", "evidence_note"}
    },
    "tariff": {
        "required": {"record_no", "tariff_name", "effective_date", "segment_name",
                     "start_hour", "end_hour", "price"}
    },
}


def import_file(
    db: Session,
    site: models.Site,
    source_type: str,
    filename: str,
    content: bytes,
    username: str,
    storage_dir: str,
) -> dict:
    if source_type not in SPECS:
        raise ImportError_(f"未知来源类型 {source_type}")
    os.makedirs(storage_dir, exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    missing = SPECS[source_type]["required"] - set(reader.fieldnames or [])
    if missing:
        raise ImportError_(f"缺少必需列: {sorted(missing)}")

    stored = Path(storage_dir) / f"{sha[:12]}_{os.path.basename(filename)}"
    stored.write_bytes(content)

    imp = models.ImportFile(
        site_id=site.id,
        source_type=source_type,
        original_name=filename,
        storage_path=str(stored),
        sha256=sha,
        imported_by=username,
    )
    db.add(imp)
    db.flush()

    raw_lines = text.splitlines()
    results: list[RowResult] = []
    tariff_groups: dict[str, dict] = {}
    tariff_row_ids: dict[str, list[int]] = {}

    for idx, row in enumerate(reader):
        line_no = idx + 2  # 物理行号：表头占第 1 行
        raw = raw_lines[line_no - 1] if line_no - 1 < len(raw_lines) else ""
        record_no = (row.get("record_no") or "").strip()
        ir = models.ImportRow(
            import_file_id=imp.id,
            line_no=line_no,
            raw_line=raw,
            outcome="rejected",
            source_record_no=record_no or None,
        )
        db.add(ir)
        db.flush()
        clean = augment_chain_fields(
            {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        )
        try:
            if not record_no:
                raise ImportError_("缺少 record_no")
            if source_type == "tariff":
                group = augment_chain_fields(clean)
                tariff_groups.setdefault(record_no, {"segments": [],
                                                     "_is_revocation": group["_is_revocation"],
                                                     "_replaces_revision_no": group["_replaces_revision_no"]})["segments"].append(clean)
                tariff_row_ids.setdefault(record_no, []).append(ir.id)
                continue
            # 逐行 SAVEPOINT：业务处理失败只回滚本行
            try:
                with db.begin_nested():
                    sr = _get_or_create_source_record(db, site.id, source_type, record_no)
                    rev, result = _register_revision(db, sr, clean, ir)
                    if result != REV_DUPLICATE and result != REV_BLOCKED_ADOPTED:
                        apply_payload(db, site, source_type, clean, sr, rev)
            except Exception as exc:
                if isinstance(exc, ImportError_):
                    raise
                raise ImportError_(str(exc)) from exc
            results.append(RowResult(line_no, record_no, ir.outcome, ir.note))
        except Exception as exc:
            ir.outcome = "rejected"
            ir.note = str(exc)[:240]
            results.append(RowResult(line_no, record_no, "rejected", str(exc)[:240]))

    if source_type == "tariff":
        for record_no, grouped in tariff_groups.items():
            row_ids = tariff_row_ids[record_no]
            first_row = db.get(models.ImportRow, row_ids[0])
            try:
                with db.begin_nested():
                    sr = _get_or_create_source_record(db, site.id, "tariff", record_no)
                    rev, result = _register_revision(db, sr, grouped, first_row)
                    if result == REV_DUPLICATE:
                        for rid in row_ids:
                            db.get(models.ImportRow, rid).outcome = REV_DUPLICATE
                    elif result == REV_BLOCKED_ADOPTED:
                        for rid in row_ids:
                            db.get(models.ImportRow, rid).outcome = REV_BLOCKED_ADOPTED
                    else:
                        _apply_tariff_payload(db, site, grouped, sr, rev)
                        for rid in row_ids[1:]:
                            db.get(models.ImportRow, rid).outcome = REV_APPLIED
                outcome = first_row.outcome
                for rid in row_ids:
                    r = db.get(models.ImportRow, rid)
                    results.append(RowResult(r.line_no, record_no, r.outcome, r.note))
            except Exception as exc:
                for rid in row_ids:
                    r = db.get(models.ImportRow, rid)
                    r.outcome = "rejected"
                    r.note = str(exc)[:240]
                    results.append(RowResult(r.line_no, record_no, "rejected", str(exc)[:240]))

    imp.row_count = len(results)
    db.flush()
    summary: dict[str, int] = {}
    for r in results:
        summary[r.outcome] = summary.get(r.outcome, 0) + 1
    return {"import_file_id": imp.id, "row_count": len(results), "summary": summary}
