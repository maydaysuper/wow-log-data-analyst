from __future__ import annotations

from typing import Optional

import pandas as pd

from .parser import DAMAGE_EVENTS, HEAL_EVENTS, CAST_EVENTS, INTERRUPT_EVENTS, DEATH_EVENTS, MISS_EVENTS


def _slice(df: pd.DataFrame, segment: Optional[str] = None) -> pd.DataFrame:
    if df.empty or not segment or segment == "All":
        return df
    if segment.startswith("RUN::") and "run_segment" in df.columns:
        return df[df["run_segment"] == segment[5:]]
    if "segment" in df.columns:
        return df[df["segment"] == segment]
    return df


def _is_player_guid(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.startswith("Player-")


def _is_nonplayer_guid(series: pd.Series) -> pd.Series:
    s = series.fillna("").astype(str)
    return s.ne("") & ~s.str.startswith("Player-")


def segment_pulls(df: pd.DataFrame, segment: Optional[str] = None, idle_gap_s: float = 8.0) -> pd.DataFrame:
    """Approximate M+ combat pulls from hostile interactions separated by idle gaps.

    This is intentionally labeled approximate: Combat Log does not expose the route's
    conceptual pull boundaries, and chain pulls can merge into one continuous window.
    """
    x = _slice(df, segment).sort_values("ts").copy()
    if x.empty:
        return pd.DataFrame()

    src_player = _is_player_guid(x.get("source_guid", pd.Series(index=x.index, dtype=str)))
    dst_player = _is_player_guid(x.get("dest_guid", pd.Series(index=x.index, dtype=str)))
    src_npc = _is_nonplayer_guid(x.get("source_guid", pd.Series(index=x.index, dtype=str)))
    dst_npc = _is_nonplayer_guid(x.get("dest_guid", pd.Series(index=x.index, dtype=str)))
    hostile_event = x["event"].isin(DAMAGE_EVENTS | CAST_EVENTS | INTERRUPT_EVENTS | MISS_EVENTS)
    hostile = hostile_event & ((src_player & dst_npc) | (src_npc & dst_player))
    activity = x[hostile].copy()
    if activity.empty:
        return pd.DataFrame()

    groups = []
    gid = 1
    last_ts = None
    for ts in activity["ts"].astype(float):
        if last_ts is not None and ts - last_ts > idle_gap_s:
            gid += 1
        groups.append(gid)
        last_ts = ts
    activity["pull_id"] = groups

    rows = []
    for pull_id, g in activity.groupby("pull_id"):
        start, end = float(g["ts"].min()), float(g["ts"].max())
        # Include all events inside the detected combat window for deaths/healing/casts.
        w = x[(x["ts"] >= start) & (x["ts"] <= end)]
        enemy_mask = _is_nonplayer_guid(g["dest_guid"]) | _is_nonplayer_guid(g["source_guid"])
        enemy_names = set()
        for col, mask in [("dest_name", _is_nonplayer_guid(g["dest_guid"])), ("source_name", _is_nonplayer_guid(g["source_guid"]))]:
            enemy_names.update(str(v) for v in g.loc[mask, col].dropna().tolist() if str(v))
        player_damage = w[_is_player_guid(w["source_guid"]) & w["event"].isin(DAMAGE_EVENTS)]["amount"].sum()
        deaths = w[_is_player_guid(w["dest_guid"]) & w["event"].isin(DEATH_EVENTS)]
        boss_names = [str(v) for v in w.get("encounter", pd.Series(dtype=str)).dropna().unique() if str(v)]
        rows.append({
            "pull_id": int(pull_id),
            "start_ts": start,
            "end_ts": end,
            "duration_s": round(max(0.0, end - start), 2),
            "enemy_count_observed": int(len(enemy_names)),
            "enemy_names": ", ".join(sorted(enemy_names)[:12]),
            "team_damage": float(player_damage),
            "team_dps": round(float(player_damage) / max(end - start, 1.0), 1),
            "team_casts": int(len(w[_is_player_guid(w["source_guid"]) & w["event"].isin(CAST_EVENTS)])),
            "interrupts": int(len(w[_is_player_guid(w["source_guid"]) & w["event"].isin(INTERRUPT_EVENTS)])),
            "player_deaths": int(len(deaths)),
            "boss": boss_names[0] if boss_names else "",
            "kind": "Boss" if boss_names else "Trash/Other",
        })
    return pd.DataFrame(rows)


def player_pull_breakdown(df: pd.DataFrame, player: str, pulls: pd.DataFrame, top_skills: int = 5) -> pd.DataFrame:
    if df.empty or pulls is None or pulls.empty or not player:
        return pd.DataFrame()
    rows = []
    for p in pulls.itertuples(index=False):
        w = df[(df["ts"] >= float(p.start_ts)) & (df["ts"] <= float(p.end_ts))]
        dmg = w[(w["source_name"] == player) & w["event"].isin(DAMAGE_EVENTS)]
        casts = w[(w["source_name"] == player) & w["event"].isin(CAST_EVENTS)]
        total = float(dmg["amount"].sum())
        skill = dmg.groupby("spell_name", dropna=False)["amount"].sum().sort_values(ascending=False).head(top_skills)
        top = "; ".join(f"{str(k) if pd.notna(k) else 'Melee'} {v/total*100:.1f}%" for k, v in skill.items()) if total > 0 else ""
        rows.append({
            "pull_id": int(p.pull_id), "kind": p.kind, "boss": p.boss,
            "duration_s": float(p.duration_s), "player_damage": total,
            "player_dps": round(total / max(float(p.duration_s), 1.0), 1),
            "casts": int(len(casts)), "casts_per_min": round(len(casts) / max(float(p.duration_s) / 60.0, 1/60), 2),
            "top_skills": top,
        })
    return pd.DataFrame(rows)


def death_contexts(df: pd.DataFrame, player: str, segment: Optional[str] = None, before_s: float = 10.0, after_s: float = 2.0) -> pd.DataFrame:
    x = _slice(df, segment).sort_values("ts")
    if x.empty or not player:
        return pd.DataFrame()
    deaths = x[(x["dest_name"] == player) & x["event"].isin(DEATH_EVENTS)]
    rows = []
    for d in deaths.itertuples(index=False):
        ts = float(d.ts)
        w = x[(x["ts"] >= ts - before_s) & (x["ts"] <= ts + after_s)]
        incoming = w[(w["dest_name"] == player) & w["event"].isin(DAMAGE_EVENTS)]
        heals = w[(w["dest_name"] == player) & w["event"].isin(HEAL_EVENTS)]
        own_casts = w[(w["source_name"] == player) & w["event"].isin(CAST_EVENTS)]
        top_incoming = incoming.groupby("spell_name", dropna=False)["amount"].sum().sort_values(ascending=False).head(5)
        rows.append({
            "death_ts": ts,
            "incoming_damage_10s": float(incoming["amount"].sum()),
            "healing_received_10s": float(heals["amount"].sum()),
            "own_casts_10s": int(len(own_casts)),
            "last_own_cast": str(own_casts.sort_values("ts").iloc[-1]["spell_name"]) if not own_casts.empty else "",
            "top_incoming": "; ".join(f"{str(k) if pd.notna(k) else 'Melee'}={v:.0f}" for k, v in top_incoming.items()),
        })
    return pd.DataFrame(rows)


def burst_windows(df: pd.DataFrame, player: str, segment: Optional[str] = None, window_s: float = 10.0, step_s: float = 2.0, top_n: int = 5) -> pd.DataFrame:
    x = _slice(df, segment).sort_values("ts")
    dmg = x[(x["source_name"] == player) & x["event"].isin(DAMAGE_EVENTS)]
    if dmg.empty:
        return pd.DataFrame()
    start, end = float(dmg["ts"].min()), float(dmg["ts"].max())
    candidates = []
    t = start
    while t <= end:
        w = dmg[(dmg["ts"] >= t) & (dmg["ts"] < t + window_s)]
        if not w.empty:
            total = float(w["amount"].sum())
            candidates.append((total, t, t + window_s, w))
        t += step_s
    candidates.sort(key=lambda z: z[0], reverse=True)
    chosen = []
    for total, a, b, w in candidates:
        if any(not (b <= ca or a >= cb) for _, ca, cb, _ in chosen):
            continue
        chosen.append((total, a, b, w))
        if len(chosen) >= top_n:
            break
    rows = []
    for rank, (total, a, b, w) in enumerate(chosen, 1):
        skill = w.groupby("spell_name", dropna=False)["amount"].sum().sort_values(ascending=False).head(6)
        rows.append({
            "rank": rank, "start_ts": a, "end_ts": b, "window_s": window_s,
            "damage": total, "window_dps": round(total / window_s, 1),
            "top_skills": "; ".join(f"{str(k) if pd.notna(k) else 'Melee'} {v/total*100:.1f}%" for k, v in skill.items()) if total else "",
        })
    return pd.DataFrame(rows)


def advanced_timeline_payload(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    pulls = segment_pulls(df, segment)
    pbreak = player_pull_breakdown(_slice(df, segment), player, pulls)
    deaths = death_contexts(df, player, segment)
    bursts = burst_windows(df, player, segment)
    return {
        "pulls": pulls.head(40).to_dict("records") if not pulls.empty else [],
        "player_pulls": pbreak.head(40).to_dict("records") if not pbreak.empty else [],
        "death_contexts": deaths.head(20).to_dict("records") if not deaths.empty else [],
        "burst_windows": bursts.head(10).to_dict("records") if not bursts.empty else [],
        "limitations": [
            "Pull 边界由战斗活跃间隔近似推断，链式拉怪可能被合并。",
            "爆发窗口按观察到的伤害峰值识别，不等同于职业官方爆发技能定义。",
        ],
    }
