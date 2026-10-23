"""Fresh sessions for tasks and chats, and the MCP servers a project chat loads. Stdlib only, no Claude calls:
a fake claude script records its arguments and prompt and streams one assistant event.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47961")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARGS"
echo '{"type":"system","subtype":"init","session_id":"new-sid","model":"claude-test"}'
echo '{"type":"assistant","session_id":"new-sid","message":{"content":[{"type":"text","text":"hi back"}],"usage":{"input_tokens":5,"cache_creation_input_tokens":1000,"cache_read_input_tokens":40000}}}'
echo '{"type":"result","session_id":"new-sid","total_cost_usd":0}'
"""


class Base(unittest.TestCase):
    args = Path(DATA) / "args"

    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        repo = Path(DATA) / "repo"
        if repo.exists():  # set up once for all the classes
            return
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        (opsd.PROJECTS_DIR / "demo.toml").write_text(f'repo = "{repo}"\n')
        (opsd.PROJECTS_DIR / "mcpdemo.toml").write_text(f'repo = "{repo}"\nchat_mcp = ["kept", "nowhere"]\n')
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"kept": {"command": "true"},
                                                                   "other": {"command": "false"}}}))
        opsd.load_projects()
        fake = Path(DATA) / "fake-claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        opsd.CLAUDE = str(fake)
        os.environ["FAKE_ARGS"] = str(Path(DATA) / "args")

    def chat(self, project="demo", kind="project", sid=None, ctx=None):
        now = time.time()
        opsd.q("INSERT INTO designs (project, title, kind, session_id, session_model, ctx_tokens, last_call, created,"
               " updated) VALUES (?,?,?,?,?,?,?,?,?)", (project, "t", kind, sid, "claude-old", ctx, now, now, now))
        return opsd.q("SELECT MAX(id) AS id FROM designs", one=True)["id"]

    def say(self, did, role, text):
        opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, role, text, time.time()))

    def turn(self, did, text):
        self.say(did, "user", text)
        self.args.unlink(missing_ok=True)
        opsd.design_turn(did, text)
        return self.args.read_text().split("\n")


class TaskLimit(unittest.TestCase):
    def task(self, ctx, ago=60):
        return {"session_id": "s", "last_call": time.time() - ago, "ctx_tokens": ctx}

    def test_limit_is_90k(self):
        self.assertEqual(opsd.FRESH_ABOVE, 90_000)

    def test_over_limit_starts_fresh(self):
        why = opsd.stale_session(self.task(95_000))
        self.assertIsNotNone(why)
        self.assertEqual(why[1], "context 95k")

    def test_under_limit_resumes(self):
        self.assertIsNone(opsd.stale_session(self.task(85_000)))
        self.assertIsNone(opsd.stale_session(self.task(90_000)))  # over, not at

    def test_expired_cache_still_starts_fresh(self):
        self.assertIn("cache expired", opsd.stale_session(self.task(10_000, ago=3 * 3600))[1])

    def test_no_last_call_resumes(self):
        self.assertIsNone(opsd.stale_session({"session_id": "s", "last_call": None, "ctx_tokens": 999_999}))


class ChatFresh(Base):
    def test_rule(self):
        self.assertIsNone(opsd.chat_stale({"session_id": None, "ctx_tokens": 500_000}))
        self.assertIsNone(opsd.chat_stale({"session_id": "s", "ctx_tokens": 90_000}))
        self.assertIsNone(opsd.chat_stale({"session_id": "s", "ctx_tokens": None}))
        self.assertEqual(opsd.chat_stale({"session_id": "s", "ctx_tokens": 120_000})[1],
                         "this chat's context reached 120k tokens")

    def test_under_limit_resumes_and_tracks_context(self):
        did = self.chat(sid="old-sid", ctx=50_000)
        args = self.turn(did, "next question")
        self.assertIn("old-sid", args[args.index("--resume") + 1])
        d = opsd.design(did)
        self.assertEqual(d["ctx_tokens"], 41_005)  # input, cache writes and reads of its main call
        self.assertEqual(d["session_id"], "new-sid")

    def test_over_limit_starts_fresh_with_handoff(self):
        did = self.chat(sid="old-sid", ctx=120_000)
        self.say(did, "user", "first question")
        self.say(did, "assistant", "first answer")
        args = self.turn(did, "next question")
        self.assertNotIn("--resume", args)
        prompt = args[args.index("-p") + 1:]
        joined = "\n".join(prompt)
        self.assertIn("## Handoff from your earlier session", joined)
        self.assertIn("The owner: first question", joined)
        self.assertIn("You: first answer", joined)
        self.assertIn("## Now\n\nnext question", joined)
        self.assertNotIn("The owner: next question", joined)  # this turn's message only once, as Now
        self.assertEqual(args[args.index("--model") + 1], "claude-old")  # keeps the old session's model
        msgs = opsd.q("SELECT role,text FROM design_msgs WHERE design_id=? ORDER BY id", (did,))
        self.assertTrue(any(m["role"] == "ops" and m["text"].startswith("Started a fresh session: this chat's "
                                                                         "context reached 120k") for m in msgs))
        self.assertEqual(msgs[-1]["text"], "hi back")
        self.assertEqual(opsd.design(did)["ctx_tokens"], 41_005)

    def test_design_chat_too(self):
        did = self.chat(kind="design", sid="old-sid", ctx=100_000)
        args = self.turn(did, "what about this")
        self.assertNotIn("--resume", args)
        self.assertIn("## Handoff from your earlier session", "\n".join(args))

    def test_handoff_is_capped(self):
        did = self.chat(sid="s", ctx=1)
        for i in range(40):
            self.say(did, "user", f"msg{i} " + "x" * 3000)
        self.say(did, "user", "now")
        note = opsd.chat_handoff(did, "why")
        self.assertLessEqual(len(note), opsd.HANDOFF_CHARS)
        self.assertIn("earlier messages cut", note)
        self.assertIn("msg39", note)
        self.assertNotIn("msg0 ", note)


