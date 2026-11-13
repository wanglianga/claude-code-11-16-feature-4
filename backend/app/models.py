"""SQLAlchemy 模型：原件索引 / 来源记录修订 / 计算依据 / 结算修订 分层存储。"""
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# ---- 枚举（字符串常量，避免方言枚举迁移问题）----
ROLE_FINANCE = "finance"          # 平台财务：全部站点、账单确认
ROLE_OPERATOR = "operator"        # 运营：冲突/换表确认
ROLE_PROPERTY = "property"        # 物业：仅分配给自己站点的核对事项

REV_DUPLICATE = "duplicate"            # 同号同内容
REV_APPLIED = "applied"                # 同号内容变化，已生效（原数据未被账单采用）
REV_BLOCKED_ADOPTED = "blocked_adopted"  # 同号内容变化但原数据已被采用，留待下一轮
REV_BACKFILLED = "backfilled"          # 已由跨月回补批次处理，不再提升为当期生效实体

ENTITY_ACTIVE = "active"
ENTITY_SUPERSEDED = "superseded"
ENTITY_PENDING = "pending_data"    # 缺起止读数等
ENTITY_SUSPENDED = "suspended"     # 回退无依据等原因挂起
ENTITY_REVOKED = "revoked"         # 迟到的撤销：重复记录/误报被撤回，原贡献需在回补中撤出

JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_SUSPENDED = "suspended"
JOB_FAILED = "failed"

BASIS_MEASURED = "measured"        # 读数差值实测
BASIS_ESTIMATED = "estimated"      # 跨时段无中间计量，明确标注的估算
BASIS_PENDING = "pending"          # 缺起止读数，待补
BASIS_SUSPENDED = "suspended"      # 回退无依据，整段挂起

BILL_DRAFT = "draft"
BILL_SUSPENDED = "suspended"
BILL_CONFIRMED = "confirmed"
BILL_ADJUSTED = "adjusted"

TASK_OPEN = "open"
TASK_SUBMITTED = "submitted"
TASK_RESOLVED = "resolved"

CONFLICT_OPEN = "open"
CONFLICT_RESOLVED = "resolved"

# ---- 跨月回补（backfill）状态 ----
BF_BATCH_PENDING = "pending"        # 已建批次，尚未执行
BF_BATCH_RUNNING = "running"        # 分段执行中，进度按区间落库，失败可续算
BF_BATCH_SUSPENDED = "suspended"    # 存在挂起分段（缺中间修订版本等），补齐后续算
BF_BATCH_COMPLETED = "completed"    # 全部分段处理完成
BF_BATCH_FAILED = "failed"          # 执行异常，已完成分段不重算

BF_SEG_PENDING = "pending"
BF_SEG_RUNNING = "running"
BF_SEG_DONE = "done"
BF_SEG_SUSPENDED = "suspended"      # 同源修订未收齐（缺中间版本），挂起等待
BF_SEG_FAILED = "failed"
BF_SEG_SKIPPED = "skipped"          # 该区间无任何更正贡献，无需重算

BF_ADJ_PENDING = "pending_confirmation"   # 待双方复核确认
BF_ADJ_PROPERTY = "property_reviewed"     # 物业已复核
BF_ADJ_CONFIRMED = "confirmed"            # 财务确认，差额进入后续结算
BF_ADJ_REJECTED = "rejected"              # 物业否认该段依据，仅本调整退回
BF_ADJ_SUSPENDED = "suspended"            # 修订链缺版本挂起，未产生可结算金额

BF_REVIEW_PROPERTY = "property"
BF_REVIEW_FINANCE = "finance"

# 回补调整项涉及的成分（与账单字段对齐，另加 master 总表与 rounding 舍入说明）
BF_COMP_MASTER = "master_total_kwh"
BF_COMP_CHARGING_KWH = "charging_kwh"
BF_COMP_ESTIMATED_KWH = "estimated_kwh"
BF_COMP_PUBLIC_KWH = "public_kwh"
BF_COMP_UNEXPLAINED_KWH = "unexplained_kwh"
BF_COMP_CHARGING_AMOUNT = "charging_amount"
BF_COMP_PUBLIC_AMOUNT = "public_amount"
BF_COMP_ROUNDING = "rounding"          # 纯舍入变化（电量不变、仅金额尾差）
BF_COMP_TARIFF = "tariff_price"        # 电价修正引起的金额变化

