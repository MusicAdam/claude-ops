"""Reply blocks (queue, todo, schedule, topic) close only on a fence on its own line.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OPS_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


class ReplyBlocks(unittest.TestCase):
    def test_fence_inside_json_string(self):
        for lang in ("queue", "todo", "schedule", "topic"):
            rx = getattr(opsd, lang.upper() + "_BLOCK")
            body = json.dumps([{"body": "mention ```todo and ``` mid-line"}])
            reply = f"hi\n```{lang}\n{body}\n```\nafter\n```{lang}\n[1]\n```"
            found = rx.findall(reply)
            self.assertEqual(len(found), 2, lang)
            self.assertIn("```todo", json.loads(found[0])[0]["body"])
            self.assertEqual(rx.sub("", reply).strip(), "hi\n\nafter", lang)

    def test_closes_at_end_of_text(self):
        self.assertEqual(len(opsd.QUEUE_BLOCK.findall("```queue\n[]\n```")), 1)


if __name__ == "__main__":
    unittest.main()