class ChatMcp(Base):
    def test_default_none(self):
        did = self.chat()
        args = self.turn(did, "hello")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})

    def test_listed_servers_only(self):
        did = self.chat(project="mcpdemo")
        args = self.turn(did, "hello")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]),
                         {"mcpServers": {"kept": {"command": "true"}}})
        msgs = opsd.q("SELECT text FROM design_msgs WHERE design_id=? AND role='ops'", (did,))
        self.assertTrue(any("nowhere" in m["text"] for m in msgs))


class TaskChat(Base):
    """A chat with a stopped task: full tools in its worktree, read-only once it is merged."""
    def task(self, state, wt=True):
        tid = opsd.add_task("demo", "do a thing", title="thing")
        path = Path(DATA) / f"wt{tid}"
        if wt:
            path.mkdir()
        opsd.upd(tid, state=state, session_id="worker-sid", worktree=str(path), branch=f"task/{tid}")
        return tid, path

    def turn(self, tid, text="hello"):
        self.args.unlink(missing_ok=True)
        opsd.TASK_CHAT_LIVE[tid] = []
        opsd.task_chat_turn(tid, text)
        self.assertNotIn(tid, opsd.TASK_CHAT_LIVE)
        return self.args.read_text().split("\n")

    def test_stopped_task_gets_full_tools(self):
        for state in ("review", "needs_input", "failed", "cancelled", "paused"):
            tid, path = self.task(state)
            self.assertTrue(opsd.chat_full(opsd.task(tid)))
            args = self.turn(tid)
            self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
            self.assertNotIn("--allowedTools", args)
            self.assertIn("--settings", args)
            self.assertEqual(args[args.index("--resume") + 1], "worker-sid")
            self.assertIn("full tools", args[args.index("-p") + 1])
            self.assertEqual(opsd.q("SELECT text FROM task_msgs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
                                    one=True)["text"], "hi back")

    def test_merged_task_is_read_only(self):
        tid, path = self.task("done", wt=False)
        self.assertFalse(opsd.chat_full(opsd.task(tid)))
        args = self.turn(tid)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn(f"Bash({opsd.SEND_CMD}:*)", args[args.index("--allowedTools") + 1])
        self.assertIn("follow-up task", args[args.index("-p") + 1])
        self.assertFalse(path.exists())  # the stand-in dir for --resume is gone again

    def test_result_file_from_a_chat_is_dropped(self):
        tid, path = self.task("review")
        os.environ["FAKE_RESULT"] = str(path / opsd.RESULT_FILE)
        try:
            opsd.CLAUDE_WAS, opsd.CLAUDE = opsd.CLAUDE, str(Path(DATA) / "fake-writes")
            Path(opsd.CLAUDE).write_text(FAKE.replace("#!/bin/sh\n", "#!/bin/sh\necho '{}' > \"$FAKE_RESULT\"\n"))
            Path(opsd.CLAUDE).chmod(0o755)
            self.turn(tid)
        finally:
            opsd.CLAUDE = opsd.CLAUDE_WAS
        self.assertFalse((path / opsd.RESULT_FILE).exists())

    def test_ask_refuses_while_worker_runs(self):
        tid, _ = self.task("running")
        with self.assertRaises(ValueError):
            opsd.task_ask(tid, "hi")
        tid, _ = self.task("review")
        opsd.THREADS[tid] = object()
        try:
            with self.assertRaises(ValueError):
                opsd.task_ask(tid, "hi")
        finally:
            opsd.THREADS.pop(tid)

    def test_scheduler_waits_for_a_chat(self):
        tid, _ = self.task("queued")
        opsd.TASK_CHAT_LIVE[tid] = []
        started = []
        was = opsd.threading.Thread
        opsd.threading.Thread = lambda target, args, daemon: started.append(args) or type("T", (), {"start": lambda s: None})()
        try:
            opsd.schedule_once()
            self.assertEqual(opsd.task(tid)["state"], "queued")
        finally:
            opsd.threading.Thread = was
            opsd.TASK_CHAT_LIVE.pop(tid, None)
            opsd.upd(tid, state="cancelled")
            for a in started:
                opsd.THREADS.pop(a[0], None)
                opsd.upd(a[0], state="cancelled")


