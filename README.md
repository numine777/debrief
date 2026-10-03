# Debrief

Debrief lets you review a coding agent's end-to-end work without reading every
diff. Agents keep short structured records while they work: a brief, the
systems they built, the tests that prove them and the decisions behind them.
Debrief checks those claims against git and serves an offline viewer that links
every explanation to the code it describes, and every changed hunk back to an
explanation, or flags it.

- **Harness-agnostic.** Any agent that can read an instruction file and write
  files can take part: Devin Local, Claude Code, Codex, pi and others.
- **Zero footprint in your projects.** Nothing is written into a repository
  Debrief supports: no files, refs or commit trailers. Records live in an
  archive under `~/.local/share/ai-sessions`.
- **Offline by construction.** Python 3.9+ standard library, one file
  (`debrief.pyz`), every asset vendored, and a content security policy that
  blocks network requests from the viewer.
- **Data stays where you allow.** Debrief contacts only allowlisted hosts and
  refuses cloud-synced archive folders.

## Quick start

```sh
python3 debrief.pyz install          # agent instructions, skills and launchers for the harnesses on this host
debrief init ~/src/my-repo           # track a repo (writes nothing into it)
# ...let an agent work on a branch; it runs debrief-session as it goes...
debrief serve                        # http://127.0.0.1:7319
```

On a remote Linux host, forward the port: `ssh -L 7319:127.0.0.1:7319 host`.

## How it works

1. **The protocol.** `debrief install` adds a short always-on block to each
   harness's global instructions (`~/.claude/CLAUDE.md`, `~/.config/devin/AGENTS.md`,
   `~/.codex/AGENTS.md`, `~/.pi/agent/AGENTS.md`) and two skills to
   `~/.agents/skills`. The block tells the agent when to start a session, how to
   keep its journal, to run tests through the run ledger, to commit atomically,
   and to close out a leg only when you ask.
2. **bin/session.** Agents call `~/.local/bin/debrief-session` (install writes
   the actual path into the instructions): `start`, `now` (the journal time
   stamp on stdout; logged commits and your requests on stderr), `run <cmd>`
   (records exit codes and the uncommitted files it tested), `changed` (what
   explains each changed hunk), `close` and `publish`.
3. **Legs.** A feature is a branch; a leg is the work between two closeouts,
   which you trigger. At closeout the agent writes `brief.md`, `systems/*.md`,
   `tests.yaml` and `decisions/*.md` against the final code, and `publish`
   commits them to the project's archive repository.
4. **Evidence.** Ingest checks the records against git: claim coverage of every
   changed hunk (an anchor must reach the changed lines, not just the diff's
   context), stale and ambiguous anchors, undeclared loops, retries, locks and
   external calls, weakened tests, test claims against the run ledger, developer
   and non-atomic commits, thin commit messages. Commits merged in from the
   default branch aren't the feature's, and amends and rebases don't
   misattribute work. Patches, commits and file versions are archived, so a
   feature stays readable after its branch is gone, and a host that lacks the
   feature's commits keeps the archived evidence instead of replacing it.
