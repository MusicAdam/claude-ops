"""Branch tasks and GitHub PRs (README: Branches and pull requests), against a fake gh on PATH and bare local repos
as origin (reached through url.<bare>.insteadOf, so origin's URL still reads as GitHub).
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
    os.environ[k] = "t"
for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
    os.environ[k] = "t@example.com"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

# The fake gh keeps its PRs in $FAKE_GH/state.json and logs every call to $FAKE_GH/calls.log. A merge really moves
# the bare origin's base branch (a squash: the branch's tree on top of it), as GitHub would.
FAKE_GH = r'''#!PYTHON
import json, os, subprocess, sys
d = os.environ["FAKE_GH"]
path = os.path.join(d, "state.json")
st = json.load(open(path))
a = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(json.dumps(a) + "\n")
repo = a[a.index("-R") + 1] if "-R" in a else None
opt = lambda name: a[a.index(name) + 1] if name in a else None
def save():
    json.dump(st, open(path, "w"))
def git(*x):
    return subprocess.run(["git", "-C", st["bare"][repo], *x], capture_output=True, text=True, check=True).stdout.strip()
if a[:2] == ["auth", "status"]:
    sys.exit(st.get("auth", 0))
if a[:2] == ["pr", "list"]:
    print(json.dumps([{"number": int(k.split("#")[1]), "url": v["url"]} for k, v in st["prs"].items()
                      if k.startswith(repo + "#") and v["head"] == opt("--head") and v["state"] == "OPEN"]))
    sys.exit(0)
if a[:2] == ["pr", "create"]:
    n = st["next"]
    st["next"] += 1
    url = f"https://github.com/{repo}/pull/{n}"
    st["prs"][f"{repo}#{n}"] = {"head": opt("--head"), "base": opt("--base"), "title": opt("--title"),
                                "body": opt("--body"), "state": "OPEN", "checks": [], "url": url}
    save()
    print(url)
    sys.exit(0)
if a[:2] == ["run", "view"]:
    print("\n".join(f"log line {i}" for i in range(300)))
    sys.exit(0)
if a[:2] == ["api", "graphql"]:  # the review threads and reviews of one PR
    import re
    qy = opt("-f")[len("query="):]
    m = re.search(r'owner: "([^"]+)", name: "([^"]+)"\) \{ pullRequest\(number: (\d+)', qy)
    pr = st["prs"][f"{m.group(1)}/{m.group(2)}#{m.group(3)}"]
    print(json.dumps({"data": {"repository": {"pullRequest": {
        "author": {"login": "me"}, "reviewThreads": {"nodes": pr.get("threads", [])},
        "reviews": {"nodes": pr.get("reviews", [])}}}}}))
    sys.exit(0)
if a[0] == "api" and a[-1].startswith("reviewers[]="):  # gh api -X POST repos/o/r/pulls/N/requested_reviewers
    if st.get("request_error"):
        print(st["request_error"], file=sys.stderr)
        sys.exit(1)
    _, o, n, _, num, _ = a[a.index("POST") + 1].split("/")
    pr = st["prs"][f"{o}/{n}#{num}"]
    pr["requests"] = pr.get("requests", []) + [a[-1].split("=", 1)[1]]
    save()
    sys.exit(0)
pr = st["prs"][f"{repo}#{a[2]}"]
if a[:2] == ["pr", "view"]:
    print(json.dumps({"state": pr["state"], "mergedAt": pr.get("mergedAt"),
                      "mergeCommit": {"oid": pr["merge"]} if pr.get("merge") else None,
                      "statusCheckRollup": pr["checks"], "mergeable": "MERGEABLE", "url": pr["url"],
                      "author": {"login": "me"}, "reviews": pr.get("reviews", []),
                      "reviewRequests": [{"login": x} for x in pr.get("requests", [])],
                      "baseRefName": pr.get("base") or "main", "reviewDecision": pr.get("decision", "")}))
elif a[:2] == ["pr", "merge"]:
    if pr.get("merge_error"):
        print(pr["merge_error"], file=sys.stderr)
        sys.exit(1)
    base = pr.get("base") or "main"
    oid = git("commit-tree", git("rev-parse", f"refs/heads/{pr['head']}^{{tree}}"), "-p", base, "-m",
              f"{pr['title']} (#{a[2]})")
    git("update-ref", f"refs/heads/{base}", oid)
    if "--delete-branch" in a:
        git("update-ref", "-d", f"refs/heads/{pr['head']}")
    pr.update(state="MERGED", merge=oid, mergedAt="2026-10-29T10:00:00Z", how=a[3])
elif a[:2] == ["pr", "reopen"]:
    pr["state"] = "OPEN"
elif a[:2] == ["pr", "edit"]:
    if "--body" in a:
        pr["body"] = opt("--body")
    if "--add-reviewer" in a:
        pr["requests"] = pr.get("requests", []) + opt("--add-reviewer").split(",")
elif a[:2] == ["pr", "comment"]:
    pr.setdefault("comments", []).append(opt("--body"))
save()
'''.replace("#!PYTHON", "#!" + sys.executable)


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg="edit"):
    (Path(repo) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(repo) / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def new_repo(at):
    at.mkdir(parents=True)
    git(at, "init", "-q", "-b", "main")
    commit(at, "a.txt", "one\n", "init")
    return at


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.root = Path(tempfile.mkdtemp(dir=DATA))
        self.gh_dir = self.root / "gh"
        self.gh_dir.mkdir()
        (self.gh_dir / "gh").write_text(FAKE_GH)
        (self.gh_dir / "gh").chmod(0o755)
        self.state = {"auth": 0, "next": 1, "prs": {}, "bare": {}}
        self.save()
        self.ids, self.pushes = [], []
        real_net = opsd.git_net

        def net(r, *args):  # every git call that talks to origin, so a test can check none forces
            self.pushes.append(list(args))
            return real_net(r, *args)
        patches = [mock.patch.dict(os.environ, {"FAKE_GH": str(self.gh_dir),
                                                "PATH": f"{self.gh_dir}:{os.environ['PATH']}"}),
                   mock.patch.object(opsd, "notify"), mock.patch.object(opsd, "git_net", side_effect=net)]
        for m in patches:
            m.start()
            self.addCleanup(m.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.ids:
            opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids)
            opsd.q(f"DELETE FROM prs WHERE task_id IN ({','.join('?' * len(self.ids))})", self.ids)
            opsd.q(f"DELETE FROM events WHERE task_id IN ({','.join('?' * len(self.ids))})", self.ids)
        for tid in self.ids:
            shutil.rmtree(opsd.RUNS / str(tid), ignore_errors=True)
            opsd.set_setting(f"prready:{tid}", "")
            opsd.set_setting(f"prreadystep:{tid}", "")
            opsd.set_setting(f"prreviewers:{tid}", "")
        self.toml.unlink()
        opsd.load_projects()

    # -- the fake GitHub

    def save(self):
        (self.gh_dir / "state.json").write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads((self.gh_dir / "state.json").read_text())
        return self.state

    def calls(self):
        f = self.gh_dir / "calls.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def checks(self, slug, n, *conclusions):
        """Set PR n's checks: each a conclusion, or None for one still running."""
        self.load()
        self.state["prs"][f"o/{slug}#{n}"]["checks"] = [
            {"__typename": "CheckRun", "name": f"check{i}", "status": "COMPLETED" if c else "IN_PROGRESS",
             "conclusion": c or "", "detailsUrl": f"https://github.com/o/{slug}/actions/runs/7{i}/job/1"}
            for i, c in enumerate(conclusions)]
        self.save()

    def gh_merge(self, slug, n):
        """A merge on GitHub, by someone outside the queue."""
        subprocess.run([str(self.gh_dir / "gh"), "pr", "merge", str(n), "--squash", "-R", f"o/{slug}"],
                       env={**os.environ, "FAKE_GH": str(self.gh_dir)}, check=True)

    def add_origin(self, repo, slug):
        """A bare repo as repo's origin, reached through insteadOf: origin's URL still reads as GitHub."""
        bare = self.root / f"{slug}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        url = f"https://github.com/o/{slug}.git"
        git(repo, "remote", "add", "origin", url)
        git(repo, "config", f"url.{bare}.insteadOf", url)
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q", "origin")
        self.load()
        self.state["bare"][f"o/{slug}"] = str(bare)
        self.save()
        return bare

    def on_origin(self, bare, name, text, branch="main"):
        """A commit pushed to origin from another clone (someone else's work)."""
        c = self.root / f"clone{len(os.listdir(self.root))}"
        subprocess.run(["git", "clone", "-q", str(bare), str(c)], check=True)
        if branch != "main":
            git(c, "checkout", "-q", "-B", branch, f"origin/{branch}" if git(c, "branch", "-r", "--list",
                                                                               f"origin/{branch}") else "main")
        sha = commit(c, name, text, f"{name} from elsewhere")
        git(c, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return sha

    # -- the queue

    def project(self, extra="", repos=None):
        name = f"bt{self.root.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.root / "wt"}"\n'
                             + (f"repos = {json.dumps(repos)}\n" if repos else "") + extra)
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        return self.p

    def add(self, title="Fix login crash", **kw):
        tid = opsd.add_task(self.p.name, "body", title, **kw)
        self.ids.append(tid)
        return tid

    def run_task(self, tid, edits=None, prompt_seen=None):
        """Run tid's pipeline once; its worker (run_claude) writes edits ({path: text}) and commits each repo."""
        opsd.upd(tid, state="running")

        def worker(p, tid, prompt):
            if prompt_seen is not None:
                prompt_seen.append(prompt)
            # run_claude hands the worker your messages, and marks them delivered
            opsd.q("UPDATE task_msgs SET delivered=? WHERE task_id=? AND delivered IS NULL", (time.time(), tid))
            wt = Path(opsd.task(tid)["worktree"])
            for rel, text in (edits or {}).items():
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                (wt / rel).write_text(text)
            for r in p.parts:
                if git(r.at(wt), "status", "--porcelain"):
                    git(r.at(wt), "add", "-A")
                    git(r.at(wt), "commit", "-q", "-m", "BALL-1: edit")
            return {"status": "done", "summary": "edited"}
        with mock.patch.object(opsd, "run_claude", side_effect=worker), \
                mock.patch.object(opsd, "worker_prompt", return_value="brief"):
            opsd._pipeline(self.p, tid)
        return opsd.task(tid)

    def step(self, tid):
        """What the scheduler starts for a queued PR step (start_at 'pr')."""
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "pr"), t["question"])
        opsd.upd(tid, state="running")
        opsd._pipeline(self.p, tid)
        return opsd.task(tid)

    def events(self, tid):
        return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def no_force(self):
        for a in self.pushes:
            self.assertFalse({"--force", "-f", "--force-with-lease"} & set(a), a)
        for a in self.calls():
            self.assertNotIn("--force", a)


