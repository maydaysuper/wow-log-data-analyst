from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .analyzer import player_overview, skill_breakdown
from .advanced_timeline import advanced_timeline_payload
from .combat_efficiency import deep_efficiency_payload
from .learning import default_db_path
from .responsiveness import responsiveness_summary
from .target_focus import aggregate_target_knowledge, compare_target_profiles


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


def init_knowledge_db(path: Path | None = None) -> Path:
    db = Path(path or default_db_path())
    with _connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                run_label TEXT,
                player TEXT,
                dungeon TEXT,
                key_level INTEGER,
                class_name TEXT,
                spec_name TEXT,
                source TEXT DEFAULT 'local_log',
                sample_role TEXT DEFAULT 'auto',
                performance_value REAL DEFAULT 0,
                signature_json TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_samples_context
                ON knowledge_samples(class_name, spec_name, dungeon, key_level, sample_role);

            CREATE TABLE IF NOT EXISTS knowledge_playbooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                class_name TEXT,
                spec_name TEXT,
                dungeon_scope TEXT DEFAULT '',
                key_scope TEXT DEFAULT '',
                patch_scope TEXT DEFAULT '',
                model TEXT DEFAULT '',
                sample_count INTEGER DEFAULT 0,
                evidence_json TEXT NOT NULL,
                playbook_json TEXT NOT NULL,
                notes TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_playbooks_context
                ON knowledge_playbooks(class_name, spec_name, dungeon_scope, id);
            """
        )
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(knowledge_playbooks)").fetchall()}
        if "patch_scope" not in cols:
            conn.execute("ALTER TABLE knowledge_playbooks ADD COLUMN patch_scope TEXT DEFAULT ''")
    return db


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _clean_number(v: Any) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def behavior_signature(df: pd.DataFrame, player: str, meta: dict[str, Any] | None = None, segment: str | None = None) -> dict[str, Any]:
    """Build a bounded, reusable behavior signature from one observed run.

    The signature intentionally stores structured summaries rather than the raw combat log.
    It is empirical evidence, not a hard-coded class rotation definition.
    """
    meta = dict(meta or {})
    seg = None if not segment or segment == "All" else segment
    overview = player_overview(df, player, seg)
    skills = skill_breakdown(df, player, seg)
    efficiency = deep_efficiency_payload(df, player, seg)
    response = responsiveness_summary(df, player, seg)
    timeline = advanced_timeline_payload(df, player, seg)

    skill_rows = []
    if not skills.empty:
        for row in skills.head(40).to_dict("records"):
            skill_rows.append({
                "spell_id": row.get("spell_id"),
                "spell_name": row.get("spell_name"),
                "damage_pct": round(_clean_number(row.get("damage_pct")), 4),
                "casts_per_min": round(_clean_number(row.get("casts_per_min")), 4),
                "hits_per_cast": round(_clean_number(row.get("hits_per_cast")), 4),
                "crit_pct": round(_clean_number(row.get("crit_pct")), 4),
                "avg_hit": round(_clean_number(row.get("avg_hit")), 2),
                "damage_per_cast": round(_clean_number(row.get("damage_per_cast")), 2),
            })

    # Keep only compact evidence used by the learning engine.
    compact_eff = {
        "resource_efficiency": efficiency.get("resource_efficiency") or {},
        "target_switching": efficiency.get("target_switching") or {},
        "pull_downtime": efficiency.get("pull_downtime") or {},
        "buff_uptime": (efficiency.get("buff_uptime") or [])[:25],
        "cast_cadence": (efficiency.get("cast_cadence") or [])[:30],
        "death_recovery": (efficiency.get("death_recovery") or [])[:12],
        "buff_burst_overlap": (efficiency.get("buff_burst_overlap") or [])[:16],
    }
    compact_timeline = {
        "pulls": (timeline.get("pulls") or [])[:40],
        "player_pulls": (timeline.get("player_pulls") or [])[:40],
        "burst_windows": (timeline.get("burst_windows") or [])[:10],
        "death_contexts": (timeline.get("death_contexts") or [])[:12],
    }
    return {
        "schema_version": 1,
        "context": {
            "run_label": meta.get("label") or meta.get("run_label") or "",
            "player": player,
            "dungeon": meta.get("dungeon") or "",
            "key_level": int(meta.get("key_level") or 0),
            "class_name": meta.get("class_name") or "",
            "spec_name": meta.get("spec_name") or "",
            "segment": segment or "All",
        },
        "overview": overview,
        "skills": skill_rows,
        "efficiency": compact_eff,
        "responsiveness": response,
        "timeline": compact_timeline,
        "limitations": [
            "这是从实际日志提取的行为特征，不等同于职业理论最优循环。",
            "DPS 会受路线、怪量、钥匙层数、装备、队友和战斗时长影响；知识引擎会尽量按上下文分层。",
        ],
    }


def signature_fingerprint(signature: dict[str, Any]) -> str:
    # WCL samples have a stable natural identity. Ranking percentile and fetched_at can
    # change on later syncs, so they must not create duplicate learned samples.
    prov = signature.get("provenance") or {}
    if prov.get("report_code") and prov.get("fight_id") and prov.get("source_id"):
        natural = f"wcl:{prov.get('report_code')}:{int(prov.get('fight_id') or 0)}:{int(prov.get('source_id') or 0)}"
        return hashlib.sha256(natural.encode("utf-8")).hexdigest()
    body = _dumps(signature)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def save_knowledge_sample(
    signature: dict[str, Any],
    source: str = "local_log",
    sample_role: str = "auto",
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> int:
    init_knowledge_db(path)
    ctx = signature.get("context") or {}
    overview = signature.get("overview") or {}
    fp = signature_fingerprint(signature)
    role = sample_role if sample_role in {"auto", "reference", "normal", "exclude"} else "auto"
    with _connect(path) as conn:
        existing = conn.execute("SELECT id FROM knowledge_samples WHERE fingerprint=?", (fp,)).fetchone()
        values = (
            datetime.now(timezone.utc).isoformat(), fp,
            str(ctx.get("run_label") or ""), str(ctx.get("player") or ""),
            str(ctx.get("dungeon") or ""), int(ctx.get("key_level") or 0),
            str(ctx.get("class_name") or ""), str(ctx.get("spec_name") or ""),
            source, role, _clean_number(overview.get("dps")),
            _dumps(signature), _dumps(metadata or {}),
        )
        # Atomic UPSERT makes repeated/background WCL learning idempotent.
        conn.execute(
            """
            INSERT INTO knowledge_samples(
                created_at, fingerprint, run_label, player, dungeon, key_level,
                class_name, spec_name, source, sample_role, performance_value,
                signature_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                run_label=excluded.run_label, player=excluded.player, dungeon=excluded.dungeon,
                key_level=excluded.key_level, class_name=excluded.class_name, spec_name=excluded.spec_name,
                source=excluded.source, sample_role=excluded.sample_role, performance_value=excluded.performance_value,
                signature_json=excluded.signature_json, metadata_json=excluded.metadata_json
            """,
            values,
        )
        row = conn.execute("SELECT id FROM knowledge_samples WHERE fingerprint=?", (fp,)).fetchone()
        return int(row["id"])




def has_wcl_knowledge_sample(
    report_code: str, fight_id: int, source_id: int, path: Path | None = None,
    *, min_schema_version: int = 0,
) -> bool:
    """Fast natural-identity check with optional feature-schema freshness."""
    if not report_code or not fight_id or not source_id:
        return False
    natural = f"wcl:{report_code}:{int(fight_id)}:{int(source_id)}"
    fp = hashlib.sha256(natural.encode("utf-8")).hexdigest()
    init_knowledge_db(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT signature_json FROM knowledge_samples WHERE fingerprint=? LIMIT 1", (fp,)).fetchone()
    if not row:
        return False
    if int(min_schema_version or 0) <= 0:
        return True
    try:
        sig = json.loads(row["signature_json"] or "{}")
        return int(sig.get("schema_version") or 0) >= int(min_schema_version)
    except Exception:
        return False
def update_sample_role(sample_id: int, sample_role: str, path: Path | None = None) -> None:
    init_knowledge_db(path)
    role = sample_role if sample_role in {"auto", "reference", "normal", "exclude"} else "auto"
    with _connect(path) as conn:
        conn.execute("UPDATE knowledge_samples SET sample_role=? WHERE id=?", (role, int(sample_id)))


def delete_knowledge_sample(sample_id: int, path: Path | None = None) -> None:
    init_knowledge_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM knowledge_samples WHERE id=?", (int(sample_id),))


def list_knowledge_samples(limit: int = 500, path: Path | None = None) -> list[dict[str, Any]]:
    init_knowledge_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM knowledge_samples ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    return [dict(r) for r in rows]


def _load_signature(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("signature_json") or "{}")
    except Exception:
        return {}


def _match_row(row: dict[str, Any], class_name: str, spec_name: str, dungeon: str = "", key_level: int = 0, key_tolerance: int = 1) -> bool:
    if str(row.get("sample_role") or "") == "exclude":
        return False
    if class_name and str(row.get("class_name") or "").lower() != class_name.lower():
        return False
    if spec_name and str(row.get("spec_name") or "").lower() != spec_name.lower():
        return False
    if dungeon and str(row.get("dungeon") or "").lower() != dungeon.lower():
        return False
    if key_level:
        k = int(row.get("key_level") or 0)
        if not k or abs(k - int(key_level)) > max(0, int(key_tolerance)):
            return False
    return True


def select_knowledge_samples(
    class_name: str,
    spec_name: str,
    dungeon: str = "",
    key_level: int = 0,
    key_tolerance: int = 1,
    limit: int = 250,
    path: Path | None = None,
    patch_scope: str = "",
) -> list[dict[str, Any]]:
    rows = list_knowledge_samples(max(limit * 3, 500), path)
    out = [r for r in rows if _match_row(r, class_name, spec_name, dungeon, key_level, key_tolerance)]
    wanted_patch = str(patch_scope or "").strip().lower()
    if wanted_patch:
        filtered = []
        for r in out:
            sig = _load_signature(r)
            ctx = sig.get("context") or {}
            prov = sig.get("provenance") or {}
            observed = str(ctx.get("patch_scope") or prov.get("patch_scope") or ctx.get("game_version") or prov.get("game_version") or "").lower()
            if observed and (observed == wanted_patch or observed.startswith(wanted_patch) or wanted_patch.startswith(observed)):
                filtered.append(r)
        out = filtered
    return out[:limit]


def _q(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values, dtype=float).quantile(q))


def _skill_maps(samples: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    bucket: dict[str, list[dict[str, Any]]] = {}
    for row in samples:
        sig = _load_signature(row)
        for s in sig.get("skills") or []:
            name = str(s.get("spell_name") or s.get("spell_id") or "Unknown")
            bucket.setdefault(name, []).append({**s, "sample_id": row.get("id"), "performance": _clean_number(row.get("performance_value")), "role": row.get("sample_role")})
    return bucket


def empirical_cohort_profile(samples: list[dict[str, Any]], top_n_skills: int = 30) -> dict[str, Any]:
    """Summarize observed class behavior without pretending correlation is a rotation rule."""
    usable = [r for r in samples if str(r.get("sample_role") or "") != "exclude"]
    if not usable:
        return {"sample_count": 0, "skills": [], "empirical_patterns": [], "limitations": ["没有可用样本。"]}

    dps_values = [_clean_number(r.get("performance_value")) for r in usable if _clean_number(r.get("performance_value")) > 0]
    reference_ids = {int(r["id"]) for r in usable if str(r.get("sample_role") or "") == "reference"}
    if not reference_ids and len(dps_values) >= 4:
        threshold = _q(dps_values, 0.75)
        reference_ids = {int(r["id"]) for r in usable if _clean_number(r.get("performance_value")) >= threshold}

    bucket = _skill_maps(usable)
    skill_rows: list[dict[str, Any]] = []
    patterns: list[dict[str, Any]] = []
    metrics = ["damage_pct", "casts_per_min", "hits_per_cast", "crit_pct", "damage_per_cast"]
    for name, rows in bucket.items():
        # Require recurrence across samples so a one-off proc does not become a "rotation rule".
        if len(rows) < max(2, min(4, len(usable))):
            continue
        item: dict[str, Any] = {"spell_name": name, "sample_count": len(rows)}
        for m in metrics:
            vals = [_clean_number(r.get(m)) for r in rows]
            item[f"{m}_p25"] = round(_q(vals, 0.25), 4)
            item[f"{m}_p50"] = round(_q(vals, 0.50), 4)
            item[f"{m}_p75"] = round(_q(vals, 0.75), 4)
            item[f"{m}_p90"] = round(_q(vals, 0.90), 4)

        ref_rows = [r for r in rows if int(r.get("sample_id") or 0) in reference_ids]
        base_rows = [r for r in rows if int(r.get("sample_id") or 0) not in reference_ids]
        if len(ref_rows) >= 2 and len(base_rows) >= 2:
            for m in ("casts_per_min", "damage_pct", "hits_per_cast"):
                rv = [_clean_number(r.get(m)) for r in ref_rows]
                bv = [_clean_number(r.get(m)) for r in base_rows]
                r50, b50 = _q(rv, 0.5), _q(bv, 0.5)
                denom = abs(b50) if abs(b50) > 1e-9 else 1.0
                delta_pct = (r50 - b50) / denom * 100.0
                if abs(delta_pct) >= 8.0:
                    patterns.append({
                        "type": "reference_group_difference",
                        "spell_name": name,
                        "metric": m,
                        "reference_p50": round(r50, 4),
                        "other_p50": round(b50, 4),
                        "delta_pct": round(delta_pct, 2),
                        "evidence_samples": len(ref_rows) + len(base_rows),
                        "confidence": "medium" if len(ref_rows) + len(base_rows) < 12 else "high",
                        "interpretation_guardrail": "这是参考组与其他样本的相关性差异，不单独证明因果或理论最优。",
                    })
        skill_rows.append(item)

    skill_rows.sort(key=lambda r: (r.get("damage_pct_p50", 0), r.get("casts_per_min_p50", 0)), reverse=True)
    patterns.sort(key=lambda r: (abs(_clean_number(r.get("delta_pct"))), int(r.get("evidence_samples") or 0)), reverse=True)

    # Aggregate non-skill behavior features.
    efficiency_rows = []
    response_scores = []
    for row in usable:
        sig = _load_signature(row)
        eff = sig.get("efficiency") or {}
        res = eff.get("resource_efficiency") or {}
        switch = eff.get("target_switching") or {}
        gap = eff.get("pull_downtime") or {}
        efficiency_rows.append({
            "avg_resource_pct": _clean_number(res.get("avg_resource_pct")),
            "high_resource_pct": _clean_number(res.get("time_at_or_above_90_pct")),
            "overcap_pct": _clean_number(res.get("overcap_pct_of_generated")),
            "switches_per_min": _clean_number(switch.get("switches_per_min")),
            "inter_pull_downtime_s": _clean_number(gap.get("inter_pull_downtime_s")),
        })
        response_scores.append(_clean_number((sig.get("responsiveness") or {}).get("score")))

    aggregate: dict[str, Any] = {}
    if efficiency_rows:
        for m in efficiency_rows[0]:
            vals = [r[m] for r in efficiency_rows]
            aggregate[m] = {"p25": round(_q(vals, .25), 3), "p50": round(_q(vals, .5), 3), "p75": round(_q(vals, .75), 3), "p90": round(_q(vals, .9), 3)}
    if response_scores:
        aggregate["responsiveness_score"] = {"p25": round(_q(response_scores, .25), 2), "p50": round(_q(response_scores, .5), 2), "p75": round(_q(response_scores, .75), 2), "p90": round(_q(response_scores, .9), 2)}

    # Aggregate battle-window Buff coverage, within-pull cadence, and Buff/burst
    # alignment across the same cohort. These are the dimensions the coach needs to
    # distinguish "按得少" from "按得不在正确窗口".
    min_recurrence = max(2, min(4, len(usable)))
    buff_bucket: dict[str, list[dict[str, Any]]] = {}
    cadence_bucket: dict[str, list[dict[str, Any]]] = {}
    overlap_bucket: dict[str, list[dict[str, Any]]] = {}
    for r in usable:
        sig = _load_signature(r)
        eff = sig.get("efficiency") or {}
        for item in eff.get("buff_uptime") or []:
            name = str(item.get("spell_name") or item.get("spell_id") or "Unknown")
            buff_bucket.setdefault(name, []).append(item)
        for item in eff.get("cast_cadence") or []:
            name = str(item.get("spell_name") or item.get("spell_id") or "Unknown")
            cadence_bucket.setdefault(name, []).append(item)
        for item in eff.get("buff_burst_overlap") or []:
            name = str(item.get("spell_name") or item.get("spell_id") or "Unknown")
            overlap_bucket.setdefault(name, []).append(item)

    def metric_dist(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
        vals = [_clean_number(x.get(field)) for x in rows if x.get(field) is not None]
        if not vals:
            return {}
        return {
            "p25": round(_q(vals, .25), 3), "p50": round(_q(vals, .5), 3),
            "p75": round(_q(vals, .75), 3), "p90": round(_q(vals, .9), 3),
            "sample_count": len(vals),
        }

    buff_profiles = []
    for name, rows in buff_bucket.items():
        if len(rows) < min_recurrence:
            continue
        combat_vals = []
        for x in rows:
            v = x.get("combat_uptime_pct") if x.get("combat_uptime_pct") is not None else x.get("uptime_pct")
            if v is not None:
                combat_vals.append({"v": v})
        buff_profiles.append({
            "spell_name": name, "sample_count": len(rows),
            "combat_uptime_pct": metric_dist(combat_vals, "v"),
            "avg_window_s": metric_dist(rows, "avg_window_s"),
            "applications": metric_dist(rows, "applications"),
        })
    buff_profiles.sort(key=lambda x: _clean_number((x.get("combat_uptime_pct") or {}).get("p50")), reverse=True)

    cadence_profiles = []
    for name, rows in cadence_bucket.items():
        if len(rows) < min_recurrence:
            continue
        cadence_profiles.append({
            "spell_name": name, "sample_count": len(rows),
            "total_casts": metric_dist(rows, "total_casts"),
            "combat_interval_median_s": metric_dist(rows, "combat_interval_median_s"),
            "combat_interval_p90_s": metric_dist(rows, "combat_interval_p90_s"),
            "pulls_with_cast": metric_dist(rows, "pulls_with_cast"),
        })
    cadence_profiles.sort(key=lambda x: _clean_number((x.get("total_casts") or {}).get("p50")), reverse=True)

    overlap_profiles = []
    for name, rows in overlap_bucket.items():
        if len(rows) < min_recurrence:
            continue
        overlap_profiles.append({
            "spell_name": name, "sample_count": len(rows),
            "burst_overlap_pct": metric_dist(rows, "burst_overlap_pct"),
            "burst_windows_covered": metric_dist(rows, "burst_windows_covered"),
        })
    overlap_profiles.sort(key=lambda x: _clean_number((x.get("burst_overlap_pct") or {}).get("p50")), reverse=True)

    target_focus_profile = aggregate_target_knowledge([_load_signature(r) for r in usable], min_samples=min_recurrence, limit=40)

    contexts = sorted({(str(r.get("dungeon") or ""), int(r.get("key_level") or 0)) for r in usable})

    # Preserve environment/context distributions so the AI can see obvious confounders
    # instead of learning "higher DPS == better rotation" across incomparable runs.
    game_versions: dict[str, int] = {}
    patch_scopes: dict[str, int] = {}
    context_metric_values: dict[str, list[float]] = {
        "average_item_level": [], "key_level": [], "pull_count": [],
        "keystone_time_ms": [], "fight_duration_s": [],
    }
    ref_context: dict[str, list[float]] = {k: [] for k in context_metric_values}
    other_context: dict[str, list[float]] = {k: [] for k in context_metric_values}
    for r in usable:
        sig = _load_signature(r)
        ctx = sig.get("context") or {}
        ver = str(ctx.get("game_version") or (sig.get("provenance") or {}).get("game_version") or "").strip()
        if ver:
            game_versions[ver] = game_versions.get(ver, 0) + 1
        patch = str(ctx.get("patch_scope") or (sig.get("provenance") or {}).get("patch_scope") or "").strip()
        if patch:
            patch_scopes[patch] = patch_scopes.get(patch, 0) + 1
        vals = {
            "average_item_level": _clean_number(ctx.get("average_item_level")),
            "key_level": _clean_number(ctx.get("key_level") or r.get("key_level")),
            "pull_count": _clean_number(ctx.get("pull_count")),
            "keystone_time_ms": _clean_number(ctx.get("keystone_time_ms")),
            "fight_duration_s": _clean_number((sig.get("overview") or {}).get("duration_s")),
        }
        is_ref = int(r.get("id") or 0) in reference_ids
        for metric, value in vals.items():
            if value > 0:
                context_metric_values[metric].append(value)
                (ref_context if is_ref else other_context)[metric].append(value)

    context_distributions: dict[str, Any] = {}
    for metric, vals in context_metric_values.items():
        if vals:
            context_distributions[metric] = {
                "p25": round(_q(vals, .25), 3), "p50": round(_q(vals, .5), 3),
                "p75": round(_q(vals, .75), 3), "p90": round(_q(vals, .9), 3),
                "sample_count": len(vals),
            }

    potential_confounders: list[dict[str, Any]] = []
    for metric in context_metric_values:
        rv, ov = ref_context[metric], other_context[metric]
        if len(rv) < 2 or len(ov) < 2:
            continue
        r50, o50 = _q(rv, .5), _q(ov, .5)
        denom = abs(o50) if abs(o50) > 1e-9 else 1.0
        delta_pct = (r50 - o50) / denom * 100.0
        # Item level, key level, duration and pull structure can all materially bias DPS.
        thresholds = {"average_item_level": 1.5, "key_level": 5.0, "pull_count": 8.0, "keystone_time_ms": 8.0, "fight_duration_s": 8.0}
        if abs(delta_pct) >= thresholds.get(metric, 8.0):
            potential_confounders.append({
                "metric": metric, "reference_p50": round(r50, 3), "other_p50": round(o50, 3),
                "delta_pct": round(delta_pct, 2),
                "warning": "参考组与对照组在此上下文变量上存在差异，技能行为相关性可能被该变量混杂。",
            })

    source_distribution: dict[str, int] = {}
    unique_wcl_runs = set()
    provenance_count = 0
    for r in usable:
        src = str(r.get("source") or "unknown")
        source_distribution[src] = source_distribution.get(src, 0) + 1
        meta = {}
        try:
            meta = json.loads(r.get("metadata_json") or "{}")
        except Exception:
            pass
        prov = meta.get("provenance") or {}
        if prov:
            provenance_count += 1
        if prov.get("report_code") and prov.get("fight_id") and prov.get("source_id"):
            unique_wcl_runs.add((prov.get("report_code"), int(prov.get("fight_id") or 0), int(prov.get("source_id") or 0)))
    return {
        "sample_count": len(usable),
        "source_distribution": source_distribution,
        "game_version_distribution": game_versions,
        "patch_scope_distribution": patch_scopes,
        "context_distributions": context_distributions,
        "potential_confounders": potential_confounders,
        "provenance_coverage_pct": round(provenance_count / len(usable) * 100.0, 2) if usable else 0.0,
        "unique_wcl_sample_count": len(unique_wcl_runs),
        "reference_sample_count": len(reference_ids),
        "context_count": len(contexts),
        "contexts": [{"dungeon": d, "key_level": k} for d, k in contexts[:50]],
        "performance_dps": {"p25": round(_q(dps_values, .25), 1), "p50": round(_q(dps_values, .5), 1), "p75": round(_q(dps_values, .75), 1), "p90": round(_q(dps_values, .9), 1)} if dps_values else {},
        "skills": skill_rows[:top_n_skills],
        "buffs": buff_profiles[:25],
        "cadence": cadence_profiles[:25],
        "buff_burst_overlap": overlap_profiles[:20],
        "target_focus": target_focus_profile,
        "empirical_patterns": patterns[:40],
        "aggregate_behavior": aggregate,
        "limitations": [
            "参考组默认优先使用用户标记的 reference 样本；若没有且样本>=4，才临时使用当前样本DPS前25%作为探索组。",
            "DPS相关规律可能被装备、路线、怪量、层数、队友和副本机制混杂；context_distributions / potential_confounders 用于显式暴露这些偏差，它们仍是待验证经验，不是职业理论定律。",
            "样本量越小、上下文越单一，结论越应该保守。",
        ],
    }


def dungeon_target_knowledge(
    dungeon: str,
    *,
    patch_scope: str = "",
    limit: int = 800,
    path: Path | None = None,
) -> dict[str, Any]:
    """Build a dungeon-wide target library from every learned timed WCL sample.

    This intentionally ignores class/spec so bosses and repeatedly focused trash can be
    learned across the whole local evidence library.  It is incremental: personal sync
    and online reference learning both feed the same knowledge_samples table.
    """
    if not dungeon:
        return {"sample_count": 0, "bosses": [], "priority_targets": [], "important_targets": [], "all_targets": []}
    init_knowledge_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_samples WHERE lower(dungeon)=lower(?) AND sample_role!='exclude' ORDER BY id DESC LIMIT ?",
            (str(dungeon), max(50, int(limit))),
        ).fetchall()
    signatures: list[dict[str, Any]] = []
    wanted = str(patch_scope or "").strip().lower()
    for rr in rows:
        sig = _load_signature(dict(rr))
        ctx = sig.get("context") or {}
        if ctx.get("timed_success") is False:
            continue
        if wanted:
            observed = str(ctx.get("patch_scope") or ctx.get("game_version") or (sig.get("provenance") or {}).get("patch_scope") or "").strip().lower()
            if observed and not (observed == wanted or observed.startswith(wanted) or wanted.startswith(observed)):
                continue
        if (sig.get("target_focus") or {}).get("targets"):
            signatures.append(sig)
    profile = aggregate_target_knowledge(signatures, min_samples=2, limit=50)
    profile["dungeon"] = dungeon
    profile["patch_scope"] = patch_scope
    profile["source_scope"] = "all learned timed WCL samples for this dungeon/patch, across class/spec when available"
    return profile


def save_playbook(
    class_name: str,
    spec_name: str,
    profile: dict[str, Any],
    playbook: dict[str, Any],
    model: str,
    dungeon_scope: str = "",
    key_scope: str = "",
    notes: str = "",
    patch_scope: str = "",
    path: Path | None = None,
) -> int:
    init_knowledge_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO knowledge_playbooks(
                created_at, class_name, spec_name, dungeon_scope, key_scope, patch_scope,
                model, sample_count, evidence_json, playbook_json, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), class_name, spec_name, dungeon_scope, key_scope, patch_scope,
                model, int(profile.get("sample_count") or 0), _dumps(profile), _dumps(playbook), notes,
            ),
        )
        return int(cur.lastrowid)


def list_playbooks(class_name: str = "", spec_name: str = "", limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    init_knowledge_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM knowledge_playbooks ORDER BY id DESC LIMIT ?", (max(limit * 4, 100),)).fetchall()
    out = []
    for rr in rows:
        r = dict(rr)
        if class_name and str(r.get("class_name") or "").lower() != class_name.lower():
            continue
        if spec_name and str(r.get("spec_name") or "").lower() != spec_name.lower():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def delete_playbook(playbook_id: int, path: Path | None = None) -> None:
    init_knowledge_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM knowledge_playbooks WHERE id=?", (int(playbook_id),))


def _sample_context(row: dict[str, Any]) -> dict[str, Any]:
    return (_load_signature(row).get("context") or {})


def _timed_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if (_sample_context(r).get("timed_success") is True)]


def _quality_filter(rows: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer samples with similar item level, completion time and pull structure.

    Missing values never hard-fail a sample. This keeps the filter useful with older cached
    WCL samples while still favoring genuinely comparable runs when the fields exist.
    """
    if not rows:
        return []
    tilvl = _clean_number(target.get("player_item_level") or target.get("average_item_level"))
    tdur = _clean_number(target.get("duration_s"))
    if not tdur:
        tdur = _clean_number(target.get("fight_duration_s"))
    tpulls = int(_clean_number(target.get("pull_count")))
    ilvl_tol = max(6.0, tilvl * 0.018) if tilvl > 0 else 0.0
    dur_tol = max(75.0, tdur * 0.16) if tdur > 0 else 0.0
    pull_tol = 4
    out = []
    for row in rows:
        ctx = _sample_context(row)
        sig = _load_signature(row)
        rilvl = _clean_number(ctx.get("player_item_level") or ctx.get("average_item_level"))
        rdur = _clean_number((sig.get("overview") or {}).get("duration_s"))
        rpulls = int(_clean_number(ctx.get("pull_count")))
        if tilvl > 0 and rilvl > 0 and abs(rilvl - tilvl) > ilvl_tol:
            continue
        if tdur > 0 and rdur > 0 and abs(rdur - tdur) > dur_tol:
            continue
        if tpulls > 0 and rpulls > 0 and abs(rpulls - tpulls) > pull_tol:
            continue
        out.append(row)
    return out


def _match_details(
    samples: list[dict[str, Any]],
    *,
    tier: str,
    scope: str,
    target: dict[str, Any],
    relaxed: bool,
) -> dict[str, Any]:
    latest = ""
    if samples:
        latest = max(str(r.get("created_at") or "") for r in samples)
    return {
        "tier": tier,
        "scope": scope,
        "sample_count": len(samples),
        "relaxed": bool(relaxed),
        "target": {
            "dungeon": str(target.get("dungeon") or ""),
            "key_level": int(_clean_number(target.get("key_level"))),
            "patch_scope": str(target.get("patch_scope") or ""),
            "average_item_level": round(_clean_number(target.get("player_item_level") or target.get("average_item_level")), 2),
            "duration_s": round(_clean_number(target.get("duration_s") or target.get("fight_duration_s")), 2),
            "pull_count": int(_clean_number(target.get("pull_count"))),
        },
        "latest_sample_at": latest,
        "human_summary": (
            f"本次使用 {len(samples)} 场{scope}的同专精参考记录。"
            if samples else "当前没有足够的同条件同专精参考记录。"
        ),
    }


def knowledge_context_for_analysis(
    class_name: str,
    spec_name: str,
    dungeon: str = "",
    key_level: int = 0,
    path: Path | None = None,
    patch_scope: str = "",
    target_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a quality-tiered cohort for one analysis target.

    The old implementation jumped from ±2 keys straight to a broad same-patch pool.  This
    version degrades gradually and exposes the exact tier to the AI/report layer so weak
    cohorts cannot masquerade as precise comparisons.
    """
    target = dict(target_context or {})
    target.setdefault("dungeon", dungeon)
    target.setdefault("key_level", key_level)
    target.setdefault("patch_scope", patch_scope)

    min_good = 4
    chosen: list[dict[str, Any]] = []
    tier = "none"
    scope = ""
    relaxed = False

    # 1) Same patch + same dungeon + key ±1, timed, then prefer similar ilvl/time/pulls.
    base1 = select_knowledge_samples(class_name, spec_name, dungeon, key_level, 1, 250, path, patch_scope=patch_scope)
    timed1 = _timed_only(base1) if key_level > 0 else base1
    quality1 = _quality_filter(timed1, target)
    if len(quality1) >= min_good:
        chosen = quality1; tier = "strong"; scope = "同版本、同副本、层数接近、限时成功，且装等/完成时间/波次结构相近"
    elif len(timed1) >= min_good:
        chosen = timed1; tier = "strong_context"; scope = "同版本、同副本、层数接近、限时成功"; relaxed = True

    # 2) Same patch + same dungeon + key ±2 timed.
    if not chosen:
        base2 = select_knowledge_samples(class_name, spec_name, dungeon, key_level, 2, 250, path, patch_scope=patch_scope)
        timed2 = _timed_only(base2) if key_level > 0 else base2
        if len(timed2) >= min_good:
            chosen = timed2; tier = "standard"; scope = "同版本、同副本、层数相近、限时成功"; relaxed = True

    # 3) Same patch + same dungeon + key ±3 timed.
    if not chosen:
        base3 = select_knowledge_samples(class_name, spec_name, dungeon, key_level, 3, 250, path, patch_scope=patch_scope)
        timed3 = _timed_only(base3) if key_level > 0 else base3
        if len(timed3) >= min_good:
            chosen = timed3; tier = "relaxed_key"; scope = "同版本、同副本、层数范围已放宽到约 ±3、限时成功"; relaxed = True

    # 4) Same patch + same dungeon, any key (timed only for M+).
    if not chosen and dungeon:
        same_dungeon = select_knowledge_samples(class_name, spec_name, dungeon, 0, 0, 250, path, patch_scope=patch_scope)
        timed_dungeon = _timed_only(same_dungeon) if key_level > 0 else same_dungeon
        if len(timed_dungeon) >= min_good:
            chosen = timed_dungeon; tier = "relaxed_dungeon"; scope = "同版本、同副本、限时成功，但层数条件已明显放宽"; relaxed = True

    # 5) Same patch + same spec, cross-dungeon. Useful only for long-term habits.
    if not chosen:
        same_patch = select_knowledge_samples(class_name, spec_name, "", 0, 0, 250, path, patch_scope=patch_scope)
        timed_patch = _timed_only(same_patch) if key_level > 0 else same_patch
        if len(timed_patch) >= min_good:
            chosen = timed_patch; tier = "broad_patch"; scope = "同版本、同专精，已跨副本/层数放宽；仅适合看重复习惯，不适合直接比较副本输出"; relaxed = True

    # 6) Final fallback: same dungeon/spec around key across patches. Always low confidence.
    if not chosen:
        cross_patch = select_knowledge_samples(class_name, spec_name, dungeon, key_level, 2, 250, path)
        timed_cross = _timed_only(cross_patch) if key_level > 0 else cross_patch
        chosen = timed_cross or cross_patch
        tier = "cross_patch" if chosen else "none"
        scope = "同专精历史，但包含跨版本样本；只能作为弱参考" if chosen else "无可用同专精参考样本"
        relaxed = True

    profile = empirical_cohort_profile(chosen)
    dungeon_targets = dungeon_target_knowledge(dungeon, patch_scope=patch_scope, path=path) if dungeon else {"sample_count": 0, "all_targets": []}
    playbooks = list_playbooks(class_name, spec_name, 3, path)
    safe_playbooks = []
    for p in playbooks:
        try:
            pb = json.loads(p.get("playbook_json") or "{}")
        except Exception:
            pb = {}
        safe_playbooks.append({
            "created_at": p.get("created_at"), "sample_count": p.get("sample_count"),
            "dungeon_scope": p.get("dungeon_scope"), "key_scope": p.get("key_scope"),
            "patch_scope": p.get("patch_scope"), "model": p.get("model"), "playbook": pb,
        })

    details = _match_details(chosen, tier=tier, scope=scope, target=target, relaxed=relaxed)
    return {
        "policy": (
            "这些职业知识来自真实日志样本的经验归纳。优先使用当前日志直接证据；"
            "横向比较必须先看 cohort_match_details。若匹配层级已放宽、跨副本或跨版本，"
            "只能把差异描述为弱参考，不能包装成精确同条件结论。"
        ),
        "cohort_match_scope": scope,
        "cohort_match_details": details,
        "requested_patch_scope": patch_scope,
        "empirical_profile": profile,
        "dungeon_target_knowledge": dungeon_targets,
        "recent_playbooks": safe_playbooks,
    }


def knowledge_context_for_signatures(
    signatures: list[dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    """Build separate cohort contexts for a multi-run selection.

    Different dungeons are never collapsed into one performance cohort. Cross-dungeon
    evidence can still be used by the AI to identify repeated personal habits, but each
    dungeon gets its own reference pool and match-quality label.
    """
    if not signatures:
        return {"cohort_groups": [], "policy": "没有可用战斗，无法建立横向样本。"}
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for sig in signatures:
        ctx = sig.get("context") or {}
        key = (
            str(ctx.get("class_name") or ""),
            str(ctx.get("spec_name") or ""),
            str(ctx.get("dungeon") or ""),
            str(ctx.get("patch_scope") or ctx.get("game_version") or ""),
        )
        groups.setdefault(key, []).append(sig)

    out = []
    for (class_name, spec_name, dungeon, patch_scope), rows in groups.items():
        keys = [int(_clean_number((s.get("context") or {}).get("key_level"))) for s in rows]
        keys = [k for k in keys if k > 0]
        ilvls = [_clean_number((s.get("context") or {}).get("average_item_level") or (s.get("context") or {}).get("player_item_level")) for s in rows]
        ilvls = [x for x in ilvls if x > 0]
        durations = [_clean_number((s.get("overview") or {}).get("duration_s")) for s in rows]
        durations = [x for x in durations if x > 0]
        pulls = [int(_clean_number((s.get("context") or {}).get("pull_count"))) for s in rows]
        pulls = [x for x in pulls if x > 0]
        target = {
            "dungeon": dungeon,
            "key_level": round(sum(keys) / len(keys)) if keys else 0,
            "patch_scope": patch_scope,
            "average_item_level": round(sum(ilvls) / len(ilvls), 2) if ilvls else 0,
            "duration_s": round(sum(durations) / len(durations), 2) if durations else 0,
            "pull_count": round(sum(pulls) / len(pulls)) if pulls else 0,
        }
        ctx = knowledge_context_for_analysis(
            class_name, spec_name, dungeon, int(target["key_level"] or 0),
            path=path, patch_scope=patch_scope, target_context=target,
        )
        current_target_profile = aggregate_target_knowledge(rows, min_samples=1, limit=30)
        reference_target_profile = ((ctx.get("empirical_profile") or {}).get("target_focus") or {})
        ctx["target_focus_comparison"] = compare_target_profiles(
            current_target_profile, reference_target_profile, ctx.get("dungeon_target_knowledge") or {}
        )
        out.append({
            "class_name": class_name,
            "spec_name": spec_name,
            "dungeon": dungeon,
            "patch_scope": patch_scope,
            "selected_fight_count": len(rows),
            "target_context": target,
            "cohort": ctx,
        })
    out.sort(key=lambda x: x.get("selected_fight_count", 0), reverse=True)
    return {
        "policy": "多场横向比较按副本/版本/专精分组。跨副本只能用于判断重复个人习惯，不能把输出或技能占比直接混在一起比较。",
        "cohort_groups": out,
        "group_count": len(out),
    }
