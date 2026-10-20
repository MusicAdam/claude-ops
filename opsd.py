#!/usr/bin/env python3
"""claude-ops: a task queue for your git projects, worked by parallel headless Claude sessions.

  opsd.py serve                        run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add-project PATH [NAME]      detect PATH and write ~/.claude-ops/projects/NAME.toml
  opsd.py add -p NAME "text..."        queue a task through the running daemon
  opsd.py pause-all | resume-all       pause every running task (it stops now, resumable) / resume every paused one
  opsd.py guard --daemon PID [--root DIR]   PreToolUse hook: refuse Bash commands that stop processes the session did not start

Per project (paths come from ~/.claude-ops/projects/<name>.toml):
  <repo>                 your checkout of the main branch. The queue only fast-forwards it, when a release passes.
  <worktrees>/dev        integration worktree on the dev branch. Finished tasks merge here.
  <worktrees>/main       build of main, the golden version (detached). Changes only when a release passes.
  <worktrees>/t<id>      one worktree per task, branch task/<id>-<slug> (a release: detached at its dev snapshot).

A workspace (`repos = [...]` in its config) is a folder that holds several git repos, such as a backend and an
app. Each of the paths above is then a folder with a copy of the workspace's own files and one worktree per repo,
and every repo has its own main, dev and task branches.
"""
import base64, json, os, re, secrets, shlex, shutil, signal, sqlite3, subprocess, sys, threading, time, tomllib
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
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
CLAUDE = os.environ.get("OPS_CLAUDE") or shutil.which("claude") or str(HOME / ".local/bin/claude")
MAX_FIX_ROUNDS = 3  # times a worker is sent back after a conflict or failed gate
RESULT_FILE = ".ops-result.json"

ACTIVE = ("running", "gating", "merging")
STOPPED = ("cancelled", "paused")  # set by you; the pipeline stops at its next step and never overrides them
ENDED = ("done", "failed", "cancelled")
RELEASE_LOCK = threading.Lock()  # one release queued at a time, when tasks finish together
DBL = threading.RLock()
PROCS = {}  # task id -> current Popen (claude or a gate step), for cancel
STOPPING = threading.Event()  # this daemon is shutting down: start nothing new. Never stored, so it can't outlive us
THREADS = {}  # task id -> pipeline thread
PHASE_SINCE = {}  # task id -> when its phase last changed, so the dashboard can say how long a step has run


# ---------------------------------------------------------------- projects

HAS = set()  # (repo, sha) of commits known to be in that repo


class Part:
    """One git repo of a project: the project itself, or one folder of a workspace."""

    def __init__(self, p, sub):
        self.sub = sub
        self.src = p.repo / sub if sub else p.repo  # your checkout of it

    def at(self, root):
        """This repo's worktree inside a project worktree."""
        return Path(root) / self.sub if self.sub else Path(root)

    def git(self, *a, cwd=None, check=True):
        return sh("git", *a, cwd=cwd or self.src, check=check)

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
        wk = c.get("worker", {})
        self.env = wk.get("env", {})
        self.port_base = int(wk.get("port_base", 47900))
        self.worker_notes = wk.get("notes", "").strip()
        self.design_notes = c.get("design", {}).get("notes", "").strip()
        self.dev_wt = self.wt / "dev"
        self.main_wt = self.wt / "main"
        self.lock = threading.Lock()  # merges into dev and promotes

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


PROJECTS = {}


def load_projects():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    found = {}
    for f in sorted(PROJECTS_DIR.glob("*.toml")):
        try:
            p = Project(f)
            old = PROJECTS.get(p.name)
            if old:
                p.lock = old.lock
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


