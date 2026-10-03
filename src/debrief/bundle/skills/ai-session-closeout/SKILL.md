---
name: ai-session-closeout
description: Close out a leg of Debrief session records - write the brief, systems, tests and decisions against the final code, then publish them. Use only when the developer asks you to close out, wrap up or publish the leg, or when bin/session relays a close request.
---

# Closing out a leg

A leg is the work between two closeouts. The developer triggers every closeout;
never close a leg on your own initiative. Complete every step, in order. Paths
are relative to the feature dir that `bin/session start` printed. Formats and
fields are in `ai-session/reference.md`; templates are in `ai-session/templates/`.

0. **Open a session.** Unless a session is already open in this conversation
   (you ran `bin/session start` and haven't closed it), run
   `bin/session start --task "Close out the leg"`. The closeout's commits, test
   runs and handoff belong to it.

1. **Commit the code.** Run `bin/session changed`. Commit any uncommitted code
   atomically, one logical change per commit (see the ai-session skill).
   Untracked files count: commit the ones that belong to the change and delete
   scratch files; publish refuses while any remain.

2. **Systems.** Write or update `systems/<id>.md` for every mechanism this feature
   adds or modifies, covering the whole feature, not just this leg. A leg that
   changed no code (an investigation, notes) needs no systems.
   - Fill in Purpose, Change ("New" for a new system), How it works, Limitations.
   - Anchor by `symbol` or `lines` against the code as it is now; a path alone
     is a weak claim.
   - Declare every loop, retry, state machine, concurrency point, error-handling
     change and external call under `critical_paths`, each with its invariant.
   - Link related systems with `depends_on` and a short `relation`.

3. **Tests.** Update `tests.yaml`: one entry per test group that proves a
   behavior, what it validates (`system` or `system/critical-path`), its claim,
   its command and `claimed_result`. Run each test group with `bin/session run`
   before claiming `pass`, and copy the command you ran into `command` word for
   word. List known gaps under `gaps`; write `gaps: []` only if there are none.

4. **Decisions.** Write `decisions/<nnn>-<slug>.md` for every non-obvious choice:
   Context, Options, Choice and why, and its reversibility.

5. **Brief.** Rewrite `brief.md` for the whole feature so far: Intent, What was
   built, Divergences (from the intent and from the plan), Risks and gaps,
   Follow-ups. Put the top three review targets in `review_first`. Set `epic`
   only if the user named one.

6. **Self-audit.** Run `bin/session changed` again. It refreshes the evidence and
   lists every changed hunk nothing explains, by file and line: anchor each from
   a system (symbol or line range) or list the file under `incidental` in the
   brief. Run `debrief check` if it is on your PATH, and fix what it reports.

7. **Hand off.** Append the `handoff` journal entry (done, unverified, next).
   Don't run `bin/session close`: publish closes the session.

8. **Publish.** Run `bin/session publish`. It closes the session and the leg,
   computes evidence, commits the records to the project's archive repository
   and syncs them. If it refuses, fix what it lists (commit code fixes as usual;
   your session is still open) and run it again. Report what it printed to the
   developer, including anything it says nothing explains.
