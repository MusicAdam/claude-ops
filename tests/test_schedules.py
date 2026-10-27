"""Recurring jobs (README: ## Recurring jobs): parsing `every`, next_at, due/skip/missed with a fake clock, the
```schedule block in a chat reply, and Run now. Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def local(y, mo, d, h=0, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class InZone(unittest.TestCase):
    """Runs in America/New_York, so the daylight-saving tests mean something wherever they run."""
    def setUp(self):
        self.tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()


class ParseEvery(unittest.TestCase):
    def test_accepted_forms(self):
        for given, tidy in (("day 9:05", "day 09:05"), ("weekday 18:30", "weekday 18:30"), ("Mon 09:00", "mon 09:00"),
                            ("thu,mon 07:15", "mon,thu 07:15"), ("mon, thu 07:15", "mon,thu 07:15"),
                            ("  hour ", "hour"), ("mon,tue,wed,thu,fri,sat,sun 01:00", "day 01:00"),
                            ("mon,tue,wed,thu,fri 01:00", "weekday 01:00")):
            self.assertEqual(opsd.every_text(given), tidy, given)

    def test_rejects_others_naming_the_forms(self):
        for bad in ("", "daily", "mon", "mon 9", "mon 24:00", "mon 09:60", "funday 09:00", "mon,mon 09:00",
                    "hour 09:00", "every monday", "day 9:00pm", None):
            with self.assertRaises(ValueError, msg=bad) as e:
                opsd.parse_every(bad)
            self.assertIn("'mon,thu HH:MM'", str(e.exception))


class NextSlot(InZone):
    def test_day_later_today_and_tomorrow(self):
        self.assertEqual(opsd.next_slot("day 09:00", local(2026, 3, 2, 8)), local(2026, 3, 2, 9))
        self.assertEqual(opsd.next_slot("day 09:00", local(2026, 3, 2, 9)), local(2026, 3, 3, 9))  # strictly after

    def test_week_wraps(self):
        # Sat 2026-03-07 10:00 -> Mon 2026-03-09 09:00; Sun 23:59 -> Mon; Thu 10:00 with mon,thu -> next Mon
        self.assertEqual(opsd.next_slot("mon 09:00", local(2026, 3, 7, 10)), local(2026, 3, 9, 9))
        self.assertEqual(opsd.next_slot("mon 09:00", local(2026, 3, 8, 23, 59)), local(2026, 3, 9, 9))
        self.assertEqual(opsd.next_slot("mon,thu 09:00", local(2026, 3, 5, 10)), local(2026, 3, 9, 9))
        self.assertEqual(opsd.next_slot("mon 09:00", local(2026, 3, 9, 9, 1)), local(2026, 3, 16, 9))
        # across a month and a year end
        self.assertEqual(opsd.next_slot("fri 08:00", local(2026, 12, 31, 12)), local(2027, 1, 1, 8))

    def test_weekday_skips_the_weekend(self):
        self.assertEqual(opsd.next_slot("weekday 09:00", local(2026, 3, 6, 10)), local(2026, 3, 9, 9))

    def test_day_keeps_wall_time_across_dst(self):
        # US clocks go forward on 2026-03-08 and back on 2026-11-01: 09:00 stays 09:00, the gap is 23 or 25 hours
        before = opsd.next_slot("day 09:00", local(2026, 3, 7, 10))
        self.assertEqual(time.localtime(before)[:5], (2026, 3, 8, 9, 0))
        self.assertEqual(before - local(2026, 3, 7, 9), 23 * 3600)
        autumn = opsd.next_slot("day 09:00", local(2026, 10, 31, 10))
        self.assertEqual(time.localtime(autumn)[:5], (2026, 11, 1, 9, 0))
        self.assertEqual(autumn - local(2026, 10, 31, 9), 25 * 3600)

    def test_hour(self):
        self.assertEqual(opsd.next_slot("hour", local(2026, 3, 2, 8, 0)), local(2026, 3, 2, 9))
        self.assertEqual(opsd.next_slot("hour", local(2026, 3, 2, 8, 59) + 59.5), local(2026, 3, 2, 9))
        self.assertEqual(opsd.next_slot("hour", local(2026, 3, 2, 23, 30)), local(2026, 3, 3, 0))
        # spring forward: 02:00 EST is 03:00 EDT, so from 01:30 the next :00 is 30 real minutes away, at 03:00
        t = opsd.next_slot("hour", local(2026, 3, 8, 1, 30))
        self.assertEqual(t - local(2026, 3, 8, 1, 30), 1800)
        self.assertEqual(time.localtime(t)[3:5], (3, 0))


class Fixture(unittest.TestCase):
    """A project and a clean schedules table. No git: add_task only needs the project loaded."""
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        repo = Path(tempfile.mkdtemp(dir=DATA))
        cls.toml = opsd.PROJECTS_DIR / "sched.toml"
        cls.toml.write_text(f'repo = "{repo}"\nworktrees = "{repo / "wt"}"\n')
        opsd.load_projects()

    @classmethod
    def tearDownClass(cls):
        cls.toml.unlink()
        opsd.load_projects()

    def setUp(self):
        opsd.q("DELETE FROM schedules")
        opsd.q("DELETE FROM tasks")

    def tasks(self):
        return opsd.q("SELECT * FROM tasks ORDER BY id")


class DueSkipMissed(Fixture, InZone):
    def setUp(self):
        Fixture.setUp(self)
        InZone.setUp(self)
        self.sid = opsd.add_schedule({"project": "sched", "title": "Check deps", "body": "check them", "every":
                                      "mon 09:00", "model": "sonnet", "effort": "high", "hold": True, "priority": 1},
                                     now=local(2026, 3, 4, 12))  # a Wednesday

    def test_created_with_next_slot(self):
        self.assertEqual(opsd.schedule(self.sid)["next_at"], local(2026, 3, 9, 9))

    def test_not_due_yet(self):
        self.assertEqual(opsd.run_schedules(now=local(2026, 3, 9, 8, 59)), [])

    def test_due_queues_an_ordinary_task_and_moves_next_at(self):
        ids = opsd.run_schedules(now=local(2026, 3, 9, 9, 0) + 1)
        self.assertEqual(len(ids), 1)
        t = opsd.task(ids[0])
        self.assertEqual((t["project"], t["title"], t["body"], t["state"], t["kind"], t["model"], t["effort"],
                          t["hold"], t["priority"]), ("sched", "Check deps", "check them", "queued", "task", "sonnet",
                                                       "high", 1, 1))
        o = opsd.json.loads(t["origin"])
        self.assertEqual((o["kind"], o["id"]), ("schedule", self.sid))
        s = opsd.schedule(self.sid)
        self.assertEqual((s["last_task"], s["next_at"]), (ids[0], local(2026, 3, 16, 9)))
        self.assertEqual(opsd.run_schedules(now=local(2026, 3, 9, 9, 5)), [])  # once per slot

    def test_live_last_task_skips_the_slot(self):
        first = opsd.run_schedules(now=local(2026, 3, 9, 9))[0]
        opsd.upd(first, state="review")
        self.assertEqual(opsd.run_schedules(now=local(2026, 3, 16, 9)), [])
        self.assertEqual(opsd.schedule(self.sid)["next_at"], local(2026, 3, 23, 9))  # the slot passes
        self.assertTrue(any("skipped" in e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=?",
                                                                    (first,))))
        opsd.upd(first, state="done")
        self.assertEqual(len(opsd.run_schedules(now=local(2026, 3, 23, 9))), 1)

    def test_paused_does_not_run(self):
        opsd.q("UPDATE schedules SET paused=1 WHERE id=?", (self.sid,))
        self.assertEqual(opsd.run_schedules(now=local(2026, 3, 9, 9)), [])

    def test_missed_slots_run_once(self):
        # the daemon was down for three Mondays: one task, and next_at is the first slot after now
        now = local(2026, 3, 25, 14)
        self.assertEqual(len(opsd.run_schedules(now=now)), 1)
        self.assertEqual(opsd.schedule(self.sid)["next_at"], local(2026, 3, 30, 9))
        self.assertEqual(opsd.run_schedules(now=now + 5), [])
        self.assertEqual(len(self.tasks()), 1)

    def test_run_now_keeps_next_at(self):
        before = opsd.schedule(self.sid)["next_at"]
        tid = opsd.queue_scheduled(opsd.schedule(self.sid), "Run now")
        s = opsd.schedule(self.sid)
        self.assertEqual((s["last_task"], s["next_at"]), (tid, before))
        self.assertEqual(opsd.task(tid)["title"], "Check deps")

    def test_edit_every_moves_next_at(self):
        opsd.edit_schedule(self.sid, {"every": "day 07:00"}, now=local(2026, 3, 4, 12))
        s = opsd.schedule(self.sid)
        self.assertEqual((s["every"], s["next_at"], s["title"]), ("day 07:00", local(2026, 3, 5, 7), "Check deps"))
        with self.assertRaises(ValueError):
            opsd.edit_schedule(self.sid, {"every": "sometimes"})


class AfterSchedule(Fixture, InZone):
    """SCHED-1: a schedule whose task waits on another schedule's last task."""
    def setUp(self):
        Fixture.setUp(self)
        InZone.setUp(self)
        now = local(2026, 3, 4, 12)
        self.a = opsd.add_schedule({"project": "sched", "title": "Scout", "every": "day 01:00"}, now=now)
        self.s = opsd.add_schedule({"project": "sched", "title": "Ideas", "every": "day 01:00",
                                    "after_schedule": self.a}, now=now)

    def events(self, tid):
        return " | ".join(e["text"] for e in opsd.q("SELECT text FROM events WHERE task_id=?", (tid,)))

    def test_both_due_the_one_waited_on_queues_first(self):
        # S has the lower id here, so the plain next_at, id order would queue it first
        opsd.q("UPDATE schedules SET after_schedule=NULL WHERE id=?", (self.s,))
        opsd.q("UPDATE schedules SET after_schedule=? WHERE id=?", (self.s, self.a))
        ids = opsd.run_schedules(now=local(2026, 3, 5, 1))
        first, second = opsd.schedule(self.s)["last_task"], opsd.schedule(self.a)["last_task"]
        self.assertEqual(ids, [first, second])
        self.assertEqual(opsd.task(second)["depends_on"], first)
        self.assertIn(f"waits on #{first}", self.events(second))

    def test_waits_until_that_task_merges(self):
        ids = opsd.run_schedules(now=local(2026, 3, 5, 1))
        ta, ts = opsd.schedule(self.a)["last_task"], opsd.schedule(self.s)["last_task"]
        self.assertEqual(ids, [ta, ts])
        self.assertEqual(opsd.task(ts)["depends_on"], ta)
        why = opsd.wait_reasons(opsd.q("SELECT * FROM tasks"))
        self.assertIn(f"waiting for #{ta}", why.get(ts, ""))
        opsd.upd(ta, state="done")
        self.assertNotIn(f"waiting for #{ta}", opsd.wait_reasons(opsd.q("SELECT * FROM tasks")).get(ts, ""))

    def test_ended_last_task_means_no_wait(self):
        for state, note in (("done", None), ("failed", "runs anyway"), ("cancelled", "runs anyway")):
            ta = opsd.queue_scheduled(opsd.schedule(self.a), "Run now")
            opsd.upd(ta, state=state)
            ts = opsd.queue_scheduled(opsd.schedule(self.s), "Run now")
            self.assertIsNone(opsd.task(ts)["depends_on"], state)
            if note:
                self.assertIn(note, self.events(ts))
            opsd.upd(ts, state="done")

    def test_no_last_task_or_deleted_schedule_means_no_wait(self):
        self.assertIsNone(opsd.task(opsd.queue_scheduled(opsd.schedule(self.s), "Run now"))["depends_on"])
        opsd.queue_scheduled(opsd.schedule(self.a), "Run now")
        opsd.q("DELETE FROM schedules WHERE id=?", (self.a,))  # by hand: the API also clears after_schedule
        self.assertIsNone(opsd.task(opsd.queue_scheduled(opsd.schedule(self.s), "Run now"))["depends_on"])
        opsd.edit_schedule(self.s, {"title": "Ideas 2"})  # a dangling after_schedule is dropped, not refused
        self.assertIsNone(opsd.schedule(self.s)["after_schedule"])

    def test_a_later_failure_releases_the_wait(self):
        opsd.run_schedules(now=local(2026, 3, 5, 1))
        ta, ts = opsd.schedule(self.a)["last_task"], opsd.schedule(self.s)["last_task"]
        opsd.upd(ta, state="failed")
        opsd.run_schedules(now=local(2026, 3, 5, 2))
        self.assertIsNone(opsd.task(ts)["depends_on"])
        self.assertIn("runs anyway", self.events(ts))

    def test_self_and_cycles_are_refused(self):
        with self.assertRaises(ValueError):
            opsd.edit_schedule(self.s, {"after_schedule": self.s})
        with self.assertRaises(ValueError):
            opsd.edit_schedule(self.a, {"after_schedule": self.s})  # S already waits on A
        c = opsd.add_schedule({"project": "sched", "title": "C", "every": "hour", "after_schedule": self.s})
        with self.assertRaises(ValueError):
            opsd.edit_schedule(self.a, {"after_schedule": c})  # A <- S <- C <- A
        with self.assertRaises(ValueError):
            opsd.add_schedule({"project": "sched", "title": "D", "every": "hour", "after_schedule": 9999})
        self.assertIsNone(opsd.schedule(self.a)["after_schedule"])

    def test_edit_sets_and_clears_it(self):
        opsd.edit_schedule(self.s, {"after_schedule": "none"})
        self.assertIsNone(opsd.schedule(self.s)["after_schedule"])
        opsd.edit_schedule(self.s, {"after_schedule": f"#{self.a}"})
        self.assertEqual(opsd.schedule(self.s)["after_schedule"], self.a)
        view = {v["id"]: v for v in opsd.schedules_view()}[self.s]
        self.assertEqual(view["after_title"], "Scout")

    def test_api_delete_clears_it(self):
        h = opsd.H.__new__(opsd.H)
        h.route(f"/api/schedules/{self.a}/delete", {})
        self.assertIsNone(opsd.schedule(self.s)["after_schedule"])

    def test_cli_and_reply_block(self):
        self.assertEqual(opsd.schedule_request(["add", "-p", "sched", "--every", "day 02:00", "--after-schedule", "1",
                                                "Ideas"]),
                         ("/api/schedules", {"project": "sched", "every": "day 02:00", "after_schedule": "1",
                                             "title": "Ideas"}))
        self.assertEqual(opsd.schedule_request(["edit", "7", "--after-schedule", "none"]),
                         ("/api/schedules/7", {"after_schedule": "none"}))
        h = opsd.H.__new__(opsd.H)
        h.route(*opsd.schedule_request(["edit", str(self.s), "--after-schedule", "none"]))
        self.assertIsNone(opsd.schedule(self.s)["after_schedule"])
        h.route(*opsd.schedule_request(["edit", str(self.s), "--after-schedule", str(self.a)]))
        self.assertEqual(opsd.schedule(self.s)["after_schedule"], self.a)
        reply = f'Ok.\n```schedule\n[{{"title": "Later", "every": "day 03:00", "after_schedule": {self.a}}}]\n```'
        out, note = opsd.schedules_from_reply(opsd.PROJECTS["sched"], reply, 1)
        self.assertEqual(out, "Ok.")
        self.assertEqual(opsd.q("SELECT after_schedule FROM schedules WHERE title='Later'", one=True)["after_schedule"],
                         self.a)


