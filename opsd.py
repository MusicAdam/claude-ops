#!/usr/bin/env python3
"""claude-ops: a task queue for your git projects, worked by parallel headless Claude sessions.

  opsd.py serve                        run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add-project PATH [NAME]      detect PATH and write ~/.claude-ops/projects/NAME.toml
  opsd.py add -p NAME [--model M] [--effort E] "text..."   queue a task through the running daemon; --model opus,
                                       sonnet, haiku or a full model id (else the setting model:task, then model);
                                       --effort low, medium, high, xhigh or max (else the setting effort)
  opsd.py deploy -p NAME TARGET [--version same|patch|minor|major|X.Y.Z] [--note TEXT]   deploy a [[deploy]] target
  opsd.py send NAME [--design|--chat] [--title T] [--hold] [--model M] [--effort E] "text..."
                                       from a session the queue started: a task (or chat) in another project
  opsd.py pause-all | resume-all       pause every running task (it stops now, resumable) / resume every paused one
  opsd.py archive-project NAME         take a project off the queue: its config moves to projects/archived/
  opsd.py unarchive-project NAME       bring an archived project back, as it was
  opsd.py event TASK_ID TEXT           add a line to a task's log, without the daemon (opsctl, when a restart fails)
  opsd.py guard --daemon PID [--root DIR]   PreToolUse hook: refuse Bash commands that stop processes the session did not start

Per project (paths come from ~/.claude-ops/projects/<name>.toml):
  <repo>                 your checkout of the main branch. The queue only fast-forwards it, when a release passes.
  <worktrees>/dev        integration worktree on the dev branch. Finished tasks merge here.
  <worktrees>/main       build of main, the golden version (detached). Changes only when a release passes.
  <worktrees>/t<id>      one worktree per task, branch task/<id>-<slug> (a release: detached at its dev snapshot).
  <worktrees>/deploy     where deploy targets run (detached). Reset for each deploy but never cleaned.

A workspace (`repos = [...]` in its config) is a folder that holds several git repos, such as a backend and an
app. Each of the paths above is then a folder with a copy of the workspace's own files and one worktree per repo,
and every repo has its own main, dev and task branches.
"""
import base64, fnmatch, json, os, re, secrets, shlex, shutil, signal, sqlite3, subprocess, sys, threading, time, tomllib
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HOME = Path.home()
CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("OPS_DATA", HOME / ".claude-ops"))
PROJECTS_DIR = DATA / "projects"
RUNS = DATA / "runs"
PIDFILE = DATA / "opsd.pid"  # stop a daemon with kill $(cat "$OPS_DATA/opsd.pid"), never pkill -f
IMAGES = DATA / "images"  # screenshots dropped or pasted into a message box
DB = DATA / "ops.db"
HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8788"))
DAEMON_URL = f"http://{'127.0.0.1' if HOST in ('', '0.0.0.0', '::') else HOST}:{PORT}"  # for `opsd.py send`
CLAUDE = os.environ.get("OPS_CLAUDE") or shutil.which("claude") or str(HOME / ".local/bin/claude")
MAX_FIX_ROUNDS = 3  # times a worker is sent back after a conflict or failed gate
# A worker or release checker's next turn starts a fresh session, with a handoff note, instead of resuming one whose
# prompt cache has expired (it lasts an hour) or whose context makes every call dear. docs/usage-analysis.md Q2, Q3.
FRESH_AFTER = 55 * 60  # seconds since the session's last call
FRESH_ABOVE = 155_000  # tokens of context at its last call
HANDOFF_CHARS = 40_000  # about 10k tokens of handoff note
RESULT_FILE = ".ops-result.json"

ACTIVE = ("running", "gating", "merging")
STOPPED = ("cancelled", "paused")  # set by you; the pipeline stops at its next step and never overrides them
ENDED = ("done", "failed", "cancelled")
MERGES = ("task", "senddev")  # kinds that go through the gate into dev: they can release after merge
RELEASE_LOCK = threading.Lock()  # one release queued at a time, when tasks finish together
TRAIN = {}  # project -> {"ids", "by"}: merged tasks waiting for the release train (auto_release), for the dashboard
IN_MAIN = set()  # ids of merged tasks main contains: main only moves forward, so auto_release checks each once
DBL = threading.RLock()
PROCS = {}  # task id -> current Popen (claude or a gate step), for cancel
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
    """One git repo of a project: the project itself, or one folder of a workspace."""

    def __init__(self, p, sub):
        self.sub = sub
        self.src = p.repo / sub if sub else p.repo  # your checkout of it

    def at(self, root):
        """This repo's worktree inside a project worktree."""
        return Path(root) / self.sub if self.sub else Path(root)

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
    def __init__(self, path):
        c = tomllib.loads(path.read_text())
        self.name = path.stem
        self.title = c.get("title", self.name)
        self.repo = Path(c["repo"]).expanduser()
        self.main = c.get("main_branch", "main")
        self.dev = c.get("dev_branch", "dev")
        self.wt = Path(c.get("worktrees", f"{self.repo}-wt")).expanduser()
        self.repos = [r.strip("/") for r in c.get("repos", [])]  # a workspace: git repos in folders of repo
        self.parts = [Part(self, r) for r in self.repos] or [Part(self, "")]
        self.max_parallel = int(c.get("max_parallel", 0))  # 0: only the global limit applies
        w = c.get("worktree", {})
        self.clone = w.get("clone", [])
        self.setup = w.get("setup", [])
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
        self.dev_wt = self.wt / "dev"
        self.main_wt = self.wt / "main"
        self.deploy_wt = self.wt / "deploy"  # persistent: build output survives from one deploy to the next
        self.deploys = deploy_targets(self, c.get("deploy", []))
        self.lock = threading.Lock()  # merges into dev and promotes
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
        return ", ".join(f"{r.sub} {sha[:n]}" if r.sub else sha[:n] for r, sha in self.pairs(revs or ""))

    def label(self, shas, n=8):
        """Like show, for a list that may hold only some repos' shas, as a task's merged does."""
        shas = (shas or "").split()
        if not self.repos or len(shas) == len(self.parts):
            return self.show(" ".join(shas), n)
        found = [(r, self.mine(r, " ".join(shas))) for r in self.parts]
        known = {s for _, s in found if s}
        return ", ".join([f"{r.sub} {s[:n]}" for r, s in found if s] + [s[:n] for s in shas if s not in known])

    def mine(self, r, shas):
        """The sha in a space-separated list that is a commit of repo r, or None."""
        return next((s for s in (shas or "").split() if r.has(s)), None)

    def port_for(self, tid):
        return self.port_base + tid % 90


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


PROJECTS = {}


def load_projects():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    found = {}
    for f in sorted(PROJECTS_DIR.glob("*.toml")):
        try:
            p = Project(f)
            for t in p.deploys:
                if t.error:
                    print(f"project {f.name}: deploy target {t.name}: {t.error}", flush=True)
            old = PROJECTS.get(p.name)
            if old:
                p.lock, p.checkout_lock = old.lock, old.checkout_lock
            found[p.name] = p
        except Exception as e:
            print(f"project {f.name}: {e}", flush=True)
    PROJECTS.clear()
    PROJECTS.update(found)
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
    for r in p.parts:
        if not r.git("rev-parse", "--verify", "-q", f"refs/heads/{p.dev}", check=False):
            r.git("branch", p.dev, p.main)
        if not r.at(p.dev_wt).exists():
            r.git("worktree", "add", str(r.at(p.dev_wt)), p.dev)
            set_setting(f"dev_prepared:{p.name}", "")  # a new dev worktree: Play dev prepares it first
        exclude = Path(r.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
        if RESULT_FILE not in (exclude.read_text() if exclude.exists() else ""):
            exclude.parent.mkdir(exist_ok=True)
            with open(exclude, "a") as f:
                f.write(f"\n{RESULT_FILE}\n")
    if p.build or p.launch:  # Play main runs it there
        copy_workspace_files(p, p.main_wt)
        if not all(r.at(p.main_wt).exists() for r in p.parts):
            for r in p.parts:
                if not r.at(p.main_wt).exists():
                    r.git("worktree", "add", "--detach", str(r.at(p.main_wt)), p.main)
            set_setting(f"main_prepared:{p.name}", "")  # setup can take minutes: promote or Play main does it


def prepare_main(p, say):
    """Clone files and setup for a main worktree bootstrap made, on its first use; say(text) reports the phase.
    Slow, so only off the request thread. No main_prepared setting at all is a worktree made before it existed."""
    r = q("SELECT value FROM settings WHERE key=?", (f"main_prepared:{p.name}",), one=True)
    if r and r["value"] != "1":
        say(f"setting up {p.main_wt}")
        prepare_worktree(p, p.main_wt, src_first=p.repo)
        set_setting(f"main_prepared:{p.name}", "1")


def copy_workspace_files(p, dest):
    """A workspace's own files (CLAUDE.md, docs, .claude, ...) are in none of its repos: copy them into dest
    (APFS clone), replacing older copies. Edits made to them in a worktree are not kept."""
    if not p.repos:
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
        subprocess.run(["cp", "-cR", str(src), str(out)], capture_output=True)


def prepare_worktree(p, dest, src_first=None, setup=True):
    """Clone warm caches (APFS clone, near-instant) and run the project's setup commands."""
    for rel in p.clone:
        if (dest / rel).exists():
            continue
        for src in (src_first or p.main_wt, p.repo):
            if (src / rel).exists():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["cp", "-cR", str(src / rel), str(dest / rel)], capture_output=True)
                break
    for cmd in p.setup if setup else []:
        subprocess.run(cmd, shell=True, cwd=dest, capture_output=True,
                       env=dict(os.environ, OPS_REPO=str(p.repo)))


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
    lines = [f'title = "{repo.name}"', f'repo = "{repo}"', f'main_branch = "{main}"', f'dev_branch = "{dev}"', ""]
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
        lines += ['[worktree]', 'setup = ["npm ci --silent"]', ""]
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
             f'main_branch = "{main}"', f'dev_branch = "{dev}"', "", "[worktree]",
             "clone = [" + ", ".join(f'"{c}"' for c in clone) + "]",
             "setup = [" + ", ".join(f'"{c}"' for c in setup) + "]", ""]
    for g in gates:
        lines += g
    lines += ["[worker]", "notes = \"\"\"", "\"\"\"", "", "[design]", "notes = \"\"\"", "\"\"\"", ""]
    return "\n".join(lines)


def gate_toml(name, run, **kw):
    return ["[[gate]]", f'name = "{name}"', f'run = "{run}"', *(f'{k} = "{v}"' for k, v in kw.items()), ""]


# ---------------------------------------------------------------- storage

