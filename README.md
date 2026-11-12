# 工时归属修订与历史压缩 API（FastAPI + SQLite）

财务数仓场景：员工调岗补录会改变成本中心归属，但**月结时的口径必须冻结**。
本服务以「递增修订号 + 只追加（append-only）存储」实现补录与历史口径并存，并提供
**历史压缩（compaction）**能力：把过期的人事归属历史折叠为持久基线后物理删除，
同时保证工时入仓仍在使用的保留口径查询结果**逐字节不变**。

- 人事导出的员工 / 成本中心 / 生效日期区间按批提交，每批成功返回一个**递增修订号**；
- 补录只覆盖同员工在指定区间内的归属，**区间外原归属保留，允许空档**；
- 日期区间为**左闭右开** `[effective_from, effective_to)`；
- 给定修订号查询工时归属时，只可见该修订号及之前的数据，**后续跨段补录不改变旧修订的查询结果**；
- 批内同员工区间重叠，或起日不早于止日时，**整批拒绝**：数据与修订号均不变；
- **历史压缩**：指定现存修订 B 为最早保留口径，早于 B 的有效归属折叠成持久基线，
  实际删除这些旧修订及原始区间；B 及之后的查询/审计完全保持不变，早于 B 的查询/审计明确报过期。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常处理（含压缩/基线/边界接口）
│   ├── service.py     # 批次校验、修订提交、归属查询、两阶段历史压缩（核心业务规则）
│   ├── database.py    # SQLite 连接、WAL、建表（修订表 + 基线表 + 压缩状态/审计表）
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── errors.py      # 业务错误类型
├── tests/
│   ├── test_api.py          # 基础能力端到端测试
│   └── test_compaction.py   # 压缩：等价性、过期、幂等、并发、原子性、重启持久化
├── scripts/
│   ├── demo.sh              # 命令行演示：补录 + 口径冻结 + 整批拒绝
│   └── demo_compaction.sh   # 命令行演示：历史压缩全流程
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 一、Docker Compose 启动（交付方式）

```bash
docker compose up -d --build
```

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`），
  容器重建后修订历史、持久基线与保留边界均保留。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空所有修订与压缩状态）
```

## 二、通过交互式文档演示压缩与保留口径查询

打开 <http://localhost:8000/docs>，按以下顺序操作（各接口均含示例请求体，可直接 `Try it out`）：

1. **POST `/api/revisions`** 连续提交三个修订（初始归属 → 4 月调岗 → 6 月调岗，构造跨批覆盖）
2. **POST `/api/attribution/query`** 用最新修订号查询，留档压缩前结果（含命中修订号、空档、半开边界）
3. **POST `/api/compactions`** 请求体 `{"retain_from_revision": 3}`，把修订 3 设为最早保留口径 B
   → 返回基线行数、删除的修订/区间行数、`idempotent_replay: false`
4. 再次调用同一压缩接口 → `idempotent_replay: true`，**数据不重复改变**（同一 B 幂等）
5. **GET `/api/compactions/boundary`** 查看保留边界与累计删除行数
6. **POST `/api/baseline/segments`** 审计持久基线：折叠区间及折叠前真正命中的 `source_revision_id`
7. 用第 2 步完全相同的查询再次请求 → **结果与压缩前逐字节一致**（成本中心、`matched_revision_id`、空档、半开边界）
8. 用早于 B 的修订号（如 1）查询 → **HTTP 410 `REVISION_EXPIRED`**；
   POST `/api/revisions/1/segments` 审计旧区间同样 410
9. POST `/api/revisions/3/segments` → 保留修订的**原始区间仍完整可审计**
10. 再 POST `/api/revisions` 补录 → 修订号继续递增（压缩不阻塞补录、不丢新修订）

也可以一条命令跑完等价的命令行演示：

