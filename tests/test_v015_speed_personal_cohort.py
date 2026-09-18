from __future__ import annotations

import json
from pathlib import Path

import core.ai_client as ai
from core.knowledge import has_wcl_knowledge_sample, save_knowledge_sample
from core.personal_baseline import (
    character_identity,
    latest_personal_model_summary,
    save_personal_model_summary,
    save_personal_sample,
    sync_timed_wcl_personal_baseline,
)
from core.wcl_client import WCLClient


def _sig():
    return {
        "schema_version": 5,
        "context": {
            "player": "Tester", "class_name": "Warrior", "spec_name": "Protection",
            "dungeon": "Example", "key_level": 12, "patch_scope": "12.0.1",
            "player_item_level": 650,
        },
        "overview": {"duration_s": 1200, "casts": 400, "dps": 1000000},
        "skills": [{"spell_name": "Shield Slam", "casts_per_min": 5.0, "damage_pct": 10.0, "hits_per_cast": 1.0, "crit_pct": 20.0, "damage_per_cast": 1000}],
        "responsiveness": {},
        "efficiency": {},
        "provenance": {"report_code": "R1", "fight_id": 1, "source_id": 7},
    }


def test_default_model_uses_v4_flash():
    assert ai.DEFAULT_MODEL == "deepseek-flash"


def test_sanitize_player_markdown_hides_backend_terms():
    out = ai.sanitize_player_markdown("P50 / P90 / casts/min / hits/cast / cadence / percentile")
    for token in ("P50", "P90", "casts/min", "hits/cast", "cadence", "percentile"):
        assert token not in out
    assert "典型水平" in out and "优秀参考范围" in out


def test_fast_coach_pipeline_is_two_calls(monkeypatch):
    calls = []
    def fake_analyze(*args, **kwargs):
        calls.append(("analyze", kwargs.get("reasoning_effort"), kwargs.get("max_tokens")))
        return {"summary": "ok"}
    def fake_write(*args, **kwargs):
        calls.append(("write", kwargs.get("reasoning_effort"), kwargs.get("max_tokens")))
        return "# 报告\n\nP50 casts/min"
    def fail_verify(*args, **kwargs):
        raise AssertionError("fast mode must not call remote verifier")
    monkeypatch.setattr(ai, "analyze_with_deepseek_json", fake_analyze)
    monkeypatch.setattr(ai, "write_player_report_with_deepseek", fake_write)
    monkeypatch.setattr(ai, "verify_player_report_with_deepseek", fail_verify)
    analysis, report, meta = ai.run_coach_pipeline("k", {"x": 1}, speed_mode="fast")
    assert analysis == {"summary": "ok"}
    assert meta["api_passes"] == 2
    assert [x[0] for x in calls] == ["analyze", "write"]
    assert "P50" not in report and "casts/min" not in report


def test_wcl_fork_reuses_token_but_not_session():
    c = WCLClient("id", "secret")
    c._token = "abc"
    c._expires_at = 12345.0
    f = c.fork()
    assert f._token == "abc"
    assert f._expires_at == 12345.0
    assert f._session is not c._session
    assert f._short_cache is c._short_cache


def test_wcl_knowledge_natural_key_can_skip_redownload(tmp_path: Path):
    db = tmp_path / "k.sqlite3"
    sig = _sig()
    save_knowledge_sample(sig, source="wcl_online", metadata={"report_code": "R1", "fight_id": 1, "source_id": 7}, path=db)
    assert has_wcl_knowledge_sample("R1", 1, 7, path=db)


def test_personal_summary_roundtrip(tmp_path: Path):
    db = tmp_path / "p.sqlite3"
    save_personal_model_summary("cn:realm:name", 12, "# 长期总结\n稳定", model="deepseek-flash", scope="同副本", path=db)
    row = latest_personal_model_summary("cn:realm:name", path=db)
    assert row["sample_count"] == 12
    assert "长期总结" in row["summary_markdown"]


class NoFetchClient:
    def report_with_talents(self, code):
        raise AssertionError("known timed fight should be skipped before Report fetch")
    def rate_limit(self):
        return {"limitPerHour": 9000, "pointsSpentThisHour": 1}


def test_timed_personal_sync_skips_known_fight_before_api(tmp_path: Path):
    db = tmp_path / "p.sqlite3"
    profile = {"id": 99, "name": "Tester", "server": {"slug": "realm", "region": {"slug": "cn"}}}
    ident = character_identity(profile)
    sig = _sig()
    save_personal_sample(ident, sig, metadata={"report_code": "R1", "fight_id": 1, "source_id": 7}, path=db)
    run = {"report_code": "R1", "fight_id": 1, "source_id": 7, "player_name": "Tester", "class_name": "Warrior"}
    res = sync_timed_wcl_personal_baseline(NoFetchClient(), profile, [run], max_new_fights=5, path=db)
    assert res.imported == 0
    assert res.skipped_existing == 1
    assert res.reports_scanned == 0

def test_wcl_bracket_mapping_uses_zone_min_bucket(monkeypatch):
    c = WCLClient("id", "secret")
    monkeypatch.setattr(c, "encounter_bracket_info", lambda _eid: {"brackets": {"min": 2, "max": 20, "bucket": 1, "type": "Keystone"}})
    assert c.ranking_bracket_for_value(1, 2) == 1
    assert c.ranking_bracket_for_value(1, 12) == 11
    assert c.ranking_bracket_for_value(1, 99) == 19
