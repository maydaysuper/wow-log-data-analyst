# Deep Combat Efficiency (v0.6.0)

This layer turns Advanced Combat Log evidence into cautious, comparable efficiency features.

## What is measured

- Observed player buff windows and uptime from `SPELL_AURA_*` events.
- Observed cast cadence per spell (casts/min, median/P95/max interval, variability).
- Advanced power snapshots: average resource %, time >=90%, time <=10%.
- `SPELL_ENERGIZE` generated resource and `overEnergize` waste when present.
- Target switches from successful casts with explicit non-player targets.
- Inter-pull downtime from approximate pull segmentation.
- Death-to-next-effective-action recovery and rough opportunity cost.
- Overlap between observed buff windows and top damage burst windows.
- Cross-run cohort tables and buff uptime matrices.

## Important limitations

1. Cast cadence is not the same as a spell's theoretical cooldown. Until a versioned spell database is added, the software must not call an observed interval a "cooldown delay" as a proven fact.
2. Pull downtime can include travel, RP, elevators, waiting for patrols, route decisions, or deliberate regrouping.
3. Advanced power snapshots are event-driven and incomplete. The analyzer caps time-weighting between snapshots to reduce overconfidence.
4. ENERGIZE overcap only covers resource sources that emit ENERGIZE data.
5. Aura uptime only covers apply/refresh/remove events visible in the uploaded log.
6. Death opportunity cost is a rough estimate based on pre-death damage and is not a counterfactual simulation.

## AI contract

DeepSeek receives these features as structured evidence. It must separate:

- directly observed facts,
- cross-sample differences,
- hypotheses that need a spell database, talent/loadout context, route data, telemetry, or video to verify.

User-confirmed corrections continue to feed the local learning/RAG store. This improves future retrieval and prompting; it does not fine-tune DeepSeek's model weights.
