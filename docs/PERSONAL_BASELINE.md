# Personal Behavior Baseline (v0.10)

The personal baseline answers a different question from the class/spec cohort:

- **Class cohort:** how does this player compare with other players in a similar context?
- **Personal baseline:** is this fight unusual compared with this same character's own history?

## Data flow

1. Resolve one WCL Character identity.
2. Sync recent public WCL reports.
3. For eligible Mythic+ fights, fetch only four streams for baseline building: Casts, DamageDone, Buffs and Resources.
4. Convert each fight to the same structured behavior signature used by the class learning engine.
5. Deduplicate by character + WCL report/fight/source identity.
6. Build personal quantiles for skills, responsiveness and efficiency.
7. When a new fight is analyzed, select the closest baseline scope available:
   - same spec + patch + dungeon + key ±2,
   - same spec + patch + dungeon,
   - same spec + patch,
   - same spec history,
   - all character history.
8. Compare the current fight to the historical distribution **before** saving the current fight.
9. Send both personal and class evidence to DeepSeek with explicit guardrails.

## What is learned

For recurring skills:

- casts/min P10/P25/P50/P75/P90
- damage share P10/P25/P50/P75/P90
- hits/cast
- crit rate
- damage/cast

For responsiveness:

- responsiveness anomaly score
- cast-gap median and P95
- maximum cast gap
- long/severe gap counts
- when available from targeted WCL analysis, player-only gaps while teammates keep casting

For efficiency when observable:

- average resource percentage
- time at >=90% resource
- time at <=10% resource
- observed overcap rate
- target switches/min

## Interpretation guardrail

A personal outlier is evidence that a fight differs from the player's own historical behavior. It is **not** proof that the historical behavior is optimal, and it is **not** proof of network lag. Network/FPS attribution still requires telemetry or other direct evidence.
