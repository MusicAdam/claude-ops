"""A held task records a proof video before review (record_proof): the step's place in the pipeline, its failures,
the queue-wide recording lock, Send back, and GET /api/tasks/<id>/proof.mp4. The worker (run_claude) is stubbed,
except in the timeout test, which runs the real run_claude against a fake claude that never answers.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
    os.environ[k] = "t"
for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
    os.environ[k] = "t@example.com"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 4  # an ftyp box, then bytes to take ranges of


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def events(tid):
    return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.ids = []
        self.addCleanup(self.cleanup)
        for m in (mock.patch.object(opsd, "notify"), mock.patch.object(opsd, "worker_prompt", return_value="brief"),
                  mock.patch.object(opsd, "screen_recording_granted", return_value=True)):
            m.start()
            self.addCleanup(m.stop)

    def cleanup(self):
        for table, col in (("tasks", "id"), ("events", "task_id"), ("task_msgs", "task_id")):
            opsd.q(f"DELETE FROM {table} WHERE {col} IN ({','.join('?' * len(self.ids))})", self.ids)
        for tid in self.ids:  # ids come round again in the next test
            shutil.rmtree(opsd.RUNS / str(tid), ignore_errors=True)

    def bare_task(self, **kw):
        """A task row with a worktree folder, for calling record_proof directly."""
        wt = tempfile.mkdtemp(dir=DATA)
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, hold, worktree) VALUES "
                     "('x', 'held', '', 0, 'gating', 1, ?)", (wt,))
        self.ids.append(tid)
        if kw:
            opsd.upd(tid, **kw)
        return tid


def fake_project(**kw):
    p = mock.Mock(launch="true", stop_cmd=None, running_cmd=None, process=None, env={}, port_for=lambda tid: 47999)
    p.configure_mock(name="x", **kw)
    return p


class HeldPipeline(Base):
    """Real _pipeline runs of a held task in a project with [app] build and launch."""

    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"pv{self.repo.parent.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n'
                             '[app]\nbuild = "true"\nlaunch = "true"\n')
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        self.addCleanup(lambda: (self.toml.unlink(), opsd.load_projects()))

    def run_held(self, record):
        """record(tid): what the recording turn does; returns its result."""
        tid = opsd.add_task(self.p.name, "body", f"task {len(self.ids)}", hold=True)
        self.ids.append(tid)
        opsd.upd(tid, state="running")
        calls, phases = [], []

        def worker(p, tid, prompt, proof=False):
            calls.append(proof)
            if proof:
                return record(tid)
            wt = Path(opsd.task(tid)["worktree"])
            (wt / "a.txt").write_text("b\n")
            git(wt, "commit", "-q", "-am", "edit")
            return {"status": "done", "summary": "edited"}
        real_upd = opsd.upd

        def upd(tid_, **kw):
            if kw.get("phase"):
                phases.append(kw["phase"])
            return real_upd(tid_, **kw)
        with mock.patch.object(opsd, "run_claude", side_effect=worker), mock.patch.object(opsd, "upd", upd):
            opsd._pipeline(self.p, tid)
        return tid, calls, phases

    def test_building_then_recording_then_review(self):
        def proof(tid):
            opsd.proof_files(tid)[0].write_bytes(MP4)
            return {"status": "done", "proof": "the menu opens with the new item"}
        tid, calls, phases = self.run_held(proof)
        t = opsd.task(tid)
        self.assertEqual(t["state"], "review", t["error"])
        self.assertEqual(calls, [False, True])  # the worker's turn, then the recording turn
        self.assertLess(phases.index("building for review"), phases.index("recording proof"))
        self.assertIn("proof video recorded: the menu opens with the new item", events(tid))
        self.assertEqual(opsd.proof_note(tid), "the menu opens with the new item")
        self.assertEqual(git(self.repo, "show", "dev:a.txt"), "a")  # held: nothing merged

    def test_failed_recording_still_reaches_review(self):
        tid, _, _ = self.run_held(lambda tid: {"status": "skipped", "summary": "no Playwright or ffmpeg here"})
        self.assertEqual(opsd.task(tid)["state"], "review")
        self.assertIn("no proof video: no Playwright or ffmpeg here", events(tid))
        self.assertFalse(opsd.proof_files(tid)[0].exists())

    def test_no_file_or_an_error_still_reaches_review(self):
        tid, _, _ = self.run_held(lambda tid: {"status": "done", "proof": "says it did"})
        self.assertEqual(opsd.task(tid)["state"], "review")
        self.assertIn("no proof video: the worker saved no proof.mp4", events(tid))

        def boom(tid):
            raise RuntimeError("claude would not start")
        tid, _, _ = self.run_held(boom)
        self.assertEqual(opsd.task(tid)["state"], "review")
        self.assertIn("no proof video: error: claude would not start", events(tid))

    def test_steer_during_recording_resumes_the_worker(self):
        said = []

        def worker(p, tid_, prompt, proof=False):
            if proof:
                if not said:  # no pipe on a recording turn: the message waits
                    opsd.task_steer(tid_, "also fix the colour")
                    said.append(1)
                return {"status": "skipped", "summary": "nothing to show"}
            wt = Path(opsd.task(tid_)["worktree"])
            (wt / "a.txt").write_text(f"b{len(said)}\n")
            git(wt, "commit", "-q", "-am", "edit")
            if said:  # the steered turn: the message is read, and the task is no longer held for this test
                opsd.q("UPDATE task_msgs SET delivered=? WHERE task_id=?", (time.time(), tid_))
            return {"status": "done", "summary": "edited"}
        tid = opsd.add_task(self.p.name, "body", "steered", hold=True)
        self.ids.append(tid)
        opsd.upd(tid, state="running")
        with mock.patch.object(opsd, "run_claude", side_effect=worker) as rc:
            opsd._pipeline(self.p, tid)
        prompts = [c.args[2] for c in rc.call_args_list if not c.kwargs.get("proof")]
        self.assertEqual(len(prompts), 2)
        self.assertIn("new messages", prompts[1])  # steered_prompt, after the recording
        self.assertEqual(opsd.task(tid)["state"], "review")


class RecordProof(Base):
    def test_native_without_permission_skips_without_a_worker(self):
        tid = self.bare_task()
        with mock.patch.object(opsd, "screen_recording_granted", return_value=False), \
                mock.patch.object(opsd, "run_claude") as rc:
            opsd.record_proof(fake_project(), tid)
        rc.assert_not_called()
        self.assertIn("no proof video: Screen Recording not granted to Claude Ops", events(tid))

    def test_web_app_without_permission_still_records_headless(self):
        tid = self.bare_task()
        with mock.patch.object(opsd, "screen_recording_granted", return_value=False), \
                mock.patch.object(opsd, "run_claude", return_value={"status": "skipped", "summary": "x"}) as rc:
            opsd.record_proof(fake_project(stop_cmd="make stop"), tid)
        self.assertIn("screencapture` would hang", rc.call_args.args[2])

    def test_not_an_mp4_is_dropped(self):
        tid = self.bare_task()

        def turn(p, tid, prompt, **kw):
            opsd.proof_files(tid)[0].write_bytes(b"<html>not a video</html>")
            return {"status": "done", "proof": "x"}
        with mock.patch.object(opsd, "run_claude", side_effect=turn):
            opsd.record_proof(fake_project(), tid)
        self.assertIn("no proof video: proof.mp4 is not an MP4 file", events(tid))
        self.assertFalse(opsd.proof_files(tid)[0].exists())

    def test_note_from_proof_txt(self):
        tid = self.bare_task()

        def turn(p, tid, prompt, **kw):
            video, note = opsd.proof_files(tid)
            video.write_bytes(MP4)
            note.write_text("the dialog closes on Escape\n")
            return None  # saved the video, wrote no result
        with mock.patch.object(opsd, "run_claude", side_effect=turn):
            opsd.record_proof(fake_project(), tid)
        self.assertEqual(opsd.proof_note(tid), "the dialog closes on Escape")

    def test_two_tasks_never_record_at_once(self):
        live, most, lock = [0], [0], threading.Lock()

        def turn(p, tid, prompt, **kw):
            with lock:
                live[0] += 1
                most[0] = max(most[0], live[0])
            time.sleep(0.3)
            with lock:
                live[0] -= 1
            return {"status": "skipped", "summary": "x"}
        a, b = self.bare_task(), self.bare_task()
        with mock.patch.object(opsd, "run_claude", side_effect=turn) as rc:
            th = [threading.Thread(target=opsd.record_proof, args=(fake_project(), tid)) for tid in (a, b)]
            for x in th:
                x.start()
            for x in th:
                x.join()
        self.assertEqual(rc.call_count, 2)
        self.assertEqual(most[0], 1)

    def test_timeout_stops_the_turn(self):
        fake = Path(tempfile.mkdtemp(dir=DATA)) / "claude"
        fake.write_text("#!/bin/sh\nexec sleep 60\n")
        fake.chmod(0o755)
        tid = self.bare_task()
        t0 = time.time()
        with mock.patch.object(opsd, "CLAUDE", str(fake)), mock.patch.object(opsd, "PROOF_MINUTES", 0.02), \
                mock.patch.object(opsd, "guard_settings", return_value="{}"):
            opsd.record_proof(fake_project(), tid)
        self.assertLess(time.time() - t0, 30)
        self.assertIn("no proof video: the recording turn ran past 0.02 min, so it was stopped", events(tid))
        self.assertIsNone(opsd.task(tid)["pid"])


class Api(Base):
    def setUp(self):
        super().setUp()
        opsd.READY.set()  # projects are loaded: the API answers
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def call(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Host": "127.0.0.1", "X-Ops": "1", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def test_serves_whole_and_ranges(self):
        tid = self.bare_task(state="review")
        opsd.proof_files(tid)[0].parent.mkdir(parents=True, exist_ok=True)
        opsd.proof_files(tid)[0].write_bytes(MP4)
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof.mp4")
        self.assertEqual((code, h["Content-Type"], h["Accept-Ranges"], data), (200, "video/mp4", "bytes", MP4))
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof.mp4", {"Range": "bytes=0-1"})
        self.assertEqual((code, data, h["Content-Range"]), (206, MP4[:2], f"bytes 0-1/{len(MP4)}"))
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof.mp4", {"Range": "bytes=100-"})
        self.assertEqual((code, data, h["Content-Length"]), (206, MP4[100:], str(len(MP4) - 100)))
        code, _, data = self.call("GET", f"/api/tasks/{tid}/proof.mp4", {"Range": "bytes=-10"})
        self.assertEqual((code, data), (206, MP4[-10:]))
        code, h, _ = self.call("GET", f"/api/tasks/{tid}/proof.mp4", {"Range": f"bytes={len(MP4)}-"})
        self.assertEqual((code, h["Content-Range"]), (416, f"bytes */{len(MP4)}"))

    def test_404_when_absent(self):
        tid = self.bare_task(state="review")
        self.assertEqual(self.call("GET", f"/api/tasks/{tid}/proof.mp4")[0], 404)

    def test_proof_in_state(self):
        with_video, without = self.bare_task(state="review"), self.bare_task(state="review")
        video, note = opsd.proof_files(with_video)
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(MP4)
        note.write_text("it shows the thing\n")
        tasks = {t["id"]: t for t in opsd.state_payload()["tasks"]}
        self.assertEqual((tasks[with_video]["proof"], tasks[with_video]["proof_note"]), (True, "it shows the thing"))
        self.assertEqual((tasks[without]["proof"], tasks[without]["proof_note"]), (False, None))
        code, _, data = self.call("GET", f"/api/tasks/{with_video}")
        self.assertEqual((code, json.loads(data)["proof"]), (200, True))

    def test_send_back_removes_the_proof(self):
        tid = self.bare_task(state="review")
        video, note = opsd.proof_files(tid)
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(MP4)
        note.write_text("x\n")
        code, _, data = self.call("POST", f"/api/tasks/{tid}/reject", body={"feedback": "make it blue"})
        self.assertEqual(code, 200, data)
        self.assertEqual(opsd.task(tid)["state"], "queued")
        self.assertFalse(video.exists() or note.exists())


if __name__ == "__main__":
    unittest.main()
