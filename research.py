"""The research dashboard's backend, served by claude-ops (see README.md, ## Research).

A port of ~/dev/research/dashboard/server.py: the same files at the same paths in the research repo (the setting
research_repo), the same runs and chats, the same response shapes, under /api/research/... What it adds:
- one writer: while anything answers on the old server's port, every write is refused and the watchdog rests;
- a backup of the state files before the first write after each daemon start;
- atomic writes (temp file, fsync, os.replace);
- adopting runs the old server started, by pid and process start time, so a reused pid is never signalled;
- the usage meter, notifications and the usage brake.
Research runs live outside the tasks table and PROCS/STEPS, so a daemon restart or Promote never stops one.
opsd binds itself as `ops` (bind) before any of this runs."""
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ops = None  # the opsd module: setting, notify, usage_seen, usage_braked, DATA


def bind(module):
    global ops
    ops = module


OFF = {"error": "research is off"}
READONLY = "The old research dashboard is running. Stop it to make changes here."
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Bump when the chat context changes, so resumed conversations get told once.
CONTEXT_VERSION = 3
# Read-only Claude in Chrome tools (runs pass --chrome). Unattended runs drive Adam's
# signed-in browser, so no click/type (computer, form_input), no javascript_tool and
# no uploads: a hostile page can't steer the run into acting in his accounts.
CHROME_READ_TOOLS = [
    f"mcp__claude-in-chrome__{t}" for t in
    ("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "get_page_text",
     "read_page", "find")
]
# On a Claude subscription nothing is billed per run. `--max-budget-usd` still stops
# a run once its token use, priced at API rates, passes the cap. So these are run
# sizes in API-equivalent dollars: they guard the plan's 5-hour and weekly usage
# allowance, not money.
RUN_SIZES = {"small": 1, "normal": 3, "large": 10}
TIER_SIZE = {"T0": "small", "T1": "normal", "T2": "large"}
CHAT_SIZE = "normal"
# Wall-clock limit in minutes. Catches stuck runs the size cap misses.
TIME_LIMIT_MIN = {"T0": 5, "T1": 15, "T2": 40, "chat": 15}
WATCHDOG_SECONDS = 30
START_SLACK = 5  # seconds a run's process start may differ from its job's 'started', for the pid to count as its own
BACKUPS_KEPT = 10
NOTIFY_WITHIN = 3600  # seconds: a job that ended longer ago never notifies, so turning research on replays nothing

FIELD_RE = re.compile(r"^- \*\*(.+?):\*\*\s*(.*)$")
TIER_RE = re.compile(r"\bT[0-3]\b")
URL_RE = re.compile(r"https?://[^\s)|>\]`\"'<]+")
BRIEF_REF_RE = re.compile(r"briefs/([\w.-]+)\.md")
SLUG_RE = re.compile(r"[\w.-]+")

lock = threading.RLock()  # guards every JSON state file
procs: dict[str, subprocess.Popen] = {}  # job id -> the claude this daemon started; never in opsd's PROCS or STEPS
BACKED_UP = set()  # research repos backed up since this daemon started
OFFSETS = {}  # job id -> bytes of its .jsonl the usage meter has read
EVENTS = {}  # .jsonl path -> ((size, mtime), read_events result)
LSTART = {}  # pid -> (when read, ps lstart)

# The research repo's paths, set by current() from the setting research_repo.
REPO = HERE = JOBS_DIR = JOBS_FILE = THREADS_FILE = ARCHIVE_FILE = PROJECTS_FILE = NOTES_DIR = None
todos = None  # the repo's own dashboard/todos.py, so its .todos.lock is shared with bin/todo and the skill


class Refused(Exception):
    """A request this backend turns down with its own status: (code, body)."""

    def __init__(self, code, body):
        super().__init__(body.get("error"))
        self.code, self.body = code, body


def current():
    """The research repo from the setting research_repo, or None while research is off. Points the paths at it."""
    global REPO, HERE, JOBS_DIR, JOBS_FILE, THREADS_FILE, ARCHIVE_FILE, PROJECTS_FILE, NOTES_DIR, todos
    raw = (ops.setting("research_repo") or "").strip()
    if not raw:
        return None
    repo = Path(raw).expanduser().resolve()
    if repo != REPO:
        with lock:
            HERE = repo / "dashboard"
            JOBS_DIR = repo / "logs" / "dashboard"
            JOBS_FILE, THREADS_FILE = JOBS_DIR / "jobs.json", JOBS_DIR / "threads.json"
            ARCHIVE_FILE, PROJECTS_FILE = HERE / "archived.json", HERE / "projects.json"
            NOTES_DIR = repo / "projects"
            todos = load_todos(HERE / "todos.py")
            REPO = repo
    return REPO


def load_todos(path):
    """Load the repo's todos.py by path. No bytecode is written: nothing in the research repo changes."""
    spec = importlib.util.spec_from_file_location("research_todos", path)
    mod = importlib.util.module_from_spec(spec)
    was, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = was
    return mod


def check_repo(raw):
    """The setting's value for a path you entered: "" turns research off."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    repo = Path(raw).expanduser().resolve()
    if not (repo / "dashboard" / "todos.py").is_file():
        raise ValueError(f"{repo} isn't the research repo: it has no dashboard/todos.py")
    return str(repo)


# ---------- one writer, backup, atomic writes ----------

def old_port():
    return int(os.environ.get("OPS_RESEARCH_OLD_PORT", "8787"))


def old_running():
    """Whether anything answers on the old server's port. Its files have an in-process lock only, so while it runs
    it must be the only writer."""
    try:
        with socket.create_connection(("127.0.0.1", old_port()), timeout=0.3):
            return True
    except OSError:
        return False


def writable():
    """Raise Refused unless this daemon may write the research files now. The first write after each daemon start
    backs them up first."""
    if old_running():
        raise Refused(409, {"error": READONLY, "readonly": True})
    if REPO in BACKED_UP:
        return
    with lock:
        if REPO in BACKED_UP:
            return
        try:
            backup()
        except Exception as e:
            raise Refused(500, {"error": f"Couldn't back up the research files, so nothing was changed: {e}"})
        BACKED_UP.add(REPO)


def backup_key(d):
    stamp, _, n = d.name.partition("~")
    return stamp, int(n or 0)


def backup():
    """Copy the state files to DATA/research-backup/<YYYYmmdd-HHMMSS>/ and keep the newest BACKUPS_KEPT. Not the
    .jsonl transcripts: only their claude process writes one, and only by appending."""
    root = ops.DATA / "research-backup"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest, n = root / stamp, 2
    while dest.exists():
        dest, n = root / f"{stamp}~{n}", n + 1
    part = root / f".{dest.name}.partial"  # a backup cut short is never counted as one
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir()
    for f in (PROJECTS_FILE, ARCHIVE_FILE, HERE / "todos.json", JOBS_FILE, THREADS_FILE):
        if f.is_file():
            shutil.copy2(f, part / f.name)
    notes = sorted(NOTES_DIR.glob("*.md")) if NOTES_DIR.is_dir() else []
    if notes:
        (part / "notes").mkdir()
        for f in notes:
            shutil.copy2(f, part / "notes" / f.name)
    os.rename(part, dest)
    kept = sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")), key=backup_key)
    for old in kept[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def write_atomic(path: Path, text: str) -> None:
    """Write a temp file in the same folder, fsync it, then os.replace: a reader sees the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            os.chmod(tmp, 0o644)  # mkstemp makes it 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, indent=2) + "\n")