# 更正原因类别
BF_REASON_LATE_ORDER = "late_order"            # 迟到订单
BF_REASON_CORRECTED_READING = "corrected_reading"  # 被纠正的表读数
BF_REASON_DUPLICATE_REVOKED = "duplicate_revoked"  # 撤销的重复记录
BF_REASON_TARIFF_CORRECTION = "tariff_correction"  # 电价更正
BF_REASON_PUBLIC_CORRECTION = "public_correction"  # 公共负荷更正


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA 时区，如 Asia/Shanghai
    diff_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.02"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sites: Mapped[list["UserSite"]] = relationship(back_populates="user")


class UserSite(Base):
    """物业账号与站点的授权范围。"""
    __tablename__ = "user_sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    user: Mapped[User] = relationship(back_populates="sites")
    __table_args__ = (UniqueConstraint("user_id", "site_id"),)


class Meter(Base):
    __tablename__ = "meters"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_no: Mapped[str] = mapped_column(String(64))
    meter_kind: Mapped[str] = mapped_column(String(16))  # master=物业总表 / device=设备表
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_cumulative: Mapped[bool] = mapped_column(Boolean, default=True)  # 累计/增量
    __table_args__ = (UniqueConstraint("site_id", "meter_no"),)


class ImportFile(Base):
    """原件索引：原始文件本身落盘并留痕。"""
    __tablename__ = "import_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    original_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_by: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportRow(Base):
    """原件逐行留存：文件 id + 行号 + 原始行文本 + 处理结果。"""
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_file_id: Mapped[int] = mapped_column(ForeignKey("import_files.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    raw_line: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32))  # inserted/revised/duplicate/blocked_adopted/rejected
    source_record_no: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SourceRecord(Base):
    """按“来源+来源记录号+站点”识别同一条业务记录的所有修订版本。"""
    __tablename__ = "source_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_record_no: Mapped[str] = mapped_column(String(96))
    current_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("record_revisions.id", use_alter=True, name="fk_source_current_revision"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revisions: Mapped[list["RecordRevision"]] = relationship(
        order_by="RecordRevision.revision_no",
        foreign_keys="RecordRevision.source_record_id",
    )
    __table_args__ = (UniqueConstraint("site_id", "source_type", "source_record_no"),)


class RecordRevision(Base):
    __tablename__ = "record_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))  # applied / duplicate / blocked_adopted / backfilled
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    import_row_id: Mapped[int] = mapped_column(ForeignKey("import_rows.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("source_record_id", "revision_no",
                                       name="uq_revision_source_no"),)


class DeviceReplacement(Base):
    """设备/电表更换依据；回退处理必须先查到本记录。"""
    __tablename__ = "device_replacements"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    effective_ts: Mapped[datetime] = mapped_column(DateTime)  # UTC
    old_final_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    new_initial_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    evidence_note: Mapped[str] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MeterReading(Base):
    """设备/总表累计读数；每次修订产生新行，旧行不可变、不被覆盖。"""
    __tablename__ = "meter_readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    reading_ts: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    reading_ts_local: Mapped[datetime] = mapped_column(DateTime)
    reading_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_reading_meter_ts", "meter_id", "reading_ts"),)


class ChargingOrder(Base):
    __tablename__ = "charging_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(96), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)       # UTC
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    start_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    end_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reported_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))  # 设备上报电量，仅作比对
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    conflict_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_conflicts.id", use_alter=True, name="fk_order_conflict"),
        nullable=True,
    )
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    effective_date: Mapped[date] = mapped_column(Date)   # 站点本地日期起生效
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    segments: Mapped[list["TariffSegment"]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan", order_by="TariffSegment.start_hour"
    )


class TariffSegment(Base):
    """分时段：本地时间 start_hour:00 起、到 end_hour:00（不含），价格 price_per_kwh。"""
    __tablename__ = "tariff_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), index=True)
    segment_name: Mapped[str] = mapped_column(String(32))  # peak/flat/valley
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    tariff: Mapped[Tariff] = relationship(back_populates="segments")


