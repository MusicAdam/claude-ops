"""dashboard.html: the step's ✕ and ⤢ and the review report (README TODAY-2 the header block, HDR-2, Proof video). Runs the
page's own pure helpers in node (skipped without node): tdCanClose (✕ only for a to-do with no topic), tdTaskOf (the
task a full-screen view loads) and reviewHtml. The wiring is checked statically. Run from the repo root:
/opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
page = PAGE.read_text()
STEP = ("// ---- the tab strip and the step, pure", "// ---- end of the tab strip and the step, pure")
PROOF = ("// ---- Proof video, pure", "// ---- end of Proof video")
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8"), a = JSON.parse(process.argv[3]);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const cut = ([s, e]) => h.slice(h.indexOf(s), h.indexOf(e));
const f = new Function("esc", cut(a[0]) + cut(a[1]) + "; return {tdCanClose, tdTaskOf, reviewHtml};")(esc);
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([n, ...x]) => f[n](...x) ?? null)));
"""

ITEM = {"key": "todo:3", "verb": "Do", "project": "ops", "title": "Grant access", "why": "", "subject": None,
        "source": {"kind": "chat", "id": 4}, "topic": None, "topic_ended": None}
CV = {"project": "ops", "topic": 2, "topics": []}


@unittest.skipUnless(shutil.which("node"), "needs node")
class Pure(unittest.TestCase):
    def js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), json.dumps([STEP, PROOF])],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_close_only_without_a_topic(self):
        self.assertEqual(self.js(["tdCanClose", ITEM, CV], ["tdCanClose", {**ITEM, "topic": 2}, CV],
                                 ["tdCanClose", {**ITEM, "topic": 9}, CV], ["tdCanClose", ITEM, {**CV, "project": "x"}],
                                 ["tdCanClose", ITEM, None]),
                         [True, False, False, False, False])  # here, back, another project, not loaded

    def test_task_of(self):
        review = {**ITEM, "key": "task:7:review", "subject": "task:7", "source": {"kind": "task", "id": 7}}
        raised = {**ITEM, "key": "todo:9", "subject": "task:8", "source": {"kind": "task", "id": 8}}
        waits = {**ITEM, "parked": {"wake_tasks": [12, 13], "since": 1}}
        self.assertEqual(self.js(["tdTaskOf", review], ["tdTaskOf", raised], ["tdTaskOf", waits], ["tdTaskOf", ITEM]),
                         [7, 8, 12, None])

    def test_review_report(self):
        t = {"proof": True, "proof_watch": ["0:05 tap <Nearby>"],
             "review": {"fixed": "The list was empty", "before": "Nothing showed", "after": "Players show",
                        "check": ["Tap Nearby"]}}
        [html] = self.js(["reviewHtml", t])
        for k in ("What was fixed", "Before", "Now", "Check in the video", "0:05 tap &lt;Nearby&gt;"):
            self.assertIn(k, html)
        self.assertNotIn("Tap Nearby", html)  # the watch list wins over the checks
        [html] = self.js(["reviewHtml", {**t, "proof": False, "proof_watch": None}])
        self.assertIn(">Check<", html)
        self.assertIn("<li>Tap Nearby</li>", html)
        self.assertEqual(self.js(["reviewHtml", {"proof": True}], ["reviewHtml", {"review": {}}]), ["", ""])


class Wiring(unittest.TestCase):
    def test_step_buttons_and_full_view(self):
        self.assertIn('data-close title="Close: keep it on your list">✕', page)
        self.assertIn('data-full title="Open full screen">⤢', page)
        self.assertIn('<div id="tdfull"', page)
        self.assertIn('history.pushState({td: "full"', page)  # one history entry on a phone (PHONE-2)
        self.assertIn('TD.full && !dlg) tdFullClose()', page)  # Esc closes it
        self.assertIn("api(`/api/tasks/${id}`)", page)  # loaded on open

    def test_report_on_the_task_page_above_the_feedback(self):
        i = page.index("${reviewHtml(t)}")
        self.assertLess(i, page.index('<textarea id="feedback"'))
        self.assertGreater(i, page.index('${t.state === "review" ? proofHtml(t, t.can_play) : ""}'))


if __name__ == "__main__":
    unittest.main()
