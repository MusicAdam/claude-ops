"""Messages sent while Claude is replying wait in the chat's queue and go as the next turn when the reply ends.
Stdlib only, no Claude calls: a fake claude script sleeps, then records each prompt it was given.
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

# the prompt follows -p; each call appends it to the prompts file, one JSON string a line
FAKE = """#!/bin/sh
while [ "$1" != "-p" ]; do shift; done
/opt/homebrew/bin/python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2" >> "$FAKE_PROMPTS"
sleep 0.4
echo '{"type":"system","subtype":"init","session_id":"sid","model":"claude-test"}'
echo '{"type":"assistant","session_id":"sid","message":{"content":[{"type":"text","text":"ok"}]}}'
echo '{"type":"result","session_id":"sid","total_cost_usd":0}'
"""


class ChatQueue(unittest.TestCase):
    prompts = Path(DATA) / "prompts"

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
        os.environ["FAKE_PROMPTS"] = str(cls.prompts)

    def setUp(self):
        self.prompts.write_text("")
        now = time.time()
        self.did = opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES (?,?,?,?,?)",
                          ("demo", "t", "project", now, now))

    def wait_idle(self):
        for _ in range(100):
            d = opsd.design(self.did)
            if d["state"] != "thinking" and not opsd.queued_of(d):
                time.sleep(0.1)  # a queued turn may be starting
                if opsd.design(self.did)["state"] != "thinking":
                    return
            time.sleep(0.1)
        self.fail("the chat never went idle")

    def sent(self):
        return [json.loads(x) for x in self.prompts.read_text().splitlines()]

    def users(self):
        return [m["text"] for m in opsd.q("SELECT text FROM design_msgs WHERE design_id=? AND role='user' ORDER BY id",
                                          (self.did,))]

    def test_messages_sent_while_replying_go_next_as_one_turn(self):
        self.assertFalse(opsd.design_say(self.did, "first"))
        self.assertTrue(opsd.design_say(self.did, "second"))
        self.assertTrue(opsd.design_say(self.did, "third"))
        self.assertEqual(opsd.queued_of(opsd.design(self.did)), ["second", "third"])
        self.assertEqual(self.users(), ["first"])  # not in the thread until they are sent
        self.wait_idle()
        prompts = self.sent()
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0].endswith("first"))
        self.assertTrue(prompts[1].endswith("second\n\nthird"))
        self.assertEqual(self.users(), ["first", "second", "third"])
        self.assertEqual(opsd.queued_of(opsd.design(self.did)), [])

    def test_slash_command_goes_on_its_own(self):
        opsd.design_say(self.did, "first")
        opsd.design_say(self.did, "a")
        opsd.design_say(self.did, "/cmd x")
        opsd.design_say(self.did, "b")
        self.wait_idle()
        prompts = self.sent()
        self.assertEqual(len(prompts), 4)
        self.assertTrue(prompts[1].endswith("a"))
        self.assertEqual(prompts[2], "/cmd x")
        self.assertTrue(prompts[3].endswith("b"))

    def test_ended_chat_drops_its_queue(self):
        opsd.design_say(self.did, "first")
        opsd.design_say(self.did, "second")
        opsd.q("UPDATE designs SET ended=? WHERE id=?", (time.time(), self.did))
        self.wait_idle()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(opsd.queued_of(opsd.design(self.did)), [])

    def test_restart_sends_a_queue_left_behind(self):
        opsd.q("UPDATE designs SET queued=? WHERE id=?", (json.dumps(["left"]), self.did))
        opsd.resume_chats()
        self.wait_idle()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.users(), ["left"])


if __name__ == "__main__":
    unittest.main()
