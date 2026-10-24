"""The drag handle that resizes the Today view's right column (README "Today view", Layout), checked statically in
dashboard.html. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import re
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"


class TodayRightColumnResize(unittest.TestCase):
    page = PAGE.read_text()

    def test_handle_element(self):
        self.assertEqual(len(re.findall(r'id="tdgrip"', self.page)), 1)
        self.assertIn('$("#tdgrip").onpointerdown', self.page)
        self.assertIn('$("#tdgrip").ondblclick', self.page)

    def test_width_is_kept_under_its_key(self):
        for use in ('getItem("ops-td-right")', 'setItem("ops-td-right"', 'removeItem("ops-td-right")'):
            self.assertIn(use, self.page)

    def test_grid_uses_the_variable(self):
        self.assertIn("grid-template-columns: 300px minmax(0, 1fr) var(--td-right, 320px)", self.page)
        self.assertIn('setProperty("--td-right"', self.page)

    def test_hidden_when_columns_stack(self):
        stack = self.page[self.page.index("@media (max-width: 1099px) { #todayview"):]
        self.assertIn("#tdgrip { display: none; }", stack[:stack.index("\n@media")])


if __name__ == "__main__":
    unittest.main()
