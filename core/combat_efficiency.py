from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .parser import (
    AURA_EVENTS,
    CAST_EVENTS,
    DAMAGE_EVENTS,
    DEATH_EVENTS,
    RESOURCE_EVENTS,
    RESURRECT_EVENTS,
)
from .advanced_timeline import segment_pulls, burst_windows


def _slice(df: pd.DataFrame, segment: Optional[str] = None) -> pd.DataFrame:
    if df.empty or not segment or segment == "All":
        return df
    if segment.startswith("RUN::") and "run_segment" in df.columns:
        return df[df["run_segment"] == segment[5:]]
    if "segment" in df.columns:
        return df[df["segment"] == segment]
    return df


def _player_guid(df: pd.DataFrame, player: str) -> str:
    if df.empty or not player:
        return ""
    for guid_col, name_col in (("source_guid", "source_name"), ("dest_guid", "dest_name")):
        if guid_col not in df.columns or name_col not in df.columns:
            continue
        m = (df[name_col] == player) & df[guid_col].fillna("").astype(str).str.startswith("Player-")
        if m.any():
            return str(df.loc[m, guid_col].iloc[0])
    return ""


def _run_bounds(x: pd.DataFrame) -> tuple[float, float]:
    if x.empty:
        return 0.0, 0.0
    starts = x.loc[x["event"] == "CHALLENGE_MODE_START", "ts"] if "event" in x.columns else pd.Series(dtype=float)
    ends = x.loc[x["event"] == "CHALLENGE_MODE_END", "ts"] if "event" in x.columns else pd.Series(dtype=float)
    start = float(starts.min()) if not starts.empty else float(x["ts"].min())
    end = float(ends.max()) if not ends.empty else float(x["ts"].max())
    return start, max(end, start)


