"""Spent worktrees go: remove_worktree never leaves a half-removed folder, fail and cancel keep the worktree
SPENT_KEEP_DAYS for a retry, and the janitor (sweep_worktrees) removes what nothing needs, skipping work.
Stdlib only, no Claude calls. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

DAY = 86400


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class Janitor(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "branch", "dev")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.toml = opsd.PROJECTS_DIR / f"jt{self.repo.parent.name[-6:].lower()}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n')
        opsd.load_projects()
        self.p = opsd.PROJECTS[self.toml.stem]
        self.p.wt.mkdir(exist_ok=True)
        opsd.SWEEP_SAID.clear()
        for m in (mock.patch.object(opsd, "notify"), mock.patch.object(opsd, "tell_sender")):
            m.start()
            self.addCleanup(m.stop)
        self.addCleanup(lambda: (self.toml.unlink(), opsd.load_projects()))

    def task(self, state="running", finished=None, made=True):
        """A task of the project with a real worktree t<id> on its own branch (made=False: none yet)."""
        tid = opsd.add_task(self.p.name, "work", f"task {time.time()}")
        path = self.p.wt / f"t{tid}"
        if made:
            git(self.repo, "worktree", "add", "-q", "-b", f"task/{tid}", str(path), "dev")
            opsd.upd(tid, worktree=str(path), branch=f"task/{tid}")
        opsd.upd(tid, state=state, finished=finished)
        return tid, path

    def events(self, tid):
        return [e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def stray(self, name):
        """A folder git doesn't know, as a half-done removal leaves: no .git, files still there."""
        f = self.p.wt / name / "sub"
        f.mkdir(parents=True, exist_ok=True)
        (f / "x").write_text("left\n")
        return f.parent

    # -- remove_worktree

    def test_remove_worktree_removes_it(self):
        tid, path = self.task()
        self.assertIsNone(opsd.remove_worktree(self.p, path))
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repo, "worktree", "list"))
        self.assertTrue(git(self.repo, "rev-parse", "--verify", f"task/{tid}"))  # the branch stays

    def test_remove_worktree_finishes_a_half_removed_folder(self):
        f = self.stray("t9999")
        locked = f / "sub"
        os.chmod(locked, 0o500)  # read-only folder: a plain rmtree can't empty it
        self.addCleanup(lambda: locked.exists() and os.chmod(locked, 0o700))
        self.assertIsNone(opsd.remove_worktree(self.p, f))
        self.assertFalse(f.exists())

    def test_remove_worktree_says_why_it_failed(self):
        _, path = self.task()

        def busy(*a, **kw):
            if a[:2] == ("worktree", "remove"):
                raise RuntimeError("git worktree remove: busy")
        with mock.patch.object(opsd.shutil, "rmtree"), mock.patch.object(self.p.parts[0], "git", side_effect=busy):
            err = opsd.remove_worktree(self.p, path)
        self.assertIn("busy", err)
        self.assertTrue(path.exists())

    def test_finish_logs_a_failed_removal_and_the_janitor_finishes_it(self):
        tid, path = self.task()
        with mock.patch.object(opsd, "remove_worktree", return_value="Resource busy"):
            opsd.finish(self.p, tid, None, "done")
        self.assertTrue(any("couldn't remove worktree" in e and "Resource busy" in e for e in self.events(tid)))
        out = opsd.sweep_worktrees(self.p)
        self.assertEqual(out["removed"], [path.name])
        self.assertFalse(path.exists())
        self.assertIn(f"janitor removed worktree {path}", self.events(tid))

    # -- fail and cancel

    def test_fail_keeps_the_worktree_for_a_retry_then_the_janitor_removes_it(self):
        tid, path = self.task()
        opsd.fail(self.p, tid, "it broke")
        self.assertTrue(path.exists())
        self.assertIn(f"worktree kept {opsd.SPENT_KEEP_DAYS} days for a retry, then removed", self.events(tid))
        self.assertEqual(opsd.sweep_worktrees(self.p)["removed"], [])
        self.assertTrue(path.exists())
        out = opsd.sweep_worktrees(self.p, now=time.time() + (opsd.SPENT_KEEP_DAYS + 1) * DAY)
        self.assertEqual(out["removed"], [path.name])
        self.assertFalse(path.exists())
        self.assertTrue(git(self.repo, "rev-parse", "--verify", f"task/{tid}"))  # a later retry starts from it

    def test_cancel_keeps_the_worktree_for_a_retry(self):
        tid, path = self.task()
        opsd.H.__new__(opsd.H).route(f"/api/tasks/{tid}/cancel", {})
        self.assertEqual(opsd.task(tid)["state"], "cancelled")
        self.assertIn(f"worktree kept {opsd.SPENT_KEEP_DAYS} days for a retry, then removed", self.events(tid))
        self.assertEqual(opsd.sweep_worktrees(self.p, now=time.time() + DAY)["removed"], [])
        out = opsd.sweep_worktrees(self.p, now=time.time() + (opsd.SPENT_KEEP_DAYS + 1) * DAY)
        self.assertEqual(out["removed"], [path.name])

    def test_a_retried_task_keeps_its_worktree(self):
        tid, path = self.task(state="queued", finished=None)  # retry clears finished
        self.assertEqual(opsd.sweep_worktrees(self.p, now=time.time() + 30 * DAY)["removed"], [])
        self.assertTrue(path.exists())

    # -- the janitor

    def test_janitor_keeps_what_is_needed_and_removes_strays(self):
        for name in ("dev", "main", "deploy", "deploy-peek"):
            self.stray(name)
        opsd.q("INSERT INTO designs (project, title) VALUES (?, 'x')", (self.p.name,))
        did = opsd.q("SELECT max(id) AS id FROM designs", one=True)["id"]
        self.stray(f"spec-{did}")
        self.stray("spec-99999")  # its chat is gone
        self.stray("openspec-update-now")  # a command is using it
        old = self.stray("openspec-update-old")
        os.utime(old, (time.time() - 2 * opsd.SWEEP_FRESH,) * 2)
        queued, qpath = self.task(state="queued", made=False)
        self.stray(qpath.name)  # its pipeline is making it
        running, rpath = self.task()
        review, vpath = self.task(state="review")
        asks, apath = self.task(state="needs_input")
        done, dpath = self.task(state="done", finished=time.time())
        self.stray("junk")
        out = opsd.sweep_worktrees(self.p)
        self.assertEqual(sorted(out["removed"]), sorted([dpath.name, "junk", "openspec-update-old", "spec-99999"]))
        left = {f.name for f in self.p.wt.iterdir()}
        self.assertEqual(left, {"dev", "main", "deploy", "deploy-peek", f"spec-{did}", "openspec-update-now",
                                qpath.name, rpath.name, vpath.name, apath.name})

    def test_janitor_skips_uncommitted_changes_and_logs_it_once(self):
        tid, path = self.task(state="done", finished=time.time())
        (path / "new.txt").write_text("work\n")
        (path / opsd.RESULT_FILE).write_text('{"status": "done"}')  # never counts as a change
        out = opsd.sweep_worktrees(self.p)
        self.assertIn("uncommitted changes", out["skipped"][path.name])
        self.assertIn("new.txt", out["skipped"][path.name])
        self.assertTrue(path.exists())
        opsd.sweep_worktrees(self.p)
        kept = [e for e in self.events(tid) if e.startswith("janitor kept worktree")]
        self.assertEqual(len(kept), 1)
        (path / "new.txt").unlink()
        self.assertEqual(opsd.sweep_worktrees(self.p)["removed"], [path.name])

    def test_janitor_skips_a_pending_question(self):
        f = self.stray("t8888")
        (f / opsd.RESULT_FILE).write_text(json.dumps({"status": "needs_input", "question": "which?"}))
        out = opsd.sweep_worktrees(self.p)
        self.assertIn("asks you something", out["skipped"]["t8888"])
        self.assertTrue(f.exists())

    def test_janitor_logs_a_failed_removal_and_tries_again(self):
        tid, path = self.task(state="done", finished=time.time())
        with mock.patch.object(opsd, "remove_worktree", return_value="Operation not permitted"):
            out = opsd.sweep_worktrees(self.p)
            opsd.sweep_worktrees(self.p)
        self.assertEqual(out["failed"], {path.name: "Operation not permitted"})
        self.assertEqual(sum("couldn't remove worktree" in e for e in self.events(tid)), 1)
        self.assertEqual(opsd.sweep_worktrees(self.p)["removed"], [path.name])

    def test_janitor_leaves_a_task_with_a_live_chat(self):
        tid, path = self.task(state="done", finished=time.time())
        opsd.TASK_CHAT_LIVE[tid] = []
        self.addCleanup(opsd.TASK_CHAT_LIVE.pop, tid, None)
        self.assertEqual(opsd.sweep_worktrees(self.p)["removed"], [])
        self.assertTrue(path.exists())

    def test_derived_data_of_a_removed_worktree_goes(self):
        dd = Path(tempfile.mkdtemp(dir=DATA))

        def entry(name, ws):
            (dd / name / "Build").mkdir(parents=True)
            (dd / name / "info.plist").write_bytes(opsd.plistlib.dumps({"WorkspacePath": str(ws)}))
        _, live = self.task()
        entry("app-live", live / "ios/app.xcworkspace")
        entry("app-gone", self.p.wt / "t77777/ios/app.xcworkspace")
        entry("app-dev", self.p.dev_wt / "ios/app.xcworkspace")
        entry("other", self.repo / "ios/app.xcworkspace")  # your checkout: not a worktree
        (dd / "ModuleCache.noindex").mkdir()  # shared, no info.plist
        with mock.patch.object(opsd, "DERIVED_DATA", dd):
            self.assertEqual(opsd.sweep_derived_data([self.p.wt]), ["app-gone"])
        self.assertEqual(sorted(f.name for f in dd.iterdir()), ["ModuleCache.noindex", "app-dev", "app-live", "other"])


if __name__ == "__main__":
    unittest.main()
