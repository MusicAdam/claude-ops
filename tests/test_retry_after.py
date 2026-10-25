"""Retry after…: POST /api/tasks/<id>/retry with `after` retries a failed or cancelled task so it waits (depends_on)
until the named task merges. Stdlib only, no Claude calls. Run from the repo root:
/opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
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


class RetryAfter(unittest.TestCase):
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

    def route(self, path, body):
        return opsd.H.__new__(opsd.H).route(path, body)

    def failed(self, project="alpha", depends_on=None):
        tid = opsd.add_task(project, "the work that failed", depends_on=depends_on)
        opsd.upd(tid, state="failed", error="it broke", finished=1.0)
        return tid

    def retry(self, tid, **body):
        return self.route(f"/api/tasks/{tid}/retry", body)

    def events(self, tid):
        return [e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def test_after_sets_depends_on_and_queues(self):
        dep = opsd.add_task("beta", "the fix it needs")
        for after in (dep, str(dep), f"#{dep}", f" #{dep} "):
            tid = self.failed()
            self.retry(tid, after=after)
            t = opsd.task(tid)
            self.assertEqual((t["state"], t["depends_on"], t["error"]), ("queued", dep, None))
            self.assertEqual(self.events(tid)[-1], f"retried after #{dep}")

    def test_note_goes_in_the_log(self):
        dep = opsd.add_task("beta", "x")
        tid = self.failed()
        self.retry(tid, after=dep, note="needs the fix")
        self.assertEqual(self.events(tid)[-1], f"retried after #{dep}: needs the fix")

    def test_after_replaces_an_earlier_wait(self):
        old, new = opsd.add_task("alpha", "old"), opsd.add_task("alpha", "new")
        tid = self.failed(depends_on=old)
        self.retry(tid, after=new)
        self.assertEqual(opsd.task(tid)["depends_on"], new)

    def test_without_after_the_wait_stays(self):
        dep = opsd.add_task("alpha", "x")
        tid = self.failed(depends_on=dep)
        self.retry(tid)
        self.assertEqual(opsd.task(tid)["depends_on"], dep)
        self.assertEqual(opsd.task(tid)["state"], "queued")
        blank = self.failed(depends_on=dep)
        self.retry(blank, after="")
        self.assertEqual(opsd.task(blank)["depends_on"], dep)

    def assertRefused(self, tid, after, msg):
        before = opsd.task(tid)
        with self.assertRaises(ValueError) as e:
            self.retry(tid, after=after)
        self.assertEqual(str(e.exception), msg)
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["depends_on"], t["error"]), (before["state"], before["depends_on"], "it broke"))

    def test_unknown_task(self):
        tid = self.failed()
        self.assertRefused(tid, 999999, "no task #999999")
        self.assertRefused(tid, "#999999", "no task #999999")
        self.assertRefused(tid, "soon", "no task #soon")

    def test_itself(self):
        tid = self.failed()
        self.assertRefused(tid, tid, "a task can't wait on itself")
        self.assertRefused(tid, f"#{tid}", "a task can't wait on itself")

    def test_cycle(self):
        tid = self.failed()
        a = opsd.add_task("alpha", "waits on the failed one", depends_on=tid)
        b = opsd.add_task("beta", "waits on that", depends_on=a)
        self.assertRefused(tid, a, f"#{a} already waits on this task")
        self.assertRefused(tid, b, f"#{b} already waits on this task")

    def test_done_dependency_is_fine(self):
        dep = opsd.add_task("beta", "x")
        opsd.upd(dep, state="done")
        tid = self.failed()
        self.retry(tid, after=dep)
        self.assertEqual(opsd.task(tid)["depends_on"], dep)

    def test_scheduler_waits_for_the_dependency(self):
        dep = opsd.add_task("beta", "the fix")
        opsd.upd(dep, state="running")
        tid = self.failed()
        self.retry(tid, after=dep)
        started = []
        real, slots = opsd.pipeline, opsd.setting("max_parallel")
        opsd.pipeline = lambda i: (started.append(i), opsd.THREADS.pop(i, None))
        opsd.set_setting("max_parallel", "50")
        try:
            # only ours is queued: every other test task is set aside first
            for r in opsd.q("SELECT id FROM tasks WHERE state='queued' AND id!=?", (tid,)):
                opsd.upd(r["id"], state="cancelled")
            opsd.THREADS.clear()
            opsd.schedule_once()
            self.assertEqual(started, [])
            self.assertEqual(opsd.task(tid)["state"], "queued")
            opsd.upd(dep, state="done")
            opsd.schedule_once()
            for th in list(opsd.THREADS.values()):
                th.join(5)
            self.assertEqual(started, [tid])
        finally:
            opsd.pipeline = real
            opsd.set_setting("max_parallel", slots or "1")
            opsd.THREADS.clear()


if __name__ == "__main__":
    unittest.main()