class PublicLoad(Base):
    """经确认的公共负荷（如公共照明）；未经确认不得计入。"""
    __tablename__ = "public_loads"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    load_name: Mapped[str] = mapped_column(String(128))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReconciliationJob(Base):
    """核算作业：冻结所用读数、电价、订单与分摊规则。"""
    __tablename__ = "reconciliation_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    cycle_start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    cycle_end: Mapped[datetime] = mapped_column(DateTime)
    cycle_start_local: Mapped[datetime] = mapped_column(DateTime)
    cycle_end_local: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=JOB_RUNNING)
    threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    rerun_of_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(16), default="1.0")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill: Mapped["Bill | None"] = relationship(back_populates="job", uselist=False)


class JobInput(Base):
    """作业输入快照：重跑沿用相同输入，执行期间的更正不进入本轮。"""
    __tablename__ = "job_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    input_kind: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    entity_id: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (UniqueConstraint("job_id", "input_kind", "entity_id"),)


class ReconciliationInterval(Base):
    """计算依据：总表计量区间内各成分电量，财务可逐段下钻。"""
    __tablename__ = "reconciliation_intervals"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    master_reading_start: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_reading_end: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    device_meter_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))       # 实测订单电量
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))      # 标注估算
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))        # 待补（不计金额）
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    order_meter_diff_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    basis_status: Mapped[str] = mapped_column(String(16), default=BASIS_MEASURED)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderIntervalSplit(Base):
    """订单按电价边界拆分后的最小计费段。"""
    __tablename__ = "order_interval_splits"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"), index=True)
    reconciliation_interval_id: Mapped[int | None] = mapped_column(
        ForeignKey("reconciliation_intervals.id"), nullable=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    segment_name: Mapped[str] = mapped_column(String(32))
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    basis_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), unique=True)
    bill_no: Mapped[str] = mapped_column(String(64), unique=True)
    master_total_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_order_count: Mapped[int] = mapped_column(Integer, default=0)
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    public_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default=BILL_DRAFT)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[ReconciliationJob] = relationship(back_populates="bill")
    adjustments: Mapped[list["BillAdjustment"]] = relationship(back_populates="bill")


class BillAdjustment(Base):
    """已确认账单的差额更正：保留原值，以更正行体现差异，不直接替换。"""
    __tablename__ = "bill_adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    source_job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"))
    component: Mapped[str] = mapped_column(String(32))  # charging/public/unexplained/...
    original_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    corrected_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    delta: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bill: Mapped[Bill] = relationship(back_populates="adjustments")


