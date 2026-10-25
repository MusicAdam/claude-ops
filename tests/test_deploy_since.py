"""What a deploy ships spans the previous release tag to now: a target's release tags are its own release_tag or its
`from` chain's; the span starts at the newest tag by version that the deployed commit descends from (not one at that
commit), with or without a deploy record; with no tag it is since the last deploy, as before.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class Since(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"snc{self.repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\ndirect_main = false\n'
                        '[[deploy]]\nname = "test"\nrun = "true"\nversion = "echo 1.0.0"\nrelease_tag = "v{version}"\n\n'
                        '[[deploy]]\nname = "dist"\nfrom = "test"\nrun = "true"\nnote = "What\'s new"\n\n'
                        '[[deploy]]\nname = "plain"\nrun = "true"\nnote = "What\'s new"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.p = opsd.PROJECTS[self.name]

    def task(self, title):
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state) VALUES (?, ?, '', 0, 'done')",
                     (self.name, title))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", f"Merge task #{tid}: {title}")
        return git(self.repo, "rev-parse", "HEAD")

    def deployed(self, target, sha=None):
        sha = sha or git(self.repo, "rev-parse", "HEAD")
        opsd.set_setting(f"deployed:{self.name}:{target}", json.dumps({"commit": sha, "revs": sha, "time": 1}))

    def titles(self, target):
        return [t for t, _ in opsd.draft_rows(self.p, self.p.target(target))]

    def facts(self, target):
        return opsd.target_facts(self.p, self.p.target(target), None, opsd.settings_now())

    def test_from_target_with_no_record_spans_the_source_tag(self):  # (a)
        self.task("Old")
        git(self.repo, "tag", "v1.0.0")
        self.task("Share a game")
        self.task("Faster scores")
        self.deployed("test")
        self.task("Not deployed by test yet")
        self.assertEqual(self.titles("dist"), ["Faster scores", "Share a game"])
        f = self.facts("dist")
        self.assertIsNone(f["last"])
        self.assertEqual(f["since"], "v1.0.0")
        self.assertEqual(f["behind"], 2)
        self.assertEqual([r[3] for r in f["log"]][-1].split(": ", 1)[1], "Share a game")

    def test_a_newer_record_still_spans_the_tag(self):  # (b)
        git(self.repo, "tag", "v1.0.0")
        self.task("One")
        self.deployed("test")
        self.deployed("dist")
        self.task("Two")
        self.deployed("test")
        self.assertEqual(self.titles("dist"), ["Two", "One"])
        self.assertEqual(self.facts("dist")["since"], "v1.0.0")

    def test_a_tag_at_the_deployed_commit_uses_the_one_before(self):  # (c)
        git(self.repo, "tag", "v1.0.0")
        self.task("One")
        self.task("Two")
        git(self.repo, "tag", "v1.1.0")
        self.deployed("test")
        self.assertEqual(self.titles("dist"), ["Two", "One"])
        self.assertEqual(self.facts("dist")["since"], "v1.0.0")

    def test_newest_by_version_not_by_name_or_date(self):
        git(self.repo, "tag", "v1.10.0")
        self.task("After 1.10")
        later = dict(os.environ, GIT_COMMITTER_DATE="2099-01-01T00:00:00")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "tag",
                        "-a", "-m", "x", "v1.9.0"], check=True, env=later)  # newer by date, "v1.9" > "v1.10" by name
        self.task("Two")
        self.deployed("test")
        self.assertEqual(self.facts("dist")["since"], "v1.10.0")

    def test_no_tags_is_since_the_last_deploy(self):  # (d)
        self.task("One")
        self.deployed("test")
        self.assertEqual(self.titles("dist"), [])  # no record, no tag: first deploy
        f = self.facts("dist")
        self.assertIsNone(f["since"])
        self.assertIsNone(f["log"])
        self.deployed("dist")
        self.task("Two")
        self.deployed("test")
        self.assertEqual(self.titles("dist"), ["Two"])
        self.assertEqual(self.facts("dist")["behind"], 1)

    def test_a_target_without_release_tags_is_unchanged(self):  # (e)
        git(self.repo, "tag", "v1.0.0")
        self.task("One")
        self.assertEqual(self.titles("plain"), [])
        self.assertIsNone(self.facts("plain")["since"])
        self.deployed("plain")
        self.task("Two")
        self.assertEqual(self.titles("plain"), ["Two"])
        f = self.facts("plain")
        self.assertIsNone(f["since"])
        self.assertEqual(f["behind"], 1)


if __name__ == "__main__":
    unittest.main()
