# AI Learning Engine (v0.5.0)

## What “learning” means here

DeepSeek API calls do **not** automatically retrain the public DeepSeek model for this desktop app. Instead, the app implements a local case-memory / retrieval layer:

1. Deterministic code computes combat statistics.
2. DeepSeek produces a structured JSON diagnosis.
3. The result is stored locally in SQLite.
4. The user marks the report as accurate / partially accurate / inaccurate and can add the confirmed cause or a correction.
5. Similar future analyses retrieve only feedback-bearing cases and reusable lessons.
6. Those memories are supplied to DeepSeek as auxiliary context, never as stronger evidence than the current log.

This gives the product practical iterative behavior without pretending that API calls modify model weights.

## Privacy

- Database: `analyst_memory.sqlite3` in the OS user-data directory.
- API keys are never stored in the database.
- Raw full Combat Logs are not stored in the learning database.
- Stored payloads contain the compact structured summaries used for AI analysis.
- Users can delete individual cases or clear the entire learning database.

## Similarity

The retriever combines:

- class/spec match
- dungeon match
- key-level proximity
- analysis mode
- similarity of numeric structured features
- positive feedback / explicit correction bonus

The system deliberately avoids embeddings for now so no extra embedding API/provider is required.

## Feedback → reusable lesson

When enabled, DeepSeek converts explicit user feedback into a cautious lesson with:

- `pattern`
- `when_to_apply`
- `when_not_to_apply`
- `confidence`

The lesson must be grounded in user-confirmed information. It cannot invent facts.
