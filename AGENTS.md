# WoW Log Data Analyst — AI engineering contract

## Product intent

This is a native PySide6 World of Warcraft Retail combat-log/WCL analysis tool. The deterministic Python layer owns statistics; DeepSeek turns verified evidence into concise Chinese coaching. WCL-derived patterns are empirical references, not official class mechanics.

## Roles

- **ChatGPT in this project = implementation owner.** Make focused changes, add regression tests, run checks, and update the PR.
- **Independent reviewer bots are optional.** The implementation owner performs a concrete diff/test review before delivery; external review can be added later if the user requests it.
- **Human user = product owner / merge authority.** Only the human decides merge/release acceptance.

## Invariants

1. Do not expose API secrets in source, logs, PRs, or test fixtures.
2. Do not claim network lag from Combat Log alone. Death/rez/travel/team-wide downtime must stay excluded from single-player stall evidence.
3. Do not turn WCL correlations into official rotation/mechanic claims.
4. Same-spec comparisons must disclose sample quality and must not mix different dungeons as if they were one homogeneous cohort.
5. Current-fight data must not contaminate the baseline used to judge that same fight.
6. Prefer stable WCL NPC/ability IDs over display-name guessing.
7. Expensive WCL/API work must stay off the Qt GUI thread.
8. Windows portable packaging must produce a frozen executable that passes `--self-test`.

## Required validation

Before review:

```bash
python -m compileall -q desktop_app.py core tests
PYTHONPATH=. pytest -q
```

GitHub Actions must also pass the Windows portable build and frozen self-test.

## Self-review contract

Before delivery, review the actual diff for **BLOCKER**, **IMPORTANT**, and **OPTIONAL** findings. Every BLOCKER/IMPORTANT must identify file/function, concrete failure mode, impact, expected fix, and regression-test need. Delivery requires zero unresolved BLOCKER/IMPORTANT findings and green CI.
