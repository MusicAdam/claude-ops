#!/usr/bin/env python3
"""diskgame-ops: a task queue for Disk Game, worked by parallel headless Claude sessions.

  opsd.py serve            run the dashboard + scheduler (http://127.0.0.1:8788)
  opsd.py add "text..."    queue a task through the running daemon

Layout (all outside the repo except git worktrees, which share its .git):
  ~/dev/diskgame              your checkout of main. The queue only fast-forwards it, on Promote.
  ~/dev/diskgame-wt/dev       integration worktree on branch `dev`. Finished tasks merge here.
  ~/dev/diskgame-wt/t<id>     one worktree per running task, branch task/<id>-<slug>.
  ~/dev/diskgame-wt/main      build of main, the golden version (detached). Changes only on Promote.
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
MAIN_WT = WT / "main"
OLD_STG_WT = HOME / "dev/diskgame-stg"  # where the main build lived when it was called staging
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
MAIN_LOCK = threading.Lock()
PROCS = {}  # task id -> current Popen (claude or unity), for cancel
THREADS = {}  # task id -> pipeline thread
MAIN = {"state": "idle", "msg": ""}  # promote progress


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
    q("""CREATE TABLE IF NOT EXISTS designs (id INTEGER PRIMARY KEY, title TEXT, session_id TEXT,
        state TEXT DEFAULT 'idle', created REAL, updated REAL, cost REAL DEFAULT 0)""")
    q("""CREATE TABLE IF NOT EXISTS design_msgs (id INTEGER PRIMARY KEY, design_id INTEGER, role TEXT,
        text TEXT, ts REAL, task_ids TEXT, note TEXT)""")
    q("UPDATE designs SET state='idle' WHERE state='thinking'")  # a reply cut off by a restart
    q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in {"max_parallel": "3", "paused": "0", "model": "", "main_built": ""}.items():
        q("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    if setting("staged"):  # renamed from staging
        set_setting("main_built", setting("staged"))
        q("DELETE FROM settings WHERE key='staged'")


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
    for src in (MAIN_WT / "Library", REPO / "Library"):
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
    if OLD_STG_WT.exists() and not MAIN_WT.exists():
        git("worktree", "move", str(OLD_STG_WT), str(MAIN_WT))
    if git("rev-parse", "--verify", "-q", "refs/heads/stg", check=False):
        git("branch", "-D", "stg")
    if not MAIN_WT.exists():
        git("worktree", "add", "--detach", str(MAIN_WT), "main")
        clone_library(MAIN_WT)


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
    log = RUNS / str(tid or "main") / "gate.log"
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


# ---------------------------------------------------------------- main (the golden build)

def main_info():
    built = setting("main_built")
    main, dev = rev("main"), rev("dev")
    ahead = git("log", "--first-parent", "--format=%h %s", "main..dev").splitlines()[:30]
    return {**MAIN, "main": main[:8], "built": built[:8], "dev": dev[:8], "ahead": ahead,
            "stale": bool(built) and built != main, "running": running_from(MAIN_WT),
            "has_build": (MAIN_WT / APP_REL).exists()}


def promote_blocker():
    """Why main cannot move to dev right now, or None. Checked before anything changes."""
    if running_from(MAIN_WT):
        return "Quit the main build first, then promote again."
    if git("branch", "--show-current", check=False) != "main":
        return f"Your checkout {REPO} is not on main. Switch it back to main, then promote."
    if git("status", "--porcelain", "-uno", check=False):
        return f"Your checkout {REPO} has uncommitted changes. Commit or stash them, then promote."
    return None


def promote():
    if not MAIN_LOCK.acquire(blocking=False):
        return
    try:
        why = promote_blocker()
        if why:
            return MAIN.update(state="failed", msg=why)
        with MERGE_LOCK:
            # Commits made straight to main (outside the queue) come along into dev first.
            if subprocess.run(["git", "merge-base", "--is-ancestor", "main", "dev"], cwd=REPO).returncode:
                r = subprocess.run(["git", "merge", "--no-ff", "-m", "Merge main into dev", "main"],
                                   cwd=DEV_WT, capture_output=True, text=True)
                if r.returncode:
                    subprocess.run(["git", "merge", "--abort"], cwd=DEV_WT, capture_output=True)
                    return MAIN.update(state="failed", msg="main has commits that conflict with dev:\n"
                                       + r.stdout[-1500:] + r.stderr[-1500:])
            commit = rev("dev")
        MAIN.update(state="building", msg=f"building {commit[:8]}")
        app, prev = MAIN_WT / APP_REL, MAIN_WT / "Builds" / "previous.app"
        shutil.rmtree(prev, ignore_errors=True)
        if app.exists():
            subprocess.run(["cp", "-cR", str(app), str(prev)], capture_output=True)
        old = git("rev-parse", "HEAD", cwd=MAIN_WT)
        git("checkout", "--detach", commit, cwd=MAIN_WT)
        ok, detail = unity(None, "build", MAIN_WT)
        if not ok:
            if old:
                git("checkout", "--detach", old, cwd=MAIN_WT, check=False)
            if prev.exists():
                shutil.rmtree(app, ignore_errors=True)
                prev.rename(app)
            return MAIN.update(state="failed", msg="Build failed; main and its build are unchanged.\n" + detail[:2000])
        r = subprocess.run(["git", "merge", "--ff-only", commit], cwd=REPO, capture_output=True, text=True)
        if r.returncode:
            return MAIN.update(state="failed", msg="Built, but main could not fast-forward:\n" + r.stderr[-1000:])
        set_setting("main_built", commit)
        shutil.rmtree(prev, ignore_errors=True)
        MAIN.update(state="idle", msg=f"main is now {commit[:8]}")
        notify("Disk Game main updated", f"main is now {commit[:8]}")
    except Exception as e:
        MAIN.update(state="failed", msg=str(e))
    finally:
        MAIN_LOCK.release()


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


def ops_note(did):
    """Context the dashboard adds to each user message: the queue, and what the last queue block did."""
    live = q("SELECT id,state,title FROM tasks WHERE state NOT IN ('done','cancelled') ORDER BY id")
    parts = ["queue: " + ("; ".join(f"#{t['id']} {t['state']} {t['title']}" for t in live[:25]) or "empty")]
    last = q("SELECT note FROM design_msgs WHERE design_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
             (did,), one=True)
    if last and last["note"]:
        parts.append(last["note"])
    return "[ops: " + " | ".join(parts) + "]\n\n"


def design_turn(did, text):
    d = design(did)
    DESIGN_LIVE[did] = []
    cmd = [CLAUDE, "-p", ops_note(did) + text, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--allowedTools", DESIGN_TOOLS,
           "--strict-mcp-config", "--name", f"diskgame design #{did}",
           "--append-system-prompt", (OPS / "design_prompt.md").read_text().format(dev_wt=DEV_WT)]
    if d["session_id"]:
        cmd += ["--resume", d["session_id"]]
    if setting("model"):
        cmd += ["--model", setting("model")]
    log_dir = RUNS / f"design-{did}"
    log_dir.mkdir(parents=True, exist_ok=True)
    texts, cost, err_tail = [], 0.0, ""
    try:
        with open(log_dir / "claude.jsonl", "a") as log, open(log_dir / "claude.err", "a+") as err:
            p = subprocess.Popen(cmd, cwd=DEV_WT, stdout=subprocess.PIPE, stderr=err, text=True,
                                 start_new_session=True)
            DESIGN_PROCS[did] = p
            for line in p.stdout:
                log.write(line)
                log.flush()
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("session_id") and e["session_id"] != design(did)["session_id"]:
                    q("UPDATE designs SET session_id=? WHERE id=?", (e["session_id"], did))
                if e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
                    for c in e.get("message", {}).get("content", []):
                        if c.get("type") == "text" and c.get("text", "").strip():
                            texts.append(c["text"].strip())
                        elif c.get("type") == "tool_use":
                            texts.clear()  # text before a tool call is narration; the reply comes after
                            DESIGN_LIVE[did].append(tool_line(c.get("name"), c.get("input") or {}))
                elif e.get("type") == "result":
                    cost += e.get("total_cost_usd") or 0
            p.wait()
            err.seek(0)
            err_tail = err.read()[-1200:]
        reply = "\n\n".join(texts)
        if not reply:
            reply = f"(no reply; claude exited {p.returncode})\n{err_tail.strip()}".strip()
        reply, ids, note = queue_from_reply(reply, did)
        q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids, note) VALUES (?,?,?,?,?,?)",
          (did, "assistant", reply, time.time(), json.dumps(ids), note))
    except Exception as e:
        q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
          (did, "assistant", f"(design chat error: {e})", time.time()))
    finally:
        DESIGN_PROCS.pop(did, None)
        DESIGN_LIVE.pop(did, None)
        q("UPDATE designs SET state='idle', updated=?, cost=cost+? WHERE id=?", (time.time(), cost, did))


def queue_from_reply(reply, did):
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
            tid = add_task(it.get("body") or it.get("title", ""), it.get("title", ""),
                           priority=int(it.get("priority") or 0), hold=bool(it.get("hold")), depends_on=dep)
            ev(tid, f"queued from design chat #{did}")
            local.append(tid)
        ids += local
    if ids:
        notes.append("your last reply queued " + ", ".join(f"#{i}" for i in ids))
    return QUEUE_BLOCK.sub("", reply).strip(), ids, "; ".join(notes)


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
                "tasks": tasks, "now": time.time(), "main": main_info(),
                "settings": {k: setting(k) for k in ("max_parallel", "paused", "model")},
                "active": len(THREADS),
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
            if t["branch"]:
                base = "dev" if t["state"] != "done" else f"{t['merged']}^1" if t["merged"] else "dev"
                t["diffstat"] = git("diff", "--stat", f"{base}...{t['branch']}", check=False)[-3000:]
            return self.send(200, t)
        if path == "/api/designs":
            return self.send(200, q("SELECT id,title,state,updated,cost FROM designs ORDER BY updated DESC"))
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
            dep = int(b["depends_on"]) if str(b.get("depends_on") or "").strip() else None
            return {"id": add_task(b["body"], b.get("title", ""), b.get("priority", 0), b.get("hold", 0), dep)}
        if path == "/api/settings":
            for k in ("max_parallel", "paused", "model"):
                if k in b:
                    set_setting(k, b[k])
            return
        if path == "/api/main/promote":
            why = promote_blocker()
            if why:
                raise ValueError(why)
            threading.Thread(target=promote, daemon=True).start()
            return
        if path == "/api/main/play":
            if not (MAIN_WT / APP_REL).exists():
                raise ValueError("no build of main yet; promote first")
            subprocess.Popen(["open", str(MAIN_WT / APP_REL)])
            return
        if path == "/api/designs":
            text = (b.get("text") or "").strip()
            if not text:
                raise ValueError("say something to start the chat")
            did = q("INSERT INTO designs (title, created, updated) VALUES (?,?,?)",
                    (text.splitlines()[0][:80], time.time(), time.time()))
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
                p = DESIGN_PROCS.get(did)
                if p:
                    killgroup(p.pid)
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
    for p in list(PROCS.values()) + list(DESIGN_PROCS.values()):
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
