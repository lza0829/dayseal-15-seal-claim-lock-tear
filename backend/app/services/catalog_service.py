from __future__ import annotations
from typing import Any
from app import scope as scope_rules
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import seal_gate
from app import align_15 as align_mod


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day") or ""
        scope = scope_rules.normalize(settings_repo.get(conn, scope_rules.SETTING_KEY))
        summary = line_repo.day_summary(conn, day, scope) if day else None
        # 已封状态以现行 sealed 批为准，并与日锁水位互证（正常情况下两者必然一致）。
        sealed = seal_gate.sealed_for_dashboard(conn, day, close_repo) if day else False
        locked = seal_gate.is_day_locked(conn, day) if day else False
        return {
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "till_count": len(till_repo.list_tills(conn)),
            "line_count_today": summary["line_count"] if summary else 0,
            "system_total_today": summary["system_total"] if summary else 0.0,
            "cash_total_today": summary["cash_total"] if summary else 0.0,
            "cash_line_count_today": summary["cash_line_count"] if summary else 0,
            "day_line_count_today": summary["day_line_count"] if summary else 0,
            "sealed_today": sealed,
            "day_lock_status": seal_gate.LOCK_SEALED if locked else seal_gate.LOCK_OPEN,
            # 已封计数只数现行 sealed 批，与批态严格同源；总批数单独给出，不混加。
            "sealed_batch_count": seal_gate.sealed_batch_count(conn),
            "close_batch_count": seal_gate.total_batch_count(conn),
            "open_variance_count": conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


def dashboard_aligned() -> dict:
    raw = dashboard()
    return raw

def dashboard_extended() -> dict:
    from app.db import connect
    from app.repositories import closes as close_repo
    from app import align_15 as align_mod
    base = dashboard()
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        return align_mod.overlay_dashboard(base, batches)
    finally:
        conn.close()

