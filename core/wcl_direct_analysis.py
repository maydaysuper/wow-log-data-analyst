from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .online_learning import build_wcl_behavior_signature


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _master_maps(report: dict[str, Any]) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    md = report.get("masterData") or {}
    abilities: dict[int, str] = {}
    actors: dict[int, dict[str, Any]] = {}
    for a in md.get("abilities") or []:
        try:
            abilities[int(a.get("gameID") or 0)] = str(a.get("name") or a.get("gameID") or "")
        except Exception:
            continue
    for a in md.get("actors") or []:
        try:
            actors[int(a.get("id") or 0)] = a
        except Exception:
            continue
    return abilities, actors


def resolve_report_actor(
    report: dict[str, Any],
    *,
    character_name: str = "",
    server_slug: str = "",
    source_id: int | None = None,
) -> tuple[int, str, str]:
    """Resolve a report actor and return (source_id, player_name, class_name)."""
    _, actors = _master_maps(report)
    if source_id and int(source_id) in actors:
        actor = actors[int(source_id)]
        return int(source_id), str(actor.get("name") or source_id), str(actor.get("subType") or "")

    name_l = character_name.strip().lower()
    server_l = server_slug.strip().lower().replace(" ", "-")
    candidates = []
    for aid, actor in actors.items():
        if str(actor.get("type") or "").lower() != "player":
            continue
        if name_l and str(actor.get("name") or "").strip().lower() != name_l:
            continue
        score = 1
        srv = actor.get("server")
        if isinstance(srv, dict):
            srv_name = str(srv.get("slug") or srv.get("name") or "").strip().lower().replace(" ", "-")
        else:
            srv_name = str(srv or "").strip().lower().replace(" ", "-")
        if server_l and srv_name == server_l:
            score += 4
        candidates.append((score, aid, actor))
    if not candidates:
        return 0, character_name, ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, aid, actor = candidates[0]
    return int(aid), str(actor.get("name") or character_name), str(actor.get("subType") or "")


def spec_and_item_level_for_fight(fight: dict[str, Any], source_id: int) -> tuple[str, int]:
    players = [int(x) for x in (fight.get("friendlyPlayers") or []) if x is not None]
    specs = list(fight.get("friendlySpecs") or [])
    levels = list(fight.get("friendlyItemLevels") or [])
    try:
        idx = players.index(int(source_id))
    except ValueError:
        return "", 0
    spec = str(specs[idx]) if idx < len(specs) and specs[idx] is not None else ""
    try:
        ilvl = int(levels[idx]) if idx < len(levels) and levels[idx] is not None else 0
    except Exception:
        ilvl = 0
    return spec, ilvl


def fights_for_source(report: dict[str, Any], source_id: int) -> list[dict[str, Any]]:
    out = []
    for f in report.get("fights") or []:
        try:
            players = {int(x) for x in (f.get("friendlyPlayers") or [])}
        except Exception:
            players = set()
        if int(source_id) in players:
            out.append(f)
    return out


def _ability_id(e: dict[str, Any]) -> int:
    for key in ("abilityGameID", "abilityID", "gameID"):
        try:
            v = int(e.get(key) or 0)
        except Exception:
            v = 0
        if v:
            return v
    ability = e.get("ability")
    if isinstance(ability, dict):
        try:
            return int(ability.get("gameID") or ability.get("id") or 0)
        except Exception:
            pass
    return 0


def _event_ts(e: dict[str, Any]) -> float:
    return _num(e.get("timestamp"))


def _event_source(e: dict[str, Any]) -> int:
    for key in ("sourceID", "sourceId", "source_id", "actorID"):
        try:
            v = int(e.get(key) or 0)
        except Exception:
            v = 0
        if v:
            return v
    return 0


