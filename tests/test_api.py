"""端到端 API 测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_api.py
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import services
from app.db import engine
from app.main import app

TABLES = (
    "shift_result_changes",
    "shift_results",
    "event_versions",
    "event_sources",
    "change_seq",
    "shifts",
    "devices",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
E_MAINT = {  # 保养 10:00-11:00
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "reason": "maintenance",
}
E_FAIL = {  # 故障 10:30-12:00
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:30:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "failure",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_shift(client) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------- 校验类


def test_unknown_device_shift_rejected(client):
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_unknown_device_event_rejected(client):
    r = client.post("/events", json=E_MAINT)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_invalid_intervals_rejected(client):
    client.post("/devices", json=DEVICE)
    bad = [
        {**SHIFT, "start": SHIFT["end"]},  # start == end
        {**SHIFT, "start": "2026-10-01T17:00:00+08:00"},  # start > end
        {**SHIFT, "start": "2026-10-01T08:00:30+08:00"},  # 未对齐整分钟
        {**SHIFT, "start": "2026-10-01T08:00:00"},  # 缺时区
    ]
    for payload in bad:
        r = client.post("/shifts", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 事件同样校验
    r = client.post("/events", json={**E_MAINT, "end": "2026-10-01T10:00:45+08:00"})
    assert r.status_code == 422
    r = client.post("/events", json={**E_MAINT, "reason": "unknown"})
    assert r.status_code == 422


def test_shift_overlap_rejected_but_adjacent_allowed(client):
    _make_shift(client)
    # 重叠 → 409
    r = client.post(
        "/shifts",
        json={**SHIFT, "start": "2026-10-01T15:00:00+08:00", "end": "2026-10-01T20:00:00+08:00"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SHIFT_OVERLAP"
    # 半开区间：端点相接不算重叠
    r = client.post(
        "/shifts",
        json={**SHIFT, "start": "2026-10-01T16:00:00+08:00", "end": "2026-10-01T23:00:00+08:00"},
    )
    assert r.status_code == 201
    # 其他设备不受限
    client.post("/devices", json={"id": "CNC-02", "name": "二号机"})
    r = client.post("/shifts", json={**SHIFT, "device_id": "CNC-02"})
    assert r.status_code == 201


# ---------------------------------------------------------------- 版本链与幂等


def test_event_version_chain(client):
    _make_shift(client)
    # 首版本必须从 1 开始
    r = client.post("/events", json={**E_MAINT, "version": 2})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    assert client.post("/events", json=E_MAINT).status_code == 201

    # 同来源同版本同内容 → 幂等
    r = client.post("/events", json=E_MAINT)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []

    # 同版本异内容 → 409
    r = client.post("/events", json={**E_MAINT, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 跳号 → 409
    r = client.post("/events", json={**E_MAINT, "version": 3})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    # 正确的下一版本 → 201
    r = client.post("/events", json={**E_MAINT, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 201
    assert r.json()["idempotent"] is False


# ---------------------------------------------------------------- 结算 / 更正 / 修订


def test_settle_and_late_correction_flow(client):
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT).status_code == 201
    assert client.post("/events", json=E_FAIL).status_code == 201

    # 结算：保养 60（10:00-11:00），故障 60（11:00-12:00，10:30-11:00 被保养覆盖）
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200
    res = r.json()["result"]
    assert r.json()["settled"] is True
    assert res["revision"] == 1
    assert res["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert res["delta"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert {u["source_id"] for u in res["event_versions"]} == {"MES-1", "PLC-9"}

    # 重复结算 → 幂等返回最新修订
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.json()["settled"] is False
    assert r.json()["result"]["revision"] == 1

    # 迟到更正：故障改为 10:30-11:30（v2）→ 故障只剩 11:00-11:30 的 30 分钟
    r = client.post("/events", json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 201
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    assert rec[0]["shift_id"] == shift_id
    assert rec[0]["revision"] == 2
    assert rec[0]["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}

    # 最新结果 = 修订 2；依据版本更新为 PLC-9 v2
    r = client.get(f"/shifts/{shift_id}/result")
    body = r.json()
    assert body["revision"] == 2
    assert body["latest_revision"] == 2
    assert body["totals"]["failure"] == 30
    versions = {u["source_id"]: u["version"] for u in body["event_versions"]}
    assert versions == {"MES-1": 1, "PLC-9": 2}

    # 指定修订 1 → 原结果保留
    r = client.get(f"/shifts/{shift_id}/result", params={"revision": 1})
    body = r.json()
    assert body["revision"] == 1
    assert body["totals"]["failure"] == 60
    assert body["latest_revision"] == 2

    # 不存在的修订 → 404
    r = client.get(f"/shifts/{shift_id}/result", params={"revision": 99})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"

    # 修订列表
    r = client.get(f"/shifts/{shift_id}/revisions")
    assert [x["revision"] for x in r.json()] == [1, 2]
    assert r.json()[1]["trigger"] == "correction"


def test_correction_without_change_creates_no_revision(client):
    shift_id = _make_shift(client)
    client.post("/events", json=E_MAINT)
    client.post(f"/shifts/{shift_id}/settle")
    # 同内容但递增版本号：作为新版本接受，但归因结果不变 → 不产生空修订
    r = client.post("/events", json={**E_MAINT, "version": 2})
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    r = client.get(f"/shifts/{shift_id}/revisions")
    assert [x["revision"] for x in r.json()] == [1]


def test_pending_priority_and_result_query(client):
    shift_id = _make_shift(client)
    # 待判 09:00-10:30；故障 10:00-11:00 覆盖其后半段
    p = {
        "device_id": "CNC-01",
        "source_id": "OP-1",
        "version": 1,
        "start": "2026-10-01T09:00:00+08:00",
        "end": "2026-10-01T10:30:00+08:00",
        "reason": "pending",
    }
    f = {
        "device_id": "CNC-01",
        "source_id": "PLC-9",
        "version": 1,
        "start": "2026-10-01T10:00:00+08:00",
        "end": "2026-10-01T11:00:00+08:00",
        "reason": "failure",
    }
    client.post("/events", json=p)
    client.post("/events", json=f)
    r = client.post(f"/shifts/{shift_id}/settle")
    totals = r.json()["result"]["totals"]
    assert totals == {"maintenance": 0, "failure": 60, "pending": 60}


def test_unsettled_shift_result_404(client):
    shift_id = _make_shift(client)
    r = client.get(f"/shifts/{shift_id}/result")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RESULT_NOT_FOUND"
    r = client.get("/shifts/99999/result")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"


# ---------------------------------------------------------------- 下游同步游标


def test_sync_changes_replay_and_pagination(client):
    shift_id = _make_shift(client)
    # 尚无变更：空页，游标保持 0
    r = client.get("/sync/changes")
    assert r.status_code == 200
    body = r.json()
    assert body == {"changes": [], "cursor": 0, "next_cursor": 0, "max_cursor": 0, "limit": 100}

    client.post("/events", json=E_MAINT)
    client.post("/events", json=E_FAIL)
    client.post(f"/shifts/{shift_id}/settle")

    # 默认从头回放：首条结算变更
    r = client.get("/sync/changes")
    page = r.json()
    assert page["cursor"] == 0
    assert page["next_cursor"] == 1
    assert page["max_cursor"] == 1
    assert len(page["changes"]) == 1
    ch = page["changes"][0]
    assert ch["seq"] == 1
    assert ch["shift_id"] == shift_id
    assert ch["device_id"] == "CNC-01"
    assert ch["revision"] == 1
    assert ch["trigger"] == "settle"
    assert ch["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert ch["delta"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert ch["shift_start"].endswith("+08:00") or ch["shift_start"].endswith("Z")

    # 结果未变化的新版本 → 不产生变更
    client.post("/events", json={**E_MAINT, "version": 2})
    assert client.get("/sync/changes", params={"cursor": 1}).json()["changes"] == []

    # 迟到更正 → 第二条变更
    client.post(
        "/events",
        json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"},
    )
    r = client.get("/sync/changes", params={"cursor": 1})
    page = r.json()
    assert page["next_cursor"] == 2
    assert page["max_cursor"] == 2
    ch = page["changes"][0]
    assert ch["seq"] == 2
    assert ch["revision"] == 2
    assert ch["trigger"] == "correction"
    assert ch["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert ch["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}

    # 追到最新：空页保持原游标
    r = client.get("/sync/changes", params={"cursor": 2})
    assert r.json() == {"changes": [], "cursor": 2, "next_cursor": 2, "max_cursor": 2, "limit": 100}

    # 分页：limit=1 逐页回放，游标内容不变（重复读取条目一致）
    p1 = client.get("/sync/changes", params={"cursor": 0, "limit": 1}).json()
    p2 = client.get("/sync/changes", params={"cursor": 1, "limit": 1}).json()
    assert [c["seq"] for c in p1["changes"]] == [1]
    assert p1["next_cursor"] == 1
    assert [c["seq"] for c in p2["changes"]] == [2]
    assert p2["next_cursor"] == 2
    again = client.get("/sync/changes", params={"cursor": 0, "limit": 1}).json()
    assert again["changes"] == p1["changes"]


def test_sync_changes_correction_multiple_shifts_single_commit(client):
    # 一次事件更正同时影响两个已结算班次 → 两条变更在同一事务提交，游标连续
    client.post("/devices", json=DEVICE)
    s1 = client.post("/shifts", json=SHIFT).json()["id"]
    s2 = client.post(
        "/shifts",
        json={
            **SHIFT,
            "start": "2026-10-01T16:00:00+08:00",
            "end": "2026-10-02T00:00:00+08:00",
        },
    ).json()["id"]
    # 事件跨两班次交界（15:00-17:00 故障，两班各 60 分钟）
    ev = {
        "device_id": "CNC-01",
        "source_id": "PLC-9",
        "version": 1,
        "start": "2026-10-01T15:00:00+08:00",
        "end": "2026-10-01T17:00:00+08:00",
        "reason": "failure",
    }
    client.post("/events", json=ev)
    client.post(f"/shifts/{s1}/settle")
    client.post(f"/shifts/{s2}/settle")
    # 更正：故障扩展为 14:00-18:00（两班都变为 120 分钟，各产生一条修订）
    client.post(
        "/events",
        json={**ev, "version": 2, "start": "2026-10-01T14:00:00+08:00", "end": "2026-10-01T18:00:00+08:00"},
    )
    page = client.get("/sync/changes", params={"limit": 10}).json()
    seqs = [(c["seq"], c["shift_id"], c["revision"]) for c in page["changes"]]
    assert seqs == [(1, s1, 1), (2, s2, 1), (3, s1, 2), (4, s2, 2)]
    assert page["next_cursor"] == 4
    # 按班次过滤视角：每个班次的修订号各自从 1 递增
    for shift_id in (s1, s2):
        revs = client.get(f"/shifts/{shift_id}/revisions").json()
        assert [r["revision"] for r in revs] == [1, 2]


def test_sync_changes_invalid_cursor_and_limit(client):
    shift_id = _make_shift(client)
    client.post("/events", json=E_MAINT)
    client.post(f"/shifts/{shift_id}/settle")

    # 游标越过当前最大值 → 422 INVALID_CURSOR
    r = client.get("/sync/changes", params={"cursor": 999})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 负数 / 非整数游标 → 422
    assert client.get("/sync/changes", params={"cursor": -1}).status_code == 422
    assert client.get("/sync/changes", params={"cursor": "abc"}).status_code == 422

    # 页大小越界 / 非法 → 422
    assert client.get("/sync/changes", params={"limit": 0}).status_code == 422
    assert client.get("/sync/changes", params={"limit": 501}).status_code == 422
    assert client.get("/sync/changes", params={"limit": "x"}).status_code == 422

    # 被拒绝的请求不改变同步进度：游标 0 仍能读到变更
    body = client.get("/sync/changes").json()
    assert len(body["changes"]) == 1
    assert body["next_cursor"] == 1


def test_sync_changes_atomic_rollback(client, monkeypatch):
    # 修订与变更必须同事务：提交前异常 → 修订与变更同时回滚，可重新结算
    shift_id = _make_shift(client)
    client.post("/events", json=E_MAINT)

    import app.services as services_mod

    real_commit = services_mod.Session.commit

    def boom(self):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(services_mod.Session, "commit", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/shifts/{shift_id}/settle")
    monkeypatch.setattr(services_mod.Session, "commit", real_commit)

    # 回滚后：无变更、无修订，计数器未推进
    body = client.get("/sync/changes").json()
    assert body["changes"] == []
    assert body["max_cursor"] == 0
    r = client.get(f"/shifts/{shift_id}/result")
    assert r.status_code == 404

    # 可重新正常结算
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200
    body = client.get("/sync/changes").json()
    assert [c["seq"] for c in body["changes"]] == [1]


def test_sync_changes_concurrent_gapless(client):
    # 并发结算 + 更正：游标必须连续无空隙、不越过未提交的较早变更，且条目内容可重读不变
    client.post("/devices", json=DEVICE)
    s1 = client.post("/shifts", json=SHIFT).json()["id"]
    s2 = client.post(
        "/shifts",
        json={
            **SHIFT,
            "start": "2026-10-01T16:00:00+08:00",
            "end": "2026-10-02T00:00:00+08:00",
        },
    ).json()["id"]
    client.post("/events", json=E_MAINT)  # 10:00-11:00 落在 s1
    client.post(f"/shifts/{s1}/settle")
    client.post(f"/shifts/{s2}/settle")

    n = 25

    from app.db import SessionLocal
    from app.schemas import EventIn

    errors: list[Exception] = []

    def make_ev(source, version):
        # 奇数版 15:00-18:00、偶数版 15:30-17:00：起止都变化，两个班次每版结果都不同
        if version % 2:
            start, end = "15:00", "18:00"
        else:
            start, end = "15:30", "17:00"
        return {
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-01T{start}:00+08:00",
            "end": f"2026-10-01T{end}:00+08:00",
            "reason": "failure",
        }

    def writer(src):
        db = SessionLocal()
        try:
            for v in range(1, n + 1):
                services.ingest_event(db, EventIn(**make_ev(src, v)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    seen: dict[int, dict] = {}
    stop = threading.Event()

    def reader():
        time.sleep(0.05)
        cur = 0
        while not stop.is_set():
            r = client.get("/sync/changes", params={"cursor": cur, "limit": 7})
            if r.status_code != 200:
                errors.append(AssertionError(f"reader got {r.status_code}"))
                return
            page = r.json()
            expected = cur + 1
            for ch in page["changes"]:
                # 游标不能越过尚未可见的较早变更：页内序号必须严格连续
                if ch["seq"] != expected:
                    errors.append(
                        AssertionError(f"non-contiguous feed: expected {expected}, got {ch['seq']}")
                    )
                    return
                expected += 1
                if ch["seq"] in seen:
                    if seen[ch["seq"]] != ch:
                        errors.append(AssertionError("change content mutated"))
                        return
                else:
                    seen[ch["seq"]] = ch
            cur = page["next_cursor"]
            if not page["changes"]:
                time.sleep(0.005)

    t_read = threading.Thread(target=reader)
    t_read.start()
    threads = [threading.Thread(target=writer, args=(f"PLC-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.2)
    stop.set()
    t_read.join()

    assert errors == []

    # 最终：全部变更连续无空隙；变更条数与修订行数一致（结果未变化不产生二者）
    page = client.get("/sync/changes", params={"cursor": 0, "limit": 500}).json()
    total_revisions = 0
    with engine.connect() as conn:
        total_revisions = conn.execute(text("SELECT count(*) FROM shift_results")).scalar_one()
    seqs = sorted(c["seq"] for c in page["changes"])
    assert len(seqs) == total_revisions
    assert seqs == list(range(1, total_revisions + 1))
    assert page["max_cursor"] == total_revisions
    # 两班次各至少经历一次更正（并发写入确实触发了新修订）
    assert total_revisions > 2
    # 重读全部条目，与并发期间观察到的内容逐一比对，必须完全一致
    for ch in page["changes"]:
        assert seen.get(ch["seq"], ch) == ch
