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


def reading(pct, live=True, left=3600, now=None):
    """A 5-hour reading with left seconds to its reset (4 of 5 hours gone by default: pace 68 at brake 85)."""
    now = time.time() if now is None else now
    return json.dumps({"utilization": pct / 100, "resetsAt": now + (left if live else -60), "ts": now})


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        saved = {k: opsd.setting(k) for k in KEYS}
        self.addCleanup(lambda: [opsd.set_setting(k, v) for k, v in saved.items()])
        for k in KEYS:
            opsd.q("DELETE FROM settings WHERE key=?", (k,))
        opsd.set_setting("paused", "0")
        opsd.set_setting("usage_brake", "85")
        opsd.set_setting("usage_taper", "30")
        opsd.set_setting("max_parallel", "6")
        notify = opsd.notify
        opsd.notify = lambda *a: None
        self.addCleanup(setattr, opsd, "notify", notify)

    def five(self, pct, live=True, left=3600, now=None):
        opsd.set_setting("usage:five_hour", reading(pct, live, left, now))

    def cap(self):
        t = opsd.usage_info()["taper"]
        return t and t["cap"]


class Taper(Base):
    def test_cap_steps_with_six_workers(self):
        # most 6, taper 30, brake 85: cap = 6 - ceil(5 * sqrt(frac)), so step k ends at 30 + 55 * k² / 25 = 30 + 2.2k²
        # (32.2, 38.8, 49.8, 65.2): exact boundaries on the lower step
        for pct, want in ((30, None), (30.1, 5), (32.2, 5), (32.3, 4), (38.8, 4), (38.9, 3), (49.8, 3), (49.9, 2),
                          (65.2, 2), (65.3, 1), (84.9, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        self.five(85)
        info = opsd.usage_info()
        self.assertTrue(info["braked"])  # the brake takes over at 85
        self.assertEqual(opsd.usage_taper_cap(36, 30, 6, 85), 4)

    def test_reference_values(self):
        cap = opsd.usage_taper_cap
        # most 6, taper 30, brake 90: 35 -> sqrt(5/60) * 5 = 1.44 -> 4; 45 -> sqrt(.25) * 5 = 2.5 -> 3;
        # 60 -> sqrt(.5) * 5 = 3.54 -> 2; 75 -> sqrt(.75) * 5 = 4.33 -> 1; 89 -> 1
        self.assertEqual([cap(p, 30, 6, 90) for p in (35, 45, 60, 75, 89)], [4, 3, 2, 1, 1])
        # most 3, taper 30, brake 90: one boundary at sqrt(frac) * 2 = 1, frac .25, pct 45 (stays on 2)
        self.assertEqual([cap(p, 30, 3, 90) for p in (30, 30.1, 35, 45, 45.1, 60, 89)], [None, 2, 2, 2, 1, 1, 1])
        opsd.set_setting("usage_brake", "90")
        for pct, want in ((35, 4), (45, 3), (60, 2)):  # behind the pace line (72), so the taper alone
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)

    def test_pure_boundaries(self):
        cap = opsd.usage_taper_cap
        for pct in range(31, 85):  # every whole percent, against the step ends 30 + 2.2k²
            k = next(k for k in range(1, 6) if 30 + 2.2 * k * k >= pct)
            self.assertEqual(cap(pct, 30, 6, 85), 6 - k, pct)
        self.assertEqual([cap(p, 30, 3, 85) for p in (30, 30.1, 43.75, 43.8, 84)], [None, 2, 2, 1, 1])
        # brake off: to 100, step ends 30 + 2.8k² (32.8, 41.2, 55.2, 74.8)
        self.assertEqual([cap(p, 30, 6, 0) for p in (32.8, 32.9, 41.2, 41.3, 55.2, 55.3, 74.8, 74.9, 99)],
                         [5, 4, 4, 3, 3, 2, 2, 1, 1])
        self.assertEqual([cap(p, 30, 6, 100) for p in (41.2, 41.3)], [4, 3])
        self.assertIsNone(cap(30, 30, 6, 85))  # at Taper from
        self.assertIsNone(cap(70, 30, 6, 30))  # brake at Taper from: no room
        self.assertIsNone(cap(70, 30, 6, 20))  # brake below it
        self.assertIsNone(cap(70, 0, 6, 85))  # taper off
        self.assertEqual([cap(p, 30, 1, 85) for p in (31, 70, 84)], [1, 1, 1])  # most 1: always 1

    def test_floor_is_one(self):
        opsd.set_setting("max_parallel", "3")
        for pct, want in ((31, 2), (43.75, 2), (43.8, 1), (84, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        opsd.set_setting("usage_brake", "0")  # the taper alone, spread to 100, never below 1
        for pct, want in ((47.5, 2), (47.6, 1), (99, 1)):
            self.five(pct)
            self.assertEqual(self.cap(), want, pct)
        self.assertFalse(opsd.usage_info()["braked"])

    def test_no_room_and_most_one(self):
        opsd.set_setting("usage_brake", "30")
        self.five(29, left=60)  # late in the window, so behind the pace line (29.9) too
        self.assertIsNone(opsd.usage_info()["taper"])  # brake at Taper from: no taper
        opsd.set_setting("usage_brake", "85")
        opsd.set_setting("max_parallel", "1")
        self.five(80)
        self.assertIsNone(opsd.usage_info()["taper"])  # cap 1 of 1 is no change

    def test_default_is_30(self):
        opsd.q("DELETE FROM settings WHERE key='usage_taper'")
        opsd.init_db()
        self.assertEqual(opsd.setting("usage_taper"), "30")
        opsd.set_setting("usage_taper", "50")
        opsd.init_db()
        self.assertEqual(opsd.setting("usage_taper"), "50")  # a stored value is kept

    def test_off_seven_day_and_stale(self):
        opsd.set_setting("usage_taper", "0")
        self.five(80)
        self.assertIsNone(opsd.usage_info()["taper"])
        opsd.set_setting("usage_taper", "30")
        opsd.set_setting("usage:seven_day", reading(80))
        self.five(10)
        self.assertIsNone(opsd.usage_info()["taper"])  # the 7-day window keeps only the brake
        self.five(80, live=False)
        self.assertIsNone(opsd.usage_info()["taper"])  # its reset has passed: counts as 0

    def test_words(self):
        self.five(45)
        self.assertEqual(opsd.usage_info()["taper"]["words"], "Usage at 45% of the 5-hour window · 3 of 6 workers")


NOW = 1_800_000_000.0  # a fixed 'now'
HOUR_IN = 4 * 3600  # one hour into the 5-hour window: pace 17 at brake 85


class Pace(Base):
    """The pace slowdown (README: Usage limits, **Pace slowdown**), at a fixed now."""

    def at(self, pct, left=HOUR_IN, live=True):
        self.five(pct, live, left, NOW)
        return opsd.usage_info(now=NOW)["taper"]

    def test_pure(self):
        self.assertAlmostEqual(opsd.usage_lead(30, NOW + HOUR_IN, NOW, 85), 13)
        self.assertAlmostEqual(opsd.usage_lead(30, NOW + HOUR_IN, NOW, 0), 10)  # brake off: pace runs to 100
        self.assertAlmostEqual(opsd.usage_lead(30, NOW + 99999, NOW, 85), 30)  # elapsed clamps at 0
        self.assertIsNone(opsd.usage_lead(30, NOW - 1, NOW, 85))  # stale
        cap = opsd.usage_pace_cap
        self.assertEqual([cap(x, 30, 6) for x in (-3, 0, 0.1, 3, 3.1, 6, 6.1, 9, 12, 12.1, 99)],
                         [None, None, 5, 5, 4, 4, 3, 3, 2, 1, 1])  # one fewer per 3 points ahead
        self.assertIsNone(cap(13, 0, 6))  # taper off
        self.assertIsNone(cap(None, 30, 6))  # no live reading

    def test_example_ahead_and_behind(self):
        t = self.at(25)  # lead 8: 6 - ceil(8 / 3); no taper cap at 25
        self.assertEqual((t["cap"], t["max"], t["ahead"]), (3, 6, 8))
        self.assertEqual(t["words"], "Usage at 25% of the 5-hour window, 8 points ahead of pace · 3 of 6 workers")
        self.assertIsNone(self.at(10))  # behind pace: all 6
        self.assertIsNone(self.at(17))  # on pace

    def test_taper_only_when_behind(self):
        t = self.at(45, left=600)  # pace 82.2: behind, so the taper cap 3 alone
        self.assertEqual((t["cap"], t["ahead"]), (3, 0))
        self.assertEqual(t["words"], "Usage at 45% of the 5-hour window · 3 of 6 workers")

    def test_lower_cap_wins_both_ways(self):
        t = self.at(80, left=600)  # taper cap 1, lead -2.2: taper
        self.assertEqual((t["cap"], t["ahead"]), (1, 0))
        t = self.at(31)  # taper cap 5; lead 14: pace cap 1 is lower
        self.assertEqual((t["cap"], t["ahead"]), (1, 14))
        t = self.at(40, left=3 * 3600)  # pace 34, lead 6: pace cap 4 vs taper cap 3: the taper binds
        self.assertEqual((t["cap"], t["ahead"]), (3, 0))
        t = self.at(55, left=1800)  # pace 76.5: behind; taper cap 2
        self.assertEqual((t["cap"], t["ahead"]), (2, 0))
        t = self.at(38, left=3 * 3600)  # pace 34, lead 4: pace cap 4 vs taper cap 4: tie, the pace cap binds
        self.assertEqual((t["cap"], t["ahead"]), (4, 4))

    def test_floor_is_one(self):
        self.assertEqual(self.at(49)["cap"], 1)  # 32 ahead: 6 - 11 floors at 1

    def test_taper_off_and_stale(self):
        opsd.set_setting("usage_taper", "0")
        self.assertIsNone(self.at(30))
        opsd.set_setting("usage_taper", "30")
        self.assertIsNone(self.at(30, live=False))

    def test_brake_off_runs_to_100(self):
        opsd.set_setting("usage_brake", "0")
        t = self.at(30)  # pace 20, lead 10: 6 - ceil(10 / 3)
        self.assertEqual((t["cap"], t["ahead"]), (2, 10))

    def test_seven_day_ignored(self):
        opsd.set_setting("usage:seven_day", json.dumps({"utilization": 0.5, "resetsAt": NOW + 3600, "ts": NOW}))
        self.assertIsNone(self.at(10))


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
        self.five(36)  # cap 4 of 6
        self.add(state="running")
        self.add(state="running")
        self.add("deploy", state="running")  # not a Claude worker
        queued = [self.add() for _ in range(4)]
        self.schedule()
        self.assertEqual(sorted(self.started), queued[:2])  # 2 running + 2 new = the cap of 4
        self.assertEqual([opsd.task(i)["state"] for i in queued[2:]], ["queued", "queued"])

    def test_pace_cap_limits_starts(self):
        self.five(25, left=HOUR_IN)  # 8 ahead of pace: cap 3 of 6, no taper cap
        self.add(state="running")
        self.add("deploy", state="running")
        queued = [self.add() for _ in range(4)]
        self.schedule()
        self.assertEqual(sorted(self.started), queued[:2])  # 1 running + 2 new = the pace cap of 3
        why = opsd.wait_reasons([opsd.task(queued[2])])
        self.assertEqual(why[queued[2]], "Usage at 25% of the 5-hour window, 8 points ahead of pace · 3 of 6 workers")

    def test_deploys_and_resumes_start_while_tapered(self):
        self.five(55)  # cap 2 of 6
        for _ in range(2):
            self.add(state="running")
        held = self.add()
        dep = self.add("deploy")
        gate = self.add(start_at="gate")  # resuming past 'claude'
        self.schedule()
        self.assertEqual(sorted(self.started), [dep, gate])
        self.assertEqual(opsd.task(held)["state"], "queued")

    def test_held_task_says_why(self):
        self.five(36)  # cap 4 of 6
        for _ in range(4):
            self.add(state="running")
        held, dep = self.add(), self.add("deploy")
        why = opsd.wait_reasons([opsd.task(held), opsd.task(dep)])
        self.assertEqual(why[held], "Usage at 36% of the 5-hour window · 4 of 6 workers")
        self.assertEqual(why[dep], "starting now")
        self.five(85)  # the brake's words win
        self.assertTrue(opsd.wait_reasons([opsd.task(held)])[held].startswith("Usage at 85% of the 5-hour window · "
                                                                              "starts again"))


if __name__ == "__main__":
    unittest.main()
