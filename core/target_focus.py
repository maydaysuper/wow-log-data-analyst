from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any, Iterable


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


def _target_id(event: dict[str, Any]) -> int:
    for key in ("targetID", "targetId", "target_id"):
        try:
            value = int(event.get(key) or 0)
        except Exception:
            value = 0
        if value:
            return value
    return 0


def _ts(event: dict[str, Any]) -> float:
    return _num(event.get("timestamp") if event.get("timestamp") is not None else event.get("time"))


def _amount(event: dict[str, Any]) -> float:
    for key in ("amount", "unmitigatedAmount", "effectiveAmount"):
        value = _num(event.get(key))
        if value > 0:
            return value
    return 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * min(1.0, max(0.0, q))
    lo = int(pos)
    hi = min(len(xs) - 1, lo + 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _dist(values: Iterable[float]) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {}
    return {
        "p25": round(_quantile(vals, 0.25), 3),
        "p50": round(_quantile(vals, 0.50), 3),
        "p75": round(_quantile(vals, 0.75), 3),
        "p90": round(_quantile(vals, 0.90), 3),
        "sample_count": len(vals),
    }


def _actor_maps(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for actor in ((report.get("masterData") or {}).get("actors") or []):
        try:
            actor_id = int(actor.get("id") or 0)
        except Exception:
            actor_id = 0
        if actor_id:
            out[actor_id] = dict(actor)
    return out


def _pull_rows(fight: dict[str, Any], fight_start_ms: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, pull in enumerate(fight.get("dungeonPulls") or []):
        start = max(0.0, _num(pull.get("startTime")) - fight_start_ms)
        end = max(start, _num(pull.get("endTime")) - fight_start_ms)
        if end <= start:
            continue
        npc_ids: set[int] = set()
        for npc in pull.get("enemyNPCs") or []:
            try:
                gid = int(npc.get("gameID") or 0)
            except Exception:
                gid = 0
            if gid:
                npc_ids.add(gid)
        rows.append({
            "index": i,
            "id": int(_num(pull.get("id"))),
            "name": str(pull.get("name") or f"第{i+1}波"),
            "start_ms": start,
            "end_ms": end,
            "encounter_id": int(_num(pull.get("encounterID"))),
            "kill": bool(pull.get("kill")),
            "enemy_npc_ids": npc_ids,
        })
    return rows


def _pull_for_ts(ts: float, pulls: list[dict[str, Any]]) -> dict[str, Any] | None:
    for pull in pulls:
        if pull["start_ms"] <= ts <= pull["end_ms"]:
            return pull
    return None


def _successful_casts(casts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in sorted(casts or [], key=_ts):
        typ = str(event.get("type") or "cast").lower()
        if typ in {"begincast", "startcast", "begin_cast", "caststart"}:
            continue
        if _ability_id(event):
            out.append(event)
    return out


def build_target_focus_profile(
    report: dict[str, Any],
    fight: dict[str, Any],
    casts: list[dict[str, Any]],
    damage: list[dict[str, Any]],
    *,
    fight_start_ms: float = 0.0,
    early_window_s: float = 8.0,
    limit: int = 40,
) -> dict[str, Any]:
    """Build a stable per-NPC target-damage/focus profile for one run.

    ``casts`` and ``damage`` are expected to have timestamps relative to fight start.
    WCL actor report IDs are mapped to stable NPC ``gameID`` values so the same target
    can be compared across reports. The function does not decide that a trash mob is a
    mandatory priority target by itself; that label is learned only after recurrence
    across multiple timed runs.
    """
    actors = _actor_maps(report)
    pulls = _pull_rows(fight, fight_start_ms)
    early_ms = max(1000.0, float(early_window_s) * 1000.0)
    duration_s = max(0.001, (_num(fight.get("endTime")) - _num(fight.get("startTime"))) / 1000.0)

    def actor_info(report_actor_id: int) -> tuple[int, str, str, str] | None:
        actor = actors.get(int(report_actor_id)) or {}
        typ = str(actor.get("type") or "")
        subtype = str(actor.get("subType") or "")
        # Enemy targets of interest are NPCs/Bosses. Pets/summons are too unstable to
        # become dungeon-priority knowledge and are intentionally ignored here.
        if typ.lower() != "npc" and "boss" not in subtype.lower():
            return None
        try:
            game_id = int(float(actor.get("gameID") or 0))
        except Exception:
            game_id = 0
        if game_id <= 0:
            return None
        name = str(actor.get("name") or f"NPC {game_id}")
        return game_id, name, typ, subtype

    damage_by: defaultdict[int, float] = defaultdict(float)
    damage_events: Counter[int] = Counter()
    names: dict[int, str] = {}
    subtypes: dict[int, str] = {}
    actor_report_ids: defaultdict[int, set[int]] = defaultdict(set)
    pulls_seen: defaultdict[int, set[int]] = defaultdict(set)
    boss_pulls_seen: defaultdict[int, set[int]] = defaultdict(set)
    first_hit_delay: defaultdict[int, list[float]] = defaultdict(list)
    first_hit_per_pull: dict[tuple[int, int], float] = {}
    early_damage_by: defaultdict[int, float] = defaultdict(float)
    pull_damage_total: defaultdict[int, float] = defaultdict(float)
    pull_damage_by: defaultdict[tuple[int, int], float] = defaultdict(float)
    pull_early_damage_total: defaultdict[int, float] = defaultdict(float)
    pull_early_damage_by: defaultdict[tuple[int, int], float] = defaultdict(float)
    total_npc_damage = 0.0
    total_early_damage = 0.0

    for event in damage or []:
        amount = _amount(event)
        if amount <= 0:
            continue
        rid = _target_id(event)
        info = actor_info(rid)
        if not info:
            continue
        npc_id, name, _typ, subtype = info
        ts = _ts(event)
        pull = _pull_for_ts(ts, pulls)
        damage_by[npc_id] += amount
        total_npc_damage += amount
        damage_events[npc_id] += 1
        names[npc_id] = name
        subtypes[npc_id] = subtype
        actor_report_ids[npc_id].add(rid)
        if pull:
            pi = int(pull["index"])
            pulls_seen[npc_id].add(pi)
            pull_damage_total[pi] += amount
            pull_damage_by[(npc_id, pi)] += amount
            if int(pull.get("encounter_id") or 0) > 0:
                boss_pulls_seen[npc_id].add(pi)
            delay = max(0.0, (ts - float(pull["start_ms"])) / 1000.0)
            key = (npc_id, pi)
            if key not in first_hit_per_pull or delay < first_hit_per_pull[key]:
                first_hit_per_pull[key] = delay
            if ts <= float(pull["start_ms"]) + early_ms:
                early_damage_by[npc_id] += amount
                total_early_damage += amount
                pull_early_damage_total[pi] += amount
                pull_early_damage_by[(npc_id, pi)] += amount

    for (npc_id, _pi), delay in first_hit_per_pull.items():
        first_hit_delay[npc_id].append(delay)

    target_casts: Counter[int] = Counter()
    early_target_casts: Counter[int] = Counter()
    pull_targeted_cast_total: Counter[int] = Counter()
    pull_targeted_cast_by: Counter[tuple[int, int]] = Counter()
    pull_early_targeted_cast_total: Counter[int] = Counter()
    pull_early_targeted_cast_by: Counter[tuple[int, int]] = Counter()
    total_targeted_casts = 0
    total_early_targeted_casts = 0
    for event in _successful_casts(casts or []):
        rid = _target_id(event)
        info = actor_info(rid)
        if not info:
            continue
        npc_id, name, _typ, subtype = info
        names[npc_id] = name
        subtypes[npc_id] = subtype
        target_casts[npc_id] += 1
        total_targeted_casts += 1
        pull = _pull_for_ts(_ts(event), pulls)
        if pull:
            pi = int(pull["index"])
            pulls_seen[npc_id].add(pi)
            pull_targeted_cast_total[pi] += 1
            pull_targeted_cast_by[(npc_id, pi)] += 1
            if _ts(event) <= float(pull["start_ms"]) + early_ms:
                early_target_casts[npc_id] += 1
                total_early_targeted_casts += 1
                pull_early_targeted_cast_total[pi] += 1
                pull_early_targeted_cast_by[(npc_id, pi)] += 1

    rows: list[dict[str, Any]] = []
    all_ids = set(damage_by) | set(target_casts)
    for npc_id in all_ids:
        dmg = float(damage_by.get(npc_id, 0.0))
        direct_casts = int(target_casts.get(npc_id, 0))
        subtype = str(subtypes.get(npc_id) or "")
        is_boss = "boss" in subtype.lower()
        in_boss_pull = bool(boss_pulls_seen.get(npc_id))
        damage_share = dmg / total_npc_damage * 100.0 if total_npc_damage else 0.0
        early_damage_share = float(early_damage_by.get(npc_id, 0.0)) / total_early_damage * 100.0 if total_early_damage else 0.0
        cast_share = direct_casts / total_targeted_casts * 100.0 if total_targeted_casts else 0.0
        early_cast_share = int(early_target_casts.get(npc_id, 0)) / total_early_targeted_casts * 100.0 if total_early_targeted_casts else 0.0
        delays = first_hit_delay.get(npc_id) or []
        presence = {int(p["index"]) for p in pulls if npc_id in (p.get("enemy_npc_ids") or set())}
        presence.update(pulls_seen.get(npc_id, set()))
        pull_damage_shares = [
            float(pull_damage_by.get((npc_id, pi), 0.0)) / float(pull_damage_total.get(pi, 0.0)) * 100.0
            for pi in presence if float(pull_damage_total.get(pi, 0.0)) > 0
        ]
        pull_cast_shares = [
            float(pull_targeted_cast_by.get((npc_id, pi), 0)) / float(pull_targeted_cast_total.get(pi, 0)) * 100.0
            for pi in presence if int(pull_targeted_cast_total.get(pi, 0)) > 0
        ]
        pull_early_cast_shares = [
            float(pull_early_targeted_cast_by.get((npc_id, pi), 0)) / float(pull_early_targeted_cast_total.get(pi, 0)) * 100.0
            for pi in presence if int(pull_early_targeted_cast_total.get(pi, 0)) > 0
        ]
        pull_early_damage_shares = [
            float(pull_early_damage_by.get((npc_id, pi), 0.0)) / float(pull_early_damage_total.get(pi, 0.0)) * 100.0
            for pi in presence if float(pull_early_damage_total.get(pi, 0.0)) > 0
        ]
        rows.append({
            "npc_id": int(npc_id),
            "npc_name": names.get(npc_id, str(npc_id)),
            "npc_subtype": subtype,
            "is_boss": bool(is_boss),
            "seen_in_boss_pull": bool(in_boss_pull),
            "damage": round(dmg, 2),
            "target_dps_over_run": round(dmg / duration_s, 2),
            "damage_share_pct": round(damage_share, 3),
            "damage_events": int(damage_events.get(npc_id, 0)),
            "targeted_casts": direct_casts,
            "targeted_cast_share_pct": round(cast_share, 3),
            "early_damage_share_pct": round(early_damage_share, 3),
            "early_targeted_casts": int(early_target_casts.get(npc_id, 0)),
            "early_targeted_cast_share_pct": round(early_cast_share, 3),
            "pulls_seen": len(pulls_seen.get(npc_id, set())),
            "pull_presence_count": len(presence),
            "boss_pulls_seen": len(boss_pulls_seen.get(npc_id, set())),
            "pull_damage_share_when_present_pct": round(float(median(pull_damage_shares)), 3) if pull_damage_shares else 0.0,
            "targeted_cast_share_when_present_pct": round(float(median(pull_cast_shares)), 3) if pull_cast_shares else 0.0,
            "early_focus_when_present_pct": round(float(median(pull_early_cast_shares)), 3) if pull_early_cast_shares else 0.0,
            "early_damage_when_present_pct": round(float(median(pull_early_damage_shares)), 3) if pull_early_damage_shares else 0.0,
            "first_hit_delay_median_s": round(float(median(delays)), 3) if delays else None,
            "stable_npc_identity": True,
        })

    # Keep bosses even when damage is low, then prioritize direct focus and damage share.
    rows.sort(
        key=lambda x: (
            1 if x.get("is_boss") else 0,
            float(x.get("early_targeted_cast_share_pct") or 0),
            float(x.get("targeted_cast_share_pct") or 0),
            float(x.get("damage_share_pct") or 0),
        ),
        reverse=True,
    )
    rows = rows[: max(10, int(limit))]
    boss_rows = [r for r in rows if r.get("is_boss")]
    boss_damage = sum(float(r.get("damage") or 0) for r in boss_rows)
    return {
        "sample_type": "player_target_focus",
        "target_count": len(rows),
        "boss_target_count": len(boss_rows),
        "boss_damage": round(boss_damage, 2),
        "boss_damage_share_pct": round(boss_damage / total_npc_damage * 100.0, 3) if total_npc_damage else 0.0,
        "total_npc_damage": round(total_npc_damage, 2),
        "total_targeted_casts": total_targeted_casts,
        "early_window_s": round(early_ms / 1000.0, 1),
        "targets": rows,
        "limitations": [
            "单场只描述玩家把伤害/直接目标技能放到了哪些NPC上；不会仅凭一场就认定某只小怪必须优先击杀。",
            "AoE技能可能没有明确targetID，因此“直接目标技能占比”是保守证据，不等于全部集火行为。",
            "跨Log比较使用稳定NPC gameID；宠物/临时召唤物默认不进入副本优先目标知识。",
        ],
    }


def aggregate_target_knowledge(
    signatures: list[dict[str, Any]],
    *,
    min_samples: int = 2,
    limit: int = 40,
) -> dict[str, Any]:
    """Learn dungeon target importance from repeated timed-run behavior.

    This is intentionally an empirical model. A trash mob becomes a "priority focus"
    candidate only when several runs repeatedly direct early targeted casts / damage to
    the same stable NPC id. Bosses are identified separately from report actor metadata.
    """
    usable = [s for s in signatures if (s.get("target_focus") or {}).get("targets")]
    sample_count = len(usable)
    if sample_count == 0:
        return {
            "sample_count": 0,
            "bosses": [],
            "priority_targets": [],
            "important_targets": [],
            "all_targets": [],
            "limitations": ["没有带目标伤害特征的样本。"],
        }

    buckets: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    names: defaultdict[int, Counter[str]] = defaultdict(Counter)
    subtypes: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for sig in usable:
        seen_this_sample: set[int] = set()
        for row in (sig.get("target_focus") or {}).get("targets") or []:
            try:
                npc_id = int(row.get("npc_id") or 0)
            except Exception:
                npc_id = 0
            if npc_id <= 0 or npc_id in seen_this_sample:
                continue
            seen_this_sample.add(npc_id)
            buckets[npc_id].append(dict(row))
            if row.get("npc_name"):
                names[npc_id][str(row.get("npc_name"))] += 1
            if row.get("npc_subtype"):
                subtypes[npc_id][str(row.get("npc_subtype"))] += 1

    learned: list[dict[str, Any]] = []
    min_seen = max(1, int(min_samples))
    for npc_id, rows in buckets.items():
        seen = len(rows)
        if seen < min_seen and not any(bool(r.get("is_boss")) for r in rows):
            continue
        recurrence_pct = seen / sample_count * 100.0 if sample_count else 0.0
        damage_dist = _dist(float(r.get("damage_share_pct") or 0) for r in rows)
        dps_dist = _dist(float(r.get("target_dps_over_run") or 0) for r in rows)
        cast_dist = _dist(float(r.get("targeted_cast_share_pct") or 0) for r in rows)
        early_cast_dist = _dist(float(r.get("early_targeted_cast_share_pct") or 0) for r in rows)
        early_dmg_dist = _dist(float(r.get("early_damage_share_pct") or 0) for r in rows)
        local_damage_dist = _dist(float(r.get("pull_damage_share_when_present_pct") or 0) for r in rows)
        local_cast_dist = _dist(float(r.get("targeted_cast_share_when_present_pct") or 0) for r in rows)
        local_early_cast_dist = _dist(float(r.get("early_focus_when_present_pct") or 0) for r in rows)
        local_early_damage_dist = _dist(float(r.get("early_damage_when_present_pct") or 0) for r in rows)
        delay_dist = _dist(float(r.get("first_hit_delay_median_s")) for r in rows if r.get("first_hit_delay_median_s") is not None)
        is_boss = sum(1 for r in rows if r.get("is_boss")) >= max(1, (seen + 1) // 2)
        boss_pull = sum(1 for r in rows if r.get("seen_in_boss_pull")) >= max(1, (seen + 1) // 2)

        # Prefer per-appearance focus. Global run shares dilute a dangerous mob that
        # appears in only one or two pulls and were causing real priority targets to be
        # missed in long dungeons.
        early = float((local_early_cast_dist or {}).get("p50") or (early_cast_dist or {}).get("p50") or 0)
        direct = float((local_cast_dist or {}).get("p50") or (cast_dist or {}).get("p50") or 0)
        dmg_share = float((local_damage_dist or {}).get("p50") or (damage_dist or {}).get("p50") or 0)
        early_dmg = float((local_early_damage_dist or {}).get("p50") or (early_dmg_dist or {}).get("p50") or 0)
        rec_n = min(1.0, recurrence_pct / 65.0)
        early_n = min(1.0, early / 45.0)
        direct_n = min(1.0, direct / 35.0)
        dmg_n = min(1.0, max(dmg_share, early_dmg) / 30.0)
        focus_score = 100.0 if is_boss else min(
            99.0,
            100.0 * (0.20 * rec_n + 0.40 * early_n + 0.20 * direct_n + 0.20 * dmg_n)
            + (7.0 if boss_pull else 0.0),
        )

        if is_boss:
            role = "boss"
            role_cn = "BOSS"
        elif seen >= max(2, min_seen) and focus_score >= 58 and (early >= 22 or direct >= 18 or early_dmg >= 28):
            role = "priority_focus"
            role_cn = "常见优先集火目标"
        elif seen >= max(2, min_seen) and focus_score >= 42:
            role = "important_large"
            role_cn = "重要大怪/高价值目标"
        else:
            role = "normal"
            role_cn = "一般目标"

        if seen >= 8 and recurrence_pct >= 55:
            confidence = "high"
        elif seen >= 4:
            confidence = "medium"
        else:
            confidence = "low"

        learned.append({
            "npc_id": npc_id,
            "npc_name": names[npc_id].most_common(1)[0][0] if names[npc_id] else str(npc_id),
            "npc_subtype": subtypes[npc_id].most_common(1)[0][0] if subtypes[npc_id] else "",
            "learned_role": role,
            "learned_role_cn": role_cn,
            "focus_score": round(focus_score, 2),
            "confidence": confidence,
            "samples_seen": seen,
            "sample_count": sample_count,
            "recurrence_pct": round(recurrence_pct, 2),
            "seen_in_boss_pull": bool(boss_pull),
            "damage_share_pct": damage_dist,
            "target_dps_over_run": dps_dist,
            "targeted_cast_share_pct": cast_dist,
            "early_targeted_cast_share_pct": early_cast_dist,
            "early_damage_share_pct": early_dmg_dist,
            "pull_damage_share_when_present_pct": local_damage_dist,
            "targeted_cast_share_when_present_pct": local_cast_dist,
            "early_focus_when_present_pct": local_early_cast_dist,
            "early_damage_when_present_pct": local_early_damage_dist,
            "first_hit_delay_s": delay_dist,
            "interpretation_guardrail": (
                "这是多份限时WCL中观察到的集火行为模式，不等于游戏机制文档。"
                "高优先级表示参考玩家经常更早/更集中攻击该目标；路线、词缀、队伍职责仍可能改变实际优先级。"
            ),
        })

    role_order = {"boss": 0, "priority_focus": 1, "important_large": 2, "normal": 3}
    learned.sort(key=lambda r: (role_order.get(str(r.get("learned_role")), 9), -float(r.get("focus_score") or 0), -int(r.get("samples_seen") or 0)))
    learned = learned[: max(12, int(limit))]
    return {
        "sample_count": sample_count,
        "bosses": [r for r in learned if r.get("learned_role") == "boss"],
        "priority_targets": [r for r in learned if r.get("learned_role") == "priority_focus"],
        "important_targets": [r for r in learned if r.get("learned_role") == "important_large"],
        "all_targets": learned,
        "method": "跨限时WCL按稳定NPC ID聚合：重复出现率 + 目标出现波次内的前8秒直接集火 + 波次内目标技能占比 + 波次内目标伤害占比；BOSS单独识别。",
        "limitations": [
            "优先击杀大怪来自WCL行为学习，不是官方机制标签；只有多场重复出现后才提高置信度。",
            "某些职业依赖无目标AoE/顺劈，因此直接目标技能指标可能低估实际集火；同时保留目标伤害占比辅助判断。",
            "不同路线、词缀、控制/打断职责会改变某波的实际优先级，AI报告必须结合当前Pull和样本匹配质量。",
        ],
    }


def _metric_value(value: Any, key: str = "p50") -> float:
    if isinstance(value, dict):
        return _num(value.get(key))
    return _num(value)


def compare_target_profiles(
    current_profile: dict[str, Any],
    reference_profile: dict[str, Any],
    role_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare one player's target investment to a same-spec cohort.

    ``reference_profile`` supplies the numerical same-spec distributions.  An optional
    dungeon-wide ``role_profile`` supplies stronger boss/priority-target labels learned
    across every available timed sample.  Keeping those roles separate is important:
    cross-class behavior may identify *what* is commonly focused, while the quantitative
    comparison still comes from the user's own spec.
    """
    current_map = {
        int(r.get("npc_id") or 0): r
        for r in (current_profile.get("targets") or current_profile.get("all_targets") or [])
        if int(r.get("npc_id") or 0) > 0
    }
    ref_map = {
        int(r.get("npc_id") or 0): r
        for r in (reference_profile.get("all_targets") or [])
        if int(r.get("npc_id") or 0) > 0
    }
    role_profile = role_profile or {}
    role_map = {
        int(r.get("npc_id") or 0): r
        for r in (role_profile.get("all_targets") or [])
        if int(r.get("npc_id") or 0) > 0
    }

    # Use the union so a dungeon-wide learned priority target is not hidden simply
    # because the current same-spec cohort is still small. Numerical comparisons remain
    # empty until the same-spec cohort has actual measurements for that NPC.
    candidate_ids: list[int] = []
    for source in (role_map, ref_map):
        for npc_id, row in source.items():
            role = str(row.get("learned_role") or "normal")
            if role in {"boss", "priority_focus", "important_large"} and npc_id not in candidate_ids:
                candidate_ids.append(npc_id)

    rows: list[dict[str, Any]] = []
    for npc_id in candidate_ids:
        ref = ref_map.get(npc_id) or {}
        role_ref = role_map.get(npc_id) or ref
        role = str(role_ref.get("learned_role") or ref.get("learned_role") or "normal")
        if role not in {"boss", "priority_focus", "important_large"}:
            continue
        cur = current_map.get(npc_id) or {}
        cur_share = _metric_value(cur.get("damage_share_pct"))
        cur_dps = _metric_value(cur.get("target_dps_over_run"))
        cur_early = _metric_value(cur.get("early_targeted_cast_share_pct"))
        share_dist = ref.get("damage_share_pct") or {}
        dps_dist = ref.get("target_dps_over_run") or {}
        early_dist = ref.get("early_targeted_cast_share_pct") or {}
        ref_share = float(share_dist.get("p50") or 0)
        ref_share_hi = float(share_dist.get("p75") or 0)
        ref_dps = float(dps_dist.get("p50") or 0)
        ref_early = float(early_dist.get("p50") or 0)
        metric_samples = int(share_dist.get("sample_count") or ref.get("samples_seen") or 0)
        delta_share = cur_share - ref_share
        ratio = cur_share / ref_share if ref_share > 1e-9 else (1.0 if cur_share <= 0 else 99.0)
        if ref_share <= 0 or metric_samples <= 0:
            label = "已识别重要目标，暂缺同专精数值参考"
        elif ratio < 0.78:
            label = "低于同专精参考玩家常见投入"
        elif ratio > 1.22:
            label = "高于同专精参考玩家常见投入"
        else:
            label = "接近同专精参考玩家常见投入"
        rows.append({
            "npc_id": npc_id,
            "npc_name": str(role_ref.get("npc_name") or ref.get("npc_name") or npc_id),
            "role": role,
            "role_cn": str(role_ref.get("learned_role_cn") or ref.get("learned_role_cn") or role),
            "confidence": str(role_ref.get("confidence") or ref.get("confidence") or "low"),
            "role_samples_seen": int(role_ref.get("samples_seen") or 0),
            "same_spec_samples_seen": metric_samples,
            "current_damage_share_pct": round(cur_share, 3),
            "reference_damage_share_pct": round(ref_share, 3),
            "reference_high_damage_share_pct": round(ref_share_hi, 3),
            "damage_share_delta_points": round(delta_share, 3),
            "current_target_dps": round(cur_dps, 2),
            "reference_target_dps": round(ref_dps, 2),
            "current_early_focus_pct": round(cur_early, 3),
            "reference_early_focus_pct": round(ref_early, 3),
            "comparison_label": label,
            "present_in_current_run": bool(cur),
            "same_spec_reference_available": bool(ref_share > 0 and metric_samples > 0),
        })
    role_order = {"boss": 0, "priority_focus": 1, "important_large": 2}
    rows.sort(key=lambda r: (
        role_order.get(str(r.get("role")), 9),
        0 if r.get("same_spec_reference_available") else 1,
        -int(r.get("role_samples_seen") or 0),
        -float(r.get("reference_damage_share_pct") or 0),
    ))
    chart_rows = [r for r in rows if r.get("same_spec_reference_available")][:8]
    return {
        "reference_sample_count": int(reference_profile.get("sample_count") or 0),
        "dungeon_role_sample_count": int(role_profile.get("sample_count") or 0),
        "rows": rows[:16],
        "chart_rows": chart_rows,
        "guardrail": (
            "重要目标角色可由副本级多职业WCL行为库识别，但数值横向比较仍只使用同专精参考样本。"
            "priority_focus/important_large 是经验行为标签，不等于官方机制或固定击杀顺序。"
        ),
    }


def compare_signature_target_focus(
    signature: dict[str, Any],
    reference_profile: dict[str, Any],
    role_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compare_target_profiles(signature.get("target_focus") or {}, reference_profile or {}, role_profile or {})
