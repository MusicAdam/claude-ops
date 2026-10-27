"""Today's step arrows in dashboard.html (README HDR-2): on a phone ‹ and › open the previous or next to-do in the
list as shown, with no wrap, and never from or to a parked item. Runs the page's own pure helper in node; skipped
without node. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Today's step arrows, pure", "// ---- end of Today's step arrows"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => tdNeighbour(...a))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class Neighbour(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_walks_in_list_order(self):
        order = ["a", "b", "c"]
        self.assertEqual(self.run_js([order, "a", 1], [order, "b", 1], [order, "c", -1], [order, "b", -1]),
                         ["b", "c", "b", "a"])

    def test_no_wrap_at_either_end(self):
        order = ["a", "b", "c"]
        self.assertEqual(self.run_js([order, "c", 1], [order, "a", -1], [["a"], "a", 1], [["a"], "a", -1]),
                         [None, None, None, None])

    def test_missing_key_has_no_neighbour(self):
        # a parked item, or one that left the list, is not in the order: no arrows lead anywhere
        self.assertEqual(self.run_js([["a", "b"], "p", 1], [["a", "b"], "p", -1], [[], "a", 1]), [None, None, None])

    def test_filtered_order_skips_hidden_items(self):
        # the filter is applied before the order is taken: b (another group) is not in it, so a's next is c
        self.assertEqual(self.run_js([["a", "c"], "a", 1], [["a", "c"], "c", -1]), ["c", "a"])


if __name__ == "__main__":
    unittest.main()
