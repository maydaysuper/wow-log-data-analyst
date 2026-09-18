from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Optional

import pandas as pd

from .parser import DAMAGE_EVENTS, CAST_EVENTS, CAST_START_EVENTS, CAST_FAILED_EVENTS, MISS_EVENTS, DEATH_EVENTS


def _slice(df: pd.DataFrame, segment: Optional[str] = None) -> pd.DataFrame:
    if df.empty or not segment or segment == "All":
        return df
    if segment.startswith("RUN::") and "run_segment" in df.columns:
        return df[df["run_segment"] == segment[5:]]
    if "segment" in df.columns:
        return df[df["segment"] == segment]
    return df


def _dedupe_times(values, epsilon: float = 0.03) -> list[float]:
    out: list[float] = []
    for v in sorted(float(x) for x in values if pd.notna(x)):
        if not out or v - out[-1] > epsilon:
            out.append(v)
    return out


def _robust_center(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    med = float(median(values))
    mad = float(median([abs(v - med) for v in values])) if len(values) > 1 else 0.0
    return med, mad


def _player_guid_mask(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index)
    return df[col].fillna("").astype(str).str.startswith("Player-")


def _pair_casts(src: pd.DataFrame) -> pd.DataFrame:
    """Pair SPELL_CAST_START with the next success/failure for the same spell.

    This intentionally estimates observed server-side cast duration only. It is not
    treated as direct network latency because the combat log does not contain the
    user's key-down timestamp.
    """
    if src.empty:
        return pd.DataFrame(columns=["spell_id", "spell_name", "start_ts", "end_ts", "duration_s", "result", "failure_reason"])

    pending: dict[tuple, list[float]] = defaultdict(list)
    rows = []
    for row in src.sort_values("ts").itertuples(index=False):
        event = str(getattr(row, "event", ""))
        spell_id = getattr(row, "spell_id", None)
        spell_name = getattr(row, "spell_name", None)
        key = (spell_id if pd.notna(spell_id) else None, str(spell_name or ""))
        ts = float(getattr(row, "ts"))
        if event == "SPELL_CAST_START":
            pending[key].append(ts)
        elif event in {"SPELL_CAST_SUCCESS", "SPELL_CAST_FAILED"} and pending.get(key):
            start = pending[key].pop(0)
            dur = ts - start
            if 0 <= dur <= 20:
                rows.append({
                    "spell_id": key[0],
                    "spell_name": key[1] or "Unknown",
                    "start_ts": start,
                    "end_ts": ts,
                    "duration_s": dur,
                    "result": "success" if event == "SPELL_CAST_SUCCESS" else "failed",
                    "failure_reason": getattr(row, "failure_reason", None),
                })
    return pd.DataFrame(rows)


def action_gap_table(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    """Find long manual-action gaps while the party remains actively fighting.

    A gap is evidence of a responsiveness anomaly only when other party members keep
    casting / damaging, or the player continues taking damage. This removes many boss
    transitions and out-of-combat pauses. It still cannot prove network lag.
    """
    x = _slice(df, segment)
    if x.empty:
        return pd.DataFrame()

    src = x[x.get("source_name") == player].sort_values("ts")
    action_ts = _dedupe_times(src.loc[src["event"].isin(CAST_EVENTS), "ts"].tolist())
    if len(action_ts) < 3:
        return pd.DataFrame()

    intervals = [b - a for a, b in zip(action_ts[:-1], action_ts[1:]) if 0 < b - a <= 12]
    if not intervals:
        return pd.DataFrame()
    med, _ = _robust_center(intervals)
    threshold = max(2.5, med * 2.35)

    other_player = _player_guid_mask(x, "source_guid") & (x["source_name"].fillna("") != player)
    other_casts = x[other_player & x["event"].isin(CAST_EVENTS)]
    team_damage = x[_player_guid_mask(x, "source_guid") & x["event"].isin(DAMAGE_EVENTS)]
    incoming = x[(x["dest_name"] == player) & x["event"].isin(DAMAGE_EVENTS)]
    own_swings = x[(x["source_name"] == player) & x["event"].isin({"SWING_DAMAGE", "SWING_MISSED"})]
    own_damage = x[(x["source_name"] == player) & x["event"].isin(DAMAGE_EVENTS)]
    deaths = x[(x["dest_name"] == player) & x["event"].isin(DEATH_EVENTS)].sort_values("ts")

    rows = []
    death_excluded = 0
    for a, b in zip(action_ts[:-1], action_ts[1:]):
        gap = b - a
        if gap < threshold:
            continue
        # A player death inside the apparent cast gap invalidates the whole window.
        # Being dead is not a responsiveness stall.
        if not deaths[(deaths["ts"] >= a) & (deaths["ts"] <= b)].empty:
            death_excluded += 1
            continue
        between_other_casts = other_casts[(other_casts["ts"] > a) & (other_casts["ts"] < b)]
        between_team_damage = team_damage[(team_damage["ts"] > a) & (team_damage["ts"] < b)]
        between_incoming = incoming[(incoming["ts"] > a) & (incoming["ts"] < b)]
        between_swings = own_swings[(own_swings["ts"] > a) & (own_swings["ts"] < b)]
        between_own_damage = own_damage[(own_damage["ts"] > a) & (own_damage["ts"] < b)]

        other_cast_n = int(len(between_other_casts))
        team_damage_n = int(len(between_team_damage))
        incoming_n = int(len(between_incoming))
        active = other_cast_n >= 2 or team_damage_n >= 4 or incoming_n >= 1
        if not active:
            continue

        rows.append({
            "start_ts": a,
            "end_ts": b,
            "gap_s": round(gap, 3),
            "other_player_casts": other_cast_n,
            "team_damage_events": team_damage_n,
            "incoming_hits": incoming_n,
            "own_swing_events": int(len(between_swings)),
            "own_damage_events": int(len(between_own_damage)),
            "isolated_player_stall": bool(other_cast_n >= 3),
            "full_manual_silence": bool(len(between_swings) == 0 and len(between_own_damage) == 0),
            "player_alive_for_entire_gap": True,
        })
    out = pd.DataFrame(rows).sort_values("gap_s", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()
    out.attrs["death_excluded_gap_count"] = death_excluded
    return out


def _swing_stalls(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    x = _slice(df, segment)
    if x.empty:
        return pd.DataFrame()
    swings = x[(x["source_name"] == player) & x["event"].isin({"SWING_DAMAGE", "SWING_MISSED"})].sort_values("ts")
    ts = _dedupe_times(swings["ts"].tolist())
    if len(ts) < 5:
        return pd.DataFrame()
    intervals = [b - a for a, b in zip(ts[:-1], ts[1:]) if 0 < b - a <= 10]
    if len(intervals) < 4:
        return pd.DataFrame()
    med, _ = _robust_center(intervals)
    threshold = max(3.0, med * 1.85)
    other_casts = x[_player_guid_mask(x, "source_guid") & (x["source_name"].fillna("") != player) & x["event"].isin(CAST_EVENTS)]
    incoming = x[(x["dest_name"] == player) & x["event"].isin(DAMAGE_EVENTS)]
    rows = []
    for a, b in zip(ts[:-1], ts[1:]):
        gap = b - a
        if gap < threshold:
            continue
        oc = int(len(other_casts[(other_casts["ts"] > a) & (other_casts["ts"] < b)]))
        inc = int(len(incoming[(incoming["ts"] > a) & (incoming["ts"] < b)]))
        if oc >= 2 or inc >= 1:
            rows.append({"start_ts": a, "end_ts": b, "gap_s": round(gap, 3), "other_player_casts": oc, "incoming_hits": inc})
    return pd.DataFrame(rows).sort_values("gap_s", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


def _cast_timing_outliers(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    x = _slice(df, segment)
    src = x[x["source_name"] == player]
    pairs = _pair_casts(src)
    if pairs.empty:
        return pd.DataFrame()
    good = pairs[pairs["result"] == "success"].copy()
    rows = []
    for (spell_id, spell_name), g in good.groupby(["spell_id", "spell_name"], dropna=False):
        vals = [float(v) for v in g["duration_s"].tolist() if v >= 0]
        if len(vals) < 4:
            continue
        med, mad = _robust_center(vals)
        limit = med + max(0.25, 4 * mad)
        for r in g[g["duration_s"] > limit].itertuples(index=False):
            rows.append({
                "spell_id": spell_id,
                "spell_name": spell_name,
                "duration_s": round(float(r.duration_s), 3),
                "spell_median_s": round(med, 3),
                "excess_s": round(float(r.duration_s) - med, 3),
                "start_ts": float(r.start_ts),
            })
    return pd.DataFrame(rows).sort_values("excess_s", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


def responsiveness_summary(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    x = _slice(df, segment)
    if x.empty or not player:
        return {"player": player, "score": 0, "label": "无数据", "confidence": "low"}

    src = x[x["source_name"] == player].sort_values("ts")
    cast_success = src[src["event"].isin(CAST_EVENTS)]
    cast_start = src[src["event"].isin(CAST_START_EVENTS)]
    cast_failed = src[src["event"].isin(CAST_FAILED_EVENTS)]
    gaps = action_gap_table(x, player)
    swing = _swing_stalls(x, player)
    timing = _cast_timing_outliers(x, player)

    action_ts = _dedupe_times(cast_success["ts"].tolist())
    intervals = [b - a for a, b in zip(action_ts[:-1], action_ts[1:]) if 0 < b - a <= 12]
    med_interval = float(median(intervals)) if intervals else 0.0
    p95_interval = float(pd.Series(intervals).quantile(0.95)) if intervals else 0.0

    gap_count = int(len(gaps))
    death_excluded_gap_count = int(gaps.attrs.get("death_excluded_gap_count", 0))
    severe_gap_count = int((gaps["gap_s"] >= 4.5).sum()) if not gaps.empty else 0
    isolated_count = int(gaps["isolated_player_stall"].sum()) if not gaps.empty else 0
    full_silence_count = int(gaps["full_manual_silence"].sum()) if not gaps.empty else 0
    longest_gap = float(gaps["gap_s"].max()) if not gaps.empty else 0.0
    stall_seconds = float(gaps["gap_s"].sum()) if not gaps.empty else 0.0

    start_ts = float(x["ts"].min()) if not x.empty else 0.0
    end_ts = float(x["ts"].max()) if not x.empty else 0.0
    duration = max(end_ts - start_ts, 1.0)

    # Conservative score: this is a responsiveness-anomaly score, not a network-lag score.
    score = 0.0
    score += min(gap_count * 5.0, 20.0)
    score += min(severe_gap_count * 9.0, 27.0)
    score += min(isolated_count * 6.0, 18.0)
    score += min(full_silence_count * 4.0, 12.0)
    if longest_gap >= 6:
        score += 8
    if longest_gap >= 10:
        score += 6
    stall_ratio = stall_seconds / duration
    if stall_ratio >= 0.01:
        score += 4
    if stall_ratio >= 0.03:
        score += 6
    score += min(len(timing) * 2.0, 6.0)
    score += min(len(swing) * 1.5, 6.0)
    score = int(round(min(score, 100.0)))

    cast_count = int(len(cast_success))
    if cast_count >= 50 and duration >= 120:
        confidence = "high"
    elif cast_count >= 20 and duration >= 60:
        confidence = "medium"
    else:
        confidence = "low"

    if score < 20:
        label = "未见明显响应异常"
    elif score < 40:
        label = "轻微响应异常"
    elif score < 60:
        label = "明显响应异常，建议复核"
    else:
        label = "高疑似存在卡顿/输入/网络响应问题"

    failures = Counter(str(v) for v in cast_failed.get("failure_reason", pd.Series(dtype=str)).dropna().tolist() if str(v))
    evidence = []
    if gap_count:
        evidence.append(f"玩家存活且战斗活跃期间出现 {gap_count} 个长施法空档，其中 {isolated_count} 个空档期间其他队员仍持续施法")
    if death_excluded_gap_count:
        evidence.append(f"另有 {death_excluded_gap_count} 个跨越死亡的停手窗口已排除，不计入卡顿/响应异常")
    if severe_gap_count:
        evidence.append(f"存在 {severe_gap_count} 个 ≥4.5 秒的严重空档，最长 {longest_gap:.2f} 秒")
    if full_silence_count:
        evidence.append(f"{full_silence_count} 个异常窗口中该玩家连平砍/伤害事件也同时消失")
    if len(swing):
        evidence.append(f"近战平砍节奏出现 {len(swing)} 个异常长间隔（只能作为弱证据）")
    if len(timing):
        evidence.append(f"同技能读条观测时长出现 {len(timing)} 个明显离群点（可能受急速/机制影响）")
    if failures:
        evidence.append("失败施法：" + "、".join(f"{k}×{v}" for k, v in failures.most_common(5)))
    if not evidence:
        evidence.append("当前日志中没有形成足够强的异常时间模式")

    return {
        "player": player,
        "score": score,
        "label": label,
        "confidence": confidence,
        "duration_s": round(duration, 2),
        "cast_successes": cast_count,
        "cast_starts": int(len(cast_start)),
        "cast_failures": int(len(cast_failed)),
        "median_action_interval_s": round(med_interval, 3),
        "p95_action_interval_s": round(p95_interval, 3),
        "active_long_gap_count": gap_count,
        "death_excluded_gap_count": death_excluded_gap_count,
        "alive_in_combat_only": True,
        "severe_gap_count": severe_gap_count,
        "isolated_stall_count": isolated_count,
        "full_silence_count": full_silence_count,
        "longest_gap_s": round(longest_gap, 3),
        "estimated_stall_seconds": round(stall_seconds, 2),
        "estimated_stall_ratio_pct": round(stall_ratio * 100, 2),
        "swing_stall_count": int(len(swing)),
        "cast_timing_outlier_count": int(len(timing)),
        "failure_reasons": dict(failures),
        "evidence": evidence,
        "network_conclusion": "异常停手只统计战斗活跃且玩家存活的窗口；死亡窗口已排除。即使如此，仅凭 Combat Log 仍不能证明是网络延迟，需要与 World latency / FPS 遥测或当时录像结合。",
    }


def responsiveness_details(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict[str, pd.DataFrame]:
    return {
        "action_gaps": action_gap_table(df, player, segment),
        "swing_stalls": _swing_stalls(df, player, segment),
        "cast_timing_outliers": _cast_timing_outliers(df, player, segment),
    }
