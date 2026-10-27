"""Straight to main (direct_main) is the default: how a project's toml sets it, and a Send to dev landing on main.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import contextlib
import io
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

    def test_deploy_targets_keep_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            p = self.load('[[deploy]]\nname = "web"\nrun = "true"\n')
        self.assertTrue(p.direct_main)
        self.assertNotIn("direct_main is off", out.getvalue())
        self.assertFalse(self.load('direct_main = false\n[[deploy]]\nname = "web"\nrun = "true"\n').direct_main)

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


class DeployAndMerge(unittest.TestCase):
    """A project with [[deploy]] targets: under direct_main a merge moving main and a deploy take turns, and a deploy's
    commits on main come into dev; with direct_main = false a merge stops at dev, as before."""

    def make(self, extra=""):
        opsd.init_db()
        self.repo = new_repo()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"dd{self.repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{name}.toml"
        toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n{extra}'
                        '[[deploy]]\nname = "site"\n'
                        'run = "git -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m bump"\n')
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        self.addCleanup(lambda: (opsd.q("DELETE FROM tasks WHERE project=?", (name,)), toml.unlink(),
                                 opsd.load_projects()))
        opsd.bootstrap(self.p)

    def merged_task(self):
        """A task the gate passed, merged --no-ff into dev, at the point land() takes over."""
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, kind) VALUES (?, 'work', '', 0, "
                     "'merging', 'task')", (self.p.name,))
        dev = self.p.dev_wt
        git(dev, "checkout", "-q", "-b", f"task/{tid}")
        (dev / "a.txt").write_text(f"task {tid}\n")
        git(dev, "commit", "-q", "-am", "work")
        git(dev, "checkout", "-q", self.p.dev)
        git(dev, "merge", "-q", "--no-ff", "-m", "merge", f"task/{tid}")
        return tid, git(dev, "rev-parse", "HEAD")

    def deploy(self, state):
        return opsd.q("INSERT INTO tasks (project, title, body, created, state, kind) VALUES (?, 'Deploy site', '', "
                      "0, ?, 'deploy')", (self.p.name, state))

    def events(self, tid):
        return [e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def test_merge_waits_for_a_running_deploy_then_lands(self):
        self.make()
        dep = self.deploy("running")
        tid, merged = self.merged_task()
        main = git(self.repo, "rev-parse", "main")
        opsd.land(self.p, tid, merged, "merged")
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "main"))
        self.assertIn(f"waiting for deploy #{dep}: it moves main too", self.events(tid))
        self.assertEqual(git(self.repo, "rev-parse", "main"), main)  # main stayed put
        self.assertEqual(opsd.wait_reasons([t])[tid], f"waiting for deploy #{dep}")
        opsd.upd(dep, state="done")
        self.assertIsNone(opsd.exclusive_blocker(opsd.task(tid)))
        opsd._pipeline(self.p, tid)  # what the scheduler starts once the deploy has ended
        self.assertEqual(opsd.task(tid)["state"], "done", opsd.task(tid)["error"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), merged)

    def test_a_deploy_waits_for_a_merge_moving_main(self):
        self.make()
        dep = self.deploy("queued")
        tid, _ = self.merged_task()
        self.assertIsNone(opsd.exclusive_blocker(opsd.task(dep)))
        opsd.upd(tid, start_at="main")  # land(): moving main now
        self.assertEqual(opsd.exclusive_blocker(opsd.task(dep))["id"], tid)
        opsd.upd(tid, state="needs_input")  # waiting for you to quit the build: the deploy may go
        self.assertIsNone(opsd.exclusive_blocker(opsd.task(dep)))

    def test_deploy_commit_moves_main_and_reaches_dev(self):
        self.make()
        before = git(self.repo, "rev-parse", "main")
        dep = opsd.queue_deploy(self.p, "site")
        opsd.upd(dep, state="running")
        opsd.deploy_pipeline(self.p, dep)
        self.assertEqual(opsd.task(dep)["state"], "done", opsd.task(dep)["error"])
        main = git(self.repo, "rev-parse", "main")
        self.assertNotEqual(main, before)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "main"), "bump")
        self.assertNotEqual(git(self.repo, "rev-parse", self.p.dev), main)
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state) VALUES (?, 'next', '', 0, 'gating')",
                     (self.p.name,))
        with self.p.lock:  # what the next task's gate start does under direct_main
            self.assertIsNone(opsd.catch_up_dev(self.p, tid))
        self.assertEqual(git(self.repo, "rev-parse", self.p.dev), main)  # a fast-forward: dev has the bump

    def test_direct_main_false_keeps_dev_and_promote(self):
        self.make("direct_main = false\n")
        self.assertFalse(self.p.direct_main)
        dep = self.deploy("running")
        tid, merged = self.merged_task()
        main = git(self.repo, "rev-parse", "main")
        opsd.land(self.p, tid, merged, "merged")
        t = opsd.task(tid)
        self.assertEqual(t["state"], "done")
        self.assertNotEqual(t["start_at"], "main")
        self.assertEqual(git(self.repo, "rev-parse", "main"), main)  # main waits for Promote
        self.assertIsNone(opsd.exclusive_blocker(opsd.task(dep)))
        self.assertFalse(any("waiting for deploy" in e for e in self.events(tid)))


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
