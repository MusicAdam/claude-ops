#!/usr/bin/env python3
"""A throwaway claude-ops with a made-up sample project, for the README's screenshots and clips (scripts/demo.sh).

Everything lives in one temp folder: a fake HOME (so no path, git identity or Claude setting of yours shows), the
sample repo ~/dev/recipe-box inside it, and the daemon's data in ~/.claude-ops inside it (OPS_DATA). OPS_CLAUDE
points at this file's stub mode, which plays canned worker and chat turns in the stream-json format opsd parses, so
real tasks go through the real pipeline (worktree, gate, merge, hold, proof) with no model calls and no tokens.
After seeding, retime() moves every timestamp onto a fixed timeline (see DEMO_NOW), so each run shows the same thing.

Stdlib only. Used by scripts/demo_capture.py and tests/test_demo_seed.py; run by hand to browse the demo:
    python3 scripts/demo_seed.py            # seed, print the URL, and wait for Ctrl-C (then stop and clean up)
"""
import contextlib
import datetime
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve()
CODE = HERE.parent.parent  # the claude-ops checkout whose opsd.py the demo runs
UTC = datetime.timezone.utc
DEMO_NOW = datetime.datetime(2026, 3, 13, 10, 30, tzinfo=UTC).timestamp()  # a Friday morning: what "now" shows
NEXT_MONDAY_9 = datetime.datetime(2026, 3, 16, 9, 0, tzinfo=UTC).timestamp()  # the schedule's next slot from then
GIT_DATE = "2026-03-12T09:00:00+0000"  # every commit's date, so commit ids are the same on every run
WHO = {"GIT_AUTHOR_NAME": "Sam Example", "GIT_AUTHOR_EMAIL": "sam@example.com",
       "GIT_COMMITTER_NAME": "Sam Example", "GIT_COMMITTER_EMAIL": "sam@example.com",
       "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}
PROJECT = "recipe-box"


# ---------------------------------------------------------------- the sample repo

RECIPES = [
    {"id": 1, "title": "Lemon pasta", "serves": 2, "minutes": 20,
     "ingredients": ["200 g spaghetti", "1 lemon", "40 g parmesan", "2 tbsp butter"],
     "steps": ["Cook the pasta.", "Melt the butter with the lemon zest and juice.", "Toss with the cheese."]},
    {"id": 2, "title": "Tomato soup", "serves": 4, "minutes": 35,
     "ingredients": ["800 g tomatoes", "1 onion", "2 cloves garlic", "500 ml stock"],
     "steps": ["Soften the onion and garlic.", "Add the tomatoes and stock; simmer 20 minutes.", "Blend."]},
    {"id": 3, "title": "Chickpea curry", "serves": 4, "minutes": 30,
     "ingredients": ["2 tins chickpeas", "1 onion", "1 tin coconut milk", "2 tbsp curry paste"],
     "steps": ["Fry the onion and paste.", "Add the chickpeas and coconut milk.", "Simmer 15 minutes."]},
    {"id": 4, "title": "Lemon drizzle cake", "serves": 8, "minutes": 60,
     "ingredients": ["225 g butter", "225 g sugar", "4 eggs", "225 g flour", "2 lemons"],
     "steps": ["Beat the butter and sugar.", "Add the eggs and flour.", "Bake 45 minutes; drizzle with lemon."]},
    {"id": 5, "title": "Green salad", "serves": 2, "minutes": 10,
     "ingredients": ["1 lettuce", "1 cucumber", "1 lemon", "3 tbsp olive oil"],
     "steps": ["Chop.", "Dress with the lemon and oil."]},
    {"id": 6, "title": "Overnight oats", "serves": 1, "minutes": 5,
     "ingredients": ["50 g oats", "150 ml milk", "1 apple"],
     "steps": []},
]

REPO_FILES = {
    "README.md": """# recipe-box

A small recipe book for the terminal.

```bash
python3 -m recipe_box list
python3 -m recipe_box search lemon
python3 -m recipe_box show 3
python3 -m recipe_box serve        # the same book in a browser
```

Recipes live in `recipes.json`. Run the tests with `python3 -m unittest discover -s tests -q`.
""",
    "recipes.json": json.dumps(RECIPES, indent=2) + "\n",
    "recipe_box/__init__.py": '"""recipe-box: a small recipe book for the terminal."""\n',
    "recipe_box/__main__.py": "from .cli import main\n\nmain()\n",
    "recipe_box/store.py": '''"""Load and search the recipe book (recipes.json)."""
import json
from pathlib import Path

BOOK = Path(__file__).resolve().parent.parent / "recipes.json"


def load(path=BOOK):
    return json.loads(Path(path).read_text())


def find(recipes, rid):
    return next((r for r in recipes if r["id"] == rid), None)


def search(recipes, words):
    """Recipes whose title has every word."""
    words = [w.lower() for w in words]
    return [r for r in recipes if all(w in r["title"].lower() for w in words)]
''',
    "recipe_box/cli.py": '''"""The recipe-box command line."""
import argparse

from . import store


def line(r):
    return f"{r['id']:>3}  {r['title']}"


def show(r):
    out = [r["title"], ""] + [f"- {i}" for i in r["ingredients"]]
    out += [""] + [f"{n}. {s}" for n, s in enumerate(r["steps"], 1)]
    return "\\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="recipe-box")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("search").add_argument("words", nargs="+")
    sub.add_parser("show").add_argument("id", type=int)
    sub.add_parser("serve")
    a = ap.parse_args(argv)
    recipes = store.load()
    if a.cmd == "list":
        print("\\n".join(line(r) for r in recipes))
    elif a.cmd == "search":
        print("\\n".join(line(r) for r in store.search(recipes, a.words)) or "no recipes match")
    elif a.cmd == "show":
        r = store.find(recipes, a.id)
        print(show(r) if r else f"no recipe {a.id}")
    elif a.cmd == "serve":
        from . import web
        web.serve()
''',
    "recipe_box/web.py": '''"""python3 -m recipe_box serve: the book as one web page, on $PORT (default 8000)."""
import html
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import store


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        rows = "".join(f"<li>{html.escape(r['title'])}</li>" for r in store.load())
        body = f"<!doctype html><title>recipe-box</title><h1>recipe-box</h1><ul>{rows}</ul>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


def serve():
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Page).serve_forever()
''',
    "tests/__init__.py": "",
    "tests/test_store.py": '''import unittest

from recipe_box import store


class Store(unittest.TestCase):
    def test_search_title(self):
        titles = [r["title"] for r in store.search(store.load(), ["lemon"])]
        self.assertIn("Lemon pasta", titles)

    def test_find(self):
        self.assertEqual(store.find(store.load(), 3)["title"], "Chickpea curry")
''',
    ".gitignore": "__pycache__/\n.ops-result.json\n",
}

TOML_EXTRA = '''
[[gate]]
name = "tests"
run = "python3 -m unittest discover -s tests -q"

[app]
launch = "python3 -m recipe_box serve"
running = "false"   # the demo never launches it; a server-style app is recorded headless
stop = "true"
'''


# ---------------------------------------------------------------- what each worker and chat does (the stub's plan)

def rp(path, old, new):
    return {"replace": [path, old, new]}


def wr(path, text):
    return {"write": [path, text]}


def ap(path, text):
    return {"append": [path, text]}


INGREDIENT_TEST = '''

class Ingredients(unittest.TestCase):
    def test_search_ingredient(self):
        titles = [r["title"] for r in store.search(store.load(), ["lemon"])]
        self.assertIn("Green salad", titles)  # lemon is only in its ingredients

    def test_every_word(self):
        self.assertEqual([r["title"] for r in store.search(store.load(), ["lemon", "butter"])],
                         ["Lemon pasta", "Lemon drizzle cake"])
'''

SCALE = '''"""Scale a recipe to more or fewer people."""
import re

QTY = re.compile(r"^(\\d+(?:\\.\\d+)?)\\b")


def scale(recipe, serves):
    f = serves / recipe["serves"]
    out = []
    for i in recipe["ingredients"]:
        m = QTY.match(i)
        out.append(f"{float(m.group(1)) * f:g}{i[m.end():]}" if m else i)
    return {**recipe, "serves": serves, "ingredients": out}
'''

SCALE_TEST = '''import unittest

from recipe_box import scale, store


class Scale(unittest.TestCase):
    def test_double(self):
        r = scale.scale(store.find(store.load(), 1), 4)
        self.assertEqual(r["ingredients"][0], "400 g spaghetti")
        self.assertEqual(r["ingredients"][1], "2 lemon")

    def test_words_stay(self):
        r = scale.scale(store.find(store.load(), 2), 2)
        self.assertEqual(r["ingredients"][3], "250 ml stock")
'''

SHOPPING = '''"""One shopping list for several recipes: the same ingredient is added up, not listed twice."""
import re
from collections import OrderedDict

QTY = re.compile(r"^(\\d+(?:\\.\\d+)?)\\s*(g|ml|tbsp|tins?|cloves?)?\\s+(.*)$")


def shopping_list(recipes):
    need = OrderedDict()
    for r in recipes:
        for i in r["ingredients"]:
            m = QTY.match(i)
            n, unit, what = (float(m.group(1)), m.group(2) or "", m.group(3)) if m else (1.0, "", i)
            key = (what.lower(), unit)
            need[key] = need.get(key, 0) + n
    return [f"{n:g} {unit + ' ' if unit else ''}{what}" for (what, unit), n in need.items()]
'''

SHOPPING_TEST = '''import unittest

from recipe_box import shopping, store


class Shopping(unittest.TestCase):
    def test_adds_up(self):
        book = store.load()
        items = shopping.shopping_list([store.find(book, 1), store.find(book, 5)])
        self.assertIn("2 lemon", items)  # one from each recipe
        self.assertEqual(len(items), 7)
'''

EXPORT = '''"""recipe-box export: a recipe as Markdown, to paste into notes or a chat."""


def markdown(r):
    out = [f"# {r['title']}", "", f"Serves {r['serves']} · {r['minutes']} min", "", "## Ingredients", ""]
    out += [f"- {i}" for i in r["ingredients"]] + ["", "## Method", ""]
    out += [f"{n}. {s}" for n, s in enumerate(r["steps"], 1)]
    return "\\n".join(out) + "\\n"
'''

EXPORT_TEST = '''import unittest

from recipe_box import export, store


class Export(unittest.TestCase):
    def test_markdown(self):
        md = export.markdown(store.find(store.load(), 2))
        self.assertTrue(md.startswith("# Tomato soup\\n"))
        self.assertIn("- 1 onion", md)
'''

SUMMARY_REVIEW = {
    "fixed": "`show` crashed on a recipe with no steps, such as Overnight oats. It now prints the ingredients and "
             "says the recipe has no method yet.",
    "before": "`python3 -m recipe_box show 6` stopped with an IndexError.",
    "after": "It prints the title, the ingredients and \"No method yet.\"",
    "check": ["`show 6` prints Overnight oats with \"No method yet.\"", "`show 1` is unchanged"],
}

# Tasks in the order they are queued; ids are 1, 2, 3 … in this order. Each one's worker plays `steps` (the tool calls
# the dashboard shows), makes `edits`, commits and writes `result`. `at`: (queued, finished) in minutes before the
# demo clock, for retime(); None for one that hasn't finished.
TASKS = [
    {"title": "Search by ingredient as well as title",
     "body": "`search lemon` should also find recipes that use lemon, not only ones with lemon in the title.",
     "at": (27 * 60, 26 * 60 + 38),
     "steps": [("Read", "recipe_box/store.py"), ("Grep", "search"), ("Edit", "recipe_box/store.py"),
               ("Edit", "tests/test_store.py"), ("Bash", "python3 -m unittest discover -s tests -q")],
     "edits": [rp("recipe_box/store.py", '''    """Recipes whose title has every word."""
    words = [w.lower() for w in words]
    return [r for r in recipes if all(w in r["title"].lower() for w in words)]''',
                  '''    """Recipes whose title or ingredients have every word."""
    words = [w.lower() for w in words]
    text = lambda r: " ".join([r["title"], *r["ingredients"]]).lower()
    return [r for r in recipes if all(w in text(r) for w in words)]'''),
               ap("tests/test_store.py", INGREDIENT_TEST)],
     "commit": "search: match ingredients as well as titles",
     "result": {"status": "done", "summary": "`search` now matches ingredients as well as titles. `search lemon` "
                "finds Green salad, which has lemon only in its ingredients. Every word must match, as before. "
                "Two new tests cover it."}},
    {"title": "Show the cooking time in list and show",
     "body": "Each recipe has `minutes`. Show it in `list` (a column) and at the top of `show`.",
     "at": (25 * 60 + 50, 25 * 60 + 31),
     "steps": [("Read", "recipe_box/cli.py"), ("Read", "recipes.json"), ("Edit", "recipe_box/cli.py"),
               ("Write", "tests/test_cli.py"), ("Bash", "python3 -m unittest discover -s tests -q")],
     "edits": [rp("recipe_box/cli.py", '''    return f"{r['id']:>3}  {r['title']}"''',
                  '''    return f"{r['id']:>3}  {r['title']:<24} {r['minutes']:>3} min"'''),
               rp("recipe_box/cli.py", '''    out = [r["title"], ""] + ''', '''    out = [r["title"], f"{r['minutes']} min", ""] + '''),
               wr("tests/test_cli.py", '''import unittest

from recipe_box import cli, store


class Cli(unittest.TestCase):
    def test_list_line(self):
        self.assertTrue(cli.line(store.find(store.load(), 2)).endswith(" 35 min"))

    def test_show_time(self):
        self.assertEqual(cli.show(store.find(store.load(), 1)).splitlines()[1], "20 min")
''')],
     "commit": "list and show: print the cooking time",
     "result": {"status": "done", "summary": "`list` has a minutes column, and `show` prints the time under the "
                "title. Tests in `tests/test_cli.py`."}},
    {"title": "Scale a recipe with show 3 --serves 6",
     "body": "Add `--serves N` to `show`: multiply each quantity so the recipe feeds N people.",
     "at": (6 * 60 + 5, 5 * 60 + 41),
     "steps": [("Read", "recipe_box/cli.py"), ("Write", "recipe_box/scale.py"), ("Edit", "recipe_box/cli.py"),
               ("Write", "tests/test_scale.py"), ("Bash", "python3 -m unittest discover -s tests -q"),
               ("Bash", "python3 -m recipe_box show 1 --serves 4")],
     "edits": [wr("recipe_box/scale.py", SCALE), wr("tests/test_scale.py", SCALE_TEST),
               rp("recipe_box/cli.py", '''    sub.add_parser("show").add_argument("id", type=int)''',
                  '''    sh = sub.add_parser("show")
    sh.add_argument("id", type=int)
    sh.add_argument("--serves", type=int, help="scale the quantities to feed this many")'''),
               rp("recipe_box/cli.py", '''        print(show(r) if r else f"no recipe {a.id}")''',
                  '''        if r and a.serves:
            from .scale import scale
            r = scale(r, a.serves)
        print(show(r) if r else f"no recipe {a.id}")''')],
     "commit": "show --serves: scale the quantities",
     "result": {"status": "done", "summary": "`show 3 --serves 6` scales every quantity that starts with a number. "
                "Words like \"a pinch\" stay as they are. The scaling is in `recipe_box/scale.py`, with tests."}},
    {"title": "Read my own recipes from ~/.recipe-box",
     "body": "Besides the bundled recipes.json, load `~/.recipe-box/recipes.json` if it exists, so I can keep my "
             "own recipes out of the repo.",
     "at": (4 * 60 + 30, 4 * 60 + 12),
     "steps": [("Read", "recipe_box/store.py"), ("Edit", "recipe_box/store.py"),
               ("Bash", "python3 -m unittest discover -s tests -q")],
     "edits": [ap("recipe_box/store.py", '''

MINE = Path.home() / ".recipe-box" / "recipes.json"


def load_all():
    """The bundled book, then your own recipes from ~/.recipe-box/recipes.json if that file exists."""
    return load() + (load(MINE) if MINE.exists() else [])
''')],
     "commit": "store: add load_all for ~/.recipe-box/recipes.json",
     "result": {"status": "done", "summary": "`store.load_all()` reads the bundled book, then "
                "`~/.recipe-box/recipes.json` if it exists. The commands still use the bundled book until you "
                "copy your recipes there.",
                "todos": [{"verb": "Do", "title": "Copy your own recipes into ~/.recipe-box/recipes.json",
                           "why": "The worker can't see your files. Once they are there, `list` and `search` can "
                                  "include them."}]}},
    {"title": "Fix: show crashes on a recipe with no steps",
     "body": "`python3 -m recipe_box show 6` stops with an IndexError. Overnight oats has no steps yet.",
     "at": (2 * 60 + 50, 2 * 60 + 36),
     "steps": [("Bash", "python3 -m recipe_box show 6"), ("Read", "recipe_box/cli.py"),
               ("Edit", "recipe_box/cli.py"), ("Edit", "tests/test_cli.py"),
               ("Bash", "python3 -m unittest discover -s tests -q")],
     "edits": [rp("recipe_box/cli.py", '''    out += [""] + [f"{n}. {s}" for n, s in enumerate(r["steps"], 1)]''',
                  '''    out += [""] + ([f"{n}. {s}" for n, s in enumerate(r["steps"], 1)] or ["No method yet."])'''),
               ap("tests/test_cli.py", '''
    def test_no_steps(self):
        self.assertTrue(cli.show(store.find(store.load(), 6)).endswith("No method yet."))
''')],
     "commit": "show: say \"No method yet.\" for a recipe with no steps",
     "result": {"status": "done", "summary": "`show` no longer crashes on a recipe with no steps. It prints the "
                "ingredients, then \"No method yet.\"", "review": SUMMARY_REVIEW}},
    {"title": "Import a recipe from a web page",
     "body": "`recipe-box import <url>` adds the recipe on that page to recipes.json.",
     "at": (100, 94),
     "steps": [("Read", "recipe_box/store.py"), ("WebFetch", "schema.org/Recipe"),
               ("Bash", "python3 -c 'import urllib.request'")],
     "edits": [],
     "result": {"status": "needs_input",
                "summary": "Read how recipe sites mark up their recipes. Nothing changed yet.",
                "question": "Most recipe sites embed schema.org Recipe data, which is easy to read. Some don't, "
                            "and a few block plain downloads. Should `import` read only pages with that data, "
                            "and say so when a page has none? Or should it also guess from the page text, "
                            "which is less reliable?"}},
    {"title": "Shopping list for several recipes",
     "body": "`recipe-box shop 1 5` prints one shopping list for those recipes, adding up an ingredient that "
             "appears in more than one (2 lemons, not 1 lemon twice).",
     "hold": 1, "at": (75, 41),
     "steps": [("Read", "recipe_box/cli.py"), ("Write", "recipe_box/shopping.py"), ("Edit", "recipe_box/cli.py"),
               ("Write", "tests/test_shopping.py"), ("Bash", "python3 -m unittest discover -s tests -q"),
               ("Bash", "python3 -m recipe_box shop 1 5")],
     "edits": [wr("recipe_box/shopping.py", SHOPPING), wr("tests/test_shopping.py", SHOPPING_TEST),
               rp("recipe_box/cli.py", '''    sub.add_parser("serve")''',
                  '''    sub.add_parser("shop").add_argument("ids", type=int, nargs="+")
    sub.add_parser("serve")'''),
               rp("recipe_box/cli.py", '''    elif a.cmd == "serve":''',
                  '''    elif a.cmd == "shop":
        from .shopping import shopping_list
        print("\\n".join(shopping_list([r for r in recipes if r["id"] in a.ids])))
    elif a.cmd == "serve":''')],
     "commit": "shop: one shopping list for several recipes",
     "result": {"status": "done", "summary": "`shop 1 5` prints one list for Lemon pasta and Green salad. The "
                "lemon from each is added up to 2 lemon. The web page has a Shopping list button too.",
                "review": {"fixed": "There was no way to shop for more than one recipe. `shop` adds up the "
                                    "ingredients of the recipes you pick.",
                           "after": "`shop 1 5` lists 7 items, with 2 lemon instead of lemon twice.",
                           "check": ["Pick Lemon pasta and Green salad: the list shows 2 lemon",
                                     "Each other ingredient appears once"]}},
     "proof": {"proof": "Picks Lemon pasta and Green salad, then opens the shopping list: 2 lemon, listed once.",
               "watch": ["0:02 tick Lemon pasta and Green salad", "0:05 Shopping list shows 2 lemon, listed once"]}},
    {"title": "Add a random command for tonight's dinner",
     "body": "`recipe-box random` picks one recipe. `--quick` picks from the ones under 30 minutes.",
     "at": (12, None), "block": True,
     "steps": [("Read", "recipe_box/cli.py"), ("Read", "recipes.json"), ("Grep", "minutes"),
               ("Edit", "recipe_box/cli.py"), ("Write", "tests/test_random.py"),
               ("Bash", "python3 -m unittest discover -s tests -q")]},
    {"title": "Tag recipes and filter by tag",
     "body": "Recipes get optional tags (vegetarian, quick, baking). `list --tag quick` shows only those.",
     "after": "Add a random command for tonight's dinner", "at": (8, None)},
]

# The design chat seeded before the screenshots. Its reply's queue block queues two tasks, both waiting on the
# import task (which waits on your answer) — `after_task` takes that title here and the stub swaps in its id.
CHATS = [
    {"match": "week of dinners", "at": (38, 33),
     "say": "I'd like to plan a week of dinners and get one shopping list for the whole week. How would that "
            "fit into recipe-box?",
     "steps": [("Read", "recipe_box/cli.py"), ("Read", "recipe_box/store.py"), ("Grep", "ingredients")],
     "reply": """It fits well. The shopping list task (#{shop}) already adds up ingredients across recipes, so a week \
plan only needs a place to keep the week and a command to read it.

**How it would work**

- `plan.json` next to `recipes.json` holds seven days, each a recipe id or empty.
- `recipe-box plan` prints the week; `plan mon 3` sets Monday to recipe 3.
- `recipe-box shop --week` builds one list from the plan, using the shopping list code.

**What I'd leave out for now:** leftovers and portions per day. Both are easy to add once the plan file exists.

I've queued it as two tasks. The second waits for the first, and both wait for #{shop} to merge.

```queue
[{"title": "Keep a week of dinners in plan.json", "body": "Add plan.json (seven days, each a recipe id or empty) and `recipe-box plan` to print and set it, with tests.", "after_task": "{shop}"},
 {"title": "Shopping list for the whole week", "body": "`recipe-box shop --week` builds one shopping list from plan.json with recipe_box.shopping, with tests.", "after": 0}]
```"""},
]

# What the clips do live (scripts/demo_capture.py): a task queued from the dashboard, and a chat reply.
CLIP_TASKS = [
    {"title": "Export a recipe as Markdown",
     "match": "export a recipe as markdown",
     "slow": 1.5,
     "steps": [("Read", "recipe_box/cli.py"), ("Write", "recipe_box/export.py"), ("Write", "tests/test_export.py"),
               ("Bash", "python3 -m unittest discover -s tests -q")],
     "edits": [wr("recipe_box/export.py", EXPORT), wr("tests/test_export.py", EXPORT_TEST)],
     "commit": "export: a recipe as Markdown",
     "result": {"status": "done", "summary": "`recipe_box.export.markdown()` turns a recipe into Markdown: title, "
                "serving and time, ingredients and method. Tested on Tomato soup."}},
]
CLIP_CHATS = [
    {"match": "what's in the fridge",
     "say": "Could recipe-box suggest what to cook from what's in the fridge?",
     "slow": 1.6,
     "steps": [("Read", "recipe_box/store.py"), ("Grep", "ingredients")],
     "reply": """Yes. Search already matches ingredients, so "what can I cook" is a search that ranks recipes by how \
many of their ingredients you have.

- `recipe-box cook lemon butter pasta` lists recipes, best match first, with what you are missing.
- A recipe you can make with nothing missing goes at the top.

Queued as one task, after the random command (#{random}), since both change `cli.py`.

```queue
[{"title": "Suggest recipes from what's in the fridge", "body": "`recipe-box cook <ingredient>...` ranks recipes by how many of their ingredients you have and lists what is missing, with tests.", "after_task": "{random}"}]
```"""},
]


# ---------------------------------------------------------------- the stub claude (OPS_CLAUDE)

def emit(e):
    print(json.dumps(e), flush=True)


def tool(name, arg):
    key = {"Bash": "command", "Grep": "pattern", "WebFetch": "url"}.get(name, "file_path")
    return {"type": "tool_use", "id": f"toolu_{abs(hash((name, arg))) % 10**8:08d}", "name": name, "input": {key: arg}}


def play(sid, steps, pause):
    for name, arg in steps:
        emit({"type": "assistant", "session_id": sid,
              "message": {"role": "assistant", "content": [tool(name, arg)]}})
        time.sleep(pause)


def apply_edits(edits):
    for e in edits:
        (op, (path, *rest)), = e.items()
        f = Path(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        if op == "write":
            f.write_text(rest[0])
        elif op == "append":
            f.write_text(f.read_text() + rest[0])
        else:
            old, new = rest
            text = f.read_text()
            if old not in text:
                sys.exit(f"demo stub: {path} has no {old[:60]!r}")
            f.write_text(text.replace(old, new, 1))


def ids_in(text, plan):
    """Fill {shop}, {random} … in a chat reply with the ids of the tasks the seeder queued."""
    return re.sub(r"\{(\w+)\}", lambda m: str(plan["ids"].get(m.group(1), m.group(0))), text)


def stub(argv):
    """Stand in for `claude -p`: a worker turn (stream-json on stdin, $OPS_RESULT), a recording turn, or a chat."""
    plan = json.loads(Path(os.environ["DEMO_PLAN"]).read_text())
    model = "claude-opus-5-5"
    if "--output-format" in argv and argv[argv.index("--output-format") + 1] == "text":  # a one-shot draft
        print("A short note.")
        return
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else f"demo-{os.getpid()}-{time.time_ns() % 10**6}"
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": model})
    if "--input-format" in argv:  # a worker: the prompt is the first stdin line
        line = sys.stdin.readline()
        prompt = (json.loads(line).get("message") or {}).get("content") if line.strip() else ""
        prompt = prompt if isinstance(prompt, str) else json.dumps(prompt)
        return worker(plan, sid, prompt)
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    said = prompt.lower()  # a chat's prompt can quote earlier topics: the question asked last is the one to answer
    spec = max((c for c in plan["chats"] if c["match"] in said), key=lambda c: said.rfind(c["match"]), default=None)
    if spec is None:
        reply = "This is the claude-ops demo: its chats only answer the questions the demo script asks."
        steps, pause = [], 0
    else:
        reply, steps, pause = ids_in(spec["reply"], plan), spec["steps"], spec.get("slow", 0.2)
    play(sid, steps, pause)
    emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant",
                                                              "content": [{"type": "text", "text": reply}]}})
    emit({"type": "result", "subtype": "success", "session_id": sid, "num_turns": len(steps) + 1,
          "total_cost_usd": round(0.09 + 0.04 * len(steps), 2), "result": reply})


