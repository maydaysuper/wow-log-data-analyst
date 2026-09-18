from __future__ import annotations

from collections import Counter
from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def latest_expansion_id(payload: dict[str, Any]) -> int:
    rows = (((payload.get("worldData") or {}).get("expansions")) or [])
    ids = []
    for row in rows:
        try:
            ids.append(int(row.get("id") or 0))
        except Exception:
            pass
    return max(ids) if ids else 0


def looks_like_keystone_bracket(bracket: dict[str, Any] | None) -> bool:
    """Conservative Mythic+ bracket detector that does not depend on English labels.

    WCL's bracket ``type`` is localized, so numeric scale is a useful fallback: WoW
    keystone brackets are small numbers while item-level brackets are hundreds.  The
    label check catches the obvious localized/English cases; the numeric check keeps the
    catalog useful on Chinese WCL responses.
    """
    b = bracket or {}
    label = str(b.get("type") or "").strip().lower()
    tokens = (
        "keystone", "mythic+", "mythic plus", "key level", "key",
        "钥匙", "鑰匙", "秘境", "大秘境", "ключ", "piedra", "clé",
    )
    if any(t in label for t in tokens):
        return True
    mn, mx, bucket = _num(b.get("min")), _num(b.get("max")), _num(b.get("bucket"))
    return bool(bucket > 0 and mx > mn and mx <= 100 and bucket <= 10)


