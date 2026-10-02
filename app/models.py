"""SQLAlchemy 数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Shift(Base):
    """班次：半开区间 [start_ts, end_ts)，同设备不得重叠（由排他约束兜底）。"""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), nullable=False, index=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_ts > start_ts", name="ck_shifts_valid_range"),
    )


class EventSource(Base):
    """每个 (设备, 来源) 的版本链游标，用于幂等校验与连续版本控制。"""

    __tablename__ = "event_sources"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))


class EventVersion(Base):
    """停机事件的某个版本；同一来源的最新版本为当前生效事件。"""

    __tablename__ = "event_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "source_id", "version", name="uq_event_version"),
        CheckConstraint("end_ts > start_ts", name="ck_events_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')", name="ck_events_reason"
        ),
    )


class ShiftResult(Base):
    """班次结算结果的一个修订。

    结算后原修订永久保留；迟到更正触发重算时追加新修订并记录与上一修订的差额。
    """

    __tablename__ = "shift_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    maintenance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 与上一修订的差额（首修订相对 0）
    d_maintenance: Mapped[int] = mapped_column(Integer, nullable=False)
    d_failure: Mapped[int] = mapped_column(Integer, nullable=False)
    d_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本次计算所依据的事件版本：[{"source_id", "version", "reason"}, ...]
    event_versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # settle | correction
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("shift_id", "revision", name="uq_shift_result_revision"),
    )


class ChangeSeq(Base):
    """变更游标的全局单行计数器。

    每次追加变更时对该行加行锁并自增，直到事务提交才释放：
    序号按提交顺序分配且无空隙，保证游标不会越过尚未可见的较早变更。
    """

    __tablename__ = "change_seq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (CheckConstraint("id = 0", name="ck_change_seq_singleton"),)


class ShiftResultChange(Base):
    """下游报表同步用的变更日志（outbox）：每条对应一次结算或更正产生的新修订。

    与对应 ShiftResult 在同一事务中写入（同提交 / 同回滚）；
    seq 全局严格递增、连续无空隙且不可变，作为同步分页游标。
    快照字段在插入时固化，重读已返回的条目内容永远不变。
    """

    __tablename__ = "shift_result_changes"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shift_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    shift_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    maintenance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    d_maintenance: Mapped[int] = mapped_column(Integer, nullable=False)
    d_failure: Mapped[int] = mapped_column(Integer, nullable=False)
    d_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # settle | correction
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('settle','correction')", name="ck_change_trigger"
        ),
    )
