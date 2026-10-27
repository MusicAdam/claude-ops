"""Start later (README: Start later): a queued task waits until its not_before, a local time or the next usage-limit
reset, then takes its normal place in the queue. Stdlib only, no Claude calls: pipeline is stubbed, so a started task
only records its id. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
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
        "usage_noted:seven_day", "paused", "limited_until", "limited_kind")


def local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


NOW = local(2026, 10, 28, 10, 0)  # a Wednesday, 10:00 local time


def reading(pct, resets):
    return json.dumps({"utilization": pct / 100, "resetsAt": resets, "ts": resets - 3600})


class StartAt(unittest.TestCase):
    def at(self, text):
        return opsd.start_at_text(text, NOW)

    def test_iso(self):
        self.assertEqual(self.at("2026-10-28 14:30"), local(2026, 10, 28, 14, 30))
        self.assertEqual(self.at("2026-10-28T14:30"), local(2026, 10, 28, 14, 30))
        self.assertEqual(self.at("2026-11-02 09:00"), local(2026, 11, 2, 9, 0))

    def test_hh_mm_today_or_tomorrow(self):
        self.assertEqual(self.at("14:30"), local(2026, 10, 28, 14, 30))
        self.assertEqual(self.at("9:00"), local(2026, 10, 29, 9, 0))  # passed today: tomorrow

    def test_weekday(self):
        self.assertEqual(self.at("fri 09:00"), local(2026, 10, 30, 9, 0))
        self.assertEqual(self.at("wed 11:00"), local(2026, 10, 28, 11, 0))  # today's slot, still ahead
        self.assertEqual(self.at("Wed 09:00"), local(2026, 11, 4, 9, 0))  # today's passed: next week

    def test_today_and_tomorrow(self):
        self.assertEqual(self.at("today 11:00"), local(2026, 10, 28, 11, 0))
        self.assertEqual(self.at("tomorrow 09:00"), local(2026, 10, 29, 9, 0))
        with self.assertRaisesRegex(ValueError, "in the past"):
            self.at("today 09:00")

    def test_refuses_past_and_unparseable(self):
        for text in ("2026-10-27 09:00", "2026-10-28 09:59"):
            with self.assertRaisesRegex(ValueError, "in the past"):
                self.at(text)
        for text in ("soon", "25:00", "12:75", "someday 09:00", "", "tue"):
            with self.assertRaisesRegex(ValueError, "HH:MM"):  # the message lists the forms
                self.at(text)

    def test_epoch_number(self):
        self.assertEqual(opsd.start_time(NOW + 60, NOW), (NOW + 60, None))
        with self.assertRaisesRegex(ValueError, "in the past"):
            opsd.start_time(NOW - 60, NOW)
        with self.assertRaises(ValueError):
            opsd.start_time(True, NOW)

    def test_clock(self):
        self.assertEqual(opsd.start_clock(local(2026, 10, 28, 15, 1), NOW), "15:01")
        self.assertEqual(opsd.start_clock(local(2026, 11, 3, 9, 0), NOW), "Tue 09:00")
        self.assertEqual(opsd.start_words({"not_before": local(2026, 11, 3, 9, 0)}, NOW), "waits until Tue 09:00")
        self.assertEqual(opsd.start_words({"not_before": NOW + 300, "not_before_why": "5-hour"}, NOW),
                         "waits for the 5-hour reset · 10:05")
        self.assertIsNone(opsd.start_words({"not_before": NOW - 1}, NOW))


class Reset(unittest.TestCase):
    def reset(self, s):
        return opsd.start_time("reset", NOW, s)

    def test_limit_window_first(self):
        s = {"limited_until": str(NOW + 600), "limited_kind": "seven_day", "usage_brake": "85",
             "usage:five_hour": reading(95, NOW + 300)}
        self.assertEqual(self.reset(s), (NOW + 660, "7-day"))

    def test_seven_day_never_brakes(self):
        s = {"usage_brake": "85", "usage:five_hour": reading(50, NOW + 1000),
             "usage:seven_day": reading(90, NOW + 90000)}
        self.assertEqual(self.reset(s), (NOW + 1060, "5-hour"))

    def test_live_five_hour_last(self):
        s = {"usage_brake": "85", "usage:five_hour": reading(30, NOW + 1000),
             "usage:seven_day": reading(40, NOW + 90000)}
        self.assertEqual(self.reset(s), (NOW + 1060, "5-hour"))

    def test_refused_without_a_reading(self):
        for s in ({}, {"usage:five_hour": reading(30, NOW - 10)}, {"usage:seven_day": reading(30, NOW + 9000)}):
            with self.assertRaisesRegex(ValueError, "no usage reading yet: give a time instead"):
                self.reset(s)

    def test_past_limit_is_ignored(self):
        s = {"limited_until": str(NOW - 600), "limited_kind": "five_hour", "usage:five_hour": reading(30, NOW + 1000)}
        self.assertEqual(self.reset(s), (NOW + 1060, "5-hour"))


class AddRequest(unittest.TestCase):
    def test_at_and_after_reset(self):
        self.assertEqual(opsd.add_request("demo", ["--at", "tomorrow 09:00", "fix", "it"]),
                         ("/api/tasks", {"project": "demo", "body": "fix it", "not_before": "tomorrow 09:00"}))
        self.assertEqual(opsd.add_request("demo", ["--after-reset", "--model", "sonnet", "big", "job"]),
                         ("/api/tasks", {"project": "demo", "body": "big job", "not_before": "reset",
                                         "model": "sonnet"}))

    def test_send_cli_passes_them_through(self):
        sent = []

        class Answer:
            def read(self):
                return b'{"said": "ok"}'

        real = opsd.urlopen
        opsd.urlopen = lambda req, timeout=None: (sent.append(json.loads(req.data)), Answer())[1]
        self.addCleanup(setattr, opsd, "urlopen", real)
        opsd.send_cli(["beta", "--at", "fri 09:00", "do", "it"])
        opsd.send_cli(["beta", "--after-reset", "do", "it"])
        self.assertEqual([b["not_before"] for b in sent], ["fri 09:00", "reset"])
        self.assertEqual(sent[0]["text"], "do it")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True, text=True)


class Queue(unittest.TestCase):
    """schedule_once, add_task, queue_from_reply and Start now with a project of our own and nothing else queued."""

    def setUp(self):
        opsd.init_db()
        saved = {k: opsd.setting(k) for k in KEYS}
        self.addCleanup(lambda: [opsd.set_setting(k, v) if v is not None else
                                 opsd.q("DELETE FROM settings WHERE key=?", (k,)) for k, v in saved.items()])
        for k in KEYS:
            opsd.q("DELETE FROM settings WHERE key=?", (k,))
        opsd.set_setting("paused", "0")
        opsd.set_setting("max_parallel", "1")
        repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"sl{repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        others = [r["id"] for r in opsd.q("SELECT id FROM tasks WHERE state='queued'")]  # the shared database's
        opsd.q(f"UPDATE tasks SET state='paused' WHERE id IN ({','.join('?' * len(others))})", others)
        self.addCleanup(lambda: opsd.q(f"UPDATE tasks SET state='queued' WHERE id IN "
                                       f"({','.join('?' * len(others))})", others))
        self.started = []
        self.addCleanup(lambda: opsd.q("DELETE FROM tasks WHERE project=?", (self.name,)))
        real = opsd.pipeline
        opsd.pipeline = self.started.append
        self.addCleanup(setattr, opsd, "pipeline", real)
        threads = dict(opsd.THREADS)
        opsd.THREADS.clear()
        self.addCleanup(lambda: (opsd.THREADS.clear(), opsd.THREADS.update(threads)))

    def add(self, priority=0, not_before=None):
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, priority, not_before) VALUES "
                     "(?, 't', '', 0, 'queued', ?, ?)", (self.name, priority, not_before))
        return tid

    def schedule(self):
        opsd.schedule_once()
        for th in list(opsd.THREADS.values()):
            if hasattr(th, "join"):
                th.join(5)

    def events(self, tid):
        return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def test_future_waits_and_past_runs_in_normal_order(self):
        later = self.add(priority=1, not_before=time.time() + 3600)
        due = self.add(not_before=time.time() - 5)
        plain = self.add()
        self.schedule()
        self.assertEqual(self.started, [due])  # the high-priority one waits; the due one keeps its id order
        self.assertEqual(opsd.task(later)["state"], "queued")
        why = opsd.wait_reasons([opsd.task(i) for i in (later, plain)])
        self.assertTrue(why[later].startswith("waits until "), why[later])
        opsd.q("UPDATE tasks SET not_before=? WHERE id=?", (time.time() - 1, later))
        opsd.THREADS.clear()
        self.schedule()
        self.assertEqual(self.started, [due, later])  # due now: its priority puts it before plain, no further

    def test_add_task_resolves_and_logs(self):
        tid = opsd.add_task(self.name, "x", not_before="tomorrow 09:00")
        lt = time.localtime(time.time() + 86400)
        self.assertEqual(opsd.task(tid)["not_before"], local(lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0))
        self.assertIn("waits until ", self.events(tid)[-1])
        reset = time.time() + 1800
        opsd.set_setting("usage:five_hour", reading(20, reset))
        tid = opsd.add_task(self.name, "y", not_before="reset")
        t = opsd.task(tid)
        self.assertEqual((t["not_before"], t["not_before_why"]), (reset + 60, "5-hour"))
        self.assertIn("waits for the 5-hour reset: starts after ", self.events(tid)[-1])

    def test_add_task_refuses_before_queueing(self):
        n = len(opsd.q("SELECT id FROM tasks WHERE project=?", (self.name,)))
        for v in ("yesterday", time.time() - 10, "reset"):  # no usage reading: reset is refused too
            with self.assertRaises(ValueError):
                opsd.add_task(self.name, "x", not_before=v)
        self.assertEqual(len(opsd.q("SELECT id FROM tasks WHERE project=?", (self.name,))), n)

    def test_new_research_passes_not_before(self):
        reset = time.time() + 1800
        opsd.set_setting("usage:five_hour", reading(20, reset))
        t = opsd.task(opsd.new_research(self.name, {"question": "q", "tier": "T1", "not_before": "reset"})["id"])
        self.assertEqual((t["not_before"], t["not_before_why"]), (reset + 60, "5-hour"))
        out = opsd.new_research(self.name, {"question": "q2", "tier": "T1", "not_before": "tomorrow 09:00"})
        lt = time.localtime(time.time() + 86400)
        self.assertEqual(opsd.task(out["id"])["not_before"], local(lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0))

    def test_new_research_refuses_bad_time_before_queueing(self):
        n = len(opsd.q("SELECT id FROM tasks WHERE project=?", (self.name,)))
        with self.assertRaises(ValueError):
            opsd.new_research(self.name, {"question": "q", "tier": "T1", "not_before": "whenever"})
        self.assertEqual(len(opsd.q("SELECT id FROM tasks WHERE project=?", (self.name,))), n)

    def test_add_request_research_end_to_end(self):
        reset = time.time() + 1800
        opsd.set_setting("usage:five_hour", reading(20, reset))
        path, body = opsd.add_request(self.name, ["--research", "T1", "--after-reset", "q"])
        self.assertTrue(path.endswith("/research"))
        self.assertEqual(opsd.task(opsd.new_research(self.name, body)["id"])["not_before"], reset + 60)

    def test_queue_from_reply(self):
        p = opsd.proj(self.name)
        reply = ('ok\n```queue\n[{"title": "a", "body": "a", "not_before": "tomorrow 09:00"},'
                 ' {"title": "b", "body": "b", "not_before": "whenever"}, {"title": "c", "body": "c"}]\n```')
        text, ids, note = opsd.queue_from_reply(p, reply, 1)
        self.assertEqual(text, "ok")
        self.assertEqual(len(ids), 3)
        a, b, c = (opsd.task(i) for i in ids)
        self.assertGreater(a["not_before"], time.time())
        self.assertIsNone(b["not_before"])  # refused, and said so; it still queues
        self.assertIsNone(c["not_before"])
        self.assertIn("'whenever' is not a time", note)
        self.assertIn("starts as soon as the queue can", note)

    def test_start_now(self):
        tid = opsd.add_task(self.name, "x", not_before=time.time() + 3600)
        self.assertTrue(opsd.start_words(opsd.task(tid)))
        opsd.start_now(tid)
        t = opsd.task(tid)
        self.assertIsNone(t["not_before"])
        self.assertIsNone(opsd.start_words(t))
        self.assertIn("Start now", self.events(tid)[-1])
        with self.assertRaises(ValueError):
            opsd.start_now(tid)  # nothing left to clear
        self.schedule()
        self.assertEqual(self.started, [tid])


if __name__ == "__main__":
    unittest.main()
