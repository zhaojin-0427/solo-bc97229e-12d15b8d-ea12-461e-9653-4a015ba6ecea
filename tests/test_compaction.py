"""历史压缩端到端测试：基线折叠、保留口径不变、过期报错、幂等与并发安全。

运行：ATTRIBUTION_DB_PATH 指向临时文件后 pytest。
"""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


def _rev(client, entries, note=None):
    body = {"entries": entries}
    if note:
        body["note"] = note
    r = client.post("/api/revisions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["revision_id"]


def _query(client, revision_id, employee_id, work_date):
    r = client.post(
        "/api/attribution/query",
        json={
            "revision_id": revision_id,
            "records": [{"employee_id": employee_id, "work_date": work_date, "hours": 8}],
        },
    )
    return r


def _build_three_revisions(client):
    """构造带覆盖、空档、半开边界的三修订历史。

    E1:
      rev1: [2026-01-01, 2026-07-01) -> CC-OLD
      rev2: [2026-04-01, 2026-07-01) -> CC-MID  （覆盖 rev1 的 4~6 月）
      rev3: [2026-06-01, 2026-07-01) -> CC-NEW  （保留边界 B=3；7 月起为真实空档）
    E2:
      rev1: [2026-01-01, 2026-03-01) -> CC-E2
    """
    r1 = _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-OLD",
         "effective_from": "2026-01-01", "effective_to": "2026-07-01"},
        {"employee_id": "E2", "cost_center": "CC-E2",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    ], note="初始归属")
    r2 = _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-MID",
         "effective_from": "2026-04-01", "effective_to": "2026-07-01"},
    ], note="四月调岗")
    r3 = _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-NEW",
         "effective_from": "2026-06-01", "effective_to": "2026-07-01"},
    ], note="六月再调岗")
    assert (r1, r2, r3) == (1, 2, 3)
    return r1, r2, r3


# 一组跨边界、覆盖/空档/半开边界的探针日期。
PROBE_DATES = [
    "2026-01-01",  # 基线区，rev1 左端点（含）
    "2026-02-15",  # 基线区，rev1
    "2026-03-31",  # 基线区，rev1 末日
    "2026-04-01",  # 基线区，rev2 覆盖左端点（含），半开边界
    "2026-05-15",  # 基线区，rev2 覆盖
    "2026-05-31",  # 基线区，rev2 末日
    "2026-06-01",  # 保留区，rev3 左端点（含）
    "2026-06-30",  # 保留区，rev3 末日
    "2026-07-01",  # 半开：所有区间右端点（不含）-> 真实空档
    "2026-09-01",  # 全部区间之外 -> 未归属
]


