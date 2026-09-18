from core.parser import parse_text
from core.analyzer import events_df, infer_run_metadata, list_players, skill_breakdown, team_summary


def test_mplus_metadata_persists_after_boss():
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9,10]\n9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,1000,0,1,0,0,0,false\n9/18 10:01:00.000  ENCOUNTER_START,1,"Boss",8,5,999\n9/18 10:01:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-2,"Boss",0,0,100,"Hit",1,2000,0,1,0,0,0,false\n9/18 10:02:00.000  ENCOUNTER_END,1,"Boss",8,5,1,60000\n9/18 10:02:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-3,"Mob2",0,0,100,"Hit",1,3000,0,1,0,0,0,false\n9/18 10:03:00.000  CHALLENGE_MODE_END,999,1,12,180000\n'''
    df = events_df(parse_text(log, year=2026))
    meta = infer_run_metadata(df)
    assert meta["dungeon"] == "Test Dungeon"
    assert meta["key_level"] == 12
    assert list_players(df) == ["Alice"]
    assert set(df["run_segment"].unique()) == {"M+ 01 - Test Dungeon +12"}
    assert df.iloc[-1]["segment"] == "M+ 01 - Test Dungeon +12"


def test_team_and_skill_stats():
    log = '''9/18 10:00:00.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1\n9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,1000,0,1,0,0,0,false\n9/18 10:00:02.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,2000,0,1,0,0,0,true\n'''
    df = events_df(parse_text(log, year=2026))
    s = skill_breakdown(df, "Alice")
    assert int(s.iloc[0]["damage"]) == 3000
    assert int(s.iloc[0]["casts"]) == 1
    t = team_summary(df)
    assert int(t["team_damage"]) == 3000


def test_combatant_info_spec_detection():
    from core.analyzer import infer_player_specs
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9,10]\n9/18 10:00:00.100  COMBATANT_INFO,Player-1,1,100,100,100,100,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,100,73,(),(),[],[],[]\n9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,1000,0,1,0,0,0,false\n'''
    df = events_df(parse_text(log, year=2026))
    specs = infer_player_specs(df)
    assert specs["Alice"]["class_name"] == "Warrior"
    assert specs["Alice"]["spec_name"] == "Protection"


def test_split_multiple_mplus_runs():
    from core.analyzer import split_mplus_runs
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Dungeon A",100,10,12,[9]\n9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,1000,0,1,0,0,0,false\n9/18 10:01:00.000  CHALLENGE_MODE_END,100,1,12,60000\n9/18 11:00:00.000  CHALLENGE_MODE_START,"Dungeon B",200,20,13,[10]\n9/18 11:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,2000,0,1,0,0,0,false\n9/18 11:01:00.000  CHALLENGE_MODE_END,200,1,13,60000\n'''
    df = events_df(parse_text(log, year=2026))
    runs = split_mplus_runs(df, "combat.log")
    assert len(runs) == 2
    metas = [infer_run_metadata(x) for x in runs.values()]
    assert {m["dungeon"] for m in metas} == {"Dungeon A", "Dungeon B"}


def test_mplus_duration_uses_challenge_markers():
    from core.analyzer import duration_seconds, player_overview
    log = """9/18 10:00:00.000  CHALLENGE_MODE_START,\"Test Dungeon\",999,777,12,[9]\n9/18 10:00:10.000  SPELL_DAMAGE,Player-1,\"Alice\",0,0,Creature-1,\"Mob\",0,0,100,\"Hit\",1,1000,0,1,0,0,0,false\n9/18 10:01:50.000  SPELL_DAMAGE,Player-1,\"Alice\",0,0,Creature-1,\"Mob\",0,0,100,\"Hit\",1,1000,0,1,0,0,0,false\n9/18 10:02:00.000  CHALLENGE_MODE_END,999,1,12,120000\n"""
    df = events_df(parse_text(log, year=2026))
    assert duration_seconds(df) == 120.0
    assert player_overview(df, "Alice")["duration_s"] == 120.0


def test_party_kill_is_not_counted_as_player_death():
    from core.analyzer import player_overview
    log = """9/18 10:00:00.000  SPELL_DAMAGE,Player-1,\"Alice\",0,0,Player-2,\"Bob\",0,0,100,\"Hit\",1,1000,0,1,0,0,0,false\n9/18 10:00:01.000  PARTY_KILL,Player-1,\"Alice\",0,0,Player-2,\"Bob\",0,0\n"""
    df = events_df(parse_text(log, year=2026))
    assert player_overview(df, "Bob")["deaths"] == 0


def test_responsiveness_detects_isolated_action_stall():
    from core.responsiveness import responsiveness_summary, action_gap_table
    log = '''9/18 10:00:00.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:01.500  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:03.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:04.000  SPELL_CAST_SUCCESS,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,200,"B",1
9/18 10:00:05.000  SPELL_CAST_SUCCESS,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,200,"B",1
9/18 10:00:06.000  SPELL_DAMAGE,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,200,"B",1,1000,0,1,0,0,0,false
9/18 10:00:07.000  SPELL_CAST_SUCCESS,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,200,"B",1
9/18 10:00:08.500  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:10.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
'''
    df = events_df(parse_text(log, year=2026))
    gaps = action_gap_table(df, "Alice")
    assert len(gaps) >= 1
    assert bool(gaps.iloc[0]["isolated_player_stall"])
    summary = responsiveness_summary(df, "Alice")
    assert summary["active_long_gap_count"] >= 1
    assert "Combat Log" in summary["network_conclusion"]


def test_parser_keeps_cast_start_failed_and_swing_missed():
    log = '''9/18 10:00:00.000  SPELL_CAST_START,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:00.400  SPELL_CAST_FAILED,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1,"Interrupted"
9/18 10:00:01.000  SWING_MISSED,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,"MISS",false
'''
    df = events_df(parse_text(log, year=2026))
    assert set(df["event"]) == {"SPELL_CAST_START", "SPELL_CAST_FAILED", "SWING_MISSED"}
    failed = df[df["event"] == "SPELL_CAST_FAILED"].iloc[0]
    assert failed["spell_name"] == "A"
    assert failed["failure_reason"] == "Interrupted"


def test_telemetry_parser_and_network_correlation():
    from datetime import datetime
    import pandas as pd
    from core.telemetry import parse_savedvariables, correlate_gaps_with_telemetry

    samples = [
        "2026-09-18 10:00:00|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:01|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:02|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:03|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:04|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:05|118.00|20|260|1.0|1.0",
        "2026-09-18 10:00:06|119.00|20|280|1.0|1.0",
        "2026-09-18 10:00:07|120.00|20|25|1.0|1.0",
        "2026-09-18 10:00:08|120.00|20|25|1.0|1.0",
    ]
    text = "\n".join('\"' + x + '\",' for x in samples)
    tdf = parse_savedvariables(text)
    assert len(tdf) == len(samples)
    a = datetime.strptime("2026-09-18 10:00:04", "%Y-%m-%d %H:%M:%S").timestamp()
    b = datetime.strptime("2026-09-18 10:00:07", "%Y-%m-%d %H:%M:%S").timestamp()
    gaps = pd.DataFrame([{"start_ts": a, "end_ts": b, "gap_s": 3.0}])
    corr = correlate_gaps_with_telemetry(gaps, tdf)
    assert corr["matched_gaps"] == 1
    assert corr["network_spike_gaps"] == 1
