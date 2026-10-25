"""The Deploy dialog's note drafted from what the deploy ships: the model's notes, else the task titles, else nothing;
a `from` target drafts from its own changelog; drafting never queues a task, and a deploy still needs a note.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class Draft(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"drf{self.repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\ndirect_main = false\n'
                        '[[deploy]]\nname = "test"\nrun = "true"\nnote = "What\'s new"\n\n'
                        '[[deploy]]\nname = "dist"\nfrom = "test"\nrun = "true"\nnote = "What\'s new"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.p = opsd.PROJECTS[self.name]
        self.calls = []
        real = opsd.claude_text
        self.addCleanup(lambda: setattr(opsd, "claude_text", real))
        self.answer = "- You can now share a game.\n- Scores load faster."
        opsd.claude_text = lambda prompt, model=None, timeout=None: (self.calls.append((prompt, model)), self.answer)[1]

    def task(self, title, summary=None):
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, summary) VALUES (?, ?, '', 0, 'done', ?)",
                     (self.name, title, summary))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", f"Merge task #{tid}: {title}")
        return tid

    def deployed(self, target):
        sha = git(self.repo, "rev-parse", "HEAD")
        opsd.set_setting(f"deployed:{self.name}:{target}", json.dumps({"commit": sha, "revs": sha, "time": 1}))

    def ntasks(self):
        return opsd.q("SELECT COUNT(*) n FROM tasks WHERE project=?", (self.name,))[0]["n"]

    def test_model_notes_from_titles_and_summaries(self):
        self.deployed("test")
        self.task("Share button on the game page", "Adds a share sheet to the game page.")
        self.task("Refactor the score cache")
        before = self.ntasks()
        out = opsd.draft_note(self.p, "test")
        self.assertEqual(out, {"note": self.answer, "source": "model"})
        prompt, model = self.calls[0]
        self.assertEqual(model, "sonnet")
        self.assertIn("What's new", prompt)
        self.assertIn("- Share button on the game page\n  Adds a share sheet to the game page.", prompt)
        self.assertIn("- Refactor the score cache", prompt)
        self.assertNotIn("#", prompt.split("What ships:")[1])  # no task ids
        self.assertEqual(self.ntasks(), before)  # drafting queues nothing

    def test_titles_when_the_model_says_nothing(self):
        self.deployed("test")
        self.task("First")
        self.task("Second")
        self.answer = ""
        self.assertEqual(opsd.draft_note(self.p, "test"), {"note": "- Second\n- First", "source": "titles"})

    def test_titles_are_capped(self):
        self.deployed("test")
        for i in range(30):
            self.task(f"A rather long task title number {i:02d} that goes on")
        self.answer = ""
        out = opsd.draft_note(self.p, "test")
        self.assertLessEqual(len(out["note"]), opsd.DRAFT_MAX)
        self.assertTrue(out["note"].endswith("goes on"))  # cut at a line end

    def test_model_notes_are_capped(self):
        self.deployed("test")
        self.task("One")
        self.answer = ("- " + "x" * 98 + "\n") * 20
        self.assertLessEqual(len(opsd.draft_note(self.p, "test")["note"]), opsd.DRAFT_MAX)

    def test_no_changelog_on_a_first_deploy(self):
        self.task("One")
        self.assertEqual(opsd.draft_note(self.p, "test"), {"note": "", "source": None})
        self.assertEqual(opsd.draft_note(self.p, "dist"), {"note": "", "source": None})  # its source never deployed
        self.assertEqual(self.calls, [])

    def test_from_target_drafts_what_its_source_deployed(self):
        self.task("Shipped to dist already")
        self.deployed("test")
        self.deployed("dist")
        self.task("In the test build")
        self.deployed("test")
        self.task("Only on main")
        self.answer = ""
        self.assertEqual(opsd.draft_note(self.p, "dist")["note"], "- In the test build")
        self.assertEqual(opsd.draft_note(self.p, "test")["note"], "- Only on main")

    def test_the_endpoint_answers_and_queues_nothing(self):
        self.deployed("test")
        self.task("One")
        before = self.ntasks()
        opsd.READY.set()
        srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/deploys/draft",
                                     json.dumps({"project": self.name, "target": "test"}).encode(),
                                     {"Content-Type": "application/json", "X-Ops": "1"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read()), {"note": self.answer, "source": "model"})
        self.assertEqual(self.ntasks(), before)

    def test_a_deploy_still_needs_a_note(self):
        with self.assertRaisesRegex(ValueError, "What's new is required"):
            opsd.queue_deploy(self.p, "test", note="  ")


class ClaudeText(unittest.TestCase):
    """claude_text gives "" when Claude fails or runs out of time, so the titles stand in."""

    def setUp(self):
        opsd.init_db()

    def fake(self, body):
        f = Path(tempfile.mkdtemp(dir=DATA)) / "claude"
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        real = opsd.CLAUDE
        opsd.CLAUDE = str(f)
        self.addCleanup(lambda: setattr(opsd, "CLAUDE", real))

    def test_answer(self):
        self.fake('cat > /dev/null; echo "- Notes"')
        self.assertEqual(opsd.claude_text("hi", "sonnet"), "- Notes")

    def test_failure(self):
        self.fake('echo "- half"; exit 1')
        self.assertEqual(opsd.claude_text("hi"), "")

    def test_timeout(self):
        self.fake("exec sleep 5")
        self.assertEqual(opsd.claude_text("hi", timeout=0.5), "")


if __name__ == "__main__":
    unittest.main()
