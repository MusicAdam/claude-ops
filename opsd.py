#!/usr/bin/env python3
"""diskgame-ops: a task queue for Disk Game, worked by parallel headless Claude sessions.

  opsd.py serve            run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add "text..."    queue a task through the running daemon

Layout (all outside the repo except git worktrees, which share its .git):
  ~/dev/diskgame              your checkout (main). The queue never edits it, except a
                              fast-forward of main on promote when it is clean.
  ~/dev/diskgame-wt/dev       integration worktree on branch `dev`. Finished tasks merge here.
  ~/dev/diskgame-wt/t<id>     one worktree per running task, branch task/<id>-<slug>.
  ~/dev/diskgame-stg          staging worktree (detached). Its build only changes on Promote.
"""
import json, os, re, shutil, signal, sqlite3, subprocess, sys, threading, time
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HOME = Path.home()
OPS = Path(__file__).resolve().parent
REPO = Path(os.environ.get("OPS_REPO", HOME / "dev/diskgame"))
WT = Path(os.environ.get("OPS_WORKTREES", HOME / "dev/diskgame-wt"))
DEV_WT = WT / "dev"
STG_WT = Path(os.environ.get("OPS_STAGING", HOME / "dev/diskgame-stg"))
RUNS = OPS / "runs"
DB = OPS / "queue.db"
HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8788"))
CLAUDE = os.environ.get("OPS_CLAUDE") or shutil.which("claude") or str(HOME / ".local/bin/claude")
APP_REL = "Builds/macOS/DiskGame.app"
GATE = ["compile", "test-edit", "test-play"]  # scripts/unity.sh steps a task must pass to merge
MAX_FIX_ROUNDS = 3  # times a worker is sent back after a conflict or failed gate