def bootstrap(p):
    p.wt.mkdir(parents=True, exist_ok=True)
    copy_workspace_files(p, p.dev_wt)
    for r in p.parts:
        if not r.git("rev-parse", "--verify", "-q", f"refs/heads/{p.dev}", check=False):
            r.git("branch", p.dev, p.main)
        if not r.at(p.dev_wt).exists():
            r.git("worktree", "add", str(r.at(p.dev_wt)), p.dev)
        exclude = Path(r.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
        if RESULT_FILE not in (exclude.read_text() if exclude.exists() else ""):
            exclude.parent.mkdir(exist_ok=True)
            with open(exclude, "a") as f:
                f.write(f"\n{RESULT_FILE}\n")
    if p.build:
        copy_workspace_files(p, p.main_wt)
        if not all(r.at(p.main_wt).exists() for r in p.parts):
            for r in p.parts:
                if not r.at(p.main_wt).exists():
                    r.git("worktree", "add", "--detach", str(r.at(p.main_wt)), p.main)
            prepare_worktree(p, p.main_wt, src_first=p.repo)


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


def prepare_worktree(p, dest, src_first=None):
    """Clone warm caches (APFS clone, near-instant) and run the project's setup commands."""
    for rel in p.clone:
        if (dest / rel).exists():
            continue
        for src in (src_first or p.main_wt, p.repo):
            if (src / rel).exists():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["cp", "-cR", str(src / rel), str(dest / rel)], capture_output=True)
                break
    for cmd in p.setup:
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
                      ("pid_start", "TEXT"), ("paused_by", "TEXT"), ("auto_release", "INTEGER DEFAULT 0")):
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
                      ("seen", "REAL DEFAULT 0"), ("ended", "REAL")):  # and before "ready for you" and End chat
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
    for k, v in {"max_parallel": "3", "paused": "0", "model": ""}.items():
        q("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    q("DELETE FROM settings WHERE key='draining'")  # the old restart's stored hold: a crash could leave it set


def setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return r["value"] if r else ""


def set_setting(k, v):
    q("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, str(v)))


def release_default(p):
    """Whether new tasks of p are marked "Release after merge" unless you say otherwise."""
    return setting(f"auto_release:{p.name}") == "1"


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


def ev(tid, text):
    q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (tid, time.time(), text))
    print(f"[#{tid}] {text}", flush=True)
    stale_facts((task(tid) or {}).get("project"))  # a step that may have moved dev or main, or quit the build


def add_task(project, body, title="", priority=0, hold=0, depends_on=None, parent=None, kind="task", auto_release=None):
    """auto_release None takes the project's default ("Release after merge" on the project)."""
    p = proj(project)
    if auto_release is None:
        auto_release = release_default(p)
    body = body.strip()
    title = (title or (title_of(body) if body else "untitled")).strip()[:120]
    tid = q("INSERT INTO tasks (project, title, body, priority, hold, depends_on, parent, created, kind, auto_release) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project, title, body, int(priority), int(bool(hold)), depends_on or None, parent, time.time(), kind,
             int(bool(auto_release) and kind == "task")))
    ev(tid, "queued" + (f" after #{depends_on}" if depends_on else ""))
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
        raise RuntimeError(f"{' '.join(map(str, cmd))}: {r.stderr.strip() or r.stdout.strip()}")
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


def app_pids(p, path):
    """Copies of the project's app running from this worktree: your processes whose executable lies in
    <path>/<process>. Matched on the executable, literally: `pkill -f` matched a regex against every
    command line, so it also hit a shell, editor or `tail` that merely named the path."""
    if not (p and p.process and path):
        return []
    app = os.path.realpath(Path(path) / p.process)
    if not within(app, path):  # an absolute or ../ process would reach outside the worktree
        return []
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
    found += [(pid, os.path.join(cwds.get(pid) or "/nonexistent", exe)) for pid, exe in relative]
    return [pid for pid, exe in found if within(exe, app)]


def kill_app(p, path):
    """Quit any copy of the project's app running from this worktree."""
    for pid in app_pids(p, path):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def app_running(p, path):
    return bool(app_pids(p, path))


