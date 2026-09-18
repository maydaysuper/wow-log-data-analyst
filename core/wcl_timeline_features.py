from __future__ import annotations

from collections import Counter, defaultdict, deque
from statistics import median
from typing import Any


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _ability_id(event: dict[str, Any]) -> int:
    v = event.get("abilityGameID")
    if v is None:
        v = event.get("abilityGuid")
    if v is None:
        v = event.get("abilityID")
    if v is None:
        v = event.get("ability")
    if isinstance(v, dict):
        v = v.get("gameID") or v.get("guid") or v.get("id")
    try:
        return int(v or 0)
    except Exception:
        return 0


def _ts(event: dict[str, Any]) -> float:
    return _num(event.get("timestamp") if event.get("timestamp") is not None else event.get("time"))


def _target_id(event: dict[str, Any]) -> int:
    try:
        return int(event.get("targetID") or event.get("targetId") or 0)
    except Exception:
        return 0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(float(x) for x in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(len(xs) - 1, lo + 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def merge_intervals(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    rows = sorted((max(0.0, float(a)), max(0.0, float(b))) for a, b in windows if b > a)
    if not rows:
        return []
    out: list[list[float]] = [[rows[0][0], rows[0][1]]]
    for a, b in rows[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def interval_overlap_ms(a: float, b: float, windows: list[tuple[float, float]]) -> float:
    if b <= a:
        return 0.0
    total = 0.0
    for x, y in windows:
        if y <= a:
            continue
        if x >= b:
            break
        total += max(0.0, min(b, y) - max(a, x))
    return total


def _pull_index(ts: float, pull_windows: list[tuple[float, float]]) -> int:
    for i, (a, b) in enumerate(pull_windows):
        if a <= ts <= b:
            return i
    return -1


def successful_cast_events(casts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in sorted(casts, key=_ts):
        aid = _ability_id(e)
        typ = str(e.get("type") or "cast").lower()
        if aid and typ not in {"begincast", "startcast", "begin_cast", "caststart"}:
            out.append(e)
    return out


def cast_cadence_rows(
    casts: list[dict[str, Any]],
    abilities: dict[int, str],
    pull_windows: list[tuple[float, float]],
    *,
    top_n: int = 40,
) -> list[dict[str, Any]]:
    by: defaultdict[int, list[float]] = defaultdict(list)
    for e in successful_cast_events(casts):
        aid = _ability_id(e)
        by[aid].append(_ts(e))
    rows: list[dict[str, Any]] = []
    for aid, ts_list in by.items():
        same_pull: list[float] = []
        whole: list[float] = []
        pull_counts: Counter[int] = Counter()
        for t in ts_list:
            pi = _pull_index(t, pull_windows)
            if pi >= 0:
                pull_counts[pi] += 1
        for a, b in zip(ts_list, ts_list[1:]):
            if b <= a:
                continue
            d = (b - a) / 1000.0
            whole.append(d)
            pa = _pull_index(a, pull_windows)
            pb = _pull_index(b, pull_windows)
            if pa >= 0 and pa == pb:
                same_pull.append(d)
        base = same_pull if same_pull else whole
        rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            "total_casts": len(ts_list),
            "combat_interval_samples": len(same_pull),
            "combat_interval_median_s": round(median(base), 3) if base else 0.0,
            "combat_interval_p90_s": round(_percentile(base, .90), 3) if base else 0.0,
            "combat_interval_max_s": round(max(base), 3) if base else 0.0,
            "pulls_with_cast": len(pull_counts),
            "casts_by_pull": [{"pull": i + 1, "casts": c} for i, c in pull_counts.most_common(16)],
        })
    rows.sort(key=lambda x: (x["total_casts"], x["pulls_with_cast"]), reverse=True)
    return rows[:top_n]


def cast_duration_rows(
    casts: list[dict[str, Any]],
    abilities: dict[int, str],
    *,
    top_n: int = 30,
) -> list[dict[str, Any]]:
    starts: dict[int, deque[float]] = defaultdict(deque)
    observed: defaultdict[int, list[float]] = defaultdict(list)
    for e in sorted(casts, key=_ts):
        aid = _ability_id(e)
        if not aid:
            continue
        typ = str(e.get("type") or "cast").lower()
        t = _ts(e)
        if typ in {"begincast", "startcast", "begin_cast", "caststart"}:
            starts[aid].append(t)
            while len(starts[aid]) > 4:
                starts[aid].popleft()
            continue
        if not starts[aid]:
            continue
        start = starts[aid].popleft()
        delta = (t - start) / 1000.0
        if 0.02 <= delta <= 30.0:
            observed[aid].append(delta)
    rows = []
    for aid, vals in observed.items():
        rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            "observed_casts": len(vals),
            "start_to_cast_median_s": round(median(vals), 3),
            "start_to_cast_p90_s": round(_percentile(vals, .9), 3),
            "start_to_cast_max_s": round(max(vals), 3),
            "note": "这是日志中 begincast 到 cast 的实际观察时长，不等同于技能数据库中的理论基础施法时间。",
        })
    rows.sort(key=lambda x: x["observed_casts"], reverse=True)
    return rows[:top_n]


def buff_features(
    events: list[dict[str, Any]],
    duration_ms: float,
    abilities: dict[int, str],
    pull_windows: list[tuple[float, float]],
    *,
    top_n: int = 40,
) -> tuple[list[dict[str, Any]], dict[int, list[tuple[float, float]]]]:
    merged_pulls = merge_intervals(pull_windows)
    combat_ms = sum(b - a for a, b in merged_pulls)
    active: dict[int, float] = {}
    windows: defaultdict[int, list[tuple[float, float]]] = defaultdict(list)
    applications: Counter[int] = Counter()
    refreshes: Counter[int] = Counter()
    stack_changes: Counter[int] = Counter()

    for e in sorted(events, key=_ts):
        aid = _ability_id(e)
        if not aid:
            continue
        typ = str(e.get("type") or "").lower()
        t = max(0.0, min(float(duration_ms), _ts(e)))
        if typ == "applybuff":
            applications[aid] += 1
            active.setdefault(aid, t)
        elif typ == "applybuffstack":
            stack_changes[aid] += 1
            if aid not in active:
                applications[aid] += 1
                active[aid] = t
        elif typ == "refreshbuff":
            refreshes[aid] += 1
            if aid not in active:
                applications[aid] += 1
                active[aid] = t
        elif typ == "removebuffstack":
            # A stack decrement is not proof that the aura ended. WCL normally emits
            # removebuff when the aura actually disappears, so keep the window open.
            stack_changes[aid] += 1
        elif typ == "removebuff":
            start = active.pop(aid, None)
            if start is not None and t >= start:
                windows[aid].append((start, t))

    for aid, start in list(active.items()):
        windows[aid].append((start, float(duration_ms)))

    rows: list[dict[str, Any]] = []
    for aid, ws in windows.items():
        ws = merge_intervals(ws)
        total_ms = sum(b - a for a, b in ws)
        combat_overlap = sum(interval_overlap_ms(a, b, merged_pulls) for a, b in ws)
        durations = [(b - a) / 1000.0 for a, b in ws]
        rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            # Backward-compatible field: now explicitly means whole-run uptime.
            "uptime_pct": round(total_ms / max(1.0, duration_ms) * 100.0, 3),
            "whole_run_uptime_pct": round(total_ms / max(1.0, duration_ms) * 100.0, 3),
            "combat_uptime_pct": round(combat_overlap / max(1.0, combat_ms) * 100.0, 3) if combat_ms > 0 else 0.0,
            "applications": int(applications[aid]),
            "refreshes": int(refreshes[aid]),
            "stack_change_events": int(stack_changes[aid]),
            "observed_windows": len(ws),
            "avg_window_s": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "median_window_s": round(median(durations), 3) if durations else 0.0,
            "max_window_s": round(max(durations), 3) if durations else 0.0,
            "combat_active_duration_s": round(combat_ms / 1000.0, 3),
        })
        windows[aid] = ws
    rows.sort(key=lambda x: (x["combat_uptime_pct"], x["applications"], x["whole_run_uptime_pct"]), reverse=True)
    return rows[:top_n], dict(windows)


