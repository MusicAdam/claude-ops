"""Task ids on the dashboard (README: Task ids): every task title carries its "#<id>", drawn by tid(), and a click on
it copies "#<id>" (a stored to-do's "todo:<id>") without also opening the card. Runs the page's own code in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
# Pull single functions out of the page by name and run them with stubs for what they call.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const grab = (start, end) => { const a = h.indexOf(start); if (a < 0) throw start; return h.slice(a, h.indexOf(end, a) + end.length); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const out = {}, list = {dataset: {}};
const $ = s => s === "#list" ? list : null;
const document = {querySelectorAll: () => []};
const P = () => ({title: "Proj"}), archivedP = () => false, modelTag = () => "", trainOf = () => null, inView = () => true;
const branchTag = () => "", prsShort = () => "", prsOf = () => null, prStep = () => null, isBranchTask = () => false;
const took = () => "1m", ago = () => "1m", nowLine = () => "", LABEL = {}, sel = 0, pf = "";
const verbPill = x => `<span class="verb">${x.verb}</span>`, TD = {key: ""};
let state = {now: 100, tasks: [{id: 316, project: "p", title: "Show ids", state: "queued", priority: 0, created: 1, kind: "task"},
                               {id: 7, project: "p", title: "Done one", state: "done", priority: 0, created: 1, kind: "task", finished: 2}]};
const code = [grab("const tid = (id", "`;"), grab("function put(el, html) {", "\n}\n"), grab("function renderList() {", "\n}\n"),
              grab("const tdTid = x =>", ";\n"), grab("const p2 = n =>", "\n};"), grab("const tdRow = x =>", "</div>`;")];
eval(code.join("\n").replace(/^const (\w+) =/gm, "globalThis.$1 ="));  // so the arrows outlive eval's own scope
out.tid = tid(316);
renderList(); out.list = list.innerHTML;
out.taskRow = tdRow({key: "task:316:answer", verb: "Answer", group: "unblock", project: "p", title: "Show ids", source: {kind: "task", id: 316}});
out.todoRow = tdRow({key: "todo:4", verb: "Do", group: "only_me", project: "p", title: "Grant access", source: {kind: "task", id: 316}, todo: 4});
out.parkedRow = tdRow({key: "todo:5", verb: "Do", group: "only_me", project: "p", title: "Try it", source: {kind: "todo", id: 5}, todo: 5,
                       parked: {since: 1, wake_tasks: [283, 290], wake_on: "merged"}});
out.promoteRow = tdRow({key: "project:p:promote", verb: "Promote", group: "ship", project: "p", title: "Promote P", source: {kind: "project", id: "p"}});
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class TaskIdsRendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = json.loads(subprocess.run(["node", "-e", RUN, str(PAGE)], capture_output=True, text=True,
                                            check=True).stdout)

    def test_tid_is_muted_copyable_hash_id(self):
        self.assertEqual(self.out["tid"], '<span class="id tid" data-copyid="#316" title="Copy #316">#316</span>')

    def test_task_list_card_shows_id_before_title(self):
        for i, title in ((316, "Show ids"), (7, "Done one")):
            self.assertRegex(self.out["list"], rf'data-copyid="#{i}"[^>]*>#{i}</span><span class="title">{title}</span>')

    def test_today_item_about_a_task_shows_its_id(self):
        self.assertIn('<span class="t" title="Show ids"><span class="id tid" data-copyid="#316"', self.out["taskRow"])

    def test_stored_todo_shows_its_todo_id_not_a_task_id(self):
        self.assertIn('<span class="t" title="Grant access"><span class="id tid" data-copyid="todo:4" title="Copy todo:4">'
                      'todo:4</span>Grant access', self.out["todoRow"])
        self.assertNotIn('data-copyid="#316"', self.out["todoRow"])

    def test_item_with_no_stored_todo_or_task_shows_no_id(self):
        self.assertNotIn("data-copyid", self.out["promoteRow"])

    def test_parked_wait_names_copyable_ids(self):
        self.assertIn('Waiting on <span class="id tid" data-copyid="#283"', self.out["parkedRow"])
        self.assertIn('data-copyid="#290"', self.out["parkedRow"])


class TaskIdsPage(unittest.TestCase):
    page = PAGE.read_text()

    def test_one_format(self):
        """The old bare id spans are gone: every task id in a title is drawn by tid()."""
        self.assertNotIn('<span class="id">#${t.id}</span>', self.page)
        for where in ('<h2>${tid(t.id)}·', '<button class="chip" data-task="${t.id}">${tid(t.id)}',
                      'class="ptask" data-task="${t.id}" title="Open #${t.id} on the Tasks tab">${tid(t.id)}'):
            self.assertIn(where, self.page)

    def test_copy_click_stops_in_capture_phase(self):
        a = self.page.index('e.target.closest("[data-copyid]")')
        handler = self.page[a:self.page.index("}, true);", a)]
        self.assertIn("e.stopPropagation()", handler)
        self.assertIn("copyText(el.dataset.copyid,", handler)  # the exact text: "#316" or "todo:12"
        a = self.page.index("function copyText(")
        self.assertTrue(re.search(r"navigator\.clipboard", self.page[a:self.page.index("\n}\n", a)]))


if __name__ == "__main__":
    unittest.main()