def launch_app(p, path):
    if not p.launch:
        raise ValueError(f"{p.name} has no [app] launch command")
    if p.artifact and not (Path(path) / p.artifact).exists():
        raise ValueError("there is no build there yet")
    subprocess.Popen(p.launch, shell=True, cwd=path, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    run_dir = RUNS / str(tid)
    run_dir.mkdir(parents=True, exist_ok=True)
    rp = result_path(t)
    rp.unlink(missing_ok=True)
    prompt = with_images(prompt)
    acking = False  # a message was just read: its reply is the next thing the worker says
    cmd = ["caffeinate", "-i", CLAUDE, "-p", "--input-format", "stream-json", "--replay-user-messages",
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--permission-prompts", "none", "--strict-mcp-config",
           "--no-chrome", "--name", f"{p.name} #{tid}", "--add-dir", str(IMAGES),
           "--settings", guard_settings(t["worktree"])]
    if t["session_id"]:
        cmd += ["--resume", t["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]
    port = p.port_for(tid)
    env = dict(os.environ, OPS_RESULT=str(rp), OPS_TASK_ID=str(tid), OPS_PORT=str(port),
               **{k: str(v).format(port=port) for k, v in p.env.items()})
    upd(tid, phase="claude working")
    cost0, sid = task(tid)["cost"] or 0, t["session_id"]
    with open(run_dir / "claude.jsonl", "a") as log, open(run_dir / "claude.err", "a") as err:
        log.write(json.dumps({"type": "ops", "prompt": prompt[:2000], "ts": time.time()}) + "\n")
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
            content = e.get("message", {}).get("content")
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
                said = [c["text"].strip() for c in content or [] if c.get("type") == "text" and c.get("text", "").strip()]
                if said:  # its first words after your message are its answer to it
                    q("INSERT INTO task_msgs (task_id, role, text, ts) VALUES (?,?,?,?)",
                      (tid, "assistant", "\n\n".join(said), time.time()))
                    acking = False
            elif e.get("type") == "result":
                # total_cost_usd counts the whole process; a message sent at the very end adds a turn.
                upd(tid, cost=cost0 + (e.get("total_cost_usd") or 0))
                close_steer(tid)  # messages already written still get their turn before claude exits
        close_steer(tid)
        proc.wait()
        untrack(tid)
        with STEER_LOCK:
            INFLIGHT.pop(tid, None)  # written but never read: still pending, so the next turn gets them
    kill_app(p, t["worktree"])
    if rp.exists():
        try:
            return json.loads(rp.read_text())
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

def run_step(key, label, cmd, cwd, log_name):
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
                                stderr=subprocess.STDOUT, start_new_session=True)
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
        (release_pipeline if t["kind"] == "release" else _pipeline)(proj(t["project"]), tid)
    except Exception as e:  # keep the daemon alive; surface the error on the task
        if not stopped(tid):
            upd(tid, state="failed", phase="", error=f"orchestrator error: {e}", finished=time.time())
            ev(tid, f"orchestrator error: {e}")
    finally:
        THREADS.pop(tid, None)


def _pipeline(p, tid):
    t = task(tid)
    upd(tid, started=t["started"] or time.time(), error=None)
    ensure_worktree(p, tid)
    t = task(tid)
    wt = t["worktree"]
    prompt = t["next_prompt"] or worker_prompt(p, t)
    need_claude = t["start_at"] == "claude"
    upd(tid, next_prompt=None, start_at="claude")

    while True:
        if need_claude:
            res = run_claude(p, tid, prompt)
            if stopped(tid):
                return
            if res is None:
                err = (RUNS / str(tid) / "claude.err").read_text(errors="replace")[-1500:]
                return fail(p, tid, "worker stopped without writing a result.\n" + err)
            status = res.get("status")
            upd(tid, summary=res.get("summary") or task(tid)["summary"])
            if status == "split":
                return split(p, tid, res)
            if status == "needs_input":
                upd(tid, state="needs_input", phase="", question=res.get("question") or res.get("summary"))
                ev(tid, "needs your input")
                notify(f"{p.title}: task needs input", f"#{tid}: {res.get('question', '')}")
                return
            if status != "done":
                return fail(p, tid, res.get("summary") or f"worker reported {status}")
            if pending_steers(tid):  # sent as the worker finished
                prompt = steered_prompt("as you finished")
                continue
            for r in p.parts:
                if r.git("status", "--porcelain", cwd=r.at(wt)):
                    r.git("stash", "push", "-u", "-m", "ops-autostash: uncommitted after worker", cwd=r.at(wt))
                    ev(tid, "stashed uncommitted leftovers (git stash list in the worktree"
                            + (f" of {r.sub})" if r.sub else ")"))
        need_claude = True

        if not changed_parts(p, t["branch"]):
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
        where = ", ".join(f"{r.sub} {sha[:8]}" if r.sub else sha[:8] for r, sha in done)
        return finish(p, tid, merged=merged, note=f"merged into {p.dev} at {where}")


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


def finish(p, tid, merged, note):
    t = task(tid)
    upd(tid, state="done", phase="", merged=merged, finished=time.time())
    ev(tid, note)
    if t["worktree"] and Path(t["worktree"]).exists():
        kill_app(p, t["worktree"])
        remove_worktree(p, t["worktree"])
    notify(f"{p.title}: task done", f"#{tid}: {t['title']}")
    if merged:  # a task marked "Release after merge", or a release that ends with more of them in dev
        auto_release(p)


def remove_worktree(p, path):
    for r in p.parts:
        r.git("worktree", "remove", "--force", str(r.at(path)), check=False)
    if p.repos:  # the workspace's own files
        shutil.rmtree(path, ignore_errors=True)
        for r in p.parts:
            r.git("worktree", "prune", check=False)


def add_subtasks(p, tid, items, priority=0, hold=0, auto_release=None):
    """Queue a worker's task list ({title, body, after}) with tid as parent. Returns the new ids."""
    ids = []
    for sub in items or []:
        after = sub.get("after")
        dep = ids[after] if isinstance(after, int) and 0 <= after < len(ids) else None
        ids.append(add_task(p.name, sub.get("body") or sub.get("title", ""), sub.get("title", ""),
                            priority=priority, hold=hold, depends_on=dep, parent=tid, auto_release=auto_release))
    return ids


def split(p, tid, res):
    t = task(tid)
    ids = add_subtasks(p, tid, res.get("tasks"), t["priority"], t["hold"], t["auto_release"])
    finish(p, tid, merged=None, note="split into " + ", ".join(f"#{i}" for i in ids))


# ---------------------------------------------------------------- release (dev -> main)

def queue_release(p):
    """The Promote button: queue a release check of dev, ahead of ordinary tasks. It never refuses: with a release
    already open it returns that one, and whatever is in the way of moving main the release waits for."""
    open_ = q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN ('done','failed','cancelled')",
              (p.name,), one=True)
    if open_:
        return open_["id"]
    body = (f"Check the latest {p.dev} as a whole: the merge check, then a build that a release checker runs "
            f"and plays like a player. If it passes, build it into {p.main} and fast-forward {p.main} to it.")
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
    """Queue a release for tasks marked "Release after merge" once they are in dev and not yet in main. With a
    release already open this waits: the release's own end calls it again, so anything that merged after its
    snapshot gets the next one. The tasks of one split release together, after the last of them merges."""
    with RELEASE_LOCK:
        if q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN (?,?,?)",
             (p.name, *ENDED), one=True):
            return None
        tasks = q("SELECT * FROM tasks WHERE project=? AND kind='task' AND auto_release=1", (p.name,))
        busy = {root_of(t) for t in tasks if t["state"] not in ENDED}
        ready = [t for t in tasks if t["state"] == "done" and t["merged"] and root_of(t) not in busy
                 and not all(any(is_ancestor(r, sha, p.main) for r in p.parts if r.has(sha))
                             for sha in t["merged"].split())]
        if not ready:
            return None
        rid = queue_release(p)
    for t in ready:
        ev(t["id"], f"queued release #{rid}")
    ev(rid, "queued after " + ", ".join(f"#{t['id']}" for t in ready) + " merged (release after merge)")
    return rid


