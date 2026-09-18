from __future__ import annotations

import ast
import csv
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, Optional

_TS_RE = re.compile(r'^(?P<date>\d{1,2}/\d{1,2})\s+(?P<time>\d{1,2}:\d{2}:\d{2}\.\d+)\s{2}(?P<body>.*)$')

DAMAGE_EVENTS = {"SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE", "SWING_DAMAGE", "DAMAGE_SHIELD", "DAMAGE_SPLIT"}
HEAL_EVENTS = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
CAST_EVENTS = {"SPELL_CAST_SUCCESS"}
CAST_START_EVENTS = {"SPELL_CAST_START"}
CAST_FAILED_EVENTS = {"SPELL_CAST_FAILED"}
MISS_EVENTS = {"SWING_MISSED", "SPELL_MISSED", "RANGE_MISSED"}
INTERRUPT_EVENTS = {"SPELL_INTERRUPT"}
DEATH_EVENTS = {"UNIT_DIED", "UNIT_DESTROYED"}
AURA_EVENTS = {
    "SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED", "SPELL_AURA_REFRESH",
    "SPELL_AURA_APPLIED_DOSE", "SPELL_AURA_REMOVED_DOSE",
}
RESOURCE_EVENTS = {"SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE", "SPELL_DRAIN", "SPELL_LEECH"}
DISPEL_EVENTS = {"SPELL_DISPEL", "SPELL_DISPEL_FAILED", "SPELL_STOLEN"}
RESURRECT_EVENTS = {"SPELL_RESURRECT"}


@dataclass
class CombatEvent:
    ts: float
    event: str
    source_guid: Optional[str] = None
    source_name: Optional[str] = None
    dest_guid: Optional[str] = None
    dest_name: Optional[str] = None
    spell_id: Optional[int] = None
    spell_name: Optional[str] = None
    amount: float = 0.0
    overheal: float = 0.0
    critical: bool = False
    segment: str = "Full Log"
    run_segment: str = "Full Log"
    encounter: Optional[str] = None
    dungeon: Optional[str] = None
    map_id: Optional[int] = None
    challenge_mode_id: Optional[int] = None
    keystone_level: Optional[int] = None
    affixes: str = ""
    spec_id: Optional[int] = None
    failure_reason: Optional[str] = None

    # Advanced combat logging snapshot fields. These describe advanced_info_guid,
    # which may be the source or destination depending on the combat event.
    advanced_info_guid: Optional[str] = None
    advanced_owner_guid: Optional[str] = None
    current_hp: Optional[float] = None
    max_hp: Optional[float] = None
    power_type: Optional[int] = None
    current_power: Optional[float] = None
    max_power: Optional[float] = None
    power_cost: Optional[float] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    ui_map_id: Optional[int] = None
    facing: Optional[float] = None
    unit_level: Optional[float] = None

    # Aura/resource suffix details.
    aura_type: Optional[str] = None
    aura_stacks: Optional[int] = None
    overenergize: float = 0.0
    resource_power_type: Optional[int] = None
    resource_max_power: Optional[float] = None
    extra_spell_id: Optional[int] = None
    extra_spell_name: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def _unquote(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().strip('"')
    if s.lower() in {"nil", "none", "null", ""}:
        return None
    return s


def _as_int(v, default=None):
    try:
        if v is None or v == "" or str(v).lower() == "nil":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v, default=0.0):
    try:
        if v is None or v == "" or str(v).lower() == "nil":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes"}


_DAY_TS_CACHE: dict[tuple[int, str], float] = {}


def _parse_ts(date_s: str, time_s: str, year: Optional[int] = None) -> float:
    """Fast combat-log timestamp parser.

    ``datetime.strptime`` on every combat-log line dominated local-log import time for
    large files.  Cache the local midnight once per day and parse only H:M:S.f manually.
    """
    year = year or datetime.now().year
    key = (int(year), date_s)
    base = _DAY_TS_CACHE.get(key)
    if base is None:
        month_s, day_s = date_s.split("/", 1)
        base = datetime(int(year), int(month_s), int(day_s)).timestamp()
        _DAY_TS_CACHE[key] = base
    h_s, m_s, sec_s = time_s.split(":", 2)
    return base + int(h_s) * 3600 + int(m_s) * 60 + float(sec_s)


def _csv_parts(body: str) -> list[str]:
    # csv.reader accepts an iterable of lines; avoiding StringIO saves one allocation per event.
    return next(csv.reader([body], skipinitialspace=False))


def _parse_affixes(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, (list, tuple)):
            return ",".join(str(_as_int(x, x)) for x in parsed)
    except Exception:
        pass
    return value.strip().strip("[]")


def _looks_like_guid(v: Optional[str]) -> bool:
    s = _unquote(v)
    if not s:
        return False
    return s == "0000000000000000" or "-" in s


