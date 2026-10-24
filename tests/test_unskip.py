"""The Today step shows Unskip for a skipped to-do item (README: TODAY-2, TODO-6). Static check on dashboard.html.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parent.parent / "dashboard.html").read_text()


class Unskip(unittest.TestCase):
    def test_button_for_skipped_and_parked(self):
        line = next(l for l in PAGE.splitlines() if "const unpark = cls" in l)
        self.assertIn("it.parked || it.skipped", line)
        self.assertIn('it.parked ? "Unpark" : "Unskip"', line)
        self.assertIn("data-unpark", line)

    def test_both_layouts_render_it(self):
        self.assertIn('${unpark("ph skip")}', PAGE)
        self.assertIn('${unpark("tdunpark")}', PAGE)

    def test_reuses_unpark_endpoint(self):
        self.assertIn('querySelectorAll("[data-unpark]").forEach(b => b.onclick = tdUnpark)', PAGE)
        self.assertIn('api("/api/todos/unpark"', PAGE)


if __name__ == "__main__":
    unittest.main()
