from pathlib import Path
import sys, types

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))
from core.batch_analysis import build_multi_fight_payload
from core.ai_client import render_analysis_markdown, PLAYER_REPORT_SYSTEM_PROMPT


def _sig(player='Tester', casts=5.0, dmg=30.0):
    return {
        'context': {'player': player, 'class_name': 'Warrior', 'spec_name': 'Arms', 'dungeon': 'Test', 'key_level': 10},
        'overview': {'duration_s': 120, 'dps': 500000, 'casts': 100},
        'skills': [
            {'spell_name': 'Skill A', 'damage_pct': dmg, 'casts_per_min': casts, 'hits_per_cast': 1.2, 'crit_pct': 20, 'damage_per_cast': 1000},
            {'spell_name': 'Skill B', 'damage_pct': 15, 'casts_per_min': 2, 'hits_per_cast': 2.1, 'crit_pct': 10, 'damage_per_cast': 500},
        ],
        'responsiveness': {'score': 10, 'max_cast_gap_s': 2.1},
        'direct_fight_evidence': {'interrupt_count': 2, 'death_count': 0},
    }


def test_multi_fight_payload_finds_repeated_skill_behavior():
    out = build_multi_fight_payload([_sig(casts=4), _sig(casts=6), _sig(casts=5)])
    assert out['analysis_request']['mode'] == 'multi_wcl_fights'
    assert out['analysis_request']['fight_count'] == 3
    skill = next(x for x in out['repeated_skill_behavior'] if x['spell_name'] == 'Skill A')
    assert skill['present_in_fights'] == 3
    assert skill['avg_casts_per_min'] == 5.0
    assert skill['min_casts_per_min'] == 4.0
    assert skill['max_casts_per_min'] == 6.0


def test_player_report_prompt_hides_percentile_jargon():
    assert '不要直接向玩家展示 P10/P25/P50/P75/P90' in PLAYER_REPORT_SYSTEM_PROMPT
    assert '全篇使用简体中文' in PLAYER_REPORT_SYSTEM_PROMPT or '全文使用简体中文' in PLAYER_REPORT_SYSTEM_PROMPT


def test_fallback_report_is_chinese_player_facing():
    md = render_analysis_markdown({
        'executive_summary': ['核心技能使用偏少。'],
        'personal_baseline_findings': [{'finding': '本场低于本人常态', 'evidence': '多波重复出现', 'confidence': 'high'}],
        'next_actions': ['下一把优先保证核心技能使用频率。'],
        'overall_confidence': 'high',
    })
    assert '战斗分析结论' in md
    assert '和你自己平时相比' in md
    assert '证据较强' in md
    assert 'P90' not in md


def test_desktop_version_and_reportmap_schema_are_updated():
    root = Path(__file__).resolve().parents[1]
    desktop = (root / 'desktop_app.py').read_text(encoding='utf-8')
    wcl = (root / 'core' / 'wcl_client.py').read_text(encoding='utf-8')
    assert 'APP_VERSION = _read_app_version()' in desktop
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "0.18.0"
    assert 'maps { id name }' not in wcl
    assert wcl.count('maps { id }') >= 2
