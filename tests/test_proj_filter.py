"""The header's project filter in dashboard.html (README "Today view"): a set of project names, empty for every
project. Runs the page's own pure helpers in node; skipped without node. The Analyze side (project=a,b) is in
test_analyze.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- the project filter's pure helpers", "// ---- end of the project filter's pure helpers"
# Evaluates each [helper, args] case with the helpers cut out of the page, and prints their results as JSON.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
const H = {pfRead, pfHas, pfLabel: (set, titles) => pfLabel(set, n => titles[n] || n), pfPicker};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([f, args]) => H[f](...args))));
"""
PROJECTS = [{"name": n, "title": n.upper()} for n in ("a", "b", "c", "d", "e")]


@unittest.skipUnless(shutil.which("node"), "needs node")
class ProjectFilter(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_reads_old_and_new_values(self):
        self.assertEqual(self.run_js(["pfRead", ['["a","b"]']], ["pfRead", ["claude-ops"]], ["pfRead", [""]],
                                     ["pfRead", [None]], ["pfRead", ["[]"]], ["pfRead", ['["a","a",""]']]),
                         [["a", "b"], ["claude-ops"], [], [], [], ["a"]])

    def test_in_view(self):
        self.assertEqual(self.run_js(["pfHas", [[], "x"]], ["pfHas", [["a"], "a"]], ["pfHas", [["a"], "b"]],
                                     ["pfHas", [["a", "b"], "b"]], ["pfHas", [["a", "b"], "c"]]),
                         [True, True, False, True, False])

    def test_label(self):
        titles = {"a": "Alpha", "b": "Beta", "c": "Gamma", "l": "A rather long project title"}
        self.assertEqual(self.run_js(["pfLabel", [[], titles]], ["pfLabel", [["a"], titles]],
                                     ["pfLabel", [["a", "b"], titles]], ["pfLabel", [["a", "b", "c"], titles]],
                                     ["pfLabel", [["l", "a", "b"], titles]]),
                         ["All projects", "Alpha", "Alpha, Beta", "Alpha, Beta +1", "A rather long project title +2"])

    def test_picker(self):
        none, one, three, three_f = self.run_js(["pfPicker", [[], PROJECTS, "c"]], ["pfPicker", [["b"], PROJECTS, "c"]],
                                                ["pfPicker", [["d", "b", "e"], PROJECTS, "c"]],
                                                ["pfPicker", [["d", "b", "e"], PROJECTS, "e"]])
        names = lambda p: [x["name"] for x in p["list"]]  # noqa: E731
        self.assertEqual((none["show"], names(none), none["def"]), (True, ["a", "b", "c", "d", "e"], "c"))
        self.assertEqual((one["show"], names(one), one["def"]), (False, ["b"], "b"))
        self.assertEqual((three["show"], names(three), three["def"]), (True, ["b", "d", "e"], "b"))
        self.assertEqual(three_f["def"], "e")  # the focused project, when it is ticked


class ProjectFilterPage(unittest.TestCase):
    page = PAGE.read_text()

    def test_switcher_is_a_checklist(self):
        for el in ("proj", "projbtn", "projmenu"):
            self.assertEqual(len(re.findall(f'id="{el}"', self.page)), 1, el)
        self.assertNotIn('<select id="proj"', self.page)
        self.assertIn('localStorage.setItem("ops-proj", JSON.stringify(pfs))', self.page)
        self.assertIn("&project=${encodeURIComponent(pfs.join(\",\"))}", self.page)


if __name__ == "__main__":
    unittest.main()
