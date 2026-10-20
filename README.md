# claude-ops

A task queue for any git project. You queue work on a dashboard, or talk it through in a design
chat that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A gate (your test commands) guards every merge into the dev branch. The main branch is
the golden version: it changes only when you promote.

## Use it

```bash
./opsctl start                       # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open                        # dashboard: http://127.0.0.1:8788  (…/#design, …/#chat open those tabs)
./opsctl status | log | stop | restart   # stop and restart pause running tasks; the next start resumes them
./opsctl pause | resume              # pause every running task now / resume every paused one
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
./opsd.py deploy -p myrepo "store test" --version patch --note "Fixes the crash"   # see Deploys
./opsd.py send myrepo --design "How does the save format work?"   # what a worker or chat runs to ask a peer project
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
- **main** moves only when a **release** passes, and its build (`<worktrees>/main`) always matches it. The one
  exception: commits a successful [deploy](#deploys) makes itself, such as a build-number bump, move main too.
- **Promote** queues a release task ahead of other work. It takes a snapshot of dev, runs the gate,
  then a Claude release checker (`release_prompt.md`) builds the app and plays it the way a player
  would. If the checker passes it, main moves to exactly that snapshot; anything merged into dev
  meanwhile waits for the next release. The checker never changes code: problems it finds are queued
  as ordinary tasks, and a blocked release leaves main as it was.
- Promote never refuses: it always queues a release (or points at the one already open). Anything in the way
  of moving main is checked only when the release gets there. If only you can clear it (quit the main build,
  or switch another worktree off main), the release **needs you**: it shows under Needs you, with a
  notification, and goes on by itself once you've cleared it. If your checkout is on another branch, main
  moves as a branch and your checkout is left alone. If main gets new commits during the release, it starts
  over with them.
- **Your uncommitted changes come along.** When your checkout is on main, uncommitted changes to files the
  release doesn't touch simply stay. Changes to files it does touch are merged onto the release in its
  worktree (a worker resolves any conflicts) and put back in your checkout, unstaged, once main moves. A copy
  of them as they were is kept at `refs/ops/release-<id>`. If the worker can't merge them, the release needs
  you: answer with how to combine them and it tries again, or commit or stash them and it goes on by itself.
  Either way main and its build stay as they were until it moves.
- With nothing new on dev, Promote only rebuilds main (no gate, no checker).
- **Release after merge** (a checkbox on a new task, and on its page until it finishes) promotes for you:
  once the task merges into dev, the queue queues a release, the same one Promote makes. If a release is
  already open, the task waits for it and the next release takes it. A task that splits passes the box to
  its subtasks, and they release together after the last one merges (or is cancelled). In a design chat,
  ask for it and the task's `release` is set.
- **Release after merge by default** (a checkbox on the project's card) ticks that box for every task the
  project gets from then on: from the form, `opsd.py add`, a design chat, and the follow-ups a release checker
  queues. You can still untick it on a task. Tasks already queued keep what they have. It is off until you
  turn it on.
- Promoting claude-ops itself restarts the daemon to load the new code (`opsctl restart`: running
  tasks pause and resume).
- Commits made straight to main are merged into dev when the release takes its snapshot (no gate). If they
  conflict with dev, a worker merges them in the release's worktree, resolves the conflicts and runs the checks;
  dev moves to that merge, and the release checks it as usual. Retry starts the merge over.
- If the build fails, main and its build stay as they were.
- Nothing is ever pushed to git. Push main yourself when you want to. A [deploy](#deploys) does publish
  things (a store build, a backend), but only when you click it.

## Deploys

A project can list deploy targets: a store test build, a store release, a backend's prod deploy. claude-ops knows
nothing about stores or servers. Each target is a shell command of the project's own (`bin/swish release`), and the
daemon runs it for you.

```toml
[[deploy]]
name = "swish test"                 # required, unique in the project
repo = "ballup-swish"               # workspace only: the repo it deploys; it runs in that folder
run = "bin/swish build && bin/swish upload"   # required; non-zero exit fails the deploy
note = "What's new"                 # optional: then a note is required, passed as OPS_NOTE and OPS_NOTE_FILE
version = "node -p \"require('./package.json').version\""   # optional: prints the current version, X.Y[.Z]
release_tag = "v{version}"          # optional, needs version: the tag that marks a version as released
build_tags = "build/*/{version}+*"  # optional, needs version: the tags its builds leave

