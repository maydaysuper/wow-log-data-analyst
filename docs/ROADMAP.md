# Roadmap

## P0 - data correctness

- Complete Retail V22 event parser coverage.
- Pet/guardian ownership attribution.
- Advanced combat logging trailing fields.
- Full `COMBATANT_INFO` talents/gear parsing.
- Accurate active-time vs wall-time DPS options.

## P1 - Mythic+ analyst

- Trash pull auto-segmentation.
- Boss vs trash split.
- Pull-size and pull-duration comparison.
- Death time-loss timeline.
- Interrupt/dispel/CC assignment analysis.
- Cooldown usage, delay and overlap analysis.
- Buff/debuff uptime.
- Resource generation/spend/overcap.
- Trinket/potion/consumable usage.

## P1 - benchmarking

- Same dungeon + same key level + same spec cohorts.
- Median / P75 / P90 baselines.
- WCL public-report sampling pipeline.
- Normalize by fight duration and target count where evidence exists.
- Version benchmarks by patch/season.

## P2 - product

- Persistent local SQLite log library.
- HTML/PDF report export.
- Windows desktop packaging.
- Background-free local indexing for large logs.

## Online learning direction after v0.8

- Seasonal/patch-aware cohort partitioning so old samples decay instead of contaminating current knowledge.
- Item-level / route / pull-size / group-composition normalization before claiming a behavior difference is meaningful.
- Automatic source freshness checks and knowledge invalidation when a new patch/season is detected.
- Additional authorized/official source adapters behind the same provenance interface; do not rely on brittle search-engine scraping.
- Optional scheduled WCL sync with explicit API-budget controls.
