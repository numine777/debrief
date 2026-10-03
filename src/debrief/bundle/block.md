## Session records (ai-sessions protocol 0.1)

A human reviews your work through records you write, not by reading every diff.
The records are checked against the actual diff, so keep them accurate.
`bin/session` below means `<session>`.

**When:** any task that changes behavior or touches more than two files.
Skip questions, read-only investigation and trivial edits.

**Start:** before your first edit, run `bin/session start`. If it reports that this
repo isn't tracked, ignore the rest of this section. Otherwise follow
`<skills>/ai-session/SKILL.md`. The script prints your feature and session
directories, which sit outside the repo; write records only there, never in the repo.
If it reports an existing brief, read the brief and the latest journal first.

**During:** after each decision, finding, test run or blocker, append to your
session's `journal.md` with your file-editing tool: a `### <time> · <kind>` heading
(time from `bin/session now`, the only line it prints on stdout), then 1-5 lines.
`bin/session now` also prints pending developer requests on stderr (close the leg,
read new feedback); read all of its output and follow them.
Kinds: plan, decision, finding, change, test, blocker, handoff.
Run builds and tests as `bin/session run <command>` so results are recorded.
Commit each logical change as you finish it. The message is what a reviewer reads
above the diff: an imperative subject (72 characters max), then 1-4 lines on what
behavior changed and why. Never mention these records in commits or the repo.

**Finish:** before your final message, append a `handoff` entry (done, unverified,
next), then run `bin/session close` (`close blocked` if you are stuck). If more work
arrives later in the conversation, run `bin/session start` again before editing.
Close out the leg only when the developer asks, in chat or through a request from
`bin/session`: then follow `<skills>/ai-session-closeout/SKILL.md` and complete every
step; it opens its own session.

**Rules**
- Describe what the code does, not what you meant it to do. Record any gap under Divergences.
- Declare every loop, retry, state machine, concurrency point, error-handling change
  and external call you add or modify, with its invariant.
- Anchor by symbol or line range; a path alone explains nothing.
- Never report a test as passing unless you ran it in this session.
- One logical change per commit. If you must combine changes, say why in the journal.
- Never close a leg on your own initiative.
- Only the top-level agent writes records; subagents report back to it.
- Set `epic` in the brief only when the user names one; never infer it.
- When unsure of a field, read `<skills>/ai-session/reference.md`.
