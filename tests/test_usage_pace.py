"""The usage meters' pace (README: Header): each bar's fill is red ahead of an even pace, yellow within PACE_WARN
points below it, green further below, with a line where an even pace would be. Runs the page's own pure helpers in
node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- the usage meter's pure helpers", "// ---- end of the usage meter's pure helpers"
# Evaluates each [helper, args] case with the helpers cut out of the page, and prints their results as JSON.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
// The helpers use const, which stays inside eval, so the map is built there too.
const H = eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])) + ";({usagePace, paceWords})");
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([f, args]) => H[f](...args))));
"""
NOW = 1_800_000_000  # epoch s
FIVE, SEVEN = 18000, 604800


def win(key, exact, left):
    """A window of key with exact % used and left seconds until it resets."""
    return {"key": key, "pct": round(exact), "exact": exact, "resetsAt": NOW + left}


@unittest.skipUnless(shutil.which("node"), "needs node")
class UsagePace(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def pace(self, w, now=NOW, brake=None):
        return self.run_js(["usagePace", [w, now * 1000] + ([brake] if brake is not None else [])])[0]

    def test_five_hour_ahead(self):
        p = self.pace(win("five_hour", 60, FIVE / 2))
        self.assertEqual((round(p["pace"], 6), round(p["lead"], 6), p["tone"]), (50, 10, "ahead"))

    def test_seven_day_near(self):
        p = self.pace(win("seven_day", 45, SEVEN / 2))
        self.assertEqual((round(p["pace"], 6), round(p["lead"], 6), p["tone"]), (50, -5, "near"))

    def test_seven_day_ok(self):
        p = self.pace(win("seven_day", 20, SEVEN / 2))
        self.assertEqual((round(p["pace"], 6), round(p["lead"], 6), p["tone"]), (50, -30, "ok"))

    def test_margin_edge_is_near(self):
        self.assertEqual(self.pace(win("seven_day", 40, SEVEN / 2))["tone"], "near")
        self.assertEqual(self.pace(win("seven_day", 39.9, SEVEN / 2))["tone"], "ok")

    def test_five_hour_runs_to_the_brake(self):
        p = self.pace(win("five_hour", 50, FIVE / 2), brake=85)
        self.assertEqual((round(p["pace"], 6), round(p["lead"], 6), p["tone"]), (42.5, 7.5, "ahead"))
        p = self.pace(win("five_hour", 35, FIVE / 2), brake=85)
        self.assertEqual((round(p["lead"], 6), p["tone"]), (-7.5, "near"))  # it would be "ok" against 50
        self.assertEqual(round(self.pace(win("five_hour", 10, FIVE + 60), brake=85)["pace"], 6), 0)
        self.assertAlmostEqual(self.pace(win("five_hour", 10, 1), brake=85)["pace"], 85, places=2)

    def test_brake_off_and_seven_day_unchanged(self):
        self.assertEqual(round(self.pace(win("five_hour", 50, FIVE / 2), brake=0)["pace"], 6), 50)
        p = self.pace(win("seven_day", 45, SEVEN / 2), brake=85)
        self.assertEqual((round(p["pace"], 6), round(p["lead"], 6), p["tone"]), (50, -5, "near"))

    def test_words_use_the_scaled_pace(self):
        p = self.pace(win("five_hour", 60, FIVE / 2), brake=85)
        self.assertEqual(self.run_js(["paceWords", [p]])[0], "Pace 43% · 18 points ahead")

    def test_render_passes_the_brake(self):
        self.assertIn("usagePace(w, Date.now(), u.brake)", PAGE.read_text())

    def test_one_second_before_reset(self):
        p = self.pace(win("five_hour", 10, 1))
        self.assertAlmostEqual(p["pace"], 99.99, places=2)

    def test_start_clamps_to_zero(self):
        self.assertEqual(self.pace(win("five_hour", 0, FIVE + 60))["pace"], 0)

    def test_no_live_window(self):
        self.assertIsNone(self.pace({**win("five_hour", 0, 0), "resetsAt": NOW - 5}))  # it has reset
        self.assertIsNone(self.pace({**win("five_hour", 0, 0), "resetsAt": NOW}))
        self.assertIsNone(self.pace({"key": "five_hour", "pct": 30, "exact": 30}))  # no resetsAt
        self.assertIsNone(self.pace(win("other", 30, 100)))  # unknown length

    def test_words(self):
        self.assertEqual(self.run_js(["paceWords", [{"pace": 50, "lead": 10, "tone": "ahead"}]],
                                     ["paceWords", [{"pace": 50.4, "lead": -5.4, "tone": "near"}]],
                                     ["paceWords", [{"pace": 50, "lead": 0.6, "tone": "ahead"}]]),
                         ["Pace 50% · 10 points ahead", "Pace 50% · 5 points behind", "Pace 50% · on pace"])


class UsagePacePage(unittest.TestCase):
    page = PAGE.read_text()

    def test_bar_holds_the_line(self):
        self.assertIn(".st .bar { position: relative;", self.page)

    def test_pace_colour_wins_on_the_fill(self):
        # The pace classes come after, and are more specific than, the alert and danger fill rules.
        alert = self.page.index(".st.danger .bar i {")
        self.assertGreater(self.page.index(".st .bar i.p-ahead { background: var(--bad); }"), alert)
        self.assertIn(".st .bar i.p-ok { background: var(--ok); }", self.page)

    def test_margin_is_one_constant(self):
        self.assertIn("const PACE_WARN = 10;", self.page)


if __name__ == "__main__":
    unittest.main()