# ---------- markdown files ----------

def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def section(text: str, heading: str) -> str:
    """First paragraph under '## <heading>'."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == f"## {heading}".lower():
            para = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("#"):
                    break
                if not nxt.strip():
                    if para:
                        break
                    continue
                para.append(nxt.strip())
            return " ".join(para)
    return ""


def brief_fields(path: Path) -> dict:
    fields = {}
    for line in read_text(path).splitlines():
        m = FIELD_RE.match(line)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    return fields


def brief_slugs() -> set[str]:
    return {p.stem for p in (REPO / "briefs").glob("*.md")}


def index_reports() -> dict[str, dict]:
    """Map brief slug -> report info. Uses the report's 'Brief:' reference, else the filename."""
    out = {}
    for path in sorted((REPO / "reports").glob("*.md")):
        text = read_text(path)
        head = "\n".join(text.splitlines()[:8])
        m = BRIEF_REF_RE.search(head)
        slug = m.group(1) if m else re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
        conf = re.search(r"\*\*Confidence:\*\*\s*(.+)", head)
        out[slug] = {
            "path": rel(path),
            "title": title,
            "answer": section(text, "Answer")[:420],
            "confidence": conf.group(1).strip() if conf else "",
        }
        out.setdefault(path.stem, out[slug])  # sweep briefs are named like the report
    return out


# ---------- archive ----------

def load_archived() -> dict[str, str]:
    """slug -> date archived."""
    return load_json(ARCHIVE_FILE, {})


def set_archived(slug: str, archived: bool) -> dict:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ValueError("Bad slug")
    with lock:
        data = load_archived()
        if archived:
            data[slug] = today()
        else:
            data.pop(slug, None)
        save_json(ARCHIVE_FILE, dict(sorted(data.items())))
    return {"slug": slug, "archived": archived}


# ---------- projects ----------

def load_projects() -> dict:
    data = load_json(PROJECTS_FILE, {})
    data.setdefault("projects", {})
    data.setdefault("members", {})
    return data


def code_projects() -> dict:
    """The claude-ops projects a research project can link to (`repo`): the queue's own, by id."""
    return getattr(ops, "PROJECTS", None) or {}