def active_mplus_catalog(
    zones_payload: dict[str, Any],
    *,
    fallback_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract active Mythic+ dungeon encounters from WCL WorldData.

    Active, non-frozen zones with keystone-style brackets are preferred.  If WCL's
    catalog shape changes, recently observed timed runs provide a safe fallback instead
    of hard-coding a season dungeon list into the app.
    """
    zones = (((zones_payload.get("worldData") or {}).get("zones")) or [])
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for zone in zones:
        if bool(zone.get("frozen")):
            continue
        if not looks_like_keystone_bracket(zone.get("brackets") or {}):
            continue
        try:
            zone_id = int(zone.get("id") or 0)
        except Exception:
            zone_id = 0
        for enc in zone.get("encounters") or []:
            try:
                encounter_id = int(enc.get("id") or 0)
            except Exception:
                encounter_id = 0
            name = str(enc.get("name") or "").strip()
            if encounter_id <= 0 or not name or encounter_id in seen:
                continue
            seen.add(encounter_id)
            out.append({
                "encounter_id": encounter_id,
                "dungeon": name,
                "zone_id": zone_id,
                "zone_name": str(zone.get("name") or ""),
                "brackets": dict(zone.get("brackets") or {}),
                "source": "wcl_worlddata_active_keystone_zone",
            })

    if out:
        return out

    # Fallback: infer only from runs the user has actually seen. This is intentionally
    # narrower than claiming a complete season catalog when WorldData did not identify it.
    for run in fallback_runs or []:
        fight = run.get("fight") or {}
        try:
            encounter_id = int(fight.get("encounterID") or 0)
        except Exception:
            encounter_id = 0
        name = str(run.get("dungeon") or fight.get("name") or "").strip()
        if encounter_id <= 0 or not name or encounter_id in seen:
            continue
        seen.add(encounter_id)
        out.append({
            "encounter_id": encounter_id,
            "dungeon": name,
            "zone_id": 0,
            "zone_name": "",
            "brackets": {},
            "source": "recent_timed_run_fallback",
        })
    return out


def dominant_class_spec(runs: list[dict[str, Any]]) -> tuple[str, str]:
    pairs = []
    for run in runs or []:
        c = str(run.get("class_name") or "").strip()
        s = str(run.get("spec_name") or "").strip()
        if c and s:
            pairs.append((c, s))
    if not pairs:
        return "", ""
    return Counter(pairs).most_common(1)[0][0]


def _existing_same_spec_dungeon_counts(class_name: str, spec_name: str, *, path=None) -> dict[str, int]:
    # Local import keeps this lightweight catalog module usable without initializing
    # SQLite until season learning is explicitly requested.
    from .knowledge import list_knowledge_samples
    import json

    counts: Counter[str] = Counter()
    for row in list_knowledge_samples(limit=2500, path=path):
        if str(row.get("sample_role") or "") == "exclude":
            continue
        if str(row.get("class_name") or "").lower() != str(class_name or "").lower():
            continue
        if str(row.get("spec_name") or "").lower() != str(spec_name or "").lower():
            continue
        dungeon = str(row.get("dungeon") or "").strip()
        if not dungeon:
            continue
        try:
            sig = json.loads(row.get("signature_json") or "{}")
        except Exception:
            sig = {}
        if int(sig.get("schema_version") or 0) < 5:
            continue
        if (sig.get("context") or {}).get("timed_success") is not True:
            continue
        if not (sig.get("target_focus") or {}).get("targets"):
            continue
        counts[dungeon.lower()] += 1
    return dict(counts)


def learn_current_season_targets(
    client,
    *,
    class_name: str,
    spec_name: str,
    fallback_runs: list[dict[str, Any]] | None = None,
    samples_per_dungeon: int = 5,
    max_dungeons: int = 12,
    path=None,
) -> dict[str, Any]:
    """Explicitly learn current-season boss/priority-target behavior from WCL.

    This is intentionally user-triggered because it can spend a meaningful number of
    WCL API points on first use.  Already learned schema-v5 fights are reused/skipped.
    Ranking pages only discover candidates; ``learn_from_wcl_rankings`` re-fetches each
    underlying Report/Fight and player event stream before a sample enters the library.
    """
    from .online_learning import learn_from_wcl_rankings
    from .knowledge import dungeon_target_knowledge

    if not class_name or not spec_name:
        raise ValueError("缺少职业/专精，无法建立同专精赛季目标库。")

    expansion_payload = client.world_expansions()
    expansion_id = latest_expansion_id(expansion_payload)
    zones_payload = client.world_zones(expansion_id if expansion_id else None)
    catalog = active_mplus_catalog(zones_payload, fallback_runs=None)
    # Seasonal Mythic+ can include dungeons originating from older expansions. Always
    # inspect all active/non-frozen keystone-style zones and union them by Encounter ID.
    # WorldData is cached for six hours, so this completeness check is cheap on repeats.
    try:
        all_zones_payload = client.world_zones(None)
        all_catalog = active_mplus_catalog(all_zones_payload, fallback_runs=None)
        merged: dict[int, dict[str, Any]] = {}
        for row in catalog + all_catalog:
            eid = int(row.get("encounter_id") or 0)
            if eid:
                merged[eid] = row
        if merged:
            catalog = list(merged.values())
    except Exception:
        pass
    if not catalog:
        catalog = active_mplus_catalog({"worldData": {"zones": []}}, fallback_runs=fallback_runs)
    if not catalog:
        raise ValueError("WCL WorldData 暂时没有识别到当前赛季大秘境目录。请先查询角色并扫描最近限时场次后重试。")

    catalog_source = str(catalog[0].get("source") or "")
    catalog_complete = catalog_source == "wcl_worlddata_active_keystone_zone"
    existing = _existing_same_spec_dungeon_counts(class_name, spec_name, path=path)
    target_n = max(2, min(12, int(samples_per_dungeon)))
    results: list[dict[str, Any]] = []
    imported_total = skipped_total = failed_total = 0

    for item in catalog[: max(1, int(max_dungeons))]:
        dungeon = str(item.get("dungeon") or "").strip()
        encounter_id = int(item.get("encounter_id") or 0)
        if not dungeon or encounter_id <= 0:
            continue
        have = int(existing.get(dungeon.lower()) or 0)
        if have >= target_n:
            profile = dungeon_target_knowledge(dungeon, path=path)
            results.append({
                "dungeon": dungeon, "encounter_id": encounter_id, "status": "cached_enough",
                "existing_before": have, "imported": 0, "skipped": 0, "failed": 0,
                "knowledge_sample_count": int(profile.get("sample_count") or 0),
                "boss_count": len(profile.get("bosses") or []),
                "priority_count": len(profile.get("priority_targets") or []),
                "important_count": len(profile.get("important_targets") or []),
            })
            continue
        want = max(2, target_n - have)
        try:
            learned = learn_from_wcl_rankings(
                client,
                encounter_id=encounter_id,
                class_name=class_name,
                spec_name=spec_name,
                bracket=None,
                pages=3,
                sample_limit=want,
                evidence_level="standard",
                target_context={
                    "dungeon": dungeon,
                    "require_timed_success": True,
                    "season_target_learning": True,
                },
                path=path,
            )
            imported = int(learned.get("imported") or 0)
            skipped = int(learned.get("skipped") or 0)
            failed = int(learned.get("failed") or 0)
            imported_total += imported; skipped_total += skipped; failed_total += failed
            profile = dungeon_target_knowledge(dungeon, path=path)
            results.append({
                "dungeon": dungeon, "encounter_id": encounter_id, "status": "ok",
                "existing_before": have, "imported": imported, "skipped": skipped,
                "failed": failed, "errors": (learned.get("errors") or [])[:3],
                "knowledge_sample_count": int(profile.get("sample_count") or 0),
                "boss_count": len(profile.get("bosses") or []),
                "priority_count": len(profile.get("priority_targets") or []),
                "important_count": len(profile.get("important_targets") or []),
            })
        except Exception as exc:
            failed_total += 1
            results.append({
                "dungeon": dungeon, "encounter_id": encounter_id, "status": "failed",
                "existing_before": have, "imported": 0, "skipped": 0, "failed": 1,
                "errors": [str(exc)[:300]],
            })

    rate = {}
    try:
        rate = client.rate_limit() or {}
    except Exception:
        pass
    return {
        "class_name": class_name,
        "spec_name": spec_name,
        "expansion_id": expansion_id,
        "catalog_complete": catalog_complete,
        "catalog_source": catalog_source,
        "dungeon_count": len(results),
        "imported": imported_total,
        "skipped": skipped_total,
        "failed": failed_total,
        "results": results,
        "rate_limit": rate,
        "warning": (
            "重要目标来自多场限时WCL行为学习；它描述参考玩家经常优先投入伤害的目标，"
            "不等于暴雪官方机制标签或固定击杀顺序。"
        ),
    }
