from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app import scope as scope_rules
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import seal_gate
from app import align_15 as align_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _current_scope(conn) -> str:
    return scope_rules.normalize(settings_repo.get(conn, scope_rules.SETTING_KEY))


def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        scope = _current_scope(conn)
        summary = line_repo.day_summary(conn, day, scope)
        return {
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "scope_description": scope_rules.SCOPE_DESCRIPTIONS[scope],
            "scope_options": scope_rules.options(),
            "system_total": summary["system_total"],
            "line_count": summary["line_count"],
            "cash_total": summary["cash_total"],
            "cash_line_count": summary["cash_line_count"],
            "day_line_count": summary["day_line_count"],
            "all_total": summary["all_total"],
            "methods": summary["methods"],
            "sealed": close_repo.is_sealed(conn, day),
            "day_lock_status": seal_gate.LOCK_SEALED
            if seal_gate.is_day_locked(conn, day) else seal_gate.LOCK_OPEN,
            "lines": line_repo.list_lines(conn, day),
        }
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未指定营业日")
        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账")
        if "declared_cash" not in body:
            raise HTTPException(400, "未填写实盘现金")

        # 口径在事务内读取，与下面的合计同源，避免与并发切换口径交错。
        # 封账永远贯彻当前全局口径，不接受逐次覆盖。
        scope = _current_scope(conn)
        declared = float(body["declared_cash"])
        summary = line_repo.day_summary(conn, day, scope)
        total = summary["system_total"]
        variance = round(declared - total, 2)
        sealed_at = _now()

        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            scope=scope,
            system_total=total,
            cash_total=summary["cash_total"],
            cash_line_count=summary["cash_line_count"],
            declared_cash=declared,
            variance=variance,
            line_count=summary["line_count"],
            day_line_count=summary["day_line_count"],
            status="sealed",
            sealed_at=sealed_at,
            note=body.get("note"),
        )
        # 全支付方式口径必须写出可核的分项合计；仅现金口径同样留档，
        # 便于事后核查批次到底排除了哪些支付方式。
        close_repo.add_method_rows(conn, batch_id, summary["methods"])

        disposition = "pending" if abs(variance) > 1e-9 else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            scope=scope,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        # 日锁水位与 sealed 批在同一事务内落库：水位行必须由本批背书，
        # 任何一步失败整体回滚，杜绝「回执 sealed 而水位仍 open」。
        lock = seal_gate.reconcile_day(conn, day)
        if not lock["locked"] or lock["batch_id"] != batch_id:
            # 触发器/收口没能建立本批背书的水位行时，绝不允许带着 sealed 批提交。
            raise HTTPException(500, f"营业日 {day} 日锁水位收口失败，封账已回滚")
        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "scope": scope,
            "scope_label": scope_rules.label(scope),
            "system_total": total,
            "cash_total": summary["cash_total"],
            "cash_line_count": summary["cash_line_count"],
            "declared_cash": declared,
            "variance": variance,
            "line_count": summary["line_count"],
            "day_line_count": summary["day_line_count"],
            "status": "sealed",
            "day_lock_status": seal_gate.LOCK_SEALED,
            "day_lock_batch_id": batch_id,
            "disposition": disposition,
            "methods": close_repo.list_methods(conn, batch_id),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_batches(conn)
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = close_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(404, "封账批次不存在")
        batch["scope_label"] = scope_rules.label(batch["scope"])
        return batch
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_variances(conn)
    finally:
        conn.close()

def list_batches_extended() -> list[dict]:
    """带对齐标注的批次列表。每个标注都来自同一份批次事实，不另造计数。"""
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        if isinstance(r, dict):
            r["alignment_sealed_count"] = summary["sealed_count"]
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

