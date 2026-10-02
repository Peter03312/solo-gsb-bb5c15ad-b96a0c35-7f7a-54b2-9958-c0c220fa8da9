# 设备停机归因服务（Downtime Attribution）

按**班次**对设备停机时间进行归因的 HTTP 服务：输入设备、班次（带时区、整分钟）与停机事件
（来源编号 + 连续递增版本 + 起止 + 原因），输出各班次的**保养 / 故障 / 待判**分钟数及所依据
的事件版本；支持事件幂等上报、版本链校验、迟到更正重算与结算修订查询。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Docker Compose

## 快速启动

```bash
docker compose up --build
```

一条命令完成依赖安装、PostgreSQL 建库建表（含扩展与约束）与服务启动。

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | API 根地址 |
| `http://localhost:8000/docs` | Swagger 交互文档（可在线试调用） |
| `http://localhost:8000/health` | 健康检查 |
| `localhost:5432` | PostgreSQL（用户/密码 `postgres`，库 `downtime`） |

冒烟验证（覆盖下文全部流程）：

```bash
./scripts/smoke.sh                 # 或指定地址 ./scripts/smoke.sh http://localhost:8000
```

运行测试：

```bash
docker compose exec app pytest            # 算法单测 + API 端到端测试
```

## 配置

通过环境变量配置（`docker-compose.yml` 已为 `app` 服务注入）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/downtime` | SQLAlchemy 连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `downtime` | Compose 中数据库初始化参数 |

端口映射：`8000`（API）、`5432`（数据库），可在 `docker-compose.yml` 中调整。

## 业务规则

- **半开区间**：班次与事件均为 `[start, end)`；端点相接不算重叠、不漏分钟。
- **时间戳**：必须带时区且精确到整分钟（秒须为 `00`），否则 `422`。
- **班次**：同一设备的班次区间不可重叠（应用校验 + 数据库排他约束双保险），冲突 `409`。
- **事件版本链**：每个 `(设备, 来源)` 的版本从 1 开始连续递增。
  - 同来源 + 同版本 + 同内容 → 幂等返回（`200`，`idempotent=true`），不产生新数据；
  - 同版本异内容 → `409 VERSION_CONFLICT`；跳号 → `409 VERSION_GAP`；
  - 新版本即对旧版本的更正，当前生效事件 = 每个来源的最新版本。
- **归因**：窗口内每分钟只归因一次，重叠时 **保养 > 故障 > 待判**；结果含各原因分钟数
  与实际贡献分钟的事件版本列表。
- **结算与修订**：`POST /shifts/{id}/settle` 结算生成修订 1 并永久保留；迟到更正按事件时间
  （新旧版本区间的并集）重算受影响的已结算班次，结果变化时追加新修订并记录与上一修订的
  差额（`delta`）；查询可用 `?revision=N` 指定历史修订。
- **下游同步（变更游标）**：`GET /sync/changes` 提供全局严格递增、连续无空隙的变更流，覆盖每次
  结算与更正产生的新修订（归因结果未变化则不产生变更）。变更与其修订在**同一事务**提交/回滚；
  游标按事务提交顺序分配，并发写入下游也不会读到"越过尚未可见的较早变更"的页面；条目快照不可变，
  重读同一 `seq` 内容永远一致。
- **错误**：未知设备 `404 DEVICE_NOT_FOUND`，非法区间 `422`，未结算 `404 RESULT_NOT_FOUND`，
  未知班次/修订 `404`。错误体统一为 `{"detail": {"code": "...", "message": "..."}}`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 注册设备（同 id 重复提交幂等返回 200） |
| `GET` | `/devices` | 设备列表 |
| `POST` | `/shifts` | 创建班次（重叠 409 / 未知设备 404 / 非法区间 422） |
| `GET` | `/shifts/{id}` | 班次详情 |
| `POST` | `/events` | 上报事件版本（新建 201 / 幂等 200 / 冲突·跳号 409） |
| `POST` | `/shifts/{id}/settle` | 结算班次（重复调用幂等返回最新修订） |
| `GET` | `/shifts/{id}/result?revision=N` | 查询归因结果，缺省最新修订 |
| `GET` | `/shifts/{id}/revisions` | 全部修订及差额 |
| `GET` | `/sync/changes?cursor=N&limit=M` | 下游同步：按全局游标分页读取结算/更正变更 |
| `GET` | `/health` | 健康检查 |

原因枚举：`maintenance`（保养）、`failure`（故障）、`pending`（待判）。

错误体统一为 `{"detail": {"code": "...", "message": "..."}}`：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `DEVICE_NOT_FOUND` | 未知设备 |
| 404 | `SHIFT_NOT_FOUND` | 未知班次 |
| 404 | `RESULT_NOT_FOUND` | 班次尚未结算 |
| 404 | `REVISION_NOT_FOUND` | 指定修订不存在 |
| 409 | `SHIFT_OVERLAP` | 同设备班次区间重叠 |
| 409 | `VERSION_CONFLICT` | 同来源同版本但内容不同 |
| 409 | `VERSION_GAP` | 版本跳号（含首版本不为 1） |
| 422 | `VALIDATION_ERROR` | 非法参数：区间无效、缺时区、未对齐整分钟、原因非法、页大小越界等 |
| 422 | `INVALID_CURSOR` | 同步游标非法（负数或超过当前最大游标） |

## 调用示例

以下完整走一遍主流程（假设服务运行于 `http://localhost:8000`）。

