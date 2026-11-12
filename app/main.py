"""FastAPI 入口：归属修订 API 路由与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .database import init_db
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    BaselineQueryRequest,
    BaselineSegmentOut,
    CompactionBoundaryOut,
    CompactionRequest,
    CompactionRunOut,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
)
from .service import (
    attribute_hours,
    compact_history,
    get_boundary_info,
    list_baseline_segments,
    list_compaction_runs,
    list_revisions,
    list_segments,
    submit_revision,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表结构存在（幂等）；重启后基线、边界与保留修订均在盘上，可继续补录与查询。
    init_db()
    yield


app = FastAPI(
    title="工时归属修订与历史压缩 API",
    description=(
        "人事调岗补录导致的工时归属修正服务，支持过期人事归属历史压缩。\n\n"
        "- 每批成功提交返回递增修订号，批次内校验失败则整批拒绝；\n"
        "- 生效日期区间为左闭右开 `[effective_from, effective_to)`，允许空档；\n"
        "- 按修订号查询时只可见该修订号及之前的数据，后续跨段补录不改变旧修订查询结果；\n"
        "- **历史压缩**：指定现存修订 B 为最早保留口径，早于 B 的有效归属折叠为持久基线，"
        "旧修订与原始区间物理删除；B 及之后的查询（命中修订号、空档、半开边界）保持不变，"
        "早于 B 的查询/审计明确返回 410 过期；\n"
        "- 基线计算不占用归属写锁（补录可继续），提交为单事务原子切换，"
        "边界单调不倒退，同一 B 重复压缩幂等。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/revisions",
    response_model=RevisionOut,
    status_code=201,
    tags=["归属修订"],
    summary="提交一批员工-成本中心归属区间",
    description=(
        "整批校验通过后写入，返回递增修订号。\n\n"
        "拒绝条件（整批回滚，数据与修订号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 批内同一员工的区间相互重叠（首尾相接不算重叠）。\n\n"
        "压缩期间仍可正常补录：基线计算不持有写锁，提交事务不会丢失期间的新修订。"
    ),
)
def create_revision(req: RevisionSubmitRequest) -> RevisionOut:
    return RevisionOut(**submit_revision(req))


@app.get("/api/revisions", tags=["归属修订"], summary="列出全部现存修订号")
def get_revisions() -> list[RevisionOut]:
    return [RevisionOut(**row) for row in list_revisions()]


@app.post(
    "/api/attribution/query",
    response_model=list[AttributionResult],
    tags=["归属查询"],
    summary="按指定修订号的口径逐条查询工时归属",
    description=(
        "对每条工时记录返回成本中心或未归属。只可见 `revision_id` 及之前的补录；"
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。\n\n"
        "压缩后：保留口径（>= 边界 B）结果与压缩前完全一致（含命中修订号、空档、半开边界）；"
        "早于 B 的口径返回 `410 REVISION_EXPIRED`。每次查询在单一快照事务中执行，"
        "只会看到切换前或切换后的同一状态。"
    ),
)
def query_attribution(req: AttributionQueryRequest) -> list[AttributionResult]:
    return attribute_hours(req)


@app.post(
    "/api/revisions/{revision_id}/segments",
    tags=["归属修订"],
    summary="查看某个修订号下提交的原始区间（审计）",
    description=(
        "保留修订（>= 边界 B）的原始区间完整可审计；"
        "早于 B 的修订已物理删除，返回 `410 REVISION_EXPIRED`，请改用基线审计接口。"
    ),
)
def get_revision_segments(revision_id: int, req: SegmentsQueryRequest | None = None) -> list[dict]:
    employee_id = req.employee_id if req else None
    return list_segments(revision_id, employee_id)


# ---------------- 历史压缩 ----------------


@app.post(
    "/api/compactions",
    response_model=CompactionRunOut,
    status_code=200,
    tags=["历史压缩"],
    summary="压缩早于保留边界 B 的归属历史",
    description=(
        "指定**现存修订 B** 为最早保留口径：\n\n"
        "1. 只读阶段计算折叠基线（不占用归属写锁，补录可继续，不丢期间新修订）；\n"
        "2. 单个 `BEGIN IMMEDIATE` 事务内原子完成：写入持久基线、删除修订号 < B 的"
        "原始区间与修订、推进保留边界、写审计流水——要么全部生效要么完整回滚；\n"
        "3. B 及之后的归属查询与原始区间审计保持不变；早于 B 的查询/审计返回 410；\n"
        "4. 边界单调不倒退；同一 B 重复请求幂等回放（`idempotent_replay=true`，不重复改数）。"
    ),
)
def create_compaction(req: CompactionRequest) -> CompactionRunOut:
    return CompactionRunOut(**compact_history(req.retain_from_revision))


@app.get(
    "/api/compactions/boundary",
    response_model=CompactionBoundaryOut,
    tags=["历史压缩"],
    summary="查询保留边界与累计删除行数",
)
def get_compaction_boundary() -> CompactionBoundaryOut:
    return CompactionBoundaryOut(**get_boundary_info())


@app.get(
    "/api/compactions",
    response_model=list[CompactionRunOut],
    tags=["历史压缩"],
    summary="压缩审计流水（每次压缩的基线/删除行数）",
)
def get_compaction_runs() -> list[CompactionRunOut]:
    return [CompactionRunOut(**row) for row in list_compaction_runs()]


@app.post(
    "/api/baseline/segments",
    response_model=list[BaselineSegmentOut],
    tags=["历史压缩"],
    summary="审计持久基线（折叠区间及折叠前命中修订号）",
    description=(
        "返回基线中的折叠区间。每条基线行携带 `source_revision_id`，"
        "即折叠前该日期点真正命中的修订号；同员工相邻且成本中心/来源修订相同的日期已合并。"
    ),
)
def get_baseline_segments(req: BaselineQueryRequest | None = None) -> list[BaselineSegmentOut]:
    employee_id = req.employee_id if req else None
    return [BaselineSegmentOut(**row) for row in list_baseline_segments(employee_id)]
