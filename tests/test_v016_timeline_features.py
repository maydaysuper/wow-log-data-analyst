from pathlib import Path

from core.wcl_timeline_features import (
    buff_burst_overlap_rows, buff_features, burst_windows_from_damage,
    cast_cadence_rows, cast_duration_rows, pull_breakdown_rows,
)
from core.online_learning import build_wcl_behavior_signature
from core.personal_baseline import build_personal_profile
from core.knowledge import empirical_cohort_profile, save_knowledge_sample, list_knowledge_samples


def test_buff_stack_drop_does_not_close_aura():
    abilities = {1: "测试增益"}
    events = [
        {"timestamp": 0, "type": "applybuff", "abilityGameID": 1},
        {"timestamp": 1000, "type": "applybuffstack", "abilityGameID": 1},
        {"timestamp": 2000, "type": "removebuffstack", "abilityGameID": 1},
        {"timestamp": 5000, "type": "removebuff", "abilityGameID": 1},
    ]
    rows, windows = buff_features(events, 10000, abilities, [(0, 10000)])
    assert rows[0]["combat_uptime_pct"] == 50.0
    assert rows[0]["stack_change_events"] == 2
    assert windows[1] == [(0.0, 5000.0)]


def test_cast_cadence_uses_same_pull_intervals_and_exposes_total_casts():
    abilities = {10: "核心技能"}
    casts = [
        {"timestamp": 1000, "type": "cast", "abilityGameID": 10},
        {"timestamp": 3000, "type": "cast", "abilityGameID": 10},
        {"timestamp": 15000, "type": "cast", "abilityGameID": 10},
    ]
    rows = cast_cadence_rows(casts, abilities, [(0, 5000), (12000, 18000)])
    assert rows[0]["total_casts"] == 3
    assert rows[0]["combat_interval_samples"] == 1
    assert rows[0]["combat_interval_median_s"] == 2.0
    assert rows[0]["pulls_with_cast"] == 2


def test_observed_cast_duration_pairs_begincast_and_cast():
    abilities = {20: "读条技能"}
    casts = [
        {"timestamp": 1000, "type": "begincast", "abilityGameID": 20},
        {"timestamp": 2500, "type": "cast", "abilityGameID": 20},
    ]
    rows = cast_duration_rows(casts, abilities)
    assert rows[0]["observed_casts"] == 1
    assert rows[0]["start_to_cast_median_s"] == 1.5


def test_burst_overlap_and_pull_breakdown_are_built():
    abilities = {1: "增益", 2: "伤害技"}
    buffs = [
        {"timestamp": 0, "type": "applybuff", "abilityGameID": 1},
        {"timestamp": 5000, "type": "removebuff", "abilityGameID": 1},
    ]
    damage = [
        {"timestamp": 1000, "type": "damage", "abilityGameID": 2, "amount": 100},
        {"timestamp": 2000, "type": "damage", "abilityGameID": 2, "amount": 200},
        {"timestamp": 3000, "type": "damage", "abilityGameID": 2, "amount": 300},
    ]
    casts = [{"timestamp": 1500, "type": "cast", "abilityGameID": 2}]
    buff_rows, windows = buff_features(buffs, 10000, abilities, [(0, 8000)])
    bursts = burst_windows_from_damage(damage, abilities, window_s=4, top_n=2)
    overlap = buff_burst_overlap_rows(buff_rows, windows, bursts)
    pulls = [{"name": "第1波"}]
    pull_rows = pull_breakdown_rows(pulls, [(0, 8000)], casts, damage, [], abilities)
    assert bursts
    assert overlap and overlap[0]["burst_overlap_pct"] > 0
    assert pull_rows[0]["casts"] == 1
    assert pull_rows[0]["damage"] == 600.0


def _report_and_fight():
    report = {
        "code": "R1", "revision": 1, "startTime": 0,
        "zone": {"id": 1, "name": "测试副本"},
        "masterData": {
            "gameVersion": "12.0.1", "logVersion": 1,
            "abilities": [
                {"gameID": 1, "name": "测试增益"},
                {"gameID": 2, "name": "核心技能"},
                {"gameID": 3, "name": "读条技能"},
            ],
            "actors": [{"id": 7, "name": "Tester", "type": "Player", "subType": "Warrior"}],
        },
    }
    fight = {
        "id": 1, "name": "测试副本", "startTime": 0, "endTime": 20000,
        "kill": True, "keystoneLevel": 12, "keystoneBonus": 1, "keystoneTime": 30000,
        "friendlyPlayers": [7], "friendlyItemLevels": [650],
        "dungeonPulls": [
            {"name": "第一波", "startTime": 0, "endTime": 9000},
            {"name": "第二波", "startTime": 11000, "endTime": 19000},
        ],
    }
    return report, fight


def _signature():
    report, fight = _report_and_fight()
    casts = [
        {"timestamp": 1000, "type": "cast", "abilityGameID": 2},
        {"timestamp": 3000, "type": "cast", "abilityGameID": 2},
        {"timestamp": 12000, "type": "begincast", "abilityGameID": 3},
        {"timestamp": 13500, "type": "cast", "abilityGameID": 3},
    ]
    damage = [
        {"timestamp": 1500, "type": "damage", "abilityGameID": 2, "amount": 500, "targetID": 100},
        {"timestamp": 3500, "type": "damage", "abilityGameID": 2, "amount": 600, "targetID": 101},
        {"timestamp": 14000, "type": "damage", "abilityGameID": 3, "amount": 700, "targetID": 102},
    ]
    buffs = [
        {"timestamp": 0, "type": "applybuff", "abilityGameID": 1},
        {"timestamp": 8000, "type": "removebuff", "abilityGameID": 1},
    ]
    return build_wcl_behavior_signature(
        report=report, fight=fight, player_name="Tester", source_id=7,
        class_name="Warrior", spec_name="Arms", casts=casts, damage=damage,
        buffs=buffs, resources=[], deaths=[], ranking={},
    )


def test_wcl_signature_contains_timeline_quality_features():
    sig = _signature()
    assert sig["schema_version"] == 5
    assert any(x["spell_name"] == "核心技能" and x["total_casts"] == 2 for x in sig["skills"])
    assert sig["efficiency"]["cast_cadence"]
    assert sig["efficiency"]["cast_duration"]
    assert sig["efficiency"]["buff_uptime"][0]["combat_uptime_pct"] > 0
    assert sig["timeline"]["pull_breakdown"]
    assert sig["timeline"]["burst_windows"]


def test_personal_and_cohort_profiles_keep_buff_and_cadence(tmp_path: Path):
    sig = _signature()
    row = {
        "signature_json": __import__("json").dumps(sig, ensure_ascii=False),
        "dungeon": "测试副本", "spec_name": "Arms", "patch_scope": "12.0.1",
        "key_level": 12, "player_item_level": 650,
    }
    # Two repeated rows are enough for the small-sample recurrence threshold.
    profile = build_personal_profile([row, dict(row)])
    assert profile["buffs"]
    assert profile["cadence"]

    db = tmp_path / "k.sqlite3"
    sig2 = __import__("copy").deepcopy(sig)
    sig2["provenance"]["report_code"] = "R2"
    save_knowledge_sample(sig, source="wcl_online", path=db)
    save_knowledge_sample(sig2, source="wcl_online", path=db)
    rows = list_knowledge_samples(10, path=db)
    cohort = empirical_cohort_profile(rows)
    assert cohort["buffs"]
    assert cohort["cadence"]