def _full_caliber(client, revision_id):
    r = client.post("/api/attribution/query", json={
        "revision_id": revision_id,
        "records": [
            {"employee_id": "E1", "work_date": d, "hours": 8} for d in PROBE_DATES
        ] + [
            {"employee_id": "E2", "work_date": "2026-02-10", "hours": 8},
            {"employee_id": "E2", "work_date": "2026-03-01", "hours": 8},  # 半开右端点 -> 空档
            {"employee_id": "E9", "work_date": "2026-02-10", "hours": 8},  # 未知员工
        ],
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1. 保留口径查询与压缩前完全一致 ----------

def test_retained_calibers_unchanged_after_compaction(client):
    _build_three_revisions(client)

    before_rev3 = _full_caliber(client, 3)

    r = client.post("/api/compactions", json={"retain_from_revision": 3})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["retention_boundary"] == 3
    assert run["idempotent_replay"] is False
    assert run["segments_deleted"] == 3          # rev1 两条 + rev2 一条
    assert run["revisions_deleted"] == 2
    assert run["baseline_inserted"] >= 2         # E1 折叠段 + E2 折叠段
    assert run["new_revisions_kept"] == 1        # 仅剩 rev3

    # 保留口径（B 及之后）逐字节不变：命中修订号、空档、半开边界、未归属全部保持。
    assert _full_caliber(client, 3) == before_rev3
    # rev2 < B，属于过期口径，不再可查（见 test_expired_caliber_and_segments_return_410）
    assert _query(client, 2, "E1", "2026-02-15").status_code == 410

    # 关键命中点断言（含 matched_revision_id 不变）
    def hit(rev_id, d):
        return _query(client, rev_id, "E1", d).json()[0]

    assert hit(3, "2026-02-15")["matched_revision_id"] == 1
    assert hit(3, "2026-02-15")["cost_center"] == "CC-OLD"
    assert hit(3, "2026-05-15")["matched_revision_id"] == 2
    assert hit(3, "2026-05-15")["cost_center"] == "CC-MID"
    assert hit(3, "2026-06-01")["matched_revision_id"] == 3
    assert hit(3, "2026-06-01")["cost_center"] == "CC-NEW"
    # 半开右端点 = 空档
    gap = hit(3, "2026-07-01")
    assert gap["attributed"] is False and gap["cost_center"] is None
    assert gap["matched_revision_id"] is None


# ---------- 2. 早于 B 的查询与审计明确报过期 ----------

def test_expired_caliber_and_segments_return_410(client):
    _build_three_revisions(client)
    client.post("/api/compactions", json={"retain_from_revision": 3})

    r = _query(client, 1, "E1", "2026-02-15")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "REVISION_EXPIRED"
    assert r.json()["error"]["details"]["retention_boundary"] == 3

    r2 = _query(client, 2, "E1", "2026-02-15")
    assert r2.status_code == 410

    # 原始区间审计：保留修订可审计，过期修订 410
    ok = client.post("/api/revisions/3/segments", json={})
    assert ok.status_code == 200
    assert len(ok.json()) == 1 and ok.json()[0]["cost_center"] == "CC-NEW"

    gone = client.post("/api/revisions/1/segments", json={})
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "REVISION_EXPIRED"

    # 现存修订列表里旧修订已消失，修订号不重排
    ids = [x["revision_id"] for x in client.get("/api/revisions").json()]
    assert ids == [3]


# ---------- 3. 幂等：同一 B 重复操作不重复改变数据 ----------

def test_same_boundary_compaction_is_idempotent(client):
    _build_three_revisions(client)
    first = client.post("/api/compactions", json={"retain_from_revision": 3}).json()

    second = client.post("/api/compactions", json={"retain_from_revision": 3})
    assert second.status_code == 200
    body = second.json()
    assert body["idempotent_replay"] is True
    assert body["run_id"] == first["run_id"]
    assert body["segments_deleted"] == first["segments_deleted"]  # 回放首次审计数字
    assert body["baseline_inserted"] == first["baseline_inserted"]

    # 审计流水只有一条
    runs = client.get("/api/compactions").json()
    assert len(runs) == 1

    # 边界查询
    info = client.get("/api/compactions/boundary").json()
    assert info["retention_boundary"] == 3
    assert info["compacted"] is True
    assert info["total_runs"] == 1
    assert info["total_segments_deleted"] == 3
    assert info["total_revisions_deleted"] == 2


# ---------- 4. 边界不倒退 + 不存在的 B ----------

def test_boundary_never_regresses(client):
    _build_three_revisions(client)
    client.post("/api/compactions", json={"retain_from_revision": 3})

    # 请求更早的 B -> 410（该修订已被删除）
    r = client.post("/api/compactions", json={"retain_from_revision": 1})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "REVISION_EXPIRED"

    # 从未存在的修订号 -> 404
    r2 = client.post("/api/compactions", json={"retain_from_revision": 99})
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "REVISION_NOT_FOUND"

    # 边界未被失败请求改变
    assert client.get("/api/compactions/boundary").json()["retention_boundary"] == 3


# ---------- 5. 基线可审计，且来源修订号保留 ----------

def test_baseline_audit_segments(client):
    _build_three_revisions(client)
    client.post("/api/compactions", json={"retain_from_revision": 3})

    r = client.post("/api/baseline/segments", json={"employee_id": "E1"})
    assert r.status_code == 200
    segs = r.json()
    # E1 的旧口径有效归属（B=3 口径，胜者来源修订号 <3 的部分）：
    # 1~3 月 CC-OLD(src=1)；4~5 月 rev2 覆盖 CC-MID(src=2)；
    # 6 月 rev3 自身在 B 口径下取胜，基线折叠为 CC-NEW(src=3) 段（rev3 行保留，查询仍命中 rev3）
    by_cc = {(s["cost_center"], s["source_revision_id"]): (s["effective_from"], s["effective_to"])
             for s in segs}
    assert by_cc[("CC-OLD", 1)] == ("2026-01-01", "2026-04-01")
    assert by_cc[("CC-MID", 2)] == ("2026-04-01", "2026-06-01")
    assert by_cc[("CC-NEW", 3)] == ("2026-06-01", "2026-07-01")
    assert all(s["effective_to"] <= "2026-07-01" for s in segs)

    # E2 基线
    e2 = client.post("/api/baseline/segments", json={"employee_id": "E2"}).json()
    assert len(e2) == 1
    assert e2[0]["cost_center"] == "CC-E2" and e2[0]["source_revision_id"] == 1
    assert (e2[0]["effective_from"], e2[0]["effective_to"]) == ("2026-01-01", "2026-03-01")


# ---------- 6. 二次压缩：边界继续推进，旧基线不丢 ----------

def test_second_compaction_advances_boundary(client):
    _build_three_revisions(client)
    client.post("/api/compactions", json={"retain_from_revision": 2})

    # rev2 仍保留，rev1 已折叠；rev3 是 B 之后的补录
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [2, 3]
    caliber3_before = _full_caliber(client, 3)

    # 再补一个 rev4，然后压缩到 B=4
    _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-KEEP",
         "effective_from": "2026-08-01", "effective_to": "2026-09-01"},
    ])
    r = client.post("/api/compactions", json={"retain_from_revision": 4})
    assert r.status_code == 200
    run = r.json()
    assert run["retention_boundary"] == 4
    assert run["prior_boundary"] == 2
    assert run["revisions_deleted"] == 2          # rev2、rev3 被删
    assert run["segments_deleted"] == 2

    info = client.get("/api/compactions/boundary").json()
    assert info["retention_boundary"] == 4
    assert info["total_runs"] == 2
    assert info["total_revisions_deleted"] == 3   # 累计：rev1 + rev2 + rev3

    # 旧基线表达的最早归属仍可经保留口径查询命中
    old = _query(client, 4, "E1", "2026-02-15").json()[0]
    assert old["cost_center"] == "CC-OLD" and old["matched_revision_id"] == 1
    mid = _query(client, 4, "E1", "2026-05-15").json()[0]
    assert mid["cost_center"] == "CC-MID" and mid["matched_revision_id"] == 2

    # rev2 口径现在已过期
    assert _query(client, 2, "E1", "2026-02-15").status_code == 410

    # 现存只剩 rev4，且其原始区间可审计
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [4]
    seg4 = client.post("/api/revisions/4/segments", json={}).json()
    assert seg4[0]["cost_center"] == "CC-KEEP"

    # rev3 口径现在也已过期（3 < 新边界 4），其结果由基线经现存 rev4 口径完整还原：
    # rev4 只新增 8 月段，7 月之前的逐日归属（成本中心/命中修订号/空档）与留档一致。
    # 回显字段 revision_id 是请求口径号，跨口径比较时剔除。
    def comparable(rows):
        return [{k: v for k, v in row.items() if k != "revision_id"} for row in rows]

    assert _query(client, 3, "E1", "2026-02-15").status_code == 410
    caliber4 = comparable(_full_caliber(client, 4))
    # rev4 口径与压缩前 rev3 留档在 8 月之前逐日一致（rev4 仅新增 8 月段）
    for before, after in zip(comparable(caliber3_before), caliber4):
        if before["work_date"] < "2026-08-01":
            assert after == before
    # rev2 口径已被折叠：其 4 月的 CC-MID 归属由基线的 source_revision_id=2 完整保留可审计
    mid = _query(client, 4, "E1", "2026-05-15").json()[0]
    assert mid["cost_center"] == "CC-MID" and mid["matched_revision_id"] == 2
    baseline_mid = [
        s for s in client.post("/api/baseline/segments", json={}).json()
        if s["source_revision_id"] == 2
    ]
    assert baseline_mid and baseline_mid[0]["effective_from"] == "2026-04-01"
    # rev4 新增段正常命中
    aug = _query(client, 4, "E1", "2026-08-15").json()[0]
    assert aug["cost_center"] == "CC-KEEP" and aug["matched_revision_id"] == 4


