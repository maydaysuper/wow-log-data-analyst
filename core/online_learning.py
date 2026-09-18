from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

from .knowledge import save_knowledge_sample, has_wcl_knowledge_sample
from .learning import default_db_path
from .wcl_client import WCLClient, extract_report_code, iter_dicts
from .wcl_timeline_features import (
    buff_burst_overlap_rows, buff_features, burst_windows_from_damage,
    cast_cadence_rows, cast_duration_rows, pull_breakdown_rows,
)
from .target_focus import build_target_focus_profile


USER_AGENT = "WoWLogDataAnalyst/0.17 (+local desktop learning client)"
MAX_REFERENCE_BYTES = 1_500_000
MAX_REFERENCE_TEXT = 35_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(v: Any, default: Any) -> Any:
    try:
        return json.loads(v or "")
    except Exception:
        return default


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
    return conn


def init_online_learning_db(path: Path | None = None) -> Path:
    db = Path(path or default_db_path())
    with _connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS online_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                canonical_id TEXT NOT NULL,
                url TEXT DEFAULT '',
                title TEXT DEFAULT '',
                revision TEXT DEFAULT '',
                source_hash TEXT NOT NULL,
                trust_class TEXT DEFAULT 'unknown',
                context_json TEXT DEFAULT '{}',
                payload_json TEXT DEFAULT '{}',
                UNIQUE(source_type, canonical_id, source_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_online_sources_context
                ON online_sources(source_type, canonical_id, created_at);

            CREATE TABLE IF NOT EXISTS external_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                class_name TEXT DEFAULT '',
                spec_name TEXT DEFAULT '',
                patch_scope TEXT DEFAULT '',
                evidence_kind TEXT DEFAULT 'reference',
                model TEXT DEFAULT '',
                evidence_json TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES online_sources(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_external_evidence_context
                ON external_evidence(class_name, spec_name, evidence_kind, id);

            CREATE TABLE IF NOT EXISTS online_learning_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                class_name TEXT DEFAULT '',
                spec_name TEXT DEFAULT '',
                dungeon TEXT DEFAULT '',
                key_level INTEGER DEFAULT 0,
                requested INTEGER DEFAULT 0,
                candidates_seen INTEGER DEFAULT 0,
                imported INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                details_json TEXT DEFAULT '{}'
            );
            """
        )
    return db


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_source_snapshot(
    source_type: str,
    canonical_id: str,
    payload: Any,
    *,
    url: str = "",
    title: str = "",
    revision: str = "",
    trust_class: str = "unknown",
    context: dict[str, Any] | None = None,
    path: Path | None = None,
) -> int:
    init_online_learning_db(path)
    body = _dumps(payload).encode("utf-8")
    digest = _sha256_bytes(body)
    now = _utcnow()
    with _connect(path) as conn:
        existing = conn.execute(
            "SELECT id FROM online_sources WHERE source_type=? AND canonical_id=? AND source_hash=?",
            (source_type, canonical_id, digest),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO online_sources(
                created_at, fetched_at, source_type, canonical_id, url, title,
                revision, source_hash, trust_class, context_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, now, source_type, canonical_id, url, title, revision, digest,
             trust_class, _dumps(context or {}), _dumps(payload)),
        )
        return int(cur.lastrowid)


def list_online_sources(limit: int = 300, source_type: str = "", path: Path | None = None) -> list[dict[str, Any]]:
    init_online_learning_db(path)
    q = "SELECT * FROM online_sources"
    args: list[Any] = []
    if source_type:
        q += " WHERE source_type=?"
        args.append(source_type)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _connect(path) as conn:
        rows = conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def save_external_evidence(
    source_id: int,
    evidence: dict[str, Any],
    *,
    class_name: str = "",
    spec_name: str = "",
    patch_scope: str = "",
    evidence_kind: str = "reference",
    model: str = "",
    path: Path | None = None,
) -> int:
    init_online_learning_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO external_evidence(
                created_at, source_id, class_name, spec_name, patch_scope,
                evidence_kind, model, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_utcnow(), int(source_id), class_name, spec_name, patch_scope,
             evidence_kind, model, _dumps(evidence)),
        )
        return int(cur.lastrowid)


def list_external_evidence(
    class_name: str = "",
    spec_name: str = "",
    limit: int = 100,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    init_online_learning_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT e.*, s.source_type, s.canonical_id, s.url, s.title,
                   s.fetched_at, s.revision, s.trust_class
            FROM external_evidence e
            JOIN online_sources s ON s.id=e.source_id
            ORDER BY e.id DESC LIMIT ?
            """,
            (max(100, int(limit) * 3),),
        ).fetchall()
    out = []
    for rr in rows:
        r = dict(rr)
        if class_name and str(r.get("class_name") or "").lower() not in {"", class_name.lower()}:
            continue
        if spec_name and str(r.get("spec_name") or "").lower() not in {"", spec_name.lower()}:
            continue
        r["evidence"] = _loads(r.get("evidence_json"), {})
        out.append(r)
        if len(out) >= limit:
            break
    return out


def online_context_for_analysis(
    class_name: str,
    spec_name: str,
    *,
    limit: int = 8,
    path: Path | None = None,
) -> dict[str, Any]:
    rows = list_external_evidence(class_name, spec_name, max(1, int(limit)), path)
    compact = []
    for r in rows:
        evidence = r.get("evidence") or {}
        compact.append({
            "source_type": r.get("source_type"),
            "title": r.get("title"),
            "url": r.get("url"),
            "revision": r.get("revision"),
            "fetched_at": r.get("fetched_at"),
            "trust_class": r.get("trust_class"),
            "class_name": r.get("class_name"),
            "spec_name": r.get("spec_name"),
            "patch_scope": r.get("patch_scope"),
            "evidence_kind": r.get("evidence_kind"),
            "evidence": evidence,
        })
    return {
        "policy": (
            "联网资料只能作为可追溯的辅助证据。经验Log优先用于描述实际行为；"
            "外部机制/攻略资料用于解释或提出验证方向。不得把社区观点当作游戏机制事实，"
            "不得把旧版本资料直接套到当前版本。"
        ),
        "items": compact,
    }


