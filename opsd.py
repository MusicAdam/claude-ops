#!/usr/bin/env python3
"""claude-ops: a task queue for your git projects, worked by parallel headless Claude sessions.

  opsd.py serve                        run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add-project PATH [NAME]      detect PATH and write ~/.claude-ops/projects/NAME.toml
  opsd.py add -p NAME "text..."        queue a task through the running daemon

Per project (paths come from ~/.claude-ops/projects/<name>.toml):
  <repo>                 your checkout of the main branch. The queue only fast-forwards it, when a release passes.
  <worktrees>/dev        integration worktree on the dev branch. Finished tasks merge here.
  <worktrees>/main       build of main, the golden version (detached). Changes only when a release passes.
  <worktrees>/t<id>      one worktree per task, branch task/<id>-<slug> (a release: detached at its dev snapshot).
"""
import json, os, re, shutil, signal, sqlite3, subprocess, sys, threading, time, tomllib
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
DB = DATA / "ops.db"
HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8788"))
CLAUDE = os.environ.get("OPS_CLAUDE") or shutil.which("claude") or str(HOME / ".local/bin/claude")
MAX_FIX_ROUNDS = 3  # times a worker is sent back after a conflict or failed gate
RESULT_FILE = ".ops-result.json"

ACTIVE = ("running", "gating", "merging")
DBL = threading.RLock()
PROCS = {}  # task id -> current Popen (claude or a gate step), for cancel
THREADS = {}  # task id -> pipeline thread


# ---------------------------------------------------------------- projects

class Project:
    def __init__(self, path):
        c = tomllib.loads(path.read_text())
        self.name = path.stem
        self.title = c.get("title", self.name)
        self.repo = Path(c["repo"]).expanduser()
        self.main = c.get("main_branch", "main")
        self.dev = c.get("dev_branch", "dev")
        self.wt = Path(c.get("worktrees", f"{self.repo}-wt")).expanduser()
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

    def git(self, *a, cwd=None, check=True):
        return sh("git", *a, cwd=cwd or self.repo, check=check)

    def rev(self, ref):
        return self.git("rev-parse", ref)

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
    for p in PROJECTS.values():
        try:
            bootstrap(p)
        except Exception as e:
            print(f"project {p.name} bootstrap: {e}", flush=True)


def proj(name):
    if name not in PROJECTS:
        raise ValueError(f"unknown project {name!r}")
    return PROJECTS[name]


