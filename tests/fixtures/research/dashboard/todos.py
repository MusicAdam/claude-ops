"""To-do store shared by the dashboard server and bin/todo.

A to-do can stem from a proposal (brief slug), a project, or neither. Linking a
proposal is enough: its project is looked up at read time, so moving the proposal
to another project moves its to-dos with it.

File: dashboard/todos.json  {"next": N, "todos": [{id, text, notes, project,
proposal, due, created, done, by}]}. Writes take an flock and replace the file
atomically, so the server and the CLI can both write without losing an edit.
"""
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TODOS_FILE = HERE / "todos.json"
LOCK_FILE = HERE / ".todos.lock"
PROJECTS_FILE = HERE / "projects.json"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FIELDS = ("text", "notes", "project", "proposal", "due")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(TODOS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("todos", [])
    data.setdefault("next", max((t["id"] for t in data["todos"]), default=0) + 1)
    return data


@contextmanager
def _locked():
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        yield data
        tmp = TODOS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, TODOS_FILE)


def _projects() -> dict:
    try:
        data = json.loads(PROJECTS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    return {"projects": data.get("projects", {}), "members": data.get("members", {})}


def _clean(body: dict) -> dict:
    """Validate the editable fields present in body. Empty strings clear a field."""
    out = {}
    for key in FIELDS:
        if key not in body:
            continue
        val = (body[key] or "").strip() or None
        if key == "text" and not val:
            raise ValueError("Write the to-do first.")
        if key == "project" and val and val not in _projects()["projects"]:
            raise ValueError(f"No project named {val}")
        if key == "proposal" and val and not (re.fullmatch(r"[\w.-]+", val) and (REPO / "briefs" / f"{val}.md").is_file()):
            raise ValueError(f"No proposal named {val}")
        if key == "due" and val and not DATE_RE.fullmatch(val):
            raise ValueError("Due date must be YYYY-MM-DD.")
        out[key] = val
    return out


def list_todos() -> list[dict]:
    """Every to-do, with `effectiveProject`: its own project, else its proposal's."""
    members = _projects()["members"]
    todos = _load()["todos"]
    return [{**t, "effectiveProject": t.get("project") or members.get(t.get("proposal") or "")} for t in todos]


def add(body: dict, by: str = "dashboard") -> dict:
    fields = _clean({"text": body.get("text", ""), **{k: body[k] for k in FIELDS[1:] if k in body}})
    with _locked() as data:
        todo = {"id": data["next"], "text": fields["text"], "notes": fields.get("notes"),
                "project": fields.get("project"), "proposal": fields.get("proposal"),
                "due": fields.get("due"), "created": today(), "done": None, "by": by}
        data["todos"].append(todo)
        data["next"] += 1
    return todo


def update(tid: int, body: dict) -> dict:
    fields = _clean(body)
    with _locked() as data:
        todo = next((t for t in data["todos"] if t["id"] == tid), None)
        if not todo:
            raise ValueError(f"No to-do #{tid}")
        todo.update(fields)
        if "done" in body:
            todo["done"] = today() if body["done"] else None
    return todo


def delete(tid: int) -> dict:
    with _locked() as data:
        before = len(data["todos"])
        data["todos"] = [t for t in data["todos"] if t["id"] != tid]
        if len(data["todos"]) == before:
            raise ValueError(f"No to-do #{tid}")
    return {"id": tid, "deleted": True}
