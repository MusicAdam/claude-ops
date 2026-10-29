#!/usr/bin/env python3
"""claude-ops: a task queue for your git projects, worked by parallel headless Claude sessions.

  opsd.py serve                        run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add-project PATH [NAME]      detect PATH and write ~/.claude-ops/projects/NAME.toml
  opsd.py adopt-workspace-root -p NAME make workspace NAME's own folder a git repo its tasks commit to ("." in repos)
  opsd.py export-config -p NAME        write NAME's gates, deploys, app and the rest into claude-ops.toml in its repo
  opsd.py add -p NAME [--model M] [--effort E] "text..."   queue a task through the running daemon; --model opus,
                                       sonnet, haiku or a full model id (else the setting model:task, then model);
                                       --effort low, medium, high, xhigh or max (else the setting effort);
                                       --research T0|T1|T2 queues a research run whose question is the text;
                                       --branch NAME or --jira KEY: a branch task (README: Branches and pull requests)
  opsd.py pr open|merge TASK_ID        a branch task: push and open its PRs / merge them in pr_order
  opsd.py schedule add -p NAME --every 'mon 09:00' [--body TEXT] [--hold] [--model M] [--effort E] "title..."
                                       a recurring job: queue that task on that timetable (README: Recurring jobs);
                                       --research T0|T1|T2 makes it a research job whose question is the text
  opsd.py schedule edit ID [--title T] [--every E] [--body TEXT | --body-file PATH|-] [--hold|--no-hold] ...
                                       change schedule ID: only the fields given, the rest stay as they are;
                                       --after-schedule ID|none (add or edit): its task waits on schedule ID's task
  opsd.py deploy -p NAME TARGET [--version same|patch|minor|major|X.Y.Z] [--note TEXT]   deploy a [[deploy]] target
  opsd.py send NAME [--design|--chat] [--title T] [--hold] [--model M] [--effort E] [--branch B | --jira K] "text..."
                                       from a session the queue started: a task (or chat) in another project
  opsd.py pause-all | resume-all       pause every running task (it stops now, resumable) / resume every paused one
  opsd.py restart                      restart the claude-ops service now, detached, as Settings' Restart button does:
                                       how a project chat (the daemon's child) restarts it without stopping itself
  opsd.py archive-project NAME         take a project off the queue: its config moves to projects/archived/
  opsd.py unarchive-project NAME       bring an archived project back, as it was
  opsd.py event TASK_ID TEXT           add a line to a task's log, without the daemon (opsctl, when a restart fails)
  opsd.py chat show ID [--last K]      print any project's chat, from the db: a header, then its messages in full
  opsd.py guard --daemon PID [--root DIR]   PreToolUse hook: refuse Bash commands that stop processes the session did not start

Per project (paths come from ~/.claude-ops/projects/<name>.toml):
  <repo>                 your checkout of the main branch. The queue only fast-forwards it, when a release passes.
  <worktrees>/dev        integration worktree on the dev branch. Finished tasks merge here. The queue owns it: at
                         merge time, uncommitted changes to tracked files there are saved as a patch in the task's
                         run folder (runs/<id>/dev-uncommitted-<repo>.patch) and discarded. Untracked files stay.
  <worktrees>/main       build of main, the golden version (detached). Changes only when a release passes.
  <worktrees>/t<id>      one worktree per task, branch task/<id>-<slug> (a release: detached at its dev snapshot;
                         a branch task: its named branch).
  <worktrees>/deploy     where deploy targets run (detached). Reset for each deploy but never cleaned.

A workspace (`repos = [...]` in its config) is a folder that holds several git repos, such as a backend and an
app. Each of the paths above is then a folder with a copy of the workspace's own files and one worktree per repo,
and every repo has its own main, dev and task branches. With "." in repos the workspace folder is a repo too: each
path above is then a worktree of it, with the other repos' worktrees in folders it gitignores, and nothing is copied.
"""
from contextlib import nullcontext
import base64, ctypes, datetime, filecmp, fnmatch, json, math, os, plistlib, re, secrets, shlex, shutil, signal, sqlite3, subprocess, sys, threading, time, tomllib
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import research  # the research dashboard's backend (README: ## Research); it calls back into this module as ops
research.bind(sys.modules[__name__])

HOME = Path.home()
CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("OPS_DATA", HOME / ".claude-ops"))
PROJECTS_DIR = DATA / "projects"
RUNS = DATA / "runs"
PIDFILE = DATA / "opsd.pid"  # stop a daemon with kill $(cat "$OPS_DATA/opsd.pid"), never pkill -f
IMAGES = DATA / "images"  # screenshots dropped or pasted into a message box
CHAT_FILES = DATA / "designs"  # <design id>/: documents a design chat writes, such as a design doc or prototype
DB = DATA / "ops.db"
HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8788"))
DAEMON_URL = f"http://{'127.0.0.1' if HOST in ('', '0.0.0.0', '::') else HOST}:{PORT}"  # for `opsd.py send`
CLAUDE = os.environ.get("OPS_CLAUDE") or shutil.which("claude") or str(HOME / ".local/bin/claude")
# The skills and commands workers and chats use (README: ### The plugin), loaded with --plugin-dir. Their names are
# namespaced by the plugin, /claude-ops:research and /claude-ops:openspec:<cmd>, so no user copy can shadow them.
PLUGIN = CODE / "plugin"
PLUGIN_ARGS = ["--plugin-dir", str(PLUGIN)]
MAX_FIX_ROUNDS = 3  # times a worker is sent back after a conflict or failed gate
# A worker's next turn starts a fresh session, with a handoff note, instead of resuming one whose
# prompt cache has expired (it lasts an hour) or whose context makes every call dear. Measured on the owner's own usage: a cold turn costs about
# $0.80 and a fresh worker start-up about $0.53, so a handoff note is cheaper.
# A chat's next turn does the same above FRESH_ABOVE (see chat_stale). Every call rereads the whole context, so cache
# reads are most of the tokens used: a 90k limit about halves the dearest calls for the price of a ~10k-token note.
FRESH_AFTER = 55 * 60  # seconds since the session's last call
FRESH_ABOVE = 90_000  # tokens of context at its last call
HANDOFF_CHARS = 40_000  # about 10k tokens of handoff note
RESULT_FILE = ".ops-result.json"

ACTIVE = ("running", "gating", "merging")
STOPPED = ("cancelled", "paused")  # set by you; the pipeline stops at its next step and never overrides them
ENDED = ("done", "failed", "cancelled")
MERGES = ("task", "senddev")  # kinds that go through the gate into dev: they can promote after merge
RELEASE_LOCK = threading.Lock()  # one release queued at a time, when tasks finish together
TRAIN = {}  # project -> {"ids", "by"}: merged tasks waiting for the promote train (auto_release), for the dashboard
IN_MAIN = set()  # ids of merged tasks main contains: main only moves forward, so auto_release checks each once
DBL = threading.RLock()
PROCS = {}  # task id -> current Popen (claude or a gate step), for cancel
STEPS = {}  # Popen -> cwd of every run_step command running now, so a shutdown or a setup in its folder can stop it
SETUP_TIMEOUT = 15  # minutes a [worktree] setup command may run; a project's setup_timeout overrides it
STUCK_AFTER = 15 * 60  # seconds a worker may stay silent with no command running before it counts as stuck
STOPPING = threading.Event()  # this daemon is shutting down: start nothing new. Never stored, so it can't outlive us
THREADS = {}  # task id -> pipeline thread
PHASE_SINCE = {}  # task id -> when its phase last changed, so the dashboard can say how long a step has run


# ---------------------------------------------------------------- projects

HAS = set()  # (repo, sha) of commits known to be in that repo
WORKTREE_LOCKS = {}  # repo -> lock: two tasks starting at once add their worktrees one after the other
GIT_LOCKED = re.compile(r"\.lock\b|cannot lock|could not lock|another git process", re.I)
GIT_CHATTER = re.compile(r"^(Preparing worktree|HEAD is now at|Updating files)")  # stderr before the real error


def worktree_lock(repo):
    return WORKTREE_LOCKS.setdefault(str(repo), threading.Lock())  # setdefault is atomic: one lock per repo


class Part:
    """One git repo of a project: the project itself, or one folder of a workspace. A workspace whose repos list "."
    has a root repo too: the workspace folder itself, with the other repos in folders it gitignores."""

    def __init__(self, p, sub):
        self.sub = sub
        self.root = sub == "."
        self.src = p.repo / sub if sub and not self.root else p.repo  # your checkout of it
        self.name = p.repo.name if self.root else sub  # for people: a workspace's root repo goes by its folder name
        self.prefix = f"{sub}/" if sub and not self.root else ""  # its files' paths in a project worktree

    def at(self, root):
        """This repo's worktree inside a project worktree."""
        return Path(root) / self.prefix if self.prefix else Path(root)

    def present(self, root):
        """Whether its worktree inside a project worktree exists. A root repo's folder exists before its worktree
        does (the other repos are in it), so for it, the worktree's .git file is what counts."""
        return (Path(root) / ".git").exists() if self.root else self.at(root).exists()

    def add_worktree(self, root, *args):
        """git worktree add args... for this repo inside project worktree root."""
        return self.add_worktree_at(self.at(root), *args)

    def add_worktree_at(self, path, *args):
        """git worktree add args... at path, its last arg the commit. A workspace's root repo goes where the folder
        may already hold the other repos' worktrees (a worktree made before the root was a repo): git won't add into
        a folder that has files, so it adds the worktree beside it with no files, moves its .git in and checks the
        files out there."""
        path = Path(path)
        if not self.root or not path.is_dir() or not any(path.iterdir()):
            return self.git("worktree", "add", *args[:-1], str(path), args[-1])
        tmp = path.with_name(path.name + ".root-tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        self.git("worktree", "add", "--no-checkout", *args[:-1], str(tmp), args[-1])
        (tmp / ".git").rename(path / ".git")
        tmp.rmdir()
        self.git("worktree", "repair", str(path))
        self.git("reset", "-q", "--hard", cwd=path)  # its files replace the copies the folder held

    def git(self, *a, cwd=None, check=True):
        if a[:1] != ("worktree",) or a[1:2] == ("list",):
            return sh("git", *a, cwd=cwd or self.src, check=check)
        # worktree add/remove/prune write the repo's shared refs and worktree files: one at a time per repo, and
        # once more after a second if a git of yours or a worker's held a lock
        with worktree_lock(self.src):
            for retry in (True, False):
                try:
                    return sh("git", *a, cwd=cwd or self.src, check=check)
                except RuntimeError as e:
                    if not (retry and GIT_LOCKED.search(str(e))):
                        raise
                    time.sleep(1)

    def rev(self, ref):
        return self.git("rev-parse", ref)

    def has(self, sha):
        if (self.src, sha) in HAS:
            return True
        ok = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.src,
                            capture_output=True).returncode == 0
        if ok:  # a commit stays in its repo; one that is missing may still arrive, so only a yes is kept
            HAS.add((self.src, sha))
        return ok


class Project:
    def __init__(self, path, c=None):
        """c: the config to use, the home toml merged with the repo file (see load_project); else the home toml."""
        c = tomllib.loads(path.read_text()) if c is None else c
        self.name = path.stem
        self.title = c.get("title", self.name)
        self.repo = Path(c["repo"]).expanduser()
        self.main = c.get("main_branch", "main")
        self.dev = c.get("dev_branch", "dev")
        self.wt = Path(c.get("worktrees", f"{self.repo}-wt")).expanduser()
        self.repos = [r.strip("/") or "." for r in c.get("repos", [])]  # a workspace: git repos in folders of repo
        if "." in self.repos:  # a root repo: the workspace folder itself (revs keep repos order, so it stays put)
            check_root_repo(self.repo, [r for r in self.repos if r != "."], self.main)
        self.parts = [Part(self, r) for r in self.repos] or [Part(self, "")]
        self.max_parallel = int(c.get("max_parallel", 0))  # 0: only the global limit applies
        w = c.get("worktree", {})
        self.clone = w.get("clone", [])
        self.setup = w.get("setup", [])
        self.setup_timeout = float(w.get("setup_timeout", SETUP_TIMEOUT))  # minutes, per command
        self.gate = c.get("gate", [])
        app = c.get("app", {})
        self.build = app.get("build")
        self.launch = app.get("launch")
        self.process = app.get("process")
        self.artifact = app.get("artifact")
        self.running_cmd = app.get("running")  # a server-style app: exit 0 = it runs from the worktree (cwd)
        self.stop_cmd = app.get("stop")  # and this stops it there; both replace the process-table match
        wk = c.get("worker", {})
        self.env = wk.get("env", {})
        self.port_base = int(wk.get("port_base", 47900))
        self.worker_notes = wk.get("notes", "").strip()
        self.design_notes = c.get("design", {}).get("notes", "").strip()
        self.chat_mcp = [str(n) for n in c.get("chat_mcp", [])]  # MCP servers a task chat loads (see mcp_servers)
        self.dev_wt = self.wt / "dev"
        self.main_wt = self.wt / "main"
        self.deploy_wt = self.wt / "deploy"  # persistent: build output survives from one deploy to the next
        self.deploys = deploy_targets(self, c.get("deploy", []))
        # direct_main (the default; `direct_main = false` opts out): a task's merge goes on to move main at once (see
        # land), with no release. [[deploy]] targets keep it: a deploy and a merge moving main take turns
        # (exclusive_blocker), and commits a deploy makes on main reach dev before the next gate (catch_up_dev).
        # flow "pr" (default "dev"): every task is a branch task that ends in a PR, so nothing merges into dev and
        # nothing waits for Promote; dev only follows main (pr_main). Straight to main is off with it.
        self.flow = "pr" if c.get("flow") == "pr" else "dev"
        self.direct_main = bool(c.get("direct_main", True)) and self.flow != "pr"
        # branch tasks (README: Branches and pull requests): a Jira key's branch name, the order a workspace's PRs
        # open and merge in, and how Merge merges one
        self.branch_pattern = str(c.get("branch_pattern") or BRANCH_PATTERN)
        dbp = str(c.get("default_branch_pattern") or "")  # flow "pr": the branch of a task given none
        self.default_branch_pattern = dbp if "{id}" in dbp else DEFAULT_BRANCH_PATTERN
        self.pr_order = [str(x).strip("/") or "." for x in c.get("pr_order", []) if isinstance(x, str)]
        self.pr_merge = c.get("pr_merge") if c.get("pr_merge") in PR_MERGES else "squash"
        self.config_blob = ""  # claude-ops.toml's blob on main, "" with none (see load_project)
        self.config_error = None  # why the repo file on main is not the config in use
        self.config_warnings = []  # keys the repo file sets that only the home toml may
        self.missing = []  # repos folders the repo file lists that your checkout lacks: the queue holds new tasks
        self.lock = threading.Lock()  # merges into dev and promotes
        self.main_lock = threading.Lock()  # building main and moving it: a release and direct merges take turns
        self.checkout_lock = threading.Lock()  # moving main in your checkout, and clearing what Send to dev landed

    def target(self, name):
        t = next((t for t in self.deploys if t.name == name), None)
        if not t:
            raise ValueError(f"{self.name} has no deploy target {name!r}")
        return t

    def revs(self, ref):
        """ref's commit in each repo, space-separated in repos order: one sha for a plain project."""
        return " ".join(r.rev(ref) for r in self.parts)

    def pairs(self, revs):
        """Pair each repo with its sha in a revs string."""
        return zip(self.parts, revs.split())

    def show(self, revs, n=8):
        """A revs string for people: "abc12345", or "backend abc12345, app def67890" for a workspace."""
        return ", ".join(f"{r.name} {sha[:n]}" if r.sub else sha[:n] for r, sha in self.pairs(revs or ""))

    def label(self, shas, n=8):
        """Like show, for a list that may hold only some repos' shas, as a task's merged does."""
        shas = (shas or "").split()
        if not self.repos or len(shas) == len(self.parts):
            return self.show(" ".join(shas), n)
        found = [(r, self.mine(r, " ".join(shas))) for r in self.parts]
        known = {s for _, s in found if s}
        return ", ".join([f"{r.name} {s[:n]}" for r, s in found if s] + [s[:n] for s in shas if s not in known])

    def mine(self, r, shas):
        """The sha in a space-separated list that is a commit of repo r, or None."""
        return next((s for s in (shas or "").split() if r.has(s)), None)

    def root_first(self, items=None):
        """parts (or (part, x) pairs) with a root repo first, the order to make worktrees in: the others' go
        inside its worktree. Reversed, the order to remove them."""
        return sorted(self.parts if items is None else items,
                      key=lambda x: not (x[0] if isinstance(x, tuple) else x).root)

    def port_for(self, tid):
        return self.port_base + tid % 90


def check_root_repo(repo, subs, main):
    """Raise ValueError unless repo (a workspace folder) is a git repo of its own with a main branch that gitignores
    each of subs (its other repos' folders): a root repo that tracked them would hold them twice."""
    top = sh("git", "rev-parse", "--show-toplevel", cwd=repo, check=False) if repo.is_dir() else ""
    if not top or Path(top).resolve() != repo.resolve():
        raise ValueError(f'repos lists ".", but {repo} is not a git repository: run '
                         f"opsd.py adopt-workspace-root to make it one")
    if not sh("git", "rev-parse", "--verify", "-q", f"refs/heads/{main}", cwd=repo, check=False):
        raise ValueError(f'repos lists ".", but {repo} has no {main} branch')
    missing = [s for s in subs if subprocess.run(["git", "check-ignore", "-q", f"{s}/"], cwd=repo).returncode != 0]
    if missing:
        raise ValueError(f'repos lists ".", but {repo}/.gitignore does not ignore '
                         + ", ".join(f"{s}/" for s in missing) + ": add " + " ".join(f"/{s}/" for s in missing)
                         + " to it and commit it")


class Target:
    """One [[deploy]] target: a command the daemon runs only when you click Deploy. error is why its config can't
    be deployed, or None."""

    def __init__(self, c):
        c = c if isinstance(c, dict) else {}
        val = lambda k: str(c.get(k) or "").strip()
        self.name, self.run, self.repo, self.from_, self.note = (val(k) for k in ("name", "run", "repo", "from", "note"))
        self.repo = self.repo.strip("/")
        self.version, self.release_tag, self.build_tags = val("version"), val("release_tag"), val("build_tags")
        self.error = None

    def parts(self, p):
        """The repos this target deploys: its repo, or all of them."""
        return [r for r in p.parts if r.sub == self.repo] if self.repo else p.parts

    def folder(self, root):
        """Where its commands run: its repo's folder in a project worktree, or the worktree root."""
        return Path(root) / self.repo if self.repo else Path(root)


def deploy_targets(p, items):
    """The [[deploy]] targets, each with error set if its config is wrong (it then can't be deployed)."""
    out, errs = [], {}
    for k, c in enumerate(items if isinstance(items, list) else [], 1):
        t = Target(c)
        e = errs[id(t)] = []
        if not t.name:
            t.name = f"[[deploy]] #{k}"
            e.append("it has no name")
        elif any(o.name == t.name for o in out):
            e.append(f"another target is also named {t.name!r}")
        if not t.run:
            e.append("it has no run command")
        if t.repo and t.repo not in p.repos:
            e.append(f"repo {t.repo!r} is not one of repos" if p.repos else "repo is only for a workspace")
        for key in ("release_tag", "build_tags"):
            v = getattr(t, key)
            if v and not t.version:
                e.append(f"{key} needs version")
            elif v and "{version}" not in v:
                e.append(f"{key} has no {{version}}")
        out.append(t)
    names = {t.name for t in out}
    for t in out:
        if t.from_ and t.from_ not in names:
            errs[id(t)].append(f"from {t.from_!r} names no target")
        seen, cur = {t.name}, t
        while cur and cur.from_:  # a chain that comes back round can never deploy: each waits for the next
            if cur.from_ in seen:
                errs[id(t)].append(f"its from chain loops back to {cur.from_!r}")
                break
            seen.add(cur.from_)
            cur = next((o for o in out if o.name == cur.from_), None)
        t.error = "; ".join(errs[id(t)]) or None
    return out


# ---------------------------------------------------------------- config in the repo (README: ## Config in the repo)
# A project's repo (a workspace's root repo, "." in repos) may hold claude-ops.toml at its root, read on main: a
# change a task makes takes effect only once it reaches main, so a task can't loosen the gate it is judged by. Its
# keys replace the home toml's whole; the home toml keeps identity and machine settings (HOME_ONLY).

REPO_CONFIG = "claude-ops.toml"
HOME_ONLY = ("title", "repo", "main_branch", "dev_branch", "worktrees", "max_parallel", "direct_main", "flow",
             "chat_mcp")
MOVABLE = ("repos", "worktree", "gate", "app", "deploy", "branch_pattern", "default_branch_pattern", "pr_order",
           "pr_merge")  # and notes in
# [worker] and [design]
BRANCH_PATTERN = "{key}-{slug}"  # a Jira-key task's branch, unless the project sets branch_pattern
DEFAULT_BRANCH_PATTERN = "ops/{id}-{slug}"  # flow "pr": the branch of a task given none, unless default_branch_pattern
PR_MERGES = ("squash", "merge", "rebase")  # pr_merge: gh pr merge --<this>
NOTES_TABLES = ("worker", "design")  # only notes moves: [worker] env and port_base are machine settings
LAST_GOOD = {}  # project name -> the repo file's keys last loaded: a malformed one keeps these


def repo_config_home(repo):
    """The repo that may hold the repo file: repo itself if it is the top of a git repo (a plain project, or a
    workspace with a root repo), else None (a workspace whose folder is not a repo)."""
    if not repo.is_dir():
        return None
    top = sh("git", "rev-parse", "--show-toplevel", cwd=repo, check=False)
    return repo if top and Path(top).resolve() == repo.resolve() else None


def repo_config_blob(repo, main):
    """The blob of claude-ops.toml on main in repo, or "" when there is none."""
    return sh("git", "rev-parse", "--verify", "-q", f"refs/heads/{main}:{REPO_CONFIG}", cwd=repo, check=False)


def split_repo_config(text):
    """(the repo file's keys the queue reads from it, warnings about the rest, which it ignores)."""
    d = tomllib.loads(text)
    keep, warns = {}, []
    for k, v in d.items():
        if k in MOVABLE:
            keep[k] = v
        elif k in NOTES_TABLES and isinstance(v, dict):
            if "notes" in v:
                keep[k] = {"notes": v["notes"]}
            warns += [f"[{k}] {x} is set in the home toml only: ignored" for x in v if x != "notes"]
        elif k in HOME_ONLY or k in NOTES_TABLES:
            warns.append(f"{k} is set in the home toml only: ignored")
        else:
            warns.append(f"{k} is not a config key: ignored")
    for k, v in keep.items():
        bad = {"repos": not isinstance(v, list) or not all(isinstance(x, str) for x in v),
               "pr_order": not isinstance(v, list) or not all(isinstance(x, str) for x in v),
               "branch_pattern": not isinstance(v, str) or "{key}" not in v,
               "default_branch_pattern": not isinstance(v, str) or "{id}" not in v,
               "pr_merge": v not in PR_MERGES,
               "gate": not isinstance(v, list) or not all(isinstance(g, dict) and isinstance(g.get("run"), str)
                                                          for g in v),
               "deploy": not isinstance(v, list) or not all(isinstance(t, dict) for t in v)}.get(k, not isinstance(v, dict))
        if bad or (k in NOTES_TABLES and not isinstance(v["notes"], str)):
            raise ValueError(f"{k} is not " + {"repos": "a list of folder names", "gate": "a list of [[gate]] tables "
                             "each with a run command", "deploy": "a list of [[deploy]] tables",
                             "pr_order": "a list of repo folder names", "branch_pattern": "text with {key} in it",
                             "default_branch_pattern": "text with {id} in it",
                             "pr_merge": "one of " + ", ".join(PR_MERGES)}.get(
                                 k, f"a [{k}] table" if k not in NOTES_TABLES else "a [{k}] table whose notes is text"))
    if "repos" in keep and "." not in [str(r).strip("/") or "." for r in keep["repos"]]:
        raise ValueError(f'repos must list "." (the repo {REPO_CONFIG} is in)')
    return keep, warns


def merged_config(c, r):
    """The home toml c with the repo file's keys r in place of its own: each replaces the home key whole."""
    out = dict(c)
    for k, v in r.items():
        out[k] = {**(c.get(k) or {}), **v} if k in NOTES_TABLES else v
    return out


def toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def toml_value(v, multiline=False):
    """v as TOML: strings, numbers, booleans, lists and inline tables, what a project toml holds."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if multiline and "\n" in v and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", v):
            return '"""\n' + v.replace("\\", "\\\\").replace('"""', '""\\"') + '"""'
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise ValueError(f"can't write {v!r} as TOML")


def config_toml(r):
    """The repo file's text for keys r (split_repo_config's shape), tables in the README's order."""
    out = ["# claude-ops config for this project, read from the main branch (README: Config in the repo).",
           f"# The owner's home toml keeps {', '.join(HOME_ONLY[:4])},", f"# {', '.join(HOME_ONLY[4:])} and [worker] env.", ""]
    if "repos" in r:
        out += [f"repos = {toml_value(r['repos'])}", ""]
    if top := [f"{k} = {toml_value(r[k])}" for k in ("branch_pattern", "default_branch_pattern", "pr_order", "pr_merge")
                  if k in r]:
        out += top + [""]
    for k in ("worktree", "gate", "app", "deploy", "worker", "design"):
        v = r.get(k)
        for table in (v if isinstance(v, list) else [v] if isinstance(v, dict) else []):
            out.append(f"[[{k}]]" if isinstance(v, list) else f"[{k}]")
            out += [f"{toml_key(x)} = {toml_value(y, multiline=True)}" for x, y in table.items()]
            out.append("")
    return "\n".join(out)


def export_config(name):
    """opsd.py export-config -p NAME: write the keys of NAME's home toml that may live in the repo into
    claude-ops.toml in your checkout, uncommitted. The home toml stays as it is: until you commit the file and it
    reaches main, the queue goes on reading those keys there. Returns what moved."""
    path = PROJECTS_DIR / f"{name}.toml"
    if not re.fullmatch(r"[\w-]+", name or "") or not path.is_file():
        raise ValueError(f"no project {name!r} ({path})")
    c = tomllib.loads(path.read_text())
    repo = Path(c["repo"]).expanduser()
    home = repo_config_home(repo)
    if not home:
        raise ValueError(f"{repo} is not a git repo: a workspace keeps {REPO_CONFIG} in its root repo, so run "
                         f"opsd.py adopt-workspace-root -p {name} first")
    out = home / REPO_CONFIG
    if out.exists():
        raise ValueError(f"{out} exists already: edit it, or delete it and run this again")
    r = {k: c[k] for k in MOVABLE if k in c}
    for k in NOTES_TABLES:
        if str((c.get(k) or {}).get("notes", "")).strip():
            r[k] = {"notes": c[k]["notes"]}
    if "repos" in r and "." not in [str(x).strip("/") or "." for x in r["repos"]]:
        raise ValueError(f'{name}\'s repos has no ".": the queue reads {REPO_CONFIG} only from a root repo, so run '
                         f"opsd.py adopt-workspace-root -p {name} first")
    if not r:
        raise ValueError(f"{path} has no keys that may live in the repo ({', '.join(MOVABLE)}, [worker] notes, "
                         "[design] notes)")
    text = config_toml(r)
    if tomllib.loads(text) != r:  # what it wrote must read back as the same config
        raise ValueError(f"writing {path}'s keys as TOML went wrong; nothing was written")
    out.write_text(text)
    moved = [f"[{k}] notes" if k in NOTES_TABLES else f"[[{k}]] x{len(r[k])}" if isinstance(r[k], list) and
             k in ("gate", "deploy") else f"[{k}]" if isinstance(r[k], dict) else k for k in r]
    return [f"wrote {out} (uncommitted): " + ", ".join(moved),
            f"{path} is unchanged: it keeps {', '.join(k for k in HOME_ONLY if k in c)}"
            + (" and [worker] env" if (c.get("worker") or {}).get("env") else "")
            + f", and the queue keeps reading the moved keys there until {REPO_CONFIG} reaches "
              f"{c.get('main_branch', 'main')}. Then you may delete them from it: the repo file's keys win."]


class NotCloned(Exception):
    pass


def load_project(f):
    """Project for home toml f, with the repo file on main merged in. A repo file that won't load (bad TOML, a bad
    value, a repo folder you haven't cloned) leaves the last good one in use, or the home toml alone, with the error
    on p.config_error; for missing folders also on p.missing, which holds the project's new tasks."""
    c = tomllib.loads(f.read_text())
    name, main = f.stem, c.get("main_branch", "main")
    home = repo_config_home(Path(c["repo"]).expanduser()) if c.get("repo") else None
    blob = repo_config_blob(home, main) if home else ""
    if not blob:
        LAST_GOOD.pop(name, None)
        return Project(f, c)
    warns, missing = [], []
    try:
        r, warns = split_repo_config(sh("git", "cat-file", "blob", blob, cwd=home))
        p = Project(f, merged_config(c, r))
        missing = [x.sub for x in p.parts if not x.root and not x.src.is_dir()]
        if missing:
            raise NotCloned(" and ".join(f"{m}/" for m in missing) + f" {'is' if len(missing) == 1 else 'are'} "
                            f"not cloned: {REPO_CONFIG} on {main} lists {'it' if len(missing) == 1 else 'them'} in "
                            f"repos. Clone {'it' if len(missing) == 1 else 'them'} into {home}; the queue holds "
                            "new tasks until then")
        LAST_GOOD[name] = r
        err = None
    except Exception as e:
        err = str(e) if isinstance(e, NotCloned) else f"{REPO_CONFIG} on {main} did not load: {e}"
        last = LAST_GOOD.get(name)
        p = Project(f, merged_config(c, last) if last else c)
        print(f"project {f.name}: {err}" + ("; the last good one stays in use" if last else
                                             "; the home toml alone is in use"), flush=True)
    for w in warns:
        print(f"project {f.name}: {REPO_CONFIG}: {w}", flush=True)
    p.config_blob, p.config_error, p.config_warnings, p.missing = blob, err, warns, missing
    return p


def repo_configs_moved():
    """Whether a project's repo file on main changed, or a repo folder it waits for is now cloned."""
    for p in list(PROJECTS.values()):
        if not hasattr(p, "config_blob"):
            continue
        home = repo_config_home(p.repo)
        if (repo_config_blob(home, p.main) if home else "") != p.config_blob:
            return True
        if p.missing and any((p.repo / m).is_dir() for m in p.missing):
            return True
    return False


PROJECTS = {}
BROKEN = {}  # name -> (title, why) of a project whose config can't load: its card shows why


def load_projects():
    global PROJECTS, BROKEN
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    found, broken = {}, {}
    for f in sorted(PROJECTS_DIR.glob("*.toml")):
        try:
            p = load_project(f)
            for t in p.deploys:
                if t.error:
                    print(f"project {f.name}: deploy target {t.name}: {t.error}", flush=True)
            old = PROJECTS.get(p.name)
            if old:
                p.lock, p.checkout_lock, p.main_lock = old.lock, old.checkout_lock, old.main_lock
            found[p.name] = p
        except Exception as e:
            print(f"project {f.name}: {e}", flush=True)
            try:
                title = str(tomllib.loads(f.read_text()).get("title") or f.stem)
            except Exception:
                title = f.stem
            broken[f.stem] = (title, f"{f.name} did not load: {e}")
    BROKEN = broken
    PROJECTS = found  # swap, never clear and refill: requests iterate PROJECTS while a reload or archive runs
    with FACTS_LOCK:
        FACTS.clear()
    for p in PROJECTS.values():
        try:
            bootstrap(p)
        except Exception as e:
            print(f"project {p.name} bootstrap: {e}", flush=True)
    with FACTS_LOCK:
        for p in found.values():  # read them now, so the first dashboard request doesn't wait on git
            refresh_facts(p)


def proj(name):
    if name not in PROJECTS:
        raise ValueError(f"unknown project {name!r}")
    return PROJECTS[name]


# ---------------------------------------------------------------- archived projects
# Archiving moves <name>.toml into archived/, which load_projects doesn't glob, and touches nothing else: the repo,
# its worktrees, its tasks and its settings stay, so a restore brings the project back as it was.

ARCHIVE_DIR = PROJECTS_DIR / "archived"
ARCHIVE_LOCK = threading.Lock()


def archive_blockers(name):
    """What stops name from being archived: its tasks at work, and its open releases and deploys."""
    rows = q(f"SELECT id,kind,state FROM tasks WHERE project=? AND (state IN ({','.join('?' * len(ACTIVE))}) OR "
             f"(kind IN ('release','deploy') AND state NOT IN ({','.join('?' * len(ENDED))}))) ORDER BY id",
             (name, *ACTIVE, *ENDED))
    return [f"{t['kind']} #{t['id']} is {STATE_WORDS.get(t['state'], t['state'])}" for t in rows]


def archive_project(name):
    if not re.fullmatch(r"[\w-]+", name) or not (PROJECTS_DIR / f"{name}.toml").is_file():
        raise ValueError(f"{name} is already archived" if re.fullmatch(r"[\w-]+", name) and
                         (ARCHIVE_DIR / f"{name}.toml").is_file() else f"unknown project {name!r}")
    with ARCHIVE_LOCK:
        if why := archive_blockers(name):
            raise ValueError(f"{name} can't be archived while it has work under way: {', '.join(why)}. "
                             "Wait for it to finish, or cancel it.")
        dest = ARCHIVE_DIR / f"{name}.toml"
        if dest.exists():
            raise ValueError(f"an archived project is already named {name!r}: restore or remove {dest} first")
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (PROJECTS_DIR / f"{name}.toml").rename(dest)
        load_projects()
        if why := archive_blockers(name):  # the scheduler started one between the check and the move: undo
            dest.rename(PROJECTS_DIR / f"{name}.toml")
            load_projects()
            raise ValueError(f"{name} can't be archived: {', '.join(why)} just now.")
    return {"archived": name, "config": str(dest)}


def unarchive_project(name):
    src = ARCHIVE_DIR / f"{name}.toml"
    if not re.fullmatch(r"[\w-]+", name) or not src.is_file():
        raise ValueError(f"no archived project {name!r}")
    with ARCHIVE_LOCK:
        dest = PROJECTS_DIR / f"{name}.toml"
        if dest.exists():
            raise ValueError(f"can't restore {name}: another project is already named {name!r} ({dest}). "
                             f"Rename or archive that one first.")
        src.rename(dest)
        load_projects()
    return {"restored": name, "config": str(dest)}


def archived_projects():
    """The archived projects for the dashboard: name, title and how many of their tasks are not finished."""
    out = []
    for f in sorted(ARCHIVE_DIR.glob("*.toml")):
        try:
            title = tomllib.loads(f.read_text()).get("title") or f.stem
        except Exception:
            title = f.stem
        n = q(f"SELECT COUNT(*) AS n FROM tasks WHERE project=? AND state NOT IN ({','.join('?' * len(ENDED))})",
              (f.stem, *ENDED), one=True)["n"]
        out.append({"name": f.stem, "title": str(title), "open_tasks": n})
    return out


def bootstrap(p):
    p.wt.mkdir(parents=True, exist_ok=True)
    copy_workspace_files(p, p.dev_wt)
    for r in p.root_first():
        if not r.git("rev-parse", "--verify", "-q", f"refs/heads/{p.dev}", check=False):
            r.git("branch", p.dev, p.main)
        if not r.present(p.dev_wt):
            r.add_worktree(p.dev_wt, p.dev)
            set_setting(f"dev_prepared:{p.name}", "")  # a new dev worktree: Play dev prepares it first
        exclude = Path(r.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
        if RESULT_FILE not in (exclude.read_text() if exclude.exists() else ""):
            exclude.parent.mkdir(exist_ok=True)
            with open(exclude, "a") as f:
                f.write(f"\n{RESULT_FILE}\n")
    if p.build or p.launch:  # Play main runs it there
        copy_workspace_files(p, p.main_wt)
        if not all(r.present(p.main_wt) for r in p.parts):
            for r in p.root_first():
                if not r.present(p.main_wt):
                    r.add_worktree(p.main_wt, "--detach", p.main)
            set_setting(f"main_prepared:{p.name}", "")  # setup can take minutes: promote or Play main does it


def prepare_main(p, say, key):
    """Clone files and setup for a main worktree bootstrap made, on its first use; say(text) reports the phase.
    Slow, so only off the request thread. No main_prepared setting at all is a worktree made before it existed.
    key is where its setup.log goes (a task id or play-<name>); raises SetupFailed, and then it stays unprepared."""
    r = q("SELECT value FROM settings WHERE key=?", (f"main_prepared:{p.name}",), one=True)
    if r and r["value"] != "1":
        say(f"setting up {p.main_wt}")
        prepare_worktree(p, p.main_wt, src_first=p.repo, key=key)
        set_setting(f"main_prepared:{p.name}", "1")
    else:
        sync_clone_files(p, p.main_wt, src_first=p.repo)  # your edits to clone files since it was prepared


_libc = ctypes.CDLL(None, use_errno=True)


def clone_tree(src, dst):
    """APFS-clone src (file or directory) to dst, which must not exist. One clonefile(2) call clones a whole tree
    in the kernel: ~2 s for an 80k-file node_modules, where cp -cR clones file by file and took 30+ minutes.
    Falls back to cp -cR where clonefile can't (another volume, not APFS)."""
    if _libc.clonefile(os.fsencode(src), os.fsencode(dst), 1) != 0:  # 1 = CLONE_NOFOLLOW: a symlink stays one, as cp -R
        if Path(dst).is_dir() and not Path(dst).is_symlink():
            shutil.rmtree(dst, ignore_errors=True)
        subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True)


def copy_workspace_files(p, dest):
    """A workspace's own files (CLAUDE.md, docs, .claude, ...) are in none of its repos: copy them into dest
    (APFS clone), replacing older copies. Edits made to them in a worktree are not kept. A workspace with a root
    repo (repos lists ".") has none: its own files are in that repo."""
    if not p.repos or "." in p.repos:
        return
    dest.mkdir(parents=True, exist_ok=True)
    skip = {r.split("/")[0] for r in p.repos} | {".git", RESULT_FILE}
    for src in p.repo.iterdir():
        if src.name in skip:
            continue
        out = dest / src.name
        if out.is_dir() and not out.is_symlink():
            shutil.rmtree(out)
        elif out.exists() or out.is_symlink():
            out.unlink()
        clone_tree(src, out)


class SetupFailed(Exception):
    """A [worktree] setup command failed or timed out: nothing runs in that worktree until setup succeeds."""


def unprepared(dest):
    """True if setup in dest started but never succeeded (it failed, timed out, or was stopped). A worktree with no
    prepared setting at all was made before it existed, and counts as prepared."""
    r = q("SELECT value FROM settings WHERE key=?", (f"prepared:{dest}",), one=True)
    return bool(r) and r["value"] != "1"


def sync_clone_files(p, dest, src_first=None):
    """Bring dest's [worktree] clone entries up to date (APFS clone, seconds). A folder (node_modules, .venv) is a
    warm cache: cloned once, from src_first or the main worktree, else your checkout, and setup keeps it current. A
    file (.env, a signing key, .xcode.env.local) is yours: it follows your checkout every time, so your edit reaches
    the worktree, and a file you deleted, or took off the clone list, leaves it. Copies that never refreshed once
    pinned a node brew had removed, in the main worktree and from there in every deploy (#437)."""
    rec = f"cloned:{dest}"
    before = set(json.loads(setting(rec) or "[]"))
    files = set()
    for rel in p.clone:
        out, mine = dest / rel, p.repo / rel
        srcs = [s / rel for s in (src_first or p.main_wt, p.repo) if (s / rel).exists()]
        if any(s.is_dir() for s in srcs) or out.is_dir():
            if not out.exists() and srcs:
                out.parent.mkdir(parents=True, exist_ok=True)
                clone_tree(srcs[0], out)
            continue
        if not mine.is_file():  # you have none: the worktree's goes too, if it came from you
            if rel in before and (out.is_file() or out.is_symlink()):
                out.unlink()
            continue
        files.add(rel)
        if out.is_file() and filecmp.cmp(mine, out, shallow=False):
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() or out.is_symlink():
            out.unlink()
        clone_tree(mine, out)
    for rel in before - set(p.clone):
        if (dest / rel).is_file() or (dest / rel).is_symlink():
            (dest / rel).unlink()
    set_setting(rec, json.dumps(sorted(files)))


def prepare_worktree(p, dest, src_first=None, setup=True, key=None):
    """Sync the clone files (sync_clone_files) and run the project's setup commands, logged to
    runs/<key>/setup.log. Raises SetupFailed; the worktree counts as prepared only once every command succeeded."""
    sync_clone_files(p, dest, src_first)
    if setup and p.setup:
        set_setting(f"prepared:{dest}", "")
        run_setup(p, key or f"setup-{p.name}", dest)
        set_setting(f"prepared:{dest}", "1")


def run_setup(p, key, dest, log_name="setup.log"):
    """Run p's setup commands in dest, each in a process group of its own with p.setup_timeout. First stops any
    setup an earlier attempt left running there. Raises SetupFailed naming the command, with its output tail."""
    stop_setup_in(dest)
    log = RUNS / str(key) / log_name
    for i, cmd in enumerate(p.setup, 1):
        rc, tail = run_step(key, f"setup {i}/{len(p.setup)}", cmd, dest, log_name,
                            env=dict(os.environ, OPS_REPO=str(p.repo)), timeout=p.setup_timeout * 60,
                            pidkey=f"setup_pid:{dest}")
        if isinstance(key, int) and stopped(key):
            raise SetupFailed("stopped")
        if rc is None:
            raise SetupFailed(f"setup command `{cmd}` timed out after {p.setup_timeout:g} min (log: {log}).\n"
                              + tail[-1500:])
        if rc:
            raise SetupFailed(f"setup command `{cmd}` exited {rc} (log: {log}).\n" + tail[-1500:])


def setup_running(dest):
    """Setup still runs in dest: the pid run_setup recorded there is alive, with the start time it recorded."""
    rec = setting(f"setup_pid:{dest}").split(" ", 1)
    return len(rec) == 2 and rec[0].isdigit() and started_at(int(rec[0])) == rec[1]


def stop_setup_in(dest):
    """Stop whatever setup is still running in dest: one this daemon started (a Play, a task paused moments ago),
    or one an earlier daemon left behind, by the pid and start time it recorded."""
    stop_many([proc for proc, cwd in list(STEPS.items()) if str(cwd) == str(dest)])
    rec = setting(f"setup_pid:{dest}").split(" ", 1)
    if len(rec) == 2 and rec[0].isdigit() and started_at(int(rec[0])) == rec[1]:
        killgroup(int(rec[0]))
    set_setting(f"setup_pid:{dest}", "")


def projects_home():
    """Where a bare name like "mygame" lives: the folder most existing projects sit in, else ~/dev."""
    parents = [p.repo.parent for p in PROJECTS.values()]
    if parents:
        return max(set(parents), key=parents.count)
    return HOME / "dev" if (HOME / "dev").is_dir() else HOME


def resolve_repo(path):
    path = (path or "").strip()
    if not path:
        raise ValueError("enter a folder path or a name")
    repo = Path(path).expanduser()
    return (repo if repo.is_absolute() else projects_home() / repo).resolve()


def create_repo(repo):
    """A new, empty project: a git repo with one empty commit, so main exists for the dev branch."""
    if repo.exists():
        raise ValueError(f"{repo} already exists")
    repo.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "commit", "-q", "--allow-empty", "-m", "Initial commit", cwd=repo)


DIRECT_MAIN_NOTE = ("# merges go straight to main (see Straight to main in the README). To merge into dev and "
                    "Promote by hand, add:\n# direct_main = false")


def detect_project(path, name=None, create=False):
    """Write a starter config for the repo at path. Returns the config path."""
    repo = resolve_repo(path)
    if create:
        create_repo(repo)
    if not repo.exists():
        raise ValueError(f"{repo} does not exist")
    subs = [] if (repo / ".git").exists() else sorted(d.name for d in repo.iterdir() if (d / ".git").exists())
    if not (repo / ".git").exists() and not subs:
        raise ValueError(f"{repo} is not a git repository, and no folder in it is one")
    name = re.sub(r"[^a-z0-9-]+", "-", (name or repo.name).lower()).strip("-")
    out = PROJECTS_DIR / f"{name}.toml"
    if out.exists():
        raise ValueError(f"project {name} already exists ({out})")
    if subs:
        out.write_text(workspace_toml(repo, subs))
        return out
    main = sh("git", "symbolic-ref", "--short", "HEAD", cwd=repo, check=False) or "main"
    dev = "dev"
    if sh("git", "rev-parse", "--verify", "-q", "refs/heads/dev", cwd=repo, check=False):
        dev = "ops-dev"  # don't take over a dev branch the repo already uses
    lines = [f'title = "{repo.name}"', f'repo = "{repo}"', f'main_branch = "{main}"', f'dev_branch = "{dev}"', DIRECT_MAIN_NOTE, ""]
    unity = repo / "scripts" / "unity.sh"
    if unity.exists() and (repo / "Assets").is_dir():
        app = re.search(r"Builds/macOS/([\w .-]+\.app)", unity.read_text())
        app = f"Builds/macOS/{app.group(1)}" if app else "Builds/macOS/App.app"
        lines += ['[worktree]', 'clone = ["Library"]   # warm Unity import cache', "",
                  *gate_toml("compile", "scripts/unity.sh compile", log="Logs/compile.log", errors="error CS"),
                  *gate_toml("test-edit", "scripts/unity.sh test-edit", log="Logs/test-editmode.log",
                             report="Logs/test-editmode.xml", errors="error CS"),
                  *gate_toml("test-play", "scripts/unity.sh test-play", log="Logs/test-playmode.log",
                             report="Logs/test-playmode.xml", errors="error CS"),
                  "[app]", 'build = "scripts/unity.sh build"', f'launch = "open -n \'{app}\'"',
                  f'process = "{app}"', f'artifact = "{app}"', ""]
    elif (repo / "package.json").exists():
        scripts = json.loads((repo / "package.json").read_text()).get("scripts", {})
        lines += ['[worktree]', 'setup = ["npm ci --silent"]', "# setup_timeout = 15   # minutes each setup command may run before it is stopped and the task fails", ""]
        for s in ("lint", "typecheck", "test"):
            if s in scripts:
                lines += gate_toml(s, f"npm run {s} --silent")
        if "build" in scripts:
            lines += ["[app]", 'build = "npm run build --silent"', ""]
    elif (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        lines += gate_toml("test", "python3 -m pytest -q")
    lines += ["[worker]", "notes = \"\"\"", "\"\"\"", "", "[design]", "notes = \"\"\"", "\"\"\"", ""]
    out.write_text("\n".join(lines))
    return out


ADOPT_MSG = "chore: track workspace root"


def adopt_workspace_root(name):
    """opsd.py adopt-workspace-root -p NAME: make workspace NAME's own folder a git repo whose edits tasks keep. In
    your checkout: git init it if needed (on main_branch), gitignore each repo folder, commit the folder's own files,
    branch dev from main, and add "." to repos in NAME's toml (the rest of the file as it was). Safe to run again: a
    step already done is skipped. Refuses if the folder is a repo with uncommitted changes. Returns what it did."""
    path = PROJECTS_DIR / f"{name}.toml"
    if not re.fullmatch(r"[\w-]+", name or "") or not path.is_file():
        raise ValueError(f"no project {name!r} ({path})")
    text = path.read_text()
    c = tomllib.loads(text)
    repo = Path(c["repo"]).expanduser()
    main, dev = c.get("main_branch", "main"), c.get("dev_branch", "dev")
    listed = [r.strip("/") or "." for r in c.get("repos", [])]
    subs = [r for r in listed if r != "."]
    if not subs:
        raise ValueError(f"{name} is not a workspace: its config has no repos")
    if not repo.is_dir():
        raise ValueError(f"{repo} does not exist")
    did = []
    git = lambda *a, check=True: sh("git", "-C", str(repo), *a, check=check)
    top = git("rev-parse", "--show-toplevel", check=False) if (repo / ".git").exists() else ""
    if (repo / ".git").exists() and (not top or Path(top).resolve() != repo.resolve()):
        raise ValueError(f"{repo}/.git is there but {repo} is not the top of a git repo")
    if not top:
        git("init", "-q", "-b", main)
        did.append(f"git init {repo} on {main}")
    born = bool(git("rev-parse", "--verify", "-q", "HEAD", check=False))
    head = git("symbolic-ref", "-q", "--short", "HEAD", check=False)
    if born:
        dirty = git("status", "--porcelain", "--", ".", *(f":(exclude){s}" for s in subs))
        if dirty:
            raise ValueError(f"{repo} has uncommitted changes; commit or discard them first:\n{dirty[:1000]}")
        if not git("rev-parse", "--verify", "-q", f"refs/heads/{main}", check=False):
            raise ValueError(f"{repo} is a git repo with commits but no {main} branch")
    elif head != main:
        git("symbolic-ref", "HEAD", f"refs/heads/{main}")  # an empty repo: its first commit goes on main
        head = main
    ignore = repo / ".gitignore"
    missing = [s for s in subs if subprocess.run(["git", "check-ignore", "-q", f"{s}/"], cwd=repo).returncode]
    if missing:
        old = ignore.read_text() if ignore.exists() else ""
        ignore.write_text(old + ("\n" if old and not old.endswith("\n") else "")
                          + "".join(f"/{s}/\n" for s in missing))
        did.append(f".gitignore: added " + ", ".join(f"/{s}/" for s in missing))
    if missing or not born:
        if born and head != main:
            raise ValueError(f"{repo} is on {head or 'a detached HEAD'}, not {main}: check out {main} first")
        git("add", "-A")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode:
            git("commit", "-q", "-m", ADOPT_MSG)
            did.append(f"committed the root's files on {main}: {git('rev-parse', '--short', 'HEAD')}")
        elif not born:
            git("commit", "-q", "--allow-empty", "-m", ADOPT_MSG)
            did.append(f"committed an empty root on {main}")
    if not git("rev-parse", "--verify", "-q", f"refs/heads/{dev}", check=False):
        git("branch", dev, main)
        did.append(f"made {dev} from {main}")
    check_root_repo(repo, subs, main)
    if "." not in listed:
        m = re.search(r"^(\s*repos\s*=\s*\[)", text, re.M)
        if not m:
            raise ValueError(f"can't find the repos = [...] line in {path}")
        end = text.index("]", m.end())  # last: revs strings list repos in order, so ones recorded stay right
        head = text[:end].rstrip()
        new = head + ("" if head.endswith("[") else " " if head.endswith(",") else ", ") + '"."' + text[end:]
        try:
            ok = "." in tomllib.loads(new).get("repos", [])
        except tomllib.TOMLDecodeError:
            ok = False
        if not ok:
            raise ValueError(f'the root is ready, but adding "." to repos in {path} by itself went wrong: add it '
                             "there yourself, then run this again")
        path.write_text(new)
        did.append(f'added "." to repos in {path}')
    return did


def workspace_toml(repo, subs):
    """Starter config for a workspace: a folder whose git repos are folders in it, such as a backend and an
    app. Each repo's setup and tests run from its own folder."""
    heads = {sh("git", "symbolic-ref", "--short", "HEAD", cwd=repo / s, check=False) for s in subs}
    main = heads.pop() if len(heads) == 1 and "" not in heads else "main"
    for s in subs:
        if not sh("git", "rev-parse", "--verify", "-q", f"refs/heads/{main}", cwd=repo / s, check=False):
            raise ValueError(f"{repo / s} has no {main} branch, so the repos in {repo} share no main branch")
    dev = "dev"
    if any(sh("git", "rev-parse", "--verify", "-q", "refs/heads/dev", cwd=repo / s, check=False) for s in subs):
        dev = "ops-dev"  # don't take over a dev branch a repo already uses
    setup, clone, gates = [], [], []
    for s in subs:
        d = repo / s
        ignored = sh("git", "ls-files", "-o", "-i", "--exclude-standard", "--directory", cwd=d, check=False)
        clone += [f"{s}/{f}" for f in ignored.splitlines()  # env files the repo's tests need but git lacks
                  if not f.endswith("/") and f.count("/") < 2 and re.match(r"(.*/)?(\.env.*|.*\.env)$", f)]
        if (d / "package.json").exists():
            scripts = json.loads((d / "package.json").read_text()).get("scripts", {})
            setup.append(f"cd {s} && npm ci --silent")
            gates += [gate_toml(f"{s} {k}", f"cd {s} && npm run {k} --silent") for k in ("lint", "typecheck", "test")
                      if k in scripts]
        elif (d / "poetry.lock").exists():
            setup.append(f"cd {s} && poetry install --no-interaction --quiet")
            tests = " tests/unit" if (d / "tests" / "unit").is_dir() else ""
            gates.append(gate_toml(f"{s} test", f"cd {s} && poetry run pytest -q{tests}"))
        elif (d / "pyproject.toml").exists() or (d / "setup.py").exists():
            gates.append(gate_toml(f"{s} test", f"cd {s} && python3 -m pytest -q"))
    lines = [f'title = "{repo.name}"', f'repo = "{repo}"',
             "repos = [" + ", ".join(f'"{s}"' for s in subs) + "]   # a workspace: each folder is its own git repo",
             f'main_branch = "{main}"', f'dev_branch = "{dev}"', DIRECT_MAIN_NOTE, "", "[worktree]",
             "clone = [" + ", ".join(f'"{c}"' for c in clone) + "]",
             "setup = [" + ", ".join(f'"{c}"' for c in setup) + "]",
             "# setup_timeout = 15   # minutes each setup command may run before it is stopped and the task fails", ""]
    for g in gates:
        lines += g
    lines += ["[worker]", "notes = \"\"\"", "\"\"\"", "", "[design]", "notes = \"\"\"", "\"\"\"", ""]
    return "\n".join(lines)


def gate_toml(name, run, **kw):
    return ["[[gate]]", f'name = "{name}"', f'run = "{run}"', *(f'{k} = "{v}"' for k, v in kw.items()), ""]


# ---------------------------------------------------------------- storage

SLOW_MS = 100  # a request slower than this is logged, with where its time went
SQLT = threading.local()  # per request thread: [seconds waiting for DBL, seconds in queries]


def q(sql, args=(), one=False):
    """One statement on its own connection. A read takes no lock (the database is in WAL mode, so a write in
    progress does not block it); a write takes DBL, so the daemon's writes go one at a time."""
    t0 = time.time()
    head = sql.lstrip()[:6].upper()
    read = head == "SELECT" or (head == "PRAGMA" and "=" not in sql)
    with (nullcontext() if read else DBL):
        t1 = time.time()
        try:
            return q_run(sql, args, one)
        finally:
            t = getattr(SQLT, "t", None)
            if t:
                t[0] += t1 - t0
                t[1] += time.time() - t1


DB_POOL = []  # idle connections: opening one and closing it (which checkpoints the WAL) cost ms per query


def q_run(sql, args, one):
    try:
        c = DB_POOL.pop()
    except IndexError:
        c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        c.commit()
        out = cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else (
            (rows[0] if rows else None) if one else rows)
    except BaseException:
        c.close()
        raise
    if len(DB_POOL) < 8:
        DB_POOL.append(c)
    else:
        c.close()
    return out


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
    q("PRAGMA journal_mode=WAL")  # readers and the writer don't block each other; the mode is kept in the file
    q("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, project TEXT, title TEXT, body TEXT, priority INTEGER DEFAULT 0,
        hold INTEGER DEFAULT 0, depends_on INTEGER, parent INTEGER,
        state TEXT DEFAULT 'queued', phase TEXT DEFAULT '', created REAL, started REAL,
        finished REAL, branch TEXT, worktree TEXT, session_id TEXT, cost REAL DEFAULT 0,
        rounds INTEGER DEFAULT 0, summary TEXT, question TEXT, error TEXT,
        next_prompt TEXT, merged TEXT, start_at TEXT DEFAULT 'claude', pid INTEGER,
        kind TEXT DEFAULT 'task', base TEXT, pid_start TEXT)""")
    cols = {r["name"] for r in q("PRAGMA table_info(tasks)")}
    for col, decl in (("kind", "TEXT DEFAULT 'task'"), ("base", "TEXT"),  # databases from before releases
                      ("pid_start", "TEXT"), ("paused_by", "TEXT"), ("auto_release", "INTEGER DEFAULT 0"),
                      ("deploy", "TEXT"),  # a deploy's target and choices, as JSON
                      ("origin", "TEXT"),  # and before peer sends
                      ("senddev", "TEXT"),  # and before Send to dev: its snapshot and how it went, as JSON
                      ("model", "TEXT"),  # and before models per task: null takes model:<kind> (see model_for)
                      ("session_model", "TEXT"),  # the model its session started on; every resume keeps it
                      ("effort", "TEXT"),  # and before effort per task: null takes the setting effort (see effort_for)
                      ("last_call", "REAL"), ("ctx_tokens", "INTEGER"),  # its session's last call (see stale_session)
                      ("research", "TEXT"),  # a research run: {question, tier, slug, path, read} as JSON (RES-1)
                      ("reports", "TEXT"),  # reports its done result listed: [{path, read}] as JSON (RES-10)
                      ("not_before", "REAL"),  # it starts no earlier than this epoch (README: Start later)
                      ("not_before_why", "TEXT"),
                      ("jira", "TEXT"), ("target", "TEXT"),  # a branch task's Jira key and named branch
                      ("review", "TEXT")):  # a done result's review report {fixed, before, after, check}, as JSON  # the window when not_before is "after the next reset", e.g. 5-hour
        if col not in cols:
            q(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    q("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, task_id INTEGER, ts REAL, text TEXT)")
    q("""CREATE TABLE IF NOT EXISTS prs (id INTEGER PRIMARY KEY, task_id INTEGER, repo TEXT, seq INTEGER DEFAULT 0,
        number INTEGER, url TEXT, state TEXT DEFAULT 'waiting', ci TEXT DEFAULT 'pending', failing TEXT,
        checked_at REAL, opened_at REAL, merged_at REAL, merge_commit TEXT, synced INTEGER DEFAULT 0, error TEXT,
        warning TEXT)""")  # a branch task's GitHub PRs, one per repo (README: Branches and pull requests)
    q("CREATE INDEX IF NOT EXISTS prs_task ON prs (task_id, seq)")
    q("CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id)")  # a task's log, and the latest per task
    q("""CREATE TABLE IF NOT EXISTS designs (id INTEGER PRIMARY KEY, project TEXT, title TEXT, session_id TEXT,
        state TEXT DEFAULT 'idle', created REAL, updated REAL, cost REAL DEFAULT 0)""")
    q("""CREATE TABLE IF NOT EXISTS design_msgs (id INTEGER PRIMARY KEY, design_id INTEGER, role TEXT,
        text TEXT, ts REAL, task_ids TEXT, note TEXT)""")
    q("CREATE INDEX IF NOT EXISTS design_msgs_design ON design_msgs (design_id, id)")
    q("""CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY, project TEXT, verb TEXT, title TEXT, why TEXT,
        source_kind TEXT, source_id INTEGER, created REAL, closed REAL, task INTEGER)""")  # what Claude asks of you
    # (see To-dos); task: the task Queue as task made of it (TODO-7)
    if "task" not in {r["name"] for r in q("PRAGMA table_info(todos)")}:  # databases from before Queue as task
        q("ALTER TABLE todos ADD COLUMN task INTEGER")
    q("""CREATE TABLE IF NOT EXISTS todo_notes (key TEXT PRIMARY KEY, note TEXT, parked REAL, skipped REAL,
        wake_tasks TEXT, wake_on TEXT DEFAULT 'merged', wake_set INTEGER DEFAULT 0, updated REAL, topic TEXT,
        wake_at REAL)""")
    # your note on a to-do, its skip or park, the tasks and time a parked one waits for (TODO-6), and its topic
    # (TODAY-2 bond)
    ncols = {r["name"] for r in q("PRAGMA table_info(todo_notes)")}
    for col, decl in (("wake_tasks", "TEXT"), ("wake_on", "TEXT DEFAULT 'merged'"),  # databases from before wait sets
                      ("wake_set", "INTEGER DEFAULT 0"),  # 1: set by the picker or Claude, not read from the note
                      ("topic", "TEXT"),  # and before a to-do's bond: the topic ref it was first sent to
                      ("wake_at", "REAL")):  # and before timed waits: hide until this epoch
        if col not in ncols:
            q(f"ALTER TABLE todo_notes ADD COLUMN {col} {decl}")
            if col == "wake_tasks" and "wake_task" in ncols:  # one task becomes a set of one
                q("UPDATE todo_notes SET wake_tasks=json_array(wake_task) WHERE wake_task IS NOT NULL")
    dcols = {r["name"] for r in q("PRAGMA table_info(designs)")}
    for col, decl in (("kind", "TEXT DEFAULT 'design'"),  # databases from before project chats
                      ("spec", "TEXT"), ("spec_sub", "TEXT DEFAULT ''"),  # and before OpenSpec chats
                      ("seen", "REAL DEFAULT 0"), ("ended", "REAL"),  # and before "ready for you" and End chat
                      ("origin", "TEXT"),  # and before peer sends
                      ("spec_draft", "INTEGER DEFAULT 0"),  # and before New change: drafting, no change yet
                      ("session_model", "TEXT"),  # and before models per kind: the model its session started on
                      ("last_call", "REAL"), ("ctx_tokens", "INTEGER"),  # its session's last call (see chat_stale)
                      ("queued", "TEXT DEFAULT '[]'"),  # and before queued messages: sent while Claude replies
                      ("subject", "TEXT"),  # and before a topic per to-do: what it is about (see conv_open)
                      ("quick", "INTEGER DEFAULT 0")):  # 1: the quick chat started it (its Recent list, TODAY-6)
        if col not in dcols:
            q(f"ALTER TABLE designs ADD COLUMN {col} {decl}")
            if col == "seen":  # chats from before count as read, so they don't all show a new reply at once
                q("UPDATE designs SET seen=updated")
    q("CREATE TABLE IF NOT EXISTS task_msgs (id INTEGER PRIMARY KEY, task_id INTEGER, role TEXT, text TEXT, ts REAL)")
    q("CREATE INDEX IF NOT EXISTS task_msgs_task ON task_msgs (task_id, id)")
    if "delivered" not in {r["name"] for r in q("PRAGMA table_info(task_msgs)")}:  # databases from before steering
        q("ALTER TABLE task_msgs ADD COLUMN delivered REAL")
    q("""CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY, project TEXT, title TEXT, body TEXT,
        every TEXT, next_at REAL, paused INTEGER DEFAULT 0, last_task INTEGER, created REAL, model TEXT, effort TEXT,
        hold INTEGER DEFAULT 0, priority INTEGER DEFAULT 0)""")  # recurring jobs: see ## Recurring jobs
    scols = {r["name"] for r in q("PRAGMA table_info(schedules)")}
    if "research" not in scols:  # a recurring research job's tier
        q("ALTER TABLE schedules ADD COLUMN research TEXT")
    if "after_schedule" not in scols:  # the schedule whose last task this one's task waits on (SCHED-1)
        q("ALTER TABLE schedules ADD COLUMN after_schedule INTEGER")
    q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    q("CREATE TABLE IF NOT EXISTS session_costs (session_id TEXT PRIMARY KEY, total REAL)")  # see cost_added
    for k, v in {"max_parallel": "3", "paused": "0", "model": "", "usage_brake": "85", "usage_taper": "30", "model:release": "sonnet",
                 "release_wait": "180", "effort": "medium"}.items():
        q("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    q("DELETE FROM settings WHERE key='draining'")  # the old restart's stored hold: a crash could leave it set


def setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return r["value"] if r else ""


def set_setting(k, v):
    q("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, str(v)))


# ---------------------------------------------------------------- models
# Each kind of work has a model setting: model:task, model:release, model:design, model:chat. Each is "" (the next
# one down), an alias (opus, sonnet, haiku) or a full model id; "" falls back to the global setting model, and that
# "" to Claude Code's own default. A task's own model column beats its kind's setting.
# A session keeps the model it started with: the prompt cache belongs to one model, so resuming an Opus session on
# Sonnet would rewrite the whole context. The model a session started on (session_model, read from its init event)
# goes to --model on every resume, whatever the settings say now.

MODEL_KINDS = ("task", "release", "design", "chat")
SETTING_KEYS = ("max_parallel", "paused", "model", "usage_brake", "usage_taper", "release_wait", "effort",  # the dashboard's
                "research_repo", *(f"model:{k}" for k in MODEL_KINDS))
MODEL_OF_KIND = {"senddev": "task", "deploy": "task", "project": "chat"}  # a project chat is the Chat tab


def model_for(kind):
    """The model a new session of this kind starts on: model:<kind>, else model, else "" (Claude Code's default)."""
    return setting(f"model:{MODEL_OF_KIND.get(kind, kind)}") or setting("model")


def model_name(m):
    """A model as typed: "" or None for the default, else an alias or id. Raises on anything else."""
    m = (m or "").strip()
    if m.lower() in ("", "default", "none"):
        return None
    if not re.fullmatch(r"[\w.:\-\[\]]{1,80}", m):
        raise ValueError(f"not a model name: {m!r}")
    return m.lower() if m.lower() in ("opus", "sonnet", "haiku", "fable") else m


def session_model(row, kind):
    """The --model for row's next turn (a task or a chat): the model its session started on if it has a session,
    else its own model, else its kind's. A session from before models were stored ran on the global model."""
    if row["session_id"]:
        return setting("model") if row["session_model"] is None else row["session_model"]
    return row.get("model") or model_for(kind)


def model_args(m):
    return ["--model", m] if m else []


# ---------------------------------------------------------------- effort
# How hard Claude thinks: --effort low, medium, high, xhigh or max. The setting effort (medium unless you change it)
# covers every session; "" leaves it to Claude Code (effortLevel in your settings.json). A task's own effort column
# beats the setting. Effort goes on every turn, resumes included: --effort lasts one process, not the session.

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_name(e):
    """An effort as typed: "" or None for the default, else one of EFFORTS. Raises on anything else."""
    e = (e or "").strip().lower()
    if e in ("", "default", "none"):
        return None
    if e not in EFFORTS:
        raise ValueError(f"effort is one of {', '.join(EFFORTS)}, not {e!r}")
    return e


def effort_for(row=None):
    """The --effort for a turn of row (a task, or None for a chat): its own effort, else the setting effort."""
    return (row or {}).get("effort") or setting("effort")


def effort_args(e):
    return ["--effort", e] if e else []


def init_model(e):
    """The model a stream's system init event names, or None for any other event."""
    if e.get("type") == "system" and e.get("subtype") == "init" and isinstance(e.get("model"), str):
        return e["model"]
    return None


def release_default(p):
    """Whether new tasks of p are marked "Promote after merge" unless you say otherwise."""
    return setting(f"auto_release:{p.name}") == "1"


def release_wait():
    """Minutes the promote train waits at most for p's queue to go quiet, after the first of its tasks merges."""
    try:
        return max(0.0, float(setting("release_wait") or 180))
    except ValueError:
        return 180.0


def task(tid):
    return q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)


def upd(tid, **kw):
    guard = ""
    if kw.get("state") not in (None, "queued", *STOPPED):  # a pipeline state change loses to a pause or cancel
        guard = f" AND state NOT IN ({','.join('?' * len(STOPPED))})"
    q(f"UPDATE tasks SET {','.join(k + '=?' for k in kw)} WHERE id=?{guard}",
      (*kw.values(), tid, *(STOPPED if guard else ())))
    if "phase" in kw:
        PHASE_SINCE[tid] = time.time()


def cost_added(sid, total):
    """The dollars a result event adds. Its total_cost_usd runs for the whole session, and a resumed session
    carries the earlier total forward, so only the rise since the session's last result is new."""
    total = total or 0
    if not sid:
        return total
    row = q("SELECT total FROM session_costs WHERE session_id=?", (sid,), one=True)
    q("INSERT INTO session_costs (session_id, total) VALUES (?,?) "
      "ON CONFLICT(session_id) DO UPDATE SET total=excluded.total", (sid, total))
    last = row["total"] if row else 0
    return total - last if total >= last else total  # lower: a session that started its count over


def ev(tid, text):
    q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (tid, time.time(), text))
    print(f"[#{tid}] {text}", flush=True)
    stale_facts((task(tid) or {}).get("project"))  # a step that may have moved dev or main, or quit the build


def add_task(project, body, title="", priority=0, hold=0, depends_on=None, parent=None, kind="task", auto_release=None,
             deploy=None, origin=None, senddev=None, model=None, effort=None, research=None, not_before=None,
             jira=None, target=None):
    """auto_release None takes the project's default ("Promote after merge" on the project).
    A deploy (kind "deploy") is made only by queue_deploy: you clicked Deploy, or ran `opsd.py deploy`.
    origin: the session in another project that sent it (see send_peer).
    senddev: the snapshot a Send to dev task (kind "senddev", made by queue_senddev) lands on dev.
    model: the model its worker starts on; None takes model:<kind> when it starts (see model_for).
    effort: its --effort; None takes the setting effort on each turn (see effort_for).
    research: a research run's {question, tier, slug, path} (see queue_research).
    not_before: when it may start, as start_time takes it (a time, "reset" or an epoch); None starts it when it can.
    jira, target: a branch task's Jira key and named branch (README: Branches and pull requests); its branch is
    worked out now and kept in branch. A branch task never promotes after merge. In a flow "pr" project every task
    and Send to dev is one: given no branch, it gets default_branch_pattern's (default_branch)."""
    p = proj(project)
    jira, target = (str(jira or "").strip() or None), (str(target or "").strip() or None)
    model, effort = model_name(model), effort_name(effort)
    nb, why = start_time(not_before) if not_before not in (None, "") else (None, None)
    if auto_release is None:
        auto_release = release_default(p)
    body = body.strip()
    title = (title or (title_of(body) if body else "untitled")).strip()[:120]
    if (kind == "deploy") != bool(deploy):
        raise ValueError("a deploy needs its target, and only a deploy has one")
    branch = None
    if jira or target:
        if kind != "task" or research:
            raise ValueError("only a task can work on a named branch")
        branch = branch_name(p, title, jira, target)
    pr_flow = getattr(p, "flow", "dev") == "pr" and kind in MERGES and not branch
    with DBL:  # flow "pr": its branch name has its id in it, so the id is picked first and no other write comes between
        tid = q("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM tasks", one=True)["n"] if pr_flow else None
        if pr_flow:  # kept as its target too, so it is a branch task from the start
            branch = target = default_branch(p, tid, title)
        tid = q("INSERT INTO tasks (id, project, title, body, priority, hold, depends_on, parent, created, kind, "
                "auto_release, deploy, origin, senddev, model, effort, research, jira, target, branch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, project, title, body, int(priority), int(bool(hold)), depends_on or None, parent, time.time(),
                 kind, int(bool(auto_release) and kind in MERGES and not branch), json.dumps(deploy) if deploy else None,
                 json.dumps(origin) if origin else None, json.dumps(senddev) if senddev else None, model, effort,
                 json.dumps(research) if research else None, jira, target, branch))
    ev(tid, "queued" + (f" after #{depends_on}" if depends_on else "") + (f" on {model}" if model else "")
       + (f" at {effort} effort" if effort else "") + (f" on branch {branch}" if branch else ""))
    if nb:
        q("UPDATE tasks SET not_before=?, not_before_why=? WHERE id=?", (nb, why, tid))
        ev(tid, f"waits for the {why} reset: starts after {start_clock(nb)}" if why
           else f"waits until {start_clock(nb)}")
    return tid


JIRA_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")  # a Jira key: it names a branch task's branch and prefixes its titles


def slug_of(title):
    """A title as a branch name's part: lowercase, each run of other than a-z0-9 a '-', 40 characters at most."""
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40] or "task"


def branch_name(p, title, jira=None, target=None):
    """A branch task's branch: target as given, else the project's branch_pattern for the Jira key. ValueError for
    a bad key or a name git refuses, or one of the queue's own branches."""
    if jira and not JIRA_RE.fullmatch(jira):
        raise ValueError(f"{jira!r} is not a Jira key (like BALL-123)")
    name = target or p.branch_pattern.replace("{key}", jira).replace("{slug}", slug_of(title))
    if subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True).returncode:
        raise ValueError(f"{name!r} is not a branch name git accepts")
    if name in (p.main, p.dev) or name.startswith("task/"):
        raise ValueError(f"{name!r} is one of the queue's own branches: pick another name")
    return name


def default_branch(p, tid, title):
    """A flow "pr" task's branch when it was given none: default_branch_pattern for its id and title, or
    DEFAULT_BRANCH_PATTERN's when that makes no name git accepts."""
    for pattern in (p.default_branch_pattern, DEFAULT_BRANCH_PATTERN):
        name = pattern.replace("{id}", str(tid)).replace("{slug}", slug_of(title))
        if not subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True).returncode and \
                name not in (p.main, p.dev) and not name.startswith("task/"):
            return name
    return DEFAULT_BRANCH_PATTERN.replace("{id}", str(tid)).replace("{slug}", "task")


def branch_input(text):
    """The New task form's one field, Branch or Jira key: (jira, target). A value that looks like a key is one."""
    text = str(text or "").strip()
    return (text, None) if JIRA_RE.fullmatch(text) else (None, text or None)


def is_branch_task(t):
    """A task that works on a named branch and ends in pull requests, not a merge into dev."""
    return bool(t and (t.get("jira") or t.get("target")))


def notify(title, msg):
    msg = msg.replace('"', "'").replace("\\", "/")[:200]
    title = title.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                   capture_output=True)


# ---------------------------------------------------------------- shell / processes

def sh(*cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        err = "\n".join(l for l in r.stderr.strip().splitlines() if not GIT_CHATTER.match(l))
        raise RuntimeError(f"{' '.join(map(str, cmd))}: {err or r.stdout.strip()}")
    return r.stdout.strip()


def within(path, root):
    """path is root or inside it, after resolving symlinks and '..'."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def proc_cwds(pids):
    """pid -> working directory, for many pids in one lsof call. One call per pid took 50 ms each, and
    a Mac has dozens of processes started by a relative path: the dashboard waited seconds on it."""
    if not pids:
        return {}
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    cwds, pid = {}, None
    for l in out.splitlines():
        if l.startswith("p"):
            pid = int(l[1:])
        elif l.startswith("n") and pid is not None:
            cwds[pid] = l[1:]
    return cwds


def proc_cwd(pid):
    return proc_cwds([pid]).get(pid, "")


def my_exes():
    """(pid, executable) of each of your processes but the daemon, the executable as an absolute path."""
    found, relative = [], []
    ps = subprocess.run(["ps", "-axo", "pid=,uid=,comm="], capture_output=True, text=True).stdout
    for line in ps.splitlines():
        pid, uid, exe = line.split(None, 2)
        pid = int(pid)
        if int(uid) != os.getuid() or pid == os.getpid():
            continue
        if exe.startswith("/"):
            found.append((pid, exe))
        else:  # started by a relative path: ps shows it as given, so it is relative to the process's cwd
            relative.append((pid, exe))
    cwds = proc_cwds([pid for pid, _ in relative])
    return found + [(pid, os.path.join(cwds.get(pid) or "/nonexistent", exe)) for pid, exe in relative]


def app_pids(p, path, exes=None):
    """Copies of the project's app running from this worktree: your processes whose executable lies in
    <path>/<process>. Matched on the executable, literally: `pkill -f` matched a regex against every
    command line, so it also hit a shell, editor or `tail` that merely named the path. exes is my_exes(),
    passed in when the caller checks several worktrees, so the process table is read once."""
    if not (p and p.process and path):
        return []
    app = os.path.realpath(Path(path) / p.process)
    if not within(app, path):  # an absolute or ../ process would reach outside the worktree
        return []
    return [pid for pid, exe in (my_exes() if exes is None else exes) if within(exe, app)]


APP_CMD_TIMEOUT = 120  # seconds an [app] running or stop command may take


def app_cmd(p, cmd, path):
    """Run an [app] running or stop command in the worktree. Returns its exit code; 1 if it can't run there."""
    if not Path(path).is_dir():
        return 1
    try:
        return subprocess.run(cmd, shell=True, cwd=path, capture_output=True, timeout=APP_CMD_TIMEOUT,
                              env=dict(os.environ, OPS_REPO=str(p.repo))).returncode
    except subprocess.TimeoutExpired:
        print(f"{p.name}: [app] command timed out in {path}: {cmd}", flush=True)
        return 1


def kill_app(p, path):
    """Quit any copy of the project's app running from this worktree: its [app] stop command, or else SIGTERM
    to the processes app_pids finds."""
    if not (p and path):
        return
    if p.stop_cmd:
        if not p.running_cmd or app_running(p, path):  # with a running command, stop only where it runs
            app_cmd(p, p.stop_cmd, path)
        return
    for pid in app_pids(p, path):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def app_running(p, path, exes=None):
    """The project's app runs from this worktree: its [app] running command exits 0, or else app_pids finds it."""
    if p and p.running_cmd:
        return bool(path) and app_cmd(p, p.running_cmd, path) == 0
    return bool(app_pids(p, path, exes))


def launch_app(p, path):
    if not p.launch:
        raise ValueError(f"{p.name} has no [app] launch command")
    if p.artifact and not (Path(path) / p.artifact).exists():
        raise ValueError("there is no build there yet")
    # Setup rewrites files the app serves (an npm install in node_modules): an app started while it runs, or after
    # it failed half way, serves a broken tree. Launch only once every setup command has finished there.
    if setup_running(path):
        raise ValueError(f"wait: [worktree] setup is still running in {path}")
    if unprepared(path):
        raise ValueError(f"[worktree] setup has not succeeded in {path}: Play dev or a retry runs it again")
    if p.stop_cmd:  # a server-style app holds fixed ports, so only one worktree can serve: stop the others
        tasks = q(f"SELECT worktree FROM tasks WHERE project=? AND worktree IS NOT NULL AND state NOT IN "
                  f"({','.join('?' * len(ENDED))})", (p.name, *ENDED))
        for o in [p.main_wt, p.dev_wt] + [Path(t["worktree"]) for t in tasks]:
            if o.is_dir() and not within(o, path):
                kill_app(p, o)
    subprocess.Popen(p.launch, shell=True, cwd=path, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- Play main and Play dev
# The card's Play buttons run [app] launch in <worktrees>/main or <worktrees>/dev. Play dev first prepares the dev
# worktree (the [worktree] clone files and setup, once) and runs [app] build there when dev has moved since its
# last build. That can take minutes, so a Play runs in a thread, and PLAYS holds what it is doing for the card.
# The steps run in order, each to its end: setup, then build, then launch. When setup or build is to run, a
# running app is stopped first and the launch starts it fresh, so no server serves a tree rewritten under it.

PLAYS = {}  # project name -> {"where": "main"|"dev", "phase": what it does now or None, "error": why it failed}
PLAYS_LOCK = threading.Lock()


def play(p, where):
    """Start a Play of p's main or dev in the background. Raises if it can't start."""
    if not p.launch:
        raise ValueError(f"{p.name} has no [app] launch command")
    if where == "main" and not (p.main_wt.is_dir() and (not p.artifact or (p.main_wt / p.artifact).exists())):
        raise ValueError(f"there is no build of {p.main} yet: Promote to build it")
    with PLAYS_LOCK:
        if (PLAYS.get(p.name) or {}).get("phase"):
            raise ValueError(f"wait: already {PLAYS[p.name]['phase']}")
        PLAYS[p.name] = {"where": where, "phase": f"starting {p.dev if where == 'dev' else p.main}", "error": None}
    threading.Thread(target=play_run, args=(p, where), daemon=True).start()


def play_run(p, where):
    def phase(text):
        PLAYS[p.name] = {"where": where, "phase": text, "error": None}

    def stop_first(path, name):
        """Setup or build is about to rewrite files under a running app: stop it first, so no server goes on
        serving (or caching) a tree that changes under it. The launch below starts it again from scratch."""
        phase(f"stopping {name} before it changes")
        kill_app(p, path)
    try:
        if where == "dev":
            path = p.dev_wt
            prepare = setting(f"dev_prepared:{p.name}") != "1"  # bootstrap made it without clone files and setup
            head = dev_head(p) if p.build else None
            build = p.build and (setting(f"dev_built:{p.name}") != head
                                 or (p.artifact and not (path / p.artifact).exists()))
            if prepare or build:
                stop_first(path, p.dev)
            if prepare:
                phase(f"setting up {path}")
                copy_workspace_files(p, path)
                prepare_worktree(p, path, key=f"play-{p.name}")
                set_setting(f"dev_prepared:{p.name}", "1")
            if build:
                sync_clone_files(p, path)
                phase(f"building {p.dev} at {p.show(head)}")
                log = RUNS / f"play-{p.name}" / "build.log"
                rc, tail = run_step(f"play-{p.name}", f"building {p.dev}", p.build, path, "build.log")
                if rc or (p.artifact and not (path / p.artifact).exists()):
                    raise RuntimeError(f"Building {p.dev} failed (log: {log}).\n{tail[-1500:]}")
                set_setting(f"dev_built:{p.name}", head)  # what it was at the start: a merge meanwhile rebuilds
        else:
            path = p.main_wt
            r = q("SELECT value FROM settings WHERE key=?", (f"main_prepared:{p.name}",), one=True)
            if r and r["value"] != "1":  # prepare_main is about to run setup there
                stop_first(path, p.main)
            prepare_main(p, phase, f"play-{p.name}")
            if not p.build:  # no build pins main's worktree to what was built: play main as it is now
                for r in p.parts:
                    checkout_main(p, r, p.main, check=False)
        phase(f"launching {p.dev if where == 'dev' else p.main}")
        launch_app(p, path)
        PLAYS.pop(p.name, None)
    except Exception as e:
        PLAYS[p.name] = {"where": where, "phase": None, "error": str(e)}
    stale_facts(p.name)


def stop_play(p, where):
    """Stop p's app running from its main or dev worktree, as the card's Stop button asks. An [app] stop command
    can take a while, so it runs in the background and PLAYS shows "stopping" on the card meanwhile."""
    path = p.dev_wt if where == "dev" else p.main_wt
    name = p.dev if where == "dev" else p.main
    with PLAYS_LOCK:
        if (PLAYS.get(p.name) or {}).get("phase"):
            raise ValueError(f"wait: already {PLAYS[p.name]['phase']}")
        PLAYS[p.name] = {"where": where, "phase": f"stopping {name}", "error": None}

    def run():
        try:
            kill_app(p, path)
            PLAYS.pop(p.name, None)
        except Exception as e:
            PLAYS[p.name] = {"where": where, "phase": None, "error": str(e), "stop": True}
        stale_facts(p.name)
    threading.Thread(target=run, daemon=True).start()


def dev_head(p):
    """The commit (a revs string) checked out in the dev worktree."""
    return " ".join(r.git("rev-parse", "HEAD", cwd=r.at(p.dev_wt)) for r in p.parts)


def child_commands(pid):
    """Commands a Claude process is running: its children other than caffeinate. Claude starts each
    Bash command in a process group of its own."""
    out = subprocess.run(["pgrep", "-lP", str(pid)], capture_output=True, text=True).stdout.splitlines()
    return [int(l.split()[0]) for l in out if l.split()[1:2] != ["caffeinate"]]


STOP_WAIT = 5  # seconds a process group gets to exit on SIGTERM before SIGKILL: npm ci can ignore SIGTERM


def signal_groups(groups, sig):
    for g in groups:
        try:
            os.killpg(g, sig)
        except (ProcessLookupError, PermissionError):
            pass


def group_alive(g):
    try:
        os.killpg(g, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_groups(groups, procs=(), wait=STOP_WAIT):
    """SIGTERM every group, wait up to wait seconds for them all to go, then SIGKILL what is left. procs are our
    Popens among them: polling reaps them, as a zombie leader would keep its group alive."""
    signal_groups(groups, signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline:
        for proc in procs:
            proc.poll()
        if not any(group_alive(g) for g in groups):
            return
        time.sleep(0.1)
    signal_groups([g for g in groups if group_alive(g)], signal.SIGKILL)


def killgroup(pid):
    """Stop pid's process group and its commands' groups, so no test run or build is left orphaned.
    Only for a pid that is surely ours: a stale one may by now be Chrome, a terminal shell, anything."""
    if pid:
        kill_groups(child_commands(pid) + [pid])


def stop_many(procs):
    """Stop processes we started, unless they already exited (a pid may then belong to someone else), all at once:
    SIGTERM, up to STOP_WAIT seconds, then SIGKILL."""
    live = [proc for proc in procs if proc and proc.poll() is None]
    groups = [g for proc in live for g in child_commands(proc.pid) + [proc.pid]]
    if groups:
        kill_groups(groups, live)


def stop(proc):
    """Stop a process we started, unless it already exited (its pid may then belong to someone else)."""
    stop_many([proc])


def started_at(pid):
    """When pid started, as ps prints it; '' if there is no such process. With the pid, this names one
    process: macOS reuses pids, and they wrap within hours when builds and tests run."""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def track(tid, proc):
    PROCS[tid] = proc
    upd(tid, pid=proc.pid, pid_start=started_at(proc.pid))


def untrack(tid):
    PROCS.pop(tid, None)
    upd(tid, pid=None, pid_start=None)


def own_pid(t):
    """The task's recorded pid if that process is still the one this daemon (or the last) started."""
    pid = t["pid"]
    return pid if pid and t["pid_start"] and started_at(pid) == t["pid_start"] else None


# ---------------------------------------------------------------- guard: sessions stop only their own processes
# Every Claude session the daemon starts with a shell (workers, project chats) runs each
# Bash command past `opsd.py guard` first, a PreToolUse hook. It refuses commands that would stop or quit a
# process the session did not start. In task #15 a worker's `pkill -f 'opsd.py serve' -U $(id -u) -n` killed
# Chrome, the terminal and the live daemon. It is a net for slips, not a sandbox: a script can still call
# kill() itself, so the briefs state the rule too.

GUARD_RULE = ("Stop only processes you started in this session, by the pid you got when you started them "
              "($!, or a pidfile your program writes). Never stop a process by name or pattern: a pattern also "
              "matches the user's apps, terminals and other workers. If a process of yours will not stop, "
              "leave it and say so.")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
WRAPPERS = {"env", "nohup", "nice", "time", "command", "exec", "builtin", "noglob", "caffeinate", "timeout",
            "gtimeout"}
PID_FINDERS = {"pgrep", "pidof", "lsof", "fuser", "ps"}
LAUNCHCTL_READ = {"print", "list", "blame", "getenv", "help", "version", "print-disabled", "print-cache"}


def guard_settings(root, p=None, chat=False, guard=True):
    """--settings for a Claude session: run each Bash command past the guard. root is the session's worktree;
    a process working or installed there counts as the session's own. None: only its descendants do. With p, the
    project of an auto-mode session (a worker, or a chat when chat), add auto-mode rules for it (see auto_mode).
    guard False (a project chat, attended like the CLI): no guard hook, only the auto-mode rules."""
    cmd = [sys.executable, str(CODE / "opsd.py"), "guard", "--daemon", str(os.getpid()), "--port", str(PORT)]
    if root:
        cmd += ["--root", str(root)]
    hook = {"type": "command", "command": " ".join(shlex.quote(a) for a in cmd)}
    out = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}} if guard else {}
    if p:
        out["autoMode"] = auto_mode(p, root, chat)
    return json.dumps(out)


# Auto mode's classifier judges each step against the owner's own autoMode settings; Claude Code appends these
# lists to theirs. The owner's environment may name another project's repo as the trusted one and treat main as
# protected, so in task #300 a worker's own repo looked untrusted and a commit the owner approved in their
# checkout was denied as [Git Destructive]. These say where the session works and what its brief and the owner's
# answers may ask for. They add no soft_deny and drop no default rule.

def auto_mode(p, root, chat):
    """The autoMode environment and allow lists for a session of project p in worktree root (None: p's repo)."""
    repos = list(dict.fromkeys(str(r) for r in [p.repo] + [r.src for r in p.parts]))
    environment = [
        f"**claude-ops session**: This session is a claude-ops {'chat' if chat else 'worker'} for project "
        f"`{p.name}`, started by the owner's claude-ops task queue.",
        f"**Trusted repos** (claude-ops project `{p.name}`): the project's checkout {p.repo}"
        + (f" and every repo in its workspace ({', '.join(repos)})" if len(repos) > 1 else "")
        + f", its worktrees under {p.wt}" + (f" (this session's is {root})" if root else "")
        + f", and their origin remotes. `{p.main}` and `{p.dev}` are ordinary branches here, not protected.",
        f"**claude-ops state**: {DATA} is the queue's own state and config, owned by the same user.",
    ]
    allow = [
        "claude-ops Trusted Repo Work: Editing, committing and pushing in any claude-ops trusted repo listed in "
        "Environment, on any branch (`main` included), when the task brief or the owner asks for it. A plain "
        "push, not a force push.",
        f"claude-ops Project Config: Editing `{REPO_CONFIG}` in a project repo, or "
        f"`{PROJECTS_DIR / (p.name + '.toml')}`, is routine project config, not Self-Modification.",
        f"claude-ops CLI: Running `opsd.py` subcommands ({CODE / 'opsd.py'} export-config, adopt-workspace-root, "
        "add, send and the like).",
        f"claude-ops Owner Answers: A user turn that starts with `{ANSWERED}` quotes the question this session "
        "asked the owner and gives the owner's answer. It counts as the user explicitly approving the action that "
        "question named.",
    ]
    return {"environment": environment, "allow": allow}


def shell_segments(cmd):
    """The simple commands in a shell command line, as word lists. Pipelines, lists, subshells and command
    substitutions each count, and a shell's -c string is split again. Redirections are dropped."""
    cmd = cmd.replace("`", " $ ( ").replace("\n", " ; ")  # `x` reads as $(x, enough to split it out
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    segs, cur, skip = [], [], False
    for tok in lex:
        if tok and set(tok) <= set(";&|()<>"):
            if "<" in tok or ">" in tok:  # redirection: drop its target, and an fd number before it (2>)
                skip = True
                if cur and cur[-1].isdigit():
                    cur.pop()
                continue
            segs.append(cur)
            cur = []
        elif skip:
            skip = False
        else:
            cur.append(tok)
    segs.append(cur)
    out = []
    for words in segs:
        while words and (re.fullmatch(r"\w+=.*", words[0]) or Path(words[0]).name in WRAPPERS):
            first = Path(words.pop(0)).name
            while words and (words[0].startswith("-") or (first in ("timeout", "gtimeout") and
                                                          re.fullmatch(r"[\d.]+[smhd]?", words[0]))):
                words.pop(0)
        if not words:
            continue
        if Path(words[0]).name in SHELLS and "-c" in words[:-1]:
            out += shell_segments(words[words.index("-c") + 1])
        elif words[0] == "eval":
            out += shell_segments(" ".join(words[1:]))
        else:
            out.append(words)
    return out


def watch_stuck(proc, seen, stuck):
    """Stop a worker that printed nothing for STUCK_AFTER and started no process since: it is blocked inside
    Claude, not running a long build or test. In #179 that was a macOS privacy prompt for ~/Downloads nobody
    saw, and the task sat 'claude working' for 90 minutes. seen[0] is when its last line came; stuck gets
    how long it was silent, and run_claude turns that into a question for you."""
    while True:
        try:
            proc.wait(60)
            return
        except subprocess.TimeoutExpired:
            pass
        quiet = time.time() - seen[0]
        if quiet > STUCK_AFTER and not started_under(proc.pid, seen[0]):
            stuck.append(quiet)
            stop(proc)
            return


def started_under(pid, since):
    """True if a process below pid, other than caffeinate, started after the time since: a command it runs."""
    kids, born, now = {}, {}, time.time()
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,comm="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        f = line.split(None, 3)
        if len(f) < 4:
            continue
        d, _, hms = f[2].rpartition("-")  # [[dd-]hh:]mm:ss
        secs = sum(int(x) * m for x, m in zip(reversed(hms.split(":")), (1, 60, 3600))) + int(d or 0) * 86400
        kids.setdefault(int(f[1]), []).append(int(f[0]))
        born[int(f[0])] = (now - secs, os.path.basename(f[3]))
    todo = list(kids.get(pid, []))
    while todo:
        c = todo.pop()
        todo += kids.get(c, [])
        if born[c][1] != "caffeinate" and born[c][0] > since:
            return True
    return False


def tool_label(block):
    """A tool call as the question names it: Bash `ls ~/Downloads`, Read some/file, ..."""
    i = block.get("input") or {}
    arg = next((str(i[k]) for k in ("command", "file_path", "path", "url", "query", "pattern") if i.get(k)), "")
    return f"{block.get('name')} `{arg[:120]}`" if arg else str(block.get("name"))


def proc_table():
    """pid -> (ppid, uid) for every process."""
    out = {}
    for line in subprocess.run(["ps", "-axo", "pid=,ppid=,uid="], capture_output=True, text=True).stdout.splitlines():
        pid, ppid, uid = map(int, line.split())
        out[pid] = (ppid, uid)
    return out


def session_owns(pid, root, daemon):
    """pid was started by the Claude session this hook runs for: it descends from the session's process, or
    it runs from inside the session's worktree (a server backgrounded with & outlives the Bash call that
    started it and is re-parented to launchd, but keeps its working directory)."""
    procs = proc_table()
    if pid not in procs or procs[pid][1] != os.getuid():
        return False
    chain = []  # this hook and its ancestors: the session's Claude, the daemon, launchd
    p = os.getpid()
    while p in procs and p > 1:
        chain.append(p)
        p = procs[p][0]
    if pid in chain:
        return False
    # the session's process is the daemon's child; if the daemon is not our ancestor, credit no descendants
    top = next((p for p in chain if procs[p][0] == daemon), os.getpid())
    p = pid
    while p in procs and p > 1:
        if p == top:
            return True
        p = procs[p][0]
    if not root:
        return False
    if within(proc_cwd(pid) or "/", root):
        return True
    exe = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return exe.startswith("/") and within(exe, root)


def guard_check(cmd, root=None, daemon=0, port=PORT):
    """Why the guard refuses this Bash command, or None to let it run."""
    segs = shell_segments(cmd)
    finds_pids = any(Path(s[0]).name in PID_FINDERS for s in segs)
    for words in segs:
        name, args = Path(words[0]).name, words[1:]
        if name in ("pkill", "killall"):
            return f"`{name}` stops processes by name or pattern."
        if name == "xargs" and {Path(a).name for a in args} & {"kill", "pkill", "killall"}:
            return "`xargs kill` stops whatever a search found, which includes processes you did not start."
        if name in ("shutdown", "reboot", "halt"):
            return f"`{name}` would stop the whole Mac."
        if name in ("sudo", "doas"):
            return f"`{name}` runs as root, beyond what this session started."
        if name == "launchctl" and next((a for a in args if not a.startswith("-")), "list") not in LAUNCHCTL_READ:
            return "launchctl changes services the user runs; only `launchctl print` and `list` are allowed."
        if name == "osascript" and re.search(r"\bquit\b|System Events", " ".join(args), re.I):
            return "osascript quitting apps or driving System Events touches the user's apps."
        if name == "opsctl" and args[:1] and args[0] in ("start", "stop", "restart", "pause", "resume"):
            return f"`opsctl {args[0]}` acts on the live claude-ops service and every task it runs."
        if any(Path(a).name == "opsd.py" and words[i + 1:i + 2] == ["restart"] for i, a in enumerate(words)):
            return "`opsd.py restart` restarts the live claude-ops service and pauses every task it runs."
        if any(re.search(rf"(127\.0\.0\.1|localhost|\[::1\]):{port}\b", a) for a in args):
            return (f"port {port} is the live claude-ops dashboard; its API pauses and cancels tasks. Use the "
                    f"opsd.py commands instead: `{OPSD_CMD} schedule edit ID --body-file PATH` changes a schedule "
                    f"(also --title, --every), and `add` and `send` queue work.")
        if name != "kill":
            continue
        if args[:1] in (["-l"], ["-L"], ["-0"]) or args[:2] in (["-s", "0"], ["-n", "0"]):
            continue  # lists signal names, or only checks that the process exists
        if args[:1] in (["-s"], ["-n"]):
            args = args[2:]
        elif args[:1] and args[0].startswith("-") and args[0] != "--":
            args = args[1:]  # the signal: -9, -TERM, -s9
        if args[:1] == ["--"]:
            args = args[1:]
        for a in args:
            if a.startswith("%"):
                continue  # a job of this shell
            if re.fullmatch(r"-?\d+", a):
                if int(a) <= 0:
                    return f"`kill {a}` signals a whole process group, or every process you own."
                if not session_owns(int(a), root, daemon):
                    return f"pid {a} is not a process this session started."
            elif finds_pids:
                return "this kills pids found by a search (pgrep, lsof, ps), which match processes you did not start."
    return None


def guard(argv):
    """PreToolUse hook: read the Bash call on stdin, exit 2 with the reason to refuse it."""
    opts = dict(zip(argv[::2], argv[1::2]))
    cmd = ""
    try:
        cmd = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
        why = guard_check(cmd, opts.get("--root"), int(opts.get("--daemon", 0)), int(opts.get("--port", PORT)))
    except Exception as e:  # unsure: refuse anything that could stop a process
        why = (f"the claude-ops guard could not check this command ({e})."
               if re.search(r"kill|launchctl|osascript|opsctl|shutdown|reboot", cmd) else None)
    if why:
        print(f"Blocked by claude-ops: {why} {GUARD_RULE}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------- claude worker

def worker_prompt(p, t):
    tpl = (CODE / "worker_prompt.md").read_text()
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured: run whatever tests the repo has)"
    notes = workspace_note(p, t["branch"], is_branch_task(t)) + \
        (f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else "")
    notes += repo_config_note(p) + peer_brief(p, "task")
    base, rules, finish = p.dev, "", (f"When you finish, the queue rebases your branch onto the latest `{p.dev}`, runs "
                                      f"the merge check again,\nand merges into `{p.dev}`. If that fails, you are "
                                      "resumed with the error.")
    if is_branch_task(t):  # README: Branches and pull requests
        base = f"origin/{p.main}" + (f" (or `{p.main}` in a repo with no origin)" if p.repos else "")
        rules = (f"\n- This is a branch task: commit on `{t['branch']}`, the branch you are given, and on no other. "
                 f"Don't touch `{p.dev}` or `{p.main}`, and don't push: the queue pushes it when the owner opens a "
                 f"pull request. The branch may have commits from earlier work already; add yours on top."
                 + (f" Start each commit message with the Jira key `{t['jira']}`, as in `{t['jira']}: Fix the login "
                    f"crash`." if t["jira"] else ""))
        finish = (f"When you finish, the queue runs the merge check on `{t['branch']}` (rebased onto the latest "
                  f"`origin/{p.main}` first, until the branch has been pushed; never after), and leaves the commits on "
                  f"the branch. Nothing merges into `{p.dev}`. If the check fails, you are resumed with the error.")
    return tpl.format(id=t["id"], title=t["title"], body=t["body"], project=p.title, worktree=t["worktree"],
                      branch=t["branch"], repo=p.repo, dev=p.dev, main=p.main, port=p.port_for(t["id"]),
                      result=result_path(t), checks=checks, notes=notes, proof_dir=proof_dir(t["id"]),
                      base=base, branch_rules=rules, finish=finish)


def repo_config_note(p):
    """For the briefs of a project that keeps its config in its repo. Empty for one that doesn't."""
    if not getattr(p, "config_blob", ""):
        return ""
    where = f"the root of the `{p.repo.name}` root repo (the worktree root)" if p.repos else "the repo root"
    return (f"## This project's queue config is `{REPO_CONFIG}` in the repo\n\n"
            f"It is at {where}: repos, [worktree] clone/setup/setup_timeout, [[gate]], [app], [[deploy]], [worker] "
            f"notes and [design] notes. Change it like any file you commit. The queue reads it from `{p.main}`, so "
            f"your change takes effect only once it reaches `{p.main}`: this task is still checked by the gates in "
            f"use now. {', '.join(HOME_ONLY)} and [worker] env stay in the owner's "
            f"~/.claude-ops/projects/{p.name}.toml and can't be changed there (the queue ignores them in "
            f"{REPO_CONFIG}). A repo you add to repos must be cloned by the owner before the project's next task "
            "starts: the queue holds them until then.\n\n")


def workspace_note(p, branch=None, named=False):
    """How a workspace's worktree is laid out, for the briefs. Empty for a plain project."""
    if not p.repos:
        return ""
    if "." in p.repos:
        kids = [r for r in p.parts if not r.root]
        names = ", ".join(f"`{r.sub}/`" for r in kids)
        on = (f"Each is a git worktree on branch `{branch}`, branched from that repo's "
              f"`{f'origin/{p.main}' if named else p.dev}`. Commit in each repo you change; a repo you leave alone "
              f"is not {'pushed' if named else 'merged'}.") if branch else \
            "Each is a git worktree, detached at the commit being checked."
        return (f"## This is a workspace of {len(p.repos)} git repos\n\n"
                f"The worktree root is itself a git repo: the workspace's own files (`CLAUDE.md`, docs, `.claude/`, "
                f"`bin/`, `.gitignore`, ...) are tracked there. The other repos are folders inside it that it "
                f"gitignores: {names}. {on} Run git per repo: `git status` at the root covers only the root's own "
                f"files, `git -C {kids[0].sub if kids else '.'} status` a repo folder. Edits to the root's files are "
                f"kept only if you commit them in the root repo, like any other repo.\n\n")
    names = ", ".join(f"`{r}/`" for r in p.repos)
    on = (f"Each is a git worktree of its own repo on branch `{branch}`, branched from that repo's "
          f"`{f'origin/{p.main}' if named else p.dev}`. Commit in each repo you change; a repo you leave alone is not "
          f"{'pushed' if named else 'merged'}.") if branch else \
        "Each is a git worktree of its own repo, detached at the commit being checked."
    return (f"## This is a workspace of {len(p.repos)} git repos\n\n"
            f"The worktree root is not a git repo. It holds a copy of the workspace's own files (`CLAUDE.md`, "
            f"docs, ...) and one folder per repo: {names}. {on} Run git per repo "
            f"(`git -C {p.repos[0]} status`). Edits to files outside the repo folders are thrown away.\n\n")


def result_path(t):
    return Path(t["worktree"]) / RESULT_FILE  # inside the worktree, where auto mode lets workers write


def main_call(e):
    """The context of a main-agent call, in tokens (input, cache writes and cache reads), from its assistant event.
    0 for any other event, a subagent's, or one that never reached the API (an error message has no usage)."""
    msg = e.get("message")
    if e.get("type") != "assistant" or e.get("parent_tool_use_id") or not isinstance(msg, dict):
        return 0
    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    return sum(n for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
               if isinstance(n := u.get(k), int))


def span(s):
    return f"{round(s / 60)} min" if s < 7200 else f"{s / 3600:.0f} h"


def stale_session(t):
    """Why t's session should not be resumed, as (words for the note, words for the event), or None to resume it.
    Its prompt cache has expired, so a resume writes the whole context again at 40 times the price of rereading it,
    or its context is so large that every call is dear. Tasks from before last_call was kept always resume."""
    if not t["session_id"] or not t["last_call"]:
        return None
    gap, ctx = time.time() - t["last_call"], t["ctx_tokens"] or 0
    if gap > FRESH_AFTER:
        return f"the cache expired after {span(gap)}", f"last call {span(gap)} ago (cache expired)"
    if ctx > FRESH_ABOVE:
        return f"the context reached {ctx:,} tokens", f"context {ctx // 1000}k"
    return None


def first_brief(p, t):
    """The brief t's first turn sent, built again for a fresh session."""
    if t["kind"] == "senddev":
        return senddev_brief(p, t)
    return worker_prompt(p, t)


def handoff_note(p, t, why):
    """What a fresh session needs from the one it replaces, built by the daemon with no Claude call: the branch, the
    last summary, your messages and answers so far, and the last question. At most HANDOFF_CHARS: the diff stat is
    cut first, then the oldest messages."""
    wt = t["worktree"]

    def git(*a):
        out = []
        for r in p.parts:
            try:
                got = r.git(*a, cwd=r.at(wt), check=False)
            except OSError:
                got = ""
            out.append((f"{r.name}/:\n" if r.sub else "") + (got or "(none)"))
        return "\n".join(out)

    said = [(m["ts"], {"steer": "The user, by message", "user": "The user, asking about the task",
                       "assistant": "You replied"}.get(m["role"], m["role"]) + f": {m['text'][:2000]}")
            for m in q("SELECT role,text,ts FROM task_msgs WHERE task_id=? ORDER BY id", (t["id"],))]
    try:
        with open(RUNS / str(t["id"]) / "claude.jsonl", errors="replace") as f:
            for line in f:  # each turn's prompt is kept there; an answer is one
                if '"type": "ops"' in line:
                    o = json.loads(line)
                    turn = o.get("turn") or o.get("prompt") or ""
                    if head := next((h for h in ANSWER_HEADS if turn.startswith(h)), None):
                        said.append((o.get("ts") or 0, head + " " + turn[len(head):].strip().removesuffix(
                            ANSWER_TAIL).strip().removesuffix(ANSWER_GO).strip()[:2000]))
    except (OSError, ValueError):
        pass
    said = "\n\n".join(s for _, s in sorted(said, key=lambda x: x[0])) or "(none)"
    asked = t["question"] or next((e["text"].split(": ", 1)[1] for e in q(
        "SELECT text FROM events WHERE task_id=? AND text LIKE 'needs your input: %' ORDER BY id DESC LIMIT 1",
        (t["id"],))), "(none)")
    stat = git("diff", "--stat", f"{p.dev}...HEAD")

    def note(stat, said):
        return (f"## Handoff from your earlier session\n\nYour earlier session on this task ended (reason: {why}). "
                f"This is a fresh session; the work so far is on your branch.\n\n"
                f"Commits (`git log --oneline {p.dev}..HEAD`):\n```\n{log}\n```\n\n"
                f"Files changed (`git diff --stat {p.dev}...HEAD`):\n```\n{stat}\n```\n\n"
                f"Uncommitted (`git status --short`):\n```\n{status}\n```\n\n"
                f"Your last summary:\n{t['summary'] or '(none)'}\n\n"
                f"The user's messages and answers so far, oldest first:\n{said}\n\n"
                f"The last question you asked:\n{asked}")

    log, status = git("log", "--oneline", f"{p.dev}..HEAD"), git("status", "--short")
    over = len(note(stat, said)) - HANDOFF_CHARS
    if over > 0:  # the diff stat first, keeping its total line, then the oldest messages
        last = stat.rsplit("\n", 1)[-1]
        keep = max(0, len(stat) - over - len(last) - 20)
        stat = stat[:keep].rstrip() + "\n… (cut)\n" + last if keep else "(cut: too long)"
        over = len(note(stat, said)) - HANDOFF_CHARS
    if over > 0:
        said = "… (earlier ones cut)\n" + said[over + 30:]
    return note(stat, said)[:HANDOFF_CHARS]


def stopped(tid):
    """You paused or cancelled the task: its pipeline returns at the next check."""
    return task(tid)["state"] in STOPPED


def run_claude(p, tid, prompt, proof=False):
    """Run one headless Claude turn in the task worktree. Returns the parsed result file or None.
    The prompt goes in on stdin, which stays open until the turn's result so the user can steer it.
    proof: the recording turn (record_proof). It resumes the session as it is, takes no messages (they wait for the
    next real turn), may write in runs/<id>, stops after PROOF_MINUTES, and never pauses the task."""
    t = task(tid)
    if t["state"] in STOPPED:
        if t["state"] == "paused":  # paused before Claude got this prompt: resume with it
            upd(tid, next_prompt=prompt, start_at="claude")
        return None
    if limited():  # it would hit the usage limit at once: wait for the reset with this prompt
        if proof:
            return {"status": "failed", "summary": "the queue is waiting out the Claude usage limit"}
        limit_pause(tid, prompt)
        return None
    run_dir = RUNS / str(tid)
    run_dir.mkdir(parents=True, exist_ok=True)
    proof_dir(tid).mkdir(exist_ok=True)  # $OPS_PROOF_DIR: where the worker may write its proof report
    rp = result_path(t)
    rp.unlink(missing_ok=True)
    turn, kept = prompt, None
    if not proof and (why := stale_session(t)):  # cheaper to start over than to resume: brief, handoff note, then this turn
        kept = session_model(t, t["kind"])  # the fresh session stays on the model the old one ran
        brief = first_brief(p, t)
        prompt = "\n\n".join(x for x in (brief, handoff_note(p, t, why[0]),
                                         "## Now\n\n" + turn if turn and turn != brief else "") if x)
        upd(tid, session_id=None)
        ev(tid, f"started a fresh session: {why[1]}")
        t = task(tid)
    asked, prompt = prompt, with_images(prompt)
    acking = False  # a message was just read: its reply is the next thing the worker says
    limit = None  # rate_limit_info once the turn hits the usage limit
    cmd = ["caffeinate", "-i", CLAUDE, "-p", "--input-format", "stream-json", "--replay-user-messages",
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--permission-prompts", "none", "--strict-mcp-config",
           "--no-chrome", *PLUGIN_ARGS, "--name", f"{p.name} #{tid}", "--add-dir", str(IMAGES),
           "--settings", guard_settings(t["worktree"], p), "--add-dir", str(run_dir if proof else proof_dir(tid))]
    if t["session_id"]:
        cmd += ["--resume", t["session_id"]]
        model = session_model(t, t["kind"])
    else:  # a new session: what it starts on, until its init event names the model exactly
        model = session_model(t, t["kind"]) if kept is None else kept
        upd(tid, session_model=model, last_call=None, ctx_tokens=None)
    cmd += model_args(model) + effort_args(effort_for(t))
    rs = research_of(t)
    if rs:  # a research run's size follows its tier (RES-4)
        cmd += ["--max-budget-usd", str(RESEARCH_TIERS[research_tier(rs.get("tier"))][1])]
    port = p.port_for(tid)
    env = dict(os.environ, OPS_RESULT=str(rp), OPS_TASK_ID=str(tid), OPS_PORT=str(port), OPS_FROM=f"task:{tid}",
               OPS_DAEMON=DAEMON_URL, OPS_PROOF_DIR=str(proof_dir(tid)), **{k: str(v).format(port=port) for k, v in p.env.items()})
    upd(tid, phase="recording proof" if proof else "claude working")
    sid = t["session_id"]
    with open(run_dir / "claude.jsonl", "a") as log, open(run_dir / "claude.err", "a") as err:
        log.write(json.dumps({"type": "ops", "prompt": prompt[:2000], "ts": time.time(),
                              **({"turn": turn[:4000]} if kept is not None else {})}) + "\n")  # see handoff_note
        proc = subprocess.Popen(cmd, cwd=t["worktree"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, text=True, start_new_session=True)
        track(tid, proc)

        def end_input():  # a recording turn's stdin is not in STEER_PIPES: close it here, or claude waits for more
            if not proof:
                return close_steer(tid)
            try:
                proc.stdin.close()
            except OSError:
                pass
        seen, stuck, last_tool = [time.time()], [], "its first step"  # see watch_stuck
        threading.Thread(target=watch_stuck, args=(proc, seen, stuck), daemon=True).start()
        timed = []  # a research run that reached its tier's time limit (RES-4), or a recording its cap
        if rs or proof:
            limit_min = PROOF_MINUTES if proof else RESEARCH_TIERS[research_tier(rs.get("tier"))][0]
            timer = threading.Timer(limit_min * 60, lambda: (timed.append(limit_min), killgroup(proc.pid)))
            timer.daemon = True
            timer.start()
        try:
            send_user(proc.stdin, prompt)
            if not proof:  # a recording turn takes no messages: they wait for the turn after it (the hold branch)
                with STEER_LOCK:
                    STEER_PIPES[tid] = proc.stdin
                    INFLIGHT[tid] = []
                    for m in pending_steers(tid):  # waited for this turn: sent like a live one, after the prompt
                        text = WAITED_NOTE + with_images(m["text"])
                        send_user(proc.stdin, text)
                        INFLIGHT[tid].append((text, m["id"]))
        except OSError:
            pass  # claude died at once; its stderr says why
        for line in proc.stdout:
            seen[0] = time.time()
            log.write(line)
            log.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "assistant" and isinstance(e.get("message"), dict):
                last_tool = next((tool_label(c) for c in e["message"].get("content") or []
                                  if isinstance(c, dict) and c.get("type") == "tool_use"), last_tool)
            if e.get("session_id") and e["session_id"] != sid:  # compared here: a turn streams thousands of lines
                sid = e["session_id"]
                upd(tid, session_id=sid)
            if (m := init_model(e)) is not None:
                upd(tid, session_model=m)
            msg = e.get("message")  # a string on some system events, e.g. permission_denied
            content = msg.get("content") if isinstance(msg, dict) else None
            # content is a list of blocks, or a plain string (user events, and maybe others): read both alike
            blocks = ([{"type": "text", "text": content}] if isinstance(content, str) else
                      [c for c in content if isinstance(c, dict)] if isinstance(content, list) else [])
            if e.get("is_api_error_message"):  # e.g. the usage limit: the turn ends here, so say why
                err.write("".join(c.get("text") or "" for c in blocks) + "\n")
                err.flush()
            if (hit := limit_of(e)) is not None:
                limit = {**hit, **(limit or {})}  # the first event says when it resets; the result doesn't
            if (used := usage_of(e)) is not None:
                usage_seen(used)
            if ctx := main_call(e):
                upd(tid, last_call=time.time(), ctx_tokens=ctx)
            if e.get("type") == "user" and e.get("isReplay") and isinstance(content, str):
                with STEER_LOCK:
                    sent = INFLIGHT.get(tid, [])
                    mid = next((m for txt, m in sent if txt == content), None)
                    if mid:
                        sent.remove((content, mid))
                if mid:
                    q("UPDATE task_msgs SET delivered=? WHERE id=?", (time.time(), mid))
                    ev(tid, "the worker got your message")
                    acking = True
            elif e.get("type") == "assistant" and not e.get("parent_tool_use_id") and acking:
                said = [c["text"].strip() for c in blocks if c.get("type") == "text" and (c.get("text") or "").strip()]
                if said:  # its first words after your message are its answer to it
                    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)",
                      (tid, "assistant", "\n\n".join(said), time.time()))
                    acking = False
            elif e.get("type") == "result":
                # one result per turn; a message sent at the very end adds a turn
                q("UPDATE tasks SET cost=COALESCE(cost,0)+? WHERE id=?",
                  (cost_added(e.get("session_id") or sid, e.get("total_cost_usd")), tid))
                end_input()  # messages already written still get their turn before claude exits
        end_input()
        proc.wait()
        if rs or proof:
            timer.cancel()
        untrack(tid)
        with STEER_LOCK:
            INFLIGHT.pop(tid, None)  # written but never read: still pending, so the next turn gets them
    kill_app(p, t["worktree"])
    if timed and proof and not stopped(tid):
        return {"status": "failed", "summary": f"the recording turn ran past {timed[0]} min, so it was stopped"}
    if timed and not stopped(tid) and not rp.exists():  # a result written just in time still counts
        ev(tid, f"stopped at the {rs.get('tier')} time limit ({timed[0]} min)")
        return {"status": "failed", "summary": f"stopped at the {rs.get('tier')} time limit ({timed[0]} min)"}
    if stuck and not stopped(tid):
        return {"status": "needs_input", "summary": f"stuck on {last_tool}",
                "question": f"The worker was stuck for {stuck[0] // 60:.0f} min on {last_tool}, with no command "
                            "running, so it was stopped. On a Mac this is almost always a privacy prompt for Claude "
                            "Ops (Downloads, Desktop, Documents, data from other apps, App Management) waiting on "
                            "the screen. Allow it there, or give Claude Ops Full Disk Access in System Settings → "
                            "Privacy & Security, then answer to resume the worker."}
    if limit is not None:
        until = limit_hit(limit)
        if not rp.exists() and proof:
            return {"status": "failed", "summary": "the recording turn hit the Claude usage limit"}
        if not rp.exists():  # cut off mid-turn: wait for the reset, then resume the session
            ev(tid, f"hit the Claude usage limit; resumes after {clock(until)}")
            limit_pause(tid, None if task(tid)["session_id"] else asked)
            return None
    if rp.exists():
        try:
            return json.loads(rp.read_text(), strict=False)  # workers often put raw newlines in the summary
        except ValueError:
            return {"status": "failed", "summary": f"{RESULT_FILE} was not valid JSON"}
    return None


# ---------------------------------------------------------------- steering (messages to a working worker)
# A message you send while a task is working goes straight into the worker's stdin; Claude reads it after its
# current step. When no worker is running (queued, checking, between turns) it waits, and the next worker turn
# gets it as its own user message right after the prompt. Either way it counts as delivered only once Claude
# replays it as read, and the worker's next words are its reply. A task with an unread message never merges:
# the worker is resumed with it first.

STEER_LOCK = threading.Lock()  # guards the two dicts below and the step from gating to merging
STEER_PIPES = {}  # task id -> stdin of its worker, open while the worker can take a message
INFLIGHT = {}  # task id -> [(text written to stdin, message id)], until claude replays it back as read
STEER_STATES = ("queued", "running", "gating", "paused")  # a worker is working or will be: messages steer it
STEER_NOTE = "[ops: a message from the user on the dashboard, sent while you work]\n\n"
WAITED_NOTE = ("[ops: a message from the user on the dashboard, sent while you were not running. Answer it first, "
               "before anything else you were about to say]\n\n")


def send_user(pipe, text):
    pipe.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    pipe.flush()


def close_steer(tid):
    with STEER_LOCK:
        pipe = STEER_PIPES.pop(tid, None)
    if pipe:
        try:
            pipe.close()
        except OSError:
            pass


def pending_steers(tid):
    return q("SELECT id,text FROM task_msgs WHERE task_id=? AND role='steer' AND delivered IS NULL ORDER BY id",
             (tid,))


def task_steer(tid, text):
    """Send a message to the task's worker: now if it is running, else at its next turn."""
    if not text:
        raise ValueError("say something")
    with STEER_LOCK:
        t = task(tid)
        pipe = STEER_PIPES.get(tid)
        if is_branch_task(t) and (t["state"] in ("on_branch", "pr") or (t["state"] == "needs_input" and
                                                                          t["phase"] in (PR_RED, PR_CLOSED))):
            # README: Branches and pull requests. The worker resumes with it, the gate runs, and the queue pushes
            # to an open PR: how you pass on review comments
            q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "steer", text, time.time()))
            upd(tid, state="queued", phase="", question=None, start_at="claude", rounds=0,
                next_prompt=steered_prompt("after your work was on its branch"))
            ev(tid, "you sent a message: " + text[:200])
            return
        if t["state"] not in STEER_STATES:
            raise ValueError(f"the worker is not working (task is {t['state']})")
        if t["kind"] == "deploy":
            raise ValueError("a deploy runs its own command; there is no worker to talk to")
        if t["kind"] == "release" and not pipe:
            raise ValueError("the release has no worker running right now")
        mid = q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "steer", text, time.time()))
        if pipe:
            try:
                send_user(pipe, STEER_NOTE + with_images(text))
                INFLIGHT.setdefault(tid, []).append((STEER_NOTE + with_images(text), mid))
            except OSError:
                pass  # the worker just stopped; the message waits for its next turn
    ev(tid, "you sent a message: " + text[:200])


def steered_prompt(when):
    return (f"The user sent you new messages {when}. They come next, each as its own message. Do what they ask, "
            "re-run the checks, commit, and write $OPS_RESULT again. If they need no change, just write $OPS_RESULT "
            "again.")


# ---------------------------------------------------------------- gate

def run_step(key, label, cmd, cwd, log_name, env=None, timeout=None, pidkey=None):
    """Run a shell step in cwd, output appended to runs/<key>/<log_name>. Returns (exit code, output tail); the
    exit code is None if it ran past timeout seconds, and then its process group is stopped.
    An int key is a task id: the step shows as its phase and can be cancelled. pidkey: a setting that holds the
    step's pid and start time while it runs, so a later daemon can find it."""
    is_task = isinstance(key, int)
    if is_task:
        upd(key, phase=label)
    log = RUNS / str(key) / log_name
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"\n==== {time.ctime()} {label}: {cmd}  (in {cwd})\n")
        f.flush()
        start = f.tell()
        proc = subprocess.Popen(["caffeinate", "-i", "/bin/bash", "-lc", cmd], cwd=cwd, stdout=f,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)
        STEPS[proc] = cwd
        if is_task:
            track(key, proc)
        if pidkey:
            set_setting(pidkey, f"{proc.pid} {started_at(proc.pid)}")
        try:
            rc = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            stop(proc)
            proc.wait()
            f.write(f"\n==== timed out after {timeout / 60:g} min; stopped\n")
            rc = None
        finally:
            STEPS.pop(proc, None)
        if pidkey:
            set_setting(pidkey, "")
        if is_task:
            untrack(key)
    with open(log, errors="replace") as f:
        f.seek(start)
        tail = f.read()[-3000:]
    return rc, tail


def gate(p, tid, wt):
    """Run every gate step. Returns (ok, detail of the first failure)."""
    for i, g in enumerate(p.gate, 1):
        if stopped(tid):
            return False, "stopped"
        rc, tail = run_step(tid, f"gate {i}/{len(p.gate)}: {g['name']}", g["run"], wt, "gate.log")
        if stopped(tid):
            return False, "stopped"
        if rc:
            return False, failure_detail(Path(wt), g, rc, tail)
    return True, ""


def failure_detail(wt, g, rc, tail):
    lines = []
    if g.get("report") and (wt / g["report"]).exists():
        try:
            for case in ET.parse(wt / g["report"]).iter():
                if case.tag not in ("test-case", "testcase"):  # NUnit and JUnit
                    continue
                fail = case.find("failure")
                if case.get("result") == "Failed" or fail is not None:
                    msg = (case.findtext("failure/message") or (fail.get("message") if fail is not None else "")
                           or (fail.text if fail is not None else "") or "").strip()
                    lines.append(f"FAILED {case.get('fullname') or case.get('name')}: {msg[:400]}")
        except ET.ParseError:
            pass
    if g.get("log") and (wt / g["log"]).exists():
        text = (wt / g["log"]).read_text(errors="replace").splitlines()
        if g.get("errors"):
            lines += [l for l in text if g["errors"] in l][:20]
        if not lines:
            lines += text[-40:]
    if not lines:
        lines = tail.splitlines()[-40:]
    return f"`{g['run']}` exited {rc}\n" + "\n".join(lines[:60])


# ---------------------------------------------------------------- proof video
# A held task, in a project with [app] launch, gets a short video of what it changed before it goes to review: the
# worker's session is resumed once more (run_claude with proof) to launch the build, record it and stop it, and the
# Try card plays runs/<id>/proof.mp4. One recording at a time, queue-wide (there is one screen), each capped at
# PROOF_MINUTES. Nothing about it ever blocks: no video, for whatever reason, is an event and the task goes to
# review as before. Send back deletes the video; the next round records a new one.
# A worker may instead write a proof report: runs/<id>/proof/report.md ($OPS_PROOF_DIR) with its clips and
# screenshots next to it, flat. With one, the recording turn is skipped; Send back deletes the folder too.

PROOF_LOCK = threading.Lock()  # one screen: one recording turn at a time
PROOF_MINUTES = 10


def proof_files(tid):
    """(the video, its one-line note) of a task."""
    return RUNS / str(tid) / "proof.mp4", RUNS / str(tid) / "proof.txt"


def proof_watch_file(tid):
    """What to check in the proof video, as a JSON list of lines (the recording turn's "watch")."""
    return RUNS / str(tid) / "proof_watch.json"


def clear_proof(tid, report=False):
    """Delete the proof video, its note and watch list; with report, the proof folder (its report) as well."""
    for f in (*proof_files(tid), proof_watch_file(tid)):
        f.unlink(missing_ok=True)
    if report:
        shutil.rmtree(proof_dir(tid), ignore_errors=True)


PROOF_VIDEOS = {".mp4", ".webm"}
PROOF_MEDIA = PROOF_VIDEOS | {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def proof_dir(tid):
    """A task's proof folder: the worker's report.md and the clips and screenshots it embeds ($OPS_PROOF_DIR)."""
    return RUNS / str(tid) / "proof"


def proof_report(tid):
    """True when the task's worker wrote a proof report (proof/report.md)."""
    return (proof_dir(tid) / "report.md").is_file()


def proof_report_counts(tid):
    """(videos, images) in the proof folder."""
    fs = [f.suffix.lower() for f in proof_dir(tid).iterdir() if f.is_file()] if proof_dir(tid).is_dir() else []
    return sum(x in PROOF_VIDEOS for x in fs), sum(x in PROOF_MEDIA - PROOF_VIDEOS for x in fs)


def proof_clip(tid):
    """The first video the proof report embeds that is in the folder, as {name, alt}, for the Try card when there
    is no proof.mp4. None without one."""
    try:
        text = (proof_dir(tid) / "report.md").read_text(errors="replace")[:FILE_MAX]
    except OSError:
        return None
    for alt, url in re.findall(r"!\[([^\]]*)\]\(<?([^\s()<>]+)>?\)", text):
        name = unquote(re.sub(r"^\./", "", url.split("#")[0].split("?")[0]))
        if Path(name).suffix.lower() in PROOF_VIDEOS and proof_path(tid, name)[0]:
            return {"name": name, "alt": " ".join(alt.split())[:200]}
    return None


def proof_path(tid, name):
    """(path, None) for name, a file of the task's proof folder, or (None, why not). Flat names only: no /, no ..,
    no hidden file; report.md or a picture or video, at most FILE_MAX bytes for text and MEDIA_MAX for media."""
    if not re.fullmatch(r"[\w][\w.-]*", name or "") or ".." in name:
        return None, f"Not a proof file name: {name}"
    ext = Path(name).suffix.lower()
    if name != "report.md" and ext not in PROOF_MEDIA:
        return None, f"Not a proof report or media file: {name}"
    folder = proof_dir(tid)
    path = folder / name
    if not path.is_file() or not within(path, folder):
        return None, f"No such proof file: {name}"
    if path.stat().st_size > (MEDIA_MAX if ext in PROOF_MEDIA else FILE_MAX):
        return None, f"Over {50 if ext in PROOF_MEDIA else 1} MB: {name}"
    return path, None


def proof_watch(tid):
    """The proof video's watch list (["0:05 tap Nearby — …", …]), or None when there is no video or no list."""
    if not proof_files(tid)[0].is_file():
        return None
    try:
        w = json.loads(proof_watch_file(tid).read_text())
    except (OSError, ValueError):
        return None
    return clean_lines(w) or None


def clean_lines(x, n=12, width=300):
    """A list of non-empty one-line strings, at most n of them, each cut to width; a lone string counts as one."""
    xs = [x] if isinstance(x, str) else x if isinstance(x, list) else []
    return [" ".join(str(v).split())[:width] for v in xs if isinstance(v, (str, int, float)) and str(v).strip()][:n]


REVIEW_KEYS = ("fixed", "before", "after")


def clean_review(r):
    """A done result's "review" as {fixed, before, after, check} (README: the review report): strings cut to 2000,
    check a list of lines. None when nothing is in it."""
    if not isinstance(r, dict):
        return None
    out = {k: str(r[k]).strip()[:2000] for k in REVIEW_KEYS if isinstance(r.get(k), str) and r[k].strip()}
    if check := clean_lines(r.get("check")):
        out["check"] = check
    return out or None


def review_of(t):
    """A task's stored review report as a dict, or None."""
    try:
        return clean_review(json.loads(t["review"])) if t and t.get("review") else None
    except ValueError:
        return None


def proof_note(tid):
    """The one line saying what the proof video shows, or None when there is no video or no note."""
    video, note = proof_files(tid)
    if not video.is_file():
        return None
    try:
        return note.read_text(errors="replace").strip()[:300] or None
    except OSError:
        return None


def screen_recording_granted():
    """macOS Screen Recording permission for the daemon's TCC identity (Claude Ops.app), which workers inherit.
    A preflight only: it never shows a prompt, which would hang a worker with nobody there to answer it."""
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return False


def is_mp4(path):
    try:
        with open(path, "rb") as f:
            return f.read(12)[4:8] == b"ftyp"
    except OSError:
        return False


def proof_prompt(p, t, granted):
    video = proof_files(t["id"])[0]
    r = review_of(t) or {}
    said = "".join(f"- {label}: {r[k]}\n" for k, label in (("fixed", "What was fixed"), ("before", "Before"),
                                                            ("after", "Now")) if r.get(k))
    said += "".join(f"- Check: {c}\n" for c in r.get("check", []))
    report = (("Your review report for this task says:\n\n" + said + "\nRecord so that each of these is visible in the "
               "video, in that order: show the change doing what \"Now\" says, and every check.\n\n") if said else
              "Record so that the owner can see the change do what it should, one thing at a time.\n\n")
    if p.stop_cmd:
        stop = f"run the project's stop command in the worktree: `{p.stop_cmd}`"
    elif p.process:
        stop = f"stop the app you started (its executable is `{p.process}` in this worktree) by its pid"
    else:
        stop = "stop the processes you started, by their pids"
    native = ("- A native app: `screencapture -v -V <seconds> <file>.mov`, limited to the app's window (`-l <window "
              "id>`, which CGWindowListCopyWindowInfo gives you) or else its display (`-D <n>`), then convert it to "
              "MP4 the same way.\n" if granted else
              "- A native app: Screen Recording is not granted to Claude Ops, so `screencapture` would hang on a "
              "macOS prompt. Do not run it: skip, with the reason \"Screen Recording not granted to Claude Ops\".\n")
    return (f"The queue checked your work on task #{t['id']} ({t['title']}). It is held for the owner to review "
            "before it merges. First, record a short video that shows the behaviour this task targets, so the owner "
            "can watch it instead of launching the build.\n\n"
            + report +
            "This turn only records: change no tracked file and commit nothing. Keep scratch files (scripts, raw "
            f"recordings) in `{video.parent}`, not in the worktree.\n\n"
            f"1. Launch the build from this worktree with the project's launch command: `{p.launch}`. It is already "
            "built.\n"
            "2. Drive it to show what this task changed. Never post OS-level mouse or keyboard events and never take "
            "focus: the owner is using this Mac. Drive a web app from the headless browser that records it.\n"
            f"3. Record 10 to 60 seconds and save it as `{video}`: H.264 MP4, so a browser `<video>` plays it.\n"
            "- A web app (it serves on a port): headless Playwright with `record_video_dir`, then "
            f"`ffmpeg -i <in> -c:v libx264 -pix_fmt yuv420p -movflags +faststart {video}`.\n"
            + native +
            "- Use whatever is installed. If neither way works, don't install anything large: skip, and say why.\n"
            f"4. Stop the app: {stop}. Never `pkill`, `killall` or any pattern.\n"
            f"5. As your last action, write JSON to $OPS_RESULT ({result_path(t)}):\n"
            '- Recorded: `{"status": "done", "proof": "<one line: what the video shows>", "watch": ["0:05 tap Nearby — '
            'the list shows Chicago players", ...]}`\n'
            "  `proof` is the caption. `watch` lists what to check in the video, in order, one short line each, with "
            "a rough timestamp where you can. Plain language, short sentences.\n"
            '- Not recorded: `{"status": "skipped", "summary": "<why, in one line>"}`\n\n'
            f"You have {PROOF_MINUTES} minutes; the queue stops this turn then.")


def record_proof(p, tid):
    """The step "recording proof" of a held task: resume its worker to record runs/<id>/proof.mp4. Logs why when
    there is no video, and never fails the task. Returns early when the task is paused or cancelled. Skipped when the
    worker wrote a proof report: that is the task's proof."""
    clear_proof(tid)
    if proof_report(tid):
        videos, images = proof_report_counts(tid)
        return ev(tid, f"proof report: {videos} videos, {images} images")
    proof_files(tid)[0].parent.mkdir(parents=True, exist_ok=True)
    granted = screen_recording_granted()
    if not granted and not (p.running_cmd or p.stop_cmd):  # not a server-style app: only screencapture can record it
        return ev(tid, "no proof video: Screen Recording not granted to Claude Ops")
    upd(tid, phase="waiting to record proof")
    while not PROOF_LOCK.acquire(timeout=2):
        if stopped(tid):
            return
    try:
        if stopped(tid):
            return
        upd(tid, phase="recording proof")
        res = run_claude(p, tid, proof_prompt(p, task(tid), granted), proof=True)
    except Exception as e:  # a recording must never fail the task
        res = {"status": "failed", "summary": f"error: {e}"}
    finally:
        PROOF_LOCK.release()
    if stopped(tid):
        return
    video, note = proof_files(tid)
    res = res if isinstance(res, dict) else {}
    if res.get("status") in ("done", None) and video.is_file() and is_mp4(video):  # None: saved it, wrote no result
        line = (str(res.get("proof") or "").strip() or proof_note(tid) or "").splitlines()
        note.write_text((line[0][:300] if line else "") + "\n")
        if watch := clean_lines(res.get("watch")):
            proof_watch_file(tid).write_text(json.dumps(watch))
        return ev(tid, "proof video recorded" + (f": {line[0][:200]}" if line else ""))
    if not res:
        why = "the worker stopped without writing a result"
    elif res.get("status") != "done":
        why = str(res.get("summary") or res.get("question") or "").strip() or f"the worker reported {res.get('status')}"
    else:
        why = "proof.mp4 is not an MP4 file" if video.is_file() else "the worker saved no proof.mp4"
    clear_proof(tid)
    ev(tid, f"no proof video: {why.splitlines()[0][:300]}")


# ---------------------------------------------------------------- task pipeline

def ensure_worktree(p, tid):
    t = task(tid)
    if is_branch_task(t):
        return branch_worktree(p, tid)
    if t["worktree"] and all(r.present(t["worktree"]) for r in p.parts):
        if unprepared(t["worktree"]):  # its setup failed or was stopped: run it again
            upd(tid, phase="setting up the worktree")
            prepare_worktree(p, Path(t["worktree"]), key=tid)
        return
    slug = slug_of(t["title"])
    branch = t["branch"] or f"task/{tid}-{slug}"
    path = p.wt / f"t{tid}"
    upd(tid, phase="setting up the worktree")
    copy_workspace_files(p, path)
    for r in p.root_first():
        if r.present(path):  # a workspace worktree that was cut off half made
            continue
        if r.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
            r.add_worktree(path, branch)
        else:
            r.add_worktree(path, "-b", branch, p.dev)
    prepare_worktree(p, path, key=tid)
    upd(tid, branch=branch, worktree=str(path))
    ev(tid, f"worktree {path} on {branch}")


def pipeline(tid):
    try:
        t = task(tid)
        {"release": release_pipeline, "deploy": deploy_pipeline}.get(t["kind"], _pipeline)(proj(t["project"]), tid)
    except SetupFailed as e:  # before any gate runs, as a failed gate would
        if not stopped(tid):
            fail(proj(task(tid)["project"]), tid, str(e))
    except Exception as e:  # keep the daemon alive; surface the error on the task
        if not stopped(tid):
            upd(tid, state="failed", phase="", error=f"orchestrator error: {e}", finished=time.time())
            ev(tid, f"orchestrator error: {e}")
    finally:
        THREADS.pop(tid, None)


def gate_key(p, wt):
    """What a gate pass vouches for: every repo's commit in worktree wt, plus the gate commands. None if the
    worktree has uncommitted changes, since the gate ran on those too."""
    if any(r.git("status", "--porcelain", cwd=r.at(wt)) for r in p.parts):
        return None
    return " ".join(r.git("rev-parse", "HEAD", cwd=r.at(wt)) for r in p.parts) + "|" + "|".join(g["run"] for g in p.gate)


def _pipeline(p, tid):
    t = task(tid)
    upd(tid, started=t["started"] or time.time(), error=None)
    sending = t["kind"] == "senddev"
    if sending and senddev_of(t).get("landed"):  # merged before a restart, a retry or a cancel: clear now
        return senddev_landed(p, tid, t["merged"], f"merged into {p.dev} earlier")
    if t["start_at"] == "main":  # direct_main: merged into dev before a wait, a restart or a retry; main moves now
        return land_on_main(p, tid)
    if t["start_at"] == "pr":  # a branch task's Open PR, or a step after it (README: Branches and pull requests)
        return pr_step(p, tid)
    on_branch = is_branch_task(t)
    ensure_worktree(p, tid)
    t = task(tid)
    wt = t["worktree"]
    prompt = t["next_prompt"] or ("" if sending else worker_prompt(p, t))
    need_claude = t["start_at"] == "claude"
    upd(tid, next_prompt=None, start_at="claude")
    if sending and need_claude:  # your changes go onto the branch first; a worker runs only for conflicts
        prompt = senddev_start(p, tid, prompt)
        if prompt is None:
            return
        need_claude = bool(prompt)

    while True:
        if need_claude:
            if sending and not task(tid)["session_id"] and SENDDEV_HEAD not in prompt[:300]:
                prompt = senddev_brief(p, task(tid)) + "\n\n## Now\n\n" + prompt  # a fresh worker needs the story
            res = run_claude(p, tid, prompt)
            if stopped(tid):
                return
            if res is None:
                err = (RUNS / str(tid) / "claude.err").read_text(errors="replace")[-1500:]
                return fail(p, tid, "worker stopped without writing a result.\n" + err)
            status = res.get("status")
            upd(tid, summary=res.get("summary") or task(tid)["summary"])
            if "review" in res:  # the review report, for the Try step and the task page; a later round's replaces it
                upd(tid, review=json.dumps(rv) if (rv := clean_review(res["review"])) else None)
            if res.get("todos"):  # what only you can do, for your to-do list; the task goes on regardless
                if said := todo_add(p.name, res["todos"], "task", tid):
                    ev(tid, "to-dos: " + said)
            applying = sending and not senddev_of(task(tid)).get("applied")
            if status == "split":
                if sending:
                    return fail(p, tid, "the worker asked to split a Send to dev task, which can't be split")
                return split(p, tid, res)
            if status == "needs_input" or (applying and status != "done"):  # it can't merge your changes: ask you
                return ask_owner(p, tid, res.get("question") or res.get("summary") or f"the worker reported {status}")
            if status != "done":
                return fail(p, tid, res.get("summary") or f"worker reported {status}")
            if pending_steers(tid):  # sent as the worker finished
                prompt = steered_prompt("as you finished")
                continue
            if "reports" in res:  # files it wrote for you to read: Read to-dos once it merges (RES-10)
                if said := set_reports(tid, res["reports"]):
                    ev(tid, said)
            if res.get("followups"):  # steps for after this merges, queued behind it; only a done result has them
                if ids := add_followups(p, tid, res["followups"]):
                    ev(tid, "follow-ups: " + ", ".join(f"#{i}" for i in ids))
            if applying:
                why = senddev_commit(p, tid)
                if why:
                    return ask_owner(p, tid, why)
            for r in p.parts:
                if r.git("status", "--porcelain", cwd=r.at(wt)):
                    r.git("stash", "push", "-u", "-m", "ops-autostash: uncommitted after worker", cwd=r.at(wt))
                    ev(tid, "stashed uncommitted leftovers (git stash list in the worktree"
                            + (f" of {r.name})" if r.sub else ")"))
        need_claude = True

        if not (branch_parts(p, t["branch"]) if on_branch else changed_parts(p, t["branch"])):
            if sending and not on_branch:  # applying your changes changed nothing: dev has them already
                return senddev_landed(p, tid, None, f"{p.dev} already had these changes")
            if sending:  # flow "pr": main has them already, and your checkout gets them when main moves there
                return finish(p, tid, merged=None, note=f"origin/{p.main} already had these changes: no PR to open")
            return finish(p, tid, merged=None, note="no code changes")

        # Rebase onto the latest dev, gate, then merge (or hold for review). A branch task rebases onto the latest
        # origin/main instead, and only in a repo where its branch was never pushed; then it stays on its branch.
        upd(tid, state="gating", phase=f"rebasing onto {p.main if on_branch else p.dev}")
        if p.direct_main and not on_branch:  # commits made straight to main come into dev first, so the gate checks them too
            with p.lock:
                err = catch_up_dev(p, tid)
            if err:
                return fail(p, tid, err + f"\n\nPromote once: the release has a worker merge {p.main} into "
                                          f"{p.dev}. Then Retry this task.")
        if on_branch:
            fetch_origin(p, tid)
        base = p.revs(p.dev)
        r, kept = subprocess.CompletedProcess([], 0, "", ""), []
        for part in p.parts:  # every repo, so the gate checks the latest dev of each
            if on_branch and pushed(part, t["branch"]):  # never rebase a pushed branch: new commits go on top
                kept.append(part)
                continue
            onto = base_ref(p, part) if on_branch else p.dev
            r = subprocess.run(["git", "rebase", onto], cwd=part.at(wt), capture_output=True, text=True)
            if r.returncode:
                subprocess.run(["git", "rebase", "--abort"], cwd=part.at(wt), capture_output=True)
                break
        if stopped(tid):
            return
        if r.returncode:
            if not bump_round(p, tid, "rebase conflict" + (f" in {part.name}" if part.sub else "")):
                return
            run = f"git -C {part.sub} rebase {onto}" if part.sub else f"git rebase {onto}"
            what = f"`{p.main}` (`{onto}`)" if on_branch else f"`{p.dev}`"
            prompt = (f"The {what} branch{f' of {part.name}' if part.sub else ''} moved and branch "
                      f"`{t['branch']}` no longer rebases cleanly onto it. Run `{run}`, resolve the conflicts "
                      f"so both your change and the new {p.main if on_branch else p.dev} work survive, re-run the "
                      f"checks, commit, and write $OPS_RESULT again.\n\ngit said:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
            upd(tid, state="running")
            continue
        if kept:
            ev(tid, f"not rebased: {t['branch']} is pushed already" + (f" ({', '.join(x.name for x in kept)})"
                                                                         if p.repos else ""))
        key = gate_key(p, wt)
        if key and setting(f"gated:{tid}") == key:  # approved after a pass, and nothing moved since
            ok, detail = True, ""
            ev(tid, "gate skipped: it passed on exactly these commits")
        else:
            ok, detail = gate(p, tid, wt)
        if stopped(tid):
            return
        if ok and key:
            set_setting(f"gated:{tid}", key)
        if not ok:
            ev(tid, f"gate failed: {detail.splitlines()[0]}")
            if not bump_round(p, tid, "gate failed:\n" + detail):
                return
            if on_branch and kept:
                prompt = (f"The queue ran its merge check on branch `{t['branch']}`. It is pushed already, so it was "
                          f"not rebased. It failed:\n\n{detail}\n\nFix it with new commits on top (never rebase, "
                          f"amend or force-push a pushed branch), re-run the failing step yourself, commit, and write "
                          f"$OPS_RESULT again.")
            else:
                onto = f"origin/{p.main}" if on_branch else p.dev
                prompt = (f"The queue rebased your branch onto the latest `{onto}` and ran its merge check. "
                          f"It failed:\n\n{detail}\n\nFix it in this worktree (keep the history rebased on "
                          f"{onto}), re-run the failing step yourself, commit, and write $OPS_RESULT again.")
            upd(tid, state="running")
            continue
        ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))

        if task(tid)["hold"] and not p.launch:  # no [app] launch: no Try button, so nothing to wait for
            ev(tid, "not holding for review: this project has no app to try, so it merges now")
        elif task(tid)["hold"]:
            if p.build:
                run_step(tid, "building for review", p.build, wt, "gate.log")
                if stopped(tid):
                    return
            heads = [r.git("rev-parse", "HEAD", cwd=r.at(wt)) for r in p.parts]
            if not pending_steers(tid):  # with a message waiting the worker changes things first: record after
                record_proof(p, tid)
            if stopped(tid):
                return
            if [r.git("rev-parse", "HEAD", cwd=r.at(wt)) for r in p.parts] != heads:  # it was told not to commit
                ev(tid, "the recording turn changed the branch; checking it again")
                need_claude = False
                continue
            with STEER_LOCK:
                steered = bool(pending_steers(tid))
                if not steered:
                    upd(tid, state="review", phase="")
            if steered:
                prompt = steered_prompt("while the queue checked your work")
                upd(tid, state="running")
                continue
            ev(tid, "waiting for your review")
            notify(f"{p.title}: task ready for review", f"#{tid}: {t['title']}")
            return

        if on_branch:  # no merge into dev: the commits stay on the branch, for Open PR (or a push to its PR)
            with STEER_LOCK:
                steered = bool(pending_steers(tid))
                if not steered:
                    upd(tid, phase="")
            if steered:
                prompt = steered_prompt("while the queue checked your work")
                upd(tid, state="running")
                continue
            return branch_end(p, tid)
        with p.lock:
            if stopped(tid):
                return
            if p.revs(p.dev) != base:
                ev(tid, f"{p.dev} moved during the gate; re-checking")
                need_claude = False
                continue
            with STEER_LOCK:  # a message sent from here on is refused: the task is merging
                steered = bool(pending_steers(tid))
                if not steered:
                    upd(tid, state="merging", phase=f"merging into {p.dev}")
            if steered:
                prompt = steered_prompt("while the queue checked your work")
                upd(tid, state="running")
                continue
            done = []
            for r in changed_parts(p, t["branch"]):
                save_dev_changes(p, r, tid)
                r.git("merge", "--no-ff", "-m", f"Merge task #{tid}: {t['title']}", t["branch"], cwd=r.at(p.dev_wt))
                done.append((r, r.rev(p.dev)))
            merged = " ".join(sha for _, sha in done)
            if sending:  # from here on the work is on dev: a restart or retry goes straight to clearing
                upd(tid, merged=merged, senddev=json.dumps({**senddev_of(task(tid)), "landed": True}))
        where = ", ".join(f"{r.name} {sha[:8]}" if r.sub else sha[:8] for r, sha in done)
        if sending:
            return senddev_landed(p, tid, merged, f"merged into {p.dev} at {where}")
        return land(p, tid, merged, f"merged into {p.dev} at {where}")


def land(p, tid, merged, note):
    """A merge into dev is done: finish, or with direct_main move main to it first (land_on_main)."""
    if not (p.direct_main and merged):
        return finish(p, tid, merged=merged, note=note)
    upd(tid, merged=merged, start_at="main")  # a wait, a restart or a retry goes on from here, not from the merge
    ev(tid, note)
    return land_on_main(p, tid)


def land_on_main(p, tid):
    """direct_main: move main to dev, which has this task's merge (and any that merged after it), the way a release
    does after its gate: build main if the project has an app, carry your uncommitted changes over, and wait for you
    while something only you can clear is in the way (unblock_releases goes on once it is clear). The gate is not
    run again: dev is what this task's gate passed on. One task at a time moves main (p.main_lock)."""
    t = task(tid)
    resume = t["next_prompt"]
    upd(tid, next_prompt=None, error=None, state="merging", phase=f"moving {p.main}")
    with p.main_lock:
        if stopped(tid):
            return
        deploy = exclusive_blocker(task(tid))  # it moves main too: this goes on once it has ended
        if deploy:
            upd(tid, state="queued", phase="")
            ev(tid, f"waiting for deploy #{deploy['id']}: it moves {p.main} too")
            return
        if all(any(is_ancestor(r, sha, p.main) for r in p.parts if r.has(sha)) for sha in t["merged"].split()):
            upd(tid, start_at="claude")  # a later merge already took it to main
            return finish(p, tid, merged=t["merged"], note=f"in {p.main} already")
        with p.lock:  # a commit made straight to main since: dev takes it in, or main could not fast-forward
            err = catch_up_dev(p, tid)
            commit = p.revs(p.dev)
        if err:
            return fail(p, tid, f"{p.dev} has the merge, but {p.main} could not move to it. {err}\n\nPromote "
                                f"once: the release has a worker merge {p.main} into {p.dev}.")
        why = promote_blocker(p, commit, "merge")
        if why:
            return wait_for_you(p, tid, why, WAIT)
        n = sum(len(first_parent_log(r, r.rev(p.main), sha)) for r, sha in p.pairs(commit))
        if n > 1:
            ev(tid, f"{p.main} moves {n} merges: {p.dev} had ones not in {p.main} yet")
        before = p.revs(p.main)
        try:  # your answer, when the merge needed you, guides the worker that carries your changes over
            err = promote(p, tid, commit, note=resume if resume and resume.startswith(ANSWER_HEADS) else "")
        except NeedsYou as e:
            return wait_for_you(p, tid, str(e), WAIT_CHANGES)
    if stopped(tid):
        return
    if err:
        return fail(p, tid, f"{p.dev} has the merge, but {p.main} did not move. Retry moves it again.\n{err}")
    upd(tid, start_at="claude")
    stale_facts(p.name)
    finish(p, tid, merged=t["merged"], note=f"{p.main} is now {p.show(commit)}")
    if Path(p.repo).resolve() == CODE and p.revs(p.main) != before:
        restart_when_idle(tid)


# ---------------------------------------------------------------- branches and pull requests
# (README: Branches and pull requests) A branch task works on a named branch, given or made from a Jira key, and ends
# `on_branch` instead of merging into dev. Open PR pushes it and opens a GitHub PR per repo it commits to, in the
# project's pr_order; the queue then watches them (pr_poll), and once every PR has merged, main follows origin/main.
# Every git or gh write runs in the task's own pipeline (start_at 'pr'), one task per branch at a time
# (branch_holder); the poll only reads.

PR_RED = "CI failed"  # the phase of a branch task that needs you because a PR's CI failed
PR_CLOSED = "PR closed"  # ... because a PR was closed without merging
PR_SETUP = "can't open PRs"  # ... because gh, its login, a GitHub origin or a push is in the way
PR_MAIN = "main can't follow origin"  # ... because your main has commits origin/main lacks
PR_PHASES = (PR_RED, PR_CLOSED, PR_SETUP, PR_MAIN)
PR_POLL_EVERY = 120  # seconds between polls of open PRs
GH_TIMEOUT = 120  # seconds a gh or git network call may take
PR_LOCKS = {}  # task id -> lock: its PR step and a Merge click never run gh writes at once
PR_POLLING = threading.Lock()
NET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
GITHUB_URL = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?(?P<host>[^/:@]*github[^/:]*)(?::\d+)?[:/]"
                        r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")


def pr_lock(tid):
    return PR_LOCKS.setdefault(tid, threading.Lock())


def origin_url(r):
    """origin's URL as configured (git config, not get-url, which applies insteadOf), "" with no origin."""
    return r.git("config", "--get", "remote.origin.url", check=False)


def gh_repo(r):
    """[HOST/]OWNER/NAME of repo r's origin on GitHub, for gh -R, or None when origin is not on GitHub."""
    m = GITHUB_URL.fullmatch(origin_url(r))
    if not m:
        return None
    return m.group("repo") if m.group("host") == "github.com" else f"{m.group('host')}/{m.group('repo')}"


def gh_path():
    return shutil.which("gh") or next((x for x in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
                                       if os.access(x, os.X_OK)), None)


def gh(r, *args, timeout=GH_TIMEOUT):
    """gh args... against repo r's GitHub repo, by -R: gh never touches your checkout (no local branch switch or
    delete). Returns (exit code, stdout, stderr)."""
    exe = gh_path()
    if not exe:
        return 127, "", "gh is not installed"
    try:
        res = subprocess.run([exe, *args, *(("-R", gh_repo(r)) if r and gh_repo(r) else ())], cwd=r.src if r else None,
                             capture_output=True, text=True, timeout=timeout, env={**os.environ, **NET_ENV})
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", f"gh {' '.join(args[:2])}: {e}"
    return res.returncode, res.stdout, res.stderr


def git_net(r, *args):
    """A git command that talks to origin, with a time limit and no password prompt: None, or git's error."""
    try:
        res = subprocess.run(["git", *args], cwd=r.src, capture_output=True, text=True, timeout=GH_TIMEOUT,
                             env={**os.environ, **NET_ENV})
    except subprocess.TimeoutExpired:
        return f"git {' '.join(args)} took over {GH_TIMEOUT} s"
    return None if res.returncode == 0 else (res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}")


def fetch_origin(p, tid=None):
    """git fetch origin in each repo that has an origin. A failure is logged on the task, not fatal: the branch
    work goes on from what was fetched before."""
    for r in p.parts:
        if origin_url(r) and (err := git_net(r, "fetch", "-q", "origin")) and tid:
            ev(tid, f"git fetch origin{' in ' + r.name if r.sub else ''} failed: {err.splitlines()[-1][:300]}")


def ref_ok(r, ref):
    return bool(r.git("rev-parse", "--verify", "-q", ref, check=False))


def base_ref(p, r):
    """What a branch task branches from and rebases onto in repo r: origin/main, or main for a repo with no
    origin (or one whose origin has no main yet)."""
    return f"origin/{p.main}" if origin_url(r) and ref_ok(r, f"refs/remotes/origin/{p.main}") else p.main


def pushed(r, branch):
    """Whether branch is on origin (as of the last fetch or push): then the queue never rebases it again."""
    return ref_ok(r, f"refs/remotes/origin/{branch}")


def branch_parts(p, branch):
    """The repos where branch has commits its base lacks: the ones the task commits to."""
    return [r for r in p.parts if ref_ok(r, f"refs/heads/{branch}")
            and int(r.git("rev-list", "--count", f"{base_ref(p, r)}..refs/heads/{branch}") or 0)]


def part_of(p, sub):
    return next((r for r in p.parts if r.sub == sub), None)


def branch_holder(t):
    """The other task of t's project on t's branch that t waits for, or None. One task per branch at a time: an
    earlier queued one, or any that is working on it. One that is on branch, has PRs out, ended or failed doesn't
    hold it (a PR phase that needs you doesn't either: Fix CI and messages requeue it, and then it waits its turn)."""
    if not is_branch_task(t) or not t.get("branch"):
        return None
    for o in q("SELECT id,state,phase FROM tasks WHERE project=? AND branch=? AND id!=? AND (jira IS NOT NULL OR "
               "target IS NOT NULL) ORDER BY id", (t["project"], t["branch"], t["id"])):
        if o["state"] in ("on_branch", "pr", *ENDED) or (o["state"] == "queued" and o["id"] > t["id"]):
            continue
        if o["state"] == "needs_input" and o["phase"] in PR_PHASES:
            continue
        return o
    return None


def past_branch(t):
    """Whether branch task t is on its branch or later: a task on the same branch after it may start."""
    return t["state"] in ("on_branch", "pr") or (t["state"] == "needs_input" and t.get("phase") in PR_PHASES)


def dep_waits(t, dep):
    """Whether t still waits for dep, its depends_on: until dep is done (a branch task: all its PRs merged), but a
    task on the same branch only until dep is on branch."""
    if not dep or dep["state"] == "done":
        return False
    if is_branch_task(t) and is_branch_task(dep) and dep.get("project") == t.get("project") and \
            dep.get("branch") == t.get("branch"):
        return not past_branch(dep)
    return True


def free_branch(p, r, branch, mine):
    """Detach each queue worktree of repo r that has branch checked out (a task on it that ended, failed or is on
    branch: its files stay as they are), so worktree mine can check it out. RuntimeError if a checkout of yours has
    it: git checks a branch out in one place only."""
    for w in r.git("worktree", "list", "--porcelain").split("\n\n"):
        lines = w.splitlines()
        if not lines or f"branch refs/heads/{branch}" not in lines:
            continue
        at = lines[0][len("worktree "):]
        if os.path.realpath(at) == os.path.realpath(r.at(mine)):
            continue
        if not within(at, p.wt):
            raise RuntimeError(f"{branch} is checked out in {at}: switch that checkout to another branch, then Retry")
        r.git("checkout", "-q", "--detach", cwd=at)


def start_point(p, r, branch):
    """(how, args for git worktree add / checkout -b) for a branch the repo lacks locally."""
    if pushed(r, branch):
        return f"from origin/{branch}", ["-b", branch, "--track", f"origin/{branch}"]
    base = base_ref(p, r)
    return f"new from {base}", ["-b", branch, "--no-track", base]


def branch_worktree(p, tid):
    """ensure_worktree for a branch task: its worktree has its named branch checked out in every repo. A local branch
    is used (fast-forwarded first when origin/<branch> is ahead of it), else one is made tracking origin/<branch>,
    else from origin/main (main with no origin)."""
    t = task(tid)
    branch = t["branch"]
    path = Path(t["worktree"]) if t["worktree"] else p.wt / f"t{tid}"
    upd(tid, phase="fetching origin")
    fetch_origin(p, tid)
    upd(tid, phase="setting up the worktree")
    fresh = not all(r.present(path) for r in p.parts)
    if fresh:
        copy_workspace_files(p, path)
    said = []
    for r in p.root_first():
        here = r.present(path)
        on = here and r.git("branch", "--show-current", cwd=r.at(path), check=False) == branch
        if ref_ok(r, f"refs/heads/{branch}"):
            free_branch(p, r, branch, path)
            o, head = f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"
            if ref_ok(r, o) and not is_ancestor(r, o, head) and is_ancestor(r, head, o):  # pushed to from GitHub
                if on:
                    r.git("merge", "--ff-only", "-q", o, cwd=r.at(path))
                else:
                    r.git("update-ref", head, r.rev(o), r.rev(head))
                said.append(f"{r.name + ' ' if r.sub else ''}fast-forwarded to origin/{branch}")
            how, args = "local", [branch]
        else:
            how, args = start_point(p, r, branch)
        if on:
            continue
        if here:
            r.git("checkout", "-q", *args, cwd=r.at(path))
        else:
            r.add_worktree(path, *args)
        said.append(f"{r.name + ': ' if r.sub else ''}{how}")
    if fresh or unprepared(path):
        prepare_worktree(p, path, key=tid)
    upd(tid, worktree=str(path))
    ev(tid, f"worktree {path} on branch {branch}" + (f" ({'; '.join(said)})" if said else ""))


def branch_line(p, t):
    """The summary's first line on branch: the branch and its commits per repo, then the commits themselves."""
    branch, counts, lines = t["branch"], [], []
    for r in p.parts:
        if not ref_ok(r, f"refs/heads/{branch}"):
            continue
        log = r.git("log", "--format=%h %s", f"{base_ref(p, r)}..refs/heads/{branch}").splitlines()
        if not log:
            continue
        n = f"{len(log)} commit{'s' * (len(log) != 1)}"
        local = "" if origin_url(r) else " (no origin: they stay on the local branch, no PR)"
        counts.append((f"{r.name} {n}" if r.sub else n) + local)
        lines += [f"- {r.name + ': ' if r.sub else ''}{c}" for c in log[:10]] + \
            ([f"- ... and {len(log) - 10} more"] if len(log) > 10 else [])
    return f"On branch {branch}: {', '.join(counts) or 'no commits'}.", lines


def with_branch_line(p, t, tail):
    """t's summary with the branch line on top, replacing the one an earlier round put there."""
    s = (t["summary"] or "").strip()
    if s.startswith("On branch "):
        s = s.split("\n\n", 1)[1] if "\n\n" in s else ""
    head, lines = branch_line(p, t)
    return "\n".join([f"{head} {tail}".strip(), *lines]) + (f"\n\n{s}" if s else "")


def detach_worktree(p, t):
    """Leave the branch free for the next task on it: the worktree stays, detached at the same commit."""
    wt = t["worktree"]
    for r in p.parts:
        if wt and r.present(wt) and r.git("branch", "--show-current", cwd=r.at(wt), check=False):
            r.git("checkout", "-q", "--detach", cwd=r.at(wt), check=False)


def branch_end(p, tid):
    """A branch task passed its gate (and review): its commits stay on its branch. With PRs open already, the queue
    pushes them (pr_step); else the task is on branch, and waits for Open PR."""
    t = task(tid)
    detach_worktree(p, t)
    if q("SELECT 1 FROM prs WHERE task_id=? AND number IS NOT NULL", (tid,)):
        upd(tid, start_at="pr")
        return pr_step(p, tid)
    summary = with_branch_line(p, t, "Nothing pushed yet.")
    upd(tid, state="on_branch", phase="", summary=summary, start_at="claude", question=None)
    ev(tid, summary.splitlines()[0])
    notify(f"{p.title}: task on its branch", f"#{tid}: {t['title']}")
    tell_sender(tid, f"is on branch {t['branch']}, waiting for the owner to open a PR: " + summary)


def pr_rows(tid):
    return q("SELECT * FROM prs WHERE task_id=? ORDER BY seq, id", (tid,))


def pr_name(p, row):
    r = part_of(p, row["repo"])
    return (r.name if r else row["repo"]) if row["repo"] else ""


def pr_words(p, row):
    """"ballup-dunk #12" (or "#12" in a plain project; "ballup-dunk" before it opens)."""
    name = pr_name(p, row)
    num = f"#{row['number']}" if row["number"] else ""
    return " ".join(x for x in (name, num) if x) or "the PR"


def pr_plan(p, t):
    """(repos with commits on t's branch and an origin, as [(part, seq)] in PR order; repos with commits and no
    origin). seq is the place in pr_order among them: the first, and every repo pr_order leaves out, is 0."""
    touched = branch_parts(p, t["branch"])
    local = [r for r in touched if not origin_url(r)]
    touched = [r for r in touched if r not in local]
    named = lambda r, n: r.sub == n or (r.root and n in (".", r.name))
    order = [r for n in p.pr_order for r in touched if named(r, n)]
    order = [r for i, r in enumerate(order) if r not in order[:i]]
    seqs = {r.sub: i for i, r in enumerate(order)}
    return [(r, seqs.get(r.sub, 0)) for r in order + [r for r in touched if r not in order]], local


def pr_precheck(p, parts):
    """What is missing to push and open PRs for these repos, or None: gh on PATH, logged in, origins on GitHub."""
    if not gh_path():
        return "gh, the GitHub CLI, is not on PATH: install it (brew install gh), run gh auth login, then Retry."
    rc, out, err = gh(None, "auth", "status")
    if rc:
        return f"gh is not logged in to GitHub: run gh auth login, then Retry. gh auth status said: " + \
            ((err or out).strip().splitlines() or ["nothing"])[-1][:300]
    bad = [f"{r.name or p.repo.name}'s origin ({origin_url(r) or 'none'})" for r in parts if not gh_repo(r)]
    if bad:
        return f"Not on GitHub: {', '.join(bad)}. Point origin at a GitHub repo, then Retry."
    return None


def pr_body(p, t, row, rows):
    """A PR's description: the task summary, the review report, and the task's other PRs."""
    out = []
    deps = [x for x in rows if x["seq"] < row["seq"] and x["url"]]
    out += [f"Depends on {x['url']}" for x in deps]
    s = (t["summary"] or "").strip()
    out += ["", s] if s else []
    rv = clean_review(json.loads(t["review"])) if t["review"] else None
    if rv:
        out += ["", "## Review"]
        out += [f"**{k.title()}:** {rv[k]}" for k in ("fixed", "before", "after") if rv.get(k)]
        out += ["", "**Check:**", *[f"- {c}" for c in rv.get("check") or []]] if rv.get("check") else []
    others = [x for x in rows if x["id"] != row["id"]]
    if others:
        out += ["", "## The other PRs of this change"]
        out += [f"- {pr_name(p, x) or 'repo'}: " + (x["url"] or ("opens once the PR before it has CI green"
                                                                  if x["seq"] > row["seq"] else "not open yet"))
                for x in others]
    out += ["", f"Opened by claude-ops task #{t['id']}" + (f" ({t['jira']})" if t["jira"] else "") + "."]
    return "\n".join(out).strip()


def pr_title(t):
    return f"{t['jira']}: {t['title']}" if t["jira"] else t["title"]


def pr_push(p, tid, r, branch):
    """git push -u origin <branch>, never --force. None, or why it failed."""
    err = git_net(r, "push", "-u", "origin", branch)
    if err:
        return (f"git push origin {branch}{' in ' + r.name if r.sub else ''} was refused, and the queue never "
                f"force-pushes. git said:\n{err[-1500:]}\n\nIf GitHub has commits the branch lacks, bring them in "
                f"(git pull on the branch), then Retry.")
    ev(tid, f"pushed {branch}{' in ' + r.name if r.sub else ''}")
    return None


def pr_open_one(p, t, row, rows):
    """Push and open (or adopt) row's PR. None, or why not."""
    r, branch = part_of(p, row["repo"]), t["branch"]
    if why := pr_push(p, t["id"], r, branch):
        return why
    rc, out, err = gh(r, "pr", "list", "--head", branch, "--base", p.main, "--state", "open", "--json", "number,url")
    found = None
    try:
        found = (json.loads(out) or [None])[0] if rc == 0 else None
    except ValueError:
        pass
    if found:
        number, url = int(found["number"]), found["url"]
        ev(t["id"], f"{pr_name(p, row) + ': ' if row['repo'] else ''}PR #{number} was open already for {branch}; "
                    f"watching it: {url}")
    else:
        rc, out, err = gh(r, "pr", "create", "--base", p.main, "--head", branch, "--title", pr_title(t),
                          "--body", pr_body(p, t, row, rows))
        m = re.search(r"https?://\S+/pull/(\d+)", out + err)
        if rc or not m:
            return f"gh pr create{' in ' + r.name if r.sub else ''} failed: {(err or out).strip()[-1500:]}"
        number, url = int(m.group(1)), m.group(0)
        ev(t["id"], f"opened {pr_name(p, row) + ' ' if row['repo'] else ''}PR #{number}: {url}")
    q("UPDATE prs SET number=?, url=?, state='OPEN', ci='pending', failing=NULL, opened_at=?, error=NULL WHERE id=?",
      (number, url, time.time(), row["id"]))
    for x in pr_rows(t["id"]):  # the earlier PRs' bodies link this one now
        if x["id"] != row["id"] and x["number"] and x["state"] == "OPEN":
            gh(part_of(p, x["repo"]), "pr", "edit", str(x["number"]), "--body", pr_body(p, task(t["id"]), x,
                                                                                       pr_rows(t["id"])))
    return None


def ci_of(rollup):
    """(ci, failing checks) from gh's statusCheckRollup: green once every check succeeded or was skipped (none is
    green too), red on any failure, else pending."""
    ci, failing = "green", []
    for c in rollup or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("context") or "check"
        url = c.get("detailsUrl") or c.get("targetUrl") or ""
        if c.get("__typename") == "StatusContext" or ("state" in c and "conclusion" not in c):
            v = str(c.get("state") or "").upper()
            ok, bad = v == "SUCCESS", v in ("FAILURE", "ERROR")
        else:
            v = str(c.get("conclusion") or "").upper()
            ok = v in ("SUCCESS", "SKIPPED", "NEUTRAL")
            bad = v in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
        if bad:
            ci = "red"
            failing.append({"name": name, "url": url})
        elif not ok and ci == "green":
            ci = "pending"
    return ci, failing


def pr_view(p, t, row):
    """Read one PR's state and checks from GitHub into its row. Returns the new row, or None if gh failed."""
    r = part_of(p, row["repo"])
    rc, out, err = gh(r, "pr", "view", str(row["number"]), "--json",
                      "state,mergedAt,mergeCommit,statusCheckRollup,mergeable,url")
    try:
        d = json.loads(out) if rc == 0 else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        q("UPDATE prs SET checked_at=?, error=? WHERE id=?",
          (time.time(), f"gh pr view failed: {(err or out).strip()[-300:]}", row["id"]))
        return None
    state = str(d.get("state") or "OPEN").upper()
    ci, failing = ci_of(d.get("statusCheckRollup"))
    kw = {"state": state, "ci": ci, "failing": json.dumps(failing) if failing else None, "checked_at": time.time(),
          "url": d.get("url") or row["url"]}
    if state == "MERGED" and row["state"] != "MERGED":
        kw["merged_at"] = iso_epoch(d.get("mergedAt")) or time.time()
        kw["merge_commit"] = (d.get("mergeCommit") or {}).get("oid")
        early = [x for x in pr_rows(t["id"]) if x["seq"] < row["seq"] and x["state"] != "MERGED"]
        if early:
            kw["warning"] = (f"merged on GitHub before {', '.join(pr_words(p, x) for x in early)}, out of pr_order")
        ev(t["id"], f"{pr_words(p, row)} merged" + (f": {kw['warning']}" if early else ""))
    elif state == "CLOSED" and row["state"] != "CLOSED":
        ev(t["id"], f"{pr_words(p, row)} was closed without merging")
    elif state == "OPEN" and ci != row["ci"] and ci != "pending":
        ev(t["id"], f"{pr_words(p, row)}: CI {ci}" + (": " + ", ".join(f["name"] for f in failing) if failing else ""))
    if state == "OPEN" and row["state"] == "CLOSED":
        kw["error"] = None
    q(f"UPDATE prs SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), row["id"]))
    return q("SELECT * FROM prs WHERE id=?", (row["id"],), one=True)


def iso_epoch(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def pr_due(row, rows):
    """Whether a PR not open yet may open: every PR before it in the order is open with CI green, or merged."""
    return not row["number"] and all(x["state"] == "MERGED" or (x["number"] and x["state"] == "OPEN" and
                                                                  x["ci"] == "green")
                                     for x in rows if x["seq"] < row["seq"])


def pr_mergeable(row, rows):
    """Why row's Merge button is off, or None when it may merge: open, CI green, every earlier PR merged."""
    if row["state"] != "OPEN" or not row["number"]:
        return "it is not open"
    if row["ci"] != "green":
        return f"its CI is {row['ci']}"
    early = [x for x in rows if x["seq"] < row["seq"] and x["state"] != "MERGED"]
    if early:
        return "an earlier PR in pr_order has not merged"
    return None


def pr_merge_one(p, tid, row):
    """gh pr merge --<pr_merge> --delete-branch. None, or gh's message (also kept on the row for the card)."""
    r = part_of(p, row["repo"])
    rc, out, err = gh(r, "pr", "merge", str(row["number"]), f"--{p.pr_merge}", "--delete-branch")
    if rc:
        msg = (err or out).strip()[-1000:] or f"gh pr merge exited {rc}"
        q("UPDATE prs SET error=? WHERE id=?", (msg, row["id"]))
        ev(tid, f"merging {pr_words(p, row)} failed: {msg.splitlines()[0][:300]}")
        return msg
    q("UPDATE prs SET error=NULL WHERE id=?", (row["id"],))
    ev(tid, f"merged {pr_words(p, row)} ({p.pr_merge})")
    pr_view(p, task(tid), row)  # its merge commit; the next PR step moves main
    return None


def pr_main_target(p):
    """(revs main moves to after a PR merged: origin/main in each repo where it is ahead, main elsewhere; None and
    why when your main has commits origin/main lacks while origin/main has new ones)."""
    out = []
    for r in p.parts:
        o = f"refs/remotes/origin/{p.main}"
        if not origin_url(r) or not ref_ok(r, o) or is_ancestor(r, o, p.main):
            out.append(r.rev(p.main))
        elif is_ancestor(r, p.main, o):
            out.append(r.rev(o))
        else:
            return None, (f"{p.main}{' of ' + r.name if r.sub else ''} has commits origin/{p.main} lacks, so it can't "
                          f"fast-forward to the merged PR. Bring them together yourself (say git pull --rebase in "
                          f"{r.src}), then Retry.")
    return " ".join(out), None


def pr_main(p, tid):
    """After a PR merged: move your main to origin/main the way Straight to main moves it (build main if there is an
    [app] build, fast-forward, carry your uncommitted changes over), then dev takes main in. True once done; False
    when it waits (a deploy, or something only you can clear) or stopped."""
    upd(tid, state="merging", phase=f"moving {p.main} to origin/{p.main}")
    fetch_origin(p, tid)  # the merge commit GitHub just made, whether the queue merged it in this step or you did
    with p.main_lock:
        if stopped(tid):
            return False
        if deploy := exclusive_blocker(task(tid)):
            upd(tid, state="queued", phase="")
            ev(tid, f"waiting for deploy #{deploy['id']}: it moves {p.main} too")
            return False
        commit, why = pr_main_target(p)
        if why:
            upd(tid, state="needs_input", phase=PR_MAIN, question=why)
            ev(tid, "needs you: " + why)
            notify(f"{p.title}: task needs you", f"#{tid}: {why}")
            return False
        if commit != p.revs(p.main):
            if why := promote_blocker(p, commit, "merge"):
                wait_for_you(p, tid, why, WAIT)
                return False
            try:
                err = promote(p, tid, commit)
            except NeedsYou as e:
                wait_for_you(p, tid, str(e), WAIT_CHANGES)
                return False
            if stopped(tid):
                return False
            if err:
                upd(tid, state="needs_input", phase=PR_MAIN, question=f"{p.main} did not move to origin/{p.main}. "
                                                                          f"Retry moves it again.\n{err}")
                ev(tid, f"{p.main} did not move: {err.splitlines()[0][:300]}")
                return False
            ev(tid, f"{p.main} is now {p.show(commit)} (origin/{p.main})")
            stale_facts(p.name)
    with p.lock:
        err = catch_up_dev(p, tid)
    if err:
        ev(tid, f"{p.dev} could not take in {p.main}; the next release merges it: {err.splitlines()[0][:300]}")
    q("UPDATE prs SET synced=1 WHERE task_id=? AND state='MERGED'", (tid,))
    return True


def merge_all_on(tid):
    return setting(f"prmergeall:{tid}") == "1"


def pr_step(p, tid):
    """Open PR and every step after it, in the task's pipeline (start_at 'pr'): push and open the PRs that are due,
    push new commits to open ones, Merge all's next merge, read every PR, move main after a merge, then settle the
    task: done, needs you, or watching (state 'pr')."""
    upd(tid, next_prompt=None, error=None, question=None, phase="checking the pull requests")
    with pr_lock(tid):
        return _pr_step(p, tid)


def _pr_step(p, tid):
    t = task(tid)
    branch = t["branch"]
    upd(tid, phase="fetching origin")
    fetch_origin(p, tid)
    plan, local = pr_plan(p, t)
    rows = pr_rows(tid)
    opened, top = any(x["number"] for x in rows), max((x["seq"] for x in rows), default=0)
    for r, seq in plan:
        if r.sub not in {x["repo"] for x in rows}:  # a repo that joins after the first PRs opened goes after them
            q("INSERT INTO prs (task_id, repo, seq) VALUES (?,?,?)", (tid, r.sub, top + 1 if opened else seq))
    rows = pr_rows(tid)
    if local and setting(f"prlocal:{tid}") != branch:
        set_setting(f"prlocal:{tid}", branch)
        ev(tid, f"no origin in {', '.join(r.name for r in local)}: its commits stay on the local branch {branch}, "
                "with no PR")
    if not rows:
        if not local:
            return finish(p, tid, merged=None, note=f"no commits on {branch} that {p.main} lacks: no PR to open")
        return pr_needs(p, tid, PR_SETUP, f"No repo with commits on {branch} has an origin, so there is no PR to "
                                          "open. Add a GitHub origin, then Retry.")
    todo = [x for x in rows if (x["state"] == "OPEN" and x["number"]) or pr_due(x, rows)]
    if todo and (why := pr_precheck(p, [part_of(p, x["repo"]) for x in todo])):
        return pr_needs(p, tid, PR_SETUP, why)
    # A PR opened or pushed to in this step is read at the next poll, not now: GitHub registers its checks a little
    # after the push, and "no checks" would read as green (opening the next PR in pr_order, or hiding a red CI)
    fresh = set()
    for x in rows:  # new commits on an open PR's branch (a fix round): push them, no rebase, no force
        r = part_of(p, x["repo"])
        if x["state"] == "OPEN" and x["number"] and r.rev(f"refs/heads/{branch}") != \
                (r.git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}", check=False) or ""):
            upd(tid, phase=f"pushing {pr_words(p, x)}")
            if why := pr_push(p, tid, r, branch):
                return pr_needs(p, tid, PR_SETUP, why)
            q("UPDATE prs SET ci='pending', failing=NULL WHERE id=?", (x["id"],))
            fresh.add(x["id"])
    for _ in range(len(rows) + 1):
        changed = False
        rows = pr_rows(tid)
        for x in rows:
            if stopped(tid):
                return
            if pr_due(x, rows):
                upd(tid, phase=f"opening the PR for {pr_name(p, x) or branch}")
                if why := pr_open_one(p, task(tid), x, rows):
                    return pr_needs(p, tid, PR_SETUP, why)
                fresh.add(x["id"])
                changed = True
        rows = pr_rows(tid)
        for x in rows:
            if x["state"] == "OPEN" and x["number"] and x["id"] not in fresh:
                pr_view(p, t, x)
        rows = pr_rows(tid)
        if merge_all_on(tid):
            for x in rows:
                if not pr_mergeable(x, rows):
                    upd(tid, phase=f"merging {pr_words(p, x)}")
                    if pr_merge_one(p, tid, x):
                        set_setting(f"prmergeall:{tid}", "")
                    changed = True
                    break
        if not changed:
            break
    if stopped(tid):
        return
    rows = pr_rows(tid)
    if any(x["state"] == "MERGED" and not x["synced"] for x in rows):
        if not pr_main(p, tid):
            return
    return pr_settle(p, tid)


def pr_needs(p, tid, phase, why):
    """The branch task needs you, in one of the PR phases."""
    upd(tid, state="needs_input", phase=phase, question=why)
    ev(tid, f"needs you: {why.splitlines()[0][:500]}")
    tell_sender(tid, "needs the owner: " + why)
    notify(f"{p.title}: task needs you", f"#{tid}: {why}")


def pr_settle(p, tid):
    """Where the task's PRs leave it: done once all merged, needs you if one closed or has red CI, else watching."""
    rows, t = pr_rows(tid), task(tid)
    if rows and all(x["state"] == "MERGED" for x in rows):
        set_setting(f"prmergeall:{tid}", "")
        upd(tid, start_at="claude")
        return finish(p, tid, merged=" ".join(x["merge_commit"] for x in rows if x["merge_commit"]) or None,
                      note="every PR merged: " + ", ".join(f"{pr_words(p, x)} {x['url']}" for x in rows))
    if closed := [x for x in rows if x["state"] == "CLOSED"]:
        return pr_needs(p, tid, PR_CLOSED, "; ".join(f"PR #{x['number']}" + (f" ({pr_name(p, x)})" if x["repo"] else "")
                                                     + " was closed without merging" for x in closed)
                        + ". Retry opens it again; Cancel leaves it closed.")
    if red := [x for x in rows if x["state"] == "OPEN" and x["ci"] == "red"]:
        set_setting(f"prmergeall:{tid}", "")
        checks = [f"{pr_words(p, x)}: " + ", ".join(f["name"] for f in json.loads(x["failing"] or "[]"))
                  for x in red]
        later = [x for x in rows if not x["number"]]
        return pr_needs(p, tid, PR_RED, "CI failed on " + "; ".join(checks) + "."
                        + (f" {', '.join(pr_name(p, x) for x in later)} won't open until it is green." if later else "")
                        + " Fix CI has the worker fix it from the failed logs; or send the task a message.")
    if pending_steers(tid):  # a message came in while the queue pushed: the worker takes it next
        upd(tid, state="queued", phase="", start_at="claude", next_prompt=steered_prompt("while the queue pushed"))
        return
    waiting = [x for x in rows if x["state"] == "OPEN" and x["ci"] == "pending"]
    ready = [x for x in rows if not pr_mergeable(x, rows)]
    phase = ("CI running on " + ", ".join(pr_words(p, x) for x in waiting)) if waiting else \
        ("ready to merge: " + ", ".join(pr_words(p, x) for x in ready)) if ready else "PRs open"
    upd(tid, state="pr", phase=phase, question=None, start_at="pr")
    if phase != t["phase"] or t["state"] != "pr":
        ev(tid, phase)


def pr_poll():
    """Every PR_POLL_EVERY seconds (and after you act on a PR): read each open PR of a branch task that is
    watching them, then queue the task's PR step when there is something to do."""
    if not PR_POLLING.acquire(blocking=False):
        return
    try:
        for t in q("SELECT * FROM tasks WHERE id IN (SELECT task_id FROM prs WHERE state IN ('OPEN','CLOSED') AND "
                   "number IS NOT NULL) AND (state='pr' OR (state='needs_input' AND phase IN (?,?)))",
                   (PR_RED, PR_CLOSED)):
            p = PROJECTS.get(t["project"])
            if not p or t["id"] in THREADS or STOPPING.is_set():
                continue
            try:
                pr_poll_task(p, t)
            except Exception as e:  # one task's trouble must not stop the others' polls
                print(f"pr poll #{t['id']}:", e, flush=True)
    finally:
        PR_POLLING.release()


def pr_poll_task(p, t):
    before = {x["id"]: (x["state"], x["ci"]) for x in pr_rows(t["id"])}
    for x in pr_rows(t["id"]):
        if x["number"] and x["state"] in ("OPEN", "CLOSED"):
            pr_view(p, t, x)
    rows = pr_rows(t["id"])
    moved = any(before.get(x["id"]) != (x["state"], x["ci"]) for x in rows)
    due = any(pr_due(x, rows) for x in rows) or (merge_all_on(t["id"]) and any(not pr_mergeable(x, rows)
                                                                                for x in rows))
    if (moved or due) and q("UPDATE tasks SET state='queued', phase='', question=NULL, start_at='pr' WHERE id=? AND "
                            "state=? AND IFNULL(phase,'')=? RETURNING id", (t["id"], t["state"], t["phase"] or "")):
        ev(t["id"], "the PRs moved on GitHub; checking them")


def poll_soon():
    threading.Thread(target=pr_poll, daemon=True).start()


def pr_queue(tid, why):
    """Queue the task's PR step (open, push, merge, move main), e.g. after a Merge click."""
    upd(tid, state="queued", phase="", question=None, start_at="pr")
    ev(tid, why)


def pr_action(p, t, action, b):
    """The PR buttons on a branch task's card (README: Branches and pull requests)."""
    tid, st = t["id"], t["state"]
    if not is_branch_task(t):
        raise ValueError("this task merges into dev; only a task on a named branch opens PRs")
    if not p:
        raise ValueError("its project is not loaded")
    rows = pr_rows(tid)
    if action == "pr-open":  # Open PR, or Retry after what was missing is fixed
        if st not in ("on_branch",) and not (st == "needs_input" and t["phase"] in (PR_SETUP, PR_MAIN)):
            raise ValueError(f"task is {STATE_WORDS.get(st, st)}: Open PR is for a task on its branch")
        return pr_queue(tid, "Open PR")
    if action == "pr-retry":  # a PR was closed without merging: open it again
        if not (st == "needs_input" and t["phase"] == PR_CLOSED):
            raise ValueError("no PR of this task is closed")
        for x in rows:
            if x["state"] == "CLOSED":
                rc, out, err = gh(part_of(p, x["repo"]), "pr", "reopen", str(x["number"]))
                if rc:
                    raise ValueError(f"gh pr reopen {x['number']} failed: {(err or out).strip()[-500:]}")
                q("UPDATE prs SET state='OPEN', ci='pending', error=NULL WHERE id=?", (x["id"],))
                ev(tid, f"reopened {pr_words(p, x)}")
        return pr_queue(tid, "retried: PRs reopened")
    if action == "fix-ci":
        if not (st == "needs_input" and t["phase"] == PR_RED):
            raise ValueError("no PR of this task has failed CI")
        upd(tid, state="queued", phase="", question=None, start_at="claude", rounds=0,
            next_prompt=fix_ci_prompt(p, t))
        ev(tid, "Fix CI: the worker gets the failed logs")
        return
    if st not in ("pr", "needs_input"):
        raise ValueError(f"task is {STATE_WORDS.get(st, st)}: nothing to merge now")
    if tid in THREADS:
        raise ValueError("the queue is working on its PRs right now: try again in a moment")
    if action == "pr-merge-all":
        if not [x for x in rows if x["state"] == "OPEN"]:
            raise ValueError("no PR of this task is open")
        set_setting(f"prmergeall:{tid}", "1")
        return pr_queue(tid, "Merge all: the PRs merge in order, each once its CI is green")
    row = next((x for x in rows if str(x["id"]) == str(b.get("pr")) or x["repo"] == b.get("repo")), None)
    if not row and len([x for x in rows if x["state"] == "OPEN"]) == 1:
        row = next(x for x in rows if x["state"] == "OPEN")
    if not row:
        raise ValueError("which PR? Give its repo")
    if why := pr_mergeable(row, rows):
        raise ValueError(f"{pr_words(p, row)} can't merge yet: {why}")
    with pr_lock(tid):
        if err := pr_merge_one(p, tid, row):
            raise ValueError(f"gh pr merge {row['number']} failed: {err}")
    pr_queue(tid, f"merged {pr_words(p, row)}; moving {p.main}")


def pr_view_rows(t, rows):
    """A branch task's PRs for the dashboard: repo, number, link, CI and state, and whether Merge may go."""
    p = PROJECTS.get(t["project"])
    out = []
    for x in rows or []:
        why = pr_mergeable(x, rows)
        out.append({"id": x["id"], "repo": x["repo"], "name": (pr_name(p, x) if p else x["repo"]) or "",
                    "number": x["number"], "url": x["url"], "state": x["state"], "ci": x["ci"],
                    "failing": json.loads(x["failing"] or "[]"), "checked_at": x["checked_at"],
                    "merged_at": x["merged_at"], "error": x["error"], "warning": x["warning"],
                    "merge": not why and t["state"] in ("pr", "needs_input"), "merge_why": why})
    return out


def fix_ci_prompt(p, t):
    """Fix CI: the failing checks and the end of each failed run's log, for the worker."""
    parts, runs = [], set()
    for x in pr_rows(t["id"]):
        failing = json.loads(x["failing"] or "[]")
        if x["state"] != "OPEN" or x["ci"] != "red":
            continue
        r = part_of(p, x["repo"])
        parts.append(f"### {pr_words(p, x)} ({x['url']})\n\nFailing: " + ", ".join(f["name"] for f in failing))
        for f in failing:
            m = re.search(r"/actions/runs/(\d+)", f.get("url") or "")
            if not m or (x["repo"], m.group(1)) in runs:
                continue
            runs.add((x["repo"], m.group(1)))
            rc, out, err = gh(r, "run", "view", m.group(1), "--log-failed")
            log = "\n".join((out or err).splitlines()[-200:])
            parts.append(f"`gh run view {m.group(1)} --log-failed` (last 200 lines):\n\n```\n{log}\n```")
    where = f"`{t['branch']}`" + (" in each repo" if p.repos else "")
    return (f"CI failed on the pull request{'s' * (len(parts) > 1)} for branch {where}. The branch is pushed, so "
            f"don't rebase, amend or force-push: fix it with new commits on top, re-run the failing step yourself "
            f"if you can, commit, and write $OPS_RESULT again. The queue runs the merge check, then pushes.\n\n"
            + "\n\n".join(parts))


def ask_owner(p, tid, question):
    """The worker (or the queue) needs a decision only you can make: the task waits on you."""
    upd(tid, state="needs_input", phase="", question=question)
    ev(tid, f"needs your input: {question[:1000]}")  # kept for a fresh session's handoff note after you answer
    tell_sender(tid, "needs the owner: " + question)
    notify(f"{p.title}: task needs input", f"#{tid}: {question}")


def changed_parts(p, ref):
    """The repos where ref has commits that dev lacks."""
    return [r for r in p.parts if int(r.git("rev-list", "--count", f"{p.dev}..{ref}") or 0)]


def bump_round(p, tid, why):
    n = (task(tid)["rounds"] or 0) + 1
    upd(tid, rounds=n)
    if n > MAX_FIX_ROUNDS:
        fail(p, tid, f"gave up after {MAX_FIX_ROUNDS} fix rounds. Last problem: {why}")
        return False
    ev(tid, f"fix round {n}: {why.splitlines()[0]}")
    return True


def fail(p, tid, why):
    upd(tid, state="failed", phase="", error=why, finished=time.time())
    ev(tid, "failed: " + why.splitlines()[0][:200])
    kept_for_retry(tid)
    notify(f"{p.title}: task failed", f"#{tid}: {why[:120]}")
    tell_sender(tid, "failed: " + why)


def finish(p, tid, merged, note):
    t = task(tid)
    upd(tid, state="done", phase="", merged=merged, finished=time.time())
    ev(tid, note)
    if t["worktree"] and Path(t["worktree"]).exists():
        kill_app(p, t["worktree"])
        removal_failed(tid, t["worktree"], remove_worktree(p, t["worktree"]))
    notify(f"{p.title}: task done", f"#{tid}: {t['title']}")
    tell_sender(tid, f"is done ({note}): " + (task(tid)["summary"] or "no summary"))
    if merged:  # a task marked "Promote after merge", or a release that ends with more of them in dev
        todo_queued_merged(tid)  # and a to-do queued as this task is done (TODO-7)
        auto_release(p)


def remove_worktree(p, path):
    """Remove a project worktree: each repo's git worktree (a root repo last: the others' worktrees are inside its
    worktree), then whatever is left of the folder, then prune. None once the folder is gone, else why it is not:
    the janitor (sweep_worktrees) tries again, so a failed removal never leaves a half-removed folder for good."""
    path = Path(path)
    errs = []
    for r in reversed(p.root_first()):
        if r.present(path):
            try:
                r.git("worktree", "remove", "--force", str(r.at(path)))
            except RuntimeError as e:
                errs.append(str(e).splitlines()[-1][:200])
    if os.path.lexists(path):  # a workspace's own files, or what a failed git worktree remove left
        rmtree(path, errs)
    for r in p.parts:
        r.git("worktree", "prune", check=False)
    if os.path.lexists(path):
        return "; ".join(errs[:3]) or "the folder is still there"
    return None


def rmtree(path, errs):
    """shutil.rmtree that makes a read-only (on macOS also a locked) file or folder writable and tries it once more;
    what still fails goes onto errs."""
    def again(fn, f, _exc):
        try:
            for x in {os.path.dirname(f), f}:
                if hasattr(os, "lchflags"):
                    os.lchflags(x, 0)
                if not os.path.islink(x):
                    os.chmod(x, 0o700)
            fn(f)
        except OSError as e:
            errs.append(f"{f}: {e.strerror or e}")
    shutil.rmtree(path, **{"onexc" if sys.version_info >= (3, 12) else "onerror": again})


SPENT_KEEP_DAYS = 7  # a failed or cancelled task's worktree stays this long: Retry resumes the worker in it
SWEEP_EVERY = 10 * 60  # seconds between janitor passes (see sweep_worktrees)
SWEEP_FRESH = 60 * 60  # seconds an openspec-* or *.root-tmp folder is left alone: a running command may use it
SWEEP_LOCK = threading.Lock()  # one janitor pass at a time
SWEEP_SAID = {}  # folder -> the last skip or failure logged for it, so each pass doesn't log it again


def kept_for_retry(tid):
    """Note on a failed or cancelled task that its worktree stays for a retry, and for how long."""
    t = task(tid)
    if t["worktree"] and Path(t["worktree"]).exists():
        ev(tid, f"worktree kept {SPENT_KEEP_DAYS} days for a retry, then removed")


def removal_failed(tid, path, err):
    """Log a failed worktree removal on the task, once per distinct error; err None (removed) clears it."""
    key = str(path)
    if not err:
        SWEEP_SAID.pop(key, None)
        return
    if SWEEP_SAID.get(key) != err:
        SWEEP_SAID[key] = err
        ev(tid, f"couldn't remove worktree {path}: {err}. The janitor tries again in {SWEEP_EVERY // 60} minutes")


def worktree_spent(t, now):
    """Whether a task is through with its worktree: done (finish removes it; this catches a removal that failed),
    or failed or cancelled more than SPENT_KEEP_DAYS ago. Until then Retry resumes the worker in its worktree,
    uncommitted work and all, and a chat about the task works there."""
    if t["state"] == "done":
        return True
    return t["state"] in ("failed", "cancelled") and now - (t["finished"] or 0) > SPENT_KEEP_DAYS * 86400


def worktree_holds_work(p, path):
    """Why the janitor must not remove folder path, or None: a repo there with uncommitted changes, or a
    .ops-result.json asking you something. A repo whose .git is gone, or points at metadata git pruned, is the rest
    of a half-done removal: nothing there to keep."""
    try:
        res = json.loads((path / RESULT_FILE).read_text())
        if isinstance(res, dict) and res.get("status") == "needs_input":
            return f"its {RESULT_FILE} asks you something"
    except (OSError, ValueError):
        pass
    for r in p.parts:
        at = r.at(path)
        dotgit = at / ".git"
        if not dotgit.exists():
            continue
        if dotgit.is_file():
            m = re.match(r"gitdir:\s*(.+)", dotgit.read_text(errors="replace").strip())
            if m and not Path(m.group(1)).exists():
                continue
        res = subprocess.run(["git", "status", "--porcelain"], cwd=at, capture_output=True, text=True)
        if res.returncode:
            return f"git status failed{' in ' + r.name if r.name else ''}: {res.stderr.strip()[:200]}"
        dirty = [l[3:] for l in res.stdout.splitlines() if l[3:] != RESULT_FILE]
        if dirty:
            return f"uncommitted changes{' in ' + r.name if r.name else ''}: {', '.join(dirty[:5])}"
    return None


def sweep_worktrees(p, now=None):
    """The janitor: remove each folder under p.wt that nothing needs. It keeps dev, main, deploy (its build output
    carries over between deploys) and deploy-peek; t<id> and the recorded worktree of each task that isn't
    worktree_spent, or whose pipeline or chat is running; spec-<id> of each spec chat; and an openspec-* or
    *.root-tmp folder younger than SWEEP_FRESH. It skips, and logs, a folder worktree_holds_work finds work in.
    A failed removal is logged on its task (else in the daemon's log) and tried again on the next pass.
    Returns {"removed": [names], "skipped": {name: why}, "failed": {name: why}}."""
    now = now or time.time()
    out = {"removed": [], "skipped": {}, "failed": {}}
    if not p.wt.is_dir():
        return out
    keep = {p.dev_wt.name, p.main_wt.name, p.deploy_wt.name, "deploy-peek"}
    owner = {}  # folder name -> the spent task it was the worktree of, for its log
    for t in q("SELECT id, state, finished, worktree FROM tasks WHERE project=?", (p.name,)):
        names = {f"t{t['id']}"} | ({Path(t["worktree"]).name} if t["worktree"] and
                                   Path(t["worktree"]).parent == p.wt else set())
        busy = t["id"] in THREADS or t["id"] in TASK_CHAT_LIVE or t["id"] in TASK_CHAT_PROCS
        if busy or not worktree_spent(t, now):
            keep |= names
        else:
            owner.update(dict.fromkeys(names, t["id"]))
    keep |= {f"spec-{d['id']}" for d in q("SELECT id FROM designs WHERE project=?", (p.name,))}
    for f in sorted(p.wt.iterdir()):
        if f.name in keep or f.is_symlink() or not f.is_dir():
            continue
        if (f.name.startswith("openspec-") or f.name.endswith(".root-tmp")) and now - f.stat().st_mtime < SWEEP_FRESH:
            continue
        tid = owner.get(f.name)
        why = worktree_holds_work(p, f)
        if why:
            out["skipped"][f.name] = why
            if SWEEP_SAID.get(str(f)) != why:
                SWEEP_SAID[str(f)] = why
                print(f"janitor: kept {f}: {why}", flush=True)
                if tid:
                    ev(tid, f"janitor kept worktree {f}: {why}")
            continue
        t = task(tid) if tid else None
        if t and (tid in THREADS or not worktree_spent(t, now)):  # retried since the list above
            continue
        kill_app(p, f)
        err = remove_worktree(p, f)
        if err:
            out["failed"][f.name] = err
            if tid:
                removal_failed(tid, f, err)
            elif SWEEP_SAID.get(str(f)) != err:
                SWEEP_SAID[str(f)] = err
                print(f"janitor: couldn't remove {f}: {err}", flush=True)
            continue
        SWEEP_SAID.pop(str(f), None)
        out["removed"].append(f.name)
        print(f"janitor: removed {f}" + (f" (task #{tid})" if tid else ""), flush=True)
        if tid:
            ev(tid, f"janitor removed worktree {f}")
    return out


def sweep_all():
    """One janitor pass over every project, in the background: removing a worktree can take a while."""
    if not SWEEP_LOCK.acquire(blocking=False):
        return
    try:
        for p in list(PROJECTS.values()):
            if STOPPING.is_set():
                break
            if getattr(p, "missing", None):
                continue
            try:
                sweep_worktrees(p)
            except Exception as e:  # one project's trouble doesn't stop the others
                print(f"janitor error in {p.name}: {e}", flush=True)
        try:
            sweep_derived_data([p.wt for p in PROJECTS.values()])
        except Exception as e:
            print(f"janitor error in {DERIVED_DATA}: {e}", flush=True)
    finally:
        SWEEP_LOCK.release()


DERIVED_DATA = HOME / "Library/Developer/Xcode/DerivedData"


def sweep_derived_data(roots):
    """Remove Xcode's build cache of a worktree that is gone. Xcode keeps one DerivedData/<name>-<hash> per
    workspace path, about 3.5 GB for an iOS app, and no build uses it once that path's worktree is removed.
    roots: the projects' worktree folders; an entry whose WorkspacePath is in a folder under one of them that no
    longer exists goes. Returns the entries removed."""
    out = []
    if not DERIVED_DATA.is_dir():
        return out
    for d in sorted(DERIVED_DATA.iterdir()):
        try:
            ws = Path(plistlib.loads((d / "info.plist").read_bytes()).get("WorkspacePath") or "")
        except (OSError, ValueError, AttributeError):
            continue
        root = next((r for r in roots if r in ws.parents), None)
        if not root or (root / ws.relative_to(root).parts[0]).exists():
            continue
        errs = []
        rmtree(d, errs)
        if errs:
            print(f"janitor: couldn't remove {d}: {errs[0]}", flush=True)
        else:
            out.append(d.name)
            print(f"janitor: removed {d}, Xcode's build cache of {ws}", flush=True)
    return out


def add_subtasks(p, tid, items, priority=0, hold=0, auto_release=None, model=None, effort=None):
    """Queue a worker's task list ({title, body, after}) with tid as parent. Returns the new ids."""
    ids = []
    for sub in items or []:
        after = sub.get("after")
        dep = ids[after] if isinstance(after, int) and 0 <= after < len(ids) else None
        ids.append(add_task(p.name, sub.get("body") or sub.get("title", ""), sub.get("title", ""),
                            priority=priority, hold=hold, depends_on=dep, parent=tid, auto_release=auto_release,
                            model=model, effort=effort))
    return ids


def add_followups(p, tid, items):
    """Queue a done worker's follow-ups ({title, body}) as tasks that depend on tid, so each runs only once tid has
    merged; if tid fails they wait behind it like any dependent. Queue defaults for priority, model, effort and
    release, not the parent's. A worker resumed after a gate failure may list them again: a title already queued
    behind tid is skipped. Returns the new ids."""
    have = {r["title"] for r in q("SELECT title FROM tasks WHERE parent=? AND depends_on=?", (tid, tid))}
    ids = []
    for f in items if isinstance(items, list) else []:
        if not isinstance(f, dict) or not (f.get("body") or f.get("title")):
            continue
        body = f.get("body") or f.get("title")
        title = (f.get("title") or title_of(body)).strip()[:120]
        if title in have:
            continue
        have.add(title)
        ids.append(add_task(p.name, body, title, depends_on=tid, parent=tid))
    return ids


def split(p, tid, res):
    t = task(tid)
    ids = add_subtasks(p, tid, res.get("tasks"), t["priority"], t["hold"], t["auto_release"], t["model"],
                        t["effort"])
    finish(p, tid, merged=None, note="split into " + ", ".join(f"#{i}" for i in ids))


# ---------------------------------------------------------------- release (dev -> main)

def queue_release(p):
    """The Promote button: queue a release check of dev, ahead of ordinary tasks. It never refuses: with a release
    already open it returns that one, and whatever is in the way of moving main the release waits for. A flow "pr"
    project has no Promote: ValueError."""
    if getattr(p, "flow", "dev") == "pr":
        raise ValueError(f"{p.title} sends every task through a branch and PR (flow = \"pr\"), so there is no "
                         f"Promote: {p.main} moves when a PR merges.")
    open_ = q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN ('done','failed','cancelled')",
              (p.name,), one=True)
    if open_:
        return open_["id"]
    TRAIN.pop(p.name, None)
    body = (f"Check the latest {p.dev} with the merge check. If it passes, build it into {p.main} and "
            f"fast-forward {p.main} to it.")
    return add_task(p.name, body, f"Release {p.dev} → {p.main}", priority=100, kind="release")


def root_of(t):
    """The task a split started from: subtasks of subtasks share it."""
    while t["parent"] and t["kind"] == "task":
        up = task(t["parent"])
        if not up or up["kind"] != "task":
            break
        t = up
    return t["id"]


def auto_release(p):
    """Queue a release (what Promote queues) for tasks marked "Promote after merge" once they are in dev and not yet in main, as a train:
    when the project is quiet (none of its tasks or Send to devs queued, running, gating or merging; ones waiting
    on you don't count), or once the first of them merged release_wait minutes ago. The scheduler calls this every
    minute, and a merge at its end. With a release already open this waits: the release's own end calls it again,
    so anything that merged after its snapshot gets the next one. The tasks of one split are promoted together, after
    the last of them merges."""
    with RELEASE_LOCK:
        TRAIN.pop(p.name, None)
        if p.direct_main or getattr(p, "flow", "dev") == "pr":  # its merges move main themselves (land_on_main, pr_main)
            return None
        if q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN (?,?,?)",
             (p.name, *ENDED), one=True):
            return None
        tasks = q("SELECT * FROM tasks WHERE project=? AND kind IN (?,?) AND auto_release=1", (p.name, *MERGES))
        busy = {root_of(t) for t in tasks if t["state"] not in ENDED}
        ready = [t for t in tasks if t["state"] == "done" and t["merged"] and t["id"] not in IN_MAIN
                 and root_of(t) not in busy]
        for t in ready:
            if all(any(is_ancestor(r, sha, p.main) for r in p.parts if r.has(sha)) for sha in t["merged"].split()):
                IN_MAIN.add(t["id"])
        ready = [t for t in ready if t["id"] not in IN_MAIN]
        if not ready:
            return None
        first = min(t["finished"] or 0 for t in ready)
        by = first + release_wait() * 60
        quiet = not q(f"SELECT id FROM tasks WHERE project=? AND kind IN (?,?) AND state IN "
                      f"({','.join('?' * (1 + len(ACTIVE)))}) LIMIT 1", (p.name, *MERGES, "queued", *ACTIVE), one=True)
        if not quiet and time.time() < by:
            TRAIN[p.name] = {"ids": [t["id"] for t in ready], "by": by}
            return None
        rid = queue_release(p)
    for t in ready:
        ev(t["id"], f"queued release #{rid}")
    why = "the queue is quiet" if quiet else f"the first merged {round((time.time() - first) / 60)} min ago"
    ev(rid, "queued after " + ", ".join(f"#{t['id']}" for t in ready) + f" merged (promote after merge; {why})")
    return rid


def release_trains():
    """The scheduler's minute check: a train may leave because its time is up, or because the queue went quiet
    without a merge (a task failed, was cancelled, or now waits for your review)."""
    for p in list(PROJECTS.values()):
        try:
            auto_release(p)
        except Exception as e:  # one project's git trouble must not stop the others' trains
            print(f"promote train {p.name}:", e, flush=True)


ANSWERED = "You asked the owner:"  # how an answer reaches a task's next turn (see answer_prompt)
ANSWERED_OLD = "The user answered your question:"  # how it did before, in transcripts from then
ANSWER_HEADS = (ANSWERED, ANSWERED_OLD)
ANSWER_GO = ("If they agreed, that is their approval for the action you asked about: do it, including steps "
             "outside your worktree.")
ANSWER_TAIL = "Continue the task and write $OPS_RESULT."  # and what ends it


def answer_prompt(question, answer):
    """The turn that carries the owner's answer, under the question it answers: a bare "Do it" names no action,
    and auto mode allows a step the user approved only when their words name it."""
    asked = "\n".join("> " + line for line in (question or "(the question was not kept)").strip().splitlines())
    return f"{ANSWERED}\n\n{asked}\n\nThe owner answered:\n\n{answer}\n\n{ANSWER_GO}\n\n{ANSWER_TAIL}"
WAIT = "waiting on you"  # the phase of a release that needs you because something is in the way of moving main
WAIT_CHANGES = "waiting on your changes"  # ... because your uncommitted changes could not be carried over
WAIT_MAIN = "waiting on main"  # a deploy's commits can't fast-forward main: it moved during the deploy


class NeedsYou(Exception):
    """Only you can settle this: the release waits for you (needs_input) rather than failing."""


def wait_for_you(p, tid, why, phase):
    """The release needs you. It goes on by itself once the way is clear (see unblock_releases), or when you
    answer."""
    upd(tid, state="needs_input", phase=phase, question=why)
    ev(tid, "needs you: " + why)
    notify(f"{p.title}: {task(tid)['kind']} needs you", f"#{tid}: {why}")


def unblock_releases():
    """Requeue each release (or deploy moving main) that needs you once you've cleared its way: quit the build,
    switched the worktree off main, or (for changes that could not be carried over) committed or stashed them."""
    for t in q("SELECT * FROM tasks WHERE kind='release' AND state='needs_input' AND phase IN (?,?)",
               (WAIT, WAIT_CHANGES)):
        p = PROJECTS.get(t["project"])
        try:
            if not p or promote_blocker(p, t["base"]) or (t["phase"] == WAIT_CHANGES and clashing(p, t["base"])):
                continue
        except Exception as e:  # one project's git trouble must not stop the others' checks
            print(f"unblock #{t['id']}:", e, flush=True)
            continue
        if q("UPDATE tasks SET state='queued', phase='', question=NULL WHERE id=? AND state='needs_input' "
             "RETURNING id", (t["id"],)):  # unless you answered or cancelled it just now
            ev(t["id"], "the way is clear; going on")
    for t in q(f"SELECT * FROM tasks WHERE kind IN ({','.join('?' * len(MERGES))}) AND state='needs_input' AND "
               "start_at='main' AND phase IN (?,?)", (*MERGES, WAIT, WAIT_CHANGES)):  # direct_main merges
        p = PROJECTS.get(t["project"])
        try:
            if not p or promote_blocker(p, p.revs(p.dev)) or (t["phase"] == WAIT_CHANGES and clashing(p, p.revs(p.dev))):
                continue
        except Exception as e:
            print(f"unblock #{t['id']}:", e, flush=True)
            continue
        if q("UPDATE tasks SET state='queued', phase='', question=NULL WHERE id=? AND state='needs_input' "
             "RETURNING id", (t["id"],)):
            ev(t["id"], "the way is clear; going on")
    for t in q("SELECT * FROM tasks WHERE state='needs_input' AND start_at='pr' AND phase IN (?,?)",
               (WAIT, WAIT_CHANGES)):  # a branch task moving main to origin/main after a PR merged
        p = PROJECTS.get(t["project"])
        try:
            commit = pr_main_target(p)[0] if p else None
            if not commit or promote_blocker(p, commit) or (t["phase"] == WAIT_CHANGES and clashing(p, commit)):
                continue
        except Exception as e:
            print(f"unblock #{t['id']}:", e, flush=True)
            continue
        if q("UPDATE tasks SET state='queued', phase='', question=NULL WHERE id=? AND state='needs_input' "
             "RETURNING id", (t["id"],)):
            ev(t["id"], "the way is clear; going on")
    for t in q("SELECT * FROM tasks WHERE kind='deploy' AND state='needs_input' AND phase IN (?,?,?)",
               (WAIT, WAIT_CHANGES, WAIT_MAIN)):  # a deploy whose commits are waiting to move main
        p = PROJECTS.get(t["project"])
        try:
            if not p or deploy_in_the_way(p, t):
                continue
        except Exception as e:
            print(f"unblock #{t['id']}:", e, flush=True)
            continue
        if q("UPDATE tasks SET state='queued', phase='', question=NULL WHERE id=? AND state='needs_input' "
             "RETURNING id", (t["id"],)):
            ev(t["id"], "the way is clear; going on")


def release_contents(p, t):
    """What release t brings to main: [(repo, "sha short subject")] of first-parent commits, and the tasks that
    merged them."""
    log = [(r, l) for r, sha in p.pairs(t["base"])
           for l in r.git("log", "--first-parent", "--format=%H %h %s", f"{p.main}..{sha}").splitlines()]
    shas = {l.split(" ", 1)[0] for _, l in log}
    done = [d for d in q("SELECT id,title,summary,merged FROM tasks WHERE project=? AND merged IS NOT NULL "
                         "ORDER BY id", (p.name,)) if shas & set(d["merged"].split())] \
        if shas else []
    return log, done


def gate_only_summary(p, t):
    """A gate-only release's summary: what passed, then each task's own summary."""
    checks = ", ".join(g["name"] for g in p.gate) or "no checks configured"
    _, done = release_contents(p, t)
    return "\n\n".join([f"Gate-only release: the merge check passed ({checks})"]
                       + [f"#{d['id']} {d['title']}\n{d['summary'] or '(no summary)'}" for d in done])


def release_pipeline(p, tid):
    """Snapshot dev, gate it, then promote exactly that commit."""
    t = task(tid)
    start_at, resume = t["start_at"], t["next_prompt"]
    upd(tid, started=t["started"] or time.time(), error=None, next_prompt=None, start_at="claude")
    if not t["base"]:
        with p.lock:
            err = merge_main_into_dev(p, tid)
            if not err:
                upd(tid, base=p.revs(p.dev))
        if err and not resolve_main_into_dev(p, tid, err):
            return
        ev(tid, f"checking {p.dev} at {p.show(task(tid)['base'])}")
    t = task(tid)
    if t["base"] == p.revs(p.main):
        start_at = "promote"  # nothing new to check: this only rebuilds main
        ev(tid, f"nothing new on {p.dev}; rebuilding {p.main}")
    if not (t["worktree"] and all(r.present(t["worktree"]) for r in p.parts)):
        path = p.wt / f"t{tid}"
        copy_workspace_files(p, path)
        for r, sha in p.root_first(p.pairs(t["base"])):
            if not r.present(path):
                r.add_worktree(path, "--detach", sha)
        prepare_worktree(p, path, key=tid)
        upd(tid, worktree=str(path))
        ev(tid, f"worktree {path} at {p.show(t['base'])}")
        t = task(tid)
    else:
        for r, sha in p.pairs(t["base"]):
            if r.git("rev-parse", "HEAD", cwd=r.at(t["worktree"])) != sha:  # left at the merge worker's commit
                r.git("checkout", "--force", "--detach", sha, cwd=r.at(t["worktree"]))
        if unprepared(t["worktree"]):  # its setup failed or was stopped: run it again
            prepare_worktree(p, Path(t["worktree"]), key=tid)

    if start_at != "promote":
        upd(tid, state="gating")
        ok, detail = gate(p, tid, t["worktree"])
        if stopped(tid):
            return
        if not ok:
            return fail(p, tid, "gate failed, so the release stops here:\n" + detail)
        ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))
        upd(tid, summary=gate_only_summary(p, t))

    upd(tid, start_at="promote")  # a retry, a resume or a restart continues here
    if stopped(tid):
        return
    t = task(tid)
    if not all(is_ancestor(r, p.main, sha) for r, sha in p.pairs(t["base"])):  # main moved: check again
        ev(tid, f"{p.main} moved during the release; checking again with its new commits")
        upd(tid, base=None, session_id=None, start_at="claude", phase="")
        return release_pipeline(p, tid)
    why = promote_blocker(p, t["base"])
    if why:
        return wait_for_you(p, tid, why, WAIT)
    upd(tid, state="merging")
    before = p.revs(p.main)
    try:  # your answer, when the release needed you, guides the worker that carries your changes over
        with p.main_lock:  # never alongside a direct_main merge moving main
            err = promote(p, tid, t["base"], note=resume if resume and resume.startswith(ANSWER_HEADS) else "")
    except NeedsYou as e:
        return wait_for_you(p, tid, str(e), WAIT_CHANGES)
    if stopped(tid):
        return
    if err:
        return fail(p, tid, err)
    upd(tid, start_at="claude")
    if p.revs(p.main) != before:  # the main changelog of a project that never deployed starts here
        set_setting(f"promoted_from:{p.name}", before)
    finish(p, tid, merged=t["base"], note=f"{p.main} is now {p.show(t['base'])}")  # and releases what merged since
    if Path(p.repo).resolve() == CODE and p.revs(p.main) != before:
        restart_when_idle(tid, deploys_only=True)


def is_service():
    """This daemon is the launchd service that opsctl manages, not a scratch daemon a test or worker runs.
    launchd's pid for the job is Claude Ops.app's launcher, our parent (or us, from an older plist)."""
    out = subprocess.run(["launchctl", "list", "com.claude-ops"], capture_output=True, text=True).stdout
    m = re.search(r'"PID" = (\d+);', out)
    return bool(m) and int(m.group(1)) in (os.getpid(), os.getppid())


RESTART_IDLE_MAX = 15 * 60  # seconds a direct_main merge of claude-ops waits for the queue to go idle before restarting
# {"tid", "since", "deploys_only", "told"}: a restart waiting. Not a setting: the next daemon has the new code
RESTART_AFTER = {}
RESTART_LOCK = threading.Lock()  # the release thread and the scheduler both try the restart


def running_deploys():
    """Deploys a restart would cut off: their command runs, and recover() fails them rather than run it again. One at
    start_at 'promote' has finished its command and only moves main, which a restart resumes."""
    return q(f"SELECT id FROM tasks WHERE kind='deploy' AND start_at!='promote' AND state IN "
             f"({','.join('?' * len(ACTIVE))}) ORDER BY id", ACTIVE)


def deploy_words(deploys):
    ids = [f"#{t['id']}" for t in deploys]
    return ("deploy " if len(ids) == 1 else "deploys ") + ", ".join(ids)


def restart_holds(t):
    """Whether a restart waiting to load new code holds queued task t back. A merge's restart (restart_when_idle)
    holds every task; a Promote's holds only deploys, which it must not cut off."""
    return bool(RESTART_AFTER) and (not RESTART_AFTER.get("deploys_only") or t.get("kind") == "deploy")


def restart_when_idle(tid, deploys_only=False):
    """main of claude-ops itself moved: restart to load the new code, but never while a deploy runs, in any project
    (it publishes things, and recover() won't run a cut-off one again), however long it takes. A direct_main merge
    (deploys_only False) also waits for every other task, as a restart pauses them and starts their gates over, but
    for at most RESTART_IDLE_MAX. A Promote, which you click, waits only for deploys. Meanwhile new deploys don't
    start, nor, for a merge, other tasks. restart_if_idle, from the scheduler, does it; here it runs once at once."""
    with RESTART_LOCK:
        if not RESTART_AFTER:
            RESTART_AFTER.update(tid=tid, since=time.time(), deploys_only=deploys_only, told=False)
        elif deploys_only and not RESTART_AFTER["deploys_only"]:  # a Promote while a merge's restart waits: sooner
            RESTART_AFTER.update(tid=tid, deploys_only=True, told=False)
        else:
            ev(tid, f"claude-ops restarts to load the new code with #{RESTART_AFTER['tid']}'s restart, already waiting")
            return
    if restart_if_idle():
        return
    deploys = running_deploys()
    if deploys_only:
        ev(tid, f"claude-ops restarts to load the new code once {deploy_words(deploys)} finishes; new deploys wait "
                f"for it")
    else:
        ev(tid, f"claude-ops restarts to load the new code once no task is running (at most {RESTART_IDLE_MAX // 60} "
                f"min, but never while a deploy runs"
                + (f": {deploy_words(deploys)} runs now" if deploys else "") + "); new tasks wait for it")


def restart_if_idle():
    """Restart if the restart waiting can go now; True if it did."""
    with RESTART_LOCK:
        if not RESTART_AFTER or STOPPING.is_set():
            return False
        tid = RESTART_AFTER["tid"]
        deploys = running_deploys()
        busy = [] if RESTART_AFTER["deploys_only"] else \
            q(f"SELECT id FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE)
        over = time.time() - RESTART_AFTER["since"] >= RESTART_IDLE_MAX
        if deploys:  # no time limit
            if busy and over and not RESTART_AFTER["told"]:
                RESTART_AFTER["told"] = True
                ev(tid, f"{RESTART_IDLE_MAX // 60} min passed, but {deploy_words(deploys)} still runs: claude-ops "
                        f"restarts once it finishes")
            return False
        if busy and not over:
            return False
        RESTART_AFTER.clear()
    if busy:
        ev(tid, f"restarting after {RESTART_IDLE_MAX // 60} min although tasks still run: "
                + ", ".join(f"#{t['id']}" for t in busy))
    restart_self(tid)
    return True


def spawn_restart(log, **env):
    """Run `opsctl restart` in a session of its own, so stopping this daemon's launchd job spares it. If the new
    daemon doesn't answer, opsctl says so in log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(CODE / "opsctl"), "restart"], cwd=CODE, env=dict(os.environ, OPS_DATA=str(DATA), **env),
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(log, "a"), stderr=subprocess.STDOUT)


def restart_self(tid):
    """main of claude-ops itself just moved: run `opsctl restart` to load the new code. Running tasks pause and
    the new daemon resumes them."""
    if not is_service():
        ev(tid, "not restarting claude-ops: this daemon is not the launchd service, and opsctl restart "
                "would restart that one")
        return
    ev(tid, "restarting claude-ops to load the new code; running tasks pause and resume")
    spawn_restart(RUNS / str(tid) / "restart.log", OPS_RESTART_TASK=str(tid))


def restart_now(src=""):
    """The Restart button in Settings' Danger zone, or `opsd.py restart` from a chat (src, its OPS_FROM): `opsctl
    restart` at once, like typing it. It doesn't wait for deploys or idle tasks: running tasks pause and resume, and
    a running deploy is cut off and fails. A chat that asked is told so when the new daemon resumes it."""
    if not is_service():
        raise ValueError("not the launchd service; opsctl restart would restart that one")
    deploys = running_deploys()
    chat = int(src[5:]) if re.fullmatch(r"chat:\d+", src or "") else None
    print("restarting claude-ops: " + (f"chat #{chat} ran opsd.py restart" if chat else
                                       "you clicked Restart in Settings' Danger zone")
          + (f"; {deploy_words(deploys)} will fail" if deploys else ""), flush=True)
    set_setting("restarted_by", json.dumps(chat))
    spawn_restart(DATA / "restart.log")
    return {"ok": True}


# ---------------------------------------------------------------- the Claude usage limit
# When Claude's usage limit hits, its turn ends with an event saying so (api_error "usage_limit_reached") and when it
# resets. The queue waits it out instead of failing work: it saves when the limit resets (settings limited_until and
# limited_kind, so a restart keeps them), pauses each task whose worker hit it (paused_by 'limit'), starts nothing
# until then, and a minute after the reset requeues those tasks. Tasks you paused stay paused.

LIMIT_LOCK = threading.Lock()
LIMIT_GRACE = 60  # seconds after the reset before the queue tries again
LIMIT_UNKNOWN = 15 * 60  # how long to wait when Claude doesn't say when it resets
LIMIT_KINDS = {"five_hour": "5-hour", "seven_day": "7-day", "seven_day_opus": "7-day Opus",
               "seven_day_sonnet": "7-day Sonnet"}
LIMIT_NOTE = ("The Claude usage limit stopped you mid-turn, and the queue waited for it to reset. Any command you "
              "were running was stopped. Check `git status` and `git log` in this worktree, finish the task, and "
              "write $OPS_RESULT.")


def limit_of(e):
    """The rate_limit_info of a stream-json event that says the usage limit hit ({} if it doesn't say more), or None
    for any other event."""
    if e.get("api_error") != "usage_limit_reached":
        return None
    info = (e.get("api_error_params") or {}).get("rate_limit_info")
    return info if isinstance(info, dict) else {}


def clock(ts):
    """3:00 pm, or Thu 3:00 pm when it isn't today."""
    lt = time.localtime(ts)
    words = time.strftime("%I:%M %p", lt).lstrip("0").lower()
    return words if lt[:3] == time.localtime()[:3] else time.strftime("%a ", lt) + words


def limit_hit(info):
    """A Claude session hit the usage limit: save until when, so the queue starts nothing before then. Notifies once
    per limit, not once per worker that runs into it. Returns the reset time."""
    try:
        until = float(info.get("resetsAt") or 0)
    except (TypeError, ValueError):
        until = 0
    known = until > time.time()
    if not known:  # missing, or already past: try again in a while
        until = time.time() + LIMIT_UNKNOWN
    kind = str(info.get("rateLimitType") or "")
    with LIMIT_LOCK:
        s = settings_now()
        try:
            was = float(s.get("limited_until") or 0)
        except ValueError:
            was = 0
        if was > time.time() and (until <= was or not known):  # already waiting for this one (or a later one)
            return was
        set_setting("limited_until", until)
        set_setting("limited_kind", kind)
    print(f"usage limit ({kind or 'unknown'}): waiting until {clock(until)}", flush=True)
    notify("Claude usage limit", f"the queue resumes at {clock(until)}")
    return until


def limit_info(s=None):
    """The usage limit the queue is waiting out, for the dashboard: {until, kind, label, resumes, words}; or None."""
    s = settings_now() if s is None else s
    try:
        until = float(s.get("limited_until") or 0)
    except ValueError:
        return None
    if not until:
        return None
    kind = s.get("limited_kind") or ""
    label = f"Usage limit ({LIMIT_KINDS.get(kind, kind.replace('_', ' '))})" if kind else "Usage limit"
    return {"until": until, "kind": kind, "label": label, "resumes": clock(until),
            "words": f"{label} · resumes {clock(until)}"}


def limited():
    """The queue is waiting out the usage limit: start no Claude session. Clears the limit a minute after its reset."""
    lim = limit_info()
    if lim and time.time() >= lim["until"] + LIMIT_GRACE:
        limit_clear("the usage limit reset")
        return False
    return bool(lim)


def limit_clear(why):
    """Stop waiting (the limit reset, or you clicked Try now) and requeue the tasks the limit paused. Returns their ids."""
    with LIMIT_LOCK:
        q("DELETE FROM settings WHERE key IN ('limited_until','limited_kind')")
    ids = [r["id"] for r in q("UPDATE tasks SET state='queued', paused_by=NULL WHERE state='paused' AND "
                              "paused_by='limit' RETURNING id")]
    for tid in ids:
        ev(tid, f"resumed: {why}")
    print(f"usage limit cleared ({why}); requeued {ids}", flush=True)
    return {"resumed": ids}


def limit_pause(tid, prompt=None):
    """The usage limit stopped this task's Claude turn: pause it until the reset. prompt: what the next turn should
    send instead of LIMIT_NOTE, when Claude never got it (or has no session to resume)."""
    t = task(tid)
    try:
        pause_task(tid, by="limit")
    except ValueError:
        return  # you paused or cancelled it meanwhile
    if prompt is not None and t["state"] == "running":  # a merge's step (carrying your changes) starts over anyway
        upd(tid, next_prompt=prompt, start_at="claude")


# ---------------------------------------------------------------- the usage brake
# Every Claude stream reports how much of each usage window is used (a rate_limit_event, utilization 0 to 1). The
# usage is account-wide, so it counts your own Claude use too. The daemon keeps the latest reading per window in
# settings usage:<window>, and at usage_brake percent (default 85, 0 = off) of the 5-hour window the scheduler starts
# no new task until that window resets (the 7-day window never brakes: it only shows on the meter), which leaves the rest of it for your design chats, chats and answers. Running
# work carries on, and chats are never held. Below the brake, the usage taper (usage_taper, default 30, 0 = off)
# starts fewer Claude workers as the 5-hour window fills, on a square-root curve that cuts most of them early, from
# max_parallel at the taper down to 1 just under the brake (or 100 with the brake off): see usage_taper_cap. The dashboard's 5-hour pace line likewise
# runs up to the brake. With the taper on, the pace slowdown also starts fewer workers once 5-hour usage comes within
# PACE_MARGIN points of that pace line: one fewer from PACE_MARGIN points under it, then one fewer per PACE_STEP
# points more, never below 1 (see usage_pace_cap). The lower of the two caps applies, to the same new starts. The 7-day window has none of the three.

USAGE_WINDOWS = {"five_hour": ("5-hour", "5h"), "seven_day": ("7-day", "7d")}
USAGE_LOCK = threading.Lock()


def usage_of(e):
    """The unifiedWindows of a rate_limit_event ({window: {utilization, resetsAt}}), or None for any other event."""
    if e.get("type") != "rate_limit_event":
        return None
    w = (e.get("rate_limit_info") or {}).get("unifiedWindows")
    return w if isinstance(w, dict) else None


def usage_seen(windows):
    """Save the latest reading of each window, only when it changed: a stream reports it on every turn."""
    for k in USAGE_WINDOWS:
        w = windows.get(k)
        try:
            used, resets = float(w["utilization"]), float(w.get("resetsAt") or 0)
        except (TypeError, KeyError, ValueError):
            continue
        with USAGE_LOCK:
            was = usage_reading(setting(f"usage:{k}"))
            if was and (was["utilization"], was["resetsAt"]) == (used, resets):
                continue
            set_setting(f"usage:{k}", json.dumps({"utilization": used, "resetsAt": resets, "ts": time.time()}))


def usage_reading(raw):
    """A stored reading, {utilization, resetsAt, ts}, or None."""
    try:
        r = json.loads(raw or "null")
        return {k: float(r.get(k) or 0) for k in ("utilization", "resetsAt", "ts")} if isinstance(r, dict) else None
    except (TypeError, ValueError, AttributeError):
        return None


def usage_brake_pct(s):
    try:
        return max(0, int(float(s.get("usage_brake") or 0)))
    except ValueError:
        return 0


def usage_taper_pct(s):
    try:
        return max(0, int(float(s.get("usage_taper") or 0)))
    except ValueError:
        return 0


def usage_taper_cap(pct, taper, most, brake=0):
    """How many Claude workers may run at pct percent of the 5-hour window. With end = brake (100 with the brake off)
    and frac = (pct - taper) / (end - taper): max(1, most - ceil(sqrt(frac) * (most - 1))) above the taper, so the cap
    falls fast just past the taper and slowly near the brake, reaching 1 before it. None (no cap) at or below the
    taper, with the taper off, or when end is at or below the taper (no room to taper)."""
    end = brake if brake > 0 else 100
    if not taper or pct <= taper or end <= taper:
        return None
    # rounded before ceil, so an exact step boundary (38.8 at most 6, taper 30, brake 85) stays on the lower step
    return max(1, most - math.ceil(round(math.sqrt((pct - taper) / (end - taper)) * (most - 1), 9)))


PACE_STEP = 3  # points ahead of pace per worker dropped
PACE_MARGIN = 6  # points before the pace line where the pace slowdown starts
FIVE_HOUR = 18000  # the 5-hour window's length in seconds, as the dashboard's USAGE_LEN.five_hour


def usage_lead(pct, resets_at, now, brake=0):
    """How many points the 5-hour window's usage (pct, exact) is ahead of an even pace, the dashboard's usagePace:
    pace = elapsed share of the window * end, end = brake (100 with the brake off). None with no live reading."""
    if not resets_at or resets_at <= now:
        return None
    end = brake if brake > 0 else 100
    elapsed = min(FIVE_HOUR, max(0, FIVE_HOUR - (resets_at - now))) / FIVE_HOUR
    return pct - elapsed * end


def usage_pace_cap(lead, taper, most):
    """How many Claude workers may run while usage is lead points ahead of pace (negative: under it): max(1, most -
    ceil((lead + PACE_MARGIN) / PACE_STEP)), so one fewer from PACE_MARGIN points under the line, then one fewer per
    PACE_STEP points. None (no cap) at PACE_MARGIN or more points under pace, with no live reading (lead None), or with
    the taper off. Rounded before ceil, so an exact step boundary (lead -3, 0, 3) stays on the higher cap."""
    if not taper or lead is None or lead <= -PACE_MARGIN:
        return None
    return max(1, most - math.ceil(round((lead + PACE_MARGIN) / PACE_STEP, 9)))


def claude_running(find=None):
    """The running Claude workers, for the taper: running tasks that are not a deploy or release."""
    find = find or task
    return sum(1 for i in list(THREADS) if (t := find(i)) and t["kind"] not in ("deploy", "release"))


def usage_info(s=None, now=None):
    """The usage windows and the brake, for the scheduler and the dashboard: {brake, windows: [{key, label, short,
    pct, resetsAt, resets, ts}], braked, taper}. A reading whose reset has passed is stale and counts as 0. braked is the
    5-hour window when it is at or above the brake, or None: the 7-day window never brakes. taper is {pct, cap, max, ahead,
    words} while the 5-hour window's effective cap (the lower of the taper cap and the pace cap) is below
    max_parallel, else None. pace is True when the pace cap binds, and then ahead is the signed points ahead of pace
    (negative: under it, since the pace cap starts PACE_MARGIN points before the line); else pace is False and
    ahead 0."""
    s = settings_now() if s is None else s
    brake, now, wins = usage_brake_pct(s), time.time() if now is None else now, []
    for k, (label, short) in USAGE_WINDOWS.items():
        r = usage_reading(s.get(f"usage:{k}"))
        if not r:
            continue
        live = r["resetsAt"] > now
        pct = round(r["utilization"] * 100, 6) if live else 0  # rounded: 0.29 * 100 is 28.999999999999996
        wins.append({"key": k, "label": label, "short": short, "pct": round(pct), "exact": pct,
                     "resetsAt": r["resetsAt"], "resets": clock(r["resetsAt"]) if live else "", "ts": r["ts"]})
    braked = next((w for w in wins if w["key"] == "five_hour" and brake and w["exact"] >= brake), None)
    if braked:
        braked = {**braked, "words": f"Usage at {braked['pct']}% of the {braked['label']} window · starts again "
                                     f"after {braked['resets']}"}
    try:
        most = int(s.get("max_parallel") or 1)
    except ValueError:
        most = 1
    five = next((w for w in wins if w["key"] == "five_hour"), None)
    tp = usage_taper_pct(s)
    cap = usage_taper_cap(five["exact"], tp, most, brake) if five else None
    lead = usage_lead(five["exact"], five["resetsAt"], now, brake) if five else None
    pace = usage_pace_cap(lead, tp, most)
    ahead, paced, why = 0, False, ""
    if pace is not None and (cap is None or pace <= cap):  # the pace cap binds
        cap, paced = pace, True
        ahead = max(1, round(lead)) if lead > 0 else round(lead)
        n = abs(ahead)
        why = (f", {n} point{'s' * (n != 1)} {'ahead of' if ahead > 0 else 'under'} pace" if ahead
               else ", at pace")
    taper = None
    if cap is not None and cap < most:
        taper = {"pct": five["pct"], "cap": cap, "max": most, "ahead": ahead, "pace": paced,
                 "words": f"Usage at {five['pct']}% of the 5-hour window{why} · {cap} of {most} workers"}
    return {"brake": brake, "windows": wins, "braked": braked, "taper": taper}


def usage_braked():
    """The queue is at the usage brake: start no new Claude session (see starts_claude). Notifies once per window per
    reset period."""
    s = settings_now()
    b = usage_info(s)["braked"]
    if not b:
        return False
    key, period = f"usage_noted:{b['key']}", str(b["resetsAt"])
    if s.get(key) != period:
        set_setting(key, period)
        print(f"usage brake: {b['words']}", flush=True)
        notify("Claude usage brake", f"Usage at {b['pct']}% of the {b['label']} window: no new tasks until "
                                     f"{b['resets']}")
    return True


def starts_claude(t):
    """Whether starting queued task t starts a Claude session, so the usage brake holds it. A deploy only runs its
    command, and a task whose start_at is past 'claude' resumes at its gate, merge or promote; those hold back no
    usage, so they start while braked (a merge or promote that needs a worker for a conflict still runs one), and so
    does a release, which only gates."""
    return t["kind"] not in ("deploy", "release") and t["start_at"] == "claude"


# ---------------------------------------------------------------- scheduler

def scheduler():
    n = 0
    while True:
        try:
            run_schedules()  # before schedule_once, so a task a schedule queues can start in the same pass
            schedule_once()
            if n % 3 == 0:  # every 6 s: a scan for a running build takes up to 0.4 s
                unblock_releases()
                restart_if_idle()
            if n % 5 == 0 and repo_configs_moved():  # every 10 s: main moved a claude-ops.toml, or a clone arrived
                load_projects()
            if n % 30 == 0 and not STOPPING.is_set():  # every minute: promote trains whose time is up
                release_trains()
            if n % (PR_POLL_EVERY // 2) == 0 and not STOPPING.is_set():  # branch tasks' open PRs, off this thread
                poll_soon()
            if n % (SWEEP_EVERY // 2) == 0 and not STOPPING.is_set():  # the worktree janitor, off this thread
                threading.Thread(target=sweep_all, daemon=True).start()
        except Exception as e:
            print("scheduler error:", e, flush=True)
        n += 1
        time.sleep(2)


def schedule_once():
    waiting = limited()  # first, so the limit clears and its tasks requeue even while the queue is paused
    held = RESTART_AFTER and not RESTART_AFTER.get("deploys_only")  # a merge's restart waits for the queue to go idle
    if setting("paused") == "1" or STOPPING.is_set() or waiting or held:
        return
    braked = usage_braked()  # running work carries on; no new Claude session starts until the window resets
    taper = usage_info()["taper"]  # below the brake, fewer new Claude workers as the 5-hour window fills
    claude = claude_running() if taper else 0
    slots = int(setting("max_parallel") or 1) - len(THREADS)
    for t in q("SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, id"):
        quick = t["start_at"] == "pr"  # a branch task's PR step: git and gh calls, no worker, so no slot
        if slots <= 0 and not quick:
            continue
        p = PROJECTS.get(t["project"])
        if not p or setting(f"paused:{p.name}") == "1":
            continue
        if getattr(p, "missing", None):  # its repo file lists a repo folder you haven't cloned
            continue
        if restart_holds(t):  # a Promote's restart waits for running deploys: a new one would hold it longer
            continue
        if t["not_before"] and time.time() < t["not_before"]:  # Start later: once due, it takes its normal place
            continue
        if braked and starts_claude(t):
            continue
        if taper and starts_claude(t) and claude >= taper["cap"]:
            continue
        if t["id"] in THREADS:  # resumed before its paused pipeline finished stopping
            continue
        if t["id"] in TASK_CHAT_LIVE:  # a chat turn has its session: the worker starts when the reply is in
            continue
        if p.max_parallel and not quick:
            mine = sum(1 for i in THREADS if (task(i) or {}).get("project") == p.name)
            if mine >= p.max_parallel:
                continue
        if t["depends_on"] and dep_waits(t, task(t["depends_on"])):
            continue
        if holder := branch_holder(t):  # one task per branch at a time
            why = f"waiting for task #{holder['id']} on {t['branch']}"
            if t["phase"] != why:
                upd(t["id"], phase=why)
            continue
        # a deploy of a direct_main project starts only with main_lock free: a merge holding it is moving main
        held = p.main_lock if t["kind"] == "deploy" and p.direct_main else None
        if held and not held.acquire(blocking=False):
            continue
        try:
            if exclusive_blocker(t):
                continue
            upd(t["id"], state="running", phase="starting", pid=None, pid_start=None)
        finally:
            if held:
                held.release()
        if task(t["id"])["state"] != "running":  # paused or cancelled just now
            continue
        ev(t["id"], "started")
        th = threading.Thread(target=pipeline, args=(t["id"],), daemon=True)
        THREADS[t["id"]] = th
        th.start()
        slots -= not quick
        if t["kind"] not in ("deploy", "release") and not quick:
            claude += 1


def exclusive_blocker(t):
    """The deploy or release of t's project that t has to wait for, or None. A project runs one deploy at a time,
    and never a deploy and a release at once: both move main. Releases are one at a time already (queue_release).
    An open one blocks unless it is queued behind t too, so two waiting ones never wait for each other.
    direct_main: a merge about to move main (start_at 'main') waits for a deploy that has started and not ended,
    and a deploy waits for a merge that is moving main. Check and start a deploy with p.main_lock held (schedule_once),
    as land_on_main checks with it held, so the two never both go."""
    p = PROJECTS.get(t["project"])
    if t.get("start_at") == "pr" and q("SELECT 1 FROM prs WHERE task_id=? AND state='MERGED' AND synced=0", (t["id"],)):
        return q("SELECT id,kind,state,priority FROM tasks WHERE project=? AND kind='deploy' AND state NOT IN "
                 "(?,?,?,'queued') ORDER BY id", (t["project"], *ENDED), one=True)  # a merged PR moves main next
    if getattr(p, "direct_main", False) and t["kind"] in MERGES and t.get("start_at") == "main":
        return q("SELECT id,kind,state,priority FROM tasks WHERE project=? AND kind='deploy' AND state NOT IN "
                 "(?,?,?,'queued') ORDER BY id", (t["project"], *ENDED), one=True)
    if t["kind"] not in ("deploy", "release"):
        return None
    if t["kind"] == "deploy" and getattr(p, "direct_main", False):
        o = q(f"SELECT id,kind,state,priority FROM tasks WHERE project=? AND id!=? AND kind IN "
              f"({','.join('?' * len(MERGES))}) AND start_at='main' AND state IN ('running','merging') ORDER BY id",
              (t["project"], t["id"], *MERGES), one=True)
        if o:
            return o
    kinds = ("deploy", "release") if t["kind"] == "deploy" else ("deploy",)
    for o in q(f"SELECT id,kind,state,priority FROM tasks WHERE project=? AND id!=? AND kind IN "
               f"({','.join('?' * len(kinds))}) AND state NOT IN (?,?,?) ORDER BY priority DESC, id",
               (t["project"], t["id"], *kinds, *ENDED)):
        if o["state"] != "queued" or (-o["priority"], o["id"]) < (-t["priority"], t["id"]):
            return o
    return None


# ---------------------------------------------------------------- recurring jobs (README: ## Recurring jobs)
# A schedule queues an ordinary task on a timetable. Its `every` is local wall-clock time; next_at is an epoch.

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()
EVERY_FORMS = "'day HH:MM', 'weekday HH:MM', 'mon HH:MM' (any of mon..sun, or a list such as 'mon,thu HH:MM'), 'hour'"
EVERY_RE = re.compile(r"(day|weekday|[a-z]{3}(?:,[a-z]{3})*) (\d{1,2}):(\d{2})")


def parse_every(text):
    """(days, hour, minute) for an `every`, days a tuple of weekdays (0 = Monday); (None, None, 0) for 'hour'.
    ValueError, naming the accepted forms, for anything else."""
    t = " ".join(str(text or "").lower().replace(", ", ",").split())
    if t == "hour":
        return None, None, 0
    m = EVERY_RE.fullmatch(t)
    if m and int(m.group(2)) < 24 and int(m.group(3)) < 60:
        w = m.group(1)
        days = tuple(range(7)) if w == "day" else tuple(range(5)) if w == "weekday" else \
            tuple(sorted({DAYS.index(d) for d in w.split(",") if d in DAYS}))
        if w in ("day", "weekday") or len(days) == len(w.split(",")):
            return days, int(m.group(2)), int(m.group(3))
    raise ValueError(f"every {text!r} is not a schedule: use one of {EVERY_FORMS}, in local time")


def every_text(text):
    """An `every` in its tidy form: 'mon,thu 09:00'. ValueError as parse_every."""
    days, h, m = parse_every(text)
    if days is None:
        return "hour"
    w = "day" if len(days) == 7 else "weekday" if days == tuple(range(5)) else ",".join(DAYS[d] for d in days)
    return f"{w} {h:02d}:{m:02d}"


def next_slot(every, after):
    """The first slot of `every` strictly after epoch `after`, in local time. A day's HH:MM is made with mktime from
    the local date, so it stays HH:MM across a daylight-saving change (a time the change skips comes out an hour
    later). 'hour' is the next local :00, counted in real seconds, so an hour repeated in autumn runs twice."""
    days, h, mi = parse_every(every)
    lt = time.localtime(after)
    if days is None:
        return after - lt.tm_min * 60 - lt.tm_sec - (after % 1) + 3600
    d0 = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
    for i in range(9):
        d = d0 + datetime.timedelta(days=i)
        if d.weekday() not in days:
            continue
        at = time.mktime((d.year, d.month, d.day, h, mi, 0, 0, 0, -1))
        if at > after:
            return at
    raise ValueError(f"no slot for {every!r}")  # unreachable: every form has a day within a week


# ---------------------------------------------------------------- start later (README: ## Start later)
# A queued task with not_before in the future waits for it; then it is an ordinary queued task in its normal place.

START_FORMS = ("'YYYY-MM-DD HH:MM' (or YYYY-MM-DDTHH:MM), 'HH:MM' (today, or tomorrow once passed), "
               "'<day> HH:MM' (mon..sun, today or tomorrow), or 'reset' (after the next usage-limit reset), in local "
               "time")
START_RE = re.compile(r"(?:([a-z]+) )?(\d{1,2}):(\d{2})")


def start_at_text(text, now=None):
    """The epoch a local time names, for Start later: an ISO date-time, HH:MM or '<day> HH:MM' (see START_FORMS).
    ValueError, naming the forms, for anything it can't read or that is not in the future."""
    now = time.time() if now is None else now
    t = " ".join(str(text or "").strip().lower().split())
    bad = ValueError(f"start {text!r} is not a time: use {START_FORMS}")
    at = None
    m = START_RE.fullmatch(t)
    if m:
        day, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
        if h > 23 or mi > 59:
            raise bad
        if day is None or day in DAYS:  # the next such slot: next_slot's day and weekday parsing
            at = next_slot(f"{day or 'day'} {h:02d}:{mi:02d}", now)
        elif day in ("today", "tomorrow"):
            lt = time.localtime(now)
            d = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday) + datetime.timedelta(days=day == "tomorrow")
            at = time.mktime((d.year, d.month, d.day, h, mi, 0, 0, 0, -1))
        else:
            raise bad
    else:
        try:
            dt = datetime.datetime.fromisoformat(t.upper() if "t" in t else t)
        except ValueError:
            raise bad from None
        at = dt.timestamp()  # a naive one is local time, as mktime takes it
    if at <= now:
        raise ValueError(f"start {text!r} is {start_clock(at, now)}, in the past: give a time still ahead "
                         f"({START_FORMS})")
    return at


def reset_start(s=None, now=None):
    """(epoch, window) for "after the next reset": the window's resetsAt plus LIMIT_GRACE. The window is the limit
    the queue is waiting out, else the one holding the brake, else the live 5-hour reading. ValueError without one."""
    s = settings_now() if s is None else s
    now = time.time() if now is None else now
    lim = limit_info(s)
    if lim and lim["until"] + LIMIT_GRACE > now:
        return lim["until"] + LIMIT_GRACE, LIMIT_KINDS.get(lim["kind"]) or "usage-limit"
    u = usage_info(s, now)
    w = u["braked"] or next((w for w in u["windows"] if w["key"] == "five_hour" and w["resetsAt"] > now), None)
    if not w:
        raise ValueError("no usage reading yet: give a time instead")
    return w["resetsAt"] + LIMIT_GRACE, w["label"]


def start_time(v, now=None, s=None):
    """(epoch, window) for a not_before value: "reset" (window is its label, as reset_start), a time string as
    start_at_text, or an epoch number (window None). ValueError for anything else or a time in the past."""
    now = time.time() if now is None else now
    if isinstance(v, bool):
        raise ValueError(f"start {v!r} is not a time: use {START_FORMS}")
    if isinstance(v, (int, float)):
        if v <= now:
            raise ValueError(f"start {v} is in the past: give a time still ahead ({START_FORMS})")
        return float(v), None
    if str(v).strip().lower() == "reset":
        return reset_start(s, now)
    return start_at_text(v, now), None


def start_clock(ts, now=None):
    """15:01 today, else Tue 09:00 within the coming week, else 2026-11-03 09:00."""
    now = time.time() if now is None else now
    lt, today = time.localtime(ts), time.localtime(now)
    if lt[:3] == today[:3]:
        return time.strftime("%H:%M", lt)
    days = (datetime.date(*lt[:3]) - datetime.date(*today[:3])).days
    return time.strftime("%a %H:%M" if 0 < days < 7 else "%Y-%m-%d %H:%M", lt)


def start_now(tid):
    """Start now: clear the task's not_before, so it starts as soon as the queue can."""
    t = task(tid)
    if not t or not t["not_before"]:
        raise ValueError("this task does not wait for a start time")
    q("UPDATE tasks SET not_before=NULL, not_before_why=NULL WHERE id=?", (tid,))
    ev(tid, "Start now: no longer waits for its start time")


def start_words(t, now=None):
    """'waits until Tue 09:00', or 'waits for the 5-hour reset · 15:01', while t's not_before is ahead; else None."""
    now = time.time() if now is None else now
    nb = t.get("not_before")
    if not nb or nb <= now:
        return None
    why = t.get("not_before_why")
    return f"waits for the {why} reset · {start_clock(nb, now)}" if why else f"waits until {start_clock(nb, now)}"


def schedule(sid):
    return q("SELECT * FROM schedules WHERE id=?", (sid,), one=True)


def schedule_fields(b, old=None):
    """The schedules row a create or edit body makes, checked: a known project, a title or body, a valid every."""
    s = dict(old or {})
    for k in ("project", "title", "body", "every", "model", "effort", "hold", "priority", "research", "after_schedule"):
        if k in b:
            s[k] = b[k]
    proj(s.get("project") or "")
    s["title"], s["body"] = (s.get("title") or "").strip()[:120], (s.get("body") or "").strip()
    if not s["title"] and not s["body"]:
        raise ValueError("give the schedule a title or a body")
    s["title"] = s["title"] or title_of(s["body"])
    s["every"] = every_text(s.get("every"))
    s["model"], s["effort"] = model_name(s.get("model")), effort_name(s.get("effort"))
    s["hold"], s["priority"] = int(bool(s.get("hold"))), int(s.get("priority") or 0)
    s["research"] = research_tier(s["research"]) if s.get("research") else None  # its body is the question (RES-6)
    if "after_schedule" not in b and s.get("after_schedule") and not schedule(s["after_schedule"]):
        s["after_schedule"] = None  # it pointed at a deleted schedule: that is none
    s["after_schedule"] = after_schedule_id(s.get("after_schedule"), s.get("id"))
    return s


def after_schedule_id(v, sid=None):
    """An after_schedule value (12, "#12", "none", "" or None) as a schedule id or None, checked (SCHED-1): it must
    exist, and following after_schedule from it must not come back to schedule sid."""
    v = str(v if v is not None else "").strip().lstrip("#").lower()
    if v in ("", "none", "null", "0"):
        return None
    if not v.isdigit():
        raise ValueError(f"after_schedule {v!r} is not a schedule id")
    a = int(v)
    if sid is not None and a == sid:
        raise ValueError(f"schedule #{sid} can't wait on itself")
    if not schedule(a):
        raise ValueError(f"after_schedule: no schedule #{a}")
    seen, cur = {a}, schedule(a)["after_schedule"]
    while cur and sid is not None:
        if cur == sid:
            raise ValueError(f"schedule #{a} already waits on #{sid}: after_schedule #{a} would make a cycle")
        if cur in seen:
            break
        seen.add(cur)
        cur = (schedule(cur) or {}).get("after_schedule")
    return a


def add_schedule(b, now=None):
    s = schedule_fields(b)
    return q("INSERT INTO schedules (project, title, body, every, next_at, paused, created, model, effort, hold, "
             "priority, research, after_schedule) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?)",
             (s["project"], s["title"], s["body"], s["every"], next_slot(s["every"], now or time.time()),
              time.time(), s["model"], s["effort"], s["hold"], s["priority"], s["research"], s["after_schedule"]))


def edit_schedule(sid, b, now=None):
    old = schedule(sid)
    if not old:
        raise ValueError(f"no schedule #{sid}")
    s = schedule_fields(b, old)
    nxt = next_slot(s["every"], now or time.time()) if s["every"] != old["every"] else old["next_at"]
    q("UPDATE schedules SET project=?, title=?, body=?, every=?, next_at=?, model=?, effort=?, hold=?, priority=?, "
      "research=?, after_schedule=? WHERE id=?", (s["project"], s["title"], s["body"], s["every"], nxt, s["model"],
                                                  s["effort"], s["hold"], s["priority"], s["research"],
                                                  s["after_schedule"], sid))


def queue_scheduled(s, why):
    """Queue schedule s's task, as /api/tasks would, and make it the schedule's last task. A recurring research job
    builds a fresh research brief each time, with a dated slug (RES-6). With after_schedule, the task waits on that
    schedule's last task while it is open, as `--after` does (SCHED-1)."""
    origin = {"ref": f"schedule:{s['id']}", "kind": "schedule", "id": s["id"], "project": s["project"],
              "title": s["title"]}
    dep, note = None, ""
    a = schedule(s["after_schedule"]) if s.get("after_schedule") else None  # a deleted one counts as none
    last = task(a["last_task"]) if a and a["last_task"] else None
    if last and last["state"] not in ENDED:
        dep, note = last["id"], f"; waits on #{last['id']}, schedule #{a['id']}'s task"
    elif last and last["state"] != "done":
        note = f"; schedule #{a['id']}'s last task #{last['id']} {last['state']}, so it runs anyway"
    if s.get("research"):
        tid = queue_research(s["project"], s["body"] or s["title"], s["research"], s["hold"], priority=s["priority"],
                             model=s["model"], effort=s["effort"], origin=origin, dated=True, depends_on=dep)["id"]
    else:
        tid = add_task(s["project"], s["body"] or s["title"], s["title"], s["priority"], s["hold"], depends_on=dep,
                       model=s["model"], effort=s["effort"], origin=origin)
    ev(tid, f"queued by schedule #{s['id']} ({s['every']}): {why}{note}")
    q("UPDATE schedules SET last_task=? WHERE id=?", (tid, s["id"]))
    return tid


def run_schedules(now=None):
    """Queue the task of every schedule that is due. One whose last task is still open skips the slot (no pile-up).
    next_at moves to the first slot after now, so slots missed while the daemon was down run once, not once each."""
    if STOPPING.is_set():
        return []
    now = now or time.time()
    ids = []
    release_schedule_waits()
    due = q("SELECT * FROM schedules WHERE paused=0 AND next_at<=? ORDER BY next_at, id", (now,))
    by_id = {s["id"]: s for s in due}

    def depth(s, n=0):  # how many due schedules s waits behind: those queue first, so it waits on their fresh task
        a = by_id.get(s["after_schedule"])
        return depth(a, n + 1) if a and n < len(by_id) else n
    for s in sorted(due, key=depth):
        try:
            nxt = next_slot(s["every"], now)
        except ValueError:  # a row edited by hand: park it rather than fail every pass
            q("UPDATE schedules SET paused=1 WHERE id=?", (s["id"],))
            print(f"schedule #{s['id']}: bad every {s['every']!r}, paused", flush=True)
            continue
        q("UPDATE schedules SET next_at=? WHERE id=?", (nxt, s["id"]))  # first: an error below never repeats a slot
        last = task(s["last_task"]) if s["last_task"] else None
        slot = time.strftime("%a %H:%M", time.localtime(s["next_at"]))
        if last and last["state"] not in ENDED:
            ev(last["id"], f"schedule #{s['id']} skipped its {slot} slot: this task is still {last['state']}")
            continue
        try:
            ids.append(queue_scheduled(s, f"its {slot} slot" + (" (missed while the queue was down)"
                                                                 if now - s["next_at"] > 120 else "")))
        except Exception as e:  # its project was archived, say: the slot passes, and the next one tries again
            print(f"schedule #{s['id']}: not queued: {e}", flush=True)
    return ids


def release_schedule_waits():
    """A schedule's task that waits on another schedule's task stops waiting when that one fails or is cancelled
    (SCHED-1): a broken scout doesn't hold the job after it. A plain --after wait keeps waiting."""
    for r in q("SELECT t.id, d.id AS dep, d.state AS dep_state, s.after_schedule FROM schedules s "
               "JOIN tasks t ON t.id = s.last_task JOIN tasks d ON d.id = t.depends_on "
               "WHERE s.after_schedule IS NOT NULL AND t.state='queued' AND d.state IN ('failed','cancelled')"):
        q("UPDATE tasks SET depends_on=NULL WHERE id=?", (r["id"],))
        ev(r["id"], f"no longer waits on #{r['dep']} (schedule #{r['after_schedule']}'s task, {r['dep_state']}): "
                    "runs anyway")


def schedules_view():
    return q("SELECT s.*, t.state AS last_state, t.title AS last_title, a.title AS after_title FROM schedules s "
             "LEFT JOIN tasks t ON t.id = s.last_task LEFT JOIN schedules a ON a.id = s.after_schedule "
             "ORDER BY s.project, s.next_at")


SCHEDULE_BLOCK = re.compile(r"```schedule[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)


def schedules_from_reply(p, reply, did):
    """Create every schedule in the reply's ```schedule blocks. Returns (reply without blocks, note for Claude)."""
    made, notes = [], []
    for block in SCHEDULE_BLOCK.findall(reply):
        try:
            items = json.loads(block)
            items = items if isinstance(items, list) else [items]
        except ValueError as e:
            notes.append(f"your last schedule block was not valid JSON ({e}); no schedule was made from it")
            continue
        for it in items:
            try:
                if not isinstance(it, dict):
                    raise ValueError("each schedule is a JSON object")
                sid = add_schedule({"project": p.name, **it})
                s = schedule(sid)
                made.append(f"#{sid} {s['title']!r} ({s['every']}, next "
                            f"{time.strftime('%a %d %b %H:%M', time.localtime(s['next_at']))})")
            except Exception as e:
                notes.append(f"schedule {str((it or {}).get('title', '') if isinstance(it, dict) else it)[:60]!r} "
                             f"was not made: {e}")
    if made:
        print(f"chat #{did} made schedules {', '.join(made)}", flush=True)
        notes.insert(0, "your last reply made schedule " + "; ".join(made))
    return SCHEDULE_BLOCK.sub("", reply).strip(), "; ".join(notes)


# ---------------------------------------------------------------- research tasks (README: ## Research)
# A research run is a normal task flagged research (RES-1): its brief has the worker write one report to
# docs/research/<slug>.md in its worktree, and it gates and merges like code. A merged report you haven't opened is a
# Read to-do (RES-5). Research to-dos are ordinary claude-ops to-dos: the research repo's own list is retired (RES-8).

RESEARCH_DIR = "docs/research"
# tier -> (minutes per worker turn, --max-budget-usd, what the tier means): research.py's TIME_LIMIT_MIN and RUN_SIZES
RESEARCH_TIERS = {"T0": (10, 2, "a quick answer: a handful of searches, no subagents"),
                  "T1": (30, 6, "a normal run: enough sources to be confident, no parallel workers unless needed"),
                  "T2": (80, 20, "a deep run: broad sources, parallel workers allowed")}
SLUG_STOP = {"a", "an", "the", "is", "are", "of", "for", "to", "in", "on", "and", "or", "what", "whats", "how", "does",
             "do", "anyone", "there", "any", "can", "i", "we", "it", "its", "be", "with", "by", "should", "my", "me",
             "this", "that", "who", "which", "into"}  # research.make_slug's
REPORT_DATES = {}  # repo folder -> (its HEAD, {report name: time of the last commit that touched it})


def research_tier(tier):
    tier = str(tier or "T1").strip().upper()
    if tier not in RESEARCH_TIERS:
        raise ValueError(f"tier must be one of {', '.join(RESEARCH_TIERS)}, not {tier!r}")
    return tier


def research_of(t):
    """A task's research flag as a dict, or None for an ordinary task."""
    try:
        r = json.loads(t["research"]) if t and t.get("research") else None
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


REPORTS_MAX = 5


def reports_of(t):
    """The reports a task's done result listed (RES-10), as [{path, read}], or [] for none."""
    r = t.get("reports") if t else None
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except ValueError:
            return []
    return [x for x in r if isinstance(x, dict) and x.get("path")] if isinstance(r, list) else []


def set_reports(tid, given):
    """Store a done result's "reports" on the task, replacing an earlier round's (RES-10): non-empty relative paths
    with no .., normalised, at most REPORTS_MAX, and not the research report it has already. Returns the event to log,
    or "" when there is nothing to say."""
    t = task(tid)
    own = (research_of(t) or {}).get("path")
    keep, dropped = [], []
    for x in given if isinstance(given, list) else [given]:
        rel = os.path.normpath(x.strip()) if isinstance(x, str) and x.strip() else None
        if not rel or os.path.isabs(rel) or ".." in Path(rel).parts or rel == ".":
            dropped.append(repr(x)[:80])
        elif len(keep) >= REPORTS_MAX:
            dropped.append(f"{rel} (more than {REPORTS_MAX})")
        elif rel != own and rel not in keep:
            keep.append(rel)
    old = {x["path"]: x.get("read") for x in reports_of(t)}  # a report read already stays read
    upd(tid, reports=json.dumps([{"path": r, "read": old.get(r)} for r in keep]) if keep else None)
    said = ("reports: " + ", ".join(keep)) if keep else ""
    if dropped:
        said += ("; " if said else "") + "dropped reports: " + ", ".join(dropped)
    return said


def report_rel(p, slug):
    """Where a report goes, relative to a task worktree: docs/research/<slug>.md, in the first repo of a workspace."""
    return f"{p.parts[0].prefix}{RESEARCH_DIR}/{slug}.md"


def research_slug(p, question, dated=False):
    """A slug from the question's words, unique among the reports on dev and the project's open research tasks."""
    words = [w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in SLUG_STOP]
    base = "-".join(words[:6]) or "research"
    if dated:
        base += "-" + time.strftime("%Y-%m-%d")
    taken = {Path(r["path"]).stem for r in research_reports(p)}
    taken |= {r["slug"] for t in q(f"SELECT research FROM tasks WHERE project=? AND research IS NOT NULL AND state NOT "
                                   f"IN ({','.join('?' * len(ENDED))})", (p.name, *ENDED))
              if (r := research_of(t))}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def research_brief(question, tier, path):
    """The body of a research task: what the worker answers, where the report goes and what else it may touch."""
    minutes, budget, means = RESEARCH_TIERS[tier]
    return f"""Research run at tier {tier} ({means}).

## Question

{question}

## What to do

1. Answer the question at tier {tier} with the `/claude-ops:research` skill (the Skill tool) in project mode: this
   repo is the target, and this task's worktree is where the report goes.
2. Write the report to `{path}` in this worktree, and nowhere else. This overrides the research skill's own paths:
   do not write briefs/, reports/, topics/, sources/registry.md or watchlist.md anywhere.
3. Start the report with a `# ` heading that names the subject, then one paragraph that answers the question in plain
   words (the dashboard shows the heading as the title and that paragraph as the summary). Then the findings, with a
   source for every claim you quote or count, and a `## Sources` list of links at the end. Say what you could not
   reach.
4. Commit only that file, with the message `Research: <the heading>`. Touch nothing else in the repo: no code, no
   docs, no README.
5. Report done with a summary that is the answer in two or three sentences.

Limits: this worker is stopped after {minutes} minutes and at about ${budget} of API-equivalent usage, so finish and
commit the report well before that. Don't run the project's build or app; the gate runs on its own."""


def queue_research(project, question, tier="T1", hold=False, priority=0, model=None, effort=None, origin=None,
                   dated=False, not_before=None, depends_on=None):
    """Queue a research task in project (RES-2). Returns {id, slug, path}."""
    p = proj(project)
    question = (question or "").strip()
    if not question:
        raise ValueError("Write a question first.")
    tier = research_tier(tier)
    slug = research_slug(p, question, dated)
    path = report_rel(p, slug)
    tid = add_task(project, research_brief(question, tier, path), f"Research: {title_of(question)}"[:120], priority,
                   hold, depends_on=depends_on, model=model, effort=effort, origin=origin,
                   not_before=not_before, research={"question": question, "tier": tier, "slug": slug, "path": path})
    ev(tid, f"research at {tier}: writes {path}")
    return {"id": tid, "slug": slug, "path": path}


def new_research(project, b):
    """POST /api/projects/<name>/research: queue a run, and with every a recurring job whose first run it is."""
    tier, every = research_tier(b.get("tier")), (b.get("every") or "").strip()
    if every:
        every_text(every)  # refused before anything is queued
    question = (b.get("question") or "").strip()
    out = queue_research(project, question, tier, bool(b.get("hold")), model=b.get("model") or None,
                         effort=b.get("effort") or None, not_before=b.get("not_before") or None)
    out["schedule"] = None
    if every:
        out["schedule"] = add_schedule({"project": project, "title": f"Research: {title_of(question)}"[:120],
                                        "body": question, "every": every, "hold": bool(b.get("hold")),
                                        "research": tier})
        q("UPDATE schedules SET last_task=? WHERE id=?", (out["id"], out["schedule"]))  # no second run while it runs
    return out


def report_head(text):
    """(title, summary) of a report: its first heading, and its first paragraph of prose, in one line."""
    title, para, body = "", [], text.split("\n")
    if body and body[0].strip() == "---":  # front matter
        end = next((i for i, ln in enumerate(body[1:], 1) if ln.strip() == "---"), 0)
        body = body[end + 1:]
    fence = False
    for ln in body:
        s = ln.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if not title and s.startswith("# "):
            title = s[2:].strip()
            continue
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "|", "---", "<!--", "![")) or re.match(r"^([-*]\s+)?\*\*[^*]{1,40}:\*\*", s):
            if para:
                break
            continue  # headings, tables, rules and **Field:** lines are not the summary
        para.append(re.sub(r"^([-*>]|\d+\.)\s+", "", s))
    summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(para))
    return title, clip(re.sub(r"[*_`]", "", summary), 240)


def report_dates(folder):
    """{report name: unix time of the last commit that touched it} on the dev worktree folder, one git call per HEAD."""
    try:
        head = sh("git", "rev-parse", "HEAD", cwd=folder)
        if REPORT_DATES.get(str(folder), (None,))[0] != head:
            out, seen, ts = sh("git", "log", "--format=%x00%ct", "--name-only", "HEAD", "--", RESEARCH_DIR,
                               cwd=folder), {}, 0
            for ln in out.split("\n"):
                if ln.startswith("\0"):
                    ts = int(ln[1:])
                elif ln.strip():
                    seen.setdefault(Path(ln.strip()).name, ts)
            REPORT_DATES[str(folder)] = (head, seen)
        return REPORT_DATES[str(folder)][1]
    except (RuntimeError, ValueError):
        return {}


def research_reports(p):
    """The reports in docs/research/*.md on p's dev worktree (each repo's, in a workspace), newest first:
    {path, title, date, summary, task, unread}. task is the latest research task that wrote it, unread whether
    that task merged and you haven't opened its report."""
    tasks = {}
    for t in q("SELECT id,state,merged,research FROM tasks WHERE project=? AND research IS NOT NULL ORDER BY id",
               (p.name,)):
        if r := research_of(t):
            tasks[r.get("path")] = (t, r)
    out = []
    for part in p.parts:
        root = part.at(p.dev_wt)
        folder = root / RESEARCH_DIR
        if not folder.is_dir():
            continue
        dates = report_dates(root)
        for f in folder.glob("*.md"):
            if not f.is_file():
                continue
            rel = f"{part.prefix}{RESEARCH_DIR}/{f.name}"
            try:
                with open(f, errors="replace") as fh:
                    title, summary = report_head(fh.read(16384))
            except OSError:
                continue
            t, r = tasks.get(rel, (None, None))
            out.append({"path": rel, "title": title or f.stem, "summary": summary,
                        "date": dates.get(f.name) or f.stat().st_mtime, "task": t["id"] if t else None,
                        "unread": bool(t and t["state"] == "done" and t["merged"] and not r.get("read"))})
    return sorted(out, key=lambda x: (-(x["date"] or 0), x["path"]))


def report_read(project, path):
    """Mark the report at path read: every research task of project that wrote it. Returns their ids (RES-5)."""
    proj(project)
    path = os.path.normpath(path.strip()) if path and path.strip() else ""
    now = time.time()
    ids = [r["id"] for r in q("UPDATE tasks SET research=json_set(research, '$.read', ?) WHERE project=? AND "
                              "research IS NOT NULL AND json_extract(research, '$.path')=? AND "
                              "json_extract(research, '$.read') IS NULL RETURNING id", (now, project, path))]
    for t in reports_with(project, path):  # and a report any task listed (RES-10)
        rs = reports_of(t)
        if any(x["path"] == path and not x.get("read") for x in rs):
            upd(t["id"], reports=json.dumps([{**x, "read": now} if x["path"] == path and not x.get("read") else x
                                             for x in rs]))
            ids.append(t["id"])
    return ids


def reports_with(project, path):
    """The tasks of project whose listed reports (RES-10) include path."""
    return q("SELECT id,reports FROM tasks WHERE project=? AND reports IS NOT NULL AND EXISTS (SELECT 1 FROM "
             "json_each(reports) WHERE json_extract(value, '$.path')=?)", (project, path))


def report_mark(project, path):
    """POST /api/projects/<name>/research/read (RES-5): marked, the tasks this read; already, those read before. Both
    empty means no research task of the project wrote path, which the dashboard says rather than keep the step."""
    marked = report_read(project, path)
    path = os.path.normpath(path.strip()) if path and path.strip() else ""
    already = [r["id"] for r in q("SELECT id FROM tasks WHERE project=? AND research IS NOT NULL AND "
                                  "json_extract(research, '$.path')=? AND json_extract(research, '$.read') IS NOT "
                                  "NULL", (project, path)) if r["id"] not in marked]
    already += [t["id"] for t in reports_with(project, path) if t["id"] not in marked and t["id"] not in already
                and any(x["path"] == path and x.get("read") for x in reports_of(t))]
    return {"ok": True, "path": path, "marked": marked, "already": already}


def serve_repo_file(p, rel):
    """GET /api/files: repo_file. Opening a research report doesn't mark it read: only Mark read does (RES-5)."""
    return repo_file(p, rel)


def project_research(project):
    """GET /api/projects/<name>/research: the Research section of TODAY-4."""
    p = proj(project)
    running = []
    for t in q(f"SELECT id,title,state,research FROM tasks WHERE project=? AND research IS NOT NULL AND state NOT IN "
               f"({','.join('?' * len(ENDED))}) ORDER BY id DESC", (project, *ENDED)):
        t["research"] = research_of(t)
        running.append(t)
    return {"dev": p.dev, "reports": research_reports(p), "running": running,
            "schedules": [s for s in schedules_view() if s["project"] == project and s["research"]]}


def project_documents(project):
    """GET /api/projects/<name>/documents: the files the project's plan & design topics wrote, newest first."""
    proj(project)
    out = []
    for d in q("SELECT id,title FROM designs WHERE project=? AND COALESCE(kind,'design')!='project'", (project,)):
        out += [{"design": d["id"], "title": d["title"], **f} for f in chat_files(d["id"])]
    return sorted(out, key=lambda x: -x["mtime"])


RECAP_TASKS = 8  # done tasks the digest reads
RECAP_DONE = 5  # done tasks the recap lists
RECAP_MODEL = "haiku"
RECAP_PROMPT = """Write a short digest of the recent work in a software project called "{title}", for its owner, who \
wants to pick up where they left off.

Rules:
- 2 or 3 short sentences in plain language.
- Name the themes of the work, not a list of tasks. No task ids.
- Output only the digest: no heading, no preamble.

The last tasks that finished, newest first, each a title and maybe the worker's summary:

{tasks}

Topics still open in the project's conversation:

{topics}
"""


def recap_done(project, limit):
    """The project's tasks that finished done, newest first (releases and deploys aren't tasks here)."""
    return q("SELECT id,title,finished,summary FROM tasks WHERE project=? AND state='done' AND "
             "COALESCE(kind,'task')='task' ORDER BY finished DESC, id DESC LIMIT ?", (project, limit))


def project_recap(project):
    """GET /api/projects/<name>/recap: a project home's Lately and Open (README TODAY-2). Reads only: never calls
    Claude. The digest is the one Summarize last wrote, with how many tasks finished done since."""
    p = proj(project)
    try:
        d = json.loads(setting(f"recap:{project}") or "null")
    except ValueError:
        d = None
    digest = None
    if isinstance(d, dict) and d.get("text"):
        since = q("SELECT COUNT(*) n FROM tasks WHERE project=? AND state='done' AND COALESCE(kind,'task')='task' "
                  "AND id>?", (project, int(d.get("last_task") or 0)), one=True)["n"]
        digest = {"text": d["text"], "written": d.get("written"), "since": since}
    n = q("SELECT COUNT(*) n FROM tasks WHERE project=? AND state='done' AND COALESCE(kind,'task')='task'",
          (project,), one=True)["n"]
    tops = topics(project)
    cur = conv_state(project, tops)["topic"]
    try:
        changes = spec_changes(p)
    except Exception:  # an unreadable change folder: the rest of the recap still shows
        changes = []
    return {"digest": digest if n >= 2 else None, "can_summarize": n >= 2,
            "done": [{"id": t["id"], "title": t["title"], "finished": t["finished"],
                      "summary_first_line": next((ln.strip() for ln in (t["summary"] or "").splitlines() if ln.strip()), "")}
                     for t in recap_done(project, RECAP_DONE)],
            "topics": [{"ref": t["id"], "title": t["title"], "unread": bool(t["unread"]), "current": t["id"] == cur}
                       for t in tops if not t["ended"]],
            "changes": [{"name": c["name"], "sub": c["sub"], "title": c["title"], "done": c["done"],
                         "total": c["done"] + c["open"]} for c in changes]}


def recap_write(project):
    """POST /api/projects/<name>/recap: Summarize. One haiku turn writes the digest from the last RECAP_TASKS done
    tasks and the open topics' titles, saved in recap:<project>. On a failure or the usage limit the old digest
    stays and the answer carries error."""
    p = proj(project)
    rows = recap_done(project, RECAP_TASKS)
    if len(rows) < 2:
        return {**project_recap(project), "error": "Summarize needs at least 2 done tasks."}
    if limited():
        return {**project_recap(project), "error": "Claude is at the usage limit: try again after it resets."}
    tasks = "\n".join(f"- {t['title']}" + (f"\n  {' '.join((t['summary'] or '').split())[:600]}" if (t["summary"] or "").strip()
                                           else "") for t in rows)
    tops = [t["title"] for t in topics(project) if not t["ended"] and (t["title"] or "").strip()]
    text = claude_text(RECAP_PROMPT.format(title=p.title, tasks=tasks,
                                           topics="\n".join(f"- {t}" for t in tops) or "(none)"), RECAP_MODEL)
    if not text:
        return {**project_recap(project), "error": "Couldn't write the summary: Claude gave no answer. Try again."}
    set_setting(f"recap:{project}", json.dumps({"text": text.strip()[:2000], "written": time.time(),
                                                "last_task": max(t["id"] for t in rows)}))
    return project_recap(project)


UNCOMMITTED_DIFF = 400  # diff lines the Commit to-do's card shows
UNCOMMITTED_FILES = 500  # files it lists per repo
UNCOMMITTED_READ = 2 << 20  # bytes of an untracked file read for its line count and diff
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: what a repo with no commit yet diffs against


def project_uncommitted(project):
    """GET /api/projects/<name>/uncommitted: what the Commit to-do's card lists. Per repo with uncommitted files,
    each file's status (new, changed, deleted), lines added and removed (git diff --numstat against HEAD; an untracked
    text file's line count) and mtime, or binary; then one diff of them all, the first UNCOMMITTED_DIFF lines, an
    untracked file as a diff against nothing. Read only: git status, diff and file reads, with no index lock taken.
    {"repos": [{"repo", "files": [{path, status, added, removed, binary, mtime}], "more"}], "diff", "diff_lines",
    "cut"}"""
    p = proj(project)
    repos, diff, total = [], [], 0

    def git(r, *a):
        return subprocess.run(["git", "--no-optional-locks", *a], cwd=r.src, capture_output=True, text=True,
                              errors="replace", timeout=20)

    def add(lines):
        nonlocal total
        total += len(lines)
        diff.extend(lines[:max(0, UNCOMMITTED_DIFF - len(diff))])

    for r in p.parts:
        st = git(r, "status", "--porcelain", "-z", "--untracked-files=all")
        if st.returncode:
            continue
        status, it = {}, iter(st.stdout.split("\0"))
        for e in it:
            if len(e) > 3:
                xy = e[:2]
                status[e[3:]] = ("new" if xy == "??" or "A" in xy else "deleted" if "D" in xy else "changed")
                if e[0] in "RC":
                    next(it, None)
        if not status:
            continue
        base = "HEAD" if not git(r, "rev-parse", "-q", "--verify", "HEAD").returncode else EMPTY_TREE
        num, it = {}, iter(git(r, "diff", "--numstat", "-z", base).stdout.split("\0"))
        for e in it:
            parts = e.split("\t", 2)
            if len(parts) < 3:
                continue
            path = parts[2] or (next(it, ""), next(it, ""))[1]  # a rename: old and new path follow
            num[path] = (None, None) if parts[0] == "-" else (int(parts[0]), int(parts[1]))
        out, new = [], []  # new: the untracked files' diffs, after git's
        for path in sorted(status)[:UNCOMMITTED_FILES]:
            f = {"path": path, "status": status[path], "added": None, "removed": None, "binary": False, "mtime": None}
            try:
                f["mtime"] = os.lstat(r.src / path).st_mtime
            except OSError:
                pass
            if path in num:
                f["added"], f["removed"] = num[path]
                f["binary"] = f["added"] is None
            elif status[path] == "new":  # untracked: not in git's diff
                try:
                    with open(r.src / path, "rb") as fh:
                        data = fh.read(UNCOMMITTED_READ)
                except OSError:
                    data = b""
                if b"\0" in data[:8000]:
                    f["binary"] = True
                    new.append([f"diff --git a/{path} b/{path}", "new file", f"Binary file {path}"])
                else:
                    lines = data.decode("utf-8", "replace").splitlines()
                    f["added"], f["removed"] = len(lines), 0
                    new.append([f"diff --git a/{path} b/{path}", "new file", "--- /dev/null", f"+++ b/{path}",
                         f"@@ -0,0 +1,{len(lines)} @@"] + ["+" + x for x in lines])
            out.append(f)
        add(git(r, "diff", "--no-color", "--no-ext-diff", base).stdout.splitlines())
        for lines in new:
            add(lines)
        repos.append({"repo": str(r.src).replace(str(HOME), "~", 1), "files": out,
                      "more": max(0, len(status) - UNCOMMITTED_FILES)})
    return {"repos": repos, "diff": "\n".join(diff), "diff_lines": total, "cut": total > len(diff)}


RESTART_NOTE = ("The task queue restarted while you were working. Any command you were running was stopped. "
                "Check `git status` and `git log` in this worktree, finish the task, and write $OPS_RESULT.")
PAUSE_NOTE = ("The user paused this task while you were working and has now resumed it. Any command you were "
              "running was stopped. Check `git status` and `git log` in this worktree, finish the task, and "
              "write $OPS_RESULT.")


def recover():
    """On start: resume the tasks the last daemon paused as it stopped, and requeue any it left mid-flight
    (it crashed or was killed) so they resume their Claude session. Tasks you paused stay paused, and so do tasks
    the usage limit paused: limit_clear requeues them once it resets (limited_until is in settings, so it survives).
    A deploy cut off while its command ran is not run again by itself (it publishes things): it fails, and Retry
    starts it over. One whose command had finished goes on moving main."""
    for t in q(f"SELECT id FROM tasks WHERE kind='deploy' AND start_at!='promote' AND (state IN "
               f"({','.join('?' * len(ACTIVE))}) OR (state='paused' AND paused_by='restart'))", ACTIVE):
        q("UPDATE tasks SET state='failed', phase='', paused_by=NULL, pid=NULL, pid_start=NULL, finished=?, error=? "
          "WHERE id=?", (time.time(), "claude-ops restarted while the deploy ran, and stopped its command. Retry "
                                      "starts the deploy over.", t["id"]))
        ev(t["id"], "failed: claude-ops restarted during the deploy")
    for t in q("SELECT * FROM tasks WHERE state='paused' AND paused_by='restart'"):
        killgroup(own_pid(t))  # the last daemon stops its commands as it goes, unless it was killed first
        upd(t["id"], state="queued", paused_by=None, pid=None, pid_start=None)
        ev(t["id"], "resumed after the restart")
    for t in q(f"SELECT * FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        killgroup(own_pid(t))  # a worker from the old daemon would otherwise run alongside its resume
        upd(t["id"], state="queued", phase="", pid=None, pid_start=None, next_prompt=RESTART_NOTE)
        ev(t["id"], "requeued after daemon restart")


def shutdown():
    """The daemon is stopping (opsctl restart or stop, launchd, a kill): start nothing new, pause every running
    task for the next daemon to resume, and give a merge, which can't be paused, a moment to finish."""
    STOPPING.set()
    set_setting("cut_task_chats", json.dumps(list(TASK_CHAT_LIVE)))  # replies on_term stops: resume_chats redoes them
    deadline = time.time() + 45  # launchd's ExitTimeOut (opsctl) is 60
    paused = []
    for t in q(f"SELECT id FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        try:
            pause_task(t["id"], by="restart", kill=False)
            paused.append(t["id"])
        except ValueError:
            pass  # merging
    # Then stop them all at once, so commands that ignore SIGTERM cost STOP_WAIT once, not once per task. A Play's
    # setup or build goes too; a merge's steps are left to finish.
    tasked = list(PROCS.values())
    stop_many([PROCS.get(tid) for tid in paused] + [proc for proc in list(STEPS) if proc not in tasked])
    while q("SELECT id FROM tasks WHERE state='merging'") and time.time() < deadline:
        time.sleep(0.5)


def pause_task(tid, by=None, kill=True):
    """Stop a task now and keep its place: a worker resumes its Claude session, a gate starts over.
    by='restart': the daemon is stopping, and the next one resumes the task.
    by='limit': its worker hit the usage limit, and the queue resumes it after the reset (see limit_clear).
    kill=False leaves stopping its process to the caller (shutdown stops them all at once)."""
    t = task(tid)
    if t["state"] == "merging" and by != "limit":  # the limit stops a merge only in a Claude step, before main moves
        raise ValueError("it is merging; that can't be paused, and it is nearly done")
    if t["state"] not in ("queued", *ACTIVE):
        raise ValueError(f"task is {t['state']}")
    kw = {}
    if t["state"] == "gating" and t["kind"] in MERGES:
        kw = {"start_at": "integrate"}  # Claude's work is done: rebase and run the gate again
    elif t["state"] == "running" and t["phase"] == "claude working" and t["session_id"]:
        kw = {"start_at": "claude", "next_prompt": {"restart": RESTART_NOTE, "limit": LIMIT_NOTE}.get(by, PAUSE_NOTE)}
    # Otherwise Claude has not had its prompt yet, and run_claude keeps it for the resume.
    was = t["state"]
    upd(tid, state="paused", phase="", paused_by=by, **kw)
    if kill:
        stop(PROCS.get(tid))
    if was != "queued" and t["worktree"]:
        kill_app(PROJECTS.get(t["project"]), t["worktree"])
    ev(tid, {"restart": "paused for a restart", "limit": "paused: usage limit"}.get(by, "paused")
        + (f" while {t['phase']}" if was != "queued" and t["phase"] else ""))


def resume_task(tid):
    if task(tid)["state"] != "paused":
        raise ValueError(f"task is {task(tid)['state']}")
    upd(tid, state="queued", paused_by=None)
    ev(tid, "resumed")


def pause_all():
    """Pause every running task. Returns the ids paused and the ones that could not be."""
    done, skipped = [], []
    for t in q(f"SELECT id,state FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        try:
            pause_task(t["id"])
            done.append(t["id"])
        except ValueError:
            skipped.append(t["id"])
    return {"paused": done, "merging": skipped}


def resume_all():
    ids = [t["id"] for t in q("SELECT id FROM tasks WHERE state='paused'")]
    for tid in ids:
        resume_task(tid)
    return {"resumed": ids}


# ---------------------------------------------------------------- main (the golden version)

def settings_now():
    """Every setting in one query, for code that reads many of them at once."""
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def branch_open(p):
    """The project's branch tasks that haven't ended, and their open PRs: {"tasks", "prs"}."""
    t = q("SELECT COUNT(*) AS n FROM tasks WHERE project=? AND (jira IS NOT NULL OR target IS NOT NULL) AND "
          "state NOT IN (?,?,?)", (p.name, *ENDED), one=True)
    r = q("SELECT COUNT(*) AS n FROM prs JOIN tasks ON tasks.id=prs.task_id WHERE tasks.project=? AND "
          "prs.state='OPEN' AND prs.number IS NOT NULL AND tasks.state NOT IN (?,?,?)", (p.name, *ENDED), one=True)
    return {"tasks": t["n"], "prs": r["n"]}


def main_info(p, s=None):
    s = settings_now() if s is None else s
    f = facts(p)
    if f.get("error"):
        raise RuntimeError(f["error"])
    built = s.get(f"main_built:{p.name}", "")
    dev_built = s.get(f"dev_built:{p.name}", "")
    rel = q("SELECT id,state,phase,error FROM tasks WHERE project=? AND kind='release' ORDER BY id DESC LIMIT 1",
            (p.name,), one=True)
    opens = q("SELECT id,state,deploy FROM tasks WHERE project=? AND kind='deploy' AND state NOT IN (?,?,?) "
              "ORDER BY state='queued', priority DESC, id", (p.name, *ENDED))  # the one under way first
    deploys = [dict(x, open=next((o["id"] for o in opens if json.loads(o["deploy"] or "{}").get("target") == x["name"]),
                                 None), log=x.get("log") and titled(p, x["log"])) for x in f.get("deploys") or []]
    return {"name": p.name, "title": p.title, "main_branch": p.main, "dev_branch": p.dev, "direct_main": p.direct_main,
            "flow": p.flow, "branch_open": branch_open(p),  # flow "pr": the header's count instead of Promote
            # where its files live on disk, so a chat's absolute path to one links as a repo file (Links in chats)
            "roots": [str(x).replace(str(HOME), "~", 1) if v else str(x) for x in (p.repo, p.wt) for v in (0, 1)],
            "main": p.show(f["main"]), "built": p.show(built), "dev": p.show(f["dev"]), "ahead": f["ahead"],
            "main_sha": f["main"],  # its tip, each repo's full sha: the to-do list's released check (TODO-6)
            "has_app": bool(p.build), "can_launch": bool(p.launch),
            "stale": bool(p.build) and built != f["main"], "running": f["running"],
            "has_build": bool(p.build or p.launch) and f["has_build"],  # without a build, Play main needs none
            "dev_running": f.get("dev_running", False), "play": PLAYS.get(p.name),
            "dev_stale": bool(p.build) and (dev_built != f["dev"] or not f.get("dev_has_build")),
            "paused": s.get(f"paused:{p.name}") == "1",
            "auto_release": s.get(f"auto_release:{p.name}") == "1",  # release_default(p), from the snapshot
            "train": TRAIN.get(p.name),  # merged tasks waiting for the promote train: {"ids", "by"}
            "gate": [g["name"] for g in p.gate], "release": rel,
            "deploys": deploys, "deploys_loading": bool(p.deploys) and f.get("deploys") is None,
            "deploy_open": opens[0]["id"] if opens else None, "uncommitted": f.get("uncommitted") or [],
            "senddev_open": senddev_open(p), "senddev_pending": f.get("senddev") or [],
            "dev_log": titled(p, f.get("dev_log") or []), "main_log": titled(p, f.get("main_log") or []),
            "main_since": p.show(f["main_since"]) if f.get("main_since") else None,
            "main_since_kind": f.get("main_since_kind"),
            "config_error": p.config_error, "config_warnings": p.config_warnings, "missing": p.missing,
            "repo_config": bool(p.config_blob)}


def titled(p, log):
    """A changelog for the dashboard: one row per task, with its title from the tasks table, and one per commit no
    task made, with its subject. [{"task": id or None, "title", "at": merge time, "sha", "repo"}], newest first."""
    ids = {int(m.group(1)) for _, _, _, subj in log if (m := MERGE_SUBJECT.match(subj))}
    titles = {t["id"]: t["title"] for t in q(f"SELECT id,title FROM tasks WHERE project=? AND id IN "
                                             f"({','.join('?' * len(ids))})", (p.name, *ids))} if ids else {}
    out, seen = [], set()
    for repo, sha, at, subj in log:
        m = MERGE_SUBJECT.match(subj)
        tid = int(m.group(1)) if m else None
        if tid in seen:  # a task of a workspace merges in each repo it changed
            continue
        if tid:
            seen.add(tid)
        out.append({"task": tid, "title": titles.get(tid) or (subj[m.end():] if m else subj), "at": at,
                    "sha": sha, "repo": repo})
    return out


# ---------------------------------------------------------------- project facts for the dashboard
# Git and the process table are slow next to the database: a git call takes 10-20 ms and a scan for a running
# app 0.2-0.4 s, and every open dashboard tab asks for /api/state every few seconds. So /api/state answers from
# FACTS at once and rereads an entry older than FACTS_TTL behind the request: what it shows is at most a poll old.
# Every task event marks its project's entry stale and rereads it, so a merge or promote shows on the next poll.

FACTS = {}  # project name -> {"at": when read, "main", "dev": revs, "ahead": [lines], "dev_log", "main_log":
#   [changelog entries], "main_since", "main_since_kind", "running", "dev_running", "has_build", "dev_has_build",
#   "deploys"}
FACTS_TTL = 5
FACTS_BUSY = set()  # projects being reread now, so a burst of requests starts one reread, not one each
FACTS_LOCK = threading.Lock()
LOGS = {}  # (repo, from sha, to sha) -> [(short sha, commit time, subject)]: to's first-parent log since from; a
#   pair of shas never changes
MERGE_SUBJECT = re.compile(r"Merge task #(\d+): ")
CHANGES = 200  # commits a changelog reads per repo


def first_parent_log(r, a, b):
    """b's first-parent commits that a lacks, newest first: on dev, one merge commit per task."""
    key = (str(r.src), a, b)
    if key not in LOGS:
        if len(LOGS) > 500:
            LOGS.clear()
        out = r.git("log", "--first-parent", f"-n{CHANGES}", "--format=%h%x09%ct%x09%s", f"{a}..{b}")
        LOGS[key] = [(x[0], int(x[1]), x[2]) for x in (l.split("\t", 2) for l in out.splitlines()) if len(x) == 3]
    return LOGS[key]


def main_since(p, s):
    """Where the main changelog starts: (revs, "deploy" or "promote"), or (None, None) when nothing is recorded.
    The last deploy of main (a target without `from`) when there is one, else main before the last promote."""
    recs = [deploy_record(p, t.name, s) for t in p.deploys if not t.from_]
    recs = [x for x in recs if x and len((x.get("revs") or "").split()) == len(p.parts)]
    if recs:
        return max(recs, key=lambda x: x.get("time") or 0)["revs"], "deploy"
    before = s.get(f"promoted_from:{p.name}", "")
    return (before, "promote") if len(before.split()) == len(p.parts) else (None, None)


def changelog(p, revs_from, revs_to):
    """[[repo or "", short sha, time, subject]] of revs_to's first-parent history since revs_from, newest first.
    A repo where git can't compare them (a sha gone) adds nothing rather than failing the facts."""
    out = []
    for (r, a), b in zip(p.pairs(revs_from), revs_to.split()):
        try:
            out += [[r.sub, *c] for c in first_parent_log(r, a, b)]
        except Exception:
            pass
    return sorted(out, key=lambda c: -c[2])


def read_facts(p, deploys=True):
    try:
        main, dev, ahead = [], [], []
        for r in p.parts:
            m, d = r.git("rev-parse", p.main, p.dev).split()
            main.append(m)
            dev.append(d)
            ahead += [f"{r.name + ': ' if r.sub else ''}{h} {subj}" for h, _, subj in first_parent_log(r, m, d)]
        exes = my_exes() if p.process and not p.running_cmd else None  # one read of the process table for both
        since, since_kind = main_since(p, settings_now())
        f = {"main": " ".join(main), "dev": " ".join(dev), "ahead": ahead[:30],
             "dev_log": changelog(p, " ".join(main), " ".join(dev)),  # on dev, not yet on main
             "main_log": changelog(p, since, " ".join(main)) if since else [],  # on main since the last deploy
             "main_since": since, "main_since_kind": since_kind,
             "running": app_running(p, p.main_wt, exes),
             "dev_running": bool(p.launch) and app_running(p, p.dev_wt, exes),
             "has_build": not p.artifact or (p.main_wt / p.artifact).exists(),
             "dev_has_build": not p.artifact or (p.dev_wt / p.artifact).exists()}
        f["deploys"] = deploy_facts(p, f["main"]) if deploys else None  # tags, and version commands
        f["senddev"] = senddev_pending(p, f["main"])  # sent to dev, not yet back in main
    except Exception as e:
        f = {"error": str(e)}
    f["uncommitted"] = uncommitted(p)
    f["at"] = time.time() if deploys or not p.deploys else 0  # without its deploys: reread at once
    with FACTS_LOCK:
        FACTS[p.name] = f
        FACTS_BUSY.discard(p.name)
    return f


UNCOMMITTED_PATHS = 50  # paths kept per repo, for the dashboard to list
UNCOMMITTED_STAT = 2000  # paths whose mtime is read for the oldest change: a huge untracked tree stays cheap


def uncommitted(p):
    """Uncommitted files in your checkout, per repo: on no branch, so no worker sees them and no release ships
    them. [{"repo": path, "count", "paths": first few, "oldest": mtime or None}], only repos that have any."""
    out = []
    for r in p.parts:
        try:  # --no-optional-locks: a status every few seconds must not take index.lock from under your git
            st = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"],
                                cwd=r.src, capture_output=True, text=True, timeout=10)
            if st.returncode:
                continue
            paths, it = [], iter(st.stdout.split("\0"))
            for e in it:
                if len(e) > 3:
                    paths.append(e[3:])
                    if e[0] in "RC":
                        next(it, None)  # a rename's or copy's old path: one change, not two
            if not paths:
                continue
            times = []
            for x in paths[:UNCOMMITTED_STAT]:
                try:
                    times.append(os.lstat(r.src / x).st_mtime)
                except OSError:  # deleted: nothing to date
                    pass
            out.append({"repo": str(r.src).replace(str(HOME), "~", 1), "count": len(paths),
                        "paths": paths[:UNCOMMITTED_PATHS], "oldest": min(times) if times else None})
        except Exception:  # the other facts matter more than this one
            continue
    return out


COMMIT_WHY_NAMES = 3  # paths the Commit to-do's why names; more are counted


def commit_why(us):
    """The Commit to-do's why: the first few uncommitted paths, relative to their repo, then how many more, per repo:
    'docs/a.md, notes/b.md +1 more in ~/dev/research'."""
    out, left = [], COMMIT_WHY_NAMES
    for u in us:
        names = (u.get("paths") or [])[:left]
        left -= len(names)
        more = u["count"] - len(names)
        what = ", ".join(names) + (f" +{more} more" if more else "") if names else f"{more} {files(more)}"
        out.append(f"{what} in {u['repo']}")
    return "; ".join(out)


def files(n):
    return "file" if n == 1 else "files"


def day(ts):
    """A date as the dashboard shows one: 20 Oct, with the year when it isn't this one."""
    t = time.localtime(ts)
    return f"{t.tm_mday} {time.strftime('%b', t)}" + (f" {t.tm_year}" if t.tm_year != time.localtime().tm_year else "")


def uncommitted_warning(p):
    """The line the New task form and `opsd.py add` show while your checkout has uncommitted files, or ""."""
    n = sum(u["count"] for u in facts(p).get("uncommitted") or [])
    return f"Workers start from dev and won't see your {n} uncommitted {files(n)}." if n else ""


def facts(p):
    """What git and the process table say about a project: from FACTS, reread behind the request when stale."""
    with FACTS_LOCK:
        f = FACTS.get(p.name)
        if f and time.time() - f["at"] < FACTS_TTL:
            return f
        if f:
            refresh_facts(p)
            return f
    f = read_facts(p, deploys=False)  # the first request after a start or reload waits for git, not version commands
    if p.deploys:
        with FACTS_LOCK:
            refresh_facts(p)
    return f


def refresh_facts(p):
    """Reread a project's facts in the background. Call with FACTS_LOCK held."""
    if p.name in FACTS_BUSY:
        return
    FACTS_BUSY.add(p.name)
    threading.Thread(target=read_facts, args=(p,), daemon=True).start()


def stale_facts(name):
    """Something happened in project name (a merge, a promote, a build): reread its facts now."""
    p = PROJECTS.get(name)
    with FACTS_LOCK:
        if p and p.name in FACTS:
            FACTS[p.name]["at"] = 0
            refresh_facts(p)


def promote_blocker(p, commit, what="release"):
    """What only you can clear before main moves to commit, or None. The release (or a direct_main merge: what)
    needs you while there is one. Your uncommitted changes are not one: move_main carries them over."""
    if app_running(p, p.main_wt):
        return (f"Quit the {p.main} build: it is rebuilt in {p.main_wt}. The {what} goes on by itself once it "
                "has quit.")
    for r, sha in p.pairs(commit):
        if r.git("branch", "--show-current", check=False) != p.main:  # main moves as a branch; your checkout stays
            wt = r.git("worktree", "list", "--porcelain", check=False).split("\n\n")
            held = [w.split("\n")[0][9:] for w in wt if f"branch refs/heads/{p.main}" in w.split("\n")]
            if held:
                return (f"{p.main} is checked out in {held[0]}. Switch that one off it, and the {what} goes on "
                        "by itself.")
    return None


def is_ancestor(r, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=r.src, capture_output=True).returncode == 0


def clashing(p, commit):
    """The repos whose checkout is on main with uncommitted changes to files commit changes: [(part, sha)]."""
    out = []
    for r, sha in p.pairs(commit):
        if r.git("branch", "--show-current", check=False) != p.main:
            continue
        if dirty(r) & set(r.git("diff", "--name-only", p.main, sha).splitlines()):
            out.append((r, sha))
    return out


def dirty(r):
    """The paths your checkout has uncommitted: tracked changes and untracked files, not ignored ones."""
    return set(r.git("diff", "--name-only", "HEAD", check=False).splitlines()
               + r.git("ls-files", "--others", "--exclude-standard", check=False).splitlines())


LEFT_PATHS = 20  # paths a release summary lists per repo
CARRIED_NAMES = 5  # carried paths it names; more are counted


def left_note(p, tid, left):
    """The release summary's section on the uncommitted files main moved under ([(part, paths, carried paths)]),
    or ""."""
    blocks = []
    for r, paths, carried in left:
        n, k = len(paths), len(carried)
        b = (f"Not part of this release: {n} uncommitted {files(n)} in {str(r.src).replace(str(HOME), '~', 1)} "
             f"stayed in your checkout and {'was' if n == 1 else 'were'} not shipped:\n"
             + "".join(f"- {x}\n" for x in paths[:LEFT_PATHS])
             + (f"and {n - LEFT_PATHS} more\n" if n > LEFT_PATHS else ""))
        if carried:
            if k == n:
                who = "It" if n == 1 else "Both" if n == 2 else "All of them"
            elif k <= CARRIED_NAMES:
                who = " and ".join(filter(None, [", ".join(carried[:-1]), carried[-1]]))
            else:
                who = f"{k} of them"
            b += (f"{who} {'was' if k == 1 else 'were'} merged onto the new {p.main} and put back (copy at "
                  f"refs/ops/release-{tid})"
                  + ("" if k == n else "; the other stayed as it was" if n - k == 1 else "; the others stayed as they were")
                  + ".\n")
        blocks.append(b)
    them = "it" if sum(len(paths) for _, paths, _ in left) == 1 else "them"
    return "\n".join(blocks) + f"Commit {them} to dev for {them} to ship." if blocks else ""


def snapshot(r):
    """Your checkout's working tree, uncommitted and untracked files alike (not ignored ones), as (HEAD, tree).
    It is made in a copy of the index, so the checkout and its index are left as they are."""
    index = Path(r.git("rev-parse", "--path-format=absolute", "--git-path", "index"))
    tmp = RUNS / f"index-{secrets.token_hex(6)}"
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        if index.exists():
            shutil.copyfile(index, tmp)  # with its stat cache, git reads only the files that changed
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp))
        head = r.rev("HEAD")
        for cmd in (("add", "-A"), ("write-tree",)):
            res = subprocess.run(["git", *cmd], cwd=r.src, env=env, capture_output=True, text=True)
            if res.returncode:
                raise RuntimeError(f"git {' '.join(cmd)} in {r.src}: {res.stderr.strip()}")
        return head, res.stdout.strip()
    finally:
        tmp.unlink(missing_ok=True)


CONFLICT_MARK = re.compile(r"^(<<<<<<<|>>>>>>>)( |$)", re.M)


def merge_wip(p, tid, r, onto, base, wip, note=""):
    """Apply your changes (commit base to tree wip) on top of commit onto, in the release's worktree. A worker
    resolves any conflicts. Returns the result as a tree, or None if you stopped the task; raises NeedsYou if
    the worker can't resolve them."""
    wt = r.at(task(tid)["worktree"])
    for a in (("cherry-pick", "--quit"), ("reset", "--hard", "-q"), ("clean", "-fdq")):
        subprocess.run(["git", *a], cwd=wt, capture_output=True)
    r.git("checkout", "-q", "--detach", onto, cwd=wt)
    yours = r.git("commit-tree", wip, "-p", base, "-m", "Your uncommitted changes")
    m = subprocess.run(["git", "cherry-pick", "--no-commit", yours], cwd=wt, capture_output=True, text=True)
    if m.returncode:
        conflicted = r.git("diff", "--name-only", "--diff-filter=U", cwd=wt).splitlines()
        if not conflicted:
            raise NeedsYou(f"git could not apply your uncommitted changes in {r.src} on top of the release: "
                           + (m.stderr or m.stdout).strip()[-1500:])
        ev(tid, f"your uncommitted changes in {r.src} conflict with the release in {len(conflicted)} file(s); "
                "a worker is merging them")
        upd(tid, session_id=None)
        res = run_claude(p, tid, carry_prompt(p, task(tid), r, onto, base, yours, conflicted, note))
        if stopped(tid):
            return None
        if not res or res.get("status") != "done":
            why = (res or {}).get("summary") or "the worker stopped without writing a result"
            raise NeedsYou(f"Your uncommitted changes in {r.src} conflict with the release, and the worker could "
                           f"not merge them: {why}\n\nSay how to combine them and the worker tries again, or commit "
                           "or stash them and the release goes on by itself.")
        left = [f for f in conflicted
                if (wt / f).is_file() and CONFLICT_MARK.search((wt / f).read_text(errors="replace"))]
        if left:
            raise NeedsYou(f"The worker merging your uncommitted changes in {r.src} left conflict markers in "
                           f"{', '.join(left)}. Say how to combine them and the worker tries again, or commit or "
                           "stash them and the release goes on by itself.")
        r.git("add", "-A", "--", *conflicted, cwd=wt)  # only these: the cherry-pick staged everything else
        ev(tid, "merged your uncommitted changes: " + (res.get("summary") or "done"))
    return r.git("write-tree", cwd=wt)


def carry(p, tid, commit, note=""):
    """Your uncommitted changes to files the release changes, merged onto it, ready to put back once main moves:
    [(part, sha, head, wip tree, merged tree)]. Changes that touch none of those files need none of this: the
    fast-forward keeps them. Returns None if you stopped the task; raises NeedsYou if a worker can't merge them."""
    plan = []
    for r, sha in clashing(p, commit):
        head, wip = snapshot(r)
        merged = merge_wip(p, tid, r, sha, head, wip, note)
        for _ in range(5):  # you kept editing while it merged: apply your newer edits on top, the same way
            if merged is None:
                return None
            now_head, now = snapshot(r)
            if now_head != head:
                raise NeedsYou(f"Your checkout {r.src} got a new commit while the release merged your uncommitted "
                               f"changes. Answer anything, and the release checks {p.main} again.")
            if now == wip:
                break
            merged = merge_wip(p, tid, r, r.git("commit-tree", merged, "-p", sha, "-m", "merged"),
                               r.git("commit-tree", wip, "-p", head, "-m", "before"), now, note)
            wip = now
        else:
            raise NeedsYou(f"Your checkout {r.src} kept changing while the release merged your uncommitted changes "
                           "into it. Answer anything when you've paused, and it tries again.")
        plan.append((r, sha, head, wip, merged))
    return plan


def put_back(r, tid, sha, head, wip, merged):
    """Move main in your checkout from head to sha, and leave your changes (tree wip, merged onto sha as tree
    merged) in the working tree, uncommitted. Only files that differ between wip and merged are written."""
    keep = r.git("commit-tree", wip, "-p", head, "-m", f"Uncommitted changes before release #{tid}")
    r.git("update-ref", f"refs/ops/release-{tid}", keep)
    out = subprocess.run(["git", "diff", "--name-status", "--no-renames", "-z", wip, merged], cwd=r.src,
                         capture_output=True, text=True, check=True).stdout.split("\0")[:-1]
    write, gone = [], []
    for st, path in zip(out[::2], out[1::2]):
        (gone if st == "D" else write).append(path)
    if write:
        subprocess.run(["git", "--literal-pathspecs", "checkout", merged, "--pathspec-from-file=-",
                        "--pathspec-file-nul"], input="\0".join(write), cwd=r.src, capture_output=True, text=True,
                       check=True)
    for path in gone:
        (r.src / path).unlink(missing_ok=True)
    r.git("reset", "-q", sha, "--")  # main and the index move; the working tree keeps what was just written
    return keep


def carry_prompt(p, t, r, onto, base, yours, conflicted, note):
    notes = f"## Notes for {p.title}\n\n{p.worker_notes}\n\n" if p.worker_notes else ""
    said = f"## What the user said when the queue asked them\n\n{note}\n\n" if note else ""
    return (f"You are an unattended worker on the {p.title} project, started by the claude-ops task queue. "
            f"Nobody watches this session.\n\n"
            f"Release #{t['id']} passed, and `{p.main}` is about to move to it. The user's checkout ({r.src}) is on "
            f"`{p.main}` with uncommitted changes, a work in progress, and some of them touch files the release "
            f"changes too. The queue carries them over: the user gets them back, still uncommitted, on top of the "
            f"new `{p.main}`. This worktree ({r.at(t['worktree'])}) is detached at {onto[:12]}, with the user's "
            f"changes applied by `git cherry-pick --no-commit {yours[:12]}`, stopped on conflicts in:\n\n"
            + "".join(f"- {f}\n" for f in conflicted) +
            f"\nResolve every conflict so both sides' changes survive. The release side is finished, checked work: "
            f"`git diff {base[:12]} {onto[:12]} -- <file>` shows what it changed. The other side is the user's: "
            f"`git diff {base[:12]} {yours[:12]} -- <file>`. Where they truly contradict, keep what the user meant "
            f"and fit it to the release's code. Only the files listed count: edits anywhere else are thrown away. "
            f"Don't commit, abort, reset or stash, and don't touch other worktrees, branches or {r.src}. Don't build "
            f"or run the tests: a work in progress may not build yet, and that's fine.\n\n{said}{notes}"
            f"As your last action, write JSON to $OPS_RESULT ({result_path(t)}): "
            f'{{"status": "done", "summary": "..."}} once no conflict is left, or '
            f'{{"status": "failed", "summary": "why"}} if only the user can decide how the two sides combine. '
            f"The user reads the summary: say in plain words, per file, how you combined the two sides.")


def move_main(p, tid, commit, note="", left=None):
    """Fast-forward main to commit in each repo: in your checkout when it is on main, else the branch alone. Your
    uncommitted changes stay: ones to files the release changes are merged onto it (by a worker, if they conflict)
    and put back. Every repo is checked first, so a workspace never moves only some of them. Returns an error or
    None; raises NeedsYou if your changes can't be carried over. Fills the list left, if given, with what stayed
    uncommitted in each checkout on main: (part, sorted paths, sorted paths the plan merged and put back). Holds
    the checkout lock: a Send to dev never clears your checkout while this snapshots it, carries your changes over or
    puts them back."""
    with p.checkout_lock:
        return _move_main(p, tid, commit, note, left)


def _move_main(p, tid, commit, note, left):
    for r, sha in p.pairs(commit):
        if not is_ancestor(r, p.main, sha):
            return f"{p.main}{f' of {r.name}' if r.sub else ''} could not fast-forward: it has commits {p.dev} lacks."
    kept = [(r, sorted(d), sorted(d & set(r.git("diff", "--name-only", p.main, sha).splitlines())))
            for r, sha in p.pairs(commit)
            if r.git("branch", "--show-current", check=False) == p.main and (d := dirty(r))]
    plan = carry(p, tid, commit, note)
    if plan is None:
        return "stopped"
    for r, sha, head, wip, merged in plan:
        keep = put_back(r, tid, sha, head, wip, merged)
        ev(tid, f"your uncommitted changes in {r.src} are back on top of the new {p.main}, unstaged "
                f"(a copy of them as they were: commit {keep[:8]}, ref refs/ops/release-{tid})")
    carried = [r for r, *_ in plan]
    for r, sha in p.pairs(commit):
        if r in carried:
            continue
        if r.git("branch", "--show-current", check=False) == p.main:
            res = subprocess.run(["git", "merge", "--ff-only", sha], cwd=r.src, capture_output=True, text=True)
        else:
            res = subprocess.run(["git", "update-ref", f"refs/heads/{p.main}", sha, r.rev(p.main)],
                                 cwd=r.src, capture_output=True, text=True)
        if res.returncode:
            return f"{p.main}{f' of {r.name}' if r.sub else ''} could not fast-forward:\n" + res.stderr[-1000:]
    if left is not None:
        left[:] = [(r, paths, touched if r in carried else []) for r, paths, touched in kept]
    return None


def catch_up_dev(p, tid):
    """direct_main: dev takes in commits made straight to main, by a fast-forward when dev has nothing main lacks
    (the usual case: main moves to dev after every merge). Returns an error or None. Call with p.lock held."""
    for part in p.parts:
        if not is_ancestor(part, p.main, p.dev) and is_ancestor(part, p.dev, p.main):
            save_dev_changes(p, part, tid)
            part.git("merge", "--ff-only", "-q", p.main, cwd=part.at(p.dev_wt))
    return merge_main_into_dev(p, tid)


def merge_main_into_dev(p, tid):
    """Commits made straight to main (outside the queue) come along into dev. Returns an error or None.
    Call with p.lock held."""
    for part in p.parts:
        if subprocess.run(["git", "merge-base", "--is-ancestor", p.main, p.dev], cwd=part.src).returncode == 0:
            continue
        save_dev_changes(p, part, tid)
        r = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", p.main],
                           cwd=part.at(p.dev_wt), capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "merge", "--abort"], cwd=part.at(p.dev_wt), capture_output=True)
            return (f"{p.main}{f' of {part.name}' if part.sub else ''} has commits that conflict with {p.dev}:\n"
                    + r.stdout[-1500:] + r.stderr[-1500:])
    return None


def resolve_main_into_dev(p, tid, conflict):
    """main conflicts with dev: a worker merges main in the release worktree, resolves the conflicts and runs the
    checks, then dev moves to its merge and the release goes on from there. In a workspace each repo merges its
    own main. A pause, a restart or a retry starts the merge over.
    Returns True once dev contains main (the task's base is set); False if it failed or stopped."""
    path = p.wt / f"t{tid}"
    for _ in range(MAX_FIX_ROUNDS):  # dev can move while the worker merges; then it merges again
        fresh = not all(r.present(path) for r in p.parts)
        copy_workspace_files(p, path)
        for r in p.root_first():
            if r.present(path):
                for a in (("merge", "--abort"), ("reset", "--hard"), ("clean", "-fd")):
                    subprocess.run(["git", *a], cwd=r.at(path), capture_output=True)
                r.git("checkout", "--detach", p.dev, cwd=r.at(path))
            else:
                r.add_worktree(path, "--detach", p.dev)
        if fresh or unprepared(path):
            prepare_worktree(p, path, key=tid)
        upd(tid, worktree=str(path), session_id=None)
        conflicts = []
        for r in p.parts:
            m = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", p.main],
                               cwd=r.at(path), capture_output=True, text=True)
            if m.returncode:
                conflicts.append((f"In {r.name}/:\n" if r.sub else "") + m.stdout[-2500:] + m.stderr[-1000:])
        if conflicts:
            ev(tid, f"{p.main} conflicts with {p.dev}; a worker is resolving it")
            res = run_claude(p, tid, resolve_prompt(p, task(tid), "\n".join(conflicts)))
            if stopped(tid):
                return False
            if res is None:
                err = (RUNS / str(tid) / "claude.err").read_text(errors="replace")[-1500:]
                fail(p, tid, f"the worker merging {p.main} into {p.dev} stopped without writing a result.\n" + err)
                return False
            if res.get("status") != "done":
                fail(p, tid, f"the worker could not merge {p.main} into {p.dev}: "
                             + (res.get("summary") or f"it reported {res.get('status')}") + "\n\n" + conflict)
                return False
        heads = " ".join(r.git("rev-parse", "HEAD", cwd=r.at(path)) for r in p.parts)
        bad = [r.at(path) for r in p.parts
               if r.git("diff", "--name-only", "--diff-filter=U", cwd=r.at(path))
               or r.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=r.at(path), check=False)
               or r.git("rev-list", "--count", f"HEAD..{p.main}", cwd=r.at(path)) != "0"]
        if bad:
            fail(p, tid, f"the worker said it merged {p.main} into {p.dev}, but the merge is not committed "
                         f"in {', '.join(map(str, bad))}. Click Retry to have a worker merge it again from the start.")
            return False
        with p.lock:
            moved = False
            for r, head in p.pairs(heads):
                dev = r.at(p.dev_wt)
                r.git("merge", "--ff-only", head, cwd=dev, check=False)  # dev hasn't moved: it becomes the merge
                if r.rev(p.dev) != head and subprocess.run(
                        ["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", head],
                        cwd=dev, capture_output=True).returncode:
                    subprocess.run(["git", "merge", "--abort"], cwd=dev, capture_output=True)
                    moved = True
            if moved:
                ev(tid, f"{p.dev} moved while the worker merged, and conflicts again; merging again")
                continue
            upd(tid, base=p.revs(p.dev), session_id=None)  # the release has no session
        ev(tid, f"merged {p.main} into {p.dev} at {p.show(heads)}")
        return True
    fail(p, tid, f"{p.dev} kept moving while a worker merged {p.main} into it. Click Retry.")
    return False


def resolve_prompt(p, t, conflict):
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured: run whatever tests the repo has)"
    notes = f"## Notes for {p.title}\n\n{p.worker_notes}\n\n" if p.worker_notes else ""
    where = (" in each of its repos (" + ", ".join(r.prefix or "the workspace folder itself" for r in p.parts) + "), and each repo merges its "
             "own branches") if p.repos else ""
    return (f"You are an unattended worker on the {p.title} project, started by the claude-ops task queue. "
            f"Nobody watches this session.\n\n"
            f"Commits were made straight to `{p.main}`, and they conflict with `{p.dev}`. Before release #{t['id']} "
            f"can check `{p.dev}`, `{p.main}` has to be merged into it. This worktree ({t['worktree']}) is "
            f"detached at `{p.dev}`{where}, with `git merge {p.main}` stopped on the conflicts:\n\n{conflict}\n\n"
            f"Resolve every conflict so both sides' changes survive. The `{p.dev}` side came through the queue; "
            f"the `{p.main}` side is the user's own work. Read each side's commits "
            f"(`git log --oneline {p.dev}...{p.main}`, `git show`) to understand what each meant. Then run the "
            f"checks ({checks}), fix anything the merge broke, and conclude the merge with `git commit --no-edit` "
            f"(fixes go in that commit or after it). Don't abort the merge, rebase or reset, and don't touch "
            f"other branches, worktrees or {p.repo}.\n\n{notes}"
            f"As your last action, write JSON to $OPS_RESULT ({result_path(t)}): "
            f'{{"status": "done", "summary": "..."}} once the merge is committed, or '
            f'{{"status": "failed", "summary": "why"}} if the two sides can\'t be reconciled without the user. '
            f"The user reads the summary: say in plain words how you combined the two sides.")


def promote(p, tid, commit, note=""):
    """Build commit (a revs string) in <worktrees>/main, then fast-forward main to it. Returns an error or None, and
    raises NeedsYou if your uncommitted changes can't be carried over; either way main and its build are unchanged.
    note is your answer to the last question, for the worker carrying your changes over."""
    old, art, prev = [], None, None

    def undo():
        for r, sha in old:
            checkout_main(p, r, sha, tid, check=False)
        if prev and prev.exists():
            shutil.rmtree(art, ignore_errors=True)
            prev.rename(art)

    if p.build:
        art = p.main_wt / p.artifact if p.artifact else None
        if art and (not within(art, p.main_wt) or within(p.main_wt, art)):  # it gets deleted and replaced
            return f"[app] artifact must be a path inside the worktree, not {p.artifact!r}."
        prev = art.with_name(art.name + ".previous") if art else None
        if prev:
            shutil.rmtree(prev, ignore_errors=True)
            if art.exists():
                clone_tree(art, prev)
        old = [(r, r.git("rev-parse", "HEAD", cwd=r.at(p.main_wt))) for r in p.parts]
        for r, sha in p.pairs(commit):
            checkout_main(p, r, sha, tid)
        try:
            prepare_main(p, lambda text: upd(tid, phase=text), tid)
        except SetupFailed as e:
            undo()
            return f"Setting up {p.main_wt} failed; {p.main} and its build are unchanged.\n{e}"
        rc, tail = run_step(tid, f"building {p.main} at {p.show(commit)}", p.build, p.main_wt, "build.log")
        if rc or (art and not art.exists()):
            undo()
            return f"Build failed; {p.main} and its build are unchanged.\n" + tail[-2000:]
    upd(tid, phase=f"fast-forwarding {p.main}")
    left = []
    try:
        err = move_main(p, tid, commit, note, left)
    except BaseException:
        undo()
        raise
    if err:
        undo()
        return err
    if prev:
        shutil.rmtree(prev, ignore_errors=True)
    set_setting(f"main_built:{p.name}", commit)
    n = sum(len(paths) for _, paths, _ in left)
    if n and task(tid)["kind"] == "release":  # a deploy's commits are no release: its summary stays as it is
        before = task(tid)["summary"] or ""
        upd(tid, summary=(before + "\n\n" if before else "") + left_note(p, tid, left))
    notify(f"{p.title}: {p.main} updated", f"{p.main} is now {p.show(commit)}"
           + (f"; {n} uncommitted {files(n)} {'was' if n == 1 else 'were'} not released" if n else ""))
    return None


# ---------------------------------------------------------------- Send to dev (your checkout's uncommitted work -> dev)
# The card's banner about uncommitted files in your checkout has a Send to dev button. It snapshots that work at the
# click (refs/ops/senddev-<id> keeps it) and queues a task that applies it onto a branch made from dev, has a worker
# resolve any conflicts, then goes through the gate into dev like any task. Only once it has merged are the sent files
# cleared from your checkout, and only those that still match the snapshot: one copy, never none.

SENDDEV_LOCK = threading.Lock()  # one open Send to dev per project; a click finishes its snapshot before it runs
SENDDEV_HEAD = "started by the claude-ops task queue for Send to dev"  # in a prompt that tells the worker the story
SENDDEV_LIST = 30  # paths a summary names per list before "and N more"
EDITED = "you edited these after sending, so they were left in your checkout"
NEW_COMMIT = "your checkout got a new commit since the snapshot, so your copy was left as it is"


def senddev_of(t):
    """A Send to dev task's record: {"repos": [{sub, repo, branch, head, wip, keep, files, conflicts}], "applied",
    "landed", "cleared": [{cleared, left, why}] per repo}."""
    return json.loads((t or {}).get("senddev") or "{}")


def senddev_save(tid, d):
    upd(tid, senddev=json.dumps(d))


def part_of(p, sub):
    return next(r for r in p.parts if r.sub == sub)


def senddev_open(p):
    """The project's Send to dev task that hasn't ended, or None."""
    o = q("SELECT id FROM tasks WHERE project=? AND kind='senddev' AND state NOT IN (?,?,?) ORDER BY id",
          (p.name, *ENDED), one=True)
    return o["id"] if o else None


def paths_of(r, *args, cwd=None):
    """The NUL-separated paths a git command prints with -z, as a list."""
    out = subprocess.run(["git", *args, "-z"], cwd=cwd or r.src, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return [x for x in out.stdout.split("\0") if x]


def diff_status(r, a, b):
    """[(status, path)] of what differs between trees or commits a and b, renames as a delete and an add."""
    out = paths_of(r, "diff", "--name-status", "--no-renames", a, b)
    return list(zip(out[::2], out[1::2]))


def name_list(paths, n=SENDDEV_LIST):
    return ", ".join(paths[:n]) + (f" and {len(paths) - n} more" if len(paths) > n else "")


def senddev_keep(r, tid, x, when):
    """The snapshot as a commit on your HEAD, kept at refs/ops/senddev-<id>. Dated at the click, so making it again
    (after a crash cut the click off) gives the same commit."""
    stamp = f"@{int(when)} +0000"
    res = subprocess.run(["git", "commit-tree", x["wip"], "-p", x["head"], "-m",
                          f"Uncommitted changes sent to dev by #{tid}"], cwd=r.src, capture_output=True, text=True,
                         env=dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp))
    if res.returncode:
        raise RuntimeError(f"git commit-tree in {r.src}: {res.stderr.strip()}")
    r.git("update-ref", f"refs/ops/senddev-{tid}", res.stdout.strip())
    return res.stdout.strip()


def queue_senddev(p):
    """The Send to dev button: snapshot your checkout's uncommitted work now, keep it at refs/ops/senddev-<id>, and
    queue a task that lands it on dev. With one already open it returns that one. Refuses when there is nothing."""
    with SENDDEV_LOCK:
        if o := senddev_open(p):
            return o
        repos = []
        for r in p.parts:
            head, wip = snapshot(r)
            if wip == r.rev(f"{head}^{{tree}}"):
                continue  # nothing uncommitted here
            repos.append({"sub": r.sub, "repo": str(r.src).replace(str(HOME), "~", 1), "head": head, "wip": wip,
                          "branch": r.git("branch", "--show-current", check=False),
                          "files": [f for _, f in diff_status(r, head, wip)]})
        if not repos:
            raise ValueError("Your checkout has no uncommitted changes to send.")
        to = "a PR" if getattr(p, "flow", "dev") == "pr" else p.dev
        tid = add_task(p.name, senddev_body(p, repos), f"Send uncommitted changes in {senddev_where(p, repos)} to {to}",
                       kind="senddev", senddev={"repos": repos})
        when = task(tid)["created"]
        for x in repos:
            x["keep"] = senddev_keep(part_of(p, x["sub"]), tid, x, when)
        senddev_save(tid, {"repos": repos})
    ev(tid, "snapshot of your uncommitted changes: " + "; ".join(
        f"{len(x['files'])} {files(len(x['files']))} in {x['repo']}, kept as {x['keep'][:8]} at refs/ops/senddev-{tid}"
        for x in repos))
    return tid


def senddev_where(p, repos):
    """The repos in a Send to dev title, by folder name, so the list doesn't cut it off: "demo", or
    "shop (backend, app)" for a workspace."""
    if not p.repos:
        return p.repo.name
    return f"{p.repo.name} ({', '.join(part_of(p, x['sub']).name for x in repos)})"


def senddev_body(p, repos):
    pr = getattr(p, "flow", "dev") == "pr"
    lines = [f"Send to dev, from the banner on the project card. Your checkout's uncommitted work, as it was at the "
             f"click, lands " + (f"on a branch for a PR against {p.main}" if pr else f"on {p.dev}") + " as a normal change:",
             ""]
    for x in repos:
        n = len(x["files"])
        lines.append(f"{x['repo']}" + (f" (on {x['branch']})" if x["branch"] else "") + f": {n} {files(n)}")
        lines += [f"- {f}" for f in x["files"][:50]] + ([f"- …and {n - 50} more"] if n > 50 else []) + [""]
    if pr:  # flow "pr": a branch task like any other there
        lines.append(f"A copy is kept at refs/ops/senddev-<this task's id>. The queue applies it onto this task's "
                     f"branch, made from origin/{p.main} (a worker resolves any conflicts), and runs the gate. Then the "
                     f"branch waits for Open PR, as any branch task does. Once the PR merges and {p.main} moves in "
                     "your checkout, your copy there matches it. Nothing is cleared from your checkout before that.")
    else:
        lines.append(f"A copy is kept at refs/ops/senddev-<this task's id>. The queue applies it onto a branch made from "
                     f"{p.dev} (a worker resolves any conflicts), runs the gate and merges it into {p.dev}, as for any "
                     "task. Only then are the files that still match the snapshot cleared from your checkout. Stopping "
                     "or cancelling before the merge leaves your checkout as it is.")
    return "\n".join(lines)


def senddev_start(p, tid, prompt):
    """Where a Send to dev task's run begins. prompt is the task's next_prompt ("" if none). Returns the prompt for a
    worker, "" to go straight to the rebase and gate, or None if the task needs you."""
    with SENDDEV_LOCK:  # the click that queued it may still be writing the snapshot
        t = task(tid)
    if senddev_of(t).get("applied"):  # your changes are committed on the branch: on to the gate, or the worker
        return prompt if t["session_id"] or prompt not in (RESTART_NOTE, PAUSE_NOTE, LIMIT_NOTE) else ""
    if t["session_id"]:  # a worker was merging your changes with dev: it carries on
        return prompt or "Carry on: resolve the conflicts that are left, commit, and write $OPS_RESULT."
    return senddev_apply(p, tid)


def senddev_onto(p, t, r=None):
    """What a Send to dev task applies your changes onto: dev, or for a branch task (flow "pr") origin/main (main with
    no origin) of repo r, or the words for it with no r."""
    if not is_branch_task(t):
        return p.dev
    return base_ref(p, r) if r else f"origin/{p.main}"


def senddev_apply(p, tid):
    """Apply the snapshot onto the task's branch, reset to the latest dev, in each repo it changes, as merge_wip does
    for a release. A repo that applies cleanly is committed at once. Returns "" when every repo is, the prompt for a
    worker to resolve the conflicts, or None if git can't apply it at all (the task then needs you)."""
    t = task(tid)
    d = senddev_of(t)
    onto = senddev_onto(p, t)
    upd(tid, phase=f"applying your changes onto {onto}")
    conflicts, made = [], 0
    for x in d["repos"]:
        r = part_of(p, x["sub"])
        wt = r.at(t["worktree"])
        x["keep"] = x.get("keep") or senddev_keep(r, tid, x, t["created"])
        for a in (("cherry-pick", "--quit"), ("reset", "--hard", "-q", senddev_onto(p, t, r)), ("clean", "-fdq")):
            subprocess.run(["git", *a], cwd=wt, capture_output=True)
        m = subprocess.run(["git", "cherry-pick", "--no-commit", x["keep"]], cwd=wt, capture_output=True, text=True)
        x["conflicts"] = paths_of(r, "diff", "--name-only", "--diff-filter=U", cwd=wt) if m.returncode else []
        if m.returncode and not x["conflicts"]:
            senddev_save(tid, d)
            ask_owner(p, tid, f"git could not apply your uncommitted changes in {x['repo']} onto {onto}: "
                              + (m.stderr or m.stdout).strip()[-1500:])
            return None
        if x["conflicts"]:
            conflicts.append(x)
        else:
            made += senddev_commit_repo(r, wt, x)
    d["applied"] = not conflicts
    senddev_save(tid, d)
    if not conflicts:
        ev(tid, f"applied your changes onto {onto} and committed them" if made else
                f"applied your changes onto {onto}: it has them already, so there is nothing to commit")
        return ""
    n = sum(len(x["conflicts"]) for x in conflicts)
    ev(tid, f"your changes conflict with {onto} in {n} {files(n)}; a worker is merging them")
    upd(tid, session_id=None)
    return senddev_conflict_prompt(p, task(tid), conflicts)


def senddev_message(x):
    """The commit message for repo x's changes: its subject, and the files as its body."""
    fs = x["files"]
    return (f"Your uncommitted changes from {x['repo']}",
            "\n".join(f"- {f}" for f in fs[:200]) + (f"\n- …and {len(fs) - 200} more" if len(fs) > 200 else ""))


def senddev_commit_repo(r, wt, x):
    """Commit what is staged in wt as your changes from x's repo; nothing if nothing is (dev had them already).
    Returns whether it made a commit."""
    made = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt).returncode != 0
    if made:
        subject, body = senddev_message(x)
        r.git("commit", "-q", "--no-verify", "-m", subject, "-m", body, cwd=wt)
    subprocess.run(["git", "cherry-pick", "--quit"], cwd=wt, capture_output=True)
    return made


def senddev_commit(p, tid):
    """The worker merging your changes with dev says it is done: check that no conflict is left, and commit its
    resolution if it didn't. Returns why the task needs you, or None once your changes are on the branch."""
    t = task(tid)
    d = senddev_of(t)
    for x in d["repos"]:
        if not x.get("conflicts"):
            continue
        r = part_of(p, x["sub"])
        wt = r.at(t["worktree"])
        left = paths_of(r, "diff", "--name-only", "--diff-filter=U", cwd=wt) or [
            f for f in x["conflicts"] if (wt / f).is_file() and CONFLICT_MARK.search((wt / f).read_text(errors="replace"))]
        if left:
            return (f"The worker said it merged your changes in {x['repo']} with {senddev_onto(p, t)}, but "
                    f"conflicts are left in {name_list(left)}. Say how to combine them and the worker tries again, or cancel to keep them "
                    "only in your checkout.")
        if not int(r.git("rev-list", "--count", f"{senddev_onto(p, t, r)}..HEAD", cwd=wt) or 0):  # resolved, no commit
            r.git("add", "-A", "--", *x["conflicts"], cwd=wt, check=False)
            senddev_commit_repo(r, wt, x)
        subprocess.run(["git", "cherry-pick", "--quit"], cwd=wt, capture_output=True)
    d["applied"] = True
    senddev_save(tid, d)
    ev(tid, f"your changes are merged with {senddev_onto(p, t)} and committed")
    return None


def senddev_brief(p, t):
    """What a Send to dev worker needs to know whatever it is asked: the story, the rules, and how to report."""
    d = senddev_of(t)
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured)"
    notes = workspace_note(p, t["branch"]) + (f"## Notes for {p.title}\n\n{p.worker_notes}\n\n" if p.worker_notes else "")
    sent = "".join(f"- {x['repo']}: {len(x['files'])} {files(len(x['files']))}, kept as commit {x['keep'][:12]} "
                   f"(`refs/ops/senddev-{t['id']}`) on the user's HEAD {x['head'][:12]}\n" for x in d["repos"])
    onto = senddev_onto(p, t)
    if is_branch_task(t):  # flow "pr": the branch ends in a PR, not a merge into dev
        lands = f"on branch `{t['branch']}` for a pull request against `{p.main}`"
        after = (f"After you, it rebases the branch onto the latest `{onto}` and runs the merge check ({checks}). Then "
                 f"the branch waits for the user to open its PR. Once the PR merges and `{p.main}` moves in the user's "
                 "checkout, their copy matches it.")
    else:
        lands = f"on `{p.dev}`"
        after = (f"After you, it rebases the branch onto the latest `{p.dev}`, runs the merge check ({checks}) and "
                 f"merges it into `{p.dev}`. Then it clears the sent files from the user's checkout.")
    return (f"You are an unattended worker on the {p.title} project, {SENDDEV_HEAD} (task #{t['id']}). Nobody "
            f"watches this session and nobody can answer you mid-run.\n\n"
            f"The user's checkout had uncommitted changes: unfinished work on no branch, so workers didn't see it and "
            f"no release shipped it. The user clicked Send to dev, which lands that work {lands} as a normal "
            f"change:\n\n{sent}\n"
            f"This worktree ({t['worktree']}) is on branch `{t['branch']}`, made from `{onto}`. The queue applied "
            f"the user's changes here with `git cherry-pick --no-commit` of that commit, and commits them per repo as "
            f"`Your uncommitted changes from <repo>`, with the files listed. {after}\n\n"
            f"Rules: edit and commit only in this worktree. Never touch {p.repo} (the user's checkout), another "
            f"worktree, or the `{p.dev}` and `{p.main}` branches, and never push. `OPS_PORT` is {p.port_for(t['id'])}; "
            f"use it for anything that listens. Never post mouse or keyboard events or take focus: the user is using "
            f"this Mac. {GUARD_RULE} Land the user's work as it is: don't finish or extend it.\n\n{notes}"
            f"As your last action, write JSON to $OPS_RESULT ({result_path(t)}): "
            '{"status": "done", "summary": "..."} once everything is committed, or '
            '{"status": "needs_input", "question": "...", "summary": "..."} if only the user can decide (for '
            "example, how two changes that truly contradict should combine). The user reads the summary: say in plain "
            "words what you did, per file.")


def senddev_conflict_prompt(p, t, conflicts):
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured: run whatever tests the repo has)"
    parts = []
    for x in conflicts:
        subject, body = senddev_message(x)
        where = f"`{x['sub']}/`" if x["sub"] not in ("", ".") else "the worktree root"
        parts.append(
            f"In {where} ({x['repo']}), `git cherry-pick --no-commit {x['keep'][:12]}` stopped on conflicts in:\n\n"
            + "".join(f"- {f}\n" for f in x["conflicts"]) +
            f"\nThe `{senddev_onto(p, t)}` side is checked work that passed the gate: `git diff {x['head'][:12]} HEAD -- <file>` "
            f"shows what it changed since the user's base. The user's side is their unfinished work being landed: "
            f"`git diff {x['head'][:12]} {x['keep'][:12]} -- <file>`. Once resolved, `git add` those files and commit "
            f"everything staged with exactly:\n\n    git commit -m {shlex.quote(subject)} -m {shlex.quote(body)}\n")
    return (senddev_brief(p, t) + "\n\n## Now: merge the user's changes with " + senddev_onto(p, t) + "\n\n" +
            "\n".join(parts) +
            f"\nResolve every conflict so both sides survive. Where they truly contradict, keep what the user meant and "
            f"fit it to `{senddev_onto(p, t)}`'s code. Don't abort the cherry-pick, reset, rebase or stash. Then run the checks "
            f"({checks}). The user's work is unfinished, so they may fail: you may fix what they find, with the "
            f"smallest change that keeps the user's intent, in a further commit. If you can't merge the two sides "
            f"without the user, report needs_input and ask them how.")


def senddev_landed(p, tid, merged, note):
    """Your changes are on dev (merged is the merge, or None when dev had them already): clear them from your
    checkout, sum up what happened, and finish. Never runs before the merge, so a stop before it clears nothing."""
    d = senddev_of(task(tid))
    if not d.get("landed"):
        d["landed"] = True
        senddev_save(tid, d)
    if stopped(tid):
        ev(tid, f"stopped after merging into {p.dev}, so your checkout was left as it is; Retry clears it")
        return
    upd(tid, merged=merged, state="merging", phase="clearing your checkout")
    if "cleared" not in d:  # a retry after clearing only finishes
        if not p.checkout_lock.acquire(blocking=False):
            upd(tid, phase=f"waiting for {p.main} to finish moving in your checkout")
            ev(tid, f"waiting to clear your checkout: a release or deploy is moving {p.main} in it")
            p.checkout_lock.acquire()
        try:
            if stopped(tid):
                ev(tid, f"stopped after merging into {p.dev}, so your checkout was left as it is; Retry clears it")
                return
            upd(tid, phase="clearing your checkout")
            d["cleared"] = []
            for x in d["repos"]:
                try:
                    c = clear_checkout(part_of(p, x["sub"]), x)
                except Exception as e:  # say so and leave the rest; nothing is lost, the copy is in dev and the ref
                    c = {"cleared": [], "left": [], "why": f"clearing failed, so your copy was left as it is ({e})"}
                d["cleared"].append(c)
                ev(tid, f"{x['repo']}: " + (c["why"] if c["why"] else
                        f"cleared {len(c['cleared'])} {files(len(c['cleared']))}"
                        + (f"; left {len(c['left'])} you edited after sending: {name_list(c['left'], 10)}"
                           if c["left"] else "")))
        finally:
            p.checkout_lock.release()
        d["summary"] = senddev_summary(p, task(tid), d, merged)
        senddev_save(tid, d)
    upd(tid, summary=d.get("summary"))
    stale_facts(p.name)
    land(p, tid, merged, note)


def clear_checkout(r, x):
    """Put each file the snapshot x changed back as your HEAD has it (deleted if HEAD lacks it), in the working tree
    and the index, if the file still matches the snapshot (or HEAD already). Folders left empty go too. A file edited
    since is left, and nothing at all if HEAD moved. Never a path outside the snapshot's diff.
    Returns {"cleared": [paths], "left": [paths edited since], "why": why nothing was cleared, or None}."""
    head, now = snapshot(r)
    if head != x["head"]:
        return {"cleared": [], "left": [], "why": NEW_COMMIT}
    sent = diff_status(r, x["head"], x["wip"])
    edited = ({f for _, f in diff_status(r, x["wip"], now)}  # differs from the snapshot, and from HEAD too
              & {f for _, f in diff_status(r, head, now)})
    back = [(st, f) for st, f in sent if f not in edited]
    restore, drop = [f for st, f in back if st != "A"], [f for st, f in back if st == "A"]

    def git_paths(*args, paths):
        subprocess.run(["git", "--literal-pathspecs", *args, "--pathspec-from-file=-", "--pathspec-file-nul"],
                       input="\0".join(paths), cwd=r.src, capture_output=True, text=True, check=True)

    if restore:
        git_paths("checkout", head, paths=restore)
    for f in drop:  # new in the snapshot: HEAD has no copy to put back
        path = r.src / f
        if within(path.parent, r.src) and (path.is_symlink() or path.is_file()):
            path.unlink()
    if back:
        git_paths("reset", "-q", head, paths=[f for _, f in back])
    for f in drop:
        folder = (r.src / f).parent
        while folder != r.src and within(folder, r.src):
            try:
                folder.rmdir()
            except OSError:  # not empty, or already gone
                break
            folder = folder.parent
    return {"cleared": [f for _, f in back], "left": [f for _, f in sent if f in edited], "why": None}


def senddev_headline(p, d, landed):
    """The summary's first line, which the task card shows: what happened, in counts. landed: files per repo."""
    n = sum(len(f) for f in landed)
    cleared = sum(len(c["cleared"]) for c in d["cleared"])
    edited = sum(len(c["left"]) for c in d["cleared"])
    stuck = [(x, c) for x, c in zip(d["repos"], d["cleared"]) if c["why"]]  # nothing cleared there, and why
    out = [f"Landed {n} {files(n)} on {p.dev}" if n else f"Nothing new landed on {p.dev}: it already had these changes"]
    if cleared:
        out.append(f"cleared {cleared} from your checkout")
    if edited:
        out.append(f"left {edited} you edited after sending")
    if stuck and not cleared and not edited and len({c["why"] for _, c in stuck}) == 1:
        out.append(f"cleared nothing: {stuck[0][1]['why']}")
    elif stuck:
        k = sum(len(x["files"]) for x, _ in stuck)
        out.append(f"left {k} as {'it was' if k == 1 else 'they were'} (see below)")
    return "; ".join(out)


def senddev_summary(p, t, d, merged):
    """The task's summary: a headline in counts, then per repo what landed on dev, what was cleared, what was left
    and why, and the copy."""
    out, landed, gone = [], [], []  # gone: per repo, the paths your snapshot deleted
    for x in d["repos"]:
        r = part_of(p, x["sub"])
        m = p.mine(r, merged) if merged else None
        landed.append(diff_status(r, f"{m}^1", m) if m else [])
        gone.append({f for st, f in diff_status(r, x["head"], x["wip"]) if st == "D"} if x.get("wip") else set())
    for x, c, here, dead in zip(d["repos"], d["cleared"], landed, gone):
        out += ["", f"{x['repo']}:",
                f"- Landed on {p.dev}: {name_list([f + ' (deleted)' if st == 'D' else f for st, f in here])}."
                if here else f"- Nothing new landed on {p.dev}: it already had these changes."]
        if c["why"]:
            out.append(f"- Nothing cleared: {c['why']}.")
        if c["cleared"]:  # clearing a deletion puts the file back as your HEAD has it
            out.append(f"- Cleared from your checkout: "
                       f"{name_list([f + ' (deletion undone)' if f in dead else f for f in c['cleared']])}.")
        if c["left"]:
            out.append(f"- Left in your checkout: {name_list(c['left'])}. {EDITED[0].upper() + EDITED[1:]}.")
        out.append(f"- A copy of your changes as they were: refs/ops/senddev-{t['id']} (commit {x['keep'][:8]}).")
    if merged and any(c["cleared"] for c in d["cleared"]):
        out.append(f"\nWhat was cleared is on {p.dev}, not in your checkout, until the next Promote brings it back.")
    worker = (t["summary"] or "").strip()
    return (senddev_headline(p, d, [[f for _, f in here] for here in landed]) + "\n" + "\n".join(out)
            + (f"\n\nThe worker that merged them: {worker}" if worker else ""))


IN_MAIN = set()  # (repo, sha) known to be in main: main only moves forward, so it stays there


def senddev_pending(p, main):
    """Send to dev tasks whose merge is on dev but not yet in main (a revs string), newest first: the card says
    Promote to get them back."""
    out = []
    for t in q("SELECT id, merged FROM tasks WHERE project=? AND kind='senddev' AND state='done' AND merged IS NOT NULL "
               "ORDER BY id DESC LIMIT 20", (p.name,)):
        for sha in t["merged"].split():
            r = next((r for r in p.parts if r.has(sha)), None)
            if not r or (r.src, sha) in IN_MAIN:
                continue
            if is_ancestor(r, sha, dict(zip(p.parts, main.split()))[r]):
                IN_MAIN.add((r.src, sha))
            else:
                out.append(t["id"])
                break
    return out


# ---------------------------------------------------------------- deploys
# A project lists deploy targets ([[deploy]] in its config): store test builds, store releases, a backend's prod
# deploy. The daemon runs one only when you click Deploy (or run `opsd.py deploy`), never on its own, from a
# release or from anything Claude does. It runs the target's command itself, in one persistent worktree per project
# (<worktrees>/deploy) that is reset but never cleaned, so a build's output is still there for the next target to
# upload. A deploy records what it shipped (settings key deployed:<project>:<target>). Commits its command makes,
# such as a build-number bump, move main: the one way main moves without a release.

VERSION = re.compile(r"\d+\.\d+(\.\d+)?")
APP_STORE = "{tag} is released; the App Store closes a version once approved"
VERSIONS = {}  # (project, target, version command, commit) -> (when read, version or error); a commit doesn't change
PEEK_LOCKS = {}  # project name -> lock on its <worktrees>/deploy-peek, where versions are read


def deploy_record(p, name, s=None):
    """What target name last deployed: {commit, revs, time, task, version}, or None."""
    raw = (s or {}).get(f"deployed:{p.name}:{name}") if s is not None else setting(f"deployed:{p.name}:{name}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def deploy_input(p, t, main=None):
    """The commit target t deploys now, as revs (one sha per repo): main, or for a `from` target the commit that
    target last deployed (in its repo; the other repos sit at main). Raises ValueError if there is none yet."""
    shas = (main or p.revs(p.main)).split()
    if t.from_:
        rec = deploy_record(p, t.from_)
        src = (rec or {}).get("revs", "").split()
        if not rec:
            raise ValueError(f"Deploy {t.from_} first.")
        if len(src) != len(shas):
            raise ValueError(f"{t.from_} was last deployed with other repos; deploy {t.from_} again first.")
        shas = [s if not t.repo or r.sub == t.repo else m for r, m, s in zip(p.parts, shas, src)]
    return " ".join(shas)


def target_commit(p, t, revs):
    """revs as a target sees it (OPS_COMMIT, the record): its repo's sha, or the whole revs string."""
    return dict(zip((r.sub for r in p.parts), revs.split()))[t.repo] if t.repo else revs


def target_show(p, t, commit):
    return f"{t.repo} {commit[:8]}" if t.repo else p.show(commit)


def parse_version(out):
    """X.Y.Z (or X.Y, read as X.Y.0) as a tuple of 3 ints. Raises ValueError otherwise."""
    v = (out or "").strip()
    if not VERSION.fullmatch(v):
        raise ValueError(f"`version` printed {clip(v, 60)!r}, not a version like 1.2.3")
    return tuple(int(x) for x in (v + ".0").split(".")[:3]) if v.count(".") == 1 else tuple(map(int, v.split(".")))


def vstr(v):
    return ".".join(map(str, v))


def run_version(t, folder):
    """Run the target's version command in folder: the current version as a tuple. Raises ValueError."""
    try:
        r = subprocess.run(["/bin/bash", "-lc", t.version], cwd=folder, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ValueError(f"`{t.version}` ran over 60 s")
    if r.returncode:
        raise ValueError(f"`{t.version}` exited {r.returncode}: {clip(r.stderr.strip() or r.stdout, 200)}")
    return parse_version(r.stdout)


def bumps(cur):
    x, y, z = cur
    return {"patch": (x, y, z + 1), "minor": (x, y + 1, 0), "major": (x + 1, 0, 0)}


def pick_version(choice, cur, released):
    """The version a deploy ships: same, patch, minor, major or an explicit X.Y.Z higher than cur. released is the
    current version's release tag if it exists: then that version is closed and "same" is refused."""
    choice = (choice or "").strip() or ("patch" if released else "same")
    if choice == "same":
        if released:
            raise ValueError(APP_STORE.format(tag=released))
        return cur
    if choice in ("patch", "minor", "major"):
        return bumps(cur)[choice]
    if not re.fullmatch(r"\d+\.\d+\.\d+", choice):
        raise ValueError(f"version must be same, patch, minor, major or X.Y.Z, not {choice!r}")
    v = tuple(map(int, choice.split(".")))
    if v <= cur:
        raise ValueError(f"{choice} is not higher than the current version {vstr(cur)}")
    return v


def tags(p, t, pattern, merged=None, with_repo=False):
    """Tags matching pattern (a glob) in the target's repos, newest first by creatordate: [{name, commit, date}].
    merged: a revs string; only tags reachable from it count. with_repo: each also has repo (its sub) and ts."""
    out = []
    for r, sha in zip(p.parts, merged.split() if merged else [None] * len(p.parts)):
        if r not in t.parts(p):
            continue
        fmt = ("%(refname:short)%09%(if)%(*objectname)%(then)%(*objectname)%(else)%(objectname)%(end)%09"
               "%(creatordate:unix)%09%(creatordate:iso-strict)")
        lines = r.git("for-each-ref", "--sort=-creatordate", f"--format={fmt}",
                      *(["--merged", sha] if merged else []), f"refs/tags/{pattern}", check=False).splitlines()
        for l in lines:
            name, commit, ts, date = l.split("\t")
            if fnmatch.fnmatchcase(name, pattern):  # for-each-ref also takes a plain name as a prefix of a path
                out.append({"name": name, "commit": commit, "date": date, "ts": int(ts or 0), "repo": r.sub})
    return [{k: v for k, v in x.items() if k not in ("ts", "repo")} for x in sorted(out, key=lambda x: -x["ts"])] \
        if not with_repo else sorted(out, key=lambda x: -x["ts"])


def peek_version(p, t, revs):
    """The target's version at commit revs, read in <worktrees>/deploy-peek (no setup there) so the deploy
    worktree is left alone. Cached per commit; an error is retried after a minute. Raises ValueError."""
    key = (p.name, t.name, t.version, t.repo, revs)
    hit = VERSIONS.get(key)
    if hit and (not isinstance(hit[1], str) or time.time() - hit[0] < 60):
        if isinstance(hit[1], str):
            raise ValueError(hit[1])
        return hit[1]
    with PEEK_LOCKS.setdefault(p.name, threading.Lock()):
        path = p.wt / "deploy-peek"
        try:
            copy_workspace_files(p, path)
            for r, sha in p.root_first(p.pairs(revs)):
                checkout_detached(r, r.at(path), sha)
            v = run_version(t, t.folder(path))
        except (ValueError, RuntimeError) as e:
            VERSIONS[key] = (time.time(), str(e))
            raise ValueError(str(e))
    if len(VERSIONS) > 500:
        VERSIONS.clear()
    VERSIONS[key] = (time.time(), v)
    return v


def save_dev_changes(p, r, tid):
    """Before a merge into repo r's part of p's dev worktree: uncommitted changes to tracked files there (a build's
    pod install rewrote Podfile.lock) would stop it. opsd owns that worktree, so they are no one's work: they are
    saved as a patch in task tid's run folder, discarded, and named in its log. If the patch can't be written they
    stay, and this raises. Never `git clean`: untracked and ignored files (node_modules, Pods, .env, the build) stay.
    Call with p.lock held."""
    wt = r.at(p.dev_wt)
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=wt, capture_output=True)
    if diff.returncode or not diff.stdout:
        return
    files = r.git("diff", "--name-only", "HEAD", cwd=wt, check=False).splitlines()
    named = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
    name = (r.sub if r.sub and not r.root else "root").replace("/", "-")
    run = RUNS / str(tid)
    try:
        run.mkdir(parents=True, exist_ok=True)
        patch, n = run / f"dev-uncommitted-{name}.patch", 1
        while patch.exists():  # an earlier merge of this task saved some already: keep both
            n += 1
            patch = run / f"dev-uncommitted-{name}-{n}.patch"
        patch.write_bytes(diff.stdout)
    except OSError as e:
        raise RuntimeError(f"uncommitted changes in {wt} would stop the merge ({named}), and they could not be "
                           f"saved before discarding them: {e}")
    r.git("reset", "--hard", "-q", cwd=wt)
    ev(tid, f"discarded uncommitted changes in {wt}: {named}; saved to {patch} (git apply it to get them back)")


def checkout_main(p, r, sha, tid=None, check=True):
    """Put repo r's part of p's main worktree at sha, detached. opsd owns that worktree and only builds there, so a
    tracked file a build rewrote (pod install's Podfile.lock) is no one's work: it is discarded, and named in task
    tid's log so the side effect shows. Never `git clean`: cloned node_modules, .env files and the build stay."""
    wt = r.at(p.main_wt)
    dirty = r.git("diff", "--name-only", "HEAD", cwd=wt, check=False).splitlines()
    if dirty:
        text = f"discarding build changes to tracked files in {wt}: {', '.join(dirty[:20])}" + (
            f" and {len(dirty) - 20} more" if len(dirty) > 20 else "")
        if isinstance(tid, int):
            ev(tid, text)
        else:
            print(text, flush=True)
    return r.git("checkout", "--force", "--detach", sha, cwd=wt, check=check)


def checkout_detached(r, wt, sha):
    """Put repo r's worktree wt at sha, detached, its tracked files exactly as committed. Never `git clean`:
    untracked and ignored files (a build's output) stay. Makes the worktree if it is missing."""
    if not (wt / ".git").exists():
        r.git("worktree", "prune", check=False)  # a worktree whose folder was deleted is still registered
        r.add_worktree_at(wt, "--detach", sha)
        return
    r.git("checkout", "--force", "--detach", sha, cwd=wt)
    r.git("reset", "--hard", "-q", cwd=wt)


def version_facts(p, t, revs):
    """The version picker of a target with `version`, at the commit it would deploy."""
    try:
        cur = peek_version(p, t, revs)
    except ValueError as e:
        return {"error": f"version picking is off: {e}"}
    v = vstr(cur)
    released = next(iter(tags(p, t, t.release_tag.replace("{version}", v))), None) if t.release_tag else None
    latest_release = next(iter(tags(p, t, t.release_tag.replace("{version}", "*"), revs)), None) \
        if t.release_tag else None
    latest_build = next(iter(tags(p, t, t.build_tags.replace("{version}", v))), None) if t.build_tags else None
    reason = APP_STORE.format(tag=released["name"]) if released else None
    options = [{"key": "same", "version": v, "disabled": bool(released), "reason": reason}]
    options += [{"key": k, "version": vstr(n), "disabled": False, "reason": None} for k, n in bumps(cur).items()]
    return {"current": v, "released": released, "latest_release": latest_release, "latest_build": latest_build,
            "options": options, "default": "patch" if released else "same"}


def target_facts(p, t, main, s):
    """One target as the dashboard shows it: its config, its last deploy, what it would deploy now, and its version
    picker. Git and the version command: read in the background with the project's other facts."""
    rec = deploy_record(p, t.name, s)
    out = {"name": t.name, "repo": t.repo or None, "from": t.from_ or None, "note": t.note or None, "run": t.run,
           "has_version": bool(t.version), "error": t.error, "blocked": None, "commit": None, "behind": None,
           "log": None, "log_unknown": False, "since": None, "version": None,
           "last": rec and {"commit": rec.get("commit"), "show": target_show(p, t, rec.get("commit") or ""),
                            "time": rec.get("time"), "task": rec.get("task"), "version": rec.get("version")}}
    if t.error:
        return out
    try:
        revs = deploy_input(p, t, main)
    except ValueError as e:
        out["blocked"] = str(e)  # "Deploy app test first."
        return out
    pairs = [(r, sha) for r, sha in p.pairs(revs) if r in t.parts(p)]
    subject = " · ".join((f"{r.name}: " if r.sub and not t.repo else "") + r.git("log", "-1", "--format=%s", sha)
                         for r, sha in pairs)
    commit = target_commit(p, t, revs)
    out["commit"] = {"sha": commit, "show": target_show(p, t, commit), "subject": subject}
    mine, out["since"] = target_span(p, t, revs, rec)
    if mine is not None:
        try:  # what a deploy now would ship: its repos' commits since the previous release or last deploy, newest first
            out["behind"] = sum(int(r.git("rev-list", "--count", f"{old}..{sha}")) for r, old, sha in mine)
            out["log"] = target_log(mine)
        except RuntimeError:  # the recorded commit is gone (a rewritten history): what it would ship is unknown
            out["behind"], out["log"], out["log_unknown"] = None, None, True
    if t.version:
        out["version"] = version_facts(p, t, revs)
    return out


def release_pattern(p, t):
    """Target t's release tags as a glob ("v*"): its own release_tag, else its `from` chain's. None if it has none."""
    seen = set()
    while t and t.name not in seen:
        if t.release_tag:
            return t.release_tag.replace("{version}", "*")
        seen.add(t.name)
        t = next((o for o in p.deploys if o.name == t.from_), None) if t.from_ else None
    return None


def tag_version(pattern, name):
    """The version in a tag name matching pattern ("v*", "v3.5.2" -> (3, 5, 2)), or () when it has none."""
    m = re.fullmatch(re.escape(pattern).replace(r"\*", "(.*)"), name)
    try:
        return parse_version(m.group(1)) if m else ()
    except ValueError:
        return ()


def previous_releases(p, t, revs):
    """{repo sub: tag} per repo of target t: the newest release tag by version that revs descends from, but not one
    at revs itself (that is the release being made again). Empty when t has no release tags or none is found."""
    pattern = release_pattern(p, t)
    if not pattern:
        return {}
    at = {r.sub: sha for r, sha in p.pairs(revs)}
    found = [x for x in tags(p, t, pattern, revs, with_repo=True) if not at[x["repo"]].startswith(x["commit"])
             and not x["commit"].startswith(at[x["repo"]])]
    out = {}
    for x in sorted(found, key=lambda x: (tag_version(pattern, x["name"]), x["ts"]), reverse=True):
        out.setdefault(x["repo"], x)
    return out


def target_span(p, t, revs, rec):
    """What a deploy of t at revs ships: ([(repo, sha it starts from, sha it would deploy)] for t's repos, the
    release tag it starts from or None). A repo starts at the previous release tag when t has release tags, else at
    the sha t last deployed. (None, None) when there is nothing to start from: a first deploy with no tag."""
    tagged = previous_releases(p, t, revs)
    old = (rec or {}).get("revs", "").split()
    old = old if len(old) == len(p.parts) else []
    mine = [(r, tagged[r.sub]["commit"] if r.sub in tagged else old[k] if old else None, sha)
            for k, (r, sha) in enumerate(p.pairs(revs)) if r in t.parts(p)]
    mine = [x for x in mine if x[1]]
    if not mine:
        return None, None
    return mine, ", ".join(x["name"] for x in tagged.values()) or None


def target_log(mine):
    """The changelog of a target_span, as changelog() gives it: [[repo or "", short sha, time, subject]], newest
    first. Raises RuntimeError when a recorded commit is gone."""
    return sorted(([r.sub, *c] for r, old, sha in mine for c in first_parent_log(r, old, sha)), key=lambda c: -c[2])


def deploy_facts(p, main):
    s = settings_now()
    out = []
    for t in p.deploys:
        try:
            out.append(target_facts(p, t, main, s))
        except Exception as e:  # one target's trouble must not hide the others
            out.append({"name": t.name, "error": f"could not read it: {e}"})
    return out


def queue_deploy(p, name, version=None, note=None):
    """Queue a deploy of target name, ahead of ordinary tasks. Only the Deploy button and `opsd.py deploy` call this:
    no release, worker, checker or chat ever starts a deploy. Refuses what can't be deployed. Returns the task id."""
    t = p.target(name)
    if t.error:
        raise ValueError(f"{name} can't be deployed: its config is wrong ({t.error})")
    note = (note or "").strip()
    if t.note and not note:
        raise ValueError(f"{t.note} is required to deploy {name}")
    revs = deploy_input(p, t)
    title, choice = f"Deploy {name}", None
    if t.version:
        vf = version_facts(p, t, revs)
        if vf.get("error"):
            raise ValueError(f"can't deploy {name}: {vf['error']}")
        choice = (version or "").strip() or vf["default"]
        title += " → " + vstr(pick_version(choice, parse_version(vf["current"]),
                                          vf["released"] and vf["released"]["name"]))
    body = (f"Run `{t.run}` in {t.folder(p.deploy_wt)} at {target_show(p, t, target_commit(p, t, revs))}"
            + (f" (what {t.from_} last deployed)" if t.from_ else f" ({p.main})")
            + (f", version {choice}" if choice else "") + "." + (f"\n\n{t.note or 'Note'}:\n{note}" if note else ""))
    return add_task(p.name, body, title, priority=100, kind="deploy",
                    deploy={"target": name, "version": choice, "note": note})


# ---------------------------------------------------------------- drafting a deploy's note
# A target with `note` needs one on every deploy ("What's new"). The Deploy dialog asks for a draft as it opens: one
# short Claude turn turns what the deploy ships into release notes for the app's users, and you read, edit and click
# Deploy. Drafting only reads: it never queues anything, so a deploy still starts only on your click.

DRAFT_MAX = 1000  # characters a drafted note may have
DRAFT_TIMEOUT = 60  # seconds the drafting turn may take before the titles stand in
DRAFT_MODEL = "sonnet"
DRAFT_PROMPT = """Write release notes for the end users of an app. They go under the heading "{label}".

Below is what this release ships: one item per change, a title and maybe a summary written by the developer.

Rules:
- 3 to 8 short bullets in plain language, each starting with "- ".
- Only changes a user of the app can see or feel. Merge related items into one bullet.
- Skip internal, plumbing, test, refactor, build and CI items.
- No task ids, commit shas, file names or internal names.
- At most {limit} characters in all.
- Output only the notes: no heading, no preamble, no closing line.

What ships:

{items}
"""


def draft_rows(p, t):
    """What a deploy of t would ship, as the dialog's "What it ships" lists it (target_span: since the previous
    release tag, else the last deploy): [(title, summary or "")], newest first. One row per task (its summary from
    the tasks table) and one per commit no task made. [] when there is no changelog: a first deploy with no release
    tag, a `from` target whose source never deployed, or a recorded commit gone."""
    if t.error:
        return []
    try:
        revs = deploy_input(p, t)
        mine = target_span(p, t, revs, deploy_record(p, t.name))[0]
        rows = titled(p, target_log(mine)) if mine else []
    except (ValueError, RuntimeError):
        return []
    ids = [r["task"] for r in rows if r["task"]]
    sums = {x["id"]: x["summary"] for x in q(f"SELECT id,summary FROM tasks WHERE id IN ({','.join('?' * len(ids))})",
                                              ids)} if ids else {}
    return [(r["title"], (sums.get(r["task"]) or "").strip()) for r in rows]


def claude_text(prompt, model=None, timeout=None):
    """One headless Claude turn with no tools and no saved session: its text answer, or "" if it failed or ran out
    of time. For short one-shot jobs; a task's work goes through run_claude."""
    if limited():
        return ""
    cmd = [CLAUDE, "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--disable-slash-commands", *model_args(model_name(model))]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout or DRAFT_TIMEOUT, cwd=str(DATA))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def cap_note(text):
    """text cut to DRAFT_MAX characters at a line end where it can be."""
    if len(text) <= DRAFT_MAX:
        return text
    cut = text[:DRAFT_MAX]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut


def draft_note(p, name):
    """A draft of target name's note from what it ships: {"note", "source": "model" | "titles" | None}. The model's
    release notes, else a bullet per title when the model fails, times out or says nothing; an empty note when
    there is no changelog, and the owner writes it."""
    t = p.target(name)
    rows = draft_rows(p, t)
    if not rows:
        return {"note": "", "source": None}
    items = "\n".join(f"- {title}" + (f"\n  {' '.join(summary.split())[:600]}" if summary else "")
                      for title, summary in rows)
    text = claude_text(DRAFT_PROMPT.format(label=t.note or "What's new", limit=DRAFT_MAX, items=items), DRAFT_MODEL)
    if text:
        return {"note": cap_note(text), "source": "model"}
    return {"note": cap_note("\n".join(f"- {title}" for title, _ in rows)), "source": "titles"}


def dlog(tid, text):
    with open(RUNS / str(tid) / "deploy.log", "a") as f:
        f.write(f"\n==== {time.ctime()} {text}\n")


def log_tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - n))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def deploy_pipeline(p, tid):
    """Check out what the target deploys in the deploy worktree, run its command, record what it shipped, and move
    main to any commits it made. A retry after the command succeeded only moves main again."""
    t = task(tid)
    d = json.loads(t["deploy"] or "{}")
    try:
        tg = p.target(d.get("target"))
    except ValueError as e:
        return fail(p, tid, str(e))
    if t["start_at"] != "promote" and not deploy_run(p, tid, tg, d):
        return
    deploy_land(p, tid, tg)


def deploy_run(p, tid, tg, d):
    """Run the target's command. True if it succeeded and is recorded; else the task has failed or was stopped."""
    upd(tid, started=task(tid)["started"] or time.time(), error=None, summary=None)
    (RUNS / str(tid)).mkdir(parents=True, exist_ok=True)
    if tg.error:
        fail(p, tid, f"{tg.name} can't be deployed: its config is wrong ({tg.error})")
        return False
    try:
        revs = deploy_input(p, tg)
    except ValueError as e:
        fail(p, tid, str(e))
        return False
    path = p.deploy_wt
    upd(tid, base=revs, worktree=str(path), phase=f"checking out {p.show(revs)}")
    dlog(tid, f"deploy {tg.name}: checking out {p.show(revs, 12)} in {path}")
    copy_workspace_files(p, path)
    fresh = not all(r.present(path) for r in p.parts)
    for r, sha in p.root_first(p.pairs(revs)):
        checkout_detached(r, r.at(path), sha)
    # The clone list, every time: entries added since the worktree was made (a signing key the first deploy lacked)
    # reach it, and its files follow your checkout. Setup runs below, where its output is logged.
    prepare_worktree(p, path, setup=False)
    ev(tid, f"deploying {target_show(p, tg, target_commit(p, tg, revs))} in {path}")
    key = f"deploy_setup:{p.name}"
    if p.setup and (fresh or setting(key) != revs):  # setup again only when the commit changed
        try:
            run_setup(p, tid, path, "deploy.log")
        except SetupFailed as e:
            if not stopped(tid):
                fail(p, tid, str(e))
            return False
        set_setting(key, revs)
    version = None
    if tg.version:
        upd(tid, phase="reading the version")
        try:
            cur = run_version(tg, tg.folder(path))
            released = next(iter(tags(p, tg, tg.release_tag.replace("{version}", vstr(cur)))), None) \
                if tg.release_tag else None
            version = vstr(pick_version(d.get("version"), cur, released and released["name"]))
        except ValueError as e:  # the version moved since you clicked, so your choice no longer holds
            fail(p, tid, f"can't pick the version: {e}")
            return False
        if task(tid)["title"] != f"Deploy {tg.name} → {version}":
            upd(tid, title=f"Deploy {tg.name} → {version}")
        dlog(tid, f"version {vstr(cur)} → {version} ({d.get('version')})")
    rec = deploy_record(p, tg.name)
    port = p.port_for(tid)
    note_file = RUNS / str(tid) / "note.txt"
    note_file.write_text(d.get("note") or "")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPS_")}  # only the deploy's own OPS_ variables
    env.update(OPS_DEPLOY=tg.name, OPS_COMMIT=target_commit(p, tg, revs),
               OPS_DEPLOYED=(rec or {}).get("commit") or "", OPS_NOTE=d.get("note") or "",
               OPS_NOTE_FILE=str(note_file), OPS_PORT=str(port), OPS_TASK_ID=str(tid),
               **{k: str(v).format(port=port) for k, v in p.env.items()})
    if version:
        env["OPS_VERSION"] = version
    rc, tail = run_step(tid, f"deploying {tg.name}" + (f" {version}" if version else ""), tg.run, tg.folder(path),
                        "deploy.log", env=env)
    if stopped(tid):
        return False
    if rc:
        set_setting(key, "")  # Retry starts over, setup included: a fix outside git (a node_modules) gets its turn
        fail(p, tid, f"`{tg.run}` exited {rc}. {p.main} and the record of {tg.name} are unchanged; the deploy "
                     f"worktree {path} is left as it is. Retry starts the deploy over.\n\n{tail[-2000:]}")
        return False
    upd(tid, state="merging", phase="recording the deploy")
    heads = " ".join(r.git("rev-parse", "HEAD", cwd=r.at(path)) for r in p.parts)
    moved = [(r, a, b) for r, a, b in zip(p.parts, revs.split(), heads.split()) if a != b]
    for r, _, b in moved:
        r.git("update-ref", f"refs/ops/deploy-{tid}", b)
    bad = [f"{r.name or 'the repo'} at {b[:8]}" for r, a, b in moved if not is_ancestor(r, a, b)]
    if bad:
        fail(p, tid, f"`{tg.run}` succeeded, but it left {', '.join(bad)}, which does not descend from the commit "
                     f"it deployed. The record of {tg.name} and {p.main} are unchanged; its commits are at "
                     f"refs/ops/deploy-{tid}.")
        return False
    set_setting(f"deployed:{p.name}:{tg.name}", json.dumps(
        {"commit": target_commit(p, tg, heads), "revs": heads, "input": revs, "time": time.time(), "task": tid,
         "version": version}))
    d.update(input=revs, heads=heads, shipped=version)
    upd(tid, deploy=json.dumps(d), start_at="promote")
    ev(tid, f"deployed {tg.name}" + (f" {version}" if version else "") + f"; recorded {p.show(heads)}"
       + (f"; its commits are kept at refs/ops/deploy-{tid}" if moved else ""))
    return True


def deploy_moves(p, t):
    """A deploy's commits that main lacks: [(part, head)] (a deploy that made none has nothing to move)."""
    d = json.loads(t["deploy"] or "{}")
    return [(r, b) for r, a, b in zip(p.parts, d["input"].split(), d["heads"].split())
            if a != b and not is_ancestor(r, b, p.main)]


def deploy_in_the_way(p, t):
    """What still keeps a deploy that needs you from moving main, or None once it can go on."""
    todo = deploy_moves(p, t)
    stuck = [r for r, b in todo if not is_ancestor(r, p.main, b)]
    if stuck:
        return f"{p.main} moved during the deploy; its commits are at refs/ops/deploy-{t['id']}"
    if not todo:
        return None
    commit = deploy_commit(p, todo)
    if t["phase"] == WAIT_CHANGES and clashing(p, commit):
        return "your changes"
    return promote_blocker(p, commit)


def deploy_commit(p, todo):
    """main with the deploy's new commits: the revs string promote moves main to."""
    heads = {r.sub: b for r, b in todo}
    return " ".join(heads.get(r.sub, sha) for r, sha in p.pairs(p.revs(p.main)))


def deploy_land(p, tid, tg):
    """The command succeeded and is recorded: fast-forward main to the commits it made (a build-number bump), with
    the [app] build and your uncommitted changes handled as a release does. main must not have moved meanwhile."""
    t = task(tid)
    answer = t["next_prompt"] if (t["next_prompt"] or "").startswith(ANSWER_HEADS) else ""
    upd(tid, state="merging", next_prompt=None, start_at="promote")
    todo = deploy_moves(p, t)
    if not todo:
        d = json.loads(t["deploy"])
        return deploy_done(p, tid, f"deployed {tg.name}; " + (f"its commits are already in {p.main}"
                                   if d["input"] != d["heads"] else f"it made no commits, so {p.main} stays as it is"))
    if any(not is_ancestor(r, p.main, b) for r, b in todo):
        return wait_for_you(p, tid, f"{p.main} moved during the deploy; its commits are at refs/ops/deploy-{tid}. "
                                    f"Merge them into {p.main} yourself and this goes on by itself, or cancel to "
                                    f"leave {p.main} as it is (the deploy itself is recorded).", WAIT_MAIN)
    commit = deploy_commit(p, todo)
    why = promote_blocker(p, commit)
    if why:
        return wait_for_you(p, tid, why, WAIT)
    if clashing(p, commit):  # a worker may carry your uncommitted changes over: in a worktree of its own, never in
        path = p.wt / f"t{tid}"  # the deploy worktree, whose build output must stay
        copy_workspace_files(p, path)
        for r, sha in p.root_first(p.pairs(commit)):
            if not r.present(path):
                r.add_worktree(path, "--detach", sha)
        upd(tid, worktree=str(path))
    try:
        err = promote(p, tid, commit, note=answer)
    except NeedsYou as e:
        return wait_for_you(p, tid, str(e), WAIT_CHANGES)
    if stopped(tid):
        return
    if err:
        return fail(p, tid, f"The deploy succeeded and is recorded, but {p.main} could not move to its commits "
                            f"(kept at refs/ops/deploy-{tid}): {err}\n\nRetry tries moving {p.main} again.")
    deploy_done(p, tid, f"{p.main} is now {p.show(commit)}, with the deploy's commits")


def deploy_done(p, tid, note):
    t = task(tid)
    if t["worktree"] and Path(t["worktree"]) != p.deploy_wt:  # the worktree that carried your changes over
        removal_failed(tid, t["worktree"], remove_worktree(p, t["worktree"]))
    upd(tid, state="done", phase="", start_at="claude", worktree=str(p.deploy_wt), finished=time.time())
    ev(tid, note)
    notify(f"{p.title}: deployed", f"#{tid}: {t['title']}")



# ---------------------------------------------------------------- design chats and project chats
# Both live in the designs table. kind 'design': read-only planning in the dev worktree that queues tasks
# (one about an OpenSpec change has a worktree of its own and may edit openspec/: see OpenSpec changes below).
# kind 'project': plain Claude in your checkout, as if you ran `claude` there.

DESIGN_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(ls:*)"
DESIGN_PROCS = {}  # design id -> Popen
DESIGN_LIVE = {}  # design id -> tool lines of the turn in progress
QUEUE_BLOCK = re.compile(r"```queue[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)


def design(did):
    return q("SELECT * FROM designs WHERE id=?", (did,), one=True)


# A design chat can't edit the project, but it may write documents of its own: a design doc, an HTML prototype,
# a diagram. They go in a folder of its own outside every repo, and the dashboard serves and lists them.
FILE_TEXT = "text/plain; charset=utf-8"
FILE_TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".md": FILE_TEXT,
              ".txt": FILE_TEXT, ".json": "application/json", ".csv": FILE_TEXT, ".css": "text/css",
              ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm"}


def chat_files(did):
    """The files a design chat wrote, newest first: name, size, mtime and the URL the dashboard serves it at."""
    out = []
    folder = CHAT_FILES / str(did)
    for f in folder.iterdir() if folder.is_dir() else []:
        if f.is_file() and re.fullmatch(r"[\w.-]+", f.name):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime,
                        "url": f"/api/designs/{did}/files/{f.name}"})
    return sorted(out, key=lambda x: -x["mtime"])


# ---------------------------------------------------------------- links in chats (README: Links in chats)
# A chat message names tasks, repo files, pictures and design documents. The dashboard asks link_check about all of
# a message's targets in one request, and links only those that exist; /api/files serves a repo file from dev.
FILE_MAX = 1 << 20
MEDIA_MAX = 50 << 20  # a video or picture, which /api/files serves by byte range
MEDIA = {k for k, v in FILE_TYPES.items() if v.startswith(("image/", "video/"))}


def repo_file(p, rel):
    """(path, None) for rel, a file of p as on its dev worktree, or (None, why not). Only a file inside the dev
    worktree once symlinks and '..' are resolved, outside .git, not git-ignored and at most FILE_MAX bytes
    (MEDIA_MAX for a video or picture)."""
    rel = (rel or "").strip()
    if not rel or rel.startswith(("/", "~")) or "\0" in rel:
        return None, f"Not a repo path: {rel}"
    root = p.dev_wt
    path = root / rel
    if not within(path, root) or ".git" in Path(os.path.relpath(os.path.realpath(path), os.path.realpath(root))).parts:
        return None, f"Outside the project: {rel}"
    if not path.is_file():
        return None, f"Not on dev: {rel}"
    part = max((r for r in p.parts if within(path, r.at(root))), key=lambda r: len(r.prefix), default=None)
    if not part or subprocess.run(["git", "check-ignore", "-q", "--", os.path.realpath(path)], cwd=part.at(root),
                                  capture_output=True).returncode == 0:
        return None, f"Ignored by git: {rel}"
    media = path.suffix.lower() in MEDIA
    if path.stat().st_size > (MEDIA_MAX if media else FILE_MAX):
        return None, f"Over {50 if media else 1} MB: {rel}"
    return Path(os.path.realpath(path)), None


def head_words(t):
    return " ".join(re.sub(r"[`*_]", "", t).lower().split())


def file_section(text, sec):
    """The markdown heading of text that sec names: the longest one sec starts with, as a sentence may run on
    after it ("README.md § Design mode for details"). None if there is none."""
    sec = head_words(sec)
    heads = [head_words(h) for h in re.findall(r"(?m)^ {0,3}#{1,6}[ \t]+(.*?)[ \t#]*$", text)]
    return max((h for h in heads if h and (sec == h or sec.startswith(h + " "))), key=len, default=None)


def link_check(project, refs, heads=None, chats=None):
    """{ref: None if its target exists, else why not} for refs of the forms task:<id>, chat:<id>, file:<repo
    path>[:<line>][§<heading>], spec:[<sub>/]<change>, image:<name> and design:<chat id>/<name>. heads gets, for each
    file ref with a § that exists, the heading it found, so the page can end the link there; chats gets each chat
    ref's {title, project}, for its tooltip and to open it."""
    p = PROJECTS.get(project or "")
    out, heads = {}, {} if heads is None else heads
    chats = {} if chats is None else chats
    for ref in list(dict.fromkeys(refs or []))[:500]:
        kind, _, val = str(ref).partition(":")
        if kind == "task":
            out[ref] = None if val.isdigit() and task(int(val)) else f"No task #{val}"
        elif kind == "chat":  # any project's chat, as a task: not the message's project
            d = q("SELECT title,project FROM designs WHERE id=?", (int(val),), one=True) if val.isdigit() else None
            out[ref] = None if d and d["project"] in PROJECTS else \
                f"Chat {val} is in {d['project']}, which is archived" if d else f"No chat {val}"
            if d:
                chats[ref] = {"title": d["title"] or "Untitled", "project": d["project"]}
        elif kind == "image":
            ok = re.fullmatch(r"[\w-]+\.\w+", val) and (IMAGES / val).is_file()
            out[ref] = None if ok else f"Picture not found: {val}"
        elif kind == "design":
            m = re.fullmatch(r"(\d+)/([\w-][\w.-]*)", val)
            out[ref] = None if m and (CHAT_FILES / m[1] / m[2]).is_file() else f"No such design document: {val}"
        elif not p:
            out[ref] = f"Unknown project: {project}"
        elif kind == "file":  # <path>[:<line>][§<heading>]
            path, _, sec = val.partition("§")
            m = re.fullmatch(r"(.+?)(?::(\d+)(?:-\d+)?)?", path.strip())
            f, why = repo_file(p, m[1]) if m else (None, f"Not a repo path: {val}")
            if not why and (m[2] or sec.strip()):
                text = f.read_bytes().decode(errors="replace")
                if m[2] and int(m[2]) > text.count("\n") + 1:
                    why = f"{m[1]} has only {text.count(chr(10)) + 1} lines"
                elif sec.strip() and not (head := file_section(text, sec)):
                    why = f"No section “{sec.strip()}” in {m[1]}"
                elif sec.strip():
                    heads[ref] = head
            out[ref] = why
        elif kind == "spec":
            sub, _, name = val.rpartition("/")
            ok = sub in [r.sub for r in p.parts] and re.fullmatch(r"[\w.-]+", name) and name != "archive" and \
                (spec_part(p, sub).at(p.dev_wt) / "openspec" / "changes" / name).is_dir()
            out[ref] = None if ok else f"No OpenSpec change on dev: {name}"
        else:
            out[ref] = f"Unknown link: {ref}"
    return out


# A chat is "ready for you" when Claude isn't replying, and has a new reply when its last answer (Claude's, or a
# note from the queue) came after you last looked at it. Ending a chat takes it out of both until you reopen it, or
# send it a message: an open reopens it only when the caller saw it ended (conv_open's compare-and-set).
CHAT_COLS = ("id,project,title,state,updated,cost,kind,spec,spec_draft,seen,ended,subject,(SELECT MAX(ts) FROM design_msgs m "
             "WHERE m.design_id=designs.id AND m.role!='user') AS replied")


def chat_counts():
    """Open chats per kind, for the badges on the Design and Chat tabs: how many have a new reply, how many
    are waiting on Claude. Each project's counts separately, so the dashboard can apply its project filter."""
    return q(f"SELECT project, kind, SUM(state!='thinking' AND COALESCE(replied,0)>seen) AS unread, "
             f"SUM(state='thinking') AS replying FROM (SELECT {CHAT_COLS} FROM designs WHERE ended IS NULL) "
             f"GROUP BY project, kind")


DESIGN_LOCK = threading.Lock()  # a message sent as a reply ends: it either starts the next turn or waits in the queue


def design_say(did, text):
    """Start one design-chat turn in the background. While Claude is replying, the message waits in the chat's
    queue instead, and goes as the next turn when the reply ends (send_queued). Returns whether it was queued."""
    with DESIGN_LOCK:
        d = design(did)
        if d["state"] == "thinking":
            q("UPDATE designs SET queued=?, updated=?, ended=NULL WHERE id=?",
              (json.dumps(queued_of(d) + [text]), time.time(), did))
            return True
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "user", text, time.time()))
        q("UPDATE designs SET state='thinking', updated=?, ended=NULL WHERE id=?", (time.time(), did))  # reopens it
    threading.Thread(target=design_turn, args=(did, text), daemon=True).start()
    return False


def queued_of(d):
    return json.loads(d["queued"] or "[]")


def send_queued(did):
    """After a reply: the messages queued while it ran go as the next turn, as one message. A slash command goes on
    its own, as the CLI runs one only from the start of the prompt. An ended chat drops its queue."""
    with DESIGN_LOCK:
        d = design(did)
        waiting = queued_of(d)
        if not waiting or d["state"] == "thinking":
            return
        if d["ended"]:
            q("UPDATE designs SET queued='[]' WHERE id=?", (did,))
            return
        n = 1 if waiting[0].strip().startswith("/") else \
            next((k for k, t in enumerate(waiting) if t.strip().startswith("/")), len(waiting))
        q("UPDATE designs SET queued=? WHERE id=?", (json.dumps(waiting[n:]), did))
        now = time.time()
        for t in waiting[:n]:
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "user", t, now))
        q("UPDATE designs SET state='thinking', updated=? WHERE id=?", (now, did))
    threading.Thread(target=design_turn, args=(did, "\n\n".join(waiting[:n])), daemon=True).start()


def ops_note(p, did, extra="", text=""):
    """Context the dashboard adds to each user message: the queue, what happened since the last one
    (what the last queue block did, edits saved to dev, tasks queued with the buttons), and the chats text names."""
    live = q("SELECT id,state,title FROM tasks WHERE project=? AND state NOT IN ('done','cancelled') ORDER BY id",
             (p.name,))
    parts = ["queue: " + ("; ".join(f"#{t['id']} {t['state']} {t['title']}" for t in live[:25]) or "empty"),
             f"merges go straight to {p.main} (direct_main), so release does nothing here" if p.direct_main else
             "promote after merge: " + ("on" if release_default(p) else "off") + " by default for this project"]
    dirty = facts(p).get("uncommitted") or []
    if dirty:
        parts.append("checkout: " + "; ".join(
            f"{u['count']} uncommitted {files(u['count'])} in {u['repo']} (not on {p.dev}"
            + (f"; oldest {day(u['oldest'])}" if u["oldest"] else "") + ")" for u in dirty))
    parts += news(did) + agree_open(did) + ([extra] if extra else []) + chat_mentions(text)
    return "[ops: " + " | ".join(parts) + "]\n\n"


def chat_said(did, text):
    """The [ops: ...] items a project chat's message gets: word from a project it sent something to, and the chats
    text names (a design chat gets these in ops_note)."""
    return news(did) + agree_open(did) + chat_mentions(text)


CHAT_REF = re.compile(r"(?<![\w/:.#&-])chat:(\d+)(?![\w-])")  # chat:42 as a word of its own (README: LINK-8)


def chat_kind(d):
    return "OpenSpec change" if d["spec"] or d["spec_draft"] else \
        "project chat" if d["kind"] == "project" else "design chat"


def chat_mentions(text):
    """One [ops: ...] item per chat:N in the owner's message, outside fenced code and URLs: what that chat is and
    how to read it. Its transcript is never pasted."""
    keep, fence = [], None
    for line in (text or "").split("\n"):
        m = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is None and m:
            fence = m[1]
        elif fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
        else:
            keep.append(re.sub(r"https?://\S+", " ", line))
    out = []
    for n in dict.fromkeys(CHAT_REF.findall("\n".join(keep))):
        d = q("SELECT id,project,title,kind,spec,spec_draft,updated,(SELECT COUNT(*) FROM design_msgs m "
              "WHERE m.design_id=designs.id) AS n FROM designs WHERE id=?", (int(n),), one=True)
        out.append(f"chat:{n}: no such chat" if not d else
                   f"chat:{n} is \"{d['title'] or 'Untitled'}\" ({d['project']}, {chat_kind(d)}, {d['n']} message"
                   f"{'' if d['n'] == 1 else 's'}, last active {day(d['updated'] or 0)}): read it with "
                   f"`{OPSD_CMD} chat show {n}`")
    return out


def topic_search(text="", project="", exclude=None, limit=10):
    """GET /api/topics/search: the chats of every project that isn't archived whose title has each word of text, or
    whose id starts with it, for the message box's @ picker: project's first, open before ended, then newest
    activity, at most limit."""
    text = (text or "").strip().lower()
    words, num = text.split(), text.removeprefix("chat:")
    out = []
    for d in q("SELECT id,project,title,kind,spec,spec_draft,updated,ended FROM designs "
               "ORDER BY updated DESC, id DESC"):
        title = (d["title"] or "Untitled").lower()
        if d["project"] in PROJECTS and d["id"] != exclude and (
                all(w in title for w in words) or (num.isdigit() and str(d["id"]).startswith(num))):
            out.append({"id": d["id"], "title": d["title"] or "Untitled", "project": d["project"],
                        "kind": chat_kind(d), "ended": d["ended"], "last": d["updated"] or 0})
    out.sort(key=lambda t: (t["project"] != project, bool(t["ended"]), -t["last"]))
    return out[:max(0, min(limit, 50))]


def chat_show(did, last=None):
    """opsd.py chat show: a chat's header line, then its messages oldest first (only the last `last` with it), each
    labelled and dated, in full. None when there is no such chat."""
    d = q("SELECT * FROM designs WHERE id=?", (did,), one=True)
    if not d:
        return None
    msgs = q("SELECT role,text,ts FROM design_msgs WHERE design_id=? ORDER BY id", (did,))
    if last is not None:
        msgs = msgs[len(msgs) - last:] if last < len(msgs) else msgs
    when = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))  # noqa: E731
    who = {"user": "The owner", "assistant": "Claude", "ops": "The dashboard"}
    head = (f"chat:{did} · {d['project']} · {chat_kind(d)} · \"{d['title'] or 'Untitled'}\" · {d['state']} · "
            + (f"ended {when(d['ended'])}" if d["ended"] else "open") + f" · created {when(d['created'])}")
    return "\n\n".join([head] + [f"--- {who.get(m['role'], m['role'])} · {when(m['ts'])}\n{m['text'] or ''}"
                                 for m in msgs]) + "\n"


def news(did):
    """The notes left in a chat since your last message: what its queue block did, edits saved, word from a
    project it sent something to."""
    prev = q("SELECT id FROM design_msgs WHERE design_id=? AND role='user' ORDER BY id DESC LIMIT 1 OFFSET 1",
             (did,), one=True)
    return [m["note"] for m in q("SELECT note FROM design_msgs WHERE design_id=? AND role!='user' AND id>? "
                                 "AND note!='' ORDER BY id", (did, prev["id"] if prev else 0))]


def design_system_prompt(p, d=None, cwd=None):
    notes = f"## Notes for {p.title}\n\n{p.design_notes}\n" if p.design_notes else ""
    did = d["id"] if d else 0
    out = (CODE / "design_prompt.md").read_text().format(project=p.title, dev_wt=cwd or p.dev_wt, dev=p.dev,
                                                         files=CHAT_FILES / str(did), did=did, checkout=p.repo,
                                                         research=f"{OPSD_CMD} add -p {shlex.quote(p.name)} --research",
                                                         schedule_edit=f"{OPSD_CMD} schedule edit",
                                                         chat_show=f"{OPSD_CMD} chat show",
                                                         notes=notes + peer_brief(p, "design"))
    if d and d["spec"]:
        out += "\n" + (CODE / "spec_prompt.md").read_text().format(change=d["spec"], path=SPEC_DIR / d["spec"],
                                                                   dev=p.dev)
    elif d and d["spec_draft"]:
        out += "\n" + (CODE / "spec_new_prompt.md").read_text().format(path=SPEC_DIR, dev=p.dev)
    return out


def stream_claude(cmd, cwd, log_dir, live, procs, key, on_session, env=None, on_model=None, on_call=None):
    """Run one chat turn of claude -p, tool lines into live. Returns (reply text, cost).
    on_model gets the model the session runs on, from its init event; on_call the context of each main-agent call."""
    texts, cost, sid, limit = [], 0.0, None, None
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "claude.jsonl", "a") as log, open(log_dir / "claude.err", "a+") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=err, text=True,
                                start_new_session=True)
        procs[key] = proc
        for line in proc.stdout:
            log.write(line)
            log.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("session_id") and e["session_id"] != sid:  # once, not on each of thousands of lines
                sid = e["session_id"]
                on_session(sid)
            if on_model and (m := init_model(e)) is not None:
                on_model(m)
            if on_call and (ctx := main_call(e)):
                on_call(ctx)
            if (used := usage_of(e)) is not None:
                usage_seen(used)
            if (hit := limit_of(e)) is not None:
                limit = {**hit, **(limit or {})}
                if e.get("type") == "assistant":
                    continue  # its text is Claude's own words on the limit, not a reply
            if e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        texts.append(c["text"].strip())
                    elif c.get("type") == "tool_use":
                        texts.clear()  # text before a tool call is narration; the reply comes after
                        live.append(tool_line(c.get("name"), c.get("input") or {}))
            elif e.get("type") == "result":
                cost += cost_added(e.get("session_id") or sid, e.get("total_cost_usd"))
        proc.wait()
        err.seek(0)
        err_tail = err.read()[-1200:]
    if limit is not None:  # the queue waits it out too; the message is not sent again by itself
        return f"Usage limit reached. Resets at {clock(limit_hit(limit))}.", cost
    reply = "\n\n".join(texts)
    if not reply:
        reply = f"(no reply; claude exited {proc.returncode})\n{err_tail.strip()}".strip()
    return reply, cost


def chat_stale(d):
    """Why chat d's next turn should start a fresh session, as (words for the note, words for the chat), or None to
    resume it: its context at its last call was over FRESH_ABOVE, so every call rereads too much. Unlike a task, an
    expired cache alone doesn't start over: a chat's history is the conversation, and the note keeps only its text."""
    ctx = d["ctx_tokens"] or 0
    if not d["session_id"] or ctx <= FRESH_ABOVE:
        return None
    return f"its context reached {ctx:,} tokens", f"this chat's context reached {ctx // 1000}k tokens"


def chat_handoff(did, why):
    """The conversation so far, for a chat's fresh session: every message but the one being answered now, oldest
    first, each cut at 4000 characters, the oldest dropped to keep it under HANDOFF_CHARS. No Claude call."""
    msgs = q("SELECT role,text FROM design_msgs WHERE design_id=? ORDER BY id", (did,))
    if msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]  # this turn's message: it follows the note as "Now"
    return handoff_of(msgs, f"## Handoff from your earlier session\n\nYour earlier session of this chat ended (reason: {why}). "
                            "This is a fresh session. Files you changed or read are as they were; re-read what you need. "
                            "The conversation so far, oldest first:\n\n", HANDOFF_CHARS)


def handoff_of(msgs, head, room):
    """head, then msgs ({role, text}) oldest first, each cut at 4000 characters, the oldest cut to stay under room."""
    who = {"user": "The owner", "assistant": "You", "ops": "The dashboard"}
    said = "\n\n".join(f"{who.get(m['role'], m['role'])}: {(m['text'] or '')[:4000]}" for m in msgs) or "(none)"
    room -= len(head)
    if len(said) > room:
        said = "… (earlier messages cut)\n\n" + said[len(said) - room + 40:]
    return head + said


# ---------------------------------------------------------------- the project conversation (README: TODAY-2)
# The Today view shows one conversation per project, made of topics. A topic is an ordinary chat row (a design chat,
# a project chat or an OpenSpec change's chat) or a research thread, stored as before. The daemon keeps which one is
# current, and the mode of the next new one, in the setting conv:<project>: {"topic": id or "r:<thread>" or null,
# "mode", "from": the topic a mode switch left, whose handoff the new topic's first message carries}.
MODES = {"design": "plan & design", "project": "edit my checkout", "research": "research"}
SWITCH_CHARS = 8_000  # about 2k tokens: a mode switch's handoff of the topic before
TOPIC_BLOCK = re.compile(r"```topic[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)
SWITCH_HEAD = "## Handoff from the topic before this one"


def research_links(project):
    """The research projects linked to project, [{id, name}], or [] while research is off."""
    try:
        return (research.links().get(project) or []) if research.current() else []
    except Exception as e:
        print(f"research links of {project}: {e}", flush=True)
        return []


def research_topics(project):
    """The research threads about the research projects linked to project, as topics. Last activity: the latest
    turn's start."""
    targets = {f"project:{r['id']}" for r in research_links(project)}
    if not targets:
        return []
    when = lambda iso: datetime.datetime.fromisoformat(iso).timestamp() if iso else 0  # noqa: E731
    ended = research_ended()
    with research.lock:
        threads = [t for t in research.load_threads().values() if t.get("target") in targets]
        jobs = {j["id"]: j for j in research.load_jobs()} if threads else {}
    return [{"id": f"r:{t['id']}", "title": (t.get("title") or "").strip().split("\n")[0] or "Research", "mode": "research",
             "kind": "research",
             "target": t["target"], "created": when(t.get("created")), "unread": False,
             "state": "thinking" if any((jobs.get(j) or {}).get("status") == "running" for j in t["turns"]) else "idle",
             "ended": ended.get(t["id"]),
             "last": max([when(jobs[j].get("started")) for j in t["turns"] if j in jobs] + [when(t.get("created"))])}
            for t in threads]


def research_ended():
    """The research threads you ended in the Today view, {thread id: when}: the threads live in the research repo,
    so the daemon keeps this in the setting research_ended."""
    try:
        v = json.loads(setting("research_ended") or "{}")
    except ValueError:
        v = {}
    return v if isinstance(v, dict) else {}


def topics(project):
    """Every topic of project, newest activity first: its chats, ended ones too, and its research threads. A topic is
    unread when it has a reply you haven't opened and is not ended: an ended topic is done, whatever lands in it
    later (the Today list's Read items follow the same rule, see todos_list)."""
    out = [{"id": d["id"], "title": d["title"], "mode": "project" if d["kind"] == "project" else "design",
            "kind": d["kind"] or "design", "spec": d["spec"], "spec_draft": d["spec_draft"], "created": d["created"],
            "last": d["updated"] or 0, "state": d["state"], "ended": d["ended"], "subject": d["subject"],
            "unread": d["state"] != "thinking" and not d["ended"] and (d["replied"] or 0) > (d["seen"] or 0)}
           for d in q(f"SELECT {CHAT_COLS},created FROM designs WHERE project=?", (project,))]
    return sorted(out + research_topics(project), key=lambda t: -(t["last"] or 0))


def conv_state(project, tops):
    """The kept conv:<project>, checked against tops: a topic that is gone falls back to the latest one, and a
    topic's mode is its own (a topic never changes kind)."""
    try:
        c = json.loads(setting(f"conv:{project}") or "{}")
    except ValueError:
        c = {}
    ids = {t["id"]: t for t in tops}
    if not isinstance(c, dict) or "topic" not in c or (c["topic"] is not None and c["topic"] not in ids):
        latest = next((t for t in tops if not t["ended"]), None)  # the latest open topic, else a new one
        c = {"topic": latest["id"], "mode": latest["mode"]} if latest else {"topic": None, "mode": "design"}
    if c["topic"] is not None:
        c = {"topic": c["topic"], "mode": ids[c["topic"]]["mode"]}
    if c.get("mode") not in MODES or (c["mode"] == "research" and not research_links(project)):
        c["mode"] = "design"
    if c.get("from") not in ids:
        c.pop("from", None)
    if c["topic"] is not None or not isinstance(c.get("subject"), str) or not SUBJECT.fullmatch(c["subject"]):
        c.pop("subject", None)
    if c["topic"] is not None or c.get("new") is not True:  # New topic was clicked: the next message starts a fresh one
        c.pop("new", None)
    if c["topic"] is not None or not isinstance(c.get("title"), str) or not c["title"].strip():
        c.pop("title", None)  # a pending tab's title (New tab ↗, a panel's subject), until its first message
    return c


def conv_view(project):
    """GET /api/conversation: the project's topics, the current one and the mode of the message box."""
    proj(project)
    tops = topics(project)
    c = conv_state(project, tops)
    links = research_links(project)
    return {"project": project, "topic": c["topic"], "mode": c["mode"], "subject": c.get("subject"),
            "new": bool(c.get("new")), "title": c.get("title"), "modes": MODES,
            "topics": tops,
            "from": next((t for t in tops if t["id"] == c.get("from")), None),
            "research": {"on": bool(research.current()), "name": links[0]["name"] if links else None}}


def conv_pin(project):
    """Keep the topic Today shows for project in conv:<project> when nothing, or a topic that is gone, is kept there,
    so a new chat (the quick chat, README: TODAY-6) doesn't become the shown topic through the latest-activity
    fallback."""
    try:
        c = json.loads(setting(f"conv:{project}") or "{}")
    except ValueError:
        c = {}
    tops = topics(project)
    ids = {t["id"] for t in tops}
    if not isinstance(c, dict) or "topic" not in c or (c["topic"] is not None and c["topic"] not in ids):
        set_setting(f"conv:{project}", json.dumps(conv_state(project, tops)))


def quick_topics(limit=10):
    """GET /api/designs/quick: the topics the quick chat started (designs.quick), in every project that isn't
    archived, latest activity first, ended ones too: its Recent list (README: TODAY-6)."""
    rows = q("SELECT id,project,title,updated,ended FROM designs WHERE quick=1 ORDER BY updated DESC, id DESC")
    return [r for r in rows if r["project"] in PROJECTS][:max(0, min(limit, 50))]


UNSEEN = object()  # conv_open's ended when the caller sent none


def conv_open(project, how, ref=None, mode=None, ended=UNSEEN, title=None):
    """Which topic the next message goes to (README: TODAY-2), kept in conv:<project>: the tab you see.
    chat/topic: that one (a tab, a to-do's topic, a row under Ended, a link). An ended one reopens only
    when ended, what the caller last saw of it (null, or when it ended), is what is stored: you knowingly opened an
    ended topic. Saw it open but it has ended since (a stale Today list): it stays ended and doesn't become the
    current topic, and the answer names it in still_ended. No ended sent (the CLI, older callers): it reopens;
    subject: the open plan & design topic about ref (a panel's subject, see SUBJECT, or New tab ↗ for a to-do with
    one), else none yet: the first message makes it (conv_say); task: the current plan & design topic, else the
    latest one, else a new one (Ask Claude about the research runs); new: a new topic in the current mode, or mode;
    mode: switch mode, which starts a new topic of that kind carrying a handoff of the one that was open (switching
    back before you send returns to it). title: what a pending subject or new topic's tab is called until its first
    message (the to-do's or the panel's title), cut to 80."""
    proj(project)
    tops = topics(project)
    ids = {t["id"]: t for t in tops}
    c = conv_state(project, tops)
    cur = ids.get(c["topic"])
    if how in ("chat", "topic"):
        ref = int(ref) if str(ref).isdigit() else ref
        if ref not in ids:
            raise ValueError(f"no topic {ref} in {project}")
        if ids[ref]["ended"]:  # opening an ended topic reopens it, when you saw it ended (compare-and-set)
            if ended is not UNSEEN and ended != ids[ref]["ended"]:
                return {**conv_view(project), "still_ended": ref}
            topic_reopen(project, ref)
        c = {"topic": ref, "mode": ids[ref]["mode"]}
    elif how == "subject":
        if not isinstance(ref, str) or not SUBJECT.fullmatch(ref):
            raise ValueError(f"no subject {ref!r}")
        mine = next((t for t in tops if t.get("subject") == ref and t["mode"] == "design" and not t["ended"]), None)
        c = {"topic": mine["id"], "mode": "design"} if mine else {"topic": None, "mode": "design", "subject": ref,
                                                                  **pending_title(title)}
    elif how == "task":
        if (cur["mode"] if cur else c["mode"]) != "design":
            latest = next((t for t in tops if t["mode"] == "design"), None)
            c = {"topic": latest["id"] if latest else None, "mode": "design"}
    elif how == "new":  # New topic (+), or New tab ↗ for a to-do without a subject (with its title)
        c = {"topic": None, "mode": mode if mode in MODES else c["mode"], "new": True, **pending_title(title)}
    elif how == "mode":
        if mode not in MODES:
            raise ValueError(f"no mode {mode!r}")
        if mode == "research" and not research_links(project):
            raise ValueError(f"research is off, or no research project is linked to {project}")
        left = cur or ids.get(c.get("from"))
        if left and not cur and left["mode"] == mode:
            c = {"topic": left["id"], "mode": mode}
        elif mode != c["mode"]:
            c = {"topic": None, "mode": mode, **({"from": left["id"]} if left else {})}
    else:
        raise ValueError(f"unknown how {how!r}")
    set_setting(f"conv:{project}", json.dumps(c))
    return conv_view(project)


def pending_title(title):
    """{"title": ...} for a pending tab's title, cut to 80 characters, or {} for none."""
    title = " ".join(str(title or "").split())[:80] if isinstance(title, str) else ""
    return {"title": title} if title else {}


# A topic's subject: the one thing it is about, so a panel or New tab ↗ opens its own topic (conv_open how=subject).
SUBJECT = re.compile(r"task:\d+|project:[^:\s]+:(?:promote|ship|commit)|schedule:\d+")
SUBJECT_CHARS = 4_000  # the note a subject topic's first turn carries


def subject_about(project, subject):
    """A new subject topic's title, the line you see at its start and the note Claude gets with its first message:
    what the task, project item or schedule is, as the dashboard knows it now."""
    kind, _, rest = subject.partition(":")
    if kind == "task":
        t = task(int(rest)) or {}
        tid, title = int(rest), t.get("title") or ""
        lines = [f"This topic is about task #{tid} of {project}: “{title or '(gone)'}”."]
        if t:
            lines.append(f"State: {t['state']}" + (f" ({t['phase']})" if t.get("phase") else "") + ".")
            gate = q("SELECT text FROM events WHERE task_id=? AND text LIKE 'gate %' ORDER BY id DESC LIMIT 1",
                     (tid,), one=True)
            if gate:
                lines.append(f"Gate: {clip(gate['text'], 300)}")
            for k, head in (("question", "Its question"), ("error", "Its error"), ("summary", "Its summary")):
                if t.get(k):
                    lines.append(f"{head}: {clip(t[k], 600)}")
            p = PROJECTS.get(t["project"])
            if p and t.get("branch"):
                try:
                    st = diffstat(p, t)
                except Exception:
                    st = ""
                if st:
                    lines.append(f"Files changed (git diff --stat):\n{clip(st, 1200)}")
            if t.get("body"):
                lines.append(f"Its brief:\n{clip(t['body'], 1500)}")
        return {"title": f"#{tid} {title}".strip(), "line": f"About task #{tid}",
                "note": "\n\n".join(lines)[:SUBJECT_CHARS]}
    if kind == "schedule":
        s = q("SELECT * FROM schedules WHERE id=?", (int(rest),), one=True) or {}
        title = s.get("title") or f"Schedule #{rest}"
        note = (f"This topic is about the recurring job “{title}” (schedule #{rest}) of {project}"
                + (f", every {s['every']}" if s.get("every") else "") + "."
                + (f"\n\nWhat it runs:\n{clip(s['body'], 2500)}" if s.get("body") else ""))
        return {"title": title, "line": f"About the schedule “{title}”", "note": note[:SUBJECT_CHARS]}
    name, _, what = rest.partition(":")
    p = proj(name)
    try:
        info = main_info(p, settings_now())
        it = next((i for i in todos_list([], [info], {}, notes=False) if i["key"] == subject), None)
    except Exception:
        info, it = {}, None
    verb = {"promote": "Promote", "ship": "Deploy", "commit": "Review uncommitted files in"}[what]
    title = it["title"] if it else f"{verb} {p.title}"
    lines = [f"This topic is about the to-do “{title}” of {name}" + (f": {it['why']}" if it and it["why"] else ".")]
    if what == "ship" and info.get("main_log"):
        lines.append(f"On {p.main} since the last deploy, newest first:\n" + "\n".join(
            f"- {'#' + str(c['task']) + ' ' if c.get('task') else ''}{c['title']}" for c in info["main_log"][:15]))
    return {"title": title, "line": f"About {title}", "note": "\n\n".join(lines)[:SUBJECT_CHARS]}


def topic_ref(project, ref):
    """The topic ref names in project ("12", 12 or "r:<thread>"), from topics(); ValueError when there is none."""
    ref = int(ref) if str(ref).isdigit() else ref
    t = next((t for t in topics(project) if t["id"] == ref), None)
    if not t:
        raise ValueError(f"no topic {ref} in {project}")
    return t


def topic_end(project, ref):
    """POST /api/conversation/end: you are done with this topic. It leaves Earlier topics for the Ended group, and
    leaves the to-do list: its replies count as read, now and later. A chat is ended as End chat ends it, so the
    Design and Chat tabs agree: its queued messages are dropped, and a reply in progress is stopped. The stopped
    reply's turn, when it finishes, leaves the topic ended and read (design_turn, send_queued). Nothing is deleted;
    topic_reopen puts it back."""
    proj(project)
    t = topic_ref(project, ref)
    now = time.time()
    if t["mode"] == "research":
        with research.lock:  # research_ended is read and written whole: one writer at a time
            e = research_ended()
            e[t["id"][2:]] = now
            set_setting("research_ended", json.dumps(e))
            if t["state"] == "thinking":  # stop its running research turns
                th = research.load_threads().get(t["id"][2:]) or {}
                for j in research.load_jobs():
                    if j["id"] in (th.get("turns") or []) and j.get("status") == "running":
                        research.stop_job(j["id"])
    else:
        q("UPDATE designs SET ended=?, seen=?, queued='[]' WHERE id=?", (now, now, t["id"]))
        stop(DESIGN_PROCS.get(t["id"]))
    todo_remind_close(project, t["id"])
    try:
        c = json.loads(setting(f"conv:{project}") or "{}")
    except ValueError:
        c = {}
    if isinstance(c, dict) and c.get("topic") == t["id"]:  # it was the current topic: the next message starts one
        set_setting(f"conv:{project}", json.dumps({"topic": None, "mode": "design"}))
    return conv_view(project)


def topic_reopen(project, ref):
    """POST /api/conversation/reopen: put an ended topic back under Earlier topics (and on the to-do list when it
    gets a reply). Opening an ended topic, or sending it a message, does this too."""
    proj(project)
    t = topic_ref(project, ref)
    if t["mode"] == "research":
        with research.lock:
            e = research_ended()
            if e.pop(t["id"][2:], None) is not None:
                set_setting("research_ended", json.dumps(e))
    else:
        q("UPDATE designs SET ended=NULL WHERE id=?", (t["id"],))
    return conv_view(project)


def topics_end_read(project, days):
    """POST /api/conversation/end-read: end every open topic of project that you have read, that Claude isn't
    replying in, that isn't the current one, and whose last activity is more than days old. Returns how many."""
    proj(project)
    try:
        days = float(days)
    except (TypeError, ValueError):
        raise ValueError(f"days must be a number, not {days!r}")
    if days < 0:
        raise ValueError("days can't be negative")
    tops = topics(project)
    cur, cutoff = conv_state(project, tops)["topic"], time.time() - days * 86400
    old = [t["id"] for t in tops if not t["ended"] and not t["unread"] and t["state"] != "thinking"
           and t["id"] != cur and (t["last"] or 0) < cutoff]
    for ref in old:
        topic_end(project, ref)
    return {**conv_view(project), "ended_now": len(old)}


def topic_handoff(t, mode):
    """A mode switch's short handoff of topic t, for the first message of the new topic: what was said there,
    oldest first, at most SWITCH_CHARS."""
    if t["mode"] == "research":
        th = research.thread_view(t["id"][2:]) or {"turns": []}
        msgs = [m for j in th["turns"] for m in ({"role": "user", "text": (j.get("question") or "").split(
            "\n\n" + SWITCH_HEAD)[0]}, {"role": "assistant", "text": j.get("result") or ""})]
    else:
        msgs = q("SELECT role,text FROM design_msgs WHERE design_id=? ORDER BY id", (t["id"],))
    return handoff_of(msgs, f"{SWITCH_HEAD}\n\nThe owner switched this project's conversation from "
                            f"{MODES[t['mode']]} to {MODES[mode]}, so this is a new topic with a session of its own. The "
                            f"topic before it, “{t['title']}”, so far, oldest first. Re-read what you need:\n\n",
                      SWITCH_CHARS)


def todo_line(todo):
    """The line, and the note for Claude, that a message sent with a to-do attached carries into a topic (conv_say):
    which to-do the owner attached. None for no to-do."""
    if not isinstance(todo, dict) or not str(todo.get("title") or "").strip():
        return None
    key = clip(" ".join(str(todo.get("key") or "").split()), 40)
    title, why = clip(" ".join(str(todo["title"]).split()), 300), clip(str(todo.get("why") or "").strip(), 1000)
    verb = clip(" ".join(str(todo.get("verb") or "").split()), 20)
    line = f"Sent from to-do {key}: {verb + ' ' if verb else ''}{title}".strip()
    note = (f"The owner sent the next message with this to-do attached on Today, so it is about it unless it says "
            f"otherwise: {key or 'to-do'} ({verb or 'to-do'}) “{title}”." + (f" Why: {why}" if why else ""))
    return line, note


def conv_say(project, text, todo=None):
    """POST /api/conversation/say: send text into the current topic, the tab you see, or start the pending one
    (README: TODAY-2, which topic a message goes to). A pending topic with a subject (New tab ↗, a panel's Ask
    Claude) is made with that subject and its note; one with a title (New tab ↗) gets that title. A topic a mode
    switch started carries the handoff of the one before it to Claude, and a line saying so in the chat.
    todo: the to-do attached to this message (the 📎 chip), {key, verb, title, why}, or None. It never moves the
    message to another topic. The topic gets a line naming the to-do, with its title and why as a note for Claude,
    once, and the to-do is bound to its topic for good (todo_bond)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("say something first")
    proj(project)
    tops = topics(project)
    c = conv_state(project, tops)
    todo = todo if isinstance(todo, dict) else None
    ids = {t["id"]: t for t in tops}
    cur, left = ids.get(c["topic"]), ids.get(c.get("from"))
    told = todo_line(todo)
    if cur and cur["ended"]:  # a message into an ended topic reopens it, as it does an ended chat
        topic_reopen(project, cur["id"])
    if cur and cur["mode"] != "research":
        if told and not q("SELECT 1 FROM design_msgs WHERE design_id=? AND role='ops' AND text=?",
                          (cur["id"], told[0]), one=True):
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (cur["id"], "ops", told[0], time.time(), told[1]))
        if told:
            todo_bond(str(todo.get("key") or ""), cur["id"])
        return {"topic": cur["id"], "queued": design_say(cur["id"], text)}
    handoff = topic_handoff(left, c["mode"]) if left and not cur else ""
    if c["mode"] == "research":
        links = research_links(project)
        if not links:
            raise ValueError(f"research is off, or no research project is linked to {project}")
        body = {"message": text + "".join(f"\n\n{x}" for x in (handoff, told and told[1]) if x)}
        body.update({"thread": cur["id"][2:], "target": cur["target"]} if cur else {"target": f"project:{links[0]['id']}"})
        code, r = research.post("/chat", body)
        if code != 200:
            raise ValueError(r.get("error") or f"research said {code}")
        new = f"r:{r['thread']}"
    else:
        now = time.time()
        about = subject_about(project, c["subject"]) if c.get("subject") and c["mode"] == "design" else None
        new = q("INSERT INTO designs (project, title, created, updated, kind, subject) VALUES (?,?,?,?,?,?)",
                (project, (about["title"] if about else c.get("title") or title_of(text))[:80], now, now, c["mode"],
                 c.get("subject") if about else None))
        if about:  # the same mechanism as a handoff: the note goes to Claude with the first message
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (new, "ops", about["line"], now, about["note"]))
        if told:
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (new, "ops", told[0], now, told[1]))
        if handoff:  # the note goes to Claude with the first message (news); the line is what you see
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (new, "ops", f"Switched from {MODES[left['mode']]} to {MODES[c['mode']]}: this topic starts with a "
                           f"short handoff of “{left['title']}”.", now, handoff))
        design_say(new, text)
    set_setting(f"conv:{project}", json.dumps({"topic": new, "mode": c["mode"]}))
    if told:
        todo_bond(str(todo.get("key") or ""), new)
    return {"topic": new, "queued": False}


def topic_from_reply(reply, did):
    """Retitle the chat from the reply's ```topic block, {"title": "..."}. Returns (reply without it, note)."""
    note = ""
    for block in TOPIC_BLOCK.findall(reply):
        try:
            it = json.loads(block)
        except ValueError as e:
            note = f"your last topic block was not valid JSON ({e}); the title is unchanged"
            continue
        title = " ".join(str(it.get("title") or "").split())[:80] if isinstance(it, dict) else ""
        if not title:
            note = 'your last topic block had no title; write {"title": "..."}'
            continue
        q("UPDATE designs SET title=? WHERE id=?", (title, did))
        note = f"this topic is now titled “{title}”"
    return TOPIC_BLOCK.sub("", reply).strip(), note


def mcp_servers(p):
    """The MCP servers a chat with a stopped task of p loads: the chat_mcp names, defined in your local, project
    (.mcp.json) or user scope, in Claude's order. Returns (the --mcp-config JSON, names not found). claude.ai connectors can't be
    listed: --strict-mcp-config drops them and only claude.ai can define them."""
    found = {}
    try:
        cfg = json.loads((HOME / ".claude.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    try:
        shared = json.loads((p.repo / ".mcp.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        shared = {}
    for scope in ((cfg.get("projects") or {}).get(str(p.repo), {}).get("mcpServers") or {}, shared,
                  cfg.get("mcpServers") or {}):
        for name in p.chat_mcp:
            if name not in found and isinstance(scope.get(name), dict):
                found[name] = scope[name]
    return json.dumps({"mcpServers": found}), [n for n in p.chat_mcp if n not in found]


# A project chat runs like the CLI: auto mode, and what would still prompt goes to you. claude -p hands each such
# prompt to its --permission-prompt-tool, an MCP tool; the daemon serves it over HTTP (POST /mcp/permit/<chat id>)
# and holds the call until you click Approve or Deny in the chat, or the turn ends. The tool gets {tool_name, input,
# tool_use_id} and answers {"behavior": "allow", "updatedInput": ...} or {"behavior": "deny", "message": ...}.

PERMIT_SERVER = "claude-ops"
PERMIT_TOOL = f"mcp__{PERMIT_SERVER}__permit"
PERMIT_WAIT = 3600  # seconds a prompt waits for you before it counts as denied
PERMITS = {}  # request id: {id, did, tool, input, ts, done (an Event), allow (None until answered), why}
PERMIT_LOCK = threading.Lock()
DENIED = "you clicked Deny"
PERMIT_SPEC = {"name": "permit", "description": "Ask the owner, on the claude-ops dashboard, to allow a tool call.",
               "inputSchema": {"type": "object", "required": ["tool_name", "input"], "properties": {
                   "tool_name": {"type": "string"}, "input": {"type": "object"}, "tool_use_id": {"type": "string"}}}}


def permit_mcp(did):
    """The --mcp-config JSON that gives chat did the daemon's permission-prompt server."""
    return json.dumps({"mcpServers": {PERMIT_SERVER: {"type": "http", "url": f"{DAEMON_URL}/mcp/permit/{did}",
                                                      "headers": {"X-Ops": "1"}}}})


def permit_rpc(did, m):
    """Chat did's permission server: the JSON-RPC response to message m, or None for a notification."""
    if not isinstance(m, dict) or "id" not in m:
        return None
    meth, params = m.get("method"), m.get("params") or {}
    if meth == "initialize":
        r = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
             "serverInfo": {"name": PERMIT_SERVER, "version": "1"}}
    elif meth == "tools/list":
        r = {"tools": [PERMIT_SPEC]}
    elif meth == "tools/call" and params.get("name") == "permit":
        a = params.get("arguments") or {}
        r = {"content": [{"type": "text", "text": json.dumps(permit_ask(did, str(a.get("tool_name") or "?"),
                                                                        a.get("input") or {}))}]}
    elif meth == "ping":
        r = {}
    else:
        return {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": f"no method {meth}"}}
    return {"jsonrpc": "2.0", "id": m["id"], "result": r}


def permit_ask(did, tool, inp, wait=PERMIT_WAIT):
    """Hold one permission prompt of chat did until you answer it, the turn ends or wait runs out: the tool's
    answer. The chat view lists it (permits_of) with Approve and Deny."""
    req = {"id": secrets.token_hex(6), "did": did, "tool": tool, "input": inp, "ts": time.time(),
           "done": threading.Event(), "allow": None, "why": f"no answer in {wait // 60} min"}
    with PERMIT_LOCK:
        PERMITS[req["id"]] = req
    state_changed()
    try:
        req["done"].wait(wait)
    finally:
        with PERMIT_LOCK:
            PERMITS.pop(req["id"], None)
        state_changed()
    word = "allowed" if req["allow"] else f"denied ({req['why']})"
    if did in DESIGN_LIVE:
        DESIGN_LIVE[did].append(f"{tool}: {word} on the dashboard")
    if req["allow"]:
        return {"behavior": "allow", "updatedInput": inp}
    said = "The owner denied it" if req["why"] == DENIED else f"Not allowed: {req['why']}"
    return {"behavior": "deny", "message": f"{said} on the claude-ops dashboard. Don't try it another way: say what "
                                           "you wanted to do and why."}


def permit_answer(did, rid, allow):
    """Approve or Deny in the chat view: answer chat did's prompt rid."""
    with PERMIT_LOCK:
        req = PERMITS.get(rid)
        if not req or req["did"] != did or req["done"].is_set():
            raise ValueError("that permission request is gone: the turn ended or it was already answered")
        req["allow"], req["why"] = bool(allow), DENIED
        req["done"].set()
    return {"ok": True}


def permits_drop(did, why):
    """Deny chat did's open prompts: its turn ended, so nothing waits for them."""
    with PERMIT_LOCK:
        for req in PERMITS.values():
            if req["did"] == did and not req["done"].is_set():
                req["allow"], req["why"] = False, why
                req["done"].set()


def permits_of(did):
    """Chat did's open permission prompts, oldest first, for the chat view."""
    with PERMIT_LOCK:
        return [{"id": r["id"], "tool": r["tool"], "input": r["input"], "ts": r["ts"]}
                for r in sorted(PERMITS.values(), key=lambda r: r["ts"]) if r["did"] == did and not r["done"].is_set()]


def design_turn(did, text, fresh_why=None):
    """One chat turn. fresh_why: start a fresh session, with a handoff, for that reason (its old one is gone)."""
    d = design(did)
    orig = text
    p = proj(d["project"])
    DESIGN_LIVE[did] = []
    chat = d["kind"] == "project"
    cwd, tools, extra, before = p.repo if chat else p.dev_wt, DESIGN_TOOLS, "", None
    if spec_chat(d):  # its own worktree of dev, where Claude may edit openspec/
        try:
            if not d["spec"]:  # drafting: which change folder this turn creates, if any
                before = spec_names(spec_part(p, d["spec_sub"]).at(p.dev_wt))
            cwd, extra = spec_sync(p, d)
        except Exception as e:
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
              (did, "assistant", f"(chat error: could not prepare the change's worktree: {e})", time.time()))
            DESIGN_LIVE.pop(did, None)
            q("UPDATE designs SET state='idle', updated=? WHERE id=?", (time.time(), did))
            send_queued(did)
            return
        tools += spec_tools(cwd)
    # any shell command the owner could run at the CLI (git, gh, opsd.py send, …), past the guard (the owner's
    # choice, task #254); the prompt keeps it off the project's files, which change only through tasks
    tools += ",Bash"
    if not chat:  # its own documents folder: the only place outside openspec/ it may write
        files = CHAT_FILES / str(did)
        files.mkdir(parents=True, exist_ok=True)
        tools += f",Edit(/{files}/**)"  # Edit rules cover Write too
        if not spec_chat(d):  # the owner's checkout, only when they ask for a change made there (design_prompt.md)
            tools += f",Edit(/{p.repo}/**)"
    kept, handoff = None, ""
    # cheaper to start over than to resume (chat_stale), or nothing to resume (fresh_why): a handoff note, then this turn
    if why := ((fresh_why, None) if fresh_why else chat_stale(d)):
        kept, handoff = session_model(d, d["kind"]), chat_handoff(did, why[0])  # the fresh one keeps its model
        q("UPDATE designs SET session_id=NULL, last_call=NULL, ctx_tokens=NULL WHERE id=?", (did,))
        if why[1]:
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
              (did, "ops", f"Started a fresh session: {why[1]}, and every reply rereads all of it. The new session "
                           "gets a copy of this conversation so far (the oldest messages cut if it is long).",
               time.time()))
        d = design(did)
    slash = text.strip().startswith("/")  # the CLI runs a slash command only from the start of the prompt
    if handoff and not slash:
        text = handoff + "\n\n## Now\n\n" + text
    env = dict(os.environ, OPS_FROM=f"chat:{did}", OPS_DAEMON=DAEMON_URL)
    if chat:  # like the CLI: your own settings, MCP servers and Claude in Chrome; auto mode, and prompts come to you
        said = chat_said(did, orig)  # word from a project it sent something to; a design chat gets it in ops_note
        system = PROJECT_CHAT_PROMPT + "\n\n" + peer_brief(p, "chat")
        system += (f"\n\nTo queue a task in {p.name} itself, for an unattended worker in its own worktree, run "
                   f"`{SEND_CMD} {p.name} --title \"...\" \"<a brief that stands alone>\"`. Do it when the owner "
                   "asks or agrees.")
        system += CHAT_REF_RULE
        if handoff and slash:
            system += "\n\n" + handoff
        cmd = [CLAUDE, "-p", ("[ops: " + " | ".join(said) + "]\n\n" if said else "") + with_images(text),
               "--output-format", "stream-json", "--verbose",
               "--permission-mode", "auto", "--permission-prompt-tool", PERMIT_TOOL, "--mcp-config", permit_mcp(did),
               *PLUGIN_ARGS, "--name", f"{p.name} chat #{did}", "--settings", guard_settings(None, p, chat=True, guard=False),
               "--append-system-prompt", system]
        env["MCP_TOOL_TIMEOUT"] = str((PERMIT_WAIT + 60) * 1000)  # a prompt waits for you, not the 60 s default
    else:
        note, system = ops_note(p, did, extra, orig), design_system_prompt(p, d, cwd)
        if slash:
            system = system.rstrip() + "\n\n## Dashboard note for this turn\n\n" + note.strip()
            system += "\n\n" + handoff if handoff else ""
            text, note = text.strip(), ""
        cmd = [CLAUDE, "-p", note + with_images(text), "--output-format", "stream-json",
               "--verbose", "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", tools,
               "--strict-mcp-config", "--no-chrome", *PLUGIN_ARGS, "--name", f"{p.name} design #{did}",
               "--settings", guard_settings(None),
               "--append-system-prompt", system]
        cmd += ["--add-dir", str(CHAT_FILES / str(did))]
        if not spec_chat(d):
            cmd += ["--add-dir", str(p.repo)]
        if spec_chat(d):  # deny rules win over the allowed-tools a command's own frontmatter grants
            cmd += ["--disallowedTools", SPEC_DENY]
    cmd += ["--add-dir", str(IMAGES)]
    if d["session_id"]:
        cmd += ["--resume", d["session_id"]]
    else:  # a new session: what it starts on, until its init event names the model exactly
        q("UPDATE designs SET session_model=? WHERE id=?", (kept or session_model(d, d["kind"]), did))
    cmd += model_args(kept or session_model(d, d["kind"])) + effort_args(effort_for())

    def on_session(sid):
        if sid != design(did)["session_id"]:
            q("UPDATE designs SET session_id=? WHERE id=?", (sid, did))

    def on_model(m):
        q("UPDATE designs SET session_model=? WHERE id=?", (m, did))

    cost, retry = 0.0, False
    try:
        reply, cost = stream_claude(cmd, cwd, RUNS / f"design-{did}", DESIGN_LIVE[did], DESIGN_PROCS, did,
                                    on_session, env=env, on_model=on_model, on_call=lambda ctx: q(
                                        "UPDATE designs SET last_call=?, ctx_tokens=? WHERE id=?",
                                        (time.time(), ctx, did)))
        if STOPPING.is_set():  # the restart stopped it: the chat stays replying, and resume_chats picks it up
            return
        if reply.startswith("(no reply;") and LOST_SESSION in reply and d["session_id"] and not fresh_why \
                and not design(did)["ended"]:  # an ended chat doesn't start a fresh session
            retry = True  # Claude has no file for the session: start a fresh one rather than make you resend
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
              (did, "ops", "Claude could not find this chat's session, so it starts a fresh one with a copy of the "
                           "conversation so far, and answers your message there.", time.time()))
            return
        if design(did)["ended"] and reply.startswith("(no reply;"):  # End stopped it; say so, not the exit code
            reply = "You ended the chat; the reply was stopped."
        attached = spec_attach(p, d, before) if before is not None else None  # (text, note) or None
        saved = spec_land(p, design(did)) if d["spec"] or attached else ""  # before any task it queues starts from dev
        reply, ids, note = (reply, [], "") if chat else queue_from_reply(p, reply, did)
        reply, todo_note = todos_from_reply(p, reply, did, ids)
        reply, snote = schedules_from_reply(p, reply, did)  # design and project chats alike
        reply, tnote = topic_from_reply(reply, did)
        note = "; ".join(x for x in (note, todo_note, snote, tnote) if x)
        first = not q("SELECT id FROM design_msgs WHERE design_id=? AND role='assistant' LIMIT 1", (did,))
        q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
          (did, "assistant", reply, time.time(), json.dumps(ids), note))
        if first and d["origin"]:  # a peer's question: its answer goes back, as a reply to a message would
            tell_origin(json.loads(d["origin"]), f"{origin_words(origin_of(f'chat:{did}'))}, which you started, "
                                                 f"replied:\n\n{gist(reply, 1500)}")
        if saved or attached:
            text, note = attached or ("", "")
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (did, "ops", " ".join(x for x in (text, saved) if x), time.time(),
               "; ".join(x for x in (note, saved and "your last reply's edits: " + saved) if x)))
    except Exception as e:
        if not STOPPING.is_set():
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
              (did, "assistant", f"(chat error: {e})", time.time()))
    finally:
        permits_drop(did, "the reply ended first")
        DESIGN_PROCS.pop(did, None)
        DESIGN_LIVE.pop(did, None)
        # A chat ended mid-reply stays read: what the stopped turn wrote last isn't a new reply when you reopen it.
        # It stays replying when a restart stopped it (resume_chats) or it goes again in a fresh session (retry).
        now = time.time()
        q("UPDATE designs SET state=CASE WHEN ? THEN state ELSE 'idle' END, updated=?, cost=cost+?, "
          "seen=CASE WHEN ended IS NULL THEN seen ELSE ? END WHERE id=?",
          (STOPPING.is_set() or retry, now, cost, now, did))
        if retry:  # here, as the turn's early returns skip anything after the finally
            design_turn(did, orig, fresh_why="Claude could not find its session")
        elif not STOPPING.is_set():
            send_queued(did)


LOST_SESSION = "No conversation found with session ID"  # claude's error when --resume names a session it has no file for
RESUME_NOTE = ("[ops: claude-ops restarted while you were replying to the message below, which stopped your reply and "
               "any command it was running. Check what you had already done, then pick up where you left off and "
               "answer it. The owner did not resend it.]\n\n")
RESTARTED_NOTE = ("[ops: claude-ops restarted because you ran `opsd.py restart`, which stopped your reply. The restart "
                  "is done and this is the new daemon: don't run it again. Finish answering the message below. The "
                  "owner did not resend it.]\n\n")


def resumed(text, mine=False):
    """The message of a turn a restart cut off, to send again: with a note, unless it is a slash command, which the
    CLI runs only from the start of the prompt. mine: the chat itself ran opsd.py restart."""
    return text if text.strip().startswith("/") else (RESTARTED_NOTE if mine else RESUME_NOTE) + text


def resume_chats():
    """On start: answer the messages the last daemon was replying to when it stopped (a release restarts it), so
    you don't have to send them again. Chats it left replying, and the task chats shutdown listed in cut_task_chats."""
    try:
        by = json.loads(setting("restarted_by") or "null")  # the chat that ran opsd.py restart, if one did
    except ValueError:
        by = None
    set_setting("restarted_by", "null")
    for d in q("SELECT id, project, ended FROM designs WHERE state='thinking'"):
        # the turn's message: the user's messages since the last answer (queued ones go as one turn)
        msgs = q("SELECT text FROM design_msgs WHERE design_id=? AND role='user' AND id>(SELECT COALESCE(MAX(id),0) "
                 "FROM design_msgs WHERE design_id=? AND role!='user') ORDER BY id", (d["id"], d["id"])) or \
            q("SELECT text FROM design_msgs WHERE design_id=? AND role='user' ORDER BY id DESC LIMIT 1", (d["id"],))
        if d["ended"] or not msgs or d["project"] not in PROJECTS:
            q("UPDATE designs SET state='idle' WHERE id=?", (d["id"],))
            continue
        mine = by == d["id"]
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
          (d["id"], "ops", ("Claude restarted claude-ops (opsd.py restart)" if mine else
                            "claude-ops restarted while Claude was replying") + ", which stopped the reply. Claude is "
                           "picking it up again; you don't need to resend your message.", time.time()))
        text = "\n\n".join(m["text"] for m in msgs)
        threading.Thread(target=design_turn, args=(d["id"], resumed(text, mine)), daemon=True).start()
    for d in q("SELECT id, project FROM designs WHERE state!='thinking' AND ended IS NULL AND queued NOT IN ('[]', '')"):
        if d["project"] in PROJECTS:
            send_queued(d["id"])  # stopped between a reply and the queued messages after it
    try:
        cut = json.loads(setting("cut_task_chats") or "[]")
    except ValueError:
        cut = []
    set_setting("cut_task_chats", "[]")
    for tid in cut:
        t = task(tid)
        m = q("SELECT role, text FROM task_msgs WHERE task_id=? AND role IN ('user','assistant') ORDER BY id DESC "
              "LIMIT 1", (tid,), one=True)
        if not t or not t["session_id"] or t["state"] not in CHAT_STATES or not m or m["role"] != "user":
            continue
        TASK_CHAT_LIVE[tid] = []  # as task_ask: the dashboard shows it replying, and no worker starts meanwhile
        threading.Thread(target=task_chat_turn, args=(tid, resumed(m["text"])), daemon=True).start()


def queue_from_reply(p, reply, did):
    """Queue every task in the reply's ```queue blocks. Returns (reply without blocks, ids, note for Claude)."""
    ids, notes = [], []
    for block in QUEUE_BLOCK.findall(reply):
        try:
            items = json.loads(block)
            items = items if isinstance(items, list) else [items]
        except ValueError as e:
            notes.append(f"your last queue block was not valid JSON ({e}); nothing from it was queued")
            continue
        local = []
        for it in items:
            after = it.get("after")
            dep = local[after] if isinstance(after, int) and 0 <= after < len(local) else None
            if it.get("after_task") and str(it["after_task"]).lstrip("#").isdigit():
                dep = int(str(it["after_task"]).lstrip("#"))
            try:
                model = model_name(it.get("model"))
            except ValueError as e:  # queue it anyway, on its kind's model
                model = None
                notes.append(f"{e}; that task starts on the task model setting")
            try:
                effort = effort_name(it.get("effort"))
            except ValueError as e:  # queue it anyway, at the effort setting
                effort = None
                notes.append(f"{e}; that task runs at the effort setting")
            nb = it.get("not_before")
            if nb not in (None, ""):
                try:
                    start_time(nb)
                except ValueError as e:  # queue it anyway, to start as soon as it can
                    nb = None
                    notes.append(f"{e}; that task starts as soon as the queue can")
            jira, target = it.get("jira"), it.get("branch")
            if jira or target:
                try:
                    branch_name(p, it.get("title") or title_of(it.get("body") or ""), jira, target)
                except ValueError as e:  # queue it anyway, as an ordinary task that merges into dev
                    jira = target = None
                    notes.append(f"{e}; that task merges into {p.dev} as usual")
            tid = add_task(p.name, it.get("body") or it.get("title", ""), it.get("title", ""),
                           priority=int(it.get("priority") or 0), hold=bool(it.get("hold")), depends_on=dep,
                           auto_release=None if it.get("release") is None else bool(it.get("release")),
                           model=model, effort=effort, not_before=nb, jira=jira, target=target)
            ev(tid, f"queued from design chat #{did}")
            local.append(tid)
        ids += local
    if ids:
        notes.append("your last reply queued " + ", ".join(f"#{i}" for i in ids))
    return QUEUE_BLOCK.sub("", reply).strip(), ids, "; ".join(notes)


# ---------------------------------------------------------------- OpenSpec changes
# A design chat can be about one OpenSpec change, openspec/changes/<name>/ in a repo of the project. It runs in its
# own detached worktree of dev (<worktrees>/spec-<chat id>), where Claude may edit openspec/ and nothing else.
# After each reply the daemon commits those edits and fast-forwards dev to them, so the next worker sees them.
# The edits skip the gate: they are docs, and only files under openspec/ are ever committed.

SPEC_DIR = Path("openspec") / "changes"
SPEC_SMALL = 4  # a tasks.md phase with fewer open items is folded into its neighbour
CHECKBOX = re.compile(r"\s*[-*]\s+\[([ xX])\]\s+(.*)")

# A change's chat may run any openspec command but those that write outside openspec/ (init, update: a chat can't
# save them) or change this machine's OpenSpec setup. Deny rules also beat a slash command's own allowed-tools.
SPEC_DENY = ",".join(f"Bash(openspec {c}:*)" for c in (
    "init", "update", "feedback", "completion install", "completion uninstall", "config set", "config unset",
    "config reset", "config edit", "config profile", "store setup", "store register", "store unregister",
    "store remove", "workset create", "workset open", "workset remove"))
# The opsx commands `openspec init` installs in a repo, or the skills newer versions install instead
OPSX_SKILLS = {"explore": "openspec-explore", "sync": "openspec-sync-specs", "archive": "openspec-archive-change",
               "propose": "openspec-propose", "apply": "openspec-apply-change"}
SPEC_COMMANDS = (  # (id, label, tip): what the chat's buttons may send. Never apply or implement: those are tasks.
    ("validate", "Validate", "Checks the change's format with openspec validate --strict. Changes nothing."),
    ("explore", "Explore", "Thinks the change through with you: questions, options, risks. Edits only if you agree."),
    ("audit", "Audit", "Checks the change's files agree with each other and with the code. Reports; changes nothing."),
    ("review", "Review", "Several read-only expert agents review the proposal and report findings. Changes nothing."),
    ("deviation-check", "Deviation check", "Compares the plan with the code on dev and lists where they differ. "
                                           "Changes nothing."),
    ("sync", "Sync specs", "Merges the change's spec deltas into openspec/specs/. The change stays open; the edits "
                           "are saved to dev."),
    ("archive", "Archive", "Merges the spec deltas into openspec/specs/ and moves the change to "
                           "openspec/changes/archive/. Saved to dev; do it once the work is merged."))


def spec_tools(cwd):
    """What a change's chat may use on top of a design chat's tools: skills, subagents, openspec, and edits under
    openspec/ only. Subagents get the same rules. No mkdir or mv: an archive moves the change with openspec
    archive, which spec_prompt.md points it to."""
    return f",Skill,Agent,Edit(/{cwd}/openspec/**),Bash(openspec:*)"


def opsx_invocation(roots, cid):
    """How to run opsx's `cid` in a repo: /opsx:<cid> if a root has the command, /<skill> if it has the skill
    newer OpenSpec versions install instead, else None. roots: the repo's folders to look in."""
    for root in roots:
        if (root / ".claude" / "commands" / "opsx" / f"{cid}.md").is_file():
            return f"/opsx:{cid}"
    for root in roots:
        if cid in OPSX_SKILLS and (root / ".claude" / "skills" / OPSX_SKILLS[cid] / "SKILL.md").is_file():
            return f"/{OPSX_SKILLS[cid]}"
    return None


def spec_propose(p, sub=""):
    """The slash command that proposes a new change in this repo's dev, or None if opsx isn't installed there."""
    return opsx_invocation([spec_part(p, sub).at(p.dev_wt)], "propose")


def spec_commands(roots, name):
    """The commands a change's chat can run for it, as {id, label, text, tip}; text is the exact message to send."""
    out = []
    for cid, label, tip in SPEC_COMMANDS:
        if cid == "validate":
            text = shutil.which("openspec") and f"Run `openspec validate {name} --strict` and report the result."
        elif cid in OPSX_SKILLS:
            text = (opsx_invocation(roots, cid) or "") and f"{opsx_invocation(roots, cid)} {name}"
        else:
            text = f"/claude-ops:openspec:{cid} {name}"  # the plugin's own: always there
        if text:
            out.append({"id": cid, "label": label, "text": text, "tip": tip})
    return out


def spec_archived(root, name):
    """Where the change went if it was archived (openspec/changes/archive/<date>-<name>), relative to root."""
    d = root / SPEC_DIR / "archive"
    hits = sorted(f for f in d.iterdir() if f.is_dir() and re.fullmatch(
        rf"(\d{{4}}-\d{{2}}-\d{{2}}-)?{re.escape(name)}", f.name)) if d.is_dir() else []
    return str(hits[-1].relative_to(root)) if hits else None


def spec_part(p, sub):
    r = next((r for r in p.parts if r.sub == (sub or "")), None)
    if not r:
        raise ValueError(f"{p.name} has no repo {sub!r}")
    return r


def spec_phases(text):
    """tasks.md as phases: its ## sections, each with its open items and how many are done."""
    phases = []
    for line in text.splitlines():
        m = re.match(r"##\s+(.+)", line)
        if m:
            phases.append({"title": m.group(1).strip(), "open": [], "done": 0})
            continue
        m = CHECKBOX.match(line)
        if m:
            if not phases:
                phases.append({"title": "", "open": [], "done": 0})
            if m.group(1) == " ":
                phases[-1]["open"].append(m.group(2).strip())
            else:
                phases[-1]["done"] += 1
    return [ph for ph in phases if ph["open"] or ph["done"]]


def spec_groups(phases):
    """What one task per phase would queue: the phases with open items, small ones folded into a neighbour
    (a "Tests" or "Docs" phase of two items is no task of its own)."""
    groups = []
    for ph in phases:
        if not ph["open"]:
            continue
        if groups and (len(ph["open"]) < SPEC_SMALL or len(groups[-1]["open"]) < SPEC_SMALL):
            groups[-1]["titles"].append(ph["title"])
            groups[-1]["open"] += ph["open"]
        else:
            groups.append({"titles": [ph["title"]], "open": list(ph["open"])})
    return groups


def spec_info(folder):
    """A change folder for the dashboard: title, why, progress, and the advice on how to queue it."""
    def text(f):
        return (folder / f).read_text(errors="replace") if (folder / f).is_file() else ""
    prop = text("proposal.md")
    m = re.search(r"^#\s+(.+)", prop, re.M)
    title = re.sub(r"^(change|proposal)\s*:\s*", "", m.group(1).strip(), flags=re.I) if m else folder.name
    why = re.search(r"^##\s+Why\s*\n+(.+?)(?:\n\s*\n|\n#|\Z)", prop, re.M | re.S)
    phases = spec_phases(text("tasks.md"))
    groups = spec_groups(phases)
    n = sum(len(g["open"]) for g in groups)
    return {"name": folder.name, "title": title, "why": first_words(why.group(1), 200) if why else "",
            "open": n, "done": sum(ph["done"] for ph in phases), "has_tasks": bool(text("tasks.md")),
            "groups": [{"title": " + ".join(t for t in g["titles"] if t) or "tasks", "open": len(g["open"])}
                       for g in groups],
            "advice": "whole"}  # one task, whatever the size: split only to check or release a phase first


def spec_changes(p):
    """The project's open OpenSpec changes: those on dev, then those only in your checkout."""
    out, chats = [], {(c["spec_sub"] or "", c["spec"]): c["id"] for c in q(
        "SELECT id,spec,spec_sub FROM designs WHERE project=? AND spec IS NOT NULL ORDER BY id", (p.name,))}
    for r in p.parts:
        seen = set()
        for where, root in (("dev", r.at(p.dev_wt)), ("checkout", r.src)):
            d = root / SPEC_DIR
            for c in sorted(d.iterdir()) if d.is_dir() else []:
                if c.name in seen or c.name == "archive" or not c.is_dir() or not any(
                        (c / f).is_file() for f in ("proposal.md", "design.md", "tasks.md")):
                    continue
                seen.add(c.name)
                out.append(dict(spec_info(c), project=p.name, sub=r.sub, where=where,
                                chat=chats.get((r.sub, c.name))))
    return out


def spec_wt(p, d):
    """The chat's worktree of the change's repo, made on first use, detached at dev."""
    r = spec_part(p, d["spec_sub"])
    path = r.at(p.wt / f"spec-{d['id']}")
    if not path.exists():
        r.git("worktree", "add", "--detach", str(path), p.dev)
    return r, path


def spec_commit(r, path, msg):
    """Commit what changed under openspec/ in the chat's worktree. True if there was something."""
    r.git("add", "-A", "--", "openspec", cwd=path, check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path).returncode == 0:
        return False
    r.git("commit", "-q", "--no-verify", "-m", msg, cwd=path)
    return True


def spec_rebase(p, r, path, did):
    """Put the chat's commits on top of dev. On a conflict the chat's side of each clash wins; if git still
    can't replay them, they are saved as a patch and the worktree starts again from dev. Returns that news."""
    res = subprocess.run(["git", "rebase", "-q", "-X", "theirs", p.dev], cwd=path, capture_output=True, text=True)
    if not res.returncode:
        return ""
    subprocess.run(["git", "rebase", "--abort"], cwd=path, capture_output=True)
    patch = RUNS / f"design-{did}" / f"set-aside-{time.strftime('%Y%m%d-%H%M%S')}.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(r.git("format-patch", "--stdout", f"{p.dev}..HEAD", cwd=path, check=False))
    r.git("checkout", "-q", "--detach", p.dev, cwd=path)
    return f"edits that clash with {p.dev} could not be saved; they are set aside in {patch}"


def spec_sync(p, d):
    """Before a turn: keep what a cut-off turn left under openspec/, drop anything else, catch up with dev.
    Returns (the worktree, news for Claude)."""
    r, path = spec_wt(p, d)
    spec_commit(r, path, spec_msg(d))
    r.git("reset", "-q", "--hard", cwd=path)
    r.git("clean", "-fdq", cwd=path)
    return path, spec_rebase(p, r, path, d["id"])


def spec_land(p, d, msg=None):
    """After a turn: commit the chat's edits under openspec/ and fast-forward dev to them.
    Returns a line for the owner, or "" when nothing changed."""
    try:
        r, path = spec_wt(p, d)
        spec_commit(r, path, msg or spec_msg(d))
        with p.lock:  # merges into dev hold it too
            lost = spec_rebase(p, r, path, d["id"])
            files = r.git("diff", "--name-only", f"{p.dev}..HEAD", cwd=path).splitlines()
            if not files:
                return lost.capitalize() + "." if lost else ""
            res = subprocess.run(["git", "merge", "--ff-only", "-q", r.git("rev-parse", "HEAD", cwd=path)],
                                 cwd=r.at(p.dev_wt), capture_output=True, text=True)
            if res.returncode:
                return f"Couldn't save the edits to {p.dev}: {(res.stderr or res.stdout).strip()[-300:]}"
        pre = f"{SPEC_DIR / d['spec']}/" if d["spec"] else f"{SPEC_DIR}/"
        names = [f[len(pre):] if f.startswith(pre) else f for f in files]
        return (f"Saved to {p.dev} at {r.rev(p.dev)[:8]}: {', '.join(names[:6])}{' …' if len(names) > 6 else ''}."
                + (f" {lost.capitalize()}." if lost else ""))
    except Exception as e:
        return f"Couldn't save the edits to {p.dev}: {e}"


def spec_chat(d):
    """Whether a design chat works on OpenSpec: about a change, or drafting a new one (no change yet)."""
    return bool(d["spec"] or d["spec_draft"])


def spec_msg(d):
    """The commit message for a chat's edits; a drafting chat has no change name yet."""
    what = d["spec"] or "new change"
    return f"OpenSpec {what}: edits from design chat #{d['id']}"


def spec_names(root):
    """The open change folders in a repo's openspec/changes/ (not archive/)."""
    d = root / SPEC_DIR
    return {c.name for c in d.iterdir() if c.is_dir() and c.name != "archive"} if d.is_dir() else set()


def spec_repos(p):
    """The repos of p that use OpenSpec (have openspec/ on dev), and whether each has a propose command."""
    return [{"project": p.name, "sub": r.sub, "propose": spec_propose(p, r.sub)} for r in p.parts
            if (r.at(p.dev_wt) / "openspec").is_dir()]


def spec_bare(p):
    """The repos of p that don't use OpenSpec yet (no openspec/ on dev), for the dashboard's Set up lines."""
    return [{"project": p.name, "sub": r.sub} for r in p.parts
            if r.at(p.dev_wt).is_dir() and not (r.at(p.dev_wt) / "openspec").exists()]


# Set up and Refresh commands: the daemon runs openspec init or update, which a chat can't (they write into
# .claude/), in a throwaway worktree of dev, and fast-forwards dev to a commit of openspec/ and .claude/ only.
# Like a chat's edits these skip the gate: they are docs and command files.
OPENSPEC_RUN = {"init": (["openspec", "init", "--tools", "claude", "--no-animation", "--no-copilot-cloud"],
                         "OpenSpec: set up (dashboard)", "Set up"),
                "update": (["openspec", "update"], "OpenSpec: refresh commands (dashboard)", "Refreshed")}
OPENSPEC_KEEP = ("openspec", ".claude")  # the only folders whose changes are committed
OPENSPEC_BUSY = set()  # (project, sub) being set up or refreshed now
OPENSPEC_LOCK = threading.Lock()  # guards OPENSPEC_BUSY


def openspec_run(p, sub, what):
    """Set up OpenSpec in a repo of p (what="init") or refresh its commands ("update"). Returns {text, sha,
    files, dropped}; raises ValueError when it is refused or the CLI fails."""
    r = spec_part(p, sub)
    argv, msg, done = OPENSPEC_RUN[what]
    where = f"{p.name}/{r.prefix[:-1]}" if r.prefix else p.name
    if not shutil.which("openspec"):
        raise ValueError("Install the openspec CLI: npm i -g @fission-ai/openspec")
    has = (r.at(p.dev_wt) / "openspec").exists()
    if what == "init" and has:
        raise ValueError(f"{where} already uses OpenSpec: {p.dev} has openspec/")
    if what == "update" and not has:
        raise ValueError(f"{where} doesn't use OpenSpec yet: {p.dev} has no openspec/. Set it up first")
    if q("SELECT id FROM tasks WHERE project=? AND state='merging'", (p.name,)):
        raise ValueError(f"a task of {p.name} is merging into {p.dev}: try again after the merge")
    with OPENSPEC_LOCK:
        if (p.name, r.sub) in OPENSPEC_BUSY:
            raise ValueError(f"OpenSpec is already being set up or refreshed in {where}")
        OPENSPEC_BUSY.add((p.name, r.sub))
    top = p.wt / f"openspec-{what}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{threading.get_ident() % 10000}"
    path = r.at(top)
    try:
        r.git("worktree", "add", "-q", "--detach", str(path), p.dev)
        if what == "init" and (path / "openspec").exists():  # dev moved since the check above
            raise ValueError(f"{where} already uses OpenSpec: {p.dev} has openspec/")
        try:
            res = subprocess.run(argv, cwd=path, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=120, env=dict(os.environ, CI="1", NO_COLOR="1"))
        except subprocess.TimeoutExpired:
            raise ValueError(f"openspec {what} took over 120 seconds and was stopped")
        if res.returncode:
            out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", (res.stdout or "") + (res.stderr or "")).strip()
            raise ValueError(f"openspec {what} failed: {out[-300:] or f'exit code {res.returncode}'}")
        status = subprocess.run(["git", "status", "--porcelain", "-z", "--ignored", "--untracked-files=all"],
                                cwd=path, capture_output=True, text=True).stdout  # not sh: it strips the codes
        dropped = [x[3:] for x in status.split("\0") if x[3:] and  # ignored ones are never committed
                   (x[:2] == "!!" or x[3:].split("/")[0] not in OPENSPEC_KEEP)]
        for keep in OPENSPEC_KEEP:  # one at a time: a folder that matches nothing would fail the others too
            r.git("add", "-A", "--", keep, cwd=path, check=False)
        files = r.git("diff", "--cached", "--name-only", cwd=path).splitlines()
        tail = (f" Not committed: {', '.join(dropped[:8])}{' …' if len(dropped) > 8 else ''}." if dropped else "")
        if not files:
            return {"text": "Nothing to do: the commands are up to date." + tail if what == "update" else
                    "Nothing to do: openspec init changed nothing under openspec/ or .claude/." + tail,
                    "sha": None, "files": 0, "dropped": dropped}
        r.git("commit", "-q", "--no-verify", "-m", msg, cwd=path)
        r.git("reset", "-q", "--hard", cwd=path)  # drop the rest, or the rebase below refuses to start
        r.git("clean", "-fdqx", cwd=path)
        with p.lock:  # merges into dev hold it too
            res = subprocess.run(["git", "rebase", "-q", p.dev], cwd=path, capture_output=True, text=True)
            if res.returncode:
                subprocess.run(["git", "rebase", "--abort"], cwd=path, capture_output=True)
                raise ValueError(f"{p.dev} moved and the commit no longer applies: "
                                 f"{(res.stderr or res.stdout).strip()[-300:]}")
            sha = r.git("rev-parse", "HEAD", cwd=path)
            res = subprocess.run(["git", "merge", "--ff-only", "-q", sha], cwd=r.at(p.dev_wt),
                                 capture_output=True, text=True)
            if res.returncode:
                raise ValueError(f"couldn't fast-forward {p.dev}: {(res.stderr or res.stdout).strip()[-300:]}")
        n = len(files)
        print(f"project {p.name} openspec {what}{f' ({r.name})' if r.sub else ''}: {sha[:8]}, {n} files", flush=True)
        return {"text": f"{done}: committed {n} file{'s' if n != 1 else ''} to {p.dev} at {sha[:8]}." + tail,
                "sha": sha, "files": n, "dropped": dropped}
    finally:
        r.git("worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(top, ignore_errors=True)
        r.git("worktree", "prune", check=False)
        with OPENSPEC_LOCK:
            OPENSPEC_BUSY.discard((p.name, r.sub))


def spec_first(p, sub, text):
    """The first message of a chat that drafts a new change: opsx's propose if the repo has it, else the steps."""
    propose = spec_propose(p, sub)
    if propose:
        return f"{propose} {text}"
    return (f"Create a new OpenSpec change for this: {text}\n\n"
            f"1. Pick a short kebab-case name for it and run `openspec new change <name>`.\n"
            f"2. Run `openspec status --change <name> --json` to see its artifacts. Write each one in order, "
            f"following `openspec instructions <artifact> --change <name> --json`.\n"
            f"3. Run `openspec validate <name> --strict` and fix anything it reports.")


def spec_new(p, sub, text):
    """Start a design chat that drafts a new change in a repo of p, with no change attached yet. It works in its
    own worktree like a change's chat; spec_attach ties it to the change its first folder becomes."""
    r = spec_part(p, sub)
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("describe the change")
    if not shutil.which("openspec"):
        raise ValueError("Install the openspec CLI: npm i -g @fission-ai/openspec")
    if not (r.at(p.dev_wt) / "openspec").is_dir():
        raise ValueError(f"{r.prefix}openspec/ isn't on {p.dev}: run openspec init there first")
    did = q("INSERT INTO designs (project, title, created, updated, kind, spec_sub, spec_draft) VALUES (?,?,?,?,?,?,1)",
            (p.name, f"New change: {text[:60]}", time.time(), time.time(), "design", r.sub))
    design_say(did, spec_first(p, r.sub, text))
    return did


def spec_attach(p, d, before):
    """After a drafting chat's turn: tie it to the change folder the turn created. before: the open changes on
    dev before the turn. Returns (text for the owner, note for Claude), or None when the turn created none."""
    _, path = spec_wt(p, d)
    new = sorted(spec_names(path) - before)
    if not new:  # Claude is probably asking about scope first
        return None
    if len(new) > 1:
        names = ", ".join(f"`{n}`" for n in new)
        return (f"This reply created {len(new)} changes ({names}). A chat keeps to one change, so this one is "
                f"attached to none of them: open each from the OpenSpec list.",
                f"your last reply created {len(new)} changes ({', '.join(new)}), so this chat is attached to none "
                f"of them. Keep to one change per chat: the owner opens each of these in its own chat")
    name = new[0]
    title = spec_info(path / SPEC_DIR / name)["title"][:80]
    q("UPDATE designs SET spec=?, spec_draft=0, title=? WHERE id=?", (name, title, d["id"]))
    return f"Created change `{name}`.", f"this chat is now about the change {name}, which your last reply created"


def spec_open(p, sub, name):
    """The chat about a change: the latest one, or a new one. A change only in your checkout is copied into dev."""
    r = spec_part(p, sub)
    if not re.fullmatch(r"[\w.-]+", name or "") or name == "archive":
        raise ValueError(f"bad change name {name!r}")
    old = q("SELECT id FROM designs WHERE project=? AND spec=? AND spec_sub=? ORDER BY updated DESC LIMIT 1",
            (p.name, name, r.sub), one=True)
    if old:
        q("UPDATE designs SET ended=NULL WHERE id=?", (old["id"],))  # opening an ended change's chat reopens it
        return old["id"]
    on_dev, own = r.at(p.dev_wt) / SPEC_DIR / name, r.src / SPEC_DIR / name
    if not on_dev.is_dir() and not own.is_dir():
        raise ValueError(f"there is no OpenSpec change {name}")
    did = q("INSERT INTO designs (project, title, created, updated, kind, spec, spec_sub) VALUES (?,?,?,?,?,?,?)",
            (p.name, spec_info(on_dev if on_dev.is_dir() else own)["title"][:80], time.time(), time.time(),
             "design", name, r.sub))
    if not on_dev.is_dir():
        d = design(did)
        _, path = spec_wt(p, d)
        shutil.copytree(own, path / SPEC_DIR / name)
        saved = spec_land(p, d, f"OpenSpec {name}: copied from {own}")
        q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
          (did, "ops", f"Copied `{SPEC_DIR / name}` from your checkout into {p.dev}. {saved} Your checkout keeps "
                       f"its own copy: if it isn't committed there, delete it when you're done with it, or a "
                       f"release that brings this change to {p.main} will wait for it.", time.time(),
           f"the change was copied from the owner's checkout into {p.dev}"))
    return did


def spec_view(p, d):
    """The change as the chat sees it now, files and all, for the dashboard. That is dev's copy unless the
    chat's worktree holds edits dev lacks: a turn's unsaved files, or commits that didn't land. The worktree
    only catches up with dev before a turn, so once workers tick boxes on dev its copy is stale."""
    r = spec_part(p, d["spec_sub"])
    path = r.at(p.wt / f"spec-{d['id']}")
    own = path.exists() and (
        subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", p.dev], cwd=path,
                       capture_output=True).returncode != 0
        or bool(r.git("--no-optional-locks", "status", "--porcelain", "--untracked-files=all", "--",  # the
                      str(SPEC_DIR), cwd=path, check=False)))  # dashboard polls this while a turn resets the index
    root = path if own else r.at(p.dev_wt)
    folder = root / SPEC_DIR / d["spec"]
    if not folder.is_dir():  # archived (by /opsx:archive or openspec archive in the chat), or removed
        gone = spec_archived(root, d["spec"])
        return {"name": d["spec"], "sub": r.sub, "missing": True, "archived": bool(gone), "archive": gone,
                "commands": []}
    files = [f for f in ("proposal.md", "design.md", "tasks.md") if (folder / f).is_file()]
    files += sorted(str(f.relative_to(folder)) for f in folder.rglob("*.md") if str(f.relative_to(folder)) not in
                    files)
    return dict(spec_info(folder), sub=r.sub, path=str(SPEC_DIR / d["spec"]),
                commands=spec_commands([x for x in (path, r.at(p.dev_wt)) if x.exists()], d["spec"]),
                files=[{"name": f, "text": (folder / f).read_text(errors="replace")[:200_000]} for f in files[:40]])


def spec_brief(p, r, name, title, group=None, k=0, n=1):
    """The task body for a whole change, or for one group of its phases."""
    rel = f"{r.prefix}{SPEC_DIR / name}" if r.prefix else str(SPEC_DIR / name)
    if group:
        items = "\n".join(f"  - [ ] {i}" for i in group["open"])
        scope = (f"This task is part {k + 1} of {n}. It covers only these sections of `tasks.md`: "
                 f"{', '.join(t or '(untitled)' for t in group['titles'])}. Their open items:\n\n{items}\n\n"
                 + ("Earlier sections were built by the tasks before this one and are on dev. " if k else "")
                 + ("Later sections are separate tasks queued after this one: leave them alone." if k + 1 < n
                    else "This is the last part."))
        split = ""
    else:
        scope = "This task covers every open item in `tasks.md`."
        split = ("- The change is one task on purpose. If it is too big for one worker, split it (status "
                 "\"split\") by phase or area of `tasks.md`, never one subtask per checklist item.\n")
    return (f"Implement the OpenSpec change `{name}`: {title}.\n\n"
            f"The change is the brief. Its files are in `{rel}/`: read `proposal.md` (why, and what is in scope), "
            f"`design.md` (how), the requirement deltas in `specs/`, and `tasks.md`. {scope}\n\n"
            f"Rules:\n"
            f"- Keep to the proposal's scope. If what you build has to differ from `design.md` or a spec delta, "
            f"update that file to match and say so in your summary.\n"
            f"- Tick each `tasks.md` box (`- [x]`) as you finish it, in the same commit as the work.\n"
            f"- Add or update tests for the behaviour in the spec scenarios (`#### Scenario:`).\n"
            f"- If the `openspec` CLI is installed, `openspec validate {name} --strict` must pass.\n"
            f"- Don't archive the change (`openspec archive`); that is a separate step the owner asks for.\n"
            + f"- Work through the items with `/claude-ops:openspec:implement {name}` (the Skill tool), still "
              f"within this task's scope and these rules.\n"
            + f"{split}\n"
            f"Done when every box this task covers is ticked, the tests pass, and the change's docs match what "
            f"you built.")


def spec_queue(did, plan, hold=False, release=None):
    """Queue a chat's change as one task, or one task per phase chained in order. Returns the new ids.
    release: None takes the project's default; only the last task of a chain is promoted."""
    d = design(did)
    if not d or not d["spec"]:
        raise ValueError("this chat is not about an OpenSpec change")
    if d["state"] == "thinking":
        raise ValueError("wait for Claude's reply first")
    p = proj(d["project"])
    r = spec_part(p, d["spec_sub"])
    folder = r.at(p.dev_wt) / SPEC_DIR / d["spec"]
    if not folder.is_dir():
        raise ValueError(f"{p.dev} has no {SPEC_DIR / d['spec']}")
    info = spec_info(folder)
    groups = spec_groups(spec_phases((folder / "tasks.md").read_text(errors="replace"))) if info["has_tasks"] else []
    if info["has_tasks"] and not groups:
        raise ValueError("every item in tasks.md is ticked: nothing left to queue")
    parts = [(info["title"], None)] if plan != "phases" or len(groups) < 2 else \
        [(f"{info['title']}: {' + '.join(t for t in g['titles'] if t) or f'part {k + 1}'}", g)
         for k, g in enumerate(groups)]
    ids = []
    for k, (title, g) in enumerate(parts):
        tid = add_task(p.name, spec_brief(p, r, d["spec"], info["title"], g, k, len(parts)), title[:120],
                       hold=bool(hold), depends_on=ids[-1] if ids else None,
                       auto_release=(None if release is None else bool(release)) if k + 1 == len(parts) else False)
        ev(tid, f"queued from OpenSpec change {d['spec']} (design chat #{did})")
        ids.append(tid)
    what = "the whole change as one task" if len(ids) == 1 else f"{len(ids)} tasks, one per phase, each after the one before"
    q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
      (did, "ops", f"Queued {what}.", time.time(), json.dumps(ids),
       f"the owner queued this change with the dashboard's buttons: {', '.join(f'#{i}' for i in ids)}"))
    q("UPDATE designs SET updated=? WHERE id=?", (time.time(), did))
    return ids


# ---------------------------------------------------------------- activity feed

# ---------------------------------------------------------------- task chat (questions to a worker)

TASK_CHAT_PROCS = {}  # task id -> Popen
TASK_CHAT_LIVE = {}  # task id -> tool lines of the reply in progress; a key here means a reply is running
# The opening of a chat's system prompt. A project chat is the owner's own session, like the CLI: their prompts
# come to the dashboard. A chat with a stopped task keeps the worker's limits: nothing can answer a prompt.
PROJECT_CHAT_HEAD = ("You are talking with the owner through the claude-ops dashboard, not a terminal. This chat is "
                     "their own attended session, like the Claude Code CLI: auto mode, their settings, MCP servers "
                     "and Claude in Chrome. When an action needs their permission, the dashboard shows them the "
                     "request with Approve and Deny, and your turn waits for the answer. When they deny one, say what "
                     "you wanted to do and why; don't try it another way. " + GUARD_RULE + " "
                     "You run as a child of the claude-ops daemon, so `opsctl restart`, `opsctl stop` or stopping its "
                     "launchd job stops you mid-command and leaves the restart half done. To restart claude-ops (to "
                     f"load new code, say), run `{CODE / 'opsd.py'} restart`: it restarts in a process of its own, as "
                     "the Restart button in Settings does. Running tasks pause and resume, and this chat stops and "
                     "picks up again in the new daemon; run it once, last in your turn, and only when the owner asks. ")
TASK_CHAT_HEAD = ("You are talking with the owner through the claude-ops dashboard, not a terminal. "
                  "Nobody can answer permission prompts here: actions that would prompt are denied. "
                  "When one is denied, say what you tried, and that they can run it themselves or "
                  "continue this session in a terminal with `claude --resume`. " + GUARD_RULE + " ")
CHAT_PROMPT = ("Reading code: search first (`grep -n`, Grep, Glob), then read only the range you need "
               "(Read with offset and limit, or `sed -n 'a,bp'`), 150 lines or fewer at a time. Never Read a "
               "file over about 500 lines without a range, and never `cat` one. Don't reread what is already "
               "in your context. Keep command output short: pipe it through `tail` or `grep`, and use quiet "
               "flags for tests and builds. "
               f"To show the owner a picture, such as a screenshot, copy it into {IMAGES} under a name of "
               "letters, digits, - and _ with a .png, .jpg, .gif or .webp ending, then embed it with "
               "`![what it shows](/api/images/<name>)`. The dashboard can't show a picture from anywhere else "
               "on disk, such as /tmp or the repo. "
               "A to-do is only for something that truly needs the owner: a password or sudo, a macOS "
               "permission grant (Full Disk Access and the like), an API key or account they must create, or "
               "a decision (you proposed something and wait for their yes). Add one to their to-do list with "
               "a fenced block in the language `todo` holding a JSON list of "
               '{"verb": "Do" or "Agree", "title", "why", "wait"?}; wait, {"tasks": [283], "until": '
               '"merged" or "released", "at": "tue 09:00"} (tasks, at, or both; at is local time as '
               "Start later takes it), keeps it out of their list until those tasks get there and that time comes, or "
               'until a task fails or stops. A step that a command or code change can do is '
               "never a to-do, even setup that must wait for a task to merge: do it, or queue it as a task. "
               'Close an '
               'item that is settled with {"close": "todo:<id>"} in a `todo` block. #N means a task and '
               'todo:N means a to-do; the ids the dashboard reports, and the owner pastes, come as todo:N. '
               "When the owner's reply settles one of your Agree items (a yes, a no, or a changed plan), close "
               "it that way; leave it open while it is still pending. The [ops: ...] line at the start of a "
               "message lists this chat's open Agree items with their todo:N ids. "
               "When the owner asks for work that repeats (\"every Monday, check the dependencies\"), add a "
               "fenced block with the language `schedule` holding a JSON list of {project, title, body, every, "
               "hold?, model?, effort?, after_schedule?}; after_schedule is another schedule's id whose current "
               "task each run waits on; every is local time: 'day HH:MM', 'weekday HH:MM', 'mon HH:MM' (any of "
               "mon..sun, or 'mon,thu HH:MM') or 'hour'. Each slot queues a task with that body for an "
               "unattended worker, so the body must stand alone. "
               "This chat is one topic of the project's conversation on the dashboard, titled from its first "
               "message. When the subject clearly changes (not every reply), retitle it with a fenced block in "
               'the language `topic` holding {"title": "a few words"}; it is taken out of the reply.')
PROJECT_CHAT_PROMPT = PROJECT_CHAT_HEAD + CHAT_PROMPT
TASK_CHAT_PROMPT = TASK_CHAT_HEAD + CHAT_PROMPT
CHAT_STATES = ("review", "needs_input", "failed", "done", "cancelled", "paused")  # the worker is not running
CHAT_NEXT = {"review": "Approve merges the branch with your commits (the gate runs again first); Send back with "
                       "feedback sends the worker back to work",
             "needs_input": "Send answer resumes the task",
             "failed": "Retry runs the task again in this worktree, in this session",
             "cancelled": "Retry runs the task again in this worktree, in this session",
             "paused": "Resume continues the task"}  # the dashboard button that carries a chat's edits on


def task_ask(tid, text):
    """Talk with the task's stopped worker: resume its Claude session in the background. In its worktree it has its
    full tools; once the task is done (merged, its worktree gone) it can read and queue a follow-up task."""
    t = task(tid)
    if not text:
        raise ValueError("ask something")
    if t["state"] not in CHAT_STATES or tid in THREADS or tid in PROCS:
        raise ValueError("the worker is busy; talk to it with Send, or ask when it stops")
    if not t["session_id"]:
        raise ValueError("this task has no worker session to talk to")
    if tid in TASK_CHAT_LIVE:
        raise ValueError("wait for the reply first")
    TASK_CHAT_LIVE[tid] = []  # schedule_once starts no worker for the task while this is set
    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "user", text, time.time()))
    threading.Thread(target=task_chat_turn, args=(tid, text), daemon=True).start()


def chat_full(t):
    """Whether a chat with stopped task t gets full tools: its worktree is there and its branch can still land."""
    return t["state"] != "done" and bool(t["worktree"]) and Path(t["worktree"]).is_dir()


def task_chat_turn(tid, text):
    t = task(tid)
    p = proj(t["project"])
    cwd = Path(t["worktree"] or p.wt / f"t{tid}")
    full = chat_full(t)
    made = not cwd.exists()  # the worktree goes after a merge, but the session lives under its path
    env = dict(os.environ, OPS_FROM=f"task:{tid}", OPS_DAEMON=DAEMON_URL, OPS_TASK_ID=str(tid))
    env.pop("OPS_RESULT", None)  # a chat turn reports nothing to the queue
    if full:  # like the Chat tab: your own settings and auto mode, in the worker's worktree on its branch
        port = p.port_for(tid)
        env.update(OPS_PORT=str(port), **{k: str(v).format(port=port) for k, v in p.env.items()})
        note = (f"[ops: the user is talking with you about this task on the dashboard; its state is {t['state']}. "
                f"You are not running as the worker now: this is a chat, with your full tools, in your worktree "
                f"{cwd} on branch {t['branch']}. Do what they ask. If you change code, run the checks and commit "
                "on this branch as the worker would. Do not write $OPS_RESULT, do not merge, and do not switch "
                f"branches. When you changed something, end by saying the next step: {CHAT_NEXT.get(t['state'], '')}."
                "]\n\n")
        mcp, _ = mcp_servers(p)
        cmd = [CLAUDE, "-p", note + with_images(text), "--output-format", "stream-json", "--verbose",
               "--permission-mode", "auto", "--permission-prompts", "none", "--no-chrome", *PLUGIN_ARGS,
               "--mcp-config", mcp, "--strict-mcp-config", "--settings", guard_settings(str(cwd), p, chat=True),
               "--append-system-prompt", TASK_CHAT_PROMPT]
    else:  # merged: its edits would land nowhere, so it reads, and a change goes to a follow-up task
        note = (f"[ops: the user is asking about this task on the dashboard; its state is {t['state']}. Answer them. "
                "This turn is read-only: do not change files and do not write $OPS_RESULT. "
                + (f"Your worktree was removed after the task finished; the merged code is in {p.dev_wt}. "
                   if made else "")
                + "If they want a change, offer to queue a follow-up task, and when they agree run "
                  f"`{SEND_CMD} {p.name} --title \"...\" \"<a brief that stands alone>\"`: it runs in its own "
                  f"worktree from {p.dev} and merges through the queue.]\n\n")
        cmd = [CLAUDE, "-p", note + with_images(text), "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk", "--permission-prompts", "none",
               "--allowedTools", DESIGN_TOOLS + f",Bash({SEND_CMD}:*)", "--strict-mcp-config", "--no-chrome",
               *PLUGIN_ARGS]
        if made:
            cmd += ["--add-dir", str(p.dev_wt)]
    cmd += ["--resume", t["session_id"], "--add-dir", str(IMAGES)]
    cmd += model_args(session_model(t, t["kind"])) + effort_args(effort_for(t))  # the worker's own session: its model
    rp = cwd / RESULT_FILE
    had = rp.exists()

    def on_session(sid):
        if sid != task(tid)["session_id"]:
            upd(tid, session_id=sid)

    try:
        if made:
            cwd.mkdir(parents=True)
        reply, cost = stream_claude(cmd, cwd, RUNS / str(tid) / "chat", TASK_CHAT_LIVE[tid], TASK_CHAT_PROCS,
                                    tid, on_session, env=env,  # its calls warm the worker's cache: see stale_session
                                    on_call=lambda ctx: upd(tid, last_call=time.time(), ctx_tokens=ctx))
        upd(tid, cost=(task(tid)["cost"] or 0) + cost)
    except Exception as e:
        reply = f"(chat error: {e})"
    finally:
        TASK_CHAT_PROCS.pop(tid, None)
        if full and not had:  # wrote its result anyway: the queue reads only what a worker turn writes
            rp.unlink(missing_ok=True)
        if made:
            try:
                cwd.rmdir()
            except OSError:
                pass
    if STOPPING.is_set():  # the restart stopped it; the next daemon answers again (resume_chats)
        return
    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "assistant", reply, time.time()))
    TASK_CHAT_LIVE.pop(tid, None)


# ---------------------------------------------------------------- peers (one project asks another)
# A worker or chat can hand work to another project, as one Claude session messages a peer: `opsd.py send` (the
# daemon gives each session OPS_FROM, naming its task or chat, and OPS_DAEMON) starts a task, a design chat or a
# project chat there. The new task or chat says who sent it and where that session works, and shows on the
# dashboard like any other. How it ends (a task) or its first reply (a chat) goes back to the sender: an event on
# a task, a note in a chat, which a design chat's next turn reads.

OPSD_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(CODE / 'opsd.py'))}"
SEND_CMD = f"{OPSD_CMD} send"
CHAT_REF_RULE = (f"\n\nA `chat:N` in the owner's message is another chat, of any project; the `[ops: ...]` line says "
                 f"what it is. Read it with `{OPSD_CMD} chat show N` when the request needs it, and cite chats as "
                 "`chat:N`.")
PEER_KINDS = {"task": "task", "design": "design chat", "chat": "chat"}  # what `send` starts -> words
PEER_HOPS = 3  # a send passed on this many times is refused, so two projects can't keep each other busy
PEER_MAX = 10  # sends one task or chat may make


def gist(text, n):
    """text cut to n characters, keeping its lines."""
    t = str(text or "").strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def list_summary(t):
    """A task's summary as one line for the list. A Send to dev summary from before it had a headline
    starts with "<repo>:", which says nothing, so its first "- " detail line stands in for it."""
    lines = str(t["summary"] or "").strip().splitlines()
    if t["kind"] == "senddev" and lines and lines[0].rstrip().endswith(":"):
        detail = next((l.strip()[2:] for l in lines[1:] if l.strip().startswith("- ")), "")
        if detail.strip():
            return clip(detail, 400)
    return clip(t["summary"], 400) or None


def origin_of(ref):
    """The task or chat an OPS_FROM value ("task:12", "chat:3") names, as stored on what it sends. None for a
    send from outside the queue (someone at a terminal)."""
    m = re.fullmatch(r"(task|chat):(\d+)", ref or "")
    row = m and (task if m.group(1) == "task" else design)(int(m.group(2)))
    if not row:
        return None
    up = json.loads(row["origin"] or "null") or {}
    return {"ref": ref, "kind": row["kind"], "id": row["id"],  # task or release; design or project (a chat)
            "project": row["project"], "title": row["title"], "hops": up.get("hops", 0) + 1}


def origin_words(o):
    """"claude-ops task #12", "game design chat #3"."""
    p = PROJECTS.get(o["project"])
    what = {"design": "design chat", "project": "chat"}.get(o["kind"], o["kind"])  # or task, release, deploy
    return f"{p.title if p else o['project']} {what} #{o['id']}"


def origin_view(raw):
    """A stored origin for the dashboard's "Sent by" line, or None."""
    o = json.loads(raw or "null")
    return o and {**o, "words": origin_words(o)}


def origin_place(o):
    """Where the sending session works, so the receiver can read its files."""
    if o["ref"].startswith("task:"):
        t = task(o["id"])
        return f"It works in {t['worktree']}" + (f" (branch {t['branch']})" if t["branch"] else "") \
            if t and t["worktree"] else ""
    d, p = design(o["id"]), PROJECTS.get(o["project"])
    if not p or not d:
        return ""
    if spec_chat(d):
        return f"It works in {spec_part(p, d['spec_sub']).at(p.wt / ('spec-' + str(d['id'])))}"
    return f"It works in {p.repo if o['kind'] == 'project' else p.dev_wt}"


def tell_origin(o, text, ids=()):
    """Pass word back to the session that sent something: an event on its task, a note in its chat."""
    try:
        if o["ref"].startswith("schedule:"):  # a schedule has no session to tell; its page shows the last task
            return
        if o["ref"].startswith("task:"):
            if task(o["id"]):
                ev(o["id"], text)
        elif design(o["id"]):
            q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
              (o["id"], "ops", text, time.time(), json.dumps(list(ids)), " ".join(text.split())[:1500]))
    except Exception as e:  # the sender's side is a courtesy: never fail the task that ended over it
        print(f"tell {o.get('ref')}: {e}", flush=True)


def tell_sender(tid, how):
    """A task another project sent has ended, or needs the owner: tell the sender."""
    t = task(tid)
    if t and t["origin"]:
        p = PROJECTS.get(t["project"])
        tell_origin(json.loads(t["origin"]), f"{p.title if p else t['project']} task #{tid} ({t['title']}), "
                                             f"which you sent, {gist(how, 1500)}", [tid])


def retry_after(tid, after):
    """The task id (12, "12" or "#12") retrying task tid waits on until it merges, checked; None when after is blank.
    It must exist, not be tid, and not wait on tid itself through its own depends_on chain."""
    if after is None or not str(after).strip():
        return None
    a = str(after).strip().lstrip("#").strip()
    if not a.isdigit() or not task(int(a)):
        raise ValueError(f"no task #{a}")
    a = int(a)
    if a == tid:
        raise ValueError("a task can't wait on itself")
    seen, cur = set(), task(a)["depends_on"]
    while cur and cur not in seen:
        if cur == tid:
            raise ValueError(f"#{a} already waits on this task")
        seen.add(cur)
        cur = (task(cur) or {}).get("depends_on")
    return a


def send_peer(ref, project, kind, text, title="", hold=False, model=None, effort=None, after=None, not_before=None,
              jira=None, branch=None):
    """Start a task or chat in project for the session ref names. Returns {kind, id, project, said}.
    model, effort: the task's, as add_task takes them; a chat starts on its kind's model, at the effort setting.
    after: a task id (12 or "#12"), in any project, the new task waits on until it merges (its depends_on).
    not_before: when the task may start, as add_task takes it (--at, --after-reset).
    jira, branch: a branch task's Jira key or named branch (--jira, --branch; README: Branches and pull requests)."""
    p, text = proj(project), (text or "").strip()
    if kind not in PEER_KINDS:
        raise ValueError(f"kind is one of {', '.join(PEER_KINDS)}")
    model = model_name(model)
    if model and kind != "task":
        raise ValueError("--model is for tasks; a chat starts on the project's chat or design model")
    effort = effort_name(effort)
    if effort and kind != "task":
        raise ValueError("--effort is for tasks; a chat runs at the effort setting")
    if after is not None:
        if kind != "task":
            raise ValueError("--after is for tasks; a chat can't wait on a task")
        a = str(after).strip().lstrip("#")
        if not a.isdigit() or not task(int(a)):
            raise ValueError(f"no task #{a}")
        after = int(a)
    if not_before not in (None, ""):
        if kind != "task":
            raise ValueError("--at and --after-reset are for tasks; a chat starts at once")
        start_time(not_before)  # refused here, before anything is made
    if (jira or branch) and kind != "task":
        raise ValueError("--branch and --jira are for tasks; a chat works on no branch")
    if not text:
        raise ValueError("say what the other project should do")
    o = origin_of(ref)
    if o:
        if o["hops"] > PEER_HOPS:
            raise ValueError(f"this work was already passed between projects {PEER_HOPS} times; ask the owner")
        sent = sum(q(f"SELECT COUNT(*) AS n FROM {tb} WHERE json_extract(origin, '$.ref')=?", (ref,), one=True)["n"]
                   for tb in ("tasks", "designs"))
        if sent >= PEER_MAX:
            raise ValueError(f"{origin_words(o)} already sent {PEER_MAX} things to other projects; ask the owner")
        if o["kind"] == "design" and kind == "chat":
            raise ValueError("a design chat can't change files, so it can't start a chat that does; send a task")
    who = f"{origin_words(o)} (\"{o['title']}\")" if o else "someone outside the queue"
    title = (title or title_of(text)).strip()  # from the message: the header would name every send alike
    place = (origin_place(o) if o else "").replace("\n", " ")
    if kind == "task":
        head = (f"[ops: sent by {who}, a Claude session in the claude-ops queue, not typed by the owner. "
                f"{place + '. ' if place else ''}You may read its files; change only your own worktree. "
                "When this task ends, the queue tells the sender how it went.]")
        tid = add_task(p.name, f"{head}\n\n{text}", title, hold=hold, depends_on=after, origin=o, model=model,
                       effort=effort, not_before=not_before, jira=jira, target=branch)
        ev(tid, f"sent by {who}" + (f", after #{after}" if after else ""))
        if o:
            tell_origin(o, f"Sent task #{tid} to {p.title}: {task(tid)['title']}", [tid])
        return {"kind": "task", "id": tid, "project": p.name,
                "said": f"Queued task #{tid} in {p.title} ({p.name})" + (f", after #{after}" if after else "") + ". It shows on the dashboard"
                        + ("; the queue tells you here when it ends." if o else ".")}
    head = (f"[ops: this chat was started by {who}, a Claude session in the claude-ops queue, not by the owner. "
            f"{place + '. ' if place else ''}Your first reply goes back to it. The owner sees this chat on the "
            "dashboard and may join in.]")
    did = q("INSERT INTO designs (project, title, created, updated, kind, origin) VALUES (?,?,?,?,?,?)",
            (p.name, title[:80], time.time(), time.time(),
             "project" if kind == "chat" else "design", json.dumps(o) if o else None))
    design_say(did, f"{head}\n\n{text}")
    if o:
        tell_origin(o, f"Started {PEER_KINDS[kind]} #{did} in {p.title}: {title[:80]}")
    return {"kind": kind, "id": did, "project": p.name,
            "said": f"Started {PEER_KINDS[kind]} #{did} in {p.title} ({p.name}). It shows on the dashboard"
                    + ("; its first reply comes back to you here." if o else ".")}


def peer_brief(p, who):
    """The part of a brief that says how to hand work to another project. who: task, design or chat."""
    others = [o for o in PROJECTS.values() if o.name != p.name]
    if not others:
        return ""
    names = ", ".join(f"`{o.name}` ({o.title}, {o.repo})" if o.title != o.name else f"`{o.name}` ({o.repo})"
                      for o in others)
    when = {"task": "Send only what this task truly needs from that project; never edit its files yourself.",
            "design": "Send only when the owner asks you to or agrees, as with queueing. You can start a task or "
                      "a design chat there, not a chat.",
            "chat": "Send only when the owner asks you to or agrees."}[who]
    back = ("how it ends is logged on this task" if who == "task"
            else "how it ends (or the chat's first reply) reaches you in an `[ops: ...]` line")
    return f"""## Other projects

The claude-ops queue also runs {names}. To hand work to one of them, as you would message a peer:

    {SEND_CMD} <project> "<message>"

- Default: a task in that project's queue. An unattended worker does it in its own worktree.
- `--design`: a design chat there instead, to ask how something works or talk an idea through.
- `--chat`: a chat in that project's checkout, which can edit files there.
- `--title "..."` names it; `--hold` has the owner try a task before it merges.
- `--after <task id>` (tasks only) holds it in that queue until that task merges.

Nothing wakes you when another task merges, so never promise to do something later ("once #N merges, I'll
send…"). When work must wait for a task, send it now with `--after`.

The other session can't see this conversation, so the message must stand alone: the goal and why, exactly what
to do, and how to tell it is done. The queue adds who sent it and where you work. The command prints the new
task's or chat's id, and {back}. {when}

"""


# A task page asks for its worker's transcript every few seconds, and a transcript grows to megabytes. It only
# ever grows, so each read starts where the last one stopped.
FEEDS = {}  # task id -> (bytes read, the newest activity entries)
STEP_SEEN = {}  # task id -> ((size, mtime) of the transcript, last_step's answer)
FEEDS_LOCK = threading.Lock()


def activity(tid, limit=120):
    f = RUNS / str(tid) / "claude.jsonl"
    with FEEDS_LOCK:
        pos, items = FEEDS.get(tid, (0, []))
        try:
            with open(f, "rb") as fh:
                size = fh.seek(0, 2)
                if size < pos:  # a new file in its place: start over
                    pos, items = 0, []
                fh.seek(pos)
                data = fh.read(size - pos)
        except OSError:
            return []
        end = data.rfind(b"\n") + 1  # a line still being written is read next time
        if end:
            items = (items + activity_of(data[:end].decode(errors="replace").splitlines()))[-limit:]
            FEEDS[tid] = (pos + end, items)
        return items


def activity_of(lines):
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ty = e.get("type")
        if ty == "ops":
            out.append({"k": "ops", "t": "▶ new turn: " + e.get("prompt", "")[:240]})
        elif ty == "user" and str(e.get("message", {}).get("content")).startswith(STEER_NOTE):
            out.append({"k": "you", "t": "✉ you: " + e["message"]["content"][len(STEER_NOTE):][:1200]})
        elif ty == "assistant" and not e.get("parent_tool_use_id"):
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append({"k": "say", "t": c["text"].strip()[:1200]})
                elif c.get("type") == "tool_use":
                    out.append({"k": "tool", "t": tool_line(c.get("name"), c.get("input") or {})})
        elif ty == "result":
            out.append({"k": "ops", "t": f"■ turn ended ({e.get('num_turns', '?')} steps, "
                                         f"${e.get('total_cost_usd') or 0:.2f})"})
    return out


def last_step(tid):
    """What the worker is doing, in words, read from the end of its transcript. t is its latest tool
    call as a phrase ("Run the tests", "Editing opsd.py"), or what it said if it spoke last; say is
    what it said before that call, while still recent. ts is when the transcript last grew: for a
    long command, how long it has been running."""
    f = RUNS / str(tid) / "claude.jsonl"
    try:
        st = os.stat(f)
        if STEP_SEEN.get(tid, (None,))[0] == (st.st_size, st.st_mtime):
            return STEP_SEEN[tid][1]  # the transcript has not moved since the last poll
        with open(f, "rb") as fh:
            fh.seek(max(0, fh.seek(0, 2) - (256 << 10)))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    STEP_SEEN[tid] = ((st.st_size, st.st_mtime), step_of(lines, st.st_mtime))
    return STEP_SEEN[tid][1]


def step_of(lines, ts):
    step, calls = None, 0
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant" or e.get("parent_tool_use_id"):
            continue  # a subagent's step: its Agent call already says what it is doing
        for c in reversed(e.get("message", {}).get("content") or []):
            if c.get("type") == "tool_use":
                calls += 1
                if not step:
                    step = {"k": "tool", "t": step_phrase(c.get("name"), c.get("input") or {}), "ts": ts}
                elif calls > 8:  # words from that many calls back no longer describe this step
                    return step
            elif c.get("type") == "text" and c.get("text", "").strip():
                if not step:
                    return {"k": "say", "t": first_words(c["text"]), "ts": ts}
                step["say"] = first_words(c["text"])
                return step
    return step


STATE_WORDS = {"running": "working", "gating": "checking", "needs_input": "needs you", "on_branch": "on its branch",
               "pr": "watching its PRs"}


def wait_reasons(tasks, s=None):
    """Why each queued task has not started yet, in the scheduler's own order of checks."""
    s = settings_now() if s is None else s
    if STOPPING.is_set() or (RESTART_AFTER and not RESTART_AFTER.get("deploys_only")):
        return {t["id"]: "waiting for claude-ops to restart" for t in tasks if t["state"] == "queued"}
    if s.get("paused") == "1":
        return {t["id"]: "the queue is paused" for t in tasks if t["state"] == "queued"}
    if lim := limit_info(s):
        return {t["id"]: lim["words"] for t in tasks if t["state"] == "queued"}
    usage = usage_info(s)
    braked, taper = usage["braked"], usage["taper"]
    by_id = {t["id"]: t for t in tasks}
    find = lambda i: by_id.get(i) or task(i)  # the list holds the newest 500; an older one is read on its own
    free = int(s.get("max_parallel") or 1) - len(THREADS)
    claude = claude_running(find) if taper else 0
    mine = {}
    for i in list(THREADS):
        name = (find(i) or {}).get("project")
        mine[name] = mine.get(name, 0) + 1
    out = {}
    for t in sorted((t for t in tasks if t["state"] == "queued"), key=lambda t: (-t["priority"], t["id"])):
        p = PROJECTS.get(t["project"])
        dep = find(t["depends_on"]) if t["depends_on"] else None
        if not p:
            why = "its project is not loaded"
        elif s.get(f"paused:{p.name}") == "1":
            why = f"{p.title} is paused"
        elif getattr(p, "missing", None):
            why = " and ".join(f"{m}/" for m in p.missing) + " " + ("is" if len(p.missing) == 1 else "are") + \
                  f" not cloned: clone {'it' if len(p.missing) == 1 else 'them'} into {p.repo}"
        elif restart_holds(t):
            why = "waiting for claude-ops to restart"
        elif t.get("not_before") and time.time() < t["not_before"]:
            why = start_words(t)
        elif braked and starts_claude(t):
            why = braked["words"]
        elif dep and dep_waits(t, dep):
            why = f"waiting for #{dep['id']} to finish (it is {STATE_WORDS.get(dep['state'], dep['state'])})"
        elif holder := branch_holder(t):
            why = f"waiting for task #{holder['id']} on {t['branch']}"
        elif blocker := exclusive_blocker(t):
            why = f"waiting for {blocker['kind']} #{blocker['id']}"
        elif p.max_parallel and mine.get(p.name, 0) >= p.max_parallel:
            why = f"{p.title} already runs {p.max_parallel} at once, its limit"
        elif free <= 0 and t.get("start_at") != "pr":
            why = "waiting for a free slot (all busy)"
        elif taper and starts_claude(t) and claude >= taper["cap"]:
            why = taper["words"]
        else:
            why = "starting now"
            free -= t.get("start_at") != "pr"
            claude += t["kind"] not in ("deploy", "release")
        out[t["id"]] = why
    return out


def tool_line(name, i):
    arg = (i.get("command") or i.get("file_path") or i.get("description") or i.get("pattern")
           or i.get("prompt") or "")
    return f"{name}: {str(arg).splitlines()[0][:200] if arg else ''}"


def clip(text, n):
    """The first line of text, cut to n characters."""
    t = str(text or "").strip()
    t = t.splitlines()[0] if t else ""
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def step_phrase(name, i):
    """A tool call in words, for the task list: what it does, not the command it runs."""
    name = name or "a tool"
    base = lambda v: Path(str(v)).name or str(v)
    if i.get("description") and name not in ("Agent", "Task"):
        d = clip(i["description"], 160)  # Bash, Monitor and others say what they do themselves
        return d[:1].upper() + d[1:]
    if name == "Bash":
        words = [w for w in str(i.get("command", "")).split() if "=" not in w and w not in ("cd", "&&", "(")]
        return f"Running {base(words[0])}" if words else "Running a command"
    if name in ("Agent", "Task"):
        return f"Subagent: {clip(i['description'], 160)}" if i.get("description") else "Starting a subagent"
    if name == "Read":
        return f"Reading {base(i.get('file_path') or 'a file')}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {base(i.get('file_path') or i.get('notebook_path') or 'a file')}"
    if name == "Write":
        return f"Writing {base(i.get('file_path') or 'a file')}"
    if name == "Grep":
        return f"Searching the code for “{clip(i.get('pattern'), 60)}”"
    if name == "Glob":
        return f"Looking for files named {clip(i.get('pattern'), 60)}"
    if name == "WebSearch":
        return f"Searching the web for “{clip(i.get('query'), 80)}”"
    if name == "WebFetch":
        return f"Reading {urlparse(str(i.get('url') or '')).netloc or 'a web page'}"
    if name == "TodoWrite":  # the item it marked in progress is the best summary there is
        now = next((t for t in i.get("todos") or [] if t.get("status") == "in_progress"), None)
        return clip(now.get("activeForm") or now.get("content"), 160) if now else "Updating its to-do list"
    if name == "Skill":
        return f"Using the {i['skill']} skill" if i.get("skill") else "Using a skill"
    if name == "ToolSearch":
        return "Loading tools"
    if name.startswith("mcp__"):
        return "Using " + " ".join(name.split("__")[1:]).replace("_", " ")
    return f"Using {name}"


def first_words(text, n=240):
    """The opening of what the worker said: its first paragraph, cut at a sentence when it is long."""
    t = " ".join(re.sub(r"[*`#>]+", "", text.strip().split("\n\n")[0]).split())
    if len(t) <= n:
        return t
    cut = max(t.rfind(". ", 0, n), t.rfind("? ", 0, n), t.rfind("! ", 0, n))
    return t[:cut + 1] if cut > n // 3 else t[:n - 1].rstrip() + "…"


DIFFSTATS = {}  # task id -> (what the diff depends on, its --stat), so a task page polls git only when that moves


def diffstat(p, t):
    """The files a task changed, as git diff --stat per repo."""
    tips = "" if t["state"] == "done" else " ".join(  # a done task's branch no longer moves
        r.git("rev-parse", "--verify", "-q", t["branch"], check=False) for r in p.parts)
    key = (t["merged"], t["branch"], tips)
    hit = DIFFSTATS.get(t["id"])
    if hit and hit[0] == key:
        return hit[1]
    stats = []
    for r in p.parts:
        sha = p.mine(r, t["merged"]) if p.repos else t["merged"]
        if t["merged"] and not sha:
            continue  # a workspace repo this task did not change
        base = f"{sha}^1" if sha else p.dev
        st = r.git("diff", "--stat", f"{base}...{t['branch']}", check=False)
        if st:
            stats.append(f"{r.name}/\n{st}" if r.sub else st)
    out = "\n\n".join(stats)[-3000:]
    DIFFSTATS[t["id"]] = (key, out)
    return out


# ---------------------------------------------------------------- images
# The dashboard uploads a dropped or pasted screenshot here and puts "[image: <path>]" in the message.
# Every Claude it starts gets --add-dir IMAGES, and with_images tells it to look.

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
IMAGE_REF = re.compile(r"\[image: ([^\]\n]+)\]")
MAX_IMAGE = 20 << 20


def save_image(data_url):
    m = re.fullmatch(r"data:(image/[\w.+-]+);base64,(.+)", data_url or "", re.S)
    if not m or m.group(1) not in IMAGE_TYPES:
        raise ValueError("only PNG, JPEG, GIF and WebP images can be attached")
    data = base64.b64decode(m.group(2))
    if len(data) > MAX_IMAGE:
        raise ValueError(f"image is over {MAX_IMAGE >> 20} MB")
    IMAGES.mkdir(parents=True, exist_ok=True)
    path = IMAGES / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}{IMAGE_TYPES[m.group(1)]}"
    path.write_bytes(data)
    return {"path": str(path), "name": path.name}


def title_of(text):
    """The first line of text that is more than image references, for a task or chat title."""
    for line in (text or "").splitlines():
        line = IMAGE_REF.sub("", line).strip()
        if line:
            return line
    return "screenshot" if IMAGE_REF.search(text or "") else "untitled"


def with_images(text):
    """text, plus a note telling Claude to look at the images it attaches."""
    paths = [s for s in IMAGE_REF.findall(text or "") if Path(s).parent == IMAGES and Path(s).is_file()]
    if not paths:
        return text
    return (f"{text}\n\n[ops: the user attached {len(paths)} image{'s' if len(paths) > 1 else ''}. "
            "Look at each with the Read tool before you answer: " + ", ".join(paths) + "]")


# ---------------------------------------------------------------- analyze
# The Analyze tab: where the queue's time and money went over a period, and plain rules that say how to spend less.
# All of it comes from the tasks table (tasks.cost is exact per task, see cost_added) and each task's events.

TURN_EVENTS = ("fix round", "answered", "changes requested", "retried", "resumed")  # each starts another Claude turn
WAIT_EVENTS = {"needs your input": "your answer", "needs you:": "your answer", "waiting for your review": "your review",
               "hit the Claude usage limit": "the usage limit", "paused: usage limit": "the usage limit",
               "paused": "a pause"}  # the task sits idle from one of these to its next event
COLD_GAP = 3600  # the prompt cache lives an hour: a turn that starts later writes its whole context again
COLD_COST = 0.80  # extra $ per cold turn: $11.48 over 14 misses (measured on the owner's own usage)
STARTUP_COST = 0.53  # $ a fresh worker spends before its first edit, median (measured on the owner's own usage)
SONNET_SHARE = 0.5  # Sonnet 5.5 costs half of Opus 5.5 per token ($2/$10 against $4/$20; cache reads are equal)


def pct(xs, p):
    """The p-th percentile of xs (nearest rank), or None for none."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if p == 50 and len(xs) % 2 == 0:  # the median of an even count: the mean of the middle two
        return (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def model_family(m):
    m = (m or "").lower()
    return next((f for f in ("opus", "sonnet", "haiku", "fable") if f in m), m or "default")


def task_times(t, evs, now):
    """(queued, ran, waited, colds, fixes, causes) for task t from its events. waited: seconds it sat on you, the usage
    limit or a pause between started and finished; ran is the rest. colds: the reasons of turns that started over an
    hour after the task's previous event. causes: what each fix round fixed."""
    queued = t["started"] - t["created"] if t["started"] and t["created"] else None
    end = t["finished"] or now
    waited, colds, fixes, causes = 0.0, [], 0, []
    for i, e in enumerate(evs):
        text, prev = e["text"], evs[i - 1] if i else None
        why = next((w for k, w in WAIT_EVENTS.items() if text.startswith(k)), None)
        if why and t["started"] and e["ts"] >= t["started"]:
            nxt = evs[i + 1]["ts"] if i + 1 < len(evs) else end
            waited += max(0, min(nxt, end) - e["ts"])
        if text.startswith("fix round"):
            fixes += 1
            if "rebase conflict" in text:
                causes.append("rebase conflict")
        if text.startswith("gate failed: "):
            causes.append(text[len("gate failed: "):])
        if text.startswith(TURN_EVENTS) and prev and e["ts"] - prev["ts"] > COLD_GAP:
            colds.append(next((w for k, w in WAIT_EVENTS.items() if prev["text"].startswith(k)), "other"))
    ran = max(0.0, end - t["started"] - waited) if t["started"] else None
    return queued, ran, waited, colds, fixes, causes


def gate_cause(p, text):
    """A gate failure's first line ("`cmd` exited 1") as the gate step's name, when the project has that step."""
    m = re.match(r"`(.*)` exited", text)
    if p and m:
        for g in p.gate:
            if g["run"] == m.group(1):
                return g["name"]
    return text[:100]


def group_stats(rows, total):
    costs, rans = [r["cost"] for r in rows], [r["ran"] for r in rows if r["ran"] is not None]
    cost = sum(costs)
    return {"n": len(rows), "cost": cost, "share": cost / total if total else 0,
            "cost_med": pct(costs, 50), "cost_p90": pct(costs, 90),
            "ran_med": pct(rans, 50), "ran_p90": pct(rans, 90), "ran_total": sum(rans)}


def breakdown(rows, key, total, order=None):
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = [{"key": k, **group_stats(v, total)} for k, v in groups.items()]
    return sorted(out, key=lambda g: order.index(g["key"]) if order and g["key"] in order else -g["cost"])


def turns_bucket(n):
    return "1" if n <= 1 else "2" if n == 2 else "3–4" if n <= 4 else "5+"


def analysis(period="7", project=""):
    """The /api/analyze body: run times and costs of the tasks created in the period, per project and overall,
    broken down, with the dearest and slowest tasks and the suggestions computed from them. project is a
    comma-separated list of project names (the header's project filter); empty means every project."""
    now = time.time()
    days = {"1": 1, "7": 7, "30": 30}.get(period)
    since = now - days * 86400 if days else 0
    cols = "id,project,title,kind,state,cost,created,started,finished,model,session_model,effort,error"
    every = q(f"SELECT {cols} FROM tasks WHERE created >= ? ORDER BY id", (since,))
    evs = {}
    for e in q("SELECT task_id, ts, text FROM events WHERE task_id IN (SELECT id FROM tasks WHERE created >= ?) "
               "ORDER BY task_id, id", (since,)):
        evs.setdefault(e["task_id"], []).append(e)
    rows = []
    for t in every:
        queued, ran, waited, colds, fixes, causes = task_times(t, evs.get(t["id"], []), now)
        p = PROJECTS.get(t["project"])
        turns = 1 + sum(e["text"].startswith(TURN_EVENTS) for e in evs.get(t["id"], [])) if t["started"] else 0
        rows.append({"id": t["id"], "project": t["project"], "title": t["title"], "kind": t["kind"] or "task",
                     "created": t["created"] or now,
                     "state": t["state"], "cost": t["cost"] or 0, "queued": queued, "ran": ran, "waited": waited,
                     "colds": colds, "fixes": fixes, "turns": turns, "causes": [gate_cause(p, c) for c in causes],
                     "model": model_family(t["session_model"] or t["model"] or model_for(t["kind"] or "task")),
                     "effort": t["effort"] or "default", "error": (t["error"] or "").splitlines()[0][:160]
                     if t["error"] else ""})
    first = min((r["created"] for r in every if r["created"]), default=now)
    span_days = days or max(1.0, (now - first) / 86400)
    chats = q("SELECT project, kind, cost FROM designs WHERE updated >= ?", (since,))

    def summary(rs, chat_rows):
        total = sum(r["cost"] for r in rs)
        qs, rans = [r["queued"] for r in rs if r["queued"] is not None], [r["ran"] for r in rs if r["ran"] is not None]
        wasted = [r for r in rs if r["state"] in ("failed", "cancelled")]
        # The medians are of tasks proper: releases and Send to dev runs rarely start Claude, and their $0 and
        # sub-second runs would pull "cost per task" and "run time" to zero. by_kind still shows them.
        work = [r for r in rs if r["kind"] == "task"] or rs
        wq = [r["queued"] for r in work if r["queued"] is not None]
        meds = {k: v for k, v in group_stats(work, total).items() if k.endswith(("_med", "_p90"))}
        return {**group_stats(rs, total), **meds, "chat_cost": sum(c["cost"] or 0 for c in chat_rows), "work_n": len(work),
                "queued_med": pct(wq, 50), "queued_p90": pct(wq, 90), "queued_total": sum(qs),
                "waited_total": sum(r["waited"] for r in rs), "ran_total": sum(rans),
                "wasted": sum(r["cost"] for r in wasted), "wasted_n": len(wasted)}

    scope = {x.strip() for x in project.split(",") if x.strip()}
    if scope:
        rows, chats = [r for r in rows if r["project"] in scope], [c for c in chats if c["project"] in scope]
    per_project = sorted(({"project": name, "title": PROJECTS[name].title if name in PROJECTS else name,
                           **summary([r for r in rows if r["project"] == name],
                                     [c for c in chats if c["project"] == name])}
                          for name in {r["project"] for r in rows}), key=lambda s: -s["cost"])
    total = sum(r["cost"] for r in rows)
    day = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts))  # noqa: E731
    daily = {}
    start = time.localtime(since or min((r["created"] for r in rows), default=now))
    d0 = time.mktime((start.tm_year, start.tm_mon, start.tm_mday, 12, 0, 0, 0, 0, -1))
    while d0 <= now + 86400 and len(daily) < 400:  # every day of the period, so a quiet day shows as a gap
        if day(d0) <= day(now):
            daily.setdefault(day(d0), {"day": day(d0), "cost": 0, "n": 0})
        d0 += 86400
    for r in rows:
        d = daily.setdefault(day(r["created"]), {"day": day(r["created"]), "cost": 0, "n": 0})
        d["cost"] += r["cost"]
        d["n"] += 1
    causes = {}
    for r in rows:
        for c in r["causes"]:
            g = causes.setdefault(c, {"cause": c, "n": 0, "tasks": set()})
            g["n"] += 1
            g["tasks"].add(r["id"])
    causes = sorted(({**g, "tasks": sorted(g["tasks"])} for g in causes.values()), key=lambda g: -g["n"])
    colds = {}
    for r in rows:
        for c in r["colds"]:
            colds[c] = colds.get(c, 0) + 1
    brief = lambda r: {k: r[k] for k in ("id", "project", "title", "kind", "state", "cost", "ran", "queued",  # noqa: E731
                                         "fixes", "turns", "model", "effort")}
    out = {"now": now, "period": period if days else "all", "days": span_days, "project": project,
           "summary": summary(rows, chats), "per_project": per_project,
           "by_kind": breakdown(rows, lambda r: r["kind"], total),
           "by_model": breakdown(rows, lambda r: r["model"], total),
           "by_effort": breakdown(rows, lambda r: r["effort"], total,
                                  ["low", "medium", "high", "xhigh", "max", "default"]),
           "by_fixes": breakdown(rows, lambda r: "2+" if r["fixes"] >= 2 else str(r["fixes"]), total, ["0", "1", "2+"]),
           "by_turns": breakdown([r for r in rows if r["turns"]], lambda r: turns_bucket(r["turns"]), total,
                                 ["1", "2", "3–4", "5+"]),
           "by_state": breakdown(rows, lambda r: r["state"], total),
           "dearest": [brief(r) for r in sorted(rows, key=lambda r: -r["cost"])[:8] if r["cost"]],
           "slowest": [brief(r) for r in sorted(rows, key=lambda r: -(r["ran"] or 0))[:8] if r["ran"]],
           "daily": sorted(daily.values(), key=lambda d: d["day"]),
           "gate_causes": causes[:8], "colds": colds}
    out["suggestions"] = suggestions(rows, out)
    return out


def suggestions(rows, a):
    """How to make the queue cheaper and faster, from plain rules over the analysis. Each is {text, save}: save is
    the dollars it might save over the period (an estimate, or None), and the list is dearest first."""
    out, total, per_week = [], a["summary"]["cost"], 7 / a["days"]
    money = lambda x: f"${x:,.2f}"  # noqa: E731
    fixed, clean = [r for r in rows if r["fixes"]], [r for r in rows if not r["fixes"] and r["state"] == "done"]
    if len(fixed) >= 2 and len(clean) >= 2:
        mf, mc = pct([r["cost"] for r in fixed], 50), pct([r["cost"] for r in clean], 50)
        if mc and mf > 1.3 * mc:
            text = (f"Tasks that needed a gate-fix round cost {mf / mc:.1f}× as much as those that passed first time "
                    f"(median {money(mf)} against {money(mc)}; {len(fixed)} tasks).")
            if a["gate_causes"]:
                top = a["gate_causes"][0]
                rounds = sum(g["n"] for g in a["gate_causes"])
                text += (f" The main cause is {top['cause']} ({top['n']} of {rounds} failures). "
                         + ("Tasks that touch the same files ran side by side: chain them with After #."
                            if top["cause"] == "rebase conflict" else
                            "Say in the project's CLAUDE.md how to run that check and what usually trips it, so "
                            "workers pass it before they hand in."))
            out.append({"text": text, "save": sum(max(0, r["cost"] - mc) for r in fixed) / 2})
    rel = [r for r in rows if r["kind"] == "release"]  # the one kind with its own model setting and routine work
    opus = [r for r in rel if r["model"] in ("opus", "fable")]
    cost = sum(r["cost"] for r in opus)
    if cost >= 1 and model_family(model_for("release")) not in ("sonnet", "haiku"):
        sonnet = [r["cost"] for r in rel if r["model"] == "sonnet"]
        share = min(1, pct(sonnet, 50) / pct([r["cost"] for r in opus], 50)) if len(sonnet) >= 2 else SONNET_SHARE
        out.append({"text": f"Opus on release checks costs {money(cost * per_week)}/week ({len(opus)} runs). Sonnet "
                            f"would save about {money(cost * (1 - share) * per_week)}/week: set Releases to sonnet "
                            f"under Models on the Settings tab.", "save": cost * (1 - share)})
    n = sum(a["colds"].values())
    if n:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(a["colds"].items(), key=lambda kv: -kv[1]))
        tip = []
        if a["colds"].get("your answer", 0) + a["colds"].get("your review", 0):
            tip.append("answering questions and reviews within an hour keeps the cache warm")
        if a["colds"].get("the usage limit"):
            tip.append("a lower Usage brake (Settings) stops new tasks before the limit cuts running ones off")
        out.append({"text": f"{n} turn{'s' if n > 1 else ''} started over an hour after the task's last step, so "
                            f"the prompt cache had expired and the context was paid for again (about "
                            f"{money(COLD_COST)} each). Waiting on: {parts}." + (f" Tip: {'; '.join(tip)}." if tip else ""),
                    "save": n * COLD_COST})
    s = a["summary"]
    if s["wasted"] > 1 and s["wasted"] > 0.1 * total:
        worst = max((r for r in rows if r["state"] in ("failed", "cancelled")), key=lambda r: r["cost"])
        out.append({"text": f"Failed and cancelled tasks cost {money(s['wasted'])}, {s['wasted'] / total:.0%} of "
                            f"the spend ({s['wasted_n']} task{'s' if s['wasted_n'] > 1 else ''}). The dearest is #{worst['id']} "
                            f"({money(worst['cost'])}, {worst['state']}{': ' + worst['error'] if worst['error'] else ''}). "
                            f"A design chat before queueing catches briefs that can't be done as written.",
                    "save": s["wasted"]})
    small = [r for r in rows if r["kind"] == "task" and r["state"] == "done" and 0 < r["cost"] < 2 * STARTUP_COST]
    if len(small) >= 5 and len(small) > 0.3 * len(rows):
        out.append({"text": f"{len(small)} tasks cost under {money(2 * STARTUP_COST)} each, and about "
                            f"{money(STARTUP_COST)} of any task goes on starting up and reading the code. Queue small "
                            f"related changes as one task.", "save": len(small) * STARTUP_COST / 2})
    if s["queued_med"] and s["queued_med"] > 900:
        out.append({"text": f"Tasks waited a median {span(s['queued_med'])} in the queue before a worker took them "
                            f"(p90 {span(s['queued_p90'])}). More Parallel workers (Settings) start them sooner, at "
                            f"the same cost, as long as the usage window allows it.", "save": None})
    hi = [r["cost"] for r in rows if r["effort"] in ("high", "xhigh", "max") and r["state"] == "done"]
    lo = [r["cost"] for r in rows if r["effort"] in ("low", "medium") and r["state"] == "done"]
    if len(hi) >= 3 and len(lo) >= 3 and pct(hi, 50) > 1.5 * pct(lo, 50):
        out.append({"text": f"Tasks at high effort or above cost a median {money(pct(hi, 50))}, against "
                            f"{money(pct(lo, 50))} at low or medium. Keep high effort for hard tasks.",
                    "save": sum(hi) - len(hi) * pct(lo, 50)})
    return sorted(out, key=lambda x: -(x["save"] or 0))


# ---------------------------------------------------------------- to-dos
# Everything that needs you, in one list for /api/state (README "To-dos"). Most items are derived on each state
# build from what the tasks, chats and project facts already say; only Do and Agree, which Claude adds, are stored.

TODO_GROUP = {"Answer": "unblock", "Fix": "unblock", "Promote": "ship", "Ship": "ship", "Commit": "ship",
              "Try": "review", "Review": "review", "Agree": "review", "Read": "review", "Do": "only_me"}
TODO_ORDER = ("unblock", "ship", "review", "only_me")
TODO_MIN = {"Answer": 2, "Fix": 5, "Try": 5, "Review": 3, "Agree": 2, "Read": 3, "Promote": 1, "Ship": 2,
            "Commit": 2, "Do": 5}  # a guess at the minutes each takes you
TODO_CLOSES = {"Answer": "Closes when you answer.", "Fix": "Closes when it is retried or cancelled.",
               "Try": "Closes when you approve or send it back.", "Review": "Closes when you approve or send it back.",
               "Promote": "Closes when dev is promoted to main.", "Ship": "Closes when you deploy.",
               "Commit": "Closes when the files are committed or gone.",
               "Agree": "Closes when Claude records your answer, or you click Done.",
               "Do": "Closes when you click Done."}  # how each item leaves the list, shown under its why
TODO_BLOCK = re.compile(r"```todo[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)


def todo_item(key, verb, project, title, why, since, kind, sid, **extra):
    """subject: what the item is about (task:<id>, project:<name>:promote|ship|commit): the latest topic with it is the
    item's topic (todo_topic), and New tab ↗ opens a pending topic about it (conv_open how=subject)."""
    subject = f"task:{sid}" if kind == "task" else key if kind == "project" else None
    return {"key": key, "subject": subject, "verb": verb, "group": TODO_GROUP[verb], "project": project, "title": title,
            "why": why or "", "since": since or 0, "est_min": TODO_MIN[verb], "closes": todo_closes(verb, kind),
            "source": {"kind": kind, "id": sid}, **extra}


def todo_closes(verb, kind):
    """The sentence the Today step and list show on how the item closes: a chat's Read when you end the topic, a
    report's when you mark it read (RES-5)."""
    if verb == "Read":
        return "Closes when you end the topic." if kind == "chat" else "Closes when you mark it read."
    return TODO_CLOSES[verb]


def todo_add(project, items, kind, sid, queued=None):
    """Store what Claude asks of you: a list of {"verb": "Do"|"Agree", "title", "why", "wait"?}, or {"close": id}.
    kind is 'design' (a chat) or 'task'. queued: the ids the same reply's queue block queued, which a wait's
    "after" indices point into (None for a worker). Returns a note on what it did, for Claude's next turn."""
    added, closed, bad = [], [], []
    for it in items if isinstance(items, list) else [items]:
        if not isinstance(it, dict):
            bad.append("an item that is not an object")
        elif "close" in it:
            tid = todo_id(it["close"])
            if tid is None:
                bad.append(f"close {it['close']!r} is not a to-do id (12 or \"todo:12\")")
                continue
            if todo_done(tid, project):
                closed.append(tid)
            else:
                bad.append(f"no open to-do todo:{tid} in {project}")
        elif it.get("verb") not in ("Do", "Agree") or not str(it.get("title") or "").strip():
            bad.append(f"an item needs verb Do or Agree and a title, not {json.dumps(it)[:120]}")
        else:
            title = str(it["title"]).strip()
            if q("SELECT id FROM todos WHERE project=? AND title=? AND closed IS NULL", (project, title), one=True):
                continue  # already on your list
            try:
                wait = todo_wait_from(it.get("wait"), queued)
            except ValueError as e:
                wait = None
                bad.append(f"the wait on {title!r} ({e}); it was added, not parked")
            tid = q("INSERT INTO todos (project, verb, title, why, source_kind, source_id, created) VALUES "
                    "(?,?,?,?,?,?,?)", (project, it["verb"], title, str(it.get("why") or ""), kind, sid, time.time()))
            added.append(tid)
            if wait and (wait[0] or wait[2]):
                todo_wait_set(f"todo:{tid}", *wait)
    notes = ([f"added to-do {', '.join(f'todo:{i}' for i in added)}"] if added else []) + \
            ([f"closed to-do {', '.join(f'todo:{i}' for i in closed)}"] if closed else []) + \
            [f"a to-do was skipped: {b}" if not b.startswith("the wait") else b for b in bad]
    return "; ".join(notes)


def todo_id(v):
    """A stored to-do's id as Claude writes it in {"close": ...}: the integer 12 or the string "todo:12" (a "#12"
    is a task). None for anything else."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    m = isinstance(v, str) and re.fullmatch(r"todo:(\d+)", v.strip())
    return int(m.group(1)) if m else None


def todo_wait_from(wait, queued=None):
    """An item's wait, {"tasks": [ids], "until", "after": [indices into queued], "at": "tue 09:00"}, as
    (ids, until, wake_at); None for none. Raises ValueError for a task id that isn't there, an index out of range,
    or a time that is past or unreadable."""
    if not wait:
        return None
    if not isinstance(wait, dict):
        raise ValueError("wait is an object: tasks, until, after, at")
    tasks = wait.get("tasks") or []
    tasks = list(tasks) if isinstance(tasks, list) else [tasks]
    after = wait.get("after") or []
    for i in after if isinstance(after, list) else [after]:
        if queued is None:
            raise ValueError("after needs a queue block in the same reply")
        if not (isinstance(i, int) and 0 <= i < len(queued)):
            raise ValueError(f"after {i!r} is not a task the queue block queued (0 to {len(queued) - 1})"
                             if queued else f"after {i!r}: the reply queued no tasks")
        tasks.append(queued[i])
    return todo_wait_check(tasks, wait.get("until"), wait.get("at"))


def todo_own(b):
    """Your own to-do (POST /api/todos): a Do with source kind owner, parked when it comes with a wait (TODO-2)."""
    project, title = str(b.get("project") or ""), str(b.get("title") or "").strip()
    if project not in PROJECTS:
        raise ValueError(f"no project {project!r}" if project else "pick a project")
    if not title:
        raise ValueError("a to-do needs a title")
    if r := q("SELECT id FROM todos WHERE project=? AND title=? AND closed IS NULL", (project, title), one=True):
        raise ValueError(f"to-do todo:{r['id']} in {project} already has that title")
    wait = todo_wait_from(b.get("wait"))  # before the insert: a bad task id or time adds nothing
    tid = q("INSERT INTO todos (project, verb, title, why, source_kind, source_id, created) VALUES (?,?,?,?,?,?,?)",
            (project, "Do", title, str(b.get("why") or "").strip(), "owner", None, time.time()))
    parked = bool(wait and (wait[0] or wait[2]))
    if parked:
        todo_wait_set(f"todo:{tid}", *wait)
    return {"id": tid, "key": f"todo:{tid}", "parked": parked}


REMIND_WHY = "Pick up this chat"  # a reminder's why, which also tells it from the chat's other stored to-dos


TODO_QUEUE_ENDED = ("done", "cancelled")  # a to-do's task in these (or deleted) no longer blocks another Queue as task


def todo_queue(b):
    """Queue as task (POST /api/todos/queue {key, body?}): queue a stored Do as a task in its project, titled as the
    to-do, with body (default todo_brief) at the queue's defaults, and park the to-do on it until it merges (TODO-7).
    Its merge closes the to-do (todo_queued_merged); a fail or stop wakes it as any wait does. A to-do whose task is
    still under way can't be queued again."""
    key = todo_key(b.get("key"))
    m = re.fullmatch(r"todo:(\d+)", key.strip())
    if not m:
        raise ValueError("only a stored to-do (todo:N) can be queued as a task")
    tid = int(m.group(1))
    r = q("SELECT * FROM todos WHERE id=? AND closed IS NULL", (tid,), one=True)
    if not r:
        raise ValueError(f"no open to-do todo:{tid}")
    if r["verb"] != "Do":
        raise ValueError(f"todo:{tid} is {r['verb']}: only a Do can be queued as a task")
    if r["project"] not in PROJECTS:
        raise ValueError(f"no project {r['project']!r}")
    if live := todo_queued_live(r["task"]):
        raise ValueError(f"todo:{tid} is queued already as #{live}")
    note = q("SELECT note FROM todo_notes WHERE key=?", (f"todo:{tid}",), one=True)
    body = str(b.get("body") or "").strip() or todo_brief(tid, r["title"], r["why"], note and note["note"])
    task_id = add_task(r["project"], body, r["title"])
    q("UPDATE todos SET task=? WHERE id=?", (task_id, tid))
    todo_wait_set(f"todo:{tid}", [task_id], "merged")
    return {"id": task_id, "key": f"todo:{tid}"}


def todo_brief(tid, title, why, note=None):
    """The brief Queue as task suggests: the to-do's title, why and note, and where it came from (TODO-7)."""
    parts = [title.strip(), *([f"Why: {why.strip()}"] if (why or "").strip() else []),
             *([f"Note: {note.strip()}"] if (note or "").strip() else []), f"From to-do todo:{tid}."]
    return "\n\n".join(parts)


def todo_queued_live(task_id, states=None):
    """The to-do's task (todos.task) while it is under way, else None: done, cancelled or deleted frees the button.
    states: task id -> state already read, to skip the query."""
    if not task_id:
        return None
    st = (states or {}).get(task_id)
    if st is None and (t := q("SELECT state FROM tasks WHERE id=?", (task_id,), one=True)):
        st = t["state"]
    return task_id if st and st not in TODO_QUEUE_ENDED else None


def todo_queued_merged(task_id):
    """A task merged: close the to-dos Queue as task made it from (TODO-7)."""
    q("UPDATE todos SET closed=? WHERE task=? AND closed IS NULL", (time.time(), task_id))


def todo_remind(b):
    """Remind me (POST /api/todos/remind {project, ref}): put a topic, usually an ended one, back on your list as a Do
    linked to it, without reopening it (TODO-2). Picking the to-do reopens it, as its chat is its topic (TODAY-2). One
    per topic: while one is open, another call adds nothing and returns it, matched on the source, not the title."""
    project, ref = str(b.get("project") or ""), b.get("ref")
    if project not in PROJECTS:
        raise ValueError(f"no project {project!r}" if project else "pick a project")
    if ref is None or not str(ref).strip():
        raise ValueError("pick a topic")
    t = topic_ref(project, str(ref).strip())
    if r := todo_remind_open(project, t["id"]):
        return {"id": r["id"], "key": f"todo:{r['id']}", "added": False}
    research_ = t["mode"] == "research"
    title = (t["title"] or "").strip() or ("Untitled" if research_ else f"Chat #{t['id']}")
    tid = q("INSERT INTO todos (project, verb, title, why, source_kind, source_id, created) VALUES (?,?,?,?,?,?,?)",
            (project, "Do", title[:80], REMIND_WHY, "research" if research_ else "design", t["id"], time.time()))
    return {"id": tid, "key": f"todo:{tid}", "added": True}


def todo_remind_open(project, ref):
    """The open reminder of topic ref (a chat id or "r:<thread>") in project, or None."""
    return q("SELECT id FROM todos WHERE project=? AND source_kind IN ('design','research') AND source_id=? AND why=? "
             "AND closed IS NULL ORDER BY id LIMIT 1", (project, ref, REMIND_WHY), one=True)


def todo_remind_close(project, ref):
    """Ending a topic closes its reminder: you are done with it (TODO-3)."""
    q("UPDATE todos SET closed=? WHERE project=? AND source_kind IN ('design','research') AND source_id=? AND why=? "
      "AND closed IS NULL", (time.time(), project, ref, REMIND_WHY))


def todo_done(tid, project=None):
    """Close a stored to-do (Done, or {"close": id} from Claude). True when it was open."""
    return bool(q("UPDATE todos SET closed=? WHERE id=? AND closed IS NULL" + (" AND project=?" if project else "")
                  + " RETURNING id", (time.time(), tid, *([project] if project else []))))


def agree_open(did):
    """The [ops: ...] part naming chat did's open Agree to-dos, so Claude can close one the owner's reply settles
    (TODO-3). Empty when there are none."""
    rs = q("SELECT id,title FROM todos WHERE verb='Agree' AND source_kind IN ('design','research') AND source_id=? "
           "AND closed IS NULL ORDER BY id", (did,))
    return ["open Agree to-dos from this chat: " + "; ".join(f"todo:{r['id']} {r['title']}" for r in rs)] if rs else []


def todos_from_reply(p, reply, did, queued=None):
    """Store the reply's ```todo blocks. queued: the ids its queue blocks queued, for a wait's "after". Returns
    (reply without them, note for Claude)."""
    notes = []
    for block in TODO_BLOCK.findall(reply):
        try:
            items = json.loads(block)
        except ValueError as e:
            notes.append(f"your last todo block was not valid JSON ({e}); nothing from it was added")
            continue
        notes.append(todo_add(p.name, items, "design", did, queued))
    return TODO_BLOCK.sub("", reply).strip(), "; ".join(n for n in notes if n)


def chat_from(t):
    """The design chat that queued a task, or its parent (a split), for the Today view's conversation. None for a
    task queued from the dashboard or the command line."""
    for tid in (t["id"], t["parent"]):
        if tid and (r := q("SELECT m.design_id FROM design_msgs m, json_each(m.task_ids) j "
                           "WHERE m.task_ids IS NOT NULL AND j.value=? ORDER BY m.id LIMIT 1", (tid,), one=True)):
            return r["design_id"]
    return None


TODO_WAKE = re.compile(r"#(\d+)|\btask (\d+)", re.I)
TODO_WAITING = ("queued", "running", "gating", "merging")  # a parked item waiting on such a task stays parked
TODO_WAKE_WORD = {"failed": "failed", "needs_input": "needs you", "review": "in review", "paused": "paused",
                  "cancelled": "cancelled"}  # a task that stops short of the target: it wakes the item at once
TODO_UNTIL = ("merged", "released")
TODO_MAIN = {}  # project -> (main's tip, {merge sha: in main}): the released check, kept until main moves


def todo_wake_task(note):
    """The task a note waits on: its first #N or "task N" (TODO-6), else None."""
    m = TODO_WAKE.search(note or "")
    return int(m.group(1) or m.group(2)) if m else None


def todo_key(key):
    if not str(key or "").strip():
        raise ValueError("no to-do key")
    return str(key)


def todo_wake_at(at, now=None):
    """A wait's time, as Start later reads it (start_time, without "reset"): its epoch, or None for none. ValueError
    for a time in the past or one it can't read."""
    if at is None or (isinstance(at, str) and not at.strip()):
        return None
    if isinstance(at, str) and at.strip().lower() == "reset":
        raise ValueError(f"at is a time, not reset: use {START_FORMS}")
    try:
        return start_time(at, now)[0]
    except ValueError as e:
        raise ValueError("at" + str(e).removeprefix("start")) from None


def todo_wait_check(tasks, until=None, at=None, now=None):
    """A wait from the picker, Claude or POST /api/todos: (task ids, until, wake_at). Raises ValueError naming a
    task id that isn't there, or for a bad time (todo_wake_at). until "released" on tasks all in straight-to-main
    projects is stored as merged (TODO-6)."""
    until = until or "merged"
    if until not in TODO_UNTIL:
        raise ValueError(f"until is merged or released, not {until!r}")
    ids = []
    for x in tasks if isinstance(tasks, list) else [tasks]:
        try:
            n = int(str(x).strip().lstrip("#"))
        except ValueError:
            raise ValueError(f"{x!r} is not a task id") from None
        if n not in ids:
            ids.append(n)
    rows = {r["id"]: r["project"] for r in q(f"SELECT id, project FROM tasks WHERE id IN ({','.join('?' * len(ids))})",
                                             ids)} if ids else {}
    if missing := [n for n in ids if n not in rows]:
        raise ValueError(f"no task {', '.join(f'#{n}' for n in missing)}")
    if until == "released" and ids and all(getattr(PROJECTS.get(rows[n]), "direct_main", False) for n in ids):
        until = "merged"  # straight to main: a merge moves main, so merged is released
    return ids, until, todo_wake_at(at, now)


def todo_wait_set(key, ids, until, wake_at=None, now=None):
    """Store an explicit wait and park the item; no ids and no time clears the wait and unparks it."""
    now = now or time.time()
    if not ids and not wake_at:
        q("UPDATE todo_notes SET wake_tasks=NULL, wake_on='merged', wake_at=NULL, wake_set=0, parked=NULL, "
          "skipped=NULL, updated=? WHERE key=?", (now, key))
        q("DELETE FROM todo_notes WHERE key=? AND note='' AND topic IS NULL", (key,))
        return {"parked": False}
    q("INSERT INTO todo_notes (key, note, parked, wake_tasks, wake_on, wake_at, wake_set, updated) "
      "VALUES (?,'',?,?,?,?,1,?) ON CONFLICT(key) DO UPDATE SET parked=COALESCE(parked, excluded.parked), "
      "skipped=NULL, wake_tasks=excluded.wake_tasks, wake_on=excluded.wake_on, wake_at=excluded.wake_at, "
      "wake_set=1, updated=excluded.updated", (key, now, json.dumps(ids), until, wake_at, now))
    return {"parked": True, "wake_tasks": ids, "wake_on": until, "wake_at": wake_at}


def todo_wait(key, tasks, until=None, at=None):
    """Wait for… (POST /api/todos/wait): park the item until tasks reach until and the time at comes; tasks []
    and no at clears it (TODO-6)."""
    key = todo_key(key)
    return todo_wait_set(key, *todo_wait_check(tasks or [], until, at))


def todo_note(key, note):
    """Save your note on a to-do (POST /api/todos/note). A parked item stays parked, even with no note. Without an
    explicit wait, the note's first task number is the wait; with one, the note never changes it."""
    key = todo_key(key)
    note = str(note or "").strip()
    n = todo_wake_task(note)
    q("INSERT INTO todo_notes (key, note, wake_tasks, updated) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
      "note=excluded.note, updated=excluded.updated, wake_tasks=CASE WHEN wake_set=1 THEN wake_tasks "
      "ELSE excluded.wake_tasks END, wake_on=CASE WHEN wake_set=1 THEN wake_on ELSE 'merged' END",
      (key, note, json.dumps([n]) if n else None, time.time()))
    q("DELETE FROM todo_notes WHERE key=? AND note='' AND parked IS NULL AND skipped IS NULL AND topic IS NULL", (key,))


def todo_skip(key, now=None):
    """Skip for now (POST /api/todos/skip): park the item when it has a note or an explicit wait, else move it
    last until midnight."""
    key = todo_key(key)
    now = now or time.time()
    r = q("SELECT note, wake_set FROM todo_notes WHERE key=?", (key,), one=True)
    if r and ((r["note"] or "").strip() or r["wake_set"]):
        q("UPDATE todo_notes SET parked=?, skipped=NULL, updated=? WHERE key=?", (now, now, key))
        return {"parked": True}
    q("INSERT INTO todo_notes (key, note, skipped, updated) VALUES (?,'',?,?) ON CONFLICT(key) DO UPDATE SET "
      "skipped=excluded.skipped, updated=excluded.updated", (key, now, now))
    return {"skipped": True}


def todo_unpark(key):
    """Unpark (POST /api/todos/unpark): back into the list at its normal place, not skipped."""
    q("UPDATE todo_notes SET parked=NULL, skipped=NULL, updated=? WHERE key=?", (time.time(), key))
    q("DELETE FROM todo_notes WHERE key=? AND note='' AND topic IS NULL", (key,))


def todo_wake_tasks(r):
    """A todo_notes row's wait: its task ids, [] for none or a row edited by hand."""
    try:
        ids = json.loads(r["wake_tasks"] or "[]")
    except ValueError:
        return []
    return [n for n in ids if isinstance(n, int)] if isinstance(ids, list) else []


def todo_in_main(name, main, shas):
    """Which of shas (merge commits of project name) main, its tip revs from the facts, contains. Kept per project
    against that tip: no git call until main moves, then one rev-list per repo for the shas not yet known."""
    p = PROJECTS.get(name)
    if not p or not main or not shas:
        return set()
    tip, known = TODO_MAIN.get(name) or (None, {})
    if tip != main:
        tip, known = main, {}
        TODO_MAIN[name] = (tip, known)
    if new := [s for s in dict.fromkeys(shas) if s not in known]:
        try:
            found = {}
            for r, head in p.pairs(main):  # a commit not in main is one rev-list lists; one missing here is skipped
                out = set(r.git("rev-list", "--ignore-missing", *new, "--not", head).split())
                for s in new:
                    if s not in out and (len(p.parts) == 1 or r.has(s)):
                        found[s] = True
            known.update((s, found.get(s, False)) for s in new)
        except Exception:
            pass  # git failed: ask again next build rather than keep a wrong answer
    return {s for s in shas if known.get(s)}


def todo_wake(r, info, facts_of, now=None):
    """Whether a parked row's wait is over, and the words for its note: None while it waits, else
    "#283 merged, #284 failed" or "woke at Tue 09:00". Tasks and a time: both must be met, but a task that stops
    short wakes it at once. info: task id -> {state, merged, project}, None for one deleted."""
    ids, until = todo_wake_tasks(r), r["wake_on"] if r["wake_on"] in TODO_UNTIL else "merged"
    at = r["wake_at"]
    if not ids and not at:
        return None
    now = time.time() if now is None else now
    words, short, waiting = [], False, False
    for n in ids:
        t = info.get(n)
        st = t["state"] if t else None
        if st is None or st in TODO_WAKE_WORD:
            short = True
            words.append(f"#{n} {TODO_WAKE_WORD.get(st, 'gone')}")
        elif st == "done" and not t["merged"]:
            words.append(f"#{n} done")  # nothing merged, so nothing to wait for
        elif st == "done" and until == "merged":
            words.append(f"#{n} merged")
        elif st == "done":
            f = facts_of.get(t["project"]) or {}
            shas = t["merged"].split()
            if f.get("direct_main") or todo_in_main(t["project"], f.get("main_sha"), shas) >= set(shas):
                words.append(f"#{n} released")
            else:
                waiting = True
                words.append(f"#{n} merged")
        else:
            waiting = True
            words.append(f"#{n} {st}")
    if short:
        return ", ".join(words)
    if waiting or (at and now < at):
        return None
    return " · ".join(w for w in (", ".join(words), at and f"woke at {todo_clock(at)}") if w)


def todo_clock(ts):
    """A wait's time in a note: Tue 09:00, or 2026-11-03 09:00 when it is a week or more away from now."""
    near = abs(ts - time.time()) < 6.5 * 86400
    return time.strftime("%a %H:%M" if near else "%Y-%m-%d %H:%M", time.localtime(ts))


def todo_notes_apply(out, tasks, projects, now):
    """Put your notes, skips and parks on the to-do list (TODO-6), wake parked items whose wait is over, drop
    rows whose item is gone, and order it: unparked, then skipped in skip order, then parked."""
    rows = {r["key"]: r for r in q("SELECT * FROM todo_notes")}
    if not rows:
        return [{**x, "note": ""} for x in out]
    keys = {x["key"] for x in out}
    unknown = {f["name"] for f in projects if f.get("error")}  # its items are missing only because it failed to load
    facts_of = {f["name"]: f for f in projects if not f.get("error")}
    midnight = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    info = {t["id"]: t for t in tasks}
    want = list({n for r in rows.values() if r["parked"] for n in todo_wake_tasks(r) if n not in info})
    if want:  # older than the tasks state_payload read: one query for them all
        info.update((t["id"], t) for t in q(
            f"SELECT id, state, merged, project FROM tasks WHERE id IN ({','.join('?' * len(want))})", want))
    for k, r in list(rows.items()):
        if k not in keys:
            if not (k.startswith("project:") and k.split(":")[1] in unknown):
                q("DELETE FROM todo_notes WHERE key=?", (k,))
            rows.pop(k)
            continue
        if r["parked"] and (said := todo_wake(r, info, facts_of, now)):
            note = f"{r['note'] or ''} · {said}".strip(" ·")
            q("UPDATE todo_notes SET note=?, parked=NULL, skipped=NULL, updated=? WHERE key=?", (note, now, k))
            r = rows[k] = {**r, "note": note, "parked": None, "skipped": None}
        if r["skipped"] and r["skipped"] < midnight:
            r = rows[k] = {**r, "skipped": None}
        if not (r["note"] or r["parked"] or r["skipped"] or r["topic"]):  # a bond stays (TODAY-2)
            q("DELETE FROM todo_notes WHERE key=?", (k,))
    main, skipped, parked = [], [], []
    for x in out:
        r = rows.get(x["key"]) or {}
        x = {**x, "note": r.get("note") or ""}
        if r.get("parked"):
            ids = todo_wake_tasks(r)
            parked.append({**x, "parked": {"since": r["parked"], "wake_tasks": ids,
                                           "wake_on": r["wake_on"] if r["wake_on"] in TODO_UNTIL else "merged",
                                           "wake_at": r["wake_at"],
                                           "wake_task": ids[0] if ids else None}})  # wake_task: for one release
        elif r.get("skipped"):
            skipped.append({**x, "skipped": r["skipped"]})
        else:
            main.append(x)
    return (main + sorted(skipped, key=lambda x: x["skipped"])
            + sorted(parked, key=lambda x: x["parked"]["since"]))


class TopicIndex:
    """What todo_topic looks topics up in, read once per list: every chat row, and a project's research threads only
    when a ref names one."""
    def __init__(self):
        self.chats = {d["id"]: d for d in q("SELECT id, project, subject, ended, created FROM designs")}
        self.latest = {}  # (project, subject): the latest chat about it, open or ended
        for d in sorted(self.chats.values(), key=lambda d: (d["created"] or 0, d["id"])):
            if d["subject"]:
                self.latest[(d["project"], d["subject"])] = d
        self.rs = {}

    def get(self, project, ref):
        """(ref, ended) of the topic ref names in project, or None."""
        ref = int(ref) if str(ref).isdigit() else ref
        if isinstance(ref, int):
            d = self.chats.get(ref)
            return (d["id"], d["ended"]) if d and d["project"] == project else None
        if isinstance(ref, str) and ref.startswith("r:"):
            if project not in self.rs:
                try:
                    self.rs[project] = {t["id"]: t["ended"] for t in research_topics(project)}
                except Exception:  # the research repo is gone or unreadable: no research topics
                    self.rs[project] = {}
            return (ref, self.rs[project][ref]) if ref in self.rs[project] else None
        return None


def todo_topic(project, source, subject, bond, idx):
    """A to-do's topic (README TODAY-2, a to-do's topic), as (ref, ended), or (None, None): its stored bond, else the
    chat it came from, else the latest topic about its subject, open or ended."""
    source = source or {}
    for ref in (bond, source.get("id") if source.get("kind") == "chat" else None):
        if ref is not None and (hit := idx.get(project, ref)):
            return hit
    d = idx.latest.get((project, subject)) if subject else None
    return (d["id"], d["ended"]) if d else (None, None)


def todo_topics_apply(out):
    """Put each item's topic on it: topic, the ref, and topic_ended, when that topic ended (both None for none)."""
    bonds = {r["key"]: r["topic"] for r in q("SELECT key, topic FROM todo_notes WHERE topic IS NOT NULL")}
    idx = TopicIndex()
    res = []
    for x in out:
        ref, ended = todo_topic(x["project"], x.get("source"), x.get("subject"), bonds.get(x["key"]), idx)
        res.append({**x, "topic": ref, "topic_ended": ended})
    return res


def todo_source(key):
    """(project, source, subject) of the to-do with this key, as todos_list builds it, from the key alone; None for
    a key it doesn't know. For conv_say, which gets only the key of the to-do a message was sent with."""
    parts = str(key or "").split(":")
    if parts[0] == "task" and len(parts) >= 2 and parts[1].isdigit():
        t = q("SELECT project FROM tasks WHERE id=?", (int(parts[1]),), one=True)
        return (t["project"], {"kind": "task", "id": int(parts[1])}, f"task:{parts[1]}") if t else None
    if parts[0] == "project" and len(parts) == 3:
        return parts[1], {"kind": "project", "id": parts[1]}, key
    if parts[0] == "chat" and len(parts) >= 2 and parts[1].isdigit():
        d = q("SELECT project FROM designs WHERE id=?", (int(parts[1]),), one=True)
        return (d["project"], {"kind": "chat", "id": int(parts[1])}, None) if d else None
    if parts[0] == "todo" and len(parts) == 2 and parts[1].isdigit():
        r = q("SELECT * FROM todos WHERE id=?", (int(parts[1]),), one=True)
        if not r:
            return None
        kind = "chat" if r["source_kind"] in ("design", "research") else r["source_kind"]  # as todos_list has it
        return r["project"], {"kind": kind, "id": r["source_id"]}, f"task:{r['source_id']}" if kind == "task" else None
    return None


def todo_bond(key, ref):
    """A message was sent with this to-do attached and landed in topic ref (conv_say): store the to-do's topic for
    good, unless it has a bond already. The bond is the topic it had (its chat, or its subject's), else ref, so it
    never changes after the first send. Returns the bond, or None for a key it doesn't know."""
    src = todo_source(key)
    if not src:
        return None
    project, source, subject = src
    row = q("SELECT topic FROM todo_notes WHERE key=?", (key,), one=True)
    if row and row["topic"]:
        return row["topic"]
    had = todo_topic(project, source, subject, None, TopicIndex())[0]
    bond = str(had if had is not None else ref)
    q("INSERT INTO todo_notes (key, note, topic, updated) VALUES (?,'',?,?) ON CONFLICT(key) DO UPDATE SET "
      "topic=COALESCE(todo_notes.topic, excluded.topic)", (key, bond, time.time()))
    return bond


def todos_list(tasks, projects, last, now=None, notes=True):
    """The to-do list for /api/state, from what state_payload already read: its tasks (with last, their latest
    events), its projects (main_info, so no git call here), and two small queries for chats and stored items.
    Your notes, skips and parks go on last (TODO-6), then each item's topic (todo_topics_apply); now is the clock,
    for tests. notes=False: a partial list (subject_about's), which must not touch todo_notes: its cleanup would
    drop the rows of every item the partial list leaves out."""
    out = []
    for t in tasks:
        k, st = t["kind"], t["state"]
        since = (last.get(t["id"]) or {}).get("ts") or t["finished"] or t["started"] or t["created"]

        def item(key, verb, why, t=t, since=since):
            return todo_item(key, verb, t["project"], t["title"], why, since, "task", t["id"])
        if st == "needs_input":
            if k in ("release", "deploy"):
                out.append(item(f"task:{t['id']}:promote", "Promote", why=t["question"]))
            else:
                out.append(item(f"task:{t['id']}:answer", "Answer", why=t["question"]))
        elif st == "failed":
            out.append(item(f"task:{t['id']}:fix", "Fix", why=t["error"]))
        elif st == "done" and t["merged"] and (rs := t.get("research")) and not rs.get("read"):  # RES-5
            out.append({**item(f"task:{t['id']}:report", "Read", why=f"Research report {rs.get('path')} merged into "
                               f"{getattr(PROJECTS.get(t['project']), 'dev', 'dev')}"), "report": {"path": rs.get("path")}})
        elif st == "review":
            p = PROJECTS.get(t["project"])
            if p and p.launch:  # can_play in GET /api/tasks/<id>
                out.append(item(f"task:{t['id']}:try", "Try", why="Watch its proof video, then approve or send it back"
                                if proof_files(t["id"])[0].is_file() else "Play it, then approve or send it back"))
            else:
                out.append(item(f"task:{t['id']}:review", "Review", why="Read the diff and summary, then approve"))
        if st == "done" and t["merged"]:  # the reports it listed (RES-10), next to RES-5's own
            pj = PROJECTS.get(t["project"])
            dev = getattr(pj, "main" if is_branch_task(t) else "dev", "dev")  # a branch task's PR merged into main
            out += [{**item(f"task:{t['id']}:report:{x['path']}", "Read", why=f"Report {x['path']} merged into {dev}"),
                     "report": {"path": x["path"]}} for x in reports_of(t) if not x.get("read")]
    for f in projects:
        if f.get("error"):
            continue
        name = f["name"]
        rel = f.get("release")
        if f.get("ahead") and not f.get("auto_release") and not (rel and rel["state"] not in ENDED) and \
                f.get("flow") != "pr":  # flow "pr": no Promote; dev follows main
            n = len(f["ahead"])
            out.append(todo_item(f"project:{name}:promote", "Promote", name, f"Promote {f['title']}",
                                 f"{f['dev_branch']} is {n}{'+' if n >= 30 else ''} change{'s' if n != 1 else ''} "
                                 f"ahead of {f['main_branch']}",
                                 min((x["at"] for x in f.get("dev_log") or []), default=0), "project", name))
        if f.get("main_since_kind") == "deploy" and f.get("main_log") and not f.get("deploy_open"):  # see main_since
            n = len(f["main_log"])
            out.append(todo_item(f"project:{name}:ship", "Ship", name, f"Deploy {f['title']}",
                                 f"{f['main_branch']} has {n} change{'s' if n != 1 else ''} since the last deploy",
                                 min(x["at"] for x in f["main_log"]), "project", name))
        if f.get("uncommitted"):
            n = sum(u["count"] for u in f["uncommitted"])
            out.append(todo_item(f"project:{name}:commit", "Commit", name,
                                 f"Review {n} uncommitted {files(n)} in {f['title']}", commit_why(f["uncommitted"]),
                                 min((u["oldest"] for u in f["uncommitted"] if u["oldest"]), default=0),
                                 "project", name))
    # Every chat you haven't ended waits on you once Claude is done replying: read its new reply, or answer it or
    # end it. An ended chat is done: a reply that lands in it later doesn't bring it back (topics() agrees), nor does
    # a dashboard that still lists it opening it (conv_open reopens only what the caller saw ended).
    # A chat with an open reminder (Remind me) is on the list once, as that Do: no Read item while it is open.
    reminded = {r["source_id"] for r in q("SELECT source_id FROM todos WHERE closed IS NULL AND source_kind='design' "
                                          "AND why=?", (REMIND_WHY,))}
    for d in q(f"SELECT * FROM (SELECT {CHAT_COLS} FROM designs WHERE ended IS NULL) WHERE state!='thinking'"):
        if d["id"] in reminded:
            continue
        unread = (d["replied"] or 0) > (d["seen"] or 0)
        out.append(todo_item(f"chat:{d['id']}:read", "Read", d["project"], d["title"] or f"Chat #{d['id']}",
                             "Claude replied" if unread else "Open: reply, or end it when you are done",
                             d["replied"] if unread else max(d["seen"] or 0, d["replied"] or 0, d["updated"] or 0),
                             "chat", d["id"], unread=unread))
    states = {t["id"]: t["state"] for t in tasks}
    for r in q("SELECT * FROM todos WHERE closed IS NULL"):
        x = todo_item(f"todo:{r['id']}", r["verb"], r["project"], r["title"], r["why"], r["created"],
                      "chat" if r["source_kind"] in ("design", "research") else r["source_kind"], r["source_id"],
                      todo=r["id"])
        if r["verb"] == "Do" and (live := todo_queued_live(r["task"], states)):  # Queue as task (TODO-7)
            x = {**x, "queued": live, "closes": f"Closes when #{live} merges, or when you click Done."}
        out.append(x)
    out.sort(key=lambda x: (TODO_ORDER.index(x["group"]), x["since"], x["key"]))
    if not notes:
        return out
    return todo_topics_apply(todo_notes_apply(out, tasks, projects, now or time.time()))


# ---------------------------------------------------------------- http

def state_payload():
    tasks = q("SELECT id,project,title,state,phase,priority,hold,depends_on,parent,created,started,"
              "finished,cost,rounds,summary,question,error,merged,branch,kind,auto_release,origin,paused_by,start_at,"
              "model,session_model,effort,research,reports,not_before,not_before_why,review,jira,target FROM tasks "
              "ORDER BY id DESC LIMIT 500")
    prs = {}
    for x in q("SELECT * FROM prs WHERE task_id IN (SELECT id FROM tasks ORDER BY id DESC LIMIT 500) "
               "ORDER BY task_id, seq, id"):
        prs.setdefault(x["task_id"], []).append(x)
    # What each task is doing right now, for the status line under it in the list.
    # Per listed task, one index lookup (events_task): a GROUP BY over every event scanned the whole table.
    last = {r["task_id"]: r for r in q(
        "SELECT t.id AS task_id, e.text, e.ts FROM (SELECT id FROM tasks "
        "ORDER BY id DESC LIMIT 500) t JOIN events e ON e.id = "
        "(SELECT MAX(id) FROM events WHERE task_id = t.id)")}
    s = settings_now()
    waits = wait_reasons(tasks, s)
    for t in tasks:
        t["summary"] = list_summary(t)  # the list shows one line; the task page has the rest
        for k in ("question", "error"):
            t[k] = clip(t[k], 400) or None
        ev_ = last.get(t["id"])
        t["last_event"] = {"text": ev_["text"], "ts": ev_["ts"]} if ev_ else None
        t["phase_since"] = PHASE_SINCE.get(t["id"]) if t["state"] in ACTIVE else None
        t["step"] = last_step(t["id"]) if t["state"] == "running" and t["phase"] == "claude working" else None
        t["wait"] = waits.get(t["id"])
        t["prs"] = pr_view_rows(t, prs.get(t["id"])) if is_branch_task(t) else None
        t["start_words"] = start_words(t) if t["state"] == "queued" else None
        t["origin"] = origin_view(t["origin"])
        t["research"] = research_of(t)
        t["reports"] = reports_of(t) or None
        t["proof"] = proof_files(t["id"])[0].is_file()  # GET /api/tasks/<id>/proof.mp4 plays it
        t["proof_note"] = proof_note(t["id"]) if t["proof"] else None
        t["proof_watch"] = proof_watch(t["id"]) if t["proof"] else None
        t["proof_report"] = proof_report(t["id"])  # GET /api/tasks/<id>/proof/report.md and its media
        t["proof_clip"] = proof_clip(t["id"]) if t["proof_report"] and not t["proof"] else None
        t["review"] = review_of(t)
    projects = []
    for p in PROJECTS.values():
        try:
            projects.append(main_info(p, s))
        except Exception as e:
            projects.append({"name": p.name, "title": p.title, "error": str(e)})
    projects += [{"name": n, "title": t, "error": why} for n, (t, why) in BROKEN.items()]
    return {
        "tasks": tasks, "now": time.time(), "projects": projects, "archived": archived_projects(),
        "settings": {k: s.get(k, "") for k in SETTING_KEYS},
        "limit": limit_info(s), "usage": usage_info(s),
        "active": len(THREADS), "data_dir": str(DATA),
        "pid": os.getpid(),  # the Restart button waits for a new one
        "running_deploys": [t["id"] for t in running_deploys()],  # what the Restart button would cut off
        "research": research.state(),  # its own count: research runs take no task slot
        "design_cost": (q("SELECT SUM(cost) AS c FROM designs", one=True) or {}).get("c") or 0,
        "chats": chat_counts(), "todos": todos_list(tasks, projects, last), "schedules": schedules_view()}


STATE_CACHE = {"at": 0.0, "data": None, "busy": False, "gen": 0}
STATE_TTL = 1.0  # seconds a built /api/state answers requests; every open tab polls it every few seconds
STATE_LOCK = threading.Lock()


def state_bytes():
    """The /api/state body. A request never waits on a rebuild once there is one to show: it gets the last body at
    once and, when that is older than STATE_TTL, starts one rebuild behind it (what it shows is at most a poll old)."""
    c = STATE_CACHE
    if c["data"] is not None and time.time() - c["at"] < STATE_TTL:
        return c["data"]
    with STATE_LOCK:
        if c["busy"] and c["data"] is not None:
            return c["data"]
        stale = c["data"]
        c["busy"] = True

    def build():
        gen = c["gen"]
        try:
            data = json.dumps(state_payload()).encode()
            if c["gen"] == gen:  # an action since it started: what it read may be from before, so don't keep it
                c["data"], c["at"] = data, time.time()
            return data
        finally:
            c["busy"] = False
    if stale is None:
        return build()  # nothing to show yet: build it now
    threading.Thread(target=build, daemon=True).start()
    return stale


def state_changed():
    """An action changed what /api/state shows: the next request builds it afresh, and a build already under way,
    which may have read the database before the change, is not kept (a Read step came back after Open report)."""
    STATE_CACHE["gen"] += 1
    STATE_CACHE["data"] = None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_video(self, f, ctype="video/mp4", headers=None):
        """A video (or any file), whole or the byte range the Range header asks for: Safari plays a <video> only
        from a server that answers Range requests. 404 when there is no file."""
        try:
            fh = open(f, "rb")
        except OSError:
            return self.send(404, {"error": "no proof video"})
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
            ranged = bool(m and (m.group(1) or m.group(2)))  # anything else is ignored: the whole file
            if ranged:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # bytes=-N: the last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    return self.send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}", **(headers or {})})
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Accept-Ranges", "bytes")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := fh.read(min(left, 1 << 16))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player seeks by dropping a request and asking for another range

    def host_ok(self):
        """Refuse DNS rebinding: a web page whose own domain is re-pointed at 127.0.0.1 could otherwise
        drive this API, and so start work on your machine, from your browser."""
        host = (self.headers.get("Host") or "").lower()
        host = host[1:host.find("]")] if host.startswith("[") else host.split(":")[0]
        allowed = {"127.0.0.1", "localhost", "::1", HOST.lower()} | {
            h.strip().lower() for h in os.environ.get("OPS_ALLOWED_HOSTS", "").split(",") if h.strip()}
        return host in allowed or host.endswith(".ts.net")  # Tailscale serve, tailnet-only

    def timed(self, handler):
        """Run a request handler and log it when it takes over SLOW_MS, with the time it spent waiting for
        the database lock and in queries, so a slow page says where its time went."""
        SQLT.t = [0.0, 0.0]
        t0 = time.time()
        try:
            handler()
        finally:
            ms = (time.time() - t0) * 1000
            if ms > SLOW_MS:
                wait, run = SQLT.t
                print(f"slow request {self.command} {self.path} {ms:.0f} ms "
                      f"(db lock wait {wait * 1000:.0f} ms, queries {run * 1000:.0f} ms)", flush=True)

    def do_GET(self):
        self.timed(self.get)

    def do_POST(self):
        state_changed()  # an action changes what /api/state shows: the next poll builds it afresh
        self.timed(self.post)
        state_changed()

    def get(self):
        if not self.host_ok():
            return self.send(403, {"error": "unknown Host header"})
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, (CODE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/mcp/"):  # the permission server answers POSTs only: no event stream
            return self.send(405, {"error": "POST only"})
        if path == "/api/state" and not READY.is_set():  # the dashboard shows "starting up" until this goes
            return self.send(200, {"starting": True, "now": time.time(), "data_dir": str(DATA)})
        if path == "/api/state":
            return self.send(200, state_bytes())
        if path.startswith("/api/research/"):
            return self.send(*research.get(path, urlparse(self.path).query))
        m = re.fullmatch(r"/api/tasks/(\d+)", path)
        if m:
            tid = int(m.group(1))
            t = task(tid)
            if not t:
                return self.send(404, {"error": "no such task"})
            t["events"] = q("SELECT ts,text FROM events WHERE task_id=? ORDER BY id", (tid,))
            t["origin"] = origin_view(t["origin"])
            t["activity"] = activity(tid)
            p = PROJECTS.get(t["project"])
            t["diffstat"] = diffstat(p, t) if p and t["branch"] else ""
            if p:  # the commits with their repo names, for the line under the title
                t["merged_show"] = p.label(t["merged"])
                t["base_show"] = p.label(t["base"])
            t["can_play"] = bool(p and p.launch and t["state"] == "review")
            t["proof"] = proof_files(tid)[0].is_file()
            t["proof_note"] = proof_note(tid) if t["proof"] else None
            t["proof_watch"] = proof_watch(tid) if t["proof"] else None
            t["proof_report"] = proof_report(tid)
            t["proof_clip"] = proof_clip(tid) if t["proof_report"] and not t["proof"] else None
            t["review"] = review_of(t)
            t["start_words"] = start_words(t) if t["state"] == "queued" else None
            t["wait"] = wait_reasons([t]).get(tid) if t["state"] == "queued" else None
            t["chat_from"] = chat_from(t)
            t["chat"] = q("SELECT role,text,ts,delivered FROM task_msgs WHERE task_id=? ORDER BY id", (tid,))
            t["chat_live"] = TASK_CHAT_LIVE.get(tid)
            t["can_chat"] = bool(t["session_id"]) and t["state"] in CHAT_STATES
            t["chat_full"] = t["can_chat"] and chat_full(t)  # full tools in its worktree, or read-only after a merge
            t["steer_live"] = tid in STEER_PIPES  # the worker reads a message after its current step
            t["can_steer"] = t["state"] in STEER_STATES and (t["kind"] in MERGES or t["steer_live"])
            if t["kind"] == "deploy":  # its command's output
                t["deploy"] = json.loads(t["deploy"] or "{}")
                t["log"] = log_tail(RUNS / str(tid) / "deploy.log", 8000)
            return self.send(200, t)
        m = re.fullmatch(r"/api/tasks/(\d+)/proof\.mp4", path)
        if m:
            return self.send_video(proof_files(int(m.group(1)))[0])
        m = re.fullmatch(r"/api/tasks/(\d+)/proof/(.+)", path)  # the proof report and its media; flat names only
        if m:
            f, why = proof_path(int(m.group(1)), unquote(m.group(2)))
            if not f:
                return self.send(404, {"error": why})
            ctype, csp = FILE_TYPES.get(f.suffix.lower(), FILE_TEXT), {
                "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups"}
            if f.suffix.lower() in PROOF_MEDIA:  # by byte range, so a <video> plays and seeks
                return self.send_video(f, ctype, csp)
            return self.send(200, f.read_bytes(), ctype, csp)
        m = re.fullmatch(r"/api/designs/(\d+)/files/([\w.-]+)", path)  # a design chat's document; no / in a name
        if m:
            f = CHAT_FILES / m.group(1) / m.group(2)
            if m.group(2).startswith(".") or not f.is_file():
                return self.send(404, {"error": "no such file"})
            # sandboxed: a prototype's scripts run, but in an origin of their own, so they can't drive the dashboard
            return self.send(200, f.read_bytes(), FILE_TYPES.get(f.suffix.lower(), FILE_TEXT),
                             {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups"})
        if path == "/api/files":  # a repo file as on the project's dev worktree, for a link in a chat
            qs = parse_qs(urlparse(self.path).query)
            p = PROJECTS.get(qs.get("project", [""])[0])
            f, why = serve_repo_file(p, qs.get("path", [""])[0]) if p else (None, "unknown project")
            if not f:
                return self.send(404, {"error": why})
            ctype, csp = FILE_TYPES.get(f.suffix.lower(), FILE_TEXT), {
                "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups"}
            if f.suffix.lower() in MEDIA:  # by byte range, so a <video> plays and seeks
                return self.send_video(f, ctype, csp)
            return self.send(200, f.read_bytes(), ctype, csp)
        m = re.fullmatch(r"/api/images/([\w-]+(\.\w+))", path)
        if m and m.group(2) in IMAGE_TYPES.values() and (IMAGES / m.group(1)).is_file():
            ctype = next(k for k, v in IMAGE_TYPES.items() if v == m.group(2))
            return self.send(200, (IMAGES / m.group(1)).read_bytes(), ctype)
        if path == "/api/analyze":  # the Analyze tab: ?period=1|7|30|all&project=<name>[,<name>…]
            qs = parse_qs(urlparse(self.path).query)
            return self.send(200, analysis(qs.get("period", ["7"])[0], qs.get("project", [""])[0]))
        m = re.fullmatch(r"/api/projects/([\w-]+)/(research|documents|uncommitted|recap)", path)  # TODAY-4; the Commit card; a home's recap
        if m:
            try:
                return self.send(200, {"research": project_research, "documents": project_documents,
                                       "uncommitted": project_uncommitted, "recap": project_recap}[m.group(2)](m.group(1)))
            except ValueError as e:
                return self.send(404, {"error": str(e)})
        if path == "/api/conversation":  # the Today view's conversation: ?project=<name>
            try:
                return self.send(200, conv_view(parse_qs(urlparse(self.path).query).get("project", [""])[0]))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        if path == "/api/designs":
            chats = q(f"SELECT {CHAT_COLS},origin FROM designs ORDER BY updated DESC")
            for c in chats:
                c["origin"] = origin_view(c["origin"])
            return self.send(200, chats)
        if path == "/api/designs/quick":  # the quick chat's Recent list (README: TODAY-6)
            limit = parse_qs(urlparse(self.path).query).get("limit", ["10"])[0]
            return self.send(200, quick_topics(int(limit) if limit.isdigit() else 10))
        if path == "/api/topics/search":  # the message box's @ picker (README: LINK-8)
            a = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            ex = a.get("exclude", "")
            return self.send(200, topic_search(a.get("q", ""), a.get("project", ""), int(ex) if ex.isdigit() else None))
        if path == "/api/openspec":  # open changes, the repos that use OpenSpec and those that don't, and drafts
            out = {"changes": [], "repos": [], "bare": [], "cli": bool(shutil.which("openspec")),
                   "drafts": q("SELECT id,project,title,spec_sub AS sub,state FROM designs WHERE spec_draft=1 "
                               "AND spec IS NULL AND ended IS NULL ORDER BY id")}
            for p in PROJECTS.values():
                try:
                    out["repos"] += spec_repos(p)
                    out["bare"] += spec_bare(p)
                    out["changes"] += spec_changes(p)
                except Exception as e:
                    print(f"project {p.name} openspec: {e}", flush=True)
            return self.send(200, out)
        m = re.fullmatch(r"/api/designs/(\d+)", path)
        if m:
            did = int(m.group(1))
            d = design(did)
            if not d:
                return self.send(404, {"error": "no such chat"})
            p = PROJECTS.get(d["project"])
            d["cwd"] = str((p.repo if d["kind"] == "project" else p.dev_wt) if p else "")
            if p and spec_chat(d):
                d["cwd"] = str(spec_part(p, d["spec_sub"]).at(p.wt / f"spec-{did}"))
            if p and d["spec"]:
                d["spec_view"] = spec_view(p, d)
            d["origin"] = origin_view(d["origin"])
            d["msgs"] = q("SELECT role,text,ts,task_ids FROM design_msgs WHERE design_id=? ORDER BY id", (did,))
            d["replied"] = max([m["ts"] for m in d["msgs"] if m["role"] != "user"], default=None)
            for msg in d["msgs"]:
                ids = json.loads(msg["task_ids"] or "[]")
                msg["tasks"] = [{"id": i, "title": t.get("title"), "state": t.get("state")}
                                for i in ids for t in [task(i) or {}]]
            d["live"] = DESIGN_LIVE.get(did, [])[-12:]
            d["permits"] = permits_of(did)
            d["queued"] = queued_of(d)
            d["files"] = chat_files(did) if d["kind"] != "project" else []
            return self.send(200, d)
        self.send(404, {"error": "not found"})

    def post(self):
        if not self.host_ok():
            return self.send(403, {"error": "unknown Host header"})
        if self.headers.get("X-Ops") != "1":  # blocks cross-site form posts
            return self.send(403, {"error": "missing X-Ops header"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        if not READY.is_set():  # PROJECTS is half loaded and tasks not yet recovered
            return self.send(503, {"error": STARTING, "starting": True})
        if m := re.fullmatch(r"/mcp/permit/(\d+)", path):  # a project chat's permission prompts (permit_rpc)
            out = permit_rpc(int(m.group(1)), body)
            return self.send(200, out) if out else self.send(202, b"")
        if path.startswith("/api/research/"):  # its own status codes: 404 while off, 409 while read-only
            return self.send(*research.post(path, body))
        try:
            return self.send(200, self.route(path, body) or {"ok": True})
        except KeyError as e:
            return self.send(400, {"error": f"missing {e}"})
        except Exception as e:
            return self.send(400, {"error": str(e)})

    def route(self, path, b):
        if path == "/api/images":
            return save_image(b.get("data"))
        if m := re.fullmatch(r"/api/todos/(\d+)/done", path):
            if not todo_done(int(m.group(1))):
                raise ValueError("no open to-do with that id")
            return {"ok": True}
        if path == "/api/todos":  # your own (TODO-2)
            return todo_own(b)
        if path == "/api/todos/remind":  # {project, ref}: Remind me on an ended topic (TODO-2)
            return todo_remind(b)
        if path == "/api/todos/queue":  # {key, body?}: Queue as task (TODO-7)
            return todo_queue(b)
        if path == "/api/todos/wait":  # TODO-6
            return todo_wait(b["key"], b.get("tasks"), b.get("until"), b.get("at"))
        if path == "/api/todos/note":  # TODO-6
            return todo_note(b["key"], b.get("note"))
        if path == "/api/todos/skip":
            return todo_skip(b["key"])
        if path == "/api/todos/unpark":
            return todo_unpark(b["key"])
        if path == "/api/links/check":  # which of a chat message's link targets exist (README: Links in chats)
            heads, chats = {}, {}
            return {"refs": link_check(b.get("project"), b.get("refs"), heads, chats), "heads": heads, "chats": chats}
        if path == "/api/tasks":
            if not (b.get("body") or "").strip():
                raise ValueError("describe the task")
            dep = int(str(b["depends_on"]).lstrip("#")) if str(b.get("depends_on") or "").strip() else None
            jira, target = branch_input(b.get("branch_or_jira")) if b.get("branch_or_jira") else \
                (b.get("jira"), b.get("branch"))  # the form's one field, or opsd.py add --jira / --branch
            tid = add_task(b["project"], b["body"], b.get("title", ""), b.get("priority", 0),
                           b.get("hold", 0), dep, auto_release=b.get("release"), model=b.get("model"),
                           effort=b.get("effort"), not_before=b.get("not_before"), jira=jira, target=target)
            p = PROJECTS.get(b["project"])
            return {"id": tid, **({"warning": w} if p and (w := uncommitted_warning(p)) else {})}
        if path == "/api/schedules":
            return {"id": add_schedule(b)}
        if m := re.fullmatch(r"/api/projects/([\w-]+)/recap", path):  # Summarize on a project home (TODAY-2)
            return recap_write(m.group(1))
        if m := re.fullmatch(r"/api/projects/([\w-]+)/research", path):  # New research (RES-2)
            return new_research(m.group(1), b)
        if m := re.fullmatch(r"/api/projects/([\w-]+)/research/read", path):  # you opened a report (RES-5)
            return report_mark(m.group(1), str(b.get("path") or ""))
        m = re.fullmatch(r"/api/schedules/(\d+)(?:/(run|pause|resume|delete))?", path)
        if m:
            sid, act = int(m.group(1)), m.group(2)
            s = schedule(sid)
            if not s:
                raise ValueError(f"no schedule #{sid}")
            if not act:
                return edit_schedule(sid, b)
            if act == "run":  # now, whatever its last task is doing: you asked. next_at stays
                return {"id": queue_scheduled(s, "Run now")}
            if act == "delete":
                q("DELETE FROM schedules WHERE id=?", (sid,))
                q("UPDATE schedules SET after_schedule=NULL WHERE after_schedule=?", (sid,))  # waits on nothing now
            else:  # resume from the next slot after now: a long pause doesn't run at once
                q("UPDATE schedules SET paused=?, next_at=? WHERE id=?",
                  (int(act == "pause"), s["next_at"] if act == "pause" else next_slot(s["every"], time.time()), sid))
            return
        if path == "/api/openspec/new":  # New change: a design chat that drafts one
            return {"id": spec_new(proj(b["project"]), b.get("sub") or "", b.get("text"))}
        if path in ("/api/openspec/init", "/api/openspec/update"):  # Set up, Refresh commands
            return openspec_run(proj(b["project"]), b.get("sub") or "", path.rsplit("/", 1)[1])
        if path == "/api/send":  # opsd.py send, from a session the queue started
            return send_peer(b.get("from"), b["project"], b.get("kind") or "task", b.get("text"), b.get("title") or "",
                             bool(b.get("hold")), b.get("model"), b.get("effort"), b.get("after"),
                             b.get("not_before"), b.get("jira"), b.get("branch"))
        if path == "/api/settings":
            if "usage_brake" in b:
                b["usage_brake"] = min(100, max(0, int(float(b["usage_brake"] or 0))))
            if "usage_taper" in b:
                b["usage_taper"] = min(100, max(0, int(float(b["usage_taper"] or 0))))
            if "release_wait" in b:
                b["release_wait"] = max(0, int(float(b["release_wait"] or 0)))
            if "research_repo" in b:  # "" turns research off
                b["research_repo"] = research.check_repo(b["research_repo"])
            for k in SETTING_KEYS:
                if k in b:
                    set_setting(k, model_name(b[k]) or "" if k.startswith("model")
                                else effort_name(b[k]) or "" if k == "effort" else b[k])
            if "release_wait" in b:  # a train whose new time is up leaves now
                release_trains()
            return
        if path == "/api/projects":
            if b.get("check"):  # what the path resolves to, and whether it can be created
                repo = resolve_repo(b["path"])
                return {"repo": str(repo), "exists": repo.exists(), "git": (repo / ".git").exists()}
            out = detect_project(b["path"], b.get("name") or None, create=bool(b.get("create")))
            load_projects()
            return {"name": out.stem, "config": str(out)}
        if path == "/api/deploys":  # the Deploy button and `opsd.py deploy`: the only ways a deploy starts
            p = proj(b["project"])
            tid = queue_deploy(p, b["target"], b.get("version"), b.get("note"))
            stale_facts(p.name)
            return {"id": tid}
        if path == "/api/deploys/draft":  # the Deploy dialog's note, drafted from the changelog: queues nothing
            return draft_note(proj(b["project"]), b["target"])
        if path == "/api/pause-all":
            return pause_all()
        if path == "/api/resume-all":
            return resume_all()
        if path == "/api/restart":  # Settings' Danger zone, or opsd.py restart: restart at once
            return restart_now(str(b.get("from") or ""))
        if path == "/api/limit/clear":  # Try now: stop waiting out the usage limit
            return limit_clear("you clicked Try now")
        if path == "/api/projects/reload":
            load_projects()
            return {"projects": list(PROJECTS)}
        m = re.fullmatch(r"/api/projects/([^/]+)/(archive|unarchive)", path)
        if m:  # unarchive names a project that is not loaded, so these come before proj()
            return (archive_project if m.group(2) == "archive" else unarchive_project)(unquote(m.group(1)))
        m = re.fullmatch(r"/api/projects/([\w-]+)/(promote|play|stop|pause|resume|release|senddev)", path)
        if m:
            p, act = proj(m.group(1)), m.group(2)
            if act == "senddev":  # Send to dev, on the banner about your checkout's uncommitted files
                tid = queue_senddev(p)
                stale_facts(p.name)
                return {"id": tid}
            if act == "release":  # the project's "Promote after merge" default, for tasks queued from now on
                set_setting(f"auto_release:{p.name}", "1" if b.get("on") else "0")
            elif act == "promote":
                return {"id": queue_release(p)}
            elif act == "play":  # Play main, or Play dev with {"where": "dev"}
                play(p, "dev" if b.get("where") == "dev" else "main")
                stale_facts(p.name)
            elif act == "stop":  # Stop main, or Stop dev with {"where": "dev"}: quit the app running from there
                stop_play(p, "dev" if b.get("where") == "dev" else "main")
            else:
                set_setting(f"paused:{p.name}", "1" if act == "pause" else "0")
            return
        # pick the topic: {project, how: chat|topic|subject|task|new|mode, ref?, mode?, ended?, title?}
        if path == "/api/conversation":
            return conv_open(b.get("project") or "", b.get("how"), b.get("ref"), b.get("mode"),
                             b["ended"] if "ended" in b else UNSEEN, b.get("title"))
        if path == "/api/conversation/say":
            return conv_say(b.get("project") or "", b.get("text"), b.get("todo"))
        if path == "/api/conversation/end":  # {project, ref}: you are done with that topic
            return topic_end(b.get("project") or "", b.get("ref"))
        if path == "/api/conversation/reopen":  # {project, ref}: put an ended topic back
            return topic_reopen(b.get("project") or "", b.get("ref"))
        if path == "/api/conversation/end-read":  # {project, days}: end the read topics older than days
            return topics_end_read(b.get("project") or "", b.get("days"))
        if path == "/api/designs":
            text = (b.get("text") or "").strip()
            if b.get("spec"):  # open the chat about an OpenSpec change; a message is optional
                did = spec_open(proj(b["project"]), b.get("sub") or "", b["spec"])
                if text:
                    design_say(did, text)
                return {"id": did}
            if not text:
                raise ValueError("say something to start the chat")
            proj(b["project"])
            if b.get("quick"):  # the quick chat: leave the topic Today shows alone (README: TODAY-6)
                conv_pin(b["project"])
            kind = "project" if b.get("kind") == "project" else "design"
            did = q("INSERT INTO designs (project, title, created, updated, kind, quick) VALUES (?,?,?,?,?,?)",
                    (b["project"], title_of(text)[:80], time.time(), time.time(), kind, int(bool(b.get("quick")))))
            design_say(did, text)
            return {"id": did}
        m = re.fullmatch(r"/api/designs/(\d+)/(say|unqueue|stop|rename|queue|seen|end|reopen|permit)", path)
        if m:
            did = int(m.group(1))
            d = design(did)
            if not d:
                raise ValueError("no such chat")
            if d["quick"] and m.group(2) in ("say", "reopen"):  # it never becomes the topic Today shows (TODAY-6)
                conv_pin(d["project"])
            if m.group(2) == "permit":  # Approve or Deny on a permission prompt the chat's turn waits on
                return permit_answer(did, str(b["id"]), b.get("allow") is True)
            if m.group(2) == "say":
                return {"queued": design_say(did, b["text"].strip())}
            elif m.group(2) == "unqueue":  # take back a queued message, by its place in the queue
                with DESIGN_LOCK:
                    waiting = queued_of(design(did))
                    k = int(b.get("index", -1))
                    if not 0 <= k < len(waiting):
                        raise ValueError("that message was already sent")
                    q("UPDATE designs SET queued=? WHERE id=?", (json.dumps(waiting[:k] + waiting[k + 1:]), did))
                    return {"text": waiting[k]}
            elif m.group(2) == "stop":
                stop(DESIGN_PROCS.get(did))
            elif m.group(2) == "queue":
                return {"ids": spec_queue(did, b.get("plan"), b.get("hold"), b.get("release"))}
            elif m.group(2) == "seen":  # you looked at its latest reply
                q("UPDATE designs SET seen=? WHERE id=?", (time.time(), did))
            elif m.group(2) == "end":  # off the list of chats waiting for you; a reply in progress stops
                q("UPDATE designs SET ended=?, seen=?, queued='[]' WHERE id=?", (time.time(), time.time(), did))
                stop(DESIGN_PROCS.get(did))
                todo_remind_close(d["project"], did)
            elif m.group(2) == "reopen":  # with ended, only what you saw ended (compare-and-set, as conv_open)
                if "ended" in b and d["ended"] and b["ended"] != d["ended"]:
                    raise ValueError("it ended again after this list was drawn: pick it again")
                q("UPDATE designs SET ended=NULL WHERE id=?", (did,))
            else:
                q("UPDATE designs SET title=? WHERE id=?", (b["title"][:80], did))
            return
        m = re.fullmatch(r"/api/tasks/(\d+)/([\w-]+)", path)
        if not m:
            raise ValueError("unknown endpoint")
        tid, action = int(m.group(1)), m.group(2)
        t = task(tid)
        if not t:
            raise ValueError("no such task")
        p = PROJECTS.get(t["project"])
        st = t["state"]
        if action in ("retry", "answer", "approve", "reject", "resume") and tid in TASK_CHAT_LIVE:
            raise ValueError("wait for the worker's reply to your question first")
        if action == "ask":
            return task_ask(tid, (b.get("text") or "").strip())
        if action == "steer":
            return task_steer(tid, (b.get("text") or "").strip())
        if action == "stopask":
            stop(TASK_CHAT_PROCS.get(tid))
            return
        if action == "cancel":
            if st in ("done", "cancelled"):
                raise ValueError(f"task is {st}")
            upd(tid, state="cancelled", phase="", finished=time.time())
            stop(PROCS.get(tid))
            ev(tid, "cancelled")
            kept_for_retry(tid)
            tell_sender(tid, "was cancelled by the owner")
            if p and t["kind"] in MERGES and t["auto_release"]:  # its split's other tasks may be waiting for it
                auto_release(p)
        elif action == "release":  # the "Promote after merge" toggle
            if t["kind"] not in MERGES or st in ENDED:
                raise ValueError(f"task is {st}" if st in ENDED else "a release releases itself")
            if is_branch_task(t) and b.get("on"):
                raise ValueError("a branch task doesn't merge into dev, so there is nothing to promote")
            upd(tid, auto_release=int(bool(b.get("on"))))
            ev(tid, "will be promoted after it merges" if b.get("on") else "will not be promoted after it merges")
        elif action == "start-now":  # Start later's Start now: it takes its normal place in the queue
            start_now(tid)
        elif action == "pause":
            pause_task(tid)
        elif action == "resume":
            resume_task(tid)
        elif action == "retry":
            if st not in ("failed", "cancelled"):
                raise ValueError(f"task is {st}")
            note = b.get("note") or ""
            after = retry_after(tid, b.get("after"))  # replaces any earlier depends_on; without it, that stays
            upd(tid, state="queued", rounds=0, error=None, finished=None, **({"depends_on": after} if after else {}),
                next_prompt=(f"Your previous attempt stopped: {t['error'] or st}. {note} "
                             "Check the worktree state and finish the task; write $OPS_RESULT.")
                if t["session_id"] else None)
            ev(tid, "retried" + (f" after #{after}" if after else "") + (f": {note}" if note else "")
               + ("; moving main again (the deploy itself succeeded)" if t["kind"] == "deploy" and
                  t["start_at"] == "promote" else "; the deploy starts over" if t["kind"] == "deploy" else ""))
        elif action == "answer":
            if st != "needs_input":
                raise ValueError(f"task is {st}")
            back = {"start_at": "claude"} if t["phase"] in (PR_RED, PR_CLOSED) else {}  # the worker takes it
            upd(tid, state="queued", question=None, next_prompt=answer_prompt(t["question"], b["answer"]), **back)
            ev(tid, "answered: " + b["answer"][:200])
        elif action == "approve":
            if st != "review":
                raise ValueError(f"task is {st}")
            kill_app(p, t["worktree"])
            upd(tid, state="queued", hold=0, start_at="integrate")
            ev(tid, "approved")
        elif action == "reject":
            if st != "review":
                raise ValueError(f"task is {st}")
            kill_app(p, t["worktree"])
            clear_proof(tid, report=True)  # it shows the work you sent back; the next round makes a new one
            upd(tid, state="queued", rounds=0,
                next_prompt=f"The user tried your change and wants this changed:\n\n{b['feedback']}\n\n"
                            "Update the work, re-run the checks, commit, and write $OPS_RESULT (with an updated "
                            "`review`).")
            ev(tid, "changes requested: " + b["feedback"][:200])
        elif action in ("pr-open", "pr-merge", "pr-merge-all", "fix-ci", "pr-retry"):
            return pr_action(p, t, action, b)
        elif action == "play":
            if not p or not t["worktree"]:
                raise ValueError("this task has no worktree")
            launch_app(p, t["worktree"])
        elif action == "edit":
            if st != "queued":
                raise ValueError("only queued tasks can be edited")
            upd(tid, body=b["body"], title=(b.get("title") or title_of(b["body"]))[:120],
                priority=int(b.get("priority", t["priority"])))
        else:
            raise ValueError("unknown action")


def on_term(*_):
    if STOPPING.is_set():  # a second signal: stop now
        os._exit(1)
    try:
        shutdown()
    except Exception as e:  # still stop; the next daemon requeues whatever is left running
        print("shutdown error:", e, flush=True)
    stop_many(list(PROCS.values()) + list(STEPS) + list(DESIGN_PROCS.values()) + list(TASK_CHAT_PROCS.values()))
    try:
        if PIDFILE.read_text().strip() == str(os.getpid()):
            PIDFILE.unlink()
    except OSError:
        pass
    os._exit(0)


READY = threading.Event()  # set once projects are loaded and tasks recovered; until then the API says it's starting
STARTING = "claude-ops is still starting up. Try again in a few seconds."


def start_up():
    """Load projects and recover tasks, then start the scheduler: after the bind, so the port answers at once."""
    try:
        load_projects()
        recover()
        resume_chats()
    except Exception as e:  # serve anyway: a broken project or task shouldn't keep the dashboard down
        print("startup error:", e, flush=True)
    READY.set()
    threading.Thread(target=scheduler, daemon=True).start()
    research.start()  # its watchdog: time limits, the usage meter and notifications for research runs
    print(f"claude-ops ready  projects: {', '.join(PROJECTS) or 'none'}", flush=True)


class Stamped:
    """A text stream that starts each line with the local time: opsd.log is the daemon's stdout and stderr, and
    without times the order of what happened (a setup, a build, a launch) could not be told from it."""

    def __init__(self, out):
        self.out, self.at_start, self.lock = out, True, threading.Lock()

    def write(self, text):
        with self.lock:
            for part in text.splitlines(keepends=True):
                if self.at_start:
                    self.out.write(time.strftime("%Y-%m-%d %H:%M:%S "))
                self.out.write(part)
                self.at_start = part.endswith("\n")
        return len(text)

    def __getattr__(self, name):  # flush, fileno, isatty, encoding: the wrapped stream's
        return getattr(self.out, name)


def serve():
    sys.stdout, sys.stderr = Stamped(sys.stdout), Stamped(sys.stderr)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    print(f"claude-ops starting on http://{HOST}:{PORT}  (pid {os.getpid()})", flush=True)
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    threading.Thread(target=state_bytes, daemon=True).start()  # the first dashboard load finds /api/state built
    PIDFILE.write_text(f"{os.getpid()}\n")  # after the bind, so a second daemon that fails can't overwrite it
    print(f"claude-ops on http://{HOST}:{PORT}  loading projects  (claude: {CLAUDE})", flush=True)
    threading.Thread(target=start_up, daemon=True).start()
    srv.serve_forever()


def post(path, body):
    req = Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    out = urlopen(req).read().decode()
    print(out)
    return out


def send_cli(argv):
    """opsd.py send PROJECT [--design|--chat] [--title T] [--hold] [--model M] [--effort E] [--after TASK]
    [--at TIME | --after-reset] [--branch NAME | --jira KEY] [TEXT...]; TEXT from stdin when not given. --after takes
    12 or #12."""
    body, words, it = {"project": argv[0], "kind": "task", "from": os.environ.get("OPS_FROM", "")}, [], iter(argv[1:])
    for a in it:
        if a in ("--task", "--design", "--chat"):
            body["kind"] = a[2:]
        elif a == "--title":
            body["title"] = next(it, "")
        elif a == "--hold":
            body["hold"] = True
        elif a in ("--model", "--effort"):
            body[a[2:]] = next(it, "")
        elif a == "--after":
            v = next(it, "").strip().lstrip("#")
            body["after"] = int(v) if v.isdigit() else v
        elif a == "--at":
            body["not_before"] = next(it, "")
        elif a in ("--branch", "--jira"):
            body[a[2:]] = next(it, "")
        elif a == "--after-reset":
            body["not_before"] = "reset"
        else:
            words.append(a)
    body["text"] = " ".join(words) if words else sys.stdin.read()
    url = os.environ.get("OPS_DAEMON") or DAEMON_URL
    req = Request(f"{url}/api/send", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    try:
        print(json.loads(urlopen(req, timeout=60).read())["said"])
    except HTTPError as e:
        sys.exit(f"not sent: {json.loads(e.read() or b'{}').get('error', e)}")
    except URLError as e:
        sys.exit(f"not sent: the claude-ops daemon at {url} did not answer ({e.reason})")


def restart_cli():
    """opsd.py restart: the daemon that started this session (OPS_DAEMON) runs opsctl restart detached, as Settings'
    Restart button does, so a chat, the daemon's child, isn't stopped before the restart is under way."""
    url = os.environ.get("OPS_DAEMON") or DAEMON_URL
    req = Request(f"{url}/api/restart", data=json.dumps({"from": os.environ.get("OPS_FROM", "")}).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    try:
        urlopen(req, timeout=30).read()
    except HTTPError as e:
        sys.exit(f"not restarted: {json.loads(e.read() or b'{}').get('error', e)}")
    except URLError as e:
        sys.exit(f"not restarted: the claude-ops daemon at {url} did not answer ({e.reason})")
    print("claude-ops is restarting: running tasks pause and resume, and a chat that ran this picks up again in the "
          f"new daemon. Log: {DATA / 'restart.log'}")


def add_request(project, words):
    """opsd.py add -p PROJECT [--model M] [--effort E] [--research T0|T1|T2] [--at TIME | --after-reset]
    [--branch NAME | --jira KEY] TEXT: (path,
    body) to post. With --research it queues a one-off research run whose question is TEXT (RES-2), else a task.
    --at and --after-reset set its not_before (README: Start later). TEXT from stdin when not given."""
    opts, words = {}, list(words)
    while (words[:1] in (["--model"], ["--effort"], ["--research"], ["--at"], ["--branch"], ["--jira"])
           and len(words) >= 2) or words[:1] == ["--after-reset"]:
        if words[0] == "--after-reset":
            opts["not_before"], words = "reset", words[1:]
        else:
            opts["not_before" if words[0] == "--at" else words[0][2:]], words = words[1], words[2:]
    if "research" in opts and ("branch" in opts or "jira" in opts):
        raise ValueError("--branch and --jira are for tasks, not research runs")
    text = " ".join(words) or sys.stdin.read()
    if "research" in opts:
        return (f"/api/projects/{quote(project, safe='')}/research",
                {"question": text, "tier": opts.pop("research"), **opts})
    return "/api/tasks", {"project": project, "body": text, **opts}


SCHEDULE_OPTS = ("--title", "--every", "--body", "--body-file", "--model", "--effort", "--priority", "--research",
                 "--after-schedule")


def schedule_request(args):
    """opsd.py schedule add -p PROJECT [opts] TITLE, or schedule edit ID [opts]: (path, body) to post. An edit sends
    only the fields given, so the rest of the schedule stays as it was. --body-file reads the body from a file (- for
    stdin), for a body too long to quote. The guard keeps a session the queue started off the dashboard's API, which
    also pauses and cancels tasks, so this is how a session changes a schedule. ValueError for a bad command line."""
    verb, rest = args[0], list(args[1:])
    if verb == "add":
        if rest[:1] != ["-p"] or len(rest) < 2:
            raise ValueError("usage: opsd.py schedule add -p NAME --every 'mon 09:00' [options] \"title...\"")
        path, opts, rest = "/api/schedules", {"project": rest[1]}, rest[2:]
    else:
        sid = rest.pop(0).lstrip("#") if rest else ""
        if not sid.isdigit():
            raise ValueError("usage: opsd.py schedule edit ID [--title T] [--every E] [--body TEXT | --body-file PATH]")
        path, opts = f"/api/schedules/{sid}", {}
    words = []
    while rest:
        a = rest.pop(0)
        if a in SCHEDULE_OPTS and rest:
            opts[a[2:].replace("-", "_") if a == "--after-schedule" else a[2:]] = rest.pop(0)
        elif a in ("--hold", "--no-hold"):
            opts["hold"] = a == "--hold"
        else:
            words.append(a)
    if "body-file" in opts:
        f = opts.pop("body-file")
        opts["body"] = sys.stdin.read() if f == "-" else Path(f).expanduser().read_text()
    if verb == "add":
        opts["title"] = " ".join(words) or opts.get("title", "")
    elif words:
        raise ValueError(f"schedule edit takes options only, not {' '.join(words)!r}: --title sets the title")
    elif not opts:
        raise ValueError("schedule edit: nothing to change; give --title, --every, --body or another option")
    return path, opts


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "guard":
        guard(args)
    elif cmd == "event" and len(args) == 2 and args[0].isdigit():  # straight into the db: the daemon may be down
        q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (int(args[0]), time.time(), args[1]))
    elif cmd == "chat" and args[:1] == ["show"] and len(args) in (2, 4) and args[1].isdigit() and (
            len(args) == 2 or (args[2] == "--last" and args[3].isdigit())):  # straight from the db, as event
        out = chat_show(int(args[1]), int(args[3]) if len(args) == 4 else None)
        if out is None:
            print(f"no chat {args[1]}", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(out)
    elif cmd in ("pause-all", "resume-all"):
        post(f"/api/{cmd}", {})
    elif cmd == "restart" and not args:
        restart_cli()
    elif cmd == "add-project" and args:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        out = detect_project(str(Path(args[0]).expanduser().absolute()), args[1] if len(args) > 1 else None)
        print(f"wrote {out}. Review it, then reload projects on the dashboard (or restart the service).")
    elif cmd == "adopt-workspace-root" and len(args) == 2 and args[0] == "-p":
        try:
            did = adopt_workspace_root(args[1])
        except (ValueError, RuntimeError) as e:
            sys.exit(f"not adopted: {e}")
        print("\n".join(did) or f"{args[1]}: nothing to do, its root is already a repo the queue uses")
        try:
            post("/api/projects/reload", {})
        except (HTTPError, URLError) as e:
            print(f"the claude-ops daemon did not reload ({e}): reload projects on the dashboard", file=sys.stderr)
    elif cmd == "export-config" and len(args) == 2 and args[0] == "-p":
        try:
            print("\n".join(export_config(args[1])))
        except (ValueError, RuntimeError) as e:
            sys.exit(f"not exported: {e}")
    elif cmd == "send" and args and not args[0].startswith("-"):
        send_cli(args)
    elif cmd == "add" and len(args) >= 2 and args[0] == "-p":
        try:
            out = post(*add_request(args[1], args[2:]))
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
        except ValueError as e:
            sys.exit(str(e))
        if w := (json.loads(out or "{}") or {}).get("warning"):
            print(w, file=sys.stderr)
    elif cmd == "schedule" and args[:1] in (["add"], ["edit"]):
        try:
            post(*schedule_request(args))
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
        except URLError as e:
            sys.exit(f"the claude-ops daemon on port {PORT} did not answer ({e.reason})")
        except (ValueError, OSError) as e:  # a bad command line, or a --body-file it can't read
            sys.exit(str(e))
    elif cmd == "pr" and len(args) == 2 and args[0] in ("open", "merge") and args[1].lstrip("#").isdigit():
        try:  # README: Branches and pull requests
            post(f"/api/tasks/{args[1].lstrip('#')}/" + {"open": "pr-open", "merge": "pr-merge-all"}[args[0]], {})
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
        except URLError as e:
            sys.exit(f"the claude-ops daemon on port {PORT} did not answer ({e.reason})")
    elif cmd in ("archive-project", "unarchive-project") and len(args) == 1:
        try:
            post(f"/api/projects/{quote(args[0], safe='')}/{cmd.split('-')[0]}", {})
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
        except URLError as e:
            sys.exit(f"the claude-ops daemon on port {PORT} did not answer ({e.reason})")
    elif cmd == "deploy" and len(args) >= 3 and args[0] == "-p":
        words, opts, rest = [], {}, args[2:]
        while rest:
            a = rest.pop(0)
            if a in ("--version", "--note") and rest:
                opts[a[2:]] = rest.pop(0)
            else:
                words.append(a)
        try:
            post("/api/deploys", {"project": args[1], "target": " ".join(words), **opts})
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
    else:
        print(__doc__)
        sys.exit(2)
