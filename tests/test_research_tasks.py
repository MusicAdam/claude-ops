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
import time
import unittest
from pathlib import Path

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


class ResearchTodoBridge(unittest.TestCase):
    """RES-8: open to-dos in the research repo's todos.json are Do items in their claude-ops project."""

    def setUp(self):
        opsd.init_db()
        opsd.q("DELETE FROM tasks")
        os.environ["OPS_RESEARCH_OLD_PORT"] = str(free_port())  # nothing answers: research is writable
        self.repo = Path(tempfile.mkdtemp()) / "research"
        shutil.copytree(FIXTURE, self.repo)
        opsd.set_setting("research_repo", str(self.repo.resolve()))
        self.saved = dict(opsd.PROJECTS)
        opsd.PROJECTS.clear()
        opsd.PROJECTS["web"] = FakeProject("web", Path(tempfile.mkdtemp()))

    def tearDown(self):
        opsd.set_setting("research_repo", "")
        research.current()
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)
        shutil.rmtree(self.repo.parent, ignore_errors=True)

    def link(self, repo):
        f = self.repo / "dashboard" / "projects.json"
        data = json.loads(f.read_text())
        data["projects"]["ai-search"]["repo"] = repo
        f.write_text(json.dumps(data))

    def test_unlinked_and_no_home_is_hidden(self):
        self.assertEqual(opsd.research_todo_items(), [])

    def test_linked_project(self):
        self.link("web")
        got = opsd.research_todo_items()
        self.assertEqual(len(got), 1)
        it = got[0]
        self.assertEqual((it["key"], it["verb"], it["group"], it["project"]), ("rtodo:1", "Do", "only_me", "web"))
        self.assertEqual((it["title"], it["rtodo"], it["source"]), ("Email the vendor", 1,
                                                                     {"kind": "research_todo", "id": 1}))
        self.assertIn("ai-search", it["why"])
        self.assertGreater(it["since"], 0)
        self.assertIn("rtodo:1", [x["key"] for x in opsd.todos_list([], [], {})])

    def test_unlinked_goes_to_the_research_project(self):
        opsd.PROJECTS["research"] = FakeProject("research", self.repo)
        self.assertEqual([x["project"] for x in opsd.research_todo_items()], ["research"])
        self.link("gone")  # a stale link falls back too
        self.assertEqual([x["project"] for x in opsd.research_todo_items()], ["research"])

    def test_live_task_and_done_are_left_out(self):
        self.link("web")
        tid = opsd.add_task("web", "body", "t")
        f = self.repo / "dashboard" / "todos.json"
        data = json.loads(f.read_text())
        data["todos"][0]["task"] = tid
        f.write_text(json.dumps(data))
        self.assertEqual(opsd.research_todo_items(), [])
        opsd.upd(tid, state="cancelled")
        self.assertEqual(len(opsd.research_todo_items()), 1)
        code, _ = research.post("/api/research/todos/1", {"done": True})  # the Done button
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(f.read_text())["todos"][0]["done"])
        self.assertEqual(opsd.research_todo_items(), [])

    def test_research_off(self):
        opsd.set_setting("research_repo", "")
        self.assertEqual(opsd.research_todo_items(), [])


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
