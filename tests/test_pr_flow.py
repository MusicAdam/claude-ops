"""flow = "pr" (README: Branches and pull requests): every task and Send to dev is a branch task, named by
default_branch_pattern when given no branch; there is no Promote, and dev follows main. Uses test_branch_tasks' fake
gh and bare origins. Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_branch_tasks import Base, git, new_repo, opsd  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"


class PrFlow(Base):
    def setUp(self):
        super().setUp()
        self.repo = new_repo(self.root / "repo")
        self.bare = self.add_origin(self.repo, "r")
        self.project('flow = "pr"\ndirect_main = true\n')

    def test_every_task_gets_a_branch_at_queue_time(self):
        self.assertEqual(self.p.flow, "pr")
        self.assertFalse(self.p.direct_main)  # Straight to main is off with flow "pr"
        tid = self.add("Fix login crash", auto_release=True)
        t = opsd.task(tid)
        self.assertEqual((t["branch"], t["target"], t["jira"], t["auto_release"]),
                         (f"ops/{tid}-fix-login-crash", f"ops/{tid}-fix-login-crash", None, 0))
        self.assertTrue(opsd.is_branch_task(t))
        self.assertIn(f"queued on branch ops/{tid}-fix-login-crash", self.events(tid))
        # a branch or Jira key you give still wins
        self.assertEqual(opsd.task(self.add(target="feat/mine"))["branch"], "feat/mine")
        self.assertEqual(opsd.task(self.add(jira="BALL-3"))["branch"], "BALL-3-fix-login-crash")
        # default_branch_pattern, and a fallback when it makes no name git accepts
        self.p.default_branch_pattern = "work/{slug}-{id}"
        tid = self.add("Other")
        self.assertEqual(opsd.task(tid)["branch"], f"work/other-{tid}")
        self.p.default_branch_pattern = "bad name {id}"
        tid = self.add("Third")
        self.assertEqual(opsd.task(tid)["branch"], f"ops/{tid}-third")

    def test_task_ends_in_a_pr_and_dev_follows_main(self):
        tid = self.add("Plain task")
        t = self.run_task(tid, {"c.txt": "x\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "dev"), git(self.repo, "rev-parse", "main"))  # no merge into dev
        self.assertEqual(opsd.branch_open(self.p), {"tasks": 1, "prs": 0})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual(t["state"], "pr")
        self.assertEqual(self.load()["prs"]["o/r#1"]["head"], f"ops/{tid}-plain-task")
        self.checks("r", 1, "SUCCESS")
        opsd.pr_poll()
        opsd.pr_view(self.p, opsd.task(tid), opsd.pr_rows(tid)[0])
        self.assertEqual(opsd.branch_open(self.p), {"tasks": 1, "prs": 1})
        t = self.step(tid)
        opsd.pr_action(self.p, t, "pr-merge", {"pr": opsd.pr_rows(tid)[0]["id"]})
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        merged = self.load()["prs"]["o/r#1"]["merge"]
        self.assertEqual(git(self.repo, "rev-parse", "main"), merged)  # your main fast-forwarded to origin/main
        self.assertEqual(git(self.repo, "rev-parse", "dev"), merged)  # dev just follows main
        self.assertEqual(opsd.branch_open(self.p), {"tasks": 0, "prs": 0})

    def test_no_promote(self):
        with self.assertRaises(ValueError):
            opsd.queue_release(self.p)
        self.assertIsNone(opsd.auto_release(self.p))

    def test_send_uncommitted_changes_goes_onto_a_branch(self):
        ahead = self.on_origin(self.bare, "b.txt", "from github\n")  # origin/main moved; your main and dev didn't
        dev = git(self.repo, "rev-parse", "dev")
        (self.repo / "a.txt").write_text("your edit\n")
        tid = opsd.queue_senddev(self.p)
        self.ids.append(tid)
        t = opsd.task(tid)
        self.assertEqual(t["branch"], f"ops/{tid}-send-uncommitted-changes-in-repo-to-a-pr")
        self.assertIn("for a PR against main", t["body"])
        t = self.run_task(tid)
        self.assertEqual(t["state"], "on_branch", t["error"] or t["question"])
        self.assertEqual(git(self.repo, "merge-base", t["branch"], ahead), ahead)  # made from origin/main
        self.assertEqual(git(self.repo, "show", f"{t['branch']}:a.txt"), "your edit")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", t["branch"]),
                         "Your uncommitted changes from " + str(self.repo).replace(str(opsd.HOME), "~", 1))
        self.assertEqual(git(self.repo, "rev-parse", "dev"), dev)  # nothing landed on dev
        self.assertEqual((self.repo / "a.txt").read_text(), "your edit\n")  # nothing cleared from your checkout
        self.assertIn("applied your changes onto origin/main", " ".join(self.events(tid)))
        brief = opsd.senddev_brief(self.p, opsd.task(tid))
        self.assertIn(f"on branch `{t['branch']}` for a pull request against `main`", brief)
        self.assertNotIn("merges it into `dev`", brief)


class DevFlow(Base):
    def setUp(self):
        super().setUp()
        self.repo = new_repo(self.root / "repo")
        self.add_origin(self.repo, "r")
        self.project()

    def test_default_is_dev(self):
        self.assertEqual(self.p.flow, "dev")
        self.assertTrue(self.p.direct_main)
        t = opsd.task(self.add("Plain"))
        self.assertIsNone(t["branch"])
        self.assertFalse(opsd.is_branch_task(t))

    def test_repo_file_keys(self):
        keep, warns = opsd.split_repo_config('flow = "pr"\ndefault_branch_pattern = "x/{id}"\n')
        self.assertEqual(keep, {"default_branch_pattern": "x/{id}"})  # flow is the home toml's only
        self.assertEqual(warns, ["flow is set in the home toml only: ignored"])
        with self.assertRaises(ValueError):
            opsd.split_repo_config('default_branch_pattern = "x/{slug}"\n')  # no {id}: tasks would share a branch


@unittest.skipUnless(shutil.which("node"), "needs node")
class Page(unittest.TestCase):
    """The project header in flow "pr": the open branch tasks and PRs instead of dev → main and Promote."""

    def test_header_counts_instead_of_promote(self):
        js = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const grab = (start, end) => { const a = h.indexOf(start); if (a < 0) throw start; return h.slice(a, h.indexOf(end, a) + end.length); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const files = n => `${n} file${n === 1 ? "" : "s"}`, NEEDS = [], dirtyCount = () => 0;
let state = {tasks: []};
eval([grab("function prFlow(g)", "}"), grab("function branchOpenWords(g) {", "\n}"),
      grab("function projStats(g) {", "\n}")].join("\n"));
const g = {name: "m", flow: "pr", main_branch: "main", dev_branch: "ops-dev", ahead: [1, 2, 3], direct_main: false,
           branch_open: {tasks: 2, prs: 1}, gate: []};
console.log(JSON.stringify({pr: projStats(g), none: branchOpenWords({...g, branch_open: {tasks: 0, prs: 0}}),
                            dev: projStats({...g, flow: "dev"})}));
"""
        out = json.loads(subprocess.run(["node", "-e", js, str(PAGE)], capture_output=True, text=True,
                                        check=True).stdout)
        self.assertIn("2 open branch tasks · 1 PR open", out["pr"])
        self.assertIn("PRs only", out["pr"])
        self.assertNotIn("to promote", out["pr"])
        self.assertEqual(out["none"], "")
        self.assertIn("3 to promote", out["dev"])
        page = PAGE.read_text()
        a = page.index("function tdPipeline(")
        self.assertIn("const promote = prFlow(g) ?", page[a:a + 2000])


if __name__ == "__main__":
    unittest.main()
