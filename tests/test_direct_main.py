"""Straight to main (direct_main) is the default: how a project's toml sets it, and a Send to dev landing on main.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import subprocess
import sys
import tempfile
import tomllib
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


def new_repo():
    repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class Config(unittest.TestCase):
    def setUp(self):
        opsd.init_db()

    def load(self, extra):
        repo = new_repo()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        toml = opsd.PROJECTS_DIR / f"dm{repo.parent.name[-6:].lower()}.toml"
        toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n{extra}')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        return opsd.PROJECTS[toml.stem]

    def test_no_key_is_on(self):
        self.assertTrue(self.load("").direct_main)

    def test_false_opts_out(self):
        self.assertFalse(self.load("direct_main = false\n").direct_main)

    def test_deploy_forces_it_off(self):
        self.assertFalse(self.load('[[deploy]]\nname = "web"\nrun = "true"\n').direct_main)
        self.assertFalse(self.load('direct_main = true\n[[deploy]]\nname = "web"\nrun = "true"\n').direct_main)

    def test_add_project_writes_no_key_but_says_how_to_opt_out(self):
        repo = new_repo()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        out = opsd.detect_project(str(repo), f"gen{repo.parent.name[-6:].lower()}")
        self.addCleanup(out.unlink)
        text = out.read_text()
        self.assertNotIn("direct_main", tomllib.loads(text))
        self.assertIn("# direct_main = false", text)


class SendToDevLandsOnMain(unittest.TestCase):
    """A Send to dev task that merged into dev clears your checkout, then main moves to dev (land_on_main)."""

    def setUp(self):
        opsd.init_db()
        self.repo = new_repo()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"sd{self.repo.parent.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n')
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        self.assertTrue(self.p.direct_main)
        opsd.bootstrap(self.p)

    def tearDown(self):
        opsd.q("DELETE FROM tasks WHERE project=?", (self.p.name,))
        self.toml.unlink()
        opsd.load_projects()

    def test_send_to_dev_moves_main(self):
        (self.repo / "a.txt").write_text("two\n")  # your uncommitted edit
        tid = opsd.queue_senddev(self.p)
        opsd.upd(tid, state="merging")
        # what the worker and the merge do: the snapshot applied on a branch from dev, merged --no-ff into dev
        dev = self.p.dev_wt
        git(dev, "checkout", "-q", "-b", f"task/{tid}")
        git(dev, "cherry-pick", "--no-commit", f"refs/ops/senddev-{tid}")
        git(dev, "commit", "-q", "-m", "Your uncommitted changes")
        git(dev, "checkout", "-q", self.p.dev)
        git(dev, "merge", "-q", "--no-ff", "-m", "merge", f"task/{tid}")
        merged = git(dev, "rev-parse", "HEAD")
        opsd.senddev_landed(self.p, tid, merged, "merged")
        t = opsd.task(tid)
        self.assertEqual(t["state"], "done", t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), merged)  # main moved, no Promote
        self.assertEqual((self.repo / "a.txt").read_text(), "two\n")  # the work is back in your checkout, committed
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


class RestartWhenIdle(unittest.TestCase):
    """A direct_main merge of claude-ops restarts the daemon only once no task runs, or after RESTART_IDLE_MAX."""

    def setUp(self):
        opsd.init_db()
        self.restarts = []
        real = opsd.restart_self
        opsd.restart_self = self.restarts.append
        self.addCleanup(lambda: (setattr(opsd, "restart_self", real), opsd.RESTART_AFTER.clear()))
        self.merged = opsd.q("INSERT INTO tasks (project, title, body, created, state) VALUES "
                             "('x', 'merged', '', 0, 'done')")
        self.busy = opsd.q("INSERT INTO tasks (project, title, body, created, state) VALUES "
                           "('x', 'busy', '', 0, 'gating')")
        self.addCleanup(lambda: opsd.q("DELETE FROM tasks WHERE id IN (?,?)", (self.merged, self.busy)))

    def test_waits_for_idle_and_holds_new_starts(self):
        opsd.restart_when_idle(self.merged)
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [])
        self.assertEqual(opsd.wait_reasons([{"id": 1, "state": "queued"}]), {1: "waiting for claude-ops to restart"})
        opsd.upd(self.busy, state="done")
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [self.merged])
        self.assertEqual(opsd.RESTART_AFTER, {})

    def test_restarts_anyway_after_the_cap(self):
        opsd.restart_when_idle(self.merged)
        opsd.RESTART_AFTER["since"] -= opsd.RESTART_IDLE_MAX + 1
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [self.merged])


if __name__ == "__main__":
    unittest.main()