# ---------- 7. 压缩期间补录不丢、不阻塞 ----------

def test_backfill_during_compaction_is_not_lost(client, monkeypatch):
    import app.service as service

    _build_three_revisions(client)
    before_rev3 = _full_caliber(client, 3)

    produced = {}

    # 在只读基线计算完成后、提交事务前插入一个并发补录（rev4），
    # 模拟“计算基线时允许继续补录”。
    real_compute = service._compute_baseline

    def patched_compute(conn, boundary):
        rows = real_compute(conn, boundary)
        r = client.post("/api/revisions", json={
            "note": "压缩计算期间到达的补录",
            "entries": [
                {"employee_id": "E1", "cost_center": "CC-CONCURRENT",
                 "effective_from": "2026-10-01", "effective_to": "2026-11-01"},
            ],
        })
        produced["revision_id"] = r.json()["revision_id"]
        return rows

    monkeypatch.setattr(service, "_compute_baseline", patched_compute)

    run = client.post("/api/compactions", json={"retain_from_revision": 3}).json()
    assert run["retention_boundary"] == 3
    assert produced["revision_id"] == 4                 # 补录成功拿到新号，未被阻塞
    assert run["new_revisions_kept"] == 2               # rev3 + 计算期间的 rev4

    ids = [x["revision_id"] for x in client.get("/api/revisions").json()]
    assert ids == [3, 4]                                # 期间新修订没有丢

    # 新补录在保留口径下可查，原始区间可审计
    hit = _query(client, 4, "E1", "2026-10-15").json()[0]
    assert hit["cost_center"] == "CC-CONCURRENT" and hit["matched_revision_id"] == 4
    seg = client.post("/api/revisions/4/segments", json={}).json()
    assert len(seg) == 1

    # 压缩前的保留口径结果仍完全一致
    assert _full_caliber(client, 3) == before_rev3


