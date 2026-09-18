from pathlib import Path

from core.personal_baseline import (
    best_personal_profile_for_signature,
    build_personal_profile,
    character_identity,
    compare_to_personal_profile,
    list_personal_samples,
    save_personal_sample,
    sync_recent_wcl_personal_baseline,
)


def _character():
    return {
        "id": 123,
        "canonicalID": "cn:realm:mayday",
        "name": "Mayday",
        "server": {"slug": "realm", "name": "Realm", "region": {"slug": "cn", "name": "China"}},
    }


def _sig(i: int, *, cpm: float = 5.0, resp: float = 10.0, patch: str = "12.0.1", dungeon: str = "Dungeon A"):
    return {
        "schema_version": 3,
        "context": {
            "player": "Mayday", "dungeon": dungeon, "key_level": 18 + (i % 2),
            "class_name": "Warrior", "spec_name": "Protection", "patch_scope": patch,
            "player_item_level": 700 + i,
        },
        "overview": {"duration_s": 300, "casts": int(cpm * 5), "dps": 2_000_000 + i * 10_000},
        "skills": [
            {"spell_name": "Shield Slam", "casts_per_min": cpm, "damage_pct": 20 + i * .1, "hits_per_cast": 1, "crit_pct": 20, "damage_per_cast": 1000},
            {"spell_name": "Thunder Clap", "casts_per_min": 4.0, "damage_pct": 18, "hits_per_cast": 5, "crit_pct": 15, "damage_per_cast": 2000},
        ],
        "efficiency": {
            "resource_efficiency": {"time_at_or_above_90_pct": 8 + i * .1, "time_at_or_below_10_pct": 4, "avg_resource_pct": 45, "overcap_pct_of_generated": 2},
            "target_switching": {"switches_per_min": 3.0},
        },
        "responsiveness": {"score": resp, "cast_gap_median_s": 1.3, "cast_gap_p95_s": 1.8, "max_cast_gap_s": 3.0, "long_gap_count": 0, "severe_gap_count": 0},
        "provenance": {"report_code": f"R{i}", "fight_id": i + 1, "source_id": 10, "patch_scope": patch},
    }


def test_personal_identity_and_dedupe(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    ident = character_identity(_character())
    assert ident["character_key"]
    a = save_personal_sample(ident, _sig(1), path=db)
    b = save_personal_sample(ident, _sig(1), path=db)
    assert a == b
    assert len(list_personal_samples(ident["character_key"], path=db)) == 1


def test_build_profile_and_detect_personal_outlier(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    ident = character_identity(_character())
    for i in range(10):
        save_personal_sample(ident, _sig(i, cpm=5.0 + (i % 3) * .1, resp=8 + i * .2), path=db)
    rows = list_personal_samples(ident["character_key"], path=db)
    profile = build_personal_profile(rows)
    assert profile["sample_count"] == 10
    assert profile["confidence"] == "medium"
    shield = next(x for x in profile["skills"] if x["spell_name"] == "Shield Slam")
    assert shield["casts_per_min"]["p50"] >= 5

    current = _sig(50, cpm=2.2, resp=70)
    current["responsiveness"].update({"cast_gap_p95_s": 5.5, "max_cast_gap_s": 9.0, "severe_gap_count": 2})
    cmp = compare_to_personal_profile(current, profile)
    assert cmp["anomaly_score"] >= 30
    assert any(x["type"] == "skill_frequency_low" for x in cmp["findings"])
    assert any(x["type"] == "responsiveness_outlier" for x in cmp["findings"])


def test_best_profile_prefers_conditioned_history(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    ident = character_identity(_character())
    for i in range(6):
        save_personal_sample(ident, _sig(i, patch="12.0.1", dungeon="Dungeon A"), path=db)
    for i in range(6, 12):
        save_personal_sample(ident, _sig(i, patch="11.2.7", dungeon="Old Dungeon"), path=db)
    current = _sig(99, patch="12.0.1", dungeon="Dungeon A")
    profile = best_personal_profile_for_signature(ident["character_key"], current, path=db)
    assert profile["sample_count"] >= 5
    assert profile["scope"].startswith("同专精 + 同版本 + 同副本")


class FakeWCL:
    def __init__(self):
        self.event_calls = 0

    def report_with_talents(self, code):
        return {"reportData": {"report": {
            "code": code, "revision": 1, "startTime": 0,
            "zone": {"id": 1, "name": "Dungeon A"},
            "masterData": {
                "gameVersion": "12.0.1", "logVersion": 30,
                "actors": [{"id": 10, "name": "Mayday", "type": "Player", "subType": "Warrior", "server": {"slug": "realm"}}],
                "abilities": [{"gameID": 100, "name": "Shield Slam"}],
            },
            "fights": [{
                "id": 1, "name": "Dungeon A", "startTime": 0, "endTime": 300000,
                "kill": True, "keystoneLevel": 18, "keystoneTime": 300000,
                "friendlyPlayers": [10], "friendlySpecs": ["Protection"], "friendlyItemLevels": [700],
                "averageItemLevel": 700, "dungeonPulls": [],
            }],
        }}}

    def report_events(self, code, fight_id, data_type, **kwargs):
        self.event_calls += 1
        if data_type == "Casts":
            return [{"timestamp": t, "abilityGameID": 100, "type": "cast", "targetID": 20} for t in range(1000, 290000, 2000)]
        if data_type == "DamageDone":
            return [{"timestamp": t, "abilityGameID": 100, "amount": 1000, "targetID": 20} for t in range(1000, 290000, 2000)]
        return []

    def rate_limit(self):
        return {"limitPerHour": 3600, "pointsSpentThisHour": 100, "pointsResetIn": 1200}


def test_sync_recent_personal_baseline_is_idempotent(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    fake = FakeWCL()
    reports = [{"code": "ABC123"}]
    first = sync_recent_wcl_personal_baseline(fake, _character(), reports, max_new_fights=5, path=db)
    assert first.imported == 1
    calls = fake.event_calls
    second = sync_recent_wcl_personal_baseline(fake, _character(), reports, max_new_fights=5, path=db)
    assert second.imported == 0
    assert second.skipped_existing == 1
    # Existing fights are skipped before expensive event streams are fetched again.
    assert fake.event_calls == calls
