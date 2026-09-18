from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _compact_fight(sig: dict[str, Any], personal: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = sig.get("context") or {}
    ov = sig.get("overview") or {}
    resp = sig.get("responsiveness") or {}
    direct = sig.get("direct_fight_evidence") or {}
    eff = sig.get("efficiency") or {}
    timeline = sig.get("timeline") or {}
    skills = []
    for s in (sig.get("skills") or [])[:18]:
        skills.append({
            "spell_name": s.get("spell_name"),
            "total_casts": s.get("total_casts"),
            "damage_pct": s.get("damage_pct"),
            "casts_per_min": s.get("casts_per_min"),
            "hits_per_cast": s.get("hits_per_cast"),
            "crit_pct": s.get("crit_pct"),
            "damage_per_cast": s.get("damage_per_cast"),
        })
    return {
        "context": ctx,
        "overview": {
            "duration_s": ov.get("duration_s"), "dps": ov.get("dps"), "casts": ov.get("casts"),
            "damage": ov.get("damage"),
        },
        "skills": skills,
        "responsiveness": {
            "score": resp.get("score"),
            "longest_gap_s": resp.get("longest_gap_s") or resp.get("max_cast_gap_s"),
            "team_activity_stalls": direct.get("team_activity_stalls") or resp.get("team_activity_stalls"),
        },
        "efficiency": {
            "buff_uptime": (eff.get("buff_uptime") or [])[:12],
            "cast_cadence": (eff.get("cast_cadence") or [])[:14],
            "cast_duration": (eff.get("cast_duration") or [])[:8],
            "buff_burst_overlap": (eff.get("buff_burst_overlap") or [])[:10],
            "resource_efficiency": eff.get("resource_efficiency") or {},
        },
        "timeline": {
            "burst_windows": (timeline.get("burst_windows") or [])[:6],
            "pull_breakdown": (timeline.get("pull_breakdown") or [])[:24],
        },
        "direct_fight_evidence": {
            "interrupt_count": direct.get("interrupt_count"),
            "death_count": direct.get("death_count"),
            "top_damage_taken": direct.get("top_damage_taken"),
        },
        "target_focus": {
            "boss_damage_share_pct": (sig.get("target_focus") or {}).get("boss_damage_share_pct"),
            "targets": ((sig.get("target_focus") or {}).get("targets") or [])[:18],
        },
        "personal_comparison": personal or {},
    }


def build_multi_fight_payload(
    signatures: list[dict[str, Any]],
    personal_comparisons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a bounded multi-fight payload for recurring-pattern analysis.

    It intentionally keeps deterministic aggregates separate from AI interpretation.
    """
    pcs = list(personal_comparisons or [])
    fights = [_compact_fight(sig, pcs[i] if i < len(pcs) else {}) for i, sig in enumerate(signatures)]
    skill_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sig in signatures:
        for s in sig.get("skills") or []:
            name = str(s.get("spell_name") or s.get("spell_id") or "Unknown")
            skill_bucket[name].append(s)

    repeated = []
    n = max(1, len(signatures))
    for name, rows in skill_bucket.items():
        if len(rows) < max(2, (n + 1) // 2):
            continue
        repeated.append({
            "spell_name": name,
            "present_in_fights": len(rows),
            "fight_share_pct": round(len(rows) / n * 100, 1),
            "avg_damage_pct": round(mean(_num(r.get("damage_pct")) for r in rows), 3),
            "avg_casts_per_min": round(mean(_num(r.get("casts_per_min")) for r in rows), 4),
            "min_casts_per_min": round(min(_num(r.get("casts_per_min")) for r in rows), 4),
            "max_casts_per_min": round(max(_num(r.get("casts_per_min")) for r in rows), 4),
        })
    repeated.sort(key=lambda x: (x["avg_damage_pct"], x["present_in_fights"]), reverse=True)

    recurring_personal = defaultdict(int)
    for pc in pcs:
        for f in pc.get("findings") or []:
            if not isinstance(f, dict):
                continue
            key = str(f.get("metric") or f.get("finding") or f.get("evidence") or "").strip()
            if key:
                recurring_personal[key] += 1

    contexts = [sig.get("context") or {} for sig in signatures]

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for sig in signatures:
        ctx = sig.get("context") or {}
        key = (
            str(ctx.get("dungeon") or "未知副本"),
            str(ctx.get("spec_name") or ""),
            str(ctx.get("patch_scope") or ctx.get("game_version") or ""),
        )
        grouped[key].append(sig)
    comparison_groups = []
    for (dungeon, spec, patch), group in grouped.items():
        keys = [int((g.get("context") or {}).get("key_level") or 0) for g in group if int((g.get("context") or {}).get("key_level") or 0)]
        comparison_groups.append({
            "dungeon": dungeon,
            "spec_name": spec,
            "patch_scope": patch,
            "fight_count": len(group),
            "key_levels": keys,
            "min_key": min(keys) if keys else 0,
            "max_key": max(keys) if keys else 0,
            "all_timed_success": all((g.get("context") or {}).get("timed_success") is True for g in group),
        })
    comparison_groups.sort(key=lambda x: x["fight_count"], reverse=True)
    return {
        "analysis_request": {
            "mode": "multi_wcl_fights",
            "fight_count": len(signatures),
            "goal": "综合多场战斗，优先识别重复性问题、单场偶发异常、本人长期习惯，以及与同职业同专精参考样本的稳定差距。",
        },
        "batch_context": {
            "players": sorted({str(c.get("player") or "") for c in contexts if c.get("player")}),
            "class_names": sorted({str(c.get("class_name") or "") for c in contexts if c.get("class_name")}),
            "spec_names": sorted({str(c.get("spec_name") or "") for c in contexts if c.get("spec_name")}),
            "dungeons": sorted({str(c.get("dungeon") or "") for c in contexts if c.get("dungeon")}),
            "key_levels": [int(c.get("key_level") or 0) for c in contexts],
            "patches": sorted({str(c.get("patch_scope") or c.get("game_version") or "") for c in contexts if c.get("patch_scope") or c.get("game_version")}),
        },
        "fights": fights,
        "comparison_groups": comparison_groups,
        "repeated_skill_behavior": repeated[:30],
        "recurring_personal_findings": [
            {"finding_key": k, "fight_count": v, "fight_share_pct": round(v / n * 100, 1)}
            for k, v in sorted(recurring_personal.items(), key=lambda kv: kv[1], reverse=True)[:20]
        ],
        "guardrails": [
            "多场重复出现比单场偶发更适合判断长期习惯。",
            "DPS和技能占比会受副本、路线、怪量、层数、装备与队伍影响，横向比较必须结合职业样本上下文。",
            "响应空档不能单独证明网络问题；只有玩家存活且处于战斗窗口的停手才可计入响应异常。",
            "如果选中的场次来自不同副本，不要把副本间DPS或技能占比直接混成一个横向结论；长期重复习惯可以跨副本观察，但同职业基准应按副本/版本/相近层数分组。",
        ],
    }
