"""A project home's recap (README TODAY-2, Lately and Open): GET reads and never calls Claude; POST writes the digest
with one haiku turn, saved per project; `since` counts the tasks done after it; a failure keeps the old digest; a
project with fewer than 2 done tasks has no Summarize.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class Recap(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"rcp{self.repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\ntitle = "Recap Game"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.calls = []
        self.answer = "Lately the work was about sharing and speed."
        real, real_lim = opsd.claude_text, opsd.limited
        self.addCleanup(lambda: (setattr(opsd, "claude_text", real), setattr(opsd, "limited", real_lim)))
        opsd.claude_text = lambda prompt, model=None, timeout=None: (self.calls.append((prompt, model)), self.answer)[1]
        opsd.limited = lambda: False

    def task(self, title, state="done", summary=None, finished=None, kind="task"):
        n = opsd.q("SELECT COUNT(*) n FROM tasks", one=True)["n"]
        return opsd.q("INSERT INTO tasks (project, title, body, created, state, summary, finished, kind) "
                      "VALUES (?, ?, '', 0, ?, ?, ?, ?)", (self.name, title, state, summary, finished or 1000 + n, kind))

    def topic(self, title, ended=None, replied=0, seen=0):
        did = opsd.q("INSERT INTO designs (project, title, state, created, updated, ended, seen) "
                     "VALUES (?, ?, 'idle', 1, 1, ?, ?)", (self.name, title, ended, seen))
        if replied:
            opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?, 'assistant', 'hi', ?)", (did, replied))
        return did

    def test_get_never_calls_claude(self):
        for i in range(4):
            self.task(f"T{i}", summary=f"Did {i}.\nMore.")
        r = opsd.project_recap(self.name)
        self.assertEqual(self.calls, [])
        self.assertIsNone(r["digest"])
        self.assertTrue(r["can_summarize"])
        self.assertEqual([d["title"] for d in r["done"]], ["T3", "T2", "T1", "T0"])
        self.assertEqual(r["done"][0]["summary_first_line"], "Did 3.")

    def test_fewer_than_two_done_tasks(self):
        self.task("Only one")
        self.task("Still going", state="running")
        r = opsd.project_recap(self.name)
        self.assertFalse(r["can_summarize"])
        out = opsd.recap_write(self.name)
        self.assertIn("error", out)
        self.assertIsNone(out["digest"])
        self.assertEqual(self.calls, [])

    def test_write_saves_and_counts_since(self):
        for i in range(10):
            self.task(f"Task {i}", summary=f"Summary {i}")
        self.task("A release", kind="release")
        self.topic("Open topic")
        self.topic("Ended topic", ended=5)
        out = opsd.recap_write(self.name)
        prompt, model = self.calls[0]
        self.assertEqual(model, "haiku")
        self.assertIn("Task 9", prompt)
        self.assertIn("Summary 9", prompt)
        self.assertIn("Task 2", prompt)
        self.assertNotIn("Task 1\n", prompt)  # only the last 8
        self.assertNotIn("A release", prompt)
        self.assertIn("- Open topic", prompt)
        self.assertNotIn("Ended topic", prompt)
        self.assertEqual(out["digest"]["text"], self.answer)
        self.assertEqual(out["digest"]["since"], 0)
        saved = json.loads(opsd.setting(f"recap:{self.name}"))
        self.assertEqual(saved["text"], self.answer)
        self.task("After the digest")
        self.task("Failed after", state="failed")
        r = opsd.project_recap(self.name)
        self.assertEqual(r["digest"]["since"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(r["done"]), 5)
        self.assertEqual(r["done"][0]["title"], "After the digest")

    def test_failure_keeps_the_old_digest(self):
        self.task("One")
        self.task("Two")
        opsd.recap_write(self.name)
        self.answer = ""
        out = opsd.recap_write(self.name)
        self.assertIn("error", out)
        self.assertEqual(out["digest"]["text"], "Lately the work was about sharing and speed.")

    def test_usage_limit_keeps_the_old_digest(self):
        self.task("One")
        self.task("Two")
        opsd.recap_write(self.name)
        opsd.limited = lambda: True
        out = opsd.recap_write(self.name)
        self.assertIn("usage limit", out["error"])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(out["digest"])

    def test_topics_mark_the_current_and_unread(self):
        a = self.topic("First")
        b = self.topic("Second", replied=5, seen=1)
        self.topic("Gone", ended=3)
        opsd.set_setting(f"conv:{self.name}", json.dumps({"topic": a, "mode": "design"}))
        r = opsd.project_recap(self.name)
        tops = {t["ref"]: t for t in r["topics"]}
        self.assertEqual(set(tops), {a, b})
        self.assertTrue(tops[a]["current"])
        self.assertTrue(tops[b]["unread"])
        self.assertFalse(tops[b]["current"])

    def test_unknown_project(self):
        with self.assertRaises(ValueError):
            opsd.project_recap("no-such-project")


if __name__ == "__main__":
    unittest.main()