def bootstrap(p):
    p.wt.mkdir(parents=True, exist_ok=True)
    if not p.git("rev-parse", "--verify", "-q", f"refs/heads/{p.dev}", check=False):
        p.git("branch", p.dev, p.main)
    if not p.dev_wt.exists():
        p.git("worktree", "add", str(p.dev_wt), p.dev)
    exclude = Path(p.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    if RESULT_FILE not in (exclude.read_text() if exclude.exists() else ""):
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a") as f:
            f.write(f"\n{RESULT_FILE}\n")
    if p.build and not p.main_wt.exists():
        p.git("worktree", "add", "--detach", str(p.main_wt), p.main)
        prepare_worktree(p, p.main_wt, src_first=p.repo)


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


def detect_project(path, name=None):
    """Write a starter config for the repo at path. Returns the config path."""
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"{repo} is not a git repository")
    name = re.sub(r"[^a-z0-9-]+", "-", (name or repo.name).lower()).strip("-")
    out = PROJECTS_DIR / f"{name}.toml"
    if out.exists():
        raise ValueError(f"project {name} already exists ({out})")
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
    q("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, project TEXT, title TEXT, body TEXT, priority INTEGER DEFAULT 0,
        hold INTEGER DEFAULT 0, depends_on INTEGER, parent INTEGER,
        state TEXT DEFAULT 'queued', phase TEXT DEFAULT '', created REAL, started REAL,
        finished REAL, branch TEXT, worktree TEXT, session_id TEXT, cost REAL DEFAULT 0,
        rounds INTEGER DEFAULT 0, summary TEXT, question TEXT, error TEXT,
        next_prompt TEXT, merged TEXT, start_at TEXT DEFAULT 'claude', pid INTEGER,
        kind TEXT DEFAULT 'task', base TEXT)""")
    cols = {r["name"] for r in q("PRAGMA table_info(tasks)")}
    for col, decl in (("kind", "TEXT DEFAULT 'task'"), ("base", "TEXT")):  # databases from before releases
        if col not in cols:
            q(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    q("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, task_id INTEGER, ts REAL, text TEXT)")
    q("""CREATE TABLE IF NOT EXISTS designs (id INTEGER PRIMARY KEY, project TEXT, title TEXT, session_id TEXT,
        state TEXT DEFAULT 'idle', created REAL, updated REAL, cost REAL DEFAULT 0)""")
    q("""CREATE TABLE IF NOT EXISTS design_msgs (id INTEGER PRIMARY KEY, design_id INTEGER, role TEXT,
        text TEXT, ts REAL, task_ids TEXT, note TEXT)""")
    q("UPDATE designs SET state='idle' WHERE state='thinking'")  # a reply cut off by a restart
    q("CREATE TABLE IF NOT EXISTS task_msgs (id INTEGER PRIMARY KEY, task_id INTEGER, role TEXT, text TEXT, ts REAL)")
    q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in {"max_parallel": "3", "paused": "0", "model": ""}.items():
        q("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))


def setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return r["value"] if r else ""


def set_setting(k, v):
    q("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, str(v)))


def task(tid):
    return q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)


def upd(tid, **kw):
    q(f"UPDATE tasks SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), tid))


def ev(tid, text):
    q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (tid, time.time(), text))
    print(f"[#{tid}] {text}", flush=True)


def add_task(project, body, title="", priority=0, hold=0, depends_on=None, parent=None, kind="task"):
    proj(project)
    body = body.strip()
    title = (title or body.splitlines()[0] if body else title or "untitled").strip()[:120]
    tid = q("INSERT INTO tasks (project, title, body, priority, hold, depends_on, parent, created, kind) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (project, title, body, int(priority), int(bool(hold)), depends_on or None, parent, time.time(), kind))
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


def kill_app(p, path):
    """Quit any copy of the project's app running from this worktree."""
    if p and p.process and path:
        subprocess.run(["pkill", "-f", f"{path}/{p.process}"], capture_output=True)


def app_running(p, path):
    return bool(p.process) and subprocess.run(["pgrep", "-f", f"{path}/{p.process}"],
                                              capture_output=True).returncode == 0


def launch_app(p, path):
    if not p.launch:
        raise ValueError(f"{p.name} has no [app] launch command")
    if p.artifact and not (Path(path) / p.artifact).exists():
        raise ValueError("there is no build there yet")
    subprocess.Popen(p.launch, shell=True, cwd=path, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def killgroup(pid):
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------- claude worker

def worker_prompt(p, t):
    tpl = (CODE / "worker_prompt.md").read_text()
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured: run whatever tests the repo has)"
    notes = f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else ""
    return tpl.format(id=t["id"], title=t["title"], body=t["body"], project=p.title, worktree=t["worktree"],
                      branch=t["branch"], repo=p.repo, dev=p.dev, main=p.main, port=p.port_for(t["id"]),
                      result=result_path(t), checks=checks, notes=notes)


def result_path(t):
    return Path(t["worktree"]) / RESULT_FILE  # inside the worktree, where auto mode lets workers write


def cancelled(tid):
    return task(tid)["state"] == "cancelled"