def worker(plan, sid, prompt):
    tid = os.environ.get("OPS_TASK_ID", "")
    known = Path(plan["root"]) / "sessions" / tid  # the title a task's first turn saw, for its later turns
    m = re.search(r"^## Task #\d+: (.+)$", prompt, re.M)
    if m:
        known.parent.mkdir(parents=True, exist_ok=True)
        known.write_text(m.group(1).strip())
    title = known.read_text() if known.exists() else ""
    spec = next((t for t in plan["tasks"] if t["match"] in title.lower()), None)
    result = Path(os.environ["OPS_RESULT"])
    if spec is None or spec.get("block"):  # one with no plan, or the task that stays running: play its steps, then
        play(sid, (spec or {}).get("steps") or [("Read", "recipe_box/cli.py"), ("Read", "recipe_box/store.py")], 0.5)
        end = time.time() + 30 * 60  # wait on the last one until the daemon stops it (or half an hour passes)
        while time.time() < end and os.getppid() != 1:
            time.sleep(1)
        return
    if "proof.mp4" in prompt and not m:  # the recording turn of a held task
        play(sid, [("Bash", "python3 -m recipe_box serve"), ("Bash", "playwright: record the page")], 0.3)
        video = os.environ.get("DEMO_PROOF_MP4")
        if video and Path(video).is_file():
            shutil.copy(video, Path(os.environ["OPS_PROOF_DIR"]).parent / "proof.mp4")
        result.write_text(json.dumps({"status": "done", **(spec.get("proof") or {})}))
    else:
        play(sid, spec["steps"], spec.get("slow", 0.15))
        if spec.get("edits"):
            apply_edits(spec["edits"])
            subprocess.run(["git", "add", "-A"], check=True)
            subprocess.run(["git", "commit", "-qm", spec["commit"]], check=True)
        result.write_text(json.dumps(spec["result"]))
    emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done. I wrote the result file."}]}})
    steps = len((spec or {}).get("steps") or []) + 1
    emit({"type": "result", "subtype": "success", "session_id": sid, "num_turns": steps, "duration_ms": steps * 41_000,
          "total_cost_usd": round(0.18 + 0.07 * steps, 2), "result": "done"})


