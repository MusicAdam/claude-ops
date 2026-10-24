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
# The window's pure helpers (qcFit, qcRestore, qcAfterClose, qcPill …), from "const QC_MIN" to the marker after them.
# The cases are [function name, ...arguments]; a qcPill title argument is a {project: title} map.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const QC_MIN"), b = h.indexOf("// (end of the pure helpers");
const matchMedia = () => ({matches: false});
const f = new Function(h.slice(a, b) + "; return {qcFit, qcRestore, qcCanAdd, qcAfterClose, qcToEnd, qcUnread, qcPill, QC_MAX};")();
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([name, ...args]) =>
  name === "qcPill" ? f.qcPill(args[0], p => args[1][p]) : name === "QC_MAX" ? f.QC_MAX : f[name](...args))));
"""


def run(*cases):
    out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(cases)], capture_output=True, text=True,
                         check=True).stdout
    return json.loads(out)


@unittest.skipUnless(shutil.which("node"), "needs node")
class QuickChatWindow(unittest.TestCase):
    def fit(self, *cases):
        return run(*(["qcFit", *c] for c in cases))

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


def topic(*roles, state="idle"):
    return {"state": state, "msgs": [{"role": r} for r in roles]}


@unittest.skipUnless(shutil.which("node"), "needs node")
class QuickChatTabs(unittest.TestCase):
    """Several chats in the one window: what ops-qc restores, the tab limit, closing tabs and the minimised pill."""

    def test_old_shape_is_one_tab(self):
        r, = run(["qcRestore", {"project": "ops", "did": 12, "min": True, "seen": 4}])
        self.assertEqual(r, {"tabs": [{"project": "ops", "did": 12, "seen": 4}], "active": 0, "min": True})

    def test_tabs_shape_round_trips(self):
        k = {"tabs": [{"project": "a", "did": None, "seen": 0}, {"project": "b", "did": 7, "seen": 3}],
             "active": 1, "min": False}
        r, = run(["qcRestore", k])
        self.assertEqual(r, k)

    def test_restore_nothing_or_junk(self):
        self.assertEqual(run(["qcRestore", None], ["qcRestore", {"tabs": []}], ["qcRestore", "x"]), [None, None, None])
        r, = run(["qcRestore", {"tabs": [{"project": "a"}, {"project": "b"}], "active": 9}])
        self.assertEqual(r["active"], 1)  # an active index past the end is the last tab

    def test_at_most_six_tabs(self):
        tabs = lambda n: [{"project": "p", "did": None, "seen": 0}] * n
        self.assertEqual(run(["QC_MAX"], ["qcCanAdd", tabs(5)], ["qcCanAdd", tabs(6)]), [6, True, False])
        r, = run(["qcRestore", {"tabs": tabs(8)}])
        self.assertEqual(len(r["tabs"]), 6)

    def test_closing_tabs(self):
        self.assertEqual(run(["qcAfterClose", 1, 0, 0]), [-1])  # the last tab closes the window
        self.assertEqual(run(["qcAfterClose", 3, 1, 1], ["qcAfterClose", 3, 2, 2], ["qcAfterClose", 2, 0, 0]),
                         [1, 1, 0])  # the active one: its right-hand neighbour, or its left when it was last
        self.assertEqual(run(["qcAfterClose", 4, 2, 0], ["qcAfterClose", 4, 1, 3]), [1, 1])  # another: same tab

    def test_closing_ends_topics(self):
        # × on a tab ends its topic; × on the window ends every tab's. A tab with no message sent yet has none.
        tab = lambda did: {"project": "demo", "did": did, "seen": 0}  # noqa: E731
        self.assertEqual(run(["qcToEnd", [tab(7)]], ["qcToEnd", [tab(None)]]), [[7], []])
        self.assertEqual(run(["qcToEnd", [tab(3), tab(None), tab(9), tab(3)]]), [[3, 9]])  # window ×: each once
        self.assertEqual(run(["qcToEnd", []]), [[]])

    def test_unread(self):
        cases = [
            {"d": topic("user", "assistant"), "seen": 1},              # a reply since: unread
            {"d": topic("user", "assistant"), "seen": 2},              # seen it
            {"d": topic("user", "assistant", "user"), "seen": 2},      # your message last
            {"d": topic("user", "assistant", state="thinking"), "seen": 1},  # still replying
            {"d": None, "seen": 0},                                    # not loaded or no topic yet
        ]
        self.assertEqual(run(*(["qcUnread", c] for c in cases)), [True, False, False, False, False])

    def test_pill(self):
        titles = {"ops": "Claude Ops", "game": "Disk Game"}
        read, unread = {"project": "game", "d": topic("user", "assistant"), "seen": 2}, \
            {"project": "ops", "d": topic("user", "assistant"), "seen": 1}
        self.assertEqual(run(["qcPill", [read], titles], ["qcPill", [unread], titles],
                             ["qcPill", [read, read, read], titles], ["qcPill", [read, unread], titles]),
                         [{"label": "Disk Game", "dot": False}, {"label": "Claude Ops", "dot": True},
                          {"label": "3 chats", "dot": False}, {"label": "2 chats", "dot": True}])


class QuickChatPage(unittest.TestCase):
    """The page has the window and its own elements, so it never draws into or sends from the Today conversation."""
    page = PAGE.read_text()

    def test_elements(self):
        for el in ("askbtn", "qc", "qcbar", "qcproj", "qctitle", "qcmin", "qcx", "qcmsgs", "qcsay", "qcsend",
                   "qcpill", "qctabs"):
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
