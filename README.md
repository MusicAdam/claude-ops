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
./opsctl pause | resume              # pause every running task now / resume every paused one
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
```

On the dashboard you can type a full path or just a name. A name like `mygame` means a folder next to
your other projects (usually `~/dev/mygame`). If it doesn't exist, the dashboard offers to create it
as a new git repo with one empty commit.

`add-project` detects Unity (`scripts/unity.sh` + `Assets/`), Node (`package.json`) and Python
projects and writes a starter config. It also takes a workspace: a folder that is not a git repo but holds
several (see [Workspaces](#workspaces)). Read it, adjust it, then click **Reload projects** (or
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
- Promoting claude-ops itself restarts the daemon to load the new code (`opsctl restart`: it waits
  until no task is running a command).
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

## Workspaces

A workspace is a folder of separate git repos that work together, such as `~/dev/ballup-monorepo` with a
backend (`ballup-dunk/`) and an app (`ballup-swish/`). Its config lists them: `repos = ["ballup-dunk",
"ballup-swish"]`. `add-project` writes that list when the folder is not a git repo itself, along with each
repo's setup and tests, run from its folder (`cd ballup-dunk && poetry run pytest -q tests/unit`), and the
gitignored `.env` files each repo has, to clone into new worktrees.

- **Branches per repo.** Each repo gets its own `dev`, `task/<id>-<slug>` branches and `main`. The repos'
  checkouts stay where they are (`<repo>/ballup-dunk`, …).
- **Worktrees.** A task's worktree is a folder holding a copy of the workspace's own files (`CLAUDE.md`,
  `docs/`, `.claude/`) and one git worktree per repo. Edits to those copied files are not kept: change the
  workspace's own files in its folder yourself.
- **Merging.** The queue rebases every repo's task branch onto that repo's `dev` and runs the gate from the
  worktree root. It merges each repo the task committed to; a repo with no commits is left alone.
- **Promote.** A release snapshots `dev` in every repo and checks them together. Every checkout must be on
  `main` and clean. `main` moves in all repos or in none.
- Commits show as one id per repo, such as `ballup-dunk 1a2b3c4d, ballup-swish 5e6f7a8b`.

## Project config: `~/.claude-ops/projects/<name>.toml`

```toml
title = "My Game"
repo = "/Users/you/dev/mygame"
main_branch = "main"
dev_branch = "dev"                 # created from main if missing; add-project picks ops-dev if dev exists
worktrees = "/Users/you/dev/mygame-wt"   # default: <repo>-wt
# repos = ["backend", "app"]       # a workspace: repo is a folder and these folders in it are the git repos
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

- **Queue tab.** Projects are on the left, one line each with their counts. Click one to open it:
  branches, what is waiting to promote, Promote, Play and Pause. Opening a project also sets the
  project filter in the header, so only its tasks and chats show; click it again to see every
  project. Tasks are on the right. Clicking a
  task opens it on the left and focuses its project, and new tasks default to that project.
- **Parallelism.** "Parallel" on the dashboard is the global limit (default 3). Workers also split
  big tasks into subtasks (with "after" ordering) and use subagents inside a task.
- **Pausing.** **❚❚ Pause** on a task, or **Pause running** in the header (`./opsctl pause`), stops it
  now and keeps its place. A worker's Claude session resumes where it was; a command it was running
  (a test run, a build) is stopped and it is told to check and carry on. A task paused during its gate
  runs the gate again from the start. A task that is merging can't be paused. Paused tasks stay paused
  across restarts: click **▶ Resume** on a task, or **▶ Resume all** in the header (`./opsctl resume`),
  to queue them again. **Resume all** shows whenever a task is paused, even while others run. To restart without
  waiting: `./opsctl pause && ./opsctl restart && ./opsctl resume`. **Pause queue** is different: it
  holds tasks that haven't started and lets running ones finish.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes.
- **Talk to a working worker.** While a task is queued, working or being checked, the task page has
  a "Talk to the worker" box. A message goes straight to the running worker, which reads it after its
  current step, answers in a line or two, and changes course. Where your message and the task
  description disagree, the message wins. If no worker is running at that moment (queued, rebasing,
  running the gate), the message waits: the next worker turn starts with it, and a task with a waiting
  message never merges or goes to review before the worker has seen it. Each message shows whether the
  worker got it. Once the task is merging it is too late; queue a new task instead. A paused task
  offers both: **Ask** answers now, read-only; **Send when it resumes** waits for the worker.
- **Ask the worker.** Once a worker stops (review, needs you, failed, done), you can ask it questions
  on the task page. Each question resumes its own Claude session, read-only, so it remembers the work.
  It also remembers the questions: if you then send feedback or retry, it builds on what you discussed.
- **Screenshots.** Drop or paste an image into any message box: Design, Chat, Ask the worker, an answer,
  feedback, or a new task. It is saved in `~/.claude-ops/images/` and an `[image: <path>]` line goes in the
  text. The message shows it, and Claude (started with `--add-dir` on that folder) is told to read it.
  PNG, JPEG, GIF or WebP, up to 20 MB.
- **Notifications.** macOS notifications when a task needs you, fails, or finishes.
- **macOS permissions.** launchd starts the daemon through `~/.claude-ops/Claude Ops.app` (built from
  `launcher.c` by `opsctl start`), so privacy prompts (App Management, folder access) name **Claude Ops**,
  not python3. A grant then covers the daemon, its workers and their builds, and not every Python script.
  The app is rebuilt only when `launcher.c` changes; a rebuild changes its signature, so macOS asks again.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout, dev or main.
  None of the daemon's Claude sessions (workers, design, chat) connect to Claude in Chrome (`--no-chrome`).

## Files

| Path | What |
|---|---|
| `launcher.c` | `Claude Ops.app`: starts the daemon so macOS permission prompts name it |
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, promote, design and project chats |
| `worker_prompt.md`, `release_prompt.md`, `design_prompt.md` | briefs for workers, release checkers and design chats |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log` |
| `~/.claude-ops/images/` | screenshots attached to messages |
| `~/.claude-ops/opsd.log` | daemon log |
| `~/.claude-ops/opsd.pid` | the running daemon's pid |

Env overrides: `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_DATA` (~/.claude-ops).
Keep the host on 127.0.0.1: the API can start work on your machine. For phone access, use Tailscale
rather than binding to your LAN. It is served tailnet-only on its own HTTPS port (443 is taken by
another dashboard): `tailscale serve --bg --https=8443 http://127.0.0.1:8788`, then open
https://adams-mac-mini.pintail-bramble.ts.net:8443. Undo with `tailscale serve --https=8443 off`.

## Testing claude-ops itself

Run a scratch daemon with its own data and port, and stop it by its pid, never by pattern:

```bash
d=$(mktemp -d); OPS_DATA="$d" OPS_PORT=8799 /opt/homebrew/bin/python3 opsd.py serve > "$d/log" 2>&1 &
# ... try it ...
kill "$(cat "$d/opsd.pid")"        # or kill $! from the same shell
```

Never use `pkill -f 'opsd.py serve' …` or `lsof -ti :PORT | xargs kill`. On macOS, `pkill` stops
reading options at the first pattern, so `pkill -f 'opsd.py serve' -U $(id -u) -n` treats `-U`, your
uid and `-n` as extra patterns. It then kills every process whose command line contains any of them:
Chrome's helpers (Claude in Chrome goes down with them), Spotify, Steam, Unity, every claude-ops
worker and the live daemon. `lsof -ti :PORT` lists clients as well as the server, so piping it to
`kill` also kills Chrome when a tab has that dashboard open.
