"""The to-do list in /api/state (README "To-dos"). Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import shutil
import subprocess
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
        self.assertEqual(got["project:game:commit"]["why"], "3 files in ~/dev/game")  # no paths known
        self.assertEqual(got["project:game:commit"]["verb"], "Commit")

    def test_commit_item(self):
        one = [{"repo": "~/dev/research", "count": 1, "paths": ["docs/research/foo.md"], "oldest": 5}]
        it = self.todos([project_facts("web", uncommitted=one)])[0]
        self.assertEqual((it["title"], it["why"], it["verb"], it["group"]),
                         ("Review 1 uncommitted file in Web", "docs/research/foo.md in ~/dev/research", "Commit", "ship"))
        five = [{"repo": "~/dev/research", "count": 5, "paths": ["a.md", "b.md", "c/d.md", "e.md", "f.md"], "oldest": 5}]
        it = self.todos([project_facts("web", uncommitted=five)])[0]
        self.assertEqual(it["title"], "Review 5 uncommitted files in Web")
        self.assertEqual(it["why"], "a.md, b.md, c/d.md +2 more in ~/dev/research")
        two = [{"repo": "~/w/a", "count": 2, "paths": ["x", "y"], "oldest": 1},
               {"repo": "~/w/b", "count": 2, "paths": ["z", "q"], "oldest": 1}]  # a workspace: 3 names in all
        self.assertEqual(opsd.commit_why(two), "x, y in ~/w/a; z +1 more in ~/w/b")


    def test_unread_chat(self):
        old = time.time() - 100
        did = opsd.q("INSERT INTO designs (project, title, state, created, updated, seen) VALUES "
                     "('web','Plan it','idle',?,?,?)", (old, old, old))
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "assistant", "hi", old + 50))
        got = self.todos()
        self.assertEqual([(x["key"], x["unread"], x["why"]) for x in got], [(f"chat:{did}:read", True, "Claude replied")])
        opsd.q("UPDATE designs SET seen=? WHERE id=?", (time.time(), did))  # you opened it: still open, so still yours
        self.assertEqual([(x["key"], x["unread"]) for x in self.todos()], [(f"chat:{did}:read", False)])
        opsd.q("UPDATE designs SET state='thinking' WHERE id=?", (did,))  # Claude is replying: not waiting on you
        self.assertEqual(self.todos(), [])
        opsd.q("UPDATE designs SET state='idle', ended=? WHERE id=?", (time.time(), did))  # you ended it
        self.assertEqual(self.todos(), [])

    def test_reply_after_end_stays_off(self):
        """An ended chat is done: a reply landing later (a task's result note) doesn't bring it back."""
        old = time.time() - 100
        did = opsd.q("INSERT INTO designs (project, title, state, created, updated, seen, ended) VALUES "
                     "('web','Plan it','idle',?,?,?,?)", (old, old, old, old))
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "ops", "#9 merged", old + 50))
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

    def test_chat_from(self):
        """The Today view's conversation for a task: the design chat that queued it, or its parent."""
        did = opsd.q("INSERT INTO designs (project, title, created, updated, kind) VALUES (?,?,?,?,?)",
                     ("web", "Plan", time.time(), time.time(), "design"))
        queued, child, alone = self.task("web", "review"), self.task("web", "review"), self.task("web", "review")
        opsd.upd(child, parent=queued)
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts, task_ids) VALUES (?,?,?,?,?)",
               (did, "assistant", "Queued", time.time(), json.dumps([queued])))
        self.assertEqual(opsd.chat_from(opsd.task(queued)), did)
        self.assertEqual(opsd.chat_from(opsd.task(child)), did)
        self.assertIsNone(opsd.chat_from(opsd.task(alone)))


