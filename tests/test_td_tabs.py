"""dashboard.html: Today's centre is always a chat with topic tabs, and a to-do joins a chat only by action or bond
(README TODAY-2: the tab strip, a to-do's topic, selecting a to-do, attaching a to-do). Runs the page's own pure
helpers in node; skipped without node. The wiring is checked statically. Run from the repo root:
/opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
page = PAGE.read_text()
START, END = "// ---- the tab strip and the step, pure", "// ---- end of the tab strip and the step, pure"
# Each case is [function name, ...args]; a taskTitles map stands in for state.tasks in tdNewTabOpen.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const f = new Function(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4]))
  + "; return {tdTabs, tdJump, tdDockMode, tdChipOf, tdNewTabOpen};")();
const fns = {...f, tdNewTabOpen: (it, titles) => f.tdNewTabOpen(it, id => titles[id])};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([f, ...a]) => fns[f](...a) ?? null)));
"""


def topic(i, created, **kw):
    return {"id": i, "title": f"T{i}", "mode": "design", "created": created, "last": created, "state": "idle",
            "unread": False, "ended": None, **kw}


ITEM = {"key": "task:7:review", "verb": "Review", "project": "ops", "title": "Fix the login", "why": "Read it",
        "subject": "task:7", "source": {"kind": "task", "id": 7}, "topic": None, "topic_ended": None}


@unittest.skipUnless(shutil.which("node"), "needs node")
class Pure(unittest.TestCase):
    def js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def tabs(self, cv):
        return self.js(["tdTabs", cv])[0]

    # ---- the strip
    def test_open_topics_oldest_first_new_on_the_right(self):
        cv = {"project": "ops", "topic": 2, "topics": [topic(3, 300), topic(1, 100), topic(2, 200, unread=True),
                                                       topic(4, 50, ended=60), topic("r:x", 250, mode="research")]}
        tabs = self.tabs(cv)
        self.assertEqual([t["id"] for t in tabs], [1, 2, "r:x", 3])  # the ended one is under ▾
        self.assertEqual([t["on"] for t in tabs], [False, True, False, False])
        self.assertTrue(tabs[1]["unread"])

    def test_order_is_stable_when_activity_changes(self):
        a = self.tabs({"project": "ops", "topic": 1, "topics": [topic(1, 100), topic(2, 200)]})
        later = [topic(2, 200, last=999), topic(1, 100)]  # topics() sorts newest activity first
        b = self.tabs({"project": "ops", "topic": 1, "topics": later})
        self.assertEqual([t["id"] for t in a], [t["id"] for t in b])

    def test_pending_tab_last_titled_from_the_state(self):
        tabs = self.tabs({"project": "ops", "topic": None, "title": None, "topics": [topic(1, 100)]})
        self.assertEqual(tabs[-1], {"id": None, "title": "New topic", "on": True, "pending": True, "plain": True})
        tabs = self.tabs({"project": "ops", "topic": None, "title": "#7 Fix the login", "subject": "task:7", "topics": []})
        self.assertEqual((tabs[0]["title"], tabs[0]["plain"]), ("#7 Fix the login", False))

    def test_activity_mark_and_current_ended_topic(self):
        cv = {"project": "ops", "topic": 1, "topics": [topic(1, 100, ended=5, state="thinking")]}
        [t] = self.tabs(cv)
        self.assertTrue(t["busy"])
        self.assertEqual(t["ended"], 5)

    # ---- selecting a to-do
    def test_unbonded_item_leaves_the_tab(self):
        self.assertEqual(self.js(["tdJump", ITEM, "ops"]), [None])

    def test_bonded_item_jumps_and_its_ended_topic_reopens(self):
        it = {**ITEM, "topic": 12, "topic_ended": 1700000000.5}
        self.assertEqual(self.js(["tdJump", it, "ops"]), [{"how": "topic", "ref": 12, "ended": 1700000000.5}])
        self.assertEqual(self.js(["tdJump", {**it, "topic_ended": None}, "ops"]),
                         [{"how": "topic", "ref": 12, "ended": None}])
        self.assertEqual(self.js(["tdJump", it, "other"]), [None])  # another project's conversation is shown

    def test_dock_mode(self):
        cv = {"project": "ops", "topic": 12}
        bonded = {**ITEM, "topic": 12}
        self.assertEqual(self.js(["tdDockMode", ITEM, cv], ["tdDockMode", bonded, cv],
                                 ["tdDockMode", bonded, {**cv, "topic": 13}], ["tdDockMode", bonded, {**cv, "topic": None}],
                                 ["tdDockMode", ITEM, {**cv, "project": "other"}], ["tdDockMode", ITEM, None],
                                 ["tdDockMode", None, cv]),
                         ["attach", "here", "back", "back", "project", "", ""])

    # ---- attaching
    def test_chip(self):
        self.assertEqual(self.js(["tdChipOf", ITEM]), [{"key": "task:7:review", "project": "ops", "verb": "Review",
                                                        "title": "Fix the login", "why": "Read it"}])

    def test_new_tab_titles(self):
        plain = {**ITEM, "key": "todo:3", "subject": None, "title": "Grant Full Disk Access " + "x" * 90,
                 "source": {"kind": None, "id": None}}
        stored = {**ITEM, "key": "todo:4", "title": "Paste the schedule"}  # a to-do a task added: the task's title
        self.assertEqual(self.js(["tdNewTabOpen", ITEM, {"7": "Fix the login"}], ["tdNewTabOpen", stored, {"7": "Keep the doc"}]),
                         [{"how": "subject", "ref": "task:7", "title": "#7 Fix the login"},
                          {"how": "subject", "ref": "task:7", "title": "#7 Keep the doc"}])
        [o] = self.js(["tdNewTabOpen", plain, {}])
        self.assertEqual((o["how"], o["mode"], len(o["title"])), ("new", "design", 80))
        proj = {**ITEM, "key": "project:ops:ship", "subject": "project:ops:ship", "title": "Deploy Ops"}
        self.assertEqual(self.js(["tdNewTabOpen", proj, {}]), [{"how": "subject", "ref": "project:ops:ship", "title": "Deploy Ops"}])


