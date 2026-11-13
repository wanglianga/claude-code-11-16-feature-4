"""时间与精度工具：所有时间先转到站点时区，存储统一用 UTC。"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytz


def local_to_utc(dt_naive: datetime, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    if dt_naive.tzinfo is not None:
        dt_naive = dt_naive.replace(tzinfo=None)
    return tz.localize(dt_naive).astimezone(pytz.utc).replace(tzinfo=None)


def utc_to_local(dt_utc: datetime, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=pytz.utc)
    return dt_utc.astimezone(tz).replace(tzinfo=None)


def parse_local_datetime(text: str, tz_name: str) -> datetime:
    text = text.strip()
    dt = datetime.fromisoformat(text)
    return local_to_utc(dt, tz_name)


def quantize(value: Decimal, scale: int) -> Decimal:
    q = Decimal(1).scaleb(-scale)
    return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


def money(value: Decimal, scale: int = 4) -> Decimal:
    return quantize(value, scale)


def display_money(value: Decimal, scale: int = 2) -> Decimal:
    return quantize(value, scale)


def overlap_seconds(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> float:
    lo = max(start_a, start_b)
    hi = min(end_a, end_b)
    return max(0.0, (hi - lo).total_seconds())


def duration_seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def merge_boundaries(
    start: datetime,
    end: datetime,
    boundaries: list[datetime],
) -> list[tuple[datetime, datetime]]:
    """以给定边界点（如电价生效/切换时刻）把 [start, end) 切成连续子区间。"""
    cuts = sorted({b for b in boundaries if start < b < end})
    points = [start, *cuts, end]
    return [(points[i], points[i + 1]) for i in range(len(points) - 1) if points[i] < points[i + 1]]
