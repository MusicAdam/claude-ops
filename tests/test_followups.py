"""A worker's follow-up tasks (README "To-dos", TODO-5). Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class FakeProject:
    def __init__(self, name):
        self.name, self.launch, self.dev, self.parts = name, "", "dev", []


class Followups(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("tasks", "events", "todos", "settings"):
            opsd.q(f"DELETE FROM {t}")
        self.saved = dict(opsd.PROJECTS)
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update({"web": FakeProject("web"), "game": FakeProject("game")})
        self.ended = []

    def tearDown(self):
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)

    def run_worker(self, res, **task_kw):
        """Run the pipeline for a new task in web whose worker writes res; the merge stops at "no code changes"."""
        tid = opsd.add_task("web", "parent body", "Parent", **task_kw)
        opsd.upd(tid, state="running", worktree=DATA)
        end = lambda how: (lambda p, t, *a, **k: self.ended.append(how))  # noqa: E731
        with mock.patch.object(opsd, "ensure_worktree"), \
                mock.patch.object(opsd, "worker_prompt", return_value="brief"), \
                mock.patch.object(opsd, "run_claude", return_value=res), \
                mock.patch.object(opsd, "changed_parts", return_value=False), \
                mock.patch.object(opsd, "finish", end("finish")), \
                mock.patch.object(opsd, "fail", end("fail")), \
                mock.patch.object(opsd, "ask_owner", end("ask")):
            opsd._pipeline(opsd.proj("web"), tid)
        return tid

    def children(self, tid):
        return opsd.q("SELECT * FROM tasks WHERE parent=? ORDER BY id", (tid,))

    def events(self, tid):
        return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=?", (tid,))]

    def test_done_queues_followups_behind_the_parent(self):
        tid = self.run_worker({"status": "done", "summary": "built it", "followups": [
            {"title": "Add the project", "body": "Run opsd.py add-project ..."},
            {"title": "Schedule it", "body": "Run opsd.py schedule add ..."}]},
            priority=5, hold=1, model="opus", effort="high", auto_release=True)
        kids = self.children(tid)
        self.assertEqual([k["title"] for k in kids], ["Add the project", "Schedule it"])
        for k in kids:
            self.assertEqual(k["project"], "web")
            self.assertEqual(k["depends_on"], tid)
            self.assertEqual(k["state"], "queued")
            self.assertEqual((k["priority"], k["hold"], k["model"], k["effort"], k["auto_release"]),
                             (0, 0, None, None, 0))  # queue defaults, not the parent's
        self.assertEqual(kids[0]["body"], "Run opsd.py add-project ...")
        self.assertIn(f"follow-ups: #{kids[0]['id']}, #{kids[1]['id']}", self.events(tid))
        self.assertEqual(self.ended, ["finish"])

    def test_release_takes_the_project_default(self):
        opsd.set_setting("auto_release:web", "1")
        tid = self.run_worker({"status": "done", "summary": "x", "followups": [{"title": "T", "body": "B"}]})
        self.assertEqual(self.children(tid)[0]["auto_release"], 1)

    def test_listed_again_is_not_queued_twice(self):
        tid = opsd.add_task("web", "b", "P")
        first = opsd.add_followups(opsd.proj("web"), tid, [{"title": "T", "body": "B"}])
        again = opsd.add_followups(opsd.proj("web"), tid, [{"title": "T", "body": "B"}, {"title": "U", "body": "C"},
                                                          "junk", {}])
        self.assertEqual(len(first), 1)
        self.assertEqual([opsd.task(i)["title"] for i in again], ["U"])

    def test_ignored_on_other_statuses(self):
        for status, how in (("needs_input", "ask"), ("failed", "fail")):
            self.ended.clear()
            tid = self.run_worker({"status": status, "summary": "s", "question": "q",
                                   "followups": [{"title": "T", "body": "B"}]})
            self.assertEqual(self.children(tid), [])
            self.assertFalse(any(e.startswith("follow-ups") for e in self.events(tid)))
            self.assertEqual(self.ended, [how])

    def test_todos_still_work(self):
        for status in ("done", "failed"):
            tid = self.run_worker({"status": status, "summary": "s", "todos": [
                {"verb": "Do", "title": f"Grant Full Disk Access {status}", "why": "TCC"}]})
            row = opsd.q("SELECT * FROM todos WHERE title=?", (f"Grant Full Disk Access {status}",), one=True)
            self.assertIsNotNone(row)
            self.assertEqual(self.children(tid), [])


if __name__ == "__main__":
    unittest.main()
