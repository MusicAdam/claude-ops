"""The merge into dev saves and discards stray uncommitted changes to tracked files in the dev worktree (a build's
pod install rewrote Podfile.lock), instead of failing. Untracked files there stay.
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import shutil
import subprocess
import sys
import tempfile
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


class DevUncommitted(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "Podfile.lock").write_text("checksum: aaa\n")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"du{self.repo.parent.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\ndirect_main = false\n')
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        self.ids = []
        for m in (mock.patch.object(opsd, "notify"), mock.patch.object(opsd, "worker_prompt", return_value="brief")):
            m.start()
            self.addCleanup(m.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids)
        for tid in self.ids:  # ids come round again in the next test
            shutil.rmtree(opsd.RUNS / str(tid), ignore_errors=True)
        self.toml.unlink()
        opsd.load_projects()

    def run_task(self, edits):
        tid = opsd.add_task(self.p.name, "body", f"task {len(self.ids)}")
        self.ids.append(tid)
        opsd.upd(tid, state="running")

        def worker(p, tid, prompt):
            wt = Path(opsd.task(tid)["worktree"])
            for rel, text in edits.items():
                (wt / rel).write_text(text)
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "-m", "edit")
            return {"status": "done", "summary": "edited"}
        with mock.patch.object(opsd, "run_claude", side_effect=worker):
            opsd._pipeline(self.p, tid)
        t = opsd.task(tid)
        self.assertEqual(t["state"], "done", t["error"])
        return tid

    def events(self, tid):
        return [r["text"] for r in opsd.q("SELECT text FROM events WHERE task_id=?", (tid,))]

    def test_stray_change_is_saved_and_discarded(self):
        dev = self.p.dev_wt
        (dev / "Podfile.lock").write_text("checksum: bbb\n")  # a build in the dev worktree rewrote it
        (dev / "notes.txt").write_text("untracked\n")
        old = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=dev, capture_output=True).stdout
        tid = self.run_task({"Podfile.lock": "checksum: ccc\n"})
        patch = opsd.RUNS / str(tid) / "dev-uncommitted-root.patch"
        self.assertEqual(patch.read_bytes(), old)
        self.assertIn(b"checksum: bbb", old)
        self.assertEqual(git(self.repo, "show", "dev:Podfile.lock"), "checksum: ccc")
        self.assertEqual((dev / "Podfile.lock").read_text(), "checksum: ccc\n")
        self.assertEqual((dev / "notes.txt").read_text(), "untracked\n")  # never git clean
        self.assertIn(f"discarded uncommitted changes in {dev}: Podfile.lock; saved to {patch} "
                      f"(git apply it to get them back)", self.events(tid))

    def test_clean_dev_writes_no_patch(self):
        tid = self.run_task({"b.txt": "b two\n"})
        self.assertEqual(list((opsd.RUNS / str(tid)).glob("dev-uncommitted-*")), [])
        self.assertFalse(any("discarded uncommitted" in e for e in self.events(tid)))

    def test_merge_main_into_dev_saves_them_too(self):
        tid = opsd.add_task(self.p.name, "body", "release")
        self.ids.append(tid)
        (self.repo / "Podfile.lock").write_text("checksum: main\n")
        git(self.repo, "commit", "-q", "-am", "straight to main")
        (self.p.dev_wt / "Podfile.lock").write_text("checksum: build\n")
        with self.p.lock:
            self.assertIsNone(opsd.merge_main_into_dev(self.p, tid))
        self.assertIn(b"checksum: build", (opsd.RUNS / str(tid) / "dev-uncommitted-root.patch").read_bytes())
        self.assertEqual((self.p.dev_wt / "Podfile.lock").read_text(), "checksum: main\n")


if __name__ == "__main__":
    unittest.main()
