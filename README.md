# claude-ops

A task queue for any git project. You queue work on a dashboard, or talk it through in a design
chat that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A gate (your test commands) guards every merge into the dev branch. The main branch is
the golden version: it changes only when you promote.

## Use it

```bash
./opsctl start                       # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open                        # dashboard: http://127.0.0.1:8788  (…/#tasks, …/#projects, …/#design, …/#chat open those tabs)
./opsctl status | log | stop | restart   # stop and restart pause running tasks; the next start resumes them
./opsctl pause | resume              # pause every running task now / resume every paused one
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
./opsd.py add -p myrepo --model sonnet "Rename the config key"   # its worker runs on Sonnet (see Models)
./opsd.py deploy -p myrepo "store test" --version patch --note "Fixes the crash"   # see Deploys
./opsd.py send myrepo --design "How does the save format work?"   # what a worker or chat runs to ask a peer project
./opsd.py archive-project myrepo     # take it off the queue; unarchive-project brings it back (see Archiving)
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
 Tasks tab ────────────────┴─▶ scheduler ──(up to N at once)──▶ worker: claude -p in <worktrees>/t<id>
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
- **Promote** queues a release task ahead of other work. It takes a snapshot of dev and runs the gate. If the
  gate passes, main moves to exactly that snapshot; anything merged into dev meanwhile waits for the next
  release. No Claude session checks the build: the gate is the whole check. A failed gate leaves main as it was.
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
  Either way main and its build stay as they were until it moves. They are not part of the release, and the
  release's summary says so: under **Not part of this release** it lists, per repo, the uncommitted files that
  stayed in your checkout (the first 20), which of them were carried over (and the ref of the copy), and says
  to commit them to dev for them to ship. The notification says how many were not released.
- **Carried changes are kept safe, not shipped.** Uncommitted work in your checkout is never part of a release,
  and workers don't see it: they start from dev. While the checkout (any repo of a workspace) has uncommitted
  files, the project card shows a banner with how many, where, and the date of the oldest change; expand it for
  the paths. The New task form warns that workers won't see them, `opsd.py add` prints the same warning, and a
  design chat's `[ops: ...]` line names them. Commit them to dev, or click **Send to dev**, for them to ship.
- **Send to dev** (a button on that banner) turns the stranded work into a normal queued change on dev, then
  clears it from your checkout, so there is exactly one copy. It queues a task of its own kind, `Send uncommitted
  changes in <repo folder> to dev` (marked `send to dev` in the list; a workspace reads `<workspace folder>
  (<repo>, <repo>)`, naming the repos that had changes), with no hold and the project's Promote after
  merge default. A project has one open at a time: while it is open, the button links to it.
  1. **At the click** it snapshots each repo with uncommitted files (tracked and untracked, not ignored), as
     `snapshot` does for a release, and keeps the snapshot as a commit on your HEAD at `refs/ops/senddev-<id>`.
     That ref is your copy: it stays after the task ends.
  2. **On dev.** In the task's worktree, on its branch made from dev, it applies the snapshot with
     `git cherry-pick --no-commit` and commits it as `Your uncommitted changes from <repo>`, listing the files.
     If it conflicts with dev, a worker resolves it so both sides survive, commits, and may fix what the gate finds.
     If the worker can't merge it, the task needs you, with its reason.
  3. **Then the normal path:** rebase, gate (with fix rounds) and merge into dev, as for any task.
  4. **Clearing, only after the merge into dev.** Per repo: if your checkout's HEAD moved since the snapshot (a
     commit, or a release that moved main), nothing is cleared and the summary says so. Otherwise each file the
     snapshot changed that still matches it is put back as HEAD has it (deleted if HEAD lacks it), in the working
     tree and the index; folders left empty are removed. A file you edited after the click stays as it is and is
     named in the summary. Files outside the snapshot are never touched. Clearing waits while a release or deploy
     is moving main in your checkout.
  5. **The summary** starts with one line in counts, which the task card shows, such as `Landed 3 files on dev;
     cleared 3 from your checkout` or `...; left 1 you edited after sending`. Below it, per repo, it lists the
     files that landed on dev, the files cleared, any left and why, and the ref.

  Pausing, stopping or cancelling the task before the merge clears nothing; Retry after a merge clears then. Until
  the next Promote the work is in dev but not in your checkout: the card says `On dev, not released yet. Promote to
  get it back here.` with a link to the task, until main contains it.
- With nothing new on dev, Promote only rebuilds main (no gate).
- A release snapshots dev, runs the gate, builds main, fast-forwards main and carries your uncommitted changes (a
  carry conflict still uses a worker). A gate failure blocks it. Its summary starts `Gate-only release: the merge
  check passed (<checks>)`, then gives each released task's summary, and **Not part of this release** as usual.
  A gate-only release starts while the usage brake is on, as it starts no Claude session.
- **Promote after merge** (a checkbox on a new task, and on its page until it finishes) promotes for you, as a
  **promote train**: once the task merges into dev, the queue queues a release, the same one Promote makes, but
  only when either
  - the project is **quiet**: none of its tasks (or Send to devs) is queued, running, checking or merging. Tasks
    waiting on you (review, needs input, paused) don't count; or
  - the first task still waiting for the train merged **Promote wait** minutes ago (`release_wait`, a global
    setting on the Settings tab; default 180).

  So tasks that merge close together share one promote instead of one each. The queue checks this at the end of
  every merge and once a minute. While a task waits, its line in the list and the project's card say `promotes
  when the queue is quiet, or by 3:40 pm`. If a release is already open, the task waits for it and the next
  release takes it. A task that splits passes the box to its subtasks, and they are promoted together after the
  last one merges (or is cancelled). In a design chat, ask for it and the task's `release` is set. The **Promote**
  button never waits for the train: it queues a release at once.

  The internal names keep the old word: the task field and API key `release`, the column `auto_release`, the
  per-project setting `auto_release:<project>` and the global setting `release_wait`.
- **Promote after merge by default** (a checkbox on the project's card) ticks that box for every task the
  project gets from then on: from the form, `opsd.py add`, a design chat, and the follow-ups a worker
  queues. You can still untick it on a task. Tasks already queued keep what they have. It is off until you
  turn it on.
- **Straight to main** (`direct_main = true` in the project's toml; the daemon reads it when it reloads
  projects or restarts). A task that passes its gate merges into dev as before, under the same lock, then moves
  main to dev at once: the steps of a release after its gate (build main if there is an `[app] build`,
  fast-forward, carry your uncommitted changes over), without running the gate again. So no release is queued:
  Promote after merge, the promote train and the Promote button go away for the project. dev stays, equal to
  main, so nothing that reads it changes.
  - Something in the way of main (the main build is running, main is checked out in another worktree, or your
    uncommitted changes can't be carried over) makes the task wait on you, `needs input`, as a release does. It
    goes on by itself once the way is clear. Answering or Retry only moves main; the merge is done.
  - Turned on while dev is ahead of main, the next merge brings all of dev into main with it (its gate ran on
    all of it). Until then the card shows Promote, to release it at once.
  - Commits made straight to main come into dev before each task's gate (a fast-forward when they can). If they
    conflict with dev, the task fails and says to Promote once: the release has a worker merge them.
  - Tasks move main one at a time, and never alongside a release.
  - For claude-ops itself, every merge restarts the daemon, as a Promote does.
  - Not for a project with `[[deploy]]` targets: a deploy moves main too, and builds from the main a release
    checked. There the option is ignored, with a line in the daemon log.
- Promoting claude-ops itself restarts the daemon to load the new code (`opsctl restart`: running
  tasks pause and resume). If the new daemon doesn't answer within 30 s, the release's log says so; its
  output is in `runs/<id>/restart.log`.
- The daemon opens its port first, then loads projects and resumes tasks. Until then the dashboard says
  "claude-ops is starting up" and refuses actions. `opsctl start` and `restart` wait up to 30 s for it to
  answer, and print the end of `opsd.log` if it doesn't. A new main worktree gets its `[worktree]` setup on
  first use (Promote's build, or Play main), not at startup. `scripts/startup-smoke.sh` checks this.
- `[worktree]` setup must succeed before anything else runs in the worktree. Each command's output goes to
  `runs/<id>/setup.log` (`runs/play-<project>/setup.log` for Play). A command that exits non-zero, or runs
  past `setup_timeout` minutes (default 15), fails the task before any gate runs, with the command and the end
  of its output; Play dev and Play main show the same error on the card. The worktree is marked prepared only
  when every command succeeded, so a retry or resume runs setup again. Each command runs in its own process
  group: pause, cancel, a timeout and a daemon restart send it SIGTERM, then SIGKILL after 5 s (`npm ci` can
  ignore SIGTERM), and setup first stops anything an earlier attempt left running in that worktree.
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
  `./opsd.py deploy -p <project> <target> [--version …] [--note …]`. Never from a release, Promote after merge, a
  worker or a design chat, and nothing that queues follow-ups can make one.
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
- **Where you deploy.** Open the project on the Projects tab: its card has a **Deploys** section, one block per target. It
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
When it has read the code a task touches, the brief ends with **Where to look**: about 20 lines of
`file:line` pointers and facts it found, so the worker skips the search. It merges small pieces into one
task only when they touch the same files. A worker that splits its task writes the same section.

### OpenSpec changes

If a project uses [OpenSpec](https://github.com/Fission-AI/OpenSpec), the Design tab lists its open changes
(`openspec/changes/<name>/`, not `archive/`) under the chats, with how many `tasks.md` items are done.
Click one to open its chat. A change that is only in your checkout is first copied into `dev`. The section
shows for every project in view whose `dev` has an `openspec/` folder, even when it has no open changes.

- **Set it up.** Each project in view, and each repo of a workspace, whose `dev` has no `openspec/` shows a muted
  line in the section: `<project>[/<repo>]: not using OpenSpec · Set up`. In the all-projects view these lines
  are folded under one **Set up OpenSpec…** toggle. **Set up** runs `openspec init --tools claude` for you.
- **Refresh its commands.** **Refresh commands**, under the list of changes, runs `openspec update` in a repo that
  uses OpenSpec, to bring its opsx commands and skills up to date with your CLI. When several repos in view use
  OpenSpec, it lists them and you pick one.
- **What those two do.** A change's chat can't run `openspec init` or `update`: they write into `.claude/`,
  outside `openspec/`. So the daemon runs them (`POST /api/openspec/init` and `/api/openspec/update`, with
  `project` and `sub`):
  1. It makes a throwaway worktree of `dev` (`<worktrees>/openspec-<init|update>-<time>`, detached).
  2. It runs the command there with no input and a 120-second limit.
  3. It commits only what changed under `openspec/` and `.claude/`, as `OpenSpec: set up (dashboard)` or
     `OpenSpec: refresh commands (dashboard)`. Anything else the CLI wrote, or anything `.gitignore` hides, is
     dropped, and the result names those paths.
  4. It fast-forwards `dev` to that commit, holding the project's merge lock, and removes the worktree.

  These commits skip the gate: they are docs and command files only, like a chat's edits. The section shows the
  result under the list: `Set up: committed <n> files to dev at <sha>.`, `Nothing to do: the commands are up to date.`, or the
  CLI's error (its last 300 characters). Set up is refused when `dev` already has `openspec/`; Refresh is refused
  when it has none. Both are refused while one of the project's tasks is merging into `dev` (try again after the
  merge), and both are disabled when the `openspec` CLI isn't on the daemon's `PATH`. Once Set up lands, the
  project shows **+ New change**.

- **Create one.** Click **+ New change** in the section, write one line on what the change should do, and click
  **Start**. Pick the project first when several in view use OpenSpec, and the repo when the project has several
  with `openspec/`. The button is disabled when the `openspec` CLI isn't on the daemon's `PATH`
  (`npm i -g @fission-ai/openspec`). Start opens a design chat, `New change: <description>`, that works like a
  change's chat (its own worktree, edits only under `openspec/`) but has no change yet. Its first message is
  `/opsx:propose <description>` when the repo has that command (or the `openspec-propose` skill); otherwise it
  asks Claude to run `openspec new change <name>`, write each artifact from
  `openspec instructions <artifact> --change <name> --json`, and run `openspec validate <name> --strict`. Claude
  drafts one change and asks you about scope first when material details are unclear. Until it has a change,
  the chat shows in the section as `<title> · drafting`. After each reply the queue compares the open change
  folders in the chat's worktree with those on `dev` before the reply:
  - **One new folder:** the chat becomes that change's chat, takes its title from `proposal.md`, and its edits
    are saved to `dev`. It says `Created change <name>.`
  - **None:** nothing happens; Claude is probably asking you something.
  - **Several:** the edits are saved but the chat stays unattached, and Claude is told to keep to one change per
    chat. Open each new change from the list.

- **Read it.** The change opens in a reader beside its chat, like a docs site: the reader on the left and the
  chat on the right, each scrolling on its own. The reader's header stays in view: the title, the path, overall
  progress (`done/total`), the "Why", and the queue buttons. Below it are tabs for **Proposal**, **Design**,
  **Tasks**, **Specs (N)** and **Other** (any other `.md` file), each shown only when it has content. The reader
  remembers the tab for each change, and keeps your place when Claude or a worker edits the files.
  - **On this page** lists the tab's `##` and `###` headings and marks the one you are reading. Click one to
    jump to it. In a reader under 900px wide it is a **Sections** menu instead.
  - **Specs** shows each delta file (`specs/<capability>/spec.md`) as cards, one per `### Requirement:`, edged
    by what the delta does: ADDED green, MODIFIED amber, REMOVED red, RENAMED blue. Each `#### Scenario:` shows
    its **WHEN**, **THEN** and **AND** lines as rows. A file that isn't in delta form shows as plain markdown.
  - **Tasks** shows one section per `##` phase of `tasks.md`, with its `done/total` and a progress bar. Phases
    that are fully done start folded.
  - Below 1200px the window is too narrow for both: a **Read | Chat** switch shows one at a time, and remembers
    your choice. Other chats keep the single chat column.
