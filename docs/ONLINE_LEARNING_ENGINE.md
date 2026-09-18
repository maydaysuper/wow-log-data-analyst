# Online Learning Engine (v0.8.0)

## Goal

Turn public, traceable data into evidence that improves WoW Log analysis over time without pretending the DeepSeek API is silently fine-tuned.

The learning loop has four layers:

1. **Empirical log evidence** — local Combat Logs and Warcraft Logs API events.
2. **Reference evidence** — public documentation/guides fetched from an explicit URL.
3. **Structured memory** — SQLite behavior signatures, provenance, source snapshots and AI-extracted claims.
4. **DeepSeek synthesis** — cohort/playbook synthesis and per-fight reasoning using the evidence above.

## WCL pipeline

`characterRankings` is used only for discovery. Ranking JSON is mutable and is not accepted as a behavior sample by itself.

The UI can first read the WCL WorldData expansion/zone/encounter catalog, so the user can choose a current dungeon/encounter without knowing its numeric ID. Catalog data is cached in the current software session.

For every candidate the importer attempts to re-fetch:

- report metadata + revision;
- exact fight;
- player actor/source ID;
- Cast events;
- DamageDone events;
- Buff events (`standard` / `deep`);
- Resources events (`deep`);
- WCL-native dungeon pulls.
- game version / patch scope, average item level, keystone time/bonus, enemy-count progress, affixes and friendly spec composition.

Only then is a behavior signature inserted into the existing class/spec knowledge cohort.

### Stable identity / de-duplication

A WCL learned sample is keyed by:

`report_code + fight_id + source_id`

A later sync updates that sample in place. Mutable ranking percentiles and fetch timestamps do not create duplicates.

When the optional automatic Playbook iteration is used, the cohort is restricted to the dominant patch scope from the newly imported batch whenever WCL exposes a game version. This reduces cross-patch contamination.

### Reference role

A discovered sample is marked `reference` automatically only when the ranking payload exposes a percentile >= 90. If no usable percentile exists, it remains `auto`. A high percentile is still only an empirical reference, not proof of a theoretically optimal rotation.

## Public reference pipeline

Non-WCL documents are fetched only from an explicit public HTTP(S) URL in v0.8.0. This intentionally avoids an uncontrolled crawler.

Safety and quality rules:

- local/private/link-local IP targets are rejected;
- redirects are validated again;
- page size and extracted text are bounded;
- scripts/styles are stripped;
- URL, title, fetch time, content hash, source class and optional patch scope are retained;
- the raw page is never treated as a Combat Log sample.

DeepSeek can convert the fetched text into compact claims labeled as one of:

- `official_mechanic`
- `documented_mechanic`
- `community_recommendation`
- `uncertain`

Unknown/old version scope lowers confidence.

## Evidence precedence

Per-fight analysis should use evidence in this order:

1. Current uploaded Log / WCL event facts.
2. Direct latency/FPS telemetry when diagnosing responsiveness.
3. Same-context empirical class/spec cohort.
4. User-confirmed historical cases.
5. Versioned external reference evidence.
6. AI hypotheses.

External references must never override contradictory current-log facts.

## Cost control

- WCL ingestion does **not** require DeepSeek calls.
- Behavior signatures and percentiles are deterministic local computations.
- DeepSeek is called only for explicit report analysis, source-evidence extraction or playbook synthesis.
- The optional “auto iterate playbook after WCL batch” switch is disabled by default and visibly marked as a paid-API action.
- API keys are not written to the learning database.

## Provenance tables

`online_sources`
- source type
- canonical ID
- URL
- title
- revision/hash
- fetch timestamp
- trust class
- context/payload snapshot

`external_evidence`
- source ID
- class/spec scope
- patch scope
- evidence kind
- model
- structured evidence JSON

`online_learning_runs`
- batch context
- requested/candidate/imported/skipped/failed counts
- run details

## Important limitations

- WCL rankings are mutable and affected by season/partition/rules.
- DPS correlations are confounded by item level, route, pull size, group composition and fight duration.
- Public guides are opinions unless the underlying mechanism is independently documented.
- A cast-gap anomaly is not proof of network lag; networking claims require telemetry or other direct evidence.