def buff_windows(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    """Build observed self/player buff windows from aura apply/refresh/remove events.

    This only reports auras that are explicitly visible in the Combat Log. Missing apply or
    remove events can truncate windows, so results are evidence rather than an authoritative
    spell-book model.
    """
    x = _slice(df, segment).sort_values("ts")
    if x.empty or not player:
        return pd.DataFrame()
    auras = x[(x["dest_name"] == player) & x["event"].isin(AURA_EVENTS)].copy()
    if auras.empty:
        return pd.DataFrame()
    # Prefer BUFFs. Some logs omit aura_type; retain those rather than silently losing data.
    if "aura_type" in auras.columns:
        auras = auras[auras["aura_type"].isna() | auras["aura_type"].astype(str).str.upper().eq("BUFF")]
    if auras.empty:
        return pd.DataFrame()

    _, run_end = _run_bounds(x)
    active: dict[tuple, dict] = {}
    rows: list[dict] = []

    for r in auras.itertuples(index=False):
        key = (getattr(r, "spell_id", None), getattr(r, "spell_name", None))
        ev = str(r.event)
        ts = float(r.ts)
        stack = getattr(r, "aura_stacks", None)
        if ev == "SPELL_AURA_APPLIED":
            # If the same aura is re-applied without a removal, close the previous observed window.
            if key in active:
                prev = active.pop(key)
                rows.append({**prev, "end_ts": ts, "duration_s": max(0.0, ts - prev["start_ts"]), "closed_by": "reapply"})
            active[key] = {
                "spell_id": getattr(r, "spell_id", None),
                "spell_name": getattr(r, "spell_name", None) or "Unknown",
                "start_ts": ts,
                "applications": 1,
                "refreshes": 0,
                "max_stacks": int(stack or 1),
            }
        elif ev == "SPELL_AURA_REFRESH":
            if key in active:
                active[key]["refreshes"] += 1
            else:
                active[key] = {
                    "spell_id": getattr(r, "spell_id", None),
                    "spell_name": getattr(r, "spell_name", None) or "Unknown",
                    "start_ts": ts,
                    "applications": 0,
                    "refreshes": 1,
                    "max_stacks": int(stack or 1),
                }
        elif ev in {"SPELL_AURA_APPLIED_DOSE", "SPELL_AURA_REMOVED_DOSE"}:
            if key in active and stack is not None:
                active[key]["max_stacks"] = max(active[key]["max_stacks"], int(stack))
        elif ev == "SPELL_AURA_REMOVED":
            if key in active:
                prev = active.pop(key)
                rows.append({**prev, "end_ts": ts, "duration_s": max(0.0, ts - prev["start_ts"]), "closed_by": "removed"})

    for prev in active.values():
        rows.append({**prev, "end_ts": run_end, "duration_s": max(0.0, run_end - prev["start_ts"]), "closed_by": "segment_end"})
    return pd.DataFrame(rows).sort_values(["start_ts", "spell_name"]) if rows else pd.DataFrame()


def buff_uptime(df: pd.DataFrame, player: str, segment: Optional[str] = None, top_n: int = 25) -> pd.DataFrame:
    x = _slice(df, segment)
    windows = buff_windows(df, player, segment)
    if windows.empty:
        return pd.DataFrame()
    start, end = _run_bounds(x)
    duration = max(end - start, 1.0)
    g = windows.groupby(["spell_id", "spell_name"], dropna=False).agg(
        uptime_s=("duration_s", "sum"),
        windows=("duration_s", "size"),
        avg_window_s=("duration_s", "mean"),
        refreshes=("refreshes", "sum"),
        max_stacks=("max_stacks", "max"),
    ).reset_index()
    g["uptime_pct"] = (g["uptime_s"] / duration * 100).clip(upper=100)
    return g.sort_values(["uptime_s", "windows"], ascending=False).head(top_n).reset_index(drop=True)


def resource_efficiency(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    """Estimate resource pressure/waste from advanced snapshots and ENERGIZE suffixes.

    Snapshot occupancy is time-weighted but each observation is capped to 2 seconds so sparse
    logs do not pretend one snapshot represents a long unseen period.
    """
    x = _slice(df, segment).sort_values("ts")
    guid = _player_guid(x, player)
    out = {
        "advanced_snapshots": 0,
        "resource_snapshot_available": False,
        "avg_resource_pct": None,
        "time_at_or_above_90_pct": None,
        "time_at_or_below_10_pct": None,
        "energize_events": 0,
        "resource_gained": 0.0,
        "resource_overcap": 0.0,
        "overcap_pct_of_generated": 0.0,
        "power_types_seen": [],
        "limitations": [],
    }
    if x.empty or not player:
        return out

    if guid and "advanced_info_guid" in x.columns:
        snaps = x[(x["advanced_info_guid"] == guid) & x["current_power"].notna() & x["max_power"].notna()].copy()
        snaps = snaps[snaps["max_power"].astype(float) > 0]
        if not snaps.empty:
            snaps = snaps.sort_values("ts").drop_duplicates("ts", keep="last")
            ratio = (snaps["current_power"].astype(float) / snaps["max_power"].astype(float)).clip(0, 2)
            next_ts = snaps["ts"].astype(float).shift(-1)
            weights = (next_ts - snaps["ts"].astype(float)).fillna(0).clip(lower=0, upper=2.0)
            if float(weights.sum()) <= 0:
                weights = pd.Series(np.ones(len(snaps)), index=snaps.index)
            out["advanced_snapshots"] = int(len(snaps))
            out["resource_snapshot_available"] = True
            out["avg_resource_pct"] = round(float(np.average(ratio, weights=weights)) * 100, 1)
            out["time_at_or_above_90_pct"] = round(float(weights[ratio >= 0.9].sum() / weights.sum() * 100), 1)
            out["time_at_or_below_10_pct"] = round(float(weights[ratio <= 0.1].sum() / weights.sum() * 100), 1)
            pts = sorted({int(v) for v in snaps["power_type"].dropna().tolist()})
            out["power_types_seen"] = pts

    energize = x[(x["dest_name"] == player) & x["event"].isin({"SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE"})].copy()
    if not energize.empty:
        gained = float(energize["amount"].fillna(0).astype(float).sum())
        over = float(energize.get("overenergize", pd.Series(0, index=energize.index)).fillna(0).astype(float).sum())
        out["energize_events"] = int(len(energize))
        out["resource_gained"] = round(gained, 1)
        out["resource_overcap"] = round(over, 1)
        denom = gained + over
        out["overcap_pct_of_generated"] = round(over / denom * 100, 1) if denom > 0 else 0.0
        out["power_types_seen"] = sorted(set(out["power_types_seen"]) | {int(v) for v in energize["resource_power_type"].dropna().tolist()})

    if not out["resource_snapshot_available"]:
        out["limitations"].append("日志里没有足够的 Advanced Combat Logging power 快照；无法估算资源高位/低位驻留。")
    if out["energize_events"] == 0:
        out["limitations"].append("没有观察到 ENERGIZE 事件；这不代表职业没有资源生成，只代表该资源机制未通过此事件暴露。")
    return out


def cooldown_cadence(df: pd.DataFrame, player: str, segment: Optional[str] = None, min_casts: int = 2, top_n: int = 30) -> pd.DataFrame:
    """Observed cast cadence by spell, without pretending to know the spell's true cooldown."""
    x = _slice(df, segment).sort_values("ts")
    casts = x[(x["source_name"] == player) & x["event"].isin(CAST_EVENTS)].copy()
    if casts.empty:
        return pd.DataFrame()
    start, end = _run_bounds(x)
    duration_min = max((end - start) / 60.0, 1 / 60)
    rows = []
    for (sid, name), g in casts.groupby(["spell_id", "spell_name"], dropna=False):
        ts = sorted(float(v) for v in g["ts"])
        if len(ts) < min_casts:
            continue
        intervals = np.diff(ts)
        rows.append({
            "spell_id": sid,
            "spell_name": name or "Unknown",
            "casts": len(ts),
            "casts_per_min": round(len(ts) / duration_min, 2),
            "median_interval_s": round(float(np.median(intervals)), 2),
            "p95_interval_s": round(float(np.percentile(intervals, 95)), 2),
            "max_interval_s": round(float(np.max(intervals)), 2),
            "interval_cv": round(float(np.std(intervals) / np.mean(intervals)), 3) if float(np.mean(intervals)) > 0 else 0.0,
        })
    return pd.DataFrame(rows).sort_values(["casts", "max_interval_s"], ascending=[False, False]).head(top_n).reset_index(drop=True) if rows else pd.DataFrame()


def target_switch_analysis(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    x = _slice(df, segment).sort_values("ts")
    casts = x[(x["source_name"] == player) & x["event"].isin(CAST_EVENTS)].copy()
    if casts.empty:
        return {"targeted_casts": 0, "unique_targets": 0, "target_switches": 0, "switches_per_min": 0.0, "rapid_switches_under_2s": 0}
    npc = ~casts["dest_guid"].fillna("").astype(str).str.startswith("Player-") & casts["dest_guid"].fillna("").astype(str).ne("")
    casts = casts[npc & casts["dest_name"].notna()].copy()
    if casts.empty:
        return {"targeted_casts": 0, "unique_targets": 0, "target_switches": 0, "switches_per_min": 0.0, "rapid_switches_under_2s": 0}
    targets = casts["dest_guid"].astype(str).tolist()
    times = casts["ts"].astype(float).tolist()
    switches = 0
    rapid = 0
    for i in range(1, len(targets)):
        if targets[i] != targets[i - 1]:
            switches += 1
            if times[i] - times[i - 1] < 2.0:
                rapid += 1
    start, end = _run_bounds(x)
    dur_min = max((end - start) / 60.0, 1 / 60)
    return {
        "targeted_casts": int(len(casts)),
        "unique_targets": int(casts["dest_guid"].nunique()),
        "target_switches": int(switches),
        "switches_per_min": round(switches / dur_min, 2),
        "rapid_switches_under_2s": int(rapid),
        "note": "目标切换只按有明确目标的成功施法估算；AOE、多目标溅射和鼠标指向技能可能不会体现为真实转火。",
    }


def pull_downtime(df: pd.DataFrame, segment: Optional[str] = None, idle_gap_s: float = 8.0) -> tuple[pd.DataFrame, dict]:
    x = _slice(df, segment).sort_values("ts")
    pulls = segment_pulls(x, None, idle_gap_s=idle_gap_s)
    empty_summary = {"pulls": 0, "inter_pull_downtime_s": 0.0, "median_gap_s": 0.0, "max_gap_s": 0.0, "pre_first_pull_s": 0.0, "post_last_pull_s": 0.0}
    if pulls.empty:
        return pd.DataFrame(), empty_summary
    start, end = _run_bounds(x)
    rows = []
    for i in range(1, len(pulls)):
        prev = pulls.iloc[i - 1]
        cur = pulls.iloc[i]
        gap = max(0.0, float(cur.start_ts) - float(prev.end_ts))
        rows.append({
            "after_pull_id": int(prev.pull_id),
            "before_pull_id": int(cur.pull_id),
            "gap_s": round(gap, 2),
            "previous_kind": prev.kind,
            "next_kind": cur.kind,
        })
    gaps = pd.DataFrame(rows)
    values = gaps["gap_s"].astype(float) if not gaps.empty else pd.Series(dtype=float)
    summary = {
        "pulls": int(len(pulls)),
        "inter_pull_downtime_s": round(float(values.sum()), 2) if not values.empty else 0.0,
        "median_gap_s": round(float(values.median()), 2) if not values.empty else 0.0,
        "max_gap_s": round(float(values.max()), 2) if not values.empty else 0.0,
        "pre_first_pull_s": round(max(0.0, float(pulls.iloc[0].start_ts) - start), 2),
        "post_last_pull_s": round(max(0.0, end - float(pulls.iloc[-1].end_ts)), 2),
        "note": "Pull 间空档包含跑图、门、电梯、RP、等怪和路线决策，不应自动判定为失误。",
    }
    return gaps, summary


def death_recovery(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    x = _slice(df, segment).sort_values("ts")
    if x.empty or not player:
        return pd.DataFrame()
    deaths = x[(x["dest_name"] == player) & x["event"].isin(DEATH_EVENTS)]
    if deaths.empty:
        return pd.DataFrame()
    rows = []
    _, end = _run_bounds(x)
    for d in deaths.itertuples(index=False):
        ts = float(d.ts)
        next_action = x[(x["ts"] > ts) & (x["source_name"] == player) & x["event"].isin(CAST_EVENTS | DAMAGE_EVENTS)]
        action_ts = float(next_action.iloc[0]["ts"]) if not next_action.empty else end
        res = x[(x["ts"] >= ts) & (x["ts"] <= action_ts) & (x["dest_name"] == player) & x["event"].isin(RESURRECT_EVENTS)]
        team_dmg = float(x[(x["ts"] >= ts) & (x["ts"] <= action_ts) & x["source_guid"].fillna("").astype(str).str.startswith("Player-") & x["event"].isin(DAMAGE_EVENTS)]["amount"].sum())
        pre = x[(x["ts"] >= ts - 15) & (x["ts"] < ts) & (x["source_name"] == player) & x["event"].isin(DAMAGE_EVENTS)]
        pre_dps = float(pre["amount"].sum()) / 15.0
        recovery = max(0.0, action_ts - ts)
        rows.append({
            "death_ts": ts,
            "return_to_action_ts": action_ts,
            "recovery_s": round(recovery, 2),
            "resurrect_event_seen": bool(not res.empty),
            "team_damage_during_recovery": round(team_dmg, 0),
            "estimated_damage_opportunity": round(pre_dps * recovery, 0),
        })
    return pd.DataFrame(rows)


def buff_burst_overlap(df: pd.DataFrame, player: str, segment: Optional[str] = None, top_n: int = 12) -> pd.DataFrame:
    buffs = buff_windows(df, player, segment)
    bursts = burst_windows(df, player, segment, window_s=10.0, step_s=2.0, top_n=5)
    if buffs.empty or bursts.empty:
        return pd.DataFrame()
    burst_total = float(bursts["window_s"].sum())
    rows = []
    for (sid, name), g in buffs.groupby(["spell_id", "spell_name"], dropna=False):
        overlap = 0.0
        for b in bursts.itertuples(index=False):
            for w in g.itertuples(index=False):
                overlap += max(0.0, min(float(b.end_ts), float(w.end_ts)) - max(float(b.start_ts), float(w.start_ts)))
        rows.append({
            "spell_id": sid,
            "spell_name": name or "Unknown",
            "burst_overlap_s": round(overlap, 2),
            "burst_window_coverage_pct": round(min(overlap / max(burst_total, 1.0) * 100, 100.0), 1),
            "observed_buff_windows": int(len(g)),
        })
    return pd.DataFrame(rows).sort_values("burst_window_coverage_pct", ascending=False).head(top_n).reset_index(drop=True)


def deep_efficiency_payload(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    buffs = buff_uptime(df, player, segment)
    cadence = cooldown_cadence(df, player, segment)
    downtime_rows, downtime_summary = pull_downtime(df, segment)
    recovery = death_recovery(df, player, segment)
    overlap = buff_burst_overlap(df, player, segment)
    return {
        "player": player,
        "buff_uptime": buffs.head(20).to_dict("records") if not buffs.empty else [],
        "cast_cadence": cadence.head(25).to_dict("records") if not cadence.empty else [],
        "resource_efficiency": resource_efficiency(df, player, segment),
        "target_switching": target_switch_analysis(df, player, segment),
        "pull_downtime": downtime_summary,
        "largest_inter_pull_gaps": downtime_rows.sort_values("gap_s", ascending=False).head(12).to_dict("records") if not downtime_rows.empty else [],
        "death_recovery": recovery.head(12).to_dict("records") if not recovery.empty else [],
        "buff_burst_overlap": overlap.head(12).to_dict("records") if not overlap.empty else [],
        "limitations": [
            "Buff 覆盖只基于日志中实际出现的 aura apply/refresh/remove，不能替代职业技能数据库。",
            "施法节奏是观察到的 cadence，不代表程序已经知道技能真实冷却时间；跨玩家/跨样本比较更可靠。",
            "资源高位/低位需要 Advanced Combat Logging power 快照；ENERGIZE overcap 只覆盖通过该事件暴露的资源来源。",
            "死亡损失使用死亡前15秒 DPS 粗略估计机会成本，不等同于反事实模拟。",
        ],
    }


def compare_efficiency_runs(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All") -> pd.DataFrame:
    """Compact cross-run efficiency table for the selected focus player in each sample."""
    rows = []
    seg = None if segment == "All" else segment
    for run, player in selections.items():
        if run not in run_dfs or not player:
            continue
        df = run_dfs[run]
        res = resource_efficiency(df, player, seg)
        sw = target_switch_analysis(df, player, seg)
        _, gap = pull_downtime(df, seg)
        rec = death_recovery(df, player, seg)
        buffs = buff_uptime(df, player, seg)
        cadence = cooldown_cadence(df, player, seg)
        rows.append({
            "run": run,
            "player": player,
            "avg_resource_pct": res.get("avg_resource_pct"),
            "resource_high_90_pct": res.get("time_at_or_above_90_pct"),
            "resource_low_10_pct": res.get("time_at_or_below_10_pct"),
            "resource_overcap_pct": res.get("overcap_pct_of_generated", 0.0),
            "target_switches_per_min": sw.get("switches_per_min", 0.0),
            "inter_pull_downtime_s": gap.get("inter_pull_downtime_s", 0.0),
            "median_inter_pull_gap_s": gap.get("median_gap_s", 0.0),
            "max_inter_pull_gap_s": gap.get("max_gap_s", 0.0),
            "death_count_observed": int(len(rec)),
            "avg_death_recovery_s": round(float(rec["recovery_s"].mean()), 2) if not rec.empty else 0.0,
            "max_death_recovery_s": round(float(rec["recovery_s"].max()), 2) if not rec.empty else 0.0,
            "observed_buff_count": int(len(buffs)),
            "cadence_spell_count": int(len(cadence)),
        })
    return pd.DataFrame(rows)


def buff_uptime_matrix(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All", top_n: int = 25) -> pd.DataFrame:
    """Cross-run matrix of observed buff uptime percentages."""
    seg = None if segment == "All" else segment
    chunks = []
    for run, player in selections.items():
        if run not in run_dfs or not player:
            continue
        b = buff_uptime(run_dfs[run], player, seg, top_n=100)
        if b.empty:
            continue
        t = b[["spell_name", "uptime_pct"]].copy()
        t["sample"] = f"{run} · {player}"
        chunks.append(t)
    if not chunks:
        return pd.DataFrame()
    long = pd.concat(chunks, ignore_index=True)
    top = long.groupby("spell_name")["uptime_pct"].median().nlargest(top_n).index
    x = long[long["spell_name"].isin(top)]
    pivot = x.pivot_table(index="spell_name", columns="sample", values="uptime_pct", aggfunc="max", fill_value=0)
    pivot["样本P50"] = pivot.median(axis=1)
    pivot["样本P75"] = pivot.drop(columns=["样本P50"], errors="ignore").quantile(0.75, axis=1)
    return pivot.sort_values("样本P50", ascending=False).reset_index()
