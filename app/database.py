"""SQLite 连接与建表。

普通补录路径保持只追加（append-only）：``revisions`` / ``attribution_segments``
的写入只 INSERT，不 UPDATE/DELETE，因此任一历史修订的可见数据在压缩前永久不变。

历史压缩是唯一的重写路径，由三张额外的表支撑（均随压缩事务原子切换）：

- ``baseline_segments``：把早于保留边界 B 的「有效归属」折叠成的持久基线。
  基线行带有折叠前真正命中的 ``source_revision_id``，保留口径查询结果因此完全不变；
- ``compaction_state``：单行表，记录当前保留边界（单调不减，绝不倒退）；
- ``compaction_runs``：每次压缩的审计记录，包含基线/删除行数与幂等请求标记。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 允许通过环境变量指定数据库文件路径（测试与 Docker 都会用到）。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "attribution.db"


def get_db_path() -> Path:
    return Path(os.environ.get("ATTRIBUTION_DB_PATH", str(DEFAULT_DB_PATH)))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；外键约束开启。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn(begin: str | None = None) -> Iterator[sqlite3.Connection]:
    """请求级连接：正常结束提交，异常回滚并关闭。

    ``begin`` 取值：
      - ``None``（默认）：不主动开启事务，依赖 sqlite3 模块的隐式事务，
        兼容历史调用方式（``submit_revision`` 内部自行 BEGIN IMMEDIATE）；
      - ``"DEFERRED"``：显式开启只读快照事务。WAL 模式下首个读语句建立
        MVCC 快照，事务期间看到的状态不会被其他连接的提交改变——
        保证一次归属查询只使用切换前或切换后的同一状态；
      - ``"IMMEDIATE"``：立即获取 RESERVED 写锁，用于压缩提交等临界区。
    """
    conn = _connect(get_db_path())
    try:
        if begin is not None:
            conn.execute(f"BEGIN {begin}")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS revisions (
    revision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count      INTEGER NOT NULL,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS attribution_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES revisions(revision_id),
    employee_id     TEXT    NOT NULL,
    cost_center     TEXT    NOT NULL,
    effective_from  TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间左端点（含）
    effective_to    TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间右端点（不含）
    CHECK (effective_from < effective_to)
);

-- 查询路径：按员工 + 日期点 + 修订号过滤后取最大修订号
CREATE INDEX IF NOT EXISTS idx_segments_emp_dates_rev
    ON attribution_segments (employee_id, effective_from, effective_to, revision_id);

CREATE INDEX IF NOT EXISTS idx_segments_revision
    ON attribution_segments (revision_id);

-- 持久基线：早于保留边界 B 的有效归属折叠结果。
-- source_revision_id 保留折叠前真正命中的修订号，审计口径不丢。
CREATE TABLE IF NOT EXISTS baseline_segments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id          TEXT    NOT NULL,
    cost_center          TEXT    NOT NULL,
    effective_from       TEXT    NOT NULL,
    effective_to         TEXT    NOT NULL,
    source_revision_id   INTEGER NOT NULL,
    CHECK (effective_from < effective_to)
);

CREATE INDEX IF NOT EXISTS idx_baseline_emp_dates
    ON baseline_segments (employee_id, effective_from, effective_to);

-- 压缩状态：单行表（id 恒为 1）。retention_boundary 单调不减。
CREATE TABLE IF NOT EXISTS compaction_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    retention_boundary  INTEGER NOT NULL,   -- 最早保留修订号 B
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 压缩审计流水：每次压缩一条，重复请求以 request_key 去重（幂等）。
CREATE TABLE IF NOT EXISTS compaction_runs (
    run_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key           TEXT    NOT NULL UNIQUE,  -- 固定为 compaction:{B}
    retention_boundary    INTEGER NOT NULL,
    prior_boundary        INTEGER,                  -- 本次压缩前的边界（首次为 NULL）
    baseline_inserted     INTEGER NOT NULL,
    segments_deleted      INTEGER NOT NULL,
    revisions_deleted     INTEGER NOT NULL,
    new_revisions_kept    INTEGER NOT NULL,         -- 计算期间允许补录：提交时 >= B 的修订数
    idempotent_replay     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def init_db() -> None:
    """创建表与索引（幂等，重启后可继续补录与查询）。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
