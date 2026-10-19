# diskgame-ops

A task queue for Disk Game. You queue work on a dashboard, or talk it through in a design chat
that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A test gate guards every merge into `dev`. `main` is the golden version: it changes only
when you promote.

## Use it

```bash
./opsctl start     # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open      # dashboard: http://127.0.0.1:8788  (…/#design opens the Design tab)
./opsctl status | log | stop | restart
./opsd.py add "Make the goal flash when someone scores"   # queue from the terminal
```

## Flow

```
 Design tab ──(you agree)──┐
 Queue tab ────────────────┴─▶ scheduler ──(up to N at once)──▶ worker: claude -p in ~/dev/diskgame-wt/t<id>
                                                                  │ commits on task/<id>-<slug>
                                                                  ▼
                               rebase onto dev ─▶ gate: compile, test-edit, test-play
                                  │ conflict / fail: the worker is resumed with the error (max 3 rounds)
                                  ▼ pass
                   "hold for review"? ── yes ─▶ build, wait for Play / Approve / Send back
                                  │ no
                                  ▼
                            merge --no-ff into dev   (~/dev/diskgame-wt/dev)
                                  │
                 you click Promote ▼
        build dev in ~/dev/diskgame-wt/main ─▶ fast-forward main (your checkout ~/dev/diskgame)
```

## main is the golden version

- **dev** gets every task that passes the gate. It is the newest version.
- **main** moves only on **Promote**, and its build (`~/dev/diskgame-wt/main`) always matches it.
  Play it from the dashboard.
- Promote checks first and changes nothing if it can't finish. It refuses while the main build is
  running, or while your checkout `~/dev/diskgame` is off `main` or has uncommitted changes.
- Commits made straight to `main` are merged into `dev` on the next Promote (no gate).
- If the build fails, `main` and its build stay as they were.

## Design mode

The Design tab is a chat with Claude about the game. It reads `docs/design.md` and the latest `dev`
code but cannot edit anything (`--permission-mode dontAsk` with read-only tools). When you agree on
the work, ask it to queue: it writes self-contained task briefs, and they appear as chips under its
reply. Click a chip to follow the task. Each message it gets starts with the current queue, so it
avoids duplicate tasks.

## Other details

- **Parallelism.** Set "Parallel" on the dashboard (default 3). Workers also split big tasks into
  subtasks (with "after" ordering) and use subagents inside a task.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes.
- **Notifications.** macOS notifications when a task needs you, fails, or finishes.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout, `dev` or `main`.

## Files

| Path | What |
|---|---|
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, promote, design chats |
| `worker_prompt.md` | brief every worker starts from |
| `design_prompt.md` | system prompt for design chats |
| `dashboard.html` | the page |
| `queue.db` | SQLite: tasks, events, settings, design chats |
| `runs/<id>/`, `runs/design-<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log` |
| `opsd.log` | daemon log |

Env overrides: `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_REPO`, `OPS_WORKTREES`.
Keep the host on 127.0.0.1: the API can start work on your machine. For phone access, use Tailscale
with `tailscale serve` rather than binding to your LAN.