LOST = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_ARGS"
for a in "$@"; do [ "$a" = --resume ] && { echo "No conversation found with session ID: gone-sid" >&2; exit 1; }; done
echo '{"type":"system","subtype":"init","session_id":"new-sid","model":"claude-test"}'
echo '{"type":"assistant","session_id":"new-sid","message":{"content":[{"type":"text","text":"hi back"}]}}'
echo '{"type":"result","session_id":"new-sid","total_cost_usd":0}'
"""


class ChatRestart(Base):
    """A restart (a release reloads the code) stops a chat's reply; the next daemon answers it, so you don't resend."""

    def msgs(self, did):
        return opsd.q("SELECT role,text FROM design_msgs WHERE design_id=? ORDER BY id", (did,))

    def test_stopped_turn_writes_nothing_and_stays_replying(self):
        did = self.chat(sid="old-sid")
        self.say(did, "user", "a question")
        opsd.q("UPDATE designs SET state='thinking' WHERE id=?", (did,))
        opsd.STOPPING.set()
        try:
            opsd.design_turn(did, "a question")
        finally:
            opsd.STOPPING.clear()
        self.assertEqual(opsd.design(did)["state"], "thinking")
        self.assertEqual([m["role"] for m in self.msgs(did)], ["user"])

    def test_next_daemon_answers_the_cut_off_message(self):
        did = self.chat(sid="old-sid")
        self.say(did, "user", "a question")
        opsd.q("UPDATE designs SET state='thinking' WHERE id=?", (did,))
        self.args.unlink(missing_ok=True)
        before = set(opsd.threading.enumerate())
        opsd.resume_chats()
        for t in set(opsd.threading.enumerate()) - before:
            t.join(10)
        args = self.args.read_text().split("\n")
        self.assertEqual(args[args.index("--resume") + 1], "old-sid")
        prompt = "\n".join(args[args.index("-p") + 1:])
        self.assertIn("claude-ops restarted while you were replying", prompt)
        self.assertIn("a question", prompt)
        msgs = self.msgs(did)
        self.assertTrue(msgs[1]["role"] == "ops" and "don't need to resend" in msgs[1]["text"])
        self.assertEqual(msgs[-1]["text"], "hi back")
        self.assertEqual(opsd.design(did)["state"], "idle")

    def test_ended_chat_is_not_resumed(self):
        did = self.chat(sid="old-sid")
        self.say(did, "user", "a question")
        opsd.q("UPDATE designs SET state='thinking', ended=? WHERE id=?", (time.time(), did))
        opsd.resume_chats()
        self.assertEqual(opsd.design(did)["state"], "idle")
        self.assertEqual(len(self.msgs(did)), 1)

    def test_lost_session_starts_fresh_instead_of_no_reply(self):
        fake = Path(DATA) / "fake-lost"
        fake.write_text(LOST)
        fake.chmod(0o755)
        was, opsd.CLAUDE = opsd.CLAUDE, str(fake)
        try:
            did = self.chat(sid="gone-sid")
            self.say(did, "user", "earlier")
            self.say(did, "assistant", "earlier answer")
            self.turn(did, "next question")
        finally:
            opsd.CLAUDE = was
        msgs = self.msgs(did)
        self.assertFalse(any(m["text"].startswith("(no reply") for m in msgs))
        self.assertTrue(any(m["role"] == "ops" and "could not find this chat's session" in m["text"] for m in msgs))
        self.assertEqual(msgs[-1]["text"], "hi back")
        self.assertEqual(opsd.design(did)["session_id"], "new-sid")
        self.assertIn("## Handoff from your earlier session", self.args.read_text())

    def test_steps_and_transcript_cache_are_apart(self):
        self.assertIsNot(opsd.STEPS, opsd.STEP_SEEN)  # one name for both made shutdown call poll() on a task id


if __name__ == "__main__":
    unittest.main()
