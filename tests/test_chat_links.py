"""Links in chats (README: Links in chats, LINK-1..7). The page side runs dashboard.html's own markdown code in node
(skipped without node) and checks what each kind of reference becomes; the daemon side checks link_check, which says
which targets exist, and /api/files, which serves a repo file from dev and nothing outside it.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47962")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
# As in test_md_images: the markdown code from "const MD = {" to md(), and userMd after it, for a chat of project
# "demo" that sits at /srv/demo (worktrees in /srv/demo-wt) and is design chat 7, which wrote flow.svg.
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const MD = {"), b = h.indexOf("// A picture in a reply that doesn't load");
const c = h.indexOf("function md(src"), e = h.indexOf("\n// What you wrote in a chat");
const u = h.indexOf("const userMd"), v = h.indexOf("\n", h.indexOf(".join(\"\\n\");", u)) + 1;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let state = {data_dir: "/srv/ops"};
eval(h.slice(a, b) + h.slice(c, e) + h.slice(u, v) + "; globalThis.userMd = userMd;");  // a const stays in eval
const links = {project: "demo", roots: ["/srv/demo", "~/demo", "/srv/demo-wt", "~/demo-wt"], did: 7, files: ["flow.svg"]};
const [user, texts] = JSON.parse(process.argv[2]);
console.log(JSON.stringify(texts.map(t => user ? userMd(t, links) : md(t, undefined, links))));
"""


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("node"), "needs node")
class LinkKinds(unittest.TestCase):
    def md(self, *texts, user=False):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps([user, texts])], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def test_image(self):
        att, shot, bare = self.md("[image: /srv/ops/images/shot-1.png]", "![New screens](~/.claude-ops/images/a.png)",
                                  "see /Users/adam/.claude-ops/images/b.png here", user=False)
        self.assertIn('data-ref="image:shot-1.png" data-href="/api/images/shot-1.png" data-img="att"', att)
        self.assertIn('data-ref="image:a.png" data-href="/api/images/a.png" data-img="mdimg">New screens', shot)
        self.assertIn('data-ref="image:b.png"', bare)
        mine, = self.md("Here:\n[image: /srv/ops/images/c.png]\nthat one", user=True)
        self.assertIn('data-ref="image:c.png"', mine)

    def test_task(self):
        html, = self.md("Done in #225, see also (#9).")
        self.assertIn('data-ref="task:225" data-href="#tasks">#225</span>', html)
        self.assertIn('data-ref="task:9"', html)
        code, url, word = self.md("`#225` and\n\n```\n#226\n```", "https://x.org/p#227", "abc#228 x/#229")
        self.assertNotIn("data-ref", code)  # code spans and fenced code are left alone
        self.assertNotIn("task:227", url)
        self.assertNotIn("data-ref", word)

    def test_repo_file(self):
        html, = self.md("Edit opsd.py:5914 and `docs/design.md`, or /srv/demo-wt/t3/tests/x.py.")
        self.assertIn('data-ref="file:opsd.py:5914" data-href="/api/files?project=demo&path=opsd.py"', html)
        self.assertIn('data-ref="file:docs/design.md"', html)
        self.assertIn("<code>docs/design.md</code></span>", html)
        self.assertIn('data-ref="file:tests/x.py"', html)  # a worktree's absolute path is the repo path
        dotted, = self.md("`state.tasks` e.g. v1.2 and `opsd.py serve`")
        self.assertNotIn("data-ref", dotted)
        quiet, = self.md("Node.js")
        self.assertIn("data-quiet", quiet)  # if there's no such file it stays plain text

    def test_file_with_section(self):
        html, = self.md("Read README.md § Design mode. And `README.md § Research`")
        self.assertIn('data-ref="file:README.md§Design mode"', html)
        self.assertIn('data-ref="file:README.md§Research"', html)

    def test_openspec_path(self):
        html, = self.md("See openspec/changes/add-links/design.md and `app/openspec/changes/x-y`")
        self.assertIn('data-ref="spec:/add-links" data-href="#today"', html)
        self.assertIn('data-ref="spec:app/x-y"', html)

    def test_design_file(self):
        html, = self.md("Wrote `flow.svg` and [the doc](/srv/ops/designs/7/design.md)")
        self.assertIn('data-ref="design:7/flow.svg" data-href="/api/designs/7/files/flow.svg"', html)
        self.assertIn('data-ref="design:7/design.md"', html)

    def test_app_place(self):
        html, = self.md("Open Settings, then the Research tab or the **Design tab**. Your settings stay.")
        self.assertIn('<a href="#settings" data-place>Settings</a>', html)
        self.assertIn('<a href="#research" data-place>Research tab</a>', html)
        self.assertIn('<b><a href="#today" data-place>Design tab</a></b>', html)  # its chats are Today's topics
        self.assertEqual(html.count("data-place"), 3)

    def test_url_trailing_punctuation(self):
        html, = self.md("Go to https://example.com/a(b). Or (https://x.org/y), https://z.dev/q: and https://w.io;")
        for u in ("https://example.com/a(b)", "https://x.org/y", "https://z.dev/q", "https://w.io"):
            self.assertIn(f'<a href="{u}" target="_blank" rel="noopener">{u}</a>', html)

    def test_unservable_link_is_greyed(self):
        html, = self.md("[log](/tmp/x.log)")
        self.assertIn('class="ref bad"', html)
        self.assertNotIn("<a ", html)


