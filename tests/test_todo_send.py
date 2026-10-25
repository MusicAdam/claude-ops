"""dashboard.html: a message sent from a to-do step carries that to-do (README TODAY-2 rule 5, conv_say's todo).
Runs the page's own convTodo in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- the to-do a message is sent from", "// ---- end of the to-do a message is sent from"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(args => convTodo(...args))));
"""
TASK = {"key": "todo:20", "verb": "Do", "project": "research", "title": "Paste the schedule", "why": "A copy.",
        "subject": "task:313", "source": {"kind": "task", "id": 313}}


@unittest.skipUnless(shutil.which("node"), "needs node")
class TodoSend(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_seen_before_carries_the_subject_without_pick(self):
        self.assertEqual(self.run_js([TASK, "research", "todo:20|research"]),
                         [{"key": "todo:20", "verb": "Do", "title": "Paste the schedule", "why": "A copy.",
                           "subject": "task:313"}])

    def test_not_picked_yet_says_pick(self):  # a failed or racing open, or after the project filter changed
        for applied in ("", "todo:20|other", "todo:9|research"):
            self.assertTrue(self.run_js([TASK, "research", applied])[0]["pick"])

    def test_no_subject_still_sends_title_and_why(self):
        it = {**TASK, "subject": None, "source": {"kind": "project", "id": None}}
        self.assertEqual(self.run_js([it, "research", ""]),
                         [{"key": "todo:20", "verb": "Do", "title": "Paste the schedule", "why": "A copy."}])

    def test_none_for_a_chat_item_another_project_or_no_step(self):
        chat = {**TASK, "subject": None, "source": {"kind": "chat", "id": 5}}
        self.assertEqual(self.run_js([chat, "research", ""], [TASK, "claude-ops", ""], [None, "research", ""],
                                     [TASK, "", ""]), [None, None, None, None])


if __name__ == "__main__":
    unittest.main()
