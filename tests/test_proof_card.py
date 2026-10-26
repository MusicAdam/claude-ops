"""The proof video on the Try card and in task details (README Proof video, TODO-1 Try): with a proof the card has
the clip, its caption and a cache-busting ?v=; without one, a muted "no proof video" line with the reason. Runs the
page's own pure helper in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Proof video, pure", "// ---- end of Proof video"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => proofHtml(...a))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class ProofCard(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_with_proof_plays_inline_with_caption(self):
        t = {"id": 7, "proof": True, "proof_note": "Opens <the> menu", "finished": 5,
             "events": [{"ts": 100, "text": "no proof video: timeout"}, {"ts": 200, "text": "proof video recorded: x"}]}
        [html] = self.run_js([t, True])
        self.assertIn('data-src="/api/tasks/7/proof.mp4?v=200"', html)
        for attr in ('preload="none"', "autoplay", "muted", "loop", "playsinline", "controls"):
            self.assertIn(attr, html)
        self.assertNotIn(" src=", html)  # loads only once in view
        self.assertIn("<figcaption>Opens &lt;the&gt; menu</figcaption>", html)

    def test_without_proof_says_so_with_the_reason(self):
        t = {"id": 7, "proof": False, "events": [{"ts": 200, "text": "proof video recorded: x"},
                                                 {"ts": 300, "text": "no proof video: the worker saved no proof.mp4"}]}
        [html] = self.run_js([t, True])
        self.assertNotIn("<video", html)
        self.assertIn('class="proof none">no proof video: the worker saved no proof.mp4<', html)

    def test_without_proof_or_event(self):
        t = {"id": 7, "proof": False, "events": []}
        self.assertEqual(self.run_js([t, True], [t, False]), ['<div class="proof none">no proof video</div>', ""])


if __name__ == "__main__":
    unittest.main()