```bash
./scripts/demo.sh               # 基础：补录 + 口径冻结 + 整批拒绝
./scripts/demo_compaction.sh    # 压缩全流程（建议在空库上运行）
```

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/revisions` | 提交一批归属区间，成功返回递增修订号（201） |
| GET | `/api/revisions` | 列出全部**现存**修订号（被压缩删除的不再出现，编号不重排） |
| POST | `/api/attribution/query` | 按指定修订号逐条返回工时成本中心 / 未归属 |
| POST | `/api/revisions/{id}/segments` | 查看某修订号提交的原始区间（审计） |
| POST | `/api/compactions` | **历史压缩**：指定现存修订 B 为最早保留口径 |
| GET | `/api/compactions/boundary` | **保留边界与累计删除行数查询** |
| GET | `/api/compactions` | 压缩审计流水（每次压缩的基线/删除行数、幂等标记） |
| POST | `/api/baseline/segments` | 审计持久基线（折叠区间 + 折叠前命中修订号） |

### 压缩请求与响应

```json
POST /api/compactions
{ "retain_from_revision": 3 }
```

```json
{
  "run_id": 1,
  "request_key": "compaction:3",
  "retention_boundary": 3,
  "prior_boundary": null,
  "baseline_inserted": 4,
  "segments_deleted": 3,
  "revisions_deleted": 2,
  "new_revisions_kept": 1,
  "idempotent_replay": false,
  "created_at": "2026-11-12T00:38:15.322Z"
}
```

- `baseline_inserted`：本次写入的持久基线区间行数；
- `segments_deleted` / `revisions_deleted`：**实际删除**的旧原始区间行数 / 旧修订行数；
- `new_revisions_kept`：提交时修订号 `>= B` 的现存修订数（含基线计算期间到达的补录）；
- 同一 B 重复请求返回同一条审计记录且 `idempotent_replay: true`，不重复改数。

### 保留边界查询

```json
GET /api/compactions/boundary
```

```json
{
  "retention_boundary": 3,
  "compacted": true,
  "total_runs": 1,
  "total_baseline_segments": 4,
  "total_segments_deleted": 3,
  "total_revisions_deleted": 2,
  "latest_run_id": 1,
  "updated_at": "2026-11-12T00:38:15.322Z"
}
```

未压缩过时 `retention_boundary` 为 `null`、`compacted` 为 `false`。

### 归属查询（压缩前后一致）

```json
POST /api/attribution/query
{
  "revision_id": 3,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8}
  ]
}
```

响应中 `matched_revision_id` 是命中区间所属的修订号。压缩后旧区间虽被删除，
基线行携带的 `source_revision_id` 会让该字段仍返回折叠前真正命中的修订号。
未归属时 `cost_center` 为 `null`、`attributed` 为 `false`。

## 四、核心设计

### 4.1 旧口径为什么不会变（补录路径）

1. **只追加，不改写历史**。每批写入一条 `revisions` 记录与若干带 `revision_id` 的
   `attribution_segments`；补录路径不执行 UPDATE/DELETE。
2. **查询带时间旅行条件**。修订 N 的查询只扫描 `revision_id <= N` 的区间，
   并对同一日期点取其中修订号最大者：

   ```sql
   WHERE employee_id = ?
     AND effective_from <= :work_date
     AND effective_to   > :work_date   -- 左闭右开
     AND revision_id <= :query_revision
   ORDER BY revision_id DESC, id DESC
   LIMIT 1
   ```
3. **整批原子性**。全部业务校验在写库前完成；修订号分配与区间插入在同一个
   `BEGIN IMMEDIATE` 事务内，失败即回滚，自增修订号不会被失败批次消耗。
4. **半开区间语义**。重叠判定 `a.from < b.to AND b.from < a.to`；首尾相接
   （`a.to = b.from`）不算重叠，`work_date = effective_to` 当天不归属该段。

### 4.2 历史压缩如何做到「删数据而口径不变」

压缩把数据分成两层：

- **保留层**：修订号 `>= B` 的 `attribution_segments` 原始行，原封不动；
- **基线层**：新增 `baseline_segments` 持久表，存放折叠后的有效归属区间，
  每行带 `source_revision_id`（折叠前该日期点真正命中的修订号）。

查询时**保留层优先**，未命中再回退基线层并返回基线行的来源修订号。
因此对任意 `revision_id >= B` 的查询，成本中心、`matched_revision_id`、空档、
半开边界与压缩前完全一致；随机历史的逐日等价性由 `tests/test_compaction.py`
的属性测试（5 个随机种子 × 全年 365 天）守护。

**基线折叠口径**：按 B 口径（`revision_id <= B`）逐日判定胜者，扫描范围覆盖
`<= B` 的全部区间。B 号修订本身是保留修订、不会被删除；它在旧口径无归属处
填补的日期会被折叠进基线，而查询时保留层优先，仍命中真正的 B 段并返回 B 的修订号。
同员工相邻且（成本中心、来源修订号）相同、日期连续的日期合并为半开区间；空档不进基线。

### 4.3 两阶段提交：不阻塞补录、不丢新修订、原子切换

1. **阶段 1 — 只读快照（`BEGIN DEFERRED`，不拿写锁）**：校验 B 现存、读取当前边界、
   逐日计算折叠基线。期间补录事务可照常 `BEGIN IMMEDIATE` 提交，**不被阻塞**。
2. **阶段 2 — 单事务原子提交（`BEGIN IMMEDIATE`）**：在一个写事务内完成
   替换基线 → 删除 `revision_id < B` 的区间与修订 → upsert 保留边界 → 写审计流水。
   要么全部生效，要么完整回滚，**失败或崩溃只留下完整旧态或完整新态**。
   阶段 1 之后到达的补录修订号 `>= B`，不在删除范围内，**绝不会丢失**。

### 4.4 并发、幂等与快照隔离

- **边界单调不倒退**：压缩状态表单行存储 `retention_boundary`；提交事务内复核，
  请求 B 小于当前边界即拒绝（`BOUNDARY_REGRESSION` / `REVISION_EXPIRED`）。
  并发压缩靠 `BEGIN IMMEDIATE` 写锁串行化，后来者要么幂等回放、要么拒绝倒退。
- **同一 B 幂等**：审计表对 `request_key = compaction:{B}` 建唯一约束；
  重复请求返回首条审计记录（`idempotent_replay=true`），不重复改变数据。
- **单次查询单一状态**：归属查询、审计、边界读取都在一个 `DEFERRED` 只读快照事务中
  完成。WAL 模式下快照在事务首条读语句建立，压缩切换对进行中的查询不可见，
  同一次查询只会看到切换前或切换后的同一状态，不会混用。
- **重启持久化**：基线、边界、审计流水与保留修订均在 SQLite（WAL）落盘；
  重启后 `init_db()` 幂等建表，可继续补录与查询。

## 五、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

`ATTRIBUTION_DB_PATH` 可指定 SQLite 文件位置，默认 `./data/attribution.db`。

## 六、错误响应格式

```json
{
  "error": {
    "code": "REVISION_EXPIRED",
    "message": "修订号 1 的口径已过期：历史压缩保留边界为 3 ...",
    "details": {"requested_revision_id": 1, "retention_boundary": 3}
  }
}
```

| code | HTTP 状态码 | 触发条件 |
| --- | --- | --- |
| `INVALID_DATE_RANGE` | 400 | 起日不早于止日 |
| `OVERLAPPING_SEGMENTS` | 400 | 批内同一员工区间重叠 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计/压缩引用了不存在的修订号 |
| `REVISION_EXPIRED` | 410 | 查询或审计了早于保留边界 B、已被折叠删除的修订（含以过期修订作为 B） |
| `BOUNDARY_REGRESSION` | 400 | 压缩请求的 B 小于当前保留边界（边界倒退） |
| Pydantic 校验错误 | 422 | 空批次、字段缺失、日期格式错误等 |