class Plain(Base):
    """A one-repo project with a GitHub origin."""

    def setUp(self):
        super().setUp()
        self.repo = new_repo(self.root / "repo")
        self.bare = self.add_origin(self.repo, "r")
        self.project()

    def test_branch_name_from_key_and_pattern(self):
        self.assertEqual(opsd.branch_name(self.p, "Fix login crash!", jira="BALL-123"), "BALL-123-fix-login-crash")
        self.p.branch_pattern = "feature/{key}"
        self.assertEqual(opsd.branch_name(self.p, "x", jira="BALL-9"), "feature/BALL-9")
        self.assertEqual(opsd.branch_name(self.p, "x", target="my/branch"), "my/branch")
        self.assertEqual(opsd.branch_input("BALL-12"), ("BALL-12", None))
        self.assertEqual(opsd.branch_input("feature/ball-12"), (None, "feature/ball-12"))
        for bad in ({"jira": "ball-1"}, {"target": "two words"}, {"target": "main"}, {"target": "task/1-x"}):
            with self.assertRaises(ValueError):
                opsd.branch_name(self.p, "x", **bad)
        self.p.branch_pattern = "{key}-{slug}"
        long = "A " * 40
        self.assertEqual(opsd.branch_name(opsd.PROJECTS[self.p.name], long, jira="KA-1"),
                         "KA-1-" + opsd.slug_of(long))
        tid = self.add(jira="BALL-123")
        t = opsd.task(tid)
        self.assertEqual((t["branch"], t["jira"], t["target"], t["auto_release"]),
                         ("BALL-123-fix-login-crash", "BALL-123", None, 0))

    def test_brief_names_the_branch_and_key(self):
        tid = self.add(jira="BALL-7")
        opsd.upd(tid, worktree=str(self.root / "wt" / f"t{tid}"))
        brief = opsd.worker_prompt(self.p, opsd.task(tid))
        self.assertIn("commit on `BALL-7-fix-login-crash`", brief)
        self.assertIn("Start each commit message with the Jira key `BALL-7`", brief)
        self.assertIn("don't push", brief)
        self.assertNotIn("rebases your branch onto the latest `dev`", brief)

    def test_new_branch_starts_from_origin_main(self):
        ahead = self.on_origin(self.bare, "b.txt", "from github\n")  # origin/main moved; your main didn't
        tid = self.add(target="feat/new")
        t = self.run_task(tid, {"c.txt": "mine\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertEqual(git(self.repo, "merge-base", "feat/new", ahead), ahead)  # made from origin/main
        self.assertEqual(git(self.repo, "rev-parse", "dev"), git(self.repo, "rev-parse", "main"))  # dev untouched
        self.assertTrue(t["summary"].startswith("On branch feat/new: 1 commit. Nothing pushed yet."), t["summary"])
        self.assertIn("BALL-1: edit", t["summary"])
        self.assertFalse(git(self.repo, "ls-remote", "origin", "feat/new"))  # nothing pushed
        wt = Path(t["worktree"])
        self.assertEqual(git(wt, "branch", "--show-current"), "")  # detached: the branch is free for the next task
        self.assertEqual(opsd.q("SELECT COUNT(*) AS n FROM prs WHERE task_id=?", (tid,), one=True)["n"], 0)

    def test_existing_local_branch_is_used(self):
        git(self.repo, "branch", "feat/local")
        git(self.repo, "checkout", "-q", "feat/local")
        mine = commit(self.repo, "l.txt", "local work\n")
        git(self.repo, "checkout", "-q", "main")
        tid = self.add(target="feat/local")
        t = self.run_task(tid, {"c.txt": "more\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "feat/local~1"), mine)
        self.assertIn("worktree", " ".join(self.events(tid)), self.events(tid))

    def test_origin_only_branch_is_tracked_and_not_rebased(self):
        theirs = self.on_origin(self.bare, "o.txt", "on origin\n", branch="feat/remote")
        self.on_origin(self.bare, "m.txt", "main moved\n")  # a rebase would pull this in
        tid = self.add(target="feat/remote")
        t = self.run_task(tid, {"c.txt": "on top\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "feat/remote~1"), theirs)  # pushed: new commits on top
        self.assertEqual(git(self.repo, "config", "branch.feat/remote.merge"), "refs/heads/feat/remote")
        self.assertTrue(any("not rebased" in e for e in self.events(tid)))

    def test_rebase_before_the_first_push_and_never_after(self):
        tid = self.add(target="feat/rb")
        t = self.run_task(tid, {"c.txt": "v1\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        moved = self.on_origin(self.bare, "m.txt", "main moved\n")
        self.run_task(tid, {"c.txt": "v2\n"})  # a message: the worker resumes; never pushed, so it rebases
        self.assertEqual(git(self.repo, "merge-base", "feat/rb", moved), moved)
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual(t["state"], "pr", t["question"])
        pushed = git(self.repo, "rev-parse", "origin/feat/rb")
        later = self.on_origin(self.bare, "m2.txt", "main moved again\n")
        opsd.task_steer(tid, "please also do v3")
        t = self.run_task(tid, {"c.txt": "v3\n"})
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertNotEqual(git(self.repo, "merge-base", "feat/rb", later), later)  # not rebased onto it
        self.assertEqual(git(self.repo, "rev-parse", "feat/rb~1"), pushed)  # on top of what was pushed
        self.assertEqual(git(self.repo, "rev-parse", "origin/feat/rb"), git(self.repo, "rev-parse", "feat/rb"))
        self.no_force()

    def test_second_task_on_the_same_branch_waits(self):
        a = self.add(target="feat/same")
        b = self.add("Second", target="feat/same", depends_on=a)
        opsd.upd(a, state="running")
        self.assertEqual(opsd.branch_holder(opsd.task(b))["id"], a)
        why = opsd.wait_reasons([opsd.task(b)])[b]
        self.assertIn("waiting for #", why)  # its depends_on comes first
        opsd.upd(b, depends_on=None)
        self.assertEqual(opsd.wait_reasons([opsd.task(b)])[b], f"waiting for task #{a} on feat/same")
        self.assertIsNone(opsd.branch_holder(opsd.task(a)))  # a later queued one doesn't hold the first
        opsd.upd(a, state="queued")
        t = self.run_task(a, {"a1.txt": "a\n"})
        self.assertEqual(t["state"], "on_branch")
        self.assertIsNone(opsd.branch_holder(opsd.task(b)))
        opsd.upd(b, depends_on=a)
        self.assertFalse(opsd.dep_waits(opsd.task(b), opsd.task(a)))  # same branch: on branch is enough
        other = self.add("Other", target="feat/other", depends_on=a)
        self.assertTrue(opsd.dep_waits(opsd.task(other), opsd.task(a)))  # anything else waits for its PRs to merge
        t = self.run_task(b, {"b1.txt": "b\n"})  # continues the branch
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertIn("a1.txt", git(self.repo, "ls-tree", "--name-only", "feat/same"))
        self.assertTrue(t["summary"].startswith("On branch feat/same: 2 commits."), t["summary"])

    def test_open_pr_ci_merge_and_main_follows(self):
        tid = self.add(jira="BALL-5")
        self.run_task(tid, {"c.txt": "fix\n"})
        (self.repo / "a.txt").write_text("your edit\n")  # uncommitted in your checkout: carried over
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("pr", "CI running on #1"))  # read at the next poll, not now
        pr = self.load()["prs"]["o/r#1"]
        self.assertEqual((pr["title"], pr["base"], pr["head"]), ("BALL-5: Fix login crash", "main",
                                                                  "BALL-5-fix-login-crash"))
        self.assertIn("On branch BALL-5-fix-login-crash", pr["body"])
        self.assertIn(["push", "-u", "origin", "BALL-5-fix-login-crash"], self.pushes)
        self.checks("r", 1, None)
        opsd.pr_poll()  # still running: nothing moved, nothing to do
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["phase"]), ("pr", "CI running on #1"))
        rows = opsd.pr_view_rows(t, opsd.pr_rows(tid))
        self.assertFalse(rows[0]["merge"])
        self.checks("r", 1, "SUCCESS", "SKIPPED")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertTrue(opsd.pr_view_rows(t, opsd.pr_rows(tid))[0]["merge"])
        opsd.pr_action(self.p, t, "pr-merge", {"pr": opsd.pr_rows(tid)[0]["id"]})
        self.assertEqual(self.load()["prs"]["o/r#1"]["how"], "--squash")
        self.assertIn("--delete-branch", self.calls()[-2] + self.calls()[-1])
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        merged = self.load()["prs"]["o/r#1"]["merge"]
        self.assertEqual(t["merged"], merged)
        self.assertEqual(git(self.repo, "rev-parse", "main"), merged)  # your main fast-forwarded to origin/main
        self.assertEqual((self.repo / "a.txt").read_text(), "your edit\n")
        self.assertTrue(opsd.is_ancestor(self.p.parts[0], merged, "dev"))  # dev took main in
        self.no_force()

    def test_merged_on_github_is_detected(self):
        tid = self.add(target="feat/gh")
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.gh_merge("r", 1)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.load()["prs"]["o/r#1"]["merge"])

    def test_open_prs_stay_on_the_todo_list_until_merged(self):
        tid = self.add(target="feat/td")
        self.run_task(tid, {"c.txt": "x\n"})

        def items():
            return {x["verb"]: x for x in opsd.todos_list([opsd.task(tid)], [], {}) if x["source"]["id"] == tid}
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        it = items()["In review"]  # watching: In review, with the PR's link, CI and review stage
        self.assertEqual((it["group"], it["key"]), ("in_review", f"task:{tid}:inreview"))
        self.assertEqual([x["number"] for x in it["prs"]], [1])
        self.assertIn("PR #1: CI pending", it["why"])
        self.checks("r", 1, "FAILURE")  # red CI needs you: an Answer, not In review
        opsd.pr_poll()
        self.step(tid)
        self.assertEqual(set(items()), {"Answer"})
        self.checks("r", 1, "SUCCESS")
        opsd.pr_poll()
        self.step(tid)
        self.assertEqual(set(items()), {"In review"})
        self.gh_merge("r", 1)
        opsd.pr_poll()
        self.assertEqual(self.step(tid)["state"], "done")
        self.assertEqual(set(items()), set())

    def test_ci_red_needs_you_and_fix_ci_resumes_the_worker(self):
        tid = self.add(target="feat/ci")
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.checks("r", 1, "SUCCESS", "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_RED))
        self.assertIn("check1", t["question"])
        opsd.pr_poll()  # still red: nothing to do
        self.assertEqual(opsd.task(tid)["state"], "needs_input")
        with self.assertRaises(ValueError):
            opsd.pr_action(self.p, opsd.task(tid), "pr-merge", {})
        opsd.pr_action(self.p, opsd.task(tid), "fix-ci", {})
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))
        self.assertIn("log line 299", t["next_prompt"])
        self.assertNotIn("log line 99\n", t["next_prompt"])  # the last 200 lines
        self.assertIn(["run", "view", "71", "--log-failed", "-R", "o/r"], self.calls())
        seen = []
        before = git(self.repo, "rev-parse", "origin/feat/ci")
        t = self.run_task(tid, {"c.txt": "fixed\n"}, seen)
        self.assertIn("log line 299", seen[0])
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "feat/ci~1"), before)
        self.assertEqual(git(self.repo, "rev-parse", "origin/feat/ci"), git(self.repo, "rev-parse", "feat/ci"))
        self.assertEqual(opsd.pr_rows(tid)[0]["ci"], "pending")  # checks run again on the new push...
        self.checks("r", 1)  # ... and none counts as green
        opsd.pr_poll()
        self.assertEqual(self.step(tid)["phase"], "ready to merge: #1")
        self.no_force()

    def test_closed_without_merging(self):
        tid = self.add(target="feat/closed")
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        self.load()["prs"]["o/r#1"]["state"] = "CLOSED"
        self.save()
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_CLOSED))
        self.assertIn("PR #1 was closed without merging", t["question"])
        opsd.pr_action(self.p, t, "pr-retry", {})
        self.assertEqual(self.load()["prs"]["o/r#1"]["state"], "OPEN")
        self.assertEqual(self.step(tid)["state"], "pr")

    def test_merge_failure_shows_ghs_message(self):
        tid = self.add(target="feat/mf")
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        opsd.pr_poll()  # no checks: green
        self.step(tid)
        self.load()["prs"]["o/r#1"]["merge_error"] = "Pull request is not mergeable: review required"
        self.save()
        with self.assertRaisesRegex(ValueError, "review required"):
            opsd.pr_action(self.p, opsd.task(tid), "pr-merge", {})
        self.assertIn("review required", opsd.pr_view_rows(opsd.task(tid), opsd.pr_rows(tid))[0]["error"])

    def test_gh_missing_or_logged_out_needs_you(self):
        tid = self.add(target="feat/auth")
        self.run_task(tid, {"c.txt": "x\n"})
        self.load()["auth"] = 1
        self.save()
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_SETUP))
        self.assertIn("gh auth login", t["question"])
        self.assertFalse(git(self.repo, "ls-remote", "origin", "feat/auth"))  # nothing pushed
        self.load()["auth"] = 0
        self.save()
        opsd.pr_action(self.p, t, "pr-open", {})  # Retry
        self.assertEqual(self.step(tid)["state"], "pr")

    def test_task_with_neither_field_is_unchanged(self):
        tid = self.add("Plain task")
        t = self.run_task(tid, {"c.txt": "x\n"})
        self.assertEqual(t["state"], "done", t["error"])
        self.assertTrue(t["branch"].startswith(f"task/{tid}-plain-task"))
        self.assertEqual(git(self.repo, "show", "main:c.txt"), "x")  # merged into dev, then main (Straight to main)
        self.assertIsNone(t["jira"])
        self.assertEqual(self.pushes, [])  # no fetch, no push
        self.assertEqual(self.calls(), [])
        self.assertFalse(git(self.repo, "ls-remote", "origin", t["branch"]))
        self.assertIsNone(opsd.pr_view_rows(t, []) or None)


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