def q(sql, args=(), one=False):
    with DBL:
        c = sqlite3.connect(DB, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            cur = c.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            c.commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return cur.lastrowid
            return (rows[0] if rows else None) if one else rows
        finally:
            c.close()


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
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
                      ("last_call", "REAL"), ("ctx_tokens", "INTEGER")):  # its session's last call (see stale_session)
        if col not in cols:
            q(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    q("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, task_id INTEGER, ts REAL, text TEXT)")
    q("CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id)")  # a task's log, and the latest per task
    q("""CREATE TABLE IF NOT EXISTS designs (id INTEGER PRIMARY KEY, project TEXT, title TEXT, session_id TEXT,
        state TEXT DEFAULT 'idle', created REAL, updated REAL, cost REAL DEFAULT 0)""")
    q("""CREATE TABLE IF NOT EXISTS design_msgs (id INTEGER PRIMARY KEY, design_id INTEGER, role TEXT,
        text TEXT, ts REAL, task_ids TEXT, note TEXT)""")
    q("CREATE INDEX IF NOT EXISTS design_msgs_design ON design_msgs (design_id, id)")
    dcols = {r["name"] for r in q("PRAGMA table_info(designs)")}
    for col, decl in (("kind", "TEXT DEFAULT 'design'"),  # databases from before project chats
                      ("spec", "TEXT"), ("spec_sub", "TEXT DEFAULT ''"),  # and before OpenSpec chats
                      ("seen", "REAL DEFAULT 0"), ("ended", "REAL"),  # and before "ready for you" and End chat
                      ("origin", "TEXT"),  # and before peer sends
                      ("spec_draft", "INTEGER DEFAULT 0"),  # and before New change: drafting, no change yet
                      ("session_model", "TEXT")):  # and before models per kind: the model its session started on
        if col not in dcols:
            q(f"ALTER TABLE designs ADD COLUMN {col} {decl}")
            if col == "seen":  # chats from before count as read, so they don't all show a new reply at once
                q("UPDATE designs SET seen=updated")
    q("UPDATE designs SET state='idle' WHERE state='thinking'")  # a reply cut off by a restart
    q("CREATE TABLE IF NOT EXISTS task_msgs (id INTEGER PRIMARY KEY, task_id INTEGER, role TEXT, text TEXT, ts REAL)")
    q("CREATE INDEX IF NOT EXISTS task_msgs_task ON task_msgs (task_id, id)")
    if "delivered" not in {r["name"] for r in q("PRAGMA table_info(task_msgs)")}:  # databases from before steering
        q("ALTER TABLE task_msgs ADD COLUMN delivered REAL")
    q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    q("CREATE TABLE IF NOT EXISTS session_costs (session_id TEXT PRIMARY KEY, total REAL)")  # see cost_added
    for k, v in {"max_parallel": "3", "paused": "0", "model": "", "usage_brake": "85", "model:release": "sonnet",
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
SETTING_KEYS = ("max_parallel", "paused", "model", "usage_brake", "release_wait", "effort",  # the dashboard's
                *(f"model:{k}" for k in MODEL_KINDS))
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
    """Whether new tasks of p are marked "Release after merge" unless you say otherwise."""
    return setting(f"auto_release:{p.name}") == "1"


def release_play(p):
    """Whether p's releases have Claude build and play dev ("Play-check releases" on the project, on by default).
    Off, a release is gate-only: the merge check passes and main moves."""
    return setting(f"release_play:{p.name}") != "0"


def release_wait():
    """Minutes the release train waits at most for p's queue to go quiet, after the first of its tasks merges."""
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
             deploy=None, origin=None, senddev=None, model=None, effort=None):
    """auto_release None takes the project's default ("Release after merge" on the project).
    A deploy (kind "deploy") is made only by queue_deploy: you clicked Deploy, or ran `opsd.py deploy`.
    origin: the session in another project that sent it (see send_peer).
    senddev: the snapshot a Send to dev task (kind "senddev", made by queue_senddev) lands on dev.
    model: the model its worker starts on; None takes model:<kind> when it starts (see model_for).
    effort: its --effort; None takes the setting effort on each turn (see effort_for)."""
    p = proj(project)
    model, effort = model_name(model), effort_name(effort)
    if auto_release is None:
        auto_release = release_default(p)
    body = body.strip()
    title = (title or (title_of(body) if body else "untitled")).strip()[:120]
    if (kind == "deploy") != bool(deploy):
        raise ValueError("a deploy needs its target, and only a deploy has one")
    tid = q("INSERT INTO tasks (project, title, body, priority, hold, depends_on, parent, created, kind, auto_release, "
            "deploy, origin, senddev, model, effort) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, title, body, int(priority), int(bool(hold)), depends_on or None, parent, time.time(), kind,
             int(bool(auto_release) and kind in MERGES), json.dumps(deploy) if deploy else None,
             json.dumps(origin) if origin else None, json.dumps(senddev) if senddev else None, model, effort))
    ev(tid, "queued" + (f" after #{depends_on}" if depends_on else "") + (f" on {model}" if model else "")
       + (f" at {effort} effort" if effort else ""))
    return tid


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
    try:
        if where == "dev":
            path = p.dev_wt
            if setting(f"dev_prepared:{p.name}") != "1":  # bootstrap made it without clone files and setup
                phase(f"setting up {path}")
                copy_workspace_files(p, path)
                prepare_worktree(p, path)
                set_setting(f"dev_prepared:{p.name}", "1")
            if p.build:
                head = dev_head(p)
                if setting(f"dev_built:{p.name}") != head or (p.artifact and not (path / p.artifact).exists()):
                    phase(f"building {p.dev} at {p.show(head)}")
                    log = RUNS / f"play-{p.name}" / "build.log"
                    rc, tail = run_step(f"play-{p.name}", f"building {p.dev}", p.build, path, "build.log")
                    if rc or (p.artifact and not (path / p.artifact).exists()):
                        raise RuntimeError(f"Building {p.dev} failed (log: {log}).\n{tail[-1500:]}")
                    set_setting(f"dev_built:{p.name}", head)  # what it was at the start: a merge meanwhile rebuilds
        else:
            path = p.main_wt
            prepare_main(p, phase)
            if not p.build:  # no build pins main's worktree to what was built: play main as it is now
                for r in p.parts:
                    r.git("checkout", "--detach", p.main, cwd=r.at(path), check=False)
        phase(f"launching {p.dev if where == 'dev' else p.main}")
        launch_app(p, path)
        PLAYS.pop(p.name, None)
    except Exception as e:
        PLAYS[p.name] = {"where": where, "phase": None, "error": str(e)}
    stale_facts(p.name)


def dev_head(p):
    """The commit (a revs string) checked out in the dev worktree."""
    return " ".join(r.git("rev-parse", "HEAD", cwd=r.at(p.dev_wt)) for r in p.parts)


def child_commands(pid):
    """Commands a Claude process is running: its children other than caffeinate. Claude starts each
    Bash command in a process group of its own."""
    out = subprocess.run(["pgrep", "-lP", str(pid)], capture_output=True, text=True).stdout.splitlines()
    return [int(l.split()[0]) for l in out if l.split()[1:2] != ["caffeinate"]]


def killgroup(pid):
    """Stop pid's process group and its commands' groups, so no test run or build is left orphaned.
    Only for a pid that is surely ours: a stale one may by now be Chrome, a terminal shell, anything."""
    if pid:
        for g in child_commands(pid) + [pid]:
            try:
                os.killpg(g, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def stop(proc):
    """Stop a process we started, unless it already exited (its pid may then belong to someone else)."""
    if proc and proc.poll() is None:
        killgroup(proc.pid)


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
# Every Claude session the daemon starts with a shell (workers, release checkers, project chats) runs each
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


def guard_settings(root):
    """--settings for a Claude session: run each Bash command past the guard. root is the session's worktree;
    a process working or installed there counts as the session's own. None: only its descendants do."""
    cmd = [sys.executable, str(CODE / "opsd.py"), "guard", "--daemon", str(os.getpid()), "--port", str(PORT)]
    if root:
        cmd += ["--root", str(root)]
    hook = {"type": "command", "command": " ".join(shlex.quote(a) for a in cmd)}
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}})


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
        if any(re.search(rf"(127\.0\.0\.1|localhost|\[::1\]):{port}\b", a) for a in args):
            return f"port {port} is the live claude-ops dashboard; its API pauses and cancels tasks."
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
    notes = workspace_note(p, t["branch"]) + (f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else "")
    notes += peer_brief(p, "task")
    return tpl.format(id=t["id"], title=t["title"], body=t["body"], project=p.title, worktree=t["worktree"],
                      branch=t["branch"], repo=p.repo, dev=p.dev, main=p.main, port=p.port_for(t["id"]),
                      result=result_path(t), checks=checks, notes=notes)


def workspace_note(p, branch=None):
    """How a workspace's worktree is laid out, for the briefs. Empty for a plain project."""
    if not p.repos:
        return ""
    names = ", ".join(f"`{r}/`" for r in p.repos)
    on = (f"Each is a git worktree of its own repo on branch `{branch}`, branched from that repo's `{p.dev}`. "
          "Commit in each repo you change; a repo you leave alone is not merged.") if branch else \
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
    if t["kind"] == "release":
        return release_prompt(p, t)
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
            out.append((f"{r.sub}/:\n" if r.sub else "") + (got or "(none)"))
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
                    if turn.startswith(ANSWERED):
                        said.append((o.get("ts") or 0, "The user answered your question: "
                                     + turn[len(ANSWERED):].strip().removesuffix(ANSWER_TAIL).strip()[:2000]))
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