class LinkTargets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "a.md").write_text("# A\n\n## Design mode\n\ntext\n")
        (repo / "openspec" / "changes" / "add-links").mkdir(parents=True)
        (repo / "openspec" / "changes" / "add-links" / "proposal.md").write_text("x\n")
        (repo / "code.py").write_text("\n".join(f"x = {i}" for i in range(20)) + "\n")
        (repo / ".gitignore").write_text("secret.txt\n")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        cls.toml = opsd.PROJECTS_DIR / "links.toml"
        cls.toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n')
        opsd.load_projects()
        cls.p = opsd.PROJECTS["links"]
        opsd.bootstrap(cls.p)
        dev = cls.p.dev_wt
        (dev / "secret.txt").write_text("ignored\n")
        (dev / "big.txt").write_text("x" * (opsd.FILE_MAX + 1))
        outside = Path(tempfile.mkdtemp(dir=DATA)) / "outside.txt"
        outside.write_text("not yours\n")
        (dev / "link.txt").symlink_to(outside)
        (opsd.IMAGES).mkdir(parents=True, exist_ok=True)
        (opsd.IMAGES / "shot-1.png").write_bytes(b"\x89PNG")
        (opsd.CHAT_FILES / "7").mkdir(parents=True, exist_ok=True)
        (opsd.CHAT_FILES / "7" / "flow.svg").write_text("<svg/>")
        cls.tid = opsd.add_task("links", "a task", "a task", hold=1)
        cls.srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        opsd.q("DELETE FROM tasks WHERE id=?", (cls.tid,))
        cls.toml.unlink()
        opsd.load_projects()

    def check(self, *refs):
        return opsd.link_check("links", list(refs))

    def test_each_kind_found(self):
        ok = self.check(f"task:{self.tid}", "file:docs/a.md", "file:code.py:12", "file:docs/a.md§Design mode for it",
                        "spec:/add-links", "image:shot-1.png", "design:7/flow.svg")
        self.assertEqual(ok, {k: None for k in ok})

    def test_missing_targets_say_why(self):
        why = self.check("task:99999", "file:docs/nope.md", "file:code.py:999", "file:docs/a.md§Nowhere",
                         "spec:/gone", "image:none.png", "design:7/none.html", "file:secret.txt")
        self.assertEqual(why["task:99999"], "No task #99999")
        self.assertEqual(why["file:docs/nope.md"], "Not on dev: docs/nope.md")
        self.assertEqual(why["file:code.py:999"], "code.py has only 21 lines")
        self.assertEqual(why["file:docs/a.md§Nowhere"], "No section “Nowhere” in docs/a.md")
        self.assertIn("gone", why["spec:/gone"])
        self.assertTrue(why["image:none.png"] and why["design:7/none.html"])
        self.assertEqual(why["file:secret.txt"], "Ignored by git: secret.txt")

    def get(self, path):
        url = f"http://127.0.0.1:{self.srv.server_port}/api/files?project=links&path={urllib.request.quote(path)}"
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())["error"]

    def test_files_serves_dev_and_refuses_escapes(self):
        self.assertEqual(self.get("docs/a.md"), (200, b"# A\n\n## Design mode\n\ntext\n"))
        for path, why in [("../repo/docs/a.md", "Outside the project"), ("/etc/passwd", "Not a repo path"),
                          ("link.txt", "Outside the project"), (".git/config", "Outside the project"),
                          ("docs/../../wt/dev/../../outside.txt", "Outside the project"),
                          ("secret.txt", "Ignored by git"), ("big.txt", "Over 1 MB")]:
            code, err = self.get(path)
            self.assertEqual(code, 404, path)
            self.assertTrue(err.startswith(why), (path, err))


class OldPages(unittest.TestCase):
    """The Design and Chat tabs are gone: #design and #chat are no pages, and the More menu has no button for them."""

    def test_no_design_or_chat_tab(self):
        import re
        page = PAGE.read_text()
        tabs = json.loads(re.search(r"^const TABS = (\[.*?\]);", page, re.M).group(1))
        self.assertIn("today", tabs)
        self.assertNotIn("design", tabs)
        self.assertNotIn("chat", tabs)
        for gone in ('id="tab-design"', 'id="tab-chat"', 'id="designview"', '"#design"', ': "#chat"'):
            self.assertNotIn(gone, page)


if __name__ == "__main__":
    unittest.main()