[[deploy]]
name = "swish distribute"
repo = "ballup-swish"
from = "swish test"                 # deploys what "swish test" last deployed, not main
run = "bin/swish release"
```

- **Only on your click.** A deploy starts only from the dashboard, `POST /api/deploys`, or
  `./opsd.py deploy -p <project> <target> [--version …] [--note …]`. Never from a release, Release after merge, a
  release checker, a worker or a design chat, and nothing that queues follow-ups can make one.
- **Like a release, it goes ahead of other work.** Its title reads `Deploy swish test → 3.5.0`. The task page shows
  its step, its events and `deploy.log`. You get notified when it needs you, fails or succeeds.
- **One at a time.** A project runs one deploy at a time; a later click queues behind it. A release and a deploy of the
  same project never run at once: the one that waits says `waiting for deploy #N` or `waiting for release #N`.
- **The deploy worktree.** Each project has one, `<worktrees>/deploy`, made on first use like a task worktree
  (workspace files, the clone list, setup). Before each deploy every repo in it is put at the commit to deploy
  (`git checkout --force --detach`, then `git reset --hard`). It is **never cleaned**: untracked and ignored files
  stay, because a later target uploads what an earlier one built (swish's release uploads the builds its test build
  left in `ios/build` and `android/app/build`). Setup runs again only when the commit changed since the last setup.
- **Which commit.** main, in every repo. A target with `from` deploys the commit that target last deployed
  successfully, in its repo (other repos sit at main); before that target's first deploy it refuses with
  "Deploy swish test first."
- **Versions.** A target with `version` runs it in its folder at the commit to deploy. You pick:
  - **same**: the current version. Refused once `release_tag` for it exists: the App Store closes a version once
    approved.
  - **patch**, **minor**, **major**: the next one (3.5.0 → 3.5.1, 3.6.0, 4.0.0).
  - an explicit **X.Y.Z**, higher than the current one.

  The default is patch when the current version is released, else same. If the command fails or prints something
  that isn't a version, the dashboard says why and that target can't deploy until it is fixed.
- **Environment.** The command runs in the target's folder (`<worktrees>/deploy/<repo>`, or the worktree root
  without `repo`) with the project's `[worker] env` ({port} filled as for workers) and:

  | Variable | Value |
  |---|---|
  | `OPS_DEPLOY` | the target's name |
  | `OPS_COMMIT` | the commit being deployed: its repo's sha, or without `repo` one sha per repo, space-separated |
  | `OPS_DEPLOYED` | the commit its last successful deploy recorded, same form; empty the first time |
  | `OPS_VERSION` | the chosen version (the current one for "same"); unset without `version` |
  | `OPS_NOTE`, `OPS_NOTE_FILE` | your note, and a file holding it (in the task's runs folder) |
  | `OPS_PORT`, `OPS_TASK_ID` | the task's own port and id |

  The daemon runs the command itself, as it runs the gate, so the Bash guard that watches Claude doesn't apply.
- **Failure** (non-zero exit): the task fails and shows the end of the log. The target's record, main and the
  worktree stay as they are, so you can look around in it. **Retry** starts the deploy over, resetting the worktree.
  A deploy cut off by a restart of claude-ops fails the same way rather than run again by itself.
- **Success:** the target records what it shipped: the worktree's commit after the command (so a build-number bump
  the command made is what `from` targets deploy next), the time, the task and the version. If the command made
  commits, they are kept at `refs/ops/deploy-<id>` and main fast-forwards to them as a release would move it (the
  `[app]` build, your uncommitted changes carried over). If main moved during the deploy, the task needs you: merge
  `refs/ops/deploy-<id>` into main yourself and it goes on by itself, or cancel it to leave main alone. Tags the
  command made are already in your repo. dev picks the commits up at the next release, which merges commits made
  straight to main into dev. If the deploy succeeded but main couldn't move (say the build failed), Retry only
  moves main.