def check_link(raw) -> str:
    """A research project's `repo`: "" (standalone) or a configured claude-ops project id."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("repo must be a claude-ops project id")
    rid = raw.strip()
    if rid and rid not in code_projects():
        raise ValueError(f"{rid} is not a claude-ops project")
    return rid


def set_link(proj: dict, raw) -> None:
    rid = check_link(raw)
    if rid:
        proj["repo"] = rid
    else:
        proj.pop("repo", None)


def code_project(pid: str | None):
    """The claude-ops project a research project links to, when the link is live and its checkout exists. A stale
    link (the project is gone) or a missing path gives None: no repo context, never an error."""
    proj = load_projects()["projects"].get(pid) if pid else None
    rid = (proj or {}).get("repo")
    p = code_projects().get(rid) if isinstance(rid, str) and rid else None
    return p if p is not None and Path(p.repo).is_dir() else None


def repo_block(p) -> str:
    return (f"This research project is the code project '{p.title}' (claude-ops project `{p.name}`) at {p.repo}.\n"
            f"Before answering, read {p.repo}/CLAUDE.md (and the design doc it names) for what is already built.\n"
            f"That repo is read-only context: research never edits files there.")


def links() -> dict:
    """Per claude-ops project id, the research projects linked to it: [{id, name}]. Stale links included (the
    dashboard only looks up live projects)."""
    out = {}
    for pid, p in load_projects()["projects"].items():
        if isinstance(p.get("repo"), str) and p["repo"]:
            out.setdefault(p["repo"], []).append({"id": pid, "name": p.get("name") or pid})
    for v in out.values():
        v.sort(key=lambda r: r["name"].lower())
    return out


def project_list() -> dict:
    data = load_projects()
    counts = Counter(data["members"].values())
    projects = [{"id": pid, **p, "repo": p.get("repo") or None, "count": counts[pid],
                 "notes": rel(notes_path(pid)) if notes_path(pid).is_file() else None}
                for pid, p in data["projects"].items()]
    projects.sort(key=lambda p: p["name"].lower())
    return {"projects": projects, "members": data["members"]}


def create_project(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("Give the project a name.")
    check_link(body.get("repo"))
    with lock:
        data = load_projects()
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
        pid, n = base, 2
        while pid in data["projects"]:
            pid, n = f"{base}-{n}", n + 1
        data["projects"][pid] = {"name": name, "description": (body.get("description") or "").strip(),
                                 "created": today()}
        set_link(data["projects"][pid], body.get("repo"))
        save_json(PROJECTS_FILE, data)
    return {"id": pid, **data["projects"][pid]}


def update_project(pid: str, body: dict) -> dict:
    with lock:
        data = load_projects()
        proj = data["projects"].get(pid)
        if not proj:
            raise ValueError("No such project")
        if "name" in body:
            if not body["name"].strip():
                raise ValueError("Give the project a name.")
            proj["name"] = body["name"].strip()
        if "description" in body:
            proj["description"] = body["description"].strip()
        if "repo" in body:
            set_link(proj, body["repo"])
        save_json(PROJECTS_FILE, data)
    return {"id": pid, **proj}


def notes_path(pid: str) -> Path:
    return NOTES_DIR / f"{pid}.md"


def save_notes(pid: str, body: dict) -> dict:
    if pid not in load_projects()["projects"]:
        raise ValueError("No such project")
    text = body.get("text") or ""
    write_atomic(notes_path(pid), text if text.endswith("\n") else text + "\n")
    return {"id": pid, "notes": rel(notes_path(pid))}


def project_activity(pid: str) -> list[dict]:
    """Files changed by chats with this project or any of its proposals, newest first."""
    members = {s for s, p in load_projects()["members"].items() if p == pid}
    targets = {f"project:{pid}"} | {f"proposal:{s}" for s in members}
    seen = {}
    for job in load_jobs():
        if job.get("kind") != "chat" or job.get("target") not in targets:
            continue
        for path in job_view(job)["changed"]:
            seen[path] = {"path": path, "when": job["started"], "question": job["question"],
                          "thread": job["thread"], "target": job["target"]}
    return sorted(seen.values(), key=lambda f: f["when"], reverse=True)


def delete_project(pid: str) -> dict:
    """Removes the grouping only; its proposals become unsorted and its notes file stays."""
    with lock:
        data = load_projects()
        data["projects"].pop(pid, None)
        data["members"] = {s: p for s, p in data["members"].items() if p != pid}
        save_json(PROJECTS_FILE, data)
    return {"id": pid, "deleted": True}


def assign(slug: str, pid: str | None) -> dict:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ValueError("Bad slug")
    with lock:
        data = load_projects()
        if pid:
            if pid not in data["projects"]:
                raise ValueError("No such project")
            data["members"][slug] = pid
        else:
            data["members"].pop(slug, None)
        save_json(PROJECTS_FILE, data)
    return {"slug": slug, "project": pid}


# ---------- proposals ----------

def list_proposals() -> list[dict]:
    finalize_chat_jobs()
    archived = load_archived()
    members = load_projects()["members"]
    reports = index_reports()
    jobs = research_jobs()  # oldest first
    # A chat turn counts for every brief it wrote, not just the first.
    by_slug = [(s, j) for j in jobs for s in (j.get("research") or [j["slug"]])]
    running = {s: j for s, j in by_slug if j["status"] == "running"}
    latest_job = dict(by_slug)
    out = []
    for path in (REPO / "briefs").glob("*.md"):
        fields = brief_fields(path)
        slug = path.stem
        tier = TIER_RE.search(fields.get("tier", ""))
        report = reports.get(slug)
        job = latest_job.get(slug)
        if slug in running:
            status = "running"
        elif report:
            status = "done"
        elif job and job["status"] in ("failed", "stopped"):
            status = job["status"]
        elif job and job["status"] == "done":
            status = "answered"  # ran, but the tier wrote no report (T0)
        else:
            status = "proposed"
        out.append({
            "slug": slug,
            "path": rel(path),
            "date": fields.get("date", ""),
            "question": fields.get("question", ""),
            "tier": tier.group(0) if tier else "",
            "tierText": fields.get("tier", ""),
            "status": status,
            "report": report,
            "jobId": job["id"] if job else None,
            "mtime": path.stat().st_mtime,
            "archived": archived.get(slug),
            "project": members.get(slug),
        })
    # Jobs whose brief isn't written yet still show up as proposals.
    briefed = {p["slug"] for p in out}
    # The daily scout (formerly the sweep) and project check write reports but no brief; list those too.
    daily = [p for kind in ("sweep", "scout", "project-check") for p in (REPO / "reports").glob(f"*-{kind}.md")]
    for path in daily:
        slug = path.stem
        if slug in briefed:
            continue
        if slug.endswith("-scout"):
            area = (reports.get(slug) or {}).get("title", "").partition(" — ")[2]
            question = f"Daily scout: {area}" if area else "Daily scout"
        elif slug.endswith("-project-check"):
            question = "Daily project check"
        else:
            question = "Daily watchlist sweep"
        out.append({
            "slug": slug, "path": None, "date": slug[:10], "question": question,
            "tier": "T1", "tierText": "T1", "status": "done", "report": reports.get(slug),
            "jobId": None, "mtime": path.stat().st_mtime, "archived": archived.get(slug),
            "project": members.get(slug),
        })
        briefed.add(slug)
    for slug, j in running.items():
        if slug not in briefed:
            out.append({
                "slug": slug, "path": None, "date": j["started"][:10], "question": j["question"],
                "tier": j["tier"], "tierText": j["tier"], "status": "running", "report": None,
                "jobId": j["id"], "mtime": time.time(), "archived": archived.get(slug),
                "project": members.get(slug),
            })
    out.sort(key=lambda p: (p["date"], p["mtime"]), reverse=True)
    return out


# ---------- references ----------

def norm_url(url: str) -> str:
    return url.rstrip(".,;:").rstrip("/")


def list_references() -> list[dict]:
    corpus = {}
    for folder in ("reports", "reports/raw", "topics", "briefs"):
        for path in (REPO / folder).glob("*.md"):
            for url in URL_RE.findall(read_text(path)):
                corpus.setdefault(norm_url(url), set()).add(rel(path))

    refs, registered, sec = [], set(), "General"
    for line in read_text(REPO / "sources" / "registry.md").splitlines():
        if line.startswith("## "):
            sec = line[3:].strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or cells[0] == "Source" or set("".join(cells)) <= set("-: "):
            continue
        cells += [""] * (4 - len(cells))
        m = URL_RE.search(cells[0])
        url = norm_url(m.group(0)) if m else None
        name = URL_RE.sub("", cells[0]).strip().rstrip("—–-").strip() or url or cells[0]
        if url:
            registered.add(url)
        refs.append({
            "name": name, "url": url, "tier": cells[1], "notes": cells[2], "added": cells[3],
            "section": sec, "citedIn": sorted(corpus.get(url, ())) if url else [],
        })

    for url, files in sorted(corpus.items()):
        if url in registered:
            continue
        refs.append({
            "name": urlparse(url).netloc.removeprefix("www."), "url": url, "tier": "",
            "notes": "", "added": "", "section": "Cited in reports, not in the registry",
            "citedIn": sorted(files),
        })
    return refs


# ---------- jobs (research runs and chat turns) ----------

def load_jobs() -> list[dict]:
    return load_json(JOBS_FILE, [])


def save_jobs(jobs: list[dict]) -> None:
    save_json(JOBS_FILE, jobs)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lstart(pid) -> str:
    """When pid started, as `ps -o lstart=` prints it in the C locale; '' if there is no such process. Read at most
    once a second per pid: every job list asks."""
    hit = LSTART.get(pid)
    if hit and time.time() - hit[0] < 1:
        return hit[1]
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    LSTART[pid] = (time.time(), r.stdout.strip())
    return LSTART[pid][1]


def own(job: dict) -> bool:
    """Whether the job's pid is still the process its run started: alive, and started with the job (its stored
    pid_start, else within START_SLACK seconds of 'started'). macOS reuses pids, so a live pid alone proves
    nothing, and nothing here signals a pid that fails this."""
    pid = job.get("pid")
    if not pid or not pid_alive(pid):
        return False
    began = lstart(pid)
    if not began:
        return False
    if job.get("pid_start"):
        return began == job["pid_start"]
    try:
        t = datetime.strptime(began, "%a %b %d %H:%M:%S %Y")
        return abs((t - datetime.fromisoformat(job["started"])).total_seconds()) <= START_SLACK
    except (KeyError, TypeError, ValueError):
        return False


def short_input(name: str, inp: dict) -> str:
    for key in ("query", "url", "file_path", "description", "skill", "command", "pattern", "prompt"):
        if key in inp:
            val = str(inp[key])
            return val.replace(str(REPO) + "/", "")[:200]
    return json.dumps(inp)[:200]


def log_of(job: dict) -> Path:
    return JOBS_DIR / f"{job['id']}.jsonl"


def read_events(job: dict) -> tuple[list[dict], dict | None, str | None]:
    """Condense the stream-json log into a feed, the result event and the session id. Kept until the file changes."""
    path = log_of(job)
    try:
        st = path.stat()
        key = (st.st_size, st.st_mtime_ns)
        hit = EVENTS.get(path)
        if hit and hit[0] == key:
            return hit[1]
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], None, None
    feed, result, session = [], None, None
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            feed.append({"kind": "stderr", "text": line[:400]})
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            session = ev.get("session_id")
        elif kind == "assistant":
            sub = bool(ev.get("parent_tool_use_id"))
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    entry = {"kind": "tool", "name": block.get("name"),
                             "detail": short_input(block.get("name", ""), inp), "sub": sub}
                    path_ = str(inp.get("file_path") or "")
                    if block.get("name") in WRITE_TOOLS and path_.startswith(str(REPO) + "/"):
                        entry["path"] = path_[len(str(REPO)) + 1:]
                    feed.append(entry)
                elif block.get("type") == "text" and block.get("text", "").strip():
                    feed.append({"kind": "text", "text": block["text"].strip()[:600], "sub": sub})
        elif kind == "result":
            result = ev
    EVENTS[path] = (key, (feed, result, session))
    return feed, result, session


def live(job: dict) -> bool:
    """Its process is still the one the run started (and not a child of ours that exited)."""
    proc = procs.get(job["id"])
    if proc is not None and proc.poll() is not None:  # reaped here, so an ended child leaves no zombie
        procs.pop(job["id"], None)
        return False
    return own(job)


def running(job: dict, result=None) -> bool:
    """No result event yet, and its process lives (see live)."""
    if result is None:
        result = read_events(job)[1]
    return not result and live(job)


def job_view(job: dict, full: bool = False) -> dict:
    feed, result, session = read_events(job)
    if result:
        status = "failed" if result.get("is_error") else "done"
    elif running(job, result):
        status = "running"
    elif job.get("stopped"):
        status = "stopped"
    else:
        status = "failed"
    view = {**job, "kind": job.get("kind", "question"), "status": status, "session": session,
            "steps": sum(f["kind"] == "tool" for f in feed),
            "archived": job["slug"] in load_archived()}
    view.pop("briefsBefore", None)
    view["changed"] = list(dict.fromkeys(f["path"] for f in feed if f.get("path")))
    if view["kind"] == "chat":
        # Briefs this turn wrote that didn't exist before it: research run from chat.
        before = set(job.get("briefsBefore", []))
        view["research"] = [m.group(1) for p in view["changed"]
                            if (m := re.fullmatch(r"briefs/([\w-]+)\.md", p)) and m.group(1) not in before]
    if result:
        view["cost"] = result.get("total_cost_usd")
        view["durationMs"] = result.get("duration_ms")
        view["resultSubtype"] = result.get("subtype")
    if full:
        view["feed"] = feed
        view["result"] = (result or {}).get("result")
    else:
        view["last"] = next((f for f in reversed(feed) if f["kind"] in ("tool", "stderr")), None)
    return view


def all_jobs() -> list[dict]:
    with lock:
        return [job_view(j) for j in load_jobs()]


def research_jobs() -> list[dict]:
    """Dispatched runs, plus chat turns that ran research, shown under the brief they wrote. Oldest first."""
    archived = load_archived()
    out = []
    for j in all_jobs():
        if j["kind"] != "chat":
            out.append(j)
        elif j.get("research"):
            slug = j["research"][0]
            fields = brief_fields(REPO / "briefs" / f"{slug}.md")
            tier = TIER_RE.search(fields.get("tier", ""))
            out.append({**j, "slug": slug, "question": fields.get("question") or j["question"],
                        "tier": tier.group(0) if tier else "chat", "archived": slug in archived})
    return out


def run_env() -> dict:
    """The old server's environment: ours, with the usual bin folders first on PATH. Minus the daemon's own OPS_
    variables, which are for the queue's workers."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPS_")}
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
    return env


