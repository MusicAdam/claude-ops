# diskgame-ops

A task queue for Disk Game. You queue work on a dashboard. Headless Claude workers do it in
parallel, each in its own git worktree. A test gate guards every merge, and a staging build only
changes when you promote it.

## Use it

```bash
./opsctl start     # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open      # dashboard: http://127.0.0.1:8787
./opsctl status | log | stop | restart
./opsd.py add "Make the goal flash when someone scores"   # queue from the terminal
```

## Flow

```
 dashboard ──queue──▶ scheduler ──(up to N at once)──▶ worker: claude -p in ~/dev/diskgame-wt/t<id>
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
              staging: ~/dev/diskgame-stg builds dev; main fast-forwards if your checkout is clean
```

- **Staging is yours.** Play it from the dashboard. Merges into `dev` never touch it. Promoting
  waits until you quit the staging game.
- **Parallelism.** Set "Parallel" on the dashboard (default 3). Workers also split big tasks into
  subtasks (with "after" ordering) and use subagents inside a task.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes.
- **Notifications.** macOS notifications when a task needs you, fails, or finishes.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout or `dev`/`main`.

## Files

| Path | What |
|---|---|
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, staging |
| `worker_prompt.md` | brief every worker starts from |
| `dashboard.html` | the page |
| `queue.db` | SQLite: tasks, events, settings |
| `runs/<id>/` | `claude.jsonl` (full worker transcript), `claude.err`, `gate.log` |
| `opsd.log` | daemon log |

Env overrides: `OPS_PORT` (8787), `OPS_HOST` (127.0.0.1), `OPS_REPO`, `OPS_WORKTREES`, `OPS_STAGING`.
Keep the host on 127.0.0.1: the API can start work on your machine. For phone access, use Tailscale
with `tailscale serve` rather than binding to your LAN.
