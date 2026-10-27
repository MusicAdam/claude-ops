"""Phone Today keeps what desktop Today has (README "Today on a phone", Parity), checked statically in dashboard.html:
the conversation's head is wrapped, not cut off; Details ▾ shows a long step title whole; a minimised quick chat
doesn't cover the send button. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
page = (ROOT / "dashboard.html").read_text()
readme = (ROOT / "README.md").read_text()


def phone_css(start):
    """The `@media (max-width: 699px)` block at or after start, without its comments."""
    a = page.index("@media (max-width: 699px) {", page.index(start))
    depth, i = 0, page.index("{", a)
    while True:
        depth += {"{": 1, "}": -1}.get(page[i], 0)
        if depth == 0:
            return re.sub(r"/\*.*?\*/", "", page[a:i + 1], flags=re.S)
        i += 1


TODAY = phone_css("/* Today on a phone")
QC = phone_css("/* Phones: full screen, no drag or resize")


def rule(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else None


class ChatHead(unittest.TestCase):
    """Where a chat runs, its resume command, Sent by, Read the change and a design's Documents sit in #chathead."""

    def test_wraps_and_scrolls_instead_of_cutting_off(self):
        r = rule(TODAY, "#tdcentre #chathead")
        self.assertIsNotNone(r)
        self.assertNotIn("nowrap", r)
        self.assertNotIn("ellipsis", r)
        self.assertIn("overflow: auto", r)
        self.assertIn("overflow-wrap: anywhere", r)
        self.assertIn("max-height", r)

    def test_documents_are_in_it(self):
        self.assertIn('$("#chathead").insertAdjacentHTML("beforeend", `<div class="docs">Documents:', page)


class StepTitle(unittest.TestCase):
    def test_clamped_title_opens_with_details(self):
        self.assertIn("-webkit-line-clamp: 2", rule(TODAY, ".stepcard h2"))
        self.assertIn("display: block", rule(TODAY, ".stepcard.open h2"))

    def test_long_title_alone_brings_details(self):
        self.assertIn("const dets = !!(it.why || facts || rv || it.title.length > 50)", page)
        self.assertIn('${dets ? `<button class="ph" data-dets', page)

    def test_sticky_top_row_covers_only_the_card_padding(self):
        self.assertIn("padding: 6px 10px 8px", rule(TODAY, ".stepcard"))
        self.assertIn("box-shadow: 0 -6px 0 var(--panel)", TODAY)


class QuickChatPill(unittest.TestCase):
    def test_pill_hidden_on_phone_today(self):
        self.assertIn("display: none", rule(QC, "body.td #qcpill"))
        self.assertIsNotNone(rule(QC, "body.td #askbtn.qcmin"))
        self.assertIsNotNone(rule(QC, "body.td #askbtn.qcdot::after"))

    def test_ask_button_follows_the_pill(self):
        a = page.index("function qcRender()")
        body = page[a:page.index("\n}\n", a)]
        self.assertIn('$("#askbtn").classList.toggle("qcmin", QC.on && QC.min)', body)
        self.assertIn('$("#askbtn").classList.toggle("qcdot", QC.min && pl.dot)', body)
        self.assertIn('$("#askbtn").classList.remove("qcdot")', body)
        self.assertIn('$("#askbtn").onclick = () => qcOpen();', page)  # qcOpen also restores a minimised one


class Readme(unittest.TestCase):
    def test_parity_note(self):
        s = readme[readme.index("### Today on a phone"):]
        s = s[:s.index("\n## ")]
        self.assertIn("PHONE-6 Parity", s)
        self.assertIn("below 700px", s)


if __name__ == "__main__":
    unittest.main()