5. **The viewer.** Brief, system map, systems, tests, review queue, a diff in
   Story mode (commits in order, led by their messages) or Systems mode (hunks
   under each system's Change section), timeline, commit pages by SHA, search.
6. **The review loop.** Click a line number to comment. Comments are private
   unless you share them; turn open ones into a copy-ready prompt for the agent,
   or queue them as `feedback.md`, which `bin/session now` relays. Mark hunks
   reviewed; marks clear when the agent changes that code. Press **Close leg**
   to ask the agent to close out.

## Commands

| Command | What it does |
| --- | --- |
| `debrief install [--harness devin,claude,codex,pi] [--claude-settings]` | Install agent instructions, skills and launchers. Idempotent; upgrades replace only Debrief's block. |
| `debrief uninstall` | Remove what install wrote. Keeps the archive. |
| `debrief init [repo] [--remote URL]` | Track a repo by its git common directory (worktrees share it). |
| `debrief check [path]` | Validate records. |
| `debrief ingest [repo...]` | Compute evidence and refresh the index. |
| `debrief serve [--watch] [--port N]` | The viewer on 127.0.0.1. `--watch` re-ingests as records and repos change and syncs archives. |
| `debrief service install` | Run `serve --watch` as a systemd user unit or launchd agent. |
| `debrief sync [project] [--remote URL]` | Commit, pull and push project archive repositories. |
| `debrief show <sha>` | The feature, leg and systems behind a commit, including squash commits. |
| `debrief squash <feature> [--model]` | Write one consolidated record (and, experimentally, a model-written summary). |
| `debrief export <feature> [-o file.html]` | One self-contained HTML file for a PR or a teammate. |
| `debrief request-close [feature]` | Ask the agent to close out the open leg. |
| `debrief hub ...` | Run or administer the shared hub (below). |

## Harness notes

- **Devin Local (Devin Desktop over Remote-SSH).** Install on the Linux host.
  Add the archive (`~/.local/share/ai-sessions`) to the agent's writable paths,
  and allowlist `~/.local/bin/debrief-session start`, `now`, `changed`,
  `close` and `publish`. Never allowlist `debrief-session run *`, which runs
  its argument; allowlist specific forms such as `debrief-session run make test`.
  When Claude Code is also installed, the block lives once in `~/.claude/CLAUDE.md`,
  which Devin reads too.
- **Claude Code.** `debrief install --claude-settings` also adds a SessionStart
  hook (the session state lands in context, including after compaction), the
  archive as an additional directory, and allow rules for the safe commands.
- **Codex.** The block goes in `$CODEX_HOME/AGENTS.md`; keep that file under
  Codex's 32 KiB limit.
- **pi.** The block goes in `~/.pi/agent/AGENTS.md`; pi reads `~/.agents/skills`.

## Configuration

`~/.config/debrief/config` (INI):

```ini
[residency]
# Hosts Debrief may contact: archive remotes, the hub, compaction endpoints.
allow_hosts = git.corp.example, hub.corp.example, *.intranet.example

[viewer]
port = 7319

[sync]
pull_interval = 120

[experimental]
compaction = false

[compaction]
provider = intranet

[provider.intranet]
api = openai                  # or anthropic
base_url = https://models.intranet.example/openai/v1
model = gpt-5-mini
api_key_env = DEBRIEF_COMPACTION_KEY
auth_header = api-key         # authorization (default), api-key or x-api-key
```

Per-project flag rules live in `<archive>/projects/<id>/rules.json`:
`{"disable": ["todo"], "severity": {"retry": "medium"}, "rules": [...]}`.

## Debrief Hub (team review)

The hub serves the same viewer to a team over TLS. Hosts sync each project's
archive to a bare repository the hub owns, over SSH with existing keys; the hub
never holds project repositories and makes no outbound connections.

```sh
debrief hub init hub.corp.example         # layout, hub.json, a self-signed cert if openssl exists
debrief hub adduser alice                 # prints alice's access token once
debrief hub repo <project-id>             # bare repo + the command hosts run to sync to it
debrief hub grant alice <project-id> --role reviewer   # owner, reviewer or reader
debrief hub serve --bind 0.0.0.0          # https://hub.corp.example:7320/
```

On each host: `debrief sync <project-id> --remote ssh://hub.corp.example/<path>`,
with the hub's host name in `allow_hosts`. Shared comments, replies, queued
feedback and Close leg requests made on the hub flow back to the developer's
host through the archive and reach the agent at its next `bin/session now`.
Replace the self-signed certificate with one from your internal CA.

Access: readers see projects and keep private review marks; reviewers also
comment, reply, resolve and request closeouts; only a comment's author edits,
shares or deletes it. Revoking a user's tokens (`debrief hub revoke <user>`)
ends their signed-in sessions at once. Failed sign-ins are slowed and counted
per address, but a valid token always works. Exports contain only the exported
feature, never another project's records that share a commit or an epic.

## Where things live

```text
~/.local/share/ai-sessions/          the archive (AI_SESSIONS_DIR overrides)
  projects/<project-id>/             one git repository per project, optional remote
    project.json
    features/<feature-id>/           brief.md, systems/, tests.yaml, decisions/, legs/,
                                     sessions/<id>/{session.json, journal.md, runs/},
                                     comments.json, feedback.md, evidence/, squash/
  .index.sqlite, .viewer/, .state/   host-local, never synced
~/.local/share/debrief/debrief.pyz   the installed zipapp
~/.config/debrief/config             configuration
```

## Development

```sh
make test         # unit, integration and (with Playwright) browser tests
make test-py39    # the suite on Python 3.9
make build        # dist/debrief.pyz, reproducible
make dogfood      # rebuild and reinstall the launchers from source
```

Debrief was built with its own protocol. Its records, legs and evidence live in
a separate archive (never in this repository), delivered alongside it.

## License

Apache-2.0. Vendored components keep their own licenses (see
`src/debrief/vendor/yaml/LICENSE`, `src/debrief/static/vendor/LICENSES.txt` and
`src/debrief/static/fonts/LICENSES.txt`).
