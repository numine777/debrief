# Record reference (ai-sessions protocol 0.1)

Read this when you are unsure of a format or field. Templates for each file are
in `templates/` next to this file.

## Where things live

```text
<archive>/projects/<project-id>/features/<feature-id>/
  brief.md                      you, at closeout
  systems/<system-id>.md        you, at closeout
  tests.yaml                    you, at closeout
  decisions/<nnn>-<slug>.md     you, at closeout
  sessions/<session-id>/
    journal.md                  you, during the session (append-only)
    session.json                bin/session (never edit)
    runs/*.json                 bin/session run (never edit)
  legs/<leg-id>.json            bin/session (never edit)
  comments.json, feedback.md    the developer's viewer (read feedback.md when relayed)
  evidence/                     Debrief ingest (never edit)
```

`bin/session start` prints the feature dir. Never write records in the repository.

## Ids

- Feature id: the branch name with `/` replaced by `--` (set by bin/session).
- System, test and decision ids: kebab-case slugs, unique within the feature,
  such as `retry-queue`. A system's id is its file name without `.md`.
- Decision files: `<nnn>-<slug>.md`, numbered from `001`.
- Critical path references: `<system-id>/<critical-path-id>`, such as `retry-queue/drain-loop`.

## Anchors

An anchor links prose to code: `{path, symbol, lines, role}`.

- `path`: relative to the repository root, such as `src/queue/retry.py`.
- `symbol`: a function, method or class, such as `RetryQueue.drain` or `parse_config`.
  Preferred: it survives edits elsewhere in the file.
- `lines`: a range in the code as it is now, such as `"40-88"`, when there is no
  good symbol (configuration, a block inside a long function).
- `role`: optional, such as `core`, `support`, `config` or `test`. Use
  `role: removed` for code the feature deleted; that anchor resolves against the
  feature's base instead of the current code.

Only anchors with a symbol or line range explain code. A path-only anchor is a
weak claim. Files too trivial for a system (a renamed import, a version bump,
vendored code) go under `incidental` in the brief, as paths or globs such as
`vendor/**` (`*` stays within a directory, `**` spans directories).

## journal.md

Append-only. Each entry is a heading, then one to five lines:

```markdown
### 2026-10-02T19:41Z · finding
The worker pool already caps concurrency at 4, so the queue needs no lock of its own.
```

Time is UTC to the minute, from `bin/session now` (the only line it prints on stdout). Kinds:
`plan`, `decision`, `finding`, `change`, `test`, `blocker`, `handoff`. A session
opens with `plan` and ends with `handoff`. A handoff says what is done, what is
unverified and what is next.

## brief.md

Rewritten at every closeout, for the whole feature.

```yaml
---
feature_id: feat--retry-queue          # the feature dir name
title: Retry failed webhook deliveries
epic: webhook-reliability              # optional; only when the user names one
status: in_progress                    # in_progress | ready_for_review | merged | abandoned
sessions: [20261002T193000Z-4f2a]      # optional: Debrief lists the feature's sessions itself
review_first:                          # at most three
  - {target: retry-queue/drain-loop, why: "Only unbounded loop in the feature"}
incidental: [src/util/strings.py, vendor/**]   # changed, not worth a system; globs allowed
---
```

Body sections, in order, as `##` headings: Intent · What was built ·
Divergences · Risks and gaps · Follow-ups. Divergences covers differences from the
intent and from the plan; write "None" if there are none.

`review_first` targets are system ids, `system/critical-path` ids, decision ids
or `tests/<test-id>`.

## systems/\<id>.md

One per coherent mechanism the feature adds or changes. The primary unit of
explanation: a reviewer reads systems instead of the diff.

```yaml
---
id: retry-queue
title: Webhook retry queue
change: new                            # new | modified | touched
depends_on:
  - {system: ingest-handler, relation: "enqueues failed deliveries"}
anchors:
  - {path: src/queue/retry.py, symbol: RetryQueue.drain, role: core}
  - {path: src/queue/retry.py, lines: "12-30", role: config}
critical_paths:
  - id: drain-loop
    kind: loop                         # loop | retry | state-machine | concurrency |
                                       # error-handling | external-io | migration
    anchor: {path: src/queue/retry.py, symbol: RetryQueue.drain}
    invariant: "Terminates when the queue is empty or the deadline passes."
    failure_mode: "A stuck endpoint holds a worker forever."   # optional
decisions: [002-bounded-queue]
---
```

Body sections: Purpose (two or three sentences) · Change (what this feature did
to the system, in one to three sentences; "New" for a new system) · How it works
(control and data flow as the code now stands) · Limitations.

`change`: `new` for a system this feature creates, `modified` when it changes
behavior, `touched` when it only adapts to other changes.

Declare every loop, retry, state machine, concurrency point, error-handling
change and external call (network, disk, subprocess) that you add or modify, with
the invariant that must hold. Debrief flags such constructs in the diff that no
critical path anchors.

## tests.yaml

```yaml
tests:
  - id: dead-letter-after-max
    validates: [retry-queue/drain-loop]          # system or system/critical-path ids
    claim: "Items past max_attempts are dead-lettered, not retried."
    kind: unit                                    # unit | integration | e2e | property | manual
    anchors: [{path: tests/queue/test_retry.py, symbol: test_dead_letter_after_max}]
    command: "pytest tests/queue/test_retry.py -k dead_letter"
    claimed_result: pass                          # pass | fail | not_run
gaps:
  - {validates: retry-queue/drain-loop, note: "No test for two workers draining concurrently"}
```

- One entry per test group that proves a behavior, not one per test function.
- `command` is what you ran with `bin/session run`. Debrief matches it word for
  word against the run ledger (shell quoting and `-v`/`-q` style flags aside), so a
  run of `pytest tests/a.py` doesn't verify a claim about `pytest`.
- `claimed_result: pass` only if a recorded run in this session passed.
- `gaps` is required. Write `gaps: []` only when you know of none.

## decisions/\<nnn>-\<slug>.md

```yaml
---
id: 002-bounded-queue
title: Bounded in-memory queue
status: accepted                  # accepted | superseded
reversibility: cheap              # cheap | costly | one-way
systems: [retry-queue]
---
```

Body sections: Context · Options · Choice and why.

## Commits

One logical change per commit, committed as soon as it works. Message: an
imperative subject of at most 72 characters, a blank line, then one to four lines
on what behavior changed and why. No file lists, and nothing about these records.
Debrief maps commits to legs from its own logs; never add trailers or ids.

## Commands

| Command | Does |
| --- | --- |
| `bin/session start [--task "..."]` | Opens a session (and a leg if none is open). Prints paths and requests. |
| `bin/session now` | Prints the journal time on stdout; logs new commits and relays requests on stderr. |
| `bin/session run <command>` | Runs a command and records it in the run ledger. |
| `bin/session changed` | Refreshes the evidence and lists what explains each changed file and hunk. |
| `bin/session close [complete\|blocked\|abandoned]` | Ends the session (publish does it at closeout). |
| `bin/session publish` | Closes the session and the leg: evidence, archive commit and sync. Only at closeout. |
| `debrief check` | Validates the records (if Debrief is on your PATH). |
