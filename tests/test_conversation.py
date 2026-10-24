"""The Today view's one conversation per project (README: TODAY-2): which topic a message goes to, mode switches
and their handoff, and the ```topic block that retitles a chat. Stdlib only, no Claude calls: a fake claude script
answers the one end-to-end turn. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
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
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("demo", "other"):  # other: a second project, for a topic ref sent with the wrong project
            repo = Path(DATA) / f"repo-{name}"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                            "-q", "--allow-empty", "-m", "init"], check=True)
            (opsd.PROJECTS_DIR / f"{name}.toml").write_text(f'repo = "{repo}"\n')
        opsd.load_projects()
        fake = Path(DATA) / "fake-claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        opsd.CLAUDE = str(fake)
        opsd.READY.set()  # projects are loaded: the API answers
        cls.srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Ops": "1"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

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

    def test_new_topic_from_older_checkout_topic_is_plan_and_design(self):
        # the Today composer's New topic always asks for plan & design; the older topic keeps its own kind
        p = self.chat("edit", kind="project")
        opsd.conv_open("demo", "chat", p)
        v = opsd.conv_open("demo", "new", mode="design")
        self.assertEqual((v["topic"], v["mode"], v["from"]), (None, "design", None))
        r = opsd.conv_say("demo", "Plan the next thing")
        self.assertEqual(opsd.design(r["topic"])["kind"], "design")
        self.assertEqual(opsd.design(p)["kind"], "project")
        opsd.conv_open("demo", "topic", p)
        self.assertEqual(opsd.conv_say("demo", "carry on")["topic"], p)  # it still opens and continues as itself

    def test_design_prompt_names_checkout_and_research_command(self):
        p = opsd.PROJECTS["demo"]
        out = opsd.design_system_prompt(p)
        self.assertIn(str(p.repo), out)
        self.assertIn("opsd.py add -p demo --research T1", out)

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


    # ---- ending topics: End, Reopen, End read topics older than
    def reply(self, did, ago=0):
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)",
               (did, "assistant", "hi", time.time() - ago))

    def todo_keys(self):
        return [x["key"] for x in opsd.todos_list([], [], {}) if x["project"] == "demo"]

    def test_end_and_reopen(self):
        a, b = self.chat("a", ago=10), self.chat("b")
        self.reply(a)
        self.assertEqual(self.todo_keys(), [f"chat:{b}:read", f"chat:{a}:read"])
        v = opsd.topic_end("demo", a)
        t = next(t for t in v["topics"] if t["id"] == a)
        self.assertTrue(t["ended"])
        self.assertFalse(t["unread"])  # ending marks it read
        self.assertEqual(self.todo_keys(), [f"chat:{b}:read"])
        self.assertEqual(opsd.design(a)["ended"], t["ended"])  # stored on the row: survives a restart
        v = opsd.topic_reopen("demo", str(a))
        self.assertIsNone(next(t for t in v["topics"] if t["id"] == a)["ended"])
        self.assertIn(f"chat:{a}:read", self.todo_keys())

    def test_end_current_topic_starts_new(self):
        a = self.chat("a")
        self.assertEqual(self.view()["topic"], a)
        self.assertIsNone(opsd.topic_end("demo", a)["topic"])

    def test_latest_open_topic_is_default(self):
        a, b = self.chat("a", ago=10), self.chat("b")
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), b))
        self.assertEqual(self.view()["topic"], a)

    def test_end_refused_while_replying(self):
        a = self.chat("a")
        opsd.q("UPDATE designs SET state='thinking' WHERE id=?", (a,))
        with self.assertRaisesRegex(ValueError, "still replying"):
            opsd.topic_end("demo", a)
        self.assertIsNone(opsd.design(a)["ended"])

    def test_end_in_its_own_project(self):
        # The Today view sends End topic with the project of the conversation shown (the project conv_view returns),
        # and the ref of a topic listed there.
        a = self.chat("a")
        o = opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                   ("other", "o", "design", time.time(), time.time()))
        self.assertEqual(self.view()["project"], "demo")
        self.assertEqual(opsd.conv_view("other")["project"], "other")
        self.assertEqual(opsd.conv_open("other", "topic", o)["project"], "other")
        v = self.post("/api/conversation/end", {"project": "other", "ref": o})
        self.assertEqual(v["project"], "other")
        self.assertTrue(next(t for t in v["topics"] if t["id"] == o)["ended"])
        self.assertNotIn(a, [t["id"] for t in v["topics"]])  # only other's topics
        self.assertIsNone(opsd.design(a)["ended"])

    def test_end_refuses_a_ref_of_another_project(self):
        # A topic of demo sent with other (what a stale view used to send) is refused, not ended or ignored.
        a = self.chat("a")
        with self.assertRaisesRegex(ValueError, f"no topic {a} in other"):
            opsd.topic_end("other", a)
        self.assertIsNone(opsd.design(a)["ended"])

    def test_opening_ended_topic_reopens_it(self):
        a, b = self.chat("a", ago=10), self.chat("b")
        opsd.topic_end("demo", a)
        v = opsd.conv_open("demo", "topic", a)
        self.assertEqual(v["topic"], a)
        self.assertIsNone(opsd.design(a)["ended"])

    def test_say_into_ended_topic_reopens_it(self):
        a = self.chat("a")
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), a))
        opsd.conv_open("demo", "topic", a)
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), a))  # ended from the Chat tab meanwhile
        opsd.conv_say("demo", "more")
        self.assertIsNone(opsd.design(a)["ended"])

    # ---- compare-and-set: an open reopens an ended topic only when the client saw it ended (README: TODAY-2)
    def test_stale_open_does_not_reopen(self):
        """End topic on a Today chat, then a refresh that still lists it opens it with the ended it saw (none)."""
        a, b = self.chat("a", ago=10), self.chat("b")
        opsd.conv_open("demo", "topic", b)
        opsd.topic_end("demo", a)
        v = opsd.conv_open("demo", "chat", a, ended=None)
        self.assertTrue(opsd.design(a)["ended"])
        self.assertNotIn(f"chat:{a}:read", self.todo_keys())
        self.assertEqual(v["topic"], b)  # not made the current topic either
        self.assertEqual(v["still_ended"], a)
        self.assertEqual(self.view()["topic"], b)

    def test_stale_open_over_http(self):
        a = self.chat("a")
        opsd.topic_end("demo", a)
        v = self.post("/api/conversation", {"project": "demo", "how": "chat", "ref": a, "ended": None})
        self.assertTrue(opsd.design(a)["ended"])
        self.assertIsNone(v["topic"])

    def test_open_with_its_ended_reopens(self):
        a = self.chat("a")
        ended = opsd.topic_end("demo", a)["topics"][0]["ended"]
        v = self.post("/api/conversation", {"project": "demo", "how": "topic", "ref": a, "ended": ended})
        self.assertEqual(v["topic"], a)
        self.assertNotIn("still_ended", v)
        self.assertIsNone(opsd.design(a)["ended"])

    def test_open_with_an_older_ended_does_not_reopen(self):
        a = self.chat("a")
        opsd.topic_end("demo", a)
        opsd.conv_open("demo", "chat", a, ended=1.0)
        self.assertTrue(opsd.design(a)["ended"])

    def test_open_without_ended_reopens_as_before(self):
        a = self.chat("a")
        opsd.topic_end("demo", a)
        self.assertEqual(self.post("/api/conversation", {"project": "demo", "how": "chat", "ref": a})["topic"], a)
        self.assertIsNone(opsd.design(a)["ended"])

    def test_open_of_an_open_topic_with_ended_none(self):
        a, b = self.chat("a", ago=10), self.chat("b")
        self.assertEqual(opsd.conv_open("demo", "chat", a, ended=None)["topic"], a)

    def test_say_into_ended_current_topic_reopens_it(self):
        a = self.chat("a")
        opsd.conv_open("demo", "topic", a)
        opsd.topic_end("demo", a)
        opsd.conv_open("demo", "topic", a, ended=None)  # the stale open leaves it ended and not current
        self.assertTrue(opsd.design(a)["ended"])
        opsd.conv_open("demo", "topic", a, ended=opsd.design(a)["ended"])
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), a))  # ended again elsewhere
        opsd.conv_say("demo", "more")
        self.assertIsNone(opsd.design(a)["ended"])

    def test_stale_open_of_ended_research_thread(self):
        opsd.q("DELETE FROM settings WHERE key='research_ended'")
        real = opsd.research_topics
        opsd.research_topics = lambda project: [{"id": "r:t1", "title": "R", "mode": "research", "kind": "research",
                                                 "created": 1, "last": 1, "unread": False, "state": "idle",
                                                 "ended": opsd.research_ended().get("t1")}]
        try:
            opsd.topic_end("demo", "r:t1")
            opsd.conv_open("demo", "topic", "r:t1", ended=None)
            self.assertIn("t1", opsd.research_ended())
            opsd.conv_open("demo", "topic", "r:t1", ended=opsd.research_ended()["t1"])
            self.assertEqual(opsd.research_ended(), {})
        finally:
            opsd.research_topics = real

    def test_reply_after_end_is_not_unread(self):
        """Earlier topics and the to-do list agree: an ended topic's later reply is new in neither."""
        a = self.chat("a")
        opsd.topic_end("demo", a)
        self.reply(a, ago=-5)  # a task's result note lands after you ended it
        self.assertFalse(next(t for t in self.view()["topics"] if t["id"] == a)["unread"])
        self.assertEqual(self.todo_keys(), [])

    def test_unread_topics_are_all_on_the_left(self):
        """Every topic Earlier topics flags new reply is a Read item on the left (designs 13/23/30/37 were not)."""
        ids = [self.chat(str(i), ago=i) for i in range(4)]
        for d in ids:
            self.reply(d, ago=-5)
        opsd.q("UPDATE designs SET ended=? WHERE id IN (?,?)", (time.time(), ids[0], ids[1]))
        unread = {t["id"] for t in self.view()["topics"] if t["unread"]}
        self.assertEqual(unread, set(ids[2:]))
        self.assertTrue({f"chat:{d}:read" for d in unread} <= set(self.todo_keys()))

    def test_end_read_older_than(self):
        cur = self.chat("current")
        old_read, old_unread, fresh = self.chat("old read", ago=10 * 86400), self.chat("old unread", ago=10 * 86400), \
            self.chat("fresh")
        opsd.q("UPDATE designs SET seen=updated")
        self.reply(old_unread, ago=-5)
        busy = self.chat("busy", ago=10 * 86400)
        opsd.q("UPDATE designs SET state='thinking' WHERE id=?", (busy,))
        opsd.conv_open("demo", "topic", cur)
        opsd.q("UPDATE designs SET updated=? WHERE id=?", (time.time() - 20 * 86400, cur))
        v = opsd.topics_end_read("demo", 7)
        self.assertEqual(v["ended_now"], 1)
        self.assertEqual([t["id"] for t in v["topics"] if t["ended"]], [old_read])
        self.assertEqual(v["topic"], cur)
        with self.assertRaises(ValueError):
            opsd.topics_end_read("demo", "soon")

    def test_end_research_thread(self):
        opsd.q("DELETE FROM settings WHERE key='research_ended'")
        real = opsd.research_topics
        opsd.research_topics = lambda project: [{"id": "r:t1", "title": "R", "mode": "research", "kind": "research",
                                                 "created": 1, "last": 1, "unread": False, "state": "idle",
                                                 "ended": opsd.research_ended().get("t1")}]
        try:
            opsd.topic_end("demo", "r:t1")
            self.assertIn("t1", json.loads(opsd.setting("research_ended")))
            self.assertTrue(self.view()["topics"][0]["ended"])
            opsd.topic_reopen("demo", "r:t1")
            self.assertEqual(json.loads(opsd.setting("research_ended")), {})
        finally:
            opsd.research_topics = real

    # ---- the quick chat (README: TODAY-6): a new topic that never takes over Today's shown one
    def test_quick_chat_keeps_fallback_topic(self):
        shown = self.chat("what Today shows", ago=100)
        self.assertEqual(self.view()["topic"], shown)  # nothing kept: the latest open topic
        did = self.post("/api/designs", {"project": "demo", "text": "a quick question", "quick": True})["id"]
        self.assertEqual(self.said, [(did, "a quick question")])
        v = self.view()
        self.assertEqual((v["topic"], v["mode"]), (shown, "design"))
        t = next(t for t in v["topics"] if t["id"] == did)  # an open plan & design topic in Earlier topics
        self.assertEqual((t["kind"], t["ended"]), ("design", None))

    def test_quick_chat_with_no_topics_keeps_new_topic(self):
        self.post("/api/designs", {"project": "demo", "text": "hello", "quick": True})
        self.assertEqual(self.view()["topic"], None)

    def test_quick_chat_leaves_kept_topic(self):
        kept = self.chat("kept", ago=100)
        self.chat("newer", ago=10)
        opsd.conv_open("demo", "topic", kept)
        self.post("/api/designs", {"project": "demo", "text": "hi", "quick": True})
        self.assertEqual(self.view()["topic"], kept)
        opsd.conv_open("demo", "new", mode="project")  # a kept "new topic" stays one
        self.post("/api/designs", {"project": "demo", "text": "again", "quick": True})
        self.assertEqual((self.view()["topic"], self.view()["mode"]), (None, "project"))

    def test_quick_chat_replaces_gone_topic(self):
        shown = self.chat("shown", ago=100)
        opsd.set_setting("conv:demo", json.dumps({"topic": 999, "mode": "design"}))
        self.post("/api/designs", {"project": "demo", "text": "hi", "quick": True})
        self.assertEqual(self.view()["topic"], shown)

    def test_plain_new_chat_pins_nothing(self):
        self.chat("old", ago=100)
        self.post("/api/designs", {"project": "demo", "text": "hi"})
        self.assertEqual(opsd.setting("conv:demo"), "")


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
