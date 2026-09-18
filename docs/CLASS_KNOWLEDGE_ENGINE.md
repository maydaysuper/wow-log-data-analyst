# Class Knowledge Engine (v0.7.0)

## Goal

The class knowledge layer is learned primarily from observed combat logs instead of a hand-written rotation guide.
It does **not** claim that a high-DPS sample is automatically correct. Route, target count, gear, key level,
party composition and encounter mechanics can confound damage.

## Learning loop

1. Parse a run and identify the focus player.
2. Convert the run into a bounded behavior signature instead of storing the raw log.
3. Store signatures locally in SQLite with class/spec/dungeon/key context.
4. Build empirical P25/P50/P75/P90 distributions across same-spec samples.
5. Compare user-confirmed `reference` samples against ordinary samples.
6. Flag stable differences as empirical pattern candidates, never as proven causal rotation rules.
7. Optionally ask DeepSeek to synthesize those aggregated facts into a versioned playbook.
8. Feed the empirical profile + latest playbooks back into later AI log analysis.
9. Continue accumulating new deduplicated samples and regenerate the playbook when the user chooses.

## Behavior signature

A signature contains compact summaries only:

- player DPS / duration / casts / deaths / interrupts
- per-skill damage share, casts/min, hits/cast, crit rate and damage/cast
- observed buff uptime
- observed cast cadence
- resource snapshots / high-resource time / observed overcap
- target switching
- inter-pull downtime
- death recovery
- burst windows
- responsiveness anomaly score and evidence

API keys are never written to the knowledge database.

## Sample roles

- `auto`: ordinary automatically accumulated sample
- `reference`: user-confirmed high-quality reference sample
- `normal`: explicit ordinary/control sample
- `exclude`: retained in the DB but excluded from cohort learning

If no `reference` samples exist and there are at least four usable samples, the deterministic engine may use the
current cohort's top DPS quartile as an **exploratory** comparison group. This is explicitly labeled as correlation,
not optimal play.

## DeepSeek usage

DeepSeek is used for synthesis and reasoning, not for basic arithmetic and not for silent fine-tuning.
The app sends an aggregated empirical profile, asks for JSON-only evidence-bound conclusions, and stores the result
as a versioned local playbook. Regenerating a playbook is an explicit paid API action.

Normal AI analysis can also receive the latest local class knowledge context. Current-log evidence always has priority.

## Guardrails

- Fewer than 4 samples => very weak class knowledge.
- Fewer than 8 samples => DeepSeek is instructed not to use high-confidence class conclusions casually.
- Cross-context evidence is more valuable than a pattern seen in one dungeon/key only.
- Correlation is not causation.
- The engine never invents unobserved skills or theoretical cooldowns.
- A class theory/rules database can be added later as an independent evidence source and cross-check.
