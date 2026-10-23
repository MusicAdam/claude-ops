"""The to-do list in /api/state (README "To-dos"). Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class FakeProject:
    def __init__(self, name, launch=""):
        self.name, self.launch = name, launch


def project_facts(name, **kw):
    """A main_info dict with nothing that needs you, plus kw."""
    return {"name": name, "title": name.title(), "main_branch": "main", "dev_branch": "dev", "ahead": [],
            "auto_release": False, "release": None, "deploys": [], "deploy_open": None, "uncommitted": [],
            "dev_log": [], "main_log": [], "main_since_kind": None, **kw}


class Todos(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("tasks", "events", "designs", "design_msgs", "todos"):
            opsd.q(f"DELETE FROM {t}")
        self.saved = dict(opsd.PROJECTS)
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update({"game": FakeProject("game", launch="open Game.app"), "web": FakeProject("web")})

    def tearDown(self):
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)

    def task(self, project, state, kind="task", **kw):
        tid = opsd.add_task(project, "body", f"{project} {state} {kind}", kind=kind,
                            **({"deploy": {"target": "x"}} if kind == "deploy" else {}))
        opsd.upd(tid, state=state, **kw)
        return tid

    def todos(self, projects=()):
        tasks = opsd.q("SELECT * FROM tasks ORDER BY id DESC")
        last = {r["task_id"]: r for r in opsd.q("SELECT task_id, MAX(ts) AS ts FROM events GROUP BY task_id")}
        return opsd.todos_list(tasks, list(projects), last)

    def test_task_states(self):
        ask = self.task("web", "needs_input", question="Which colour?")
        bad = self.task("web", "failed", error="gate failed")
        play = self.task("game", "review")
        read = self.task("web", "review")
        rel = self.task("web", "needs_input", kind="release", phase=opsd.WAIT, question="quit the app")
        self.task("web", "running")
        self.task("web", "done")
        got = {x["key"]: x for x in self.todos()}
        self.assertEqual(set(got), {f"task:{ask}:answer", f"task:{bad}:fix", f"task:{play}:try",
                                    f"task:{read}:review", f"task:{rel}:promote"})
        self.assertEqual(got[f"task:{ask}:answer"]["why"], "Which colour?")
        self.assertEqual(got[f"task:{ask}:answer"]["group"], "unblock")
        self.assertEqual(got[f"task:{bad}:fix"]["why"], "gate failed")
        self.assertEqual(got[f"task:{play}:try"]["verb"], "Try")  # its project has a launch step
        self.assertEqual(got[f"task:{read}:review"]["verb"], "Review")
        self.assertEqual(got[f"task:{read}:review"]["group"], "review")
        self.assertEqual(got[f"task:{rel}:promote"]["group"], "ship")
        self.assertEqual(got[f"task:{play}:try"]["est_min"], 5)
        self.assertEqual(got[f"task:{play}:try"]["source"], {"kind": "task", "id": play})

    def test_projects(self):
        now = time.time()
        fs = [project_facts("web", ahead=["abc one"], dev_log=[{"at": now - 50}]),
              project_facts("game", ahead=["abc one"], auto_release=True,  # promotes by itself: not yours
                            main_since_kind="deploy", main_log=[{"at": now - 90}, {"at": now - 10}],
                            uncommitted=[{"repo": "~/dev/game", "count": 3, "paths": [], "oldest": now - 500}]),
              project_facts("busy", ahead=["x"], release={"id": 1, "state": "running"},  # under way already
                            main_since_kind="deploy", main_log=[{"at": now}], deploy_open=9),
              {"name": "broken", "title": "Broken", "error": "git said no"}]
        got = {x["key"]: x for x in self.todos(fs)}
        self.assertEqual(set(got), {"project:web:promote", "project:game:ship", "project:game:commit"})
        self.assertEqual(got["project:game:ship"]["since"], now - 90)
        self.assertEqual(got["project:game:commit"]["why"], "3 uncommitted files in ~/dev/game")
        self.assertEqual(got["project:game:commit"]["verb"], "Commit")

    def test_unread_chat(self):
        old = time.time() - 100
        did = opsd.q("INSERT INTO designs (project, title, state, created, updated, seen) VALUES "
                     "('web','Plan it','idle',?,?,?)", (old, old, old))
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "assistant", "hi", old + 50))
        self.assertEqual([x["key"] for x in self.todos()], [f"chat:{did}:read"])
        opsd.q("UPDATE designs SET seen=? WHERE id=?", (time.time(), did))  # you opened it
        self.assertEqual(self.todos(), [])

    def test_order(self):
        now = time.time()
        self.task("web", "review")
        a = self.task("web", "needs_input", question="?")
        b = self.task("web", "failed", error="x")
        opsd.ev(a, "later")  # a's state began after b's
        opsd.q("UPDATE events SET ts=? WHERE task_id=?", (now + 10, a))
        opsd.ev(b, "earlier")
        opsd.q("UPDATE events SET ts=? WHERE task_id=?", (now - 10, b))
        opsd.todo_add("web", [{"verb": "Do", "title": "Grant Full Disk Access", "why": "TCC"}], "task", a)
        opsd.todo_add("web", [{"verb": "Agree", "title": "Use SQLite", "why": "simpler"}], "task", a)
        got = self.todos([project_facts("web", uncommitted=[{"repo": "r", "count": 1, "paths": [], "oldest": 1}])])
        self.assertEqual([x["verb"] for x in got], ["Fix", "Answer", "Commit", "Review", "Agree", "Do"])
        self.assertEqual([x["group"] for x in got], ["unblock", "unblock", "ship", "review", "review", "only_me"])

    def test_todo_block_in_reply(self):
        p = FakeProject("web")
        block = '```todo\n[{"verb": "Do", "title": "Add the API key", "why": "only you have it"}]\n```'
        text, note = opsd.todos_from_reply(p, f"Here is the plan.\n\n{block}", 7)
        self.assertEqual(text, "Here is the plan.")
        self.assertIn("added to-do #", note)
        opsd.todos_from_reply(p, f"Again.\n\n{block}", 7)  # same title while open: not added twice
        rows = opsd.q("SELECT * FROM todos")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["verb"], rows[0]["source_kind"], rows[0]["source_id"]), ("Do", "design", 7))
        item = [x for x in self.todos() if x["key"] == f"todo:{rows[0]['id']}"][0]
        self.assertEqual((item["group"], item["source"], item["todo"]), ("only_me", {"kind": "chat", "id": 7},
                                                                         rows[0]["id"]))
        _, note = opsd.todos_from_reply(p, '```todo\n[{"verb": "Fix", "title": "x"}]\n```', 7)
        self.assertIn("skipped", note)  # only Do and Agree are Claude's to add

    def test_close_from_block_and_endpoint(self):
        p = FakeProject("web")
        opsd.todos_from_reply(p, '```todo\n[{"verb": "Agree", "title": "A"}, {"verb": "Do", "title": "B"}]\n```', 1)
        a, b = [r["id"] for r in opsd.q("SELECT id FROM todos ORDER BY id")]
        text, note = opsd.todos_from_reply(p, f'Done.\n```todo\n[{{"close": {a}}}]\n```', 1)
        self.assertEqual(text, "Done.")
        self.assertIn(f"closed to-do #{a}", note)
        self.assertTrue(opsd.todo_done(b))  # what POST /api/todos/<id>/done calls
        self.assertFalse(opsd.todo_done(b))  # already closed
        self.assertEqual(self.todos(), [])
        self.assertEqual(len(opsd.q("SELECT id FROM todos WHERE closed IS NOT NULL")), 2)  # closed items stay

    def test_worker_result_todos(self):
        tid = self.task("web", "running")
        note = opsd.todo_add("web", json.loads('[{"verb": "Do", "title": "Run sudo make install", "why": "root"}]'),
                             "task", tid)
        self.assertIn("added", note)
        r = opsd.q("SELECT * FROM todos", one=True)
        self.assertEqual((r["source_kind"], r["source_id"], r["title"]), ("task", tid, "Run sudo make install"))


if __name__ == "__main__":
    unittest.main()
