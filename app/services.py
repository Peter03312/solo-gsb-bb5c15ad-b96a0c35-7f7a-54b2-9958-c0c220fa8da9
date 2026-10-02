"""业务逻辑：设备、班次、事件版本链、结算与迟到更正重算。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import attribution
from .attribution import EventView
from .models import ChangeSeq, Device, EventSource, EventVersion, Shift, ShiftResult, ShiftResultChange
from .schemas import DeviceCreate, EventIn, ShiftCreate

# 同步分页默认与上限
SYNC_DEFAULT_LIMIT = 100
SYNC_MAX_LIMIT = 500


class ApiError(HTTPException):
    """统一错误格式：{"detail": {"code": ..., "message": ...}}"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _content_hash(start: datetime, end: datetime, reason: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_device_or_404(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")
    return device


# ---------------------------------------------------------------- 设备


def create_device(db: Session, payload: DeviceCreate) -> tuple[Device, bool]:
    existing = db.get(Device, payload.id)
    if existing is not None:
        return existing, False
    device = Device(id=payload.id, name=payload.name)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:  # 并发重复创建
        db.rollback()
        return db.get(Device, payload.id), False
    return device, True


def list_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.id)).scalars().all())


# ---------------------------------------------------------------- 班次


def create_shift(db: Session, payload: ShiftCreate) -> Shift:
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    conflict = (
        db.execute(
            select(Shift).where(
                Shift.device_id == payload.device_id,
                Shift.start_ts < end,
                Shift.end_ts > start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "SHIFT_OVERLAP",
            f"shift overlaps existing shift id={conflict.id} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )
    shift = Shift(device_id=payload.device_id, start_ts=start, end_ts=end)
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:  # 排他约束兜底（并发创建）
        db.rollback()
        raise ApiError(
            409, "SHIFT_OVERLAP", "shift overlaps an existing shift on this device"
        )
    db.refresh(shift)
    return shift


def get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    return shift


# ---------------------------------------------------------------- 归因计算


def _current_event_views(
    db: Session, device_id: str, start: datetime, end: datetime
) -> list[EventView]:
    """设备当前生效事件（每个来源取最新版本）中与窗口 [start, end) 重叠的部分。"""
    stmt = (
        select(EventVersion)
        .join(
            EventSource,
            and_(
                EventVersion.device_id == EventSource.device_id,
                EventVersion.source_id == EventSource.source_id,
                EventVersion.version == EventSource.last_version,
            ),
        )
        .where(
            EventVersion.device_id == device_id,
            EventVersion.start_ts < end,
            EventVersion.end_ts > start,
        )
    )
    rows = db.execute(stmt).scalars().all()
    return [
        EventView(
            source_id=r.source_id,
            version=r.version,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
        )
        for r in rows
    ]


def _latest_revision(db: Session, shift_id: int) -> ShiftResult | None:
    return (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _compute(db: Session, shift: Shift) -> tuple[dict, list[dict]]:
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    return attribution.attribute(shift.start_ts, shift.end_ts, events)


def _new_revision(
    db: Session,
    shift: Shift,
    totals: dict,
    used: list[dict],
    trigger: str,
    base: ShiftResult | None,
) -> ShiftResult:
    if base is None:
        revision, bm, bf, bp = 1, 0, 0, 0
    else:
        revision = base.revision + 1
        bm, bf, bp = (
            base.maintenance_minutes,
            base.failure_minutes,
            base.pending_minutes,
        )
    rev = ShiftResult(
        shift_id=shift.id,
        revision=revision,
        maintenance_minutes=totals["maintenance"],
        failure_minutes=totals["failure"],
        pending_minutes=totals["pending"],
        d_maintenance=totals["maintenance"] - bm,
        d_failure=totals["failure"] - bf,
        d_pending=totals["pending"] - bp,
        event_versions=used,
        trigger=trigger,
    )
    db.add(rev)
    return rev


def _append_changes(
    db: Session, pairs: list[tuple[Shift, ShiftResult]]
) -> list[ShiftResultChange]:
    """为本事务内新建的修订分配全局连续游标并写入变更日志。

    与修订行在同一事务中：提交后同时可见，回滚则同时消失。
    对单行计数器加行锁并持锁至提交：游标按提交顺序分配、无空隙，
    因而任何已提交游标之前都不存在"尚未可见"的较早变更。
    """
    if not pairs:
        return []
    db.execute(
        pg_insert(ChangeSeq).values(id=0, last_seq=0).on_conflict_do_nothing()
    )
    counter = db.execute(
        select(ChangeSeq).where(ChangeSeq.id == 0).with_for_update()
    ).scalar_one()
    changes: list[ShiftResultChange] = []
    for shift, rev in pairs:
        counter.last_seq += 1
        db.flush()  # 固化自增后的值，供下面作为主键使用
        change = ShiftResultChange(
            seq=counter.last_seq,
            shift_id=shift.id,
            device_id=shift.device_id,
            shift_start=shift.start_ts,
            shift_end=shift.end_ts,
            revision=rev.revision,
            maintenance_minutes=rev.maintenance_minutes,
            failure_minutes=rev.failure_minutes,
            pending_minutes=rev.pending_minutes,
            d_maintenance=rev.d_maintenance,
            d_failure=rev.d_failure,
            d_pending=rev.d_pending,
            trigger=rev.trigger,
        )
        db.add(change)
        changes.append(change)
    return changes


# ---------------------------------------------------------------- 结算


def settle_shift(db: Session, shift_id: int) -> tuple[Shift, ShiftResult, bool]:
    """结算班次：计算并写入修订 1；已结算则幂等返回最新修订。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is not None:
        return shift, latest, False
    totals, used = _compute(db, shift)
    rev = _new_revision(db, shift, totals, used, trigger="settle", base=None)
    _append_changes(db, [(shift, rev)])
    db.commit()
    db.refresh(rev)
    return shift, rev, True


# ---------------------------------------------------------------- 事件上报


def ingest_event(
    db: Session, payload: EventIn
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """上报事件版本。

    返回 (事件版本, 是否幂等命中, 因更正而重算的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = _content_hash(start, end, payload.reason)

    # 确保版本链游标存在并加行锁，串行化同一来源的并发上报
    db.execute(
        pg_insert(EventSource)
        .values(
            device_id=payload.device_id, source_id=payload.source_id, last_version=0
        )
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
        .with_for_update()
    ).scalar_one()

    if payload.version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 同来源同版本同内容：幂等返回
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{src.last_version + 1}, got {payload.version}",
        )

    prev = None
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == src.last_version,
            )
        ).scalar_one()

    ev = EventVersion(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = payload.version
    src.last_content_hash = digest
    db.flush()

    recomputed = _recompute_affected_shifts(db, payload.device_id, prev, ev)
    db.commit()
    return ev, False, recomputed


def _recompute_affected_shifts(
    db: Session,
    device_id: str,
    prev: EventVersion | None,
    new: EventVersion,
) -> list[tuple[Shift, ShiftResult]]:
    """迟到更正：按事件时间（旧版本与新版本区间的并集）找出受影响的已结算班次，
    用最新事件版本重算；结果有变化时追加新修订并记录差额。
    """
    conds = [and_(Shift.start_ts < new.end_ts, Shift.end_ts > new.start_ts)]
    if prev is not None:
        conds.append(and_(Shift.start_ts < prev.end_ts, Shift.end_ts > prev.start_ts))
    shifts = (
        db.execute(
            select(Shift)
            .where(Shift.device_id == device_id, or_(*conds))
            .with_for_update()
            .order_by(Shift.id)
        )
        .scalars()
        .all()
    )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 尚未结算：结算时自然会使用最新事件版本
        totals, used = _compute(db, shift)
        if (
            totals["maintenance"] == latest.maintenance_minutes
            and totals["failure"] == latest.failure_minutes
            and totals["pending"] == latest.pending_minutes
        ):
            continue  # 结果未变化，不产生空修订
        rev = _new_revision(db, shift, totals, used, trigger="correction", base=latest)
        db.flush()
        recomputed.append((shift, rev))
    # 全部修订与其变更在同一事务提交；多条更正按班次 id 顺序连续取号
    _append_changes(db, recomputed)
    return recomputed


# ---------------------------------------------------------------- 结果查询


def get_result(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int]:
    """返回 (班次, 指定/最新修订, 最新修订号)。"""
    shift = get_shift_or_404(db, shift_id)
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        return shift, latest, latest.revision
    rev = db.execute(
        select(ShiftResult).where(
            ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
        )
    ).scalar_one_or_none()
    if rev is None:
        raise ApiError(
            404, "REVISION_NOT_FOUND", f"shift id={shift_id} has no revision {revision}"
        )
    return shift, rev, latest.revision


def list_revisions(db: Session, shift_id: int) -> tuple[Shift, list[ShiftResult]]:
    shift = get_shift_or_404(db, shift_id)
    revs = (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision)
        )
        .scalars()
        .all()
    )
    return shift, list(revs)


# ---------------------------------------------------------------- 下游同步（变更游标）


def list_changes(
    db: Session, cursor: int, limit: int
) -> tuple[list[ShiftResultChange], int, int]:
    """按全局游标读取变更页。

    返回 (变更条目（按 seq 升序）, 下一游标, 当前最大游标)。
    - 起始游标 0：从头回放全部既有修订；
    - 空页（cursor 已到最新）：下一游标 = cursor，保持不变；
    - 游标不可超过当前最大值（指向尚不存在的位置属非法游标）。
    """
    max_seq = db.execute(
        select(func.coalesce(func.max(ShiftResultChange.seq), 0))
    ).scalar_one()
    if cursor < 0 or cursor > max_seq:
        raise ApiError(
            422,
            "INVALID_CURSOR",
            f"cursor must be in [0, {max_seq}], got {cursor}",
        )
    rows = list(
        db.execute(
            select(ShiftResultChange)
            .where(ShiftResultChange.seq > cursor)
            .order_by(ShiftResultChange.seq)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    next_cursor = rows[-1].seq if rows else cursor
    return rows, next_cursor, max_seq