def burst_windows_from_damage(
    damage: list[dict[str, Any]],
    abilities: dict[int, str],
    *,
    window_s: int = 8,
    top_n: int = 6,
) -> list[dict[str, Any]]:
    if not damage:
        return []
    buckets: defaultdict[int, float] = defaultdict(float)
    events_by_sec: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in damage:
        amount = max(0.0, _num(e.get("amount") or e.get("unmitigatedAmount") or e.get("effectiveAmount")))
        if amount <= 0:
            continue
        sec = max(0, int(_ts(e) // 1000.0))
        buckets[sec] += amount
        events_by_sec[sec].append(e)
    if not buckets:
        return []
    lo, hi = min(buckets), max(buckets)
    # Fight duration is only tens of minutes, so a simple per-second window scan is
    # cheaper and less error-prone than materializing raw damage into the AI prompt.
    candidates: list[tuple[float, int]] = []
    for start in range(lo, hi + 1):
        amount = sum(buckets.get(sec, 0.0) for sec in range(start, start + window_s))
        if amount > 0:
            candidates.append((amount, start))
    candidates.sort(reverse=True)
    selected: list[tuple[float, int]] = []
    for amount, start in candidates:
        end = start + window_s
        if any(not (end <= s or start >= s + window_s) for _, s in selected):
            continue
        selected.append((amount, start))
        if len(selected) >= top_n:
            break
    total_damage = sum(buckets.values())
    out = []
    for rank, (amount, start) in enumerate(selected, 1):
        ability_damage: defaultdict[int, float] = defaultdict(float)
        for sec in range(start, start + window_s):
            for e in events_by_sec.get(sec, []):
                aid = _ability_id(e)
                if aid:
                    ability_damage[aid] += max(0.0, _num(e.get("amount") or e.get("unmitigatedAmount") or e.get("effectiveAmount")))
        top = sorted(ability_damage.items(), key=lambda x: x[1], reverse=True)[:5]
        out.append({
            "rank": rank,
            "start_ms": float(start * 1000),
            "end_ms": float((start + window_s) * 1000),
            "duration_s": float(window_s),
            "damage": round(amount, 2),
            "fight_damage_share_pct": round(amount / total_damage * 100.0, 3) if total_damage else 0.0,
            "top_skills": [{"spell_id": aid, "spell_name": abilities.get(aid, str(aid)), "damage": round(v, 2)} for aid, v in top],
        })
    out.sort(key=lambda x: x["start_ms"])
    return out


def buff_burst_overlap_rows(
    buff_rows: list[dict[str, Any]],
    buff_windows: dict[int, list[tuple[float, float]]],
    burst_windows: list[dict[str, Any]],
    *,
    top_n: int = 30,
) -> list[dict[str, Any]]:
    if not burst_windows:
        return []
    burst_intervals = [(float(x["start_ms"]), float(x["end_ms"])) for x in burst_windows]
    total_burst_ms = sum(b - a for a, b in burst_intervals)
    rows = []
    meta = {int(x.get("spell_id") or 0): x for x in buff_rows}
    for aid, ws in buff_windows.items():
        overlap = 0.0
        covered = 0
        for ba, bb in burst_intervals:
            local = sum(max(0.0, min(bb, wb) - max(ba, wa)) for wa, wb in ws)
            overlap += min(bb - ba, local)
            if local > 0:
                covered += 1
        m = meta.get(aid) or {}
        rows.append({
            "spell_id": aid,
            "spell_name": m.get("spell_name") or str(aid),
            "burst_overlap_pct": round(overlap / max(1.0, total_burst_ms) * 100.0, 3),
            "burst_windows_covered": covered,
            "burst_window_count": len(burst_intervals),
            "combat_uptime_pct": m.get("combat_uptime_pct", 0.0),
            "applications": m.get("applications", 0),
        })
    # Keep zero-overlap auras too: "this buff existed but missed every major damage
    # window" is valuable evidence. Prefer auras that either overlap bursts or recur.
    rows.sort(key=lambda x: (x["burst_overlap_pct"] > 0, x["burst_overlap_pct"], x["applications"]), reverse=True)
    return rows[:top_n]


def pull_breakdown_rows(
    pulls: list[dict[str, Any]],
    pull_windows: list[tuple[float, float]],
    casts: list[dict[str, Any]],
    damage: list[dict[str, Any]],
    deaths: list[dict[str, Any]],
    abilities: dict[int, str],
    *,
    top_n: int = 40,
) -> list[dict[str, Any]]:
    cast_events = successful_cast_events(casts)
    rows = []
    for i, (start, end) in enumerate(pull_windows[:top_n]):
        pc = [e for e in cast_events if start <= _ts(e) <= end]
        pd = [e for e in damage if start <= _ts(e) <= end]
        pdeath = [e for e in deaths if start <= _ts(e) <= end]
        cast_count: Counter[int] = Counter(_ability_id(e) for e in pc if _ability_id(e))
        damage_by: defaultdict[int, float] = defaultdict(float)
        for e in pd:
            aid = _ability_id(e)
            if aid:
                damage_by[aid] += max(0.0, _num(e.get("amount") or e.get("unmitigatedAmount") or e.get("effectiveAmount")))
        total_damage = sum(damage_by.values())
        original = pulls[i] if i < len(pulls) else {}
        rows.append({
            "pull": i + 1,
            "name": str(original.get("name") or f"第{i + 1}波"),
            "duration_s": round((end - start) / 1000.0, 3),
            "casts": len(pc),
            "damage": round(total_damage, 2),
            "deaths": len(pdeath),
            "top_casts": [
                {"spell_id": aid, "spell_name": abilities.get(aid, str(aid)), "casts": count}
                for aid, count in cast_count.most_common(5)
            ],
            "top_damage": [
                {"spell_id": aid, "spell_name": abilities.get(aid, str(aid)), "damage": round(value, 2), "share_pct": round(value / total_damage * 100.0, 2) if total_damage else 0.0}
                for aid, value in sorted(damage_by.items(), key=lambda x: x[1], reverse=True)[:5]
            ],
        })
    return rows
