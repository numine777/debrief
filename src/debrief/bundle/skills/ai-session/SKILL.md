---
name: ai-session
description: Keep Debrief session records while you change code - open a session, keep the journal, record test runs and commit atomically. Use at the start of any coding task that changes behavior or touches more than two files, and when resuming a feature that already has records.
---

# Keeping session records

A developer reviews your work in Debrief, a viewer that reads the records you
write and checks them against git. Records live in an archive outside the
repository. Write them only where `bin/session start` tells you, never in the repo.

`bin/session` means `~/.local/bin/debrief-session`.

## 1. Start

Run this before your first edit:

```sh
bin/session start --task "One-line restatement of the assignment"
```

- If it says the repo isn't tracked, stop following this skill; no records are needed.
- Note the **feature dir** and **journal** paths it prints. Your journal is the
  `journal.md` in your session directory.
- If it reports an existing brief, read `brief.md` and the latest journal before
  doing anything else. They are the memory of earlier sessions on this feature.
- If it prints a REQUEST or FEEDBACK line, handle it (see section 5).
- If your file tools can't write to the feature dir, tell the developer: the
  harness needs the archive path in its writable paths.

## 2. Keep the journal

The journal is append-only. Add an entry whenever something worth knowing
happens, using your file-editing tool. Get the time from `bin/session now`
(first line of its output):

```markdown
### 2026-10-02T19:41Z · decision
Bounded in-memory queue instead of a durable one; loss on restart is acceptable
because upstream redelivers.
```

| Kind | Write it when |
| --- | --- |
| `plan` | First entry of every session: what you will do and in what order. |
| `decision` | You choose between real alternatives. Name the rejected option and why. |
| `finding` | You learn something about the code that changes the approach. |
| `change` | You finish a logical change (usually right after committing it). |
| `test` | You ran tests: command, result, what it proves. |
| `blocker` | Something stops you or needs the developer. |
| `handoff` | Last entry: done, unverified, next. |

One to five lines per entry. Facts, not narration: "Retry loop now stops at the
deadline; previously it could spin forever" beats "Worked on the retry logic".

Read every line `bin/session now` prints after the time stamp: it relays the
developer's requests.

## 3. Run tests and builds through the ledger

```sh
bin/session run pytest tests/queue -q
bin/session run "make build && make test"
```

The ledger records the command, exit code, duration and output tail. Only a run
recorded this way counts as evidence that a test passes. Never report a test as
passing unless you ran it in this session.

## 4. Commit atomically

Commit each logical change as soon as it works: one behavior per commit, tests
with the code they test. The message is the summary a reviewer reads above the
diff:

```text
Stop retrying webhook deliveries after the deadline

Drain now checks the deadline before each attempt, so a stuck endpoint can no
longer hold a worker past its shift. Items that run out of time are requeued.
```

- Subject: imperative, at most 72 characters.
- Body: 1-4 lines on what behavior changed and why. Not a file list.
- Never mention Debrief, the journal or these records in commits or the repo.
- If you must combine changes in one commit, say why in a journal entry.

## 5. Developer requests

- **Close request** ("REQUEST ... close out leg-NN"): finish the current change,
  then follow `ai-session-closeout/SKILL.md` and complete every step.
- **Feedback** ("FEEDBACK ... feedback.md"): read the file in the feature dir.
  Address each item, log what you changed in the journal, and commit each fix.

## 6. Finish

Before your final message:

1. Append a `handoff` entry: what is done, what is unverified, what is next.
2. Run `bin/session close` (or `bin/session close blocked` if you are stuck).

Close the leg only when the developer asks. Brief, system, test and decision
files are written at closeout, against the final code; don't write them during
the session unless the developer asks, because their anchors go stale.

When unsure of a format or field, read `reference.md` next to this file.
