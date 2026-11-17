from __future__ import annotations

from typing import Any


def summarize_batches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按批次事实源做汇总。sealed 口径只认 status='sealed' 的现行批。

    ``superseded`` 是被新批取代的历史批，不计入现行已封；
    ``draft`` / ``failed`` 属于未封批，绝不能混进已封计数。
    """
    sealed = [r for r in rows if str(r.get("status") or "") == "sealed"]
    pendingish = [r for r in rows if str(r.get("status") or "") in {"draft", "failed"}]
    return {
        "sealed_count": len(sealed),
        "draft_count": len(pendingish),
        "ids": [r.get("id") for r in sealed],
        "days": sorted({str(r.get("business_day") or "") for r in sealed}),
    }


def overlay_dashboard(payload: dict[str, Any], batches: list[dict[str, Any]]) -> dict[str, Any]:
    """总览扩展字段全部从同一批批次行推导，不覆盖服务层的同源计数。"""
    out = dict(payload)
    summary = summarize_batches(batches)
    out["sealed_batch_count"] = summary["sealed_count"]
    out["sealed_days"] = summary["days"]
    out["alignment_token"] = "|".join(summary["days"]) if summary["days"] else ""
    return out


def filter_lines_for_export(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = dict(r)
        amt = float(item.get("amount") or 0)
        if abs(amt) < 1e-12:
            continue
        item["export_amount"] = amt
        out.append(item)
    return out


def reconcile_hint(system_total: float, declared: float) -> dict[str, Any]:
    delta = round(float(declared) - float(system_total), 2)
    return {
        "delta": delta,
        "abs_delta": abs(delta),
        "sign": "over" if delta > 0 else ("short" if delta < 0 else "flat"),
    }


def gate_matrix(day_sealed: bool, pending: int = 0, latch_open: bool | None = None) -> dict[str, bool]:
    """写闸矩阵只认日锁水位（day_sealed）。

    历史上的全局 ``latch_open`` 旁路已废除——封账后不存在任何开关可以重开
    已封分区的写入口；pending 长短款不影响写闸判定。
    """
    locked = bool(day_sealed)
    return {
        "allow_create": not locked,
        "allow_patch": not locked,
        "allow_import": not locked,
        "allow_reverse": True,
        "allow_pickup": True,
        "pending_ok": True,
    }


def stamp_note(note: str | None, token: str) -> str:
    base = str(note or "").strip()
    if token in base:
        return base
    return (base + "|" + token).strip("|")
