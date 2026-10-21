"""The Analyze tab's numbers and suggestions (opsd.analysis), from seeded tasks and events. Stdlib only.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["OPS_DATA"] = tempfile.mkdtemp()
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47961")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

H = 3600


def seed(project, cost, kind="task", state="done", model="claude-opus-5-5", effort=None, ago=H * 30, events=(),
         length=H):
    """A task created ago seconds back that started 10 min later and ended length seconds after that, with events at
    (offset, text) seconds after its start."""
    created = time.time() - ago
    started, finished = created + 600, created + 600 + length
    tid = opsd.q("INSERT INTO tasks (project, title, kind, state, cost, created, started, finished, session_model,"
                 " effort) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (project, f"{kind} {cost}", kind, state, cost, created, started, finished, model, effort))
    tid = opsd.q("SELECT MAX(id) AS id FROM tasks", one=True)["id"]
    for off, text in events:
        opsd.q("INSERT INTO events (task_id, ts, text) VALUES (?,?,?)", (tid, started + off, text))
    return tid


class Analyze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        fix = [(600, "gate failed: `make test` exited 1"), (601, "fix round 1: gate failed:")]
        for c in (1.0, 1.2, 0.9):
            seed("a", c)
        seed("a", 4.0, events=fix)
        seed("a", 5.0, events=fix + [(900, "gate failed: `make test` exited 1"), (901, "fix round 2: gate failed:")])
        seed("b", 3.0, state="failed")
        # waited 2 h for an answer: that time is not run time, and the next turn started cold
        seed("b", 2.0, events=[(60, "needs your input: which?"), (60 + 2 * H, "answered: this")], length=3 * H)
        for c in (2.0, 2.5):
            seed("b", c, kind="release")
        seed("a", 9.0, ago=H * 24 * 20)  # outside 7 days

    def test_period_and_totals(self):
        a = opsd.analysis("7")
        self.assertEqual(a["summary"]["n"], 9)
        self.assertAlmostEqual(a["summary"]["cost"], 21.6)
        self.assertAlmostEqual(a["summary"]["wasted"], 3.0)
        self.assertEqual(opsd.analysis("all")["summary"]["n"], 10)
        self.assertEqual(opsd.analysis("7", "b")["summary"]["n"], 4)
        self.assertEqual({p["project"] for p in a["per_project"]}, {"a", "b"})

    def test_waiting_is_not_run_time(self):
        a = opsd.analysis("7", "b")
        t = next(r for r in a["slowest"] if r["cost"] == 2.0 and r["kind"] == "task")
        self.assertAlmostEqual(t["ran"], H, delta=1)
        self.assertEqual(a["colds"], {"your answer": 1})
        self.assertEqual(t["turns"], 2)

    def test_breakdowns(self):
        a = opsd.analysis("7")
        fixes = {g["key"]: g["n"] for g in a["by_fixes"]}
        self.assertEqual(fixes, {"0": 7, "1": 1, "2+": 1})
        self.assertEqual([g["key"] for g in a["by_fixes"]], ["0", "1", "2+"])
        self.assertEqual(a["gate_causes"][0]["cause"], "`make test` exited 1")
        self.assertEqual(a["gate_causes"][0]["n"], 3)
        self.assertEqual({g["key"] for g in a["by_kind"]}, {"task", "release"})

    def test_suggestions(self):
        opsd.set_setting("model:release", "")  # Opus releases are only worth a tip while releases still start on Opus
        texts = " ".join(s["text"] for s in opsd.analysis("7")["suggestions"])
        self.assertIn("gate-fix round cost", texts)
        self.assertIn("Opus on release checks", texts)
        self.assertIn("prompt cache had expired", texts)
        self.assertIn("Failed and cancelled tasks cost $3.00", texts)
        opsd.set_setting("model:release", "sonnet")
        self.assertNotIn("Opus on release", " ".join(s["text"] for s in opsd.analysis("7")["suggestions"]))


if __name__ == "__main__":
    unittest.main()
