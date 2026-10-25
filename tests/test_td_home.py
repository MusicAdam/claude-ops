"""Today's pick rule in dashboard.html (README TODAY-1, Projects): while a project home is set nothing is
auto-selected; a home that left view clears. Runs the page's own pure helper in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Today's pick rule, pure", "// ---- end of Today's pick rule"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => tdHomeRule(...a))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class HomeRule(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_home_blocks_auto_select(self):
        self.assertEqual(self.run_js(["a", ["a", "b"], False, 3]), [{"home": "a", "auto": False}])

    def test_no_home_auto_selects_only_without_selection(self):
        self.assertEqual(self.run_js([None, ["a"], False, 2], [None, ["a"], True, 2], [None, ["a"], False, 0]),
                         [{"home": None, "auto": True}, {"home": None, "auto": False}, {"home": None, "auto": False}])

    def test_home_out_of_view_falls_back(self):
        self.assertEqual(self.run_js(["c", ["a", "b"], False, 2]), [{"home": None, "auto": True}])
