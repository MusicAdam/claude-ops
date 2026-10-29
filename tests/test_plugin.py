"""The claude-ops plugin (README ### The plugin): it ships in plugin/, every claude that can use skills or commands
loads it with --plugin-dir, the spec commands and briefs name its namespaced skills and commands whatever is in
~/.claude, and it holds nothing personal. Stdlib only, no Claude calls.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "opsd" not in sys.modules:
    os.environ["OPS_DATA"] = tempfile.mkdtemp()
    os.environ["OPS_PORT"] = os.environ.get("OPS_PORT", "47964")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import opsd  # noqa: E402


def claude_launches():
    """Every list literal in opsd.py that starts a claude: [..., CLAUDE, ...], with its line number."""
    tree = ast.parse((ROOT / "opsd.py").read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.List)
            and any(isinstance(e, ast.Name) and e.id == "CLAUDE" for e in n.elts)]


def toolless(node):
    """A launch with --tools "": it can't use a skill or command."""
    vals = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
    return any(a == "--tools" and b == "" for a, b in zip(vals, vals[1:]))


class PluginFolder(unittest.TestCase):
    def test_manifest_and_contents(self):
        self.assertEqual(opsd.PLUGIN, ROOT / "plugin")
        manifest = json.loads((opsd.PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "claude-ops")
        self.assertTrue(manifest.get("description") and manifest.get("version"))
        self.assertTrue((opsd.PLUGIN / "skills" / "research" / "SKILL.md").is_file())
        self.assertTrue((opsd.PLUGIN / "skills" / "research" / "access.md").is_file())
        for cid in ("audit", "review", "deviation-check", "implement"):
            self.assertTrue((opsd.PLUGIN / "commands" / "openspec" / f"{cid}.md").is_file(), cid)

    def test_nothing_personal(self):
        bad = re.compile(r"Adam|~/dev|/Users/|~/\.claude|dev/research|\btk\b|tk:")
        for f in opsd.PLUGIN.rglob("*"):
            if f.is_file():
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    self.assertIsNone(bad.search(line), f"{f.relative_to(ROOT)}:{i}: {line}")

    def test_research_skill_is_project_mode_only(self):
        skill = (opsd.PLUGIN / "skills" / "research" / "SKILL.md").read_text()
        self.assertIn("docs/research/<slug>.md", skill.split("---")[1])  # the frontmatter description
        for gone in ("scout", "sweep", "bin/fetch", "Knowledge base"):
            self.assertNotIn(gone, skill)
        templates = {f.name for f in (opsd.PLUGIN / "skills" / "research" / "templates").iterdir()}
        self.assertNotIn("scout-report.md", templates)
        self.assertNotIn("project-check.md", templates)
        for named in re.findall(r"templates/([\w-]+\.md)", skill):
            self.assertIn(named, templates)

    def test_review_names_no_specialist_agent(self):
        review = (opsd.PLUGIN / "commands" / "openspec" / "review.md").read_text()
        self.assertIn("general-purpose", review)
        for agent in ("python-code-expert", "mysql-schema-architect", "python-test-expert", "aws-cloud-architect"):
            self.assertNotIn(f"`{agent}`", review)


class Launches(unittest.TestCase):
    def test_every_launch_with_tools_loads_the_plugin(self):
        self.assertEqual(opsd.PLUGIN_ARGS, ["--plugin-dir", str(opsd.PLUGIN)])
        launches = claude_launches()
        self.assertGreaterEqual(len(launches), 6)  # worker, 2 design/spec chats, 2 project chats, the one-shot
        with_tools = [n for n in launches if not toolless(n)]
        self.assertGreaterEqual(len(with_tools), 5)
        for n in with_tools:
            self.assertTrue(any(isinstance(e, ast.Starred) and getattr(e.value, "id", "") == "PLUGIN_ARGS"
                                for e in n.elts), f"opsd.py:{n.lineno} starts claude without --plugin-dir")
        for n in launches:
            if toolless(n):
                self.assertNotIn("PLUGIN_ARGS", ast.unparse(n), f"opsd.py:{n.lineno}")


class Names(unittest.TestCase):
    def test_spec_commands_without_tk(self):
        home = tempfile.mkdtemp()  # no ~/.claude/commands/tk here
        with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.object(opsd, "HOME", Path(home)):
            cmds = {c["id"]: c["text"] for c in opsd.spec_commands([Path(tempfile.mkdtemp())], "add-x")}
        for cid in ("audit", "review", "deviation-check"):
            self.assertEqual(cmds[cid], f"/claude-ops:openspec:{cid} add-x")
        self.assertNotIn("explore", cmds)  # opsx still needs openspec init in the repo

    def test_research_brief_names_the_skill(self):
        brief = opsd.research_brief("What is X?", "T1", "docs/research/what-is-x.md")
        self.assertIn("/claude-ops:research", brief)
        self.assertIn("docs/research/what-is-x.md", brief)
        self.assertNotIn("~/dev", brief)

    def test_prompts_name_the_plugin_commands(self):
        for f in ("spec_prompt.md", "spec_new_prompt.md"):
            text = (ROOT / f).read_text()
            self.assertIn("/claude-ops:openspec:", text)
            self.assertNotIn("tk:", text)


if __name__ == "__main__":
    unittest.main()
