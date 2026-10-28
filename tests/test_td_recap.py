"""Today's project filter and a home's Open list in dashboard.html (README TODAY-1, TODAY-2): a project row selects
and filters, the same row again goes back to All; the filter clears when the project leaves view; Open lists to-dos,
tasks, topics and changes in that order, capped at 8 with the rest counted. Runs the page's own pure helpers in
node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Today's project filter and Open list, pure", "// ---- end of Today's project filter"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
const fns = {tdProjKeep, tdByProj, tdProjClick, tdOpenRows, tdFoldRead, tdFoldWrite, tdFoldNext, tdHomeCard};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a => fns[a[0]](...a.slice(1)))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class ProjFilter(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_click_selects_then_unselects(self):
        self.assertEqual(self.run_js(["tdProjClick", None, "a"], ["tdProjClick", "a", "a"], ["tdProjClick", "a", "b"]), [
            {"proj": "a", "home": "a", "key": "", "none": False},
            {"proj": None, "home": None, "key": "", "none": True},
            {"proj": "b", "home": "b", "key": "", "none": False}])

    def test_filter_and_leaving_view(self):
        items = [{"key": "1", "project": "a"}, {"key": "2", "project": "b"}, {"key": "3", "project": "a"}]
        self.assertEqual(self.run_js(["tdByProj", items, "a"], ["tdByProj", items, None],
                                     ["tdProjKeep", "a", ["a", "b"]], ["tdProjKeep", "c", ["a", "b"]]),
                         [[items[0], items[2]], items, "a", None])


@unittest.skipUnless(shutil.which("node"), "needs node")
class OpenList(unittest.TestCase):
    run_js = ProjFilter.run_js
    TODOS = [{"key": "t1", "project": "p", "verb": "Try", "title": "Try it", "since": 5, "source": {"kind": "task", "id": 7}},
             {"key": "t2", "project": "p", "verb": "Do", "title": "Parked", "parked": {"since": 1}},
             {"key": "t3", "project": "q", "verb": "Do", "title": "Other project"}]
    TASKS = [{"id": 9, "project": "p", "state": "queued", "title": "Waits", "depends_on": 8, "created": 3},
             {"id": 8, "project": "p", "state": "running", "title": "Runs", "started": 4},
             {"id": 7, "project": "p", "state": "needs_input", "title": "Has its to-do"},
             {"id": 6, "project": "p", "state": "done", "title": "Done"},
             {"id": 5, "project": "q", "state": "running", "title": "Elsewhere"}]
    RECAP = {"topics": [{"ref": 1, "title": "Shown", "unread": False, "current": True},
                        {"ref": "r:x", "title": "Thread", "unread": True, "current": False}],
             "changes": [{"name": "add-x", "sub": "", "title": "Add X", "done": 2, "total": 5}]}

    def test_order_and_facts(self):
        [o] = self.run_js(["tdOpenRows", "p", self.TODOS, self.TASKS, self.RECAP, 8, False])
        self.assertEqual([(r["kind"], r["chip"], r["title"], r["fact"]) for r in o["rows"]], [
            ("todo", "Try", "Try it", ""),
            ("task", "running", "#8 Runs", ""),
            ("task", "queued", "#9 Waits", "after #8"),
            ("topic", "topic", "Thread", "● unread"),
            ("change", "change", "Add X", "2/5")])
        self.assertEqual(o["more"], 0)
        self.assertEqual([r["act"] for r in o["rows"]], [{"todo": "t1"}, {"task": 8}, {"task": 9}, {"topic": "r:x"},
                                                          {"spec": "add-x", "sub": ""}])

    def test_a_topic_with_its_to_do_is_listed_once(self):
        todos = [{"key": "chat:4:read", "project": "p", "verb": "Read", "title": "Thread", "topic": "r:x"}]
        [o] = self.run_js(["tdOpenRows", "p", todos, [], self.RECAP, 8, False])
        self.assertEqual([(r["kind"], r["title"]) for r in o["rows"]], [("todo", "Thread"), ("change", "Add X")])

    def test_without_recap_yet(self):
        [o] = self.run_js(["tdOpenRows", "p", self.TODOS, self.TASKS, None, 8, False])
        self.assertEqual([r["kind"] for r in o["rows"]], ["todo", "task", "task"])

    def test_cap_and_more(self):
        todos = [{"key": f"k{i}", "project": "p", "verb": "Do", "title": f"T{i}"} for i in range(11)]
        [capped, every] = self.run_js(["tdOpenRows", "p", todos, [], None, 8, False],
                                      ["tdOpenRows", "p", todos, [], None, 8, True])
        self.assertEqual((len(capped["rows"]), capped["more"]), (8, 3))
        self.assertEqual((len(every["rows"]), every["more"]), (11, 0))

    def test_nothing_open(self):
        [o] = self.run_js(["tdOpenRows", "p", [], [], {"topics": [], "changes": []}, 8, False])
        self.assertEqual(o, {"rows": [], "more": 0})


@unittest.skipUnless(shutil.which("node"), "needs node")
class FoldLevels(unittest.TestCase):
    """A plain home card's three fold levels (README TODAY-2)."""
    run_js = ProjFilter.run_js
    PARTS = {"title": "Proj", "status": "1 to-do", "pipe": '<div class="pipe">PIPE</div>',
             "err": '<div class="err">ERR</div>', "recap": '<div class="recapbox">RECAP</div>'}

    def test_cycle_returns_to_the_level_closed_came_from(self):
        f, seen = {"level": "open", "back": "half"}, []
        for _ in range(4):
            [f] = self.run_js(["tdFoldNext", f])
            seen.append((f["level"], self.run_js(["tdFoldWrite", f])[0]))
        self.assertEqual(seen, [("half", "half"), ("closed", "closed:half"), ("half", "half"), ("closed", "closed:half")])
        self.assertEqual(self.run_js(["tdFoldNext", {"level": "closed", "back": "open"}]), [{"level": "open", "back": "open"}])

    def test_read_and_migrate(self):
        self.assertEqual([f["level"] for f in self.run_js(
            ["tdFoldRead", "1"], ["tdFoldRead", "0"], ["tdFoldRead", ""], ["tdFoldRead", None],
            ["tdFoldRead", "open"], ["tdFoldRead", "half"], ["tdFoldRead", "closed:open"], ["tdFoldRead", "closed"])],
            ["half", "open", "open", "open", "open", "half", "closed", "closed"])
        self.assertEqual(self.run_js(["tdFoldRead", "closed:open"], ["tdFoldRead", "closed:half"], ["tdFoldRead", "closed"]),
                         [{"level": "closed", "back": "open"}, {"level": "closed", "back": "half"},
                          {"level": "closed", "back": "half"}])

    def test_what_each_level_shows(self):
        o, h, c = self.run_js(*[["tdHomeCard", lv, self.PARTS] for lv in ("open", "half", "closed")])
        for part in ("Project", "1 to-do", "PIPE", "ERR", "RECAP"):
            self.assertIn(part, o)
        for part in ("Project", "1 to-do", "PIPE", "ERR"):
            self.assertIn(part, h)
        self.assertNotIn("RECAP", h)
        for part in ("Project", "1 to-do", "PIPE", "ERR", "RECAP", "hst", 'class="k"'):
            self.assertNotIn(part, c)
        self.assertIn("Proj", c)
        self.assertIn("tdrecap closed", c)
        self.assertIn("data-back", c)  # ‹ To-do stays usable on a phone
        for html, sym, exp, tip in ((o, "▾", "true", "Fold to the project and its pipeline"),
                                    (h, "▾", "true", "Fold to just the project name"), (c, "▸", "false", "Show the project")):
            self.assertIn(f'aria-expanded="{exp}" title="{tip}">{sym}</button>', html)
            self.assertEqual(html.count("data-recapfold"), 2)  # the chevron and the title row


class TodoHeaderUnchanged(unittest.TestCase):
    """With a to-do selected the compact project header has no fold, and only open keeps the 40% scroll limit."""
    COMPACT = """const html = it ? `<div class="stepcard tdproj"><div class="ptitle"><h2><span>${esc(g.title)}</span></h2>
    <span class="muted hst">${esc(tdHomeStatus(g))}</span></div>
    ${tdPipeline(g, busy, true)}
    ${herr ? `<div class="err" style="margin-top:8px;white-space:pre-wrap">${esc(herr)}</div>` : ""}</div>`
  : recap ? tdHomeCard("""

    def test_compact_header(self):
        page = PAGE.read_text()
        self.assertIn(self.COMPACT, page)
        self.assertIn('classList.toggle("recap", !!recap && lv === "open")', page)

if __name__ == "__main__":
    unittest.main()
