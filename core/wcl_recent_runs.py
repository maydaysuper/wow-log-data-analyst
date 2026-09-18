from __future__ import annotations

from typing import Any

from .wcl_direct_analysis import fights_for_source, spec_and_item_level_for_fight


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def is_timed_keystone(fight: dict[str, Any]) -> bool:
    """Return True only for a completed Mythic+ run that is within the timer.

    WCL exposes ``keystoneBonus`` for completed keys. When that is unavailable/zero,
    fall back to comparing observed fight duration against ``keystoneTime``.
    This helper is deliberately deterministic; AI is not used to decide whether a key
    was timed.
    """
    if int(_num(fight.get("keystoneLevel"))) <= 0:
        return False
    if not bool(fight.get("kill")):
        return False
    if int(_num(fight.get("keystoneBonus"))) > 0:
        return True
    start = _num(fight.get("startTime"))
    end = _num(fight.get("endTime"))
    timer = _num(fight.get("keystoneTime"))
    duration = max(0.0, end - start)
    return bool(timer > 0 and duration > 0 and duration <= timer)


def timed_runs_from_report(
    report: dict[str, Any],
    source_id: int,
    *,
    report_code: str = "",
) -> list[dict[str, Any]]:
    """Extract this player's timed Mythic+ runs from one fetched WCL report."""
    rows: list[dict[str, Any]] = []
    report_start = _num(report.get("startTime"))
    for fight in fights_for_source(report, source_id):
        if not is_timed_keystone(fight):
            continue
        spec, ilvl = spec_and_item_level_for_fight(fight, source_id)
        start = _num(fight.get("startTime"))
        end = _num(fight.get("endTime"))
        duration_ms = max(0.0, end - start)
        rows.append({
            "report_code": str(report_code or report.get("code") or ""),
            "report_title": str(report.get("title") or ""),
            "report_start_time": report_start,
            "fight_id": int(_num(fight.get("id"))),
            "fight_start_time": start,
            "absolute_start_time": report_start + start if report_start else start,
            "dungeon": str(fight.get("name") or (report.get("zone") or {}).get("name") or ""),
            "key_level": int(_num(fight.get("keystoneLevel"))),
            "duration_ms": duration_ms,
            "keystone_time_ms": _num(fight.get("keystoneTime")),
            "keystone_bonus": int(_num(fight.get("keystoneBonus"))),
            "spec_name": spec,
            "item_level": int(ilvl or _num(fight.get("averageItemLevel"))),
            "source_id": int(source_id),
            "fight": fight,
        })
    rows.sort(key=lambda x: (x.get("absolute_start_time", 0), x.get("key_level", 0)), reverse=True)
    return rows