def allowed_tools() -> list[str]:
    # Same tool set as bin/daily-scout.sh, plus Agent so T2 runs can spawn workers.
    # bin/fetch is the free fetch ladder (direct -> Jina -> Wayback, robots-aware); it
    # stands in for raw curl, which stays off the list. jq only reads.
    return [
        "Skill", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Agent",
        "Bash(ls:*)", "Bash(date:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(mkdir:*)",
        "Bash(jq:*)", "Bash(bin/fetch:*)", "Bash(./bin/fetch:*)", f"Bash({REPO}/bin/fetch:*)",
        "Bash(bin/todo:*)", "Bash(./bin/todo:*)", f"Bash({REPO}/bin/todo:*)",
        *CHROME_READ_TOOLS,
    ]


def spawn(job: dict, prompt: str, resume: str | None = None, add_dir: Path | None = None) -> dict:
    env = run_env()
    claude = os.environ.get("RESEARCH_DASHBOARD_CLAUDE") or shutil.which("claude", path=env["PATH"]) or "claude"
    cmd = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-budget-usd", str(job["budget"]), "--permission-mode", "dontAsk", "--chrome"]
    if resume:
        cmd += ["--resume", resume]
    if job["model"] != "default":
        cmd += ["--model", job["model"]]
    if add_dir:  # a linked code project's checkout: read-only context outside the research repo
        cmd += ["--add-dir", str(add_dir)]
    cmd += ["--allowedTools", *allowed_tools()]

    job["id"] = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    # Its own session and process group: a daemon restart doesn't reach it, and Stop or the watchdog stops it whole.
    with open(log_of(job), "w") as log:
        proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    procs[job["id"]] = proc
    job["pid"] = proc.pid
    started = datetime.now()
    job["started"] = started.isoformat(timespec="seconds")
    job["pid_start"] = lstart(proc.pid)
    job["deadline"] = (started + timedelta(minutes=TIME_LIMIT_MIN.get(job["tier"], 15))).isoformat(timespec="seconds")
    with lock:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    return job_view(job)