def fetch_event_bundle(client, report_code: str, fight_id: int, specs: dict[str, dict[str, Any]], max_workers: int = 4) -> dict[str, list[dict[str, Any]]]:
    """Fetch independent WCL event streams concurrently with forked HTTP sessions.

    The parent OAuth token is reused, so this improves wall-clock time without extra auth
    requests. Each worker owns its requests.Session to avoid cross-thread session mutation.
    """
    if not specs:
        return {}

    def one(label: str, cfg: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        child = client.fork() if hasattr(client, "fork") else client
        try:
            data = child.report_events(report_code, fight_id, **cfg)
            return label, data
        finally:
            if child is not client and hasattr(child, "close"):
                child.close()

    out: dict[str, list[dict[str, Any]]] = {}
    workers = max(1, min(int(max_workers), len(specs), 5))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wcl-events") as pool:
        futures = {pool.submit(one, label, cfg): label for label, cfg in specs.items()}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                k, data = fut.result()
                out[k] = data
            except Exception:
                # Required streams are validated by callers; optional streams degrade gracefully.
                out[label] = []
    return out


def team_activity_stalls(
    player_casts: list[dict[str, Any]],
    team_casts: list[dict[str, Any]],
    player_damage: list[dict[str, Any]],
    *,
    source_id: int,
    friendly_ids: set[int],
    deaths: list[dict[str, Any]] | None = None,
    damage_taken: list[dict[str, Any]] | None = None,
    pull_windows: list[tuple[float, float]] | None = None,
    min_gap_s: float = 2.8,
    min_other_casts: int = 3,
) -> dict[str, Any]:
    """Find *alive, in-combat* player action gaps while teammates keep fighting.

    A gap is not considered a responsiveness anomaly when the player dies during it.
    For Mythic+ runs with dungeon-pull timing, the gap midpoint must also be inside a
    combat pull. This prevents deaths, travel, RP and inter-pull downtime from being
    mislabeled as a stall. Even a valid gap is evidence of an action interruption, not
    proof of network lag.
    """
    player_ts = sorted({_event_ts(e) for e in player_casts if _event_ts(e) > 0})
    friendly_other = sorted(
        (_event_ts(e), _event_source(e)) for e in team_casts
        if _event_ts(e) > 0 and _event_source(e) in friendly_ids and _event_source(e) != int(source_id)
    )
    dmg_ts = sorted(_event_ts(e) for e in player_damage if _event_ts(e) > 0)
    incoming_ts = sorted(_event_ts(e) for e in (damage_taken or []) if _event_ts(e) > 0)
    death_ts = sorted(_event_ts(e) for e in (deaths or []) if _event_ts(e) > 0)
    pulls = list(pull_windows or [])
    windows: list[dict[str, Any]] = []
    death_excluded = 0
    outside_combat_excluded = 0

    for a, b in zip(player_ts, player_ts[1:]):
        gap_s = (b - a) / 1000.0
        if gap_s < min_gap_s:
            continue
        # A cast before and after a death creates a huge apparent gap; this is never a
        # responsiveness stall because the player was dead for part of the interval.
        if any(a <= d <= b for d in death_ts):
            death_excluded += 1
            continue
        if pulls:
            # Require the whole apparent stall to belong to one combat pull (small
            # grace for event ordering). A gap spanning two pulls is route downtime,
            # not a responsiveness stall.
            if not any(a >= ps - 750 and b <= pe + 750 for ps, pe in pulls):
                outside_combat_excluded += 1
                continue

        other = [(t, sid) for t, sid in friendly_other if a < t < b]
        if len(other) < min_other_casts:
            continue
        dmg_count = sum(1 for t in dmg_ts if a < t < b)
        incoming_count = sum(1 for t in incoming_ts if a < t < b)
        casters = len({sid for _, sid in other})
        windows.append({
            "start_ms": round(a, 2), "end_ms": round(b, 2), "gap_s": round(gap_s, 3),
            "other_friendly_casts": len(other), "other_friendly_casters": casters,
            "player_damage_events_during_gap": dmg_count,
            "player_incoming_hits_during_gap": incoming_count,
            "player_alive_for_entire_gap": True,
            "inside_combat_pull": True if pulls else None,
            "interpretation": "玩家存活且处于战斗窗口时没有施法，而其他队员仍持续施法；这是单人动作空档证据，不等于网络故障证明。",
        })
    windows.sort(key=lambda x: (x["gap_s"], x["other_friendly_casts"]), reverse=True)
    return {
        "isolated_gap_count": len(windows),
        "severe_isolated_gap_count": sum(1 for x in windows if x["gap_s"] >= 4.5),
        "max_isolated_gap_s": max((x["gap_s"] for x in windows), default=0.0),
        "death_excluded_gap_count": death_excluded,
        "outside_combat_excluded_gap_count": outside_combat_excluded,
        "windows": windows[:12],
        "guardrail": "只统计玩家全程存活、且处于战斗窗口的单人停手。死亡、波次间跑图/RP不会计入；移动、机制、失去目标、资源等待、输入或网络问题仍需结合更多证据区分。",
    }


def _top_event_amounts(events: list[dict[str, Any]], abilities: dict[int, str], limit: int = 12) -> list[dict[str, Any]]:
    amounts: defaultdict[int, float] = defaultdict(float)
    counts: Counter[int] = Counter()
    for e in events:
        aid = _ability_id(e)
        amount = max(0.0, _num(e.get("amount") or e.get("unmitigatedAmount") or e.get("effectiveAmount")))
        if aid and amount > 0:
            amounts[aid] += amount
            counts[aid] += 1
    total = sum(amounts.values())
    rows = []
    for aid, amount in sorted(amounts.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            "amount": round(amount, 2),
            "share_pct": round(amount / total * 100, 3) if total else 0.0,
            "events": int(counts[aid]),
        })
    return rows


