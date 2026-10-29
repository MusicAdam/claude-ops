"""Auto-mode rules in a session's --settings, and answers that quote the question (README "Auto-mode rules",
"Answers carry the question"). Stdlib only, no Claude calls.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def project(name, toml):
    path = Path(DATA) / f"{name}.toml"
    path.write_text(toml)
    return opsd.Project(path)


class GuardSettings(unittest.TestCase):
    def test_worker_gets_auto_mode_for_its_project(self):
        p = project("demo", f'repo = "{DATA}/demo"\nworktrees = "{DATA}/demo-wt"\n')
        wt = f"{DATA}/demo-wt/t7"
        s = json.loads(opsd.guard_settings(wt, p))
        self.assertEqual(s["hooks"], json.loads(opsd.guard_settings(wt))["hooks"])  # the guard is as before
        env = "\n".join(s["autoMode"]["environment"])
        for want in ("worker for project `demo`", f"{DATA}/demo,", f"worktrees under {DATA}/demo-wt", wt,
                     "`main` and `dev` are ordinary branches", str(opsd.DATA)):
            self.assertIn(want, env)
        allow = s["autoMode"]["allow"]
        self.assertEqual(len(allow), 4)
        repo_work, config, cli, answers = allow
        self.assertIn("committing and pushing", repo_work)
        self.assertIn("`main` included", repo_work)
        self.assertIn("not a force push", repo_work)
        self.assertIn("claude-ops.toml", config)
        self.assertIn(str(opsd.PROJECTS_DIR / "demo.toml"), config)
        self.assertIn("not Self-Modification", config)
        for sub in ("export-config", "adopt-workspace-root", "add", "send"):
            self.assertIn(sub, cli)
        self.assertIn(opsd.ANSWERED, answers)
        self.assertIn("explicitly approving the action", answers)
        self.assertNotIn("soft_deny", s["autoMode"])

    def test_chat_and_workspace(self):
        p = project("ws", f'repo = "{DATA}/ws"\nrepos = ["app", "api"]\n')
        env = "\n".join(json.loads(opsd.guard_settings(None, p, chat=True))["autoMode"]["environment"])
        self.assertIn("chat for project `ws`", env)
        for want in (f"{DATA}/ws/app", f"{DATA}/ws/api", f"worktrees under {DATA}/ws-wt"):
            self.assertIn(want, env)
        self.assertNotIn("this session's is", env)  # no worktree: the project's repo is where it works

    def test_project_chat_has_no_guard(self):  # attended like the CLI: the auto-mode rules only
        p = project("pc", f'repo = "{DATA}/pc"\n')
        s = json.loads(opsd.guard_settings(None, p, chat=True, guard=False))
        self.assertEqual(set(s), {"autoMode"})
        self.assertEqual(s["autoMode"], json.loads(opsd.guard_settings(None, p, chat=True))["autoMode"])

    def test_no_project_no_auto_mode(self):  # design chats run in dontAsk mode and keep their settings
        self.assertEqual(set(json.loads(opsd.guard_settings(None))), {"hooks"})


class Answers(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("tasks", "events"):
            opsd.q(f"DELETE FROM {t}")
        self.saved = dict(opsd.PROJECTS)
        opsd.PROJECTS["demo"] = project("demo", f'repo = "{DATA}/demo"\n')

    def tearDown(self):
        opsd.PROJECTS.clear()
        opsd.PROJECTS.update(self.saved)

    def test_answer_quotes_the_question(self):
        tid = opsd.add_task("demo", "body", "commit the config")
        question = "Commit claude-ops.toml in /Users/x/dev/repo on main?\nIt is one file."
        opsd.upd(tid, state="needs_input", question=question)
        opsd.H.__new__(opsd.H).route(f"/api/tasks/{tid}/answer", {"answer": "Do it"})
        t = opsd.task(tid)
        self.assertEqual(t["state"], "queued")
        self.assertIsNone(t["question"])
        prompt = t["next_prompt"]
        self.assertTrue(prompt.startswith(opsd.ANSWERED))
        self.assertIn("> Commit claude-ops.toml in /Users/x/dev/repo on main?\n> It is one file.", prompt)
        self.assertIn("The owner answered:\n\nDo it", prompt)
        self.assertIn("including steps outside your worktree", prompt)
        self.assertTrue(prompt.endswith(opsd.ANSWER_TAIL))
        self.assertTrue(prompt.startswith(opsd.ANSWER_HEADS))  # a release or deploy still reads it as an answer


if __name__ == "__main__":
    unittest.main()