class ReviewBase(Base):
    """A plain project with a GitHub origin, and helpers to post reviews and threads on the fake GitHub."""

    def setUp(self):
        super().setUp()
        self.repo = new_repo(self.root / "repo")
        self.bare = self.add_origin(self.repo, "r")

    def review(self, slug, n, login, state="COMMENTED", body="", ts=None):
        self.load()
        self.state["prs"][f"o/{slug}#{n}"].setdefault("reviews", []).append(
            {"author": {"login": login}, "state": state, "body": body, "submittedAt": iso(ts), "url": "u"})
        self.save()

    def thread(self, slug, n, tid, *comments, resolved=False, path="c.txt", line=1):
        """A review thread: comments are (login, body)."""
        self.load()
        self.state["prs"][f"o/{slug}#{n}"].setdefault("threads", []).append(
            {"id": tid, "isResolved": resolved, "isOutdated": False, "path": path, "line": line,
             "comments": {"nodes": [{"databaseId": 100 + i, "author": {"login": who}, "body": body, "url": "u"}
                                    for i, (who, body) in enumerate(comments)]}})
        self.save()

    def opened(self, target, extra=""):
        self.project(extra)
        tid = self.add(target=target)
        self.run_task(tid, {"c.txt": "x\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertIn(t["state"], ("pr", "needs_input"), t["question"])
        return tid

    def stage(self, tid):
        return opsd.pr_view_rows(opsd.task(tid), opsd.pr_rows(tid))[0]["review"]


class Reviews(ReviewBase):
    """The review round after Open PR: the bot review, the worker addressing comments, then human reviewers."""

    def test_bot_review_then_address_then_humans(self):
        tid = self.opened("feat/rv", 'pr_reviewers = ["alice", "org/team"]\n')
        self.assertEqual(self.stage(tid), "awaiting bot review")
        self.review("r", 1, "someone-else", body="old", ts=time.time() - 3600)  # before the push: doesn't count
        opsd.pr_poll()  # no checks: CI green
        self.step(tid)
        opsd.pr_poll()
        t = opsd.task(tid)
        self.assertEqual((t["state"], self.stage(tid)), ("pr", "awaiting bot review"))
        self.review("r", 1, "copilot-pull-request-reviewer", body="Pull request overview: 2 comments")
        self.thread("r", 1, "TH_1", ("copilot-pull-request-reviewer", "Use a constant here"), line=3)
        self.thread("r", 1, "TH_2", ("copilot-pull-request-reviewer", "Typo"), resolved=True)
        self.thread("r", 1, "TH_3", ("copilot-pull-request-reviewer", "Why?"), ("me", "Because"))  # answered
        opsd.pr_poll()
        self.assertIn("copilot-pull-request-reviewer reviewed it", " ".join(self.events(tid)))
        t = self.step(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"), t["question"])
        prompt = t["next_prompt"]
        self.assertIn("Thread `TH_1` on `c.txt:3`", prompt)
        self.assertIn("Use a constant here", prompt)
        self.assertIn("reply to comment 100", prompt)
        self.assertIn("gh api repos/o/r/pulls/1/comments/COMMENT_ID/replies", prompt)
        self.assertIn("resolveReviewThread", prompt)
        self.assertNotIn("TH_2", prompt)  # resolved
        self.assertNotIn("TH_3", prompt)  # its last word is ours
        self.assertNotIn("Pull request overview", prompt)  # a Commented review's text isn't work by itself
        self.assertEqual(self.stage(tid), "addressing comments")
        seen = []
        before = git(self.repo, "rev-parse", "origin/feat/rv")
        t = self.run_task(tid, {"c.txt": "constant\n"}, seen)
        self.assertIn("TH_1", seen[0])
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertEqual(git(self.repo, "rev-parse", "feat/rv~1"), before)  # on top, pushed
        self.assertEqual(git(self.repo, "rev-parse", "origin/feat/rv"), git(self.repo, "rev-parse", "feat/rv"))
        self.assertIn(["pr", "edit", "1", "--add-reviewer", "alice,org/team", "-R", "o/r"], self.calls())
        self.assertEqual(self.stage(tid), "awaiting human review")
        n = len(self.calls())
        opsd.pr_poll()  # one bot round, then humans: nothing more to do
        self.assertEqual(self.step(tid)["state"], "pr")
        self.assertFalse([c for c in self.calls()[n:] if c[:2] == ["api", "graphql"]])
        self.no_force()

    COPILOT_CALL = ["api", "-X", "POST", "repos/o/r/pulls/1/requested_reviewers", "-f",
                    "reviewers[]=copilot-pull-request-reviewer[bot]"]

    def test_copilot_is_asked_on_open(self):
        tid = self.opened("feat/cp")
        self.assertEqual(self.calls().count(self.COPILOT_CALL), 1)
        self.assertIn("asked Copilot to review #1", self.events(tid))
        self.assertEqual(self.load()["prs"]["o/r#1"]["requests"], ["copilot-pull-request-reviewer[bot]"])
        self.review("r", 1, "copilot-pull-request-reviewer")
        opsd.pr_poll()
        self.step(tid)
        opsd.pr_poll()
        self.assertEqual(self.calls().count(self.COPILOT_CALL), 1)  # once, when it opened
        self.assertEqual(self.stage(tid), "needs reviewers")  # Copilot's request isn't a human reviewer

    def test_copilot_not_asked_without_it_in_bot_reviewers(self):
        for i, extra in enumerate(("pr_bot_reviewers = []\n", 'pr_bot_reviewers = ["other[bot]"]\n')):
            tid = self.opened(f"feat/nc{i}", extra)
            self.assertFalse([c for c in self.calls() if c[:1] == ["api"] and "POST" in c])
            self.assertFalse([e for e in self.events(tid) if "Copilot" in e])

    def test_copilot_matched_without_bot_suffix(self):
        tid = self.opened("feat/sx", 'pr_bot_reviewers = ["Copilot-Pull-Request-Reviewer"]\n')
        self.assertIn("asked Copilot to review #1", self.events(tid))

    def test_adopted_pr_copilot_reviewed_already_is_not_asked_again(self):
        self.project()
        self.load()
        self.state["next"] = 2
        self.state["prs"]["o/r#1"] = {"head": "feat/ad", "base": "main", "title": "t", "body": "", "state": "OPEN",
                                      "checks": [], "url": "https://github.com/o/r/pull/1",
                                      "reviews": [{"author": {"login": "copilot-pull-request-reviewer"},
                                                   "state": "COMMENTED", "body": "", "submittedAt": iso(None),
                                                   "url": "u"}]}
        self.save()
        tid = self.opened("feat/ad")
        self.assertIn("PR #1 was open already", " ".join(self.events(tid)))
        self.assertNotIn(self.COPILOT_CALL, self.calls())
        self.assertFalse([e for e in self.events(tid) if "Copilot" in e])

    def test_adopted_pr_without_copilot_is_asked(self):
        self.project()
        self.load()
        self.state["next"] = 2
        self.state["prs"]["o/r#1"] = {"head": "feat/ad2", "base": "main", "title": "t", "body": "",
                                      "state": "OPEN", "checks": [], "url": "https://github.com/o/r/pull/1"}
        self.save()
        tid = self.opened("feat/ad2")
        self.assertIn("asked Copilot to review #1", self.events(tid))

    def test_failed_copilot_request_logs_and_times_out(self):
        self.load()
        self.state["request_error"] = "HTTP 422: Reviews may only be requested from collaborators."
        self.save()
        tid = self.opened("feat/cf", "pr_bot_review_timeout = 1\n")
        self.assertIn("couldn't ask Copilot to review #1: HTTP 422: Reviews may only be requested from "
                      "collaborators.", self.events(tid))
        self.assertEqual(self.stage(tid), "awaiting bot review")
        opsd.pr_poll()
        self.assertEqual(self.step(tid)["state"], "pr")
        opsd.q("UPDATE prs SET review_since=? WHERE task_id=?", (time.time() - 61, tid))
        opsd.pr_poll()
        self.step(tid)
        self.assertIn("#1: no bot review came within 1 min; going on", self.events(tid))

    def test_no_bot_review_in_time_no_comments_and_pick_reviewers(self):
        tid = self.opened("feat/to", "pr_bot_review_timeout = 1\n")
        opsd.pr_poll()  # no checks: CI green
        self.assertEqual(self.step(tid)["state"], "pr")  # still waiting for the bot
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "pr")
        opsd.q("UPDATE prs SET review_since=? WHERE task_id=?", (time.time() - 61, tid))
        opsd.pr_poll()
        t = self.step(tid)
        self.assertIn("#1: no bot review came within 1 min; going on", self.events(tid))
        self.assertIn("#1: no unresolved review comments", self.events(tid))
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_PICK_PHASE))
        self.assertIn("Pick reviewers for PR #1: https://github.com/o/r/pull/1", t["question"])
        self.assertEqual(self.stage(tid), "needs reviewers")
        opsd.H.__new__(opsd.H).route(f"/api/tasks/{tid}/answer", {"answer": "@bob, carol"})
        t = self.step(tid)
        self.assertEqual(t["state"], "pr", t["question"])
        self.assertIn(["pr", "edit", "1", "--add-reviewer", "bob,carol", "-R", "o/r"], self.calls())
        self.assertEqual(self.stage(tid), "awaiting human review")

    def test_reviewers_requested_on_github_end_the_pick(self):
        tid = self.opened("feat/gh", "pr_bot_reviewers = []\n")  # no bot: straight on
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_PICK_PHASE))
        opsd.pr_poll()  # CI green: the step runs, and still nobody reviews it
        self.assertEqual(self.step(tid)["phase"], opsd.PR_PICK_PHASE)
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "needs_input")  # nothing moved
        self.load()["prs"]["o/r#1"]["requests"] = ["dana"]
        self.save()
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "pr", t["question"])
        self.assertIn("#1: awaiting review by dana, requested on GitHub", self.events(tid))

    def test_review_comment_is_posted_and_its_review_counts(self):
        tid = self.opened("feat/cl", 'pr_bot_reviewers = []\npr_review_comment = "@claude /pr-review"\n'
                                     'pr_reviewers = ["alice"]\n')
        self.assertEqual(self.load()["prs"]["o/r#1"]["comments"], ["@claude /pr-review"])
        self.assertEqual(self.stage(tid), "awaiting bot review")
        self.review("r", 1, "claude", state="CHANGES_REQUESTED", body="Please add a test")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))
        self.assertIn("Please add a test", t["next_prompt"])  # Changes requested counts with no thread
        t = self.run_task(tid, {"t.txt": "test\n"})
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertEqual(self.stage(tid), "awaiting human review")

    def test_ci_red_comes_before_addressing(self):
        tid = self.opened("feat/red")
        self.review("r", 1, "copilot-pull-request-reviewer[bot]")
        self.thread("r", 1, "TH_9", ("copilot-pull-request-reviewer", "Nit"))
        self.checks("r", 1, "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_RED))
        opsd.pr_poll()  # still red: the bot review alone doesn't requeue it
        self.assertEqual(opsd.task(tid)["state"], "needs_input")
        opsd.pr_action(self.p, opsd.task(tid), "fix-ci", {})
        t = self.run_task(tid, {"c.txt": "fixed\n"})  # pushed: its CI runs again, not red, so the review goes on
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))
        self.assertEqual(self.stage(tid), "addressing comments")
        self.assertIn("TH_9", t["next_prompt"])

    def test_address_comments_on_demand(self):
        tid = self.opened("feat/od", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.assertEqual(self.stage(tid), "awaiting human review")
        with self.assertRaisesRegex(ValueError, "no unresolved review comments on #1"):
            opsd.pr_action(self.p, opsd.task(tid), "pr-address", {})
        self.review("r", 1, "alice", body="Rename the helper, please")  # Commented: counts when you ask
        self.thread("r", 1, "TH_A", ("alice", "Split this function"))
        opsd.pr_action(self.p, opsd.task(tid), "pr-address", {})
        t = opsd.task(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))
        self.assertIn("TH_A", t["next_prompt"])
        self.assertIn("Rename the helper", t["next_prompt"])
        calls = len([c for c in self.calls() if "--add-reviewer" in c])
        t = self.run_task(tid, {"c.txt": "split\n"})
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertEqual(len([c for c in self.calls() if "--add-reviewer" in c]), calls + 1)  # asked again
        self.assertEqual(self.stage(tid), "awaiting human review")

    def test_review_keys_in_the_repo_file(self):
        keep, _ = opsd.split_repo_config('pr_bot_reviewers = ["a[bot]"]\npr_review_comment = "@claude review"\n'
                                         'pr_bot_review_timeout = 5\npr_reviewers = ["org/team"]\n')
        self.assertEqual(keep["pr_bot_review_timeout"], 5)
        self.assertIn('pr_reviewers = ["org/team"]', opsd.config_toml(keep))
        for bad in ("pr_bot_review_timeout = -1", 'pr_reviewers = "alice"', "pr_review_comment = 3"):
            with self.assertRaises(ValueError):
                opsd.split_repo_config(bad)
        self.project()
        self.assertEqual((self.p.pr_bot_reviewers, self.p.pr_bot_review_timeout, self.p.pr_reviewers),
                         (["copilot-pull-request-reviewer[bot]"], 30, []))


