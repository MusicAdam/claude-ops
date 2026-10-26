"""dashboard.html: only an attached to-do (the 📎 chip) reaches a chat (README TODAY-2, attaching a to-do; conv_say's
todo). A step showing in the dock carries nothing. Runs the page's own convTodo and convSay in node with stubs;
skipped without node. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- the to-do a message is sent from", "// ---- end of the to-do a message is sent from"
# Each case is {item, chip, project}: TD.item is the step in the dock, TD.chip the attached to-do. Prints what
# convSay posted and the chip after it, per case.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const code = h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4]));
(async () => {
  const out = [];
  for (const c of JSON.parse(process.argv[2])) {
    const posts = [], el = {value: "hi", textContent: ""};
    const TD = {item: c.item, chip: c.chip, starter: ""};
    const run = new Function("TD", "$", "api", "convProject", "renderSlash", "renderChip", "refresh", "refreshDesign",
      "toBottom", "let chatKey;" + code + "; return convSay;");
    const say = run(TD, () => el, async (p, b) => { posts.push([p, b]); return {}; }, () => c.project, () => {},
      () => {}, () => {}, async () => {}, () => {});
    await say("hi");
    out.push({posts, chip: TD.chip});
  }
  console.log(JSON.stringify(out));
})();
"""
STEP = {"key": "todo:20", "verb": "Do", "project": "research", "title": "Paste the schedule", "why": "A copy.",
        "subject": "task:313", "source": {"kind": "task", "id": 313}, "topic": None}
CHIP = {"key": "todo:20", "project": "research", "verb": "Do", "title": "Paste the schedule", "why": "A copy."}


@unittest.skipUnless(shutil.which("node"), "needs node")
class TodoSend(unittest.TestCase):
    def run_js(self, *cases):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_attached_chip_is_sent_once_then_clears(self):
        [r] = self.run_js({"item": STEP, "chip": CHIP, "project": "research"})
        self.assertEqual(r["posts"], [["/api/conversation/say", {"project": "research", "text": "hi", "todo": {
            "key": "todo:20", "verb": "Do", "title": "Paste the schedule", "why": "A copy."}}]])
        self.assertIsNone(r["chip"])

    def test_a_step_showing_carries_nothing(self):
        """The old rule 5: a message sent while a step showed carried that to-do, with its subject and pick."""
        [r] = self.run_js({"item": STEP, "chip": None, "project": "research"})
        self.assertEqual(r["posts"], [["/api/conversation/say", {"project": "research", "text": "hi"}]])

    def test_no_subject_or_pick_even_for_a_subject_item(self):
        [r] = self.run_js({"item": STEP, "chip": {**CHIP, "subject": "task:313"}, "project": "research"})
        self.assertEqual(set(r["posts"][0][1]["todo"]), {"key", "verb", "title", "why"})

    def test_never_into_another_projects_conversation(self):
        [r] = self.run_js({"item": STEP, "chip": CHIP, "project": "claude-ops"})
        self.assertEqual(r["posts"], [["/api/conversation/say", {"project": "claude-ops", "text": "hi"}]])
        self.assertEqual(r["chip"], CHIP)  # it stays for its own project's conversation

    def test_home_or_all_clear_sends_none(self):
        [r] = self.run_js({"item": None, "chip": None, "project": "research"})
        self.assertNotIn("todo", r["posts"][0][1])


if __name__ == "__main__":
    unittest.main()
