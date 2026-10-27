"""Today's left column (README TODAY-1): Projects sits first, then the to-dos, Parked and the Topics row; Projects
and To-do each fold like Parked, with a count shown while folded, and the open state is kept in TD and localStorage
so it survives a reload and the re-render on every poll. Checked statically in dashboard.html.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import re
import unittest
from pathlib import Path

page = (Path(__file__).resolve().parent.parent / "dashboard.html").read_text()


def fn(name):
    a = page.index(f"function {name}(")
    return page[a:page.index("\n}\n", a)]


class LeftColumn(unittest.TestCase):
    def test_order(self):
        body = fn("tdLeft")
        marks = ["tdProjects()", 'class="tdfold tdtodo"', 'class="tdchips"', "tdAddForm()", "shown.map(tdRow)",
                 'class="tdparked"', "tdTopicsRow()"]
        at = [body.index(m) for m in marks]
        self.assertEqual(at, sorted(at))

    def test_sections_fold_with_count(self):
        self.assertRegex(fn("tdLeft"), r'<details class="tdfold tdtodo" \$\{TD\.todoOpen \? "open" : ""\}><summary>'
                                       r'To-do\$\{pg \? .*?\} <span class="cnt">\(\$\{shown\.length\}\)')
        self.assertRegex(fn("tdProjects"), r'<details class="tdfold tdprojs" \$\{TD\.projsOpen \? "open" : ""\}>'
                                           r'<summary>Projects <span class="cnt">')
        self.assertIn("details.tdfold[open] > summary .cnt { display: none; }", page)

    def test_open_state_kept(self):
        self.assertRegex(page, r"projsOpen: true, todoOpen: true")
        for key, k in [("ops-td-projs-open", "projsOpen"), ("ops-td-todo-open", "todoOpen")]:
            self.assertIn(f'TD.{k} = localStorage.getItem("{key}") !== "0"', page)
            self.assertIn(f'localStorage.setItem("{key}", TD.{k} ? "1" : "0")', page)
        self.assertRegex(fn("tdLeft"), r'\["\.tdprojs", "projsOpen"\], \["\.tdtodo", "todoOpen"\]\]\)\s*\n\s*if .*'
                                       r'ontoggle = e => \{ if \(TD\[k\] !== e\.target\.open\) \{ TD\[k\] = e\.target\.open; tdSave\(\);')


if __name__ == "__main__":
    unittest.main()