def run_claude(p, tid, prompt):
    """Run one headless Claude turn in the task worktree. Returns the parsed result file or None.
    The prompt goes in on stdin, which stays open until the turn's result so the user can steer it."""
    t = task(tid)
    if t["state"] in STOPPED:
        if t["state"] == "paused":  # paused before Claude got this prompt: resume with it
            upd(tid, next_prompt=prompt, start_at="claude")
        return None
    if limited():  # it would hit the usage limit at once: wait for the reset with this prompt
        limit_pause(tid, prompt)
        return None
    run_dir = RUNS / str(tid)
    run_dir.mkdir(parents=True, exist_ok=True)
    rp = result_path(t)
    rp.unlink(missing_ok=True)
    turn, kept = prompt, None
    if why := stale_session(t):  # cheaper to start over than to resume: brief, handoff note, then this turn
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
           "--no-chrome", "--name", f"{p.name} #{tid}", "--add-dir", str(IMAGES),
           "--settings", guard_settings(t["worktree"])]
    if t["session_id"]:
        cmd += ["--resume", t["session_id"]]
        model = session_model(t, t["kind"])
    else:  # a new session: what it starts on, until its init event names the model exactly
        model = session_model(t, t["kind"]) if kept is None else kept
        upd(tid, session_model=model, last_call=None, ctx_tokens=None)
    cmd += model_args(model) + effort_args(effort_for(t))
    port = p.port_for(tid)
    env = dict(os.environ, OPS_RESULT=str(rp), OPS_TASK_ID=str(tid), OPS_PORT=str(port), OPS_FROM=f"task:{tid}",
               OPS_DAEMON=DAEMON_URL, **{k: str(v).format(port=port) for k, v in p.env.items()})
    upd(tid, phase="claude working")
    sid = t["session_id"]
    with open(run_dir / "claude.jsonl", "a") as log, open(run_dir / "claude.err", "a") as err:
        log.write(json.dumps({"type": "ops", "prompt": prompt[:2000], "ts": time.time(),
                              **({"turn": turn[:4000]} if kept is not None else {})}) + "\n")  # see handoff_note
        proc = subprocess.Popen(cmd, cwd=t["worktree"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, text=True, start_new_session=True)
        track(tid, proc)
        try:
            send_user(proc.stdin, prompt)
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
            log.write(line)
            log.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
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
                close_steer(tid)  # messages already written still get their turn before claude exits
        close_steer(tid)
        proc.wait()
        untrack(tid)
        with STEER_LOCK:
            INFLIGHT.pop(tid, None)  # written but never read: still pending, so the next turn gets them
    kill_app(p, t["worktree"])
    if limit is not None:
        until = limit_hit(limit)
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
        if t["state"] not in STEER_STATES:
            raise ValueError(f"the worker is not working (task is {t['state']})")
        if t["kind"] == "deploy":
            raise ValueError("a deploy runs its own command; there is no worker to talk to")
        if t["kind"] == "release" and not pipe:
            raise ValueError("the release checker is not running right now")
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

def run_step(key, label, cmd, cwd, log_name, env=None):
    """Run a shell step in cwd, output appended to runs/<key>/<log_name>. Returns (exit code, output tail).
    An int key is a task id: the step shows as its phase and can be cancelled."""
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
        if is_task:
            track(key, proc)
        rc = proc.wait()
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


# ---------------------------------------------------------------- task pipeline

def ensure_worktree(p, tid):
    t = task(tid)
    if t["worktree"] and all(r.at(t["worktree"]).exists() for r in p.parts):
        return
    slug = re.sub(r"[^a-z0-9]+", "-", t["title"].lower()).strip("-")[:40] or "task"
    branch = t["branch"] or f"task/{tid}-{slug}"
    path = p.wt / f"t{tid}"
    upd(tid, phase="setting up the worktree")
    copy_workspace_files(p, path)
    for r in p.parts:
        if r.at(path).exists():  # a workspace worktree that was cut off half made
            continue
        if r.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
            r.git("worktree", "add", str(r.at(path)), branch)
        else:
            r.git("worktree", "add", "-b", branch, str(r.at(path)), p.dev)
    prepare_worktree(p, path)
    upd(tid, branch=branch, worktree=str(path))
    ev(tid, f"worktree {path} on {branch}")


def pipeline(tid):
    try:
        t = task(tid)
        {"release": release_pipeline, "deploy": deploy_pipeline}.get(t["kind"], _pipeline)(proj(t["project"]), tid)
    except Exception as e:  # keep the daemon alive; surface the error on the task
        if not stopped(tid):
            upd(tid, state="failed", phase="", error=f"orchestrator error: {e}", finished=time.time())
            ev(tid, f"orchestrator error: {e}")
    finally:
        THREADS.pop(tid, None)


def _pipeline(p, tid):
    t = task(tid)
    upd(tid, started=t["started"] or time.time(), error=None)
    sending = t["kind"] == "senddev"
    if sending and senddev_of(t).get("landed"):  # merged before a restart, a retry or a cancel: clear now
        return senddev_landed(p, tid, t["merged"], f"merged into {p.dev} earlier")
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
            if applying:
                why = senddev_commit(p, tid)
                if why:
                    return ask_owner(p, tid, why)
            for r in p.parts:
                if r.git("status", "--porcelain", cwd=r.at(wt)):
                    r.git("stash", "push", "-u", "-m", "ops-autostash: uncommitted after worker", cwd=r.at(wt))
                    ev(tid, "stashed uncommitted leftovers (git stash list in the worktree"
                            + (f" of {r.sub})" if r.sub else ")"))
        need_claude = True

        if not changed_parts(p, t["branch"]):
            if sending:  # applying your changes changed nothing: dev has them already
                return senddev_landed(p, tid, None, f"{p.dev} already had these changes")
            return finish(p, tid, merged=None, note="no code changes")

        # Rebase onto the latest dev, gate, then merge (or hold for review).
        upd(tid, state="gating", phase=f"rebasing onto {p.dev}")
        base = p.revs(p.dev)
        for part in p.parts:  # every repo, so the gate checks the latest dev of each
            r = subprocess.run(["git", "rebase", p.dev], cwd=part.at(wt), capture_output=True, text=True)
            if r.returncode:
                subprocess.run(["git", "rebase", "--abort"], cwd=part.at(wt), capture_output=True)
                break
        if stopped(tid):
            return
        if r.returncode:
            if not bump_round(p, tid, "rebase conflict" + (f" in {part.sub}" if part.sub else "")):
                return
            run = f"git -C {part.sub} rebase {p.dev}" if part.sub else f"git rebase {p.dev}"
            prompt = (f"The `{p.dev}` branch{f' of {part.sub}' if part.sub else ''} moved and branch "
                      f"`{t['branch']}` no longer rebases cleanly onto it. Run `{run}`, resolve the conflicts "
                      f"so both your change and the new {p.dev} work survive, re-run the checks, commit, and "
                      f"write $OPS_RESULT again.\n\ngit said:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
            upd(tid, state="running")
            continue
        ok, detail = gate(p, tid, wt)
        if stopped(tid):
            return
        if not ok:
            ev(tid, f"gate failed: {detail.splitlines()[0]}")
            if not bump_round(p, tid, "gate failed:\n" + detail):
                return
            prompt = (f"The queue rebased your branch onto the latest `{p.dev}` and ran its merge check. "
                      f"It failed:\n\n{detail}\n\nFix it in this worktree (keep the history rebased on "
                      f"{p.dev}), re-run the failing step yourself, commit, and write $OPS_RESULT again.")
            upd(tid, state="running")
            continue
        ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))

        if task(tid)["hold"]:
            if p.build:
                run_step(tid, "building for review", p.build, wt, "gate.log")
                if stopped(tid):
                    return
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
                r.git("merge", "--no-ff", "-m", f"Merge task #{tid}: {t['title']}", t["branch"], cwd=r.at(p.dev_wt))
                done.append((r, r.rev(p.dev)))
            merged = " ".join(sha for _, sha in done)
            if sending:  # from here on the work is on dev: a restart or retry goes straight to clearing
                upd(tid, merged=merged, senddev=json.dumps({**senddev_of(task(tid)), "landed": True}))
        where = ", ".join(f"{r.sub} {sha[:8]}" if r.sub else sha[:8] for r, sha in done)
        if sending:
            return senddev_landed(p, tid, merged, f"merged into {p.dev} at {where}")
        return finish(p, tid, merged=merged, note=f"merged into {p.dev} at {where}")


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
    notify(f"{p.title}: task failed", f"#{tid}: {why[:120]}")
    tell_sender(tid, "failed: " + why)


def finish(p, tid, merged, note):
    t = task(tid)
    upd(tid, state="done", phase="", merged=merged, finished=time.time())
    ev(tid, note)
    if t["worktree"] and Path(t["worktree"]).exists():
        kill_app(p, t["worktree"])
        remove_worktree(p, t["worktree"])
    notify(f"{p.title}: task done", f"#{tid}: {t['title']}")
    tell_sender(tid, f"is done ({note}): " + (task(tid)["summary"] or "no summary"))
    if merged:  # a task marked "Release after merge", or a release that ends with more of them in dev
        auto_release(p)


def remove_worktree(p, path):
    for r in p.parts:
        r.git("worktree", "remove", "--force", str(r.at(path)), check=False)
    if p.repos:  # the workspace's own files
        shutil.rmtree(path, ignore_errors=True)
        for r in p.parts:
            r.git("worktree", "prune", check=False)


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


def split(p, tid, res):
    t = task(tid)
    ids = add_subtasks(p, tid, res.get("tasks"), t["priority"], t["hold"], t["auto_release"], t["model"],
                        t["effort"])
    finish(p, tid, merged=None, note="split into " + ", ".join(f"#{i}" for i in ids))


# ---------------------------------------------------------------- release (dev -> main)

def queue_release(p):
    """The Promote button: queue a release check of dev, ahead of ordinary tasks. It never refuses: with a release
    already open it returns that one, and whatever is in the way of moving main the release waits for."""
    open_ = q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN ('done','failed','cancelled')",
              (p.name,), one=True)
    if open_:
        return open_["id"]
    TRAIN.pop(p.name, None)
    body = (f"Check the latest {p.dev} as a whole: the merge check, then a build that a release checker runs "
            f"and plays like a player. If it passes, build it into {p.main} and fast-forward {p.main} to it.")
    if not release_play(p):
        body = (f"Check the latest {p.dev} with the merge check alone (Play-check releases is off). If it passes, "
                f"build it into {p.main} and fast-forward {p.main} to it.")
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
    """Queue a release for tasks marked "Release after merge" once they are in dev and not yet in main, as a train:
    when the project is quiet (none of its tasks or Send to devs queued, running, gating or merging; ones waiting
    on you don't count), or once the first of them merged release_wait minutes ago. The scheduler calls this every
    minute, and a merge at its end. With a release already open this waits: the release's own end calls it again,
    so anything that merged after its snapshot gets the next one. The tasks of one split release together, after
    the last of them merges."""
    with RELEASE_LOCK:
        TRAIN.pop(p.name, None)
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
    ev(rid, "queued after " + ", ".join(f"#{t['id']}" for t in ready) + f" merged (release after merge; {why})")
    return rid


def release_trains():
    """The scheduler's minute check: a train may leave because its time is up, or because the queue went quiet
    without a merge (a task failed, was cancelled, or now waits for your review)."""
    for p in list(PROJECTS.values()):
        try:
            auto_release(p)
        except Exception as e:  # one project's git trouble must not stop the others' trains
            print(f"release train {p.name}:", e, flush=True)


ANSWERED = "The user answered your question:"  # how an answer reaches a task's next turn
ANSWER_TAIL = "Continue the task and write $OPS_RESULT."  # and what follows it
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
    """A gate-only release's summary (Play-check releases off): what passed, then each task's own summary."""
    checks = ", ".join(g["name"] for g in p.gate) or "no checks configured"
    _, done = release_contents(p, t)
    return "\n\n".join([f"Gate-only release: the merge check passed ({checks})"]
                       + [f"#{d['id']} {d['title']}\n{d['summary'] or '(no summary)'}" for d in done])


def release_prompt(p, t):
    log, done = release_contents(p, t)
    changes = "\n".join(f"- {r.sub + ': ' if r.sub else ''}{l.split(' ', 1)[1]}" for r, l in log[:60]) \
        or f"- (nothing: this only rebuilds {p.main})"
    summaries = "\n\n".join(f"#{d['id']} {d['title']}\n{(d['summary'] or '(no summary)')[:1500]}" for d in done)
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured)"
    notes = workspace_note(p) + (f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else "")
    return (CODE / "release_prompt.md").read_text().format(
        id=t["id"], project=p.title, dev=p.dev, main=p.main, base=p.show(t["base"], 40), base_short=p.show(t["base"]),
        checks=checks, changes=changes, summaries=summaries or "(none recorded)", worktree=t["worktree"],
        repo=p.repo, port=p.port_for(t["id"]), build=f" (`{p.build}`)" if p.build else "",
        notes=notes, result=result_path(t))