**1. 注册设备**

```bash
curl -X POST http://localhost:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"id": "CNC-01", "name": "加工中心1号"}'
# 201 {"id":"CNC-01","name":"加工中心1号"}
```

**2. 创建班次**（早班 08:00–16:00，+08:00）

```bash
curl -X POST http://localhost:8000/shifts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'
# 201 {"id":1,"device_id":"CNC-01","start":"2026-10-01T00:00:00Z","end":"2026-10-01T08:00:00Z"}
```

重叠班次返回 `409 {"detail":{"code":"SHIFT_OVERLAP", ...}}`；端点相接（如 16:00 起）允许。

**3. 上报停机事件**：保养 10:00–11:00

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"maintenance"}'
# 201 {"device_id":"CNC-01","source_id":"MES-1","version":1,"idempotent":false,"recomputed":[]}
```

**4. 幂等与版本校验**

```bash
# 原样重发 → 200，idempotent=true
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

# 同版本异内容 → 409 VERSION_CONFLICT
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

# 跳号（应为 2 却发 3）→ 409 VERSION_GAP
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'
```

**5. 再上报一条故障** 10:30–12:00（另一来源）

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":1,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'
```

**6. 结算班次**

```bash
curl -X POST http://localhost:8000/shifts/1/settle
```

```json
{
  "settled": true,
  "result": {
    "shift_id": 1, "revision": 1, "latest_revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "..."
  }
}
```

> 10:30–11:00 与保养重叠，按优先级归保养；故障只计 11:00–12:00 的 60 分钟。

**7. 迟到更正**：故障实际只到 11:30，上报 `PLC-9` 的版本 2

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":2,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'
```

```json
{
  "version": 2, "idempotent": false,
  "recomputed": [{
    "shift_id": 1, "revision": 2,
    "totals": {"maintenance": 60, "failure": 30, "pending": 0},
    "delta":  {"maintenance": 0,  "failure": -30, "pending": 0}
  }]
}
```

**8. 查询结果与修订**

```bash
curl http://localhost:8000/shifts/1/result                # 最新修订（revision=2）
curl 'http://localhost:8000/shifts/1/result?revision=1'   # 指定修订：结算时的原结果（failure=60）
curl http://localhost:8000/shifts/1/revisions             # 全部修订及每次差额
```

**9. 下游报表同步**：按全局游标分页拉取每次结算与更正

游标语义：

- `cursor` 为"已读到的位置"（不含），首次用 `0` 即可**回放全部既有修订**；
- 每页按 `seq` 升序返回至多 `limit` 条变更，响应中的 `next_cursor` 是下次请求的 `cursor`；
- **空页保持原游标**（`next_cursor == cursor`），追上后可轮询；`max_cursor == next_cursor` 即已追上；
- 每条 `seq` 全局严格递增、连续无空隙，且内容（含班次、修订号、各原因分钟数、差额、触发类型）是
  插入时的不可变快照；
- 归因结果未变化的更正不产生变更；
- `cursor` 超过当前最大值（或为负）→ `422 INVALID_CURSOR`；`limit` 须在 `1..500`（默认 100），
  越界 → `422`；非法请求不推进任何同步进度（进度完全由下游保存的游标决定）。

```bash
# 首次：cursor=0（可省略）从头回放，每页 100 条
curl 'http://localhost:8000/sync/changes?cursor=0&limit=100'
```

```json
{
  "changes": [
    {
      "seq": 1,
      "shift_id": 1,
      "device_id": "CNC-01",
      "shift_start": "2026-10-01T00:00:00Z",
      "shift_end": "2026-10-01T08:00:00Z",
      "revision": 1,
      "totals": {"maintenance": 60, "failure": 60, "pending": 0},
      "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
      "trigger": "settle",
      "created_at": "2026-10-02T09:00:00Z"
    },
    {
      "seq": 2,
      "shift_id": 1,
      "device_id": "CNC-01",
      "shift_start": "2026-10-01T00:00:00Z",
      "shift_end": "2026-10-01T08:00:00Z",
      "revision": 2,
      "totals": {"maintenance": 60, "failure": 30, "pending": 0},
      "delta":  {"maintenance": 0,  "failure": -30, "pending": 0},
      "trigger": "correction",
      "created_at": "2026-10-02T09:05:00Z"
    }
  ],
  "cursor": 0,
  "next_cursor": 2,
  "max_cursor": 2,
  "limit": 100
}
```

```bash
# 续读后续变更：用上次的 next_cursor
curl 'http://localhost:8000/sync/changes?cursor=2'
# 暂无新变更 → 空页，next_cursor 保持 2
# {"changes":[],"cursor":2,"next_cursor":2,"max_cursor":2,"limit":100}

