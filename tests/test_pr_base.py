"""pr_base (README: Branches and pull requests): PRs against a branch other than main, a PR retargeted on GitHub,
and after a merge only the repos whose PR merged into main move main. Uses test_branch_tasks' fake gh and bare
origins. Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_branch_tasks import Base, commit, git, new_repo, opsd  # noqa: E402

FEAT = "feat/admin-console-on-main"


class Config(unittest.TestCase):
    def test_repo_file_key(self):
        keep, _ = opsd.split_repo_config(f'pr_base = "{FEAT}"\n')
        self.assertEqual(keep, {"pr_base": FEAT})
        keep, _ = opsd.split_repo_config(f'pr_base = {{"web" = "{FEAT}"}}\n')
        self.assertEqual(keep, {"pr_base": {"web": FEAT}})
        for bad in ('pr_base = 3\n', 'pr_base = ""\n', 'pr_base = {"web" = 1}\n'):
            with self.assertRaises(ValueError):
                opsd.split_repo_config(bad)


class Plain(Base):
    def setUp(self):
        super().setUp()
        self.repo = new_repo(self.root / "repo")
        self.bare = self.add_origin(self.repo, "r")
        self.feat = self.on_origin(self.bare, "f.txt", "feature work\n", branch=FEAT)
        git(self.repo, "fetch", "-q", "origin")
        self.project(f'flow = "pr"\npr_base = "{FEAT}"\n')

    def open_pr(self, tid):
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        return self.step(tid)

    def test_pr_against_pr_base_and_main_stays(self):
        main = git(self.repo, "rev-parse", "main")
        tid = self.add("Admin page")
        t = self.run_task(tid, {"c.txt": "x\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertTrue(opsd.is_ancestor(self.p.parts[0], self.feat, t["branch"]))  # made from origin/<base>
        self.assertTrue(t["summary"].startswith(f"On branch {t['branch']}: 1 commit."), t["summary"])
        self.assertIn(f"against `{FEAT}`", opsd.worker_prompt(self.p, t))
        self.assertEqual(opsd.pr_against(self.p, t), FEAT)
        opsd.pr_action(self.p, t, "pr-open", {})
        t = self.step(tid)
        self.assertEqual(t["state"], "pr", t["question"])
        self.assertEqual(self.load()["prs"]["o/r#1"]["base"], FEAT)
        self.assertEqual(opsd.pr_rows(tid)[0]["base"], FEAT)
        self.assertIn(f"opened PR #1 against {FEAT}: https://github.com/o/r/pull/1", self.events(tid))
        git(self.repo, "checkout", "-q", "-b", FEAT, f"origin/{FEAT}")  # your checkout is on the base
        self.gh_merge("r", 1)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        merged = self.load()["prs"]["o/r#1"]["merge"]
        self.assertEqual(git(self.repo, "rev-parse", "main"), main)  # main is not involved
        self.assertEqual(git(self.repo, "rev-parse", "dev"), main)
        self.assertEqual(git(self.repo, "rev-parse", FEAT), merged)  # the checked-out base fast-forwarded
        self.assertIn(f"#1 merged into {FEAT}", self.events(tid))
        self.no_force()

    def test_base_that_cant_fast_forward_stays(self):
        git(self.repo, "checkout", "-q", "-b", FEAT, f"origin/{FEAT}")
        mine = commit(self.repo, "l.txt", "local only\n")
        git(self.repo, "checkout", "-q", "main")
        tid = self.add("Other")
        self.open_pr(tid)
        git(self.repo, "checkout", "-q", FEAT)
        self.gh_merge("r", 1)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])  # nothing needs you
        self.assertEqual(git(self.repo, "rev-parse", FEAT), mine)
        self.assertTrue(any("can't fast-forward" in e for e in self.events(tid)))

    def test_task_base_overrides_and_is_for_branch_tasks(self):
        self.on_origin(self.bare, "g.txt", "other\n", branch="feat/other")
        tid = self.add("Mine", pr_base="feat/other")
        self.assertEqual(opsd.task(tid)["pr_base"], "feat/other")
        self.open_pr(tid)
        self.assertEqual(self.load()["prs"]["o/r#1"]["base"], "feat/other")
        with self.assertRaises(ValueError):
            self.add("Bad", pr_base="two words")
        self.p.flow = "dev"
        with self.assertRaises(ValueError):  # merges into dev: no PR to base
            self.add("Plain", pr_base="feat/other")

    def test_retargeted_on_github_is_followed(self):
        self.p.pr_base = None  # against main
        main = git(self.repo, "rev-parse", "main")
        tid = self.add("Retarget")
        self.open_pr(tid)
        self.assertEqual(self.load()["prs"]["o/r#1"]["base"], "main")
        self.state["prs"]["o/r#1"]["base"] = FEAT  # the owner changes the base on GitHub
        self.save()
        opsd.pr_poll()
        self.assertEqual(opsd.pr_rows(tid)[0]["base"], FEAT)
        self.assertIn(f"#1 now goes against {FEAT} (was main): the task follows it", self.events(tid))
        self.assertEqual(opsd.pr_against(self.p, opsd.task(tid)), FEAT)
        self.gh_merge("r", 1)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), main)

    def test_add_request_takes_base(self):
        self.assertEqual(opsd.add_request("x", ["--branch", "f/a", "--base", FEAT, "do", "it"]),
                         ("/api/tasks", {"project": "x", "body": "do it", "branch": "f/a", "base": FEAT}))


class Workspace(Base):
    """evertrue-monorepo's shape: the PR is in one repo; the other has local main commits origin/main lacks."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "ws"
        self.repo.mkdir()
        self.core = new_repo(self.repo / "core")
        self.web = new_repo(self.repo / "web")
        self.add_origin(self.core, "core")
        self.wbare = self.add_origin(self.web, "web")
        self.on_origin(self.wbare, "f.txt", "feature\n", branch=FEAT)
        git(self.web, "fetch", "-q", "origin")
        self.mine = commit(self.core, "local.txt", "not on origin\n")  # core's main is ahead of origin/main
        self.project('flow = "pr"\n', repos=["core", "web"])

    def test_per_repo_base(self):
        self.p.pr_base = {"web": FEAT}
        self.assertEqual([self.p.base_for(r) for r in self.p.parts], ["main", FEAT])
        tid = self.add("Web only")
        self.assertEqual(opsd.pr_against(self.p, opsd.task(tid)), f"core: main, web: {FEAT}")
        self.run_task(tid, {"web/w.txt": "w\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.assertEqual(self.load()["prs"]["o/web#1"]["base"], FEAT)
        self.assertEqual(opsd.pr_against(self.p, opsd.task(tid)), FEAT)  # the PRs it has

    def test_merge_into_main_moves_only_that_repos_main(self):
        tid = self.add("Web fix")
        self.run_task(tid, {"web/w.txt": "w\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.gh_merge("web", 1)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])  # core's own main commits don't block it
        self.assertEqual(git(self.web, "rev-parse", "main"), self.load()["prs"]["o/web#1"]["merge"])
        self.assertEqual(git(self.core, "rev-parse", "main"), self.mine)

    def test_retry_of_a_pr_merged_before_bases_were_kept(self):
        """Task #10 of evertrue-monorepo: retargeted to FEAT on GitHub and merged there, its row has no base, and it
        waits on main can't follow origin. Retry reads the base from GitHub and ends done."""
        tid = self.add("Old")
        self.run_task(tid, {"web/w.txt": "w\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.state = self.load()
        self.state["prs"]["o/web#1"]["base"] = FEAT
        self.save()
        self.gh_merge("web", 1)
        opsd.q("UPDATE prs SET state='MERGED', base=NULL WHERE task_id=?", (tid,))
        opsd.upd(tid, state="needs_input", phase=opsd.PR_MAIN, question="main of core has commits origin/main lacks")
        core_main, web_main = git(self.core, "rev-parse", "main"), git(self.web, "rev-parse", "main")
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual(opsd.pr_rows(tid)[0]["base"], FEAT)
        self.assertEqual((git(self.core, "rev-parse", "main"), git(self.web, "rev-parse", "main")),
                         (core_main, web_main))


if __name__ == "__main__":
    unittest.main()
