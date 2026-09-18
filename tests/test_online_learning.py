from pathlib import Path

from core.knowledge import list_knowledge_samples
from core.online_learning import (
    build_wcl_behavior_signature,
    extract_ranking_candidates,
    init_online_learning_db,
    learn_from_wcl_rankings,
    list_online_learning_runs,
    list_online_sources,
    online_context_for_analysis,
    save_external_evidence,
    save_source_snapshot,
)


def _report():
    return {
        "code": "ABCDEF123456",
        "title": "Test Run",
        "startTime": 1000000,
        "endTime": 1100000,
        "revision": 3,
        "zone": {"id": 1, "name": "Test Dungeon"},
        "fights": [{
            "id": 7,
            "name": "Test Dungeon",
            "startTime": 1000,
            "endTime": 61000,
            "keystoneLevel": 12,
            "averageItemLevel": 630.5,
            "keystoneTime": 1800000,
            "keystoneBonus": 1,
            "countReached": 100,
            "countRequired": 100,
            "friendlySpecs": [73, 65, 577, 259, 63],
            "dungeonPulls": [{"id": 1, "name": "Trash", "startTime": 1000, "endTime": 10000, "kill": True}],
        }],
        "masterData": {
            "gameVersion": "12.0.1.99999",
            "logVersion": 22,
            "abilities": [
                {"gameID": 100, "name": "Shield Slam"},
                {"gameID": 200, "name": "Thunder Clap"},
                {"gameID": 300, "name": "Avatar"},
            ],
            "actors": [{"id": 5, "name": "Tank", "type": "Player", "subType": "Warrior"}],
        },
    }


def test_ranking_candidate_extraction():
    payload = {
        "worldData": {
            "encounter": {
                "characterRankings": {
                    "rankings": [
                        {"name": "Tank", "report": {"code": "ABCDEF123456"}, "fightID": 7, "sourceID": 5, "rankPercent": 97.2, "amount": 12345},
                        {"name": "Tank2", "reportCode": "ZZZZZZ999999", "fightId": 8, "actorID": 6, "percentile": 91.0},
                    ]
                }
            }
        }
    }
    rows = extract_ranking_candidates(payload)
    assert len(rows) == 2
    assert rows[0]["report_code"] == "ABCDEF123456"
    assert rows[0]["fight_id"] == 7
    assert rows[0]["source_id"] == 5
    assert rows[0]["percentile"] == 97.2


def test_build_wcl_signature():
    report = _report()
    fight = report["fights"][0]
    casts = [
        {"timestamp": 2000, "type": "cast", "abilityGameID": 100, "targetID": 50},
        {"timestamp": 5000, "type": "cast", "abilityGameID": 200, "targetID": 51},
        {"timestamp": 8000, "type": "cast", "abilityGameID": 100, "targetID": 50},
    ]
    damage = [
        {"timestamp": 2100, "type": "damage", "abilityGameID": 100, "targetID": 50, "amount": 1000, "hitType": 2},
        {"timestamp": 5100, "type": "damage", "abilityGameID": 200, "targetID": 51, "amount": 500},
        {"timestamp": 8100, "type": "damage", "abilityGameID": 100, "targetID": 50, "amount": 1000},
    ]
    buffs = [
        {"timestamp": 1000, "type": "applybuff", "abilityGameID": 300},
        {"timestamp": 31000, "type": "removebuff", "abilityGameID": 300},
    ]
    sig = build_wcl_behavior_signature(
        report=report, fight=fight, player_name="Tank", source_id=5,
        class_name="Warrior", spec_name="Protection",
        casts=casts, damage=damage, buffs=buffs, resources=[],
        ranking={"percentile": 99.0, "rank": 1},
    )
    assert sig["context"]["key_level"] == 12
    assert sig["context"]["patch_scope"] == "12.0.1"
    assert sig["context"]["average_item_level"] == 630.5
    assert sig["context"]["pull_count"] == 1
    assert sig["overview"]["total_damage"] == 2500
    shield = next(x for x in sig["skills"] if x["spell_name"] == "Shield Slam")
    assert shield["damage_pct"] == 80.0
    assert shield["casts_per_min"] == 2.0
    avatar = next(x for x in sig["efficiency"]["buff_uptime"] if x["spell_name"] == "Avatar")
    assert 49.9 <= avatar["uptime_pct"] <= 50.1
    assert sig["provenance"]["report_revision"] == 3