- **Where you deploy.** Open the project on the Queue tab: its card has a **Deploys** section, one block per target. It
  shows what the target last shipped (commit, when, its task), how far behind main (or its `from` target) it is, and
  for a target with `version` the current version, whether it is released and the newest build tag. **Deploy** opens
  a dialog with the commit it will ship, a version picker (new build, patch, minor, major, or another X.Y.Z), and the
  note when the target asks for one. While a deploy of the project is open, each button reads `Deploying… #N` and
  links to it; **Queue deploy** next to it queues another behind it. Deploy tasks are marked `deploy` in the task list,
  and a deploy's task page shows its log in place of the worker activity.
- **In `/api/state`.** Each project's `deploys` lists every target: its config and any config error, the last
  deploy (commit, time, task, version), how many commits it is behind, the commit it would deploy now with its
  subject, and for a target with `version` the current version, its release tag if any, the newest release and
  build tags, and the version options. Each target's `open` is its deploy task now open, and the project's
  `deploy_open` the first open deploy, if any.

## Design mode

The Design tab is a chat with Claude about one project. It reads the project's docs and the latest
dev code but cannot edit anything (`--permission-mode dontAsk` with read-only tools). When you
agree on the work, ask it to queue: it writes self-contained task briefs, and they appear as chips
under its reply. Each message it gets starts with that project's current queue, so it avoids duplicates.

### OpenSpec changes

If a project uses [OpenSpec](https://github.com/Fission-AI/OpenSpec), the Design tab lists its open changes
(`openspec/changes/<name>/`, not `archive/`) under the chats, with how many `tasks.md` items are done.
Click one to open its chat. A change that is only in your checkout is first copied into `dev`.

- **Read it.** The chat's header shows the title, the "Why", and each file (`proposal.md`, `design.md`,
  `tasks.md`, the spec deltas) folded open on click.
- **Change it.** Talk it through. This chat may edit files under `openspec/` and nothing else. It works in its
  own worktree (`<worktrees>/spec-<chat id>`, detached at `dev` and refreshed before each message). After each
  reply the queue commits its edits there and fast-forwards `dev` to them, so the next worker sees them. The
  chat shows `Saved to dev at <sha>: <files>`. These commits skip the gate, as they are only docs. If a worker
  changed the same lines meanwhile, the chat's side wins; if git still can't apply them, they are saved as a
  patch in `~/.claude-ops/runs/design-<id>/` and the chat says so. A task being checked when `dev` moves runs
  its gate again.
- **Queue it.** **Queue as one task**, or **Queue N tasks, one per phase** (the `##` sections of `tasks.md`,
  chained so each merges before the next starts), with **Hold for review** and **Release after merge** (ticked when
  the project releases after merge by default; only the last task of a chain releases). You can
  also ask Claude to queue it. Each task's brief points the worker at the change's folder, names the
  sections it covers, and tells it to tick the boxes as it goes, keep to the proposal's scope, update
  `design.md` and the deltas if it has to deviate, and not archive the change. If you have
  `/tk:openspec:implement`, the brief tells the worker to work through the items with it.
- **Slash commands.** In any design chat, a message that starts with `/` runs that command, as if you typed it
  in a terminal: `/tk:openspec:audit <name>`, `/opsx:explore <name>`. The queue's usual note for the turn goes
  into the system prompt instead of in front of your text. Other messages are sent as before.