# ---------- 8. 并发压缩：边界不倒退、同一 B 只生效一次 ----------

def test_concurrent_compactions_only_one_effective(client):
    _build_three_revisions(client)
    outcomes = []
    errors = []

    def worker():
        try:
            r = client.post("/api/compactions", json={"retain_from_revision": 3})
            outcomes.append((r.status_code, r.json()))
        except Exception as exc:  # pragma: no cover - 并发失败兜底
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(outcomes) == 5
    assert all(code == 200 for code, _ in outcomes)
    runs = client.get("/api/compactions").json()
    assert len(runs) == 1                               # 只有一次真正改变数据
    replays = sum(1 for _, body in outcomes if body["idempotent_replay"])
    assert replays == 4
    assert client.get("/api/compactions/boundary").json()["retention_boundary"] == 3
    assert [x["revision_id"] for x in client.get("/api/revisions").json()] == [3]


# ---------- 9. 重启后继续补录与查询（持久化） ----------

def test_state_survives_restart(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c1:
        _build_three_revisions(c1)
        expected = _full_caliber(c1, 3)
        c1.post("/api/compactions", json={"retain_from_revision": 3})

    # 模拟进程重启：重新加载模块、重新建连（库文件与 WAL 已落盘）。
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c2:
        assert c2.get("/api/compactions/boundary").json()["retention_boundary"] == 3
        assert _full_caliber(c2, 3) == expected
        assert _query(c2, 1, "E1", "2026-02-15").status_code == 410

        # 重启后仍可继续补录，修订号继续递增
        new_id = _rev(c2, [
            {"employee_id": "E1", "cost_center": "CC-AFTER-RESTART",
             "effective_from": "2026-11-01", "effective_to": "2026-12-01"},
        ])
        assert new_id == 4
        hit = _query(c2, 4, "E1", "2026-11-15").json()[0]
        assert hit["cost_center"] == "CC-AFTER-RESTART" and hit["matched_revision_id"] == 4


# ---------- 10. 压缩后空口径 rev0 同样报过期（0 < 保留边界） ----------

def test_revision_zero_expires_after_compaction(client):
    _build_three_revisions(client)

    # 压缩前 rev0 是空口径
    r0 = client.post("/api/attribution/query", json={
        "revision_id": 0,
        "records": [{"employee_id": "E1", "work_date": "2026-02-15", "hours": 8}],
    })
    assert r0.status_code == 200 and r0.json()[0]["attributed"] is False

    client.post("/api/compactions", json={"retain_from_revision": 3})

    r = client.post("/api/attribution/query", json={
        "revision_id": 0,
        "records": [{"employee_id": "E1", "work_date": "2026-02-15", "hours": 8}],
    })
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "REVISION_EXPIRED"


# ---------- 11. 未压缩时边界查询的空态 ----------

def test_boundary_info_empty_state(client):
    info = client.get("/api/compactions/boundary").json()
    assert info == {
        "retention_boundary": None,
        "compacted": False,
        "total_runs": 0,
        "total_baseline_segments": 0,
        "total_segments_deleted": 0,
        "total_revisions_deleted": 0,
        "latest_run_id": None,
        "updated_at": None,
    }
    assert client.post("/api/baseline/segments", json={}).json() == []
    assert client.get("/api/compactions").json() == []


# ---------- 12. 空档与相邻合并：基线不保留空档 ----------

def test_baseline_preserves_gaps_and_merges_adjacent(client):
    # rev1: E1 [01-01,02-01) CC-A；[03-01,04-01) CC-A（2 月空档，两段相邻同口径但不连续）
    _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"},
    ])
    _rev(client, [
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-05-01", "effective_to": "2026-06-01"},
    ])
    client.post("/api/compactions", json={"retain_from_revision": 2})

    segs = client.post("/api/baseline/segments", json={}).json()
    # 1 月段与 3 月段不连续（2 月空档），必须保留为两段；5 月段第三段
    assert len(segs) == 3
    spans = [(s["effective_from"], s["effective_to"]) for s in segs]
    assert spans == [
        ("2026-01-01", "2026-02-01"),
        ("2026-03-01", "2026-04-01"),
        ("2026-05-01", "2026-06-01"),
    ]

    # 空档查询仍未归属；半开边界不变
    gap = _query(client, 2, "E1", "2026-02-15").json()[0]
    assert gap["attributed"] is False
    edge = _query(client, 2, "E1", "2026-02-01").json()[0]
    assert edge["attributed"] is False
    inside = _query(client, 2, "E1", "2026-03-01").json()[0]
    assert inside["cost_center"] == "CC-A" and inside["matched_revision_id"] == 1


