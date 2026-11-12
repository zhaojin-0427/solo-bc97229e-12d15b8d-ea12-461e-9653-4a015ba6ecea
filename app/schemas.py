"""Pydantic 请求/响应模型。

日期统一使用 ISO 8601（yyyy-mm-dd），SQLite 内以 TEXT 存储；
ISO 日期字符串的字典序与时间序一致，可直接做半开区间比较。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class SegmentIn(BaseModel):
    """人事导出的一条员工-成本中心生效区间（左闭右开）。"""

    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2026-04-01"])

    @field_validator("employee_id", "cost_center")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class RevisionSubmitRequest(BaseModel):
    """一批补录/归属区间。整批要么全部成功，要么整批拒绝。"""

    entries: list[SegmentIn] = Field(..., min_length=1, description="本批归属区间（至少一条）")
    note: str | None = Field(default=None, description="备注，如人事调岗补录批次号", examples=["HR 调岗补录 2026-11"])


class RevisionOut(BaseModel):
    revision_id: int
    entry_count: int
    note: str | None
    created_at: str


class WorkHourRecord(BaseModel):
    """工时系统导出的一条工时记录。"""

    employee_id: str = Field(..., min_length=1, examples=["E001"])
    work_date: date = Field(..., description="工作日期", examples=["2026-02-15"])
    hours: float = Field(..., description="小时数（原样回显，不参与归属判断）", examples=[8.0])

    @field_validator("employee_id")
    @classmethod
    def _strip_employee(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AttributionQueryRequest(BaseModel):
    """按指定修订号的口径查询工时归属。"""

    revision_id: int = Field(..., ge=0, description="月结口径修订号；0 表示尚无任何补录的空口径")
    records: list[WorkHourRecord] = Field(..., min_length=1, description="待归属的工时记录")


class AttributionResult(BaseModel):
    employee_id: str
    work_date: date
    hours: float
    revision_id: int
    cost_center: str | None = Field(..., description="成本中心；None 表示未归属")
    attributed: bool
    matched_revision_id: int | None = Field(..., description="命中区间所属的修订号，便于审计")


class SegmentsQueryRequest(BaseModel):
    employee_id: str | None = Field(default=None, description="可选，按员工过滤")


# ---------------- 历史压缩 ----------------


class CompactionRequest(BaseModel):
    """历史压缩请求：指定现存修订 B 作为最早保留口径。"""

    retain_from_revision: int = Field(
        ...,
        ge=1,
        description="最早保留修订号 B：必须是现存修订；早于 B 的有效归属折叠为持久基线",
        examples=[3],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "retain_from_revision": 3,
                }
            ]
        }
    }


class CompactionRunOut(BaseModel):
    """一次压缩的结果（重复请求返回同一条，不重复改变数据）。"""

    run_id: int
    request_key: str
    retention_boundary: int = Field(..., description="压缩后生效的保留边界 B")
    prior_boundary: int | None = Field(..., description="压缩前的保留边界；首次压缩为 null")
    baseline_inserted: int = Field(..., description="本次写入的持久基线区间行数")
    segments_deleted: int = Field(..., description="本次实际删除的旧修订原始区间行数")
    revisions_deleted: int = Field(..., description="本次实际删除的旧修订行数")
    new_revisions_kept: int = Field(
        ..., description="压缩计算期间继续补录、提交时一并保留的修订数量（>= B）"
    )
    idempotent_replay: bool = Field(..., description="是否为同一 B 的重复请求（未再改动数据）")
    created_at: str


class CompactionBoundaryOut(BaseModel):
    """保留边界与累计删除行数查询。"""

    retention_boundary: int | None = Field(
        ..., description="当前最早保留修订号；尚未压缩过为 null（所有修订均可查）"
    )
    compacted: bool
    total_runs: int = Field(..., description="累计压缩次数（重复请求不计数）")
    total_baseline_segments: int = Field(..., description="当前持久基线区间行数")
    total_segments_deleted: int = Field(..., description="累计删除的旧修订原始区间行数")
    total_revisions_deleted: int = Field(..., description="累计删除的旧修订行数")
    latest_run_id: int | None
    updated_at: str | None


class BaselineSegmentOut(BaseModel):
    """持久基线中的一条折叠区间（含折叠前命中的修订号，可审计）。"""

    employee_id: str
    cost_center: str
    effective_from: date
    effective_to: date
    source_revision_id: int = Field(
        ..., description="该基线区间折叠前真正命中的修订号；同员工相邻同口径段已合并"
    )


class BaselineQueryRequest(BaseModel):
    employee_id: str | None = Field(default=None, description="可选，按员工过滤")