ANSWERED = "The user answered your question:"  # how an answer reaches a task's next turn
WAIT = "waiting on you"  # the phase of a release that needs you because something is in the way of moving main
WAIT_CHANGES = "waiting on your changes"  # ... because your uncommitted changes could not be carried over


class NeedsYou(Exception):
    """Only you can settle this: the release waits for you (needs_input) rather than failing."""


def wait_for_you(p, tid, why, phase):
    """The release needs you. It goes on by itself once the way is clear (see unblock_releases), or when you
    answer."""
    upd(tid, state="needs_input", phase=phase, question=why)
    ev(tid, "needs you: " + why)
    notify(f"{p.title}: release needs you", f"#{tid}: {why}")


def unblock_releases():
    """Requeue each release that needs you once you've cleared its way: quit the build, switched the worktree off
    main, or (for changes that could not be carried over) committed or stashed them."""
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


def release_prompt(p, t):
    log = [(r, l) for r, sha in p.pairs(t["base"])
           for l in r.git("log", "--first-parent", "--format=%H %h %s", f"{p.main}..{sha}").splitlines()]
    changes = "\n".join(f"- {r.sub + ': ' if r.sub else ''}{l.split(' ', 1)[1]}" for r, l in log[:60]) \
        or f"- (nothing: this only rebuilds {p.main})"
    shas = {l.split(" ", 1)[0] for _, l in log}
    done = [d for d in q("SELECT id,title,summary,merged FROM tasks WHERE project=? AND merged IS NOT NULL "
                         "ORDER BY id", (p.name,)) if shas & set(d["merged"].split())] if shas else []
    summaries = "\n\n".join(f"#{d['id']} {d['title']}\n{(d['summary'] or '(no summary)')[:1500]}" for d in done)
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured)"
    notes = workspace_note(p) + (f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else "")
    return (CODE / "release_prompt.md").read_text().format(
        id=t["id"], project=p.title, dev=p.dev, main=p.main, base=p.show(t["base"], 40), base_short=p.show(t["base"]),
        checks=checks, changes=changes, summaries=summaries or "(none recorded)", worktree=t["worktree"],
        repo=p.repo, port=p.port_for(t["id"]), build=f" (`{p.build}`)" if p.build else "",
        notes=notes, result=result_path(t))


def release_pipeline(p, tid):
    """Snapshot dev, gate it, have Claude play it, then promote exactly that commit."""
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

    if start_at != "promote":
        if resume and t["session_id"]:  # an answer, a retry or a restart resumes the checker
            prompt = resume
        else:
            upd(tid, state="gating")
            ok, detail = gate(p, tid, t["worktree"])
            if stopped(tid):
                return
            if not ok:
                return fail(p, tid, "gate failed, so the release stops here:\n" + detail)
            ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))
            prompt = release_prompt(p, t)
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
            ev(tid, "needs your input")
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
    subprocess.Popen([str(CODE / "opsctl"), "restart"], cwd=CODE, env=dict(os.environ, OPS_DATA=str(DATA)),
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(RUNS / str(tid) / "restart.log", "a"), stderr=subprocess.STDOUT)


# ---------------------------------------------------------------- scheduler

def scheduler():
    n = 0
    while True:
        try:
            schedule_once()
            if n % 3 == 0:  # every 6 s: a scan for a running build takes up to 0.4 s
                unblock_releases()
        except Exception as e:
            print("scheduler error:", e, flush=True)
        n += 1
        time.sleep(2)


def schedule_once():
    if setting("paused") == "1" or STOPPING.is_set():
        return
    slots = int(setting("max_parallel") or 1) - len(THREADS)
    for t in q("SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, id"):
        if slots <= 0:
            break
        p = PROJECTS.get(t["project"])
        if not p or setting(f"paused:{p.name}") == "1":
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
        upd(t["id"], state="running", phase="starting", pid=None, pid_start=None)
        if task(t["id"])["state"] != "running":  # paused or cancelled just now
            continue
        ev(t["id"], "started")
        th = threading.Thread(target=pipeline, args=(t["id"],), daemon=True)
        THREADS[t["id"]] = th
        th.start()
        slots -= 1


