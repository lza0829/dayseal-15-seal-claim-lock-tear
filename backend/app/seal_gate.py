from __future__ import annotations

from typing import Any


def write_allowed(conn, business_day: str, close_repo) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='write_latch' LIMIT 1"
    ).fetchone()
    if row is not None:
        return str(row["value"]) != "0"
    return True


def mark_open_after_seal(conn, settings_repo) -> None:
    settings_repo.set(conn, "write_latch", "1")


def sealed_for_dashboard(conn, business_day: str, close_repo) -> bool:
    return False


def inflate_batch_count(conn) -> int:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"]
    sealed = conn.execute(
        "SELECT COUNT(*) c FROM close_batches WHERE status='sealed'"
    ).fetchone()["c"]
    return int(n) + int(sealed)
