"""核心业务逻辑：批次校验、修订提交、按修订口径归属查询、历史压缩。

压缩语义（B = 最早保留修订号）：
  - 早于 B 的「有效归属」（即 B-1 口径下每个日期点真正命中的成本中心）
    折叠为持久基线 ``baseline_segments``，并删除修订号 < B 的修订与原始区间；
  - B 及之后的修订号、原始区间完整保留，其归属查询结果（命中修订号、空档、
    半开边界）与压缩前逐字节一致——基线行携带折叠前的 source_revision_id；
  - 早于 B 的口径查询与原始区间审计明确返回 410 REVISION_EXPIRED；
  - 基线在不持有归属写锁的只读阶段计算，提交为单个 BEGIN IMMEDIATE 事务，
    期间到达的补录（修订号 >= B）不会丢失；
  - 保留边界单调不减；同一 B 重复压缩为幂等回放，不重复改变数据。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from sqlite3 import Connection

from .database import get_conn
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    RevisionSubmitRequest,
    SegmentIn,
)

# 尚未发生任何压缩时的边界哨兵。
NO_BOUNDARY = 0


# ---------------- 补录（只追加，保持原语义） ----------------


def validate_batch(entries: list[SegmentIn]) -> None:
    """整批写入前的全部校验。任何一条不通过即抛 AppError，不触碰数据库。

    规则：
      1. 起日必须严格早于止日（左闭右开 [from, to)）；
      2. 同一员工在本批内的区间不得重叠（首尾相接允许：a.to == b.from 不重叠）。
    """
    # 规则 1：起日不早于止日 -> 拒绝（from >= to 非法）
    for idx, e in enumerate(entries):
        if e.effective_from >= e.effective_to:
            raise AppError(
                code="INVALID_DATE_RANGE",
                message=(
                    f"第 {idx + 1} 条记录起日 {e.effective_from.isoformat()} "
                    f"不早于止日 {e.effective_to.isoformat()}，左闭右开区间要求起日严格早于止日"
                ),
                details={
                    "index": idx,
                    "employee_id": e.employee_id,
                    "effective_from": e.effective_from.isoformat(),
                    "effective_to": e.effective_to.isoformat(),
                },
            )

    # 规则 2：批内同员工区间重叠检查（半开区间重叠 <=> a.from < b.to and b.from < a.to）
    by_employee: dict[str, list[tuple[int, SegmentIn]]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_employee[e.employee_id].append((idx, e))

    for employee_id, items in by_employee.items():
        ordered = sorted(items, key=lambda pair: (pair[1].effective_from, pair[1].effective_to))
        for (i_a, a), (i_b, b) in zip(ordered, ordered[1:]):
            if a.effective_from < b.effective_to and b.effective_from < a.effective_to:
                raise AppError(
                    code="OVERLAPPING_SEGMENTS",
                    message=(
                        f"员工 {employee_id} 在本批存在重叠区间："
                        f"第 {i_a + 1} 条 [{a.effective_from.isoformat()}, {a.effective_to.isoformat()}) "
                        f"与第 {i_b + 1} 条 [{b.effective_from.isoformat()}, {b.effective_to.isoformat()})"
                    ),
                    details={
                        "employee_id": employee_id,
                        "indices": [i_a, i_b],
                    },
                )


def submit_revision(req: RevisionSubmitRequest) -> dict:
    """提交一批归属区间，成功返回新的递增修订号。

    修订号分配与区间插入在同一个 BEGIN IMMEDIATE 事务内完成：
    校验失败/插入失败都会回滚，修订号与数据均保持不变。
    压缩只读取/删除旧行，不占用自增序列，因此压缩后补录号继续递增。
    """
    validate_batch(req.entries)

    with get_conn() as conn:
        # 立即拿写锁，避免两个并发批次同时分配到同一个修订号。
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO revisions (entry_count, note) VALUES (?, ?)",
            (len(req.entries), req.note),
        )
        revision_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO attribution_segments
                (revision_id, employee_id, cost_center, effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    e.employee_id,
                    e.cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.entries
            ],
        )
        row = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    return dict(row)


# ---------------- 只读快照与边界 ----------------


def _get_boundary(conn: Connection) -> int:
    """当前保留边界 B；未压缩过返回 0。"""
    row = conn.execute(
        "SELECT retention_boundary FROM compaction_state WHERE id = 1"
    ).fetchone()
    return row["retention_boundary"] if row else NO_BOUNDARY


def _assert_caliber_available(conn: Connection, revision_id: int) -> None:
    """口径可访问性检查：早于保留边界的修订明确报过期（410）。"""
    boundary = _get_boundary(conn)
    if boundary != NO_BOUNDARY and revision_id < boundary:
        raise AppError(
            code="REVISION_EXPIRED",
            message=(
                f"修订号 {revision_id} 的口径已过期：历史压缩保留边界为 {boundary}，"
                f"早于该边界的修订已折叠为持久基线并删除原始数据；"
                f"请使用修订号 >= {boundary} 的保留口径查询，或通过 /api/baseline/segments 审计基线"
            ),
            status_code=410,
            details={
                "requested_revision_id": revision_id,
                "retention_boundary": boundary,
            },
        )


def _lookup_one(
    conn: Connection, revision_id: int, employee_id: str, work_date_iso: str
) -> tuple[str | None, int | None]:
    """在 revision_id 口径下定位单条工时的成本中心。

    可见数据由两层组成（同一连接、同一快照事务内读取，保证整次查询状态一致）：
      1. 保留层：``revision_id <= 给定修订号`` 的现存原始区间，同日期点取修订号最大者；
      2. 基线层：折叠早于 B 的有效归属得到的持久区间，仅当给定修订号 >= B 时可见。

    保留层优先（它天然覆盖基线所表达的旧口径）；保留层未命中时回退基线，
    并返回基线行记录的折叠前命中修订号 source_revision_id。
    区间外/空档则未归属。半开边界与压缩前完全一致。
    """
    row = conn.execute(
        """
        SELECT cost_center, revision_id AS matched_revision_id
        FROM attribution_segments
        WHERE employee_id = ?
          AND effective_from <= ?
          AND effective_to   > ?
          AND revision_id <= ?
        ORDER BY revision_id DESC, id DESC
        LIMIT 1
        """,
        (employee_id, work_date_iso, work_date_iso, revision_id),
    ).fetchone()
    if row is not None:
        return row["cost_center"], row["matched_revision_id"]

    # 保留层无命中：回退持久基线（基线只对保留口径可见，边界检查已在调用处完成）。
    row = conn.execute(
        """
        SELECT cost_center, source_revision_id AS matched_revision_id
        FROM baseline_segments
        WHERE employee_id = ?
          AND effective_from <= ?
          AND effective_to   > ?
        ORDER BY source_revision_id DESC, id DESC
        LIMIT 1
        """,
        (employee_id, work_date_iso, work_date_iso),
    ).fetchone()
    if row is None:
        return None, None
    return row["cost_center"], row["matched_revision_id"]


def attribute_hours(req: AttributionQueryRequest) -> list[AttributionResult]:
    """逐条返回工时记录在指定修订口径下的成本中心（或未归属）。

    整个请求在一个 DEFERRED 只读快照事务中执行：压缩切换要么尚未发生、
    要么已经完成，同一次查询绝不会混用切换前后的两个状态。
    """
    with get_conn(begin="DEFERRED") as conn:
        _assert_caliber_available(conn, req.revision_id)

        max_row = conn.execute(
            "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
        ).fetchone()
        if req.revision_id > max_row["r"]:
            boundary = _get_boundary(conn)
            hint = f"，当前保留边界为 {boundary}" if boundary else ""
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {req.revision_id} 不存在，当前最大修订号为 {max_row['r']}{hint}",
                status_code=404,
            )

        results: list[AttributionResult] = []
        for rec in req.records:
            cost_center, matched = _lookup_one(
                conn, req.revision_id, rec.employee_id, rec.work_date.isoformat()
            )
            results.append(
                AttributionResult(
                    employee_id=rec.employee_id,
                    work_date=rec.work_date,
                    hours=rec.hours,
                    revision_id=req.revision_id,
                    cost_center=cost_center,
                    attributed=cost_center is not None,
                    matched_revision_id=matched,
                )
            )
    return results


def list_revisions() -> list[dict]:
    """列出全部现存修订号（被压缩删除的旧修订不再出现）。"""
    with get_conn(begin="DEFERRED") as conn:
        rows = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions ORDER BY revision_id"
        ).fetchall()
    return [dict(r) for r in rows]


def list_segments(revision_id: int, employee_id: str | None) -> list[dict]:
    """查看某修订号提交的原始区间（审计）。

    保留修订（>= B）的原始区间完整可审计；早于 B 的修订已被物理删除，
    明确返回 410 REVISION_EXPIRED 并引导到基线审计接口。
    """
    with get_conn(begin="DEFERRED") as conn:
        _assert_caliber_available(conn, revision_id)
        exists = conn.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if exists is None:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {revision_id} 不存在",
                status_code=404,
            )
        if employee_id:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ? AND employee_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id, employee_id.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------- 历史压缩 ----------------


def _compute_baseline(
    conn: Connection, boundary: int
) -> list[tuple[str, str, str, int]]:
    """只读阶段：计算折叠基线，全程不获取任何写锁（不阻塞补录）。

    基线表达「紧接保留边界 B 之前的完整有效归属状态」，即 B 口径下、
    由修订号 < B 的数据（含更早压缩留下的旧基线）决定的逐日胜者：

    - 按 **B 口径**扫描（``revision_id <= B``），但只接受来源修订号 < B 的胜者。
      B 自身拥有且未被后续修订覆盖的日期同样会被折叠——否则这些日期在
      删除旧行后将错误地变成空档；
    - 对每个员工在其全部旧区间覆盖的日期范围内逐日判定真正命中的成本中心
      （与 _lookup_one 相同的胜者规则），再把相邻且
      (cost_center, source_revision_id) 相同、日期连续的日期合并成半开区间；
    - 空档（当日无任何旧归属）不进基线，保留为空档语义。

    第二次及以后压缩时，旧基线（表达更早边界之前的口径）同样参与逐日判定，
    因此重复压缩只会推进边界、不会丢失更早之前折叠好的归属。

    返回行：(employee_id, cost_center, effective_from, source_revision_id, effective_to)
    """
    # 收集需要折叠判定的员工与日期范围：
    # 1) revision_id <= B 的原始区间（含 B 自身——B 拥有、未被后续覆盖的日期也要折叠）；
    # 2) 既有基线（更早压缩折叠的归属，其原始修订可能已删除，必须纳入否则会丢历史）。
    range_rows = conn.execute(
        """
        SELECT employee_id,
               MIN(effective_from) AS min_from,
               MAX(effective_to)   AS max_to
        FROM (
            SELECT employee_id, effective_from, effective_to
            FROM attribution_segments
            WHERE revision_id <= ?
            UNION ALL
            SELECT employee_id, effective_from, effective_to
            FROM baseline_segments
        )
        GROUP BY employee_id
        """,
        (boundary,),
    ).fetchall()

    baseline_rows: list[tuple[str, str, str, int]] = []

    for rr in range_rows:
        employee_id = rr["employee_id"]
        day = date.fromisoformat(rr["min_from"])
        end = date.fromisoformat(rr["max_to"])

        # 逐日扫描（ISO 日期串字典序即时间序，可直接参数化比较）。
        # 以 (cc, src_rev, start, end_excl) 形式归并相邻同口径日期。
        current: tuple[str, int, date, date] | None = None
        while day < end:
            day_iso = day.isoformat()
            # 按 B 口径（revision_id <= B）取胜者。B 号修订本身是保留修订、
            # 不会被删除：它的段在旧口径（<B）无归属时填补日期，使折叠后的基线
            # 与「紧接 B 之前的完整有效归属状态」一致；查询时保留层优先，
            # 这些日期仍会命中真正的 B 段并返回 B 的修订号。
            winner = conn.execute(
                """
                SELECT cost_center, revision_id AS matched_revision_id
                FROM attribution_segments
                WHERE employee_id = ?
                  AND effective_from <= ?
                  AND effective_to   > ?
                  AND revision_id <= ?
                ORDER BY revision_id DESC, id DESC
                LIMIT 1
                """,
                (employee_id, day_iso, day_iso, boundary),
            ).fetchone()
            if winner is None:
                # 旧基线表达更早压缩折叠的有效归属，仅当原始旧层当日无归属时兜底。
                winner = conn.execute(
                    """
                    SELECT cost_center, source_revision_id AS matched_revision_id
                    FROM baseline_segments
                    WHERE employee_id = ?
                      AND effective_from <= ?
                      AND effective_to   > ?
                    ORDER BY source_revision_id DESC, id DESC
                    LIMIT 1
                    """,
                    (employee_id, day_iso, day_iso),
                ).fetchone()

            next_day = day + timedelta(days=1)
            if winner is None:
                # 空档：结束当前段，跳过该日（空档不进基线，保留为空档语义）。
                if current is not None:
                    baseline_rows.append(
                        (employee_id, current[0], current[2].isoformat(), current[1], current[3].isoformat())
                    )
                    current = None
            else:
                cc = winner["cost_center"]
                src = winner["matched_revision_id"]
                # 仅当与前一归属日**日期连续**且同口径时才延长当前段；
                # current[3] != day 意味着中间隔了空档，必须另起一段（空档不进基线）。
                if (
                    current is not None
                    and current[0] == cc
                    and current[1] == src
                    and current[3] == day
                ):
                    # 与前一日同口径且相邻：延长当前段。
                    current = (cc, src, current[2], next_day)
                else:
                    if current is not None:
                        baseline_rows.append(
                            (employee_id, current[0], current[2].isoformat(), current[1], current[3].isoformat())
                        )
                    current = (cc, src, day, next_day)
            day = next_day

        if current is not None:
            baseline_rows.append(
                (employee_id, current[0], current[2].isoformat(), current[1], current[3].isoformat())
            )

    baseline_rows.sort(key=lambda r: (r[0], r[2]))
    return baseline_rows


def _insert_baseline(conn: Connection, baseline_rows: list[tuple[str, str, str, int]]) -> None:
    """把折叠基线写入事务（独立函数，便于测试注入失败验证原子回滚）。"""
    conn.executemany(
        """
        INSERT INTO baseline_segments
            (employee_id, cost_center, effective_from, effective_to, source_revision_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (emp, cc, from_iso, to_iso, src)
            for emp, cc, from_iso, src, to_iso in baseline_rows
        ],
    )