RESTART_NOTE = ("The task queue restarted while you were working. Any command you were running was stopped. "
                "Check `git status` and `git log` in this worktree, finish the task, and write $OPS_RESULT.")
PAUSE_NOTE = ("The user paused this task while you were working and has now resumed it. Any command you were "
              "running was stopped. Check `git status` and `git log` in this worktree, finish the task, and "
              "write $OPS_RESULT.")


def recover():
    """On start: resume the tasks the last daemon paused as it stopped, and requeue any it left mid-flight
    (it crashed or was killed) so they resume their Claude session. Tasks you paused stay paused."""
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
    by='restart': the daemon is stopping, and the next one resumes the task."""
    t = task(tid)
    if t["state"] == "merging":
        raise ValueError("it is merging; that can't be paused, and it is nearly done")
    if t["state"] not in ("queued", *ACTIVE):
        raise ValueError(f"task is {t['state']}")
    kw = {}
    if t["state"] == "gating" and t["kind"] == "task":
        kw = {"start_at": "integrate"}  # Claude's work is done: rebase and run the gate again
    elif t["state"] == "running" and t["phase"] == "claude working" and t["session_id"]:
        kw = {"start_at": "claude", "next_prompt": RESTART_NOTE if by else PAUSE_NOTE}
    # Otherwise Claude has not had its prompt yet, and run_claude keeps it for the resume.
    was = t["state"]
    upd(tid, state="paused", phase="", paused_by=by, **kw)
    stop(PROCS.get(tid))
    if was != "queued" and t["worktree"]:
        kill_app(PROJECTS.get(t["project"]), t["worktree"])
    ev(tid, ("paused for a restart" if by else "paused")
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
    rel = q("SELECT id,state,phase,error FROM tasks WHERE project=? AND kind='release' ORDER BY id DESC LIMIT 1",
            (p.name,), one=True)
    return {"name": p.name, "title": p.title, "main_branch": p.main, "dev_branch": p.dev,
            "main": p.show(f["main"]), "built": p.show(built), "dev": p.show(f["dev"]), "ahead": f["ahead"],
            "has_app": bool(p.build), "can_launch": bool(p.launch),
            "stale": bool(p.build) and built != f["main"], "running": f["running"],
            "has_build": bool(p.build) and f["has_build"],
            "paused": s.get(f"paused:{p.name}") == "1",
            "auto_release": s.get(f"auto_release:{p.name}") == "1",  # release_default(p), from the snapshot
            "gate": [g["name"] for g in p.gate], "release": rel}


# ---------------------------------------------------------------- project facts for the dashboard
# Git and the process table are slow next to the database: a git call takes 10-20 ms and a scan for a running
# app 0.2-0.4 s, and every open dashboard tab asks for /api/state every few seconds. So /api/state answers from
# FACTS at once and rereads an entry older than FACTS_TTL behind the request: what it shows is at most a poll old.
# Every task event marks its project's entry stale and rereads it, so a merge or promote shows on the next poll.

FACTS = {}  # project name -> {"at": when read, "main", "dev": revs, "ahead": [lines], "running", "has_build"}
FACTS_TTL = 5
FACTS_BUSY = set()  # projects being reread now, so a burst of requests starts one reread, not one each
FACTS_LOCK = threading.Lock()
AHEAD = {}  # (repo, main sha, dev sha) -> dev's first-parent log since main; a pair of shas never changes


def read_facts(p):
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
        f = {"main": " ".join(main), "dev": " ".join(dev), "ahead": ahead[:30],
             "running": app_running(p, p.main_wt),
             "has_build": not p.artifact or (p.main_wt / p.artifact).exists()}
    except Exception as e:
        f = {"error": str(e)}
    f["at"] = time.time()
    with FACTS_LOCK:
        FACTS[p.name] = f
        FACTS_BUSY.discard(p.name)
    return f


def facts(p):
    """What git and the process table say about a project: from FACTS, reread behind the request when stale."""
    with FACTS_LOCK:
        f = FACTS.get(p.name)
        if f and time.time() - f["at"] < FACTS_TTL:
            return f
        if f:
            refresh_facts(p)
            return f
    return read_facts(p)  # the first request after a start or reload waits for it


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
        dirty = set(r.git("diff", "--name-only", "HEAD", check=False).splitlines()
                    + r.git("ls-files", "--others", "--exclude-standard", check=False).splitlines())
        if dirty & set(r.git("diff", "--name-only", p.main, sha).splitlines()):
            out.append((r, sha))
    return out


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


def move_main(p, tid, commit, note=""):
    """Fast-forward main to commit in each repo: in your checkout when it is on main, else the branch alone. Your
    uncommitted changes stay: ones to files the release changes are merged onto it (by a worker, if they conflict)
    and put back. Every repo is checked first, so a workspace never moves only some of them. Returns an error or
    None; raises NeedsYou if your changes can't be carried over."""
    for r, sha in p.pairs(commit):
        if not is_ancestor(r, p.main, sha):
            return f"{p.main}{f' of {r.sub}' if r.sub else ''} could not fast-forward: it has commits {p.dev} lacks."
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
        rc, tail = run_step(tid, f"building {p.main} at {p.show(commit)}", p.build, p.main_wt, "build.log")
        if rc or (art and not art.exists()):
            undo()
            return f"Build failed; {p.main} and its build are unchanged.\n" + tail[-2000:]
    upd(tid, phase=f"fast-forwarding {p.main}")
    try:
        err = move_main(p, tid, commit, note)
    except BaseException:
        undo()
        raise
    if err:
        undo()
        return err
    if prev:
        shutil.rmtree(prev, ignore_errors=True)
    set_setting(f"main_built:{p.name}", commit)
    notify(f"{p.title}: {p.main} updated", f"{p.main} is now {p.show(commit)}")
    return None


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
CHAT_COLS = ("id,project,title,state,updated,cost,kind,spec,seen,ended,(SELECT MAX(ts) FROM design_msgs m "
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
    prev = q("SELECT id FROM design_msgs WHERE design_id=? AND role='user' ORDER BY id DESC LIMIT 1 OFFSET 1",
             (did,), one=True)
    parts += [m["note"] for m in q("SELECT note FROM design_msgs WHERE design_id=? AND role!='user' AND id>? "
                                   "AND note!='' ORDER BY id", (did, prev["id"] if prev else 0))]
    parts += [extra] if extra else []
    return "[ops: " + " | ".join(parts) + "]\n\n"


def design_system_prompt(p, d=None, cwd=None):
    notes = f"## Notes for {p.title}\n\n{p.design_notes}\n" if p.design_notes else ""
    out = (CODE / "design_prompt.md").read_text().format(project=p.title, dev_wt=cwd or p.dev_wt, dev=p.dev,
                                                         notes=notes)
    if d and d["spec"]:
        out += "\n" + (CODE / "spec_prompt.md").read_text().format(change=d["spec"], path=SPEC_DIR / d["spec"],
                                                                   dev=p.dev)
    return out


def stream_claude(cmd, cwd, log_dir, live, procs, key, on_session):
    """Run one chat turn of claude -p, tool lines into live. Returns (reply text, cost)."""
    texts, cost, sid = [], 0.0, None
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "claude.jsonl", "a") as log, open(log_dir / "claude.err", "a+") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True,
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
            if e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        texts.append(c["text"].strip())
                    elif c.get("type") == "tool_use":
                        texts.clear()  # text before a tool call is narration; the reply comes after
                        live.append(tool_line(c.get("name"), c.get("input") or {}))
            elif e.get("type") == "result":
                cost += e.get("total_cost_usd") or 0
        proc.wait()
        err.seek(0)
        err_tail = err.read()[-1200:]
    reply = "\n\n".join(texts)
    if not reply:
        reply = f"(no reply; claude exited {proc.returncode})\n{err_tail.strip()}".strip()
    return reply, cost