def make_slug(text: str) -> str:
    stop = {"a", "an", "the", "is", "are", "of", "for", "to", "in", "on", "and", "or", "what",
            "whats", "how", "does", "do", "anyone", "there", "any", "can", "i", "we", "it", "its",
            "be", "with", "by", "should", "my", "me", "this", "that", "who", "which", "into"}
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stop]
    slug = "-".join(words[:6]) or "research"
    base, n = slug, 2
    taken = brief_slugs() | {j["slug"] for j in load_jobs()}
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


UNATTENDED = """This run was dispatched from the local dashboard and is unattended: nobody can
answer questions or approve anything.
- Today is {today}.
- Do not ask profile questions. Use the founder profile saved in memory and state in
  the brief what you assumed. Business screens: size the ceiling first, then assess
  profiles A/B/C (the skill's "Business screens" section).
- Respect the tier caps in the brief.
- Follow the skill's access ladder (access.md): APIs first; `bin/fetch "<url>"` for
  anything you quote or count; Chrome (read-only tools) when bin/fetch says
  UNREACHABLE (if get_page_text errors or is near-empty, navigate again and retry,
  up to 3 tries). If Chrome isn't connected or shows a bot check, append the URL to
  data/needs-browser.md and name it in the report's "Blocked or unreachable" line.
  Never try to get past a bot check, login or paywall.
- Fold back into topics/, sources/registry.md and watchlist.md as the skill says.
- End with the skill's final summary format."""


def build_prompt(kind: str, slug: str, tier: str, question: str, notes: str, code=None) -> str:
    rules = UNATTENDED.format(today=today())
    if kind == "brief":
        head = (f"Use the research skill to run the existing brief briefs/{slug}.md against {REPO}.\n"
                f"The brief is already written: keep its question, tier and caps. Write the report to\n"
                f"reports/{today()}-{slug}.md and reference `briefs/{slug}.md` in its header.")
    else:
        head = (f"Use the research skill to answer this question against {REPO}:\n\n{question}\n\n"
                f"Tier: {tier}. Use the slug `{slug}`: write briefs/{slug}.md first, then (if the tier\n"
                f"calls for one) reports/{today()}-{slug}.md with `briefs/{slug}.md` in its header.")
    if notes:
        head += f"\n\nExtra context from Adam:\n{notes}"
    if code is not None:
        head += f"\n\n{repo_block(code)}"
    return f"{head}\n\n{rules}"


def dispatch(body: dict) -> dict:
    if ops.usage_braked():  # a run is a new Claude session; chats are never held
        raise Refused(409, {"error": (ops.usage_info()["braked"] or {}).get("words") or "The usage brake is on.",
                            "braked": True})
    kind = "brief" if body.get("brief") else "question"
    question = (body.get("question") or "").strip()
    notes = (body.get("notes") or "").strip()
    if kind == "brief":
        slug = body["brief"]
        brief = REPO / "briefs" / f"{slug}.md"
        if not SLUG_RE.fullmatch(slug or "") or not brief.exists():
            raise ValueError(f"No brief named {slug}")
        fields = brief_fields(brief)
        t = TIER_RE.search(fields.get("tier", ""))
        question = fields.get("question") or slug
        tier = t.group(0) if t else "T1"
    else:
        if not question:
            raise ValueError("Write a question first.")
        tier = body.get("tier") if body.get("tier") in TIER_SIZE else "T1"
        slug = re.sub(r"[^a-z0-9-]", "", (body.get("slug") or "").lower()) or make_slug(question)
    if any(j["slug"] == slug and j["status"] == "running" and j["kind"] != "chat" for j in all_jobs()):
        raise ValueError(f"{slug} is already running.")
    if body.get("project"):
        assign(slug, body["project"])

    job = {"kind": kind, "slug": slug, "question": question, "tier": tier,
           "model": (body.get("model") or "").strip() or "default",
           **run_size(body, TIER_SIZE.get(tier, "normal")), "notes": notes}
    code = code_project(project_of("proposal", slug))
    return spawn(job, build_prompt(kind, slug, tier, question, notes, code), add_dir=code.repo if code else None)


def run_size(body: dict, default: str) -> dict:
    size = body.get("size") if body.get("size") in RUN_SIZES else default
    return {"size": size, "budget": RUN_SIZES[size]}


def killgroup(job: dict) -> None:
    """SIGTERM the run's process group, only while its pid is still the run's own process."""
    if live(job):
        try:
            os.killpg(job["pid"], signal.SIGTERM)
        except OSError:
            pass


def stop_job(job_id: str) -> dict:
    with lock:
        jobs = load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            raise ValueError("No such job")
        killgroup(job)
        job["stopped"] = True
        save_jobs(jobs)
    return job_view(job)


# ---------- to-dos that become queue tasks ----------
# A to-do's `task` is a claude-ops task id. todos.update() writes only its FIELDS, so `task` (and closing a to-do)
# is written here, under the repo's own todos._locked(): the lock stays shared with bin/todo.

PRIORITY = {"high": 1, "normal": 0, "low": -1}
DEAD = ("failed", "cancelled")  # a linked task in any other state is live: no second one


