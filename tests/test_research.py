"""The research backend (research.py, README ## Research). Stdlib only, no Claude calls: a fake claude script on
PATH records its arguments and streams an init event (then sleeps when FAKE_SLEEP is set, or ends with a result).
The fake research repo is a copy of tests/fixtures/research, and a free port stands in for the old server's 8787.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if "opsd" not in sys.modules:
    os.environ["OPS_DATA"] = tempfile.mkdtemp()
    os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402
import research  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "research"
SCRATCH = Path(tempfile.mkdtemp())

FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARGS"
pwd > "$FAKE_CWD"
echo '{"type":"system","subtype":"init","session_id":"fake-sid"}'
if [ -n "$FAKE_SLEEP" ]; then exec sleep "$FAKE_SLEEP"; fi
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok","total_cost_usd":0}'
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def iso(dt):
    return dt.isoformat(timespec="seconds")


class Research(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        bin_ = SCRATCH / "bin"
        bin_.mkdir(exist_ok=True)
        fake = bin_ / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        os.environ["PATH"] = f"{bin_}:{os.environ['PATH']}"
        os.environ["RESEARCH_DASHBOARD_CLAUDE"] = str(fake)  # run_env puts ~/.local/bin (the real claude) first
        os.environ["FAKE_ARGS"], os.environ["FAKE_CWD"] = str(SCRATCH / "args"), str(SCRATCH / "cwd")
        os.environ["OPS_RESEARCH_OLD_PORT"] = str(free_port())
        cls.notes, cls.real_notify = [], opsd.notify
        opsd.notify = lambda title, msg: cls.notes.append((title, msg))
        opsd.READY.set()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", free_port()), opsd.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        opsd.notify = cls.real_notify

    def setUp(self):
        self.repo = SCRATCH / f"repo-{self.id().rsplit('.', 1)[1]}"
        shutil.copytree(FIXTURE, self.repo)
        old = time.time() - 2 * 86400  # the fixture's runs ended days ago: nothing to notify
        for f in (self.repo / "logs" / "dashboard").glob("*.jsonl"):
            os.utime(f, (old, old))
        shutil.rmtree(opsd.DATA / "research-backup", ignore_errors=True)
        for k in ("usage_brake", "usage:five_hour", "usage:seven_day", "usage_noted:five_hour"):
            opsd.q("DELETE FROM settings WHERE key=?", (k,))
        opsd.set_setting("research_repo", str(self.repo.resolve()))
        self.restart()
        self.notes.clear()
        self.mine = []  # pids this test started, stopped by pid in tearDown
        os.environ.pop("FAKE_SLEEP", None)

    def tearDown(self):
        for pid in self.mine:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        for proc in research.procs.values():
            proc.wait()
        self.restart()

    def restart(self):
        """What a new daemon would know: nothing in memory."""
        research.BACKED_UP.clear()
        research.OFFSETS.clear()
        research.EVENTS.clear()
        research.LSTART.clear()
        research.procs.clear()
        research.REPO = None

    # ---- helpers

    def get(self, path):
        try:
            with urlopen(self.url + path) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def post(self, path, body=None):
        req = Request(self.url + path, data=json.dumps(body or {}).encode(),
                      headers={"Content-Type": "application/json", "X-Ops": "1"})
        try:
            with urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def state(self):
        opsd.STATE_CACHE["data"] = None
        return self.get("/api/state")[1]

    def file(self, rel):
        return json.loads((self.repo / rel).read_text())

    def backups(self):
        root = opsd.DATA / "research-backup"
        return sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []

    def sleeper(self, secs=60):
        p = subprocess.Popen(["sleep", str(secs)], start_new_session=True)
        self.mine.append(p.pid)
        self.addCleanup(p.wait)
        time.sleep(0.1)
        return p

    def add_job(self, **job):
        jobs = self.file("logs/dashboard/jobs.json")
        job = {"kind": "question", "slug": "live-run", "question": "Live?", "tier": "T1", "model": "default",
               "size": "normal", "budget": 3, "notes": "", **job}
        jobs.append(job)
        (self.repo / "logs/dashboard/jobs.json").write_text(json.dumps(jobs, indent=2))
        return job

    def log(self, job_id, *events):
        with open(self.repo / "logs" / "dashboard" / f"{job_id}.jsonl", "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def wait_done(self, job_id, secs=10):
        end = time.time() + secs
        while time.time() < end:
            if self.get(f"/api/research/jobs/{job_id}")[1]["status"] != "running":
                return
            time.sleep(0.1)
        self.fail(f"{job_id} still running")

    def listen_old(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", int(os.environ["OPS_RESEARCH_OLD_PORT"])))
        s.listen(16)
        self.addCleanup(s.close)
        return s

    # ---- off

    def test_off_answers_404(self):
        opsd.set_setting("research_repo", "")
        for path in ("/api/research/proposals", "/api/research/jobs", "/api/research/todos"):
            self.assertEqual(self.get(path), (404, {"error": "research is off"}))
        self.assertEqual(self.post("/api/research/archive", {"slug": "x"}), (404, {"error": "research is off"}))
        self.assertIsNone(self.state()["research"])

    def test_setting_checks_the_repo(self):
        code, body = self.post("/api/settings", {"research_repo": str(SCRATCH)})
        self.assertEqual(code, 400)
        self.assertIn("isn't the research repo", body["error"])
        self.assertEqual(self.post("/api/settings", {"research_repo": str(self.repo)})[0], 200)
        self.assertEqual(self.state()["settings"]["research_repo"], str(self.repo.resolve()))

    # ---- reads

    def test_reads_return_the_fixture(self):
        props = {p["slug"]: p for p in self.get("/api/research/proposals")[1]}
        self.assertEqual(set(props), {"llm-search-tools", "old-idea"})
        p = props["llm-search-tools"]
        self.assertEqual((p["status"], p["tier"], p["project"], p["question"]),
                         ("done", "T1", "ai-search", "Which LLM search tools are worth using?"))
        self.assertEqual(p["report"]["path"], "reports/2026-10-04-llm-search-tools.md")
        self.assertEqual(p["report"]["confidence"], "medium")
        self.assertEqual(props["old-idea"]["archived"], "2026-10-02")
        self.assertEqual(props["old-idea"]["status"], "proposed")

        projects = self.get("/api/research/projects")[1]
        self.assertEqual(projects["members"], {"llm-search-tools": "ai-search"})
        self.assertEqual(projects["projects"][0]["notes"], "projects/ai-search.md")

        refs = {r["url"]: r for r in self.get("/api/research/references")[1]}
        self.assertEqual(refs["https://example.com/a"]["section"], "Search")
        self.assertEqual(refs["https://example.com/a"]["citedIn"], ["reports/2026-10-04-llm-search-tools.md"])
        self.assertEqual(refs["https://example.org/b"]["section"], "Cited in reports, not in the registry")

        todos = self.get("/api/research/todos")[1]
        want = self.file("dashboard/todos.json")["todos"][0]
        self.assertEqual(todos, [{**want, "effectiveProject": "ai-search"}])

        jobs = self.get("/api/research/jobs")[1]
        self.assertEqual([j["id"] for j in jobs], ["20261004-100000-aaaa"])  # chats show only when they ran research
        self.assertEqual((jobs[0]["status"], jobs[0]["cost"], jobs[0]["steps"]), ("done", 1.2, 1))
        job = self.get("/api/research/jobs/20261004-100000-aaaa")[1]
        self.assertEqual(job["result"], "Two are worth it.")
        self.assertEqual(job["feed"][0]["detail"], "llm search tools")
        self.assertEqual(self.get("/api/research/jobs/nope")[0], 404)

        threads = self.get("/api/research/threads?target=project:ai-search")[1]
        self.assertEqual(threads, [{"id": "20261005-090000-bbbb", "created": "2026-10-05T09:00:00",
                                    "title": "What next?", "turns": 1}])
        thread = self.get("/api/research/threads/20261005-090000-bbbb")[1]
        self.assertEqual([t["result"] for t in thread["turns"]], ["Run the old idea next."])
        activity = self.get("/api/research/projects/ai-search/activity")[1]
        self.assertEqual(activity, [])

        f = self.get("/api/research/file?path=briefs/old-idea.md")[1]
        self.assertEqual(f["path"], "briefs/old-idea.md")
        self.assertIn("Is the old idea any good?", f["text"])
        self.assertEqual(self.get("/api/research/slug?q=Which+LLM+search+tools")[1], {"slug": "llm-search-tools-2"})
        self.assertEqual(self.backups(), [])  # reads write nothing

    def test_file_refuses_paths_outside_the_repo(self):
        outside = SCRATCH / "secret.md"
        outside.write_text("secret")
        for path in (f"../{outside.name}", str(outside), "../../../../etc/hosts", "dashboard/projects.json"):
            self.assertEqual(self.get(f"/api/research/file?path={path}")[0], 404, path)

    # ---- backup and atomic writes

    def test_first_write_backs_up_once_per_start(self):
        before = (self.repo / "dashboard/archived.json").read_text()
        self.assertEqual(self.post("/api/research/archive", {"slug": "llm-search-tools"})[0], 200)
        self.assertEqual(self.post("/api/research/archive", {"slug": "old-idea", "archived": False})[0], 200)
        names = self.backups()
        self.assertEqual(len(names), 1)
        b = opsd.DATA / "research-backup" / names[0]
        self.assertEqual((b / "archived.json").read_text(), before)
        self.assertEqual(sorted(p.name for p in b.iterdir()),
                         ["archived.json", "jobs.json", "notes", "projects.json", "threads.json", "todos.json"])
        self.assertEqual((b / "notes" / "ai-search.md").read_text(), "# Project: AI search\n\nSearch ideas\n")
        self.assertFalse(list(b.glob("*.jsonl")))
        self.restart()  # a new daemon backs up again before its first write
        self.post("/api/research/archive", {"slug": "old-idea"})
        self.assertEqual(len(self.backups()), 2)

    def test_backups_are_pruned_to_10(self):
        root = opsd.DATA / "research-backup"
        for i in range(12):
            (root / f"20200101-0000{i:02d}").mkdir(parents=True)
        self.post("/api/research/archive", {"slug": "old-idea"})
        names = self.backups()
        self.assertEqual(len(names), 10)
        self.assertNotIn("20200101-000002", names)
        self.assertEqual(names[-1][:8], datetime.now().strftime("%Y%m%d"))

    def test_backup_failure_refuses_the_write(self):
        (opsd.DATA / "research-backup").parent.mkdir(parents=True, exist_ok=True)
        (opsd.DATA / "research-backup").write_text("in the way")
        self.addCleanup((opsd.DATA / "research-backup").unlink)
        before = (self.repo / "dashboard/archived.json").read_text()
        code, body = self.post("/api/research/archive", {"slug": "llm-search-tools"})
        self.assertEqual(code, 500)
        self.assertIn("Couldn't back up", body["error"])
        self.assertEqual((self.repo / "dashboard/archived.json").read_text(), before)

    def test_writes_are_atomic(self):
        self.post("/api/research/archive", {"slug": "llm-search-tools"})
        code, proj = self.post("/api/research/projects", {"name": "Music DAW", "description": "Audio"})
        self.assertEqual((code, proj["id"]), (200, "music-daw"))
        self.post("/api/research/assign", {"slug": "old-idea", "project": "music-daw"})
        self.post("/api/research/projects/music-daw/notes", {"text": "# Project: Music DAW"})
        self.post("/api/research/projects/ai-search", {"description": "Search, renamed"})
        code, todo = self.post("/api/research/todos", {"text": "Call Bob", "project": "music-daw"})
        self.assertEqual((code, todo["id"]), (200, 2))
        self.post("/api/research/todos/1", {"done": True})
        self.assertEqual(self.file("dashboard/archived.json"),
                         {"llm-search-tools": datetime.now().strftime("%Y-%m-%d"), "old-idea": "2026-10-02"})
        projects = self.file("dashboard/projects.json")
        self.assertEqual(projects["members"], {"llm-search-tools": "ai-search", "old-idea": "music-daw"})
        self.assertEqual(projects["projects"]["ai-search"]["description"], "Search, renamed")
        self.assertEqual((self.repo / "projects/music-daw.md").read_text(), "# Project: Music DAW\n")
        todos = self.file("dashboard/todos.json")["todos"]
        self.assertEqual([(t["text"], bool(t["done"])) for t in todos], [("Email the vendor", True), ("Call Bob", False)])
        self.post("/api/research/projects/music-daw/delete")
        self.assertNotIn("music-daw", self.file("dashboard/projects.json")["projects"])
        left = [p for p in self.repo.rglob("*") if p.name.endswith(".tmp") or ".partial" in p.name]
        self.assertEqual(left, [])
        self.assertEqual(oct((self.repo / "dashboard/projects.json").stat().st_mode & 0o777),
                         oct((FIXTURE / "dashboard/projects.json").stat().st_mode & 0o777))

    # ---- one writer

    def test_old_server_makes_it_read_only(self):
        self.listen_old()
        writes = [("/api/research/archive", {"slug": "old-idea"}), ("/api/research/assign", {"slug": "old-idea"}),
                  ("/api/research/projects", {"name": "X"}), ("/api/research/projects/ai-search", {"name": "Y"}),
                  ("/api/research/projects/ai-search/notes", {"text": "n"}),
                  ("/api/research/projects/ai-search/delete", {}), ("/api/research/todos", {"text": "t"}),
                  ("/api/research/todos/1", {"done": True}), ("/api/research/todos/1/delete", {}),
                  ("/api/research/dispatch", {"question": "Anything?"}),
                  ("/api/research/chat", {"target": "project:ai-search", "message": "hi"}),
                  ("/api/research/jobs/20261004-100000-aaaa/stop", {})]
        snapshot = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        for path, body in writes:
            code, out = self.post(path, body)
            self.assertEqual(code, 409, path)
            self.assertEqual(out, {"error": research.READONLY, "readonly": True})
        self.assertEqual(self.get("/api/research/proposals")[0], 200)
        self.assertEqual(self.get("/api/research/todos")[0], 200)
        self.assertTrue(self.state()["research"]["readonly"])
        self.assertEqual({p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}, snapshot)
        self.assertEqual(self.backups(), [])

        # The watchdog rests while the old server runs, even for a run past its deadline.
        p = self.sleeper()
        self.add_job(id="late", pid=p.pid, started=iso(datetime.now()),
                     deadline=iso(datetime.now() - timedelta(minutes=1)))
        research.watchdog_pass()
        self.assertTrue(alive(p.pid))

    def test_watchdog_stops_a_late_run_once_writable(self):
        p = self.sleeper()
        self.add_job(id="late", pid=p.pid, started=iso(datetime.now()),
                     deadline=iso(datetime.now() - timedelta(minutes=1)))
        research.watchdog_pass()
        p.wait(timeout=5)
        job = next(j for j in self.file("logs/dashboard/jobs.json") if j["id"] == "late")
        self.assertTrue(job["stopped"] and job["timedOut"])

    # ---- adopting runs

    def test_adopts_a_live_run_by_start_time(self):
        p = self.sleeper()
        self.add_job(id="mine", pid=p.pid, started=iso(datetime.now()))
        self.add_job(id="reused", slug="other", pid=p.pid, started=iso(datetime.now() - timedelta(seconds=30)),
                     deadline=iso(datetime.now() - timedelta(minutes=1)))
        self.assertEqual(self.get("/api/research/jobs/mine")[1]["status"], "running")
        self.assertEqual(self.get("/api/research/jobs/reused")[1]["status"], "failed")
        self.assertEqual(self.state()["research"]["active"], 1)
        code, view = self.post("/api/research/jobs/reused/stop")
        self.assertEqual((code, view["status"]), (200, "stopped"))
        self.assertTrue(alive(p.pid))  # its pid fails the check: never signalled
        jobs = self.file("logs/dashboard/jobs.json")
        for j in jobs:
            if j["id"] == "reused":
                j.pop("stopped")
        (self.repo / "logs/dashboard/jobs.json").write_text(json.dumps(jobs))
        research.watchdog_pass()
        self.assertTrue(alive(p.pid))
        self.assertNotIn("timedOut", next(j for j in self.file("logs/dashboard/jobs.json") if j["id"] == "reused"))
        self.post("/api/research/jobs/mine/stop")
        p.wait(timeout=5)

    # ---- runs

    def test_dispatch_spawns_like_the_old_server(self):
        code, job = self.post("/api/research/dispatch", {"question": "Is DAW music worth it?", "tier": "T2"})
        self.assertEqual(code, 200, job)
        self.assertEqual((job["slug"], job["size"], job["budget"]), ("daw-music-worth", "large", 10))
        self.wait_done(job["id"])
        args = (SCRATCH / "args").read_text().split("\n")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "10")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--chrome", args)
        self.assertIn(f"Bash({self.repo.resolve()}/bin/todo:*)", args)
        self.assertNotIn("--resume", args)
        self.assertEqual((SCRATCH / "cwd").read_text().strip(), str(self.repo.resolve()))
        saved = next(j for j in self.file("logs/dashboard/jobs.json") if j["id"] == job["id"])
        self.assertTrue(saved["pid_start"])
        started = datetime.fromisoformat(saved["started"])
        self.assertEqual(datetime.fromisoformat(saved["deadline"]) - started, timedelta(minutes=40))
        self.assertEqual(self.get(f"/api/research/jobs/{job['id']}")[1]["status"], "done")

    def test_chat_resumes_its_session(self):
        code, out = self.post("/api/research/chat", {"target": "project:ai-search", "message": "And then?",
                                                     "thread": "20261005-090000-bbbb"})
        self.assertEqual(code, 200, out)
        self.wait_done(out["turn"]["id"])
        args = (SCRATCH / "args").read_text().split("\n")
        self.assertEqual(args[args.index("--resume") + 1], "chat-sid")
        self.assertEqual(args[args.index("-p") + 1], "And then?")
        thread = self.file("logs/dashboard/threads.json")["20261005-090000-bbbb"]
        self.assertEqual(thread["turns"], ["20261005-090000-cccc", out["turn"]["id"]])

    def test_shutdown_leaves_research_runs_alone(self):
        os.environ["FAKE_SLEEP"] = "30"
        code, job = self.post("/api/research/dispatch", {"question": "Long one?"})
        self.assertEqual(code, 200, job)
        self.mine.append(job["pid"])
        self.assertNotIn(research.procs[job["id"]], opsd.PROCS.values())
        try:
            opsd.shutdown()
            self.assertTrue(alive(job["pid"]))
            opsd.pause_all()
            self.assertTrue(alive(job["pid"]))
        finally:
            opsd.STOPPING.clear()
        proc = research.procs[job["id"]]
        self.post(f"/api/research/jobs/{job['id']}/stop")
        proc.wait(timeout=5)
        self.assertEqual(self.get(f"/api/research/jobs/{job['id']}")[1]["status"], "stopped")

    def test_count_is_separate_and_takes_no_slot(self):
        os.environ["FAKE_SLEEP"] = "30"
        code, job = self.post("/api/research/dispatch", {"question": "Counted?"})
        self.assertEqual(code, 200, job)
        self.mine.append(job["pid"])
        s = self.state()
        self.assertEqual(s["research"], {"active": 1, "readonly": False})
        self.assertEqual(s["active"], len(opsd.THREADS))

        # With max_parallel 1 and a research run going, a queued task still starts.
        repo = SCRATCH / "taskrepo"
        if not repo.exists():
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                            "-q", "--allow-empty", "-m", "init"], check=True)
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        (opsd.PROJECTS_DIR / "rdemo.toml").write_text(f'repo = "{repo}"\n')
        opsd.load_projects()
        opsd.set_setting("max_parallel", "1")
        started, pipeline, busy = [], opsd.pipeline, dict(opsd.THREADS)
        opsd.pipeline = lambda tid: started.append(tid)
        opsd.THREADS.clear()
        try:
            tid = opsd.add_task("rdemo", "do a thing")
            opsd.schedule_once()
            self.assertIn(tid, opsd.THREADS)
            opsd.THREADS[tid].join(5)
            self.assertEqual(started, [tid])
        finally:
            opsd.pipeline = pipeline
            opsd.THREADS.clear()
            opsd.THREADS.update(busy)
            opsd.upd(tid, state="cancelled")
            (opsd.PROJECTS_DIR / "rdemo.toml").unlink()
            opsd.load_projects()

    # ---- usage, brake, notifications

    def test_rate_limit_events_feed_the_meter_once(self):
        p = self.sleeper()
        self.add_job(id="metered", pid=p.pid, started=iso(datetime.now()))
        resets = time.time() + 3600
        rl = lambda u: {"type": "rate_limit_event", "rate_limit_info": {  # noqa: E731
            "unifiedWindows": {"five_hour": {"utilization": u, "resetsAt": resets}}}}
        self.log("metered", {"type": "system", "subtype": "init", "session_id": "s"}, rl(0.42))
        seen, real = [], opsd.usage_seen
        opsd.usage_seen = lambda w: (seen.append(w), real(w))
        try:
            research.watchdog_pass()
            self.assertEqual(json.loads(opsd.setting("usage:five_hour"))["utilization"], 0.42)
            research.watchdog_pass()
            self.assertEqual(len(seen), 1)  # nothing read twice
            self.log("metered", rl(0.5))
            research.watchdog_pass()
            self.assertEqual(json.loads(opsd.setting("usage:five_hour"))["utilization"], 0.5)
            self.assertEqual(len(seen), 2)
        finally:
            opsd.usage_seen = real

    def test_brake_holds_runs_not_chats(self):
        opsd.set_setting("usage_brake", "90")
        opsd.set_setting("usage:five_hour", json.dumps({"utilization": 0.95, "resetsAt": time.time() + 3600,
                                                        "ts": time.time()}))
        code, body = self.post("/api/research/dispatch", {"question": "Held?"})
        self.assertEqual(code, 409)
        self.assertTrue(body["error"].startswith("Usage at 95% of the 5-hour window"))
        self.assertFalse([j for j in self.file("logs/dashboard/jobs.json") if j["question"] == "Held?"])
        code, out = self.post("/api/research/chat", {"target": "proposal:old-idea", "message": "Still here?"})
        self.assertEqual(code, 200, out)
        self.wait_done(out["turn"]["id"])

    def test_a_finished_run_notifies_once(self):
        self.add_job(id="finished", pid=999997, started=iso(datetime.now()))
        self.log("finished", {"type": "result", "is_error": False, "result": "done"})
        self.add_job(id="chat-ok", kind="chat", slug="chat:project:ai-search", target="project:ai-search",
                     thread="t", pid=999996, started=iso(datetime.now()), finalized=True)
        self.log("chat-ok", {"type": "result", "is_error": False, "result": "fine"})
        self.add_job(id="chat-bad", kind="chat", slug="chat:project:ai-search", target="project:ai-search",
                     thread="t", pid=999995, started=iso(datetime.now()), finalized=True)
        self.log("chat-bad", {"type": "result", "is_error": True, "result": "broke"})
        research.watchdog_pass()
        self.assertEqual(sorted(t for t, _ in self.notes), ["Research chat failed", "Research run finished"])
        self.restart()
        research.watchdog_pass()
        self.assertEqual(len(self.notes), 2)
        saved = {j["id"]: j for j in self.file("logs/dashboard/jobs.json")}
        self.assertTrue(saved["finished"]["notified"] and saved["chat-bad"]["notified"])
        self.assertNotIn("notified", saved["20261004-100000-aaaa"])  # history doesn't replay


if __name__ == "__main__":
    unittest.main()
