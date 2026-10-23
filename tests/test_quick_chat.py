"""The quick chat (README: TODAY-6) in dashboard.html: its window's place and size, the message drawing it shares
with the Today conversation, and the elements it needs. Runs the page's own code in node; skipped without node.
The daemon side (a quick chat never takes over the topic Today shows) is in test_conversation.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
# qcFit and QC_MIN, from "const QC_MIN" to the end of qcFit
FIT = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const QC_MIN"), b = h.indexOf("\n}\n", h.indexOf("function qcFit(")) + 3;
const matchMedia = () => ({matches: false});
eval(h.slice(a, b).replace("const QC_MIN", "globalThis.QC_MIN"));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([g, vw, vh]) => qcFit(g, vw, vh))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class QuickChatWindow(unittest.TestCase):
    def fit(self, *cases):
        out = subprocess.run(["node", "-e", FIT, str(PAGE), json.dumps(cases)], capture_output=True, text=True,
                             check=True).stdout
        return json.loads(out)

    def test_default_is_bottom_right(self):
        g, = self.fit([None, 1440, 900])
        self.assertEqual(g, {"w": 420, "h": 520, "x": 1440 - 420 - 16, "y": 900 - 520 - 16})

    def test_minimum_size(self):
        g, = self.fit([{"x": 10, "y": 10, "w": 100, "h": 50}, 1440, 900])
        self.assertEqual((g["w"], g["h"]), (320, 300))

    def test_kept_inside_viewport_after_it_shrinks(self):
        g, = self.fit([{"x": 1300, "y": 800, "w": 420, "h": 520}, 1000, 700])
        self.assertEqual(g, {"w": 420, "h": 520, "x": 580, "y": 180})
        g, = self.fit([{"x": -50, "y": -20, "w": 2000, "h": 2000}, 1000, 700])
        self.assertEqual(g, {"w": 1000, "h": 700, "x": 0, "y": 0})


class QuickChatPage(unittest.TestCase):
    """The page has the window and its own elements, so it never draws into or sends from the Today conversation."""
    page = PAGE.read_text()

    def test_elements(self):
        for el in ("askbtn", "qc", "qcbar", "qcproj", "qctitle", "qcmin", "qcx", "qcmsgs", "qcsay", "qcsend",
                   "qcpill"):
            self.assertEqual(len(re.findall(f'id="{el}"', self.page)), 1, el)

    def test_window_leaves_today_alone(self):
        a = self.page.index("// ---------------------------------------------------------------- quick chat")
        js = self.page[a:self.page.index("// ---------------------------------------------------------------- ", a + 10)]
        js = re.sub(r"^\s*//.*$", "", js, flags=re.M)  # the code, not the comments that name what it leaves alone
        for today in ('$("#say")', '$("#msgs")', "chatSel", "chatKey", "TD.", "setFilter", "convOpen", "/api/conversation"):
            self.assertNotIn(today, js)
        self.assertIn('quick: true', js)  # the daemon keeps the topic Today shows

    def test_both_chats_share_the_message_drawing(self):
        self.assertEqual(self.page.count("innerHTML = topicDivider() + msgsHtml(d, links)"), 1)
        self.assertEqual(self.page.count("box.innerHTML = msgsHtml(d, links)"), 1)
