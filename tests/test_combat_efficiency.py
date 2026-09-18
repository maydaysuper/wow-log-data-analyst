from core.parser import parse_text
from core.analyzer import events_df
from core.combat_efficiency import (
    buff_uptime,
    resource_efficiency,
    cooldown_cadence,
    target_switch_analysis,
    pull_downtime,
    death_recovery,
    deep_efficiency_payload,
)


def test_advanced_combat_log_suffix_alignment_and_power_snapshot():
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9]
9/18 10:00:01.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,Player-1,0000000000000000,5000,5000,100,100,100,0,1,80,100,30,1.0,2.0,999,0.1,650
9/18 10:00:01.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,Creature-1,0000000000000000,10000,10000,0,0,100,0,0,0,0,0,1.1,2.1,999,0.2,82,2500,0,1,0,0,0,true,false,false,false
9/18 10:00:02.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,Player-1,0000000000000000,5000,5000,100,100,100,0,1,95,100,30,1.0,2.0,999,0.1,650
9/18 10:00:03.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"Hit",1,Player-1,0000000000000000,5000,5000,100,100,100,0,1,30,100,30,1.0,2.0,999,0.1,650
9/18 10:00:10.000  CHALLENGE_MODE_END,999,1,12,10000
'''
    df = events_df(parse_text(log, year=2026))
    dmg = df[df["event"] == "SPELL_DAMAGE"].iloc[0]
    assert float(dmg["amount"]) == 2500
    assert bool(dmg["critical"])
    casts = df[df["event"] == "SPELL_CAST_SUCCESS"]
    assert float(casts.iloc[0]["current_power"]) == 80
    assert float(casts.iloc[1]["max_power"]) == 100
    res = resource_efficiency(df, "Alice")
    assert res["resource_snapshot_available"]
    assert res["advanced_snapshots"] == 3
    assert res["avg_resource_pct"] is not None


def test_buff_uptime_and_energize_overcap():
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9]
9/18 10:00:01.000  SPELL_AURA_APPLIED,Player-1,"Alice",0,0,Player-1,"Alice",0,0,200,"Avatar",1,"BUFF",1
9/18 10:00:04.000  SPELL_AURA_REFRESH,Player-1,"Alice",0,0,Player-1,"Alice",0,0,200,"Avatar",1,"BUFF",1
9/18 10:00:06.000  SPELL_AURA_REMOVED,Player-1,"Alice",0,0,Player-1,"Alice",0,0,200,"Avatar",1,"BUFF",1
9/18 10:00:07.000  SPELL_ENERGIZE,Player-1,"Alice",0,0,Player-1,"Alice",0,0,300,"Gain",1,20,5,1,100
9/18 10:00:10.000  CHALLENGE_MODE_END,999,1,12,10000
'''
    df = events_df(parse_text(log, year=2026))
    buffs = buff_uptime(df, "Alice")
    assert len(buffs) == 1
    assert round(float(buffs.iloc[0]["uptime_s"]), 1) == 5.0
    assert round(float(buffs.iloc[0]["uptime_pct"]), 1) == 50.0
    res = resource_efficiency(df, "Alice")
    assert res["resource_gained"] == 20.0
    assert res["resource_overcap"] == 5.0
    assert res["overcap_pct_of_generated"] == 20.0


def test_cadence_target_switch_and_pull_downtime():
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9]
9/18 10:00:01.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"A",1
9/18 10:00:01.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"A",1,1000,0,1,0,0,0,false
9/18 10:00:03.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-2,"MobB",0,0,100,"A",1
9/18 10:00:03.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-2,"MobB",0,0,100,"A",1,1000,0,1,0,0,0,false
9/18 10:00:15.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-3,"MobC",0,0,100,"A",1
9/18 10:00:15.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-3,"MobC",0,0,100,"A",1,1000,0,1,0,0,0,false
9/18 10:00:20.000  CHALLENGE_MODE_END,999,1,12,20000
'''
    df = events_df(parse_text(log, year=2026))
    cad = cooldown_cadence(df, "Alice")
    assert int(cad.iloc[0]["casts"]) == 3
    sw = target_switch_analysis(df, "Alice")
    assert sw["target_switches"] == 2
    gaps, summary = pull_downtime(df, idle_gap_s=8.0)
    assert summary["pulls"] == 2
    assert summary["inter_pull_downtime_s"] > 8.0
    assert len(gaps) == 1


def test_death_recovery_and_payload():
    log = '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9]
9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"A",1,1500,0,1,0,0,0,false
9/18 10:00:03.000  UNIT_DIED,0000000000000000,nil,0,0,Player-1,"Alice",0,0
9/18 10:00:04.000  SPELL_DAMAGE,Player-2,"Bob",0,0,Creature-1,"MobA",0,0,200,"B",1,1000,0,1,0,0,0,false
9/18 10:00:08.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"A",1
9/18 10:00:08.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"A",1,1000,0,1,0,0,0,false
9/18 10:00:12.000  CHALLENGE_MODE_END,999,1,12,12000
'''
    df = events_df(parse_text(log, year=2026))
    rec = death_recovery(df, "Alice")
    assert len(rec) == 1
    assert round(float(rec.iloc[0]["recovery_s"]), 1) == 5.0
    payload = deep_efficiency_payload(df, "Alice")
    assert payload["death_recovery"]
    assert "resource_efficiency" in payload


def test_skill_benchmark_exposes_percentiles():
    from core.analyzer import skill_benchmark
    log1 = '''9/18 10:00:00.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:01.000  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"Mob",0,0,100,"A",1,1000,0,1,0,0,0,false
'''
    log2 = '''9/18 10:00:00.000  SPELL_CAST_SUCCESS,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,100,"A",1
9/18 10:00:01.000  SPELL_DAMAGE,Player-2,"Bob",0,0,Creature-1,"Mob",0,0,100,"A",1,2000,0,1,0,0,0,false
'''
    runs = {
        "r1": events_df(parse_text(log1, year=2026)),
        "r2": events_df(parse_text(log2, year=2026)),
    }
    bench = skill_benchmark(runs, {"r1": "Alice", "r2": "Bob"})
    assert "damage_pct_p75" in bench.columns
    assert "casts_per_min_p90" in bench.columns