def release_pipeline(p, tid):
    """Snapshot dev, gate it, have Claude play it (unless Play-check releases is off), then promote exactly that
    commit."""
    t = task(tid)
    start_at, resume = t["start_at"], t["next_prompt"]
    upd(tid, started=t["started"] or time.time(), error=None, next_prompt=None, start_at="claude")
    if not t["base"]:
        with p.lock:
            err = merge_main_into_dev(p)
            if not err:
                upd(tid, base=p.revs(p.dev))
        if err and not resolve_main_into_dev(p, tid, err):
            return
        ev(tid, f"checking {p.dev} at {p.show(task(tid)['base'])}")
    t = task(tid)
    if t["base"] == p.revs(p.main):
        start_at = "promote"  # nothing new to check: this only rebuilds main
        ev(tid, f"nothing new on {p.dev}; rebuilding {p.main}")
    if not (t["worktree"] and all(r.at(t["worktree"]).exists() for r in p.parts)):
        path = p.wt / f"t{tid}"
        copy_workspace_files(p, path)
        for r, sha in p.pairs(t["base"]):
            if not r.at(path).exists():
                r.git("worktree", "add", "--detach", str(r.at(path)), sha)
        prepare_worktree(p, path)
        upd(tid, worktree=str(path))
        ev(tid, f"worktree {path} at {p.show(t['base'])}")
        t = task(tid)
    else:
        for r, sha in p.pairs(t["base"]):
            if r.git("rev-parse", "HEAD", cwd=r.at(t["worktree"])) != sha:  # left at the merge worker's commit
                r.git("checkout", "--force", "--detach", sha, cwd=r.at(t["worktree"]))

    checker = release_play(p)  # off: gate-only, no release checker
    if start_at != "promote":
        if checker and resume and t["session_id"]:  # an answer, a retry or a restart resumes the checker
            prompt = resume
        else:
            upd(tid, state="gating")
            ok, detail = gate(p, tid, t["worktree"])
            if stopped(tid):
                return
            if not ok:
                return fail(p, tid, "gate failed, so the release stops here:\n" + detail)
            ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))
            prompt = release_prompt(p, t) if checker else None
        if not checker:
            upd(tid, summary=gate_only_summary(p, t))
            ev(tid, "gate-only release: Play-check releases is off, so no release checker runs")
        else:
            upd(tid, state="running")
            res = run_claude(p, tid, prompt)
            if stopped(tid):
                return
            if res is None:
                err = (RUNS / str(tid) / "claude.err").read_text(errors="replace")[-1500:]
                return fail(p, tid, "release checker stopped without writing a result.\n" + err)
            upd(tid, summary=res.get("summary") or task(tid)["summary"])
            ids = add_subtasks(p, tid, res.get("tasks"))
            if ids:
                ev(tid, "queued follow-ups " + ", ".join(f"#{i}" for i in ids))
            status = res.get("status")
            if status == "needs_input":
                upd(tid, state="needs_input", phase="", question=res.get("question") or res.get("summary"))
                ev(tid, f"needs your input: {(res.get('question') or res.get('summary') or '')[:1000]}")
                notify(f"{p.title}: release needs input", f"#{tid}: {res.get('question', '')}")
                return
            if status != "done":
                return fail(p, tid, "release blocked: " + (res.get("summary") or f"checker reported {status}"))
            ev(tid, "release check passed")

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
        err = promote(p, tid, t["base"], note=resume if resume and resume.startswith(ANSWERED) else "")
    except NeedsYou as e:
        return wait_for_you(p, tid, str(e), WAIT_CHANGES)
    if stopped(tid):
        return
    if err:
        return fail(p, tid, err)
    upd(tid, start_at="claude")
    finish(p, tid, merged=t["base"], note=f"{p.main} is now {p.show(t['base'])}")  # and releases what merged since
    if Path(p.repo).resolve() == CODE and p.revs(p.main) != before:
        restart_self(tid)


def is_service():
    """This daemon is the launchd service that opsctl manages, not a scratch daemon a test or worker runs.
    launchd's pid for the job is Claude Ops.app's launcher, our parent (or us, from an older plist)."""
    out = subprocess.run(["launchctl", "list", "com.claude-ops"], capture_output=True, text=True).stdout
    m = re.search(r'"PID" = (\d+);', out)
    return bool(m) and int(m.group(1)) in (os.getpid(), os.getppid())


def restart_self(tid):
    """main of claude-ops itself just moved: run `opsctl restart` to load the new code. Running tasks pause and
    the new daemon resumes them. It runs in a session of its own, so stopping this daemon's launchd job spares it."""
    if not is_service():
        ev(tid, "not restarting claude-ops: this daemon is not the launchd service, and opsctl restart "
                "would restart that one")
        return
    ev(tid, "restarting claude-ops to load the new code; running tasks pause and resume")
    (RUNS / str(tid)).mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(CODE / "opsctl"), "restart"], cwd=CODE,  # if the new daemon doesn't answer, it logs that here
                     env=dict(os.environ, OPS_DATA=str(DATA), OPS_RESTART_TASK=str(tid)),
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(RUNS / str(tid) / "restart.log", "a"), stderr=subprocess.STDOUT)


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
# settings usage:<window>, and at usage_brake percent (default 85, 0 = off) of either window the scheduler starts no
# new task until that window resets, which leaves the rest of it for your design chats, chats and answers. Running
# work carries on, and chats are never held.

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


def usage_info(s=None):
    """The usage windows and the brake, for the scheduler and the dashboard: {brake, windows: [{key, label, short,
    pct, resetsAt, resets, ts}], braked}. A reading whose reset has passed is stale and counts as 0. braked is the
    window that holds the queue (the one that resets last, when both do), or None."""
    s = settings_now() if s is None else s
    brake, now, wins = usage_brake_pct(s), time.time(), []
    for k, (label, short) in USAGE_WINDOWS.items():
        r = usage_reading(s.get(f"usage:{k}"))
        if not r:
            continue
        live = r["resetsAt"] > now
        pct = round(r["utilization"] * 100, 6) if live else 0  # rounded: 0.29 * 100 is 28.999999999999996
        wins.append({"key": k, "label": label, "short": short, "pct": round(pct), "exact": pct,
                     "resetsAt": r["resetsAt"], "resets": clock(r["resetsAt"]) if live else "", "ts": r["ts"]})
    over = [w for w in wins if brake and w["exact"] >= brake]
    braked = max(over, key=lambda w: w["resetsAt"]) if over else None
    if braked:
        braked = {**braked, "words": f"Usage at {braked['pct']}% of the {braked['label']} window · starts again "
                                     f"after {braked['resets']}"}
    return {"brake": brake, "windows": wins, "braked": braked}


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
    does a gate-only release (Play-check releases off)."""
    if t["kind"] == "release" and t["project"] in PROJECTS and not release_play(PROJECTS[t["project"]]):
        return False
    return t["kind"] != "deploy" and t["start_at"] == "claude"


# ---------------------------------------------------------------- scheduler

def scheduler():
    n = 0
    while True:
        try:
            schedule_once()
            if n % 3 == 0:  # every 6 s: a scan for a running build takes up to 0.4 s
                unblock_releases()
            if n % 30 == 0 and not STOPPING.is_set():  # every minute: release trains whose time is up
                release_trains()
        except Exception as e:
            print("scheduler error:", e, flush=True)
        n += 1
        time.sleep(2)


def schedule_once():
    waiting = limited()  # first, so the limit clears and its tasks requeue even while the queue is paused
    if setting("paused") == "1" or STOPPING.is_set() or waiting:
        return
    braked = usage_braked()  # running work carries on; no new Claude session starts until the window resets
    slots = int(setting("max_parallel") or 1) - len(THREADS)
    for t in q("SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, id"):
        if slots <= 0:
            break
        p = PROJECTS.get(t["project"])
        if not p or setting(f"paused:{p.name}") == "1":
            continue
        if braked and starts_claude(t):
            continue
        if t["id"] in THREADS:  # resumed before its paused pipeline finished stopping
            continue
        if p.max_parallel:
            mine = sum(1 for i in THREADS if (task(i) or {}).get("project") == p.name)
            if mine >= p.max_parallel:
                continue
        if t["depends_on"]:
            dep = task(t["depends_on"])
            if dep and dep["state"] != "done":
                continue
        if exclusive_blocker(t):
            continue
        upd(t["id"], state="running", phase="starting", pid=None, pid_start=None)
        if task(t["id"])["state"] != "running":  # paused or cancelled just now
            continue
        ev(t["id"], "started")
        th = threading.Thread(target=pipeline, args=(t["id"],), daemon=True)
        THREADS[t["id"]] = th
        th.start()
        slots -= 1


def exclusive_blocker(t):
    """The deploy or release of t's project that t has to wait for, or None. A project runs one deploy at a time,
    and never a deploy and a release at once: both move main. Releases are one at a time already (queue_release).
    An open one blocks unless it is queued behind t too, so two waiting ones never wait for each other."""
    if t["kind"] not in ("deploy", "release"):
        return None
    kinds = ("deploy", "release") if t["kind"] == "deploy" else ("deploy",)
    for o in q(f"SELECT id,kind,state,priority FROM tasks WHERE project=? AND id!=? AND kind IN "
               f"({','.join('?' * len(kinds))}) AND state NOT IN (?,?,?) ORDER BY priority DESC, id",
               (t["project"], t["id"], *kinds, *ENDED)):
        if o["state"] != "queued" or (-o["priority"], o["id"]) < (-t["priority"], t["id"]):
            return o
    return None


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
    for t in q("SELECT id FROM tasks WHERE state='paused' AND paused_by='restart'"):
        upd(t["id"], state="queued", paused_by=None)
        ev(t["id"], "resumed after the restart")
    for t in q(f"SELECT * FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        killgroup(own_pid(t))  # a worker from the old daemon would otherwise run alongside its resume
        upd(t["id"], state="queued", phase="", pid=None, pid_start=None, next_prompt=RESTART_NOTE)
        ev(t["id"], "requeued after daemon restart")


def shutdown():
    """The daemon is stopping (opsctl restart or stop, launchd, a kill): start nothing new, pause every running
    task for the next daemon to resume, and give a merge, which can't be paused, a moment to finish."""
    STOPPING.set()
    for t in q(f"SELECT id FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        try:
            pause_task(t["id"], by="restart")
        except ValueError:
            pass  # merging
    deadline = time.time() + 45  # launchd's ExitTimeOut (opsctl) is 60
    while q("SELECT id FROM tasks WHERE state='merging'") and time.time() < deadline:
        time.sleep(0.5)


def pause_task(tid, by=None):
    """Stop a task now and keep its place: a worker resumes its Claude session, a gate starts over.
    by='restart': the daemon is stopping, and the next one resumes the task.
    by='limit': its worker hit the usage limit, and the queue resumes it after the reset (see limit_clear)."""
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
                                 None)) for x in f.get("deploys") or []]
    return {"name": p.name, "title": p.title, "main_branch": p.main, "dev_branch": p.dev,
            "main": p.show(f["main"]), "built": p.show(built), "dev": p.show(f["dev"]), "ahead": f["ahead"],
            "has_app": bool(p.build), "can_launch": bool(p.launch),
            "stale": bool(p.build) and built != f["main"], "running": f["running"],
            "has_build": bool(p.build or p.launch) and f["has_build"],  # without a build, Play main needs none
            "dev_running": f.get("dev_running", False), "play": PLAYS.get(p.name),
            "dev_stale": bool(p.build) and (dev_built != f["dev"] or not f.get("dev_has_build")),
            "paused": s.get(f"paused:{p.name}") == "1",
            "auto_release": s.get(f"auto_release:{p.name}") == "1",  # release_default(p), from the snapshot
            "release_play": s.get(f"release_play:{p.name}") != "0",  # release_play(p), from the snapshot
            "train": TRAIN.get(p.name),  # merged tasks waiting for the release train: {"ids", "by"}
            "gate": [g["name"] for g in p.gate], "release": rel,
            "deploys": deploys, "deploys_loading": bool(p.deploys) and f.get("deploys") is None,
            "deploy_open": opens[0]["id"] if opens else None, "uncommitted": f.get("uncommitted") or [],
            "senddev_open": senddev_open(p), "senddev_pending": f.get("senddev") or []}


