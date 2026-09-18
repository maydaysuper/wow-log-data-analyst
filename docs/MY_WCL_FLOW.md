# My WCL targeted-analysis flow

## Goal

The user should not be forced to upload `WoWCombatLog.txt` when the fight already exists on Warcraft Logs.

## Flow

1. User opens the native desktop app.
2. User enters a WCL character ID, character URL, `region:realm:name`, or WCL user ID.
3. Character mode uses `Character.recentReports`; user mode uses `ReportData.reports(userID: ...)`.
4. The app lists report metadata without loading event payloads yet.
5. After a report is selected, the app fetches report master data and fights.
6. Character mode resolves the report actor and filters fights to those containing that actor.
7. The user chooses one fight / M+ run.
8. The app fetches only the selected player's relevant event families.
9. Events are converted to the same evidence schema used by local-log learning.
10. The sample is deduplicated and stored in the class/spec cohort.
11. DeepSeek optionally analyzes the selected fight with historical corrections and learned class context.

## Why two stages

Recent report discovery is intentionally cheap. Large event requests happen only after the user chooses a fight. This reduces WCL rate-limit use and makes the desktop UI faster.

## Evidence boundary

- WCL current-fight event evidence is first-party fight evidence for the analysis.
- Learned cohort patterns are empirical references, not theoretical optimal rotation rules.
- A cast gap is an action-timeline anomaly, not proof of network latency.
