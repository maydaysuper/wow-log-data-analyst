from pathlib import Path

from core.parser import parse_text
from core.analyzer import events_df
from core.advanced_timeline import segment_pulls, player_pull_breakdown, burst_windows
from core.learning import (
    init_db, save_case, update_feedback, add_lesson, retrieve_similar_cases,
    retrieve_lessons, recent_cases, clear_memory,
)


def _sample_log():
    return '''9/18 10:00:00.000  CHALLENGE_MODE_START,"Test Dungeon",999,777,12,[9]
9/18 10:00:01.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"Hit",1
9/18 10:00:01.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-1,"MobA",0,0,100,"Hit",1,1000,0,1,0,0,0,false
9/18 10:00:02.000  SPELL_DAMAGE,Player-2,"Bob",0,0,Creature-1,"MobA",0,0,200,"Other",1,500,0,1,0,0,0,false
9/18 10:00:15.000  SPELL_CAST_SUCCESS,Player-1,"Alice",0,0,Creature-2,"MobB",0,0,100,"Hit",1
9/18 10:00:15.100  SPELL_DAMAGE,Player-1,"Alice",0,0,Creature-2,"MobB",0,0,100,"Hit",1,3000,0,1,0,0,0,false
9/18 10:00:16.000  SPELL_DAMAGE,Player-2,"Bob",0,0,Creature-2,"MobB",0,0,200,"Other",1,500,0,1,0,0,0,false
9/18 10:00:30.000  CHALLENGE_MODE_END,999,1,12,30000
'''


def test_pull_segmentation_and_player_breakdown():
    df = events_df(parse_text(_sample_log(), year=2026))
    pulls = segment_pulls(df, idle_gap_s=8.0)
    assert len(pulls) == 2
    pp = player_pull_breakdown(df, "Alice", pulls)
    assert len(pp) == 2
    assert pp.iloc[1]["player_damage"] > pp.iloc[0]["player_damage"]


def test_burst_windows_find_peak():
    df = events_df(parse_text(_sample_log(), year=2026))
    bursts = burst_windows(df, "Alice", window_s=5, step_s=1, top_n=2)
    assert not bursts.empty
    assert float(bursts.iloc[0]["damage"]) >= 3000


def test_learning_memory_roundtrip(tmp_path: Path):
    db = tmp_path / "memory.sqlite3"
    init_db(db)
    context = {"dungeon": "Test Dungeon", "key_level": 12, "class_name": "Warrior", "spec_name": "Protection", "analysis_mode": "responsiveness", "player": "Alice"}
    payload = {"responsiveness": {"score": 70, "longest_gap_s": 6.0}, "team_dps": 1000}
    cid = save_case(context, payload, "report", {"overall_confidence": "medium"}, "deepseek-flash", path=db)
    update_feedback(cid, 2, "网络延迟", "录像确认延迟峰值", path=db)
    add_lesson(cid, context, "停手窗口与高延迟重合时优先检查网络", "有直接遥测重合", "只有Combat Log无遥测时", "high", path=db)
    similar = retrieve_similar_cases(context, {"responsiveness": {"score": 68, "longest_gap_s": 5.7}, "team_dps": 1050}, path=db)
    assert len(similar) == 1
    assert similar[0]["confirmed_cause"] == "网络延迟"
    lessons = retrieve_lessons(context, path=db)
    assert lessons and lessons[0]["confidence"] == "high"
    assert len(recent_cases(path=db)) == 1
    clear_memory(path=db)
    assert recent_cases(path=db) == []