ACTIVE = ("running", "gating", "merging")
DBL = threading.RLock()
MERGE_LOCK = threading.Lock()
STAGE_LOCK = threading.Lock()
PROCS = {}  # task id -> current Popen (claude or unity), for cancel
THREADS = {}  # task id -> pipeline thread
STAGING = {"state": "idle", "msg": ""}


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
    q("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, title TEXT, body TEXT, priority INTEGER DEFAULT 0,
        hold INTEGER DEFAULT 0, depends_on INTEGER, parent INTEGER,
        state TEXT DEFAULT 'queued', phase TEXT DEFAULT '', created REAL, started REAL,
        finished REAL, branch TEXT, worktree TEXT, session_id TEXT, cost REAL DEFAULT 0,
        rounds INTEGER DEFAULT 0, summary TEXT, question TEXT, error TEXT,
        next_prompt TEXT, merged TEXT, start_at TEXT DEFAULT 'claude')""")
    if "pid" not in {r["name"] for r in q("PRAGMA table_info(tasks)")}:
        q("ALTER TABLE tasks ADD COLUMN pid INTEGER")
    q("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, task_id INTEGER, ts REAL, text TEXT)")
    q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in {"max_parallel": "3", "paused": "0", "model": "", "staged": ""}.items():
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


def add_task(body, title="", priority=0, hold=0, depends_on=None, parent=None):
    body = body.strip()
    title = (title or body.splitlines()[0] if body else title or "untitled").strip()[:120]
    tid = q("INSERT INTO tasks (title, body, priority, hold, depends_on, parent, created) VALUES (?,?,?,?,?,?,?)",
            (title, body, int(priority), int(bool(hold)), depends_on or None, parent, time.time()))
    ev(tid, "queued" + (f" after #{depends_on}" if depends_on else ""))
    return tid


def notify(title, msg):
    msg = msg.replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                   capture_output=True)


# ---------------------------------------------------------------- git / shell

def sh(*cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd))}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def git(*a, cwd=REPO, check=True):
    return sh("git", *a, cwd=cwd, check=check)


def rev(ref):
    return git("rev-parse", ref)


def clone_library(dest):
    """Copy a warm Unity Library (APFS clone, near-instant) so a new worktree skips the full import."""
    for src in (STG_WT / "Library", REPO / "Library"):
        if src.is_dir() and not (dest / "Library").exists():
            subprocess.run(["cp", "-cR", str(src), str(dest / "Library")], capture_output=True)
            return


def bootstrap():
    WT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    if git("rev-parse", "--verify", "-q", "refs/heads/dev", check=False) == "":
        git("branch", "dev", "main")
    if not DEV_WT.exists():
        git("worktree", "add", str(DEV_WT), "dev")
    exclude = Path(git("rev-parse", "--git-common-dir")).resolve() / "info" / "exclude"
    if ".ops-result.json" not in (exclude.read_text() if exclude.exists() else ""):
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a") as f:
            f.write("\n.ops-result.json\n")
    if not STG_WT.exists():
        git("worktree", "add", "--detach", str(STG_WT), "main")
        clone_library(STG_WT)


def kill_players(path):
    """Quit any game player a worker left running from its worktree."""
    subprocess.run(["pkill", "-f", f"{path}/{APP_REL}"], capture_output=True)


def running_from(path):
    return subprocess.run(["pgrep", "-f", f"{path}/{APP_REL}"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- claude worker

def worker_prompt(t):
    tpl = (OPS / "worker_prompt.md").read_text()
    return tpl.format(id=t["id"], title=t["title"], body=t["body"], worktree=t["worktree"],
                      branch=t["branch"], repo=REPO, port=port_for(t["id"]), result=result_path(t["id"]))


def port_for(tid):
    return 47900 + tid % 90


def result_path(tid):
    return WT / f"t{tid}" / ".ops-result.json"  # inside the worktree, where auto mode lets workers write


def cancelled(tid):
    return task(tid)["state"] == "cancelled"


def run_claude(tid, prompt):
    """Run one headless Claude turn in the task worktree. Returns the parsed result.json or None."""
    t = task(tid)
    run_dir = RUNS / str(tid)
    run_dir.mkdir(parents=True, exist_ok=True)
    rp = result_path(tid)
    rp.unlink(missing_ok=True)
    cmd = ["caffeinate", "-i", CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--permission-prompts", "none", "--strict-mcp-config",
           "--name", f"diskgame #{tid}"]
    if t["session_id"]:
        cmd += ["--resume", t["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]
    env = dict(os.environ, DISKGAME_PORT=str(port_for(tid)), OPS_RESULT=str(rp), OPS_TASK_ID=str(tid))
    upd(tid, phase="claude working")
    with open(run_dir / "claude.jsonl", "a") as log, open(run_dir / "claude.err", "a") as err:
        log.write(json.dumps({"type": "ops", "prompt": prompt[:2000], "ts": time.time()}) + "\n")
        p = subprocess.Popen(cmd, cwd=t["worktree"], env=env, stdout=subprocess.PIPE, stderr=err,
                             text=True, start_new_session=True)
        PROCS[tid] = p
        upd(tid, pid=p.pid)
        for line in p.stdout:
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
        p.wait()
        PROCS.pop(tid, None)
    kill_players(t["worktree"])
    if rp.exists():
        try:
            return json.loads(rp.read_text())
        except ValueError:
            return {"status": "failed", "summary": "result.json was not valid JSON"}
    return None


# ---------------------------------------------------------------- gate (unity)

def unity(tid, step, cwd):
    """Run scripts/unity.sh <step> in cwd. Returns (ok, detail)."""
    upd(tid, phase=f"gate: {step}") if tid else None
    log = RUNS / str(tid or "staging") / "gate.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"\n==== {time.ctime()} {step} in {cwd}\n")
        f.flush()
        p = subprocess.Popen(["caffeinate", "-i", "scripts/unity.sh", step], cwd=cwd, stdout=f,
                             stderr=subprocess.STDOUT, start_new_session=True)
        if tid:
            PROCS[tid] = p
            upd(tid, pid=p.pid)
        rc = p.wait()
        PROCS.pop(tid, None)
    if rc == 0:
        return True, ""
    return False, failure_detail(Path(cwd), step, rc)


def failure_detail(wt, step, rc):
    name = {"test-edit": "test-editmode", "test-play": "test-playmode"}.get(step, step)
    lines = []
    xml = wt / "Logs" / f"{name}.xml"
    if step.startswith("test") and xml.exists():
        try:
            for case in ET.parse(xml).iter("test-case"):
                if case.get("result") == "Failed":
                    msg = (case.findtext("failure/message") or "").strip()
                    lines.append(f"FAILED {case.get('fullname')}: {msg[:400]}")
        except ET.ParseError:
            pass
    logf = wt / "Logs" / f"{name}.log"
    if logf.exists():
        text = logf.read_text(errors="replace").splitlines()
        lines += [l for l in text if "error CS" in l][:20]
        if not lines:
            lines += text[-40:]
    return f"scripts/unity.sh {step} exited {rc}\n" + "\n".join(lines[:60])


# ---------------------------------------------------------------- task pipeline

def ensure_worktree(tid):
    t = task(tid)
    if t["worktree"] and Path(t["worktree"]).exists():
        return
    slug = re.sub(r"[^a-z0-9]+", "-", t["title"].lower()).strip("-")[:40] or "task"
    branch = t["branch"] or f"task/{tid}-{slug}"
    path = WT / f"t{tid}"
    if git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
        git("worktree", "add", str(path), branch)
    else:
        git("worktree", "add", "-b", branch, str(path), "dev")
    clone_library(path)
    upd(tid, branch=branch, worktree=str(path))
    ev(tid, f"worktree {path} on {branch}")


def ahead_of_dev(branch):
    return int(git("rev-list", "--count", f"dev..{branch}") or 0)


def pipeline(tid):
    try:
        _pipeline(tid)
    except Exception as e:  # keep the daemon alive; surface the error on the task
        if not cancelled(tid):
            upd(tid, state="failed", phase="", error=f"orchestrator error: {e}", finished=time.time())
            ev(tid, f"orchestrator error: {e}")
    finally:
        THREADS.pop(tid, None)


def _pipeline(tid):
    t = task(tid)
    upd(tid, started=t["started"] or time.time(), error=None)
    ensure_worktree(tid)
    t = task(tid)
    wt = t["worktree"]
    prompt = t["next_prompt"] or worker_prompt(t)
    need_claude = t["start_at"] == "claude"
    upd(tid, next_prompt=None, start_at="claude")

    while True:
        if need_claude:
            res = run_claude(tid, prompt)
            if cancelled(tid):
                return
            if res is None:
                err = (RUNS / str(tid) / "claude.err").read_text(errors="replace")[-1500:]
                return fail(tid, "worker stopped without writing a result.\n" + err)
            status = res.get("status")
            upd(tid, summary=res.get("summary") or task(tid)["summary"])
            if status == "split":
                return split(tid, res)
            if status == "needs_input":
                upd(tid, state="needs_input", phase="", question=res.get("question") or res.get("summary"))
                ev(tid, "needs your input")
                notify("Disk Game task needs input", f"#{tid}: {res.get('question', '')}")
                return
            if status != "done":
                return fail(tid, res.get("summary") or f"worker reported {status}")
            if git("status", "--porcelain", cwd=wt):
                git("stash", "push", "-u", "-m", "ops-autostash: uncommitted after worker", cwd=wt)
                ev(tid, "stashed uncommitted leftovers (git stash list in the worktree)")
        need_claude = True

        if ahead_of_dev(t["branch"]) == 0:
            return finish(tid, merged=None, note="no code changes")

        # Rebase onto the latest dev, gate, then merge (or hold for review).
        upd(tid, state="gating", phase="rebasing onto dev")
        base = rev("dev")
        r = subprocess.run(["git", "rebase", "dev"], cwd=wt, capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "rebase", "--abort"], cwd=wt, capture_output=True)
            if not bump_round(tid, "rebase conflict"):
                return
            prompt = (f"The `dev` branch moved and branch `{t['branch']}` no longer rebases cleanly onto it. "
                      "Run `git rebase dev`, resolve the conflicts so both your change and the new dev "
                      "work survive, re-run compile and both test suites, commit, and write $OPS_RESULT "
                      f"again.\n\ngit said:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
            upd(tid, state="running")
            continue
        ok, detail = True, ""
        for step in GATE:
            ok, detail = unity(tid, step, wt)
            if cancelled(tid):
                return
            if not ok:
                break
        if not ok:
            ev(tid, f"gate failed: {detail.splitlines()[0]}")
            if not bump_round(tid, "gate failed:\n" + detail):
                return
            prompt = (f"The queue rebased your branch onto the latest `dev` and ran its merge check. "
                      f"It failed:\n\n{detail}\n\nFix it in this worktree (keep the history rebased on "
                      "dev), re-run the failing step yourself, commit, and write $OPS_RESULT again.")
            upd(tid, state="running")
            continue
        ev(tid, "gate passed: " + ", ".join(GATE))

        if task(tid)["hold"]:
            unity(tid, "build", wt)
            upd(tid, state="review", phase="")
            ev(tid, "waiting for your review")
            notify("Disk Game task ready for review", f"#{tid}: {t['title']}")
            return

        with MERGE_LOCK:
            if rev("dev") != base:
                ev(tid, "dev moved during the gate; re-checking")
                need_claude = False
                continue
            upd(tid, state="merging", phase="merging into dev")
            git("merge", "--no-ff", "-m", f"Merge task #{tid}: {t['title']}", t["branch"], cwd=DEV_WT)
            merged = rev("dev")
        return finish(tid, merged=merged, note=f"merged into dev at {merged[:8]}")


def bump_round(tid, why):
    n = (task(tid)["rounds"] or 0) + 1
    upd(tid, rounds=n)
    if n > MAX_FIX_ROUNDS:
        fail(tid, f"gave up after {MAX_FIX_ROUNDS} fix rounds. Last problem: {why}")
        return False
    ev(tid, f"fix round {n}: {why.splitlines()[0]}")
    return True


def fail(tid, why):
    upd(tid, state="failed", phase="", error=why, finished=time.time())
    ev(tid, "failed: " + why.splitlines()[0][:200])
    notify("Disk Game task failed", f"#{tid}: {why[:120]}")


def finish(tid, merged, note):
    t = task(tid)
    upd(tid, state="done", phase="", merged=merged, finished=time.time())
    ev(tid, note)
    if t["worktree"] and Path(t["worktree"]).exists():
        kill_players(t["worktree"])
        git("worktree", "remove", "--force", t["worktree"], check=False)
    notify("Disk Game task done", f"#{tid}: {t['title']}")


def split(tid, res):
    ids = []
    for sub in res.get("tasks") or []:
        after = sub.get("after")
        dep = ids[after] if isinstance(after, int) and 0 <= after < len(ids) else None
        ids.append(add_task(sub.get("body") or sub.get("title", ""), sub.get("title", ""),
                            priority=task(tid)["priority"], hold=task(tid)["hold"], depends_on=dep, parent=tid))
    finish(tid, merged=None, note="split into " + ", ".join(f"#{i}" for i in ids))


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
    if slots <= 0:
        return
    for t in q("SELECT * FROM tasks WHERE state='queued' ORDER BY priority DESC, id"):
        if slots <= 0:
            break
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


# ---------------------------------------------------------------- staging

def staging_info():
    staged = setting("staged")
    dev = rev("dev")
    ahead = []
    if staged:
        ahead = git("log", "--first-parent", "--format=%h %s", f"{staged}..dev").splitlines()[:30]
    return {**STAGING, "staged": staged[:8], "dev": dev[:8], "ahead": ahead,
            "running": running_from(STG_WT), "built": (STG_WT / APP_REL).exists()}


def promote():
    if not STAGE_LOCK.acquire(blocking=False):
        return
    try:
        if running_from(STG_WT):
            STAGING.update(state="failed", msg="Quit the staging game first, then promote again.")
            return
        commit = rev("dev")
        STAGING.update(state="building", msg=f"building {commit[:8]}")
        git("checkout", "--detach", commit, cwd=STG_WT)
        git("branch", "-f", "stg", commit)
        ok, detail = unity(None, "build", STG_WT)
        if not ok:
            STAGING.update(state="failed", msg=detail[:2000])
            return
        set_setting("staged", commit)
        msg = f"staging is {commit[:8]}"
        cur = git("branch", "--show-current", check=False)
        if cur == "main" and not git("status", "--porcelain", "-uno", check=False):
            r = subprocess.run(["git", "merge", "--ff-only", "dev"], cwd=REPO, capture_output=True, text=True)
            msg += "; main fast-forwarded" if r.returncode == 0 else "; main not fast-forwarded (diverged)"
        else:
            msg += "; main left alone (your checkout is not clean or not on main)"
        STAGING.update(state="idle", msg=msg)
        notify("Disk Game staging updated", msg)
    except Exception as e:
        STAGING.update(state="failed", msg=str(e))
    finally:
        STAGE_LOCK.release()


# ---------------------------------------------------------------- activity feed

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
            return self.send(200, (OPS / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            tasks = q("SELECT id,title,state,phase,priority,hold,depends_on,parent,created,started,finished,"
                      "cost,rounds,summary,question,error,merged,branch FROM tasks ORDER BY id DESC LIMIT 300")
            for t in tasks:
                t["has_build"] = bool(t["state"] == "review" and (WT / f"t{t['id']}" / APP_REL).exists())
            return self.send(200, {
                "tasks": tasks, "now": time.time(), "staging": staging_info(),
                "settings": {k: setting(k) for k in ("max_parallel", "paused", "model")},
                "active": len(THREADS)})
        m = re.fullmatch(r"/api/tasks/(\d+)", path)
        if m:
            tid = int(m.group(1))
            t = task(tid)
            if not t:
                return self.send(404, {"error": "no such task"})
            t["events"] = q("SELECT ts,text FROM events WHERE task_id=? ORDER BY id", (tid,))
            t["activity"] = activity(tid)
            t["diffstat"] = ""
            if t["branch"]:
                base = "dev" if t["state"] != "done" else f"{t['merged']}^1" if t["merged"] else "dev"
                t["diffstat"] = git("diff", "--stat", f"{base}...{t['branch']}", check=False)[-3000:]
            return self.send(200, t)
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
            dep = int(b["depends_on"]) if str(b.get("depends_on") or "").strip() else None
            return {"id": add_task(b["body"], b.get("title", ""), b.get("priority", 0), b.get("hold", 0), dep)}
        if path == "/api/settings":
            for k in ("max_parallel", "paused", "model"):
                if k in b:
                    set_setting(k, b[k])
            return
        if path == "/api/staging/promote":
            threading.Thread(target=promote, daemon=True).start()
            return
        if path == "/api/staging/play":
            if not (STG_WT / APP_REL).exists():
                raise ValueError("no staging build yet; promote first")
            subprocess.Popen(["open", str(STG_WT / APP_REL)])
            return
        m = re.fullmatch(r"/api/tasks/(\d+)/(\w+)", path)
        if not m:
            raise ValueError("unknown endpoint")
        tid, action = int(m.group(1)), m.group(2)
        t = task(tid)
        if not t:
            raise ValueError("no such task")
        st = t["state"]
        if action == "cancel":
            if st in ("done", "cancelled"):
                raise ValueError(f"task is {st}")
            upd(tid, state="cancelled", phase="", finished=time.time())
            p = PROCS.get(tid)
            if p:
                killgroup(p.pid)
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
            kill_players(t["worktree"])
            upd(tid, state="queued", hold=0, start_at="integrate")
            ev(tid, "approved")
        elif action == "reject":
            if st != "review":
                raise ValueError(f"task is {st}")
            kill_players(t["worktree"])
            upd(tid, state="queued", rounds=0,
                next_prompt=f"The user tried your change and wants this changed:\n\n{b['feedback']}\n\n"
                            "Update the work, re-run the checks, commit, and write $OPS_RESULT.")
            ev(tid, "changes requested: " + b["feedback"][:200])
        elif action == "play":
            app = WT / f"t{tid}" / APP_REL
            if not app.exists():
                raise ValueError("this task has no build")
            subprocess.Popen(["open", "-n", str(app)])
        elif action == "edit":
            if st != "queued":
                raise ValueError("only queued tasks can be edited")
            upd(tid, body=b["body"], title=(b.get("title") or b["body"].splitlines()[0])[:120],
                priority=int(b.get("priority", t["priority"])))
        else:
            raise ValueError("unknown action")


def killgroup(pid):
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def on_term(*_):
    for p in list(PROCS.values()):
        killgroup(p.pid)
    os._exit(0)


def serve():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    init_db()
    bootstrap()
    recover()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"diskgame-ops on http://{HOST}:{PORT}  (claude: {CLAUDE})", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


def cli_add(text):
    req = Request(f"http://127.0.0.1:{PORT}/api/tasks", data=json.dumps({"body": text}).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1"})
    print(urlopen(req).read().decode())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "add":
        cli_add(" ".join(sys.argv[2:]) or sys.stdin.read())
    else:
        print(__doc__)
        sys.exit(2)
