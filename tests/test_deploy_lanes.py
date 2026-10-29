"""The project home's deploy lanes in dashboard.html (README TODAY-2): depLanes groups the [[deploy]] targets by repo
and their `from` chains. Runs the page's own pure helper in node; skipped without node.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"
START, END = "// ---- Deploy lanes, pure", "// ---- end of Deploy lanes"
RUN = r"""
const h = require("fs").readFileSync(process.argv[1], "utf8");
eval(h.slice(h.indexOf(process.argv[3]), h.indexOf(process.argv[4])));
console.log(JSON.stringify(depLanes(JSON.parse(process.argv[2]))));
"""


def t(name, repo=None, frm=None):
    return {"name": name, "repo": repo, "from": frm}


@unittest.skipUnless(shutil.which("node"), "needs node")
class Lanes(unittest.TestCase):
    def lanes(self, deploys):
        out = subprocess.run(["node", "-e", RUN, str(PAGE), json.dumps(deploys), START, END], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)

    def names(self, deploys):
        return [[s["name"] for s in lane["stages"]] for lane in self.lanes(deploys)]

    def test_shop_chains(self):
        ds = [t("app test", "shop-app"), t("app distribute", "shop-app", "app test"),
              t("api prod", "shop-api"), t("web preview", "shop-web"), t("web prod", "shop-web", "web preview")]
        lanes = self.lanes(ds)
        self.assertEqual([[s["short"] for s in l["stages"]] for l in lanes], [["test", "distribute"], ["prod"], ["preview", "prod"]])
        self.assertEqual([l["label"] for l in lanes], ["app", "api", "web"])
        self.assertEqual([l["repo"] for l in lanes], ["shop-app", "shop-api", "shop-web"])

    def test_chain_follows_from_not_config_order(self):
        ds = [t("web prod", "web", "web preview"), t("web preview", "web")]
        self.assertEqual(self.names(ds), [["web preview", "web prod"]])

    def test_missing_or_unknown_from_starts_its_own_lane(self):
        ds = [t("a test", "a"), t("a prod", "a", "a staging"), t("b prod", "b", "")]
        self.assertEqual(self.names(ds), [["a test"], ["a prod"], ["b prod"]])

    def test_lanes_of_one_repo_sit_together(self):
        ds = [t("x one", "x"), t("y one", "y"), t("x two", "x")]
        self.assertEqual(self.names(ds), [["x one"], ["x two"], ["y one"]])

    def test_a_second_child_starts_a_lane_after_its_parent(self):
        ds = [t("w preview", "w"), t("w prod", "w", "w preview"), t("w beta", "w", "w preview")]
        lanes = self.lanes(ds)
        self.assertEqual([[s["name"] for s in l["stages"]] for l in lanes], [["w preview", "w prod"], ["w beta"]])
        self.assertEqual([l["after"] for l in lanes], [None, "w preview"])

    def test_a_loop_of_froms_still_shows_every_target_once(self):
        ds = [t("a", None, "b"), t("b", None, "a"), t("c", None, "c")]
        self.assertEqual(sorted(sum(self.names(ds), [])), ["a", "b", "c"])
        self.assertEqual(self.names(ds)[0], ["c"])

    def test_no_shared_word_keeps_full_names_and_labels_by_repo(self):
        lanes = self.lanes([t("staging", "api"), t("production", "api", "staging"), t("deploy")])
        self.assertEqual([(l["label"], [s["short"] for s in l["stages"]]) for l in lanes],
                         [("api", ["staging", "production"]), ("", ["deploy"])])

    def test_no_targets_no_lanes(self):
        self.assertEqual(self.lanes([]), [])


if __name__ == "__main__":
    unittest.main()
