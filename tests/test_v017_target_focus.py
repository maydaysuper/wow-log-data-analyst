from __future__ import annotations

from pathlib import Path

from core.target_focus import (
    aggregate_target_knowledge,
    build_target_focus_profile,
    compare_target_profiles,
)
from core.season_targets import (
    active_mplus_catalog,
    dominant_class_spec,
    latest_expansion_id,
    looks_like_keystone_bracket,
    learn_current_season_targets,
)


def _report():
    return {
        "masterData": {
            "actors": [
                {"id": 10, "name": "最终首领", "type": "NPC", "subType": "Boss", "gameID": 100},
                {"id": 20, "name": "危险施法者", "type": "NPC", "subType": "NPC", "gameID": 200},
                {"id": 30, "name": "普通杂兵", "type": "NPC", "subType": "NPC", "gameID": 300},
            ]
        }
    }


def _fight():
    return {
        "startTime": 100000,
        "endTime": 160000,
        "dungeonPulls": [
            {"id": 1, "name": "第1波", "startTime": 100000, "endTime": 120000, "encounterID": 0,
             "enemyNPCs": [{"gameID": 200}, {"gameID": 300}]},
            {"id": 2, "name": "Boss", "startTime": 130000, "endTime": 160000, "encounterID": 999,
             "enemyNPCs": [{"gameID": 100}]},
        ],
    }


def test_target_focus_profile_tracks_boss_priority_evidence():
    casts = [
        {"timestamp": 1000, "type": "cast", "abilityGameID": 1, "targetID": 20},
        {"timestamp": 2500, "type": "cast", "abilityGameID": 2, "targetID": 20},
        {"timestamp": 9000, "type": "cast", "abilityGameID": 3, "targetID": 30},
        {"timestamp": 32000, "type": "cast", "abilityGameID": 1, "targetID": 10},
    ]
    damage = [
        {"timestamp": 1100, "amount": 300, "targetID": 20},
        {"timestamp": 3000, "amount": 300, "targetID": 20},
        {"timestamp": 10000, "amount": 100, "targetID": 30},
        {"timestamp": 33000, "amount": 600, "targetID": 10},
    ]
    p = build_target_focus_profile(_report(), _fight(), casts, damage, fight_start_ms=100000)
    by = {r["npc_id"]: r for r in p["targets"]}
    assert by[100]["is_boss"] is True
    assert by[200]["early_targeted_casts"] == 2
    assert by[200]["damage"] == 600
    assert by[300]["damage"] == 100
    assert p["boss_target_count"] == 1


def _learned_sig(priority_share=20.0, boss_share=40.0):
    return {
        "target_focus": {
            "targets": [
                {"npc_id": 100, "npc_name": "最终首领", "is_boss": True, "seen_in_boss_pull": True,
                 "damage_share_pct": boss_share, "target_dps_over_run": 40000, "targeted_cast_share_pct": 30,
                 "early_targeted_cast_share_pct": 20, "early_damage_share_pct": 20, "first_hit_delay_median_s": 1.0},
                {"npc_id": 200, "npc_name": "危险施法者", "is_boss": False, "seen_in_boss_pull": False,
                 "damage_share_pct": priority_share, "target_dps_over_run": 20000, "targeted_cast_share_pct": 28,
                 "early_targeted_cast_share_pct": 45, "early_damage_share_pct": 35, "first_hit_delay_median_s": 0.7},
                {"npc_id": 300, "npc_name": "普通杂兵", "is_boss": False, "seen_in_boss_pull": False,
                 "damage_share_pct": 3, "target_dps_over_run": 3000, "targeted_cast_share_pct": 3,
                 "early_targeted_cast_share_pct": 2, "early_damage_share_pct": 2, "first_hit_delay_median_s": 5.0},
            ]
        }
    }


def test_aggregate_learns_priority_target_and_comparison():
    ref = aggregate_target_knowledge([_learned_sig() for _ in range(8)], min_samples=2)
    assert ref["sample_count"] == 8
    assert any(r["npc_id"] == 100 for r in ref["bosses"])
    priority = next(r for r in ref["all_targets"] if r["npc_id"] == 200)
    assert priority["learned_role"] == "priority_focus"
    assert priority["confidence"] == "high"

    current = _learned_sig(priority_share=10.0)["target_focus"]
    cmp = compare_target_profiles(current, ref, ref)
    row = next(r for r in cmp["rows"] if r["npc_id"] == 200)
    assert row["same_spec_reference_available"] is True
    assert "低于同专精" in row["comparison_label"]
    assert cmp["chart_rows"]


def test_role_library_can_identify_target_without_same_spec_metric():
    dungeon_roles = aggregate_target_knowledge([_learned_sig() for _ in range(8)], min_samples=2)
    same_spec = {"sample_count": 2, "all_targets": [r for r in dungeon_roles["all_targets"] if r["npc_id"] == 100]}
    current = _learned_sig(priority_share=12.0)["target_focus"]
    cmp = compare_target_profiles(current, same_spec, dungeon_roles)
    row = next(r for r in cmp["rows"] if r["npc_id"] == 200)
    assert row["role"] == "priority_focus"
    assert row["same_spec_reference_available"] is False
    assert "暂缺同专精" in row["comparison_label"]


