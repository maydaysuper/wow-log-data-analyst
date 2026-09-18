from pathlib import Path

from core.knowledge import (
    init_knowledge_db,
    save_knowledge_sample,
    knowledge_context_for_analysis,
    knowledge_context_for_signatures,
)
from core.online_learning import _target_quality, _stratified_candidates
from core.ai_client import PLAYER_REPORT_SYSTEM_PROMPT, FACT_CHECK_SYSTEM_PROMPT


def _sig(player: str, dungeon: str, key: int, ilvl: float, duration: float, patch: str = "12.0.1", timed: bool = True):
    return {
        "schema_version": 3,
        "context": {
            "run_label": player,
            "player": player,
            "dungeon": dungeon,
            "key_level": key,
            "average_item_level": ilvl,
            "pull_count": 12,
            "class_name": "Warrior",
            "spec_name": "Arms",
            "patch_scope": patch,
            "game_version": patch,
            "timed_success": timed,
        },
        "overview": {"dps": 1000000 + key * 1000, "duration_s": duration},
        "skills": [
            {"spell_id": 1, "spell_name": "Mortal Strike", "damage_pct": 25, "casts_per_min": 8.0, "hits_per_cast": 1.0, "crit_pct": 20, "damage_per_cast": 100},
            {"spell_id": 2, "spell_name": "Execute", "damage_pct": 20, "casts_per_min": 6.0, "hits_per_cast": 1.0, "crit_pct": 20, "damage_per_cast": 90},
        ],
        "efficiency": {"resource_efficiency": {}, "target_switching": {}, "pull_downtime": {}},
        "responsiveness": {"score": 5},
        "provenance": {"report_code": player, "fight_id": 1, "source_id": 1, "patch_scope": patch},
    }


def test_strong_cohort_prefers_similar_ilvl_time_and_timed(tmp_path: Path):
    db = tmp_path / "k.sqlite3"
    init_knowledge_db(db)
    # Four high-quality matches.
    for i in range(4):
        save_knowledge_sample(_sig(f"good{i}", "Dungeon A", 18 + (i % 2), 650 + i, 1800 + i * 20), path=db)
    # Same dungeon/key but context too different; should be excluded by quality tier when 4 good exist.
    save_knowledge_sample(_sig("far-ilvl", "Dungeon A", 18, 710, 1800), path=db)
    save_knowledge_sample(_sig("far-time", "Dungeon A", 18, 650, 2800), path=db)
    # Overtime should not enter the clean cohort.
    save_knowledge_sample(_sig("overtime", "Dungeon A", 18, 650, 1800, timed=False), path=db)

    ctx = knowledge_context_for_analysis(
        "Warrior", "Arms", "Dungeon A", 18, path=db, patch_scope="12.0.1",
        target_context={"dungeon": "Dungeon A", "key_level": 18, "patch_scope": "12.0.1", "average_item_level": 651, "duration_s": 1830, "pull_count": 12},
    )
    details = ctx["cohort_match_details"]
    assert details["tier"] == "strong"
    assert details["sample_count"] == 4
    assert ctx["empirical_profile"]["sample_count"] == 4


def test_multi_fight_cohorts_are_separated_by_dungeon(tmp_path: Path):
    db = tmp_path / "k.sqlite3"
    init_knowledge_db(db)
    for i in range(4):
        save_knowledge_sample(_sig(f"a{i}", "Dungeon A", 18, 650, 1800), path=db)
        save_knowledge_sample(_sig(f"b{i}", "Dungeon B", 18, 650, 1900), path=db)
    selected = [
        _sig("me-a", "Dungeon A", 18, 651, 1810),
        _sig("me-b", "Dungeon B", 18, 652, 1910),
    ]
    out = knowledge_context_for_signatures(selected, path=db)
    assert out["group_count"] == 2
    assert {x["dungeon"] for x in out["cohort_groups"]} == {"Dungeon A", "Dungeon B"}
    assert all((x["cohort"]["cohort_match_details"]["sample_count"] >= 4) for x in out["cohort_groups"])


def test_target_quality_rejects_wrong_dungeon_overtime_or_far_key():
    target = {"dungeon": "Dungeon A", "key_level": 18, "patch_scope": "12.0.1", "average_item_level": 650, "duration_s": 1800}
    ok, meta = _target_quality(_sig("x", "Dungeon A", 19, 653, 1850), target)
    assert ok is True and meta["score"] >= 1.2
    ok, meta = _target_quality(_sig("x", "Dungeon B", 18, 650, 1800), target)
    assert ok is False
    ok, meta = _target_quality(_sig("x", "Dungeon A", 18, 650, 1800, timed=False), target)
    assert ok is False
    ok, meta = _target_quality(_sig("x", "Dungeon A", 22, 650, 1800), target)
    assert ok is False


def test_ranking_candidate_sampling_is_not_only_top_rows():
    rows = [{"rank": i} for i in range(100)]
    picked = _stratified_candidates(rows, 8)
    ranks = [x["rank"] for x in picked]
    assert ranks[:3] == [0, 1, 2]
    assert max(ranks) >= 90
    assert len(ranks) == 8


def test_coach_prompts_require_chinese_actionable_and_fact_checked():
    assert "下一把具体怎么改" in PLAYER_REPORT_SYSTEM_PROMPT
    assert "简体中文" in PLAYER_REPORT_SYSTEM_PROMPT
    assert "死亡" in FACT_CHECK_SYSTEM_PROMPT
    assert "cohort_match_details" in FACT_CHECK_SYSTEM_PROMPT