def _spell_prefix_end(event: str) -> int:
    # Text-file indexes: 0 event, 1..8 source/destination header, 9..11 spell prefix.
    if event.startswith("SPELL_") or event.startswith("SPELL_PERIODIC_") or event.startswith("RANGE_") or event in {"DAMAGE_SHIELD", "DAMAGE_SPLIT", "DAMAGE_SHIELD_MISSED"}:
        return 12
    if event.startswith("ENVIRONMENTAL_"):
        return 10
    return 9


def _parse_spell_prefix(ce: CombatEvent, p: list[str], event: str) -> int:
    if event.startswith("SPELL_") or event.startswith("SPELL_PERIODIC_") or event.startswith("RANGE_") or event in {"DAMAGE_SHIELD", "DAMAGE_SPLIT", "DAMAGE_SHIELD_MISSED"}:
        ce.spell_id = _as_int(p[9] if len(p) > 9 else None)
        ce.spell_name = _unquote(p[10]) if len(p) > 10 else None
        return 12
    if event.startswith("SWING_"):
        ce.spell_name = "Melee"
        return 9
    return _spell_prefix_end(event)


def _parse_advanced_snapshot(ce: CombatEvent, p: list[str], start: int) -> int:
    """Parse the optional 17-field Advanced Combat Logging snapshot.

    Blizzard inserts these fields between the event prefix and suffix. We only treat a
    block as advanced data when the first two fields look like GUIDs and enough fields
    remain, which preserves compatibility with normal/non-advanced logs.
    """
    if len(p) < start + 17:
        return start
    if not (_looks_like_guid(p[start]) and _looks_like_guid(p[start + 1])):
        return start

    ce.advanced_info_guid = _unquote(p[start])
    ce.advanced_owner_guid = _unquote(p[start + 1])
    ce.current_hp = _as_float(p[start + 2], None)
    ce.max_hp = _as_float(p[start + 3], None)
    ce.power_type = _as_int(p[start + 8], None)
    ce.current_power = _as_float(p[start + 9], None)
    ce.max_power = _as_float(p[start + 10], None)
    ce.power_cost = _as_float(p[start + 11], None)
    ce.position_x = _as_float(p[start + 12], None)
    ce.position_y = _as_float(p[start + 13], None)
    ce.ui_map_id = _as_int(p[start + 14], None)
    ce.facing = _as_float(p[start + 15], None)
    ce.unit_level = _as_float(p[start + 16], None)
    return start + 17


