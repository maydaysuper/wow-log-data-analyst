from core.wcl_identity import parse_character_locator
from core.wcl_direct_analysis import resolve_report_actor, fights_for_source, spec_and_item_level_for_fight


def sample_report():
    return {
        "masterData": {
            "actors": [
                {"id": 1, "name": "Mayday", "type": "Player", "subType": "Warrior", "server": "illidan"},
                {"id": 2, "name": "Other", "type": "Player", "subType": "Priest", "server": "illidan"},
            ],
            "abilities": [],
        },
        "fights": [
            {"id": 10, "friendlyPlayers": [1,2], "friendlySpecs": ["Protection","Holy"], "friendlyItemLevels": [650,648]},
            {"id": 11, "friendlyPlayers": [2], "friendlySpecs": ["Holy"], "friendlyItemLevels": [648]},
        ]
    }


def test_parse_numeric_id():
    loc=parse_character_locator("123456")
    assert loc.character_id == 123456
    assert loc.is_id


def test_parse_character_url():
    loc=parse_character_locator("https://www.warcraftlogs.com/character/cn/illidan/Mayday")
    assert loc.server_region == "cn"
    assert loc.server_slug == "illidan"
    assert loc.name == "Mayday"


def test_parse_manual_locator():
    loc=parse_character_locator("cn:illidan:Mayday")
    assert (loc.server_region,loc.server_slug,loc.name) == ("cn","illidan","Mayday")


def test_resolve_actor_and_fights():
    report=sample_report()
    sid,name,cls=resolve_report_actor(report,character_name="Mayday",server_slug="illidan")
    assert (sid,name,cls)==(1,"Mayday","Warrior")
    fights=fights_for_source(report,sid)
    assert [x["id"] for x in fights] == [10]
    assert spec_and_item_level_for_fight(fights[0],sid) == ("Protection",650)


def test_team_activity_stalls_marks_only_player_specific_gaps():
    from core.wcl_direct_analysis import team_activity_stalls
    player = [
        {"timestamp": 1000, "sourceID": 10},
        {"timestamp": 7000, "sourceID": 10},
        {"timestamp": 9000, "sourceID": 10},
    ]
    team = [
        {"timestamp": 2000, "sourceID": 11},
        {"timestamp": 3000, "sourceID": 12},
        {"timestamp": 4000, "sourceID": 11},
        {"timestamp": 5000, "sourceID": 13},
        {"timestamp": 8000, "sourceID": 11},
    ]
    damage = [{"timestamp": 3500, "sourceID": 10}]
    out = team_activity_stalls(player, team, damage, source_id=10, friendly_ids={10, 11, 12, 13, 14})
    assert out["isolated_gap_count"] == 1
    assert out["severe_isolated_gap_count"] == 1
    assert out["max_isolated_gap_s"] == 6.0
    assert out["windows"][0]["other_friendly_casters"] == 3
    assert out["windows"][0]["player_damage_events_during_gap"] == 1


def test_parse_chinese_character_url_and_unicode_name():
    loc = parse_character_locator(
        "https://www.warcraftlogs.com/character/cn/%E7%99%BD%E9%93%B6%E4%B9%8B%E6%89%8B/%E4%B8%8D%E6%98%AF%E9%85%92%E9%AC%BC%E4%B8%B6"
    )
    assert loc.server_region == "cn"
    assert loc.server_slug == "白银之手"
    assert loc.name == "不是酒鬼丶"


def test_character_recent_reports_id_query_uses_only_id_variables():
    from core.wcl_client import WCLClient
    cli = WCLClient("x", "y")
    captured = {}

    def fake_query(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables
        return {"characterData": {"character": {}}, "rateLimitData": {}}

    cli.query = fake_query
    cli.character_recent_reports(character_id=123, limit=30)
    q = captured["query"]
    assert "query CharacterById" in q
    assert "$id: Int!" in q
    assert "$name" not in q
    assert captured["variables"] == {"id": 123, "limit": 30, "page": 1}


def test_character_recent_reports_name_query_has_no_unused_id_variable():
    from core.wcl_client import WCLClient
    cli = WCLClient("x", "y")
    captured = {}

    def fake_query(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables
        return {"characterData": {"character": {}}, "rateLimitData": {}}

    cli.query = fake_query
    cli.character_recent_reports(name="不是酒鬼丶", server_slug="白银之手", server_region="cn", limit=30)
    q = captured["query"]
    assert "query CharacterByName" in q
    assert "$id" not in q
    assert "character(name: $name, serverSlug: $server, serverRegion: $region)" in q
    assert captured["variables"]["name"] == "不是酒鬼丶"
    assert captured["variables"]["server"] == "白银之手"
    assert captured["variables"]["region"] == "cn"