# 小页回放（例如迁移/补偿时逐页拉取）
curl 'http://localhost:8000/sync/changes?cursor=0&limit=1'   # 只返回 seq=1，next_cursor=1

# 非法游标 / 超限页大小 → 422，不影响同步进度
curl 'http://localhost:8000/sync/changes?cursor=999999'
# {"detail":{"code":"INVALID_CURSOR","message":"cursor must be in [0, 2], got 999999"}}
curl 'http://localhost:8000/sync/changes?limit=501'          # 422 VALIDATION_ERROR
```

一个持续同步的最小循环（保存好 `next_cursor` 即可实现 at-least-once，重读旧 `seq` 内容不变，
下游按 `(shift_id, revision)` 或 `seq` 去重即可）：

```bash
cursor=0
while true; do
  body=$(curl -sS "http://localhost:8000/sync/changes?cursor=${cursor}&limit=100")
  echo "$body" | jq -c '.changes[]'                    # 逐条 upsert 到报表库
  cursor=$(echo "$body" | jq -r '.next_cursor')
  [ "$(echo "$body" | jq -r '.changes | length')" = 0 ] && sleep 5
done
```

Python 示例：

```python
import requests

cursor, limit = 0, 100
while True:
    page = requests.get(
        "http://localhost:8000/sync/changes",
        params={"cursor": cursor, "limit": limit},
        timeout=10,
    ).json()
    for ch in page["changes"]:
        # 按 (shift_id, revision) upsert：totals 为该修订全量值，delta 为与上一修订的差额
        upsert_report(ch)
    cursor = page["next_cursor"]  # 空页时等于原游标，可安全轮询
    if not page["changes"]:
        time.sleep(5)
```

**10. 错误示例**

```bash
# 非法区间（start ≥ end、秒非 00、缺时区）→ 422
curl -X POST http://localhost:8000/shifts -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'

# 未知设备 → 404 DEVICE_NOT_FOUND
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"NOPE","source_id":"X","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
```

## 设计要点

- **归因算法**（`app/attribution.py`，纯标准库可独立测试）：按原因收集区间并裁剪到班次窗口；
  保养区间合并后全量生效，故障区间扣除保养覆盖，待判区间再扣除保养与故障覆盖 —— 区间运算
  保证每分钟只归因一次；`used_versions` 只保留裁剪后仍对有效区间有贡献的事件版本。
- **版本链与幂等**：`event_sources` 表保存每个 `(设备, 来源)` 的 `last_version` 与内容哈希，
  上报时 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE` 串行化校验；
  内容哈希 = `sha256(start_utc|end_utc|reason)`。
- **迟到更正**：新版本落库后，按"旧版本区间 ∪ 新版本区间"找出受影响的已结算班次，用全部
  来源的最新版本重算；仅当分钟数变化时追加新修订（避免空修订），差额 = 新 − 旧。
- **并发与一致性**：班次不重叠由 PostgreSQL 排他约束（`btree_gist` + `tstzrange '[)'`）兜底；
  结算与更正重算时对班次行加锁；事件版本链对来源行加锁，全流程单事务提交。
- **不可变历史**：`shift_results` 只追加不修改，`(shift_id, revision)` 唯一。
- **变更游标**（`shift_result_changes` outbox + `change_seq` 单行计数器）：每次追加修订时在
  **同一事务**内对计数器行 `SELECT ... FOR UPDATE` 自增并持锁至提交，因此 `seq` 按提交顺序分配、
  连续无空隙 —— 一个序号被外部读到时，它之前的所有序号必然已经提交可见，游标不可能越过尚未可见
  的较早变更；快照列在插入时固化，历史修订又只追加不修改，故重读同一 `seq` 内容永远不变。
  一次更正涉及多个班次时，其多条修订与变更同样在同一事务内连续取号、一次提交。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  services.py     # 业务逻辑：版本链、幂等、结算、更正重算
  attribution.py  # 归因算法（半开区间运算，纯标准库）
  models.py       # SQLAlchemy 模型
  schemas.py      # Pydantic 请求/响应与校验
  db.py           # 引擎、会话、建库建表（含排他约束）
  config.py       # 环境变量配置
tests/
  test_attribution.py  # 算法单测（无外部依赖，可 python3 直接跑）
  test_api.py          # API 端到端测试（需 PostgreSQL）
scripts/smoke.sh       # 冒烟脚本
Dockerfile / docker-compose.yml / requirements.txt
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/downtime'
uvicorn app.main:app --reload --port 8000
pytest
```
