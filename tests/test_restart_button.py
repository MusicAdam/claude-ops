"""Settings' Danger zone: the Restart claude-ops button (README: Settings tab). Each click shows its answer on the
button: the first click asks again, and the second shows "Restarting…" at once, then "Restarted ✓" once a daemon
with a new pid answers. Runs the page's own renderRestart in node with a small DOM stub; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
# The stub's innerHTML setter acts as Chrome does: removing the focused element fires blur on it.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const grab = (start, end) => { const a = h.indexOf(start); if (a < 0) throw start; return h.slice(a, h.indexOf(end, a) + end.length); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const document = {activeElement: null};
const byId = {};
const node = () => ({dataset: {}, textContent: "", disabled: false, innerHTML: ""});
const box = {dataset: {}, kids: [], _html: "", builds: 0,
  get innerHTML() { return this._html; },
  set innerHTML(v) {
    this._html = v; this.builds++;
    const gone = this.kids.find(k => k === document.activeElement);
    for (const k of this.kids) delete byId[k.id];
    this.kids = [...v.matchAll(/id="(\w+)"/g)].map(m => Object.assign(node(), {id: m[1]}));
    for (const k of this.kids) byId[k.id] = k;
    if (gone) { document.activeElement = null; gone.onblur && gone.onblur(); }
  }};
const $ = s => s === "#restart" ? box : byId[s.slice(1)];
let release, calls = [];
const api = (path, body) => { calls.push(path); return new Promise((ok, no) => release = {ok, no}); };
let state = {pid: 100, running_deploys: [259]};
const timers = [];
const setTimeout = (f, ms) => timers.push(f), clearTimeout = () => {};
const code = [grab("function put(el, html) {", "\n}\n"), grab("const RESTART = {", "};\n"),
              grab("function restartView(", "\n}\n"), grab("function renderRestart() {", "\n}\n")];
eval(code.join("\n").replace(/^const (\w+) =/gm, "globalThis.$1 ="));
const out = {};
const look = () => ({label: $("#restartbtn").textContent, disabled: $("#restartbtn").disabled,
                     note: $("#restartnote").innerHTML, builds: box.builds});
const click = () => { const b = $("#restartbtn"); document.activeElement = b; return b.onclick(); };
(async () => {
  renderRestart(); out.start = look();
  const first = $("#restartbtn");
  click(); out.first = look();
  out.sameButton = $("#restartbtn") === first;
  renderRestart(); out.afterRefresh = look();     // the 3 s state refresh redraws
  out.calls0 = calls.length;
  const p = click(); out.sending = look(); out.calls = calls.slice();
  renderRestart(); out.sendingRefresh = look();   // /api/state fails while the daemon is down: loadState redraws
  release.ok({}); await p; out.restarting = look();
  out.pidKept = RESTART.pid;
  // loadState sees a new pid
  const s = {pid: 200};
  eval(grab("  if (RESTART.at && s.pid !== RESTART.pid) {", "\n  }\n"));
  renderRestart(); out.restarted = look();
  RESTART.ok -= 6000; renderRestart(); out.back = look();
  // a failed request shows its error and brings the button back
  click(); const q = click(); release.no(new Error("not the launchd service")); await q; out.failed = look();
  // no new pid after 60 s
  click(); const r = click(); release.ok({}); await r; RESTART.at -= 61000; renderRestart(); out.late = look();
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class RestartButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = json.loads(subprocess.run(["node", "-e", RUN, str(PAGE)], capture_output=True, text=True,
                                            check=True).stdout)

    def test_starts_as_restart(self):
        self.assertEqual(self.out["start"]["label"], "Restart claude-ops")
        self.assertFalse(self.out["start"]["disabled"])

    def test_first_click_asks_again_on_the_same_button(self):
        self.assertEqual(self.out["first"]["label"], "Click again to restart")
        self.assertIn("deploy #259 will fail", self.out["first"]["note"])
        self.assertTrue(self.out["sameButton"])
        self.assertEqual(self.out["first"]["builds"], 1)  # never rebuilt, so no blur from a removed button

    def test_refresh_redraw_keeps_the_confirm(self):
        self.assertEqual(self.out["afterRefresh"]["label"], "Click again to restart")
        self.assertEqual(self.out["calls0"], 0)

    def test_second_click_shows_restarting_before_the_request_returns(self):
        self.assertEqual(self.out["calls"], ["/api/restart"])
        self.assertEqual(self.out["sending"]["label"], "Restarting…")
        self.assertTrue(self.out["sending"]["disabled"])
        self.assertEqual(self.out["sendingRefresh"]["label"], "Restarting…")
        self.assertEqual(self.out["restarting"]["label"], "Restarting…")
        self.assertEqual(self.out["pidKept"], 100)

    def test_new_pid_shows_restarted_then_the_button(self):
        self.assertEqual(self.out["restarted"]["label"], "Restarted ✓")
        self.assertEqual(self.out["back"]["label"], "Restart claude-ops")
        self.assertFalse(self.out["back"]["disabled"])

    def test_failed_request_shows_the_error_and_the_button(self):
        self.assertEqual(self.out["failed"]["label"], "Restart claude-ops")
        self.assertFalse(self.out["failed"]["disabled"])
        self.assertIn("not the launchd service", self.out["failed"]["note"])

    def test_no_answer_after_60_s(self):
        self.assertIn("No answer: see ~/.claude-ops/restart.log", self.out["late"]["note"])
        self.assertEqual(self.out["late"]["builds"], 1)


if __name__ == "__main__":
    unittest.main()
