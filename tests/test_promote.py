"""Promote over a main worktree whose last build rewrote a tracked file (pod install's Podfile.lock). Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class PromoteOverBuildChanges(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "Podfile.lock").write_text("hermes: aaa\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"promo{self.repo.parent.name[-6:].lower()}"
        (opsd.PROJECTS_DIR / f"{name}.toml").write_text(
            f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n'
            f'[app]\nbuild = "touch built.txt"\nartifact = "built.txt"\n')
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        self.main_wt = self.p.main_wt
        # the last build left a tracked file changed, and an untracked one that must survive
        (self.main_wt / "Podfile.lock").write_text("hermes: rewritten by pod install\n")
        (self.main_wt / ".env").write_text("SECRET=1\n")
        # the release: dev moves on with its own change to that file
        dev = self.p.dev_wt
        (dev / "Podfile.lock").write_text("hermes: bbb\n")
        git(dev, "commit", "-q", "-am", "bump")
        self.sha = git(dev, "rev-parse", "HEAD")
        self.tid = opsd.add_task(name, "release", "release", kind="release")

    def tearDown(self):  # the tests share one database: leave no queued release for another test's scheduler
        opsd.q("DELETE FROM tasks WHERE id=?", (self.tid,))
        self.toml.unlink()
        opsd.load_projects()

    def test_promote_discards_tracked_build_changes(self):
        err = opsd.promote(self.p, self.tid, self.sha)
        self.assertIsNone(err)
        self.assertEqual(git(self.main_wt, "rev-parse", "HEAD"), self.sha)
        self.assertEqual((self.main_wt / "Podfile.lock").read_text(), "hermes: bbb\n")
        self.assertEqual((self.main_wt / ".env").read_text(), "SECRET=1\n")
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.sha)
        events = [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=?", (self.tid,))]
        self.assertTrue(any("Podfile.lock" in e and "discarding" in e for e in events), events)

    def test_failed_build_rolls_back_over_build_changes(self):
        old = git(self.main_wt, "rev-parse", "HEAD")
        self.p.build = "echo changed > Podfile.lock; exit 1"
        err = opsd.promote(self.p, self.tid, self.sha)
        self.assertIn("Build failed", err)
        self.assertEqual(git(self.main_wt, "rev-parse", "HEAD"), old)
        self.assertEqual((self.main_wt / "Podfile.lock").read_text(), "hermes: aaa\n")
        self.assertEqual(git(self.repo, "rev-parse", "main"), old)


if __name__ == "__main__":
    unittest.main()