# ---------------------------------------------------------------- the demo instance

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Demo:
    """One throwaway claude-ops: start() boots it, seed() fills it, retime() fixes its clock, stop() ends it."""

    def __init__(self, root=None, port=None, python=None, proof_mp4=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="ops-demo-")).resolve()
        self.port = port or free_port()
        self.python = python or sys.executable
        self.proof_mp4 = proof_mp4
        self.home = self.root / "home"
        self.data = self.home / ".claude-ops"
        self.repo = self.home / "dev" / PROJECT
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.anchor = None  # the real time retime() mapped onto DEMO_NOW
        self.ids = {}  # short name -> task id, for the chats' replies and the capture
        self.plan_file = self.root / "plan.json"

    # -- setup

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPS_", "GIT_", "CLAUDE"))}
        stub = self.root / "bin" / "claude"
        env.update(WHO, HOME=str(self.home), OPS_DATA=str(self.data), OPS_PORT=str(self.port),
                   OPS_CLAUDE=str(stub), DEMO_PLAN=str(self.plan_file), GIT_CONFIG_NOSYSTEM="1",
                   **({"DEMO_PROOF_MP4": str(self.proof_mp4)} if self.proof_mp4 else {}))
        return env

    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, env=self.env(), check=True, capture_output=True,
                              text=True).stdout.strip()

    def build_repo(self):
        for rel, text in REPO_FILES.items():
            f = self.repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "recipe-box: list, search and show")

    def write_stub(self):
        stub = self.root / "bin" / "claude"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(f"#!/bin/sh\nexec '{self.python}' '{HERE}' stub \"$@\"\n")
        stub.chmod(0o755)
        (self.home / ".gitconfig").write_text("[user]\n\tname = Sam Example\n\temail = sam@example.com\n")
        self.write_plan()

    def write_plan(self):
        tasks = [{**t, "match": t.get("match") or t["title"].lower()} for t in TASKS + CLIP_TASKS]
        self.plan_file.write_text(json.dumps({"root": str(self.root), "ids": self.ids, "tasks": tasks,
                                              "chats": CHATS + CLIP_CHATS}))

    def start(self):
        """Build the sample repo, register it with add-project, and boot the daemon. Returns self."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.build_repo()
        self.write_stub()
        out = subprocess.run([self.python, str(CODE / "opsd.py"), "add-project", str(self.repo)], env=self.env(),
                             capture_output=True, text=True, check=True).stdout
        toml = self.data / "projects" / f"{PROJECT}.toml"
        if not toml.exists():
            raise RuntimeError(f"add-project wrote no {toml}: {out}")
        toml.write_text(toml.read_text().rstrip() + "\n" + TOML_EXTRA)
        self.log = open(self.root / "opsd.log", "w")
        self.proc = subprocess.Popen([self.python, str(CODE / "opsd.py"), "serve"], cwd=self.root, env=self.env(),
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: not self.state().get("starting") and self.state().get("projects"), 30, "the daemon to start")
        return self

    def stop(self, clean=True):
        """Stop the daemon by its pid (SIGTERM, then SIGKILL after 60 s), then remove the temp folder."""
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if getattr(self, "log", None):
            self.log.close()
        if clean:
            shutil.rmtree(self.root, ignore_errors=True)

    # -- talking to it

    def api(self, path, body=None):
        req = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "X-Ops": "1"})
        with urlopen(req, timeout=30) as r:
            out = r.read()
        return json.loads(out) if out.strip() else None

    def state(self):
        try:
            return self.api("/api/state")
        except OSError:
            return {"starting": True}

    @contextlib.contextmanager
    def db(self):
        """The demo's ops.db, committed and closed after the with block."""
        c = sqlite3.connect(self.data / "ops.db", timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def task_state(self, tid):
        with self.db() as c:
            return c.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"]

    def wait(self, ok, secs, what):
        end = time.time() + secs
        while time.time() < end:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"the demo daemon exited while waiting for {what}; see {self.root}/opsd.log")
            try:
                if ok():
                    return
            except (OSError, sqlite3.Error, KeyError, TypeError):
                pass
            time.sleep(0.3)
        raise RuntimeError(f"timed out after {secs} s waiting for {what}; see {self.root}/opsd.log")

    def add_task(self, t):
        body = {"project": PROJECT, "title": t["title"], "body": t["body"], "hold": t.get("hold", 0)}
        if t.get("after"):
            body["depends_on"] = self.ids[short(t["after"])]
        tid = self.api("/api/tasks", body)["id"]
        self.ids[short(t["title"])] = tid
        self.write_plan()
        return tid

    def chat(self, c, wait=True):
        """Start a design chat with c["say"]. Returns its id; with wait, once Claude (the stub) has replied."""
        did = self.api("/api/designs", {"project": PROJECT, "text": c["say"]})["id"]
        if wait:
            self.wait(lambda: self.chat_replied(did), 60, f"design chat #{did} to reply")
        return did

    def chat_replied(self, did):
        with self.db() as c:
            return c.execute("SELECT 1 FROM design_msgs WHERE design_id=? AND role='assistant'", (did,)).fetchone()

    # -- seeding

    def seed(self):
        """Queue the sample tasks one at a time, each to its settled state, then the chat and a schedule."""
        settled = {"done", "needs_input", "review", "failed"}
        for t in TASKS:
            tid = self.add_task(t)
            if t.get("block"):
                self.wait(lambda: self.task_state(tid) == "running", 60, f"#{tid} to start")
            elif not t.get("after"):
                self.wait(lambda: self.task_state(tid) in settled, 120, f"#{tid} to finish")
                if self.task_state(tid) == "failed":
                    raise RuntimeError(f"seed task #{tid} failed; see {self.root}/opsd.log and runs/{tid}")
        self.chats = [self.chat(c) for c in CHATS]
        self.api("/api/schedules", {"project": PROJECT, "title": "Check that every recipe still loads",
                                    "every": "mon 09:00",
                                    "body": "Load recipes.json and ~/.recipe-box/recipes.json, and report any "
                                            "recipe with a missing field."})
        return self

    # -- the fixed clock

    def retime(self):
        """Move every timestamp onto the demo timeline, the same on every run: a task is queued `at[0]` minutes before
        now, starts a fifth of the way to `at[1]` and finishes then; its log lines are spread evenly in order. A chat
        keeps its messages in proportion. The capture then shows real time minus anchor plus DEMO_NOW."""
        now = self.anchor = float(int(time.time()))  # whole seconds: the schedule lands on 09:00, not 08:59:59.9
        with self.db() as c:
            for t in TASKS:
                tid = self.ids[short(t["title"])]
                row = c.execute("SELECT created, started, finished FROM tasks WHERE id=?", (tid,)).fetchone()
                queued = now - t["at"][0] * 60
                end = now - t["at"][1] * 60 if t["at"][1] is not None else now - 45
                started = queued + (end - queued) / 5 if row["started"] else None
                c.execute("UPDATE tasks SET created=?, started=?, finished=? WHERE id=?",
                          (queued, started, end if row["finished"] else None, tid))
                rows = c.execute("SELECT id, ts FROM events WHERE task_id=? ORDER BY id", (tid,)).fetchall()
                live = [r for r in rows if row["started"] and r["ts"] >= row["started"]]
                for k, r in enumerate(rows[:len(rows) - len(live)]):  # queued … started
                    c.execute("UPDATE events SET ts=? WHERE id=?", (queued + k, r["id"]))
                for k, r in enumerate(live):  # started … finished, evenly
                    c.execute("UPDATE events SET ts=? WHERE id=?",
                              (started + (end - started) * k / max(len(live) - 1, 1), r["id"]))
                to = remap(row["started"] or row["created"], row["finished"] or now, started or queued, end)
                retime_rows(c, "task_msgs", "task_id", tid, to)
                c.execute("UPDATE tasks SET last_call=? WHERE id=? AND last_call IS NOT NULL", (end, tid))
                c.execute("UPDATE todos SET created=? WHERE source_kind='task' AND source_id=?", (end, tid))
            for spec, did in zip(CHATS, self.chats):
                d = c.execute("SELECT created, updated FROM designs WHERE id=?", (did,)).fetchone()
                last = c.execute("SELECT max(ts) FROM design_msgs WHERE design_id=?", (did,)).fetchone()[0]
                to = remap(d["created"], max(last or 0, d["updated"]), now - spec["at"][0] * 60,
                           now - spec["at"][1] * 60)
                c.execute("UPDATE designs SET created=?, updated=?, seen=? WHERE id=?",
                          (to(d["created"]), to(d["updated"]), to(d["updated"]), did))
                retime_rows(c, "design_msgs", "design_id", did, to)
                for (ids,) in c.execute("SELECT task_ids FROM design_msgs WHERE design_id=? AND task_ids IS NOT NULL",
                                        (did,)).fetchall():
                    for tid in json.loads(ids or "[]"):  # the tasks its reply queued: queued with the reply
                        at = now - spec["at"][1] * 60
                        c.execute("UPDATE tasks SET created=? WHERE id=?", (at, tid))
                        c.execute("UPDATE events SET ts=? WHERE task_id=?", (at, tid))
            c.execute("UPDATE schedules SET created=?, next_at=?", (now - 3 * 86400, now + NEXT_MONDAY_9 - DEMO_NOW))
        try:  # any POST drops the daemon's cached /api/state, so the next poll shows the new times
            self.api("/api/settings", {})
        except OSError:
            pass
        time.sleep(1.5)
        return self


