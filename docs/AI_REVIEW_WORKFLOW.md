# GitHub AI review workflow

This repository keeps implementation and review separate.

1. Human/product request becomes a scoped GitHub Issue with objective acceptance criteria.
2. ChatGPT implements on one feature branch and opens a PR linked to the Issue.
3. GitHub Actions runs unit tests on Linux/Windows and builds the Windows portable EXE.
4. GrokBot reviews the actual diff against the Issue and `AGENTS.md`.
5. ChatGPT responds to every `FIX-xxx` with a focused code/test change or a technical disagreement backed by evidence.
6. GrokBot re-reviews. Only `REVIEW: PASS` moves the PR to human acceptance.

Durable PR markers:

- `GROKBOT: REVIEW REQUESTED`
- `CHATGPT: READY FOR GROKBOT RE-REVIEW`
- `REVIEW: PASS`
- `HUMAN ACCEPTANCE REQUIRED`

The reviewer should pay special attention to WCL GraphQL compatibility, rate-limit/API-point waste, concurrency/cache races, SQLite idempotency, DeepSeek evidence grounding, personal/cohort leakage, target-focus false positives, Qt UI blocking, Windows packaging, and secret handling.
