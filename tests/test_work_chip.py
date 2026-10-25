"""The header's `N/M working` chip (README "Usage taper"): M is the taper cap while tapering, else Parallel.
Runs the page's pure helper in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- the working chip's pure helper", "// ---- end of the working chip's pure helper"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])) + "; globalThis.workSlots = workSlots");
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([st, s]) => workSlots(st, s))));
"""
S = {"max_parallel": 6, "usage_taper": 50}


@unittest.skipUnless(shutil.which("node"), "needs node")
class WorkChip(unittest.TestCase):
    def slots(self, *states):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps([[st, S] for st in states]), START, END],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_taper_cap_and_plain(self):
        tapering = {"usage": {"taper": {"pct": 63, "cap": 4, "max": 6}}}
        a, b, c = self.slots(tapering, {"usage": {"taper": None}}, {})
        self.assertEqual(a["slots"], 4)
        self.assertIn("5-hour window at 63%, Taper from 50", a["slotTip"])
        self.assertIn("Parallel setting of 6", a["slotTip"])
        self.assertEqual(b["slots"], 6)
        self.assertEqual(c["slots"], 6)
        self.assertIn("(the Parallel setting)", b["slotTip"])

    def test_pace_cap_names_the_pace(self):
        (a,) = self.slots({"usage": {"taper": {"pct": 30, "cap": 3, "max": 6, "ahead": 13}}})
        self.assertEqual(a["slots"], 3)
        self.assertIn("5-hour window at 30%, 13 points ahead of pace", a["slotTip"])
        self.assertNotIn("Taper from", a["slotTip"])


if __name__ == "__main__":
    unittest.main()
