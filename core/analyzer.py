from __future__ import annotations

from dataclasses import asdict
from typing import Iterable, Optional

import pandas as pd

from .parser import CombatEvent, DAMAGE_EVENTS, HEAL_EVENTS, CAST_EVENTS, INTERRUPT_EVENTS, DEATH_EVENTS


def events_df(events: Iterable[CombatEvent]) -> pd.DataFrame:
    rows = [asdict(e) for e in events]
    if not rows:
        return pd.DataFrame(columns=list(CombatEvent.__dataclass_fields__.keys()))
    return pd.DataFrame(rows)




def split_mplus_runs(df: pd.DataFrame, source_name: str = "log") -> dict[str, pd.DataFrame]:
    """Split one physical combat log into independent Mythic+ samples when possible."""
    if df.empty or "run_segment" not in df.columns:
        return {source_name: df}
    segments = [str(x) for x in df["run_segment"].dropna().unique() if str(x) != "Full Log"]
    if not segments:
        return {source_name: df}
    out: dict[str, pd.DataFrame] = {}
    for seg in segments:
        part = df[df["run_segment"] == seg].copy()
        if part.empty:
            continue
        out[f"{source_name} :: {seg}"] = part.reset_index(drop=True)
    return out or {source_name: df}

def list_players(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    names = set()
    if "source_guid" in df.columns:
        m = df["source_guid"].fillna("").astype(str).str.startswith("Player-")
        names.update(df.loc[m, "source_name"].dropna().astype(str))
    if "dest_guid" in df.columns:
        m = df["dest_guid"].fillna("").astype(str).str.startswith("Player-")
        names.update(df.loc[m, "dest_name"].dropna().astype(str))
    if names:
        return sorted(n for n in names if n)
    active = set(df.loc[df["source_name"].notna(), "source_name"].astype(str))
    return sorted(n for n in active if n)


SPEC_MAP = {
    250: ("Death Knight", "Blood"), 251: ("Death Knight", "Frost"), 252: ("Death Knight", "Unholy"),
    577: ("Demon Hunter", "Havoc"), 581: ("Demon Hunter", "Vengeance"),
    102: ("Druid", "Balance"), 103: ("Druid", "Feral"), 104: ("Druid", "Guardian"), 105: ("Druid", "Restoration"),
    1467: ("Evoker", "Devastation"), 1468: ("Evoker", "Preservation"), 1473: ("Evoker", "Augmentation"),
    253: ("Hunter", "Beast Mastery"), 254: ("Hunter", "Marksmanship"), 255: ("Hunter", "Survival"),
    62: ("Mage", "Arcane"), 63: ("Mage", "Fire"), 64: ("Mage", "Frost"),
    268: ("Monk", "Brewmaster"), 269: ("Monk", "Windwalker"), 270: ("Monk", "Mistweaver"),
    65: ("Paladin", "Holy"), 66: ("Paladin", "Protection"), 70: ("Paladin", "Retribution"),
    256: ("Priest", "Discipline"), 257: ("Priest", "Holy"), 258: ("Priest", "Shadow"),
    259: ("Rogue", "Assassination"), 260: ("Rogue", "Outlaw"), 261: ("Rogue", "Subtlety"),
    262: ("Shaman", "Elemental"), 263: ("Shaman", "Enhancement"), 264: ("Shaman", "Restoration"),
    265: ("Warlock", "Affliction"), 266: ("Warlock", "Demonology"), 267: ("Warlock", "Destruction"),
    71: ("Warrior", "Arms"), 72: ("Warrior", "Fury"), 73: ("Warrior", "Protection"),
}


def infer_player_specs(df: pd.DataFrame) -> dict[str, dict]:
    """Join COMBATANT_INFO spec IDs to player names via GUIDs seen in normal events."""
    if df.empty or "spec_id" not in df.columns:
        return {}
    guid_to_name = {}
    for guid_col, name_col in (("source_guid", "source_name"), ("dest_guid", "dest_name")):
        if guid_col not in df or name_col not in df:
            continue
        x = df[[guid_col, name_col]].dropna()
        for guid, name in x.itertuples(index=False):
            if str(guid).startswith("Player-") and name:
                guid_to_name[str(guid)] = str(name)
    out = {}
    specs = df[(df["event"] == "COMBATANT_INFO") & df["spec_id"].notna()]
    for row in specs.itertuples(index=False):
        guid = str(getattr(row, "source_guid", "") or "")
        name = guid_to_name.get(guid)
        if not name:
            continue
        spec_id = int(getattr(row, "spec_id"))
        cls, spec = SPEC_MAP.get(spec_id, ("", f"Spec {spec_id}"))
        out[name] = {"spec_id": spec_id, "class_name": cls, "spec_name": spec}
    return out


def infer_run_metadata(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"dungeon": "", "key_level": 0, "map_id": None, "challenge_mode_id": None, "affixes": ""}

    def first_non_null(col, default=None):
        if col not in df.columns:
            return default
        s = df[col].dropna()
        if s.empty:
            return default
        v = s.iloc[0]
        if isinstance(v, float) and pd.isna(v):
            return default
        return v

    dungeon = first_non_null("dungeon", "") or ""
    key = first_non_null("keystone_level", 0) or 0
    return {
        "dungeon": str(dungeon),
        "key_level": int(key) if str(key).replace(".", "", 1).isdigit() else 0,
        "map_id": int(first_non_null("map_id")) if first_non_null("map_id") is not None else None,
        "challenge_mode_id": int(first_non_null("challenge_mode_id")) if first_non_null("challenge_mode_id") is not None else None,
        "affixes": first_non_null("affixes", "") or "",
    }


def _slice(df: pd.DataFrame, segment: Optional[str] = None) -> pd.DataFrame:
    if not segment or segment == "All":
        return df
    if segment.startswith("RUN::") and "run_segment" in df.columns:
        return df[df["run_segment"] == segment[5:]]
    return df[df["segment"] == segment] if "segment" in df.columns else df


def duration_seconds(df: pd.DataFrame, player: Optional[str] = None, segment: Optional[str] = None) -> float:
    x = _slice(df, segment)
    if x.empty:
        return 0.0

    # For a full Mythic+ sample, prefer the real key start/end markers. Player-specific
    # DPS should use the same run clock so cross-run comparisons are normalized fairly.
    if "event" in x.columns:
        starts = x.loc[x["event"] == "CHALLENGE_MODE_START", "ts"]
        ends = x.loc[x["event"] == "CHALLENGE_MODE_END", "ts"]
        if not starts.empty and not ends.empty and float(ends.max()) > float(starts.min()):
            return max(float(ends.max() - starts.min()), 1.0)

    if player:
        px = x[(x["source_name"] == player) | (x["dest_name"] == player)]
        if not px.empty:
            x = px
    return max(float(x["ts"].max() - x["ts"].min()), 1.0)


def player_overview(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> dict:
    x = _slice(df, segment)
    dur = duration_seconds(x, player)
    src = x[x["source_name"] == player]
    taken = x[x["dest_name"] == player]

    damage = float(src[src["event"].isin(DAMAGE_EVENTS)]["amount"].sum())
    healing = float(src[src["event"].isin(HEAL_EVENTS)]["amount"].sum())
    overheal = float(src[src["event"].isin(HEAL_EVENTS)]["overheal"].sum())
    dmg_taken = float(taken[taken["event"].isin(DAMAGE_EVENTS)]["amount"].sum())
    casts = int(src[src["event"].isin(CAST_EVENTS)].shape[0])
    interrupts = int(src[src["event"].isin(INTERRUPT_EVENTS)].shape[0])
    deaths = int(taken[taken["event"].isin(DEATH_EVENTS)].shape[0])

    return {
        "player": player,
        "duration_s": round(dur, 1),
        "damage": round(damage, 0),
        "dps": round(damage / dur, 1) if dur else 0,
        "healing": round(healing, 0),
        "hps": round(healing / dur, 1) if dur else 0,
        "overheal": round(overheal, 0),
        "damage_taken": round(dmg_taken, 0),
        "casts": casts,
        "interrupts": interrupts,
        "deaths": deaths,
    }


def skill_breakdown(df: pd.DataFrame, player: str, segment: Optional[str] = None) -> pd.DataFrame:
    x = _slice(df, segment)
    src = x[x["source_name"] == player].copy()
    if src.empty:
        return pd.DataFrame()

    dmg = src[src["event"].isin(DAMAGE_EVENTS)].copy()
    if dmg.empty:
        damage_tbl = pd.DataFrame(columns=["spell_id","spell_name","damage","hits","crits"])
    else:
        dmg["spell_name"] = dmg["spell_name"].fillna("Unknown")
        damage_tbl = (dmg.groupby(["spell_id","spell_name"], dropna=False)
                      .agg(damage=("amount","sum"), hits=("amount","size"), crits=("critical","sum"))
                      .reset_index())

    casts = src[src["event"].isin(CAST_EVENTS)].copy()
    if casts.empty:
        cast_tbl = pd.DataFrame(columns=["spell_id","spell_name","casts"])
    else:
        casts["spell_name"] = casts["spell_name"].fillna("Unknown")
        cast_tbl = (casts.groupby(["spell_id","spell_name"], dropna=False)
                    .size().rename("casts").reset_index())

    out = pd.merge(damage_tbl, cast_tbl, on=["spell_id","spell_name"], how="outer").fillna(0)
    if out.empty:
        return out
    total = max(float(out["damage"].sum()), 1.0)
    out["damage_pct"] = out["damage"] / total * 100
    out["crit_pct"] = out.apply(lambda r: (r["crits"] / r["hits"] * 100) if r["hits"] else 0, axis=1)
    out["avg_hit"] = out.apply(lambda r: (r["damage"] / r["hits"]) if r["hits"] else 0, axis=1)
    out["damage_per_cast"] = out.apply(lambda r: (r["damage"] / r["casts"]) if r["casts"] else 0, axis=1)
    dur_min = max(duration_seconds(x) / 60.0, 1.0 / 60.0)
    out["casts_per_min"] = out["casts"] / dur_min
    out["hits_per_cast"] = out.apply(lambda r: (r["hits"] / r["casts"]) if r["casts"] else 0, axis=1)
    out = out.sort_values(["damage","casts"], ascending=False)
    return out[["spell_id","spell_name","damage","damage_pct","hits","crits","crit_pct","avg_hit","casts","casts_per_min","hits_per_cast","damage_per_cast"]]


def team_overview(df: pd.DataFrame, segment: Optional[str] = None) -> pd.DataFrame:
    players = list_players(_slice(df, segment))
    rows = [player_overview(df, p, segment) for p in players]
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values("damage", ascending=False)
    team_damage = max(float(out["damage"].sum()), 1.0)
    team_healing = max(float(out["healing"].sum()), 1.0)
    out["team_damage_pct"] = out["damage"] / team_damage * 100
    out["team_healing_pct"] = out["healing"] / team_healing * 100
    return out


def team_summary(df: pd.DataFrame, segment: Optional[str] = None) -> dict:
    x = _slice(df, segment)
    t = team_overview(df, segment)
    dur = duration_seconds(x)
    if t.empty:
        return {"duration_s": dur, "team_damage": 0, "team_dps": 0, "team_healing": 0, "team_hps": 0,
                "damage_taken": 0, "casts": 0, "interrupts": 0, "deaths": 0, "players": 0}
    team_damage = float(t["damage"].sum())
    team_heal = float(t["healing"].sum())
    return {
        "duration_s": round(dur, 1),
        "team_damage": round(team_damage, 0),
        "team_dps": round(team_damage / dur, 1) if dur else 0,
        "team_healing": round(team_heal, 0),
        "team_hps": round(team_heal / dur, 1) if dur else 0,
        "damage_taken": round(float(t["damage_taken"].sum()), 0),
        "casts": int(t["casts"].sum()),
        "interrupts": int(t["interrupts"].sum()),
        "deaths": int(t["deaths"].sum()),
        "players": int(t.shape[0]),
    }


def compare_teams(run_dfs: dict[str, pd.DataFrame], run_names: list[str], segment: str = "All") -> pd.DataFrame:
    rows = []
    for run in run_names:
        if run not in run_dfs:
            continue
        r = team_summary(run_dfs[run], None if segment == "All" else segment)
        r["run"] = run
        rows.append(r)
    if not rows:
        return pd.DataFrame()
    cols = ["run","duration_s","team_damage","team_dps","team_healing","team_hps","damage_taken","casts","interrupts","deaths","players"]
    return pd.DataFrame(rows)[cols]


def compare_runs(run_dfs: dict[str, pd.DataFrame], player: str, segment: str = "All") -> pd.DataFrame:
    rows = []
    for name, df in run_dfs.items():
        if player not in list_players(df):
            continue
        r = player_overview(df, player, segment if segment != "All" else None)
        r["run"] = name
        rows.append(r)
    if not rows:
        return pd.DataFrame()
    cols = ["run","player","duration_s","damage","dps","healing","hps","damage_taken","casts","interrupts","deaths"]
    return pd.DataFrame(rows)[cols].sort_values("dps", ascending=False)


def compare_selected_players(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All") -> pd.DataFrame:
    rows = []
    for run, player in selections.items():
        df = run_dfs.get(run)
        if df is None or not player or player not in list_players(df):
            continue
        r = player_overview(df, player, None if segment == "All" else segment)
        r["run"] = run
        rows.append(r)
    if not rows:
        return pd.DataFrame()
    cols = ["run","player","duration_s","damage","dps","healing","hps","damage_taken","casts","interrupts","deaths"]
    return pd.DataFrame(rows)[cols]


def compare_skills(run_dfs: dict[str, pd.DataFrame], player: str, segment: str = "All") -> pd.DataFrame:
    selections = {run: player for run in run_dfs if player in list_players(run_dfs[run])}
    return compare_selected_skills(run_dfs, selections, segment)


def compare_selected_skills(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All") -> pd.DataFrame:
    chunks = []
    for run, player in selections.items():
        df = run_dfs.get(run)
        if df is None or player not in list_players(df):
            continue
        s = skill_breakdown(df, player, None if segment == "All" else segment)
        if s.empty:
            continue
        s = s.copy()
        s.insert(0, "player", player)
        s.insert(0, "run", run)
        chunks.append(s)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def skill_share_matrix(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All", top_n: int = 20) -> pd.DataFrame:
    long = compare_selected_skills(run_dfs, selections, segment)
    if long.empty:
        return long
    top_spells = (long.groupby("spell_name", dropna=False)["damage"].sum().nlargest(top_n).index)
    x = long[long["spell_name"].isin(top_spells)].copy()
    labels = {run: f"{run} · {player}" for run, player in selections.items()}
    x["sample"] = x["run"].map(labels)
    pivot = x.pivot_table(index="spell_name", columns="sample", values="damage_pct", aggfunc="sum", fill_value=0)
    pivot["样本均值"] = pivot.mean(axis=1)
    pivot["最大差值"] = pivot.max(axis=1) - pivot.drop(columns=["样本均值"], errors="ignore").min(axis=1)
    return pivot.sort_values("样本均值", ascending=False).reset_index()


def skill_benchmark(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All", top_n: int = 30) -> pd.DataFrame:
    """Aggregate same-spec samples into a compact cohort benchmark per spell."""
    long = compare_selected_skills(run_dfs, selections, segment)
    if long.empty:
        return long
    metrics = ["damage_pct", "casts_per_min", "hits_per_cast", "crit_pct", "avg_hit", "damage_per_cast"]
    grouped = long.groupby(["spell_id", "spell_name"], dropna=False)[metrics]
    agg = grouped.agg(["mean", "median", "min", "max"])
    agg.columns = [f"{metric}_{stat}" for metric, stat in agg.columns]
    out = agg.reset_index()
    for q, suffix in ((0.25, "p25"), (0.75, "p75"), (0.90, "p90")):
        qdf = grouped.quantile(q).reset_index()
        qdf = qdf.rename(columns={m: f"{m}_{suffix}" for m in metrics})
        out = out.merge(qdf, on=["spell_id", "spell_name"], how="left")
    sample_count = long.groupby(["spell_id", "spell_name"], dropna=False)["run"].nunique().rename("sample_count").reset_index()
    out = out.merge(sample_count, on=["spell_id", "spell_name"], how="left")
    return out.sort_values("damage_pct_median", ascending=False).head(top_n).reset_index(drop=True)


def selected_player_vs_cohort_median(run_dfs: dict[str, pd.DataFrame], selections: dict[str, str], segment: str = "All") -> pd.DataFrame:
    """Return each selected player's top skills with deltas to the cohort median."""
    long = compare_selected_skills(run_dfs, selections, segment)
    if long.empty:
        return long
    metrics = ["damage_pct", "casts_per_min", "hits_per_cast", "crit_pct", "avg_hit", "damage_per_cast"]
    med = long.groupby(["spell_id", "spell_name"], dropna=False)[metrics].median().add_suffix("_median").reset_index()
    out = long.merge(med, on=["spell_id", "spell_name"], how="left")
    for metric in metrics:
        denom = out[f"{metric}_median"].replace(0, pd.NA)
        out[f"{metric}_vs_median_pct"] = ((out[metric] - out[f"{metric}_median"]) / denom * 100).fillna(0)
    return out


def team_player_matrix(run_dfs: dict[str, pd.DataFrame], run_names: list[str], segment: str = "All") -> pd.DataFrame:
    chunks = []
    for run in run_names:
        if run not in run_dfs:
            continue
        t = team_overview(run_dfs[run], None if segment == "All" else segment).copy()
        if t.empty:
            continue
        t.insert(0, "run", run)
        chunks.append(t)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def cohort_runs(run_meta: dict[str, dict], dungeon: str = "", key_level: int = 0, class_name: str = "", spec_name: str = "") -> list[str]:
    matched = []
    for run, meta in run_meta.items():
        if dungeon and meta.get("dungeon") != dungeon:
            continue
        if key_level and int(meta.get("key_level") or 0) != int(key_level):
            continue
        if class_name and meta.get("class_name") != class_name:
            continue
        if spec_name and meta.get("spec_name") != spec_name:
            continue
        if meta.get("focus_player"):
            matched.append(run)
    return matched


def run_quality(df: pd.DataFrame) -> dict:
    """Return lightweight data-quality diagnostics for one sample."""
    if df.empty:
        return {"events": 0, "has_mplus_start": False, "has_mplus_end": False, "has_combatant_info": False, "player_count": 0, "warnings": ["empty sample"]}
    events = set(df.get("event", pd.Series(dtype=str)).dropna().astype(str))
    warnings = []
    has_start = "CHALLENGE_MODE_START" in events
    has_end = "CHALLENGE_MODE_END" in events
    if has_start and not has_end:
        warnings.append("missing CHALLENGE_MODE_END; run duration may be incomplete")
    if not has_start:
        warnings.append("no Mythic+ start marker; dungeon/key metadata may require manual labels")
    has_ci = "COMBATANT_INFO" in events
    if not has_ci:
        warnings.append("no COMBATANT_INFO; class/spec auto-detection may be unavailable")
    return {
        "events": int(len(df)),
        "has_mplus_start": has_start,
        "has_mplus_end": has_end,
        "has_combatant_info": has_ci,
        "player_count": len(list_players(df)),
        "warnings": warnings,
    }


def compact_for_ai(run_dfs: dict[str, pd.DataFrame], focus_player: Optional[str] = None, segment: str = "All", top_skills: int = 15) -> dict:
    selections = {}
    if focus_player:
        for run, df in run_dfs.items():
            if focus_player in list_players(df):
                selections[run] = focus_player
    return compact_comparison_for_ai(run_dfs, selections, list(run_dfs), segment, top_skills)


def compact_comparison_for_ai(
    run_dfs: dict[str, pd.DataFrame],
    selections: dict[str, str] | None = None,
    team_runs: list[str] | None = None,
    segment: str = "All",
    top_skills: int = 20,
    run_meta: dict[str, dict] | None = None,
) -> dict:
    selections = selections or {}
    team_runs = team_runs or list(run_dfs)
    run_meta = run_meta or {}
    out = {
        "team_comparison": compare_teams(run_dfs, team_runs, segment).round(2).to_dict("records"),
        "team_players": team_player_matrix(run_dfs, team_runs, segment).round(2).to_dict("records"),
        "player_comparison": compare_selected_players(run_dfs, selections, segment).round(2).to_dict("records"),
        "skill_comparison": compare_selected_skills(run_dfs, selections, segment).groupby("run", group_keys=False).head(top_skills).round(2).to_dict("records"),
        "run_metadata": {k: run_meta.get(k, {}) for k in set(team_runs) | set(selections)},
        "data_quality": {k: run_quality(run_dfs[k]) for k in set(team_runs) | set(selections) if k in run_dfs},
    }
    matrix = skill_share_matrix(run_dfs, selections, segment, top_n=top_skills)
    out["skill_share_matrix"] = matrix.round(2).to_dict("records") if not matrix.empty else []

    # Responsiveness diagnostics are computed from the event timeline. They are
    # deliberately labeled as anomaly evidence, not direct proof of network lag.
    try:
        from .responsiveness import responsiveness_summary
        out["responsiveness"] = {
            run: responsiveness_summary(run_dfs[run], player, None if segment == "All" else segment)
            for run, player in selections.items()
            if run in run_dfs and player
        }
    except Exception:
        out["responsiveness"] = {}
    return out
