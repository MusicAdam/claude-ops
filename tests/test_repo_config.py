"""Config in the repo: claude-ops.toml on a project's main branch replaces the home toml's movable keys, a change on
dev does nothing until it reaches main, a malformed one keeps the last good config, identity keys in it are ignored,
a repo folder it lists that isn't cloned holds the queue, and export-config writes one from the home toml.
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
    os.environ[k] = "t"
for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
    os.environ[k] = "t@example.com"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

HOME_TOML = '''title = "Home"
repo = "{repo}"
worktrees = "{wt}"
direct_main = false
{extra}
[[gate]]
name = "home"
run = "true"

[app]
build = "make"

[worker]
notes = "home notes"
env = {{ A = "1" }}
'''


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo, branch, text, name=opsd.REPO_CONFIG):
    """Commit name with text on branch: in the checkout for the branch it is on, else from a scratch worktree."""
    here = git(repo, "branch", "--show-current") == branch
    wt = repo if here else Path(tempfile.mkdtemp(dir=DATA)) / "c"
    if not here:
        git(repo, "worktree", "add", "-q", str(wt), branch)
    (wt / name).write_text(text)
    git(wt, "add", name)
    git(wt, "commit", "-q", "-m", f"config on {branch}")
    if not here:
        git(repo, "worktree", "remove", "--force", str(wt))


class Base(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        for t in ("tasks", "events", "settings"):
            opsd.q(f"DELETE FROM {t}")
        self.root = Path(tempfile.mkdtemp(dir=DATA))
        self.name = f"rc{self.root.name[-6:].lower()}".replace("_", "")
        self.toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        self.addCleanup(lambda: (self.toml.unlink(missing_ok=True), opsd.LAST_GOOD.clear(), opsd.load_projects()))

    def plain_repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "branch", "dev")
        return repo

    def write_home(self, repo, extra=""):
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.toml.write_text(HOME_TOML.format(repo=repo, wt=self.root / "wt", extra=extra))

    def load(self):
        opsd.load_projects()
        self.assertNotIn(self.name, opsd.BROKEN)
        return opsd.PROJECTS[self.name]


class RepoConfig(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.plain_repo()
        self.write_home(self.repo)

    def test_no_repo_file_reads_the_home_toml_as_before(self):
        p = self.load()
        self.assertEqual([g["name"] for g in p.gate], ["home"])
        self.assertEqual((p.build, p.worker_notes, p.env), ("make", "home notes", {"A": "1"}))
        self.assertEqual((p.config_blob, p.config_error, p.config_warnings, p.missing), ("", None, [], []))
        self.assertNotIn(opsd.REPO_CONFIG, opsd.repo_config_note(p))
        self.assertFalse(opsd.repo_configs_moved())

    def test_repo_gate_replaces_the_home_gate_and_absent_keys_fall_back(self):
        commit_file(self.repo, "main", '[[gate]]\nname = "repo"\nrun = "make test"\n\n[worker]\nnotes = "repo"\n')
        p = self.load()
        self.assertEqual([g["name"] for g in p.gate], ["repo"])  # replaced whole, not merged
        self.assertEqual(p.build, "make")  # [app] is not in the repo file: the home toml's
        self.assertEqual((p.worker_notes, p.env), ("repo", {"A": "1"}))  # notes moves, env stays home
        self.assertIsNone(p.config_error)
        self.assertIn(opsd.REPO_CONFIG, opsd.repo_config_note(p))

    def test_a_change_on_dev_does_nothing_until_main_moves(self):
        commit_file(self.repo, "main", '[[gate]]\nname = "one"\nrun = "true"\n')
        self.assertEqual([g["name"] for g in self.load().gate], ["one"])
        git(self.root / "wt" / "dev", "merge", "-q", "--ff-only", "main")
        commit_file(self.root / "wt" / "dev", "dev", '[[gate]]\nname = "two"\nrun = "true"\n')
        self.assertFalse(opsd.repo_configs_moved())
        self.assertEqual([g["name"] for g in self.load().gate], ["one"])
        git(self.repo, "merge", "-q", "--ff-only", "dev")  # main reaches dev
        self.assertTrue(opsd.repo_configs_moved())
        self.assertEqual([g["name"] for g in self.load().gate], ["two"])
        self.assertFalse(opsd.repo_configs_moved())

    def test_a_malformed_file_keeps_the_last_good_config_with_an_error(self):
        commit_file(self.repo, "main", '[[gate]]\nname = "good"\nrun = "true"\n')
        self.assertEqual([g["name"] for g in self.load().gate], ["good"])
        commit_file(self.repo, "main", '[[gate]\nname = "broken"\n')
        p = self.load()
        self.assertEqual([g["name"] for g in p.gate], ["good"])
        self.assertIn("did not load", p.config_error)
        self.assertEqual(opsd.main_info(p)["config_error"], p.config_error)  # the project card shows it
        commit_file(self.repo, "main", 'repos = ["app"]\n')  # loads as TOML, but is no config a repo file may hold
        p = self.load()
        self.assertEqual(([g["name"] for g in p.gate], p.repos), (["good"], []))
        self.assertIn('must list "."', p.config_error)

    def test_a_malformed_file_with_no_last_good_uses_the_home_toml(self):
        commit_file(self.repo, "main", 'gate = 3\n')
        p = self.load()
        self.assertEqual([g["name"] for g in p.gate], ["home"])
        self.assertIn("did not load", p.config_error)

    def test_identity_keys_in_the_repo_file_are_ignored_with_a_warning(self):
        commit_file(self.repo, "main", 'title = "Hijack"\nmain_branch = "dev"\n\n[worker]\nenv = { B = "2" }\n'
                                       'notes = "n"\n')
        p = self.load()
        self.assertEqual((p.title, p.main, p.env, p.worker_notes), ("Home", "main", {"A": "1"}, "n"))
        self.assertEqual(len(p.config_warnings), 3)
        self.assertTrue(any(w.startswith("title ") for w in p.config_warnings))
        self.assertTrue(any("[worker] env" in w for w in p.config_warnings))
        self.assertIsNone(p.config_error)

    def test_export_config_writes_the_movable_keys_uncommitted(self):
        self.toml.write_text(self.toml.read_text() + '\n[[deploy]]\nname = "prod"\nrun = "echo \\"hi\\""\n\n'
                             '[design]\nnotes = """\nline one\nsays "x" \\\\ there\n"""\n')
        before = self.toml.read_text()
        did = opsd.export_config(self.name)
        self.assertEqual(self.toml.read_text(), before)  # the home toml stays as it was
        out = self.repo / opsd.REPO_CONFIG
        self.assertIn("uncommitted", did[0])
        r = tomllib.loads(out.read_text())
        home = tomllib.loads(before)
        self.assertEqual(r, {"gate": home["gate"], "app": home["app"], "deploy": home["deploy"],
                             "worker": {"notes": "home notes"}, "design": {"notes": home["design"]["notes"]}})
        self.assertIn("?? " + opsd.REPO_CONFIG, git(self.repo, "status", "--porcelain"))
        with self.assertRaises(ValueError):  # never over a file that is there
            opsd.export_config(self.name)
        git(self.repo, "add", opsd.REPO_CONFIG)
        git(self.repo, "commit", "-q", "-m", "config")
        p = self.load()  # once on main, it is the same config the home toml gave
        self.assertEqual((p.gate, p.build, p.design_notes, p.config_error), (home["gate"], "make", "line one\n"
                                                                             'says "x" \\ there', None))


class NotCloned(Base):
    def test_a_repo_folder_missing_from_your_checkout_holds_the_queue(self):
        ws = self.root / "ws"
        (ws / "a").mkdir(parents=True)
        git(ws / "a", "init", "-q", "-b", "main")
        (ws / "a" / "x").write_text("a\n")
        git(ws / "a", "add", ".")
        git(ws / "a", "commit", "-q", "-m", "a")
        git(ws, "init", "-q", "-b", "main")
        (ws / ".gitignore").write_text("/a/\n/web/\n")
        git(ws, "add", ".")
        git(ws, "commit", "-q", "-m", "root")
        self.write_home(ws, extra='repos = [".", "a"]\n')
        p = self.load()
        self.assertEqual((p.repos, p.missing), ([".", "a"], []))
        commit_file(ws, "main", 'repos = [".", "a", "web"]\n')
        self.assertTrue(opsd.repo_configs_moved())
        p = self.load()
        self.assertEqual(p.missing, ["web"])
        self.assertEqual(p.repos, [".", "a"])  # the last good config stays in use
        self.assertIn("web/ is not cloned", p.config_error)
        tid = opsd.add_task(self.name, "do it", title="t")
        with mock.patch.object(opsd, "pipeline", lambda tid: None):
            opsd.schedule_once()
        self.assertEqual(opsd.task(tid)["state"], "queued")
        self.assertIn("web/ is not cloned", opsd.wait_reasons([opsd.task(tid)])[tid])
        self.assertFalse(opsd.repo_configs_moved())
        (ws / "web").mkdir()  # you clone it
        git(ws / "web", "init", "-q", "-b", "main")
        (ws / "web" / "x").write_text("w\n")
        git(ws / "web", "add", ".")
        git(ws / "web", "commit", "-q", "-m", "w")
        self.assertTrue(opsd.repo_configs_moved())
        p = self.load()
        self.assertEqual((p.repos, p.missing, p.config_error), ([".", "a", "web"], [], None))
        opsd.q("DELETE FROM tasks WHERE id=?", (tid,))


if __name__ == "__main__":
    unittest.main()
