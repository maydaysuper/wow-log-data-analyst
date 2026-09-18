# v0.18.0

## Engineering hardening and release review

- Added shared WCL OAuth state across forked HTTP workers, preventing token-refresh storms.
- Added compare-and-refresh handling for concurrent HTTP 401 responses so one stale response cannot invalidate a token another worker just refreshed.
- Added single-flight WCL query caching for report metadata and paginated event downloads; concurrent identical cache misses now share one network load.
- Added lightweight WCL diagnostics for network queries, token refreshes, cache hit/miss, single-flight waits and authentication retries.
- Removed lock-order inversions between cache/in-flight/statistics locks and kept compatibility with lightweight WCLClient test doubles that skip `__init__`.
- Added a bounded 15-minute DeepSeek coach-result cache keyed by evidence, personal/cohort context, model and speed mode. Re-opening an identical analysis can return immediately without repeat model calls.
- Added DeepSeek single-flight deduplication so simultaneous identical analysis requests share one model run instead of racing into duplicate API calls.
- Desktop version now loads from bundled `VERSION`; Mac and Windows PyInstaller builds include the version resource.
- Added offline `--self-test`; the Windows build executes the frozen EXE and fails packaging if the smoke test fails.
- Added GitHub Actions CI on Ubuntu + Windows and a real Windows Portable EXE artifact build.
- Windows packaging now treats every native command as fail-fast; pip/test/PyInstaller/self-test non-zero exits can no longer be ignored by Windows PowerShell.
- Added a repository self-review checklist and release validation notes; external reviewer bots are optional, not a release requirement.

## Validation

- 86 automated tests pass locally.
- `python -m compileall -q desktop_app.py core tests` passes.
- Added regression tests for shared token refresh, concurrent 401 recovery, WCL single-flight event downloads and repeated AI report cache hits.

---

# v0.17.0

## Dungeon target intelligence

- Added stable NPC target-focus fingerprints from WCL DamageDone/Casts + localized report actor metadata.
- Added boss recognition and empirical trash priority learning across repeated timed runs.
- Added per-pull focus normalization so a dangerous mob appearing in one pull is not diluted by whole-dungeon percentages.
- Added dungeon-wide target-role knowledge and same-spec numerical target-damage comparisons.
- Added current-season Mythic+ target-library learning from active WCL WorldData zones, with recent-run fallback.
- Added explicit guardrails: learned priority targets are empirical WCL behavior, not official mechanics.
- Added a native paired-bar target comparison chart to AI reports.
- Added AI coach rules for boss/priority damage, including “high total DPS but low key-target damage” findings.
- Behavior schema upgraded to v5.

## Validation

- 79 automated tests pass.
- Python compilation passes for the desktop app and all core modules.

---

# v0.16.0

## Timeline-quality WCL analysis

- WCL signatures now expose explicit `total_casts` per skill.
- Added within-pull cast cadence: median / high-end / max observed intervals plus per-pull cast counts.
- Added observed cast duration by pairing `begincast` with the corresponding successful cast.
- Added a stack-safe Buff state machine. `removebuffstack` changes stack state but does not terminate the aura; `removebuff` ends the window.
- Buff coverage now separates whole-run uptime from **combat-window uptime**, avoiding travel/RP/inter-pull dilution.
- Added locally reconstructed burst windows and Buff/burst overlap. A Buff that is active but misses every detected burst window is retained as zero-overlap evidence.
- Added per-pull breakdown with duration, casts, damage, deaths, top casts and top damage skills.

## Personal model + cohort improvements

- Personal history now aggregates recurring Buff combat coverage, within-pull cadence and Buff/burst alignment.
- Selected fights can be compared against the same timeline dimensions from the closest personal history.
- Same-spec cohort profiles now aggregate Buff combat coverage, cadence, and Buff/burst alignment instead of only skill frequency/damage metrics.
- Stored WCL samples use schema version 4. Older samples can be enriched once during sync/online learning, then become cheap skips again.

## AI coach improvements

- DeepSeek is explicitly told to prefer battle-only Buff coverage over whole-run uptime for combat judgments.
- Reports can distinguish “too few total casts”, “slow usage inside a pull”, “wrong pull allocation”, and “Buff/burst misalignment”.
- Observed `begincast -> cast` duration is clearly treated as log evidence, not theoretical spell cast time.
- Multi-fight compact payloads retain bounded Buff/cadence/burst/pull evidence so batch reports do not lose the richer timeline data.
- Raw WCL events still stay local; only bounded summaries are sent to the model to protect report speed.

## Validation

- 71 automated tests pass.
- Full Python bytecode compilation passes for `desktop_app.py` and all `core/*.py` modules.