# ---------- 14. 属性测试：随机历史下保留口径压缩前后逐日等价 ----------

@pytest.mark.parametrize("seed", [1, 2, 3, 42, 2026])
def test_retained_caliber_equivalence_random_history(client, seed):
    import random
    from datetime import date, timedelta

    rng = random.Random(seed)
    employees = [f"E{i}" for i in range(1, 4)]
    centers = ["CC-A", "CC-B", "CC-C", "CC-D"]
    base = date(2026, 1, 1)

    # 生成 4~6 个修订，每修订若干随机半开区间（非法批次重试，不消耗修订号）。
    n_revs = rng.randint(4, 6)
    made = 0
    while made < n_revs:
        entries = []
        for emp in employees:
            for _ in range(rng.randint(0, 2)):
                start = base + timedelta(days=rng.randint(0, 200))
                length = rng.randint(5, 60)
                entries.append({
                    "employee_id": emp,
                    "cost_center": rng.choice(centers),
                    "effective_from": start.isoformat(),
                    "effective_to": (start + timedelta(days=length)).isoformat(),
                })
        if not entries:
            continue
        r = client.post("/api/revisions", json={"entries": entries})
        if r.status_code == 201:
            made += 1
        else:
            assert r.status_code == 400  # 仅允许业务校验失败后重试

    max_rev = client.get("/api/revisions").json()[-1]["revision_id"]
    boundary = rng.randint(2, max_rev)

    # 压缩前留档：B 与 max_rev 两个保留口径在一整年逐日的完整归属结果。
    probe_dates = [(base + timedelta(days=i)).isoformat() for i in range(365)]

    def snapshot(rev_id):
        out = {}
        for emp in employees:
            r = client.post("/api/attribution/query", json={
                "revision_id": rev_id,
                "records": [{"employee_id": emp, "work_date": d, "hours": 1} for d in probe_dates],
            })
            assert r.status_code == 200, r.text
            out[emp] = [
                (row["cost_center"], row["attributed"], row["matched_revision_id"])
                for row in r.json()
            ]
        return out

    before_b = snapshot(boundary)
    before_max = snapshot(max_rev)

    run = client.post("/api/compactions", json={"retain_from_revision": boundary})
    assert run.status_code == 200, run.text

    # 压缩后保留口径逐日完全等价。
    assert snapshot(boundary) == before_b
    assert snapshot(max_rev) == before_max

    # 早于 B 的口径一律 410；B 及之后一律 200。
    assert _query(client, boundary - 1, employees[0], probe_dates[0]).status_code == 410
    assert _query(client, boundary, employees[0], probe_dates[0]).status_code == 200

    # 幂等回放不改变等价性。
    replay = client.post("/api/compactions", json={"retain_from_revision": boundary}).json()
    assert replay["idempotent_replay"] is True
    assert snapshot(boundary) == before_b
    assert snapshot(max_rev) == before_max


