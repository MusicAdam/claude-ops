# claude-ops

A task queue for any git project. You queue work on a dashboard, or talk it through in a design
chat that queues it for you. Headless Claude workers do the work in parallel, each in its own git
worktree. A gate (your test commands) guards every merge into the dev branch. The main branch is
the golden version: it changes only when you promote.

## Use it

```bash
./opsctl start                       # install + start the launchd service (runs at login, restarts if it dies)
./opsctl open                        # dashboard: http://127.0.0.1:8788  (…/#today, …/#tasks, …/#projects open those pages)
./opsctl status | log | stop | restart   # stop and restart pause running tasks; the next start resumes them
./opsctl pause | resume              # pause every running task now / resume every paused one
./opsd.py add-project ~/dev/myrepo   # or "+ Project" on the dashboard
./opsd.py add -p myrepo "Fix the crash when the save file is empty"
./opsd.py add -p myrepo --model sonnet "Rename the config key"   # its worker runs on Sonnet (see Models)
./opsd.py add -p myrepo --research T1 "Which vector database suits a small app?"   # a research task (RES-2)
./opsd.py schedule add -p myrepo --every 'mon 09:00' "Check dependencies"   # a recurring job (see Recurring jobs)
./opsd.py deploy -p myrepo "store test" --version patch --note "Fixes the crash"   # see Deploys
./opsd.py send myrepo --design "How does the save format work?"   # what a worker or chat runs to ask a peer project
./opsd.py archive-project myrepo     # take it off the queue; unarchive-project brings it back (see Archiving)
```

On the dashboard you can type a full path or just a name. A name like `mygame` means a folder next to
your other projects (usually `~/dev/mygame`). If it doesn't exist, the dashboard offers to create it
as a new git repo with one empty commit.

