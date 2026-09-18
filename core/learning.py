from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def user_data_dir() -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        root = home / "Library/Application Support/WoW Log Data Analyst"
    elif system == "Windows":
        root = Path(os.getenv("APPDATA", str(home / "AppData/Roaming"))) / "WoW Log Data Analyst"
    else:
        root = Path(os.getenv("XDG_DATA_HOME", str(home / ".local/share"))) / "wow-log-data-analyst"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_db_path() -> Path:
    return user_data_dir() / "analyst_memory.sqlite3"


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
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> Path:
    db = Path(path or default_db_path())
    with _connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                run_label TEXT,
                player TEXT,
                dungeon TEXT,
                key_level INTEGER,
                class_name TEXT,
                spec_name TEXT,
                analysis_mode TEXT,
                model TEXT,
                payload_json TEXT NOT NULL,
                report_markdown TEXT NOT NULL,
                report_json TEXT,
                rating INTEGER DEFAULT 0,
                confirmed_cause TEXT DEFAULT '',
                correction TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_cases_context
                ON cases(class_name, spec_name, dungeon, key_level, analysis_mode);
            CREATE INDEX IF NOT EXISTS idx_cases_rating ON cases(rating);

            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                case_id INTEGER,
                class_name TEXT,
                spec_name TEXT,
                dungeon TEXT,
                analysis_mode TEXT,
                pattern TEXT NOT NULL,
                when_to_apply TEXT DEFAULT '',
                when_not_to_apply TEXT DEFAULT '',
                confidence TEXT DEFAULT 'medium',
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_lessons_context
                ON lessons(class_name, spec_name, dungeon, analysis_mode);
            """
        )
    return db


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def fingerprint_payload(payload: dict[str, Any]) -> str:
    normalized = _json_dumps(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def save_case(
    context: dict[str, Any],
    payload: dict[str, Any],
    report_markdown: str,
    report_json: dict[str, Any] | None = None,
    model: str = "",
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> int:
    init_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO cases(
                created_at, fingerprint, run_label, player, dungeon, key_level,
                class_name, spec_name, analysis_mode, model, payload_json,
                report_markdown, report_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                fingerprint_payload(payload),
                str(context.get("run_label") or ""),
                str(context.get("player") or ""),
                str(context.get("dungeon") or ""),
                int(context.get("key_level") or 0),
                str(context.get("class_name") or ""),
                str(context.get("spec_name") or ""),
                str(context.get("analysis_mode") or ""),
                model,
                _json_dumps(payload),
                report_markdown,
                _json_dumps(report_json or {}),
                _json_dumps(metadata or {}),
            ),
        )
        return int(cur.lastrowid)


def update_feedback(
    case_id: int,
    rating: int,
    confirmed_cause: str = "",
    correction: str = "",
    path: Path | None = None,
) -> None:
    init_db(path)
    rating = max(-1, min(2, int(rating)))
    with _connect(path) as conn:
        conn.execute(
            "UPDATE cases SET rating=?, confirmed_cause=?, correction=? WHERE id=?",
            (rating, confirmed_cause.strip(), correction.strip(), int(case_id)),
        )


def add_lesson(
    case_id: int,
    context: dict[str, Any],
    pattern: str,
    when_to_apply: str = "",
    when_not_to_apply: str = "",
    confidence: str = "medium",
    path: Path | None = None,
) -> int:
    init_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO lessons(created_at, case_id, class_name, spec_name, dungeon,
                                analysis_mode, pattern, when_to_apply, when_not_to_apply, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), int(case_id),
                str(context.get("class_name") or ""), str(context.get("spec_name") or ""),
                str(context.get("dungeon") or ""), str(context.get("analysis_mode") or ""),
                pattern.strip(), when_to_apply.strip(), when_not_to_apply.strip(), confidence.strip() or "medium",
            ),
        )
        return int(cur.lastrowid)


def recent_cases(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM cases ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_case(case_id: int, path: Path | None = None) -> None:
    init_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM cases WHERE id=?", (int(case_id),))


def clear_memory(path: Path | None = None) -> None:
    init_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM lessons")
        conn.execute("DELETE FROM cases")


def _flatten_numeric(value: Any, prefix: str = "", out: dict[str, float] | None = None) -> dict[str, float]:
    if out is None:
        out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_numeric(v, key, out)
    elif isinstance(value, list):
        # Lists are often top-N tables. Only aggregate simple numeric dict keys to avoid giant signatures.
        numeric_buckets: dict[str, list[float]] = {}
        for item in value[:30]:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    numeric_buckets.setdefault(str(k), []).append(float(v))
        for k, vals in numeric_buckets.items():
            if vals:
                out[f"{prefix}[]:{k}:mean"] = sum(vals) / len(vals)
                out[f"{prefix}[]:{k}:max"] = max(vals)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if math.isfinite(v):
            out[prefix] = v
    return out


def _numeric_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    fa = _flatten_numeric(a)
    fb = _flatten_numeric(b)
    common = [k for k in fa if k in fb]
    if not common:
        return 0.0
    scores: list[float] = []
    for key in common[:120]:
        x, y = fa[key], fb[key]
        # Log scale keeps damage totals from dominating smaller percentages/counters.
        lx = math.copysign(math.log1p(abs(x)), x)
        ly = math.copysign(math.log1p(abs(y)), y)
        scores.append(1.0 / (1.0 + abs(lx - ly)))
    return sum(scores) / len(scores) if scores else 0.0


def _context_similarity(ctx: dict[str, Any], row: dict[str, Any]) -> float:
    score = 0.0
    weight = 0.0
    fields = [
        ("class_name", 0.24), ("spec_name", 0.26), ("dungeon", 0.18), ("analysis_mode", 0.12),
    ]
    for key, w in fields:
        a = str(ctx.get(key) or "").strip().lower()
        b = str(row.get(key) or "").strip().lower()
        if a or b:
            weight += w
            if a and b and a == b:
                score += w
    ka, kb = int(ctx.get("key_level") or 0), int(row.get("key_level") or 0)
    if ka or kb:
        weight += 0.20
        if ka and kb:
            score += 0.20 * max(0.0, 1.0 - min(abs(ka - kb), 10) / 10.0)
    return score / weight if weight else 0.0


def retrieve_similar_cases(
    context: dict[str, Any],
    payload: dict[str, Any],
    limit: int = 5,
    require_feedback: bool = True,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    init_db(path)
    with _connect(path) as conn:
        if require_feedback:
            rows = conn.execute("SELECT * FROM cases WHERE rating != 0 ORDER BY id DESC LIMIT 300").fetchall()
        else:
            rows = conn.execute("SELECT * FROM cases ORDER BY id DESC LIMIT 300").fetchall()
    ranked = []
    current_fp = fingerprint_payload(payload)
    for rr in rows:
        row = dict(rr)
        if row.get("fingerprint") == current_fp:
            continue
        try:
            old_payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            old_payload = {}
        c = _context_similarity(context, row)
        n = _numeric_similarity(payload, old_payload)
        feedback_bonus = 0.12 if int(row.get("rating") or 0) >= 2 else (0.06 if int(row.get("rating") or 0) == 1 else 0.0)
        correction_bonus = 0.05 if str(row.get("correction") or "").strip() else 0.0
        sim = min(1.0, 0.62 * c + 0.38 * n + feedback_bonus + correction_bonus)
        row["similarity"] = round(sim, 4)
        ranked.append(row)
    ranked.sort(key=lambda r: (r["similarity"], int(r.get("rating") or 0), int(r.get("id") or 0)), reverse=True)
    return ranked[: max(0, int(limit))]


def retrieve_lessons(context: dict[str, Any], limit: int = 8, path: Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT 200").fetchall()
    scored = []
    for rr in rows:
        row = dict(rr)
        score = _context_similarity(context, row)
        row["similarity"] = round(score, 4)
        scored.append(row)
    scored.sort(key=lambda r: (r["similarity"], int(r["id"])), reverse=True)
    return scored[: max(0, int(limit))]


def memory_context_for_ai(cases: list[dict[str, Any]], lessons: list[dict[str, Any]]) -> dict[str, Any]:
    safe_cases = []
    for c in cases:
        safe_cases.append({
            "similarity": c.get("similarity"),
            "context": {
                "dungeon": c.get("dungeon"), "key_level": c.get("key_level"),
                "class_name": c.get("class_name"), "spec_name": c.get("spec_name"),
                "analysis_mode": c.get("analysis_mode"), "player": c.get("player"),
            },
            "user_feedback": {
                "rating": c.get("rating"), "confirmed_cause": c.get("confirmed_cause"),
                "correction": c.get("correction"),
            },
            "previous_ai_report_excerpt": str(c.get("report_markdown") or "")[:1800],
        })
    safe_lessons = [
        {
            "similarity": l.get("similarity"), "pattern": l.get("pattern"),
            "when_to_apply": l.get("when_to_apply"), "when_not_to_apply": l.get("when_not_to_apply"),
            "confidence": l.get("confidence"),
        }
        for l in lessons
    ]
    return {
        "policy": "历史案例和经验只用于辅助判断。用户确认过的纠正优先于旧AI结论；任何历史经验都不能覆盖当前日志的直接证据。",
        "similar_cases": safe_cases,
        "learned_lessons": safe_lessons,
    }


def recent_lessons(limit: int = 100, path: Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    return [dict(r) for r in rows]
