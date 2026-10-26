"""Topic tabs and Ended on a phone's Today (README "Today on a phone", PHONE-5) and Undo after an end (README
"Ending topics"), in dashboard.html. The helpers run in node with stubs; skipped without node. The rest is checked
statically. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
page = PAGE.read_text()

# tdTopicsRow with a state of projects and a filter. The case is {projects, pfs}; prints the chips' data-topics.
ROW = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("function tdTopicsRow()"), b = h.indexOf("function tdTopics(", a);
const c = JSON.parse(process.argv[2]);
const esc = s => String(s), inView = n => !c.pfs.length || c.pfs.includes(n), state = {projects: c.projects};
const html = new Function("state", "inView", "esc", h.slice(a, b) + "; return tdTopicsRow();")(state, inView, esc);
console.log(JSON.stringify([...html.matchAll(/data-topics="([^"]*)"/g)].map(m => m[1])));
"""

# tdUndoOffer, then a click on its Undo: which requests it makes. The case is {cv, project, ref, cur}.
UNDO = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("let tdUndoT = 0;"), b = h.indexOf("const TD_UNDO_MS");
const c = JSON.parse(process.argv[2]), calls = [], els = {};
const el = () => ({classList: {add() {}, remove() {}}, innerHTML: "", textContent: ""});
const $ = s => els[s] || (els[s] = el());
const api = async (path, body) => { calls.push([path, body]); return {}; };
const TD = {cv: c.cv}, TD_GONE = new Map([[`chat:${c.ref}:read`, 1]]), esc = s => String(s);
const f = new Function("$", "api", "TD", "TD_GONE", "esc", "refresh", "refreshDesign", "setTimeout", "clearTimeout",
  "let chatKey; const TD_UNDO_MS = 8000;" + h.slice(a, b) + "; return tdUndoOffer;")(
  $, api, TD, TD_GONE, esc, () => {}, async () => {}, () => 1, () => {});
f(c.project, c.ref, "A topic", c.cur);
$("#tdundob").onclick().then(() => console.log(JSON.stringify({calls, gone: TD_GONE.size, text: els["#tdundo"].innerHTML})));
"""


def node(script, case):
    out = subprocess.run(["node", "-e", script, str(PAGE), json.dumps(case)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "needs node")
class TopicsRow(unittest.TestCase):
    """PHONE-5: under the to-do list, a chip per project the filter shows."""
    projects = [{"name": "ops", "title": "Ops"}, {"name": "plugins", "title": "Audio Plugins"},
                {"name": "bad", "title": "Bad", "error": "no repo"}]

    def test_every_project_in_view(self):
        self.assertEqual(node(ROW, {"projects": self.projects, "pfs": []}), ["ops", "plugins"])

    def test_follows_the_filter(self):
        self.assertEqual(node(ROW, {"projects": self.projects, "pfs": ["plugins"]}), ["plugins"])

    def test_nothing_without_projects(self):
        self.assertEqual(node(ROW, {"projects": [], "pfs": []}), [])


@unittest.skipUnless(shutil.which("node"), "needs node")
class Undo(unittest.TestCase):
    def test_reopens_and_makes_current_again(self):
        r = node(UNDO, {"cv": {"project": "ops", "topic": None}, "project": "ops", "ref": 7, "cur": True})
        self.assertEqual(r["calls"], [["/api/conversation/reopen", {"project": "ops", "ref": 7}],
                                      ["/api/conversation", {"project": "ops", "how": "topic", "ref": 7, "ended": None}]])
        self.assertEqual(r["gone"], 0)  # the Read step may come back at once
        self.assertIn("Ended “A topic”", r["text"])

    def test_leaves_a_topic_the_next_item_opened(self):
        r = node(UNDO, {"cv": {"project": "ops", "topic": 9}, "project": "ops", "ref": 7, "cur": True})
        self.assertEqual([c[0] for c in r["calls"]], ["/api/conversation/reopen"])

    def test_other_project_shown_only_reopens(self):
        r = node(UNDO, {"cv": {"project": "beta", "topic": None}, "project": "ops", "ref": "r:abc", "cur": True})
        self.assertEqual(r["calls"], [["/api/conversation/reopen", {"project": "ops", "ref": "r:abc"}]])

    def test_row_end_only_reopens(self):
        r = node(UNDO, {"cv": {"project": "ops", "topic": None}, "project": "ops", "ref": 7, "cur": False})
        self.assertEqual(len(r["calls"]), 1)


class Static(unittest.TestCase):
    def test_row_only_on_a_phone(self):
        self.assertIn(".tdtopics { display: none;", page)
        phone = page[page.index("@media (max-width: 699px) {\n  body.td"):]
        self.assertIn(".tdtopics { display: block; }", phone[:phone.index("\n}\n")])

    def test_chip_opens_the_projects_tabs_in_now(self):
        js = page[page.index("function tdTopics("):]
        js = js[:js.index("\n}\n")]
        for part in ('tdGo("now")', "TD.over = {key: TD.convKey, project}", "TD.menu = false", "tdShowConv()"):
            self.assertIn(part, js)
        # tdGo first: a filter change resets TD.over when the next render picks a new item
        self.assertLess(js.index('tdGo("now")'), js.index("TD.over ="))
        self.assertIn('<div class="k">Topics and Ended</div>', page)

    def test_item_topic_only_in_its_project(self):
        self.assertIn("const tdJump = (it, project) => it && it.topic != null && it.project === project", page)

    def test_menu_button_counts_ended(self):
        self.assertIn("▾${ended.length ? ` ${ended.length}` : \"\"}", page)

    def test_strip_scrolls_sideways_on_a_phone(self):
        phone = page[page.index("@media (max-width: 699px) {\n  body.td"):]
        phone = phone[:phone.index("\n}\n")]
        self.assertIn(".tdscroll { overflow-x: auto; scrollbar-width: none;", phone)
        self.assertIn("#tdmenu { left: 0; width: auto; }", phone)

    def test_every_today_end_offers_undo(self):
        self.assertEqual(len(re.findall(r'id="tdundo"', page)), 1)
        self.assertEqual(page.count("tdUndoOffer(it.project, s.id, it.title, cur)"), 1)  # the step's End topic
        self.assertEqual(page.count('tdUndoOffer(project, body.ref, t ? t.title : "", cur)'), 1)  # End topic, End


if __name__ == "__main__":
    unittest.main()