def run_claude(p, tid, prompt):
    """Run one headless Claude turn in the task worktree. Returns the parsed result file or None."""
    t = task(tid)
    run_dir = RUNS / str(tid)
    run_dir.mkdir(parents=True, exist_ok=True)
    rp = result_path(t)
    rp.unlink(missing_ok=True)
    cmd = ["caffeinate", "-i", CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--permission-prompts", "none", "--strict-mcp-config",
           "--name", f"{p.name} #{tid}"]
    if t["session_id"]:
        cmd += ["--resume", t["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]
    port = p.port_for(tid)
    env = dict(os.environ, OPS_RESULT=str(rp), OPS_TASK_ID=str(tid), OPS_PORT=str(port),
               **{k: str(v).format(port=port) for k, v in p.env.items()})
    upd(tid, phase="claude working")
    with open(run_dir / "claude.jsonl", "a") as log, open(run_dir / "claude.err", "a") as err:
        log.write(json.dumps({"type": "ops", "prompt": prompt[:2000], "ts": time.time()}) + "\n")
        proc = subprocess.Popen(cmd, cwd=t["worktree"], env=env, stdout=subprocess.PIPE, stderr=err,
                                text=True, start_new_session=True)
        PROCS[tid] = proc
        upd(tid, pid=proc.pid)
        for line in proc.stdout:
            log.write(line)
            log.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("session_id") and e.get("session_id") != task(tid)["session_id"]:
                upd(tid, session_id=e["session_id"])
            if e.get("type") == "result":
                upd(tid, cost=(task(tid)["cost"] or 0) + (e.get("total_cost_usd") or 0))
        proc.wait()
        PROCS.pop(tid, None)
    kill_app(p, t["worktree"])
    if rp.exists():
        try:
            return json.loads(rp.read_text())
        except ValueError:
            return {"status": "failed", "summary": f"{RESULT_FILE} was not valid JSON"}
    return None


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
            PROCS[key] = proc
            upd(key, pid=proc.pid)
        rc = proc.wait()
        if is_task:
            PROCS.pop(key, None)
    with open(log, errors="replace") as f:
        f.seek(start)
        tail = f.read()[-3000:]
    return rc, tail


def gate(p, tid, wt):
    """Run every gate step. Returns (ok, detail of the first failure)."""
    for g in p.gate:
        rc, tail = run_step(tid, f"gate: {g['name']}", g["run"], wt, "gate.log")
        if cancelled(tid):
            return False, "cancelled"
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
    if t["worktree"] and Path(t["worktree"]).exists():
        return
    slug = re.sub(r"[^a-z0-9]+", "-", t["title"].lower()).strip("-")[:40] or "task"
    branch = t["branch"] or f"task/{tid}-{slug}"
    path = p.wt / f"t{tid}"
    if p.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
        p.git("worktree", "add", str(path), branch)
    else:
        p.git("worktree", "add", "-b", branch, str(path), p.dev)
    prepare_worktree(p, path)
    upd(tid, branch=branch, worktree=str(path))
    ev(tid, f"worktree {path} on {branch}")


def pipeline(tid):
    try:
        t = task(tid)
        (release_pipeline if t["kind"] == "release" else _pipeline)(proj(t["project"]), tid)
    except Exception as e:  # keep the daemon alive; surface the error on the task
        if not cancelled(tid):
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
            if cancelled(tid):
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
            if p.git("status", "--porcelain", cwd=wt):
                p.git("stash", "push", "-u", "-m", "ops-autostash: uncommitted after worker", cwd=wt)
                ev(tid, "stashed uncommitted leftovers (git stash list in the worktree)")
        need_claude = True

        if int(p.git("rev-list", "--count", f"{p.dev}..{t['branch']}") or 0) == 0:
            return finish(p, tid, merged=None, note="no code changes")

        # Rebase onto the latest dev, gate, then merge (or hold for review).
        upd(tid, state="gating", phase=f"rebasing onto {p.dev}")
        base = p.rev(p.dev)
        r = subprocess.run(["git", "rebase", p.dev], cwd=wt, capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "rebase", "--abort"], cwd=wt, capture_output=True)
            if not bump_round(p, tid, "rebase conflict"):
                return
            prompt = (f"The `{p.dev}` branch moved and branch `{t['branch']}` no longer rebases cleanly onto "
                      f"it. Run `git rebase {p.dev}`, resolve the conflicts so both your change and the new "
                      f"{p.dev} work survive, re-run the checks, commit, and write $OPS_RESULT again."
                      f"\n\ngit said:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
            upd(tid, state="running")
            continue
        ok, detail = gate(p, tid, wt)
        if cancelled(tid):
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
            upd(tid, state="review", phase="")
            ev(tid, "waiting for your review")
            notify(f"{p.title}: task ready for review", f"#{tid}: {t['title']}")
            return

        with p.lock:
            if p.rev(p.dev) != base:
                ev(tid, f"{p.dev} moved during the gate; re-checking")
                need_claude = False
                continue
            upd(tid, state="merging", phase=f"merging into {p.dev}")
            p.git("merge", "--no-ff", "-m", f"Merge task #{tid}: {t['title']}", t["branch"], cwd=p.dev_wt)
            merged = p.rev(p.dev)
        return finish(p, tid, merged=merged, note=f"merged into {p.dev} at {merged[:8]}")


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
        p.git("worktree", "remove", "--force", t["worktree"], check=False)
    notify(f"{p.title}: task done", f"#{tid}: {t['title']}")


def add_subtasks(p, tid, items, priority=0, hold=0):
    """Queue a worker's task list ({title, body, after}) with tid as parent. Returns the new ids."""
    ids = []
    for sub in items or []:
        after = sub.get("after")
        dep = ids[after] if isinstance(after, int) and 0 <= after < len(ids) else None
        ids.append(add_task(p.name, sub.get("body") or sub.get("title", ""), sub.get("title", ""),
                            priority=priority, hold=hold, depends_on=dep, parent=tid))
    return ids


def split(p, tid, res):
    t = task(tid)
    ids = add_subtasks(p, tid, res.get("tasks"), t["priority"], t["hold"])
    finish(p, tid, merged=None, note="split into " + ", ".join(f"#{i}" for i in ids))


# ---------------------------------------------------------------- release (dev -> main)

def queue_release(p):
    """The Promote button: queue a release check of dev, ahead of ordinary tasks."""
    open_ = q("SELECT id FROM tasks WHERE project=? AND kind='release' AND state NOT IN ('done','failed','cancelled')",
              (p.name,), one=True)
    if open_:
        raise ValueError(f"Release #{open_['id']} is already in the queue.")
    why = promote_blocker(p)
    if why:
        raise ValueError(why)
    body = (f"Check the latest {p.dev} as a whole: the merge check, then a build that a release checker runs "
            f"and plays like a player. If it passes, build it into {p.main} and fast-forward {p.main} to it.")
    return add_task(p.name, body, f"Release {p.dev} → {p.main}", priority=100, kind="release")


def release_prompt(p, t):
    log = p.git("log", "--first-parent", "--format=%H %h %s", f"{p.main}..{t['base']}").splitlines()
    changes = "\n".join(f"- {l.split(' ', 1)[1]}" for l in log[:60]) or f"- (nothing: this only rebuilds {p.main})"
    shas = [l.split(" ", 1)[0] for l in log]
    done = q(f"SELECT id,title,summary FROM tasks WHERE project=? AND merged IN ({','.join('?' * len(shas))})",
             (p.name, *shas)) if shas else []
    summaries = "\n\n".join(f"#{d['id']} {d['title']}\n{(d['summary'] or '(no summary)')[:1500]}" for d in done)
    checks = ", ".join(f"`{g['run']}`" for g in p.gate) or "(none configured)"
    notes = f"## Notes for {p.title}\n\n{p.worker_notes}\n" if p.worker_notes else ""
    return (CODE / "release_prompt.md").read_text().format(
        id=t["id"], project=p.title, dev=p.dev, main=p.main, base=t["base"], base_short=t["base"][:8],
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
            if err:
                return fail(p, tid, err)
            upd(tid, base=p.rev(p.dev))
        ev(tid, f"checking {p.dev} at {task(tid)['base'][:8]}")
    t = task(tid)
    if t["base"] == p.rev(p.main):
        start_at = "promote"  # nothing new to check: this only rebuilds main
        ev(tid, f"nothing new on {p.dev}; rebuilding {p.main}")
    if not (t["worktree"] and Path(t["worktree"]).exists()):
        path = p.wt / f"t{tid}"
        p.git("worktree", "add", "--detach", str(path), t["base"])
        prepare_worktree(p, path)
        upd(tid, worktree=str(path))
        ev(tid, f"worktree {path} at {t['base'][:8]}")
        t = task(tid)

    if start_at != "promote":
        if resume and t["session_id"]:  # an answer, a retry or a restart resumes the checker
            prompt = resume
        else:
            upd(tid, state="gating")
            ok, detail = gate(p, tid, t["worktree"])
            if cancelled(tid):
                return
            if not ok:
                return fail(p, tid, "gate failed, so the release stops here:\n" + detail)
            ev(tid, "gate passed" + (": " + ", ".join(g["name"] for g in p.gate) if p.gate else " (no gate steps)"))
            prompt = release_prompt(p, t)
        upd(tid, state="running")
        res = run_claude(p, tid, prompt)
        if cancelled(tid):
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

    upd(tid, start_at="promote", state="merging")  # a retry or a restart resumes here
    why = promote_blocker(p)
    if why:
        return fail(p, tid, why + " Then click Retry: it promotes without checking again.")
    err = promote(p, tid, task(tid)["base"])
    if cancelled(tid):
        return
    if err:
        return fail(p, tid, err)
    upd(tid, start_at="claude")
    finish(p, tid, merged=t["base"], note=f"{p.main} is now {t['base'][:8]}")


# ---------------------------------------------------------------- scheduler

def scheduler():
    while True:
        try:
            schedule_once()
        except Exception as e:
            print("scheduler error:", e, flush=True)
        time.sleep(2)


def schedule_once():
    if setting("paused") == "1":
        return
    slots = int(setting("max_parallel") or 1) - len(THREADS)
    for t in q("SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, id"):
        if slots <= 0:
            break
        p = PROJECTS.get(t["project"])
        if not p or setting(f"paused:{p.name}") == "1":
            continue
        if p.max_parallel:
            mine = sum(1 for i in THREADS if (task(i) or {}).get("project") == p.name)
            if mine >= p.max_parallel:
                continue
        if t["depends_on"]:
            dep = task(t["depends_on"])
            if dep and dep["state"] != "done":
                continue
        upd(t["id"], state="running", phase="starting")
        ev(t["id"], "started")
        th = threading.Thread(target=pipeline, args=(t["id"],), daemon=True)
        THREADS[t["id"]] = th
        th.start()
        slots -= 1


def recover():
    """Tasks that were mid-flight when the daemon stopped resume their Claude session."""
    for t in q(f"SELECT * FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
        killgroup(t["pid"])  # a worker from the old daemon would otherwise run alongside its resume
        upd(t["id"], state="queued", phase="", pid=None,
            next_prompt="The task queue restarted while you were working. Check `git status` and "
                        "`git log` in this worktree, finish the task, and write $OPS_RESULT.")
        ev(t["id"], "requeued after daemon restart")


# ---------------------------------------------------------------- main (the golden version)

def main_info(p):
    built = setting(f"main_built:{p.name}")
    main, dev = p.rev(p.main), p.rev(p.dev)
    ahead = p.git("log", "--first-parent", "--format=%h %s", f"{p.main}..{p.dev}").splitlines()[:30]
    rel = q("SELECT id,state,phase,error FROM tasks WHERE project=? AND kind='release' ORDER BY id DESC LIMIT 1",
            (p.name,), one=True)
    return {"name": p.name, "title": p.title, "main_branch": p.main, "dev_branch": p.dev,
            "main": main[:8], "built": built[:8], "dev": dev[:8], "ahead": ahead,
            "has_app": bool(p.build), "can_launch": bool(p.launch),
            "stale": bool(p.build) and built != main, "running": app_running(p, p.main_wt),
            "has_build": bool(p.build) and (not p.artifact or (p.main_wt / p.artifact).exists()),
            "paused": setting(f"paused:{p.name}") == "1", "gate": [g["name"] for g in p.gate], "release": rel}


def promote_blocker(p):
    """Why main cannot move right now, or None. Checked when Promote is clicked and again before promoting."""
    if app_running(p, p.main_wt):
        return f"Quit the {p.main} build first."
    if p.git("branch", "--show-current", check=False) != p.main:
        return f"Your checkout {p.repo} is not on {p.main}. Switch it back."
    if p.git("status", "--porcelain", "-uno", check=False):
        return f"Your checkout {p.repo} has uncommitted changes. Commit or stash them."
    return None


def merge_main_into_dev(p):
    """Commits made straight to main (outside the queue) come along into dev. Returns an error or None.
    Call with p.lock held."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", p.main, p.dev], cwd=p.repo).returncode == 0:
        return None
    r = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {p.main} into {p.dev}", p.main],
                       cwd=p.dev_wt, capture_output=True, text=True)
    if r.returncode:
        subprocess.run(["git", "merge", "--abort"], cwd=p.dev_wt, capture_output=True)
        return f"{p.main} has commits that conflict with {p.dev}:\n" + r.stdout[-1500:] + r.stderr[-1500:]
    return None


def promote(p, tid, commit):
    """Build commit in <worktrees>/main, then fast-forward main to it. Returns an error or None;
    on an error main and its build are unchanged."""
    if p.build:
        art = p.main_wt / p.artifact if p.artifact else None
        prev = art.with_name(art.name + ".previous") if art else None
        if prev:
            shutil.rmtree(prev, ignore_errors=True)
            if art.exists():
                subprocess.run(["cp", "-cR", str(art), str(prev)], capture_output=True)
        old = p.git("rev-parse", "HEAD", cwd=p.main_wt)
        p.git("checkout", "--detach", commit, cwd=p.main_wt)
        rc, tail = run_step(tid, f"building {p.main} at {commit[:8]}", p.build, p.main_wt, "build.log")
        if rc or (art and not art.exists()):
            p.git("checkout", "--detach", old, cwd=p.main_wt, check=False)
            if prev and prev.exists():
                shutil.rmtree(art, ignore_errors=True)
                prev.rename(art)
            return f"Build failed; {p.main} and its build are unchanged.\n" + tail[-2000:]
        if prev:
            shutil.rmtree(prev, ignore_errors=True)
    upd(tid, phase=f"fast-forwarding {p.main}")
    r = subprocess.run(["git", "merge", "--ff-only", commit], cwd=p.repo, capture_output=True, text=True)
    if r.returncode:
        return f"{p.main} could not fast-forward:\n" + r.stderr[-1000:]
    set_setting(f"main_built:{p.name}", commit)
    notify(f"{p.title}: {p.main} updated", f"{p.main} is now {commit[:8]}")
    return None


# ---------------------------------------------------------------- design chats

DESIGN_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(ls:*)"
DESIGN_PROCS = {}  # design id -> Popen
DESIGN_LIVE = {}  # design id -> tool lines of the turn in progress
QUEUE_BLOCK = re.compile(r"```queue[ \t]*\n(.*?)```", re.S)


def design(did):
    return q("SELECT * FROM designs WHERE id=?", (did,), one=True)


def design_say(did, text):
    """Start one design-chat turn in the background."""
    d = design(did)
    if d["state"] == "thinking":
        raise ValueError("Claude is still replying")
    q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "user", text, time.time()))
    q("UPDATE designs SET state='thinking', updated=? WHERE id=?", (time.time(), did))
    threading.Thread(target=design_turn, args=(did, text), daemon=True).start()


def ops_note(p, did):
    """Context the dashboard adds to each user message: the queue, and what the last queue block did."""
    live = q("SELECT id,state,title FROM tasks WHERE project=? AND state NOT IN ('done','cancelled') ORDER BY id",
             (p.name,))
    parts = ["queue: " + ("; ".join(f"#{t['id']} {t['state']} {t['title']}" for t in live[:25]) or "empty")]
    last = q("SELECT note FROM design_msgs WHERE design_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
             (did,), one=True)
    if last and last["note"]:
        parts.append(last["note"])
    return "[ops: " + " | ".join(parts) + "]\n\n"


def design_system_prompt(p):
    notes = f"## Notes for {p.title}\n\n{p.design_notes}\n" if p.design_notes else ""
    return (CODE / "design_prompt.md").read_text().format(project=p.title, dev_wt=p.dev_wt, dev=p.dev, notes=notes)


def stream_claude(cmd, cwd, log_dir, live, procs, key, on_session):
    """Run one chat turn of claude -p, tool lines into live. Returns (reply text, cost)."""
    texts, cost = [], 0.0
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
            if e.get("session_id"):
                on_session(e["session_id"])
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
    cmd = [CLAUDE, "-p", ops_note(p, did) + text, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", DESIGN_TOOLS,
           "--strict-mcp-config", "--name", f"{p.name} design #{did}",
           "--append-system-prompt", design_system_prompt(p)]
    if d["session_id"]:
        cmd += ["--resume", d["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]

    def on_session(sid):
        if sid != design(did)["session_id"]:
            q("UPDATE designs SET session_id=? WHERE id=?", (sid, did))

    cost = 0.0
    try:
        reply, cost = stream_claude(cmd, p.dev_wt, RUNS / f"design-{did}", DESIGN_LIVE[did], DESIGN_PROCS, did,
                                    on_session)
        reply, ids, note = queue_from_reply(p, reply, did)
        q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
          (did, "assistant", reply, time.time(), json.dumps(ids), note))
    except Exception as e:
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
          (did, "assistant", f"(design chat error: {e})", time.time()))
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
                           priority=int(it.get("priority") or 0), hold=bool(it.get("hold")), depends_on=dep)
            ev(tid, f"queued from design chat #{did}")
            local.append(tid)
        ids += local
    if ids:
        notes.append("your last reply queued " + ", ".join(f"#{i}" for i in ids))
    return QUEUE_BLOCK.sub("", reply).strip(), ids, "; ".join(notes)


# ---------------------------------------------------------------- activity feed

# ---------------------------------------------------------------- task chat (questions to a worker)

TASK_CHAT_PROCS = {}  # task id -> Popen
TASK_CHAT_LIVE = {}  # task id -> tool lines of the reply in progress; a key here means a reply is running
CHAT_STATES = ("review", "needs_input", "failed", "done", "cancelled")  # the worker is not running


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
            "tell them which button does it (Send back with feedback, Send answer, Retry, or a new task); "
            "you will carry this conversation into that work."
            + (f" Your worktree was removed after the task finished; the merged code is in {p.dev_wt}."
               if made else "") + "]\n\n")
    cmd = [CLAUDE, "-p", note + text, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", DESIGN_TOOLS,
           "--strict-mcp-config", "--resume", t["session_id"]]
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


def activity(tid, limit=120):
    f = RUNS / str(tid) / "claude.jsonl"
    out = []
    if f.exists():
        for line in f.read_text(errors="replace").splitlines()[-1500:]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ty = e.get("type")
            if ty == "ops":
                out.append({"k": "ops", "t": "▶ new turn: " + e.get("prompt", "")[:240]})
            elif ty == "assistant" and not e.get("parent_tool_use_id"):
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        out.append({"k": "say", "t": c["text"].strip()[:1200]})
                    elif c.get("type") == "tool_use":
                        out.append({"k": "tool", "t": tool_line(c.get("name"), c.get("input") or {})})
            elif ty == "result":
                out.append({"k": "ops", "t": f"■ turn ended ({e.get('num_turns', '?')} steps, "
                                             f"${e.get('total_cost_usd') or 0:.2f})"})
    return out[-limit:]


def tool_line(name, i):
    arg = (i.get("command") or i.get("file_path") or i.get("description") or i.get("pattern")
           or i.get("prompt") or "")
    return f"{name}: {str(arg).splitlines()[0][:200] if arg else ''}"


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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, (CODE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            tasks = q("SELECT id,project,title,state,phase,priority,hold,depends_on,parent,created,started,"
                      "finished,cost,rounds,summary,question,error,merged,branch,kind FROM tasks "
                      "ORDER BY id DESC LIMIT 500")
            projects = []
            for p in PROJECTS.values():
                try:
                    projects.append(main_info(p))
                except Exception as e:
                    projects.append({"name": p.name, "title": p.title, "error": str(e)})
            return self.send(200, {
                "tasks": tasks, "now": time.time(), "projects": projects,
                "settings": {k: setting(k) for k in ("max_parallel", "paused", "model")},
                "active": len(THREADS), "data_dir": str(DATA),
                "design_cost": (q("SELECT SUM(cost) AS c FROM designs", one=True) or {}).get("c") or 0})
        m = re.fullmatch(r"/api/tasks/(\d+)", path)
        if m:
            tid = int(m.group(1))
            t = task(tid)
            if not t:
                return self.send(404, {"error": "no such task"})
            t["events"] = q("SELECT ts,text FROM events WHERE task_id=? ORDER BY id", (tid,))
            t["activity"] = activity(tid)
            t["diffstat"] = ""
            p = PROJECTS.get(t["project"])
            if p and t["branch"]:
                base = f"{t['merged']}^1" if t["merged"] else p.dev
                t["diffstat"] = p.git("diff", "--stat", f"{base}...{t['branch']}", check=False)[-3000:]
            t["can_play"] = bool(p and p.launch and t["state"] == "review")
            t["chat"] = q("SELECT role,text,ts FROM task_msgs WHERE task_id=? ORDER BY id", (tid,))
            t["chat_live"] = TASK_CHAT_LIVE.get(tid)
            t["can_chat"] = bool(t["session_id"]) and t["state"] in CHAT_STATES
            return self.send(200, t)
        if path == "/api/designs":
            return self.send(200, q("SELECT id,project,title,state,updated,cost FROM designs ORDER BY updated DESC"))
        m = re.fullmatch(r"/api/designs/(\d+)", path)
        if m:
            did = int(m.group(1))
            d = design(did)
            if not d:
                return self.send(404, {"error": "no such design chat"})
            d["msgs"] = q("SELECT role,text,ts,task_ids FROM design_msgs WHERE design_id=? ORDER BY id", (did,))
            for msg in d["msgs"]:
                ids = json.loads(msg["task_ids"] or "[]")
                msg["tasks"] = [{"id": i, "title": (task(i) or {}).get("title"), "state": (task(i) or {}).get("state")}
                                for i in ids]
            d["live"] = DESIGN_LIVE.get(did, [])[-12:]
            return self.send(200, d)
        self.send(404, {"error": "not found"})

    def do_POST(self):
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
        if path == "/api/tasks":
            if not (b.get("body") or "").strip():
                raise ValueError("describe the task")
            dep = int(str(b["depends_on"]).lstrip("#")) if str(b.get("depends_on") or "").strip() else None
            return {"id": add_task(b["project"], b["body"], b.get("title", ""), b.get("priority", 0),
                                   b.get("hold", 0), dep)}
        if path == "/api/settings":
            for k in ("max_parallel", "paused", "model"):
                if k in b:
                    set_setting(k, b[k])
            return
        if path == "/api/projects":
            out = detect_project(b["path"], b.get("name") or None)
            load_projects()
            return {"name": out.stem, "config": str(out)}
        if path == "/api/projects/reload":
            load_projects()
            return {"projects": list(PROJECTS)}
        m = re.fullmatch(r"/api/projects/([\w-]+)/(promote|play|pause|resume)", path)
        if m:
            p, act = proj(m.group(1)), m.group(2)
            if act == "promote":
                return {"id": queue_release(p)}
            elif act == "play":
                launch_app(p, p.main_wt)
            else:
                set_setting(f"paused:{p.name}", "1" if act == "pause" else "0")
            return
        if path == "/api/designs":
            text = (b.get("text") or "").strip()
            if not text:
                raise ValueError("say something to start the chat")
            proj(b["project"])
            did = q("INSERT INTO designs (project, title, created, updated) VALUES (?,?,?,?)",
                    (b["project"], text.splitlines()[0][:80], time.time(), time.time()))
            design_say(did, text)
            return {"id": did}
        m = re.fullmatch(r"/api/designs/(\d+)/(say|stop|rename)", path)
        if m:
            did = int(m.group(1))
            if not design(did):
                raise ValueError("no such design chat")
            if m.group(2) == "say":
                design_say(did, b["text"].strip())
            elif m.group(2) == "stop":
                proc = DESIGN_PROCS.get(did)
                if proc:
                    killgroup(proc.pid)
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
        if action in ("retry", "answer", "approve", "reject") and tid in TASK_CHAT_LIVE:
            raise ValueError("wait for the worker's reply to your question first")
        if action == "ask":
            return task_ask(tid, (b.get("text") or "").strip())
        if action == "stopask":
            proc = TASK_CHAT_PROCS.get(tid)
            if proc:
                killgroup(proc.pid)
            return
        if action == "cancel":
            if st in ("done", "cancelled"):
                raise ValueError(f"task is {st}")
            upd(tid, state="cancelled", phase="", finished=time.time())
            proc = PROCS.get(tid)
            if proc:
                killgroup(proc.pid)
            ev(tid, "cancelled")
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
                next_prompt=f"The user answered your question:\n\n{b['answer']}\n\nContinue the task and write $OPS_RESULT.")
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
            upd(tid, body=b["body"], title=(b.get("title") or b["body"].splitlines()[0])[:120],
                priority=int(b.get("priority", t["priority"])))
        else:
            raise ValueError("unknown action")


def on_term(*_):
    for proc in list(PROCS.values()) + list(DESIGN_PROCS.values()) + list(TASK_CHAT_PROCS.values()):
        killgroup(proc.pid)
    os._exit(0)


def serve():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    init_db()
    load_projects()
    recover()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"claude-ops on http://{HOST}:{PORT}  projects: {', '.join(PROJECTS) or 'none'}  (claude: {CLAUDE})",
          flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


def post(path, body):
    req = Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    print(urlopen(req).read().decode())


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "add-project" and args:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        out = detect_project(args[0], args[1] if len(args) > 1 else None)
        print(f"wrote {out}. Review it, then reload projects on the dashboard (or restart the service).")
    elif cmd == "add" and len(args) >= 2 and args[0] == "-p":
        post("/api/tasks", {"project": args[1], "body": " ".join(args[2:]) or sys.stdin.read()})
    else:
        print(__doc__)
        sys.exit(2)
