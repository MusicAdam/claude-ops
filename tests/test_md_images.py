"""Pictures in chat replies: dashboard.html's md() turns ![alt](path) into an image, and a disk path into the data
folder into the URL the dashboard serves it at. Runs the page's own markdown code in node; skipped without node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
# The markdown code runs from "const MD = {" to md(); mdMissing between them needs a DOM, so it is left out.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const MD = {"), b = h.indexOf("// A picture in a reply that doesn't load");
const c = h.indexOf("function md(src"), e = h.indexOf("\n}\n", c) + 3;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let state = {data_dir: "/srv/ops"};
eval(h.slice(a, b) + h.slice(c, e));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(t => md(t))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class MdImages(unittest.TestCase):
    def md(self, *texts):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(texts)], capture_output=True, text=True,
                             check=True).stdout
        return json.loads(out)

    def test_image_from_the_images_folder(self):
        for path in ("/Users/adam/.claude-ops/images/shot-1.png", "~/.claude-ops/images/shot-1.png",
                     "/srv/ops/images/shot-1.png"):
            html, = self.md(f"![New screens]({path})")
            self.assertIn('<img class="mdimg" src="/api/images/shot-1.png" alt="New screens"', html)
            self.assertNotIn("!", html)

    def test_chat_folder_path_and_plain_link(self):
        img, link = self.md("![d](/srv/ops/designs/7/flow.svg)", "[shots](/srv/ops/images/a.png)")
        self.assertIn('src="/api/designs/7/files/flow.svg"', img)
        self.assertIn('<a href="/api/images/a.png"', link)

    def test_other_paths_and_unsafe_schemes(self):
        other, bad, escaped = self.md("![x](/tmp/x.png)", "![x](javascript:alert(1))", r"\![x](y.png)")
        self.assertIn('src="/tmp/x.png"', other)  # left as written; mdMissing says it isn't there
        self.assertEqual(bad, "<p>x</p>")
        self.assertNotIn("<img", escaped)


if __name__ == "__main__":
    unittest.main()
