# Architecture

```text
WoWCombatLog.txt
      |
      v
+-------------------------+
| Local Parser            | Blizzard combat log -> normalized events
+-------------------------+
      |
      v
+-------------------------+
| Deterministic Analytics | teams / skills / pulls / deaths / burst / responsiveness
+-------------------------+
      |                 |                     \
      |                 |                      \
      v                 v                       v
+-------------+   +----------------+      +-------------------+
| Desktop UI  |   | Local Learning |      | WCL GraphQL       |
| tables      |   | SQLite cases   |      | external baseline |
+-------------+   +----------------+      +-------------------+
      |                 |
      +--------+--------+
               v
      +-------------------+
      | DeepSeek AI Layer |
      | structured JSON   |
      +-------------------+
               |
               v
      +-------------------+
      | User feedback     |
      | corrections       |
      +-------------------+
               |
               +----> Local Learning DB
```

## Design rules

1. Deterministic statistics never depend on an LLM.
2. API keys are never committed, exported, or stored in the learning database.
3. Raw logs stay local; AI receives compact structured summaries selected by the user.
4. Historical cases are auxiliary context and never override current-log evidence.
5. Only feedback-bearing cases are used by default for learning retrieval.
6. Network attribution requires direct telemetry support; Combat Log alone can only identify suspicious responsiveness patterns.
7. WCL is a benchmark/data source, not copied proprietary implementation.
8. Every major analytics feature should ship with tests.