def design_turn(did, text):
    d = design(did)
    p = proj(d["project"])
    DESIGN_LIVE[did] = []
    chat = d["kind"] == "project"
    cwd, tools, extra = p.repo if chat else p.dev_wt, DESIGN_TOOLS, ""
    if d["spec"]:  # its own worktree of dev, where Claude may edit openspec/
        try:
            cwd, extra = spec_sync(p, d)
        except Exception as e:
            q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
              (did, "assistant", f"(chat error: could not prepare the change's worktree: {e})", time.time()))
            DESIGN_LIVE.pop(did, None)
            q("UPDATE designs SET state='idle', updated=? WHERE id=?", (time.time(), did))
            return
        tools += f",Edit(/{cwd}/openspec/**),Bash(openspec validate:*),Bash(openspec show:*),Bash(openspec list:*)"
    if chat:  # your own settings and MCP servers apply, like the CLI; auto mode stands in for prompts
        cmd = [CLAUDE, "-p", with_images(text), "--output-format", "stream-json", "--verbose",
               "--permission-mode", "auto", "--permission-prompts", "none", "--no-chrome",
               "--name", f"{p.name} chat #{did}", "--settings", guard_settings(None),
               "--append-system-prompt", PROJECT_CHAT_PROMPT]
    else:
        cmd = [CLAUDE, "-p", ops_note(p, did, extra) + with_images(text), "--output-format", "stream-json",
               "--verbose", "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", tools,
               "--strict-mcp-config", "--no-chrome", "--name", f"{p.name} design #{did}",
               "--append-system-prompt", design_system_prompt(p, d, cwd)]
    cmd += ["--add-dir", str(IMAGES)]
    if d["session_id"]:
        cmd += ["--resume", d["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]

    def on_session(sid):
        if sid != design(did)["session_id"]:
            q("UPDATE designs SET session_id=? WHERE id=?", (sid, did))

    cost = 0.0
    try:
        reply, cost = stream_claude(cmd, cwd, RUNS / f"design-{did}", DESIGN_LIVE[did], DESIGN_PROCS, did,
                                    on_session)
        saved = spec_land(p, d) if d["spec"] else ""  # before any task it queues starts from dev
        reply, ids, note = (reply, [], "") if chat else queue_from_reply(p, reply, did)
        q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
          (did, "assistant", reply, time.time(), json.dumps(ids), note))
        if saved:
            q("INSERT INTO design_msgs (design_id, role, text, ts, note) VALUES (?,?,?,?,?)",
              (did, "ops", saved, time.time(), "your last reply's edits: " + saved))
    except Exception as e:
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
          (did, "assistant", f"(chat error: {e})", time.time()))
    finally:
        DESIGN_PROCS.pop(did, None)
        DESIGN_LIVE.pop(did, None)
        q("UPDATE designs SET state='idle', updated=?, cost=cost+? WHERE id=?", (time.time(), cost, did))


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
            tid = add_task(p.name, it.get("body") or it.get("title", ""), it.get("title", ""),
                           priority=int(it.get("priority") or 0), hold=bool(it.get("hold")), depends_on=dep,
                           auto_release=None if it.get("release") is None else bool(it.get("release")))
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
    spec_commit(r, path, f"OpenSpec {d['spec']}: edits from design chat #{d['id']}")
    r.git("reset", "-q", "--hard", cwd=path)
    r.git("clean", "-fdq", cwd=path)
    return path, spec_rebase(p, r, path, d["id"])


