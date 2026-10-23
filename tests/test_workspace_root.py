"""A workspace whose own folder is a git repo ("." in repos): tasks keep edits to its files, Promote moves its main,
the loader refuses a root that doesn't gitignore the other repos, and adopt-workspace-root sets it up.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import subprocess
import sys
import tempfile
import tomllib
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


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def new_workspace():
    """A workspace folder with its own files (CLAUDE.md, bin/clone) and two repos, a and b, on main."""
    ws = Path(tempfile.mkdtemp(dir=DATA)) / "ws"
    for s in ("a", "b"):
        (ws / s).mkdir(parents=True)
        git(ws / s, "init", "-q", "-b", "main")
        (ws / s / "x.txt").write_text(f"{s} one\n")
        git(ws / s, "add", ".")
        git(ws / s, "commit", "-q", "-m", "init")
    (ws / "CLAUDE.md").write_text("# ws\n")
    (ws / "bin").mkdir()
    (ws / "bin" / "clone").write_text("git clone a\n")
    return ws


def write_toml(ws, repos, name=None):
    opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    name = name or f"ws{ws.parent.name[-6:].lower()}"
    toml = opsd.PROJECTS_DIR / f"{name}.toml"
    toml.write_text(f'title = "WS"\nrepo = "{ws}"\nworktrees = "{ws.parent / "wt"}"\n'
                    f'repos = [{", ".join(chr(34) + r + chr(34) for r in repos)}]   # the repos\n'
                    f'direct_main = false\n\n[worker]\nnotes = "keep me"  # a comment\n')
    return toml


class Adopt(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.ws = new_workspace()
        self.toml = write_toml(self.ws, ["a", "b"])
        self.addCleanup(lambda: (self.toml.unlink(missing_ok=True), opsd.load_projects()))

    def test_adopt_sets_up_the_root_and_is_idempotent(self):
        did = opsd.adopt_workspace_root(self.toml.stem)
        self.assertTrue(did)
        self.assertEqual(git(self.ws, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(self.ws, "log", "-1", "--format=%s"), opsd.ADOPT_MSG)
        self.assertEqual(git(self.ws, "rev-parse", "dev"), git(self.ws, "rev-parse", "main"))
        self.assertIn("/a/\n/b/\n", (self.ws / ".gitignore").read_text())
        tracked = git(self.ws, "ls-files").splitlines()
        self.assertEqual(sorted(tracked), [".gitignore", "CLAUDE.md", "bin/clone"])
        text = self.toml.read_text()
        self.assertEqual(tomllib.loads(text)["repos"], ["a", "b", "."])
        self.assertIn("# the repos", text)
        self.assertIn("# a comment", text)
        head = git(self.ws, "rev-parse", "HEAD")
        self.assertEqual(opsd.adopt_workspace_root(self.toml.stem), [])  # again: nothing to do
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), head)
        self.assertEqual(tomllib.loads(self.toml.read_text())["repos"], ["a", "b", "."])
        opsd.load_projects()
        self.assertIn(self.toml.stem, opsd.PROJECTS)

    def test_adopt_keeps_an_existing_gitignore(self):
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / ".gitignore").write_text(".DS_Store\n/a/")
        git(self.ws, "add", ".gitignore", "CLAUDE.md")
        git(self.ws, "commit", "-q", "-m", "mine")
        git(self.ws, "add", "bin")
        git(self.ws, "commit", "-q", "-m", "bin")
        opsd.adopt_workspace_root(self.toml.stem)
        self.assertEqual((self.ws / ".gitignore").read_text(), ".DS_Store\n/a/\n/b/\n")
        self.assertEqual(git(self.ws, "log", "--format=%s", "-3").splitlines(), [opsd.ADOPT_MSG, "bin", "mine"])

    def test_adopt_refuses_uncommitted_changes(self):
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / ".gitignore").write_text("/a/\n/b/\n")
        git(self.ws, "add", ".")
        git(self.ws, "commit", "-q", "-m", "mine")
        (self.ws / "CLAUDE.md").write_text("# edited\n")
        with self.assertRaisesRegex(ValueError, "uncommitted changes"):
            opsd.adopt_workspace_root(self.toml.stem)
        self.assertNotIn(".", tomllib.loads(self.toml.read_text())["repos"])


class Refused(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.ws = new_workspace()

    def load(self, repos):
        toml = write_toml(self.ws, repos)
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.assertNotIn(toml.stem, opsd.PROJECTS)
        why = opsd.BROKEN[toml.stem][1]
        card = next(g for g in opsd.state_payload()["projects"] if g["name"] == toml.stem)
        self.assertEqual(card["error"], why)
        return why

    def test_root_that_is_not_a_repo(self):
        self.assertIn("is not a git repository", self.load([".", "a", "b"]))

    def test_missing_gitignore_entry(self):
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / ".gitignore").write_text("/a/\n")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", "root")
        why = self.load([".", "a", "b"])
        self.assertIn("does not ignore b/", why)
        self.assertNotIn("a/,", why)


class Tasks(unittest.TestCase):
    """Real _pipeline runs whose worker (run_claude) edits and commits in the task worktree."""

    def setUp(self):
        opsd.init_db()
        self.ws = new_workspace()
        self.toml = write_toml(self.ws, [".", "a", "b"])
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / ".gitignore").write_text("/a/\n/b/\n")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", "root")
        opsd.load_projects()
        self.p = opsd.PROJECTS[self.toml.stem]
        self.root, self.a, self.b = self.p.parts
        self.ids = []
        patches = [mock.patch.object(opsd, "notify"), mock.patch.object(opsd, "worker_prompt", return_value="brief")]
        for m in patches:
            m.start()
            self.addCleanup(m.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids)
        self.toml.unlink()
        opsd.load_projects()

    def run_task(self, edits):
        """A task whose worker writes edits ({relative path: text}) and commits in each repo it touched."""
        tid = opsd.add_task(self.p.name, "body", f"task {len(self.ids)}")
        self.ids.append(tid)
        opsd.upd(tid, state="running")

        def worker(p, tid, prompt):
            wt = Path(opsd.task(tid)["worktree"])
            for rel, text in edits.items():
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                (wt / rel).write_text(text)
            for r in p.parts:
                if git(r.at(wt), "status", "--porcelain"):
                    git(r.at(wt), "add", "-A")
                    git(r.at(wt), "commit", "-q", "-m", "edit")
            return {"status": "done", "summary": "edited"}
        with mock.patch.object(opsd, "run_claude", side_effect=worker):
            opsd._pipeline(self.p, tid)
        t = opsd.task(tid)
        self.assertEqual(t["state"], "done", t["error"])
        return t

    def test_worktree_layout(self):
        opsd.bootstrap(self.p)
        dev = self.p.dev_wt
        self.assertEqual(git(dev, "rev-parse", "--abbrev-ref", "HEAD"), "dev")
        self.assertEqual(git(dev / "a", "rev-parse", "--abbrev-ref", "HEAD"), "dev")
        self.assertEqual(git(dev, "status", "--porcelain"), "")  # the repo folders are ignored, nothing copied
        self.assertIn("worktree root is itself a git repo", opsd.workspace_note(self.p, "task/1-x"))

    def test_root_and_child_edits_both_merge(self):
        opsd.bootstrap(self.p)
        t = self.run_task({"CLAUDE.md": "# ws\nnew repo c\n", "bin/clone": "git clone a\ngit clone c\n",
                           "a/x.txt": "a two\n"})
        self.assertEqual(git(self.ws, "show", "dev:CLAUDE.md"), "# ws\nnew repo c")
        self.assertEqual(git(self.ws, "show", "dev:bin/clone"), "git clone a\ngit clone c")
        self.assertEqual(git(self.ws / "a", "show", "dev:x.txt"), "a two")
        self.assertEqual(git(self.ws / "b", "rev-parse", "dev"), git(self.ws / "b", "rev-parse", "main"))
        self.assertEqual(len(t["merged"].split()), 2)
        self.assertEqual(self.p.mine(self.root, t["merged"]), git(self.ws, "rev-parse", "dev"))
        self.assertIn(f"{self.ws.name} ", self.p.label(t["merged"]))  # the root shows by the workspace's name
        self.assertEqual((self.p.dev_wt / "CLAUDE.md").read_text(), "# ws\nnew repo c\n")
        self.assertFalse(Path(t["worktree"]).exists())  # removed, root last

    def test_child_only_leaves_the_root_alone(self):
        opsd.bootstrap(self.p)
        root_dev = git(self.ws, "rev-parse", "dev")
        t = self.run_task({"b/x.txt": "b two\n"})
        self.assertEqual(git(self.ws, "rev-parse", "dev"), root_dev)
        self.assertEqual(git(self.ws / "b", "show", "dev:x.txt"), "b two")
        self.assertEqual(t["merged"].split(), [git(self.ws / "b", "rev-parse", "dev")])

    def test_promote_moves_main_in_every_repo(self):
        opsd.bootstrap(self.p)
        self.run_task({"CLAUDE.md": "# ws 2\n", "a/x.txt": "a two\n"})
        commit = self.p.revs(self.p.dev)
        rel = opsd.add_task(self.p.name, "release", "release", kind="release")
        self.ids.append(rel)
        self.assertIsNone(opsd.promote(self.p, rel, commit))
        self.assertEqual(self.p.revs(self.p.main), commit)
        self.assertEqual(git(self.ws, "rev-parse", "main"), git(self.ws, "rev-parse", "dev"))
        self.assertEqual((self.ws / "CLAUDE.md").read_text(), "# ws 2\n")  # your checkout moved with main
        self.assertEqual((self.ws / "a" / "x.txt").read_text(), "a two\n")

    def test_send_to_dev_takes_root_edits(self):
        (self.ws / "CLAUDE.md").write_text("# ws, edited in your checkout\n")
        (self.ws / "a" / "x.txt").write_text("a edited\n")
        tid = opsd.queue_senddev(self.p)
        self.ids.append(tid)
        got = {x["sub"]: x["files"] for x in opsd.senddev_of(opsd.task(tid))["repos"]}
        self.assertEqual(got, {".": ["CLAUDE.md"], "a": ["x.txt"]})
        self.assertIn(f"({self.ws.name}, a)", opsd.task(tid)["title"])

    def test_dev_worktree_made_before_the_root_was_a_repo(self):
        # what bootstrap made when repos was ["a", "b"]: copies of the root's files beside the repo worktrees
        dev = self.p.dev_wt
        opsd.remove_worktree(self.p, dev)  # load_projects made it with the root: start over without
        for r in (self.a, self.b):
            git(r.src, "worktree", "add", "-q", str(r.at(dev)), "dev")
        (dev / "CLAUDE.md").write_text("# a stale copy\n")
        opsd.bootstrap(self.p)
        self.assertEqual(git(dev, "rev-parse", "--abbrev-ref", "HEAD"), "dev")
        self.assertEqual((dev / "CLAUDE.md").read_text(), "# ws\n")
        self.assertEqual(git(dev / "a", "rev-parse", "--abbrev-ref", "HEAD"), "dev")
        self.assertEqual(git(dev, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
