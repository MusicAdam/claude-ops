"""A task's proof report (README Proof video and report): runs/<id>/proof/report.md with its clips and pictures.
The proof_report flag, GET /api/tasks/<id>/proof/<name>, the recording turn skipped when a report exists, Send back
removing the folder, OPS_PROOF_DIR in the worker's environment, and the dashboard resolving a report's media.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run as tests.test_proof_report too
from test_proof_video import DATA, MP4, Base, events, fake_project, opsd  # noqa: E402
import test_proof_video  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
REPORT = """# Bug bash

| Item | Platforms | Result | |
|---|---|---|---|
| Save button | iOS | fixed | [go](#save-button) |

## Save button

| Before | After |
|---|---|
| ![before, iOS](before.mp4) | ![after, iOS](./after.mp4) |

0:04 tap Save

![shot](shot.png)
"""


def write_report(tid, report=REPORT):
    d = opsd.proof_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(report)
    (d / "before.mp4").write_bytes(MP4)
    (d / "after.mp4").write_bytes(MP4)
    (d / "shot.png").write_bytes(PNG)
    return d


class Api(Base):
    call = test_proof_video.Api.call

    def setUp(self):  # as test_proof_video.Api: a server on a free port
        super().setUp()
        opsd.READY.set()
        self.srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def test_flag_and_first_clip(self):
        with_report, without = self.bare_task(state="review"), self.bare_task(state="review")
        write_report(with_report)
        tasks = {t["id"]: t for t in opsd.state_payload()["tasks"]}
        self.assertEqual((tasks[with_report]["proof_report"], tasks[with_report]["proof"]), (True, False))
        self.assertEqual(tasks[with_report]["proof_clip"], {"name": "before.mp4", "alt": "before, iOS"})
        self.assertEqual((tasks[without]["proof_report"], tasks[without]["proof_clip"]), (False, None))
        d = json.loads(self.call("GET", f"/api/tasks/{with_report}")[2])
        self.assertEqual((d["proof_report"], d["proof_clip"]["name"]), (True, "before.mp4"))
        self.assertFalse(json.loads(self.call("GET", f"/api/tasks/{without}")[2])["proof_report"])

    def test_serves_the_report_and_media(self):
        tid = self.bare_task(state="review")
        write_report(tid)
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof/report.md")
        self.assertEqual((code, data.decode()), (200, REPORT))
        self.assertTrue(h["Content-Type"].startswith("text/plain"))
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof/shot.png")
        self.assertEqual((code, h["Content-Type"], data), (200, "image/png", PNG))
        code, h, data = self.call("GET", f"/api/tasks/{tid}/proof/after.mp4", {"Range": "bytes=0-3"})
        self.assertEqual((code, h["Content-Type"], data, h["Content-Range"]), (206, "video/mp4", MP4[:4],
                                                                               f"bytes 0-3/{len(MP4)}"))
        code, h, _ = self.call("GET", f"/api/tasks/{tid}/proof/after.mp4", {"Range": f"bytes={len(MP4)}-"})
        self.assertEqual((code, h["Content-Range"]), (416, f"bytes */{len(MP4)}"))

    def test_rejects_paths_hidden_and_other_files(self):
        tid = self.bare_task(state="review")
        d = write_report(tid)
        (d / ".hidden.png").write_bytes(PNG)
        (d / "notes.txt").write_text("x")
        (d.parent / "x.png").write_bytes(PNG)
        (d / "sub").mkdir()
        (d / "sub" / "b.png").write_bytes(PNG)
        for name in ("../x.png", "..%2Fx.png", "sub/b.png", "sub%2Fb.png", ".hidden.png", "notes.txt", "nope.png",
                     "..", "%2E%2E%2Fx.png"):
            self.assertEqual(self.call("GET", f"/api/tasks/{tid}/proof/{name}")[0], 404, name)

    def test_size_limits(self):
        tid = self.bare_task(state="review")
        d = write_report(tid)
        with open(d / "big.mp4", "wb") as f:
            f.truncate(opsd.MEDIA_MAX + 1)
        self.assertEqual(self.call("GET", f"/api/tasks/{tid}/proof/big.mp4")[0], 404)
        (d / "report.md").write_text("x" * (opsd.FILE_MAX + 1))
        self.assertEqual(self.call("GET", f"/api/tasks/{tid}/proof/report.md")[0], 404)
        self.assertEqual(self.call("GET", f"/api/tasks/{tid}/proof/before.mp4")[0], 200)

    def test_send_back_removes_the_folder(self):
        tid = self.bare_task(state="review")
        d = write_report(tid)
        code, _, data = self.call("POST", f"/api/tasks/{tid}/reject", body={"feedback": "again"})
        self.assertEqual(code, 200, data)
        self.assertFalse(d.exists())
        self.assertFalse(opsd.proof_report(tid))


class RecordProof(Base):
    def test_skipped_when_a_report_exists(self):
        tid = self.bare_task()
        write_report(tid)
        with mock.patch.object(opsd, "run_claude") as rc:
            opsd.record_proof(fake_project(), tid)
        rc.assert_not_called()
        self.assertIn("proof report: 2 videos, 1 images", events(tid))
        self.assertTrue(opsd.proof_report(tid))  # kept for review

    def test_runs_without_a_report(self):
        tid = self.bare_task()
        opsd.proof_dir(tid).mkdir(parents=True, exist_ok=True)  # an empty folder, as every worker gets
        def turn(p, tid_, prompt, **kw):
            opsd.proof_files(tid_)[0].write_bytes(MP4)
            return {"status": "done", "proof": "it works"}
        with mock.patch.object(opsd, "run_claude", side_effect=turn) as rc:
            opsd.record_proof(fake_project(), tid)
        rc.assert_called_once()
        self.assertIn("proof video recorded: it works", events(tid))


class WorkerEnv(Base):
    def test_worker_has_its_proof_dir(self):
        out = Path(tempfile.mkdtemp(dir=DATA))
        fake = out / "claude"
        fake.write_text(f'#!/bin/sh\necho "$OPS_PROOF_DIR" > {out}/dir\necho "$@" > {out}/args\n')
        fake.chmod(0o755)
        tid = self.bare_task(state="running")
        with mock.patch.object(opsd, "CLAUDE", str(fake)), mock.patch.object(opsd, "guard_settings", return_value="{}"):
            opsd.run_claude(fake_project(), tid, "brief")
        want = str(opsd.proof_dir(tid))
        self.assertEqual((out / "dir").read_text().strip(), want)
        self.assertIn(f"--add-dir {want}", (out / "args").read_text())
        self.assertTrue(opsd.proof_dir(tid).is_dir())

    def test_brief_names_it(self):
        p = mock.Mock(gate=[], worker_notes="", repos=None, repo=Path("/r"), dev="dev", main="main", title="X",
                      port_for=lambda i: 1, config_blob="")
        t = {"id": 42, "title": "t", "body": "b", "worktree": "/w", "branch": "b"}
        with mock.patch.object(opsd, "result_path", return_value="/w/.ops-result.json"), \
                mock.patch.object(opsd, "peer_brief", return_value=""):
            brief = REAL_WORKER_PROMPT(p, t)  # Base mocks opsd.worker_prompt
        self.assertIn(str(opsd.proof_dir(42)), brief)
        self.assertIn("### Proof report", brief)


REAL_WORKER_PROMPT = opsd.worker_prompt

RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const MD = {"), b = h.indexOf("// A picture in a reply that doesn't load");
const c = h.indexOf("function md(src"), e = h.indexOf("\n}\n", c) + 3;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let state = {data_dir: "/srv/ops"};
eval(h.slice(a, b) + h.slice(c, e));
console.log(JSON.stringify(md(process.argv[2], "pr-", undefined, {proof: 7, lazy: true})));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class Renderer(unittest.TestCase):
    def md(self, text):
        return json.loads(subprocess.run(["node", "-e", RUN, str(PAGE), text], capture_output=True, text=True,
                                         check=True).stdout)

    def test_media_resolve_to_the_proof_route_and_wait(self):
        html = self.md(REPORT)
        self.assertIn('<video class="mdvideo" data-lazy="/api/tasks/7/proof/before.mp4" controls muted playsinline '
                      'preload="none" title="before, iOS"></video>', html)
        self.assertIn('data-lazy="/api/tasks/7/proof/after.mp4"', html)
        self.assertNotIn(" src=\"/api/tasks/7/proof/before.mp4", html)  # loads once in view
        self.assertIn('<img class="mdimg" src="/api/tasks/7/proof/shot.png"', html)
        self.assertIn('id="pr-save-button"', html)
        self.assertIn('href="#pr-save-button"', html)

    def test_paths_out_of_the_folder_stay_as_written(self):
        for url in ("../x.png", "a/b.png", "/x.png", "https://x.test/a.png"):
            html = self.md(f"![p]({url})")
            self.assertIn(f'src="{url}"', html)
            self.assertNotIn("/api/tasks/", html)


if __name__ == "__main__":
    unittest.main()
