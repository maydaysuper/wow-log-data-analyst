from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .knowledge import save_knowledge_sample
from .learning import default_db_path
from .online_learning import build_wcl_behavior_signature
from .wcl_direct_analysis import (
    fights_for_source,
    resolve_report_actor,
    spec_and_item_level_for_fight,
    fetch_event_bundle,
)
from .wcl_recent_runs import is_timed_keystone


# Personal baseline data lives in the same local SQLite database as the other
# learning stores. This keeps backup/deletion simple while keeping the tables
# logically separate from cohort/class knowledge.


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db = Path(path or default_db_path())
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # Another worker may be switching the same database to WAL at this instant.
        # The connection is still usable; busy_timeout covers the transient lock.
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_personal_baseline_db(path: Path | None = None) -> Path:
    db = Path(path or default_db_path())
    with _connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS personal_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                character_key TEXT NOT NULL,
                character_id INTEGER DEFAULT 0,
                canonical_id TEXT DEFAULT '',
                character_name TEXT DEFAULT '',
                server_slug TEXT DEFAULT '',
                server_region TEXT DEFAULT '',
                fingerprint TEXT NOT NULL,
                report_code TEXT DEFAULT '',
                fight_id INTEGER DEFAULT 0,
                source_id INTEGER DEFAULT 0,
                dungeon TEXT DEFAULT '',
                key_level INTEGER DEFAULT 0,
                class_name TEXT DEFAULT '',
                spec_name TEXT DEFAULT '',
                patch_scope TEXT DEFAULT '',
                player_item_level REAL DEFAULT 0,
                performance_value REAL DEFAULT 0,
                signature_json TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                UNIQUE(character_key, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_personal_samples_character
                ON personal_samples(character_key, spec_name, patch_scope, dungeon, key_level, id);
            CREATE INDEX IF NOT EXISTS idx_personal_samples_source
                ON personal_samples(report_code, fight_id, source_id);
            CREATE TABLE IF NOT EXISTS personal_model_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                character_key TEXT NOT NULL,
                sample_count INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                scope TEXT DEFAULT '',
                summary_markdown TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_personal_model_summaries_character
                ON personal_model_summaries(character_key, id);
            """
        )
    return db


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else 0.0
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def character_identity(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, display-friendly identity from WCL Character data."""
    server = profile.get("server") or {}
    region = server.get("region") or {}
    char_id = _safe_int(profile.get("id"))
    canonical = str(profile.get("canonicalID") or "").strip()
    name = str(profile.get("name") or "").strip()
    slug = str(server.get("slug") or server.get("name") or "").strip()
    region_slug = str(region.get("slug") or region.get("compactName") or region.get("name") or "").strip().lower()

    if canonical:
        key_raw = f"canonical:{canonical.lower()}"
    elif char_id:
        key_raw = f"wcl-character:{char_id}"
    else:
        key_raw = f"character:{region_slug}:{slug.lower()}:{name.lower()}"
    key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
    return {
        "character_key": key,
        "character_id": char_id,
        "canonical_id": canonical,
        "character_name": name,
        "server_slug": slug,
        "server_region": region_slug,
        "display": " · ".join(x for x in (name, slug, region_slug.upper()) if x),
    }


def signature_fingerprint(signature: dict[str, Any]) -> str:
    prov = signature.get("provenance") or {}
    if prov.get("report_code") and prov.get("fight_id") and prov.get("source_id"):
        raw = f"wcl:{prov.get('report_code')}:{_safe_int(prov.get('fight_id'))}:{_safe_int(prov.get('source_id'))}"
    else:
        raw = _json(signature)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_personal_sample(
    character: dict[str, Any],
    signature: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> int:
    init_personal_baseline_db(path)
    identity = character_identity(character) if "character_key" not in character else dict(character)
    ctx = signature.get("context") or {}
    prov = signature.get("provenance") or {}
    overview = signature.get("overview") or {}
    fp = signature_fingerprint(signature)
    with _connect(path) as conn:
        existing = conn.execute(
            "SELECT id FROM personal_samples WHERE character_key=? AND fingerprint=?",
            (identity["character_key"], fp),
        ).fetchone()
        values = (
            datetime.now(timezone.utc).isoformat(),
            identity["character_key"], _safe_int(identity.get("character_id")),
            str(identity.get("canonical_id") or ""), str(identity.get("character_name") or ""),
            str(identity.get("server_slug") or ""), str(identity.get("server_region") or ""), fp,
            str(prov.get("report_code") or ""), _safe_int(prov.get("fight_id")), _safe_int(prov.get("source_id")),
            str(ctx.get("dungeon") or ""), _safe_int(ctx.get("key_level")), str(ctx.get("class_name") or ""),
            str(ctx.get("spec_name") or ""), str(ctx.get("patch_scope") or prov.get("patch_scope") or ""),
            _safe_float(ctx.get("player_item_level") or ctx.get("average_item_level")),
            _safe_float(overview.get("dps")), _json(signature), _json(metadata or {}),
        )
        # Atomic UPSERT: baseline sync can overlap with background learning or an
        # accidental double-click. A SELECT-then-INSERT race used to surface as
        # UNIQUE constraint failures even though the sample was already valid.
        conn.execute(
            """
            INSERT INTO personal_samples(
                created_at, character_key, character_id, canonical_id, character_name, server_slug, server_region,
                fingerprint, report_code, fight_id, source_id, dungeon, key_level, class_name, spec_name, patch_scope,
                player_item_level, performance_value, signature_json, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(character_key, fingerprint) DO UPDATE SET
                created_at=excluded.created_at, character_id=excluded.character_id, canonical_id=excluded.canonical_id,
                character_name=excluded.character_name, server_slug=excluded.server_slug, server_region=excluded.server_region,
                report_code=excluded.report_code, fight_id=excluded.fight_id, source_id=excluded.source_id,
                dungeon=excluded.dungeon, key_level=excluded.key_level, class_name=excluded.class_name,
                spec_name=excluded.spec_name, patch_scope=excluded.patch_scope, player_item_level=excluded.player_item_level,
                performance_value=excluded.performance_value, signature_json=excluded.signature_json, metadata_json=excluded.metadata_json
            """,
            values,
        )
        row = conn.execute(
            "SELECT id FROM personal_samples WHERE character_key=? AND fingerprint=?",
            (identity["character_key"], fp),
        ).fetchone()
        return int(row["id"])


def has_personal_sample(
    character_key: str,
    report_code: str,
    fight_id: int,
    source_id: int,
    *,
    min_schema_version: int = 0,
    path: Path | None = None,
) -> bool:
    """Return True when the fight is already learned at the requested feature schema.

    v0.16 introduced battle-window Buff/cadence features.  Asking for schema>=4 lets
    sync jobs enrich older stored fights once, while current-schema fights still skip
    before any expensive WCL event download.
    """
    init_personal_baseline_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT signature_json FROM personal_samples WHERE character_key=? AND report_code=? AND fight_id=? AND source_id=? LIMIT 1",
            (character_key, str(report_code), int(fight_id), int(source_id)),
        ).fetchone()
    if not row:
        return False
    if int(min_schema_version or 0) <= 0:
        return True
    try:
        sig = json.loads(row["signature_json"] or "{}")
        return int(sig.get("schema_version") or 0) >= int(min_schema_version)
    except Exception:
        return False


def list_personal_samples(
    character_key: str,
    *,
    spec_name: str = "",
    patch_scope: str = "",
    dungeon: str = "",
    key_level: int = 0,
    key_tolerance: int = 99,
    limit: int = 500,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    init_personal_baseline_db(path)
    clauses = ["character_key=?"]
    args: list[Any] = [character_key]
    if spec_name:
        clauses.append("lower(spec_name)=lower(?)")
        args.append(spec_name)
    if patch_scope:
        clauses.append("(patch_scope=? OR patch_scope LIKE ? OR ? LIKE patch_scope || '%')")
        args.extend([patch_scope, patch_scope + "%", patch_scope])
    if dungeon:
        clauses.append("lower(dungeon)=lower(?)")
        args.append(dungeon)
    if key_level and key_tolerance < 99:
        clauses.append("key_level BETWEEN ? AND ?")
        args.extend([max(0, int(key_level) - int(key_tolerance)), int(key_level) + int(key_tolerance)])
    args.append(max(1, int(limit)))
    sql = "SELECT * FROM personal_samples WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?"
    with _connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def delete_personal_sample(sample_id: int, path: Path | None = None) -> None:
    init_personal_baseline_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM personal_samples WHERE id=?", (int(sample_id),))


def clear_personal_samples(character_key: str, path: Path | None = None) -> int:
    init_personal_baseline_db(path)
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM personal_samples WHERE character_key=?", (character_key,))
        return int(cur.rowcount or 0)


def _load_signature(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("signature_json") or "{}")
    except Exception:
        return {}


def _q(values: Iterable[float], q: float) -> float:
    vals = [_safe_float(v) for v in values]
    vals = [v for v in vals if math.isfinite(v)]
    if not vals:
        return 0.0
    return float(pd.Series(vals, dtype=float).quantile(q))


def _quantiles(values: Iterable[float], digits: int = 3) -> dict[str, float]:
    vals = list(values)
    return {
        "p10": round(_q(vals, .10), digits),
        "p25": round(_q(vals, .25), digits),
        "p50": round(_q(vals, .50), digits),
        "p75": round(_q(vals, .75), digits),
        "p90": round(_q(vals, .90), digits),
    }


def _resp_aliases(canonical: str) -> list[str]:
    return {
        "score": ["score"],
        "gap_median_s": ["cast_gap_median_s", "median_action_interval_s"],
        "gap_p95_s": ["cast_gap_p95_s", "p95_action_interval_s"],
        "max_gap_s": ["max_cast_gap_s", "longest_gap_s"],
        "long_gap_count": ["long_gap_count", "active_long_gap_count"],
        "severe_gap_count": ["severe_gap_count"],
        "isolated_stall_count": ["isolated_stall_count"],
        "isolated_team_active_gap_count": ["isolated_team_active_gap_count"],
        "severe_isolated_team_active_gap_count": ["severe_isolated_team_active_gap_count"],
        "isolated_team_active_max_gap_s": ["isolated_team_active_max_gap_s"],
    }.get(canonical, [canonical])


def _resp_observed(resp: dict[str, Any], canonical: str) -> float | None:
    for key in _resp_aliases(canonical):
        if key in resp and resp.get(key) is not None:
            return _safe_float(resp.get(key))
    return None


def _resp_value(resp: dict[str, Any], canonical: str) -> float:
    value = _resp_observed(resp, canonical)
    return value if value is not None else 0.0

def _eff_aliases(section: str, key: str) -> list[str]:
    return {
        ("target_switching", "switches_per_min"): ["switches_per_min"],
        ("resource_efficiency", "avg_resource_pct"): ["avg_resource_pct"],
        ("resource_efficiency", "high_resource_pct"): ["time_at_or_above_90_pct"],
        ("resource_efficiency", "low_resource_pct"): ["time_at_or_below_10_pct"],
        ("resource_efficiency", "overcap_pct"): ["overcap_pct_of_generated"],
    }.get((section, key), [key])


def _eff_observed(eff: dict[str, Any], section: str, key: str) -> float | None:
    part = eff.get(section) or {}
    if not isinstance(part, dict):
        return None
    for candidate in _eff_aliases(section, key):
        if candidate in part and part.get(candidate) is not None:
            return _safe_float(part.get(candidate))
    return None


def _eff_value(eff: dict[str, Any], section: str, key: str) -> float:
    value = _eff_observed(eff, section, key)
    return value if value is not None else 0.0

def build_personal_profile(rows: list[dict[str, Any]], *, top_n_skills: int = 30) -> dict[str, Any]:
    """Build a personal historical distribution from stored behavior signatures.

    The profile is descriptive, not a claim that the player's historical behavior is
    optimal. It is designed to answer: "is this fight unusual for this player?"
    """
    samples = [(r, _load_signature(r)) for r in rows]
    samples = [(r, s) for r, s in samples if s]
    if not samples:
        return {
            "sample_count": 0,
            "skills": [],
            "responsiveness": {},
            "efficiency": {},
            "limitations": ["还没有个人历史样本。"],
        }

    skill_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    buff_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cadence_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    burst_overlap_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dps_values: list[float] = []
    casts_per_min_values: list[float] = []
    resp_bucket: dict[str, list[float]] = defaultdict(list)
    eff_bucket: dict[str, list[float]] = defaultdict(list)
    dungeon_counter: Counter[str] = Counter()
    spec_counter: Counter[str] = Counter()
    patch_counter: Counter[str] = Counter()
    key_values: list[float] = []
    ilvl_values: list[float] = []

    for row, sig in samples:
        ctx = sig.get("context") or {}
        ov = sig.get("overview") or {}
        duration = max(1.0, _safe_float(ov.get("duration_s")))
        casts = _safe_float(ov.get("casts"))
        if _safe_float(ov.get("dps")) > 0:
            dps_values.append(_safe_float(ov.get("dps")))
        if casts > 0:
            casts_per_min_values.append(casts / duration * 60.0)
        dungeon_counter[str(ctx.get("dungeon") or row.get("dungeon") or "")] += 1
        spec_counter[str(ctx.get("spec_name") or row.get("spec_name") or "")] += 1
        patch_counter[str(ctx.get("patch_scope") or row.get("patch_scope") or "")] += 1
        if _safe_int(ctx.get("key_level") or row.get("key_level")):
            key_values.append(_safe_int(ctx.get("key_level") or row.get("key_level")))
        ilvl = _safe_float(ctx.get("player_item_level") or ctx.get("average_item_level") or row.get("player_item_level"))
        if ilvl > 0:
            ilvl_values.append(ilvl)

        for skill in sig.get("skills") or []:
            name = str(skill.get("spell_name") or skill.get("spell_id") or "Unknown")
            skill_bucket[name].append(skill)

        resp = sig.get("responsiveness") or {}
        for metric in ("score", "gap_median_s", "gap_p95_s", "max_gap_s", "long_gap_count", "severe_gap_count", "isolated_stall_count", "isolated_team_active_gap_count", "severe_isolated_team_active_gap_count", "isolated_team_active_max_gap_s"):
            observed = _resp_observed(resp, metric)
            if observed is not None:
                resp_bucket[metric].append(observed)

        eff = sig.get("efficiency") or {}
        for buff in eff.get("buff_uptime") or []:
            name = str(buff.get("spell_name") or buff.get("spell_id") or "Unknown")
            if buff.get("combat_uptime_pct") is not None or buff.get("uptime_pct") is not None:
                buff_bucket[name].append(buff)
        for cad in eff.get("cast_cadence") or []:
            name = str(cad.get("spell_name") or cad.get("spell_id") or "Unknown")
            if int(cad.get("total_casts") or 0) > 0:
                cadence_bucket[name].append(cad)
        for ovlp in eff.get("buff_burst_overlap") or []:
            name = str(ovlp.get("spell_name") or ovlp.get("spell_id") or "Unknown")
            burst_overlap_bucket[name].append(ovlp)
        for section, metric, out_name in (
            ("resource_efficiency", "avg_resource_pct", "avg_resource_pct"),
            ("resource_efficiency", "high_resource_pct", "high_resource_pct"),
            ("resource_efficiency", "low_resource_pct", "low_resource_pct"),
            ("resource_efficiency", "overcap_pct", "overcap_pct"),
            ("target_switching", "switches_per_min", "switches_per_min"),
        ):
            observed = _eff_observed(eff, section, metric)
            if observed is not None:
                eff_bucket[out_name].append(observed)

    minimum_recurrence = 2 if len(samples) < 5 else max(3, math.ceil(len(samples) * .25))
    skill_rows: list[dict[str, Any]] = []
    for name, skill_samples in skill_bucket.items():
        if len(skill_samples) < minimum_recurrence:
            continue
        row: dict[str, Any] = {"spell_name": name, "sample_count": len(skill_samples)}
        for metric in ("casts_per_min", "damage_pct", "hits_per_cast", "crit_pct", "damage_per_cast"):
            row[metric] = _quantiles([_safe_float(x.get(metric)) for x in skill_samples], 4)
        skill_rows.append(row)
    skill_rows.sort(key=lambda x: (x["damage_pct"]["p50"], x["casts_per_min"]["p50"]), reverse=True)

    buff_rows: list[dict[str, Any]] = []
    for name, vals in buff_bucket.items():
        if len(vals) < minimum_recurrence:
            continue
        combat = [_safe_float(x.get("combat_uptime_pct") if x.get("combat_uptime_pct") is not None else x.get("uptime_pct")) for x in vals]
        avg_window = [_safe_float(x.get("avg_window_s")) for x in vals if x.get("avg_window_s") is not None]
        apps = [_safe_float(x.get("applications")) for x in vals]
        buff_rows.append({
            "spell_name": name, "sample_count": len(vals),
            "combat_uptime_pct": _quantiles(combat, 3),
            "avg_window_s": _quantiles(avg_window, 3),
            "applications": _quantiles(apps, 2),
        })
    buff_rows.sort(key=lambda x: (_safe_float((x.get("combat_uptime_pct") or {}).get("p50")), x.get("sample_count", 0)), reverse=True)

    cadence_rows: list[dict[str, Any]] = []
    for name, vals in cadence_bucket.items():
        if len(vals) < minimum_recurrence:
            continue
        cadence_rows.append({
            "spell_name": name, "sample_count": len(vals),
            "total_casts": _quantiles([_safe_float(x.get("total_casts")) for x in vals], 2),
            "combat_interval_median_s": _quantiles([_safe_float(x.get("combat_interval_median_s")) for x in vals if x.get("combat_interval_median_s") is not None], 3),
            "combat_interval_p90_s": _quantiles([_safe_float(x.get("combat_interval_p90_s")) for x in vals if x.get("combat_interval_p90_s") is not None], 3),
            "pulls_with_cast": _quantiles([_safe_float(x.get("pulls_with_cast")) for x in vals], 2),
        })
    cadence_rows.sort(key=lambda x: (_safe_float((x.get("total_casts") or {}).get("p50")), x.get("sample_count", 0)), reverse=True)

    burst_rows: list[dict[str, Any]] = []
    for name, vals in burst_overlap_bucket.items():
        if len(vals) < minimum_recurrence:
            continue
        burst_rows.append({
            "spell_name": name, "sample_count": len(vals),
            "burst_overlap_pct": _quantiles([_safe_float(x.get("burst_overlap_pct")) for x in vals], 3),
            "burst_windows_covered": _quantiles([_safe_float(x.get("burst_windows_covered")) for x in vals], 2),
        })
    burst_rows.sort(key=lambda x: _safe_float((x.get("burst_overlap_pct") or {}).get("p50")), reverse=True)

    resp_profile = {k: _quantiles(v, 3) for k, v in resp_bucket.items()}
    eff_profile = {k: _quantiles(v, 3) for k, v in eff_bucket.items()}
    sample_count = len(samples)
    confidence = "high" if sample_count >= 20 else "medium" if sample_count >= 8 else "low"
    return {
        "sample_count": sample_count,
        "confidence": confidence,
        "context": {
            "dungeons": dungeon_counter.most_common(12),
            "specs": spec_counter.most_common(8),
            "patches": patch_counter.most_common(8),
            "key_level": _quantiles(key_values, 1),
            "player_item_level": _quantiles(ilvl_values, 1),
        },
        "overview": {
            "dps": _quantiles(dps_values, 1),
            "casts_per_min": _quantiles(casts_per_min_values, 3),
        },
        "skills": skill_rows[:top_n_skills],
        "buffs": buff_rows[:25],
        "cadence": cadence_rows[:25],
        "buff_burst_overlap": burst_rows[:20],
        "responsiveness": resp_profile,
        "efficiency": eff_profile,
        "limitations": [
            "个人基线描述的是这个角色过去的行为分布，不等于理论最优打法。",
            "不同版本、装备、层数、副本、路线与目标数量会改变统计；条件越接近，个人对照越可靠。",
            "偏离个人响应性基线只能证明这把行为异常，不能单独证明网络或客户端卡顿。",
        ],
    }


def _where(value: float, q: dict[str, Any], *, higher_bad: bool | None = None) -> tuple[str, float]:
    p10, p25, p50, p75, p90 = (_safe_float(q.get(k)) for k in ("p10", "p25", "p50", "p75", "p90"))
    if value <= p10:
        bucket = "below_p10"
    elif value <= p25:
        bucket = "p10_p25"
    elif value <= p75:
        bucket = "p25_p75"
    elif value <= p90:
        bucket = "p75_p90"
    else:
        bucket = "above_p90"
    denom = abs(p50) if abs(p50) > 1e-9 else 1.0
    return bucket, (value - p50) / denom * 100.0


def compare_to_personal_profile(signature: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Compare one fight with the player's own historical distribution."""
    if int(profile.get("sample_count") or 0) < 3:
        return {
            "sample_count": int(profile.get("sample_count") or 0),
            "confidence": "low",
            "anomaly_score": 0,
            "findings": [],
            "limitations": ["个人样本少于 3 场，暂不做强异常判断。"],
        }

    findings: list[dict[str, Any]] = []
    score = 0.0
    profile_skills = {str(x.get("spell_name")): x for x in profile.get("skills") or []}
    current_skills = {str(x.get("spell_name")): x for x in signature.get("skills") or []}

    # Skill frequency is more comparable than raw damage. Flag only recurring baseline skills.
    for name, base in list(profile_skills.items())[:20]:
        current = current_skills.get(name)
        if not current:
            continue
        val = _safe_float(current.get("casts_per_min"))
        q = base.get("casts_per_min") or {}
        bucket, delta = _where(val, q)
        if bucket == "below_p10" and _safe_float(q.get("p50")) > 0.2:
            severity = "high" if val < _safe_float(q.get("p10")) * .75 else "medium"
            score += 9 if severity == "high" else 6
            findings.append({
                "type": "skill_frequency_low",
                "severity": severity,
                "metric": f"{name} casts/min",
                "current": round(val, 3),
                "personal_p10": q.get("p10"),
                "personal_p50": q.get("p50"),
                "delta_vs_p50_pct": round(delta, 1),
                "evidence": f"{name} 本场施放频率低于该角色个人历史 P10。",
            })
        elif bucket == "above_p90" and _safe_float(q.get("p50")) > 0.2:
            findings.append({
                "type": "skill_frequency_high",
                "severity": "info",
                "metric": f"{name} casts/min",
                "current": round(val, 3),
                "personal_p90": q.get("p90"),
                "personal_p50": q.get("p50"),
                "delta_vs_p50_pct": round(delta, 1),
                "evidence": f"{name} 本场施放频率高于该角色个人历史 P90。",
            })

    # Compare fight-specific battle-window Buff coverage and within-pull cadence against
    # the closest personal history. These are descriptive outliers, not rotation rules.
    current_eff = signature.get("efficiency") or {}
    current_buffs = {str(x.get("spell_name") or x.get("spell_id")): x for x in (current_eff.get("buff_uptime") or [])}
    for base in (profile.get("buffs") or [])[:16]:
        name = str(base.get("spell_name") or "")
        cur = current_buffs.get(name)
        q = base.get("combat_uptime_pct") or {}
        if not cur or not q:
            continue
        val = _safe_float(cur.get("combat_uptime_pct") if cur.get("combat_uptime_pct") is not None else cur.get("uptime_pct"))
        p10 = _safe_float(q.get("p10")); p50 = _safe_float(q.get("p50"))
        if p50 >= 8 and val + 5.0 < p10:
            score += 5
            findings.append({
                "type": "buff_combat_uptime_low", "severity": "medium",
                "metric": f"{name} 战斗内覆盖", "current": round(val, 2),
                "personal_p10": q.get("p10"), "personal_p50": q.get("p50"),
                "evidence": f"{name} 本场战斗内覆盖明显低于这个角色自己同类历史；跑图和波次间隔不计入该覆盖口径。",
            })

    current_cad = {str(x.get("spell_name") or x.get("spell_id")): x for x in (current_eff.get("cast_cadence") or [])}
    for base in (profile.get("cadence") or [])[:16]:
        name = str(base.get("spell_name") or "")
        cur = current_cad.get(name)
        q = base.get("combat_interval_median_s") or {}
        if not cur or not q:
            continue
        val = _safe_float(cur.get("combat_interval_median_s")); p90 = _safe_float(q.get("p90"))
        if p90 > 0 and val > p90 + 0.5:
            findings.append({
                "type": "within_pull_cadence_slow", "severity": "medium",
                "metric": f"{name} 同一波内使用节奏", "current": round(val, 3),
                "personal_p50": q.get("p50"), "personal_p90": q.get("p90"),
                "evidence": f"{name} 本场在同一波战斗内的典型使用间隔比本人历史少见范围更长；需要结合机制和目标状态判断原因。",
            })

    resp = signature.get("responsiveness") or {}
    resp_profile = profile.get("responsiveness") or {}
    resp_checks = [
        ("score", "响应异常分", 12.0),
        ("gap_p95_s", "施法间隔 P95", 8.0),
        ("max_gap_s", "最大施法空档", 8.0),
        ("severe_gap_count", "严重长空档次数", 10.0),
        ("isolated_team_active_gap_count", "队友持续施法时的单人空档次数", 12.0),
        ("isolated_team_active_max_gap_s", "队友持续施法时的最长单人空档", 14.0),
    ]
    for metric, label, weight in resp_checks:
        q = resp_profile.get(metric) or {}
        if not q:
            continue
        observed = _resp_observed(resp, metric)
        if observed is None:
            continue
        val = observed
        p90 = _safe_float(q.get("p90"))
        # Avoid calling a tiny numerical difference an anomaly.
        margin = 1.0 if metric.endswith("_s") else 1.0
        if val > p90 + margin and val > 0:
            score += weight
            findings.append({
                "type": "responsiveness_outlier",
                "severity": "high" if metric in {"score", "max_gap_s", "severe_gap_count"} else "medium",
                "metric": label,
                "current": round(val, 3),
                "personal_p50": q.get("p50"),
                "personal_p90": q.get("p90"),
                "evidence": f"{label} 高于该角色个人历史 P90；这证明行为异常，但不是网络故障的单独证据。",
            })

    eff = signature.get("efficiency") or {}
    eff_profile = profile.get("efficiency") or {}
    for section, key, profile_key, label, weight in (
        ("resource_efficiency", "high_resource_pct", "high_resource_pct", "高资源驻留", 5.0),
        ("resource_efficiency", "overcap_pct", "overcap_pct", "资源溢出率", 6.0),
    ):
        q = eff_profile.get(profile_key) or {}
        if not q:
            continue
        observed = _eff_observed(eff, section, key)
        if observed is None:
            continue
        val = observed
        if val > _safe_float(q.get("p90")) + 2.0 and val > 0:
            score += weight
            findings.append({
                "type": "efficiency_outlier",
                "severity": "medium",
                "metric": label,
                "current": round(val, 3),
                "personal_p50": q.get("p50"),
                "personal_p90": q.get("p90"),
                "evidence": f"{label} 高于该角色个人历史 P90。",
            })

    findings.sort(key=lambda x: ({"high": 3, "medium": 2, "info": 1}.get(str(x.get("severity")), 0), abs(_safe_float(x.get("delta_vs_p50_pct")))), reverse=True)
    anomaly_score = int(round(min(100.0, score)))
    if anomaly_score >= 55:
        label = "明显偏离个人正常行为"
    elif anomaly_score >= 30:
        label = "存在多项个人异常"
    elif anomaly_score >= 12:
        label = "存在少量个人异常"
    else:
        label = "整体接近个人历史范围"
    return {
        "sample_count": int(profile.get("sample_count") or 0),
        "confidence": str(profile.get("confidence") or "low"),
        "anomaly_score": anomaly_score,
        "label": label,
        "findings": findings[:20],
        "guardrail": "个人异常表示本场偏离该玩家自己的历史分布；不能单独证明网络、掉帧或操作失误。",
    }


def personal_context_for_ai(profile: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    return {
        "personal_baseline": {
            "sample_count": profile.get("sample_count", 0),
            "confidence": profile.get("confidence", "low"),
            "overview": profile.get("overview") or {},
            "responsiveness": profile.get("responsiveness") or {},
            "efficiency": profile.get("efficiency") or {},
            "skills": (profile.get("skills") or [])[:20],
            "buffs": (profile.get("buffs") or [])[:18],
            "cadence": (profile.get("cadence") or [])[:18],
            "buff_burst_overlap": (profile.get("buff_burst_overlap") or [])[:14],
            "limitations": profile.get("limitations") or [],
        },
        "current_vs_personal": comparison,
    }


@dataclass
class PersonalSyncResult:
    requested: int
    imported: int
    skipped_existing: int
    reports_scanned: int
    fights_seen: int
    failed: int
    errors: list[str]
    sample_ids: list[int]
    rate_limit: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "imported": self.imported,
            "skipped_existing": self.skipped_existing,
            "reports_scanned": self.reports_scanned,
            "fights_seen": self.fights_seen,
            "failed": self.failed,
            "errors": self.errors,
            "sample_ids": self.sample_ids,
            "rate_limit": self.rate_limit,
        }



def save_personal_model_summary(
    character_key: str,
    sample_count: int,
    summary_markdown: str,
    *,
    model: str = "",
    scope: str = "",
    path: Path | None = None,
) -> int:
    init_personal_baseline_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """INSERT INTO personal_model_summaries(created_at,character_key,sample_count,model,scope,summary_markdown)
               VALUES(?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), character_key, int(sample_count), model, scope, summary_markdown),
        )
        return int(cur.lastrowid)


def latest_personal_model_summary(character_key: str, path: Path | None = None) -> dict[str, Any]:
    init_personal_baseline_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM personal_model_summaries WHERE character_key=? ORDER BY id DESC LIMIT 1",
            (character_key,),
        ).fetchone()
    return dict(row) if row else {}


def sync_timed_wcl_personal_baseline(
    client,
    character_profile: dict[str, Any],
    timed_runs: list[dict[str, Any]],
    *,
    max_new_fights: int = 10,
    path: Path | None = None,
) -> PersonalSyncResult:
    """Incremental fast path using the timed-run list already scanned by the desktop UI.

    Known report/fight/source triples are skipped before any report/event API call. New
    runs fetch their independent event streams concurrently and reuse one report payload
    per report code. This is substantially faster and cheaper than rescanning every Report.
    """
    identity = character_identity(character_profile)
    requested = max(1, min(50, int(max_new_fights)))
    imported = skipped = reports_scanned = fights_seen = failed = 0
    errors: list[str] = []
    sample_ids: list[int] = []
    report_cache: dict[str, dict[str, Any]] = {}
    seen_reports: set[str] = set()

    for rec in timed_runs:
        if imported >= requested:
            break
        code = str(rec.get("report_code") or "")
        fid = _safe_int(rec.get("fight_id"))
        sid = _safe_int(rec.get("source_id"))
        if not code or not fid or not sid:
            continue
        fights_seen += 1
        if has_personal_sample(identity["character_key"], code, fid, sid, min_schema_version=5, path=path):
            skipped += 1
            continue
        try:
            if code not in report_cache:
                raw = client.report_with_talents(code)
                report = (((raw or {}).get("reportData") or {}).get("report") or {})
                if not report:
                    raise RuntimeError("Report 不可用")
                report_cache[code] = report
                if code not in seen_reports:
                    reports_scanned += 1
                    seen_reports.add(code)
            report = report_cache[code]
            fight = next((f for f in report.get("fights") or [] if _safe_int(f.get("id")) == fid), None)
            if not fight or not is_timed_keystone(fight):
                skipped += 1
                continue
            spec_name, player_ilvl = spec_and_item_level_for_fight(fight, sid)
            actor_name = str(rec.get("player_name") or identity.get("character_name") or "")
            class_name = str(rec.get("class_name") or "")
            if not class_name:
                rsid, actor_name2, class_name2 = resolve_report_actor(report, character_name=actor_name, source_id=sid)
                if rsid:
                    sid = rsid
                actor_name = actor_name2 or actor_name
                class_name = class_name2 or class_name
            bundle = fetch_event_bundle(client, code, fid, {
                "casts": {"data_type": "Casts", "source_id": sid, "limit": 10000, "max_pages": 10},
                "damage": {"data_type": "DamageDone", "source_id": sid, "limit": 10000, "max_pages": 14},
                "buffs": {"data_type": "Buffs", "target_id": sid, "limit": 10000, "max_pages": 8},
                "resources": {"data_type": "Resources", "source_id": sid, "include_resources": True, "limit": 10000, "max_pages": 8},
                "deaths": {"data_type": "Deaths", "target_id": sid, "limit": 10000, "max_pages": 4},
            }, max_workers=4)
            casts = bundle.get("casts") or []
            damage = bundle.get("damage") or []
            if not casts and not damage:
                raise RuntimeError("没有读取到 Cast/Damage 事件")
            sig = build_wcl_behavior_signature(
                report=report, fight=fight, player_name=actor_name, source_id=sid,
                class_name=class_name, spec_name=spec_name,
                casts=casts, damage=damage, buffs=bundle.get("buffs") or [],
                resources=bundle.get("resources") or [], deaths=bundle.get("deaths") or [], ranking={},
            )
            sig.setdefault("context", {})["player_item_level"] = player_ilvl
            sample_id = save_personal_sample(identity, sig, metadata={"sync": "timed_runs_fast"}, path=path)
            save_knowledge_sample(sig, source="personal_wcl", sample_role="auto", metadata={
                "report_code": code, "fight_id": fid, "source_id": sid,
                "character_key": identity["character_key"], "timed_success": True,
            }, path=path)
            sample_ids.append(sample_id)
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code} fight {fid}: {exc}")

    try:
        rate = client.rate_limit()
    except Exception:
        rate = {}
    return PersonalSyncResult(
        requested=requested, imported=imported, skipped_existing=skipped,
        reports_scanned=reports_scanned, fights_seen=fights_seen, failed=failed,
        errors=errors[:20], sample_ids=sample_ids, rate_limit=rate,
    )


def sync_recent_wcl_personal_baseline(
    client,
    character_profile: dict[str, Any],
    reports: list[dict[str, Any]],
    *,
    max_new_fights: int = 10,
    include_non_keystone: bool = False,
    timed_only: bool = True,
    path: Path | None = None,
) -> PersonalSyncResult:
    """Fetch recent fights for one WCL character and build a personal baseline.

    This deliberately uses only four event streams (Casts/Damage/Buffs/Resources) to
    keep API cost lower than a full targeted diagnosis. A selected fight still fetches
    the richer evidence set in ``build_targeted_wcl_payload``.
    """
    identity = character_identity(character_profile)
    server = character_profile.get("server") or {}
    char_name = str(character_profile.get("name") or "")
    server_slug = str(server.get("slug") or server.get("name") or "")
    requested = max(1, min(50, int(max_new_fights)))
    imported = skipped = reports_scanned = fights_seen = failed = 0
    errors: list[str] = []
    sample_ids: list[int] = []

    for report_stub in reports:
        if imported >= requested:
            break
        code = str(report_stub.get("code") or "")
        if not code:
            continue
        try:
            raw = client.report_with_talents(code)
            report = (((raw or {}).get("reportData") or {}).get("report") or {})
            if not report:
                continue
            reports_scanned += 1
            sid, player_name, class_name = resolve_report_actor(
                report, character_name=char_name, server_slug=server_slug
            )
            if not sid:
                continue
            fights = fights_for_source(report, sid)
            # Whole-key/M+ fights first, then newest/longest. Non-keystone fights are
            # opt-in because a raid/boss baseline should not pollute a Mythic+ model.
            candidates = []
            for fight in fights:
                k = _safe_int(fight.get("keystoneLevel"))
                if k <= 0 and not include_non_keystone:
                    continue
                if k > 0 and timed_only and not is_timed_keystone(fight):
                    continue
                duration = _safe_float(fight.get("endTime")) - _safe_float(fight.get("startTime"))
                if duration <= 10_000:  # ignore tiny fragments
                    continue
                candidates.append(fight)
            candidates.sort(key=lambda f: (_safe_int(f.get("keystoneLevel")) > 0, _safe_float(f.get("endTime")) - _safe_float(f.get("startTime"))), reverse=True)

            for fight in candidates:
                if imported >= requested:
                    break
                fid = _safe_int(fight.get("id"))
                if not fid:
                    continue
                fights_seen += 1
                if has_personal_sample(identity["character_key"], code, fid, sid, min_schema_version=5, path=path):
                    skipped += 1
                    continue
                try:
                    spec_name, player_ilvl = spec_and_item_level_for_fight(fight, sid)
                    bundle = fetch_event_bundle(client, code, fid, {
                        "casts": {"data_type": "Casts", "source_id": sid, "limit": 10000, "max_pages": 10},
                        "damage": {"data_type": "DamageDone", "source_id": sid, "limit": 10000, "max_pages": 14},
                        "buffs": {"data_type": "Buffs", "target_id": sid, "limit": 10000, "max_pages": 8},
                        "resources": {"data_type": "Resources", "source_id": sid, "include_resources": True, "limit": 10000, "max_pages": 8},
                        "deaths": {"data_type": "Deaths", "target_id": sid, "limit": 10000, "max_pages": 4},
                    }, max_workers=4)
                    casts = bundle.get("casts") or []
                    damage = bundle.get("damage") or []
                    buffs = bundle.get("buffs") or []
                    resources = bundle.get("resources") or []
                    deaths = bundle.get("deaths") or []
                    sig = build_wcl_behavior_signature(
                        report=report, fight=fight, player_name=player_name, source_id=sid,
                        class_name=class_name, spec_name=spec_name,
                        casts=casts, damage=damage, buffs=buffs, resources=resources, deaths=deaths, ranking={},
                    )
                    sig.setdefault("context", {})["player_item_level"] = player_ilvl
                    sample_id = save_personal_sample(identity, sig, metadata={"sync": "recent_wcl"}, path=path)
                    # Also enrich the general class cohort. Stable WCL fingerprinting makes this idempotent.
                    save_knowledge_sample(sig, source="personal_wcl", sample_role="auto", metadata={
                        "report_code": code, "fight_id": fid, "source_id": sid,
                        "character_key": identity["character_key"],
                    }, path=path)
                    sample_ids.append(sample_id)
                    imported += 1
                except Exception as exc:  # keep the batch moving if one fight is malformed/rate-limited
                    failed += 1
                    errors.append(f"{code} fight {fid}: {exc}")
        except Exception as exc:
            failed += 1
            errors.append(f"{code}: {exc}")

    try:
        rate = client.rate_limit()
    except Exception:
        rate = {}
    return PersonalSyncResult(
        requested=requested, imported=imported, skipped_existing=skipped,
        reports_scanned=reports_scanned, fights_seen=fights_seen, failed=failed,
        errors=errors[:20], sample_ids=sample_ids, rate_limit=rate,
    )


def best_personal_profile_for_signature(
    character_key: str,
    signature: dict[str, Any],
    *,
    min_condition_samples: int = 5,
    path: Path | None = None,
) -> dict[str, Any]:
    """Choose the closest available personal baseline without pretending sparse data is precise."""
    ctx = signature.get("context") or {}
    spec = str(ctx.get("spec_name") or "")
    patch = str(ctx.get("patch_scope") or "")
    dungeon = str(ctx.get("dungeon") or "")
    key = _safe_int(ctx.get("key_level"))
    tiers = [
        ("同专精 + 同版本 + 同副本 + 相近层数(±2)", dict(spec_name=spec, patch_scope=patch, dungeon=dungeon, key_level=key, key_tolerance=2)),
        ("同专精 + 同版本 + 同副本", dict(spec_name=spec, patch_scope=patch, dungeon=dungeon, key_level=0, key_tolerance=99)),
        ("同专精 + 同版本", dict(spec_name=spec, patch_scope=patch, dungeon="", key_level=0, key_tolerance=99)),
        ("同专精历史", dict(spec_name=spec, patch_scope="", dungeon="", key_level=0, key_tolerance=99)),
        ("该角色全部历史", dict(spec_name="", patch_scope="", dungeon="", key_level=0, key_tolerance=99)),
    ]
    chosen_rows: list[dict[str, Any]] = []
    chosen_scope = tiers[-1][0]
    for idx, (scope, kwargs) in enumerate(tiers):
        # Do not accidentally filter everything on an empty context dimension.
        clean = dict(kwargs)
        if not spec:
            clean["spec_name"] = ""
        if not patch:
            clean["patch_scope"] = ""
        if not dungeon:
            clean["dungeon"] = ""
        if not key:
            clean["key_level"] = 0
            clean["key_tolerance"] = 99
        rows = list_personal_samples(character_key, limit=250, path=path, **clean)
        chosen_rows = rows
        chosen_scope = scope
        if len(rows) >= min_condition_samples or idx == len(tiers) - 1:
            break
    profile = build_personal_profile(chosen_rows)
    profile["scope"] = chosen_scope
    profile["scope_sample_count"] = len(chosen_rows)
    profile["requested_context"] = {
        "spec_name": spec, "patch_scope": patch, "dungeon": dungeon, "key_level": key,
    }
    return profile
