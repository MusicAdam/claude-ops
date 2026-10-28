#!/usr/bin/env python3
"""Make the README's screenshots and clips (docs/media/) from the seeded demo in scripts/demo_seed.py.

Run it through scripts/demo.sh, which finds a Python with Playwright and checks for ffmpeg. Steps: record the sample
app's proof clip, boot and seed the demo, take the screenshots on a frozen clock, record the clips, encode them
(H.264 mp4, and a looping gif of each), stop the daemon by its pid and remove the temp folder. --keep leaves the demo
running afterwards, so you can browse it.

The browser sees a fixed demo clock: Date.now() is DEMO_NOW, and every /api response has its timestamps moved by
the same offset and its temp paths shown as ~/… (see Shift). Nothing in opsd.py or dashboard.html changes for it.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_seed  # noqa: E402
from demo_seed import CODE, DEMO_NOW  # noqa: E402

MEDIA = CODE / "docs" / "media"
SHOT = {"width": 1440, "height": 900}
CLIP = {"width": 1280, "height": 800}  # a little narrower than the shots, so the 960 px gif stays readable
PHONE = {"width": 390, "height": 844}
TZ = "UTC"  # DEMO_NOW and the schedule are UTC, so the clock reads 10:30
SHOT_KB = 400
GIF_MB = 5


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


def close(ctx):
    """Close a browser context without tripping over a /api request still in its route handler."""
    ctx.unroute_all(behavior="ignoreErrors")
    ctx.close()


class Shift:
    """Rewrites the daemon's JSON for the browser: real times onto the demo clock, temp paths to ~/…"""

    def __init__(self, demo):
        self.demo = demo
        self.offset = DEMO_NOW - demo.anchor
        self.fixed = True  # screenshots: "now" is DEMO_NOW exactly; clips: it runs on from there
        self.paths = []
        for real in {demo.home, demo.home.resolve()}:
            for p in {str(real), str(real).removeprefix("/private")}:
                self.paths.append((p, "~"))
        self.paths += [(str(CODE), "~/dev/claude-ops")]
        self.paths.sort(key=lambda x: -len(x[0]))

    def now(self):
        return DEMO_NOW if self.fixed else time.time() + self.offset

    def value(self, v):
        lo, hi = self.demo.anchor - 90 * 86400, self.demo.anchor + 90 * 86400
        if isinstance(v, float) or (isinstance(v, int) and not isinstance(v, bool)):
            return v + self.offset if lo <= v <= hi else v
        if isinstance(v, str):
            for a, b in self.paths:
                v = v.replace(a, b)
            return v
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            out = {k: self.value(x) for k, x in v.items()}
            if out.get("id") == self.demo.ids.get("random") and out.get("state") == "running" and out.get("started"):
                # the seeded running task: its phase began as it started, and its current step a minute ago
                # (both are real times, which differ from run to run, and the screenshots should not)
                out["phase_since"] = out["started"]
                if isinstance(out.get("step"), dict):
                    out["step"] = {**out["step"], "ts": DEMO_NOW - 60}
            return out
        return v

    def route(self, route):
        r = route.fetch()
        if "json" not in (r.headers.get("content-type") or ""):
            return route.fulfill(response=r)
        try:
            data = self.value(r.json())
        except ValueError:
            return route.fulfill(response=r)
        if isinstance(data, dict) and "now" in data:
            data["now"] = self.now()
        route.fulfill(response=r, body=json.dumps(data), headers={**r.headers, "content-type": "application/json"})


