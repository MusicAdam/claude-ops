"""Today: a to-do or a panel opens its own topic, made on its first message (README: TODAY-2, conv_open
how=subject). Stdlib only, no Claude calls. Run from the repo root:
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class SubjectTopics(unittest.TestCase):
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
        for t in ("designs", "design_msgs", "tasks", "events", "schedules"):
            opsd.q(f"DELETE FROM {t}")
        opsd.q("DELETE FROM settings WHERE key LIKE 'conv:%'")
        self.said = []
        self.real_say = opsd.design_say
        opsd.design_say = lambda did, text: self.said.append((did, text)) or False

    def tearDown(self):
        opsd.design_say = self.real_say

    def task(self, title, **kw):
        cols = {"project": "demo", "title": title, "state": "review", "body": "Do the thing.", "created": time.time(),
                **kw}
        return opsd.q(f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))

    def rows(self):
        return opsd.q("SELECT * FROM designs ORDER BY id")

    def test_open_makes_no_row_and_first_say_makes_one(self):
        tid = self.task("Fix the login")
        opsd.q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (tid, time.time(), "gate passed: tests"))
        v = opsd.conv_open("demo", "subject", f"task:{tid}")
        self.assertEqual((v["topic"], v["mode"]), (None, "design"))
        self.assertEqual(self.rows(), [])
        r = opsd.conv_say("demo", "Why did it touch the session code?")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["subject"], rows[0]["title"]),
                         (r["topic"], f"task:{tid}", f"#{tid} Fix the login"))
        ops = opsd.q("SELECT * FROM design_msgs WHERE design_id=? AND role='ops'", (r["topic"],))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["text"], f"About task #{tid}")
        for bit in (f"#{tid}", "Fix the login", "review", "gate passed: tests", "Do the thing."):
            self.assertIn(bit, ops[0]["note"])
        self.assertLess(len(ops[0]["note"]), 4100)
        self.assertEqual(self.said, [(r["topic"], "Why did it touch the session code?")])

    def test_title_is_cut_to_80(self):
        tid = self.task("x" * 200)
        opsd.conv_open("demo", "subject", f"task:{tid}")
        r = opsd.conv_say("demo", "hi")
        self.assertEqual(len(opsd.design(r["topic"])["title"]), 80)

    def test_second_open_returns_that_topic(self):
        a, b = self.task("A"), self.task("B")
        opsd.conv_open("demo", "subject", f"task:{a}")
        ta = opsd.conv_say("demo", "about a")["topic"]
        opsd.conv_open("demo", "subject", f"task:{b}")
        tb = opsd.conv_say("demo", "about b")["topic"]
        self.assertNotEqual(ta, tb)
        self.assertEqual(opsd.conv_open("demo", "subject", f"task:{a}")["topic"], ta)
        self.assertEqual(opsd.conv_open("demo", "subject", f"task:{b}")["topic"], tb)
        self.assertEqual({t["subject"] for t in opsd.conv_view("demo")["topics"]}, {f"task:{a}", f"task:{b}"})

    def test_ended_topic_is_not_reused(self):
        tid = self.task("A")
        opsd.conv_open("demo", "subject", f"task:{tid}")
        first = opsd.conv_say("demo", "one")["topic"]
        opsd.topic_end("demo", first)
        self.assertIsNone(opsd.conv_open("demo", "subject", f"task:{tid}")["topic"])
        second = opsd.conv_say("demo", "two")["topic"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.rows()), 2)

    def test_project_item_title_and_note(self):
        opsd.conv_open("demo", "subject", "project:demo:promote")
        r = opsd.conv_say("demo", "Is dev ready?")
        d = opsd.design(r["topic"])
        self.assertEqual((d["title"], d["subject"]), ("Promote Demo", "project:demo:promote"))
        ops = opsd.q("SELECT * FROM design_msgs WHERE design_id=? AND role='ops'", (r["topic"],), one=True)
        self.assertEqual(ops["text"], "About Promote Demo")
        self.assertIn("Promote Demo", ops["note"])

    def test_schedule_subject(self):
        sid = opsd.q("INSERT INTO schedules (project, title, body, every, created) VALUES (?,?,?,?,?)",
                     ("demo", "Nightly sweep", "Sweep it.", "1d", time.time()))
        opsd.conv_open("demo", "subject", f"schedule:{sid}")
        d = opsd.design(opsd.conv_say("demo", "hi")["topic"])
        self.assertEqual((d["title"], d["subject"]), ("Nightly sweep", f"schedule:{sid}"))

    def test_bad_subject_is_refused(self):
        for bad in ("task:x", "nope", 12, None):
            with self.assertRaises(ValueError):
                opsd.conv_open("demo", "subject", bad)

    def test_chat_item_still_opens_its_chat(self):
        did = opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                     ("demo", "a chat", "design", time.time(), time.time()))
        opsd.conv_open("demo", "subject", "project:demo:ship")
        self.assertEqual(opsd.conv_open("demo", "chat", did)["topic"], did)
        self.assertNotIn("subject", json.loads(opsd.setting("conv:demo")))

    def test_new_topic_drops_subject(self):
        opsd.conv_open("demo", "subject", "project:demo:commit")
        opsd.conv_open("demo", "new", mode="design")
        d = opsd.design(opsd.conv_say("demo", "Something else")["topic"])
        self.assertEqual((d["subject"], d["title"]), (None, "Something else"))

    def test_state_keeps_subject_and_drops_a_bad_one(self):
        opsd.conv_open("demo", "subject", "project:demo:ship")
        opsd.conv_view("demo")  # a GET
        self.assertEqual(opsd.conv_state("demo", opsd.topics("demo")).get("subject"), "project:demo:ship")
        opsd.set_setting("conv:demo", json.dumps({"topic": None, "mode": "design", "subject": 5}))
        self.assertNotIn("subject", opsd.conv_state("demo", opsd.topics("demo")))
        opsd.set_setting("conv:demo", json.dumps({"topic": None, "mode": "design", "subject": "rm -rf"}))
        self.assertNotIn("subject", opsd.conv_state("demo", opsd.topics("demo")))

    def test_todo_items_carry_subject(self):
        tid = self.task("A")
        items = opsd.todos_list([dict(opsd.task(tid), research=None)], [], {})
        self.assertEqual([i["subject"] for i in items if i["key"].startswith("task:")], [f"task:{tid}"])


    # A message sent from a to-do step carries the to-do (README: TODAY-2 rule 5), so the topic doesn't hang on the
    # conv:<project> the page set earlier. Row 96 in the owner's database: to-do 20 (source task #313) was open, its
    # topic was ended (topic_end keeps no subject), the page had already picked the topic for that item (TD.applied)
    # and didn't post the subject again, so the next message made a topic with subject NULL and no note.
    def todo(self, tid, **kw):
        return {"key": "todo:20", "verb": "Do", "title": "Paste the schedule", "why": "The stored body is a copy.",
                "subject": f"task:{tid}", **kw}

    def note_of(self, did, text):
        return opsd.q("SELECT note FROM design_msgs WHERE design_id=? AND role='ops' AND text=?", (did, text),
                      one=True)

    def test_end_then_send_from_the_same_step_starts_its_topic(self):
        tid = self.task("Keep the doc")
        opsd.conv_open("demo", "subject", f"task:{tid}")  # the first visit
        first = opsd.conv_say("demo", "Do this to-do", self.todo(tid))["topic"]
        opsd.topic_end("demo", first)
        self.assertIsNone(opsd.conv_view("demo")["subject"])  # what the page saw: no subject pending
        r = opsd.conv_say("demo", "I believe this was completed double check", self.todo(tid))  # no new open
        self.assertNotEqual(r["topic"], first)
        d = opsd.design(r["topic"])
        self.assertEqual(d["subject"], f"task:{tid}")
        self.assertIn("Keep the doc", self.note_of(r["topic"], f"About task #{tid}")["note"])
        note = self.note_of(r["topic"], "Sent from to-do todo:20: Do Paste the schedule")["note"]
        self.assertIn("Paste the schedule", note)
        self.assertIn("The stored body is a copy.", note)

    def test_seen_before_step_reuses_its_open_topic(self):
        tid, other = self.task("A"), self.task("B")
        opsd.conv_open("demo", "subject", f"task:{tid}")
        mine = opsd.conv_say("demo", "one", self.todo(tid))["topic"]
        opsd.conv_open("demo", "subject", f"task:{other}")  # another step, then back: the page skips the open
        r = opsd.conv_say("demo", "two", self.todo(tid))
        self.assertEqual(r["topic"], mine)
        self.assertEqual(len([x for x in self.rows() if x["subject"] == f"task:{tid}"]), 1)

    def test_first_send_with_no_open_at_all(self):
        tid = self.task("A")
        r = opsd.conv_say("demo", "hi", self.todo(tid))  # the open failed: nothing kept, no topics yet
        self.assertEqual(opsd.design(r["topic"])["subject"], f"task:{tid}")

    def test_pick_routes_over_a_stale_current_topic(self):
        tid = self.task("A")
        opsd.conv_open("demo", "new", mode="design")
        other = opsd.conv_say("demo", "unrelated")["topic"]
        r = opsd.conv_say("demo", "hi", self.todo(tid))  # the owner's (or a stale) current topic: kept
        self.assertEqual(r["topic"], other)
        self.assertTrue(self.note_of(other, "Sent from to-do todo:20: Do Paste the schedule"))
        r = opsd.conv_say("demo", "hi", self.todo(tid, pick=True))  # the page never got the item's topic picked
        self.assertEqual(opsd.design(r["topic"])["subject"], f"task:{tid}")

    def test_new_topic_click_is_kept(self):
        tid = self.task("A")
        opsd.conv_open("demo", "subject", f"task:{tid}")
        opsd.conv_open("demo", "new", mode="design")
        self.assertTrue(opsd.conv_view("demo")["new"])
        d = opsd.design(opsd.conv_say("demo", "Something else", self.todo(tid))["topic"])
        self.assertIsNone(d["subject"])
        self.assertTrue(self.note_of(d["id"], "Sent from to-do todo:20: Do Paste the schedule"))
        self.assertFalse(opsd.conv_view("demo")["new"])

    def test_todo_without_subject_still_tells_claude(self):
        r = opsd.conv_say("demo", "done?", {"key": "todo:7", "verb": "Do", "title": "Grant Full Disk Access",
                                             "why": "Workers hang on the prompt."})
        d = opsd.design(r["topic"])
        self.assertIsNone(d["subject"])
        note = self.note_of(d["id"], "Sent from to-do todo:7: Do Grant Full Disk Access")["note"]
        self.assertIn("Workers hang on the prompt.", note)
        opsd.conv_say("demo", "again", {"key": "todo:7", "verb": "Do", "title": "Grant Full Disk Access"})
        self.assertEqual(len(opsd.q("SELECT 1 FROM design_msgs WHERE design_id=? AND role='ops'", (d["id"],))), 1)

    def test_no_todo_is_unchanged(self):
        tid = self.task("A")
        opsd.conv_open("demo", "subject", f"task:{tid}")
        first = opsd.conv_say("demo", "one")["topic"]
        opsd.topic_end("demo", first)
        d = opsd.design(opsd.conv_say("demo", "two")["topic"])
        self.assertIsNone(d["subject"])
        self.assertEqual(opsd.q("SELECT 1 FROM design_msgs WHERE design_id=? AND role='ops'", (d["id"],)), [])

    def test_say_endpoint_passes_the_todo(self):
        src = Path(opsd.__file__).read_text()
        self.assertIn('conv_say(b.get("project") or "", b.get("text"), b.get("todo"))', src)

if __name__ == "__main__":
    unittest.main()