# --------------------------- safe public reference fetch ---------------------------


def _host_is_public(host: str) -> bool:
    if not host:
        return False
    low = host.lower().strip(".")
    if low in {"localhost", "localhost.localdomain"} or low.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {x[4][0] for x in infos if x and x[4]}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _validate_public_url(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in {"http", "https"}:
        raise ValueError("只允许 http/https 公共网址。")
    if p.username or p.password:
        raise ValueError("网址不能包含账号或密码。")
    if not _host_is_public(p.hostname or ""):
        raise ValueError("出于安全原因，不允许访问本机、局域网或私有地址。")
    return p.geturl()


class _ReadableHTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth_skip = 0
        self.title = ""
        self._in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self.SKIP:
            self.depth_skip += 1
        if t == "title":
            self._in_title = True
        if t in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.SKIP and self.depth_skip:
            self.depth_skip -= 1
        if t == "title":
            self._in_title = False
        if t in {"p", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.depth_skip:
            return
        s = re.sub(r"\s+", " ", data).strip()
        if not s:
            return
        if self._in_title:
            self.title = (self.title + " " + s).strip()
        self.parts.append(s)

    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def classify_reference_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("warcraftlogs.com"):
        return "primary_log_api"
    if host.endswith("blizzard.com") or host.endswith("battle.net") or host.endswith("worldofwarcraft.com"):
        return "official"
    if host.endswith("warcraft.wiki.gg"):
        return "mechanics_reference"
    if host.endswith("wowhead.com"):
        return "community_reference"
    return "user_reference"


def fetch_public_reference(url: str, timeout: int = 20, max_redirects: int = 3) -> dict[str, Any]:
    current = _validate_public_url(url)
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.2"}
    response = None
    for _ in range(max_redirects + 1):
        response = session.get(current, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
        if response.status_code in {301, 302, 303, 307, 308}:
            loc = response.headers.get("Location")
            if not loc:
                break
            current = _validate_public_url(urljoin(current, loc))
            continue
        break
    if response is None:
        raise RuntimeError("无法获取网址。")
    response.raise_for_status()
    ctype = (response.headers.get("Content-Type") or "").lower()
    if not any(x in ctype for x in ("text/html", "text/plain", "application/xhtml+xml")):
        raise ValueError(f"暂不支持此内容类型：{ctype or 'unknown'}")
    buf = bytearray()
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > MAX_REFERENCE_BYTES:
            raise ValueError("页面过大，已停止抓取。")
    encoding = response.encoding or "utf-8"
    text = bytes(buf).decode(encoding, errors="replace")
    title = ""
    if "html" in ctype or "<html" in text[:1000].lower():
        parser = _ReadableHTML()
        parser.feed(text)
        title = parser.title
        readable = parser.text()
    else:
        readable = text
    readable = re.sub(r"\n{3,}", "\n\n", readable).strip()[:MAX_REFERENCE_TEXT]
    if len(readable) < 80:
        raise ValueError("网页可读取正文太少，可能需要登录或被站点限制。")
    return {
        "url": current,
        "title": title or current,
        "text": readable,
        "content_type": ctype,
        "fetched_at": _utcnow(),
        "trust_class": classify_reference_domain(current),
        "sha256": _sha256_bytes(bytes(buf)),
    }


def ingest_public_reference(
    url: str,
    *,
    class_name: str = "",
    spec_name: str = "",
    patch_scope: str = "",
    evidence_kind: str = "reference",
    path: Path | None = None,
) -> dict[str, Any]:
    doc = fetch_public_reference(url)
    source_id = save_source_snapshot(
        "public_reference",
        canonical_id=doc["url"],
        payload={"text": doc["text"]},
        url=doc["url"], title=doc["title"], revision=doc["sha256"][:16],
        trust_class=doc["trust_class"],
        context={"class_name": class_name, "spec_name": spec_name, "patch_scope": patch_scope, "evidence_kind": evidence_kind},
        path=path,
    )
    return {**doc, "source_id": source_id}


# --------------------------- WCL ranking -> behavior signature ---------------------------


def _game_version_scope(value: Any) -> str:
    raw = str(value or "").strip()
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", raw)
    return m.group(1) if m else raw[:48]


def _num(v: Any) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _first(d: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    lowered = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in lowered and lowered[k.lower()] is not None:
            return lowered[k.lower()]
    return default


def _report_code_near(d: dict[str, Any]) -> str:
    direct = _first(d, ["reportCode", "report_code", "reportID", "report_id", "report"])
    if isinstance(direct, str):
        c = extract_report_code(direct)
        if re.fullmatch(r"[A-Za-z0-9]{6,32}", c or ""):
            return c
    if isinstance(direct, dict):
        c = _first(direct, ["code", "reportCode", "id"])
        if isinstance(c, str):
            c = extract_report_code(c)
            if re.fullmatch(r"[A-Za-z0-9]{6,32}", c or ""):
                return c
    for k, v in d.items():
        if isinstance(v, str) and "warcraftlogs.com/reports/" in v:
            c = extract_report_code(v)
            if c:
                return c
        if str(k).lower() == "code" and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{8,32}", v):
            # Only accept a generic code field when the surrounding dict also looks ranking/report-like.
            joined = " ".join(str(x).lower() for x in d.keys())
            if any(x in joined for x in ("fight", "rank", "duration", "report")):
                return v
    return ""


def extract_ranking_candidates(payload: Any) -> list[dict[str, Any]]:
    """Best-effort parser for WCL ranking JSON scalars.

    WCL intentionally exposes ranking results as JSON, so field layouts can evolve without
    changing the GraphQL schema. We keep raw candidate fragments as provenance and require
    a report code + fight id before importing anything into the learned cohort.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for d in iter_dicts(payload):
        code = _report_code_near(d)
        fight = _first(d, ["fightID", "fightId", "fight_id", "fight"])
        try:
            fight_id = int(fight)
        except Exception:
            continue
        if not code or fight_id <= 0:
            continue
        source = _first(d, ["sourceID", "sourceId", "source_id", "actorID", "actorId", "playerID", "playerId"])
        try:
            source_id = int(source or 0)
        except Exception:
            source_id = 0
        name = str(_first(d, ["name", "playerName", "characterName"], "") or "")
        percentile = _num(_first(d, ["rankPercent", "rankPercentile", "percentile", "rank_pct"], 0))
        rank = int(_num(_first(d, ["rank", "rankPosition", "position"], 0)))
        amount = _num(_first(d, ["amount", "total", "score", "dps"], 0))
        key = (code, fight_id, source_id, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "report_code": code,
            "fight_id": fight_id,
            "source_id": source_id,
            "player_name": name,
            "percentile": percentile,
            "rank": rank,
            "ranking_value": amount,
            "raw": d,
        })
    return out


def _master_maps(report: dict[str, Any]) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    master = report.get("masterData") or {}
    abilities: dict[int, str] = {}
    actors: dict[int, dict[str, Any]] = {}
    for a in master.get("abilities") or []:
        try:
            abilities[int(a.get("gameID") or 0)] = str(a.get("name") or a.get("gameID") or "Unknown")
        except Exception:
            pass
    for a in master.get("actors") or []:
        try:
            actors[int(a.get("id") or 0)] = dict(a)
        except Exception:
            pass
    return abilities, actors


def _ability_id(event: dict[str, Any]) -> int:
    v = _first(event, ["abilityGameID", "abilityGuid", "abilityID", "ability"])
    if isinstance(v, dict):
        v = _first(v, ["gameID", "guid", "id"])
    try:
        return int(v or 0)
    except Exception:
        return 0


def _event_target(event: dict[str, Any]) -> int:
    try:
        return int(_first(event, ["targetID", "targetId", "target_id"], 0) or 0)
    except Exception:
        return 0


def _event_ts(event: dict[str, Any]) -> float:
    return _num(_first(event, ["timestamp", "time"], 0))


def _looks_crit(event: dict[str, Any]) -> bool:
    if bool(event.get("critical")):
        return True
    ht = _first(event, ["hitType", "hit_type"])
    if isinstance(ht, str):
        return ht.lower() in {"crit", "critical", "2"}
    return int(_num(ht)) == 2


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values, dtype=float).quantile(q))


def _buff_uptime_from_events(events: list[dict[str, Any]], duration_ms: float, abilities: dict[int, str]) -> list[dict[str, Any]]:
    active: dict[int, float] = {}
    total: defaultdict[int, float] = defaultdict(float)
    applies: Counter[int] = Counter()
    for e in sorted(events, key=_event_ts):
        aid = _ability_id(e)
        if not aid:
            continue
        typ = str(e.get("type") or "").lower()
        ts = max(0.0, _event_ts(e))
        if typ in {"applybuff", "applybuffstack"}:
            applies[aid] += 1
            active.setdefault(aid, ts)
        elif typ in {"refreshbuff"}:
            applies[aid] += 1
            active.setdefault(aid, ts)
        elif typ == "removebuffstack":
            # Stack loss does not necessarily end the aura; wait for removebuff.
            continue
        elif typ == "removebuff" and aid in active:
            total[aid] += max(0.0, ts - active.pop(aid))
    for aid, started in active.items():
        total[aid] += max(0.0, duration_ms - started)
    rows = []
    for aid, ms in total.items():
        rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            "uptime_pct": round(min(100.0, max(0.0, ms / max(1.0, duration_ms) * 100.0)), 3),
            "applications": int(applies[aid]),
        })
    rows.sort(key=lambda x: x["uptime_pct"], reverse=True)
    return rows[:30]


def _resource_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {}
    observed_pct: list[float] = []
    generated = 0.0
    wasted = 0.0
    by_type: Counter[str] = Counter()
    for e in events:
        cur = _num(_first(e, ["resourceAmount", "resource", "currentResource", "power"], 0))
        mx = _num(_first(e, ["maxResourceAmount", "maxResource", "maxPower"], 0))
        if mx > 0:
            observed_pct.append(max(0.0, min(100.0, cur / mx * 100.0)))
        change = max(0.0, _num(_first(e, ["resourceChange", "amount", "energizeAmount"], 0)))
        waste = max(0.0, _num(_first(e, ["waste", "resourceWaste", "overcap"], 0)))
        generated += change
        wasted += waste
        typ = str(_first(e, ["resourceChangeType", "resourceType", "powerType"], "unknown"))
        by_type[typ] += 1
    return {
        "avg_resource_pct": round(sum(observed_pct) / len(observed_pct), 3) if observed_pct else 0.0,
        "time_at_or_above_90_pct": round(sum(1 for x in observed_pct if x >= 90) / len(observed_pct) * 100, 3) if observed_pct else 0.0,
        "time_at_or_below_10_pct": round(sum(1 for x in observed_pct if x <= 10) / len(observed_pct) * 100, 3) if observed_pct else 0.0,
        "generated": round(generated, 2),
        "waste": round(wasted, 2),
        "overcap_pct_of_generated": round(wasted / generated * 100, 3) if generated > 0 else 0.0,
        "event_count": len(events),
        "resource_types": dict(by_type),
    }


def _target_switch_summary(casts: list[dict[str, Any]], duration_s: float) -> dict[str, Any]:
    seq = [_event_target(e) for e in sorted(casts, key=_event_ts) if _event_target(e) > 0]
    switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    return {
        "casts_with_target": len(seq),
        "unique_targets": len(set(seq)),
        "switch_count": switches,
        "switches_per_min": round(switches / max(duration_s / 60.0, 1e-9), 3),
    }


def _responsiveness_from_casts(
    casts: list[dict[str, Any]],
    duration_s: float,
    *,
    deaths: list[dict[str, Any]] | None = None,
    pull_windows: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Build a conservative action-gap summary.

    Only gaps while the player is alive and inside an observed combat pull count toward
    the responsiveness score. A death inside the gap invalidates the gap completely:
    being dead is not a "stall". For Mythic+ we also exclude gaps outside dungeon-pull
    windows so travel/RP/route downtime does not inflate the score.
    """
    ts = sorted(_event_ts(e) for e in casts if _event_ts(e) >= 0)
    death_ts = sorted(_event_ts(e) for e in (deaths or []) if _event_ts(e) >= 0)
    windows = list(pull_windows or [])

    valid_gaps: list[float] = []
    death_excluded = 0
    outside_combat_excluded = 0
    for a, b in zip(ts, ts[1:]):
        if b <= a:
            continue
        if any(a <= d <= b for d in death_ts):
            death_excluded += 1
            continue
        if windows:
            if not any(a >= start - 750 and b <= end + 750 for start, end in windows):
                outside_combat_excluded += 1
                continue
        valid_gaps.append((b - a) / 1000.0)

    severe = [g for g in valid_gaps if g >= 4.5]
    long_gaps = [g for g in valid_gaps if g >= 2.8]
    p95 = _percentile(valid_gaps, .95)
    score = min(100.0, len(severe) * 12.0 + len(long_gaps) * 3.0 + max(0.0, p95 - 2.5) * 4.0)
    return {
        "score": round(score, 2),
        "cast_gap_median_s": round(_percentile(valid_gaps, .5), 3),
        "cast_gap_p95_s": round(p95, 3),
        "max_cast_gap_s": round(max(valid_gaps) if valid_gaps else 0.0, 3),
        "long_gap_count": len(long_gaps),
        "severe_gap_count": len(severe),
        "death_excluded_gap_count": death_excluded,
        "outside_combat_excluded_gap_count": outside_combat_excluded,
        "valid_combat_gap_count": len(valid_gaps),
        "interpretation_guardrail": "只统计战斗中、玩家存活状态下的停手；死亡窗口、跑图/RP/波次间空档已尽量排除。即使如此也不能单独证明网络或客户端卡顿。",
    }


def build_wcl_behavior_signature(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    player_name: str,
    source_id: int,
    class_name: str,
    spec_name: str,
    casts: list[dict[str, Any]],
    damage: list[dict[str, Any]],
    buffs: list[dict[str, Any]] | None = None,
    resources: list[dict[str, Any]] | None = None,
    deaths: list[dict[str, Any]] | None = None,
    ranking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    abilities, actors = _master_maps(report)
    fight_start = _num(fight.get("startTime"))
    duration_ms = max(1.0, _num(fight.get("endTime")) - fight_start)
    duration_s = duration_ms / 1000.0

    def normalize_events(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        out = []
        for e in rows or []:
            x = dict(e)
            x["timestamp"] = max(0.0, _event_ts(e) - fight_start)
            out.append(x)
        return out

    casts = normalize_events(casts)
    damage = normalize_events(damage)
    buffs = normalize_events(buffs)
    resources = normalize_events(resources)
    deaths = normalize_events(deaths)
    pulls = fight.get("dungeonPulls") or []
    pull_windows = []
    for pull in pulls:
        ps = max(0.0, _num(pull.get("startTime")) - fight_start)
        pe = max(ps, _num(pull.get("endTime")) - fight_start)
        if pe > ps:
            pull_windows.append((ps, pe))

    # WCL already supplies the raw event streams needed for timeline-quality coaching.
    # Compute these locally once so the AI receives evidence, not thousands of raw events.
    cadence_rows = cast_cadence_rows(casts, abilities, pull_windows)
    cast_duration_stats = cast_duration_rows(casts, abilities)
    buff_rows, buff_windows = buff_features(buffs or [], duration_ms, abilities, pull_windows)
    burst_windows = burst_windows_from_damage(damage, abilities)
    burst_overlap = buff_burst_overlap_rows(buff_rows, buff_windows, burst_windows)
    pull_breakdown = pull_breakdown_rows(pulls, pull_windows, casts, damage, deaths or [], abilities)
    target_focus = build_target_focus_profile(
        report, fight, casts, damage, fight_start_ms=fight_start, early_window_s=8.0
    )

    total_damage = 0.0
    damage_by: defaultdict[int, float] = defaultdict(float)
    hits_by: Counter[int] = Counter()
    crits_by: Counter[int] = Counter()
    targets_by: defaultdict[int, set[int]] = defaultdict(set)
    for e in damage:
        aid = _ability_id(e)
        amount = max(0.0, _num(_first(e, ["amount", "unmitigatedAmount", "effectiveAmount"], 0)))
        if not aid or amount <= 0:
            continue
        total_damage += amount
        damage_by[aid] += amount
        hits_by[aid] += 1
        if _looks_crit(e):
            crits_by[aid] += 1
        t = _event_target(e)
        if t:
            targets_by[aid].add(t)
    cast_by: Counter[int] = Counter()
    successful_casts: list[dict[str, Any]] = []
    for e in casts:
        aid = _ability_id(e)
        typ = str(e.get("type") or "cast").lower()
        if not aid or typ in {"begincast", "startcast"}:
            continue
        cast_by[aid] += 1
        successful_casts.append(e)

    ability_ids = set(damage_by) | set(cast_by)
    skill_rows = []
    for aid in ability_ids:
        dmg = damage_by.get(aid, 0.0)
        hits = hits_by.get(aid, 0)
        c = cast_by.get(aid, 0)
        skill_rows.append({
            "spell_id": aid,
            "spell_name": abilities.get(aid, str(aid)),
            "total_casts": int(c),
            "damage_pct": round(dmg / total_damage * 100.0, 4) if total_damage else 0.0,
            "casts_per_min": round(c / max(duration_s / 60.0, 1e-9), 4),
            "hits_per_cast": round(hits / c, 4) if c else 0.0,
            "crit_pct": round(crits_by.get(aid, 0) / hits * 100.0, 4) if hits else 0.0,
            "avg_hit": round(dmg / hits, 2) if hits else 0.0,
            "damage_per_cast": round(dmg / c, 2) if c else 0.0,
            "unique_targets": len(targets_by.get(aid, set())),
        })
    skill_rows.sort(key=lambda x: x["damage_pct"], reverse=True)

    rank = ranking or {}
    dungeon = str(fight.get("name") or (report.get("zone") or {}).get("name") or "")
    key_level = int(_num(fight.get("keystoneLevel")))
    actor = actors.get(int(source_id)) or {}
    player_name = player_name or str(actor.get("name") or source_id)
    player_item_level = 0.0
    try:
        friendly_players = [int(x) for x in (fight.get("friendlyPlayers") or [])]
        friendly_ilvls = list(fight.get("friendlyItemLevels") or [])
        if int(source_id) in friendly_players:
            idx = friendly_players.index(int(source_id))
            if idx < len(friendly_ilvls):
                player_item_level = _num(friendly_ilvls[idx])
    except Exception:
        player_item_level = 0.0
    master = report.get("masterData") or {}
    game_version = str(master.get("gameVersion") or "")
    patch_scope = _game_version_scope(game_version)
    pulls = fight.get("dungeonPulls") or []
    observed_duration_ms = max(0.0, _num(fight.get("endTime")) - _num(fight.get("startTime")))
    timer_ms = _num(fight.get("keystoneTime"))
    timed_success = bool(
        key_level > 0
        and fight.get("kill")
        and (int(_num(fight.get("keystoneBonus"))) > 0 or (timer_ms > 0 and observed_duration_ms <= timer_ms))
    )
    return {
        "schema_version": 5,
        "context": {
            "run_label": f"WCL {report.get('code','')} fight {fight.get('id','')}",
            "player": player_name,
            "dungeon": dungeon,
            "zone_id": int(_num((report.get("zone") or {}).get("id"))),
            "encounter_id": int(_num(fight.get("encounterID"))),
            "key_level": key_level,
            "average_item_level": round(_num(fight.get("averageItemLevel")), 2),
            "player_item_level": round(player_item_level, 2),
            "keystone_time_ms": round(_num(fight.get("keystoneTime")), 2),
            "keystone_bonus": int(_num(fight.get("keystoneBonus"))),
            "timed_success": timed_success,
            "enemy_count_reached": round(_num(fight.get("countReached")), 3),
            "enemy_count_required": round(_num(fight.get("countRequired")), 3),
            "pull_count": len(pulls),
            "affixes": fight.get("keystoneAffixes") or [],
            "friendly_specs": fight.get("friendlySpecs") or [],
            "game_version": game_version,
            "patch_scope": patch_scope,
            "class_name": class_name,
            "spec_name": spec_name,
            "segment": "All",
            "source": "wcl_online",
        },
        "overview": {
            "duration_s": round(duration_s, 3),
            "total_damage": round(total_damage, 2),
            "dps": round(total_damage / max(duration_s, 1e-9), 2),
            "casts": len(successful_casts),
        },
        "skills": skill_rows[:50],
        "target_focus": target_focus,
        "efficiency": {
            "resource_efficiency": _resource_summary(resources or []),
            "target_switching": _target_switch_summary(successful_casts, duration_s),
            "pull_downtime": {},
            "buff_uptime": buff_rows,
            "cast_cadence": cadence_rows,
            "cast_duration": cast_duration_stats,
            "death_recovery": [],
            "buff_burst_overlap": burst_overlap,
        },
        "responsiveness": _responsiveness_from_casts(
            successful_casts, duration_s, deaths=deaths, pull_windows=pull_windows
        ),
        "timeline": {
            "dungeon_pulls": pulls[:60],
            "pull_breakdown": pull_breakdown[:40],
            "burst_windows": burst_windows[:8],
        },
        "ranking": {
            "percentile": _num(rank.get("percentile")),
            "rank": int(_num(rank.get("rank"))),
            "ranking_value": _num(rank.get("ranking_value")),
        },
        "provenance": {
            "source_type": "warcraft_logs_api",
            "report_code": report.get("code"),
            "report_revision": report.get("revision"),
            "fight_id": fight.get("id"),
            "source_id": int(source_id),
            "report_start_time": report.get("startTime"),
            "game_version": game_version,
            "patch_scope": patch_scope,
            "log_version": master.get("logVersion"),
            "fetched_at": _utcnow(),
        },
        "limitations": [
            "这是通过 Warcraft Logs API 重新读取事件后生成的经验行为样本，不等同于理论最优循环。",
            "WCL排名和DPS受装备、路线、怪量、队友和版本影响；所有推论必须保留赛季/副本/层数上下文。",
            "Buff覆盖率同时提供整场流程与战斗窗口口径；大秘境手法判断优先使用 combat_uptime_pct。",
            "技能持续时间中的 start_to_cast 是日志观察到的 begincast→cast 时长，不等于技能数据库理论施法时间。",
            "仅凭施法时间轴不能证明网络卡顿；网络归因仍需要延迟/FPS遥测或用户确认。",
        ],
    }


def _report_from_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return (((raw.get("reportData") or {}).get("report")) or {})


def _find_fight(report: dict[str, Any], fight_id: int) -> dict[str, Any]:
    for f in report.get("fights") or []:
        if int(_num(f.get("id"))) == int(fight_id):
            return dict(f)
    return {}


def _resolve_source_id(report: dict[str, Any], candidate: dict[str, Any], class_name: str = "") -> tuple[int, str]:
    source_id = int(candidate.get("source_id") or 0)
    player_name = str(candidate.get("player_name") or "")
    _, actors = _master_maps(report)
    if source_id and source_id in actors:
        return source_id, player_name or str(actors[source_id].get("name") or "")
    if player_name:
        for aid, actor in actors.items():
            if str(actor.get("name") or "").lower() == player_name.lower():
                if not class_name or str(actor.get("subType") or "").lower() == class_name.lower():
                    return aid, player_name
        for aid, actor in actors.items():
            if str(actor.get("name") or "").lower() == player_name.lower():
                return aid, player_name
    return 0, player_name



def _precheck_target_fight(report: dict[str, Any], fight: dict[str, Any], target_context: dict[str, Any] | None) -> tuple[bool, str]:
    """Cheap metadata gate before downloading casts/damage/buffs/resources."""
    if not target_context:
        return True, ""
    target = target_context or {}
    tk = int(_num(target.get("key_level")))
    sk = int(_num(fight.get("keystoneLevel")))
    if tk and sk and abs(tk - sk) > 2:
        return False, "层数差距超过2"
    require_timed = bool(target.get("require_timed_success")) or tk > 0
    if require_timed:
        start, end = _num(fight.get("startTime")), _num(fight.get("endTime"))
        timer = _num(fight.get("keystoneTime"))
        timed = bool(fight.get("kill")) and int(_num(fight.get("keystoneLevel"))) > 0 and (
            int(_num(fight.get("keystoneBonus"))) > 0
            or (timer > 0 and end > start and (end-start) <= timer)
        )
        if not timed:
            return False, "不是限时成功大秘境"
    td = str(target.get("dungeon") or "").strip().lower()
    sd = str(fight.get("name") or (report.get("zone") or {}).get("name") or "").strip().lower()
    tz = int(_num(target.get("zone_id")))
    sz = int(_num((report.get("zone") or {}).get("id")))
    if tz and sz and tz != sz:
        return False, "副本区域不一致"
    if not (tz and sz) and td and sd and td != sd:
        return False, "副本不一致"
    tp = str(target.get("patch_scope") or "").strip().lower()
    gv = str((report.get("masterData") or {}).get("gameVersion") or "")
    sp = _game_version_scope(gv).strip().lower()
    if tp and sp and not (tp == sp or tp.startswith(sp) or sp.startswith(tp)):
        return False, "游戏版本不一致"
    return True, ""


def _fetch_learning_events(client: WCLClient, code: str, fight_id: int, source_id: int, *, evidence_level: str, percentile: float) -> dict[str, list[dict[str, Any]]]:
    specs: dict[str, dict[str, Any]] = {
        "casts": {"data_type": "Casts", "source_id": source_id, "limit": 10000, "max_pages": 8},
        "damage": {"data_type": "DamageDone", "source_id": source_id, "limit": 10000, "max_pages": 12},
    }
    if evidence_level in {"standard", "deep"}:
        specs["buffs"] = {"data_type": "Buffs", "target_id": source_id, "limit": 10000, "max_pages": 7}
        specs["deaths"] = {"data_type": "Deaths", "target_id": source_id, "limit": 10000, "max_pages": 3}
    if evidence_level == "deep" or (evidence_level == "standard" and percentile >= 90.0):
        specs["resources"] = {"data_type": "Resources", "source_id": source_id, "include_resources": True, "limit": 10000, "max_pages": 7}

    def one(label: str, cfg: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        child = client.fork() if hasattr(client, "fork") else client
        try:
            return label, child.report_events(code, fight_id, **cfg)
        finally:
            if child is not client and hasattr(child, "close"):
                child.close()

    out: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(specs)), thread_name_prefix="wcl-cohort") as pool:
        fs = {pool.submit(one, k, v): k for k, v in specs.items()}
        for fut in as_completed(fs):
            k = fs[fut]
            try:
                label, data = fut.result()
                out[label] = data
            except Exception:
                out[k] = []
    return out


def _stratified_candidates(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Spread discovery across the ranking list instead of importing only the top few.

    WCL ranking order is useful for discovering strong players, but a useful cohort also
    needs non-elite examples.  We keep a few leading candidates and then sample across
    the remaining list.  The underlying report is still re-fetched before learning.
    """
    if limit <= 0 or not rows:
        return []
    if len(rows) <= limit:
        return list(rows)
    picks: list[int] = []
    # Keep the top three when possible for a clear high-performance reference subset.
    for i in range(min(3, limit, len(rows))):
        picks.append(i)
    remaining = limit - len(picks)
    if remaining > 0:
        start = min(3, len(rows) - 1)
        span = max(1, len(rows) - 1 - start)
        for j in range(1, remaining + 1):
            idx2 = start + round(span * j / remaining)
            picks.append(min(len(rows) - 1, idx2))
    out = []
    seen = set()
    for i in picks:
        if i not in seen:
            seen.add(i); out.append(rows[i])
    return out[:limit]


@dataclass
class WCLLearningResult:
    requested: int
    candidates_seen: int
    imported: int
    skipped: int
    failed: int
    samples: list[dict[str, Any]]
    errors: list[str]
    rate_limit: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "candidates_seen": self.candidates_seen,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "samples": self.samples,
            "errors": self.errors,
            "rate_limit": self.rate_limit,
        }


def _target_quality(signature: dict[str, Any], target_context: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    """Check whether a freshly fetched WCL sample is genuinely comparable.

    Rankings are discovery only.  We reject samples that fail the hard context match
    after re-fetching the underlying report/fight, and keep softer ilvl/time similarity
    as a score for later weighting/metadata.
    """
    if not target_context:
        return True, {"score": 1.0, "hard_match": True, "reasons": []}
    target = dict(target_context)
    ctx = signature.get("context") or {}
    ov = signature.get("overview") or {}
    reasons: list[str] = []
    hard = True

    tz = int(_num(target.get("zone_id")))
    sz = int(_num(ctx.get("zone_id")))
    td = str(target.get("dungeon") or "").strip().lower()
    sd = str(ctx.get("dungeon") or "").strip().lower()
    if tz and sz and tz != sz:
        hard = False; reasons.append("副本区域不一致")
    elif not (tz and sz) and td and sd and td != sd:
        hard = False; reasons.append("副本不一致")

    tk = int(_num(target.get("key_level")))
    sk = int(_num(ctx.get("key_level")))
    if tk and sk and abs(tk - sk) > 2:
        hard = False; reasons.append("层数差距超过2")

    tp = str(target.get("patch_scope") or "").strip().lower()
    sp = str(ctx.get("patch_scope") or "").strip().lower()
    if tp and sp and not (tp == sp or tp.startswith(sp) or sp.startswith(tp)):
        hard = False; reasons.append("游戏版本不一致")

    if (tk > 0 or bool(target.get("require_timed_success"))) and ctx.get("timed_success") is not True:
        hard = False; reasons.append("不是限时成功大秘境")

    score = 1.0 if hard else 0.0
    if hard:
        tilvl = _num(target.get("player_item_level") or target.get("average_item_level"))
        silvl = _num(ctx.get("player_item_level") or ctx.get("average_item_level"))
        if tilvl > 0 and silvl > 0:
            d = abs(tilvl - silvl)
            if d <= max(6.0, tilvl * 0.018): score += 0.20
            elif d <= max(12.0, tilvl * 0.035): score += 0.08
            else: reasons.append("装等差异较大")
        tdur = _num(target.get("duration_s") or target.get("fight_duration_s"))
        sdur = _num(ov.get("duration_s"))
        if tdur > 0 and sdur > 0:
            pct = abs(sdur - tdur) / max(tdur, 1.0)
            if pct <= 0.16: score += 0.20
            elif pct <= 0.30: score += 0.08
            else: reasons.append("完成时间差异较大")
        tpulls = int(_num(target.get("pull_count")))
        spulls = int(_num(ctx.get("pull_count")))
        if tpulls > 0 and spulls > 0:
            if abs(tpulls - spulls) <= 4: score += 0.10
            else: reasons.append("波次数量差异较大")
    return hard, {"score": round(score, 3), "hard_match": hard, "reasons": reasons}


def learn_from_wcl_rankings(
    client: WCLClient,
    *,
    encounter_id: int,
    class_name: str,
    spec_name: str,
    bracket: int | None = None,
    pages: int = 1,
    sample_limit: int = 10,
    server_region: str | None = None,
    evidence_level: str = "standard",
    target_context: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Discover ranking candidates, re-fetch their underlying reports, and learn signatures.

    Ranking JSON is only used for discovery/quality hints. A sample is imported only after
    the underlying report/fight is fetched again and the player's own event stream is read.
    """
    init_online_learning_db(path)
    requested = max(1, min(50, int(sample_limit)))
    pages = max(1, min(5, int(pages)))
    all_candidates: list[dict[str, Any]] = []
    ranking_snapshots: list[int] = []

    def fetch_ranking_pages(bracket_value: int | None, count: int, label: str) -> None:
        for page_no in range(1, count + 1):
            ranking_payload = client.character_rankings(
                int(encounter_id), class_name, spec_name, bracket=bracket_value,
                page=page_no, server_region=server_region or None,
                include_other_players=True, include_combatant_info=True,
            )
            ranking_snapshots.append(save_source_snapshot(
                "wcl_rankings",
                f"encounter:{encounter_id}:{class_name}:{spec_name}:{label}:{bracket_value or 0}:page:{page_no}",
                ranking_payload,
                url="https://www.warcraftlogs.com/",
                title=f"WCL rankings {class_name} {spec_name} encounter {encounter_id}",
                trust_class="primary_log_api",
                context={"encounter_id": encounter_id, "class_name": class_name, "spec_name": spec_name, "bracket": bracket_value, "page": page_no, "mode": label},
                path=path,
            ))
            all_candidates.extend(extract_ranking_candidates(ranking_payload))

    # WCL's `bracket` is a ranking bracket number, which does not always equal the raw
    # keystone level. Try the requested bracket first, then broaden discovery and perform
    # exact key/dungeon/timed filtering against the re-fetched Fight metadata.
    # WCL exposes a zone-specific bracket definition (min/max/bucket). The GraphQL
    # ``bracket`` argument is the 1-based bracket number, not necessarily the raw +key value.
    # Convert the user's keystone level first, retain one raw-value query as a compatibility
    # fallback, and always include unbracketed discovery. Exact Fight metadata remains the
    # final authority for key level / dungeon / timed-success filtering.
    if bracket is not None:
        mapped_bracket = None
        try:
            mapped_bracket = client.ranking_bracket_for_value(int(encounter_id), int(bracket))
        except Exception:
            mapped_bracket = None
        if mapped_bracket:
            fetch_ranking_pages(mapped_bracket, max(1, min(2, pages)), "mapped_keystone_bracket")
        if not mapped_bracket or int(mapped_bracket) != int(bracket):
            fetch_ranking_pages(bracket, 1, "raw_key_compat_hint")
        fetch_ranking_pages(None, max(2, min(4, pages)), "unbracketed_exact_filter")
    else:
        fetch_ranking_pages(None, pages, "unbracketed")
    # Deduplicate while preserving ranking order.
    dedup: list[dict[str, Any]] = []
    seen = set()
    for c in all_candidates:
        k = (c.get("report_code"), int(c.get("fight_id") or 0), int(c.get("source_id") or 0), str(c.get("player_name") or "").lower())
        if k in seen:
            continue
        seen.add(k)
        dedup.append(c)

    imported = 0
    skipped = 0
    failed = 0
    errors: list[str] = []
    samples: list[dict[str, Any]] = []
    report_cache: dict[str, dict[str, Any]] = {}
    candidate_pool = _stratified_candidates(dedup, min(len(dedup), max(requested * 10, 60, requested)))
    for candidate in candidate_pool:
        if imported >= requested:
            break
        code = str(candidate.get("report_code") or "")
        fight_id = int(candidate.get("fight_id") or 0)
        try:
            if code not in report_cache:
                summary = client.report_learning_summary(code)
                report = _report_from_summary(summary)
                report_cache[code] = report
                save_source_snapshot(
                    "wcl_report", code, summary,
                    url=f"https://www.warcraftlogs.com/reports/{code}",
                    title=str(report.get("title") or code), revision=str(report.get("revision") or ""),
                    trust_class="primary_log_api", context={"report_code": code}, path=path,
                )
            report = report_cache[code]
            fight = _find_fight(report, fight_id)
            if not fight:
                skipped += 1
                errors.append(f"{code} fight {fight_id}: 报告里找不到对应 fight")
                continue
            source_id, player_name = _resolve_source_id(report, candidate, class_name)
            if not source_id:
                # playerDetails is a secondary resolver because ranking JSON layouts vary.
                details = client.report_player_details(code, fight_id, True)
                for d in iter_dicts(details):
                    nm = str(_first(d, ["name", "playerName"], "") or "")
                    sid = int(_num(_first(d, ["id", "sourceID", "actorID"], 0)))
                    if sid and (not player_name or nm.lower() == player_name.lower()):
                        source_id, player_name = sid, nm or player_name
                        break
            if not source_id:
                skipped += 1
                errors.append(f"{code} fight {fight_id}: 无法解析玩家 actor/source ID")
                continue

            if has_wcl_knowledge_sample(code, fight_id, source_id, path=path, min_schema_version=5):
                skipped += 1
                continue

            pre_ok, pre_reason = _precheck_target_fight(report, fight, target_context)
            if not pre_ok:
                skipped += 1
                continue

            candidate_pct = _num(candidate.get("percentile"))
            event_bundle = _fetch_learning_events(
                client, code, fight_id, source_id, evidence_level=evidence_level, percentile=candidate_pct
            )
            casts = event_bundle.get("casts") or []
            damage = event_bundle.get("damage") or []
            buffs = event_bundle.get("buffs") or []
            resources = event_bundle.get("resources") or []
            deaths = event_bundle.get("deaths") or []
            if not casts and not damage:
                skipped += 1
                errors.append(f"{code} fight {fight_id}: 没有读取到该玩家的 Cast/Damage 事件")
                continue

            signature = build_wcl_behavior_signature(
                report=report, fight=fight, player_name=player_name, source_id=source_id,
                class_name=class_name, spec_name=spec_name, casts=casts, damage=damage,
                buffs=buffs, resources=resources, deaths=deaths, ranking=candidate,
            )
            hard_match, match_quality = _target_quality(signature, target_context)
            if not hard_match:
                skipped += 1
                errors.append(f"{code} fight {fight_id}: 横向样本已排除（{'、'.join(match_quality.get('reasons') or ['条件不匹配'])}）")
                continue
            percentile = _num(candidate.get("percentile"))
            role = "reference" if percentile >= 90.0 and (not target_context or float(match_quality.get("score") or 0) >= 1.2) else "auto"
            metadata = {
                "source": "wcl_online",
                "provenance": signature.get("provenance"),
                "ranking": signature.get("ranking"),
                "evidence_level": evidence_level,
                "ranking_snapshot_ids": ranking_snapshots,
                "trust_class": "primary_log_api",
                "target_match_quality": match_quality,
                "target_context": target_context or {},
                "candidate_sampling": "stratified_rank_order",
            }
            sample_id = save_knowledge_sample(signature, source="wcl_online", sample_role=role, metadata=metadata, path=path)
            save_source_snapshot(
                "wcl_player_events",
                f"{code}:{fight_id}:{source_id}",
                {"casts": casts, "damage": damage, "buffs": buffs, "resources": resources},
                url=f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}",
                title=f"{player_name} WCL event sample", revision=str(report.get("revision") or ""),
                trust_class="primary_log_api",
                context={"sample_id": sample_id, "class_name": class_name, "spec_name": spec_name, "fight_id": fight_id, "source_id": source_id},
                path=path,
            )
            samples.append({
                "sample_id": sample_id, "report_code": code, "fight_id": fight_id,
                "source_id": source_id, "player": player_name,
                "dungeon": (signature.get("context") or {}).get("dungeon"),
                "key_level": (signature.get("context") or {}).get("key_level"),
                "dps": (signature.get("overview") or {}).get("dps"),
                "average_item_level": (signature.get("context") or {}).get("average_item_level"),
                "game_version": (signature.get("context") or {}).get("game_version"),
                "patch_scope": (signature.get("context") or {}).get("patch_scope"),
                "pull_count": (signature.get("context") or {}).get("pull_count"),
                "percentile": percentile, "sample_role": role,
                "target_match_quality": match_quality,
            })
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code or '?'} fight {fight_id or '?'}: {type(exc).__name__}: {exc}")

    try:
        rate = client.rate_limit()
    except Exception:
        rate = {}
    result = WCLLearningResult(
        requested=requested, candidates_seen=len(dedup), imported=imported,
        skipped=skipped, failed=failed, samples=samples, errors=errors[:50], rate_limit=rate,
    )
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO online_learning_runs(
                created_at, source_type, class_name, spec_name, key_level, requested,
                candidates_seen, imported, skipped, failed, details_json
            ) VALUES (?, 'wcl_rankings', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_utcnow(), class_name, spec_name, int(bracket or 0), requested,
             len(dedup), imported, skipped, failed, _dumps(result.as_dict())),
        )
    return result.as_dict()


def list_online_learning_runs(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    init_online_learning_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM online_learning_runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    return [dict(r) for r in rows]