class Capture:
    def __init__(self, browser, demo, out):
        self.browser, self.demo, self.out = browser, demo, out
        self.shift = Shift(demo)
        self.tmp = Path(tempfile.mkdtemp(prefix="ops-demo-media-"))

    def context(self, viewport, scale=2, video=False, mobile=False):
        opts = dict(viewport=viewport, device_scale_factor=scale, timezone_id=TZ, locale="en-US",
                    color_scheme="dark", reduced_motion="reduce")
        if mobile:
            opts.update(is_mobile=True, has_touch=True)
        if video:
            opts.update(record_video_dir=str(self.tmp), record_video_size=viewport)
        ctx = self.browser.new_context(**opts)
        # only the JSON: a video passed through route.fetch() loses its range requests, so it can't seek
        ctx.route(lambda url: "/api/" in url and not url.split("?")[0].endswith((".mp4", ".webm", ".png", ".jpg")),
                  self.shift.route)
        return ctx

    def page(self, ctx, hash_="today"):
        pg = ctx.new_page()
        # Date.now() stays put for the page's life (a clip is seconds long); the API's "now" runs on (Shift.now).
        # Not clock.install: its fake timers slow the dashboard's polling, and a clip then skips states.
        pg.clock.set_fixed_time(self.shift.now())
        pg.goto(f"{self.demo.url}/#{hash_}")
        pg.wait_for_selector("#nav")
        pg.wait_for_timeout(1500)
        return pg

    def shot(self, pg, name, el=None):
        raw = self.tmp / f"{name}.raw.png"
        (el or pg).screenshot(path=str(raw), animations="disabled")
        out = self.out / f"{name}.png"
        # 256 colours suit a flat UI and keep each file well under SHOT_KB
        ffmpeg("-i", raw, "-vf", "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=none",
               "-pix_fmt", "pal8", out)
        kb = out.stat().st_size // 1024
        print(f"  {out.name}  {kb} KB", flush=True)
        if kb > SHOT_KB:
            print(f"  warning: {out.name} is over {SHOT_KB} KB", flush=True)

    # -- screenshots

    def screenshots(self):
        self.shift.fixed = True
        ctx = self.context(SHOT)
        pg = self.page(ctx, "today")
        self.shot(pg, "today")

        pg.locator(".tditem", has_text="Shopping list for several recipes").first.click()
        self.play_proof(pg, at=6.5)
        self.shot(pg, "try-proof")

        pg.locator(".tditem", has_text="plan a week of dinners").first.click()
        pg.wait_for_timeout(800)
        fold = pg.locator(".stepcard [data-fold]").first
        if fold.count():
            fold.click()
        pg.wait_for_timeout(500)
        self.shot(pg, "design-chat")

        pg = self.page(ctx, "tasks")
        pg.locator("#tasksview .task", has_text="Add a random command").first.click()
        pg.wait_for_timeout(1500)
        self.shot(pg, "tasks")

        pg.locator("#tasksview .task", has_text="Fix: show crashes").first.click()
        pg.wait_for_timeout(1200)
        for name, want in (("Ask the worker", False), ("Files changed", True), ("Queue log", True)):
            d = pg.locator("#detail details", has=pg.locator("summary", has_text=name)).first
            if d.count() and d.evaluate("d => d.open") != want:
                d.locator("summary").first.click()
                pg.wait_for_timeout(400)
        self.shot(pg, "task-details")

        pg = self.page(ctx, "projects")
        if not pg.locator("#mains .prow.on").count():  # a click toggles it: Today may have opened it already
            pg.locator("#mains .phead").first.click()
        pg.wait_for_timeout(1200)
        self.shot(pg, "projects")
        close(ctx)

        ctx = self.context(PHONE, scale=2, mobile=True)
        pg = self.page(ctx, "today")
        self.shot(pg, "phone-today")
        close(ctx)

    def play_proof(self, pg, at):
        """Wait for the Try card's proof video, then show the frame at `at` seconds."""
        v = pg.locator("video").first
        try:
            v.wait_for(timeout=8000)
            pg.wait_for_function("v => v.readyState >= 2", arg=v.element_handle(), timeout=10000)
            v.evaluate(f"v => {{ v.pause(); v.currentTime = {at}; }}")
            pg.wait_for_function(f"v => !v.seeking && Math.abs(v.currentTime - {at}) < 0.3", arg=v.element_handle(),
                                 timeout=10000)
        except Exception as e:  # a missing video still leaves a useful screenshot; say so
            print(f"  warning: no proof video on the Try step ({e.__class__.__name__})", flush=True)
        pg.wait_for_timeout(600)

    # -- clips

    def clip(self, name, act, hash_="today"):
        """Record act(page) in its own context, trim the page load, and write name.mp4 and name.gif."""
        self.shift.fixed = False
        ctx = self.context(CLIP, scale=1, video=True)
        t0 = time.time()
        pg = self.page(ctx, hash_)
        start = time.time() - t0
        act(pg)
        pg.wait_for_timeout(1200)
        video = Path(pg.video.path())
        close(ctx)
        mp4, gif = self.out / f"{name}.mp4", self.out / f"{name}.gif"
        ffmpeg("-ss", f"{start:.2f}", "-i", video, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
               "-preset", "slow", "-movflags", "+faststart", mp4)
        ffmpeg("-i", mp4, "-vf", "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
               "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle", "-loop", "0", gif)
        secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     mp4], capture_output=True, text=True).stdout.strip() or 0)
        print(f"  {mp4.name}  {secs:.1f} s  {mp4.stat().st_size // 1024} KB;  "
              f"{gif.name}  {gif.stat().st_size // 1024} KB", flush=True)
        if gif.stat().st_size > GIF_MB * 2**20:
            print(f"  warning: {gif.name} is over {GIF_MB} MB", flush=True)

    def queue_a_task(self, pg):
        """Clip 1: type a task on the Tasks page, queue it, and watch it go from queued to done."""
        box = pg.locator("#tasksview textarea").first
        box.click()
        box.press_sequentially("Export a recipe as Markdown\n\n`recipe-box export 2` prints the recipe as Markdown, "
                               "to paste into notes.", delay=18)
        pg.wait_for_timeout(400)
        pg.get_by_role("button", name="Queue task").click()
        pg.wait_for_timeout(800)
        card = pg.locator("#tasksview .task", has_text="Export a recipe as Markdown").first
        card.click()
        tid = self.task_id("Export a recipe as Markdown")
        self.wait(pg, lambda: self.demo.task_state(tid) == "done", 90, f"#{tid} to merge")
        pg.wait_for_timeout(2500)

    def design_chat(self, pg):
        """Clip 3: ask a design chat a question; its reply queues a task."""
        pg.locator(".stepcard [data-fold]").first.click()  # the step card folds to one line, so the reply shows
        pg.wait_for_timeout(500)
        pg.locator("#tdnew").click()
        pg.wait_for_timeout(600)
        box = pg.locator("#say")
        box.click()
        box.press_sequentially(demo_seed.CLIP_CHATS[0]["say"], delay=22)
        pg.wait_for_timeout(300)
        pg.locator("#send").click()
        self.wait(pg, lambda: self.chat_by_text("what's in the fridge"), 60, "the clip's chat to reply")
        pg.wait_for_timeout(5000)

    def approve(self, pg):
        """Clip 2: open the Try step, watch the proof video, and click Approve & merge."""
        pg.locator(".tditem", has_text="Shopping list for several recipes").first.click()
        v = pg.locator("video").first
        try:
            v.wait_for(timeout=8000)
            v.evaluate("v => { v.muted = true; v.currentTime = 0; v.play(); }")
        except Exception:
            pass
        pg.wait_for_timeout(6000)
        pg.get_by_role("button", name="Approve & merge").first.click()
        tid = self.demo.ids["shop"]
        self.wait(pg, lambda: self.demo.task_state(tid) == "done", 60, f"#{tid} to merge")
        pg.wait_for_timeout(2500)

    def wait(self, pg, ok, secs, what):
        """Demo.wait, but sleeping in the page: the sync API runs the /api route handler only inside a Playwright
        call, so a plain sleep would stall every request the dashboard makes meanwhile."""
        end = time.time() + secs
        while not ok():
            if time.time() > end:
                raise RuntimeError(f"timed out after {secs} s waiting for {what}; see {self.demo.root}/opsd.log")
            pg.wait_for_timeout(250)
            pg.clock.set_fixed_time(self.shift.now())  # so "running for" counts up with the clip

    def task_id(self, title):
        with self.demo.db() as c:
            return c.execute("SELECT max(id) FROM tasks WHERE title=?", (title,)).fetchone()[0]

    def chat_by_text(self, text):
        with self.demo.db() as c:
            return c.execute("SELECT 1 FROM design_msgs WHERE role='assistant' AND design_id IN (SELECT design_id FROM "
                             "design_msgs WHERE role='user' AND text LIKE ?)", (f"%{text}%",)).fetchone()

    def clips(self):
        self.clip("queue-a-task", self.queue_a_task, "tasks")
        self.clip("design-chat-queues-tasks", self.design_chat, "today")
        self.clip("approve-and-merge", self.approve, "today")  # last: it merges #7, which the others wait on


