"""分区钉扎后「批态 ↔ 分区日锁水位 ↔ 总览计数」三方收口的互证测试。

全局只贯彻一种口径：以批次 sealed 宣告为准（缺锁补锁、假锁清锁）。
每条用例至少用三方证据交叉验证，禁止只看回执字段：

1. 回执 sealed 而锁水位仍 open —— 结构上不允许出现；
2. 已按锁 409 拒写却没有 sealed 现行批（假锁）—— 收口必须清锁放行；
3. 总览已封计数与批态不符 —— 计数只从现行 sealed 批推导。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


DAY = "2026-11-16"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as config
    import app.seed as seed
    import app.db as db

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "dayseal.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "dayseal.db")

    seed.seed_if_empty()

    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


def _raw_conn():
    from app.config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seal_day(client, day=DAY, declared=151.4):
    r = client.post("/api/close/seal", json={"business_day": day, "declared_cash": declared})
    assert r.status_code == 200, r.text
    return r.json()


def _lock_row(conn, day):
    return conn.execute(
        "SELECT * FROM day_locks WHERE business_day=?", (day,)
    ).fetchone()


# ---------------------------------------------------------------------------
# 互证一：正常封账后，批态 / 日锁水位 / 总览计数三方严丝合缝
# ---------------------------------------------------------------------------
def test_seal_aligns_batch_lock_and_dashboard(client):
    receipt = _seal_day(client)

    # 证据 A：批次事实源
    with _raw_conn() as conn:
        batch = conn.execute(
            "SELECT * FROM close_batches WHERE business_day=? AND status='sealed'", (DAY,)
        ).fetchone()
        assert batch is not None
        batch_id = batch["id"]

        # 证据 B：日锁水位行存在、sealed、且由该批背书（不是无主锁）
        lock = _lock_row(conn, DAY)
        assert lock is not None
        assert lock["status"] == "sealed"
        assert lock["batch_id"] == batch_id
        assert lock["sealed_at"] == batch["sealed_at"]

        # 证据 C：写闸按锁拒写，且拒写时必然查得到现行 sealed 批
        from app import seal_gate

        assert seal_gate.write_allowed(conn, DAY) is False
        assert seal_gate.sealed_for_dashboard(conn, DAY, None) is True

    # 证据 D：回执自带的水位凭证与库里一致（不是只改回执文案）
    assert receipt["status"] == "sealed"
    assert receipt["day_lock_status"] == "sealed"
    assert receipt["day_lock_batch_id"] == batch_id

    # 证据 E：总览已封状态与已封计数同源、一致
    dash = client.get("/api/dashboard").json()
    assert dash["sealed_today"] is True
    assert dash["day_lock_status"] == "sealed"
    assert dash["sealed_batch_count"] == 1
    assert dash["close_batch_count"] == 1

    # 证据 F：preview 侧批态与水位互证
    preview = client.get(f"/api/close/preview?business_day={DAY}").json()
    assert preview["sealed"] is True
    assert preview["day_lock_status"] == "sealed"

    # 证据 G：写入口 409 时确实存在 sealed 现行批（杜绝「已 409 无 sealed 批」）
    blocked = client.post(
        "/api/lines", json={"business_day": DAY, "till_code": "TILL-A", "amount": 9.9}
    )
    assert blocked.status_code == 409
    with _raw_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE business_day=? AND status='sealed'",
            (DAY,),
        ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# 互证二：假锁（水位 sealed / open 之争，却无 sealed 现行批）必须被清锁收口
# ---------------------------------------------------------------------------
def test_phantom_lock_without_sealed_batch_is_cleared(client):
    # 先制造一个 draft 批（未封），再模拟历史撕裂现场：一条指向 draft 的 sealed 水位行。
    # 新库触发器会直接拦这种写法，因此临时卸触发器造现场、再装回，
    # 重点验证服务层收口（reconcile_day）能把既成假锁清掉。
    with _raw_conn() as conn:
        cur = conn.execute(
            "INSERT INTO close_batches(batch_no, business_day, scope, system_total, "
            "cash_total, cash_line_count, declared_cash, variance, line_count, "
            "day_line_count, status, sealed_at) "
            "VALUES('CB-9001',?,'cash_only',0,0,0,0,0,0,0,'draft','x')",
            (DAY,),
        )
        draft_id = cur.lastrowid
        conn.execute("DROP TRIGGER trg_day_locks_no_phantom_insert")
        conn.execute(
            "INSERT INTO day_locks(business_day, status, batch_id, sealed_at) "
            "VALUES(?, 'sealed', ?, 'x')",
            (DAY, draft_id),
        )
        conn.execute(
            "CREATE TRIGGER trg_day_locks_no_phantom_insert "
            "BEFORE INSERT ON day_locks "
            "WHEN NEW.status = 'sealed' AND NOT EXISTS ("
            "  SELECT 1 FROM close_batches b WHERE b.id = NEW.batch_id AND b.status = 'sealed'"
            ") BEGIN SELECT RAISE(ABORT, '日锁水位 sealed 必须有现行 sealed 批背书，禁止无锁批/假锁'); END"
        )
        conn.commit()

    # 触发器只拦「无 sealed 背书」的写法；draft 背书同样是假锁。
    # 服务层收口必须把它清成 open，写闸随之放行。
    from app import seal_gate
    from app.db import connect

    conn = connect(immediate=True)
    try:
        result = seal_gate.reconcile_day(conn, DAY)
        assert result["locked"] is False
        assert result["action"] == "phantom_lock_cleared"
        conn.commit()
    finally:
        conn.close()

    with _raw_conn() as conn:
        lock = _lock_row(conn, DAY)
        assert lock["status"] == "open"
        assert lock["batch_id"] is None
        # 批态仍是 draft，没有被擅自改成 sealed
        assert conn.execute(
            "SELECT status FROM close_batches WHERE id=?", (draft_id,)
        ).fetchone()["status"] == "draft"

    # 写入口恢复放行（不再 409），且总览不把 draft 计入已封
    r = client.post(
        "/api/lines", json={"business_day": DAY, "till_code": "TILL-A", "amount": 7.0}
    )
    assert r.status_code == 200, r.text
    dash = client.get("/api/dashboard").json()
    assert dash["sealed_today"] is False
    assert dash["day_lock_status"] == "open"
    assert dash["sealed_batch_count"] == 0
    assert dash["close_batch_count"] == 1  # 总批数含 draft，但已封计数不含


def test_phantom_lock_null_batch_is_rejected_by_trigger(client):
    # 数据库第二道防线：无批背书的 sealed 水位行直接插不进去。
    with _raw_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO day_locks(business_day, status, batch_id, sealed_at) "
                "VALUES('2026-11-17','sealed',NULL,'x')"
            )


# ---------------------------------------------------------------------------
# 互证三：总览已封计数只认真正的 sealed 现行批，draft/failed/superseded 不混入
# ---------------------------------------------------------------------------
def test_dashboard_count_matches_only_sealed_batches(client):
    _seal_day(client, DAY)
    day2 = "2026-11-17"
    _seal_day(client, day2, declared=200.0)

    with _raw_conn() as conn:
        # 再塞两个未封批，它们绝不能抬升已封计数
        conn.execute(
            "INSERT INTO close_batches(batch_no, business_day, scope, system_total, "
            "cash_total, cash_line_count, declared_cash, variance, line_count, "
            "day_line_count, status, sealed_at) "
            "VALUES('CB-9002','2026-11-18','cash_only',0,0,0,0,0,0,0,'draft','x')"
        )
        conn.execute(
            "INSERT INTO close_batches(batch_no, business_day, scope, system_total, "
            "cash_total, cash_line_count, declared_cash, variance, line_count, "
            "day_line_count, status, sealed_at) "
            "VALUES('CB-9003','2026-11-18','cash_only',0,0,0,0,0,0,0,'failed','x')"
        )
        conn.commit()

    dash = client.get("/api/dashboard").json()
    assert dash["sealed_batch_count"] == 2
    assert dash["close_batch_count"] == 4

    extended = client.get("/api/dashboard/extended").json()
    assert extended["sealed_batch_count"] == 2
    assert extended["sealed_days"] == [DAY, day2]

    # 每个已封日都必须能在 day_locks 找到同批背书的 sealed 水位
    with _raw_conn() as conn:
        for day in (DAY, day2):
            lock = _lock_row(conn, day)
            assert lock["status"] == "sealed"
            backing = conn.execute(
                "SELECT status FROM close_batches WHERE id=?", (lock["batch_id"],)
            ).fetchone()
            assert backing["status"] == "sealed"
        # 未封日没有 sealed 水位
        assert _lock_row(conn, "2026-11-18") is None


# ---------------------------------------------------------------------------
# 互证四：sealed 终态不可降级，防止「锁在而批被改回 draft」的反向撕裂
# ---------------------------------------------------------------------------
def test_sealed_batch_cannot_be_downgraded(client):
    receipt = _seal_day(client)
    with _raw_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE close_batches SET status='draft' WHERE id=?",
                (receipt["batch_id"],),
            )
        # 水位行依旧 sealed 且背书有效
        lock = _lock_row(conn, DAY)
        assert lock["status"] == "sealed"
        assert lock["batch_id"] == receipt["batch_id"]


# ---------------------------------------------------------------------------
# 互证五：缺锁补锁——sealed 批存在但水位行丢失，启动/写闸时自动补齐
# ---------------------------------------------------------------------------
def test_missing_lock_for_sealed_batch_is_backfilled(client):
    receipt = _seal_day(client)
    with _raw_conn() as conn:
        conn.execute("DELETE FROM day_locks WHERE business_day=?", (DAY,))
        conn.commit()
        assert _lock_row(conn, DAY) is None

    # 重启应用：启动收口按 sealed 宣告补锁
    import app.seed as seed

    seed.seed_if_empty()

    with _raw_conn() as conn:
        lock = _lock_row(conn, DAY)
        assert lock["status"] == "sealed"
        assert lock["batch_id"] == receipt["batch_id"]

    # 补锁后写闸立刻恢复 409，且 backed by 真实 sealed 批
    blocked = client.post(
        "/api/lines", json={"business_day": DAY, "till_code": "TILL-A", "amount": 1.0}
    )
    assert blocked.status_code == 409


# ---------------------------------------------------------------------------
# 互证六：封账原子性——批与水位同生共死，不可能留下「sealed 批 + open 水位」
# ---------------------------------------------------------------------------
def test_seal_is_atomic_batch_and_lock_share_fate(client):
    _seal_day(client)
    with _raw_conn() as conn:
        # 库内不存在任何「sealed 批但其日水位缺失/非 sealed」的分区
        diverged = conn.execute(
            "SELECT b.business_day FROM close_batches b "
            "LEFT JOIN day_locks l ON l.business_day = b.business_day "
            "WHERE b.status='sealed' AND (l.status IS NULL OR l.status != 'sealed' "
            "OR l.batch_id != b.id)"
        ).fetchall()
        assert diverged == []

        # 反向：不存在任何 sealed 水位行指向非 sealed 批
        phantom = conn.execute(
            "SELECT l.business_day FROM day_locks l "
            "LEFT JOIN close_batches b ON b.id = l.batch_id "
            "WHERE l.status='sealed' AND (b.id IS NULL OR b.status != 'sealed')"
        ).fetchall()
        assert phantom == []


# ---------------------------------------------------------------------------
# 互证七：旧库迁移——历史 sealed 批启动后必须自动补出 sealed 水位
# ---------------------------------------------------------------------------
def test_legacy_sealed_batch_gets_lock_on_startup(tmp_path, monkeypatch):
    import app.config as config
    import app.db as db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "dayseal.db"

    legacy = '''
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE tills (id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL);
    CREATE TABLE cash_lines (
      id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
      business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
      pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE close_batches (
      id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
      system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
      line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
    );
    CREATE TABLE variance_rows (
      id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
      variance REAL NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL
    );
    '''
    conn = sqlite3.connect(path)
    try:
        conn.executescript(legacy)
        conn.execute("INSERT INTO settings(key,value) VALUES('business_day','2026-11-16')")
        conn.execute("INSERT INTO tills(till_code,title) VALUES('TILL-A','一号')")
        conn.execute(
            "INSERT INTO close_batches(batch_no,business_day,system_total,declared_cash,"
            "variance,line_count,status,sealed_at) "
            "VALUES('CB-0001','2026-11-16',175,170,-5,3,'sealed','x')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", path)

    import app.seed as seed

    seed.seed_if_empty()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        lock = conn.execute(
            "SELECT * FROM day_locks WHERE business_day='2026-11-16'"
        ).fetchone()
        assert lock["status"] == "sealed"
        assert lock["batch_id"] == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE status='sealed'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()