def parse_lines(lines: Iterable[str], year: Optional[int] = None) -> list[CombatEvent]:
    """Parse a useful, analytics-focused subset of Blizzard's Combat Log text format.

    Supports both ordinary and Advanced Combat Logging records. For advanced records,
    the 17-unit-state fields are detected dynamically, so damage/heal/resource suffixes
    remain aligned rather than being read at fixed indexes.
    """
    events: list[CombatEvent] = []
    current_segment = "Full Log"
    current_run_segment = "Full Log"
    current_encounter: Optional[str] = None
    segment_counter = 0
    mplus_counter = 0

    dungeon: Optional[str] = None
    map_id: Optional[int] = None
    challenge_mode_id: Optional[int] = None
    keystone_level: Optional[int] = None
    affixes = ""
    in_mplus = False

    for raw in lines:
        raw = raw.rstrip("\r\n")
        m = _TS_RE.match(raw)
        if not m:
            continue
        try:
            ts = _parse_ts(m.group("date"), m.group("time"), year)
            p = _csv_parts(m.group("body"))
        except Exception:
            continue
        if not p:
            continue

        event = p[0]

        if event == "COMBATANT_INFO":
            ce = CombatEvent(
                ts=ts,
                event=event,
                source_guid=_unquote(p[1]) if len(p) > 1 else None,
                segment=current_segment,
                run_segment=current_run_segment,
                encounter=current_encounter,
                dungeon=dungeon,
                map_id=map_id,
                challenge_mode_id=challenge_mode_id,
                keystone_level=keystone_level,
                affixes=affixes,
                spec_id=_as_int(p[24] if len(p) > 24 else None),
            )
            events.append(ce)
            continue

        if event == "CHALLENGE_MODE_START":
            mplus_counter += 1
            dungeon = _unquote(p[1]) if len(p) > 1 else "Mythic+"
            map_id = _as_int(p[2] if len(p) > 2 else None)
            challenge_mode_id = _as_int(p[3] if len(p) > 3 else None)
            keystone_level = _as_int(p[4] if len(p) > 4 else None)
            affixes = _parse_affixes(",".join(p[5:]) if len(p) > 5 else None)
            level_txt = f" +{keystone_level}" if keystone_level is not None else ""
            current_run_segment = f"M+ {mplus_counter:02d} - {dungeon}{level_txt}"
            current_segment = current_run_segment
            current_encounter = None
            in_mplus = True
            events.append(CombatEvent(
                ts=ts, event=event, segment=current_segment, run_segment=current_run_segment,
                dungeon=dungeon, map_id=map_id, challenge_mode_id=challenge_mode_id,
                keystone_level=keystone_level, affixes=affixes,
            ))
            continue

        if event == "CHALLENGE_MODE_END":
            if in_mplus:
                events.append(CombatEvent(
                    ts=ts, event=event, segment=current_run_segment, run_segment=current_run_segment,
                    dungeon=dungeon, map_id=map_id, challenge_mode_id=challenge_mode_id,
                    keystone_level=keystone_level, affixes=affixes,
                ))
            in_mplus = False
            current_segment = "Full Log"
            current_run_segment = "Full Log"
            current_encounter = None
            dungeon = None
            map_id = None
            challenge_mode_id = None
            keystone_level = None
            affixes = ""
            continue

        if event == "ENCOUNTER_START":
            segment_counter += 1
            enc_id = p[1] if len(p) > 1 else "?"
            name = _unquote(p[2]) if len(p) > 2 else "Encounter"
            diff = p[3] if len(p) > 3 else "?"
            current_encounter = name
            current_segment = f"{segment_counter:02d} - {name} (enc {enc_id}, diff {diff})"
            continue

        if event == "ENCOUNTER_END":
            current_encounter = None
            current_segment = current_run_segment if in_mplus else "Full Log"
            continue

        src_guid = _unquote(p[1]) if len(p) > 1 else None
        src_name = _unquote(p[2]) if len(p) > 2 else None
        dst_guid = _unquote(p[5]) if len(p) > 5 else None
        dst_name = _unquote(p[6]) if len(p) > 6 else None

        ce = CombatEvent(
            ts=ts,
            event=event,
            source_guid=src_guid,
            source_name=src_name,
            dest_guid=dst_guid,
            dest_name=dst_name,
            segment=current_segment,
            run_segment=current_run_segment,
            encounter=current_encounter,
            dungeon=dungeon,
            map_id=map_id,
            challenge_mode_id=challenge_mode_id,
            keystone_level=keystone_level,
            affixes=affixes,
        )

        prefix_end = _parse_spell_prefix(ce, p, event)
        suffix_start = _parse_advanced_snapshot(ce, p, prefix_end)

        if event in DAMAGE_EVENTS:
            ce.amount = _as_float(p[suffix_start] if len(p) > suffix_start else 0)
            # DAMAGE suffix: amount, overkill, school, resisted, blocked, absorbed, critical...
            ce.critical = _as_bool(p[suffix_start + 6] if len(p) > suffix_start + 6 else None)
            events.append(ce)
            continue

        if event in HEAL_EVENTS:
            ce.amount = _as_float(p[suffix_start] if len(p) > suffix_start else 0)
            ce.overheal = _as_float(p[suffix_start + 1] if len(p) > suffix_start + 1 else 0)
            ce.critical = _as_bool(p[suffix_start + 3] if len(p) > suffix_start + 3 else None)
            events.append(ce)
            continue

        if event in CAST_EVENTS | CAST_START_EVENTS:
            events.append(ce)
            continue

        if event in CAST_FAILED_EVENTS:
            ce.failure_reason = _unquote(p[suffix_start] if len(p) > suffix_start else p[-1] if len(p) > prefix_end else None)
            events.append(ce)
            continue

        if event in MISS_EVENTS:
            events.append(ce)
            continue

        if event in INTERRUPT_EVENTS:
            ce.extra_spell_id = _as_int(p[suffix_start] if len(p) > suffix_start else None)
            ce.extra_spell_name = _unquote(p[suffix_start + 1]) if len(p) > suffix_start + 1 else None
            events.append(ce)
            continue

        if event in AURA_EVENTS:
            ce.aura_type = _unquote(p[suffix_start]) if len(p) > suffix_start else None
            ce.aura_stacks = _as_int(p[suffix_start + 1] if len(p) > suffix_start + 1 else None)
            events.append(ce)
            continue

        if event in RESOURCE_EVENTS:
            ce.amount = _as_float(p[suffix_start] if len(p) > suffix_start else 0)
            if event in {"SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE"}:
                ce.overenergize = _as_float(p[suffix_start + 1] if len(p) > suffix_start + 1 else 0)
                ce.resource_power_type = _as_int(p[suffix_start + 2] if len(p) > suffix_start + 2 else None)
                ce.resource_max_power = _as_float(p[suffix_start + 3] if len(p) > suffix_start + 3 else None)
            else:
                ce.resource_power_type = _as_int(p[suffix_start + 1] if len(p) > suffix_start + 1 else None)
                ce.resource_max_power = _as_float(p[suffix_start + 3] if len(p) > suffix_start + 3 else None)
            events.append(ce)
            continue

        if event in DISPEL_EVENTS:
            ce.extra_spell_id = _as_int(p[suffix_start] if len(p) > suffix_start else None)
            ce.extra_spell_name = _unquote(p[suffix_start + 1]) if len(p) > suffix_start + 1 else None
            ce.aura_type = _unquote(p[suffix_start + 3]) if len(p) > suffix_start + 3 else None
            events.append(ce)
            continue

        if event in DEATH_EVENTS | RESURRECT_EVENTS:
            events.append(ce)
            continue

    return events


def parse_text(text: str, year: Optional[int] = None) -> list[CombatEvent]:
    return parse_lines(text.splitlines(), year=year)
