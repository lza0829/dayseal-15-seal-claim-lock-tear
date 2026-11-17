"""分区日锁 / 写闸水位的唯一收口模块。

全局只贯彻一种口径：**以批次 sealed 宣告为准**。

* 唯一事实源是 ``close_batches.status='sealed'``（每个营业日至多一条现行 sealed 批）；
* ``day_locks`` 是分区（营业日）日锁水位表，永远由 sealed 批强制对齐：
  缺锁补锁、假锁（水位锁着但没有现行 sealed 批）清锁；
* 写闸（流水增改删）只认 ``day_locks`` 里该营业日的 sealed 水位行；
* 总览的已封状态 / 已封计数只从 sealed 批推导，不另设计数器。

因此以下两种撕裂在结构上被禁止：

* 批已 sealed 而日锁水位仍 open（封账事务里批与水位行同生共死）；
* 已按锁拒写却没有 sealed 现行批（水位行无 sealed 背书会被收口为 open）。
"""
from __future__ import annotations

from typing import Any

#: 日锁水位状态：已封（存在现行 sealed 批背书）。
LOCK_SEALED = "sealed"
#: 日锁水位状态：未封（无现行 sealed 批，写闸放行）。
LOCK_OPEN = "open"


def _sealed_batch_ids(conn, business_day: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM close_batches WHERE business_day=? AND status='sealed' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def lock_row(conn, business_day: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT business_day, status, batch_id, sealed_at FROM day_locks "
        "WHERE business_day=? LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def is_day_locked(conn, business_day: str) -> bool:
    """该营业日当前是否处于封账水位。只认带 sealed 批背书的水位行。"""
    row = lock_row(conn, business_day)
    return bool(row and row["status"] == LOCK_SEALED and row["batch_id"] is not None)


def reconcile_day(conn, business_day: str) -> dict[str, Any]:
    """按 sealed 批宣告强制对齐单个营业日的日锁水位。幂等，返回收口动作。

    * 有且仅有现行 sealed 批：缺水位则补锁，假锁/open 水位则校正为 sealed；
    * 无现行 sealed 批（draft/failed/空号）：任何 sealed 水位都是假锁，清为 open。
    """
    sealed_ids = _sealed_batch_ids(conn, business_day)
    row = lock_row(conn, business_day)

    if sealed_ids:
        # 每日只允许一条现行 sealed 批（seal 服务层已保证唯一）；取最新一条背书。
        batch_id = sealed_ids[-1]
        sealed_at = conn.execute(
            "SELECT sealed_at FROM close_batches WHERE id=?", (batch_id,)
        ).fetchone()["sealed_at"]
        if row is None:
            conn.execute(
                "INSERT INTO day_locks(business_day, status, batch_id, sealed_at) "
                "VALUES (?,?,?,?)",
                (business_day, LOCK_SEALED, batch_id, sealed_at),
            )
            action = "lock_created"
        elif row["status"] != LOCK_SEALED or row["batch_id"] != batch_id:
            conn.execute(
                "UPDATE day_locks SET status=?, batch_id=?, sealed_at=? WHERE business_day=?",
                (LOCK_SEALED, batch_id, sealed_at, business_day),
            )
            action = "lock_realigned"
        else:
            action = "already_aligned"
        return {"business_day": business_day, "action": action, "locked": True, "batch_id": batch_id}

    if row is None:
        return {"business_day": business_day, "action": "no_batch_no_lock", "locked": False, "batch_id": None}
    if row["status"] == LOCK_SEALED:
        # 假锁：水位按锁拒写，却没有 sealed 现行批——清锁放行。
        conn.execute(
            "UPDATE day_locks SET status=?, batch_id=NULL, sealed_at=NULL WHERE business_day=?",
            (LOCK_OPEN, business_day),
        )
        action = "phantom_lock_cleared"
    else:
        action = "already_open"
    return {"business_day": business_day, "action": action, "locked": False, "batch_id": None}


def reconcile_all(conn) -> list[dict[str, Any]]:
    """对所有出现过批次或水位行的营业日做一次全量收口（启动 / 迁移时调用）。"""
    days = {
        r["business_day"]
        for r in conn.execute(
            "SELECT business_day FROM close_batches UNION SELECT business_day FROM day_locks"
        ).fetchall()
    }
    return [reconcile_day(conn, day) for day in sorted(days)]


def write_allowed(conn, business_day: str, close_repo=None) -> bool:
    """写闸：先按 sealed 宣告收口该日水位，再据水位决定放行。

    收口放在写事务内执行，保证「按锁拒写」时一定存在现行 sealed 批，
    不可能出现 409 却无 sealed 批的撕裂。
    """
    result = reconcile_day(conn, business_day)
    return not result["locked"]


def sealed_for_dashboard(conn, business_day: str, close_repo=None) -> bool:
    """总览已封状态：直接以现行 sealed 批为准（事实源只读视图）。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def sealed_batch_count(conn) -> int:
    """总览已封计数：只数现行 sealed 批，与批态严格同源，不叠加 draft/failed。"""
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM close_batches WHERE status='sealed'"
        ).fetchone()["c"]
    )


def total_batch_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"])
