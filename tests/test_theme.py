"""Settings > Appearance: the #theme select and the head script that applies ops-theme before first paint."""
import re
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parent.parent / "dashboard.html").read_text()


class ThemeTest(unittest.TestCase):
    def test_select_has_both_options(self):
        m = re.search(r'<select id="theme">(.*?)</select>', PAGE, re.S)
        self.assertTrue(m)
        self.assertEqual(re.findall(r'<option value="(\w+)">', m.group(1)), ["solarized", "dark"])

    def test_head_script_reads_theme_before_style(self):
        head = PAGE[:PAGE.index("</head>")]
        self.assertLess(head.index('getItem("ops-theme")'), head.index("<style>"))

    def test_dark_palette_defined(self):
        self.assertIn(':root[data-theme="dark"]', PAGE)


if __name__ == "__main__":
    unittest.main()
