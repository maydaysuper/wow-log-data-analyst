from pathlib import Path

import pandas as pd

from core.knowledge import (
    empirical_cohort_profile,
    init_knowledge_db,
    save_knowledge_sample,
    list_knowledge_samples,
    update_sample_role,
    select_knowledge_samples,
    save_playbook,
    list_playbooks,
)


def _sig(player, dps, cpm, dmg_pct=30.0, dungeon="Test", key=10):
    return {
        "schema_version": 1,
        "context": {"run_label": player, "player": player, "dungeon": dungeon, "key_level": key, "class_name": "Warrior", "spec_name": "Protection", "segment": "All"},
        "overview": {"player": player, "dps": dps},
        "skills": [
            {"spell_id": 1, "spell_name": "Shield Slam", "damage_pct": dmg_pct, "casts_per_min": cpm, "hits_per_cast": 1.0, "crit_pct": 20.0, "avg_hit": 100.0, "damage_per_cast": 100.0},
            {"spell_id": 2, "spell_name": "Thunder Clap", "damage_pct": 20.0, "casts_per_min": 4.0, "hits_per_cast": 4.0, "crit_pct": 20.0, "avg_hit": 80.0, "damage_per_cast": 320.0},
        ],
        "efficiency": {"resource_efficiency": {"avg_resource_pct": 55, "time_at_or_above_90_pct": 5, "overcap_pct_of_generated": 1}, "target_switching": {"switches_per_min": 2}, "pull_downtime": {"inter_pull_downtime_s": 20}},
        "responsiveness": {"score": 10},
        "timeline": {},
    }


def test_save_select_and_profile(tmp_path: Path):
    db = tmp_path / "knowledge.sqlite3"
    init_knowledge_db(db)
    ids = []
    for i, (dps, cpm) in enumerate([(100, 4.0), (110, 4.2), (180, 6.0), (200, 6.4)]):
        ids.append(save_knowledge_sample(_sig(f"P{i}", dps, cpm), path=db))
    update_sample_role(ids[-1], "reference", db)
    update_sample_role(ids[-2], "reference", db)
    rows = select_knowledge_samples("Warrior", "Protection", "Test", 10, 0, path=db)
    assert len(rows) == 4
    profile = empirical_cohort_profile(rows)
    assert profile["sample_count"] == 4
    assert profile["reference_sample_count"] == 2
    shield = next(x for x in profile["skills"] if x["spell_name"] == "Shield Slam")
    assert shield["casts_per_min_p50"] > 0
    assert any(p["spell_name"] == "Shield Slam" and p["metric"] == "casts_per_min" for p in profile["empirical_patterns"])


def test_dedup_and_playbook(tmp_path: Path):
    db = tmp_path / "knowledge.sqlite3"
    sig = _sig("P", 100, 4.0)
    a = save_knowledge_sample(sig, path=db)
    b = save_knowledge_sample(sig, sample_role="reference", path=db)
    assert a == b
    assert len(list_knowledge_samples(path=db)) == 1
    profile = empirical_cohort_profile(list_knowledge_samples(path=db))
    pb_id = save_playbook("Warrior", "Protection", profile, {"summary": "observed"}, "test-model", path=db)
    books = list_playbooks("Warrior", "Protection", path=db)
    assert books and books[0]["id"] == pb_id