`add-project` detects Unity (`scripts/unity.sh` + `Assets/`), Node (`package.json`) and Python
projects and writes a starter config. It also takes a workspace: a folder that is not a git repo but holds
several (see [Workspaces](#workspaces)). The project goes [Straight to main](#main-is-the-golden-version);
the config has a comment on how to opt out. Read it, adjust it, then click **Reload projects** (or
`./opsctl restart`).

## Flow (per project)

```
 Design topic ─(you agree)─┐
 Tasks tab ────────────────┴─▶ scheduler ──(up to N at once)──▶ worker: claude -p in <worktrees>/t<id>
                                                                  │ commits on task/<id>-<slug>
                                                                  ▼
                               rebase onto dev ─▶ gate: the [[gate]] commands, in order
                                  │ conflict / fail: the worker is resumed with the error (max 3 rounds)
                                  ▼ pass
        "hold for review" and an [app] launch? ── yes ─▶ build, record proof, wait for Try / Approve / Send back
        (a project with no [app] launch has nothing to try, so a held task merges like any other;
         a worker with a question still stops as needs_input. See "Proof video" below)
                                  │ no
                                  ▼
                            merge --no-ff into dev   (<worktrees>/dev)
                                  │
                                  ├─ Straight to main (the default): main moves to dev at once, no release
                                  │
     direct_main = false or [[deploy]]: you click Promote ▼
   release task: snapshot dev ─▶ gate ─▶ Claude builds and plays it (blocks, or passes)
                                  ▼ pass
        build the snapshot in <worktrees>/main ─▶ fast-forward main in your checkout
```

`<worktrees>/dev` is the queue's integration worktree, and the queue owns it. Just before a merge into it, any
uncommitted changes to tracked files there (say, a `pod install` that rewrote `Podfile.lock`) are saved as a patch,
`~/.claude-ops/runs/<task id>/dev-uncommitted-<repo or root>.patch`, then discarded, and the task's log names the
files. `git apply` the patch to get them back. Untracked and ignored files (`node_modules`, `Pods`, `.env`, build
output) stay; nothing runs `git clean`. If the patch can't be written, nothing is discarded and the task fails,
naming the files. To change dev by hand, commit in that worktree; don't leave edits sitting there.

### Proof video

A held task stops before merge so you can check it before it reaches dev. So that you need not launch every
build, the queue first records a short video of what the task changed, and the Try card plays it.

- **The step.** "recording proof" runs in the hold branch, after "building for review" and before the task goes
  to `review`. Only for a held task in a project with `[app] launch`; with no launch the task merges, as before.
- **Who records.** The queue resumes the task's own worker session once more (it knows what it changed) with a
  recording prompt: launch the build with the `[app] launch` command from the worktree, drive it to show the
  change, record 10 to 60 seconds, save `~/.claude-ops/runs/<id>/proof.mp4` (H.264, so a browser `<video>` plays
  it), and stop the app (the `[app] stop` command, or by pid; never by pattern). A web app is recorded with
  headless Playwright (`record_video_dir`, converted with ffmpeg); a native app with `screencapture -v -V <secs>`,
  limited to its window (`-l <window id>`) or display. The worker reports one line, what the video shows, in
  `$OPS_RESULT` (`{"status": "done", "proof": "..."}`) or in `runs/<id>/proof.txt`. The turn may write in
  `runs/<id>`; it changes no tracked file. If it commits anyway, the queue gates the branch again.
- **One at a time.** A queue-wide lock admits one recording turn at a time (there is one screen); a task waiting
  for it shows the phase "waiting to record proof". Each turn is capped at 10 minutes, then stopped.
- **Permission.** Before a turn the daemon calls `CGPreflightScreenCaptureAccess()`, which never prompts. If
  Screen Recording isn't granted to Claude Ops, a native app's recording is skipped (event `no proof video:
  Screen Recording not granted to Claude Ops`). A server-style app (one with `[app] running` or `stop`) is still
  recorded, headless only. See "macOS permissions".
- **Failure never blocks.** No file, a file that isn't an MP4, a timeout, an error or a missing permission: the
  task logs `no proof video: <reason>` and goes to `review` as before.
- **Messages.** The recording turn takes no messages. One you send meanwhile waits; after recording the queue
  resumes the worker with it, as for a message sent during the gate. With a message already waiting when the
  step starts, nothing is recorded: the worker gets the message, and the next pass records.
- **Send back** deletes `proof.mp4` and `proof.txt`; the next round of review records a new video.
- **API.** `/api/state` and `GET /api/tasks/<id>` give each task `proof` (true when `runs/<id>/proof.mp4`
  exists) and `proof_note` (the one line, or null). `GET /api/tasks/<id>/proof.mp4` serves the file as
  `video/mp4`, answering `Range` requests with 206 (Safari plays video only that way), and 404 when there is none.

## main is the golden version

By default a project goes **Straight to main** (below): each merge moves main at once, and dev stays equal to it.
The release, Promote and Promote after merge described here are for a project that opted out with
`direct_main = false`, or one with `[[deploy]]` targets.

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
  Today lists them too, as a **Commit** item that says they need your review and what they are (TODO-1).
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

  Pausing, stopping or cancelling the task before the merge clears nothing; Retry after a merge clears then. Under
  Straight to main, main moves right after the clearing, so the work comes back at once. Otherwise, until
  the next Promote the work is in dev but not in your checkout: the card says `On dev, not released yet. Promote to
  get it back here.` with a link to the task, until main contains it.
- With nothing new on dev, Promote only rebuilds main (no gate).
- A release snapshots dev, runs the gate, builds main, fast-forwards main and carries your uncommitted changes (a
  carry conflict still uses a worker). A gate failure blocks it. Its summary starts `Gate-only release: the merge
  check passed (<checks>)`, then gives each released task's summary, and **Not part of this release** as usual.
  A gate-only release starts while the usage brake is on, as it starts no Claude session.
- **Promote after merge** (only for a project that opted out of Straight to main, or has `[[deploy]]` targets; a
  checkbox on a new task, and on its page until it finishes) promotes for you, as a
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
- **Promote after merge by default** (a checkbox on the project's card, for the same projects) ticks that box for every task the
  project gets from then on: from the form, `opsd.py add`, a design chat, and the follow-ups a worker
  queues. You can still untick it on a task. Tasks already queued keep what they have. It is off until you
  turn it on.
- **Straight to main** is the default. To opt out, put `direct_main = false` in the project's toml; the daemon
  reads it when it reloads projects or restarts. Why it is the default: a held task is tried before it merges,
  so once a task merges into dev there is nothing left on dev for you to check, and a Promote click would add
  nothing. Opt out only when you do want to check dev before main moves. A task that passes its gate merges into dev as before, under the same lock, then moves
  main to dev at once: the steps of a release after its gate (build main if there is an `[app] build`,
  fast-forward, carry your uncommitted changes over), without running the gate again. So no release is queued:
  Promote after merge, the promote train and the Promote button go away for the project. dev stays, equal to
  main, so nothing that reads it changes.
  - Something in the way of main (the main build is running, main is checked out in another worktree, or your
    uncommitted changes can't be carried over) makes the task wait on you, `needs input`, as a release does. It
    goes on by itself once the way is clear. Answering or Retry only moves main; the merge is done.
  - Turned on while dev is ahead of main (as for a project from before it was the default), the next merge brings all of dev into main with it (its gate ran on
    all of it). Until then the card shows Promote, to release it at once.
  - Commits made straight to main come into dev before each task's gate (a fast-forward when they can). If they
    conflict with dev, the task fails and says to Promote once: the release has a worker merge them.
  - Tasks move main one at a time, and never alongside a release.
  - Send to dev lands on main too: it clears your checkout after the merge into dev, then main moves and brings
    the work back into your checkout, committed.
  - For claude-ops itself, every merge restarts the daemon to load the new code. As a restart pauses every
    running task in every project and starts their gates over, it waits until no task is running, checking or
    merging, for at most 15 minutes, and no new task starts meanwhile (they say `waiting for claude-ops to
    restart`). A running deploy, in any project, holds the restart with no time limit: after 15 minutes it
    restarts only once no deploy is running. A Promote restarts sooner: see below.
  - Not for a project with `[[deploy]]` targets: a deploy moves main too, and builds from the main a release
    checked. There the option is ignored, with a line in the daemon log.
- Promoting claude-ops itself restarts the daemon to load the new code (`opsctl restart`: running
  tasks pause and resume). If the new daemon doesn't answer within 30 s, the release's log says so; its
  output is in `runs/<id>/restart.log`.
- A running deploy, in any project, always holds a restart that loads new code, a Promote's or a merge's: a
  restart would cut its command off, and a cut-off deploy fails rather than publish twice. The restart waits,
  with no time limit, until no deploy is running (a deploy that has finished its command and only moves main
  doesn't count), and the release or merge says so in its log (`claude-ops restarts to load the new code once
  deploy #259 finishes`). Meanwhile no new deploy starts: it says `waiting for claude-ops to restart`. A
  Promote waits for nothing else: other tasks keep starting, and pause and resume across the restart. A
  manual `opsctl restart` or `stop` doesn't wait.
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
note = "What's new"                 # optional: then a note is required, passed as OPS_NOTE and OPS_NOTE_FILE;
                                    # the Deploy dialog drafts it from the changelog for you to approve
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
  worker or a design chat, and nothing that queues follow-ups can make one. Drafting the note queues nothing.
- **The note is drafted for you.** For a target with `note`, the Deploy dialog fills the note box with a draft as it
  opens, and says "Drafting from the changelog…" while it waits. You read it, edit it if you like, and clicking
  **Deploy** approves it. The draft comes from what the deploy ships, the rows under "What it ships": each task's
  title and its summary, and the subject of each commit no task made. One short Claude turn (`claude -p`, Sonnet, no tools, at most 60 seconds)
  turns them into 3 to 8 plain bullets for the app's users, user-visible changes only, at most 1000 characters.
  - **What it ships spans the previous release.** A target's release tags are its own `release_tag`, or for a `from`
    target its source's (followed up the `from` chain). When it has them, what it ships, in its own repo, is the
    commits from the newest release tag (by version) that the commit it deploys descends from, up to that commit.
    A tag on that commit itself doesn't count; the one before it does. This holds on every deploy, with or without
    an earlier one: store release notes describe the change since the last store release. The list, its count (the
    Deploy button's `(N)`) and the draft all use this span, and the dialog says so: "What it ships: 12 tasks since
    v3.5.2".
  - With no matching tag, or no release tags at all, what it ships is everything since the target's last deploy (for
    a `from` target, what its source deployed since then).
  - If Claude fails, takes too long or says nothing, the draft is one `- title` line per row, cut to 1000 characters.
  - With no changelog (a first deploy with no release tag, or the last deployed commit gone) nothing is drafted: you
    type it, as before.
  - A draft never replaces what you typed: if you type before it arrives, it stays out of the box. **Redraft**, by
    the box, asks for a new draft, and asks before replacing text you changed.
  - The note is still required: `POST /api/deploys` refuses an empty one, and Deploy stays off until the box has
    text. `opsd.py deploy` drafts nothing and still needs `--note`.
  - `POST /api/deploys/draft {project, target}` answers `{note, source}`: `source` is `"model"`, `"titles"`, or
    `null` with an empty note when there is no changelog.
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
  A deploy cut off by a restart of claude-ops fails the same way rather than run again by itself. A restart
  that loads new code (a Promote or merge of claude-ops) waits for running deploys, so only a manual
  `opsctl restart`, a stop or a crash cuts one off.
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
  note, drafted for you, when the target asks for one. Today's **Ship** step opens the same dialog. While a deploy of the project is open, each button reads `Deploying… #N` and
  links to it; **Queue deploy** next to it queues another behind it. Deploy tasks are marked `deploy` in the task list,
  and a deploy's task page shows its log in place of the worker activity.
- **In `/api/state`.** Each project's `deploys` lists every target: its config and any config error, the last
  deploy (commit, time, task, version), how many commits it is behind, the commit it would deploy now with its
  subject, and for a target with `version` the current version, its release tag if any, the newest release and
  build tags, and the version options. Each target's `open` is its deploy task now open, and the project's
  `deploy_open` the first open deploy, if any.

## Design mode

A design chat is Claude talking with you about one project. You meet it as **plan & design** in the Today view's
conversation (see [TODAY-2](#today-view)), where each design chat is a topic of the project's one conversation.

It reads the project's docs and the latest dev code and changes the project through tasks
(`--permission-mode dontAsk`). Two exceptions, both because you asked: for research it queues a research task
(`opsd.py add -p NAME --research T1 "question"`), and for a change you want made directly in your checkout it edits
your checkout (see *What a topic may run* in [TODAY-2](#today-view)). It can write documents of its own, such as a design doc or a one-file HTML prototype, in `~/.claude-ops/designs/<chat id>/`:
the only place it may Write or Edit, outside every repo. Its reply links them, the dashboard lists them above
the chat, and `/api/designs/<id>/files/<name>` serves them (HTML sandboxed, so a prototype's scripts can't call
the dashboard's API). Workers can't see that folder, so a brief that builds on a document quotes it. When you
agree on the work, ask it to queue: it writes self-contained task briefs, and they appear as chips
under its reply. Each message it gets starts with that project's current queue, so it avoids duplicates.
When it has read the code a task touches, the brief ends with **Where to look**: about 20 lines of
`file:line` pointers and facts it found, so the worker skips the search. It merges small pieces into one
task only when they touch the same files. A worker that splits its task writes the same section.

### OpenSpec changes

If a project uses [OpenSpec](https://github.com/Fission-AI/OpenSpec), the Today view's **Specs & designs**
section (see [TODAY-4](#today-view)) lists its open changes (`openspec/changes/<name>/`, not `archive/`), with how
many `tasks.md` items are done. Click one to open its chat as the current topic. A change that is only in your
checkout is first copied into `dev`. The list shows for every repo of the project whose `dev` has an `openspec/`
folder, even when it has no open changes.

- **Set it up.** Each repo of the project (each repo of a workspace) whose `dev` has no `openspec/` shows a muted
  line in the section: `<project>[/<repo>]: not using OpenSpec · Set up`. **Set up** runs
  `openspec init --tools claude` for you.
- **Refresh its commands.** **Refresh commands**, under the list of changes in Specs & designs, runs
  `openspec update` in each repo of the project that uses OpenSpec, to bring its opsx commands and skills up to
  date with your CLI. It commits the commands and skills that changed to `dev`.
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
  result above the list: `Set up: committed <n> files to dev at <sha>.`, `Nothing to do: the commands are up to date.`, or the
  CLI's error (its last 300 characters). Set up is refused when `dev` already has `openspec/`; Refresh is refused
  when it has none. Both are refused while one of the project's tasks is merging into `dev` (try again after the
  merge), and both are disabled when the `openspec` CLI isn't on the daemon's `PATH`. Once Set up lands, the
  project shows **New change**.

- **Create one.** Click **New change** in Specs & designs, write one line on what the change should do, and click
  **Start**. Pick the repo first when the project has several with `openspec/`. The button is disabled when the `openspec` CLI isn't on the daemon's `PATH`
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

- **Read it.** When the current topic in Today is an OpenSpec change's chat, the conversation head shows
  **Read the change**. It opens the change's reader, like a docs site, in Today's side drawer: the same drawer repo
  files and research reports open in. The reader's header stays in view: the title, the path, overall progress
  (`done/total`), the "Why", the queue buttons and the command buttons (see *What a change's chat can run*
  below). Below it are tabs for **Proposal**, **Design**, **Tasks**, **Specs (N)** and **Other** (any other `.md`
  file), each shown only when it has content. Each heading has a link to it. The reader remembers the tab for each
  change, and keeps your place in each file, also when Claude or a worker edits the files.
  - **On this page** lists the tab's `##` and `###` headings and marks the one you are reading. Click one to
    jump to it. In a reader under 900px wide it is a **Sections** menu instead.
  - **Specs** shows each delta file (`specs/<capability>/spec.md`) as cards, one per `### Requirement:`, edged
    by what the delta does: ADDED green, MODIFIED amber, REMOVED red, RENAMED blue. Each `#### Scenario:` shows
    its **WHEN**, **THEN** and **AND** lines as rows. A file that isn't in delta form shows as plain markdown.
  - **Tasks** shows one section per `##` phase of `tasks.md`, with its `done/total` and a progress bar. Phases
    that are fully done start folded.
  - **Closing it.** The drawer closes on Escape, on **Close**, on a click beside it, when you switch topic or
    leave Today, and after you press a command or queue button, so you see the reply. On a phone it fills the
    screen, like the file drawer.
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
  to the change, in this order (hover for what it does). A click sends the command's text into the topic as a
  message, through the same path as **Send**, so it shows in the conversation as if you typed it. The buttons are
  disabled while Claude is replying. **Archive** asks first: `Archive <name>? This moves it to openspec/changes/archive and merges its
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

The Today view shows which chats need you (see [TODAY-2](#today-view)). Every chat you haven't ended is a
**Read** to-do once Claude is done replying (marked new until you read it), and an unread one is ● in **Earlier
topics**. The to-do list and Earlier topics follow the project filter. An ended chat is done: a reply that lands in
it later (a task's result note) makes it new nowhere.

- **Ending a chat.** Click **End** on its row in Earlier topics, or **End topic** above the message box, to move it
  under **Ended**, folded at the bottom of Earlier topics. Its replies count as read and its queued messages are
  dropped. Ending never waits for Claude: if Claude is replying, End stops the reply and ends the chat at once, and
  the stopped reply, when it finishes, leaves the chat ended and read (it never reopens it, sends the queue, or puts
  it back on the to-do list). Research topics stop their running research turns. Nothing is deleted. To put it back, click **Reopen**
  there, click the topic under Ended, follow a link to it, or send it a message. Opening an OpenSpec change reopens
  its chat as well. A view from before you ended it never reopens it: an open reopens only when the `ended` it
  sends matches the stored one (compare-and-set, see TODAY-2).
- **Mark read** marks a reply as read. Chats from before this feature start as read.
- **Queue a message.** While Claude is replying, the **Send** button reads **Queue**. A queued message shows
  faded at the end of the chat, and goes to Claude when the reply ends; several queued messages go together as
  one. A slash command goes on its own. **Edit** takes a queued message back into the message box. **Stop** ends
  the reply early, and the queue goes next. Task chats don't queue yet.

## To-dos

Things that need you live in several places: Tasks' **Needs you**, unread chats, a release **waiting on you**,
the uncommitted-files banner, Deploy. `/api/state` gathers them into one list, `todos`, for a Today view to show.

- **TODO-1 Derived, not stored.** Most items are a live query on each state build. An item appears when its
  state does and goes when the state changes: there is nothing to tick off.
  - **Answer**: a task (not a release or deploy) in needs_input. Why: its question.
  - **Fix**: a task in failed. Why: its error.
  - **Try**: a task in review whose project has a launch step (the same test as `can_play`). The card plays the
    task's proof video when there is one (`proof`, see [Proof video](#proof-video)), with `proof_note` as its
    caption under it. Then **Approve & merge** is the primary button and **▶ Play this build** a secondary one;
    without a video Play stays primary, and a muted line says `no proof video` (with the reason the task's last
    `no proof video: …` event gave). **Review**: a task in review whose project has no launch step; you read its
    diff and summary.
  - **Promote**: a release or deploy in needs_input (**waiting on you**), or `dev` ahead of `main` in a project
    with **Promote after merge** off and no release under way. Under Straight to main dev is ahead only until
    the next merge (a project from before it was the default), or while a merge waits on you to move main.
  - **Ship**: `main` has moved since the last deploy of a project with deploy targets, and no deploy is under
    way. This reads what the Deploy panel already knows: `main_since_kind` is `deploy` and `main_log` is not
    empty. A project with no recorded deploy of `main` yet shows no Ship item.
  - **Commit**: your checkout has uncommitted files, which need your review before they can ship. Title:
    `Review N uncommitted files in <project>`. Why: up to 3 of the paths, relative to the repo, then how many
    more, per repo: `docs/research/foo.md, notes/bar.md +1 more in ~/dev/research`. Both reuse the banner's list;
    no extra git call. Its card lists every file (the first 500 per repo): new, changed or deleted, lines added
    and removed (`git diff --numstat` against HEAD; for an untracked text file, its line count; `binary` for a
    binary file) and how old it is (mtime). Below that, a folded **Show diff**: the first 400 lines of the diff,
    with a note when it is cut, an untracked file shown as a diff against nothing. The card reads these from
    `GET /api/projects/<name>/uncommitted`, read only (git status and diff without an index lock, file reads):
    `{repos: [{repo, files: [{path, status, added, removed, binary, mtime}], more}], diff, diff_lines, cut}`.
  - **Read**: a design or project chat with a reply you haven't opened, as ● in Earlier topics marks it.
  - **Read** (a report): a research task merged into `dev` whose report you haven't marked read, or a report any
    merged task listed in its result (see [Research](#research), RES-5 and RES-10). Opening the report leaves it on
    the list: a report you looked at may still need a decision. Only **Mark read** closes it.
- **TODO-2 Two kinds Claude adds, and your own.** **Do**: something that truly needs you: a password or sudo, a macOS
  permission grant (Full Disk Access and the like), an API key or account you must create. **Agree**: a
  decision; Claude proposed something and waits for your yes. A step that a command or code change can do is
  never a to-do, even setup that has to wait for a task to merge: a chat queues it as a task, a worker as a
  follow-up (TODO-5). The worker, design and follow-up chat prompts all give this same list. These
  are stored in the `todos` table. In every chat block (`queue`, `todo`, `schedule`, `topic`) a fence closes
  the block only on a line of its own, so three backticks inside a JSON string are content. Three ways in:
  - A design or project chat reply with a fenced block in the language `todo`, holding a JSON list of
    `{"verb": "Do" | "Agree", "title", "why", "wait"?}`. The block is taken out of the reply you see, as a
    `queue` block is. The item's source is that chat. `wait` parks the new item until tasks end (TODO-6):
    `{"tasks": [283], "until": "merged" | "released"}`, and/or `{"after": [0]}`, 0-based indices into the tasks
    the same reply's `queue` block queued, which become their new ids. A task id that isn't there, or an index
    out of range, is noted back to Claude as a skipped wait, and the item is still added, unparked.
  - A research chat turn's reply with the same block (see [Research chats add to-dos](#legacy-research-runs-in-the-research-repo)).
    It goes to the claude-ops project the research project links to (`repo`), else to the project whose
    checkout is the research repo. Its source is kind `research`, id `r:<thread>` (Today shows it as a chat and
    opens that research topic).
  - A worker's result JSON with an optional `todos` list of the same shape. The task doesn't stop for it. The
    source is that task. Its `wait` may hold `tasks`, not `after`.
  - Your own: `POST /api/todos {project, title, why?, wait?}` (the Today list's **+ Add** row, TODAY-1) stores
    a **Do** with `source_kind` `owner` and no source id. `wait` is `{tasks, until}`, as above; with it the item
    is created parked. An unknown project, an empty title or an unknown task id is a 400 that says so, and
    nothing is added.

  Every item but your own links to its source. An open item with the same project and title is not added
  twice: Claude's is skipped quietly, yours is a 400.
- **TODO-3 Closing.** Derived items close by themselves. A stored item closes with
  `POST /api/todos/<id>/done` (your Done button), or when Claude writes `{"close": <id>}` in a `todo` block. The
  id is the integer `12` or the string `"todo:12"`; anything else is noted back as not an id and closes nothing.
  Sending a message in the chat (or research topic) that added an Agree doesn't close it: the message may be about
  something else. Claude closes it when your reply settles it (a yes, a no, or a changed plan), and leaves it open
  while it is still pending. So it can, each message to a design or project chat carries the chat's open Agree
  items, with their `todo:N` ids, in its `[ops: ...]` line (a research topic, in its `(Dashboard: ...)` line).
  Closed items stay in the table.
- **TODO-4 Order and groups.** Each item has a group: `unblock` (Answer, Fix), `ship` (Promote, Ship, Commit),
  `review` (Try, Review, Agree, Read), `only_me` (Do). The list runs unblock, ship, review, only_me, oldest first
  within a group.
- **TODO-5 Follow-up tasks.** A worker's result is `{"status": "done" | "needs_input" | "failed" | "split",
  "summary", "question"?, "tasks"?, "todos"?, "followups"?}`. `followups` is a list of `{title, body}`, the
  shape of split tasks, for steps a task can do once this one has merged (running a command this task adds,
  say). It is read only with status `done`; on any other status it is ignored. Each item is queued as a task in
  the same project with `parent` and `depends_on` set to this task, so it starts only after this task merges.
  Priority, model and effort take the queue defaults and release the project default, not the parent's. The
  parent logs `follow-ups: #N, #M`. If the parent then fails at the gate or merge, the follow-ups wait behind it
  as any dependent does. A worker resumed after a gate failure may list them again: a title already queued
  behind the task is skipped. Each body stands alone, like a split task: goal, exact steps, acceptance.

Each item is `{key, verb, group, project, title, why, since, est_min, closes, source: {kind, id}}`. `key` stays the same
across polls (`task:231:review`, `chat:42:read`, `project:diskgame:commit`, `todo:7`, `task:240:report`).
`since` is when the state began, as near as the queue knows: a task's last event, a chat's last reply, the oldest uncommitted file, the
oldest commit not yet promoted or deployed. `est_min` is a guess at minutes: Answer 2, Fix 5, Try 5, Review 3,
Agree 2, Read 3, Promote 1, Ship 2, Commit 2, Do 5. `closes` is one plain sentence on how the item leaves the
list, which the Today step shows under the why and the list under the item, both in muted text (a parked item
with a wait shows its wait text in the list instead): Answer "Closes when you answer.", Fix "Closes when it is
retried or cancelled.", Try and Review "Closes when you approve or send it back.", Promote "Closes when dev is
promoted to main.", Ship "Closes when you deploy.", Commit "Closes when the files are committed or gone.", Read of a
chat "Closes when you end the topic.", Read of a report "Closes when you mark it read.", Agree "Closes when Claude
records your answer, or you click Done.", Do "Closes when you click Done.". The server writes it, so every view
says the same. `source.kind` is `task`, `chat`, `project` or `owner`: a
stored item points at the chat or task that added it, and has `todo`, its id for the Done button; one you added
yourself has kind `owner` and id `null`, and no source link. The database id is an integer, but everywhere a
person or a model reads it, a stored to-do's id is written `todo:12`, so it can't be mistaken for task `#12`:
the dashboard (see Task ids), the notes after a `todo` block (`added to-do todo:12`, `closed to-do todo:12`,
`no open to-do todo:12 in <project>`, `to-do todo:12 in <project> already has that title`), and the design,
project and research chat prompts, which say `#N` is a task and `todo:N` a to-do. Every item also has `note` (your note to self, `""`
when none); a parked item has `parked: {since, wake_tasks, wake_on, wake_task}` (`wake_task` is the first of
`wake_tasks`, or null, kept for one release for older dashboards) and a skipped one
`skipped: <ts>` (TODO-6).

- **TODO-6 Notes and parking.** Notes live in the table `todo_notes (key, note, parked, skipped, wake_tasks,
  wake_on, wake_set, updated)`, keyed by the item's `key`. It works for any item, derived or stored.
  - **A wait** is a set of tasks, `wake_tasks` (a JSON list of ids), and a target, `wake_on`: `merged` (the
    default) or `released`. A database from before wait sets has a `wake_task` column; each row with one gets
    `wake_tasks` `[N]` on start.
  - `POST /api/todos/note {key, note}` saves the note. When no explicit wait is set (`wake_set` 0) it also
    re-reads the wait from the note: its first `#(\d+)` or `task (\d+)` (any case) gives `wake_tasks` `[N]`
    until merged, else none. A wait set by the picker or by Claude (`wake_set` 1) is never changed by a note
    edit. Editing a parked item's note keeps it parked; clearing it does not unpark it.
  - `POST /api/todos/wait {key, tasks: [ids], until}` sets an explicit wait and parks the item (the **Wait
    for…** chooser, TODAY-2). `until` is `merged` or `released` (default `merged`). An unknown task id is a 400
    that names it. `released` when every listed task's project is straight to main (`direct_main`) is accepted
    and stored as `merged`: there, merged is released. `tasks: []` clears the wait and unparks the item.
  - `POST /api/todos/skip {key}`: with a non-empty note or an explicit wait it parks the item (`parked` = now);
    otherwise it skips it (`skipped` = now). A skip lasts until local midnight.
  - `POST /api/todos/unpark {key}` clears `parked` and `skipped`; it is also how a skip is undone (the **Unskip** button).
  - Order: unparked items as TODO-4, then skipped ones in the order you skipped them, then parked ones, which
    carry `parked` for the dashboard to group.
  - **Auto-wake.** On each state build, a parked item with a wait wakes (is unparked and not skipped) when
    either:
    - every listed task has reached the target. `merged`: the task is done (with a merge; a task done with no
      code to merge counts too). `released`: also, every commit in its `merged` is an ancestor of its project's
      `main`. In a straight-to-main project merged counts as released. A task that is merged but not yet
      released keeps a `released` wait parked.
    - any listed task stops short: failed, needs you (needs_input), in review, paused, cancelled, or deleted.

    On wake, one entry per task is added to its note: ` · #283 merged, #284 failed`, with the word for each
    task: merged or released (the target reached), done (done with nothing merged), failed, needs you, in
    review, paused, cancelled, gone (deleted), or the state of one still under way (queued, running…). A note
    with no task number and no explicit wait never wakes; it stays parked until you unpark it.
  - **The released check** reads `main`'s tip from the project facts the state build already has, so it adds
    no `git rev-parse`. Per project it keeps which merge commits `main` contains, against that tip: while
    `main` doesn't move, no git call; when it moves, one `git rev-list --ignore-missing <shas> --not <main>`
    per repo for the commits still waited on.
  - A row is deleted when its key is no longer in `todos` (the item went by itself), or when its note is empty
    and it is neither parked nor skipped since today's midnight. Items of a project whose facts failed to load
    keep their rows.

There is no `done_today` count yet: derived items leave no record when they close, so only stored items could be
counted, and a number that skips most of what you did would mislead.

### Links in chats

Every reference in a design chat, project chat or task chat message, from Claude or from you, is a working
link. A reference whose target is missing is never a link that fails on click. The Today view will reuse this.

- **LINK-1 Images.** `[image: <path>]`, `![alt](path)` and a bare image path show a thumbnail; click it to open
  the picture full size. Paths into `~/.claude-ops/images/` and a design chat's folder `designs/<id>/` map to
  `/api/images/…` and `/api/designs/<id>/files/…`. Attach a picture with the 📎 button, by pasting, or by dropping
  it on the message box.
- **LINK-2 Tasks.** `#231` (a `#` and digits, as a word of its own, not in code or a URL) opens that task's
  details, for any task id: one older than the 500 in `/api/state` comes from `/api/tasks/<id>`.
- **LINK-3 Repo files.** A relative repo path such as `docs/design.md`, `opsd.py:5914` or `README.md § Design mode`
  opens that file as it is on the project's dev worktree, in a side drawer. Markdown renders as on the page and
  scrolls to the `§` heading; other files show as numbered lines, scrolled to `:line`. An
  `openspec/changes/<name>/…` path (a `spec:` link) opens that change's chat as the current topic in the Today
  view, and adds its project to the project filter when it is out of view. A design-folder file opens
  from `/api/designs/<id>/files/<name>`. `GET /api/files?project=<name>&path=<rel>` serves the file: only one
  inside that project's dev worktree (symlinks resolved), not git-ignored, and 1 MB or smaller.
- **LINK-4 Places in the app.** Names of places, such as Today view, Projects tab, Settings, Research tab or Analyze,
  link to that place's hash route. The old names Design tab and Chat tab link to `#today`. One table in
  `dashboard.html`, `APP_PLACES`, maps each phrase to its route; a new place (the Today view's Scheduled,
  More → …) is one more row.
- **LINK-5 URLs.** An http(s) URL opens in a new tab with `rel=noopener`. A trailing `.` `,` `)` `:` or `;` is
  the sentence's, not the link's.
- **LINK-6 No broken links.** Before a message renders, the daemon checks every target in one batched request,
  `POST /api/links/check {project, refs: [...]}` → `{refs: {ref: null | reason}}`. The page keeps each answer,
  so redrawing a long chat asks nothing new; a missing target is asked again after two minutes. The project is the
  chat's project. A missing target shows greyed with ⚠ and a tooltip giving the reason, such as
  `Not on dev: docs/x.md` or `No task #9999`, and is not clickable. A plain word that only looks like a file
  name (Node.js) stays plain text when there is no such file. For `README.md § Design mode for details` the
  daemon also returns the heading it found, so the link ends at "Design mode".
- **LINK-7 Tests.** `tests/test_chat_links.py` has one test per kind (image, task, repo file, file with `§`,
  OpenSpec path, design file, app place, URL with trailing punctuation), one for a missing target, and one for
  `/api/files` refusing a path outside the worktree.

Text in fenced code and inside a longer code span is never linked. A code span that is only a file path (as
Claude usually writes one) links as that file.

## Today view

The dashboard opens on **Today** (`#today`): one page for the day, in three columns. The header holds the name,
the project switcher and **More ▾**. The switcher filters all three columns, and every other page, to a set of
projects. More holds the older pages: History (the old Tasks tab), Projects, Research, Analyze, Settings and Schedules, and
**+ Project**. Their routes (`#tasks`, `#projects`, `#schedules`, …) still open them. The Design and Chat pages are
gone: `#design` and `#chat` open Today and rewrite the address to `#today`. The queue readouts and pause buttons
stay on the right of the header.

- **The project switcher.** It opens a checklist: **All projects** at the top, then every project that isn't
  archived, each with a tick box. A tick applies at once. **All projects** clears every tick, and so does unticking
  the last one. The button reads `All projects`, the one project's title, or `A, B +N` when more are ticked (about
  2 titles fit, then a count).
  - **One ticked** works as one project: lists leave out project tags, the project pickers hide, and new tasks
    and to-dos go to it.
  - **Two or more ticked** work like All projects, limited to the set: lists show project tags, and every project
    picker (New task, the conversation's **Project**, + Add's select, the schedule form, the quick chat's default)
    shows but lists only the ticked projects. It starts on the focused project if that is ticked, else on the
    first ticked one.
  - Opening something in a project outside the set (a task, a to-do, a chat link) adds that project to the set.
    Nothing you click in the set shrinks it. A project that is removed or archived drops out of the set.
  - It changes the view only: the queue, the workers and the schedules run as before. The set is kept in this
    browser (localStorage `ops-proj`, a JSON array of names; an older plain name reads as a set of one).

- **TODAY-1 Left: To-do.** The `todos` list from `/api/state`, nothing else.
  - Filter chips, each with its count: **All**, **Review** (Try, Review, Agree, Read), **Unblock** (Answer, Fix),
    **Ship** (Promote, Ship, Commit), **Only me** (Do).
  - Each item shows its verb, title, project (unless one project is ticked in the switcher) and age.
  - The selection is the item's `key`, so it survives polls and reloads. When the selected item leaves the list
    (you acted on it, or its state changed), the item after it is selected.
  - **Skip for now** is one click. With an empty note it moves the item to the end of the list until local
    midnight. With a note it parks the item: it leaves the list and the chip counts,
    and goes to a folded **Parked (N)** section at the bottom. Both are kept on the server (TODO-6), so
    every device agrees. A parked row shows its verb, title, project, the note and `parked 4d` (its age since
    parking). A parked item with a wait says so on its row: `Waiting on #283, #284 to merge` (or `to
    release`). An item with a note shows the note on its row, muted and cut with an ellipsis.
  - **+ Add** sits at the top of the list, under the filter chips. It opens a small form:
    a title, an optional why, and the project: the switcher's when one is ticked, else a select of the projects
    in view (every project, or the ticked ones).
    **Wait for…** in it opens the same chooser as on a step (TODAY-2), so the new item starts parked. **Add**
    calls `POST /api/todos` (TODO-2); the item is a **Do** you close with **Done**. An error (a duplicate title,
    an unknown task) shows under the form, which keeps what you typed.
  - The next item selected when one leaves the list is never a parked one.
  - **Projects** sits at the top of the column, above the filter chips, **+ Add** and the to-dos; **Parked (N)**
    and (on a phone) the Topics row follow the to-dos. **Projects** and **To-do** each have a header you click to
    fold or unfold the section, like **Parked**. Both start unfolded. A folded header keeps a count:
    **Projects (N)**, the projects in view, and **To-do (N)**, the to-dos under the current filter. Whether each
    is folded is kept in this browser (`ops-td-projs-open`, `ops-td-todo-open`), so it survives reloads and polls.
    The Projects section has one compact row per project in view (every
    non-archived project, or only the ticked ones), in the order of `/api/state`'s `projects`. A row shows the
    title, its count of open (not parked) to-dos when above 0, and a ● when it has an unread chat (a Read to-do
    from a chat). With one project ticked its one row still shows. Clicking a row opens that project's **home**
    in the centre (TODAY-2): no to-do is selected. The home is kept in this browser (`ops-td-home`) and its row is
    highlighted. Clicking a to-do clears the home. Clicking the selected to-do again, or **Esc** on Today with no
    menu, panel or dialog open and focus outside a text field, goes to the home of that item's project. While a
    home is open nothing is auto-selected, so a new to-do arriving doesn't move the centre. If the home project
    leaves view (the switcher changes, or it is archived or removed), the home clears and the auto-select
    returns.
- **TODAY-2 Centre: the step.** The selected item as one step: **Step k of n** (k within the filter) · project,
  the title, the why, and key facts. For a task: its number, the gate result, how many files it changed, and how
  long ago it was built. One primary button, a few secondary ones, and **Skip for now →**. Every button calls an
  endpoint the older pages already use. Beside **Skip for now →**, on the desktop and phone layouts alike, is a
  one-line note field, always shown, with the placeholder `Note to self, e.g. wait for #249`. It saves as you
  type (after about 500 ms, and on blur) through `POST /api/todos/note`. Skip's tooltip says what it will do:
  "Move to the end of the list until midnight" with an empty note, "Park it with this note" with one. A parked
  item's step has **Unpark** beside Skip; a skipped item's step has **Unskip** there, which calls the same
  endpoint. See TODO-6.

  A **Try** step shows the task's proof video under the title: inline, muted, looping and autoplaying, with
  controls, at most the card's width and about 360px high (less on a short screen; the card grows to fit it and
  the buttons). Its one-line note is the caption. The video loads only once it is in view (`preload="none"`
  until then), from `GET /api/tasks/<id>/proof.mp4?v=<when it was recorded>`, so a new recording after Send back
  is fetched fresh. A poll's redraw keeps it playing. A held task's details page (the review state) shows the
  same video above the feedback box, and there too Approve is primary when a video exists.

  The centre shows either a step or a project **home**. The home is a slim card: the project title, one muted
  status line from `/api/state` (for example `2 running · 4 to-dos`; zero running is left out), and the project's
  **pipeline**, with the Projects card's endpoints and rules:
  - The top line: **▶ Play dev** / **■ Stop dev** (only with a launch step), then a **dev → main** stage (left out
    for a straight-to-main project). It says **N ahead** with a **Promote** button, **Release #N in progress**
    (pulsing, a link to the release) while a release is open, or **✓ in sync**.
  - Below it, one **lane** per deploy chain. A lane starts at a `[[deploy]]` target with no `from` (or a `from`
    that names no target, or a loop of them) and follows the targets whose `from` names the stage before, joined
    by arrows: for ballup-monorepo **swish** test → distribute, **dunk** prod, **web** preview → prod. A second
    target with the same `from` starts a lane of its own, after its parent's, led by `<parent> →`. Lanes of one
    repo sit together, in config order. A lane's label is the first word all its target names share, cut off each
    stage's name ("test", "distribute"); with no shared word the label is the repo and stages keep their names.
    The grouping is `depLanes()`, tested in `tests/test_deploy_lanes.py`.
  - Each stage chip shows the short name, a status and, when known, the last deployed version and how long ago,
    in small muted text. Status: **N commits behind** or **first deploy** (accent, with a compact **Deploy**
    button), **✓ up to date** (muted, no button), **?** (the commit it last deployed is gone from git), **blocked**
    or **error** (warning colour, the reason on hover, not clickable), or **deploying #N** (a link to the open
    deploy task) in place of the status. A click anywhere on a chip that isn't blocked opens the Deploy dialog,
    so an up-to-date stage can still be redeployed.
  - A project with no launch step, straight to main and no deploy targets shows no pipeline. On a phone the
    lanes stack, and a lane's chips wrap after an arrow.

  Errors show in the card's message line. The **All clear** card (empty list, no home) is this card for the conversation's project, with a muted
  line that nothing needs you. The tabs and conversation below are unchanged. A message sent from a home
  carries no to-do (rule 5).

  Every step also has **Wait for…** beside Skip. It opens a small chooser under the step. A header line says
  what it does: `Park this item until tasks finish. It comes back when they do.` Under it come the item's
  project's tasks that are queued, running, gating, merging or in review (id and title, a checkbox each), then
  `Or type other task ids:` and a box for them (`e.g. 283, 290`). With no such task the list is replaced by one
  line, `Nothing of <Project> to wait on. Type a task id:`, above the box. Last comes `Come back when they're:`
  and a **Merged / Released** toggle. A straight-to-main project has no toggle, only the muted text
  `Comes back when these merge.`, since a merge is the release there. **Wait** calls `POST /api/todos/wait` and the item goes to Parked. A parked item with a wait
  reads `Waiting on #283, #284 to merge` (or `to release`) at the top of its step, with **Clear**, which sends
  `tasks: []` and so unparks it. The chooser opens with the item's current wait ticked.

  | Verb | Primary | Others |
  |---|---|---|
  | Try | ▶ Play this build (the task's play). With a proof video (`proof`), the card plays it above the buttons, `proof_note` as a one-line caption under it, and Approve & merge is primary | Approve & merge, Send back…; with a proof video, ▶ Play this build, Send back… |
  | Review | Approve & merge | Open diff (the task's details, files changed), Send back… |
  | Answer | Answer (focuses the message box; sending answers the task) | Details |
  | Fix | Retry | Retry with a note…, Retry after… |
  | Promote | Promote dev → main; for a release waiting on you, Stop main when the app is running, else Reply… | Details; for a project with a launch step, ▶ Play dev (■ Stop dev while it runs) |
  | Ship | Deploy (the Deploy dialog), one per target, each with its own count: `Deploy <target> (N)`, `(up to date)`, `(first deploy)`, or `(?)` when its last deployed commit is gone. N counts from the previous release tag when the target has release tags (see "The note is drafted for you"), else from its last deploy; a target with a previous release tag is never `(first deploy)` | For a project with a launch step, ▶ Play main (■ Stop main while it runs; off until it is built or while a play is under way) |
  | Commit | Commit these: **Send to dev**, unchanged underneath. Its tooltip says it lands the files on `main` through the gate, then clears them from your checkout; `dev` in a project with Promote after merge off (and not straight to main). While one is open: Sending… #id | |
  | Read | A chat: Mark read when it is new, else End topic. A report: Mark read (RES-5) | A chat: End topic, or Reply. A report: Open report (it stays on the list), Details |
  | Agree | Reply (focuses the message box; sending doesn't close it, Claude does once your answer settles it) | Done |
  | Do | Done (`POST /api/todos/<id>/done`); the same for one you added yourself (**+ Add**) | |
  | Every verb | | Wait for… (the chooser above), beside Skip |

  The Ship step also lists its changelog, open, under the why: `main_log`, the rows the Projects card's
  `main` changelog shows (task or short sha, repo, title, how long ago), at most 15, then **and N more**, which
  opens the project on Projects. With no `main_log` it says "First deploy: no changelog yet." The Play and Stop
  buttons call the Projects card's `POST /api/projects/<name>/play` and `/stop`; a failed play shows in the step's
  message line.

  **Send back…**, **Retry with a note…** and **Answer** use the message box below: a line above it says where the
  message goes, with **Cancel**. **Retry after…** opens a chooser in the step instead, as **Wait for…** does (see
  **Retrying** under the queue's controls); its errors show in the step's message line.
- **TODAY-2 Centre, below: one conversation per project.** One running conversation for the project in view,
  with one message box. With one project ticked in the switcher it is that project. Otherwise it is the selected
  item's project, else the open home's project (the conversation follows the home), else the last one you used (a **Project** picker shows when there is neither; it lists the
  projects in view). It replaces the Design chat list, the Chat list and research
  chats on this page.
  - **Topics.** The conversation is made of topics. A topic is an ordinary chat row (`designs`: a design chat, a
    project chat or an OpenSpec change's chat) or a research thread, each with its own Claude session, fresh-session
    handoff and files, stored as before. Old chats are topics as they are; nothing is migrated. The project's
    current topic is kept by the daemon (setting `conv:<project>`), so every browser shows the same one. With
    none kept, it is the project's topic with the latest activity.
  - **The timeline.** One scrolling timeline: the current topic's messages, under a divider line
    `<date> · <topic title> · <mode>`. Above it, **Earlier topics (N)** is folded. Open it for every other open topic
    of the project, newest first: title, mode, last activity, and ● when it has a reply you haven't read. Click one to
    make it the current topic. Sending a message into an older topic resumes it, as sending to an ended chat does.
  - **Ending topics.** You end a topic when you are done with it. It leaves Earlier topics and the to-do list, and
    its replies count as read, now and later. A chat's end is stored in `designs.ended`; a research
    thread is listed in the setting `research_ended`. Both survive a restart.
    - **End** on a row of Earlier topics, or **End topic** above the message box for the current one (your next
      message then starts a new topic). Both are refused while Claude is replying: stop the reply or wait.
    - They act on the topic shown, in the project of the conversation shown (the `project` that
      `GET /api/conversation` returns), never in whatever project the view moved to since. After you click another
      to-do, End topic and New topic are disabled, and the rows of Earlier topics do nothing, until its conversation has
      loaded. If an end is refused anyway, the view shows why and reloads the conversation.
    - **End read topics older than N days** (1, 7, 30 or 90) ends every open topic you have read, that Claude isn't
      replying in, other than the current one, whose last activity is older than that.
    - **Ended (N)**, folded at the bottom of Earlier topics, lists ended topics with **Reopen**. Opening one, or
      sending it a message, reopens it too. While Earlier topics is folded, its label says how many have ended:
      **Earlier topics (2) · 1 ended**. On a phone, see PHONE-5 for how to reach them.
    - **Undo.** For 8 seconds after an end from Today (**End topic** above the message box or on the step, **End**
      on a row), a box says **Ended "<title>"** with **Undo**: at the bottom of the screen, or under the header on a
      phone. Undo reopens the topic (`/reopen`), and makes it the current topic again when it was and the
      conversation shown is still that project's new topic; if the next to-do has opened its own topic, the ended one
      just goes back under Earlier topics. The end's other effects stay: its queued messages are gone, a stopped
      reply stays stopped, and its replies count as read. A newer end replaces the box. The quick chat's × has no Undo.
    - **Opening reopens only what you saw ended (compare-and-set).** Every open of a chat or topic from the
      dashboard (`POST /api/conversation` with `how: chat|topic`: a Today item whose source is a chat, a click in
      Earlier topics or Ended, a link to a chat) sends `ended`, the value it last saw for that topic: `null`, or when
      it ended. An ended topic reopens only when that equals the stored value: you knowingly opened an ended topic.
      If you saw it open but it has ended since (a Today list or `/api/state` answer from before End topic), it stays
      ended, doesn't become the current topic, and the answer carries `still_ended: <ref>`; the dashboard drops its
      Read step and moves to the next item. A Today item always sends `null`, so Today never reopens an ended chat:
      an owner to-do that came from an ended chat shows the project's current topic instead. End topic also drops the
      chat's Read step at once, and keeps it off for 20 s against an older `/api/state` answer. A call without
      `ended` (the CLI, older callers) reopens as before. No time windows.
    - With no current topic kept, the view picks the latest open topic, else starts a new one.
    - API: `POST /api/conversation/end` and `/reopen` with `{project, ref}`, and `/end-read` with `{project, days}`.
      Each answers with the conversation, as `GET /api/conversation` does.
  - **One kind of topic.** Above the message box there is only **New topic**, and a line under it when your next
    message starts one. Every new topic is plan & design: a design chat. You don't pick a mode first; you ask Claude.
    It queues tasks, starts research (it queues a research task, see RES-2) and, when you ask for a change made
    directly in your checkout, edits it there. See *What a topic may run* below.

    Older topics keep their kind. An **edit my checkout** (project chat) or **research** topic from before still
    opens and continues as that kind, and the divider and **Earlier topics** still show its mode word. A topic never
    changes kind. If the kept state names a new topic of another kind (a mode switch from before), the view resets it
    to plan & design.

    The backend modes stay (`MODES`, `conv_open()` with `how: "mode"`, the topic handoff a switch carries); the
    Today composer no longer offers a switch.
  - **Which topic a message goes to** (`conv_open()` in opsd.py):
    1. Opening a to-do whose source is a chat (Read, Agree, …) makes that chat the current topic.
    2. Opening a to-do whose source is a task (Try, Review, Fix, Answer, Promote for a release, a report's Read) or
       a project item (Promote, Ship, Commit) opens that thing's own plan & design topic. Its subject is
       `task:<id>`, or `project:<name>:promote|ship|commit`. If an open topic has that subject, the latest one
       becomes current. Otherwise nothing is made yet: your first message creates the topic, titled `#<id> <task
       title>` or the item's title (`Promote <project>`), cut to 80 characters. Its first turn carries the item's
       context to Claude as a note (for a task: state, gate result, question or error, files changed, brief; for a
       project item: its why, and for Ship the changes on main), with a line `About task #231` in the chat. An
       ended topic is not reused: the next message starts a new one. No starter text goes in the message box.
       The **Answer** step still sends the message box to the waiting worker, as before.
    3. **New topic** starts a fresh plan & design topic (`how: "new"`, `mode: "design"`): your next message opens
       it. The line under the button says so.
    4. Otherwise a message continues the current topic.
    5. **A message sent from a to-do step always carries that to-do.** The send itself (`POST
       /api/conversation/say` with `todo: {key, verb, title, why, subject?, pick?}`) says which to-do step was
       shown, so it doesn't depend on the conversation state set when the step opened. With a subject (`task:<id>`
       for a task-sourced to-do), a message that would start a new topic goes to the item's own topic instead: its
       open topic, else a new one with that subject and its note, as in rule 2. That covers ending the item's
       topic and sending again from the same step, a step seen before in this page session, a reload, and a
       project filter change. `pick` is set when the page never got the item's topic picked (the open failed or is
       still on its way); then the message goes to the item's topic over any current topic. Clicking **New topic**
       is kept: that message starts a plain new topic. Whatever topic the message lands in gets a line **Sent from
       to-do todo:20: …** once, with the to-do's title and why as a note for Claude, so even a to-do with no
       subject is never sent with no context. A chat's to-do (it opens its chat), a step of another project than
       the conversation's, and a message sent with no step shown carry nothing, as before.

    Picking a topic in Earlier topics, **New topic**, or another item replaces a subject still waiting for its first
    message. The subject is stored in `designs.subject` and kept in `conv:<project>` while pending. API:
    `POST /api/conversation` with `{project, how, ref?, mode?}`, `how` one of `chat`, `topic`, `subject` (`ref` is
    the subject key), `task` (the current plan & design topic, for the research runs panel), `new`, `mode`.

    To queue a task, ask Claude. The Tasks page's own **Queue task** form still queues one without Claude.
  - **What a topic may run.** Plan & design and edit my checkout both run any shell command you could at the CLI,
    past the guard (`guard_settings()`). Plan & design's prompt (`design_prompt.md`) sets its limits:
    - By default it reads `dev`, writes only its documents folder and, for an OpenSpec change, `openspec/`, and
      queues tasks for code.
    - When you ask for research, it runs `opsd.py add -p <project> --research T0|T1|T2 "question"`, which queues a
      research task (RES-2), and tells you the task id.
    - Only when you explicitly ask for a change made directly in your checkout does it edit files there and run
      commands in it. It may then use Edit and Write on the checkout (`Edit(/<repo>/**)`, with `--add-dir <repo>`);
      an OpenSpec change's chat doesn't get this. It doesn't commit unless you ask.

    An older edit my checkout topic is plain Claude in your checkout, as before, and can queue a task in its own
    project with `opsd.py send` (`--after <task id>` makes it wait until that task merges).
  - **Titles.** A new topic is titled from its first message, as chats are. Claude may retitle it when the
    subject clearly changes, with a fenced block in the language `topic`: `{"title": "..."}`. The design and project
    chat prompts say so (`design_prompt.md`, `PROJECT_CHAT_PROMPT`). The block is taken out of the reply, the
    divider shows the new title, and Claude's next message says it took (`topic_from_reply()`).
  - **Queued messages.** While Claude replies, **Send** reads **Queue** (see
    [Which chats need you](#which-chats-need-you)). A research topic doesn't queue: Send waits for its reply.
  - **Read state.** Today never marks a chat read by itself: **Mark read** does. The item stays on the list, no
    longer new, until you end the topic. A task's own chat with its worker is in its details panel (**Details**), not here.
- **TODAY-3 Right: timeline.** Read-only status in three groups. Anything that needs a decision is in To-do, not
  here.
  - **Working now**: running, checking and merging tasks, and research runs.
  - **Scheduled**: queued and paused tasks, with why each waits, each project's promote train, and each recurring
    job (`schedules` in `/api/state`) with its next run, or paused.
  - **Done today**: tasks that finished as done since local midnight.

  Click an item for its details, in a panel over the column. A task shows a stage bar (Queued → Working → Gate →
  Review → Merged → Promoted, the current stage lit), then the task page from History (model, effort, brief, live
  output, log). The train shows the tasks it carries and **Promote now**. A schedule shows its last runs (its tasks in
  `/api/state`), with **Run now**, **Pause** or **Resume**, and **Edit**, which opens it on the Schedules page.
  Research runs in the research repo (legacy) link to the Research page; research tasks are tasks.
  Every panel has **Ask Claude about this**. A task, the promote train and a schedule open their own topic, as their
  to-dos do (TODAY-2 rule 2; subjects `task:<id>`, `project:<name>:promote`, `schedule:<id>`). The research runs
  panel puts `About the research runs: ` in the message box of the current plan & design topic.
- **TODAY-4 Centre: a project's sections.** Over the conversation, the project in view has four tabs:
  **Conversation · Specs & designs · Research · Tasks**. Conversation is TODAY-2. The tab you pick is kept in this
  browser.
  - **Specs & designs.** The project's open OpenSpec changes, with done/total, and its drafts (see
    [OpenSpec changes](#openspec-changes)); a repo without `openspec/` shows its **Set up** line.
    Click a change to open its chat as the current topic; **Read the change** in the conversation head opens its
    reader in the side drawer. Below them, the documents its plan & design topics wrote:
    the files in `~/.claude-ops/designs/<topic id>/` of the project's design rows, newest first, each opening from
    `/api/designs/<id>/files/<name>` in a new tab (`GET /api/projects/<name>/documents`). Three buttons:
    **New change** (for this project), **New prototype**, which starts a plan & design topic with
    `Make a one-file HTML prototype of: ` in the message box, and **Refresh commands**, which runs `openspec update`
    in each repo of the project that uses OpenSpec and commits the opsx commands and skills that changed to `dev`.
  - **Research.** The reports in `docs/research/*.md` on the project's `dev` (each repo's, in a workspace), newest
    first: the title from the first heading, the date of the last commit that touched it, and a one-line summary from
    the first paragraph, with ● while it is unread. Click one to read it in the side drawer; that doesn't mark it read: its Read step's **Mark read** does (RES-5).
    Under them, the research runs under way and the project's recurring research jobs (schedules whose task is
    research). **New research** asks for a question, a tier (T0, T1, T2) and an optional repeat, and queues a
    research task (see [Research](#research)). `GET /api/projects/<name>/research` has all of it.
  - **Tasks.** The project's tasks, the History list filtered to it, newest first. Click one for its page.
- **TODAY-5 One poll.** The page reads `/api/state` every 3 seconds, as before. Other reads are for what you
  opened: the selected task (`GET /api/tasks/<id>`, as History does), the conversation shown, and the list of
  chats once when an item has no chat of its own. `GET /api/tasks/<id>` gains `chat_from`, the design chat that
  queued the task or its parent. The conversation reads `GET /api/conversation?project=<name>` with each poll,
  and the current topic (`GET /api/designs/<id>`, or `/api/research/threads/<id>`).
- **Layout.** Desktop, 1100px and wider, has the three columns. Narrower windows stack them. Below 700px the
  phone layout below takes over. On desktop, drag the border between the centre and right columns to resize the
  right column: from 240px up to 50% of the view, and never so wide that the centre drops below 420px. This
  browser keeps the width (`ops-td-right` in localStorage); double-click the border to reset it to 320px. The
  handle is hidden when the columns stack, and the saved width is ignored there.


### Quick chat

- **TODAY-6 Quick chat.** A floating window for a quick talk with any project that leaves the page under it as it
  was. **Ask** in the header (next to **More ▾**), or **⌘K** (**Ctrl+K** off macOS) on any page, even while you
  type in another box, opens it.
  - **Window.** It floats over the page, under dialogs, about 420×520px at the bottom right. There is no backdrop:
    the page stays clickable. Drag it by its title bar; resize it from its bottom-left or bottom-right corner (at
    least 320×300). It stays inside the window, also after the browser window shrinks. Its place and size are kept
    in this browser (localStorage).
  - **Title bar.** The project picker (every project that isn't archived, without All projects; it starts on
    the switcher's project when one is ticked, else on the focused one if it is in view, else the first in view),
    the topic's title once it has one, **–** (minimise) and **×** (close). The picker and title belong to the
    active tab.
  - **Tabs.** A strip under the title bar holds up to 6 chats, each its own topic in any project. A tab shows its
    project's title, a dot when Claude has replied since you last looked at it, and a small **×** that closes
    just that tab. **+** at the end adds a tab and makes it active; its project starts as a new chat's does (the
    picker's starting project above). With 6 open, **+** is disabled and its tooltip says why. **⌘⇧K**
    (**Ctrl+Shift+K** off macOS) opens the window with a new tab (or just opens it when 6 are open); **⌘K** opens
    it on the tab you used last. Each tab keeps its own unsent draft and queued messages while you switch.
    The active tab's topic is polled as before; the others every 6 seconds, enough for their dots.
  - **Body.** The topic's messages, drawn as in the Today conversation (markdown, links, pictures, the reply as it
    streams in), and a message box. Enter sends and Shift+Enter adds a line, as in every chat box; while Claude
    replies, **Send** reads **Queue**. Drop or paste screenshots to attach them.
  - **Fresh each time.** The first message starts a new plan & design topic in the picked project
    (`POST /api/designs` with `quick: true`, which also marks it in `designs.quick`, 0 or 1); later ones go to that
    topic (`/api/designs/<id>/say`). It never continues Today's current topic; to go back to an earlier quick chat,
    pick it from **Recent**. Each tab starts fresh: until its first message, the picker only changes where it will
    start; after it, that tab's picker is locked. To talk to another project, add a tab with **+**.
  - **Recent.** A tab with no topic yet lists, under its "A quick chat with …" line, the latest quick-chat topics
    (`GET /api/designs/quick?limit=10`: `designs.quick` set, in projects that aren't archived, newest activity first,
    ended ones too, each `{id, project, title, updated, ended}`). A row shows the project's title, the topic's title,
    how long ago it was last active ("2.0h ago") and "· ended" when it has ended. Topics open in another tab are left
    out; with none to list, nothing more shows. The list is fetched each time an empty tab is drawn (opening the
    window, **+**, ⌘⇧K, switching to it); it isn't polled. Click a row to load that topic into the tab, as after a
    first message: the tab takes its project, the picker locks, and its messages show. An ended one is reopened
    first (`/api/designs/<id>/reopen` with the `ended` it was listed with): when that is no longer the stored
    value, the reopen is refused, the error shows, and the list is fetched again. Only topics started here are
    listed: other topics are reached from Today.
  - **Minimise.** **–**, or Esc while focus is in the window, shrinks it to a pill at the bottom right with the
    project's name (with one tab) or `N chats` (with more), and a dot once Claude has replied in any tab since you
    last looked at it. Click the pill to get the window back on the same tab. Open or minimised, the chats stay
    across pages and reloads: `ops-qc` in localStorage keeps `{tabs: [{project, did, seen}], active, min}` (the
    older one-chat `{project, did, min, seen}` is read as one tab).
  - **Close.** **×** in a tab closes that tab; closing the last tab closes the window, and closing the active one
    moves to its right-hand neighbour (or left, when it was last). **×** in the title bar closes the window and
    every tab: the next open starts fresh. Closing ends the topic of every tab it closes, as **End topic** does
    (see "Ending a chat"): it moves under **Ended**, its replies count as read, its queued messages are dropped,
    and a reply in progress is stopped first. Nothing is deleted; **Recent** in an empty tab, or **Reopen** under Ended, puts it back. A tab with no
    topic yet (no message sent) just closes. The tab closes even when ending fails (no network, the topic is
    gone), and the error shows nowhere. Minimise and Esc never end anything. A tab whose topic was deleted
    elsewhere closes without ending.
  - **Today never changes.** Opening, using or closing the quick chat, in any tab, leaves the header filter, the selected
    to-do and its step, and every project's shown topic alone. A project whose `conv:<project>` keeps no topic
    shows its latest open topic, which a new quick chat would become. So before it creates the topic, the daemon
    pins the topic Today shows now (or "a new topic" when there is none) in `conv:<project>` when nothing is kept
    there yet. A message to a quick topic (`designs.quick`) and its reopen pin the same way first, so reopening
    one from **Recent** doesn't make it the shown topic either. (Pinning was simpler than leaving quick-chat topics
    out of that fallback.)

### Today on a phone

Below 700px Today is phone-first. The rules:

- **PHONE-1 One column at a time.** A bottom bar switches between **To-do** (with a red count badge), **Now** (the
  step and its conversation) and **Activity** (the timeline). The page opens on To-do. Tapping a to-do opens it in
  Now. The Projects section is at the top of the To-do pane, above the list, and folds as on a computer; tapping a project opens Now on its home
  (compact: a title row with **‹ To-do**, and the buttons in one row that scrolls sideways). Task details, and the other timeline panels, open full screen.
- **HDR-1 Header.** One row, about 40px: the name, the project switcher and **More**. The switcher takes the
  room left on the row, cuts its label short to fit, and opens the same checklist as on a computer. The Today button, the queue
  readouts and the pause buttons are left out on Today; the other pages still show them. The usage-limit bar
  still shows when the limit is hit.
- **HDR-2 Step card.** Compact by default, about 100px: one top row with **‹ To-do**, **Step k of n · project**,
  **Details ▾** and **Skip →** (and **Unpark** for a parked item, **Unskip** for a skipped one); then the title; then one row of action buttons
  that scrolls sideways, ending in the note field and **Wait for…** (TODO-6). The why and the facts open under **Details ▾**.
  A Try step's proof video sits under the title at full width; the card scrolls to its buttons (HDR-4).
  The title is cut at two lines; **Details ▾** shows it whole, and a title over about 50 characters brings **Details ▾**
  even when there is no why or facts.
- **Sending a message.** In the Today composer, the Design and Chat tabs' message box, and a task page's ask or
  steer box, **Enter** sends, **Shift+Enter** or **Alt/Option+Enter** adds a line, and **⌘/Ctrl+Enter** also sends.
  Enter never sends while an input method (IME) is composing. On a touch screen **Enter** adds a line and you send
  with the **↑** or **Send** button. With the `/` list open, Enter picks the command. The new-task brief, the
  research question and the deploy confirm still send on **⌘/Ctrl+Enter**.
- **HDR-3 Composer.** One row: attach (📎), the message box (it grows to at most 4 lines) and an **↑** send
  button. **New topic**, the line that says where a message goes, and the project picker sit above it. The lines
  over the messages (where a chat runs and its resume command, **Started by**, **Read the change**, a design's
  **Documents**) wrap rather than being cut off; past three lines they scroll.
- **HDR-4 Room to talk.** While a step is open the conversation gets at least 60% of the screen height; the step
  card scrolls inside what is left. A long conversation never pushes the composer off the screen.
- **PHONE-2 Back.** **‹ To-do** returns to the list, and so do **To-do** in the bottom bar and the phone's back
  gesture: opening a step or a project home, or a details panel, adds a history entry, and going back closes one level (the panel, then
  the step). Switching between Now and Activity adds no more entries, so one back from either reaches To-do. This
  works whatever the address's hash was when Today opened (`#today`, `#design`, `#chat` or none), and a reload
  starts on To-do with nothing left to go back through. The step card's top row, with **‹ To-do**, stays in view
  however far the card scrolls or the conversation grows.
- **PHONE-3 Fits the screen.** The page is sized to the visible viewport (`100dvh`, falling back to `100vh`), so
  the bottom bar is never under the browser's toolbar, and it keeps clear of the notch and home indicator
  (safe-area insets). The page itself never scrolls: in the Now pane only the message list does, and it
  doesn't drag the page or set off pull-to-refresh. Nothing scrolls sideways except the step's action row; tab rows that could overflow hide
  their scrollbar and still scroll sideways.
- **PHONE-4 Quick chat.** The quick chat (TODAY-6) opens full screen, with no drag or resize. Minimise and
  close work as on a desktop, except where a minimised one waits: on Today the pill would cover the **↑** send
  button, so the header's **Ask** turns blue instead (with an orange dot when Claude replied), and tapping it
  brings the chat back. The other pages keep the pill. An empty tab's **Recent** list is the same, full width.
- **PHONE-5 Earlier topics and Ended.** They sit where they do on a computer, at the top of the conversation in
  the **Now** pane, for the project of the conversation shown. To reach another project's, or any when the list is
  empty, use **Earlier topics and Ended** at the bottom of the **To-do** pane: one chip per project the header
  filter shows. A chip opens Now on that project's conversation with Earlier topics unfolded; open **Ended** there
  and tap **Reopen**, or tap the topic to open it. With a to-do open, the step stays and only the conversation
  moves (as Ask Claude about this moves it); tapping that to-do in the list brings back its own conversation. The
  chips are phone-only: a computer shows the conversation beside the list.
- **PHONE-6 Parity.** A phone's Today can do everything a computer's can, except what HDR-1 leaves out on purpose
  (the queue readouts and pause buttons). Anything new on Today must say how it works below 700px: where it sits,
  and that nothing hides it (a `display: none`, a one-line cut-off, or the bottom bar, the message box or the
  keyboard over it). Sideways scrolling is only for the step's action row and the chip rows.

## Chat mode

A project chat is plain Claude in the project folder, as if you ran `claude` there. You meet it as **edit my
checkout** in the Today view's conversation (see [TODAY-2](#today-view)). It works in your
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
  prompt are denied, and Claude tells you what it couldn't do. claude-ops adds its own auto-mode rules for the
  project, as workers get (see **Auto-mode rules** under **Workers**): the project's checkout is the trusted repo.
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

Research is ordinary queue work in the project it is about. A report lives in that project's repo at
`docs/research/<slug>.md`, and gets there the way code does: a worktree, the gate and a merge into `dev`.

- **RES-1 A research run is a task.** It is a normal task (kind `task`) flagged research: the `research` column
  holds `{question, tier, slug, path}` as JSON. So it queues, runs, gates, merges, holds, retries and promotes like
  any task, and every list shows it. A flag, not a kind of its own, because every rule that tests `kind` (merges,
  the promote train, Try and Review, steering) should treat it as a task. `/api/state` shows `research` on each task
  (null on others).
- **RES-2 Asking for one.** **New research** in the project's Research section (TODAY-4), or
  `POST /api/projects/<name>/research` with `{question, tier, every?, hold?}`, or from the CLI
  `opsd.py add -p NAME --research T0|T1|T2 "question"` (what a plan & design topic runs when you ask it for research). `tier` is `T0`, `T1` (the default) or
  `T2`. The slug is made from the question's words, unique among the reports on `dev` and the project's open research
  tasks. The task is titled `Research: <question>`. With `every` (any [schedule](#recurring-jobs) form) it also makes
  a recurring research job and queues the first run now, as the schedule's last task. It answers
  `{id, slug, path, schedule}`.
- **RES-3 The brief.** The task's body tells the worker to answer the question at its tier with the research skill
  in project mode, and to write the report to `docs/research/<slug>.md` in its worktree (the first repo's, in a
  workspace). It says so in so many words, because the owner's `/research` skill writes to `~/dev/research` by
  default: not there, and not `briefs/`, `reports/`, `topics/` or `watchlist.md`. The report starts with a `# `
  heading and a one-paragraph answer (the Research section's title and summary), then the findings with a source for
  each claim and a `## Sources` list. The worker commits that one file and touches nothing else.
- **RES-4 Time and size follow the tier.** T0 5, T1 15, T2 40 minutes per worker turn: a research task's worker is
  stopped at its tier's limit and the task fails with `stopped at the T1 time limit (15 min)`, so Retry is one click.
  Its size cap is `--max-budget-usd` 1, 3 or 10 (API-equivalent dollars, as the old runs used).
- **RES-5 Read when it merges.** A research task that is done and merged, and whose report you haven't marked read,
  is a **Read** to-do (`task:<id>:report`, with `report: {path}`); so is each unread report a task listed (RES-10).
  Opening the report marks nothing, from any view: the step's **Open report**, the Research section, or a link to
  the file in Details or a chat (`GET /api/files` only serves it). Open report leaves the step on your list. Only
  **Mark read**, the step's primary button, marks it: every task of the project that wrote that path, as its
  research report or in its `reports`; it stores `read` in the task's `research`, or on that entry of `reports`.
  Mark read takes the step off your list at once, before the server answers, and an `/api/state` answer from before
  the mark can't bring it back. It posts `POST /api/projects/<name>/research/read` with `{path}` (trimmed); it answers
  `marked` (the ids it marked) and `already` (ids read before), counting research tasks and tasks that listed the
  path in `reports` alike. When both are empty no task of the project wrote that path:
  the step comes back with "Found no research report at <path> to mark read", as does any error. Research tasks
  from before this have no flag, so nothing old shows up.
- **RES-6 Recurring research.** A schedule whose `research` column holds a tier is a recurring research job. Each run
  builds a fresh brief from its body (the question) with a dated slug, `<slug>-<YYYY-MM-DD>`, so runs don't overwrite
  each other. `opsd.py schedule add -p NAME --every 'day 07:00' --research T1 "question"` makes one; `POST
  /api/schedules` takes `research` too. The Schedules page lists them with the rest.
- **RES-7 Research with no code home.** Stocks, business ideas and the daily scout belong to the research repo
  itself. Make it an ordinary claude-ops project once, by hand (claude-ops never adds it for you):

  ```bash
  ./opsd.py add-project ~/dev/research research    # then review ~/.claude-ops/projects/research.toml and reload
  ```

  Its reports then go to its own `docs/research/` the same way. The daily scout becomes a schedule there:
  `./opsd.py schedule add -p research --every 'day 07:00' --research T1 "Daily scout: new leads in my interest areas"`.
- **RES-8 (retired).** The research repo had its own to-do list (`dashboard/todos.json`, `todos.py`,
  `bin/todo` and a todo skill), shown as Do items in Today and in a To-dos tab under Research. Since 2026-10-23
  there is one to-do system: the claude-ops to-dos ([To-dos](#to-dos)). claude-ops no longer reads or writes
  the research repo's list, and research turns on without it. Don't reuse the id.
- **RES-9 The Research tab is legacy.** It stays under More, read-only in spirit: for the history of the research
  repo's proposals, reports and runs. Old reports are not moved. New research goes through the queue.
- **RES-10 Any task can list reports.** Scheduled jobs with their own briefs (a daily scout, a nightly bug bash)
  write reports outside `docs/research/` and are not research-flagged, so RES-5 alone would merge them quietly. A
  `done` worker result may carry `"reports": ["path", ...]`: repo-relative paths of files the task committed for you
  to read. The queue keeps entries that are non-empty strings, relative after `os.path.normpath`, with no `..`
  segment, at most 5, and logs an event naming what it dropped. A result of any other status ignores `reports`; a
  later round's `done` replaces the list. They are stored in the task's `reports` column as JSON `[{path, read}]` and
  shown as `reports` on each task in `/api/state` (null when none). A research task's own report path is skipped
  there, so the file never shows twice. Once the task is done and merged, each unread entry is a **Read** to-do
  (`task:<id>:report:<path>`, title the task's, why `Report <path> merged into <dev>`, `report: {path}`), with the
  same Open report and Mark read as RES-5: only Mark read marks it read. Paths in a workspace are relative to the
  task's worktree, as RES-5's are.

### Legacy: research runs in the research repo

Before RES-1, research ran outside the queue, in the research repo, and that path still works for the Research tab.
claude-ops also serves the research dashboard's data and runs, which used to need their own server
(`~/dev/research/dashboard/server.py`, port 8787). The code is `research.py`; `opsd.py` only routes to it.
The API is `/api/research/...`, with the old server's endpoints and response shapes: `proposals`, `projects`
(with `/<id>/activity`, `/<id>/notes`, `/<id>/delete`), `assign`, `archive`, `references`,
`jobs` (and `/<id>`, `/<id>/stop`), `threads` (`?target=` and `/<id>`), `file?path=`, `slug?q=`, `dispatch`
and `chat`. `file` serves only `.md` files inside the research repo.

- **Setting.** `research_repo` (Settings) is the research repo's path. A path is refused unless it has
  `dashboard/projects.json` and `briefs/`. Research is off while it is empty: the
  research API answers 404 `{error: "research is off"}` and `/api/state` has `research: null`.
- **Same files, same places.** Nothing is migrated. It reads and writes what the old server did:
  `dashboard/projects.json`, `dashboard/archived.json`, `projects/<id>.md` (project
  notes), `logs/dashboard/jobs.json`, `logs/dashboard/threads.json`, `logs/dashboard/<job>.jsonl`, `briefs/`,
  `reports/` and `sources/registry.md`. Every state JSON file is written atomically (a temp file in the same
  folder, fsync, `os.replace`). The repo's old to-do list is not touched (RES-8 is retired).
- **Runs and chats are as before.** No worktree, gate or merge: `claude -p` in the research repo with the
  old flags, tools (minus `bin/todo`), prompts, run sizes ($1/$3/$10) and time limits (T0 5, T1 15, T2 40, chat 15 minutes), in
  their own process group.
- **Research chats add to-dos.** A chat is told the design chats' to-do rule and block format (`todo_rule()`),
  naming the claude-ops project its to-dos go to: the one its research project links to (`repo`), else the one
  whose checkout is the research repo. With neither it is told it can't add to-dos. When a turn ends, its reply's
  ```` ```todo ```` blocks go through `todo_add` into the claude-ops `todos` table ([To-dos](#to-dos)), so a Do
  shows in Today. It happens once per turn: new chat jobs carry `todosTaken: false` in `jobs.json`, set to true
  when taken (on the next read of the chat or watchdog pass, and only when claude-ops may write), so a restart
  adds nothing twice and turns from before this are never read. The blocks are hidden from the reply you see;
  in their place a line says what happened (`added to-do todo:12`, or that nothing was added because no claude-ops
  project fits). That note (`todoNote`) is also put before your next message in that chat, so Claude learns the
  ids. `CONTEXT_VERSION` 5 tells older threads the new rule once.
- **Single writer.** The old server guards its files with an in-process lock, so two writers would lose
  updates. Before every write, and before each watchdog pass, claude-ops tries port 8787
  (`OPS_RESEARCH_OLD_PORT` overrides it). If anything answers, the write gets 409 `{error: "The old research
  dashboard is running. Stop it to make changes here.", readonly: true}` and the watchdog skips its pass (the
  old server's watchdog covers it). Reads always work. `/api/state` shows `research.readonly`.
- **Backup.** Before the first write after each daemon start, the state files (`projects.json`,
  `archived.json`, `jobs.json`, `threads.json` and the notes in `projects/`) are copied to
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

### The Research tab (legacy)

The dashboard's **Research** tab replaces the old page. It shows only while `research_repo` is set (Settings,
under Research). It has the old page's views in claude-ops' look: proposals grouped by research project, each
with its report, brief, chat and run log; the projects themselves (create, edit, delete, notes, activity); runs
(a new question or an existing brief, with tier, size, model, project and notes), each with its live feed and a
Stop button; and the source registry. Reports render with the dashboard's own markdown, and a link to a
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
        ... --after 333                       # a task waits in that queue until task #333 (any project) merges
```

- **It shows like everything else.** A sent task is queued in the other project and goes through its gate as
  usual. A sent chat is a **Read** to-do in the Today view once it has replied. Both say **Sent by** (or
  **Started by**) with a link to the task or chat that sent them, and the lists tag them `sent by …`. A link to a
  chat opens it as the current topic in Today, and adds its project to the project filter when it is out of
  view.
- **Context comes along.** The message starts with a line naming the sender, its title and where it works (its
  worktree and branch, or the folder a chat runs in), so the other session can read its files. The sender is told
  to make the message stand alone, like a design chat's task brief.
- **Word comes back.** The sender's task logs "Sent task #71 to diskgame", and later how it ended: done with its
  summary, failed, cancelled, or waiting on you. A sent chat's first reply goes back too. In a chat that sent
  something these show as notes, and Claude reads them with your next message.
- **Waiting on a task.** `--after <task id>` (`333` or `#333`) gives the new task `depends_on`, the same wait as
  `after_task` in a queue block, so the scheduler holds it until that task merges. The id may be in any project.
  It is for tasks only: a chat can't wait on a task, and an unknown id is refused. A chat runs only when you write
  to it, so it is told never to promise to send something later ("once #333 merges, I'll send…"): it sends now
  with `--after`, or adds a to-do with a `wait` when the step needs you.
- **Limits.** A design chat stays read-only: it can send a task or a design chat, not a project chat. Work passed
  on three times (A sends to B, which sends on, and so on) is refused, and so is an eleventh send from one task or
  chat: both stop two projects keeping each other busy. Workers are told to send only what their task needs and
  never to edit another project's files themselves. Chats send only when you ask or agree.
- The daemon gives each session `OPS_FROM` (`task:12` or `chat:3`) and `OPS_DAEMON` (its own URL), which `send`
  reads. Run from a terminal without them, it still reaches the live daemon; the task then says it came from
  outside the queue.

## Recurring jobs

A schedule queues an ordinary task on a timetable: "every Monday at 09:00, check the dependencies". The task
gets a worktree, the gate and a merge like one you queued by hand, and says **Sent by** its schedule.

```bash
./opsd.py schedule add -p myrepo --every 'mon 09:00' "Check dependencies"   # also --body, --hold, --model, --effort
./opsd.py schedule edit 3 --body-file brief.md   # change #3: only what you give (--title, --every, --body, ...)
```

- **`every`** takes one of these, in local time:
  - `day HH:MM`: every day.
  - `weekday HH:MM`: Monday to Friday.
  - `mon HH:MM`: one day of the week (`mon` … `sun`), or a list such as `mon,thu HH:MM`.
  - `hour`: every hour, on the hour.

  Anything else is refused with a 400 that lists these forms. Times are local wall-clock times, so `09:00` stays
  09:00 across a daylight-saving change.
- **When it runs.** The scheduler checks schedules on every pass (every 2 s). When a schedule is due and not paused,
  it queues a task with the schedule's title, body, model, effort, hold and priority, and moves `next_at` to the
  next slot.
- **No pile-up.** When the task it queued last is still open (queued, running, gating, merging, in review, waiting on
  you, or paused), that slot is skipped: the open task's log says so, and `next_at` moves on.
- **Missed slots.** If the daemon was down over one or more slots, the schedule runs once when it starts again,
  never once per missed slot.
- **Run now** queues a task at once and leaves `next_at` alone. It queues even when the last task is still open:
  you asked for it.
- **Pause** stops it queueing; **Resume** sets `next_at` to the next slot from now, so a long pause doesn't run at
  once.
- **Where to see it.** The **Schedules** page (`#schedules`, linked from Settings) lists each schedule with its next
  run and its last task, with Run now, Pause/Resume, Edit (title, body, every) and Delete.
- **From a chat.** A design or project chat can create one when you ask for something that repeats, with a fenced
  `schedule` block holding a JSON list of `{project, title, body, every, hold?, model?, effort?}`. The daemon
  creates them and strips the block from the reply, as it does a `queue` block; the next message tells Claude
  what it made, or why one was refused.
- **Editing from a session.** The guard keeps every session the queue starts off the dashboard's API (it pauses
  and cancels tasks), so a chat or worker changes a schedule with `opsd.py schedule edit ID`, never `curl`.
  `--body-file PATH` (or `-` for stdin) takes a long body; fields you leave out keep their values, and `next_at`
  moves only when `--every` changes. The design brief names the command, and the guard's refusal points to it.

API: `/api/state` has `schedules` (each with `next_at`, `paused`, `last_task` and `last_state`).
`POST /api/schedules` creates one; `POST /api/schedules/<id>` edits it (any of project, title, body, every,
hold, priority, model, effort); `POST /api/schedules/<id>/run`, `/pause`, `/resume` and `/delete` do the rest.

A recurring research job is a schedule with `research` set: see RES-6. The research repo's old daily job becomes one once the research repo is a project (RES-7).

For one task that should start later, not a timetable, see [Start later](#start-later).

## Start later

A task can wait in the queue until a time you choose: "start this tomorrow at 09:00", or "start this after the next
usage-limit reset", so a big task gets a fresh 5-hour window. The task's `not_before` (epoch seconds) holds the time.

```bash
./opsd.py add -p myrepo --at "tomorrow 09:00" "Upgrade the build"
./opsd.py add -p myrepo --after-reset "Rewrite the parser"
./opsd.py add -p myrepo --research T1 --after-reset "Compare the options"   # a research run too
./opsd.py send myrepo --at "fri 18:00" "Run the long benchmark"   # tasks only
```

- **At a time.** Local wall-clock time, in one of these forms:
  - `2026-10-26 09:00` or `2026-10-26T09:00`: that date and time.
  - `HH:MM`: today, or tomorrow when that time has passed.
  - `<day> HH:MM`, with day `mon` … `sun`, `today` or `tomorrow`: the next such day. Today's slot counts only
    while it is still ahead (`today 09:00` at 10:00 is refused).

  A time in the past, or one it can't read, is refused with a 400 that lists the forms. A past time is refused so
  that a typo can't start the task at once.
- **After the next reset** (`reset`, or `--after-reset`). The time is fixed **when you queue the task**: the
  window's `resetsAt` plus `LIMIT_GRACE` (60 s), the same minute the queue waits after a limit. The window, in this
  order:
  1. the limit the queue is waiting out (`limit_info()`), if there is one;
  2. else the window holding the usage brake (`usage_info()["braked"]`), if braked;
  3. else the live 5-hour reading.

  With no live reading at all it is refused: "no usage reading yet: give a time instead". The task's log says what
  it waits for, such as `waits for the 5-hour reset: starts after 15:01`.
- **Once due, it is an ordinary queued task.** The scheduler skips a queued task while now is before `not_before`.
  After that it takes its normal place in the queue (priority, then id): it never jumps ahead of tasks that were
  waiting before it. The brake, taper, pace cap, `after`, the project's `max_parallel` and a project pause all apply
  as usual.
- **Where to see it.** The queue row and the task page say "waits until Tue 09:00" (or "waits for the 5-hour
  reset · 15:01") with a **Start now** button. Start now (`POST /api/tasks/<id>/start-now`) clears `not_before`,
  and logs it, so the task starts in its normal place.
- **From the dashboard.** The New task form's **Start** control: now, at… (a date and time), or after next reset.
- **From a chat.** A `queue` block item may carry `"not_before"`: a time as above, `"reset"`, or an epoch number. A
  value the daemon can't use is reported back in the next message, and that task queues to start now.
- **Why not a one-off schedule?** A schedule makes no task until its time comes. A task that starts later is in the
  queue from the moment you add it: you can see it, queue other tasks `after` it, and start it early.
- You can't change the time once it is queued, apart from Start now.

API: `POST /api/tasks` takes `not_before`: a time string as above, `"reset"`, or a number (epoch seconds).
`/api/state` task rows carry `not_before`.

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
  workspace's own files in its folder yourself, or make the folder a repo (below).
- **Merging.** The queue rebases every repo's task branch onto that repo's `dev` and runs the gate from the
  worktree root. It merges each repo the task committed to; a repo with no commits is left alone.
- **Promote.** A release snapshots `dev` in every repo and checks them together. Each checkout is handled
  as in a plain project, uncommitted changes included. `main` moves in all repos or in none.
- Commits show as one id per repo, such as `ballup-dunk 1a2b3c4d, ballup-swish 5e6f7a8b`.

### The workspace folder as a repo (`"."`)

Without it, a task can never change the workspace's own files (`CLAUDE.md`, `docs/`, `.claude/`, `bin/`,
`.gitignore`, `openspec/`): they are copies. To let tasks change them, make the workspace folder a git repo of
its own and list it as `"."`: `repos = ["ballup-dunk", "ballup-swish", "."]`.

- **Set it up** with `./opsd.py adopt-workspace-root -p NAME`. In your checkout it runs `git init` in the folder
  if needed (on `main_branch`), adds `/<repo>/` to `.gitignore` for each repo folder not yet ignored, commits the
  folder's files as `chore: track workspace root`, makes `dev` from `main`, adds `"."` to the end of `repos` in
  the toml (the rest of the file, comments included, stays as it was) and reloads projects. Running it again does
  only what is still missing. It refuses while the folder is a repo with uncommitted changes.
- **The rule.** The root repo must gitignore every other repo folder. If it doesn't, or `"."` is listed but the
  folder is not a git repo, the project doesn't load: the daemon log and the project's card say why.
- **What changes.** Each worktree root is a git worktree of the root repo (on the task branch, `dev`, or
  detached), with the other repos' worktrees in the folders it ignores. Nothing is copied any more. The root is
  one more repo everywhere else: it is rebased, gated, merged into its `dev` only when the task committed to it,
  promoted (or sent straight to main) with the others, checked out for deploys, and included in Send to dev and
  in carrying your uncommitted changes over. Its commit shows under the workspace's folder name, such as
  `ballup-monorepo 1a2b3c4d, ballup-dunk 5e6f7a8b`. The worker brief says the root is a repo and edits to its
  files must be committed there.
- **Old records.** Commits are recorded in `repos` order, which is why `"."` goes last: releases and deploys
  recorded before it still match their repos. A deploy target with `from` asks you to deploy its source
  target once more first, as after any change to `repos`.
- **Without `"."`** nothing changes.

## Config in the repo

A project's gates, deploy targets and the rest can live in its own repo, so a task can change them, with review
and history like any other change. Put `claude-ops.toml` at the root of the project's repo: the repo itself for a
plain project, the root repo (`"."` in `repos`) for a workspace. A workspace without `"."` can't have one.

- **What goes where.** The home toml (`~/.claude-ops/projects/<name>.toml`) keeps identity and machine
  settings: `title`, `repo`, `main_branch`, `dev_branch`, `worktrees`, `max_parallel`, `direct_main`,
  `chat_mcp` and `[worker] env` (and `port_base`). The repo file may hold `repos`, `[worktree]` (`clone`, `setup`,
  `setup_timeout`), `[[gate]]`, `[app]`, `[[deploy]]`, `[worker] notes` and `[design] notes`. A home-only key in
  the repo file is ignored with a warning, on the project's card and in the daemon log.
- **Which wins.** A key in the repo file replaces the home toml's whole: two `[[gate]]` lists are not merged,
  and a `[worktree]` table replaces the home one. A key the repo file leaves out comes from the home toml.
  `repos` in the repo file must list `"."`.
- **Read from main.** The queue reads the file as it is on `main_branch` (`git show main:claude-ops.toml`), never
  dev. A task's change to its gates takes effect only once it reaches main, so a task can't loosen the gate it is
  judged by. The daemon checks every 10 seconds and reloads when main's copy changes, and on Reload.
- **A broken file.** If the file is not valid TOML, or a key has the wrong type, the last good config stays in
  use (after a restart with no last good one, the home toml alone). The error shows on the project's card and in
  the daemon log. Fix it on main and it loads by itself.
- **A repo not cloned yet.** If `repos` in the file names a folder your checkout lacks (a task added
  `ballup-web`), the card says "ballup-web/ is not cloned", the last good config stays in use, and the queue
  holds that project's queued tasks. Clone it into the workspace folder yourself (nothing clones it for you);
  the daemon sees it within 10 seconds and goes on.
- **Workers know.** A project with the file gets a section in each worker brief saying where it is, that a
  change takes effect on main, and which keys stay in the home toml.
- **Move a project over** with `./opsd.py export-config -p NAME`. It writes the home toml's repo keys into
  `claude-ops.toml` in your checkout, uncommitted, and prints what it wrote. It doesn't change the home toml:
  the queue goes on reading those keys there until you commit the file and it reaches main. Then you may delete
  them from the home toml. It refuses if the file is there already, or for a workspace without `"."`.
- **Without the file** nothing changes.

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

Each key is marked **home** (only here, in the home toml) or **repo** (here, or in `claude-ops.toml` in the
project's repo, where it wins: see Config in the repo).

```toml
# --- home: identity and machine settings
title = "My Game"
repo = "/Users/you/dev/mygame"
main_branch = "main"
dev_branch = "dev"                 # created from main if missing; add-project picks ops-dev if dev exists
worktrees = "/Users/you/dev/mygame-wt"   # default: <repo>-wt
# repos = ["backend", "app"]       # a workspace: repo is a folder and these folders in it are the git repos
# repos = ["backend", "app", "."]  # "." too: the folder is a repo of its own that ignores the others (see Workspaces)
max_parallel = 0                   # per-project cap; 0 = only the global "Parallel" limit
# direct_main = false              # default true: a merge moves main at once, no release (see Straight to main)
chat_mcp = []                      # MCP servers a project chat loads, by name (see Chat mode); default none

# --- repo: the rest may also live in claude-ops.toml (repos too; it must list "." there)
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
launch = "open -n 'Builds/macOS/MyGame.app'"   # run from the worktree for Play / Try and the proof video
process = "Builds/macOS/MyGame.app"            # the app's executable or its folder, inside the worktree
artifact = "Builds/macOS/MyGame.app"           # must exist after build; kept as a backup on promote
# running = "scripts/app-running.sh"   # optional, server-style apps: exit 0 = the app runs from this worktree
# stop = "scripts/app-stop.sh"         # optional: stops the app if it runs from this worktree

[worker]
env = { GAME_PORT = "{port}" }     # home: extra env; {port} is the worker's own free port ($OPS_PORT)
notes = """Project-specific guidance added to every worker brief."""   # repo

[[deploy]]                         # zero or more; see Deploys. Runs only when you click Deploy
name = "store test"
run = "bin/release test"           # in <worktrees>/deploy (or its repo folder); gets OPS_DEPLOY, OPS_COMMIT, ...
# repo = "app"  from = "other target"  note = "What's new"
# version = "cat VERSION"  release_tag = "v{version}"  build_tags = "build/*/{version}+*"

[design]
notes = """Project-specific guidance for design chats."""
```

## Worktree cleanup

Each task works in `<worktrees>/t<id>`. When does it go?

- **Done:** removed when the task merges.
- **Failed or cancelled:** kept for 7 days (`SPENT_KEEP_DAYS`), because **Retry** resumes the worker in the same
  worktree, uncommitted work and all. The task's log says `worktree kept 7 days for a retry, then removed`. The
  branch stays after removal, so a later retry makes a fresh worktree from it.
- **Removal is checked.** `remove_worktree` runs `git worktree remove --force` in each repo, then deletes whatever
  is left (read-only and locked files too), then prunes. If the folder is still there, the task's log says
  `couldn't remove worktree …` and the janitor tries again.

**The janitor** (`sweep_worktrees`) runs at startup and every 10 minutes, in the background. In each project it
removes any folder under `<worktrees>/` that nothing needs. It keeps:

- `dev`, `main`, `deploy` and `deploy-peek`
- the worktree of every task that is not done and not failed or cancelled more than 7 days ago, and of any task
  whose pipeline or chat is running
- `spec-<id>` of each spec chat
- an `openspec-*` or `*.root-tmp` folder less than an hour old

It skips a folder that holds uncommitted changes (an untracked `.ops-result.json` doesn't count), or whose
`.ops-result.json` has status `needs_input`. It logs each skip once, in the daemon log and on the task. It logs
each removal the same way (`janitor removed worktree …`). A half-removed folder (no `.git`, files still there)
has nothing to keep, so it goes.

The janitor also removes Xcode's build cache of a worktree that is gone (`sweep_derived_data`). Xcode keeps one
`~/Library/Developer/Xcode/DerivedData/<name>-<hash>` per workspace path, about 3.5 GB for ballup's iOS app, and
nothing uses it once that worktree is removed. It only removes an entry whose `info.plist` `WorkspacePath` is in a
folder under a project's `<worktrees>/` that no longer exists. For what a worktree really costs on disk, see
[docs/worktree-disk-cost.md](docs/worktree-disk-cost.md).

## Other details

- **Tabs.** The header has **Today** and **More ▾** (see [Today view](#today-view)). More holds History (the
  Tasks page), Projects, Research, Analyze, Settings and Schedules, and **+ Project**. `#today`, `#tasks`,
  `#projects`, `#analyze`, `#settings` and `#schedules` open one; the old `#queue` opens Tasks, and the old
  `#design` and `#chat` open Today (the address changes to `#today`). The address bar follows the page you
  are on, so a reload stays there. With no page in the address, the dashboard opens on Today.
- **Header.** Only what you watch or act on: the tabs, the queue readouts (working, queued, needs you,
  paused), the usage meters, and the pause buttons. Hover a readout or meter for what it counts; the
  dashboard draws that tooltip itself, so it shows at once and survives the 3-second redraw.
- **Usage meters and pace.** The 5h and 7d chips each have a bar filled to the window's percent used. A thin
  vertical line in the bar marks the pace: where usage would be now if it were spread evenly over the window
  (pace % = time passed / window length, 5 hours or 7 days, worked out in the browser from the reset time and
  redrawn at least once a minute; for the 5-hour window with the usage brake on, times the brake instead of 100). The fill's colour says how usage compares with the pace: **red** when it is
  ahead, **yellow** when it is up to 10 points below (`PACE_WARN` in `dashboard.html`), **green** when it is
  further below. The tooltip adds `Pace 50% · 10 points ahead` (or `behind`, or `on pace` within 1 point). A
  window that has reset shows no line, the default fill and no pace line in the tooltip. The brake and limit
  colours still show on the chip itself: amber border, background and percent at or above the usage brake, red
  while the queue waits out the usage limit (see Usage limits); the pace colour drives only the fill.
- **Task ids.** Every task title carries its queue id as **#316**, muted, in front of the title: the History
  list, a project's task list, Today's timeline and Tasks panel, a To-do item about a task (in the list and on its
  step card), a task's detail page, and the task chips under a chat message. A parked item's "Waiting on #283"
  names its ids the same way. Click an id to copy `#316` (it says "copied" for a moment); the click stops there,
  so it never also opens the card or ticks a box. A stored to-do (one with `todo`, key `todo:12`) shows its own id
  instead, **todo:12**, the same way in its list row and its step card's title, on a phone too; a click copies
  `todo:12`, to paste into a chat. Items with no stored to-do (Promote, Ship, Commit, Read) show no id.
  `tid()` in `dashboard.html` draws every one, so the format stays the same; `data-copyid` holds the exact text a
  click copies. `tests/test_task_ids.py` checks it.
- **Settings tab.** Parallel, Promote wait, Usage brake, Taper from, Model, Effort and the model per kind of work, each
  with what it does written next to it, and **Spent**: what Claude has cost, priced at API rates.
- **Analyze tab.** Run times and costs of the tasks created in the last day, 7 days, 30 days or all time, for
  the projects ticked in the header or every project (`GET /api/analyze?period=1|7|30|all&project=<name>[,<name>…]`,
  a comma-separated list; empty means every project). The scope line reads `· A, B only`, and the per-project
  table shows when 2 or more projects are in scope.
  Medians, p90s and totals; spend per day; per project; by kind, model, effort, gate-fix rounds, Claude turns and
  outcome; what the fix rounds fixed; the dearest and slowest tasks; and **How to improve**: plain rules over the
  same data (fix rounds, Opus on releases, turns that started after the 1-hour cache expired, failed and cancelled
  spend, tiny tasks, queue waits, high effort), each with a rough saving. Run time leaves out the time a task
  waited on your answer or review, a pause or the usage limit. The rules' constants come from
  `docs/usage-analysis.md`.
- **Tasks tab.** New task and the task list are on the left; the task page is on the right. The project
  switcher in the header filters the list; with two or more ticked, each row shows its project. Clicking a task
  opens it on the right and focuses its project, and new tasks default to that project (when it is in view).
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
  `runs/play-<project>/build.log`). Each step finishes before the next starts: clone and setup, then build,
  then launch. When setup or build is about to run and the app already runs there, Play stops it first
  (`[app] stop`, or SIGTERM) and the launch starts it fresh, so no server keeps serving a tree that setup
  or build rewrote under it. A launch (Play or a task's Try) is refused while setup still runs in that
  worktree, or after its setup failed. The card shows which one runs ("running: main", "running: dev"),
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
  is the usage brake (setting `usage_brake`, default 85; see Usage limits), and "Taper from" the usage taper that
  starts fewer workers as the 5-hour window fills (setting `usage_taper`, default 50). "Promote wait" is the promote train's
  longest wait in minutes (setting `release_wait`, default 180; see [Promote after merge](#main-is-the-golden-version)).
- **Models.** Each kind of work picks its Claude model. "Model" on the Settings tab is the global setting (`model`);
  **Per kind of work** below it sets `model:task` (task workers and Send to dev), `model:release` (the workers a release
  starts to merge main into dev, default `sonnet`), `model:design` (design and OpenSpec chats) and `model:chat`
  (edit my checkout topics). Each is an alias (`opus`, `sonnet`, `haiku`), a full model id, or empty for the global setting,
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
- **Retrying.** **Retry** on a failed or cancelled task (its page, or Today's Fix step) queues it again
  (`POST /api/tasks/<id>/retry`); a worker with a session is told why its last attempt stopped and to finish.
  **Retry with a note…** adds `note`, which the worker reads and the log shows (`retried: <note>`).
  **Retry after…** retries it so it waits for another task to merge first, say one building the fix it needs. Its
  chooser lists the open tasks (queued, working, checking, merging, review, needs you) of every project, newest
  first, each with its id, project and title, then a box for any other task id (`333` or `#333`) and the same
  optional note. It sends `after` (an int, or a string with or without `#`), which sets the task's `depends_on`,
  the same wait as the New task form's **After** and `opsd.py send --after`: the card shows `after #N` and the log
  `retried after #N` (then `: <note>`). The new `after` replaces any earlier one; a retry without `after` leaves
  `depends_on` as it was. It is refused, and the task left as it was, for an unknown task (`no task #N`), the task
  itself (`a task can't wait on itself`), or a task whose own `depends_on` chain leads back to it
  (`#N already waits on this task`). A task already done holds nothing: the retry starts as usual. One that later
  fails or is cancelled keeps the retried task waiting, as any `depends_on` does.
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
  - **Usage taper.** Below the brake, the queue starts fewer workers as the 5-hour window fills, so it reaches
    the brake more slowly. The setting `usage_taper` is a percent: default 50, 0 turns the taper off. Set it as
    **Taper from** on the Settings tab (`POST /api/settings {"usage_taper": 50}`, clamped to 0..100). Let pct be the
    5-hour window's exact percent, and let end be `usage_brake` (or 100 with the brake off). When the taper is on,
    pct is above it and end is above it, the taper cap is
    `max(1, max_parallel - ceil((pct - usage_taper) * (max_parallel - 1) / (end - usage_taper)))`, so the cap
    steps evenly from `max_parallel` at Taper from down to 1 just under the brake. Otherwise there is no taper cap:
    with the taper off, at or below Taper from, or with the brake at or below Taper from (the taper has no room).
    With `max_parallel` 6, taper 50 and brake 85 (steps of 7 points):

    | 5-hour usage | workers that start Claude |
    |---|---|
    | 0–50% | 6 |
    | 50.x–57% | 5 |
    | 57.x–64% | 4 |
    | 64.x–71% | 3 |
    | 71.x–78% | 2 |
    | 78.x–84.x% | 1 |
    | 85%+ | 0 (the brake) |

    With `max_parallel` 3, taper 50 and brake 85: 50.x–67.5% → 2, above 67.5% → 1. With the brake off and
    `max_parallel` 6, the steps are 10 points each, ending at 100. With `max_parallel` 1 the cap is always 1, so
    nothing changes. The rules:
    - It follows the **5-hour window only**; the 7-day window keeps only the brake. A stale reading counts as 0.
    - It limits only **new starts of tasks that start a Claude session** (the same ones the brake holds). The
      running Claude workers are the running tasks that are not a deploy or release; a Claude-starting task starts
      only while there are fewer of them than the cap. Deploys, releases that only gate and tasks resuming past
      `claude` start as before, up to `max_parallel`. Running work is never stopped, chats are never held, and a
      project's own `max_parallel` still applies.
    - The brake wins: at or above `usage_brake` nothing that starts Claude starts. With the brake off (0) the
      taper still works on its own, spread up to 100%, never below 1.
    - No notification: only the brake notifies. A task held only by the taper says
      `Usage at 63% of the 5-hour window · 4 of 6 workers`, and while the cap is below `max_parallel` the header
      meter's hover adds `Tapering: 4 of 6 workers`. While tapering the header's working chip also shows the cap as
      its denominator (`3/4 working`), and its hover names the Parallel setting it lowers.
    - **Pace slowdown.** While the taper is on, the queue also slows whenever 5-hour usage runs ahead of the header
      meter's pace line. With a live reading, elapsed = the share of the 5-hour window that has passed (0 to 1),
      pace = elapsed × end (end = `usage_brake`, or 100 with the brake off, the same pace the meter shows) and
      lead = pct − pace. When lead > 0 the pace cap is `max(1, max_parallel - ceil(lead / 5))`: one worker fewer
      per 5 points ahead (`PACE_STEP` in opsd.py). On or behind pace, or with no live reading, there is no pace cap
      and the taper alone applies. The lower of the taper cap and the pace cap applies, to the same new starts as the
      taper, and the brake still wins. It is off when the taper is off, and it follows the **5-hour window only**.
      Example: `max_parallel` 6, brake 85, taper 50, one hour into the window: pace = 0.2 × 85 = 17. At 30% usage
      the lead is 13, so the pace cap is 6 − 3 = 3; there is no taper cap at 30%, so 3 workers run. At 10% usage
      the queue is behind pace, so all 6 run. When the pace cap is the lower one, a held task says
      `Usage at 30% of the 5-hour window, 13 points ahead of pace · 3 of 6 workers`, the meter's hover says
      `Tapering: 3 of 6 workers (ahead of pace)`, and the working chip's hover names the pace.
  - **The header meter** shows both windows, `5h 62% · 7d 14%`, with their reset times on hover. It turns amber at
    or above the brake, and red while the queue waits out the usage limit itself. Its pace line (see **Usage meters
    and pace**) runs up to the brake for the 5-hour window, so it says whether usage is on track to reach the brake,
    not 100%, when the window resets: at brake 85, halfway through the window the pace is 42.5%. With the brake off
    the 5-hour line runs up to 100%, and the 7-day line always does.
- **Restarts.** Whenever the daemon stops (`./opsctl restart` or `stop`, a promote of claude-ops, launchd, a
  `kill`), it pauses every running task the same way, lets a merge finish, and exits. The next daemon resumes
  those tasks on its own, each in the same Claude session (or a fresh one after a long wait: see **Fresh
  sessions**); tasks you paused stay paused, and so do tasks the usage limit paused, until it resets. Nothing about a restart is saved that could hold the
  queue: if the daemon crashes instead, the next one requeues whatever was running.
- **Questions.** A worker that needs a decision stops with a question. Answer it on the dashboard
  and the same Claude session resumes, or a fresh one if you answer after the cache expired. The answer
  arrives quoted under the question (see **Answers carry the question**).
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
- **Screenshots.** Drop or paste an image into any message box: the Today conversation, Ask the worker, an answer,
  feedback, or a new task. It is saved in `~/.claude-ops/images/` and an `[image: <path>]` line goes in the
  text. The message shows it, and Claude (started with `--add-dir` on that folder) is told to read it.
  PNG, JPEG, GIF or WebP, up to 20 MB.
- **Pictures in replies.** A reply's `![alt](path)` shows the picture. A path into `~/.claude-ops/images/`
  or a design chat's own folder is served by the dashboard; a picture anywhere else on disk can't be, so it
  shows a "Picture not found" note with its path. Chats are told to copy a picture into one of those first.
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
  **Screen Recording** for Claude Ops (System Settings → Privacy & Security → Screen & System Audio Recording)
  is needed for a native app's [proof video](#proof-video). The daemon only checks it and never asks, so grant it
  there yourself; without it those videos are skipped.
- **Workers** run `claude -p --permission-mode auto --permission-prompts none --strict-mcp-config`.
  Anything that would prompt is denied. By default they work only in their worktree: they don't push, and
  don't touch your checkout, dev or main. A step outside it (your checkout, `~/.claude-ops`, a commit on main)
  is allowed when the brief asks for it or you approved it in an answer.
- **Auto-mode rules.** The `--settings` that workers and project chats get (from `guard_settings`) carry an
  `autoMode` object. Claude Code appends its lists to your own `autoMode` lists in `~/.claude/settings.json`;
  it replaces nothing and removes no default rule. `claude --settings '<json>' auto-mode config` shows the merge.
  - `environment`: this session is a claude-ops worker (or chat) for project `<name>`. The trusted repos are
    your checkout, every repo of its workspace, its worktrees folder, and their origin remotes. `main` and `dev`
    are ordinary branches there, not protected. `~/.claude-ops` is the queue's own state and config, owned by you.
  - `allow`: (a) editing, committing and pushing in a trusted repo on any branch, `main` included, when the
    brief or you ask for it (a plain push, not a force push); (b) editing `claude-ops.toml` in a project repo and
    `~/.claude-ops/projects/<name>.toml`, which is project config, not Self-Modification; (c) running `opsd.py`
    subcommands (`export-config`, `adopt-workspace-root`, `add`, `send` …); (d) your answer to a worker's
    question counts as your explicit approval of the action that question named.
  - No `soft_deny` entries are added. Hard denials (force-pushing over others' work, leaking secrets) stay.
- **Answers carry the question.** Your answer reaches the worker under the question it answers: "You asked
  the owner: > <question> The owner answered: <answer>", then a line saying that if you agreed, that is your
  approval for the action it asked about, steps outside its worktree included. A bare "Do it" then names its
  action and target, which the auto-mode classifier needs before it allows a step such as a commit in your
  checkout.
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
| `spec_new_prompt.md` | added instead while a chat from **New change** has no change yet |
| `dashboard.html` | the page |
| `~/.claude-ops/projects/*.toml` | one config per project |
| `~/.claude-ops/ops.db` | SQLite: tasks, events, settings, design chats |
| `~/.claude-ops/runs/<id>/` | `claude.jsonl` (full transcript), `claude.err`, `gate.log`; a deploy's `deploy.log`, `note.txt` |
| `~/.claude-ops/images/` | screenshots attached to messages |
| `~/.claude-ops/opsd.log` | daemon log; each line starts with its local time |
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