class Wiring(unittest.TestCase):
    def body(self, head):
        a = page.index(head)
        return page[a:page.index("\n}\n", a)]

    def test_opening_a_todo_jumps_only_to_its_topic(self):
        js = self.body("async function loadConv()")
        self.assertIn("const how = tdJump(it, project);", js)
        self.assertNotIn('how: "subject"', js)  # the old rule 2: a to-do opened its subject topic by itself
        self.assertNotIn('how: "chat"', js)  # the old rule 1

    def test_selection_clears_mode_and_another_items_chip(self):
        js = self.body("function tdChanged(")
        self.assertIn("TD.mode = null;", js)
        # ✕ keeps the closed to-do's chip through the move to its project's home
        self.assertIn("if (!TD.chip || TD.chip.key !== (it ? it.key : TD.closed)) TD.chip = null;", js)
        self.assertIn("TD.closed = it.key;", self.body("function tdDockClose("))

    def test_buttons_that_attach(self):
        js = self.body("async function tdDo(")
        self.assertIn('if (a === "reply" || a === "attach") { TD.mode = null; return tdAttach(it); }', js)
        self.assertIn('if (a === "newtab") return tdNewTab(it);', js)
        self.assertIn("tdAttach(TD.item)", self.body("function tdMode("))  # Send back…, Retry with a note…, Answer

    def test_old_parts_are_gone(self):
        for gone in ('id="convtop"', 'id="convmodes"', 'id="tdsecbar"', "earlierOpen", '["conv", "Conversation"]'):
            self.assertNotIn(gone, page)
        for there in ('id="tdstrip"', 'id="saychip"', 'id="tdhomebox"'):
            self.assertIn(there, page)

    def test_step_sits_under_the_project_header_above_the_thread(self):
        a = page.index('<section id="tdcentre">')
        centre = page[a:page.index('class="composer"', a)]
        order = [centre.index(x) for x in ('id="tdstrip"', 'id="tdhomebox"', 'id="tdhead"', 'id="tdtodo"', 'id="msgs"')]
        self.assertEqual(order, sorted(order))
        chat = centre[centre.index('<section id="chat">'):]
        self.assertNotIn('id="td', chat[chat.index('id="msgs"') + 1:])  # nothing between the thread and the composer
        self.assertNotIn("tdstep", page)
        self.assertNotIn("stepcard dock", page)

    def test_no_chat_only_state(self):
        self.assertNotIn("TD.none", page)
        self.assertIn('localStorage.removeItem("ops-td-none")', page)  # a stale value is dropped
        self.assertEqual(page.count("ops-td-none"), 1)

    def test_deploy_lanes_toggle(self):
        self.assertIn('TD.lanes = localStorage.getItem("ops-td-lanes") === "1";', page)
        js = self.body("function tdPipeline(")
        self.assertIn("const tog = compact && nd ?", js)  # no toggle without deploy targets
        self.assertIn("Deploys (${nd})", js)
        self.assertIn("lanes && open ?", js)
        self.assertIn("tdPipeline(g, busy, true)", self.body("function tdHead("))

    def test_fold_is_kept(self):
        self.assertIn('TD.fold = localStorage.getItem("ops-td-dock") === "1";', page)
        self.assertIn('localStorage.setItem("ops-td-dock", TD.fold ? "1" : "0")', page)

    def test_tab_end_refused_while_replying(self):
        js = self.body("function tdStrip()")
        self.assertIn('data-tabx="${id(t)}" ${t.busy ? "disabled" : ""}', js)
        self.assertIn('act("[data-tabx]", "end"', js)


if __name__ == "__main__":
    unittest.main()
