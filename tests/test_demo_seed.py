"""scripts/demo_seed.py, the seed step of scripts/demo.sh: it boots a throwaway daemon in a temp folder, runs the sample
project's tasks and chat through the real pipeline with the stub claude, and retimes them. /api/state must then show
the recipe-box project and the task mix the README's screenshots need, and nothing from the real ~/.claude-ops.
No Playwright needed. Skips without git. Stdlib only. Run from the repo root:
/opt/homebrew/bin/python3 -m unittest tests.test_demo_seed -q"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import demo_seed  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "needs git")
class DemoSeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = demo_seed.Demo(root=tempfile.mkdtemp(prefix="ops-demo-test-"))
        try:
            cls.demo.start().seed().retime()
            cls.state = cls.demo.state()
        except Exception:
            log = cls.demo.root / "opsd.log"
            print(log.read_text()[-3000:] if log.exists() else "no daemon log", file=sys.stderr)
            cls.demo.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.demo.stop()

    def test_project(self):
        self.assertEqual([p["name"] for p in self.state["projects"]], ["recipe-box"])
        self.assertTrue(self.demo.data.is_relative_to(self.demo.root))  # its own OPS_DATA, never ~/.claude-ops

    def test_task_mix(self):
        by = {}
        for t in self.state["tasks"]:
            by.setdefault(t["state"], []).append(t)
        self.assertEqual(len(by.get("done", [])), 5)
        self.assertEqual([t["title"] for t in by["needs_input"]], ["Import a recipe from a web page"])
        self.assertEqual([t["title"] for t in by["review"]], ["Shopping list for several recipes"])
        self.assertEqual([t["title"] for t in by["running"]], ["Add a random command for tonight's dinner"])
        queued = by["queued"]
        self.assertEqual(len(queued), 3)
        self.assertTrue(all(t.get("depends_on") for t in queued))  # each waits on another task
        self.assertTrue(all(t.get("summary") for t in by["done"]))

    def test_chat_queued_tasks(self):
        did = self.demo.chats[0]
        with self.demo.db() as c:
            ids = c.execute("SELECT task_ids FROM design_msgs WHERE design_id=? AND role='assistant'",
                            (did,)).fetchone()[0]
        self.assertEqual(len(demo_seed.json.loads(ids)), 2)

    def test_todo_and_schedule(self):
        self.assertTrue(any("recipe-box" in t["title"] for t in self.state["todos"]))
        self.assertEqual([s["every"] for s in self.state["schedules"]], ["mon 09:00"])

    def test_merged_into_dev(self):
        log = self.demo.git("log", "--oneline", "dev")
        self.assertIn("Merge task #1: Search by ingredient as well as title", log)

    def test_retimed(self):
        first = next(t for t in self.state["tasks"] if t["id"] == 1)
        self.assertAlmostEqual(self.demo.anchor - first["created"], 27 * 3600, delta=60)


if __name__ == "__main__":
    unittest.main()