class FakeWCL:
    def character_rankings(self, *args, **kwargs):
        return {
            "worldData": {"encounter": {"characterRankings": {"rankings": [
                {"name": "Tank", "report": {"code": "ABCDEF123456"}, "fightID": 7, "sourceID": 5, "rankPercent": 96.0, "rank": 5}
            ]}}},
            "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 10, "pointsResetIn": 100},
        }

    def report_learning_summary(self, code):
        return {"reportData": {"report": _report()}}

    def report_events(self, code, fight_id, data_type, **kwargs):
        if data_type == "Casts":
            return [
                {"timestamp": 2000, "type": "cast", "abilityGameID": 100, "targetID": 50},
                {"timestamp": 5000, "type": "cast", "abilityGameID": 200, "targetID": 51},
            ]
        if data_type == "DamageDone":
            return [
                {"timestamp": 2100, "type": "damage", "abilityGameID": 100, "targetID": 50, "amount": 1000},
                {"timestamp": 5100, "type": "damage", "abilityGameID": 200, "targetID": 51, "amount": 500},
            ]
        if data_type == "Buffs":
            return [
                {"timestamp": 1000, "type": "applybuff", "abilityGameID": 300},
                {"timestamp": 31000, "type": "removebuff", "abilityGameID": 300},
            ]
        return []

    def report_player_details(self, *args, **kwargs):
        return {}

    def rate_limit(self):
        return {"limitPerHour": 3600, "pointsSpentThisHour": 30, "pointsResetIn": 90}


def test_wcl_learning_pipeline_persists_provenance(tmp_path: Path):
    db = tmp_path / "learn.sqlite3"
    init_online_learning_db(db)
    result = learn_from_wcl_rankings(
        FakeWCL(), encounter_id=123, class_name="Warrior", spec_name="Protection",
        bracket=12, pages=1, sample_limit=3, evidence_level="standard", path=db,
    )
    assert result["imported"] == 1
    rows = list_knowledge_samples(path=db)
    assert len(rows) == 1
    assert rows[0]["source"] == "wcl_online"
    assert rows[0]["sample_role"] == "reference"
    sources = list_online_sources(path=db)
    assert {x["source_type"] for x in sources} >= {"wcl_rankings", "wcl_report", "wcl_player_events"}
    runs = list_online_learning_runs(path=db)
    assert runs and runs[0]["imported"] == 1


def test_external_evidence_context(tmp_path: Path):
    db = tmp_path / "learn.sqlite3"
    sid = save_source_snapshot(
        "public_reference", "https://example.com/guide", {"text": "small"},
        url="https://example.com/guide", title="Guide", revision="abc",
        trust_class="community_reference", path=db,
    )
    save_external_evidence(
        sid,
        {"claims": [{"claim": "Use X often", "claim_type": "community_recommendation", "confidence": "medium"}]},
        class_name="Warrior", spec_name="Protection", patch_scope="12.0.1", evidence_kind="guide", model="test", path=db,
    )
    ctx = online_context_for_analysis("Warrior", "Protection", path=db)
    assert len(ctx["items"]) == 1
    assert ctx["items"][0]["trust_class"] == "community_reference"
    assert ctx["items"][0]["patch_scope"] == "12.0.1"
    assert ctx["items"][0]["evidence"]["claims"][0]["claim_type"] == "community_recommendation"

def test_resync_same_wcl_sample_updates_in_place(tmp_path: Path):
    db = tmp_path / "learn.sqlite3"
    first = learn_from_wcl_rankings(
        FakeWCL(), encounter_id=123, class_name="Warrior", spec_name="Protection",
        bracket=12, pages=1, sample_limit=1, evidence_level="light", path=db,
    )
    second = learn_from_wcl_rankings(
        FakeWCL(), encounter_id=123, class_name="Warrior", spec_name="Protection",
        bracket=12, pages=1, sample_limit=1, evidence_level="light", path=db,
    )
    assert first["imported"] == 1 and second["imported"] == 0
    assert second["skipped"] >= 1
    assert len(list_knowledge_samples(path=db)) == 1