- **What a change's chat can run.** The chat can use skills, subagents and any `openspec` command except
  `init` and `update` (they write outside `openspec/`, which a chat can't save), and the ones that change your
  machine's OpenSpec setup (`config set`, `store setup`, `completion install`, `feedback`, …). It may still edit
  only `openspec/`, and so may its subagents. The dashboard lists, in the chat's `commands`, the ones that
  apply to the change:

  | Command | Shown when | What it changes |
  |---|---|---|
  | validate | the `openspec` CLI is installed | nothing: it runs `openspec validate <name> --strict` |
  | `/opsx:explore` | the repo has the opsx command or skill | nothing, unless you agree to an edit |
  | `/opsx:sync` | the repo has the opsx command or skill | merges the deltas into `openspec/specs/` |
  | `/opsx:archive` | the repo has the opsx command or skill | merges the deltas and moves the change to `openspec/changes/archive/` |
  | `/tk:openspec:audit` | you have the tk command | nothing: it reports gaps and drift |
  | `/tk:openspec:review` | you have the tk command | nothing: read-only review agents report |
  | `/tk:openspec:deviation-check` | you have the tk command | nothing: it compares the plan with the code |

  The opsx commands come from `openspec init` in the repo (`.claude/commands/opsx/`, or as skills such as
  `openspec-explore` in `.claude/skills/` in newer versions; the dashboard sends whichever exists). Apply,
  implement and propose are never listed: the first two change code, so queue the change instead.
- **How big a task is.** One task per change is the default and the recommendation. A checklist item is far too
  small: every task costs a worktree, a worker start-up and a full gate run, and the worker can still split a
  big change itself. One task per phase is recommended only for a change with 25 or more open items in three
  or more phases. A phase with fewer than four open items (often "Tests" or "Docs") is folded into its
  neighbour. There is never one task per item.
- **Archive it in the chat.** When the work is merged, send `/opsx:archive <name>` (or ask the chat to run
  `openspec archive <name> --yes`). It touches only `openspec/`, so the edits are saved to `dev` like any
  other. The chat's header then says the change was archived and where it went
  (`openspec/changes/archive/<date>-<name>/`). Nothing archives on its own.
- A change copied from your checkout is still there. If it isn't committed there, delete it when you are done:
  otherwise the release that brings the change to `main` waits for it.

### Which chats need you

This applies to both the Design and the Chat tab.

- **The list.** Chats are grouped as **Ready for you** (Claude has answered), **Claude is replying**, and
  **Ended**, which is folded away. A chat whose reply you haven't opened yet is marked ● and **new reply**, and
  sorts first.
- **The tabs.** The Design and Chat tabs show how many chats have a new reply, and ● while Claude is replying in
  one. They follow the project filter.
- **End chat.** Click **End chat** under the message box, or × on a chat in the list, to move it under Ended.
  Nothing is deleted, and ending a chat stops any reply in progress. To put it back, click **Reopen** or send it a
  message. Opening an OpenSpec change reopens its chat as well.
- Opening a chat marks its reply as read. Chats from before this feature start as read.

## Chat mode

The Chat tab is plain Claude in the project folder, as if you ran `claude` there. It works in your
checkout (`repo`), not a worktree, so it can read, edit, commit and run commands there, with your own
settings, `CLAUDE.md` and MCP servers. Each chat keeps one Claude session across messages.

- Nobody can answer permission prompts on the dashboard, so it runs in auto mode: actions that would
  prompt are denied, and Claude tells you what it couldn't do.
- The chat header shows `cd <repo> && claude --resume <id>`. Run it to carry on in a terminal, where
  you can answer prompts. Don't send from both places at once.
- Edits land in your checkout and nothing else. Uncommitted ones survive a release: it merges them onto the
  new main and puts them back.

## Projects hand work to each other

A worker or chat in one project can start work in another, the way one Claude session messages a peer. Each
worker, design chat and project chat is told about the other projects and the command:

```bash
opsd.py send <project> "<message>"            # a task in that project's queue
opsd.py send <project> --design "<message>"   # a design chat there: ask how something works, talk an idea through
opsd.py send <project> --chat "<message>"     # a project chat there, in its checkout (it can edit files)
        ... --title "..." --hold              # name it; hold a task for your review before it merges
```

- **It shows like everything else.** A sent task is queued in the other project and goes through its gate as
  usual. A sent chat appears in its tab under **Ready for you** once it has replied. Both say **Sent by** (or
  **Started by**) with a link to the task or chat that sent them, and the lists tag them `sent by …`.
- **Context comes along.** The message starts with a line naming the sender, its title and where it works (its
  worktree and branch, or the folder a chat runs in), so the other session can read its files. The sender is told
  to make the message stand alone, like a design chat's task brief.
- **Word comes back.** The sender's task logs "Sent task #71 to diskgame", and later how it ended: done with its
  summary, failed, cancelled, or waiting on you. A sent chat's first reply goes back too. In a chat that sent
  something these show as notes, and Claude reads them with your next message.
- **Limits.** A design chat stays read-only: it can send a task or a design chat, not a project chat. Work passed
  on three times (A sends to B, which sends on, and so on) is refused, and so is an eleventh send from one task or
  chat: both stop two projects keeping each other busy. Workers are told to send only what their task needs and
  never to edit another project's files themselves. Chats send only when you ask or agree.
- The daemon gives each session `OPS_FROM` (`task:12` or `chat:3`) and `OPS_DAEMON` (its own URL), which `send`
  reads. Run from a terminal without them, it still reaches the live daemon; the task then says it came from
  outside the queue.

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
- **Promote.** A release snapshots `dev` in every repo and checks them together. Each checkout is handled
  as in a plain project, uncommitted changes included. `main` moves in all repos or in none.
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
process = "Builds/macOS/MyGame.app"            # the app's executable or its folder, inside the worktree
artifact = "Builds/macOS/MyGame.app"           # must exist after build; kept as a backup on promote

[worker]
env = { GAME_PORT = "{port}" }     # extra env; {port} is the worker's own free port ($OPS_PORT)
notes = """Project-specific guidance added to every worker brief."""

[[deploy]]                         # zero or more; see Deploys. Runs only when you click Deploy
name = "store test"
run = "bin/release test"           # in <worktrees>/deploy (or its repo folder); gets OPS_DEPLOY, OPS_COMMIT, ...
# repo = "app"  from = "other target"  note = "What's new"
# version = "cat VERSION"  release_tag = "v{version}"  build_tags = "build/*/{version}+*"

[design]
notes = """Project-specific guidance for design chats."""
```

## Other details

- **Queue tab.** Projects are on the left, one line each with their counts. Click one to open it:
  branches, what is waiting to promote, Promote, Play and Pause. Opening a project also sets the
  project filter in the header, so only its tasks and chats show; click it again to see every
  project. Tasks are on the right. Clicking a
  task opens it on the left and focuses its project, and new tasks default to that project.
- **Status at a glance.** Each task in the list has a line saying what is happening now. A working
  task says, in words, what the worker's current step does and what it last said, not the command
  it runs (`▸ Run the tests “The parser is fixed; checking it.” · 40s ago`). The full command is
  in the task's "Worker activity" feed. A task
  being checked or merged shows its step and how long it has run (`gate 2/3: test · for 1m`). A
  queued task says why it hasn't started: a free slot, the task it runs after, a paused project or
  queue. Tasks that need you show the question or error; finished ones show the summary.
- **Parallelism.** "Parallel" on the dashboard is the global limit (default 3). Workers also split
  big tasks into subtasks (with "after" ordering) and use subagents inside a task.
- **Pausing.** **❚❚ Pause** on a task, or **Pause running** in the header (`./opsctl pause`), stops it
  now and keeps its place. A worker's Claude session resumes where it was; a command it was running
  (a test run, a build) is stopped and it is told to check and carry on. A task paused during its gate
  runs the gate again from the start. A task that is merging can't be paused. Paused tasks stay paused
  across restarts: click **▶ Resume** on a task, or **▶ Resume all** in the header (`./opsctl resume`),
  to queue them again. **Resume all** shows whenever a task is paused, even while others run. **Pause queue** is
  different: it holds tasks that haven't started and lets running ones finish.
- **Restarts.** Whenever the daemon stops (`./opsctl restart` or `stop`, a promote of claude-ops, launchd, a
  `kill`), it pauses every running task the same way, lets a merge finish, and exits. The next daemon resumes
  those tasks on its own; tasks you paused stay paused. Nothing about a restart is saved that could hold the
  queue: if the daemon crashes instead, the next one requeues whatever was running.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes.
- **Talk to a working worker.** While a task is queued, working or being checked, the task page has
  a "Talk to the worker" box. A message goes straight to the running worker, which reads it after its
  current step, answers in a line or two, and changes course. Where your message and the task
  description disagree, the message wins. If no worker is running at that moment (queued, rebasing,
  running the gate), the message waits: the next worker turn gets it as its own message right after
  that turn's prompt, so the reply under it answers it. A task with a waiting message never merges or
  goes to review before the worker has seen it. Each message shows whether the
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
- **Speed.** The dashboard polls every 3 seconds, so the daemon answers a poll without waiting on git or the
  process table. Each project's branches, "to promote" list and whether its build is running are read in the
  background and kept for 5 seconds; any task event rereads its project at once. A task page reads only the
  part of the transcript that is new since the last poll, and runs `git diff --stat` only when the branch
  moved. The page sends one poll at a time, none from a hidden tab, and redraws a click at once instead of
  waiting for the daemon.
- **macOS permissions.** launchd starts the daemon through `~/.claude-ops/Claude Ops.app` (built from
  `launcher.c` by `opsctl start`), so privacy prompts (App Management, folder access) name **Claude Ops**,
  not python3. A grant then covers the daemon, its workers and their builds, and not every Python script.
  The app is rebuilt only when `launcher.c` changes; a rebuild changes its signature, so macOS asks again.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. They never push, and never touch your checkout, dev or main.
  None of the daemon's Claude sessions (workers, design, chat) connect to Claude in Chrome (`--no-chrome`).
- **Processes.** The daemon and its sessions stop only processes they started. See below.

## Process safety

Nothing claude-ops runs may stop, signal or quit a process it did not start.

- **The daemon** stops a task's command by the process handle it holds, and only while that process still
  runs. After a restart it trusts a saved pid only if the process start time still matches.
  **Quit the app** (Approve, Send back, a pause, the end of a worker turn) stops only your processes whose
  executable lies in that worktree's `[app] process` path. It matches the path exactly, never as a pattern,
  so a terminal or editor that merely names the path is spared.
- **Workers, release checkers and project chats** run each Bash command past `opsd.py guard` first (a
  `PreToolUse` hook passed with `--settings`). It refuses:
  - `pkill`, `killall`, `xargs kill`, and `kill` of pids found by `pgrep`, `lsof`, `ps` or `pidof`
  - `kill` of a pid the session did not start (not its descendant, and not running from its worktree),
    of `0`, `-1` or a process group
  - `launchctl` other than `print`/`list`, `sudo`, `shutdown`, `reboot`, `osascript` that quits apps or
    drives System Events, `opsctl start|stop|restart|pause|resume`, and requests to the live dashboard's port

  `kill $!` and `kill "$(cat pidfile)"` still work. The briefs state the same rule. The guard catches slips;
  it is not a sandbox: a program can still call `kill()` itself.
- **`opsctl start|stop|restart`** act on the one launchd service. They refuse when `OPS_DATA` is not
  `~/.claude-ops` or when run from a git worktree, so a scratch daemon or a worker can't restart or
  re-point the live one. Promoting claude-ops restarts the daemon only if it is that launchd service.
- **`[app] artifact`** must be inside the worktree: Promote deletes and replaces it.

## Files

| Path | What |
|---|---|
| `launcher.c` | `Claude Ops.app`: starts the daemon so macOS permission prompts name it |
| `opsd.py` | daemon: HTTP API + dashboard, scheduler, pipeline, promote, design and project chats |
| `worker_prompt.md`, `release_prompt.md`, `design_prompt.md` | briefs for workers, release checkers and design chats |
| `spec_prompt.md` | added to a design chat's brief when it is about an OpenSpec change |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log`; a deploy's `deploy.log`, `note.txt` |
| `~/.claude-ops/images/` | screenshots attached to messages |
| `~/.claude-ops/opsd.log` | daemon log |
| `~/.claude-ops/opsd.pid` | the running daemon's pid |

Env overrides: `OPS_DAEMON` (where `opsd.py send` goes), `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_DATA` (~/.claude-ops),
`OPS_ALLOWED_HOSTS` (extra host names the API answers, comma-separated).
The API answers only requests addressed to `127.0.0.1`, `localhost`, `OPS_HOST` or a `*.ts.net` name, so a
web page can't reach it by re-pointing its own domain at 127.0.0.1 (DNS rebinding).
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

`opsd.py send` goes to `OPS_DAEMON`, which a claude-ops worker has set to the live daemon. To try `send` against
your scratch daemon, set `OPS_DAEMON=http://127.0.0.1:8799` (its port) on the command.

Never use `pkill -f 'opsd.py serve' …` or `lsof -ti :PORT | xargs kill`. On macOS, `pkill` stops
reading options at the first pattern, so `pkill -f 'opsd.py serve' -U $(id -u) -n` treats `-U`, your
uid and `-n` as extra patterns. It then kills every process whose command line contains any of them:
Chrome's helpers (Claude in Chrome goes down with them), Spotify, Steam, Unity, every claude-ops
worker and the live daemon. `lsof -ti :PORT` lists clients as well as the server, so piping it to
`kill` also kills Chrome when a tab has that dashboard open.