- **Change it.** Talk it through. This chat may edit files under `openspec/` and nothing else. It works in its
  own worktree (`<worktrees>/spec-<chat id>`, detached at `dev` and refreshed before each message). After each
  reply the queue commits its edits there and fast-forwards `dev` to them, so the next worker sees them. The
  chat shows `Saved to dev at <sha>: <files>`. These commits skip the gate, as they are only docs. If a worker
  changed the same lines meanwhile, the chat's side wins; if git still can't apply them, they are saved as a
  patch in `~/.claude-ops/runs/design-<id>/` and the chat says so. A task being checked when `dev` moves runs
  its gate again.
- **Queue it.** **Queue as one task**, or **Queue N tasks, one per phase** (the `##` sections of `tasks.md`,
  chained so each merges before the next starts), with **Hold for review** and **Promote after merge** (ticked when
  the project promotes after merge by default; only the last task of a chain is promoted). You can
  also ask Claude to queue it. Each task's brief points the worker at the change's folder, names the
  sections it covers, and tells it to tick the boxes as it goes, keep to the proposal's scope, update
  `design.md` and the deltas if it has to deviate, and not archive the change. If you have
  `/tk:openspec:implement`, the brief tells the worker to work through the items with it.
- **Slash commands.** In any design chat, a message that starts with `/` runs that command, as if you typed it
  in a terminal: `/tk:openspec:audit <name>`, `/opsx:explore <name>`. The queue's usual note for the turn goes
  into the system prompt instead of in front of your text. Other messages are sent as before.
