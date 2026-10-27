"""Videos in repo markdown reports: md() renders ![alt](x.mp4) as a <video>, a repo file opened in the drawer
resolves its relative paths to /api/files, and /api/files serves a video by byte range up to MEDIA_MAX.
The renderer runs the page's own markdown code in node (skipped without node).
Stdlib only. Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
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
os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47963")
for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
    os.environ[k] = "t"
for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
    os.environ[k] = "t@example.com"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
const a = h.indexOf("const MD = {"), b = h.indexOf("// A picture in a reply that doesn't load");
const c = h.indexOf("function md(src"), e = h.indexOf("\n}\n", c) + 3;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let state = {data_dir: "/srv/ops"};
eval(h.slice(a, b) + h.slice(c, e));
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(([t, base]) => md(t, "fd-", undefined, base))));
"""
BASE = {"project": "demo", "dir": "a/b"}


def files(path):
    return f'/api/files?project=demo&amp;path={urllib.request.quote(path, safe="")}'


@unittest.skipUnless(shutil.which("node"), "needs node")
class Renderer(unittest.TestCase):
    def md(self, *texts):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(texts)], capture_output=True, text=True,
                             check=True).stdout
        return json.loads(out)

    def test_video_link_is_a_video(self):
        for url in ("https://x.test/clip.mp4", "https://x.test/CLIP.WebM?v=2"):
            html, = self.md([f"![before]({url})", None])
            self.assertIn(f'<video class="mdvideo" src="{url}" controls muted playsinline preload="metadata" '
                          f'title="before"></video>', html)
            self.assertNotIn("<img", html)

    def test_picture_stays_a_picture(self):
        html, = self.md(["![shot](https://x.test/a.png)", None])
        self.assertIn('<img class="mdimg" src="https://x.test/a.png"', html)
        self.assertNotIn("<video", html)

    def test_relative_paths_resolve_against_the_file(self):
        vid, up, dot, img, link = self.md(["![before](x.mp4)", BASE], ["![v](../c/y.webm)", BASE],
                                          ["![v](./x.mp4)", BASE], ["![p](shots/p.png)", BASE],
                                          ["[notes](../notes.md#top)", BASE])
        self.assertIn(f'<video class="mdvideo" src="{files("a/b/x.mp4")}"', vid)
        self.assertIn(f'src="{files("a/c/y.webm")}"', up)
        self.assertIn(f'src="{files("a/b/x.mp4")}"', dot)
        self.assertIn(f'<img class="mdimg" src="{files("a/b/shots/p.png")}"', img)
        self.assertIn(f'<a href="{files("a/notes.md")}#top"', link)

    def test_root_file_and_paths_left_as_written(self):
        root, = self.md(["![v](x.mp4)", {"project": "demo", "dir": ""}])
        self.assertIn(f'src="{files("x.mp4")}"', root)
        out = self.md(["![v](../../../x.mp4)", BASE], ["![v](/abs/x.mp4)", BASE], ["[a](#sec)", BASE],
                      ["[m](mailto:a@b.c)", BASE], ["![v](https://x.test/x.mp4)", BASE], ["![v](x.mp4)", None])
        for html, want in zip(out, ['src="../../../x.mp4"', 'src="/abs/x.mp4"', 'href="#fd-sec"',
                                    '<p>m</p>', 'src="https://x.test/x.mp4"', 'src="x.mp4"']):
            self.assertIn(want, html)
            self.assertNotIn("/api/files", html)


class Files(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        repo = Path(tempfile.mkdtemp(dir=DATA)) / "repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "r.md").write_text("![before](before.mp4)\n")
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        opsd.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        cls.toml = opsd.PROJECTS_DIR / "media.toml"
        cls.toml.write_text(f'repo = "{repo}"\nworktrees = "{repo.parent / "wt"}"\n')
        opsd.load_projects()
        opsd.bootstrap(opsd.PROJECTS["media"])
        dev = opsd.PROJECTS["media"].dev_wt
        cls.mp4 = b"\x00\x00\x00\x18ftypisom" + os.urandom(5 << 20)  # 5 MB
        (dev / "docs" / "before.mp4").write_bytes(cls.mp4)
        (dev / "docs" / "c.webm").write_bytes(b"\x1aE\xdf\xa3webm")
        (dev / "docs" / "big.md").write_text("x" * (5 << 20))
        (dev / "docs" / "huge.mp4").write_bytes(b"\0" * (opsd.MEDIA_MAX + 1))
        cls.srv = opsd.ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.toml.unlink()
        opsd.load_projects()

    def get(self, path, rng=None):
        url = f"http://127.0.0.1:{self.srv.server_port}/api/files?project=media&path={urllib.request.quote(path)}"
        req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def test_whole_video(self):
        code, hdr, body = self.get("docs/before.mp4")
        self.assertEqual((code, hdr["Content-Type"], hdr["Accept-Ranges"]), (200, "video/mp4", "bytes"))
        self.assertEqual(body, self.mp4)
        code, hdr, body = self.get("docs/c.webm")
        self.assertEqual((code, hdr["Content-Type"]), (200, "video/webm"))

    def test_ranges(self):
        size = len(self.mp4)
        code, hdr, body = self.get("docs/before.mp4", "bytes=100-199")
        self.assertEqual((code, hdr["Content-Range"], body), (206, f"bytes 100-199/{size}", self.mp4[100:200]))
        code, hdr, body = self.get("docs/before.mp4", "bytes=-10")
        self.assertEqual((code, body), (206, self.mp4[-10:]))
        code, hdr, _ = self.get("docs/before.mp4", f"bytes={size}-")
        self.assertEqual((code, hdr["Content-Range"]), (416, f"bytes */{size}"))

    def test_size_limits(self):
        code, _, body = self.get("docs/big.md")
        self.assertEqual(code, 404)
        self.assertTrue(json.loads(body)["error"].startswith("Over 1 MB"))
        code, _, body = self.get("docs/huge.mp4")
        self.assertEqual(code, 404)
        self.assertTrue(json.loads(body)["error"].startswith("Over 50 MB"))
        code, hdr, _ = self.get("docs/r.md")  # text is still sent whole
        self.assertEqual((code, hdr["Content-Type"]), (200, opsd.FILE_TEXT))
        self.assertIsNone(hdr["Accept-Ranges"])


if __name__ == "__main__":
    unittest.main()
