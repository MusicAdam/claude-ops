"""[worktree] clone entries in an existing worktree: a folder is a warm cache, cloned once; a file follows the owner's
checkout, so a stale copy (the .xcode.env.local that broke app deploys, #437) can't outlive the fix. Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class SyncCloneFiles(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        root = Path(tempfile.mkdtemp(dir=DATA))
        self.repo, self.main, self.dest = root / "repo", root / "main", root / "deploy"
        for d in (self.repo, self.main, self.dest):
            d.mkdir()
        self.p = SimpleNamespace(repo=self.repo, main_wt=self.main, clone=["ios/.xcode.env.local", "node_modules"])
        self.write(self.repo, "ios/.xcode.env.local", "NODE_BINARY=/opt/homebrew/opt/node@22/bin/node\n")
        self.write(self.main, "ios/.xcode.env.local", "NODE_BINARY=/opt/homebrew/Cellar/node@22/22.23.2/bin/node\n")
        self.write(self.main, "node_modules/rn/index.js", "main\n")
        self.write(self.repo, "node_modules/rn/index.js", "mine\n")

    def write(self, root, rel, text):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)

    def sync(self):
        opsd.sync_clone_files(self.p, self.dest)

    def test_a_file_comes_from_your_checkout_not_the_main_worktree(self):
        self.sync()
        self.assertIn("/opt/", (self.dest / "ios/.xcode.env.local").read_text())

    def test_a_stale_file_is_replaced(self):
        self.write(self.dest, "ios/.xcode.env.local", "NODE_BINARY=/opt/homebrew/Cellar/node@22/22.23.2/bin/node\n")
        self.sync()
        self.assertEqual((self.dest / "ios/.xcode.env.local").read_text(),
                         (self.repo / "ios/.xcode.env.local").read_text())

    def test_a_folder_is_cloned_once_from_the_main_worktree(self):
        self.sync()
        self.assertEqual((self.dest / "node_modules/rn/index.js").read_text(), "main\n")
        self.write(self.dest, "node_modules/rn/index.js", "setup patched it\n")
        self.sync()
        self.assertEqual((self.dest / "node_modules/rn/index.js").read_text(), "setup patched it\n")

    def test_a_file_you_deleted_leaves_the_worktree(self):
        self.sync()
        (self.repo / "ios/.xcode.env.local").unlink()
        self.sync()
        self.assertFalse((self.dest / "ios/.xcode.env.local").exists())

    def test_a_file_taken_off_the_list_leaves_the_worktree(self):
        self.sync()
        self.p.clone = ["node_modules"]
        self.sync()
        self.assertFalse((self.dest / "ios/.xcode.env.local").exists())
        self.assertTrue((self.dest / "node_modules/rn/index.js").exists())

    def test_a_file_it_never_cloned_stays(self):
        self.write(self.dest, "ios/.xcode.env.local", "made here\n")
        (self.repo / "ios/.xcode.env.local").unlink()
        self.sync()
        self.assertEqual((self.dest / "ios/.xcode.env.local").read_text(), "made here\n")


if __name__ == "__main__":
    unittest.main()
