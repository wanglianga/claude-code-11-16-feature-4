from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, get_current_user
from .. import models

router = APIRouter(prefix="/api/entities", tags=["entities"])


@router.get("/readings")
def list_readings(site_id: int, meter_no: str | None = None,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    stmt = (
        select(models.MeterReading, models.Meter)
        .join(models.Meter, models.Meter.id == models.MeterReading.meter_id)
        .where(models.MeterReading.site_id == site_id)
        .order_by(models.MeterReading.reading_ts)
    )
    if meter_no:
        stmt = stmt.where(models.Meter.meter_no == meter_no)
    rows = db.execute(stmt).all()
    return [
        {
            "id": r.id,
            "meter_no": m.meter_no,
            "meter_kind": m.meter_kind,
            "device_id": m.device_id,
            "reading_ts_local": r.reading_ts_local.isoformat(),
            "reading_kwh": float(r.reading_kwh),
            "status": r.status,
            "is_correction": r.is_correction,
            "adopted_by_job_id": r.adopted_by_job_id,
            "source_record_id": r.source_record_id,
            "revision_id": r.revision_id,
        }
        for r, m in rows
    ]


@router.get("/orders")
def list_orders(site_id: int, device_id: str | None = None,
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    stmt = select(models.ChargingOrder).where(
        models.ChargingOrder.site_id == site_id
    ).order_by(models.ChargingOrder.start_ts)
    if device_id:
        stmt = stmt.where(models.ChargingOrder.device_id == device_id)
    orders = db.scalars(stmt).all()
    return [
        {
            "id": o.id,
            "order_no": o.order_no,
            "device_id": o.device_id,
            "start_ts_local": o.start_ts_local.isoformat(),
            "end_ts_local": o.end_ts_local.isoformat(),
            "start_reading_kwh": float(o.start_reading_kwh) if o.start_reading_kwh is not None else None,
            "end_reading_kwh": float(o.end_reading_kwh) if o.end_reading_kwh is not None else None,
            "reported_energy_kwh": float(o.reported_energy_kwh),
            "status": o.status,
            "conflict_id": o.conflict_id,
            "adopted_by_job_id": o.adopted_by_job_id,
        }
        for o in orders
    ]


@router.get("/public-loads")
def list_public_loads(site_id: int, db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    site = db.get(models.Site, site_id)
    loads = db.scalars(
        select(models.PublicLoad)
        .where(models.PublicLoad.site_id == site_id)
        .order_by(models.PublicLoad.start_ts)
    ).all()
    from ..services.timeutil import utc_to_local
    return [
        {
            "id": p.id,
            "load_name": p.load_name,
            "start_ts_local": utc_to_local(p.start_ts, site.timezone).isoformat(),
            "end_ts_local": utc_to_local(p.end_ts, site.timezone).isoformat(),
            "energy_kwh": float(p.energy_kwh),
            "confirmed": p.confirmed,
            "status": p.status,
            "adopted_by_job_id": p.adopted_by_job_id,
        }
        for p in loads
    ]


@router.get("/replacements")
def list_replacements(site_id: int, db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    rows = db.execute(
        select(models.DeviceReplacement, models.Meter)
        .join(models.Meter, models.Meter.id == models.DeviceReplacement.meter_id)
        .where(models.DeviceReplacement.site_id == site_id)
        .order_by(models.DeviceReplacement.effective_ts)
    ).all()
    return [
        {
            "id": r.id,
            "meter_no": m.meter_no,
            "effective_ts": r.effective_ts.isoformat(),
            "old_final_reading": float(r.old_final_reading) if r.old_final_reading is not None else None,
            "new_initial_reading": float(r.new_initial_reading) if r.new_initial_reading is not None else None,
            "evidence_note": r.evidence_note,
        }
        for r, m in rows
    ]