# ---------------------------------------------------------------- the proof clip of the sample app

PROOF_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>recipe-box</title><style>
body { font: 17px/1.5 -apple-system, system-ui, sans-serif; margin: 0; background: #fbf8f3; color: #2b2622; }
header { background: #b8532c; color: #fff; padding: 14px 28px; font-weight: 700; font-size: 20px; }
main { display: flex; gap: 32px; padding: 24px 28px; }
section { flex: 1; background: #fff; border: 1px solid #e6ddd2; border-radius: 10px; padding: 16px 20px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: #8a7c70; margin: 0 0 10px; }
label { display: flex; gap: 10px; align-items: center; padding: 6px 0; }
input { width: 18px; height: 18px; accent-color: #b8532c; }
button { margin-top: 14px; font: inherit; font-weight: 600; background: #b8532c; color: #fff; border: 0;
  border-radius: 8px; padding: 9px 18px; }
li { padding: 3px 0; } .hit { background: #ffe7a8; border-radius: 4px; padding: 0 4px; font-weight: 600; }
.muted { color: #8a7c70; }
</style></head><body><header>recipe-box</header><main>
<section><h2>Recipes</h2>RECIPES<button id="shop">Shopping list</button></section>
<section><h2>Shopping list</h2><ul id="list"><li class="muted">Tick recipes, then press Shopping list.</li></ul></section>
</main><script>
const BOOK = BOOKJSON, QTY = /^(\\d+(?:\\.\\d+)?)\\s*(g|ml|tbsp|tins?|cloves?)?\\s+(.*)$/;
document.getElementById("shop").onclick = () => {
  const need = new Map();
  for (const box of document.querySelectorAll("input:checked"))
    for (const i of BOOK.find(r => r.id == box.value).ingredients) {
      const m = i.match(QTY), [n, unit, what] = m ? [+m[1], m[2] || "", m[3]] : [1, "", i], k = what + "|" + unit;
      need.set(k, (need.get(k) || 0) + n);
    }
  document.getElementById("list").innerHTML = [...need].map(([k, n]) => { const [what, unit] = k.split("|");
    return `<li${n > 1 && !unit ? ' class="hit"' : ""}>${n} ${unit ? unit + " " : ""}${what}</li>`; }).join("");
};
</script></body></html>"""


def proof_clip(browser, tmp):
    """The proof video the Try step plays: the sample app's shopping list, recorded like a worker would."""
    rows = "".join(f'<label><input type="checkbox" value="{r["id"]}">{r["title"]}</label>' for r in demo_seed.RECIPES)
    html = PROOF_PAGE.replace("RECIPES", rows).replace("BOOKJSON", json.dumps(demo_seed.RECIPES))
    size = {"width": 960, "height": 540}
    ctx = browser.new_context(viewport=size, record_video_dir=str(tmp), record_video_size=size)
    pg = ctx.new_page()
    t0 = time.time()
    pg.set_content(html)
    start = time.time() - t0
    pg.wait_for_timeout(1200)
    for title in ("Lemon pasta", "Green salad"):
        pg.get_by_label(title).check()
        pg.wait_for_timeout(1100)
    pg.wait_for_timeout(500)
    pg.locator("#shop").click()
    pg.wait_for_timeout(3500)
    video = Path(pg.video.path())
    close(ctx)
    out = tmp / "proof.mp4"
    ffmpeg("-ss", f"{start:.2f}", "-i", video, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28",
           "-movflags", "+faststart", out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", action="store_true", help="leave the demo running afterwards and print its URL")
    ap.add_argument("--daemon-python", default=sys.executable, help="the Python (3.11+) that runs opsd.py")
    ap.add_argument("--port", type=int, help="the demo daemon's port (default: a free one)")
    ap.add_argument("--out", type=Path, default=MEDIA, help="where the media go (default: docs/media)")
    a = ap.parse_args()
    demo_seed.stop_on_term()
    a.out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ops-demo-proof-"))
    demo = cap = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            print("recording the sample app's proof clip", flush=True)
            proof = proof_clip(browser, tmp)
            demo = demo_seed.Demo(port=a.port, python=a.daemon_python, proof_mp4=proof)
            print(f"seeding a demo claude-ops in {demo.root}", flush=True)
            demo.start().seed().retime()
            cap = Capture(browser, demo, a.out)
            print("screenshots:", flush=True)
            cap.screenshots()
            print("clips:", flush=True)
            cap.clips()
            browser.close()
        total = sum(f.stat().st_size for f in a.out.iterdir() if f.is_file())
        print(f"done: {a.out} holds {total / 2**20:.1f} MB", flush=True)
        if a.keep:
            print(f"the demo is still running: {demo.url}  (Ctrl-C stops it and removes {demo.root})", flush=True)
            while demo.proc.poll() is None:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if demo:
            demo.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        if cap:
            shutil.rmtree(cap.tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
