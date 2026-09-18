# GrokBot review prompt

Review the current PR for `maydaysuper/wow-log-data-analyst` against its linked Issue, `AGENTS.md`, existing tests, and the actual diff.

Do not rewrite the branch. Review only.

Classify every finding as BLOCKER, IMPORTANT, or OPTIONAL. Number every BLOCKER/IMPORTANT `FIX-001`, `FIX-002`, ... and include:

- affected file/function or module;
- concrete problem and trigger;
- user/runtime impact;
- violated acceptance criterion or invariant;
- testable expected fix;
- whether a regression test is required.

Prioritize:

1. WCL GraphQL/API correctness and pagination;
2. duplicated requests / rate-limit waste / cache or concurrency races;
3. current-fight leakage into personal/cohort baselines;
4. false AI certainty, especially lag attribution, class theory, and learned priority targets;
5. SQLite idempotency/concurrency;
6. GUI-thread blocking and performance regressions;
7. Windows frozen-package correctness;
8. secrets/privacy;
9. missing regression tests.

If any BLOCKER/IMPORTANT remains, end with `CHATGPT: ACTION REQUIRED`.
If all acceptance criteria and CI pass with zero BLOCKER/IMPORTANT findings, end with `REVIEW: PASS` and publish a short `HUMAN ACCEPTANCE REQUIRED` checklist for Windows launch, WCL live sync, AI report latency, and target-focus chart sanity.