# ---------------------------------------------------------------- project facts for the dashboard
# Git and the process table are slow next to the database: a git call takes 10-20 ms and a scan for a running
# app 0.2-0.4 s, and every open dashboard tab asks for /api/state every few seconds. So /api/state answers from
# FACTS at once and rereads an entry older than FACTS_TTL behind the request: what it shows is at most a poll old.
# Every task event marks its project's entry stale and rereads it, so a merge or promote shows on the next poll.

FACTS = {}  # project name -> {"at": when read, "main", "dev": revs, "ahead": [lines], "running", "dev_running",
#   "has_build", "dev_has_build", "deploys"}
FACTS_TTL = 5
FACTS_BUSY = set()  # projects being reread now, so a burst of requests starts one reread, not one each
FACTS_LOCK = threading.Lock()
AHEAD = {}  # (repo, main sha, dev sha) -> dev's first-parent log since main; a pair of shas never changes


def read_facts(p, deploys=True):
    try:
        main, dev, ahead = [], [], []
        for r in p.parts:
            m, d = r.git("rev-parse", p.main, p.dev).split()
            main.append(m)
            dev.append(d)
            key = (str(r.src), m, d)
            if key not in AHEAD:
                if len(AHEAD) > 500:
                    AHEAD.clear()
                AHEAD[key] = r.git("log", "--first-parent", "--format=%h %s", f"{m}..{d}").splitlines()
            ahead += [f"{r.sub + ': ' if r.sub else ''}{l}" for l in AHEAD[key]]
        exes = my_exes() if p.process and not p.running_cmd else None  # one read of the process table for both
        f = {"main": " ".join(main), "dev": " ".join(dev), "ahead": ahead[:30],
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


def promote_blocker(p, commit):
    """What only you can clear before main moves to commit, or None. The release needs you while there is one.
    Your uncommitted changes are not one: move_main carries them over."""
    if app_running(p, p.main_wt):
        return (f"Quit the {p.main} build: it is rebuilt in {p.main_wt}. The release goes on by itself once it "
                "has quit.")
    for r, sha in p.pairs(commit):
        if r.git("branch", "--show-current", check=False) != p.main:  # main moves as a branch; your checkout stays
            wt = r.git("worktree", "list", "--porcelain", check=False).split("\n\n")
            held = [w.split("\n")[0][9:] for w in wt if f"branch refs/heads/{p.main}" in w.split("\n")]
            if held:
                return (f"{p.main} is checked out in {held[0]}. Switch that one off it, and the release goes on "
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
            return f"{p.main}{f' of {r.sub}' if r.sub else ''} could not fast-forward: it has commits {p.dev} lacks."
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
            return f"{p.main}{f' of {r.sub}' if r.sub else ''} could not fast-forward:\n" + res.stderr[-1000:]
    if left is not None:
        left[:] = [(r, paths, touched if r in carried else []) for r, paths, touched in kept]
    return None


def merge_main_into_dev(p):
    """Commits made straight to main (outside the queue) come along into dev. Returns an error or None.
    Call with p.lock held."""
    for part in p.parts:
        if subprocess.run(["git", "merge-base", "--is-ancestor", p.main, p.dev], cwd=part.src).returncode == 0:
            continue
        r = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", p.main],
                           cwd=part.at(p.dev_wt), capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "merge", "--abort"], cwd=part.at(p.dev_wt), capture_output=True)
            return (f"{p.main}{f' of {part.sub}' if part.sub else ''} has commits that conflict with {p.dev}:\n"
                    + r.stdout[-1500:] + r.stderr[-1500:])
    return None


def resolve_main_into_dev(p, tid, conflict):
    """main conflicts with dev: a worker merges main in the release worktree, resolves the conflicts and runs the
    checks, then dev moves to its merge and the release goes on from there. In a workspace each repo merges its
    own main. A pause, a restart or a retry starts the merge over.
    Returns True once dev contains main (the task's base is set); False if it failed or stopped."""
    path = p.wt / f"t{tid}"
    for _ in range(MAX_FIX_ROUNDS):  # dev can move while the worker merges; then it merges again
        fresh = not all(r.at(path).exists() for r in p.parts)
        copy_workspace_files(p, path)
        for r in p.parts:
            if r.at(path).exists():
                for a in (("merge", "--abort"), ("reset", "--hard"), ("clean", "-fd")):
                    subprocess.run(["git", *a], cwd=r.at(path), capture_output=True)
                r.git("checkout", "--detach", p.dev, cwd=r.at(path))
            else:
                r.git("worktree", "add", "--detach", str(r.at(path)), p.dev)
        if fresh:
            prepare_worktree(p, path)
        upd(tid, worktree=str(path), session_id=None)
        conflicts = []
        for r in p.parts:
            m = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", p.main],
                               cwd=r.at(path), capture_output=True, text=True)
            if m.returncode:
                conflicts.append((f"In {r.sub}/:\n" if r.sub else "") + m.stdout[-2500:] + m.stderr[-1000:])
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
            upd(tid, base=p.revs(p.dev), session_id=None)  # the release checker starts its own session
        ev(tid, f"merged {p.main} into {p.dev} at {p.show(heads)}")
        return True
    fail(p, tid, f"{p.dev} kept moving while a worker merged {p.main} into it. Click Retry.")
    return False