class Api(Fixture):
    def test_routes(self):
        h = opsd.H.__new__(opsd.H)
        sid = h.route("/api/schedules", {"project": "sched", "title": "Nightly", "every": "day 02:00"})["id"]
        with self.assertRaises(ValueError) as e:
            h.route("/api/schedules", {"project": "sched", "title": "x", "every": "nightly"})
        self.assertIn("weekday HH:MM", str(e.exception))
        tid = h.route(f"/api/schedules/{sid}/run", {})["id"]
        self.assertEqual(opsd.schedule(sid)["last_task"], tid)
        h.route(f"/api/schedules/{sid}/pause", {})
        self.assertEqual(opsd.schedule(sid)["paused"], 1)
        h.route(f"/api/schedules/{sid}/resume", {})
        self.assertEqual(opsd.schedule(sid)["paused"], 0)
        h.route(f"/api/schedules/{sid}", {"title": "Nightly check"})
        view = {s["id"]: s for s in opsd.schedules_view()}[sid]
        self.assertEqual((view["title"], view["last_task"], view["last_state"]), ("Nightly check", tid, "queued"))
        h.route(f"/api/schedules/{sid}/delete", {})
        self.assertIsNone(opsd.schedule(sid))


class ReplyBlock(Fixture):
    def test_block_is_made_and_stripped(self):
        p = opsd.PROJECTS["sched"]
        reply = ('Done.\n\n```schedule\n[{"project": "sched", "title": "Deps", "body": "check deps", '
                 '"every": "mon 09:00", "hold": true}, {"title": "Bad", "every": "often"}]\n```\n')
        out, note = opsd.schedules_from_reply(p, reply, 1)
        self.assertEqual(out, "Done.")
        rows = opsd.q("SELECT * FROM schedules")
        self.assertEqual([(r["title"], r["every"], r["hold"]) for r in rows], [("Deps", "mon 09:00", 1)])
        self.assertIn("made schedule #", note)
        self.assertIn("'Bad' was not made", note)

    def test_bad_json(self):
        out, note = opsd.schedules_from_reply(opsd.PROJECTS["sched"], "Hi\n```schedule\n[nope\n```", 1)
        self.assertEqual(out, "Hi")
        self.assertIn("not valid JSON", note)
        self.assertEqual(opsd.q("SELECT * FROM schedules"), [])


