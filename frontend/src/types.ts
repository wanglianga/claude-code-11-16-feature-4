export interface Site {
  id: number;
  code: string;
  name: string;
  timezone: string;
  diff_threshold_pct: string;
}

export interface Bill {
  id: number;
  bill_no: string;
  site_id: number;
  job_id: number;
  master_total_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  pending_kwh: string;
  pending_order_count: number;
  public_kwh: string;
  unexplained_kwh: string;
  suspended_kwh: string;
  charging_amount: string;
  public_amount: string;
  status: string;
  confirmed_at: string | null;
  confirmed_by: string | null;
}

export interface Job {
  id: number;
  site_id: number;
  cycle_start_local: string;
  cycle_end_local: string;
  status: string;
  threshold_pct: string;
  rerun_of_job_id: number | null;
  message: string | null;
}

export interface Interval {
  id: number;
  start_ts_local: string;
  end_ts_local: string;
  master_reading_start: string | null;
  master_reading_end: string | null;
  master_energy_kwh: string | null;
  device_meter_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  public_kwh: string;
  unexplained_kwh: string;
  order_meter_diff_kwh: string;
  basis_status: string;
  note: string | null;
}

export interface Split {
  id: number;
  order_id: number;
  start_ts_local: string;
  end_ts_local: string;
  segment_name: string;
  price_per_kwh: string;
  energy_kwh: string | null;
  amount: string | null;
  basis_status: string;
  note: string | null;
}

export interface ReviewTask {
  id: number;
  site_id: number;
  job_id: number | null;
  task_kind: string;
  title: string;
  detail: string;
  assignee: string;
  status: string;
  response: string | null;
  responded_by: string | null;
}

export interface Conflict {
  id: number;
  site_id: number;
  device_id: string;
  order_a_id: number;
  order_b_id: number;
  status: string;
  resolved_order_id: number | null;
  resolution_note: string | null;
}

export interface ImportFile {
  id: number;
  site_id: number;
  source_type: string;
  original_name: string;
  sha256: string;
  row_count: number;
  imported_by: string;
  imported_at: string;
}

export interface ImportRow {
  id: number;
  line_no: number;
  raw_line: string;
  outcome: string;
  source_record_no: string | null;
  note: string | null;
}

export interface Adjustment {
  id: number;
  component: string;
  original_value: string;
  corrected_value: string;
  delta: string;
  reason: string;
  created_by: string;
  created_at: string;
}

export interface BackfillBatch {
  id: number;
  site_id: number;
  batch_no: string;
  status: string;
  reason_note: string | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
  segment_total: number;
  segment_done: number;
  segment_suspended: number;
  segment_failed: number;
}

export interface BackfillSegment {
  id: number;
  batch_id: number;
  bill_id: number;
  job_id: number;
  reconciliation_interval_id: number;
  start_ts_local: string;
  end_ts_local: string;
  status: string;
  attempts: number;
  last_error: string | null;
  suspended_reason: string | null;
  processed_at: string | null;
}

export interface BackfillItem {
  id: number;
  batch_id: number;
  segment_id: number;
  site_id: number;
  bill_id: number;
  source_job_id: number;
  source_record_id: number;
  from_revision_id: number | null;
  to_revision_id: number | null;
  change_kind: string;
  reason_kind: string;
  component: string;
  segment_name: string | null;
  price_before: string | null;
  price_after: string | null;
  energy_before: string;
  energy_after: string;
  energy_delta: string;
  amount_before: string;
  amount_after: string;
  amount_delta: string;
  rounding_delta: string;
  basis_before: string | null;
  basis_after: string | null;
  detail_json: string;
  reason_text: string;
  status: string;
  suspended_reason: string | null;
  created_by: string;
  created_at: string;
  property_reviewed_at: string | null;
  confirmed_at: string | null;
  confirmed_by: string | null;
  rejected_at: string | null;
  rejected_by: string | null;
  reject_note: string | null;
}

export interface BackfillReview {
  id: number;
  item_id: number;
  review_party: string;
  action: string;
  comment: string | null;
  reviewer: string;
  created_at: string;
}

export interface PayableView {
  bill_id: number;
  bill_no: string;
  original_charging_amount: string;
  original_public_amount: string;
  manual_adjustment_delta: string;
  backfill_confirmed_delta: string;
  current_payable_amount: string;
  backfill_item_total: number;
  backfill_confirmed_count: number;
  backfill_pending_count: number;
  backfill_rejected_count: number;
  note: string;
}
