# Responsiveness / Lag Diagnostics

Version 0.4.0 introduces a timing-analysis engine whose job is to explain *why* a player's combat output may have dropped, not merely how much it dropped.

## Evidence hierarchy

### Tier 1 — Combat Log facts

The analyzer can directly prove that a timing pattern exists in the server-side combat log:

- long gaps between successful player actions while the party remains active;
- gaps where other players continue casting during one player's silence;
- whether the player keeps taking damage during the gap;
- whether auto-attacks continue or disappear during the gap;
- `SPELL_CAST_START` → `SPELL_CAST_SUCCESS` observed-duration outliers;
- `SPELL_CAST_FAILED` counts and failure reasons;
- `SWING_DAMAGE` / `SWING_MISSED` rhythm anomalies.

These are responsiveness anomalies, not direct proof of latency. The log does not contain the moment the user pressed a key.

### Tier 2 — Optional telemetry

The optional `WowLogTelemetry` addon records once per second:

- FPS (`GetFramerate()`)
- Home latency (`GetNetStats()`)
- World latency (`GetNetStats()`)
- incoming/outgoing bandwidth

The desktop analyzer aligns telemetry samples to abnormal combat-log windows. If action stalls overlap with high World latency while FPS is stable, network latency becomes a supported explanation. If stalls overlap with FPS collapse while latency is stable, local performance stutter becomes a supported explanation.

## Interpretation rules

The AI must never claim that a user had a network problem from Combat Log evidence alone. It should use these labels:

- no obvious responsiveness anomaly;
- slight responsiveness anomaly;
- clear responsiveness anomaly — review required;
- high suspicion of input/client/network responsiveness issue;
- network issue supported by telemetry;
- local FPS/performance issue supported by telemetry;
- mixed network and client-performance issue;
- telemetry does not support network/FPS attribution.

## Important confounders

Movement, target swaps, target death, forced downtime, mechanics, resource pooling, crowd control, range, line-of-sight and deliberate defensive play can all create gaps that resemble lag. Auto-attack interruptions are therefore weak evidence. Cast-duration outliers are also weak evidence because haste and spell mechanics can change observed cast time.

### Telemetry limitation

WoW's `GetNetStats()` latency values are updated by the client at a much slower cadence than the one-second telemetry sampler (roughly every 30 seconds). The sampler therefore preserves the current latency value every second for alignment, but very short packet-loss/latency bursts may not appear as a new latency reading. FPS is sampled every second and is better suited to short local-performance stalls.