class MergeWhenReady(ReviewBase):
    """Merge when ready: the queue fixes red CI, addresses later comments and merges each PR once green and approved."""

    def decide(self, n, decision, slug="r"):
        self.load()
        self.state["prs"][f"o/{slug}#{n}"]["decision"] = decision
        self.save()

    def ready(self, tid, on=True):
        opsd.pr_action(self.p, opsd.task(tid), "pr-ready", {"on": 1 if on else 0})

    def merges(self):
        return [c for c in self.calls() if c[:2] == ["pr", "merge"]]

    def test_off_by_default_nothing_automatic(self):
        tid = self.opened("feat/off", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.assertFalse(opsd.ready_on(tid))
        self.checks("r", 1, "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_RED))  # waits for your Fix CI
        self.assertNotIn("Merge when ready", t["question"])
        self.checks("r", 1, "SUCCESS")
        self.decide(1, "APPROVED")
        self.thread("r", 1, "TH_X", ("alice", "Rename it"))
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("pr", "ready to merge: #1"))  # no address round, no merge
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "pr")
        self.assertEqual(self.merges(), [])
        self.assertIsNone(opsd.ready_words(self.p, opsd.task(tid), opsd.pr_rows(tid)))

    def test_red_ci_fixes_itself_in_the_same_session_and_gives_up_after_3(self):
        tid = self.opened("feat/fx", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        opsd.upd(tid, session_id="sess-1")
        self.ready(tid)
        self.step(tid)
        for n in (1, 2, 3):
            self.checks("r", 1, "SUCCESS", "FAILURE")
            opsd.pr_poll()
            t = self.step(tid)
            self.assertEqual((t["state"], t["start_at"], t["session_id"]), ("queued", "claude", "sess-1"), t["question"])
            self.assertIn("log line 299", t["next_prompt"])  # fix_ci_prompt, as the Fix CI button
            self.assertIn(f"Merge when ready: fixing CI on #1 (round {n} of 3)", self.events(tid))
            self.assertEqual(opsd.ready_words(self.p, t, opsd.pr_rows(tid)), f"fixing CI on #1 (round {n} of 3)")
            seen = []
            t = self.run_task(tid, {"c.txt": f"fix {n}\n"}, seen)
            self.assertIn("log line 299", seen[0])
            self.assertEqual((t["state"], t["session_id"]), ("pr", "sess-1"), t["question"] or t["error"])
        self.checks("r", 1, "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_RED))
        self.assertTrue(t["question"].startswith("Merge when ready gave up: CI still red after 3 fixes"), t["question"])
        self.assertEqual(opsd.pr_rows(tid)[0]["auto_fixes"], 3)
        self.no_force()

    def test_new_review_comment_triggers_an_address_round(self):
        tid = self.opened("feat/ad", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.assertEqual(self.stage(tid), "awaiting human review")
        self.ready(tid)
        t = self.step(tid)  # no checks: green; alice is asked and hasn't reviewed
        self.assertEqual((t["state"], t["phase"]), ("pr", "Merge when ready: waiting for approval on #1"))
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "pr")  # nothing new: no step
        self.thread("r", 1, "TH_A", ("alice", "Split this function"))
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"), t["question"])
        self.assertIn("TH_A", t["next_prompt"])
        self.assertIn("Merge when ready: addressing comments on #1 (round 1 of 3)", self.events(tid))
        t = self.run_task(tid, {"c.txt": "split\n"})
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.assertEqual(self.stage(tid), "awaiting human review")  # asked again
        self.load()["prs"]["o/r#1"]["threads"][0]["isResolved"] = True
        self.save()
        opsd.pr_poll()  # the push's CI: green
        self.step(tid)
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "pr")  # resolved: nothing to do
        for n in (2, 3):  # a new comment each time: rounds 2 and 3, then it gives up
            self.thread("r", 1, f"TH_{n}", ("alice", f"And this {n}"))
            opsd.pr_poll()
            self.step(tid)
            self.assertIn(f"Merge when ready: addressing comments on #1 (round {n} of 3)", self.events(tid))
            self.run_task(tid, {"c.txt": f"round {n}\n"})
            self.load()["prs"]["o/r#1"]["threads"][-1]["isResolved"] = True
            self.save()
        self.thread("r", 1, "TH_4", ("alice", "One more"))
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_LEFT))
        self.assertIn("Merge when ready gave up: comments left after 3 rounds on #1", t["question"])
        self.assertEqual(self.merges(), [])

    def test_no_merge_while_review_is_required_or_changes_are_requested(self):
        tid = self.opened("feat/rq", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.ready(tid)
        self.step(tid)
        for decision, words in (("REVIEW_REQUIRED", "waiting for approval on #1"),
                                ("CHANGES_REQUESTED", "changes requested on #1")):
            self.decide(1, decision)
            self.load()["prs"]["o/r#1"]["requests"] = []  # alice reviewed: no longer requested
            self.save()
            opsd.pr_poll()
            t = opsd.task(tid)
            if t["state"] == "queued":
                t = self.step(tid)
            self.assertEqual((t["state"], t["phase"]), ("pr", f"Merge when ready: {words}"))
            self.assertEqual(self.merges(), [])
        self.decide(1, "APPROVED")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual(self.merges(), [["pr", "merge", "1", "--squash", "--delete-branch", "-R", "o/r"]])
        self.assertIn("Merge when ready: merging #1, green and approved", self.events(tid))
        self.assertFalse(opsd.ready_on(tid))  # done: the switch is cleared

    def test_review_required_and_nobody_to_ask_needs_you(self):
        tid = self.opened("feat/nr", "pr_bot_review_timeout = 1\n")
        self.ready(tid)
        self.decide(1, "REVIEW_REQUIRED")
        opsd.q("UPDATE prs SET review_since=? WHERE task_id=?", (time.time() - 61, tid))
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_PICK_PHASE))
        self.assertEqual(self.merges(), [])

    def test_empty_review_decision_skips_the_human_stage_and_merges(self):
        tid = self.opened("feat/em", "pr_bot_reviewers = []\n")  # no reviewers: Pick reviewers without the switch
        self.assertEqual(opsd.task(tid)["phase"], opsd.PR_PICK_PHASE)
        self.ready(tid)  # turning it on goes on from there
        t = self.step(tid)
        self.assertEqual(t["state"], "pr", t["question"])
        self.assertIn("Merge when ready: #1 needs no review on GitHub: no reviewers to ask", self.events(tid))
        opsd.pr_poll()  # no checks: green, and GitHub requires no review
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual(len(self.merges()), 1)

    def test_comments_left_block_the_merge(self):
        tid = self.opened("feat/cl", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.ready(tid)
        self.step(tid)
        self.decide(1, "APPROVED")
        self.thread("r", 1, "TH_L", ("alice", "Nit"))
        opsd.pr_poll()
        t = self.step(tid)  # approved and green, but a comment is left: it is addressed first
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))
        self.assertEqual(self.merges(), [])

    def test_failed_merge_needs_you_with_ghs_message(self):
        tid = self.opened("feat/fm", "pr_bot_reviewers = []\n")
        self.ready(tid)
        self.step(tid)
        self.load()["prs"]["o/r#1"]["merge_error"] = "Pull request is not mergeable: the merge commit cannot be cleanly created"
        self.save()
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_NO_MERGE))
        self.assertIn("cannot be cleanly created", t["question"])
        self.no_force()

    def test_unticking_stops_further_steps(self):
        tid = self.opened("feat/un", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        self.ready(tid)
        self.step(tid)
        self.checks("r", 1, "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["start_at"]), ("queued", "claude"))  # an automatic Fix CI
        self.ready(tid, False)  # off while it waits to run: it still runs
        self.assertIn("Merge when ready off: no more automatic steps (one already running finishes)", self.events(tid))
        t = self.run_task(tid, {"c.txt": "fixed\n"})
        self.assertEqual(t["state"], "pr", t["question"] or t["error"])
        self.checks("r", 1, "FAILURE")
        opsd.pr_poll()
        t = self.step(tid)
        self.assertEqual((t["state"], t["phase"]), ("needs_input", opsd.PR_RED))  # no second automatic round
        self.assertNotIn("Merge when ready", t["question"])
        self.checks("r", 1, "SUCCESS")
        self.decide(1, "APPROVED")
        opsd.pr_poll()
        self.assertEqual(self.step(tid)["phase"], "ready to merge: #1")
        self.assertEqual(self.merges(), [])

    def test_on_before_any_pr_is_open(self):
        self.project("pr_bot_reviewers = []\n")
        tid = self.add(target="feat/early")
        self.run_task(tid, {"c.txt": "x\n"})
        self.ready(tid)
        self.assertTrue(opsd.ready_on(tid))
        self.assertEqual(opsd.task(tid)["state"], "on_branch")  # nothing opens by itself
        self.assertEqual(opsd.ready_words(self.p, opsd.task(tid), opsd.pr_rows(tid)), "on: it starts once the PRs open")
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        opsd.pr_poll()
        self.assertEqual(self.step(tid)["state"], "done")

    def test_stale_session_is_resumed_while_it_is_on(self):
        tid = self.opened("feat/st", 'pr_bot_reviewers = []\npr_reviewers = ["alice"]\n')
        opsd.upd(tid, session_id="sess-old", last_call=time.time() - opsd.FRESH_AFTER - 60, ctx_tokens=1000)
        self.assertIn("cache expired", opsd.stale_or_ready(opsd.task(tid))[1])  # off: a fresh session
        self.ready(tid)
        self.assertIsNone(opsd.stale_or_ready(opsd.task(tid)))
        self.assertTrue(any(e.startswith("Merge when ready: resumed the same session anyway (last call")
                            for e in self.events(tid)))
        real, seen = opsd.subprocess.Popen, []

        class Started(Exception):
            pass

        def popen(cmd, *a, **k):  # stop run_claude at the claude process, with its command line
            if cmd and cmd[0] == "caffeinate":
                seen.append(cmd)
                raise Started
            return real(cmd, *a, **k)
        with mock.patch.object(opsd.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(opsd, "limited", return_value=False):
            with self.assertRaises(Started):
                opsd.run_claude(self.p, tid, "fix it")
        self.assertEqual(seen[0][seen[0].index("--resume") + 1], "sess-old")
        self.assertEqual(opsd.task(tid)["session_id"], "sess-old")


class Workspace(Base):
    """ballup-monorepo's shape: a backend and an app, PRs in pr_order, and a root repo with no origin."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "ws"
        self.repo.mkdir()
        self.dunk = new_repo(self.repo / "dunk")
        self.swish = new_repo(self.repo / "swish")
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, ".gitignore", "/dunk/\n/swish/\n", "root")
        self.add_origin(self.dunk, "dunk")
        self.add_origin(self.swish, "swish")
        self.project('pr_order = ["dunk", "swish"]\npr_merge = "merge"\ndirect_main = false\n',
                     repos=[".", "dunk", "swish"])

    def test_pr_order_opens_the_app_after_the_backend_is_green(self):
        tid = self.add(jira="BALL-9")
        t = self.run_task(tid, {"dunk/api.txt": "api\n", "swish/app.txt": "app\n", "notes.md": "root\n"})
        self.assertEqual(t["state"], "on_branch", t["error"])
        self.assertIn("dunk 1 commit", t["summary"])
        self.assertIn("no origin: they stay on the local branch, no PR", t["summary"])
        opsd.pr_action(self.p, t, "pr-open", {})
        t = self.step(tid)  # the first PR opens at once, the second waits
        rows = opsd.pr_rows(tid)
        self.assertEqual([(x["repo"], x["number"]) for x in rows], [("dunk", 1), ("swish", None)])
        self.assertEqual(t["state"], "pr")
        self.assertTrue(any("no origin in ws" in e for e in self.events(tid)))
        self.assertFalse(git(self.repo / "swish", "ls-remote", "origin", t["branch"]))  # not pushed yet
        self.checks("dunk", 1, None)
        opsd.pr_poll()
        self.assertEqual(opsd.task(tid)["state"], "pr")  # pending CI: nothing to do
        self.assertIsNone(opsd.pr_rows(tid)[1]["number"])  # still waiting
        self.checks("dunk", 1, "SUCCESS")
        opsd.pr_poll()
        t = self.step(tid)
        rows = opsd.pr_rows(tid)
        self.assertEqual(rows[1]["number"], 2)
        app = self.load()["prs"]["o/swish#2"]
        self.assertEqual([x["review"] for x in opsd.pr_rows(tid)], ["bot", "bot"])  # each its own round, as it opens
        self.assertLess(rows[0]["review_since"], rows[1]["review_since"])
        self.assertIn("Depends on https://github.com/o/dunk/pull/1", app["body"])
        self.assertIn("https://github.com/o/swish/pull/2", self.state["prs"]["o/dunk#1"]["body"])  # linked back
        opsd.pr_poll()  # swish has no checks: green
        t = self.step(tid)
        rows = opsd.pr_rows(tid)
        # Merge is on only in order
        view = {x["name"]: x for x in opsd.pr_view_rows(t, rows)}
        self.assertTrue(view["dunk"]["merge"])
        self.assertFalse(view["swish"]["merge"])
        self.assertEqual(view["swish"]["merge_why"], "an earlier PR in pr_order has not merged")
        with self.assertRaises(ValueError):
            opsd.pr_action(self.p, t, "pr-merge", {"repo": "swish"})
        # Merge all (now Merge when ready): dunk now, swish once dunk has landed
        opsd.q("UPDATE prs SET review_since=? WHERE task_id=?", (time.time() - 3600, tid))  # no bot review came
        opsd.pr_action(self.p, t, "pr-merge-all", {})
        self.assertTrue(opsd.ready_on(tid))
        self.step(tid)  # past the review round: GitHub requires no review, nobody to ask
        self.assertEqual([x["review"] for x in opsd.pr_rows(tid)], ["human", "human"])
        opsd.pr_poll()
        t = self.step(tid)  # swish is green: it merges once dunk has landed, in the same step
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual([x["state"] for x in opsd.pr_rows(tid)], ["MERGED", "MERGED"])
        st = self.load()["prs"]
        self.assertEqual(st["o/dunk#1"]["how"], "--merge")  # pr_merge = "merge"
        self.assertEqual([(c[-1], c[2]) for c in self.calls() if c[:2] == ["pr", "merge"]],
                         [("o/dunk", "1"), ("o/swish", "2")])  # in pr_order
        self.assertEqual(git(self.dunk, "rev-parse", "main"), st["o/dunk#1"]["merge"])
        self.assertTrue(opsd.is_ancestor(self.p.parts[1], "main", "dev"))  # dev took it in (direct_main = false)
        self.assertEqual(git(self.swish, "rev-parse", "main"), self.load()["prs"]["o/swish#2"]["merge"])
        self.assertEqual(len(t["merged"].split()), 2)
        self.no_force()

    def test_out_of_order_merge_on_github_warns(self):
        tid = self.add(target="feat/ooo")
        self.run_task(tid, {"dunk/api.txt": "api\n", "swish/app.txt": "app\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        self.step(tid)
        opsd.pr_poll()  # dunk has no checks: green, so swish opens
        self.step(tid)
        self.gh_merge("swish", 2)
        opsd.pr_poll()
        t = self.step(tid)
        self.assertIn("out of pr_order", opsd.pr_rows(tid)[1]["warning"])
        self.assertEqual(t["state"], "pr")

    def test_merge_when_ready_merges_in_pr_order(self):
        self.project('pr_order = ["dunk", "swish"]\npr_bot_reviewers = []\ndirect_main = false\n',
                     repos=[".", "dunk", "swish"])
        tid = self.add(target="feat/mwr")
        self.run_task(tid, {"dunk/api.txt": "api\n", "swish/app.txt": "app\n"})
        opsd.pr_action(self.p, opsd.task(tid), "pr-ready", {"on": 1})
        opsd.pr_action(self.p, opsd.task(tid), "pr-open", {})
        t = self.step(tid)
        self.assertEqual(t["phase"], "Merge when ready: waiting for CI on dunk #1")
        self.checks("dunk", 1, "SUCCESS")
        opsd.pr_poll()
        t = self.step(tid)  # dunk merges; then swish opens
        self.assertEqual([x["state"] for x in opsd.pr_rows(tid)], ["MERGED", "OPEN"])
        self.assertEqual(t["phase"], "Merge when ready: waiting for CI on swish #2")
        opsd.pr_poll()  # swish: no checks, green
        t = self.step(tid)
        self.assertEqual(t["state"], "done", t["question"] or t["error"])
        self.assertEqual([(c[-1], c[2]) for c in self.calls() if c[:2] == ["pr", "merge"]],
                         [("o/dunk", "1"), ("o/swish", "2")])
        self.no_force()


if __name__ == "__main__":
    unittest.main()
