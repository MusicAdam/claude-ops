# claude-ops

A task queue for any git project. You queue work on a dashboard, or talk it through in a design
chat that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A gate (your test commands) guards every merge into the dev branch. The main branch is
the golden version: it changes only when you promote.

## Use it

```bash
./opsctl start                       # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open                        # dashboard: http://127.0.0.1:8788  (…/#design opens the Design tab)
./opsctl status | log | stop | restart
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
```

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
        build dev in <worktrees>/main ─▶ fast-forward main in your checkout
```

## main is the golden version

- **dev** gets every task that passes the gate. It is the newest version.
- **main** moves only on **Promote**, and its build (`<worktrees>/main`) always matches it.
- Promote checks first and changes nothing if it can't finish. It refuses while the main build is
  running, or while your checkout is off main or has uncommitted changes.
- Commits made straight to main are merged into dev on the next Promote (no gate).
- If the build fails, main and its build stay as they were.
- Nothing is ever pushed. Push main yourself when you want to.

## Design mode

The Design tab is a chat with Claude about one project. It reads the project's docs and the latest
dev code but cannot edit anything (`--permission-mode dontAsk` with read-only tools). When you
agree on the work, ask it to queue: it writes self-contained task briefs, and they appear as chips
under its reply. Each message it gets starts with that project's current queue, so it avoids duplicates.

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
- **Notifications.** macOS notifications when a task needs you, fails, or finishes.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout, dev or main.

## Files

| Path | What |
|---|---|
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, promote, design chats |
| `worker_prompt.md`, `design_prompt.md` | briefs for workers and design chats |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log` |
| `~/.claude-ops/opsd.log` | daemon log |

Env overrides: `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_DATA` (~/.claude-ops).
Keep the host on 127.0.0.1: the API can start work on your machine. For phone access, use Tailscale
with `tailscale serve` rather than binding to your LAN.
