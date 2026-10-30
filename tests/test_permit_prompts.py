"""A project chat's permission prompts: the MCP server the daemon serves for claude's --permission-prompt-tool, and
Approve and Deny in the chat view. Stdlib only, no Claude calls: requests go over HTTP to a daemon handler on a
spare port, as claude's HTTP MCP client sends them.
Run from the repo root: /opt/homebrew/bin/python3 -m unittest discover -s tests -q"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

DATA = tempfile.mkdtemp()
os.environ["OPS_DATA"] = DATA
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import opsd  # noqa: E402


def rpc(port, did, msg):
    req = Request(f"http://127.0.0.1:{port}/mcp/permit/{did}", data=json.dumps(msg).encode(),
                  headers={"Content-Type": "application/json", "X-Ops": "1",
                           "Accept": "application/json, text/event-stream"})
    with urlopen(req, timeout=10) as r:
        body = r.read()
        return r.status, json.loads(body) if body else None


class Permits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        opsd.init_db()
        opsd.READY.set()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), opsd.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, did, tool="Bash", inp=None):
        """A tools/call in the background, as claude makes while its turn waits: (thread, box for the answer)."""
        box = {}
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "permit", "arguments": {
            "tool_name": tool, "input": inp or {"command": "echo hi"}, "tool_use_id": "toolu_1"}}}

        def go():
            box["status"], box["out"] = rpc(self.port, did, msg)
        t = threading.Thread(target=go)
        t.start()
        for _ in range(100):
            if opsd.permits_of(did):
                break
            time.sleep(0.02)
        return t, box

    def answer(self, box):
        return json.loads(box["out"]["result"]["content"][0]["text"])

    def test_handshake(self):
        status, out = rpc(self.port, 1, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                         "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(status, 200)
        self.assertEqual(out["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", out["result"]["capabilities"])
        status, out = rpc(self.port, 1, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, out), (202, None))
        status, out = rpc(self.port, 1, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool, = out["result"]["tools"]
        self.assertEqual(f"mcp__{opsd.PERMIT_SERVER}__{tool['name']}", opsd.PERMIT_TOOL)
        self.assertEqual(rpc(self.port, 1, {"jsonrpc": "2.0", "id": 3, "method": "nope"})[1]["error"]["code"], -32601)

    def test_approve(self):
        opsd.q("INSERT INTO designs (project, title, kind, created, updated) VALUES ('p','t','project',0,0)")
        did = opsd.q("SELECT MAX(id) AS id FROM designs", one=True)["id"]
        t, box = self.call(did, inp={"command": "opsctl restart", "description": "restart"})
        (req,) = opsd.permits_of(did)
        self.assertEqual((req["tool"], req["input"]["command"]), ("Bash", "opsctl restart"))
        self.assertEqual(opsd.permits_of(did + 1), [])  # another chat's view doesn't show it
        h = opsd.H.__new__(opsd.H)
        self.assertEqual(h.route(f"/api/designs/{did}/permit", {"id": req["id"], "allow": True}), {"ok": True})
        t.join(5)
        self.assertEqual(self.answer(box), {"behavior": "allow", "updatedInput": {"command": "opsctl restart",
                                                                                  "description": "restart"}})
        self.assertEqual(opsd.permits_of(did), [])
        with self.assertRaisesRegex(ValueError, "gone"):  # a second click, or another tab
            opsd.permit_answer(did, req["id"], True)

    def test_deny(self):
        t, box = self.call(10013)
        opsd.permit_answer(10013, opsd.permits_of(10013)[0]["id"], False)
        t.join(5)
        out = self.answer(box)
        self.assertEqual(out["behavior"], "deny")
        self.assertIn("The owner denied it", out["message"])

    def test_wrong_chat_cannot_answer(self):
        t, box = self.call(10014)
        rid = opsd.permits_of(10014)[0]["id"]
        with self.assertRaises(ValueError):
            opsd.permit_answer(10015, rid, True)
        opsd.permits_drop(10014, "the reply ended first")
        t.join(5)
        out = self.answer(box)
        self.assertEqual(out["behavior"], "deny")
        self.assertIn("the reply ended first", out["message"])

    def test_no_answer_denies(self):
        out = opsd.permit_ask(10016, "Write", {"file_path": "/x"}, wait=0)
        self.assertEqual(out["behavior"], "deny")
        self.assertIn("no answer", out["message"])
        self.assertEqual(opsd.permits_of(10016), [])

    def test_get_is_refused(self):  # no event stream: claude's client then sticks to POST
        with self.assertRaises(Exception) as e:
            urlopen(f"http://127.0.0.1:{self.port}/mcp/permit/1", timeout=5)
        self.assertEqual(e.exception.code, 405)
        e.exception.close()


if __name__ == "__main__":
    unittest.main()