def short(title):
    """The key a task goes by in a chat's reply: its first distinctive word."""
    for word, key in (("random", "random"), ("shopping list for several", "shop"), ("import", "import")):
        if word in title.lower():
            return key
    return title.lower()


def remap(lo, hi, new_lo, new_hi):
    span = max(hi - lo, 1e-6)
    return lambda t: new_lo + (min(max(t, lo), hi) - lo) * (new_hi - new_lo) / span


def retime_rows(c, table, key, kid, to):
    for r in c.execute(f"SELECT id, ts FROM {table} WHERE {key}=?", (kid,)).fetchall():
        c.execute(f"UPDATE {table} SET ts=? WHERE id=?", (to(r["ts"]), r["id"]))


def stop_on_term():
    """SIGTERM (and SIGHUP) end the demo as Ctrl-C does, so the daemon is stopped and the temp folder removed."""
    def raise_interrupt(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, raise_interrupt)


def main():
    stop_on_term()
    d = Demo(port=int(os.environ["DEMO_PORT"]) if os.environ.get("DEMO_PORT") else None).start()
    try:
        d.seed().retime()
        print(f"claude-ops demo: {d.url}  (data in {d.root}; Ctrl-C stops it)", flush=True)
        while d.proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        d.stop()


if __name__ == "__main__":
    if sys.argv[1:2] == ["stub"]:
        stub(sys.argv[2:])
    else:
        main()
