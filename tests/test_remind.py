"""Remind me (README TODO-2): POST /api/todos/remind puts an ended topic back on the to-do list as a Do linked to it,
without reopening it. Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class Remind(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        repo = Path(DATA) / "repo-demo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        (opsd.PROJECTS_DIR / "demo.toml").write_text(f'repo = "{repo}"\n')
        opsd.load_projects()
        opsd.READY.set()
        cls.srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}{path}",
                                     None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Ops": "1"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def setUp(self):
        for t in ("designs", "design_msgs", "todos", "todo_notes"):
            opsd.q(f"DELETE FROM {t}")
        opsd.q("DELETE FROM settings WHERE key LIKE 'conv:%' OR key='research_ended'")

    def ended_chat(self, title="Login flow"):
        t = time.time() - 60
        did = opsd.q("INSERT INTO designs (project, title, kind, state, created, updated, seen) "
                     "VALUES (?,?,?,?,?,?,?)", ("demo", title, "design", "idle", t, t, t))
        opsd.topic_end("demo", did)
        return did

    def todos(self):
        """demo's chat and stored items: other test files may leave tasks of a project named demo."""
        return [x for x in self.call("/api/state")["todos"]
                if x["project"] == "demo" and x["key"].startswith(("chat:", "todo:"))]

    def test_remind_adds_a_do_linked_to_the_ended_chat(self):
        did = self.ended_chat()
        r = self.call("/api/todos/remind", {"project": "demo", "ref": did})
        self.assertTrue(r["added"])
        row = opsd.q("SELECT * FROM todos WHERE id=?", (r["id"],), one=True)
        self.assertEqual((row["verb"], row["title"], row["why"], row["source_kind"], row["source_id"]),
                         ("Do", "Login flow", "Pick up this chat", "design", did))
        ended = opsd.design(did)["ended"]
        self.assertTrue(ended)  # it stays ended
        x = next(x for x in self.todos() if x["key"] == r["key"])
        self.assertEqual((x["source"], x["topic"], x["topic_ended"]), ({"kind": "chat", "id": did}, did, ended))
        # selecting it opens its topic with the ended time it saw, which reopens the chat (TODAY-2)
        opsd.conv_open("demo", "topic", did, ended=ended)
        self.assertIsNone(opsd.design(did)["ended"])

    def test_untitled_chat_and_long_title(self):
        a, b = self.ended_chat(""), self.ended_chat("x" * 200)
        self.call("/api/todos/remind", {"project": "demo", "ref": str(a)})
        self.call("/api/todos/remind", {"project": "demo", "ref": b})
        titles = {r["source_id"]: r["title"] for r in opsd.q("SELECT * FROM todos")}
        self.assertEqual(titles, {a: f"Chat #{a}", b: "x" * 80})

    def test_second_remind_adds_nothing(self):
        did = self.ended_chat()
        r1 = self.call("/api/todos/remind", {"project": "demo", "ref": did})
        opsd.q("UPDATE designs SET title='Renamed' WHERE id=?", (did,))
        r2 = self.call("/api/todos/remind", {"project": "demo", "ref": did})
        self.assertEqual((r2["id"], r2["added"]), (r1["id"], False))
        self.assertEqual(len(opsd.q("SELECT id FROM todos")), 1)

    def test_unknown_ref_or_project_is_400(self):
        for body, says in (({"project": "demo", "ref": 999}, "no topic 999"), ({"project": "nope", "ref": 1}, "nope")):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.call("/api/todos/remind", body)
            self.assertEqual(e.exception.code, 400)
            self.assertIn(says, json.loads(e.exception.read())["error"])

    def test_read_item_hidden_while_reminder_open(self):
        did = self.ended_chat()
        r = self.call("/api/todos/remind", {"project": "demo", "ref": did})
        opsd.topic_reopen("demo", did)
        keys = [x["key"] for x in self.todos()]
        self.assertIn(r["key"], keys)
        self.assertNotIn(f"chat:{did}:read", keys)
        self.call(f"/api/todos/{r['id']}/done", {})
        keys = [x["key"] for x in self.todos()]
        self.assertEqual(keys, [f"chat:{did}:read"])

    def test_ending_the_chat_closes_the_reminder(self):
        for end in ("conversation", "designs"):
            did = self.ended_chat()
            r = self.call("/api/todos/remind", {"project": "demo", "ref": did})
            opsd.topic_reopen("demo", did)
            if end == "conversation":
                self.call("/api/conversation/end", {"project": "demo", "ref": did})
            else:
                self.call(f"/api/designs/{did}/end", {})
            self.assertIsNotNone(opsd.q("SELECT closed FROM todos WHERE id=?", (r["id"],), one=True)["closed"], end)
            self.assertNotIn(r["key"], [x["key"] for x in self.todos()])

    def test_research_topic(self):
        real = opsd.research_topics
        opsd.research_topics = lambda project: [{"id": "r:t1", "title": "Market scan", "mode": "research",
                                                 "kind": "research", "created": 0, "last": 0, "state": "idle",
                                                 "unread": False, "ended": opsd.research_ended().get("t1")}]
        try:
            opsd.topic_end("demo", "r:t1")
            r = self.call("/api/todos/remind", {"project": "demo", "ref": "r:t1"})
            row = opsd.q("SELECT * FROM todos WHERE id=?", (r["id"],), one=True)
            self.assertEqual((row["title"], row["source_kind"], row["source_id"]), ("Market scan", "research", "r:t1"))
            x = next(x for x in self.todos() if x["key"] == r["key"])
            self.assertEqual((x["topic"], x["topic_ended"]), ("r:t1", opsd.research_ended()["t1"]))
            self.assertFalse(self.call("/api/todos/remind", {"project": "demo", "ref": "r:t1"})["added"])
            opsd.topic_reopen("demo", "r:t1")
            opsd.topic_end("demo", "r:t1")
            self.assertIsNotNone(opsd.q("SELECT closed FROM todos WHERE id=?", (r["id"],), one=True)["closed"])
        finally:
            opsd.research_topics = real


if __name__ == "__main__":
    unittest.main()