def resolve_prompt(p, t, conflict):
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured: run whatever tests the repo has)"
    notes = f"## Notes for {p.title}\n\n{p.worker_notes}\n\n" if p.worker_notes else ""
    where = (" in each of its repos (" + ", ".join(f"{r.sub}/" for r in p.parts) + "), and each repo merges its "
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
            r.git("checkout", "--detach", sha, cwd=r.at(p.main_wt), check=False)
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
                subprocess.run(["cp", "-cR", str(art), str(prev)], capture_output=True)
        old = [(r, r.git("rev-parse", "HEAD", cwd=r.at(p.main_wt))) for r in p.parts]
        for r, sha in p.pairs(commit):
            r.git("checkout", "--detach", sha, cwd=r.at(p.main_wt))
        prepare_main(p, lambda text: upd(tid, phase=text))
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
        tid = add_task(p.name, senddev_body(p, repos), f"Send uncommitted changes in {senddev_where(p, repos)} to {p.dev}",
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
    return f"{p.repo.name} ({', '.join(x['sub'] for x in repos)})"


def senddev_body(p, repos):
    lines = [f"Send to dev, from the banner on the project card. Your checkout's uncommitted work, as it was at the "
             f"click, lands on {p.dev} as a normal change:", ""]
    for x in repos:
        n = len(x["files"])
        lines.append(f"{x['repo']}" + (f" (on {x['branch']})" if x["branch"] else "") + f": {n} {files(n)}")
        lines += [f"- {f}" for f in x["files"][:50]] + ([f"- …and {n - 50} more"] if n > 50 else []) + [""]
    lines.append(f"A copy is kept at refs/ops/senddev-<this task's id>. The queue applies it onto a branch made from "
                 f"{p.dev} (a worker resolves any conflicts), runs the gate and merges it into {p.dev}, as for any task. "
                 "Only then are the files that still match the snapshot cleared from your checkout. Stopping or "
                 "cancelling before the merge leaves your checkout as it is.")
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


def senddev_apply(p, tid):
    """Apply the snapshot onto the task's branch, reset to the latest dev, in each repo it changes, as merge_wip does
    for a release. A repo that applies cleanly is committed at once. Returns "" when every repo is, the prompt for a
    worker to resolve the conflicts, or None if git can't apply it at all (the task then needs you)."""
    t = task(tid)
    d = senddev_of(t)
    upd(tid, phase=f"applying your changes onto {p.dev}")
    conflicts, made = [], 0
    for x in d["repos"]:
        r = part_of(p, x["sub"])
        wt = r.at(t["worktree"])
        x["keep"] = x.get("keep") or senddev_keep(r, tid, x, t["created"])
        for a in (("cherry-pick", "--quit"), ("reset", "--hard", "-q", p.dev), ("clean", "-fdq")):
            subprocess.run(["git", *a], cwd=wt, capture_output=True)
        m = subprocess.run(["git", "cherry-pick", "--no-commit", x["keep"]], cwd=wt, capture_output=True, text=True)
        x["conflicts"] = paths_of(r, "diff", "--name-only", "--diff-filter=U", cwd=wt) if m.returncode else []
        if m.returncode and not x["conflicts"]:
            senddev_save(tid, d)
            ask_owner(p, tid, f"git could not apply your uncommitted changes in {x['repo']} onto {p.dev}: "
                              + (m.stderr or m.stdout).strip()[-1500:])
            return None
        if x["conflicts"]:
            conflicts.append(x)
        else:
            made += senddev_commit_repo(r, wt, x)
    d["applied"] = not conflicts
    senddev_save(tid, d)
    if not conflicts:
        ev(tid, f"applied your changes onto {p.dev} and committed them" if made else
                f"applied your changes onto {p.dev}: it has them already, so there is nothing to commit")
        return ""
    n = sum(len(x["conflicts"]) for x in conflicts)
    ev(tid, f"your changes conflict with {p.dev} in {n} {files(n)}; a worker is merging them")
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
            return (f"The worker said it merged your changes in {x['repo']} with {p.dev}, but conflicts are left in "
                    f"{name_list(left)}. Say how to combine them and the worker tries again, or cancel to keep them "
                    "only in your checkout.")
        if not int(r.git("rev-list", "--count", f"{p.dev}..HEAD", cwd=wt) or 0):  # it resolved but didn't commit
            r.git("add", "-A", "--", *x["conflicts"], cwd=wt, check=False)
            senddev_commit_repo(r, wt, x)
        subprocess.run(["git", "cherry-pick", "--quit"], cwd=wt, capture_output=True)
    d["applied"] = True
    senddev_save(tid, d)
    ev(tid, f"your changes are merged with {p.dev} and committed")
    return None


def senddev_brief(p, t):
    """What a Send to dev worker needs to know whatever it is asked: the story, the rules, and how to report."""
    d = senddev_of(t)
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured)"
    notes = workspace_note(p, t["branch"]) + (f"## Notes for {p.title}\n\n{p.worker_notes}\n\n" if p.worker_notes else "")
    sent = "".join(f"- {x['repo']}: {len(x['files'])} {files(len(x['files']))}, kept as commit {x['keep'][:12]} "
                   f"(`refs/ops/senddev-{t['id']}`) on the user's HEAD {x['head'][:12]}\n" for x in d["repos"])
    return (f"You are an unattended worker on the {p.title} project, {SENDDEV_HEAD} (task #{t['id']}). Nobody "
            f"watches this session and nobody can answer you mid-run.\n\n"
            f"The user's checkout had uncommitted changes: unfinished work on no branch, so workers didn't see it and "
            f"no release shipped it. The user clicked Send to dev, which lands that work on `{p.dev}` as a normal "
            f"change:\n\n{sent}\n"
            f"This worktree ({t['worktree']}) is on branch `{t['branch']}`, made from `{p.dev}`. The queue applied "
            f"the user's changes here with `git cherry-pick --no-commit` of that commit, and commits them per repo as "
            f"`Your uncommitted changes from <repo>`, with the files listed. After you, it rebases the branch onto the "
            f"latest `{p.dev}`, runs the merge check ({checks}) and merges it into `{p.dev}`. Then it clears the sent "
            f"files from the user's checkout.\n\n"
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
        where = f"`{x['sub']}/`" if x["sub"] else "this worktree"
        parts.append(
            f"In {where} ({x['repo']}), `git cherry-pick --no-commit {x['keep'][:12]}` stopped on conflicts in:\n\n"
            + "".join(f"- {f}\n" for f in x["conflicts"]) +
            f"\nThe `{p.dev}` side is checked work that passed the gate: `git diff {x['head'][:12]} HEAD -- <file>` "
            f"shows what it changed since the user's base. The user's side is their unfinished work being landed: "
            f"`git diff {x['head'][:12]} {x['keep'][:12]} -- <file>`. Once resolved, `git add` those files and commit "
            f"everything staged with exactly:\n\n    git commit -m {shlex.quote(subject)} -m {shlex.quote(body)}\n")
    return (senddev_brief(p, t) + "\n\n## Now: merge the user's changes with " + p.dev + "\n\n" + "\n".join(parts) +
            f"\nResolve every conflict so both sides survive. Where they truly contradict, keep what the user meant and "
            f"fit it to `{p.dev}`'s code. Don't abort the cherry-pick, reset, rebase or stash. Then run the checks "
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
    finish(p, tid, merged=merged, note=note)


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


def tags(p, t, pattern, merged=None):
    """Tags matching pattern (a glob) in the target's repos, newest first by creatordate: [{name, commit, date}].
    merged: a revs string; only tags reachable from it count."""
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
                out.append({"name": name, "commit": commit, "date": date, "ts": int(ts or 0)})
    return [{k: v for k, v in x.items() if k != "ts"} for x in sorted(out, key=lambda x: -x["ts"])]


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
            for r, sha in p.pairs(revs):
                checkout_detached(r, r.at(path), sha)
            v = run_version(t, t.folder(path))
        except (ValueError, RuntimeError) as e:
            VERSIONS[key] = (time.time(), str(e))
            raise ValueError(str(e))
    if len(VERSIONS) > 500:
        VERSIONS.clear()
    VERSIONS[key] = (time.time(), v)
    return v


def checkout_detached(r, wt, sha):
    """Put repo r's worktree wt at sha, detached, its tracked files exactly as committed. Never `git clean`:
    untracked and ignored files (a build's output) stay. Makes the worktree if it is missing."""
    if not wt.exists():
        r.git("worktree", "prune", check=False)  # a worktree whose folder was deleted is still registered
        r.git("worktree", "add", "--detach", str(wt), sha)
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
           "version": None,
           "last": rec and {"commit": rec.get("commit"), "show": target_show(p, t, rec.get("commit") or ""),
                            "time": rec.get("time"), "task": rec.get("task"), "version": rec.get("version")}}
    if t.error:
        return out
    try:
        revs = deploy_input(p, t, main)
    except ValueError as e:
        out["blocked"] = str(e)  # "Deploy swish test first."
        return out
    pairs = [(r, sha) for r, sha in p.pairs(revs) if r in t.parts(p)]
    subject = " · ".join((f"{r.sub}: " if r.sub and not t.repo else "") + r.git("log", "-1", "--format=%s", sha)
                         for r, sha in pairs)
    commit = target_commit(p, t, revs)
    out["commit"] = {"sha": commit, "show": target_show(p, t, commit), "subject": subject}
    if rec and rec.get("revs"):
        try:
            out["behind"] = sum(int(r.git("rev-list", "--count", f"{old}..{sha}"))
                                for (r, sha), old in zip(p.pairs(revs), rec["revs"].split()) if r in t.parts(p))
        except RuntimeError:
            pass  # the recorded commit is gone (a rewritten history): how far behind is unknown
    if t.version:
        out["version"] = version_facts(p, t, revs)
    return out


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
    fresh = not all(r.at(path).exists() for r in p.parts)
    for r, sha in p.pairs(revs):
        checkout_detached(r, r.at(path), sha)
    if fresh:
        prepare_worktree(p, path, setup=False)  # the clone list; setup runs below, where its output is logged
    ev(tid, f"deploying {target_show(p, tg, target_commit(p, tg, revs))} in {path}")
    key = f"deploy_setup:{p.name}"
    if p.setup and (fresh or setting(key) != revs):  # setup again only when the commit changed
        for i, cmd in enumerate(p.setup, 1):
            rc, tail = run_step(tid, f"setup {i}/{len(p.setup)}", cmd, path, "deploy.log",
                                env=dict(os.environ, OPS_REPO=str(p.repo)))
            if stopped(tid):
                return False
            if rc:
                fail(p, tid, f"setup `{cmd}` exited {rc}.\n\n{tail[-2000:]}")
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
        fail(p, tid, f"`{tg.run}` exited {rc}. {p.main} and the record of {tg.name} are unchanged; the deploy "
                     f"worktree {path} is left as it is. Retry starts the deploy over.\n\n{tail[-2000:]}")
        return False
    upd(tid, state="merging", phase="recording the deploy")
    heads = " ".join(r.git("rev-parse", "HEAD", cwd=r.at(path)) for r in p.parts)
    moved = [(r, a, b) for r, a, b in zip(p.parts, revs.split(), heads.split()) if a != b]
    for r, _, b in moved:
        r.git("update-ref", f"refs/ops/deploy-{tid}", b)
    bad = [f"{r.sub or 'the repo'} at {b[:8]}" for r, a, b in moved if not is_ancestor(r, a, b)]
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
    answer = t["next_prompt"] if (t["next_prompt"] or "").startswith(ANSWERED) else ""
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
        for r, sha in p.pairs(commit):
            if not r.at(path).exists():
                r.git("worktree", "add", "--detach", str(r.at(path)), sha)
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
        remove_worktree(p, t["worktree"])
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
QUEUE_BLOCK = re.compile(r"```queue[ \t]*\n(.*?)```", re.S)


def design(did):
    return q("SELECT * FROM designs WHERE id=?", (did,), one=True)


# A chat is "ready for you" when Claude isn't replying, and has a new reply when its last answer (Claude's, or a
# note from the queue) came after you last looked at it. Ending a chat takes it out of both until you reopen it.
CHAT_COLS = ("id,project,title,state,updated,cost,kind,spec,spec_draft,seen,ended,(SELECT MAX(ts) FROM design_msgs m "
             "WHERE m.design_id=designs.id AND m.role!='user') AS replied")


def chat_counts():
    """Open chats per kind, for the badges on the Design and Chat tabs: how many have a new reply, how many
    are waiting on Claude. Each project's counts separately, so the dashboard can apply its project filter."""
    return q(f"SELECT project, kind, SUM(state!='thinking' AND COALESCE(replied,0)>seen) AS unread, "
             f"SUM(state='thinking') AS replying FROM (SELECT {CHAT_COLS} FROM designs WHERE ended IS NULL) "
             f"GROUP BY project, kind")


def design_say(did, text):
    """Start one design-chat turn in the background."""
    d = design(did)
    if d["state"] == "thinking":
        raise ValueError("Claude is still replying")
    q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "user", text, time.time()))
    q("UPDATE designs SET state='thinking', updated=?, ended=NULL WHERE id=?", (time.time(), did))  # reopens it
    threading.Thread(target=design_turn, args=(did, text), daemon=True).start()


def ops_note(p, did, extra=""):
    """Context the dashboard adds to each user message: the queue, and what happened since the last one
    (what the last queue block did, edits saved to dev, tasks queued with the buttons)."""
    live = q("SELECT id,state,title FROM tasks WHERE project=? AND state NOT IN ('done','cancelled') ORDER BY id",
             (p.name,))
    parts = ["queue: " + ("; ".join(f"#{t['id']} {t['state']} {t['title']}" for t in live[:25]) or "empty"),
             "release after merge: " + ("on" if release_default(p) else "off") + " by default for this project"]
    dirty = facts(p).get("uncommitted") or []
    if dirty:
        parts.append("checkout: " + "; ".join(
            f"{u['count']} uncommitted {files(u['count'])} in {u['repo']} (not on {p.dev}"
            + (f"; oldest {day(u['oldest'])}" if u["oldest"] else "") + ")" for u in dirty))
    parts += news(did) + ([extra] if extra else [])
    return "[ops: " + " | ".join(parts) + "]\n\n"


def news(did):
    """The notes left in a chat since your last message: what its queue block did, edits saved, word from a
    project it sent something to."""
    prev = q("SELECT id FROM design_msgs WHERE design_id=? AND role='user' ORDER BY id DESC LIMIT 1 OFFSET 1",
             (did,), one=True)
    return [m["note"] for m in q("SELECT note FROM design_msgs WHERE design_id=? AND role!='user' AND id>? "
                                 "AND note!='' ORDER BY id", (did, prev["id"] if prev else 0))]


def design_system_prompt(p, d=None, cwd=None):
    notes = f"## Notes for {p.title}\n\n{p.design_notes}\n" if p.design_notes else ""
    out = (CODE / "design_prompt.md").read_text().format(project=p.title, dev_wt=cwd or p.dev_wt, dev=p.dev,
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


def design_turn(did, text):
    d = design(did)
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
            return
        tools += spec_tools(cwd)
    tools += f",Bash({SEND_CMD}:*)"  # read-only still: it only asks another project to do something
    if chat:  # your own settings and MCP servers apply, like the CLI; auto mode stands in for prompts
        said = news(did)  # word from a project it sent something to; a design chat gets it in ops_note
        cmd = [CLAUDE, "-p", ("[ops: " + " | ".join(said) + "]\n\n" if said else "") + with_images(text),
               "--output-format", "stream-json", "--verbose",
               "--permission-mode", "auto", "--permission-prompts", "none", "--no-chrome",
               "--name", f"{p.name} chat #{did}", "--settings", guard_settings(None),
               "--append-system-prompt", PROJECT_CHAT_PROMPT + "\n\n" + peer_brief(p, "chat")]
    else:
        note, system = ops_note(p, did, extra), design_system_prompt(p, d, cwd)
        if text.strip().startswith("/"):  # the CLI runs a slash command only from the start of the prompt
            system = system.rstrip() + "\n\n## Dashboard note for this turn\n\n" + note.strip()
            text, note = text.strip(), ""
        cmd = [CLAUDE, "-p", note + with_images(text), "--output-format", "stream-json",
               "--verbose", "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", tools,
               "--strict-mcp-config", "--no-chrome", "--name", f"{p.name} design #{did}",
               "--append-system-prompt", system]
        if spec_chat(d):  # deny rules win over the allowed-tools a command's own frontmatter grants
            cmd += ["--disallowedTools", SPEC_DENY]
    cmd += ["--add-dir", str(IMAGES)]
    if d["session_id"]:
        cmd += ["--resume", d["session_id"]]
    else:  # a new session: what it starts on, until its init event names the model exactly
        q("UPDATE designs SET session_model=? WHERE id=?", (session_model(d, d["kind"]), did))
    cmd += model_args(session_model(d, d["kind"])) + effort_args(effort_for())

    def on_session(sid):
        if sid != design(did)["session_id"]:
            q("UPDATE designs SET session_id=? WHERE id=?", (sid, did))

    def on_model(m):
        q("UPDATE designs SET session_model=? WHERE id=?", (m, did))

    cost = 0.0
    try:
        reply, cost = stream_claude(cmd, cwd, RUNS / f"design-{did}", DESIGN_LIVE[did], DESIGN_PROCS, did,
                                    on_session, env=dict(os.environ, OPS_FROM=f"chat:{did}", OPS_DAEMON=DAEMON_URL),
                                    on_model=on_model)
        if design(did)["ended"] and reply.startswith("(no reply;"):  # End stopped it; say so, not the exit code
            reply = "You ended the chat; the reply was stopped."
        attached = spec_attach(p, d, before) if before is not None else None  # (text, note) or None
        saved = spec_land(p, design(did)) if d["spec"] or attached else ""  # before any task it queues starts from dev
        reply, ids, note = (reply, [], "") if chat else queue_from_reply(p, reply, did)
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
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
          (did, "assistant", f"(chat error: {e})", time.time()))
    finally:
        DESIGN_PROCS.pop(did, None)
        DESIGN_LIVE.pop(did, None)
        # A chat ended mid-reply stays read: what the stopped turn wrote last isn't a new reply when you reopen it
        now = time.time()
        q("UPDATE designs SET state='idle', updated=?, cost=cost+?, seen=CASE WHEN ended IS NULL THEN seen ELSE ? END "
          "WHERE id=?", (now, cost, now, did))


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
            tid = add_task(p.name, it.get("body") or it.get("title", ""), it.get("title", ""),
                           priority=int(it.get("priority") or 0), hold=bool(it.get("hold")), depends_on=dep,
                           auto_release=None if it.get("release") is None else bool(it.get("release")),
                           model=model, effort=effort)
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
SPEC_BIG = (25, 3)  # open items and phases from which one task per phase is the advice
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
TK_OPENSPEC = Path.home() / ".claude" / "commands" / "tk" / "openspec"
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
            text = (TK_OPENSPEC / f"{cid}.md").is_file() and f"/tk:openspec:{cid} {name}"
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
            "advice": "phases" if n >= SPEC_BIG[0] and len(groups) >= SPEC_BIG[1] else "whole"}


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
    where = f"{p.name}/{r.sub}" if r.sub else p.name
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
        print(f"project {p.name} openspec {what}{f' ({r.sub})' if r.sub else ''}: {sha[:8]}, {n} files", flush=True)
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
        raise ValueError(f"{(r.sub + '/') if r.sub else ''}openspec/ isn't on {p.dev}: run openspec init there first")
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
    rel = f"{r.sub}/{SPEC_DIR / name}" if r.sub else str(SPEC_DIR / name)
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
            + (f"- Work through the items with `/tk:openspec:implement {name}` (the Skill tool), still within "
               f"this task's scope and these rules.\n" if (TK_OPENSPEC / "implement.md").is_file() else "")
            + f"{split}\n"
            f"Done when every box this task covers is ticked, the tests pass, and the change's docs match what "
            f"you built.")