class OrderConflict(Base):
    """两个不同订单声称占用同一设备时段。"""
    __tablename__ = "order_conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    order_a_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    order_b_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    status: Mapped[str] = mapped_column(String(16), default=CONFLICT_OPEN)
    resolved_order_id: Mapped[int | None] = mapped_column(ForeignKey("charging_orders.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReviewTask(Base):
    """核对事项：物业只能看到并处理分配给自己站点的条目。"""
    __tablename__ = "review_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    task_kind: Mapped[str] = mapped_column(String(40))
    # meter_rollback / missing_reading / order_conflict / unexplained_diff / public_confirmation / estimated_item
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    assignee: Mapped[str] = mapped_column(String(16))  # property / operator / finance
    status: Mapped[str] = mapped_column(String(16), default=TASK_OPEN)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ===================== 跨月回补（backfill）=====================

class BackfillBatch(Base):
    """回补批次：发起时冻结固定更正数据集合，新到更正只能进入下一批。"""
    __tablename__ = "backfill_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=BF_BATCH_PENDING, index=True)
    reason_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    segment_total: Mapped[int] = mapped_column(Integer, default=0)
    segment_done: Mapped[int] = mapped_column(Integer, default=0)
    segment_suspended: Mapped[int] = mapped_column(Integer, default=0)
    segment_failed: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[list["BackfillSegment"]] = relationship(back_populates="batch")
    inputs: Mapped[list["BackfillBatchInput"]] = relationship(back_populates="batch")
    items: Mapped[list["BackfillAdjustmentItem"]] = relationship(back_populates="batch")


class BackfillBatchInput(Base):
    """批次冻结的更正数据集合：批次启动后到达的修订不在其中，只能进下一批。"""
    __tablename__ = "backfill_batch_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("backfill_batches.id"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    source_type: Mapped[str] = mapped_column(String(32))
    source_record_no: Mapped[str] = mapped_column(String(96))
    change_kind: Mapped[str] = mapped_column(String(32))  # late/correction/revocation
    batch: Mapped[BackfillBatch] = relationship(back_populates="inputs")
    __table_args__ = (UniqueConstraint("batch_id", "source_record_id"),)


class BackfillSegment(Base):
    """按受影响计量区间分段保存进度；失败只重算未完成分段。"""
    __tablename__ = "backfill_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("backfill_batches.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"))
    reconciliation_interval_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_intervals.id"))
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=BF_SEG_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    batch: Mapped[BackfillBatch] = relationship(back_populates="segments")
    items: Mapped[list["BackfillAdjustmentItem"]] = relationship(back_populates="segment")
    __table_args__ = (UniqueConstraint("batch_id", "reconciliation_interval_id"),)


class BackfillContribution(Base):
    """原贡献台账：每条被旧作业采用的贡献只允许被撤回一次，后批次可见前批次生效记录。"""
    __tablename__ = "backfill_contributions"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    entity_kind: Mapped[str] = mapped_column(String(32))  # order/reading_master/reading_device/public_load/tariff
    entity_id: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    reconciliation_interval_id: Mapped[int | None] = mapped_column(
        ForeignKey("reconciliation_intervals.id"), nullable=True)
    order_split_id: Mapped[int | None] = mapped_column(ForeignKey("order_interval_splits.id"), nullable=True)
    segment_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    # active=仍在生效；withdrawn=已被某回补批次撤回；revised=被修正值替代后旧行留存
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    supersedes_contribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("backfill_contributions.id"), nullable=True)
    withdrawn_by_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("backfill_batches.id"), nullable=True)
    withdrawn_by_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("backfill_adjustment_items.id", use_alter=True,
                   name="fk_contribution_withdrawn_item"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        # 同一 split 在不同批次可留下多条历史行（撤回/替代），故只建索引不建唯一约束
        Index("ix_contribution_split_src", "order_split_id", "source_record_id"),
        Index("ix_contribution_interval_src",
              "reconciliation_interval_id", "source_record_id", "entity_kind"),
    )


class BackfillAdjustmentItem(Base):
    """回补调整项：撤原贡献+计修正值形成的待确认差额，逐项可追溯到原始事件与原计算。"""
    __tablename__ = "backfill_adjustment_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("backfill_batches.id"), index=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("backfill_segments.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    source_job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"))
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    from_revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    to_revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    change_kind: Mapped[str] = mapped_column(String(32))   # late/correction/revocation
    reason_kind: Mapped[str] = mapped_column(String(32))
    component: Mapped[str] = mapped_column(String(32))
    segment_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_before: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    price_after: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    energy_before: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    energy_after: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    energy_delta: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    amount_before: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    amount_after: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    amount_delta: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    rounding_delta: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    basis_before: Mapped[str | None] = mapped_column(String(16), nullable=True)
    basis_after: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 完整对平依据：原始事件 payload、原计算取值、修正计算过程、舍入说明
    detail_json: Mapped[str] = mapped_column(Text)
    reason_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default=BF_ADJ_PENDING, index=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    property_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch: Mapped[BackfillBatch] = relationship(back_populates="items")
    segment: Mapped[BackfillSegment] = relationship(back_populates="items")
    reviews: Mapped[list["BackfillReview"]] = relationship(back_populates="item")


class BackfillReview(Base):
    """双方复核分别保存：物业复核与财务确认各自留痕，互不覆盖。"""
    __tablename__ = "backfill_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("backfill_adjustment_items.id"), index=True)
    review_party: Mapped[str] = mapped_column(String(16))  # property / finance
    action: Mapped[str] = mapped_column(String(24))        # acknowledge / reject / confirm
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    item: Mapped[BackfillAdjustmentItem] = relationship(back_populates="reviews")
    __table_args__ = (UniqueConstraint("item_id", "review_party"),)


class BackfillBillLink(Base):
    """调整项与后续账单的关联单独保存；旧账金额不被覆盖，应付同时引用旧账与调整项。"""
    __tablename__ = "backfill_bill_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("backfill_batches.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("backfill_adjustment_items.id"))
    original_bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    settlement_bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    linked_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/linked
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
