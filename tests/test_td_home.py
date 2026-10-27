"""Today's pick rule in dashboard.html (README TODAY-1, Projects): while a project home is set nothing is
auto-selected; a home that left view clears; the project shown follows the selected item; a step's ✕ opens its
project's home. Runs the page's own pure helper in node; skipped without node.
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
const fns = {tdHomeRule, tdShownProject, tdAfterClose};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => typeof a[0] === "string" && a[0] in fns ? fns[a[0]](...a.slice(1)) : tdHomeRule(...a))));
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

    def test_shown_project_follows_the_item(self):
        a, b = {"key": "k1", "project": "a"}, {"key": "k2", "project": "b"}
        self.assertEqual(self.run_js(["tdShownProject", None, a], ["tdShownProject", None, b],
                                     ["tdShownProject", "c", None], ["tdShownProject", None, None]),
                         [{"name": "a", "how": "with"}, {"name": "b", "how": "with"}, {"name": "c", "how": "sel"}, None])

    def test_close_leads_to_the_home(self):
        [after] = self.run_js(["tdAfterClose", {"key": "todo:3", "project": "ops"}])
        self.assertEqual(after, {"key": "", "home": "ops"})
        # and the home holds: nothing auto-selects, so a reload keeps it
        self.assertEqual(self.run_js([after["home"], ["ops", "x"], False, 4]), [{"home": "ops", "auto": False}])