- **What a change's chat can run.** The chat can use skills, subagents and any `openspec` command except
  `init` and `update` (they write outside `openspec/`, which a chat can't save; use **Set up** and
  **Refresh commands** instead), and the ones that change your machine's OpenSpec setup (`config set`, `store setup`, `completion install`, `feedback`, …). It may still edit
  only `openspec/`, and so may its subagents. The reader's header shows a button for each command that applies
  to the change, in this order (hover for what it does). A click sends the command as a chat message, through
  the same path as **Send**, so it shows in the chat as if you typed it. The buttons are disabled while Claude is
  replying. **Archive** asks first: `Archive <name>? This moves it to openspec/changes/archive and merges its
  spec deltas into openspec/specs. Saved to dev after the reply.`

  | Command | Shown when | What it changes |
  |---|---|---|
  | validate | the `openspec` CLI is installed | nothing: it runs `openspec validate <name> --strict` |
  | `/opsx:explore` | the repo has the opsx command or skill | nothing, unless you agree to an edit |
  | `/opsx:sync` | the repo has the opsx command or skill | merges the deltas into `openspec/specs/` |
  | `/opsx:archive` | the repo has the opsx command or skill | merges the deltas and moves the change to `openspec/changes/archive/` |
  | `/tk:openspec:audit` | you have the tk command | nothing: it reports gaps and drift |
  | `/tk:openspec:review` | you have the tk command | nothing: read-only review agents report |
  | `/tk:openspec:deviation-check` | you have the tk command and at least one item is ticked | nothing: it compares the plan with the code |

  The opsx commands come from `openspec init` in the repo (`.claude/commands/opsx/`, or as skills such as
  `openspec-explore` in `.claude/skills/` in newer versions; the dashboard sends whichever exists). Apply,
  implement and propose are never listed: the first two change code, so queue the change instead. When the
  repo has no opsx commands and you don't have the tk ones, only Validate is listed, with a muted hint: `Run Set
  up OpenSpec or Refresh OpenSpec commands to add the opsx commands.`
- **`/` autocomplete.** In a change's chat, type `/` as the first character of the message box and a list of the
  same commands opens just above it, each by its label and the exact text it sends. Typing more filters it, by
  label or text. **Up** and **Down** move, **Enter** or **Tab** puts the command's text in the box for you to
  send (plain-text commands such as Validate insert their text too), and **Esc** closes the list. Other chats
  show no list for now.
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
settings and `CLAUDE.md`. Each chat keeps one Claude session across messages, until its context grows too big
(see **Fresh sessions in chats** below).

- **MCP servers.** A project chat loads only the MCP servers named in the project's `chat_mcp` list, none by
  default. Every server's tool definitions ride on every call, so each one you add makes every reply dearer.
  Name servers you defined with `claude mcp add` in any scope (user, local, or the repo's `.mcp.json`):
  `chat_mcp = ["ballup-app"]` in `~/.claude-ops/projects/<name>.toml`. claude.ai connectors (Gmail, Google
  Sheets, Docs …) never load in a project chat: the chat runs with `--strict-mcp-config`, which drops them, and
  only claude.ai can define them. A name with no definition is skipped, and the chat says so.

- Nobody can answer permission prompts on the dashboard, so it runs in auto mode: actions that would
  prompt are denied, and Claude tells you what it couldn't do.
- **Fresh sessions in chats.** Design and project chats track the context of each call. When the last call of a
  chat's session was over 90k tokens, your next message starts a fresh session on the same model, and the chat
  shows "Started a fresh session: this chat's context reached 95k tokens …". The new session gets a handoff
  note first: the conversation so far, oldest first, each message cut at 4000 characters, about 10k tokens in
  all with the oldest cut. Files and the change's folder are as they were. The old session id stops working in
  the header's `claude --resume` line; the new one replaces it. Unlike a task, a chat does not start over just
  because its cache expired.
- The chat header shows `cd <repo> && claude --resume <id>`. Run it to carry on in a terminal, where
  you can answer prompts. Don't send from both places at once.
- Edits land in your checkout and nothing else. Uncommitted ones survive a release: it merges them onto the
  new main and puts them back.
- Those edits are not on dev, so they don't ship and workers don't see them until you commit them to dev. The
  project card shows a banner while the checkout has any. **Send to dev** on that banner lands them on dev as a
  queued change and then clears them from the checkout (see [main is the golden version](#main-is-the-golden-version));
  they come back with the next Promote.

## Research

claude-ops also serves the research dashboard's data and runs, which used to need their own server
(`~/dev/research/dashboard/server.py`, port 8787). The code is `research.py`; `opsd.py` only routes to it.
The API is `/api/research/...`, with the old server's endpoints and response shapes: `proposals`, `projects`
(with `/<id>/activity`, `/<id>/notes`, `/<id>/delete`), `assign`, `archive`, `references`, `todos`,
`jobs` (and `/<id>`, `/<id>/stop`), `threads` (`?target=` and `/<id>`), `file?path=`, `slug?q=`, `dispatch`
and `chat`. `file` serves only `.md` files inside the research repo.

- **Setting.** `research_repo` (Settings) is the research repo's path. Research is off while it is empty: the
  research API answers 404 `{error: "research is off"}` and `/api/state` has `research: null`.
- **Same files, same places.** Nothing is migrated. It reads and writes what the old server did:
  `dashboard/projects.json`, `dashboard/archived.json`, `dashboard/todos.json`, `projects/<id>.md` (project
  notes), `logs/dashboard/jobs.json`, `logs/dashboard/threads.json`, `logs/dashboard/<job>.jsonl`, `briefs/`,
  `reports/` and `sources/registry.md`. Every state JSON file is written atomically (a temp file in the same
  folder, fsync, `os.replace`). To-dos go through the repo's own `dashboard/todos.py`, loaded by path, so its
  `.todos.lock` is shared with `bin/todo` and the skill.
- **Runs and chats are as before.** No worktree, gate or merge: `claude -p` in the research repo with the
  old flags, tools, prompts, run sizes ($1/$3/$10) and time limits (T0 5, T1 15, T2 40, chat 15 minutes), in
  their own process group.
- **Single writer.** The old server guards its files with an in-process lock, so two writers would lose
  updates. Before every write, and before each watchdog pass, claude-ops tries port 8787
  (`OPS_RESEARCH_OLD_PORT` overrides it). If anything answers, the write gets 409 `{error: "The old research
  dashboard is running. Stop it to make changes here.", readonly: true}` and the watchdog skips its pass (the
  old server's watchdog covers it). Reads always work. `/api/state` shows `research.readonly`.
- **Backup.** Before the first write after each daemon start, the state files (`projects.json`,
  `archived.json`, `todos.json`, `jobs.json`, `threads.json` and the notes in `projects/`) are copied to
  `~/.claude-ops/research-backup/<YYYYmmdd-HHMMSS>/`. The newest 10 are kept. The `.jsonl` transcripts are not
  copied: only their own claude process writes them, and only by appending. If the backup fails, the write is
  refused with a 500 that names the error.
- **Adopting runs.** A run the old server started shows as running, and keeps its deadline, while its pid is
  alive and that process started within 5 s of the job's `started` time (`ps -o lstart=`), so a reused pid
  doesn't count. New jobs also store `pid_start`. Stop and the watchdog never signal a pid that fails the check.
- **Restarts leave research runs alone.** Research processes are not in the tasks table, `PROCS` or `STEPS`, so
  `shutdown()`, `pause_all()`, an `opsctl restart` or a Promote never stop one. After a restart the watchdog
  picks the deadlines back up from `jobs.json`; it skips any more than an hour past.
- **A separate count.** `/api/state` has `research: {active, readonly}`, where `active` is the running research
  runs and chat turns. The existing `active` (pipeline threads) doesn't include them, and research runs take no
  task slot: `max_parallel` and the scheduler ignore them.
- **Usage meter.** Each watchdog pass reads what running jobs added to their `.jsonl` since the last pass (a
  byte offset per job) and feeds every `rate_limit_event` to the usage meter.
- **Notifications.** One notification when a run finishes, fails or times out; a chat notifies only when it
  fails or times out. `notified: true` in `jobs.json` stops a restart notifying again. A job that ended over an
  hour ago never notifies, so turning research on doesn't replay its history.
- **The brake.** While the usage brake holds the queue, dispatch refuses a new run with a 409 that gives the
  brake's words. Chats are never held.
- **Linked code project.** A research project in `projects.json` may carry `repo`: the id of a claude-ops
  project (a name `opsd.py send` accepts, such as `plugins`, `ballup-monorepo` or `claude-ops`). Without it, or
  with it empty, the project stands alone; nothing is migrated. Each research project links to at most one code
  project, and several can link to the same one. Create and update (`/projects`, `/projects/<id>`) take `repo`;
  `""` or null clears it, and an id that isn't a configured claude-ops project is refused with a 400 that names it.
  Other keys on a project are kept. `projects` returns `repo` (or null) for each project, and `/api/state` has
  `research.links`: for each claude-ops project, the `{id, name}` research projects linked to it (read from
  `projects.json` on each call; reports aren't scanned).
- **Stale links.** If the linked claude-ops project is later removed or archived, the stored `repo` stays. The
  Research tab shows it muted as `<id> (not a project)` and runs get no repo context for it. It is never an error.
- **Repo context.** A run assigned to a linked research project, and a chat about a linked project or about a
  proposal in one, get a short block in their prompt: the project is the code project '<title>' at its checkout
  (the toml's `repo`), read that repo's CLAUDE.md and the design doc it names for what is already built before
  answering, and treat the repo as read-only context, never editing files there. That run or chat turn also gets
  `--add-dir <repo>` so it may read there; tools and flags are otherwise unchanged. A standalone project, a stale
  link or a checkout path that doesn't exist gets neither.
- **To-do → task.** `POST /api/research/todos/<id>/task` with `{project, title, body, priority, hold, release}`
  queues a task from a to-do. It is a research write (research on, not read-only or 409, backup first). It
  refuses, with a 400 that names the task or project, a to-do that is done, one whose `task` is still live (any
  state but failed or cancelled), and a project that isn't configured. It creates the task as `/api/tasks` does
  (`priority` 1, 0 or -1; `release` null takes the project default), stores `task: <id>` on the to-do under
  `todos._locked()` (so the lock stays shared with `bin/todo`; `todos.update()` keeps the key but can't set
  it), and returns `{id}`. Queueing again after the task failed or was cancelled replaces `task`. `todos` adds
  `taskState: {state, merged}` to each to-do with a `task`, read from the tasks table, or null if that row is gone.
- **Closing on merge.** Each watchdog pass closes every open to-do whose `task` is done with a non-empty
  `merged`: `done` becomes the date the task finished (local time) and notes get the line
  `Closed by task #N, merged <date>.` A task done without a merge closes nothing. A to-do is closed only while
  `done` is null and its notes lack that line, so one you reopen by hand stays open. The pass is skipped while
  read-only, like the rest of the watchdog.

### The Research tab

The dashboard's **Research** tab replaces the old page. It shows only while `research_repo` is set (Settings,
under Research). It has the old page's views in claude-ops' look: proposals grouped by research project, each
with its report, brief, chat and run log; the projects themselves (create, edit, delete, notes, activity); runs
(a new question or an existing brief, with tier, size, model, project and notes), each with its live feed and a
Stop button; to-dos; and the source registry. Reports render with the dashboard's own markdown, and a link to a
`.md` file opens it in a side drawer. What is open is in the URL (`#research/p/<slug>/report`,
`#research/project/<id>/chat`, `#research/run` …), so the back button and links from a phone work.

- **Read-only.** While the old server runs, a banner at the top says "The old research dashboard is running.
  Stop it to make changes here." and every write control is disabled. Reads keep working. A refused write shows
  the server's words next to its button, as does a run the usage brake holds.
- **Code project.** A project's new/edit form has a **Code project** select: `— none (standalone)` or one of
  the claude-ops projects by title. The project panel shows `Code: <title>` for a linked project; click it to open
  that project on the Projects tab. A stale link shows muted as `<id> (not a project)`.
- **Research chips.** On the Projects tab, each claude-ops project shows a small `Research: <name>` chip per
  research project linked to it. Click one to open `#research/project/<id>`. No chips while research is off.
- **Header count.** While research runs or chat turns are under way, the header's working chip adds
  `· K research`. They take no task slot, so they are not in `N/max`. Click it to open the tab on the runs.
- **Queue task.** Each open to-do has a **Queue task** button beside Edit and Delete (a write control, so
  disabled while read-only). It opens an inline form: title (the to-do text), body (the text, its notes and
  `From research to-do #<id>`, plus `, proposal <slug>` if it has one), code project (the one the to-do's
  research project links to, or none if unlinked or stale), Hold, priority and Promote after merge. Changing the
  code project there doesn't change the link. The to-do then shows `↳ task #N · <state>`; click it to open the
  task. A task that no longer exists shows muted as `task #N (gone)`. Once the task fails or is cancelled the
  button comes back.

### Cutover from the old server

1. Promote while nothing runs, so the daemon restarts on the new code.
2. Set `research_repo` to `~/dev/research`. Research is read-only while the old server runs, and its history shows.
3. Stop the 8787 server. Its runs carry on: claude-ops adopts them.
4. claude-ops becomes writable, and backs up the state files before its first write.

## Projects hand work to each other

A worker or chat in one project can start work in another, the way one Claude session messages a peer. Each
worker, design chat and project chat is told about the other projects and the command:

```bash
opsd.py send <project> "<message>"            # a task in that project's queue
opsd.py send <project> --design "<message>"   # a design chat there: ask how something works, talk an idea through
opsd.py send <project> --chat "<message>"     # a project chat there, in its checkout (it can edit files)
        ... --title "..." --hold              # name it; hold a task for your review before it merges
        ... --model sonnet                    # a task's model (see Models); not for chats
        ... --effort high                     # a task's effort (see Effort); not for chats
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

## Archiving a project

On the dashboard, open a project's card and click **Archive**, or run `./opsd.py archive-project NAME`. Either
takes a project off the queue without losing anything. It moves `~/.claude-ops/projects/NAME.toml` to `~/.claude-ops/projects/archived/NAME.toml` and reloads projects, so the
project leaves the Projects tab's list. `/api/state` lists it under `archived`, with how many of its tasks
are not finished.

- **What it leaves alone.** Your repo, its worktrees, its branches, its tasks and its settings (paused, release
  after merge, …) all stay as they are.
- **What waits.** Queued, paused and needs-you tasks stay where they are. The dashboard tags them
  "(archived)" and a queued one says "archived project". They carry on after a restore.
- **When it refuses.** While a task of the project is working, being checked or merging, or while it has an open
  release or deploy. The error names them: wait for them to finish, or cancel them.
- **Restore.** Click **Restore** under **Archived projects** at the bottom of the Projects tab's list (it is
  collapsed, and hidden when none are archived), or run `./opsd.py unarchive-project NAME`. Either moves the
  file back and reloads, and the project is back as it was. It refuses if a new project has taken the name: rename or archive that one first.

Both commands go through the running daemon (`POST /api/projects/NAME/archive` and `/unarchive`).

## Project config: `~/.claude-ops/projects/<name>.toml`

```toml
title = "My Game"
repo = "/Users/you/dev/mygame"
main_branch = "main"
dev_branch = "dev"                 # created from main if missing; add-project picks ops-dev if dev exists
worktrees = "/Users/you/dev/mygame-wt"   # default: <repo>-wt
# repos = ["backend", "app"]       # a workspace: repo is a folder and these folders in it are the git repos
max_parallel = 0                   # per-project cap; 0 = only the global "Parallel" limit
direct_main = false                # true: a merge moves main at once, no release (see Straight to main)
chat_mcp = []                      # MCP servers a project chat loads, by name (see Chat mode); default none

[worktree]
clone = ["Library"]                # APFS-cloned into each new worktree (warm caches)
setup = ["npm ci --silent"]        # run in each new worktree; $OPS_REPO is your checkout
setup_timeout = 15                 # minutes each setup command may run (default 15); then it is stopped

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
# running = "scripts/app-running.sh"   # optional, server-style apps: exit 0 = the app runs from this worktree
# stop = "scripts/app-stop.sh"         # optional: stops the app if it runs from this worktree

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

- **Tabs.** The header has five: Projects, Tasks, Design, Chat and Settings. `#projects`, `#tasks`, `#design`,
  `#chat` and `#settings` open one; the old `#queue` opens Tasks. The dashboard remembers the last tab you
  used, and opens on Projects the first time.
- **Header.** Only what you watch or act on: the tabs, the queue readouts (working, queued, needs you,
  paused), the usage meters, and the pause buttons. Hover a readout or meter for what it counts; the
  dashboard draws that tooltip itself, so it shows at once and survives the 3-second redraw.
- **Settings tab.** Parallel, Promote wait, Usage brake, Model, Effort and the model per kind of work, each
  with what it does written next to it, and **Spent**: what Claude has cost, priced at API rates.
- **Analyze tab.** Run times and costs of the tasks created in the last day, 7 days, 30 days or all time, for
  the project picked in the header or every project (`GET /api/analyze?period=1|7|30|all&project=<name>`).
  Medians, p90s and totals; spend per day; per project; by kind, model, effort, gate-fix rounds, Claude turns and
  outcome; what the fix rounds fixed; the dearest and slowest tasks; and **How to improve**: plain rules over the
  same data (fix rounds, Opus on releases, turns that started after the 1-hour cache expired, failed and cancelled
  spend, tiny tasks, queue waits, high effort), each with a rough saving. Run time leaves out the time a task
  waited on your answer or review, a pause or the usage limit. The rules' constants come from
  `docs/usage-analysis.md`.
- **Tasks tab.** New task and the task list are on the left; the task page is on the right. The project
  picker in the header filters the list. Clicking a task opens it on the right and focuses its project,
  and new tasks default to that project.
- **Projects tab.** Every project, one line each with its counts, at full width. Click one to open it:
  branches, what is waiting to promote, Promote, Play main and dev, Pause, Deploys and Archive; click it
  again to close it. Archived projects are at the bottom. **+ Project** in the header opens this tab with
  the Add project card at the top. An open project lists its tasks, open ones first, then the last few
  finished; click one to open it on the Tasks tab. A task link here (such as a deploy's `#id`, or the task a merge or
  release runs in) opens that task on the Tasks tab, and so does confirming a deploy.
- **Play main and Play dev.** A project with `[app] launch` has both buttons on its card. Play main
  runs `launch` in `<worktrees>/main`: the last Promote's build, or, with no `[app] build`, `main` as it
  is now. Play dev runs it in `<worktrees>/dev`, so you can try `dev` before you promote. The first Play
  dev runs the `[worktree]` clone and `setup` there; after that, if the project has a build and `dev`
  moved since its last one, Play dev builds first (the card says "dev needs build"; the log is in
  `runs/play-<project>/build.log`). The card shows which one runs ("running: main", "running: dev"),
  and a **Stop main** or **Stop dev** button for each that runs. Stop runs `[app] stop` there, or else
  sends SIGTERM to the app's processes in that worktree.
  Merges into `dev` go on while you play it; Promote waits while `main` runs, as before.
- **Server-style apps.** When the app is a set of local servers rather than one built file, set
  `[app] running` and `stop`. Both run as shell commands in the worktree (`$OPS_REPO` is your checkout).
  `running` exits 0 when the app runs *from that worktree*; it replaces the process-table match of
  `process`. `stop` stops the app there; it replaces SIGTERM to the matched processes, and runs only where
  `running` says the app runs (always, if `running` is unset). Such an app holds fixed ports, so before
  any Play (main, dev or a task's Try) launches it, the queue runs `stop` in the other worktrees: main,
  dev and each unfinished task's. Without these keys nothing changes.
- **Status at a glance.** Each task in the list has a line saying what is happening now. A working
  task says, in words, what the worker's current step does and what it last said, not the command
  it runs (`▸ Run the tests “The parser is fixed; checking it.” · 40s ago`). The full command is
  in the task's "Worker activity" feed. A task
  being checked or merged shows its step and how long it has run (`gate 2/3: test · for 1m`). A
  queued task says why it hasn't started: a free slot, the task it runs after, a paused project or
  queue. Tasks that need you show the question or error; finished ones show the summary.
- **Parallelism.** "Parallel" on the Settings tab is the global limit (default 3; setting `max_parallel`). Workers
  also split big tasks into subtasks (with "after" ordering) and use subagents inside a task. "Usage brake" next to it
  is the usage brake (setting `usage_brake`, default 85; see Usage limits). "Promote wait" is the promote train's
  longest wait in minutes (setting `release_wait`, default 180; see [Promote after merge](#main-is-the-golden-version)).
- **Models.** Each kind of work picks its Claude model. "Model" on the Settings tab is the global setting (`model`);
  **Per kind of work** below it sets `model:task` (task workers and Send to dev), `model:release` (the workers a release
  starts to merge main into dev, default `sonnet`), `model:design` (design and OpenSpec chats) and `model:chat`
  (the Chat tab). Each is an alias (`opus`, `sonnet`, `haiku`), a full model id, or empty for the global setting,
  whose empty value is Claude Code's own default.
  - **Per task.** A task's own model beats its kind's setting: the Model dropdown on New task, `"model"` in a
    design chat's queue block (see `design_prompt.md`), `opsd.py add -p NAME --model sonnet "..."`, or
    `opsd.py send NAME --model sonnet "..."`. Subtasks from a split take their parent's model. Empty means the
    `model:task` setting at the moment the worker starts.
  - **A session keeps its model.** The prompt cache belongs to one model, so resuming an Opus session on Sonnet
    would rewrite the whole context and cost more than staying on Opus. The model is picked when a session
    starts, and the one it actually started on (from the stream's init event) is stored as `session_model` on the
    task or chat. Every resume passes it to `--model`, whatever the settings say now: answers, gate fixes, rebase
    conflicts, retries, pauses, usage-limit waits and Ask the worker. Only a new session picks again: a
    a worker that merges your uncommitted changes. Sessions from before this ran on the global model and
    keep it. A fresh session that replaces a cold or oversized one (see **Fresh sessions**) keeps it too.
  - **On the dashboard** the task page shows the model its session runs on, and the task list tags tasks that
    are not on Opus, so you can compare a trial of Sonnet tasks later.
- **Effort.** How hard Claude thinks, passed as `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). "Effort" on
  the Settings tab is the setting `effort`, default `medium`, for every task, release and chat; empty leaves it to
  Claude Code (`effortLevel` in your `~/.claude/settings.json`). A task's own effort beats it: the Effort dropdown
  on New task, `"effort"` in a design chat's queue block, or `--effort high` on `opsd.py add` and `opsd.py send`.
  Subtasks from a split take their parent's effort. Unlike the model, effort is read again on every turn: a
  change to the setting reaches tasks already under way at their next turn.
- **Pausing.** **❚❚ Pause** on a task, or **Pause running** in the header (`./opsctl pause`), stops it
  now and keeps its place. A worker's Claude session resumes where it was, unless it waited over 55 minutes
  (see **Fresh sessions**); a command it was running (a test run, a build) is stopped and it is told to check
  and carry on. A task paused during its gate
  runs the gate again from the start. A task that is merging can't be paused. Paused tasks stay paused
  across restarts: click **▶ Resume** on a task, or **▶ Resume all** in the header (`./opsctl resume`),
  to queue them again. **Resume all** shows whenever a task is paused, even while others run. **Pause queue** is
  different: it holds tasks that haven't started and lets running ones finish. A task records who paused it
  (`paused_by`): empty for you, `restart` for a restart, `limit` for the Claude usage limit (see Usage limits).
- **Usage limits.** When Claude's usage limit hits, work waits for the reset instead of failing.
  - **The worker that hits it is paused, not failed** (`paused_by` `limit`). When it resumes, its Claude session is
    told the limit stopped it mid-turn and to check `git status` and `git log`, finish, and write its result. This
    covers every Claude step: workers, and the workers that carry your changes over or merge main
    into dev. Other running workers carry on, and are paused the same way if they hit it. Gates and merges don't use
    Claude and carry on. A wait of over 55 minutes outlives the prompt cache, so a worker
    resumes in a fresh session with a handoff note (see **Fresh sessions**).
  - **The queue starts nothing until the reset.** The reset time and the limit's kind are saved as the settings
    `limited_until` and `limited_kind`, so a restart keeps them (if Claude doesn't say when it resets, or names a time
    already past, the queue waits 15 minutes). A worker about to start a new turn in that time pauses at once, keeping its prompt. Queued tasks say
    `Usage limit (5-hour) · resumes 3:00 pm`.
  - **A minute after the reset** the flag clears and every task with `paused_by` `limit` is queued again. Tasks you
    paused stay paused. If a resumed task hits the limit again, the same happens with the new reset time.
  - **On the dashboard** a header banner says `Usage limit (5-hour) · resumes 3:00 pm`, with **Try now** to stop
    waiting and resume those tasks at once (`POST /api/limit/clear`). A paused task's line says
    `paused: usage limit · resumes 3:00 pm`. You get one notification per limit: `the queue resumes at 3:00 pm`.
  - **Chats.** A design chat, project chat or question to a worker that hits the limit replies
    `Usage limit reached. Resets at 3:00 pm.` and sets the same flag. The message is not sent again by itself: send it
    again after the reset.
  - **The usage brake stops starting Claude sessions before the limit hits**, so the end of each window is left for your own
    design chats, chats and answers. Every Claude stream reports how much of each usage window is used
    (`rate_limit_event`, from 0 to 1, account-wide, so it counts your own Claude use outside claude-ops too). The daemon
    reads it in every stream it parses (workers, chats) and keeps the latest reading per window in
    the settings `usage:five_hour` and `usage:seven_day` (`{utilization, resetsAt, ts}`). A reading whose reset has
    passed is stale and counts as 0.
  - **The setting `usage_brake`** is a percent: default 85, 0 turns the brake off. Set it as **Usage brake** on the
    Settings tab (`POST /api/settings {"usage_brake": 85}`). While either window is at or above it, the queue starts
    nothing that starts a Claude session: a new task or release, or one resuming its Claude session. Queued work that
    needs no Claude still starts, since holding it frees no usage: deploys (they only run a command), a release that
    only promotes, and a task resumed at its gate or merge (its `start_at` is not `claude`). A merge or promote that
    hits a conflict may still run a worker to resolve it. Running tasks, gates and merges carry on, and design chats
    and chats are never blocked. Held queued tasks say `Usage at 87% of the 5-hour window · starts again after 3:00 pm`. The
    brake lifts when that window resets or a newer reading is under the brake. You get one notification per window
    per reset: `Usage at 85% of the 5-hour window: no new tasks until 3:00 pm`.
  - **The header meter** shows both windows, `5h 62% · 7d 14%`, with their reset times on hover. It turns amber at
    or above the brake, and red while the queue waits out the usage limit itself.
- **Restarts.** Whenever the daemon stops (`./opsctl restart` or `stop`, a promote of claude-ops, launchd, a
  `kill`), it pauses every running task the same way, lets a merge finish, and exits. The next daemon resumes
  those tasks on its own, each in the same Claude session (or a fresh one after a long wait: see **Fresh
  sessions**); tasks you paused stay paused, and so do tasks the usage limit paused, until it resets. Nothing about a restart is saved that could hold the
  queue: if the daemon crashes instead, the next one requeues whatever was running.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes, or a fresh one if you answer after the cache expired.
- **Fresh sessions.** Before a worker resumes its Claude session (an answer, a gate fix, a
  rebase conflict, a retry, a pause, a restart, a usage-limit wait), the daemon checks the session's last call.
  If it was over 55 minutes ago (the prompt cache lasts an hour, and a cold resume rewrites the whole context at
  about 40 times the price of rereading it) or its context was over 90k tokens (every call rereads it), the
  turn starts a new session instead, on the same model. It is sent the task's first-turn brief, then a handoff
  note, then the turn's own prompt. The daemon writes the note itself, with no Claude call: why the old session
  ended, `git log --oneline <dev>..HEAD`, `git diff --stat <dev>...HEAD` and `git status --short` from the
  worktree, the last summary, your messages and answers so far, and the last question. It is capped at about
  10k tokens, cutting the diff stat first. The task log says "started a fresh session: last call 3 h ago (cache
  expired)" or "…: context 120k". Each task keeps `last_call` and `ctx_tokens` from its worker's stream (Ask the
  worker updates them too). The thresholds are `FRESH_AFTER` and `FRESH_ABOVE` in `opsd.py`, from
  `docs/usage-analysis.md` Q2 and Q3, with the context limit lowered from 155k to 90k later, as cache reads were
  most of all tokens used. Ask the worker always resumes; chats start over on context only (see Chat mode).
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
- **Reading rules.** Worker, release and chat briefs tell Claude to search before reading, read big files in
  ranges of 150 lines or fewer, not reread what is already in context, and keep command output short. Everything
  read stays in context and is paid for again on every later call.
- **Processes.** The daemon and its sessions stop only processes they started. See below.

## Process safety

Nothing claude-ops runs may stop, signal or quit a process it did not start.

- **The daemon** stops a task's command by the process handle it holds, and only while that process still
  runs. After a restart it trusts a saved pid only if the process start time still matches.
  **Quit the app** (Approve, Send back, a pause, the end of a worker turn) stops only your processes whose
  executable lies in that worktree's `[app] process` path. It matches the path exactly, never as a pattern,
  so a terminal or editor that merely names the path is spared. With `[app] stop` set, it runs that command
  in the worktree instead, and your script decides what to stop.
- **Workers and project chats** run each Bash command past `opsd.py guard` first (a
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
| `research.py` | the research dashboard's API, runs, chats and watchdog (see [Research](#research)) |
| `worker_prompt.md`, `design_prompt.md` | briefs for workers and design chats |
| `spec_prompt.md` | added to a design chat's brief when it is about an OpenSpec change |
| `spec_new_prompt.md` | added instead while a chat from **+ New change** has no change yet |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log`; a deploy's `deploy.log`, `note.txt` |
| `~/.claude-ops/images/` | screenshots attached to messages |
| `~/.claude-ops/opsd.log` | daemon log |
| `~/.claude-ops/opsd.pid` | the running daemon's pid |
| `~/.claude-ops/research-backup/` | copies of the research state files, one folder per daemon start, newest 10 |

Env overrides: `OPS_DAEMON` (where `opsd.py send` goes), `OPS_PORT` (8788), `OPS_HOST` (127.0.0.1), `OPS_DATA` (~/.claude-ops),
`OPS_ALLOWED_HOSTS` (extra host names the API answers, comma-separated), `OPS_RESEARCH_OLD_PORT` (8787, the old
research server's port that makes research read-only while it answers).
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
