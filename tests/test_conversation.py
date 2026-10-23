"""The Today view's one conversation per project (README: TODAY-2): which topic a message goes to, mode switches
and their handoff, and the ```topic block that retitles a chat. Stdlib only, no Claude calls: a fake claude script
answers the one end-to-end turn. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

# replies with a topic block, as Claude would when the subject changed
FAKE = """#!/bin/sh
echo '{"type":"system","subtype":"init","session_id":"sid","model":"claude-test"}'
echo '{"type":"assistant","session_id":"sid","message":{"content":[{"type":"text","text":"Sure.\\\\n\\\\n```topic\\\\n{\\\\"title\\\\": \\\\"Login flow\\\\"}\\\\n```"}]}}'
echo '{"type":"result","session_id":"sid","total_cost_usd":0}'
"""


class Conversation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        repo = Path(DATA) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        (opsd.PROJECTS_DIR / "demo.toml").write_text(f'repo = "{repo}"\n')
        opsd.load_projects()
        fake = Path(DATA) / "fake-claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        opsd.CLAUDE = str(fake)

    def setUp(self):
        opsd.q("DELETE FROM designs")
        opsd.q("DELETE FROM design_msgs")
        opsd.q("DELETE FROM settings WHERE key LIKE 'conv:%'")
        self.said = []
        self.real_say = opsd.design_say
        opsd.design_say = lambda did, text: self.said.append((did, text)) or False

    def tearDown(self):
        opsd.design_say = self.real_say

    def chat(self, title, kind="design", ago=0):
        t = time.time() - ago
        return opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                      ("demo", title, kind, t, t))

    def view(self):
        return opsd.conv_view("demo")

    # ---- which topic is current
    def test_default_is_latest_topic(self):
        self.chat("old", ago=100)
        new = self.chat("new", kind="project", ago=10)
        v = self.view()
        self.assertEqual((v["topic"], v["mode"]), (new, "project"))
        self.assertEqual([t["title"] for t in v["topics"]], ["new", "old"])

    def test_no_topics_starts_in_plan_and_design(self):
        v = self.view()
        self.assertEqual((v["topic"], v["mode"]), (None, "design"))

    def test_chat_todo_makes_that_chat_current(self):
        old = self.chat("old", kind="project", ago=100)
        self.chat("new", ago=10)
        v = opsd.conv_open("demo", "chat", old)
        self.assertEqual((v["topic"], v["mode"]), (old, "project"))
        self.assertEqual(self.view()["topic"], old)  # kept

    def test_chat_of_another_project_is_refused(self):
        with self.assertRaises(ValueError):
            opsd.conv_open("demo", "chat", 9999)

    def test_task_todo_keeps_current_design_topic(self):
        a = self.chat("a", ago=100)
        self.chat("b", ago=10)
        opsd.conv_open("demo", "topic", a)
        self.assertEqual(opsd.conv_open("demo", "task")["topic"], a)

    def test_task_todo_leaves_project_chat_for_latest_design_topic(self):
        self.chat("design old", ago=200)
        d = self.chat("design new", ago=100)
        p = self.chat("edit", kind="project", ago=10)
        opsd.conv_open("demo", "chat", p)
        v = opsd.conv_open("demo", "task")
        self.assertEqual((v["topic"], v["mode"]), (d, "design"))

    def test_task_todo_with_no_design_topic_starts_one(self):
        self.chat("edit", kind="project")
        v = opsd.conv_open("demo", "task")
        self.assertEqual((v["topic"], v["mode"]), (None, "design"))

    def test_spec_chat_counts_as_plan_and_design(self):
        s = opsd.q("INSERT INTO designs (project, title, kind, spec, created, updated) VALUES (?,?,?,?,?,?)",
                   ("demo", "change", "design", "add-login", time.time(), time.time()))
        self.chat("edit", kind="project", ago=50)
        self.assertEqual(opsd.conv_open("demo", "task")["topic"], s)

    def test_new_topic_in_current_mode(self):
        p = self.chat("edit", kind="project")
        opsd.conv_open("demo", "chat", p)
        v = opsd.conv_open("demo", "new")
        self.assertEqual((v["topic"], v["mode"], v["from"]), (None, "project", None))

    def test_gone_topic_falls_back_to_latest(self):
        a = self.chat("a", ago=100)
        b = self.chat("b", ago=10)
        opsd.conv_open("demo", "topic", a)
        opsd.q("DELETE FROM designs WHERE id=?", (a,))
        self.assertEqual(self.view()["topic"], b)

    # ---- mode switch
    def test_switch_starts_new_topic_with_handoff_source(self):
        a = self.chat("a")
        v = opsd.conv_open("demo", "mode", mode="project")
        self.assertEqual((v["topic"], v["mode"], v["from"]["id"]), (None, "project", a))

    def test_switch_back_before_sending_returns_to_topic(self):
        a = self.chat("a")
        opsd.conv_open("demo", "mode", mode="project")
        v = opsd.conv_open("demo", "mode", mode="design")
        self.assertEqual((v["topic"], v["mode"]), (a, "design"))

    def test_switch_to_same_mode_does_nothing(self):
        a = self.chat("a")
        self.assertEqual(opsd.conv_open("demo", "mode", mode="design")["topic"], a)

    def test_research_mode_refused_while_off(self):
        self.chat("a")
        v = self.view()
        self.assertFalse(v["research"]["on"])
        with self.assertRaises(ValueError):
            opsd.conv_open("demo", "mode", mode="research")

    # ---- sending
    def test_say_continues_current_topic(self):
        a = self.chat("a")
        self.assertEqual(opsd.conv_say("demo", "hello")["topic"], a)
        self.assertEqual(self.said, [(a, "hello")])

    def test_say_into_older_topic_resumes_it(self):
        old = self.chat("old", ago=100)
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), old))
        self.chat("new", ago=10)
        opsd.conv_open("demo", "topic", old)
        opsd.conv_say("demo", "back to this")
        self.assertEqual(self.said, [(old, "back to this")])

    def test_say_with_no_topic_starts_one_titled_from_message(self):
        r = opsd.conv_say("demo", "How should login work?\nMore detail here.")
        d = opsd.design(r["topic"])
        self.assertEqual((d["kind"], d["title"]), ("design", "How should login work?"))
        self.assertEqual(self.view()["topic"], r["topic"])

    def test_say_after_switch_carries_short_handoff(self):
        a = self.chat("Login design")
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (a, "user", "x" * 30000, 1))
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (a, "assistant", "use OAuth", 2))
        opsd.conv_open("demo", "mode", mode="project")
        r = opsd.conv_say("demo", "Now build it")
        d = opsd.design(r["topic"])
        self.assertEqual(d["kind"], "project")  # a topic never changes kind: the switch made a new one
        self.assertNotEqual(r["topic"], a)
        m = opsd.q("SELECT text, note FROM design_msgs WHERE design_id=? AND role='ops'", (r["topic"],), one=True)
        self.assertIn("Switched from plan & design to edit my checkout", m["text"])
        self.assertIn("Login design", m["text"])
        self.assertIn(opsd.SWITCH_HEAD, m["note"])
        self.assertIn("use OAuth", m["note"])
        self.assertLessEqual(len(m["note"]), opsd.SWITCH_CHARS)
        self.assertEqual(opsd.news(r["topic"]), [m["note"]])  # it goes to Claude with the first message
        v = self.view()
        self.assertEqual((v["topic"], v["from"]), (r["topic"], None))

    def test_new_topic_after_new_carries_no_handoff(self):
        self.chat("a")
        opsd.conv_open("demo", "new")
        r = opsd.conv_say("demo", "fresh")
        self.assertFalse(opsd.q("SELECT id FROM design_msgs WHERE design_id=? AND role='ops'", (r["topic"],)))

    def test_unread_marks_topics(self):
        a = self.chat("a")
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (a, "assistant", "hi", time.time()))
        self.assertTrue(self.view()["topics"][0]["unread"])
        opsd.q("UPDATE designs SET seen=? WHERE id=?", (time.time() + 1, a))
        self.assertFalse(self.view()["topics"][0]["unread"])


class TopicBlock(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.did = opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                          ("demo", "first", "design", time.time(), time.time()))

    def test_retitles_and_strips(self):
        reply, note = opsd.topic_from_reply('Moving on.\n\n```topic\n{"title": "  Billing   page "}\n```', self.did)
        self.assertEqual(reply, "Moving on.")
        self.assertEqual(opsd.design(self.did)["title"], "Billing page")
        self.assertIn("Billing page", note)

    def test_bad_json_keeps_title(self):
        reply, note = opsd.topic_from_reply("ok\n```topic\nnot json\n```", self.did)
        self.assertEqual((reply, opsd.design(self.did)["title"]), ("ok", "first"))
        self.assertIn("not valid JSON", note)

    def test_no_title_keeps_title(self):
        _, note = opsd.topic_from_reply('```topic\n{"name": "x"}\n```', self.did)
        self.assertEqual(opsd.design(self.did)["title"], "first")
        self.assertIn("no title", note)

    def test_no_block(self):
        self.assertEqual(opsd.topic_from_reply("plain", self.did), ("plain", ""))

    def test_title_capped(self):
        opsd.topic_from_reply('```topic\n{"title": "%s"}\n```' % ("y" * 200), self.did)
        self.assertEqual(len(opsd.design(self.did)["title"]), 80)

    def test_chat_turn_applies_it(self):
        if "demo" not in opsd.PROJECTS:
            self.skipTest("needs the demo project of Conversation")
        did = opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                     ("demo", "first", "project", time.time(), time.time()))
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, "user", "hi", time.time()))
        opsd.design_turn(did, "hi")
        m = opsd.q("SELECT text, note FROM design_msgs WHERE design_id=? AND role='assistant'", (did,), one=True)
        self.assertEqual(m["text"], "Sure.")
        self.assertIn("Login flow", m["note"])
        self.assertEqual(opsd.design(did)["title"], "Login flow")


if __name__ == "__main__":
    unittest.main()
