"""Task detail shows why a queued task waits: GET /api/tasks/<id> carries `wait` for a queued task, null otherwise.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import sys
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("OPS_DATA", tempfile.mkdtemp())
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class WaitInDetail(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        repo = Path(tempfile.mkdtemp()) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = f"wd{repo.parent.name[-6:].lower()}"
        toml = opsd.PROJECTS_DIR / f"{self.name}.toml"
        toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n')
        self.addCleanup(lambda: (toml.unlink(), opsd.load_projects()))
        opsd.load_projects()
        self.ids = []
        self.addCleanup(lambda: opsd.q(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(self.ids))})", self.ids))

    def add(self, state, depends_on=None):
        tid = opsd.q("INSERT INTO tasks (project, title, body, created, state, depends_on) VALUES "
                     "(?, 't', '', 0, ?, ?)", (self.name, state, depends_on))
        self.ids.append(tid)
        return tid

    def get(self, tid):
        url = f"http://127.0.0.1:{self.srv.server_port}/api/tasks/{tid}"
        with urllib.request.urlopen(url) as r:
            return json.load(r)

    def test_queued_behind_unmerged_dependency(self):
        dep = self.add("running")
        tid = self.add("queued", depends_on=dep)
        self.assertTrue(self.get(tid)["wait"].startswith("waiting for #"))
        self.assertIn(f"#{dep}", self.get(tid)["wait"])

    def test_not_queued_has_no_wait(self):
        for state in ("running", "done"):
            self.assertIsNone(self.get(self.add(state))["wait"])


if __name__ == "__main__":
    unittest.main()
