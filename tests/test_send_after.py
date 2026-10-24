"""opsd.py send --after: a task sent to another project waits (depends_on) until the named task merges.
Stdlib only, no Claude calls. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class SendAfter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("alpha", "beta"):
            repo = Path(DATA) / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                            "-q", "--allow-empty", "-m", "init"], check=True)
            (opsd.PROJECTS_DIR / f"{name}.toml").write_text(f'repo = "{repo}"\n')
        opsd.load_projects()

    def test_task_waits_on_a_task_in_another_project(self):
        dep = opsd.add_task("alpha", "the work it waits for")
        for after in (dep, str(dep), f"#{dep}"):
            r = opsd.send_peer(None, "beta", "task", "do the next step", after=after)
            t = opsd.task(r["id"])
            self.assertEqual(t["project"], "beta")
            self.assertEqual(t["depends_on"], dep)
            self.assertIn(f"after #{dep}", r["said"])

    def test_without_after_no_wait(self):
        r = opsd.send_peer(None, "beta", "task", "do it now")
        self.assertIsNone(opsd.task(r["id"])["depends_on"])

    def test_after_is_for_tasks_only(self):
        dep = opsd.add_task("alpha", "x")
        for kind in ("design", "chat"):
            with self.assertRaisesRegex(ValueError, "--after is for tasks"):
                opsd.send_peer(None, "beta", kind, "talk", after=dep)

    def test_unknown_task(self):
        with self.assertRaisesRegex(ValueError, "no task #999999"):
            opsd.send_peer(None, "beta", "task", "x", after=999999)
        with self.assertRaisesRegex(ValueError, "no task #"):
            opsd.send_peer(None, "beta", "task", "x", after="soon")

    def test_cli_parses_after(self):
        for arg in ("12", "#12"):
            sent = {}

            def fake(req, timeout=None):
                sent.update(json.loads(req.data))
                return mock.Mock(read=lambda: b'{"said": "ok"}')

            with mock.patch.object(opsd, "urlopen", fake), mock.patch("builtins.print"):
                opsd.send_cli(["beta", "--after", arg, "do", "it"])
            self.assertEqual(sent["after"], 12)
            self.assertEqual(sent["text"], "do it")

    def test_prompts_say_so(self):
        brief = opsd.peer_brief(opsd.proj("alpha"), "design")
        self.assertIn("--after <task id>", brief)
        self.assertIn("never promise to do something later", brief)
        rules = (opsd.CODE / "design_prompt.md").read_text()
        self.assertIn("never promise to do something later", rules)
        self.assertIn("`--after` on `send`", rules)


if __name__ == "__main__":
    unittest.main()
