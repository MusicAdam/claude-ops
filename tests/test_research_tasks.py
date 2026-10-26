"""Research as queue work (README ## Research, RES-1..8): the report listing, the research task's brief, the Read
to-do when one merges, recurring research jobs, and the bridge from the research repo's to-dos. Stdlib only, no
Claude calls. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if "opsd" not in sys.modules:
    os.environ["OPS_DATA"] = tempfile.mkdtemp()
    os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402
import research  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "research"


def git(cwd, *args, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", **(env or {})})


def commit(repo, rel, text, when):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, "add", rel)
    stamp = f"@{int(when)} +0000"
    git(repo, "commit", "-qm", f"add {rel}", env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


class FakeProject:
    """Enough of a Project for these paths: its dev worktree is a plain git repo."""

    def __init__(self, name, root, repos=()):
        self.name, self.title, self.dev, self.launch = name, name.title(), "dev", ""
        self.repo = self.dev_wt = root
        self.repos = list(repos)
        self.parts = [opsd.Part(self, r) for r in self.repos] or [opsd.Part(self, "")]


REPORT = """---
tags: [x]
---
# Vector databases for small apps

- **Date:** 2026-10-20
- **Tier:** T1

SQLite with an [extension](https://example.com) is *enough* below a million rows.
It keeps one file.

## Findings

More text.
"""


class ResearchTasks(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("tasks", "events", "designs", "design_msgs", "todos", "schedules"):
            opsd.q(f"DELETE FROM {t}")
        opsd.REPORT_DATES.clear()
        self.root = Path(tempfile.mkdtemp())
        git(self.root, "init", "-q")
        commit(self.root, "README.md", "hi\n", 1_700_000_000)
        self.saved = dict(opsd.PROJECTS)
        opsd.PROJECTS.clear()
        opsd.PROJECTS["web"] = FakeProject("web", self.root)

    def tearDown(self):
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)
        shutil.rmtree(self.root, ignore_errors=True)

    def todos(self):
        tasks = opsd.q("SELECT * FROM tasks ORDER BY id DESC")
        for t in tasks:
            t["research"] = opsd.research_of(t)  # as state_payload does
        return opsd.todos_list(tasks, [], {})

    # ---- the report listing (RES-5, TODAY-4)

    def test_report_head(self):
        title, summary = opsd.report_head(REPORT)
        self.assertEqual(title, "Vector databases for small apps")
        self.assertEqual(summary, "SQLite with an extension is enough below a million rows. It keeps one file.")
        self.assertEqual(opsd.report_head("no heading\n\nsecond"), ("", "no heading"))
        long = opsd.report_head("# T\n\n" + "word " * 200)[1]
        self.assertLessEqual(len(long), 241)

    def test_reports_listed_newest_first(self):
        commit(self.root, "docs/research/old.md", "# Old one\n\nThe old answer.\n", 1_700_000_100)
        commit(self.root, "docs/research/vector-db.md", REPORT, 1_700_000_200)
        commit(self.root, "docs/research/notes.txt", "not a report", 1_700_000_300)
        got = opsd.research_reports(opsd.PROJECTS["web"])
        self.assertEqual([r["path"] for r in got], ["docs/research/vector-db.md", "docs/research/old.md"])
        self.assertEqual(got[0]["title"], "Vector databases for small apps")
        self.assertEqual(got[0]["date"], 1_700_000_200)
        self.assertEqual(got[1]["summary"], "The old answer.")
        self.assertEqual((got[1]["task"], got[1]["unread"]), (None, False))

    def test_reports_in_a_workspace(self):
        opsd.PROJECTS["web"] = FakeProject("web", self.root, repos=["app"])
        sub = self.root / "app"
        sub.mkdir()
        git(sub, "init", "-q")
        commit(sub, "docs/research/a.md", "# A\n\nAnswer.\n", 1_700_000_100)
        self.assertEqual([r["path"] for r in opsd.research_reports(opsd.PROJECTS["web"])], ["app/docs/research/a.md"])
        self.assertEqual(opsd.report_rel(opsd.PROJECTS["web"], "x"), "app/docs/research/x.md")

    def test_project_research_endpoint_shape(self):
        commit(self.root, "docs/research/old.md", "# Old one\n\nThe old answer.\n", 1_700_000_100)
        run = opsd.queue_research("web", "Is Rust worth it?", "T0")
        got = opsd.project_research("web")
        self.assertEqual(got["dev"], "dev")
        self.assertEqual([r["path"] for r in got["reports"]], ["docs/research/old.md"])
        self.assertEqual([t["id"] for t in got["running"]], [run["id"]])
        self.assertEqual(got["running"][0]["research"]["tier"], "T0")
        self.assertEqual(got["schedules"], [])

    # ---- the research task and its brief (RES-1..4)

    def test_queue_research_brief(self):
        r = opsd.queue_research("web", "What is the best vector database for a small app?", "T2")
        self.assertEqual(r["slug"], "best-vector-database-small-app")
        self.assertEqual(r["path"], "docs/research/best-vector-database-small-app.md")
        t = opsd.task(r["id"])
        self.assertEqual(t["kind"], "task")  # a normal task, flagged
        self.assertTrue(t["title"].startswith("Research: What is the best vector database"))
        self.assertEqual(opsd.research_of(t), {"question": "What is the best vector database for a small app?",
                                               "tier": "T2", "slug": r["slug"], "path": r["path"]})
        body = t["body"]
        self.assertIn("What is the best vector database for a small app?", body)
        self.assertIn(f"Write the report to `{r['path']}` in this worktree", body)
        self.assertIn("do not write to ~/dev/research", body)
        self.assertIn("Commit only that file", body)
        self.assertIn("Touch nothing else", body)
        self.assertIn("## Sources", body)
        self.assertIn("stopped after 40 minutes", body)
        self.assertIn("tier T2", body)

    def test_tiers_and_refusals(self):
        self.assertIn("stopped after 5 minutes", opsd.task(opsd.queue_research("web", "q one", "t0")["id"])["body"])
        self.assertEqual(opsd.research_of(opsd.task(opsd.queue_research("web", "q two", None)["id"]))["tier"], "T1")
        with self.assertRaises(ValueError):
            opsd.queue_research("web", "q", "T3")
        with self.assertRaises(ValueError):
            opsd.queue_research("web", "   ", "T1")
        with self.assertRaises(ValueError):
            opsd.queue_research("nope", "q", "T1")

    def test_slug_is_unique(self):
        commit(self.root, "docs/research/rust-worth.md", "# R\n\nx\n", 1_700_000_100)
        a = opsd.queue_research("web", "Is Rust worth it?")
        b = opsd.queue_research("web", "Is Rust worth it?")
        self.assertEqual((a["slug"], b["slug"]), ("rust-worth-2", "rust-worth-3"))
        self.assertEqual(opsd.queue_research("web", "?!")["slug"], "research")

    # ---- the Read to-do (RES-5)

    def test_read_todo_when_merged(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        self.assertEqual([x["key"] for x in self.todos()], [])
        opsd.upd(r["id"], state="done", merged=None, finished=time.time())  # done with no change: nothing to read
        self.assertEqual([x["key"] for x in self.todos()], [])
        opsd.upd(r["id"], merged="abc123")
        got = self.todos()
        self.assertEqual([x["key"] for x in got], [f"task:{r['id']}:report"])
        self.assertEqual((got[0]["verb"], got[0]["group"], got[0]["project"]), ("Read", "review", "web"))
        self.assertEqual(got[0]["report"], {"path": r["path"]})
        self.assertEqual(got[0]["source"], {"kind": "task", "id": r["id"]})
        plain = opsd.add_task("web", "body", "plain")
        opsd.upd(plain, state="done", merged="def456")
        self.assertEqual(len(self.todos()), 1)  # an ordinary merged task is no Read
        self.assertEqual(opsd.report_read("web", r["path"]), [r["id"]])
        self.assertEqual(self.todos(), [])
        self.assertEqual(opsd.report_read("web", r["path"]), [])  # already read

    def test_serving_the_report_leaves_it_unread(self):
        """Looking at a report doesn't close its Read step: only Mark read does (RES-5, TODO-1)."""
        r = opsd.queue_research("web", "Is Rust worth it?")
        commit(self.root, r["path"], "# Rust\n\nYes.\n", 1_700_000_100)
        p = opsd.PROJECTS["web"]
        opsd.upd(r["id"], state="done", merged="abc123")
        f, why = opsd.serve_repo_file(p, r["path"])
        self.assertTrue(f and not why)
        self.assertEqual(opsd.serve_repo_file(p, "./" + r["path"])[1], None)
        self.assertIsNone(opsd.research_of(opsd.task(r["id"])).get("read"))
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{r['id']}:report"])

    def test_mark_read_route(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        opsd.upd(r["id"], state="done", merged="abc123")
        h = opsd.H.__new__(opsd.H)
        self.assertEqual(h.route("/api/projects/web/research/read", {"path": r["path"]}),
                         {"ok": True, "path": r["path"], "marked": [r["id"]], "already": []})
        self.assertEqual(self.todos(), [])

    def serve(self):
        """A dashboard server on a free port, stopped after the test."""
        srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}"

    def call(self, url, body=None):
        req = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "X-Ops": "1"})
        try:
            with urlopen(req) as r:
                return r.status, r.read()
        except HTTPError as e:
            with e:
                return e.code, e.read()

    def test_opening_a_merged_report_leaves_it_on_the_list(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        commit(self.root, r["path"], "# Rust\n\nYes.\n", 1_700_000_100)
        opsd.upd(r["id"], state="done", merged="abc123")
        opsd.READY.set()  # projects are loaded: the API answers
        base = self.serve()
        self.assertEqual(self.call(base + "/api/files?project=web&path=" + quote(r["path"])), (200, b"# Rust\n\nYes.\n"))
        self.assertIsNone(opsd.research_of(opsd.task(r["id"])).get("read"))
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{r['id']}:report"])
        code, body = self.call(base + "/api/projects/web/research/read", {"path": r["path"]})  # Mark read
        self.assertEqual((code, json.loads(body)["marked"]), (200, [r["id"]]))
        self.assertEqual(self.todos(), [])

    def test_mark_read_says_what_it_found(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        opsd.upd(r["id"], state="done", merged="abc123")
        opsd.READY.set()  # projects are loaded: the API answers
        url = self.serve() + "/api/projects/web/research/read"
        code, body = self.call(url, {"path": "docs/research/nothing.md"})
        self.assertEqual((code, json.loads(body)["marked"], json.loads(body)["already"]), (200, [], []))
        code, body = self.call(url, {"path": f"  {r['path']}\n"})  # whitespace around it still matches
        self.assertEqual((code, json.loads(body)["marked"]), (200, [r["id"]]))
        self.assertEqual(self.todos(), [])
        body = json.loads(self.call(url, {"path": r["path"]})[1])  # a second mark: read before, not missing
        self.assertEqual((body["marked"], body["already"]), ([], [r["id"]]))

    def test_a_state_build_from_before_an_action_is_not_kept(self):
        started, go = threading.Event(), threading.Event()
        real = opsd.state_payload

        def slow():
            started.set()
            go.wait(5)
            return {"old": True}
        opsd.STATE_CACHE.update(data=b"{}", at=0.0, busy=False)
        opsd.state_payload = slow
        self.addCleanup(setattr, opsd, "state_payload", real)
        self.addCleanup(opsd.STATE_CACHE.update, data=None, at=0.0, busy=False)
        self.assertEqual(opsd.state_bytes(), b"{}")  # stale at once; the rebuild runs behind it
        started.wait(5)
        opsd.state_changed()  # e.g. POST /research/read lands while that rebuild still reads the old state
        go.set()
        for _ in range(50):
            if not opsd.STATE_CACHE["busy"]:
                break
            time.sleep(0.02)
        self.assertIsNone(opsd.STATE_CACHE["data"])  # so the next poll builds afresh instead of showing the old list

    # ---- reports any task lists (RES-10)
    def events(self, tid):
        return [e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def test_listed_reports_are_read_todos(self):
        tid = opsd.add_task("web", "body", "Nightly bug bash")
        commit(self.root, "bugbash/a.md", "# A\n", 1_700_000_100)
        commit(self.root, "bugbash/b.md", "# B\n", 1_700_000_200)
        self.assertEqual(opsd.set_reports(tid, ["bugbash/a.md", "./bugbash/b.md"]), "reports: bugbash/a.md, bugbash/b.md")
        self.assertEqual(self.todos(), [])  # not done yet
        opsd.upd(tid, state="done", merged=None)
        self.assertEqual(self.todos(), [])  # done, not merged
        opsd.upd(tid, state="failed", merged="abc123", error="x")
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{tid}:fix"])  # failed: a Fix, no Read
        opsd.upd(tid, state="done")
        got = self.todos()
        self.assertEqual([x["key"] for x in got], [f"task:{tid}:report:bugbash/a.md", f"task:{tid}:report:bugbash/b.md"])
        self.assertEqual((got[0]["verb"], got[0]["title"], got[0]["why"], got[0]["report"]),
                         ("Read", "Nightly bug bash", "Report bugbash/a.md merged into dev", {"path": "bugbash/a.md"}))
        url = self.serve() + "/api/files?project=web&path="
        self.assertEqual(self.call(url + quote("bugbash/a.md")), (200, b"# A\n"))  # opening one reads none
        self.assertEqual(len(self.todos()), 2)
        h = opsd.H.__new__(opsd.H)
        h.route("/api/projects/web/research/read", {"path": "bugbash/a.md"})  # Mark read takes only that one
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{tid}:report:bugbash/b.md"])
        self.assertEqual(h.route("/api/projects/web/research/read", {"path": "bugbash/b.md"}),
                         {"ok": True, "path": "bugbash/b.md", "marked": [tid], "already": []})
        self.assertEqual(self.todos(), [])
        self.assertEqual(h.route("/api/projects/web/research/read", {"path": "bugbash/b.md"})["already"], [tid])
        self.assertEqual([x["read"] is not None for x in opsd.reports_of(opsd.task(tid))], [True, True])

    def test_invalid_reports_are_dropped(self):
        tid = opsd.add_task("web", "body", "Scout")
        said = opsd.set_reports(tid, ["/etc/passwd", "../x.md", "a/../../y.md", 7, "", "  ", None,
                                      "r1.md", "r2.md", "r3.md", "r4.md", "r5.md", "r6.md"])
        self.assertEqual([x["path"] for x in opsd.reports_of(opsd.task(tid))], ["r1.md", "r2.md", "r3.md", "r4.md", "r5.md"])
        self.assertTrue(said.startswith("reports: r1.md"))
        for bad in ("'/etc/passwd'", "'../x.md'", "'a/../../y.md'", "7", "None", "r6.md (more than 5)"):
            self.assertIn(bad, said)
        self.assertEqual(opsd.set_reports(tid, ["r2.md"]), "reports: r2.md")  # a later done replaces the list
        self.assertEqual([x["path"] for x in opsd.reports_of(opsd.task(tid))], ["r2.md"])
        self.assertEqual(opsd.set_reports(tid, "nope/../.."), "dropped reports: 'nope/../..'")
        self.assertIsNone(opsd.task(tid)["reports"])

    def test_research_task_listing_its_own_report_shows_once(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        opsd.set_reports(r["id"], [r["path"], "notes/extra.md"])
        opsd.upd(r["id"], state="done", merged="abc123")
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{r['id']}:report", f"task:{r['id']}:report:notes/extra.md"])
        self.assertEqual(opsd.report_read("web", r["path"]), [r["id"]])
        self.assertEqual([x["key"] for x in self.todos()], [f"task:{r['id']}:report:notes/extra.md"])

    def test_state_shows_reports(self):
        tid = opsd.add_task("web", "body", "Scout")
        opsd.set_reports(tid, ["a.md"])
        plain = opsd.add_task("web", "body", "plain")
        opsd.PROJECTS.clear()  # no projects: state_payload reads the tasks alone
        got = {t["id"]: t for t in opsd.state_payload()["tasks"]}
        self.assertEqual(got[tid]["reports"], [{"path": "a.md", "read": None}])
        self.assertIsNone(got[plain]["reports"])

    def test_unread_in_listing(self):
        r = opsd.queue_research("web", "Is Rust worth it?")
        commit(self.root, r["path"], "# Rust\n\nYes.\n", 1_700_000_100)
        opsd.upd(r["id"], state="done", merged="abc123")
        got = opsd.research_reports(opsd.PROJECTS["web"])
        self.assertEqual((got[0]["task"], got[0]["unread"]), (r["id"], True))
        opsd.report_read("web", r["path"])
        self.assertFalse(opsd.research_reports(opsd.PROJECTS["web"])[0]["unread"])

    # ---- recurring research (RES-2, RES-6)

    def test_new_research_with_repeat(self):
        out = opsd.new_research("web", {"question": "Daily scout: new leads", "tier": "T1", "every": "day 07:00"})
        s = opsd.schedule(out["schedule"])
        self.assertEqual((s["research"], s["body"], s["last_task"]), ("T1", "Daily scout: new leads", out["id"]))
        self.assertEqual(opsd.run_schedules(s["next_at"] + 1), [])  # its first run is still open: no pile-up
        opsd.upd(out["id"], state="done")
        s = opsd.schedule(out["schedule"])  # the skipped slot moved next_at on
        tid = opsd.run_schedules(s["next_at"] + 1)[0]
        rs = opsd.research_of(opsd.task(tid))
        self.assertEqual(rs["slug"], "daily-scout-new-leads-" + time.strftime("%Y-%m-%d"))
        self.assertEqual(json.loads(opsd.task(tid)["origin"])["ref"], f"schedule:{s['id']}")
        with self.assertRaises(ValueError):
            opsd.new_research("web", {"question": "q", "every": "sometimes"})
        self.assertEqual(len(opsd.q("SELECT id FROM tasks")), 2)  # the bad every queued nothing

    def test_schedule_api_takes_research(self):
        sid = opsd.add_schedule({"project": "web", "body": "Scout", "every": "hour", "research": "t2"})
        self.assertEqual(opsd.schedule(sid)["research"], "T2")
        opsd.edit_schedule(sid, {"title": "Scout hourly"})
        self.assertEqual(opsd.schedule(sid)["research"], "T2")
        with self.assertRaises(ValueError):
            opsd.add_schedule({"project": "web", "body": "x", "every": "hour", "research": "T9"})

    # ---- design documents (TODAY-4)

    def test_project_documents(self):
        d = opsd.q("INSERT INTO designs (project, title, created, updated, kind) VALUES ('web','Plan',0,0,'design')")
        chat = opsd.q("INSERT INTO designs (project, title, created, updated, kind) VALUES ('web','Chat',0,0,'project')")
        for did, name, when in ((d, "design.md", 100), (d, "proto.html", 200), (chat, "x.md", 300)):
            f = opsd.CHAT_FILES / str(did) / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
            os.utime(f, (when, when))
        got = opsd.project_documents("web")
        self.assertEqual([(x["design"], x["name"], x["title"]) for x in got], [(d, "proto.html", "Plan"),
                                                                               (d, "design.md", "Plan")])
        self.assertEqual(got[0]["url"], f"/api/designs/{d}/files/proto.html")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ResearchTodoListRetired(unittest.TestCase):
    """RES-8 (retired): the research repo's own to-do list is gone. Research turns on without dashboard/todos.py or
    todos.json, and the Today list has no items from them."""

    def setUp(self):
        opsd.init_db()
        os.environ["OPS_RESEARCH_OLD_PORT"] = str(free_port())  # nothing answers: research is writable
        self.repo = Path(tempfile.mkdtemp()) / "research"
        shutil.copytree(FIXTURE, self.repo)

    def tearDown(self):
        opsd.set_setting("research_repo", "")
        research.current()
        shutil.rmtree(self.repo.parent, ignore_errors=True)

    def test_a_repo_without_todos_py_turns_research_on(self):
        for f in ("todos.py", "todos.json"):
            (self.repo / "dashboard" / f).unlink(missing_ok=True)
        self.assertEqual(research.check_repo(str(self.repo)), str(self.repo.resolve()))
        opsd.set_setting("research_repo", str(self.repo.resolve()))
        self.assertEqual(research.current(), self.repo.resolve())
        self.assertEqual(research.get("/api/research/proposals", "")[0], 200)
        self.assertFalse(hasattr(research, "todos"))
        self.assertFalse([x for x in opsd.todos_list([], [], {}) if x["source"]["kind"] == "research_todo"])

    def test_its_routes_are_gone(self):
        opsd.set_setting("research_repo", str(self.repo.resolve()))
        self.assertEqual(research.get("/api/research/todos", "")[0], 404)
        self.assertEqual(research.post("/api/research/todos", {"text": "t"})[0], 404)

    def test_a_repo_without_projects_json_or_briefs_is_refused(self):
        (self.repo / "dashboard" / "projects.json").unlink()
        with self.assertRaises(ValueError):
            research.check_repo(str(self.repo))
        shutil.copy(FIXTURE / "dashboard" / "projects.json", self.repo / "dashboard" / "projects.json")
        shutil.rmtree(self.repo / "briefs")
        with self.assertRaises(ValueError):
            research.check_repo(str(self.repo))


class AddCli(unittest.TestCase):
    """opsd.py add -p NAME [--research T0|T1|T2]: what it posts to the daemon (a plan & design chat starts research
    this way)."""

    def test_research_flag_posts_a_research_run(self):
        self.assertEqual(opsd.add_request("my app", ["--research", "T2", "vector", "dbs?"]),
                         ("/api/projects/my%20app/research", {"question": "vector dbs?", "tier": "T2"}))

    def test_research_flag_mixes_with_model_and_effort(self):
        path, body = opsd.add_request("demo", ["--model", "sonnet", "--research", "T0", "--effort", "low", "q"])
        self.assertEqual((path, body), ("/api/projects/demo/research",
                                        {"question": "q", "tier": "T0", "model": "sonnet", "effort": "low"}))

    def test_without_flag_a_task(self):
        self.assertEqual(opsd.add_request("demo", ["--effort", "high", "fix", "it"]),
                         ("/api/tasks", {"project": "demo", "body": "fix it", "effort": "high"}))


if __name__ == "__main__":
    unittest.main()
