"""Links in chats (README: Links in chats, LINK-1..8). The page side runs dashboard.html's own markdown code in node
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
import time
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
                                  "see /Users/me/.claude-ops/images/b.png here", user=False)
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

    def test_chat(self):
        html, = self.md("Review chat:42, then (chat:7) and `chat:43`.")
        self.assertIn('data-ref="chat:42" data-href="#today">chat:42</span>', html)
        self.assertIn('data-ref="chat:7"', html)
        self.assertIn('data-ref="chat:43" data-href="#today"><code>chat:43</code></span>', html)  # a code span of only it
        code, url, word = self.md("```\nchat:45\n```\n\n`see chat:46`", "https://x.org/chat:47 and x.org/chat:48",
                                  "mychat:49 chat:50x")
        self.assertNotIn("data-ref", code)
        self.assertNotIn("data-ref", url)
        self.assertNotIn("data-ref", word)
        mine, = self.md("look at chat:51", user=True)
        self.assertIn('data-ref="chat:51"', mine)

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


# The @ picker's pure parts, from the page: which @ word the caret ends, and the text once a chat is picked
AT_RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf("function atWord"), h.indexOf("// The boxes it serves")));
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([v, c, id]) => { const w = atWord(v, c); return [w, w && id ? atInsert(v, c, w, id) : null]; })));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class AtPicker(unittest.TestCase):
    def run_at(self, *cases):
        out = subprocess.run(["node", "-e", AT_RUN, str(PAGE), json.dumps(cases)], capture_output=True, text=True,
                             check=True).stdout
        return json.loads(out)

    def test_word_and_insert(self):
        (w1, r1), (w2, r2), (w3, _), (w4, _), (w5, _), (w6, r6) = self.run_at(
            ["review @des", 11, 42], ["@", 1, 7], ["mail a@b", 8, None], ["@ x", 2, None], ["@abc def", 8, None],
            ["see (@lo and more", 8, 3])
        self.assertEqual(w1, {"start": 7, "q": "des"})
        self.assertEqual(r1, {"value": "review chat:42 ", "caret": 15})
        self.assertEqual((w2, r2), ({"start": 0, "q": ""}, {"value": "chat:7 ", "caret": 7}))
        self.assertIsNone(w3)  # not the start of a word
        self.assertIsNone(w4)  # a space after @ ends it: nothing to pick
        self.assertIsNone(w5)
        self.assertEqual(r6, {"value": "see (chat:3  and more", "caret": 12})


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

    def chat(self, project, title, msgs=(), **cols):
        now = time.time()
        did = opsd.q("INSERT INTO designs (project, title, created, updated, kind, ended) VALUES (?,?,?,?,?,?)",
                     (project, title, now - 3600, cols.get("updated", now), cols.get("kind", "design"),
                      cols.get("ended")))
        for k, (role, text) in enumerate(msgs):
            opsd.q("INSERT INTO design_msgs (design_id, role, text, ts) VALUES (?,?,?,?)", (did, role, text, now + k))
        self.addCleanup(opsd.q, "DELETE FROM designs WHERE id=?", (did,))
        self.addCleanup(opsd.q, "DELETE FROM design_msgs WHERE design_id=?", (did,))
        return did

    def test_chat_found_with_title_and_project(self):
        did = self.chat("links", "Save format")
        chats = {}
        why = opsd.link_check("other", [f"chat:{did}", "chat:99999"], None, chats)  # not the message's project
        self.assertEqual(why, {f"chat:{did}": None, "chat:99999": "No chat 99999"})
        self.assertEqual(chats, {f"chat:{did}": {"title": "Save format", "project": "links"}})

    def test_ops_note_names_referenced_chats(self):
        did = self.chat("links", "Save format", [("user", "hi"), ("assistant", "hello")], kind="project")
        here = self.chat("links", "Here")
        line = (f'chat:{did} is "Save format" (links, project chat, 2 messages, last active {opsd.day(time.time())}): '
                f"read it with `{opsd.OPSD_CMD} chat show {did}`")
        text = f"review chat:{did} and chat:99999, not `x` in\n```\nchat:{here}\n```\nor https://x.org/chat:{here}"
        self.assertEqual(opsd.chat_mentions(text), [line, "chat:99999: no such chat"])
        note = opsd.ops_note(self.p, here, "", text)
        self.assertIn(" | " + line + " | chat:99999: no such chat]", note)
        self.assertEqual(opsd.chat_said(here, text), [line, "chat:99999: no such chat"])
        self.assertNotIn("chat:", opsd.ops_note(self.p, here, "", "nothing to see"))
        self.assertEqual(opsd.chat_said(here, "nothing to see"), [])

    def test_topic_search(self):
        old = self.chat("links", "Save format old", updated=time.time() - 100)
        new = self.chat("links", "Save format new")
        done = self.chat("links", "Save format done", ended=time.time())
        gone = self.chat("nowhere", "Save format archived")  # a project that isn't loaded
        ids = [t["id"] for t in opsd.topic_search("save FORMAT", "links", exclude=new)]
        self.assertEqual(ids[:2], [old, done])  # open before ended, and not the one you type in
        self.assertNotIn(gone, ids)
        self.assertEqual([t["id"] for t in opsd.topic_search(str(new), "links")][:1], [new])
        self.assertEqual(opsd.topic_search("no such words", "links"), [])

    def test_chat_show(self):
        did = self.chat("links", "Save format", [("user", "first"), ("assistant", "line one\nline two " + "x" * 5000),
                                                 ("ops", "a note"), ("user", "last")])
        env = {**os.environ, "OPS_DATA": str(opsd.DATA)}  # the db this module's opsd uses
        run = lambda *a: subprocess.run([sys.executable, str(PAGE.parent / "opsd.py"), "chat", "show", *a],  # noqa: E731
                                        capture_output=True, text=True, env=env)
        out = run(str(did))
        self.assertEqual(out.returncode, 0, out.stderr)
        head, *msgs = out.stdout.split("\n\n--- ")
        self.assertTrue(head.startswith(f'chat:{did} · links · design chat · "Save format" · idle · open · created '),
                        head)
        self.assertEqual([m.split(" · ")[0] for m in msgs], ["The owner", "Claude", "The dashboard", "The owner"])
        self.assertIn("line one\nline two " + "x" * 5000 + "\n", out.stdout)  # in full
        self.assertEqual(msgs[0].split("\n", 1)[1], "first")
        last = run(str(did), "--last", "2").stdout.split("\n\n--- ")
        self.assertEqual([m.split("\n", 1)[1].strip() for m in last[1:]], ["a note", "last"])
        gone = run("99999")
        self.assertEqual((gone.returncode, gone.stdout, gone.stderr), (1, "", "no chat 99999\n"))

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
