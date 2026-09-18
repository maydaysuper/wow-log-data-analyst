from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.knowledge import save_knowledge_sample, list_knowledge_samples
from core.personal_baseline import character_identity, save_personal_sample, list_personal_samples
from core.wcl_client import WCLClient


def _character():
    return {
        "id": 123,
        "canonicalID": "cn:realm:mayday",
        "name": "Mayday",
        "server": {"slug": "realm", "region": {"slug": "cn"}},
    }


def _sig():
    return {
        "schema_version": 3,
        "context": {"player": "Mayday", "dungeon": "Dungeon A", "key_level": 18,
                    "class_name": "Warrior", "spec_name": "Protection", "patch_scope": "12.0.1"},
        "overview": {"duration_s": 300, "casts": 100, "dps": 1000},
        "skills": [], "efficiency": {}, "responsiveness": {},
        "provenance": {"report_code": "ABC123", "fight_id": 1, "source_id": 10},
    }


def test_parallel_personal_save_is_idempotent(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    ident = character_identity(_character())
    sig = _sig()
    with ThreadPoolExecutor(max_workers=6) as ex:
        ids = list(ex.map(lambda _: save_personal_sample(ident, sig, path=db), range(12)))
    assert len(set(ids)) == 1
    assert len(list_personal_samples(ident["character_key"], path=db)) == 1


def test_parallel_knowledge_save_is_idempotent(tmp_path: Path):
    db = tmp_path / "x.sqlite3"
    sig = _sig()
    with ThreadPoolExecutor(max_workers=6) as ex:
        ids = list(ex.map(lambda _: save_knowledge_sample(sig, source="personal_wcl", path=db), range(12)))
    assert len(set(ids)) == 1
    assert len(list_knowledge_samples(path=db)) == 1


def test_wcl_http_retries_post_requests():
    cli = WCLClient("id", "secret")
    try:
        retry = cli._session.get_adapter("https://").max_retries
        assert retry.total == 3
        assert "POST" in retry.allowed_methods
        assert 429 in retry.status_forcelist
        assert 503 in retry.status_forcelist
    finally:
        cli.close()
