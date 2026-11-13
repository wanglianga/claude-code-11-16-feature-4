"""分时电价查询与边界生成（站点本地时区 -> UTC 边界）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytz
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


class TariffError(Exception):
    pass


def localize(dt_naive: datetime, tz_name: str):
    return pytz.timezone(tz_name).localize(dt_naive)


def tariff_for_local_date(db: Session, site_id: int, local_day,
                          tariff_ids: set[int] | None = None) -> models.Tariff:
    stmt = select(models.Tariff).where(
        models.Tariff.site_id == site_id,
        models.Tariff.status == models.ENTITY_ACTIVE,
        models.Tariff.effective_date <= local_day,
    )
    if tariff_ids is not None:
        # 作业冻结输入：只允许使用该作业采用的电价版本
        stmt = stmt.where(models.Tariff.id.in_(tariff_ids))
    tariff = db.scalar(stmt.order_by(models.Tariff.effective_date.desc()))
    if tariff is None:
        raise TariffError(f"站点 {site_id} 在 {local_day} 无生效电价")
    if tariff.end_date is not None and local_day >= tariff.end_date:
        raise TariffError(f"站点 {site_id} 在 {local_day} 电价已失效且无新版本")
    return tariff


def segment_for_hour(tariff: models.Tariff, hour: int) -> models.TariffSegment:
    for seg in tariff.segments:
        end_hour = seg.end_hour if seg.end_hour > seg.start_hour else seg.end_hour + 24
        if seg.start_hour <= hour < end_hour:
            return seg
    raise TariffError(f"电价 {tariff.name} 未覆盖小时 {hour}")


def price_at_utc(db: Session, site_id: int, tz_name: str, ts_utc: datetime,
                 tariff_ids: set[int] | None = None) -> tuple[str, Decimal]:
    """返回 (时段名, 单价)。"""
    from .timeutil import utc_to_local

    local_dt = utc_to_local(ts_utc, tz_name)
    tariff = tariff_for_local_date(db, site_id, local_dt.date(), tariff_ids)
    seg = segment_for_hour(tariff, local_dt.hour)
    return seg.segment_name, seg.price_per_kwh


def _local_midnight_utc(local_day, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return tz.localize(datetime(local_day.year, local_day.month, local_day.day)).astimezone(
        pytz.utc
    ).replace(tzinfo=None)


def tariff_boundaries(db: Session, site_id: int, tz_name: str, start: datetime,
                      end: datetime, tariff_ids: set[int] | None = None) -> list[datetime]:
    """生成 [start, end) 内全部电价边界（UTC）：本地午夜、分时切换点、电价版本生效日。"""
    from .timeutil import utc_to_local

    boundaries: set[datetime] = set()
    local_start = utc_to_local(start, tz_name)
    local_end = utc_to_local(end, tz_name)
    day = local_start.date()
    last_day = local_end.date()
    tariff_stmt = select(models.Tariff).where(
        models.Tariff.site_id == site_id, models.Tariff.status == models.ENTITY_ACTIVE
    )
    if tariff_ids is not None:
        tariff_stmt = tariff_stmt.where(models.Tariff.id.in_(tariff_ids))
    tariffs = db.scalars(tariff_stmt).all()
    version_dates = {t.effective_date for t in tariffs}
    if any(t.end_date for t in tariffs):
        version_dates |= {t.end_date for t in tariffs if t.end_date}

    while day <= last_day:
        midnight_utc = _local_midnight_utc(day, tz_name)
        if start < midnight_utc < end:
            boundaries.add(midnight_utc)
        if day in version_dates and start < midnight_utc < end:
            boundaries.add(midnight_utc)
        tariff = tariff_for_local_date(db, site_id, day, tariff_ids)
        for seg in tariff.segments:
            local_cut = datetime(day.year, day.month, day.day, seg.start_hour)
            cut_utc = localize(local_cut, tz_name).astimezone(pytz.utc).replace(tzinfo=None)
            if start < cut_utc < end:
                boundaries.add(cut_utc)
        day += timedelta(days=1)
    return sorted(boundaries)