def spec_queue(did, plan, hold=False, release=None):
    """Queue a chat's change as one task, or one task per phase chained in order. Returns the new ids.
    release: None takes the project's default; only the last task of a chain releases."""
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
PROJECT_CHAT_PROMPT = ("You are talking with the owner through the claude-ops dashboard, not a terminal. "
                       "Nobody can answer permission prompts here: actions that would prompt are denied. "
                       "When one is denied, say what you tried, and that they can run it themselves or "
                       "continue this session in a terminal with `claude --resume`. " + GUARD_RULE + " "
                       "Reading code: search first (`grep -n`, Grep, Glob), then read only the range you need "
                       "(Read with offset and limit, or `sed -n 'a,bp'`), 150 lines or fewer at a time. Never Read a "
                       "file over about 500 lines without a range, and never `cat` one. Don't reread what is already "
                       "in your context. Keep command output short: pipe it through `tail` or `grep`, and use quiet "
                       "flags for tests and builds.")
CHAT_STATES = ("review", "needs_input", "failed", "done", "cancelled", "paused")  # the worker is not running


def task_ask(tid, text):
    """Ask the task's worker a question: resume its Claude session, read-only, in the background."""
    t = task(tid)
    if not text:
        raise ValueError("ask something")
    if t["state"] not in CHAT_STATES:
        raise ValueError("the worker is busy; ask when it stops")
    if not t["session_id"]:
        raise ValueError("this task has no worker session to ask")
    if tid in TASK_CHAT_LIVE:
        raise ValueError("wait for the reply first")
    TASK_CHAT_LIVE[tid] = []
    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "user", text, time.time()))
    threading.Thread(target=task_chat_turn, args=(tid, text), daemon=True).start()


def task_chat_turn(tid, text):
    t = task(tid)
    p = proj(t["project"])
    cwd = Path(t["worktree"] or p.wt / f"t{tid}")
    made = not cwd.exists()  # the worktree goes after a merge, but the session lives under its path
    note = (f"[ops: the user is asking about this task on the dashboard; its state is {t['state']}. Answer them. "
            "This turn is read-only: do not change files and do not write $OPS_RESULT. If they want a change, "
            "tell them which button does it (Send back with feedback, Send answer, Retry, Resume, or a new task); "
            "you will carry this conversation into that work."
            + (f" Your worktree was removed after the task finished; the merged code is in {p.dev_wt}."
               if made else "") + "]\n\n")
    cmd = [CLAUDE, "-p", note + with_images(text), "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", DESIGN_TOOLS,
           "--strict-mcp-config", "--no-chrome", "--resume", t["session_id"], "--add-dir", str(IMAGES)]
    if made:
        cmd += ["--add-dir", str(p.dev_wt)]
    cmd += model_args(session_model(t, t["kind"])) + effort_args(effort_for(t))  # the worker's own session: its model

    def on_session(sid):
        if sid != task(tid)["session_id"]:
            upd(tid, session_id=sid)

    try:
        if made:
            cwd.mkdir(parents=True)
        reply, cost = stream_claude(cmd, cwd, RUNS / str(tid) / "chat", TASK_CHAT_LIVE[tid], TASK_CHAT_PROCS,
                                    tid, on_session,  # its calls warm the worker's cache: see stale_session
                                    on_call=lambda ctx: upd(tid, last_call=time.time(), ctx_tokens=ctx))
        upd(tid, cost=(task(tid)["cost"] or 0) + cost)
    except Exception as e:
        reply = f"(chat error: {e})"
    finally:
        TASK_CHAT_PROCS.pop(tid, None)
        if made:
            try:
                cwd.rmdir()
            except OSError:
                pass
    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)", (tid, "assistant", reply, time.time()))
    TASK_CHAT_LIVE.pop(tid, None)


# ---------------------------------------------------------------- peers (one project asks another)
# A worker or chat can hand work to another project, as one Claude session messages a peer: `opsd.py send` (the
# daemon gives each session OPS_FROM, naming its task or chat, and OPS_DAEMON) starts a task, a design chat or a
# project chat there. The new task or chat says who sent it and where that session works, and shows on the
# dashboard like any other. How it ends (a task) or its first reply (a chat) goes back to the sender: an event on
# a task, a note in a chat, which a design chat's next turn reads.

SEND_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(CODE / 'opsd.py'))} send"
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
    """"claude-ops task #12", "diskgame design chat #3"."""
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


