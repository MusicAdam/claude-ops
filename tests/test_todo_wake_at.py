"""Wait for… with a time in dashboard.html (README TODO-6, TODAY-2): the chooser's Hide until quick picks and the
parked row's "Hidden until …" words. Runs the page's own helpers in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Wait for…, pure", "// ---- end of Wait for…, pure"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tid = id => `#${id}`;
const state = {tasks: [{id: 283, project: "web", state: "running", title: "Build it"}]};
const P = name => ({name, title: "Web", direct_main: false});
const c = JSON.parse(process.argv[2]);
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])) + `;  // its consts live only in this eval
console.log(JSON.stringify({
  waiting: c.waits.map(tdWaiting),
  box: tdWaitBox("web", tdWaitNew("web", c.cur), ""),
  fresh: tdWaitNew("web"),
}));`);
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class WaitAt(unittest.TestCase):
    def run_js(self, waits, cur):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps({"waits": waits, "cur": cur}), START, END],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_parked_row_words(self):
        at = time.mktime(time.localtime(time.time() + 2 * 86400)[:3] + (9, 0, 0, 0, 0, -1))
        day = time.strftime("%a", time.localtime(at))
        got = self.run_js([{"wake_tasks": [], "wake_at": at}, {"wake_tasks": [283], "wake_on": "merged", "wake_at": at},
                           {"wake_tasks": [283], "wake_on": "released"}, {"wake_tasks": []}], None)["waiting"]
        self.assertEqual(got, [f"Hidden until {day} 09:00", f"Waiting on #283 to merge, not before {day} 09:00",
                               "Waiting on #283 to release", ""])

    def test_chooser_quick_picks(self):
        at = time.mktime((2030, 1, 7, 9, 0, 0, 0, 0, -1))
        got = self.run_js([], {"wake_tasks": [283], "wake_on": "merged", "wake_at": at})
        box = got["box"]
        for label in ("Hide until:", ">Later today<", ">Tomorrow 09:00<", ">Mon 09:00<", 'data-wat ',
                      "until tasks finish or a time comes"):
            self.assertIn(label, box)
        self.assertIn('data-wpick="tomorrow 09:00"', box)
        self.assertIn('value="2030-01-07 09:00"', box)  # the current time, filled in
        self.assertIn('data-wt="283" checked', box)
        self.assertEqual(got["fresh"]["at"], "")


if __name__ == "__main__":
    unittest.main()
