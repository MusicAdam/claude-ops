"""Play dev runs its steps in order: [worktree] setup and [app] build finish before [app] launch starts, and when
setup or build is to run under an app that runs there, the app is stopped first and launched fresh after.
launch_app refuses while setup still runs in the worktree, or after its setup failed. Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


# Every step appends a line to ev.log in the worktree. Setup takes a while and rewrites deps/lib (as an npm install
# rewrites node_modules); launch records what it found there, so a launch that raced setup would see no "v2".
CONFIG = """
[worktree]
setup = ["sleep 1; mkdir -p deps; echo v2 > deps/lib; echo setup >> ev.log"]
[app]
build = "echo build >> ev.log"
launch = "touch app.flag; echo launch $(cat deps/lib 2>/dev/null || echo missing) >> ev.log"
running = "test -f app.flag"
stop = "rm -f app.flag; echo stop >> ev.log"
"""


class PlayOrder(unittest.TestCase):
    def setUp(self):
        opsd.init_db()
        self.repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"play{self.repo.parent.name[-6:].lower()}"
        self.toml = opsd.PROJECTS_DIR / f"{name}.toml"
        self.toml.write_text(f'repo = "{self.repo}"\nworktrees = "{self.repo.parent / "wt"}"\n' + CONFIG)
        opsd.load_projects()
        self.p = opsd.PROJECTS[name]
        opsd.bootstrap(self.p)
        self.dev = self.p.dev_wt
        self.addCleanup(lambda: (self.toml.unlink(), opsd.load_projects()))

    def events(self, until="launch", timeout=15):
        """The lines of ev.log once one starts with until ([app] launch runs in the background)."""
        log = self.dev / "ev.log"
        end = time.time() + timeout
        while time.time() < end:
            lines = log.read_text().splitlines() if log.exists() else []
            if any(l.startswith(until) for l in lines):
                return lines
            time.sleep(0.05)
        self.fail(f"no {until} in ev.log: {lines}")

    def play_dev(self):
        opsd.play_run(self.p, "dev")
        self.assertIsNone((opsd.PLAYS.get(self.p.name) or {}).get("error"), opsd.PLAYS.get(self.p.name))

    def test_launch_waits_for_setup_and_build(self):
        self.play_dev()
        self.assertEqual(self.events(), ["setup", "build", "launch v2"])

    def test_setup_under_a_running_app_stops_it_first(self):
        (self.dev / "app.flag").touch()  # the app runs from dev, serving the tree setup is about to rewrite
        self.play_dev()
        self.assertEqual(self.events(), ["stop", "setup", "build", "launch v2"])
        self.assertTrue((self.dev / "app.flag").exists())  # and it runs again

    def test_build_under_a_running_app_stops_it_first(self):
        self.play_dev()
        self.events()
        (self.dev / "ev.log").unlink()
        (self.dev / "b.txt").write_text("two\n")  # dev moves on: Play dev builds again, setup is done
        git(self.dev, "add", ".")
        git(self.dev, "commit", "-q", "-m", "more")
        self.play_dev()
        self.assertEqual(self.events(), ["stop", "build", "launch v2"])

    def test_nothing_to_change_leaves_the_app_to_launch(self):
        self.play_dev()
        self.events()
        (self.dev / "ev.log").unlink()
        self.play_dev()
        self.assertEqual(self.events(), ["launch v2"])

    def test_launch_refused_while_setup_runs(self):
        self.play_dev()
        self.events()
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        opsd.set_setting(f"setup_pid:{self.dev}", f"{sleeper.pid} {opsd.started_at(sleeper.pid)}")
        self.addCleanup(opsd.set_setting, f"setup_pid:{self.dev}", "")
        with self.assertRaisesRegex(ValueError, "setup is still running"):
            opsd.launch_app(self.p, self.dev)

    def test_launch_refused_after_setup_failed(self):
        self.play_dev()
        self.events()
        opsd.set_setting(f"prepared:{self.dev}", "")  # setup started and never succeeded
        with self.assertRaisesRegex(ValueError, "has not succeeded"):
            opsd.launch_app(self.p, self.dev)


class StampedLog(unittest.TestCase):
    def test_each_line_starts_with_the_time(self):
        import io
        out = io.StringIO()
        s = opsd.Stamped(out)
        print("one", file=s)
        s.write("two\nthr")
        s.write("ee\n")
        lines = out.getvalue().splitlines()
        self.assertEqual([l[20:] for l in lines], ["one", "two", "three"])
        for l in lines:
            time.strptime(l[:19], "%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    unittest.main()