def build_targeted_wcl_payload(
    client,
    report_code: str,
    fight_id: int,
    *,
    character_name: str = "",
    server_slug: str = "",
    source_id: int | None = None,
) -> dict[str, Any]:
    """Fetch one selected WCL fight and turn it into an AI-ready targeted payload."""
    raw = client.report_with_talents(report_code)
    report = (((raw or {}).get("reportData") or {}).get("report") or {})
    if not report:
        raise ValueError("WCL Report 不存在或当前凭据无权访问。")
    sid, player_name, class_name = resolve_report_actor(
        report, character_name=character_name, server_slug=server_slug, source_id=source_id
    )
    if not sid:
        raise ValueError("在这份 Report 里找不到目标角色。")
    fight = next((f for f in (report.get("fights") or []) if int(f.get("id") or 0) == int(fight_id)), None)
    if not fight:
        raise ValueError("找不到选择的 Fight。")
    if sid not in {int(x) for x in (fight.get("friendlyPlayers") or [])}:
        raise ValueError("目标角色没有参加这一场 Fight。")
    spec_name, player_ilvl = spec_and_item_level_for_fight(fight, sid)

    bundle = fetch_event_bundle(client, report_code, fight_id, {
        "casts": {"data_type": "Casts", "source_id": sid, "limit": 10000, "max_pages": 10},
        "damage": {"data_type": "DamageDone", "source_id": sid, "limit": 10000, "max_pages": 16},
        "buffs": {"data_type": "Buffs", "target_id": sid, "limit": 10000, "max_pages": 10},
        "resources": {"data_type": "Resources", "source_id": sid, "include_resources": True, "limit": 10000, "max_pages": 10},
        "damage_taken": {"data_type": "DamageTaken", "target_id": sid, "limit": 10000, "max_pages": 12},
        "interrupts": {"data_type": "Interrupts", "source_id": sid, "limit": 10000, "max_pages": 6},
        "deaths": {"data_type": "Deaths", "target_id": sid, "limit": 10000, "max_pages": 4},
        "team_casts": {"data_type": "Casts", "limit": 10000, "max_pages": 12},
    }, max_workers=4)
    casts = bundle.get("casts") or []
    damage = bundle.get("damage") or []
    buffs = bundle.get("buffs") or []
    resources = bundle.get("resources") or []
    damage_taken = bundle.get("damage_taken") or []
    interrupts = bundle.get("interrupts") or []
    deaths = bundle.get("deaths") or []
    team_casts = bundle.get("team_casts") or []
    if not casts and not damage:
        raise RuntimeError("WCL 没有读取到该玩家的 Cast/Damage 事件，请稍后重试。")

    signature = build_wcl_behavior_signature(
        report=report,
        fight=fight,
        player_name=player_name,
        source_id=sid,
        class_name=class_name,
        spec_name=spec_name,
        casts=casts,
        damage=damage,
        buffs=buffs,
        resources=resources,
        deaths=deaths,
        ranking={},
    )
    abilities, _ = _master_maps(report)
    friendly_ids = {int(x) for x in (fight.get("friendlyPlayers") or []) if x is not None}
    pull_windows = []
    for pull in fight.get("dungeonPulls") or []:
        ps = _num(pull.get("startTime")); pe = _num(pull.get("endTime"))
        if pe > ps:
            pull_windows.append((ps, pe))
    isolated = team_activity_stalls(
        casts, team_casts, damage, source_id=sid, friendly_ids=friendly_ids,
        deaths=deaths, damage_taken=damage_taken, pull_windows=pull_windows,
    ) if team_casts else {
        "isolated_gap_count": 0, "severe_isolated_gap_count": 0, "max_isolated_gap_s": 0.0,
        "death_excluded_gap_count": 0, "outside_combat_excluded_gap_count": 0, "windows": [],
        "guardrail": "整队施法时间轴不可用；本场不能形成队友持续作战时的单人停手证据。",
    }
    signature["context"]["player_item_level"] = player_ilvl
    signature.setdefault("responsiveness", {}).update({
        "isolated_team_active_gap_count": isolated.get("isolated_gap_count", 0),
        "severe_isolated_team_active_gap_count": isolated.get("severe_isolated_gap_count", 0),
        "isolated_team_active_max_gap_s": isolated.get("max_isolated_gap_s", 0.0),
        "death_excluded_gap_count": isolated.get("death_excluded_gap_count", 0),
        "outside_combat_excluded_gap_count": isolated.get("outside_combat_excluded_gap_count", 0),
        "alive_in_combat_only": True,
    })
    signature["direct_fight_evidence"] = {
        "damage_taken_total": round(sum(max(0.0, _num(e.get("amount"))) for e in damage_taken), 2),
        "top_damage_taken": _top_event_amounts(damage_taken, abilities),
        "interrupt_count": len(interrupts),
        "death_count": len(deaths),
        "cast_event_count": len(casts),
        "damage_event_count": len(damage),
        "buff_event_count": len(buffs),
        "resource_event_count": len(resources),
        "team_cast_event_count": len(team_casts),
        "team_activity_stalls": isolated,
        "timeline_feature_summary": {
            "skills_with_cadence": len(((signature.get("efficiency") or {}).get("cast_cadence") or [])),
            "observed_cast_duration_skills": len(((signature.get("efficiency") or {}).get("cast_duration") or [])),
            "buffs_with_uptime": len(((signature.get("efficiency") or {}).get("buff_uptime") or [])),
            "buffs_with_burst_overlap": len(((signature.get("efficiency") or {}).get("buff_burst_overlap") or [])),
            "pulls_analyzed": len(((signature.get("timeline") or {}).get("pull_breakdown") or [])),
            "burst_windows_analyzed": len(((signature.get("timeline") or {}).get("burst_windows") or [])),
        },
    }
    signature["analysis_request"] = {
        "mode": "selected_wcl_fight",
        "goal": "对用户从自己的 WCL 最近 Log 中选择的这一场进行针对性分析；优先解释技能、节奏、资源、Buff、死亡和响应异常。",
    }
    return {
        "report": report,
        "fight": fight,
        "source_id": sid,
        "player_name": player_name,
        "class_name": class_name,
        "spec_name": spec_name,
        "signature": signature,
        "rate_limit": raw.get("rateLimitData") or {},
    }
