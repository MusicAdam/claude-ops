"""Today: a to-do's topic, the bond (README: TODAY-2, a to-do's topic and attaching a to-do): what /api/state's items
carry as topic and topic_ended, the bond a send with an attached to-do stores, and reopening a bonded to-do's ended
topic when you select it. Stdlib only, no Claude calls. Run from the repo root:
/opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class TodoBond(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        repo = Path(DATA) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        (opsd.PROJECTS_DIR / "demo.toml").write_text(f'repo = "{repo}"\ntitle = "Demo"\n')
        opsd.load_projects()

    def setUp(self):
        for t in ("designs", "design_msgs", "tasks", "events", "todos", "todo_notes"):
            opsd.q(f"DELETE FROM {t}")
        opsd.q("DELETE FROM settings WHERE key LIKE 'conv:%'")
        self.real_say = opsd.design_say
        opsd.design_say = lambda did, text: False

    def tearDown(self):
        opsd.design_say = self.real_say

    def task(self, title="Fix the login", state="review"):
        return opsd.q("INSERT INTO tasks (project, title, state, body, created) VALUES (?,?,?,?,?)",
                      ("demo", title, state, "Do the thing.", time.time()))

    def chat(self, title, subject=None, ago=0):
        t = time.time() - ago
        return opsd.q("INSERT INTO designs (project, title, kind, created, updated, subject) VALUES (?,?,?,?,?,?)",
                      ("demo", title, "design", t, t, subject))

    def stored(self, verb="Do", title="Grant Full Disk Access", kind=None, sid=None):
        return opsd.q("INSERT INTO todos (project, verb, title, why, source_kind, source_id, created) VALUES "
                      "(?,?,?,?,?,?,?)", ("demo", verb, title, "Workers hang.", kind, sid, time.time()))

    def items(self):
        return {x["key"]: x for x in opsd.todos_list([dict(t, research=None) for t in opsd.q("SELECT * FROM tasks")],
                                                    [], {})}

    def todo(self, key, title="Grant Full Disk Access"):
        return {"key": key, "verb": "Do", "title": title, "why": "Workers hang."}

    def bond(self, key):
        r = opsd.q("SELECT topic FROM todo_notes WHERE key=?", (key,), one=True)
        return r and r["topic"]

    # ---- resolving a to-do's topic
    def test_chat_source_is_that_chat(self):
        did = self.chat("Plan it")
        opsd.todo_add("demo", [{"verb": "Agree", "title": "Use SQLite"}], "design", did)
        it = next(x for x in self.items().values() if x["verb"] == "Agree")
        self.assertEqual((it["topic"], it["topic_ended"]), (did, None))
        self.assertEqual(self.items()[f"chat:{did}:read"]["topic"], did)

    def test_subject_open_or_ended_latest_one(self):
        tid = self.task()
        self.assertIsNone(self.items()[f"task:{tid}:review"]["topic"])  # no topic about it yet
        old = self.chat("#1 older", subject=f"task:{tid}", ago=100)
        new = self.chat("#1 newer", subject=f"task:{tid}", ago=10)
        self.assertEqual(self.items()[f"task:{tid}:review"]["topic"], new)
        opsd.topic_end("demo", new)
        it = self.items()[f"task:{tid}:review"]
        self.assertEqual(it["topic"], new)  # ended still counts: selecting it reopens it
        self.assertEqual(it["topic_ended"], opsd.design(new)["ended"])
        self.assertNotEqual(old, new)

    def test_unbonded_item_has_none(self):
        key = f"todo:{self.stored()}"
        self.chat("Something else")
        self.assertEqual((self.items()[key]["topic"], self.items()[key]["topic_ended"]), (None, None))

    def test_stored_bond_wins_and_never_changes(self):
        tid = self.task()
        key = f"task:{tid}:review"
        plain = opsd.conv_say("demo", "unrelated")["topic"]  # the tab you see
        opsd.conv_say("demo", "about the review", self.todo(key, "Fix the login"))
        self.assertEqual(self.bond(key), str(plain))
        later = self.chat("#1 its own topic", subject=f"task:{tid}")  # a panel's Ask Claude made one since
        self.assertEqual(self.items()[key]["topic"], plain)  # the bond wins over the subject
        opsd.conv_open("demo", "topic", later)
        opsd.conv_say("demo", "again", self.todo(key, "Fix the login"))  # a second send elsewhere
        self.assertEqual(self.bond(key), str(plain))
        self.assertEqual(self.items()[key]["topic"], plain)

    def test_bond_to_a_deleted_topic_counts_as_none(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "hi", self.todo(key))["topic"]
        opsd.q("DELETE FROM designs WHERE id=?", (did,))
        self.assertIsNone(self.items()[key]["topic"])

    # ---- a send with an attached to-do
    def test_send_with_it_attached_stores_the_bond_and_the_line_once(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "done?", self.todo(key))["topic"]
        self.assertEqual(self.bond(key), str(did))
        opsd.conv_say("demo", "and now?", self.todo(key))
        lines = opsd.q("SELECT text, note FROM design_msgs WHERE design_id=? AND role='ops'", (did,))
        self.assertEqual([x["text"] for x in lines], [f"Sent from to-do {key}: Do Grant Full Disk Access"])
        self.assertIn("Workers hang.", lines[0]["note"])
        self.assertEqual(self.items()[key]["topic"], did)

    def test_send_without_one_carries_no_todo(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "just talking")["topic"]
        self.assertEqual(opsd.q("SELECT 1 FROM design_msgs WHERE design_id=? AND role='ops'", (did,)), [])
        self.assertIsNone(self.bond(key))
        self.assertIsNone(self.items()[key]["topic"])

    def test_new_tab_for_a_subject_todo(self):
        tid = self.task()
        key = f"task:{tid}:review"
        v = opsd.conv_open("demo", "subject", f"task:{tid}", title=f"#{tid} Fix the login")
        self.assertEqual((v["topic"], v["title"]), (None, f"#{tid} Fix the login"))
        did = opsd.conv_say("demo", "why this way?", self.todo(key, "Fix the login"))["topic"]
        d = opsd.design(did)
        self.assertEqual((d["subject"], d["title"]), (f"task:{tid}", f"#{tid} Fix the login"))
        texts = [x["text"] for x in opsd.q("SELECT text FROM design_msgs WHERE design_id=? AND role='ops' ORDER BY id",
                                           (did,))]
        self.assertEqual(texts, [f"About task #{tid}", f"Sent from to-do {key}: Do Fix the login"])
        self.assertEqual(self.bond(key), str(did))

    def test_new_tab_for_a_plain_todo_takes_its_title(self):
        key = f"todo:{self.stored()}"
        v = opsd.conv_open("demo", "new", mode="design", title="  Grant Full Disk Access " + "x" * 100)
        self.assertEqual(len(v["title"]), 80)
        did = opsd.conv_say("demo", "how?", self.todo(key))["topic"]
        self.assertTrue(opsd.design(did)["title"].startswith("Grant Full Disk Access x"))
        self.assertIsNone(opsd.conv_view("demo")["title"])  # gone once the topic exists
        self.assertEqual(self.items()[key]["topic"], did)

    def test_title_over_http_and_dropped_on_another_pick(self):
        opsd.conv_open("demo", "new", mode="design", title="Pending one")
        did = self.chat("Other")
        opsd.conv_open("demo", "topic", did)
        opsd.conv_open("demo", "new", mode="design")
        self.assertIsNone(opsd.conv_view("demo")["title"])
        src = Path(opsd.__file__).read_text()
        self.assertIn('b["ended"] if "ended" in b else UNSEEN, b.get("title"))', src)

    # ---- selecting a bonded to-do whose topic ended
    def test_selecting_reopens_its_ended_topic(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "hi", self.todo(key))["topic"]
        opsd.topic_end("demo", did)
        it = self.items()[key]
        self.assertEqual(it["topic"], did)
        v = opsd.conv_open("demo", "topic", it["topic"], ended=it["topic_ended"])  # what the dashboard sends
        self.assertEqual(v["topic"], did)
        self.assertNotIn("still_ended", v)
        self.assertIsNone(opsd.design(did)["ended"])

    def test_a_stale_item_does_not_reopen(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "hi", self.todo(key))["topic"]
        stale = self.items()[key]  # listed while it was open
        opsd.topic_end("demo", did)
        v = opsd.conv_open("demo", "topic", stale["topic"], ended=stale["topic_ended"])
        self.assertEqual(v["still_ended"], did)
        self.assertTrue(opsd.design(did)["ended"])

    # ---- the bond survives the cleanups
    def test_empty_note_park_and_list_keep_a_bond(self):
        key = f"todo:{self.stored()}"
        did = opsd.conv_say("demo", "hi", self.todo(key))["topic"]
        opsd.todo_note(key, "a note")
        opsd.todo_note(key, "")
        opsd.todo_skip(key)
        opsd.todo_unpark(key)
        opsd.todo_wait_set(key, [], "merged")
        self.items()
        self.assertEqual(self.bond(key), str(did))

    def test_row_goes_when_its_item_leaves(self):
        tid = self.stored()
        key = f"todo:{tid}"
        opsd.conv_say("demo", "hi", self.todo(key))
        opsd.q("UPDATE todos SET closed=? WHERE id=?", (time.time(), tid))
        self.items()
        self.assertIsNone(self.bond(key))

    def test_subject_about_leaves_the_notes_alone(self):
        key = f"todo:{self.stored()}"
        opsd.todo_note(key, "keep me")
        opsd.subject_about("demo", "project:demo:promote")  # a partial list: must not clean up
        self.assertEqual(opsd.q("SELECT note FROM todo_notes WHERE key=?", (key,), one=True)["note"], "keep me")

    def test_source_from_the_key(self):
        tid, did = self.task(), self.chat("c")
        sid = self.stored(kind="task", sid=tid)
        self.assertEqual(opsd.todo_source(f"task:{tid}:try"), ("demo", {"kind": "task", "id": tid}, f"task:{tid}"))
        self.assertEqual(opsd.todo_source("project:demo:ship"),
                         ("demo", {"kind": "project", "id": "demo"}, "project:demo:ship"))
        self.assertEqual(opsd.todo_source(f"chat:{did}:read"), ("demo", {"kind": "chat", "id": did}, None))
        self.assertEqual(opsd.todo_source(f"todo:{sid}"), ("demo", {"kind": "task", "id": tid}, f"task:{tid}"))
        for bad in ("", "nope", "todo:x", "task:999999:try", json.dumps({})):
            self.assertIsNone(opsd.todo_source(bad))


if __name__ == "__main__":
    unittest.main()
