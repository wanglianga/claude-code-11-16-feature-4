from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    display_name: str


class SiteOut(ORMModel):
    id: int
    code: str
    name: str
    timezone: str
    diff_threshold_pct: Decimal


class ImportResultOut(BaseModel):
    import_file_id: int
    row_count: int
    summary: dict[str, int]


class ImportFileOut(ORMModel):
    id: int
    site_id: int
    source_type: str
    original_name: str
    sha256: str
    row_count: int
    imported_by: str
    imported_at: datetime


class ImportRowOut(ORMModel):
    id: int
    line_no: int
    raw_line: str
    outcome: str
    source_record_no: str | None
    note: str | None


class JobIn(BaseModel):
    site_id: int
    cycle_start: datetime  # 站点本地时间
    cycle_end: datetime
    rerun_of_job_id: int | None = None


class JobOut(ORMModel):
    id: int
    site_id: int
    cycle_start_local: datetime
    cycle_end_local: datetime
    status: str
    threshold_pct: Decimal
    rerun_of_job_id: int | None
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class IntervalOut(ORMModel):
    id: int
    start_ts_local: datetime
    end_ts_local: datetime
    master_reading_start: Decimal | None
    master_reading_end: Decimal | None
    master_energy_kwh: Decimal | None
    device_meter_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    public_kwh: Decimal
    unexplained_kwh: Decimal
    order_meter_diff_kwh: Decimal
    basis_status: str
    note: str | None


class SplitOut(ORMModel):
    id: int
    order_id: int
    start_ts_local: datetime
    end_ts_local: datetime
    segment_name: str
    price_per_kwh: Decimal
    energy_kwh: Decimal | None
    amount: Decimal | None
    basis_status: str
    note: str | None


class BillOut(ORMModel):
    id: int
    bill_no: str
    site_id: int
    job_id: int
    master_total_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    pending_kwh: Decimal
    pending_order_count: int
    public_kwh: Decimal
    unexplained_kwh: Decimal
    suspended_kwh: Decimal
    charging_amount: Decimal
    public_amount: Decimal
    status: str
    confirmed_at: datetime | None
    confirmed_by: str | None


class AdjustmentIn(BaseModel):
    component: str
    corrected_value: Decimal
    reason: str


class AdjustmentOut(ORMModel):
    id: int
    bill_id: int
    source_job_id: int
    component: str
    original_value: Decimal
    corrected_value: Decimal
    delta: Decimal
    reason: str
    created_by: str
    created_at: datetime


class TaskOut(ORMModel):
    id: int
    site_id: int
    job_id: int | None
    task_kind: str
    title: str
    detail: str
    assignee: str
    status: str
    response: str | None
    responded_by: str | None
    responded_at: datetime | None


class TaskRespondIn(BaseModel):
    response: str


class ConflictOut(ORMModel):
    id: int
    site_id: int
    device_id: str
    order_a_id: int
    order_b_id: int
    status: str
    resolved_order_id: int | None
    resolution_note: str | None


class ConflictResolveIn(BaseModel):
    resolved_order_id: int
    resolution_note: str


class RevisionOut(ORMModel):
    id: int
    source_record_id: int
    revision_no: int
    status: str
    content_hash: str
    created_at: datetime


class SourceRecordOut(ORMModel):
    id: int
    source_type: str
    source_record_no: str
    current_revision_id: int | None
    revisions: list[RevisionOut] = []


# ---------------- 跨月回补 ----------------

class BackfillBatchCreateIn(BaseModel):
    site_id: int
    reason_note: str | None = None


class BackfillCandidateOut(BaseModel):
    source_record_id: int
    source_type: str
    source_record_no: str
    revision_id: int
    revision_no: int
    change_kind: str


class BackfillBatchOut(ORMModel):
    id: int
    site_id: int
    batch_no: str
    status: str
    reason_note: str | None
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None
    segment_total: int
    segment_done: int
    segment_suspended: int
    segment_failed: int


class BackfillSegmentOut(ORMModel):
    id: int
    batch_id: int
    bill_id: int
    job_id: int
    reconciliation_interval_id: int
    start_ts_local: datetime
    end_ts_local: datetime
    status: str
    attempts: int
    last_error: str | None
    suspended_reason: str | None
    processed_at: datetime | None


class BackfillItemOut(ORMModel):
    id: int
    batch_id: int
    segment_id: int
    site_id: int
    bill_id: int
    source_job_id: int
    source_record_id: int
    from_revision_id: int | None
    to_revision_id: int | None
    change_kind: str
    reason_kind: str
    component: str
    segment_name: str | None
    price_before: Decimal | None
    price_after: Decimal | None
    energy_before: Decimal
    energy_after: Decimal
    energy_delta: Decimal
    amount_before: Decimal
    amount_after: Decimal
    amount_delta: Decimal
    rounding_delta: Decimal
    basis_before: str | None
    basis_after: str | None
    detail_json: str
    reason_text: str
    status: str
    suspended_reason: str | None
    created_by: str
    created_at: datetime
    property_reviewed_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by: str | None
    rejected_at: datetime | None
    rejected_by: str | None
    reject_note: str | None


class BackfillReviewIn(BaseModel):
    action: str            # property: acknowledge/reject；finance: confirm
    comment: str | None = None


class BackfillReviewOut(ORMModel):
    id: int
    item_id: int
    review_party: str
    action: str
    comment: str | None
    reviewer: str
    created_at: datetime


class BackfillLinkIn(BaseModel):
    settlement_bill_id: int


class BackfillLinkOut(ORMModel):
    id: int
    site_id: int
    batch_id: int
    item_id: int
    original_bill_id: int
    settlement_bill_id: int | None
    linked_amount: Decimal
    status: str
    note: str | None
    created_at: datetime


class BackfillPayableOut(BaseModel):
    bill_id: int
    bill_no: str
    original_charging_amount: Decimal
    original_public_amount: Decimal
    manual_adjustment_delta: Decimal
    backfill_confirmed_delta: Decimal
    current_payable_amount: Decimal
    backfill_item_total: int
    backfill_confirmed_count: int
    backfill_pending_count: int
    backfill_rejected_count: int
    note: str
