"""What happens to an item: a file arrives, the human skips it, Claude cancels it."""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import intake, store


def dest_for(req: dict, item: dict) -> Path:
    return Path(item.get("dest_dir") or store.outbox_dir() / req["batch_id"])


class DuplicateFile(intake.IntakeError):
    """The same content was already received in this batch."""

    def __init__(self, path: Path, item_name: str, delivered: str):
        self.item_name, self.delivered = item_name, delivered
        super().__init__(f"{Path(path).name} has the same content as {Path(delivered).name} "
                         f"already received for {item_name}; skipped")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(req: dict, digest: str) -> tuple[str, str] | None:
    """(item name, delivered file) when a file with this content was received in the batch:
    the same download picked twice, a browser's "take (1).wav" copy, or a delivered file itself."""
    res = store.load_results(req["batch_id"])
    for it in req["items"]:
        s = store.item_state(res, it["id"])
        for rec in s.get("received", []):
            if digest == rec.get("source") or digest in rec.get("outputs", []):
                return it["name"], (rec.get("files") or [""])[0]
    return None


def receive_file(item_id: str, path: Path, from_downloads: bool) -> dict:
    """Deliver `path` for the item and record it. from_downloads: move the original out of
    Downloads into originals/<batch>/ (a file Claude names by hand is only copied).
    Raises DuplicateFile (and leaves the file alone) when the batch already has this content."""
    req, item = store.find_item(item_id)
    st = store.item_state(store.load_results(req["batch_id"]), item_id)
    if st["status"] == store.CANCELLED:
        raise intake.IntakeError(f"{item_id} was cancelled")
    if not Path(path).is_file():
        raise intake.IntakeError(f"not a file: {path}")
    digest = file_hash(Path(path))
    dup = find_duplicate(req, digest)
    if dup:
        raise DuplicateFile(Path(path), *dup)
    keep = store.originals_dir() / req["batch_id"] if from_downloads else None
    seconds = None
    if Path(path).suffix.lower() in intake.MEDIA_EXT:   # measured before conversion trims the start
        seconds = intake.media_seconds(Path(path))
    length_note = intake.length_warning(seconds, item.get("duration"))
    try:
        r = intake.deliver(Path(path), dest_for(req, item), item["name"],
                           item.get("convert", "pcm16"), item.get("trim_silence", True), keep)
    except intake.IntakeError as e:
        def note_error(res):
            store.item_state(res, item_id)["error"] = str(e)
        store.update_results(req["batch_id"], note_error)
        raise

    outputs = [file_hash(Path(f)) for f in r["files"]]
    rec = {"source": digest, "outputs": outputs, "files": r["files"]}
    if seconds is not None:
        rec["seconds"] = round(seconds, 2)
    if length_note:
        rec["warnings"] = [length_note]
        r["warnings"] = r["warnings"] + [length_note]

    def record(res):
        s = store.item_state(res, item_id)
        s["status"] = store.RECEIVED
        s["files"] = s["files"] + r["files"]
        s["received"] = s.get("received", []) + [rec]
        s["warnings"] = sorted(set(s.get("warnings", []) + [w for w in r["warnings"] if w != length_note]))
        s.pop("error", None)
        s["updated"] = store.now_iso()
    store.update_results(req["batch_id"], record)
    return r


def takes(st: dict) -> dict[str, dict]:
    """file -> {"seconds": .., "warnings": [..]} for the files an item received."""
    out = {}
    for rec in st.get("received", []):
        for f in rec.get("files", []):
            out[f] = {"seconds": rec.get("seconds"), "warnings": rec.get("warnings", [])}
    return out


def unreceive(item_id: str, file: str) -> dict:
    """Take back one received file: forget it (so the same download can be received again) and
    delete the delivered copy. The original stays in originals/<batch>/ (or wherever it was).
    An item left with no files goes back to waiting."""
    target = str(Path(file))

    def drop(res):
        s = store.item_state(res, item_id)
        if target not in s["files"]:
            raise KeyError(f"{Path(file).name} is not a file received for {item_id}")
        s["files"] = [f for f in s["files"] if f != target]
        kept = []
        for rec in s.get("received", []):
            if target in rec.get("files", []):
                i = rec["files"].index(target)
                rec = {**rec, "files": rec["files"][:i] + rec["files"][i + 1:],
                       "outputs": [o for j, o in enumerate(rec.get("outputs", [])) if j != i]}
                if not rec["files"]:
                    continue
            kept.append(rec)
        s["received"] = kept
        if not s["files"] and s["status"] == store.RECEIVED:
            s["status"] = store.WAITING
        s["updated"] = store.now_iso()
    res = store.update_results(store.batch_of(item_id), drop)
    warnings = []
    try:
        Path(target).unlink(missing_ok=True)
    except OSError as e:
        warnings.append(f"could not delete {Path(target).name}: {e}")
    return {"removed": target, "status": store.item_state(res, item_id)["status"], "warnings": warnings}


def _append_feedback(s: dict, text: str) -> None:
    text = (text or "").strip()
    if text:
        s["feedback"] = s.get("feedback", []) + [{"at": store.now_iso(), "text": text}]


def add_feedback(item_id: str, text: str) -> None:
    """A note from the human to Claude about this item (e.g. which take is best)."""
    if not (text or "").strip():
        return

    def add(res):
        s = store.item_state(res, item_id)
        _append_feedback(s, text)
        s["updated"] = store.now_iso()
    store.update_results(store.batch_of(item_id), add)


def reject_item(item_id: str, feedback: str = "") -> None:
    """None of Firefly's takes was usable. The feedback says why, so Claude can fix the prompt."""
    def mark(res):
        s = store.item_state(res, item_id)
        if store.is_open(s):
            s["status"] = store.REJECTED
            _append_feedback(s, feedback)
            s["updated"] = store.now_iso()
    store.update_results(store.batch_of(item_id), mark)


def skip_item(item_id: str) -> None:
    def mark(res):
        s = store.item_state(res, item_id)
        if s["status"] == store.WAITING:
            s["status"] = store.SKIPPED
            s["updated"] = store.now_iso()
    store.update_results(store.batch_of(item_id), mark)


def cancel_items(batch_id: str, item_ids: list[str] | None = None) -> list[str]:
    """Cancel the still-waiting items (all of the batch when item_ids is None)."""
    req = store.load_request(batch_id)
    if not req:
        raise KeyError(f"no such batch: {batch_id}")
    wanted = set(item_ids) if item_ids is not None else {it["id"] for it in req["items"]}
    unknown = wanted - {it["id"] for it in req["items"]}
    if unknown:
        raise KeyError(f"not in {batch_id}: {', '.join(sorted(unknown))}")
    done: list[str] = []

    def mark(res):
        for iid in sorted(wanted):
            s = store.item_state(res, iid)
            if s["status"] == store.WAITING:
                s["status"] = store.CANCELLED
                s["updated"] = store.now_iso()
                done.append(iid)
    store.update_results(batch_id, mark)
    return done