def spec_land(p, d, msg=None):
    """After a turn: commit the chat's edits under openspec/ and fast-forward dev to them.
    Returns a line for the owner, or "" when nothing changed."""
    try:
        r, path = spec_wt(p, d)
        spec_commit(r, path, msg or f"OpenSpec {d['spec']}: edits from design chat #{d['id']}")
        with p.lock:  # merges into dev hold it too
            lost = spec_rebase(p, r, path, d["id"])
            files = r.git("diff", "--name-only", f"{p.dev}..HEAD", cwd=path).splitlines()
            if not files:
                return lost.capitalize() + "." if lost else ""
            res = subprocess.run(["git", "merge", "--ff-only", "-q", r.git("rev-parse", "HEAD", cwd=path)],
                                 cwd=r.at(p.dev_wt), capture_output=True, text=True)
            if res.returncode:
                return f"Couldn't save the edits to {p.dev}: {(res.stderr or res.stdout).strip()[-300:]}"
        pre = f"{SPEC_DIR / d['spec']}/"
        names = [f[len(pre):] if f.startswith(pre) else f for f in files]
        return (f"Saved to {p.dev} at {r.rev(p.dev)[:8]}: {', '.join(names[:6])}{' …' if len(names) > 6 else ''}."
                + (f" {lost.capitalize()}." if lost else ""))
    except Exception as e:
        return f"Couldn't save the edits to {p.dev}: {e}"


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
        or bool(r.git("status", "--porcelain", "--untracked-files=all", "--", str(SPEC_DIR), cwd=path,
                      check=False)))
    folder = (path if own else r.at(p.dev_wt)) / SPEC_DIR / d["spec"]
    if not folder.is_dir():
        return {"name": d["spec"], "sub": r.sub, "missing": True}
    files = [f for f in ("proposal.md", "design.md", "tasks.md") if (folder / f).is_file()]
    files += sorted(str(f.relative_to(folder)) for f in folder.rglob("*.md") if str(f.relative_to(folder)) not in
                    files)
    return dict(spec_info(folder), sub=r.sub, path=str(SPEC_DIR / d["spec"]),
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
            f"{split}\n"
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
                       "continue this session in a terminal with `claude --resume`. " + GUARD_RULE)
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
    if setting("model"):
        cmd += ["--model", setting("model")]

    def on_session(sid):
        if sid != task(tid)["session_id"]:
            upd(tid, session_id=sid)

    try:
        if made:
            cwd.mkdir(parents=True)
        reply, cost = stream_claude(cmd, cwd, RUNS / str(tid) / "chat", TASK_CHAT_LIVE[tid], TASK_CHAT_PROCS,
                                    tid, on_session)
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
        elif dep and dep["state"] != "done":
            why = f"waiting for #{dep['id']} to finish (it is {STATE_WORDS.get(dep['state'], dep['state'])})"
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
        if path == "/api/state":
            tasks = q("SELECT id,project,title,state,phase,priority,hold,depends_on,parent,created,started,"
                      "finished,cost,rounds,summary,question,error,merged,branch,kind,auto_release FROM tasks "
                      "ORDER BY id DESC LIMIT 500")
            # What each task is doing right now, for the status line under it in the list.
            last = {r["task_id"]: r for r in q(
                "SELECT e.task_id, e.text, e.ts FROM events e JOIN (SELECT MAX(id) AS m FROM events "
                "GROUP BY task_id) x ON e.id = x.m")}
            s = settings_now()
            waits = wait_reasons(tasks, s)
            for t in tasks:
                for k in ("summary", "question", "error"):  # the list shows a first line; the task page has the rest
                    t[k] = clip(t[k], 400) or None
                ev_ = last.get(t["id"])
                t["last_event"] = {"text": ev_["text"], "ts": ev_["ts"]} if ev_ else None
                t["phase_since"] = PHASE_SINCE.get(t["id"]) if t["state"] in ACTIVE else None
                t["step"] = last_step(t["id"]) if t["state"] == "running" and t["phase"] == "claude working" else None
                t["wait"] = waits.get(t["id"])
            projects = []
            for p in PROJECTS.values():
                try:
                    projects.append(main_info(p, s))
                except Exception as e:
                    projects.append({"name": p.name, "title": p.title, "error": str(e)})
            return self.send(200, {
                "tasks": tasks, "now": time.time(), "projects": projects,
                "settings": {k: s.get(k, "") for k in ("max_parallel", "paused", "model")},
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
            t["can_steer"] = t["state"] in STEER_STATES and (t["kind"] != "release" or t["steer_live"])
            return self.send(200, t)
        m = re.fullmatch(r"/api/images/([\w-]+(\.\w+))", path)
        if m and m.group(2) in IMAGE_TYPES.values() and (IMAGES / m.group(1)).is_file():
            ctype = next(k for k, v in IMAGE_TYPES.items() if v == m.group(2))
            return self.send(200, (IMAGES / m.group(1)).read_bytes(), ctype)
        if path == "/api/designs":
            return self.send(200, q(f"SELECT {CHAT_COLS} FROM designs ORDER BY updated DESC"))
        if path == "/api/openspec":
            out = []
            for p in PROJECTS.values():
                try:
                    out += spec_changes(p)
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
            if p and d["spec"]:
                d["cwd"] = str(spec_part(p, d["spec_sub"]).at(p.wt / f"spec-{did}"))
                d["spec_view"] = spec_view(p, d)
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
            return {"id": add_task(b["project"], b["body"], b.get("title", ""), b.get("priority", 0),
                                   b.get("hold", 0), dep, auto_release=b.get("release"))}
        if path == "/api/settings":
            for k in ("max_parallel", "paused", "model"):
                if k in b:
                    set_setting(k, b[k])
            return
        if path == "/api/projects":
            if b.get("check"):  # what the path resolves to, and whether it can be created
                repo = resolve_repo(b["path"])
                return {"repo": str(repo), "exists": repo.exists(), "git": (repo / ".git").exists()}
            out = detect_project(b["path"], b.get("name") or None, create=bool(b.get("create")))
            load_projects()
            return {"name": out.stem, "config": str(out)}
        if path == "/api/pause-all":
            return pause_all()
        if path == "/api/resume-all":
            return resume_all()
        if path == "/api/projects/reload":
            load_projects()
            return {"projects": list(PROJECTS)}
        m = re.fullmatch(r"/api/projects/([\w-]+)/(promote|play|pause|resume|release)", path)
        if m:
            p, act = proj(m.group(1)), m.group(2)
            if act == "release":  # the project's "Release after merge" default, for tasks queued from now on
                set_setting(f"auto_release:{p.name}", "1" if b.get("on") else "0")
            elif act == "promote":
                return {"id": queue_release(p)}
            elif act == "play":
                launch_app(p, p.main_wt)
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
            if p and t["kind"] == "task" and t["auto_release"]:  # its split's other tasks may be waiting for it
                auto_release(p)
        elif action == "release":  # the "Release after merge" toggle
            if t["kind"] != "task" or st in ENDED:
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
            ev(tid, "retried" + (f": {note}" if note else ""))
        elif action == "answer":
            if st != "needs_input":
                raise ValueError(f"task is {st}")
            upd(tid, state="queued", question=None,
                next_prompt=f"{ANSWERED}\n\n{b['answer']}\n\nContinue the task and write $OPS_RESULT.")
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


def serve():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    init_db()
    load_projects()
    recover()
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    PIDFILE.write_text(f"{os.getpid()}\n")  # after the bind, so a second daemon that fails can't overwrite it
    print(f"claude-ops on http://{HOST}:{PORT}  projects: {', '.join(PROJECTS) or 'none'}  (claude: {CLAUDE})",
          flush=True)
    srv.serve_forever()


def post(path, body):
    req = Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    print(urlopen(req).read().decode())


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "guard":
        guard(args)
    elif cmd in ("pause-all", "resume-all"):
        post(f"/api/{cmd}", {})
    elif cmd == "add-project" and args:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        out = detect_project(str(Path(args[0]).expanduser().absolute()), args[1] if len(args) > 1 else None)
        print(f"wrote {out}. Review it, then reload projects on the dashboard (or restart the service).")
    elif cmd == "add" and len(args) >= 2 and args[0] == "-p":
        post("/api/tasks", {"project": args[1], "body": " ".join(args[2:]) or sys.stdin.read()})
    else:
        print(__doc__)
        sys.exit(2)