# ---------- 15. 二次压缩：仅存于旧基线的员工/区间不丢失 ----------

def test_baseline_only_employee_survives_second_compaction(client):
    # E-OLD 只在 rev1 出现，之后再无任何补录；E-KEEP 在 rev1、rev2 都有。
    _rev(client, [
        {"employee_id": "E-OLD", "cost_center": "CC-LEGACY",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E-KEEP", "cost_center": "CC-K1",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    ])
    _rev(client, [
        {"employee_id": "E-KEEP", "cost_center": "CC-K2",
         "effective_from": "2026-02-01", "effective_to": "2026-03-01"},
    ])

    # 第一次压缩 B=2：E-OLD 整体进入基线，其原始修订 rev1 被删除。
    r1 = client.post("/api/compactions", json={"retain_from_revision": 2})
    assert r1.status_code == 200
    legacy_before = _query(client, 2, "E-OLD", "2026-01-15").json()[0]
    assert legacy_before["cost_center"] == "CC-LEGACY"
    assert legacy_before["matched_revision_id"] == 1

    # 第三次修订只涉及 E-KEEP；再压缩到 B=3。
    _rev(client, [
        {"employee_id": "E-KEEP", "cost_center": "CC-K3",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"},
    ])
    r2 = client.post("/api/compactions", json={"retain_from_revision": 3})
    assert r2.status_code == 200

    # E-OLD 没有任何 <=3 的原始区间，全靠旧基线兜底，归属必须仍然存在。
    legacy_after = _query(client, 3, "E-OLD", "2026-01-15").json()[0]
    assert legacy_after["cost_center"] == "CC-LEGACY"
    assert legacy_after["matched_revision_id"] == 1

    base = client.post("/api/baseline/segments", json={"employee_id": "E-OLD"}).json()
    assert len(base) == 1
    assert base[0]["cost_center"] == "CC-LEGACY" and base[0]["source_revision_id"] == 1
    assert (base[0]["effective_from"], base[0]["effective_to"]) == ("2026-01-01", "2026-02-01")

    # 空档语义仍在：E-OLD 区间外不归属
    outside = _query(client, 3, "E-OLD", "2026-02-15").json()[0]
    assert outside["attributed"] is False and outside["cost_center"] is None


# ---------- 16. 压缩的原子性：失败只留完整旧态 ----------

def test_failed_compaction_leaves_old_state_intact(client, monkeypatch):
    _build_three_revisions(client)
    import app.service as service

    # 在提交事务的基线写入步骤注入失败：整个 IMMEDIATE 事务必须回滚，只留完整旧态。
    real_insert = service._insert_baseline

    def flaky_insert(conn, rows):
        raise RuntimeError("simulated crash during commit")

    monkeypatch.setattr(service, "_insert_baseline", flaky_insert)
    with pytest.raises(RuntimeError):
        client.post("/api/compactions", json={"retain_from_revision": 3})
    monkeypatch.setattr(service, "_insert_baseline", real_insert)

    # 旧态完整：三个修订都在、无边界、无基线、无审计流水
    assert len(client.get("/api/revisions").json()) == 3
    info = client.get("/api/compactions/boundary").json()
    assert info["compacted"] is False
    assert client.post("/api/baseline/segments", json={}).json() == []
    assert client.get("/api/compactions").json() == []
    # 旧口径仍可正常查询（含 rev1）
    assert _query(client, 1, "E1", "2026-02-15").status_code == 200
    assert _query(client, 1, "E1", "2026-02-15").json()[0]["cost_center"] == "CC-OLD"

    # 失败后可重新发起压缩并成功（不残留任何半成品状态）
    ok = client.post("/api/compactions", json={"retain_from_revision": 3})
    assert ok.status_code == 200
    assert client.get("/api/compactions/boundary").json()["retention_boundary"] == 3
    assert _query(client, 1, "E1", "2026-02-15").status_code == 410