def send_peer(ref, project, kind, text, title="", hold=False, model=None, effort=None):
    """Start a task or chat in project for the session ref names. Returns {kind, id, project, said}.
    model, effort: the task's, as add_task takes them; a chat starts on its kind's model, at the effort setting."""
    p, text = proj(project), (text or "").strip()
    if kind not in PEER_KINDS:
        raise ValueError(f"kind is one of {', '.join(PEER_KINDS)}")
    model = model_name(model)
    if model and kind != "task":
        raise ValueError("--model is for tasks; a chat starts on the project's chat or design model")
    effort = effort_name(effort)
    if effort and kind != "task":
        raise ValueError("--effort is for tasks; a chat runs at the effort setting")
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
        tid = add_task(p.name, f"{head}\n\n{text}", title, hold=hold, origin=o, model=model, effort=effort)
        ev(tid, f"sent by {who}")
        if o:
            tell_origin(o, f"Sent task #{tid} to {p.title}: {task(tid)['title']}", [tid])
        return {"kind": "task", "id": tid, "project": p.name,
                "said": f"Queued task #{tid} in {p.title} ({p.name}). It shows on the dashboard"
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

The other session can't see this conversation, so the message must stand alone: the goal and why, exactly what
to do, and how to tell it is done. The queue adds who sent it and where you work. The command prints the new
task's or chat's id, and {back}. {when}

"""


# A task page asks for its worker's transcript every few seconds, and a transcript grows to megabytes. It only
# ever grows, so each read starts where the last one stopped.
FEEDS = {}  # task id -> (bytes read, the newest activity entries)
STEPS = {}  # task id -> ((size, mtime) of the transcript, last_step's answer)
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
        if STEPS.get(tid, (None,))[0] == (st.st_size, st.st_mtime):
            return STEPS[tid][1]  # the transcript has not moved since the last poll
        with open(f, "rb") as fh:
            fh.seek(max(0, fh.seek(0, 2) - (256 << 10)))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    STEPS[tid] = ((st.st_size, st.st_mtime), step_of(lines, st.st_mtime))
    return STEPS[tid][1]


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


STATE_WORDS = {"running": "working", "gating": "checking", "needs_input": "needs you"}


def wait_reasons(tasks, s=None):
    """Why each queued task has not started yet, in the scheduler's own order of checks."""
    s = settings_now() if s is None else s
    if STOPPING.is_set():
        return {t["id"]: "waiting for claude-ops to restart" for t in tasks if t["state"] == "queued"}
    if s.get("paused") == "1":
        return {t["id"]: "the queue is paused" for t in tasks if t["state"] == "queued"}
    if lim := limit_info(s):
        return {t["id"]: lim["words"] for t in tasks if t["state"] == "queued"}
    braked = usage_info(s)["braked"]
    by_id = {t["id"]: t for t in tasks}
    find = lambda i: by_id.get(i) or task(i)  # the list holds the newest 500; an older one is read on its own
    free = int(s.get("max_parallel") or 1) - len(THREADS)
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
        elif braked and starts_claude(t):
            why = braked["words"]
        elif dep and dep["state"] != "done":
            why = f"waiting for #{dep['id']} to finish (it is {STATE_WORDS.get(dep['state'], dep['state'])})"
        elif blocker := exclusive_blocker(t):
            why = f"waiting for {blocker['kind']} #{blocker['id']}"
        elif p.max_parallel and mine.get(p.name, 0) >= p.max_parallel:
            why = f"{p.title} already runs {p.max_parallel} at once, its limit"
        elif free <= 0:
            why = "waiting for a free slot (all busy)"
        else:
            why = "starting now"
            free -= 1
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
            stats.append(f"{r.sub}/\n{st}" if r.sub else st)
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


# ---------------------------------------------------------------- http

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        """Refuse DNS rebinding: a web page whose own domain is re-pointed at 127.0.0.1 could otherwise
        drive this API, and so start work on your machine, from your browser."""
        host = (self.headers.get("Host") or "").lower()
        host = host[1:host.find("]")] if host.startswith("[") else host.split(":")[0]
        allowed = {"127.0.0.1", "localhost", "::1", HOST.lower()} | {
            h.strip().lower() for h in os.environ.get("OPS_ALLOWED_HOSTS", "").split(",") if h.strip()}
        return host in allowed or host.endswith(".ts.net")  # Tailscale serve, tailnet-only

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "unknown Host header"})
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, (CODE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state" and not READY.is_set():  # the dashboard shows "starting up" until this goes
            return self.send(200, {"starting": True, "now": time.time(), "data_dir": str(DATA)})
        if path == "/api/state":
            tasks = q("SELECT id,project,title,state,phase,priority,hold,depends_on,parent,created,started,"
                      "finished,cost,rounds,summary,question,error,merged,branch,kind,auto_release,origin,paused_by,start_at,"
                      "model,session_model,effort FROM tasks "
                      "ORDER BY id DESC LIMIT 500")
            # What each task is doing right now, for the status line under it in the list.
            last = {r["task_id"]: r for r in q(
                "SELECT e.task_id, e.text, e.ts FROM events e JOIN (SELECT MAX(id) AS m FROM events "
                "GROUP BY task_id) x ON e.id = x.m")}
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
                t["origin"] = origin_view(t["origin"])
            projects = []
            for p in PROJECTS.values():
                try:
                    projects.append(main_info(p, s))
                except Exception as e:
                    projects.append({"name": p.name, "title": p.title, "error": str(e)})
            return self.send(200, {
                "tasks": tasks, "now": time.time(), "projects": projects, "archived": archived_projects(),
                "settings": {k: s.get(k, "") for k in SETTING_KEYS},
                "limit": limit_info(s), "usage": usage_info(s),
                "active": len(THREADS), "data_dir": str(DATA),
                "design_cost": (q("SELECT SUM(cost) AS c FROM designs", one=True) or {}).get("c") or 0,
                "chats": chat_counts()})
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
            t["chat"] = q("SELECT role,text,ts,delivered FROM task_msgs WHERE task_id=? ORDER BY id", (tid,))
            t["chat_live"] = TASK_CHAT_LIVE.get(tid)
            t["can_chat"] = bool(t["session_id"]) and t["state"] in CHAT_STATES
            t["steer_live"] = tid in STEER_PIPES  # the worker reads a message after its current step
            t["can_steer"] = t["state"] in STEER_STATES and (t["kind"] in MERGES or t["steer_live"])
            if t["kind"] == "deploy":  # its command's output
                t["deploy"] = json.loads(t["deploy"] or "{}")
                t["log"] = log_tail(RUNS / str(tid) / "deploy.log", 8000)
            return self.send(200, t)
        m = re.fullmatch(r"/api/images/([\w-]+(\.\w+))", path)
        if m and m.group(2) in IMAGE_TYPES.values() and (IMAGES / m.group(1)).is_file():
            ctype = next(k for k, v in IMAGE_TYPES.items() if v == m.group(2))
            return self.send(200, (IMAGES / m.group(1)).read_bytes(), ctype)
        if path == "/api/designs":
            chats = q(f"SELECT {CHAT_COLS},origin FROM designs ORDER BY updated DESC")
            for c in chats:
                c["origin"] = origin_view(c["origin"])
            return self.send(200, chats)
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
            return self.send(200, d)
        self.send(404, {"error": "not found"})

    def do_POST(self):
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
        try:
            return self.send(200, self.route(path, body) or {"ok": True})
        except KeyError as e:
            return self.send(400, {"error": f"missing {e}"})
        except Exception as e:
            return self.send(400, {"error": str(e)})

    def route(self, path, b):
        if path == "/api/images":
            return save_image(b.get("data"))
        if path == "/api/tasks":
            if not (b.get("body") or "").strip():
                raise ValueError("describe the task")
            dep = int(str(b["depends_on"]).lstrip("#")) if str(b.get("depends_on") or "").strip() else None
            tid = add_task(b["project"], b["body"], b.get("title", ""), b.get("priority", 0),
                           b.get("hold", 0), dep, auto_release=b.get("release"), model=b.get("model"),
                           effort=b.get("effort"))
            p = PROJECTS.get(b["project"])
            return {"id": tid, **({"warning": w} if p and (w := uncommitted_warning(p)) else {})}
        if path == "/api/openspec/new":  # New change: a design chat that drafts one
            return {"id": spec_new(proj(b["project"]), b.get("sub") or "", b.get("text"))}
        if path in ("/api/openspec/init", "/api/openspec/update"):  # Set up, Refresh commands
            return openspec_run(proj(b["project"]), b.get("sub") or "", path.rsplit("/", 1)[1])
        if path == "/api/send":  # opsd.py send, from a session the queue started
            return send_peer(b.get("from"), b["project"], b.get("kind") or "task", b.get("text"), b.get("title") or "",
                             bool(b.get("hold")), b.get("model"), b.get("effort"))
        if path == "/api/settings":
            if "usage_brake" in b:
                b["usage_brake"] = min(100, max(0, int(float(b["usage_brake"] or 0))))
            if "release_wait" in b:
                b["release_wait"] = max(0, int(float(b["release_wait"] or 0)))
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
        if path == "/api/pause-all":
            return pause_all()
        if path == "/api/resume-all":
            return resume_all()
        if path == "/api/limit/clear":  # Try now: stop waiting out the usage limit
            return limit_clear("you clicked Try now")
        if path == "/api/projects/reload":
            load_projects()
            return {"projects": list(PROJECTS)}
        m = re.fullmatch(r"/api/projects/([^/]+)/(archive|unarchive)", path)
        if m:  # unarchive names a project that is not loaded, so these come before proj()
            return (archive_project if m.group(2) == "archive" else unarchive_project)(unquote(m.group(1)))
        m = re.fullmatch(r"/api/projects/([\w-]+)/(promote|play|pause|resume|release|playcheck|senddev)", path)
        if m:
            p, act = proj(m.group(1)), m.group(2)
            if act == "senddev":  # Send to dev, on the banner about your checkout's uncommitted files
                tid = queue_senddev(p)
                stale_facts(p.name)
                return {"id": tid}
            if act == "release":  # the project's "Release after merge" default, for tasks queued from now on
                set_setting(f"auto_release:{p.name}", "1" if b.get("on") else "0")
            elif act == "playcheck":  # "Play-check releases": off, releases are gate-only
                set_setting(f"release_play:{p.name}", "1" if b.get("on") else "0")
            elif act == "promote":
                return {"id": queue_release(p)}
            elif act == "play":  # Play main, or Play dev with {"where": "dev"}
                play(p, "dev" if b.get("where") == "dev" else "main")
                stale_facts(p.name)
            else:
                set_setting(f"paused:{p.name}", "1" if act == "pause" else "0")
            return
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
            kind = "project" if b.get("kind") == "project" else "design"
            did = q("INSERT INTO designs (project, title, created, updated, kind) VALUES (?,?,?,?,?)",
                    (b["project"], title_of(text)[:80], time.time(), time.time(), kind))
            design_say(did, text)
            return {"id": did}
        m = re.fullmatch(r"/api/designs/(\d+)/(say|stop|rename|queue|seen|end|reopen)", path)
        if m:
            did = int(m.group(1))
            if not design(did):
                raise ValueError("no such chat")
            if m.group(2) == "say":
                design_say(did, b["text"].strip())
            elif m.group(2) == "stop":
                stop(DESIGN_PROCS.get(did))
            elif m.group(2) == "queue":
                return {"ids": spec_queue(did, b.get("plan"), b.get("hold"), b.get("release"))}
            elif m.group(2) == "seen":  # you looked at its latest reply
                q("UPDATE designs SET seen=? WHERE id=?", (time.time(), did))
            elif m.group(2) == "end":  # off the list of chats waiting for you; a reply in progress stops
                q("UPDATE designs SET ended=?, seen=? WHERE id=?", (time.time(), time.time(), did))
                stop(DESIGN_PROCS.get(did))
            elif m.group(2) == "reopen":
                q("UPDATE designs SET ended=NULL WHERE id=?", (did,))
            else:
                q("UPDATE designs SET title=? WHERE id=?", (b["title"][:80], did))
            return
        m = re.fullmatch(r"/api/tasks/(\d+)/(\w+)", path)
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
            tell_sender(tid, "was cancelled by the owner")
            if p and t["kind"] in MERGES and t["auto_release"]:  # its split's other tasks may be waiting for it
                auto_release(p)
        elif action == "release":  # the "Release after merge" toggle
            if t["kind"] not in MERGES or st in ENDED:
                raise ValueError(f"task is {st}" if st in ENDED else "a release releases itself")
            upd(tid, auto_release=int(bool(b.get("on"))))
            ev(tid, "will release after it merges" if b.get("on") else "will not release after it merges")
        elif action == "pause":
            pause_task(tid)
        elif action == "resume":
            resume_task(tid)
        elif action == "retry":
            if st not in ("failed", "cancelled"):
                raise ValueError(f"task is {st}")
            note = b.get("note") or ""
            upd(tid, state="queued", rounds=0, error=None, finished=None,
                next_prompt=(f"Your previous attempt stopped: {t['error'] or st}. {note} "
                             "Check the worktree state and finish the task; write $OPS_RESULT.")
                if t["session_id"] else None)
            ev(tid, "retried" + (f": {note}" if note else "")
               + ("; moving main again (the deploy itself succeeded)" if t["kind"] == "deploy" and
                  t["start_at"] == "promote" else "; the deploy starts over" if t["kind"] == "deploy" else ""))
        elif action == "answer":
            if st != "needs_input":
                raise ValueError(f"task is {st}")
            upd(tid, state="queued", question=None,
                next_prompt=f"{ANSWERED}\n\n{b['answer']}\n\n{ANSWER_TAIL}")
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
            upd(tid, state="queued", rounds=0,
                next_prompt=f"The user tried your change and wants this changed:\n\n{b['feedback']}\n\n"
                            "Update the work, re-run the checks, commit, and write $OPS_RESULT.")
            ev(tid, "changes requested: " + b["feedback"][:200])
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
    for proc in list(PROCS.values()) + list(DESIGN_PROCS.values()) + list(TASK_CHAT_PROCS.values()):
        stop(proc)
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
    except Exception as e:  # serve anyway: a broken project or task shouldn't keep the dashboard down
        print("startup error:", e, flush=True)
    READY.set()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"claude-ops ready  projects: {', '.join(PROJECTS) or 'none'}", flush=True)


def serve():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    print(f"claude-ops starting on http://{HOST}:{PORT}  (pid {os.getpid()})", flush=True)
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), H)
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
    """opsd.py send PROJECT [--design|--chat] [--title T] [--hold] [--model M] [--effort E] [TEXT...]; TEXT from stdin when not
    given."""
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


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "guard":
        guard(args)
    elif cmd == "event" and len(args) == 2 and args[0].isdigit():  # straight into the db: the daemon may be down
        q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (int(args[0]), time.time(), args[1]))
    elif cmd in ("pause-all", "resume-all"):
        post(f"/api/{cmd}", {})
    elif cmd == "add-project" and args:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        out = detect_project(str(Path(args[0]).expanduser().absolute()), args[1] if len(args) > 1 else None)
        print(f"wrote {out}. Review it, then reload projects on the dashboard (or restart the service).")
    elif cmd == "send" and args and not args[0].startswith("-"):
        send_cli(args)
    elif cmd == "add" and len(args) >= 2 and args[0] == "-p":
        words, opts = args[2:], {}
        while words[:1] in (["--model"], ["--effort"]) and len(words) >= 2:
            opts[words[0][2:]], words = words[1], words[2:]
        try:
            out = post("/api/tasks", {"project": args[1], "body": " ".join(words) or sys.stdin.read(), **opts})
        except HTTPError as e:
            sys.exit(json.loads(e.read() or b"{}").get("error") or str(e))
        if w := (json.loads(out or "{}") or {}).get("warning"):
            print(w, file=sys.stderr)
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
