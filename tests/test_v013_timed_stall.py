from __future__ import annotations

import pandas as pd

from core.responsiveness import action_gap_table
from core.wcl_direct_analysis import team_activity_stalls
from core.wcl_recent_runs import is_timed_keystone, timed_runs_from_report


def test_is_timed_keystone_prefers_bonus_and_requires_completion():
    f = {"keystoneLevel": 18, "kill": True, "keystoneBonus": 2, "startTime": 0, "endTime": 999999, "keystoneTime": 1}
    assert is_timed_keystone(f) is True
    f["kill"] = False
    assert is_timed_keystone(f) is False


def test_is_timed_keystone_falls_back_to_timer():
    assert is_timed_keystone({"keystoneLevel": 15, "kill": True, "keystoneBonus": 0, "startTime": 0, "endTime": 1800000, "keystoneTime": 1900000}) is True
    assert is_timed_keystone({"keystoneLevel": 15, "kill": True, "keystoneBonus": 0, "startTime": 0, "endTime": 2000000, "keystoneTime": 1900000}) is False


def test_timed_runs_from_report_filters_out_overtime():
    report = {
        "code": "ABC",
        "startTime": 1_700_000_000_000,
        "zone": {"name": "Mythic+ Season"},
        "fights": [
            {"id": 1, "name": "Dungeon A", "startTime": 0, "endTime": 1800000, "kill": True, "keystoneLevel": 18, "keystoneTime": 1900000, "keystoneBonus": 1, "friendlyPlayers": [7], "friendlySpecs": [71], "friendlyItemLevels": [650]},
            {"id": 2, "name": "Dungeon B", "startTime": 2000000, "endTime": 4100000, "kill": True, "keystoneLevel": 19, "keystoneTime": 1900000, "keystoneBonus": 0, "friendlyPlayers": [7], "friendlySpecs": [71], "friendlyItemLevels": [650]},
        ],
    }
    rows = timed_runs_from_report(report, 7)
    assert len(rows) == 1
    assert rows[0]["fight_id"] == 1
    assert rows[0]["dungeon"] == "Dungeon A"


def test_wcl_team_stall_excludes_gap_crossing_death():
    player_casts = [{"timestamp": 1000}, {"timestamp": 7000}]
    team_casts = [
        {"timestamp": 2000, "sourceID": 2},
        {"timestamp": 3000, "sourceID": 3},
        {"timestamp": 4000, "sourceID": 4},
        {"timestamp": 6000, "sourceID": 2},
    ]
    out = team_activity_stalls(
        player_casts, team_casts, [], source_id=1, friendly_ids={1, 2, 3, 4, 5},
        deaths=[{"timestamp": 5000}], pull_windows=[(0, 10000)],
    )
    assert out["isolated_gap_count"] == 0
    assert out["death_excluded_gap_count"] == 1


def test_wcl_team_stall_requires_combat_pull_and_alive_player():
    player_casts = [{"timestamp": 1000}, {"timestamp": 7000}]
    team_casts = [
        {"timestamp": 2000, "sourceID": 2},
        {"timestamp": 3000, "sourceID": 3},
        {"timestamp": 4000, "sourceID": 4},
        {"timestamp": 6000, "sourceID": 2},
    ]
    outside = team_activity_stalls(
        player_casts, team_casts, [], source_id=1, friendly_ids={1, 2, 3, 4, 5},
        deaths=[], pull_windows=[(0, 2000)],
    )
    assert outside["isolated_gap_count"] == 0
    assert outside["outside_combat_excluded_gap_count"] == 1
    inside = team_activity_stalls(
        player_casts, team_casts, [], source_id=1, friendly_ids={1, 2, 3, 4, 5},
        deaths=[], pull_windows=[(0, 10000)],
    )
    assert inside["isolated_gap_count"] == 1
    assert inside["windows"][0]["player_alive_for_entire_gap"] is True


def test_local_action_gap_excludes_death_window():
    rows = []
    for ts in (0.0, 1.0, 7.0, 8.0):
        rows.append({"ts": ts, "event": "SPELL_CAST_SUCCESS", "source_name": "Alice", "source_guid": "Player-1", "dest_name": "Mob"})
    for ts in (2.0, 3.0, 4.0, 5.0):
        rows.append({"ts": ts, "event": "SPELL_CAST_SUCCESS", "source_name": "Bob", "source_guid": "Player-2", "dest_name": "Mob"})
    rows.append({"ts": 4.5, "event": "UNIT_DIED", "source_name": None, "source_guid": None, "dest_name": "Alice"})
    df = pd.DataFrame(rows)
    out = action_gap_table(df, "Alice")
    assert out.empty
    assert out.attrs.get("death_excluded_gap_count") == 1