class Cli(Fixture):
    """opsd.py schedule add/edit: how a session the guard keeps off the API changes a schedule (chat #94)."""
    def test_add_takes_the_title_from_its_words(self):
        self.assertEqual(opsd.schedule_request(["add", "-p", "sched", "--every", "mon 09:00", "--hold", "Check", "deps"]),
                         ("/api/schedules", {"project": "sched", "every": "mon 09:00", "hold": True, "title": "Check deps"}))

    def test_edit_sends_only_what_it_is_given(self):
        self.assertEqual(opsd.schedule_request(["edit", "#7", "--every", "day 03:00", "--no-hold"]),
                         ("/api/schedules/7", {"every": "day 03:00", "hold": False}))

    def test_bad_command_lines(self):
        for args in (["edit"], ["edit", "x"], ["edit", "3"], ["edit", "3", "stray"], ["add", "sched", "t"]):
            with self.assertRaises(ValueError, msg=args):
                opsd.schedule_request(args)

    def test_body_file_edit_keeps_the_rest(self):
        h = opsd.H.__new__(opsd.H)
        sid = h.route("/api/schedules", {"project": "sched", "title": "Nightly", "body": "old", "every": "day 02:00"})["id"]
        before = opsd.schedule(sid)
        f = Path(tempfile.mkdtemp(dir=DATA)) / "brief.md"
        f.write_text("Pass 1: scout.\n\nPass 2: check.\n")
        h.route(*opsd.schedule_request(["edit", str(sid), "--body-file", str(f)]))
        s = opsd.schedule(sid)
        self.assertEqual(s["body"], "Pass 1: scout.\n\nPass 2: check.")
        self.assertEqual((s["title"], s["every"], s["next_at"]), (before["title"], before["every"], before["next_at"]))

    def test_guard_lets_the_command_through_and_names_it(self):
        self.assertIsNone(opsd.guard_check(f"{opsd.OPSD_CMD} schedule edit 1 --body-file bin/brief.md", port=8788))
        why = opsd.guard_check("curl -X POST http://127.0.0.1:8788/api/schedules/1 -d @b.json", port=8788)
        self.assertIn("schedule edit ID --body-file", why)


if __name__ == "__main__":
    unittest.main()
