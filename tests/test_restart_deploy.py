"""A restart of claude-ops that loads new code (a Promote, or a direct_main merge) never cuts off a running deploy, in
any project, and no new deploy starts while it waits. Stdlib only.
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


def new_task(project, kind="task", state="queued", start_at="claude"):
    return opsd.q("INSERT INTO tasks (project, title, body, created, state, kind, start_at) VALUES "
                  "(?, ?, '', 0, ?, ?, ?)", (project, kind, state, kind, start_at))


def events(tid):
    return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=?", (tid,))]


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.restarts = []
        real = opsd.restart_self
        opsd.restart_self = self.restarts.append
        self.addCleanup(lambda: (setattr(opsd, "restart_self", real), opsd.RESTART_AFTER.clear()))
        self.ids = []
        self.addCleanup(lambda: opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids))

    def add(self, *a, **k):
        tid = new_task(*a, **k)
        self.ids.append(tid)
        return tid


class ReleaseOfClaudeOps(Base):
    """release_pipeline of the project whose repo is CODE: it restarts once no deploy runs, and waits for nothing
    else."""

    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"rel{self.repo.parent.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\ndirect_main = false\n')
        self.addCleanup(lambda: (self.toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        (self.p.dev_wt / "a.txt").write_text("two\n")
        git(self.p.dev_wt, "commit", "-q", "-am", "new code")
        code = opsd.CODE
        opsd.CODE = self.repo.resolve()  # this project is claude-ops itself
        self.addCleanup(lambda: setattr(opsd, "CODE", code))
        self.rel = opsd.add_task(name, "release", "release", kind="release")
        self.ids.append(self.rel)
        opsd.upd(self.rel, state="running")

    def release(self):
        opsd.release_pipeline(self.p, self.rel)
        t = opsd.task(self.rel)
        self.assertEqual(t["state"], "done", t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), git(self.p.dev_wt, "rev-parse", "HEAD"))

    def test_no_deploy_restarts_at_once(self):
        busy = self.add("other", state="gating")  # an ordinary task doesn't hold a Promote
        self.release()
        self.assertEqual(self.restarts, [self.rel])
        self.assertEqual(opsd.RESTART_AFTER, {})
        self.assertEqual(opsd.task(busy)["state"], "gating")

    def test_waits_for_a_running_deploy(self):
        dep = self.add("other", kind="deploy", state="running")
        self.release()
        self.assertEqual(self.restarts, [])
        self.assertIn("claude-ops restarts to load the new code once deploy #%d finishes; new deploys wait for it"
                      % dep, events(self.rel))
        opsd.RESTART_AFTER["since"] -= opsd.RESTART_IDLE_MAX + 1  # no time limit for a deploy
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [])
        opsd.upd(dep, state="done")
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [self.rel])
        self.assertEqual(opsd.RESTART_AFTER, {})

    def test_a_deploy_past_its_command_does_not_hold_it(self):
        self.add("other", kind="deploy", state="merging", start_at="promote")  # recover() resumes this one
        self.release()
        self.assertEqual(self.restarts, [self.rel])


class DirectMainMerge(Base):
    """restart_when_idle: a direct_main merge of claude-ops waits for idle at most RESTART_IDLE_MAX, but for a
    running deploy with no limit."""

    def setUp(self):
        super().setUp()
        self.merged = self.add("x", state="done")

    def test_waits_past_the_cap_for_a_deploy(self):
        dep = self.add("other", kind="deploy", state="running")
        busy = self.add("x", state="gating")
        opsd.restart_when_idle(self.merged)
        self.assertTrue(any(f"deploy #{dep} runs now" in e for e in events(self.merged)), events(self.merged))
        opsd.RESTART_AFTER["since"] -= opsd.RESTART_IDLE_MAX + 1
        opsd.restart_if_idle()
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [])
        told = [e for e in events(self.merged) if "still runs" in e]
        self.assertEqual(len(told), 1, told)  # said once, not every pass
        self.assertIn(f"deploy #{dep}", told[0])
        opsd.upd(dep, state="done")
        opsd.restart_if_idle()  # the cap is past: the gating task no longer holds it
        self.assertEqual(self.restarts, [self.merged])
        self.assertEqual(opsd.task(busy)["state"], "gating")

    def test_without_a_deploy_the_cap_still_applies(self):
        self.add("x", state="gating")
        opsd.restart_when_idle(self.merged)
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [])
        opsd.RESTART_AFTER["since"] -= opsd.RESTART_IDLE_MAX + 1
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [self.merged])

    def test_idle_restarts_at_once(self):
        opsd.restart_when_idle(self.merged)
        self.assertEqual(self.restarts, [self.merged])

    def test_a_promote_while_a_merge_waits_restarts_sooner(self):
        busy = self.add("x", state="gating")
        opsd.restart_when_idle(self.merged)
        self.assertEqual(self.restarts, [])
        rel = self.add("x", kind="release", state="done")
        opsd.restart_when_idle(rel, deploys_only=True)
        self.assertEqual(self.restarts, [rel])
        self.assertEqual(opsd.task(busy)["state"], "gating")


class NewDeploysWait(Base):
    """While a restart waits, the scheduler starts no deploy; a Promote's restart holds back nothing else."""

    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"dep{self.repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n'
                        f'[[deploy]]\nname = "web"\nrun = "true"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.started = []
        real = opsd.pipeline
        opsd.pipeline = lambda tid: (self.started.append(tid) if tid in self.ids else None, opsd.THREADS.pop(tid, None))
        self.addCleanup(lambda: setattr(opsd, "pipeline", real))
        slots = opsd.setting("max_parallel")
        opsd.set_setting("max_parallel", "50")  # a slot for each of ours, whatever else the shared database queued
        self.addCleanup(lambda: opsd.set_setting("max_parallel", slots or "1"))
        self.addCleanup(lambda: opsd.THREADS.clear())
        self.running = self.add("other", kind="deploy", state="running")
        self.rel = self.add(self.name, kind="release", state="done")

    def schedule(self):
        opsd.schedule_once()
        for th in list(opsd.THREADS.values()):
            th.join(5)

    def test_queued_deploy_waits_for_the_restart(self):
        dep = self.add(self.name, kind="deploy")
        opsd.restart_when_idle(self.rel, deploys_only=True)
        self.schedule()
        self.assertEqual(opsd.task(dep)["state"], "queued")
        self.assertEqual(self.started, [])
        self.assertEqual(opsd.wait_reasons([opsd.task(dep)])[dep], "waiting for claude-ops to restart")
        opsd.upd(self.running, state="done")
        opsd.restart_if_idle()
        self.assertEqual(self.restarts, [self.rel])
        self.schedule()  # in real life the new daemon starts it
        self.assertEqual(self.started, [dep])

    def test_a_promotes_restart_lets_other_tasks_start(self):
        other = self.add(self.name, kind="task", start_at="gate")
        opsd.restart_when_idle(self.rel, deploys_only=True)
        self.assertNotEqual(opsd.wait_reasons([opsd.task(other)])[other], "waiting for claude-ops to restart")
        self.schedule()
        self.assertEqual(self.started, [other])

    def test_a_merges_restart_holds_every_task(self):
        other = self.add(self.name, kind="task", start_at="gate")
        opsd.restart_when_idle(self.rel)
        self.schedule()
        self.assertEqual(self.started, [])
        self.assertEqual(opsd.task(other)["state"], "queued")


if __name__ == "__main__":
    unittest.main()