def compact_history(retain_from_revision: int) -> dict:
    """把早于 B 的有效归属折叠为持久基线，并原子删除旧修订与原始区间。

    两阶段：
      阶段 1（只读快照，不持写锁）：校验 B 现存、读取当前边界、计算基线。
              期间补录可继续提交，不被阻塞；
      阶段 2（单个 BEGIN IMMEDIATE 事务）：复核 B 仍现存且边界不倒退，
              写入基线、删除旧行、推进边界、写审计流水，全部一起生效或一起回滚。

    幂等：同一 B 的重复请求返回首次压缩的审计行（idempotent_replay=true），
    不重复改变任何数据。
    """
    boundary = retain_from_revision
    request_key = f"compaction:{boundary}"

    # ---------- 阶段 1：只读计算（DEFERRED 快照，不占用归属写锁） ----------
    with get_conn(begin="DEFERRED") as ro_conn:
        prior_boundary = _get_boundary(ro_conn)

        existing = ro_conn.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?", (boundary,)
        ).fetchone()
        if existing is None:
            # 区分「已过期被删」与「从未存在」，给出明确错误。
            if prior_boundary != NO_BOUNDARY and boundary < prior_boundary:
                raise AppError(
                    code="REVISION_EXPIRED",
                    message=(
                        f"修订号 {boundary} 已在之前的压缩中过期删除，"
                        f"当前保留边界为 {prior_boundary}，不能以更早的修订号作为保留口径"
                    ),
                    status_code=410,
                    details={
                        "requested_revision_id": boundary,
                        "retention_boundary": prior_boundary,
                    },
                )
            max_row = ro_conn.execute(
                "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
            ).fetchone()
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=(
                    f"修订号 {boundary} 不存在，无法作为最早保留口径；"
                    f"当前最大修订号为 {max_row['r']}"
                ),
                status_code=404,
            )

        if boundary < prior_boundary:
            # 边界绝不倒退（理论上被上面的过期分支覆盖，这里双保险）。
            raise AppError(
                code="BOUNDARY_REGRESSION",
                message=(
                    f"压缩边界不能倒退：请求保留边界 {boundary}，"
                    f"当前保留边界已为 {prior_boundary}"
                ),
                details={
                    "requested_boundary": boundary,
                    "current_boundary": prior_boundary,
                },
            )

        # 幂等快路径：同一 B 已压缩过，直接回放，不改动数据。
        prior_run = ro_conn.execute(
            "SELECT * FROM compaction_runs WHERE request_key = ?", (request_key,)
        ).fetchone()
        if prior_run is not None:
            return _run_out(dict(prior_run), idempotent_replay=True)

        # 纯读取的基线计算：不拿写锁，补录事务可同时进行。
        baseline_rows = _compute_baseline(ro_conn, boundary)

    # ---------- 阶段 2：原子提交（IMMEDIATE 写锁，临界区尽量短） ----------
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")

        # 提交时复核：并发压缩可能已经推进过边界。
        current_boundary = _get_boundary(conn)
        race_run = conn.execute(
            "SELECT * FROM compaction_runs WHERE request_key = ?", (request_key,)
        ).fetchone()
        if race_run is not None:
            # 同一 B 被并发压缩抢先提交：回放，本次不写入。
            conn.rollback()
            return _run_out(dict(race_run), idempotent_replay=True)
        if boundary < current_boundary:
            conn.rollback()
            raise AppError(
                code="BOUNDARY_REGRESSION",
                message=(
                    f"压缩边界不能倒退：请求 {boundary}，"
                    f"计算期间边界已被并发压缩推进到 {current_boundary}"
                ),
                details={
                    "requested_boundary": boundary,
                    "current_boundary": current_boundary,
                },
            )

        # B 必须仍现存（计算期间它不可能被删——只有压缩会删行，而压缩只能删 < B 的行）。
        still_exists = conn.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?", (boundary,)
        ).fetchone()
        if still_exists is None:
            conn.rollback()
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {boundary} 在压缩提交时已不存在",
                status_code=404,
            )

        # 1) 替换持久基线：旧基线已被纳入新基线的计算，整体替换。
        conn.execute("DELETE FROM baseline_segments")
        _insert_baseline(conn, baseline_rows)

        # 2) 删除早于 B 的原始区间与修订（实际删除行数被审计记录）。
        cur_del_seg = conn.execute(
            "DELETE FROM attribution_segments WHERE revision_id < ?", (boundary,)
        )
        segments_deleted = cur_del_seg.rowcount
        cur_del_rev = conn.execute(
            "DELETE FROM revisions WHERE revision_id < ?", (boundary,)
        )
        revisions_deleted = cur_del_rev.rowcount

        # 3) 推进/建立保留边界（单行 upsert，单调不减由上面的检查保证）。
        conn.execute(
            """
            INSERT INTO compaction_state (id, retention_boundary)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                retention_boundary = excluded.retention_boundary,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (boundary,),
        )

        # 4) 审计流水（request_key 唯一约束兜底幂等）。
        kept_row = conn.execute(
            "SELECT COUNT(*) AS c FROM revisions WHERE revision_id >= ?", (boundary,)
        ).fetchone()
        cur_run = conn.execute(
            """
            INSERT INTO compaction_runs
                (request_key, retention_boundary, prior_boundary,
                 baseline_inserted, segments_deleted, revisions_deleted,
                 new_revisions_kept, idempotent_replay)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                request_key,
                boundary,
                None if prior_boundary == NO_BOUNDARY else prior_boundary,
                len(baseline_rows),
                segments_deleted,
                revisions_deleted,
                kept_row["c"],
            ),
        )
        run_id = cur_run.lastrowid
        row = conn.execute(
            "SELECT * FROM compaction_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    return _run_out(dict(row), idempotent_replay=False)


def _run_out(row: dict, idempotent_replay: bool) -> dict:
    row = dict(row)
    row["idempotent_replay"] = bool(idempotent_replay)
    return row


def get_boundary_info() -> dict:
    """保留边界及删除行数查询。"""
    with get_conn(begin="DEFERRED") as conn:
        state = conn.execute(
            "SELECT * FROM compaction_state WHERE id = 1"
        ).fetchone()
        agg_runs = conn.execute(
            """
            SELECT COUNT(*) AS total_runs,
                   COALESCE(SUM(segments_deleted), 0)  AS total_segments_deleted,
                   COALESCE(SUM(revisions_deleted), 0) AS total_revisions_deleted,
                   COALESCE(MAX(run_id), NULL)         AS latest_run_id
            FROM compaction_runs
            """
        ).fetchone()
        baseline_count = conn.execute(
            "SELECT COUNT(*) AS c FROM baseline_segments"
        ).fetchone()["c"]

    if state is None:
        return {
            "retention_boundary": None,
            "compacted": False,
            "total_runs": 0,
            "total_baseline_segments": 0,
            "total_segments_deleted": 0,
            "total_revisions_deleted": 0,
            "latest_run_id": None,
            "updated_at": None,
        }

    return {
        "retention_boundary": state["retention_boundary"],
        "compacted": True,
        "total_runs": agg_runs["total_runs"],
        "total_baseline_segments": baseline_count,
        "total_segments_deleted": agg_runs["total_segments_deleted"],
        "total_revisions_deleted": agg_runs["total_revisions_deleted"],
        "latest_run_id": agg_runs["latest_run_id"],
        "updated_at": state["updated_at"],
    }


def list_compaction_runs() -> list[dict]:
    """压缩审计流水。"""
    with get_conn(begin="DEFERRED") as conn:
        rows = conn.execute(
            "SELECT * FROM compaction_runs ORDER BY run_id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["idempotent_replay"] = bool(d["idempotent_replay"])
        out.append(d)
    return out


def list_baseline_segments(employee_id: str | None) -> list[dict]:
    """持久基线审计：折叠后的区间及其折叠前命中修订号。"""
    with get_conn(begin="DEFERRED") as conn:
        if employee_id:
            rows = conn.execute(
                """
                SELECT employee_id, cost_center, effective_from, effective_to, source_revision_id
                FROM baseline_segments
                WHERE employee_id = ?
                ORDER BY employee_id, effective_from
                """,
                (employee_id.strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT employee_id, cost_center, effective_from, effective_to, source_revision_id
                FROM baseline_segments
                ORDER BY employee_id, effective_from
                """
            ).fetchall()
    return [dict(r) for r in rows]
