from __future__ import annotations

import re
from datetime import datetime
from statistics import median

import pandas as pd

_SAMPLE_RE = re.compile(
    r'(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\|'
    r'(?P<fps>\d+(?:\.\d+)?)\|(?P<home>\d+(?:\.\d+)?)\|(?P<world>\d+(?:\.\d+)?)\|'
    r'(?P<bin>\d+(?:\.\d+)?)\|(?P<bout>\d+(?:\.\d+)?)'
)


def parse_savedvariables(text: str) -> pd.DataFrame:
    rows = []
    for m in _SAMPLE_RE.finditer(text or ""):
        dt = datetime.strptime(m.group("stamp"), "%Y-%m-%d %H:%M:%S")
        rows.append({
            "ts": dt.timestamp(),
            "local_time": m.group("stamp"),
            "fps": float(m.group("fps")),
            "latency_home_ms": float(m.group("home")),
            "latency_world_ms": float(m.group("world")),
            "bandwidth_in_kbps": float(m.group("bin")),
            "bandwidth_out_kbps": float(m.group("bout")),
        })
    return pd.DataFrame(rows)


def telemetry_summary(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"samples": 0}
    out = {"samples": int(len(df))}
    for col, prefix in [
        ("fps", "fps"),
        ("latency_home_ms", "home_ms"),
        ("latency_world_ms", "world_ms"),
    ]:
        s = df[col].dropna().astype(float)
        if s.empty:
            continue
        out[f"{prefix}_median"] = round(float(s.median()), 2)
        out[f"{prefix}_p05"] = round(float(s.quantile(0.05)), 2)
        out[f"{prefix}_p95"] = round(float(s.quantile(0.95)), 2)
        out[f"{prefix}_max"] = round(float(s.max()), 2)
        out[f"{prefix}_min"] = round(float(s.min()), 2)

    world_med = float(df["latency_world_ms"].median()) if "latency_world_ms" in df else 0
    fps_med = float(df["fps"].median()) if "fps" in df else 0
    world_threshold = max(150.0, world_med + 75.0, world_med * 2.0)
    fps_threshold = min(45.0, fps_med * 0.55) if fps_med else 30.0
    out["world_spike_threshold_ms"] = round(world_threshold, 1)
    out["world_spike_samples"] = int((df["latency_world_ms"] >= world_threshold).sum())
    out["fps_drop_threshold"] = round(fps_threshold, 1)
    out["fps_drop_samples"] = int((df["fps"] <= fps_threshold).sum())
    return out


def correlate_gaps_with_telemetry(gaps: pd.DataFrame, telemetry: pd.DataFrame) -> dict:
    if gaps is None or gaps.empty or telemetry is None or telemetry.empty:
        return {
            "matched_gaps": 0,
            "network_spike_gaps": 0,
            "fps_drop_gaps": 0,
            "mixed_gaps": 0,
            "assessment": "no_telemetry_match",
        }

    base = telemetry_summary(telemetry)
    world_thr = float(base.get("world_spike_threshold_ms", 150.0))
    fps_thr = float(base.get("fps_drop_threshold", 30.0))
    matched = network = fps = mixed = 0
    details = []

    for row in gaps.itertuples(index=False):
        a = float(row.start_ts)
        b = float(row.end_ts)
        t = telemetry[(telemetry["ts"] >= a - 1.0) & (telemetry["ts"] <= b + 1.0)]
        if t.empty:
            continue
        matched += 1
        max_world = float(t["latency_world_ms"].max())
        min_fps = float(t["fps"].min())
        net_hit = max_world >= world_thr
        fps_hit = min_fps <= fps_thr
        network += int(net_hit)
        fps += int(fps_hit)
        mixed += int(net_hit and fps_hit)
        details.append({
            "start_ts": a,
            "gap_s": float(row.gap_s),
            "max_world_ms": round(max_world, 1),
            "min_fps": round(min_fps, 1),
            "network_spike": bool(net_hit),
            "fps_drop": bool(fps_hit),
        })

    if matched == 0:
        assessment = "no_telemetry_match"
    elif network >= max(1, matched // 2) and fps == 0:
        assessment = "network_supported"
    elif fps >= max(1, matched // 2) and network == 0:
        assessment = "fps_stutter_supported"
    elif network and fps:
        assessment = "mixed_performance_issue"
    else:
        assessment = "telemetry_does_not_support_network_or_fps_issue"

    return {
        "matched_gaps": matched,
        "network_spike_gaps": network,
        "fps_drop_gaps": fps,
        "mixed_gaps": mixed,
        "assessment": assessment,
        "telemetry_summary": base,
        "details": details,
    }