class AgreeClosesOnReply(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("designs", "design_msgs", "todos"):
            opsd.q(f"DELETE FROM {t}")
        self.turns = []
        self.saved = opsd.design_turn
        opsd.design_turn = lambda did, text: self.turns.append((did, text))  # no Claude: the chat stays thinking
        self.a = opsd.q("INSERT INTO designs (project, title, created, updated, kind) VALUES ('web','a',1,1,'design')")
        self.b = opsd.q("INSERT INTO designs (project, title, created, updated, kind) VALUES ('web','b',1,1,'design')")

    def tearDown(self):
        opsd.design_turn = self.saved

    def add(self, verb, title, did):
        opsd.todo_add("web", [{"verb": verb, "title": title, "why": ""}], "design", did)
        return opsd.q("SELECT id FROM todos WHERE title=? ORDER BY id DESC", (title,), one=True)["id"]

    def closed(self, tid):
        return opsd.q("SELECT closed FROM todos WHERE id=?", (tid,), one=True)["closed"] is not None

    def say(self, did, text="yes"):
        return opsd.H.__new__(opsd.H).route(f"/api/designs/{did}/say", {"text": text})

    def test_reply_closes_agree_when_idle_and_when_queued(self):
        agree = self.add("Agree", "Use SQLite", self.a)
        self.assertFalse(self.say(self.a)["queued"])
        self.assertTrue(self.closed(agree))
        again = self.add("Agree", "Use WAL", self.a)
        self.assertTrue(self.say(self.a)["queued"])  # the chat is still replying
        self.assertTrue(self.closed(again))

    def test_do_and_other_chats_stay_open(self):
        do = self.add("Do", "Grant access", self.a)
        other = self.add("Agree", "Use Redis", self.b)
        self.say(self.a)
        self.assertFalse(self.closed(do))
        self.assertFalse(self.closed(other))

    def test_system_inserts_do_not_close(self):
        agree = self.add("Agree", "Use SQLite", self.a)
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (self.a, "ops", "note", 1))
        opsd.send_queued(self.a)
        self.assertFalse(self.closed(agree))

    def test_readding_after_close_makes_a_new_item(self):
        first = self.add("Agree", "Use SQLite", self.a)
        self.say(self.a)
        second = self.add("Agree", "Use SQLite", self.a)
        self.assertNotEqual(first, second)
        self.assertFalse(self.closed(second))


class Uncommitted(unittest.TestCase):
    """GET /api/projects/<name>/uncommitted on a scratch repo: one changed, one new and one deleted file."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", *a], cwd=self.dir, check=True, capture_output=True)
        git("init", "-q")
        (self.dir / "keep.txt").write_text("one\ntwo\nthree\n")
        (self.dir / "gone.txt").write_text("a\nb\n")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
        (self.dir / "keep.txt").write_text("one\n2\nthree\nfour\n")
        (self.dir / "gone.txt").unlink()
        (self.dir / "docs").mkdir()
        (self.dir / "docs" / "new.md").write_text("# hi\nthere\n")
        (self.dir / "pic.bin").write_bytes(b"\x89PNG\0\0data")
        self.saved = dict(opsd.PROJECTS)
        p = FakeProject("scratch")
        p.parts = [opsd.Part(type("P", (), {"repo": self.dir})(), "")]
        opsd.PROJECTS["scratch"] = p

    def tearDown(self):
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_files(self):
        got = opsd.project_uncommitted("scratch")
        self.assertEqual(len(got["repos"]), 1)
        fs = {f["path"]: f for f in got["repos"][0]["files"]}
        pick = lambda f: (f["status"], f["added"], f["removed"], f["binary"])
        self.assertEqual(pick(fs["keep.txt"]), ("changed", 2, 1, False))
        self.assertEqual(pick(fs["gone.txt"]), ("deleted", 0, 2, False))
        self.assertEqual(pick(fs["docs/new.md"]), ("new", 2, 0, False))
        self.assertEqual(pick(fs["pic.bin"]), ("new", None, None, True))
        self.assertIsNone(fs["gone.txt"]["mtime"])
        self.assertGreater(fs["keep.txt"]["mtime"], 0)
        self.assertIn("+four", got["diff"])
        self.assertIn("+++ b/docs/new.md\n@@ -0,0 +1,2 @@\n+# hi\n+there", got["diff"])
        self.assertIn("Binary file pic.bin", got["diff"])
        self.assertFalse(got["cut"])
        self.assertEqual(got["diff_lines"], len(got["diff"].splitlines()))

    def test_diff_cap(self):
        (self.dir / "big.txt").write_text("".join(f"line {i}\n" for i in range(1000)))
        got = opsd.project_uncommitted("scratch")
        self.assertTrue(got["cut"])
        self.assertEqual(len(got["diff"].splitlines()), opsd.UNCOMMITTED_DIFF)
        self.assertGreater(got["diff_lines"], 1000)
        self.assertEqual({f["path"]: f["added"] for f in got["repos"][0]["files"]}["big.txt"], 1000)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            opsd.project_uncommitted("nope")


if __name__ == "__main__":
    unittest.main()
