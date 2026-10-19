# claude-ops

A task queue for any git project. You queue work on a dashboard, or talk it through in a design
chat that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A gate (your test commands) guards every merge into the dev branch. The main branch is
the golden version: it changes only when you promote.

## Use it

```bash
./opsctl start                       # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open                        # dashboard: http://127.0.0.1:8788  (…/#design, …/#chat open those tabs)
./opsctl status | log | stop | restart   # restart waits until no task is running a command; --now doesn't
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
```

On the dashboard you can type a full path or just a name. A name like `mygame` means a folder next to
your other projects (usually `~/dev/mygame`). If it doesn't exist, the dashboard offers to create it
as a new git repo with one empty commit.

`add-project` detects Unity (`scripts/unity.sh` + `Assets/`), Node (`package.json`) and Python
projects and writes a starter config. Read it, adjust it, then click **Reload projects** (or
`./opsctl restart`).

## Flow (per project)

```
 Design tab ──(you agree)──┐
 Queue tab ────────────────┴─▶ scheduler ──(up to N at once)──▶ worker: claude -p in <worktrees>/t<id>
                                                                  │ commits on task/<id>-<slug>
                                                                  ▼
                               rebase onto dev ─▶ gate: the [[gate]] commands, in order
                                  │ conflict / fail: the worker is resumed with the error (max 3 rounds)
                                  ▼ pass
                   "hold for review"? ── yes ─▶ build, wait for Try / Approve / Send back
                                  │ no
                                  ▼
                            merge --no-ff into dev   (<worktrees>/dev)
                                  │
                 you click Promote ▼
   release task: snapshot dev ─▶ gate ─▶ Claude builds and plays it (blocks, or passes)
                                  ▼ pass
        build the snapshot in <worktrees>/main ─▶ fast-forward main in your checkout
```

## main is the golden version

- **dev** gets every task that passes the gate. It is the newest version.
- **main** moves only when a **release** passes, and its build (`<worktrees>/main`) always matches it.
- **Promote** queues a release task ahead of other work. It takes a snapshot of dev, runs the gate,
  then a Claude release checker (`release_prompt.md`) builds the app and plays it the way a player
  would. If the checker passes it, main moves to exactly that snapshot; anything merged into dev
  meanwhile waits for the next release. The checker never changes code: problems it finds are queued
  as ordinary tasks, and a blocked release leaves main as it was.
- Promote refuses while a release is already open, while the main build is running, or while your
  checkout is off main or has uncommitted changes. It checks again before moving main. If something
  blocks it then, fix it and click **Retry**: that promotes without checking again.
- With nothing new on dev, Promote only rebuilds main (no gate, no checker).
- Commits made straight to main are merged into dev when the release takes its snapshot (no gate).
- If the build fails, main and its build stay as they were.
- Nothing is ever pushed. Push main yourself when you want to.

## Design mode

The Design tab is a chat with Claude about one project. It reads the project's docs and the latest
dev code but cannot edit anything (`--permission-mode dontAsk` with read-only tools). When you
agree on the work, ask it to queue: it writes self-contained task briefs, and they appear as chips
under its reply. Each message it gets starts with that project's current queue, so it avoids duplicates.

## Chat mode

The Chat tab is plain Claude in the project folder, as if you ran `claude` there. It works in your
checkout (`repo`), not a worktree, so it can read, edit, commit and run commands there, with your own
settings, `CLAUDE.md` and MCP servers. Each chat keeps one Claude session across messages.

- Nobody can answer permission prompts on the dashboard, so it runs in auto mode: actions that would
  prompt are denied, and Claude tells you what it couldn't do.
- The chat header shows `cd <repo> && claude --resume <id>`. Run it to carry on in a terminal, where
  you can answer prompts. Don't send from both places at once.
- Edits land in your checkout and nothing else. Promote refuses while the checkout has uncommitted
  changes, so commit or discard them first.

## Project config: `~/.claude-ops/projects/<name>.toml`

```toml
title = "My Game"
repo = "/Users/you/dev/mygame"
main_branch = "main"
dev_branch = "dev"                 # created from main if missing; add-project picks ops-dev if dev exists
worktrees = "/Users/you/dev/mygame-wt"   # default: <repo>-wt
max_parallel = 0                   # per-project cap; 0 = only the global "Parallel" limit

[worktree]
clone = ["Library"]                # APFS-cloned into each new worktree (warm caches)
setup = ["npm ci --silent"]        # run in each new worktree; $OPS_REPO is your checkout

[[gate]]                           # run in order; any non-zero exit fails the gate
name = "test"
run = "scripts/unity.sh test-edit"
log = "Logs/test-editmode.log"     # optional: read on failure
errors = "error CS"                # optional: lines of `log` to show
report = "Logs/test-editmode.xml"  # optional: NUnit/JUnit XML; failed cases are shown

[app]                              # optional: something to build and run
build = "scripts/unity.sh build"
launch = "open -n 'Builds/macOS/MyGame.app'"   # run from the worktree for Play / Try
process = "Builds/macOS/MyGame.app"            # pgrep pattern (prefixed with the worktree path)
artifact = "Builds/macOS/MyGame.app"           # must exist after build; kept as a backup on promote

[worker]
env = { GAME_PORT = "{port}" }     # extra env; {port} is the worker's own free port ($OPS_PORT)
notes = """Project-specific guidance added to every worker brief."""

[design]
notes = """Project-specific guidance for design chats."""
```

## Other details

- **Parallelism.** "Parallel" on the dashboard is the global limit (default 3). Workers also split
  big tasks into subtasks (with "after" ordering) and use subagents inside a task.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes.
- **Ask the worker.** Once a worker stops (review, needs you, failed, done), you can ask it questions
  on the task page. Each question resumes its own Claude session, read-only, so it remembers the work.
  It also remembers the questions: if you then send feedback or retry, it builds on what you discussed.
- **Notifications.** macOS notifications when a task needs you, fails, or finishes.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout, dev or main.

## Files

| Path | What |
|---|---|
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, promote, design and project chats |
| `worker_prompt.md`, `release_prompt.md`, `design_prompt.md` | briefs for workers, release checkers and design chats |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log` |
| `~/.claude-ops/opsd.log` | daemon log |

Env overrides: `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_DATA` (~/.claude-ops).
Keep the host on 127.0.0.1: the API can start work on your machine. For phone access, use Tailscale
rather than binding to your LAN. It is served tailnet-only on its own HTTPS port (443 is taken by
another dashboard): `tailscale serve --bg --https=8443 http://127.0.0.1:8788`, then open
https://adams-mac-mini.pintail-bramble.ts.net:8443. Undo with `tailscale serve --https=8443 off`.