def test_active_mplus_catalog_uses_nonfrozen_keystone_zone():
    payload = {"worldData": {"zones": [
        {"id": 1, "name": "Raid", "frozen": False, "brackets": {"min": 620, "max": 700, "bucket": 5, "type": "Item Level"},
         "encounters": [{"id": 10, "name": "Raid Boss"}]},
        {"id": 2, "name": "Mythic+ Season", "frozen": False, "brackets": {"min": 2, "max": 25, "bucket": 1, "type": "钥匙层数"},
         "encounters": [{"id": 20, "name": "副本A"}, {"id": 21, "name": "副本B"}]},
        {"id": 3, "name": "Old", "frozen": True, "brackets": {"min": 2, "max": 20, "bucket": 1, "type": "Keystone"},
         "encounters": [{"id": 30, "name": "旧副本"}]},
    ]}}
    rows = active_mplus_catalog(payload)
    assert [r["encounter_id"] for r in rows] == [20, 21]
    assert all(r["source"] == "wcl_worlddata_active_keystone_zone" for r in rows)
    assert looks_like_keystone_bracket({"min": 2, "max": 20, "bucket": 1, "type": ""})


def test_catalog_helpers():
    assert latest_expansion_id({"worldData": {"expansions": [{"id": 4}, {"id": 7}, {"id": 6}]}}) == 7
    assert dominant_class_spec([
        {"class_name": "Warrior", "spec_name": "Arms"},
        {"class_name": "Warrior", "spec_name": "Arms"},
        {"class_name": "Warrior", "spec_name": "Fury"},
    ]) == ("Warrior", "Arms")


class _FakeClient:
    def world_expansions(self):
        return {"worldData": {"expansions": [{"id": 9, "name": "Now"}]}}

    def world_zones(self, expansion_id=None):
        assert expansion_id == 9
        return {"worldData": {"zones": [{
            "id": 70, "name": "M+ Season", "frozen": False,
            "brackets": {"min": 2, "max": 30, "bucket": 1, "type": "Keystone"},
            "encounters": [{"id": 701, "name": "副本A"}],
        }]}}

    def rate_limit(self):
        return {"limitPerHour": 9000, "pointsSpentThisHour": 12}


def test_season_learning_is_explicit_and_uses_underlying_learning(monkeypatch, tmp_path: Path):
    calls = []
    def fake_learn(client, **kwargs):
        calls.append(kwargs)
        assert kwargs["target_context"]["require_timed_success"] is True
        return {"imported": 3, "skipped": 1, "failed": 0, "errors": []}
    monkeypatch.setattr("core.online_learning.learn_from_wcl_rankings", fake_learn)
    out = learn_current_season_targets(
        _FakeClient(), class_name="Warrior", spec_name="Arms", samples_per_dungeon=3, path=tmp_path / "k.sqlite3"
    )
    assert out["catalog_complete"] is True
    assert out["dungeon_count"] == 1
    assert out["imported"] == 3
    assert calls and calls[0]["encounter_id"] == 701


def test_multi_selected_runs_can_compare_aggregated_current_profile():
    current = aggregate_target_knowledge([_learned_sig(priority_share=10.0), _learned_sig(priority_share=12.0)], min_samples=1)
    ref = aggregate_target_knowledge([_learned_sig(priority_share=20.0) for _ in range(8)], min_samples=2)
    cmp = compare_target_profiles(current, ref, ref)
    row = next(r for r in cmp["rows"] if r["npc_id"] == 200)
    assert 10 <= row["current_damage_share_pct"] <= 12
    assert "低于同专精" in row["comparison_label"]


def test_priority_scoring_uses_focus_within_presence_pull_not_whole_dungeon():
    sigs = []
    for _ in range(6):
        # The dangerous NPC is only a small share of the whole dungeon, but in the one
        # pull where it exists it receives most early direct focus. This is the common
        # M+ case that global-run percentages used to dilute away.
        sigs.append({"target_focus": {"targets": [
            {"npc_id": 220, "npc_name": "危险大怪", "is_boss": False, "damage_share_pct": 4,
             "target_dps_over_run": 4000, "targeted_cast_share_pct": 3, "early_targeted_cast_share_pct": 3,
             "early_damage_share_pct": 3, "pull_damage_share_when_present_pct": 34,
             "targeted_cast_share_when_present_pct": 42, "early_focus_when_present_pct": 68,
             "early_damage_when_present_pct": 51, "first_hit_delay_median_s": .5},
        ]}})
    learned = aggregate_target_knowledge(sigs, min_samples=2)
    row = next(r for r in learned["all_targets"] if r["npc_id"] == 220)
    assert row["learned_role"] == "priority_focus"
    assert row["early_focus_when_present_pct"]["p50"] == 68
