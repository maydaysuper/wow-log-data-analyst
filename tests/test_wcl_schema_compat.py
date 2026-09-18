from pathlib import Path


def test_report_map_query_uses_only_supported_id_field():
    text = (Path(__file__).parents[1] / "core" / "wcl_client.py").read_text(encoding="utf-8")
    assert "maps { id name }" not in text
    assert text.count("maps { id }") >= 2