def task_rows(ids) -> dict:
    ids = sorted({i for i in ids if isinstance(i, int)})
    if not ids:
        return {}
    rows = ops.q(f"SELECT id, state, merged, finished FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: r for r in rows}


def todo_list() -> list[dict]:
    """todos.list_todos() plus `taskState` on each to-do with a `task`: {state, merged}, or None if the row is gone."""
    out = todos.list_todos()
    rows = task_rows(t.get("task") for t in out)
    for t in out:
        if isinstance(t.get("task"), int):
            r = rows.get(t["task"])
            t["taskState"] = {"state": r["state"], "merged": bool(r["merged"])} if r else None
    return out


def todo_task(tid: int, body: dict) -> dict:
    """Queue a claude-ops task from to-do tid, as /api/tasks would, and store its id as the to-do's `task`."""
    project = (body.get("project") or "").strip() if isinstance(body.get("project"), str) else ""
    if not project:
        raise ValueError("Pick a code project.")
    if project not in code_projects():
        raise ValueError(f"{project} is not a claude-ops project")
    text = (body.get("body") or "").strip()
    if not text:
        raise ValueError("describe the task")
    prio = body.get("priority", 0)
    prio = PRIORITY[prio] if prio in PRIORITY else int(prio or 0)
    if prio not in (1, 0, -1):
        raise ValueError("priority must be high, normal or low")
    release = body.get("release")
    with todos._locked() as data:
        todo = next((t for t in data["todos"] if t["id"] == tid), None)
        if not todo:
            raise ValueError(f"No to-do #{tid}")
        if todo.get("done"):
            raise ValueError(f"To-do #{tid} is done.")
        old = todo.get("task")
        if isinstance(old, int) and (r := task_rows([old]).get(old)) and r["state"] not in DEAD:
            raise ValueError(f"To-do #{tid} already has task #{old} ({r['state']}).")
        new = ops.add_task(project, text, body.get("title") or "", prio, bool(body.get("hold")),
                           auto_release=None if release is None or release == "" else bool(release))
        todo["task"] = new
    return {"id": new}


def close_line(task_id: int, date: str) -> str:
    return f"Closed by task #{task_id}, merged {date}."


def close_merged() -> None:
    """Close each open to-do whose task is done and merged: done is the day it finished, plus a line in notes.
    A to-do already carrying that task's line stays as it is, so one reopened by hand stays open."""
    def due(data):
        rows = task_rows(t.get("task") for t in data["todos"] if not t.get("done"))
        for t in data["todos"]:
            r = rows.get(t.get("task")) if isinstance(t.get("task"), int) else None
            if (not t.get("done") and r and r["state"] == "done" and r["merged"]
                    and f"Closed by task #{r['id']}," not in (t.get("notes") or "")):
                yield t, r
    if not any(due(todos._load())):
        return
    try:
        writable()
    except Refused as e:
        print("research watchdog:", e, flush=True)
        return
    with todos._locked() as data:
        for t, r in list(due(data)):
            date = datetime.fromtimestamp(r["finished"]).strftime("%Y-%m-%d") if r["finished"] else today()
            t["done"] = date
            t["notes"] = (t["notes"].rstrip() + "\n" if t.get("notes") else "") + close_line(r["id"], date)


# ---------- watchdog: time limits, the usage meter, notifications ----------

def watchdog() -> None:
    while True:
        time.sleep(WATCHDOG_SECONDS)
        try:
            watchdog_pass()
        except Exception as e:
            print("research watchdog error:", e, flush=True)


def watchdog_pass(now=None) -> None:
    """Stop runs that pass their time limit, feed the usage meter, notify. Deadlines live in jobs.json, so this
    survives restarts. Nothing at all while the old server runs: its own watchdog covers it."""
    if not current() or old_running():
        return
    try:
        close_merged()
    except Exception as e:
        print("research watchdog: closing to-dos:", e, flush=True)
    now = now or datetime.now()
    with lock:
        jobs = load_jobs()
        late, alive = [], set()
        for job in jobs:
            if not live(job):
                continue
            alive.add(job["id"])
            if job.get("stopped") or not job.get("deadline"):
                continue
            deadline = datetime.fromisoformat(job["deadline"])
            # Skip deadlines long past: the daemon was down and the run may have been left on purpose.
            if deadline < now < deadline + timedelta(hours=1):
                late.append(job)
        for job in jobs:
            if job["id"] in alive or job["id"] in OFFSETS:  # once more after it ends, for its last events
                tail_usage(job)
                if job["id"] not in alive:
                    OFFSETS.pop(job["id"], None)
        notes = [(job, n) for job in jobs if job["id"] not in alive and (n := notice(job))]
        if not late and not notes:
            return
        try:
            writable()
        except Refused as e:
            print("research watchdog:", e, flush=True)
            return
        for job in late:
            killgroup(job)
            job["stopped"] = job["timedOut"] = True
        for job, _ in notes:
            job["notified"] = True
        save_jobs(jobs)
    for job, (title, msg) in notes:  # after the save: a notification goes at most once
        ops.notify(title, msg)


def notice(job: dict):
    """(title, message) to notify now that this ended job finished, failed or timed out, or None. A chat notifies
    only when it fails or times out; a run you stopped, never."""
    if job.get("notified"):
        return None
    try:
        if time.time() - log_of(job).stat().st_mtime > NOTIFY_WITHIN:
            return None
    except OSError:
        return None
    status = job_view(job)["status"]
    what = job.get("question") or job.get("slug") or ""
    chat = job.get("kind") == "chat"
    if job.get("timedOut"):
        return ("Research chat timed out" if chat else "Research run timed out"), what
    if status == "failed":
        return ("Research chat failed" if chat else "Research run failed"), what
    if status == "done" and not chat:
        return "Research run finished", what
    return None


def tail_usage(job: dict) -> None:
    """Feed every rate_limit_event the job's .jsonl got since the last pass to the usage meter. Whole lines only;
    the byte offset per job means no line is read twice."""
    path = log_of(job)
    try:
        size = path.stat().st_size
        if job["id"] not in OFFSETS:
            # Older than the meter's newest reading: all its readings are stale, so start at its end.
            newest = max((r["ts"] for k in ops.USAGE_WINDOWS if (r := ops.usage_reading(ops.setting(f"usage:{k}")))),
                         default=0)
            OFFSETS[job["id"]] = size if path.stat().st_mtime < newest else 0
        start = OFFSETS[job["id"]]
        if size <= start:
            return
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
    except OSError:
        return
    end = chunk.rfind(b"\n") + 1
    OFFSETS[job["id"]] = start + end
    for line in chunk[:end].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        w = ops.usage_of(e) if isinstance(e, dict) else None
        if w:
            ops.usage_seen(w)


# ---------- chat ----------

CHAT_RULES = """How to work in this chat:
- Today is {today}. Every message you get comes from Adam, typed into the dashboard.
- Answer from the knowledge base first: the files above, topics/, sources/registry.md.
  Search the web only when they don't cover the question or Adam asks you to.
- If Adam asks for new research, follow the research skill: brief first, pick a tier,
  respect its caps, write a dated report, fold back into topics/ and sources/registry.md.
  Reports are immutable, so new research gets its own brief and report with a new slug.
  Put "- **Follow-up to:** {parent}" in that brief.
- {notes_rule}
- {todo_rule}
- If Adam asks you to change a file, change it and name the file.
- Nobody approves tool calls and you can't ask mid-turn. If something needs Adam's
  decision, stop and ask in your reply.
- Keep replies short and plain. Source and date claims. Use the skill's final summary
  format only when you ran new research."""


def proposal_line(slug: str, reports: dict) -> str:
    q = brief_fields(REPO / "briefs" / f"{slug}.md").get("question", "")
    report = reports.get(slug, {}).get("path") or "none yet"
    return f"- `{slug}`: {q}\n  brief: briefs/{slug}.md · report: {report}"


def project_of(kind: str, tid: str) -> str | None:
    data = load_projects()
    pid = tid if kind == "project" else data["members"].get(tid)
    return pid if pid in data["projects"] else None


def notes_rule(kind: str, tid: str) -> str:
    pid = project_of(kind, tid)
    if not pid:
        return ("This proposal isn't in a project. If Adam asks you to save context for it, ask which\n"
                "  project it belongs to (he can create one in the dashboard).")
    proj = load_projects()["projects"][pid]
    path = rel(notes_path(pid))
    state = "Read it before your first answer." if notes_path(pid).is_file() else "It doesn't exist yet."
    return (f"Project notes live in {path}. {state} When Adam asks you to add, save or note\n"
            f"  something for the project, append a dated entry there (\"## {today()} — <title>\"); if the\n"
            f"  file is missing, start it with \"# Project: {proj['name']}\" and the description. Label\n"
            f"  reasoning as reasoning. Don't put project context in topics/: topics/ holds sourced\n"
            f"  research deltas, which you still add when you run new research.")


def todo_rule(kind: str, tid: str) -> str:
    link = f"--proposal {tid}" if kind == "proposal" else f"--project {tid}"
    return (f"To-dos: when Adam asks you to add, track or remember an action, use the todo skill\n"
            f"  (`bin/todo add \"<action>\" {link}`); it shows in the dashboard's To-dos tab.\n"
            f"  Don't add to-dos he didn't ask for; suggest them in your reply instead.")


def open_todos_lines(kind: str, tid: str) -> list[str]:
    key = "proposal" if kind == "proposal" else "effectiveProject"
    items = [t for t in todos.list_todos() if not t["done"] and t.get(key) == tid]
    if not items:
        return []
    return ["", "Open to-dos here:"] + [f"- #{t['id']} {t['text']}" + (f" (due {t['due']})" if t.get("due") else "")
                                        for t in items]


def chat_context(kind: str, tid: str) -> str:
    data = load_projects()
    reports = index_reports()
    if kind == "proposal":
        lines = [f"You're in a dashboard chat with Adam about one research proposal in {REPO}.",
                 "", "Proposal:", proposal_line(tid, reports)]
        pid = data["members"].get(tid)
        if pid and pid in data["projects"]:
            proj = data["projects"][pid]
            lines += ["", f"It belongs to the project \"{proj['name']}\"."
                      + (f" {proj['description']}" if proj["description"] else "")]
            siblings = [s for s, p in data["members"].items() if p == pid and s != tid]
            if siblings:
                lines += ["Other proposals in that project:"] + [proposal_line(s, reports) for s in sorted(siblings)]
        lines += ["", "Read the brief and the report before your first answer."]
        parent = f"briefs/{tid}.md"
    else:
        proj = data["projects"][tid]
        slugs = sorted(s for s, p in data["members"].items() if p == tid)
        lines = [f"You're in a dashboard chat with Adam about the research project \"{proj['name']}\" in {REPO}."]
        if proj["description"]:
            lines.append(f"Project description: {proj['description']}")
        lines += ["", "Proposals in this project:"]
        lines += [proposal_line(s, reports) for s in slugs] or ["- none yet"]
        lines += ["", "Read what you need before your first answer; start with the reports."]
        parent = f"project \"{proj['name']}\""
    code = code_project(project_of(kind, tid))
    if code is not None:
        lines += ["", repo_block(code)]
    lines += open_todos_lines(kind, tid)
    return "\n".join(lines) + "\n\n" + CHAT_RULES.format(today=today(), parent=parent,
                                                         notes_rule=notes_rule(kind, tid),
                                                         todo_rule=todo_rule(kind, tid))


def load_threads() -> dict:
    return load_json(THREADS_FILE, {})


def parse_target(target: str) -> tuple[str, str]:
    kind, _, tid = (target or "").partition(":")
    if kind == "proposal" and SLUG_RE.fullmatch(tid) and (REPO / "briefs" / f"{tid}.md").is_file():
        return kind, tid
    if kind == "project" and tid in load_projects()["projects"]:
        return kind, tid
    raise ValueError("Unknown chat target")


def chat(body: dict) -> dict:
    target = body.get("target", "")
    kind, tid = parse_target(target)
    message = (body.get("message") or "").strip()
    if not message:
        raise ValueError("Write a message first.")
    with lock:
        threads = load_threads()
        thread = threads.get(body.get("thread") or "")
        if thread and thread["target"] != target:
            raise ValueError("That conversation belongs to something else.")
        if not thread:
            tid_ = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            thread = threads[tid_] = {"id": tid_, "target": target, "created": datetime.now().isoformat(timespec="seconds"),
                                      "title": message[:80], "turns": [], "ctx": CONTEXT_VERSION}
        jobs = {j["id"]: j for j in load_jobs()}
        turns = [jobs[t] for t in thread["turns"] if t in jobs]
        if turns and job_view(turns[-1])["status"] == "running":
            raise ValueError("Wait for the current reply, or stop it.")
        resume = next((s for s in (read_events(t)[2] for t in reversed(turns)) if s), None)
        if not resume:
            prompt = f"{chat_context(kind, tid)}\n\nAdam's first message:\n{message}"
        elif thread.get("ctx", 1) < CONTEXT_VERSION:
            # Older conversation: tell it once about rules added since it started.
            prompt = (f"(Dashboard update, applies from now on:\n- {notes_rule(kind, tid)}\n- {todo_rule(kind, tid)})"
                      f"\n\n{message}")
        else:
            prompt = message
        thread["ctx"] = CONTEXT_VERSION
        job = {"kind": "chat", "target": target, "thread": thread["id"], "slug": f"chat:{target}",
               "question": message, "tier": "chat", "model": (body.get("model") or "").strip() or "default",
               **run_size(body, CHAT_SIZE), "notes": "",
               "briefsBefore": sorted(brief_slugs())}
        code = code_project(project_of(kind, tid))
        view = spawn(job, prompt, resume, add_dir=code.repo if code else None)
        thread["turns"].append(view["id"])
        save_json(THREADS_FILE, threads)
    return {"thread": thread["id"], "turn": view}


def finalize_chat_jobs() -> None:
    """When a chat turn ends, note the briefs it created and file them into the project. It runs on reads, so it
    writes only when it may (otherwise the old server, which may, does it)."""
    with lock:
        jobs = load_jobs()
        data = load_projects()
        changed = False
        for j in jobs:
            if j.get("kind") != "chat" or j.get("finalized") or job_view(j)["status"] == "running":
                continue
            new = sorted(brief_slugs() - set(j.get("briefsBefore", [])))
            kind, _, tid = j["target"].partition(":")
            pid = tid if kind == "project" else data["members"].get(tid)
            if pid in data["projects"]:
                for slug in new:
                    data["members"].setdefault(slug, pid)
            j["newBriefs"], j["finalized"], changed = new, True, True
        if not changed:
            return
        try:
            writable()
        except Refused:
            return
        save_jobs(jobs)
        save_json(PROJECTS_FILE, data)


def threads_for(target: str) -> list[dict]:
    threads = [t for t in load_threads().values() if t["target"] == target]
    threads.sort(key=lambda t: t["created"])
    return [{k: t[k] for k in ("id", "created", "title")} | {"turns": len(t["turns"])} for t in threads]


def thread_view(thread_id: str) -> dict | None:
    finalize_chat_jobs()
    with lock:
        thread = load_threads().get(thread_id)
        if not thread:
            return None
        jobs = {j["id"]: j for j in load_jobs()}
        return {**thread, "turns": [job_view(jobs[t], full=True) for t in thread["turns"] if t in jobs]}


# ---------- for opsd: /api/state, routes, the watchdog thread ----------

def state():
    """/api/state's research: None while it is off, else {active: running runs and chat turns, readonly, links:
    the research projects linked to each claude-ops project}."""
    if not current():
        return None
    out = {"active": 0, "readonly": old_running(), "links": {}}
    try:
        with lock:
            out["active"] = sum(running(j) for j in load_jobs())
            out["links"] = links()
    except Exception as e:
        out["error"] = str(e)
    return out


def start():
    threading.Thread(target=watchdog, daemon=True).start()


def get(path: str, query: str):
    """A GET under /api/research/: (status, body). Reads always work, whoever writes."""
    if not current():
        return 404, OFF
    path = path.removeprefix("/api/research")
    q = parse_qs(query)
    arg = lambda k: q.get(k, [""])[0]  # noqa: E731
    try:
        if path == "/proposals":
            return 200, list_proposals()
        if path == "/projects":
            return 200, project_list()
        m = re.fullmatch(r"/projects/([\w-]+)/activity", path)
        if m:
            return 200, project_activity(m.group(1))
        if path == "/references":
            return 200, list_references()
        if path == "/todos":
            return 200, todo_list()
        if path == "/jobs":
            return 200, list(reversed(research_jobs()))
        if path.startswith("/jobs/"):
            job_id = path.rsplit("/", 1)[1]
            with lock:
                job = next((j for j in load_jobs() if j["id"] == job_id), None)
            return (200, job_view(job, full=True)) if job else (404, {"error": "not found"})
        if path == "/threads":
            return 200, threads_for(arg("target"))
        if path.startswith("/threads/"):
            view = thread_view(path.rsplit("/", 1)[1])
            return (200, view) if view else (404, {"error": "not found"})
        if path == "/file":
            f = (REPO / arg("path")).resolve()
            if REPO not in f.parents or f.suffix != ".md" or not f.is_file():
                return 404, {"error": "not found"}
            return 200, {"path": rel(f), "text": read_text(f)}
        if path == "/slug":
            return 200, {"slug": make_slug(arg("q"))}
        return 404, {"error": "not found"}
    except Exception as e:  # local tool: show the error in the UI
        return 500, {"error": str(e)}


def post(path: str, body: dict):
    """A POST under /api/research/: (status, body). Every one writes, so each first checks it may (writable)."""
    if not current():
        return 404, OFF
    path = path.removeprefix("/api/research")
    try:
        handler = route(path, body)
        if not handler:
            return 404, {"error": "not found"}
        writable()
        return 200, handler()
    except Refused as e:
        return e.code, e.body
    except ValueError as e:
        return 400, {"error": str(e)}
    except Exception as e:
        return 500, {"error": str(e)}


def route(path: str, body: dict):
    """The function a POST path runs, or None for an unknown path."""
    if path == "/dispatch":
        return lambda: dispatch(body)
    if path == "/chat":
        return lambda: chat(body)
    if path == "/archive":
        return lambda: set_archived(body.get("slug"), bool(body.get("archived", True)))
    if path == "/assign":
        return lambda: assign(body.get("slug"), body.get("project") or None)
    if path == "/projects":
        return lambda: create_project(body)
    m = re.fullmatch(r"/projects/([\w-]+)(/delete|/notes)?", path)
    if m:
        pid, action = m.groups()
        if action == "/delete":
            return lambda: delete_project(pid)
        if action == "/notes":
            return lambda: save_notes(pid, body)
        return lambda: update_project(pid, body)
    if path == "/todos":
        return lambda: todos.add(body)
    m = re.fullmatch(r"/todos/(\d+)/task", path)
    if m:
        return lambda: todo_task(int(m.group(1)), body)
    m = re.fullmatch(r"/todos/(\d+)(/delete)?", path)
    if m:
        tid = int(m.group(1))
        return lambda: todos.delete(tid) if m.group(2) else todos.update(tid, body)
    m = re.fullmatch(r"/jobs/([\w-]+)/stop", path)
    if m:
        return lambda: stop_job(m.group(1))
    return None
