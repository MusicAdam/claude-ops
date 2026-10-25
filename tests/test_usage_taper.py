"""The usage taper (README: Usage limits, **Usage taper**): below the brake, the queue starts fewer Claude workers as
the 5-hour window fills. Stdlib only, no Claude calls: pipeline is stubbed, so a started task only records its id.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ.setdefault("OPS_DATA", DATA)
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

KEYS = ("max_parallel", "usage_brake", "usage_taper", "usage:five_hour", "usage:seven_day", "usage_noted:five_hour",
        "usage_noted:seven_day", "paused")


def reading(pct, live=True):
    return json.dumps({"utilization": pct / 100, "resetsAt": time.time() + (3600 if live else -60), "ts": time.time()})


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        saved = {k: opsd.setting(k) for k in KEYS}
        self.addCleanup(lambda: [opsd.set_setting(k, v) for k, v in saved.items()])
        for k in KEYS:
            opsd.q("DELETE FROM settings WHERE key=?", (k,))
        opsd.set_setting("paused", "0")
        opsd.set_setting("usage_brake", "85")
        opsd.set_setting("usage_taper", "50")
        opsd.set_setting("max_parallel", "6")
        notify = opsd.notify
        opsd.notify = lambda *a: None
        self.addCleanup(setattr, opsd, "notify", notify)

    def five(self, pct, live=True):
        opsd.set_setting("usage:five_hour", reading(pct, live))

    def cap(self):
        t = opsd.usage_info()["taper"]
        return t and t["cap"]


class Taper(Base):
    def test_cap_steps_with_six_workers(self):
        # most 6, taper 50, brake 85: steps of 7 points, exact boundaries on the lower step
        for pct, want in ((50, None), (50.5, 5), (57, 5), (57.1, 4), (64, 4), (64.1, 3), (71, 3), (71.1, 2), (78, 2),
                          (78.1, 1), (84.9, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        self.five(85)
        info = opsd.usage_info()
        self.assertTrue(info["braked"])  # the brake takes over at 85
        self.assertEqual(opsd.usage_taper_cap(63, 50, 6, 85), 4)

    def test_pure_boundaries(self):
        cap = opsd.usage_taper_cap
        for pct in range(51, 85):  # every whole-percent boundary of the table above
            self.assertEqual(cap(pct, 50, 6, 85), 6 - -(-(pct - 50) // 7), pct)
        self.assertEqual([cap(p, 50, 3, 85) for p in (50, 50.1, 67.5, 67.6, 84)], [None, 2, 2, 1, 1])
        self.assertEqual([cap(p, 50, 6, 0) for p in (60, 60.1, 90, 90.1, 99)], [5, 4, 2, 1, 1])  # brake off: to 100
        self.assertEqual([cap(p, 50, 6, 100) for p in (60, 60.1)], [5, 4])
        self.assertIsNone(cap(70, 50, 6, 50))  # brake at Taper from: no room
        self.assertIsNone(cap(70, 50, 6, 40))  # brake below it
        self.assertIsNone(cap(70, 0, 6, 85))  # taper off
        self.assertEqual([cap(p, 50, 1, 85) for p in (51, 70, 84)], [1, 1, 1])  # most 1: always 1

    def test_floor_is_one(self):
        opsd.set_setting("max_parallel", "3")
        for pct, want in ((51, 2), (67.5, 2), (67.6, 1), (84, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        opsd.set_setting("usage_brake", "0")  # the taper alone, spread to 100, never below 1
        for pct, want in ((75, 2), (75.1, 1), (99, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        self.assertFalse(opsd.usage_info()["braked"])

    def test_no_room_and_most_one(self):
        opsd.set_setting("usage_brake", "50")
        self.five(49)
        self.assertIsNone(opsd.usage_info()["taper"])  # brake at Taper from: no taper
        opsd.set_setting("usage_brake", "85")
        opsd.set_setting("max_parallel", "1")
        self.five(80)
        self.assertIsNone(opsd.usage_info()["taper"])  # cap 1 of 1 is no change

    def test_off_seven_day_and_stale(self):
        opsd.set_setting("usage_taper", "0")
        self.five(80)
        self.assertIsNone(opsd.usage_info()["taper"])
        opsd.set_setting("usage_taper", "50")
        opsd.set_setting("usage:seven_day", reading(80))
        self.five(10)
        self.assertIsNone(opsd.usage_info()["taper"])  # the 7-day window keeps only the brake
        self.five(80, live=False)
        self.assertIsNone(opsd.usage_info()["taper"])  # its reset has passed: counts as 0

    def test_words(self):
        self.five(63)
        self.assertEqual(opsd.usage_info()["taper"]["words"], "Usage at 63% of the 5-hour window · 4 of 6 workers")


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                   check=True, capture_output=True, text=True)


class Schedule(Base):
    """schedule_once and wait_reasons with a project of our own and nothing else queued."""

    def setUp(self):
        super().setUp()
        repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"tap{repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n[[deploy]]\nname = "web"\nrun = "true"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        others = [r["id"] for r in opsd.q("SELECT id FROM tasks WHERE state='queued'")]  # the shared database's
        opsd.q(f"UPDATE tasks SET state='paused' WHERE id IN ({','.join('?' * len(others))})", others)
        self.addCleanup(lambda: opsd.q(f"UPDATE tasks SET state='queued' WHERE id IN "
                                       f"({','.join('?' * len(others))})", others))
        self.ids, self.started = [], []
        self.addCleanup(lambda: opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids))
        real = opsd.pipeline
        opsd.pipeline = self.started.append  # its THREADS entry stays: it counts as running
        self.addCleanup(setattr, opsd, "pipeline", real)
        threads = dict(opsd.THREADS)
        opsd.THREADS.clear()
        self.addCleanup(lambda: (opsd.THREADS.clear(), opsd.THREADS.update(threads)))

    def add(self, kind="task", state="queued", start_at="claude"):
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, kind, start_at) VALUES "
                     "(?, ?, '', 0, ?, ?, ?)", (self.name, kind, state, kind, start_at))
        self.ids.append(tid)
        if state == "running":
            opsd.THREADS[tid] = object()
        return tid

    def schedule(self):
        opsd.schedule_once()
        for th in list(opsd.THREADS.values()):
            if hasattr(th, "join"):
                th.join(5)

    def test_starts_no_more_than_the_cap(self):
        self.five(63)  # cap 4 of 6
        self.add(state="running")
        self.add(state="running")
        self.add("deploy", state="running")  # not a Claude worker
        queued = [self.add() for _ in range(4)]
        self.schedule()
        self.assertEqual(sorted(self.started), queued[:2])  # 2 running + 2 new = the cap of 4
        self.assertEqual([opsd.task(i)["state"] for i in queued[2:]], ["queued", "queued"])

    def test_deploys_and_resumes_start_while_tapered(self):
        self.five(75)  # cap 2 of 6
        for _ in range(2):
            self.add(state="running")
        held = self.add()
        dep = self.add("deploy")
        gate = self.add(start_at="gate")  # resuming past 'claude'
        self.schedule()
        self.assertEqual(sorted(self.started), [dep, gate])
        self.assertEqual(opsd.task(held)["state"], "queued")

    def test_held_task_says_why(self):
        self.five(63)
        for _ in range(4):
            self.add(state="running")
        held, dep = self.add(), self.add("deploy")
        why = opsd.wait_reasons([opsd.task(held), opsd.task(dep)])
        self.assertEqual(why[held], "Usage at 63% of the 5-hour window · 4 of 6 workers")
        self.assertEqual(why[dep], "starting now")
        self.five(85)  # the brake's words win
        self.assertTrue(opsd.wait_reasons([opsd.task(held)])[held].startswith("Usage at 85% of the 5-hour window · "
                                                                              "starts again"))


if __name__ == "__main__":
    unittest.main()
