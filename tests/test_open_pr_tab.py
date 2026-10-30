"""Open PR on the dashboard (README: Branches and pull requests, To-dos): the click opens a blank tab that goes to the
first PR once the queue has opened it; later PRs and failures show in a toast; a task with open PRs is an In review
step with a link to each. Runs the page's own code in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const grab = (start, end) => { const a = h.indexOf(start); if (a < 0) throw start; return h.slice(a, h.indexOf(end, a)); };
const toasts = [], opened = [];
const toast = (text, o = {}) => toasts.push({text, ...o});
const tab = {closed: false, opener: "me", location: {href: ""}, document: {title: "", body: {}}, close() { this.closed = true; }};
const window = {open: (...a) => { opened.push(a); return tab; }};
let apiFail = null;
const api = async () => { if (apiFail) throw new Error(apiFail); return {ok: true}; };
const P = () => ({}), PR_STEP_TIP = {"pr-address": "address"}, prAddress = t => t.state === "pr";
let state = {tasks: [{id: 10, state: "on_branch", prs: []}, {id: 11, state: "pr", prs: [{number: 3, url: "u3", state: "OPEN"}]}]};
eval((grab("// ---- the PR news's pure helper", "// The branch (and the Jira key") + grab("function tdActions(it) {", "\n// A Try step's"))
  .replace(/^(const|let) (\w+) =/gm, "globalThis.$2 =").replace(/^function (\w+)/gm, "globalThis.$1 = function"));
const out = {};
const pr = (n, name = "") => ({number: n, url: "https://github.com/o/r/pull/" + n, name, state: "OPEN"});
out.seed = prNews(null, [], [{id: 1, state: "pr", prs: [pr(1)]}]);
out.later = prNews({1: [pr(1).url]}, [], [{id: 1, state: "pr", prs: [pr(1), pr(2, "api")]}]);
out.waitFirst = prNews({2: []}, [2], [{id: 2, state: "pr", prs: [pr(5, "web"), pr(6, "api")]}]);
out.stillQueued = prNews({2: []}, [2], [{id: 2, state: "queued", prs: [{number: null, url: null}]}]);
out.failed = prNews({2: []}, [2], [{id: 2, state: "needs_input", question: "gh pr create failed: no auth", prs: []}]);
out.inReview = tdActions({verb: "In review", source: {kind: "task", id: 11}, prs: [pr(3, "web"), pr(4)]}).map(b => [b.a, b.label, !!b.primary]);
(async () => {
  await prOpenCall(10);  // the click: a blank tab at once, then the PR's link when the state has it
  out.blank = opened.length === 1 && tab.document.title.startsWith("Opening");
  prKnown = {};
  prNewsApply({tasks: [{id: 10, state: "pr", prs: [pr(999, "evertrue-web")]}]});
  out.tab = [tab.location.href, tab.opener, tab.closed];
  out.toast1 = toasts.slice();
  toasts.length = 0; opened.length = 0;
  await prOpenCall(11);  // a Retry after its first PR opened: no tab
  out.retryTabs = opened.length;
  apiFail = "task is done: Open PR is for a task on its branch";
  state.tasks[0] = {id: 10, state: "on_branch", prs: []};
  tab.closed = false;
  try { await prOpenCall(10); } catch {}
  out.apiFail = [tab.closed, toasts.slice()];
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class OpenPrTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "-e", RUN, str(PAGE)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.out = json.loads(r.stdout)

    def test_first_state_is_not_news(self):
        self.assertEqual(self.out["seed"]["news"], [])
        self.assertEqual(self.out["seed"]["seen"], {"1": ["https://github.com/o/r/pull/1"]})

    def test_a_later_pr_is_news(self):
        self.assertEqual([n["pr"]["number"] for n in self.out["later"]["news"]], [2])

    def test_waited_task_first_pr_opens_the_rest_are_news(self):
        w = self.out["waitFirst"]
        self.assertEqual([f["pr"]["number"] for f in w["first"]], [5])
        self.assertEqual([n["pr"]["number"] for n in w["news"]], [6])
        self.assertEqual(self.out["stillQueued"]["first"] + self.out["stillQueued"]["fail"], [])

    def test_waited_task_that_stops_reports_ghs_error(self):
        self.assertEqual(self.out["failed"]["fail"], [{"id": 2, "text": "gh pr create failed: no auth"}])

    def test_click_opens_a_blank_tab_that_goes_to_the_pr(self):
        self.assertTrue(self.out["blank"])
        self.assertEqual(self.out["tab"], ["https://github.com/o/r/pull/999", None, False])
        self.assertEqual(self.out["toast1"][0]["links"],
                         [{"text": "Opened evertrue-web PR #999", "url": "https://github.com/o/r/pull/999"}])

    def test_retry_with_a_pr_open_opens_no_tab(self):
        self.assertEqual(self.out["retryTabs"], 0)

    def test_api_error_closes_the_tab_and_toasts_it(self):
        closed, toasts = self.out["apiFail"]
        self.assertTrue(closed)
        self.assertTrue(toasts[0]["err"])
        self.assertIn("Open PR failed: task is done", toasts[0]["text"])

    def test_in_review_step_links_each_pr(self):
        self.assertEqual(self.out["inReview"], [["prlink:0", "Open web PR #3 ↗", True], ["prlink:1", "Open PR #4 ↗", False],
                                                ["pr-address", "Address comments", False], ["details", "Details", False]])

    def test_page_script_parses(self):  # a name declared twice at top level stops the whole page
        js = r'const h = require("fs").readFileSync(process.argv[1], "utf8");' \
             r'for (const m of h.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(m[1]);'
        r = subprocess.run(["node", "-e", js, str(PAGE)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
