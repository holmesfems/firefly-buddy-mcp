"""Requests and results on disk, shared by the MCP server and the popup (two processes).

Layout under FIREFLY_BUDDY_HOME (default ~/.firefly-buddy-mcp):

- requests/<batch_id>.json   what Claude asked for (written once by the server)
- results/<batch_id>.json    what came back (read-modify-write under a lock, by both processes)
- active.json                the item the human is working on now (written by the popup only)
- popup.alive                touched every second while the popup runs
- originals/<batch_id>/      the downloaded files as they came from Firefly
- outbox/<batch_id>/         results for items that gave no dest_dir
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

FIREFLY_SFX_URL = "https://firefly.adobe.com/generate/sound-effects"
HEARTBEAT_STALE = 5.0      # seconds without a touch before the popup counts as closed
LOCK_STALE = 30.0          # a lock file older than this is left over from a crash

# item status: waiting -> received / rejected (none of the takes was good) / skipped / cancelled
WAITING, RECEIVED, SKIPPED, CANCELLED = "waiting", "received", "skipped", "cancelled"
REJECTED = "rejected"


def home() -> Path:
    return Path(os.environ.get("FIREFLY_BUDDY_HOME") or Path.home() / ".firefly-buddy-mcp")


def requests_dir() -> Path:
    return home() / "requests"


def results_dir() -> Path:
    return home() / "results"


def originals_dir() -> Path:
    return home() / "originals"


def outbox_dir() -> Path:
    return home() / "outbox"


def heartbeat_path() -> Path:
    return home() / "popup.alive"


def active_path() -> Path:
    return home() / "active.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------- json files

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(50):  # Windows: the target may be open in the other process for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"could not write {path}")


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.02)
    return default


@contextmanager
def locked(path: Path, timeout: float = 10.0):
    """An exclusive lock between processes: a <path>.lock file made with O_EXCL."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock is busy: {lock}")
            time.sleep(0.02)
        except PermissionError:  # Windows: the lock file is being deleted right now
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock is busy: {lock}")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except PermissionError:
            pass


# ---------------------------------------------------------------- requests / results

def save_request(req: dict) -> None:
    write_json(requests_dir() / f"{req['batch_id']}.json", req)
    results = {"batch_id": req["batch_id"], "updated": now_iso(),
               "items": {it["id"]: {"status": WAITING, "files": []} for it in req["items"]}}
    with locked(results_dir() / f"{req['batch_id']}.json"):
        write_json(results_dir() / f"{req['batch_id']}.json", results)


def load_request(batch_id: str) -> dict | None:
    return read_json(requests_dir() / f"{batch_id}.json")


def list_batch_ids() -> list[str]:
    d = requests_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if not p.name.startswith("."))


def load_results(batch_id: str) -> dict:
    res = read_json(results_dir() / f"{batch_id}.json") or {"batch_id": batch_id, "items": {}}
    res.setdefault("items", {})
    return res


def item_state(res: dict, item_id: str) -> dict:
    st = res["items"].setdefault(item_id, {})
    st.setdefault("status", WAITING)
    st.setdefault("files", [])
    return st


def update_results(batch_id: str, fn: Callable[[dict], None]) -> dict:
    path = results_dir() / f"{batch_id}.json"
    with locked(path):
        res = load_results(batch_id)
        fn(res)
        res["updated"] = now_iso()
        write_json(path, res)
    return res


def batch_of(item_id: str) -> str:
    return item_id.rsplit("-", 1)[0]


def find_item(item_id: str) -> tuple[dict, dict]:
    req = load_request(batch_of(item_id))
    if req:
        for it in req["items"]:
            if it["id"] == item_id:
                return req, it
    raise KeyError(f"no such item: {item_id}")


def is_open(st: dict) -> bool:
    """Still accepting files: waiting, or received but the human may add more."""
    return st.get("status") in (WAITING, RECEIVED)


def pending_count(req: dict, res: dict) -> int:
    return sum(1 for it in req["items"] if item_state(res, it["id"])["status"] == WAITING)


def signature() -> tuple:
    """Changes whenever a request or result file changes (cheap to poll)."""
    sig = []
    for d in (requests_dir(), results_dir()):
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                try:
                    st = p.stat()
                    sig.append((p.name, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    pass
    return tuple(sig)


# ---------------------------------------------------------------- popup presence

def touch_heartbeat() -> None:
    p = heartbeat_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()), encoding="utf-8")


def clear_heartbeat() -> None:
    try:
        heartbeat_path().unlink(missing_ok=True)
    except PermissionError:
        pass


def popup_alive() -> bool:
    try:
        return time.time() - heartbeat_path().stat().st_mtime < HEARTBEAT_STALE
    except FileNotFoundError:
        return False


def set_active(item_id: str | None, since: float | None = None) -> None:
    write_json(active_path(), {"item": item_id, "since": since})


def get_active() -> dict:
    a = read_json(active_path()) or {}
    if not popup_alive():
        return {"item": None, "since": None}
    return {"item": a.get("item"), "since": a.get("since")}
