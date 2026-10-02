### 2026-10-02T19:41Z · plan
What you will do this session and in what order, in one to five lines.

### 2026-10-02T20:15Z · test
`pytest tests/queue -q` passed (14 tests); proves items past max_attempts are dead-lettered.

### 2026-10-02T21:02Z · handoff
Done: retry queue with deadline-aware drain. Unverified: two workers draining at once.
Next: wire the dead-letter metric into the dashboard.
