"""Picking finished downloads out of the Downloads folder and putting them where Claude asked."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

MEDIA_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov"}
ARCHIVE_EXT = {".zip"}
WATCH_EXT = MEDIA_EXT | ARCHIVE_EXT
SAMPLE_RATE = 48000          # AK Studio mixes at 48 kHz and reads 16-bit PCM wav only
TRIM_FILTER = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.005"


class IntakeError(Exception):
    pass


# ---------------------------------------------------------------- where things are

def downloads_dir() -> Path:
    env = os.environ.get("FIREFLY_BUDDY_DOWNLOADS")
    if env:
        return Path(env)
    if os.name == "nt":
        try:
            return Path(_known_folder("{374DE290-123F-4565-9164-39C4925E467B}"))
        except Exception:  # noqa: BLE001 - fall back to the usual place
            pass
    return Path.home() / "Downloads"


def _known_folder(guid: str) -> str:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(guid, ctypes.byref(g))
    out = ctypes.c_wchar_p()
    ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def ffmpeg_exe() -> str | None:
    env = os.environ.get("FIREFLY_BUDDY_FFMPEG")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- watching Downloads

class DownloadWatcher:
    """Reports files that appear in `folder` after it started, once their size stops changing.

    Files already there at the start, and files that arrive while nothing is being received
    (`accepting=False`), are never reported, so unrelated downloads are left alone.
    Browsers write to a temporary name (.crdownload / .part) and rename at the end; only the
    final name has a watched extension.
    """

    def __init__(self, folder: Path, settle_polls: int = 2):
        self.folder = Path(folder)
        self.settle_polls = settle_polls   # polls with an unchanged size before a file counts as done
        self.seen: set[str] = set(self._names())
        self.sizes: dict[str, tuple[int, int]] = {}   # name -> (size, polls it has stayed that size)

    def _names(self) -> list[str]:
        try:
            return [e.name for e in os.scandir(self.folder) if e.is_file()]
        except FileNotFoundError:
            return []

    def poll(self, accepting: bool) -> list[Path]:
        ready: list[Path] = []
        try:
            entries = list(os.scandir(self.folder))
        except FileNotFoundError:
            return ready
        for e in entries:
            name = e.name
            if name in self.seen or not e.is_file():
                continue
            if Path(name).suffix.lower() not in WATCH_EXT:
                continue
            if not accepting:
                self.seen.add(name)
                self.sizes.pop(name, None)
                continue
            try:
                size = e.stat().st_size
            except FileNotFoundError:
                continue
            prev, stable = self.sizes.get(name, (None, 0))
            stable = stable + 1 if size > 0 and size == prev else 0
            if stable >= self.settle_polls:
                self.seen.add(name)
                self.sizes.pop(name, None)
                ready.append(self.folder / name)
            else:
                self.sizes[name] = (size, stable)
        return ready


# ---------------------------------------------------------------- delivering

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    name = _BAD.sub("_", name or "").strip(" .")
    return name[:80] or "sfx"


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{stem}_{n}{ext}"
        n += 1
    return p


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def to_pcm16(src: Path, dst: Path, trim_silence: bool, ff: str) -> None:
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn"]
    if trim_silence:
        cmd += ["-af", TRIM_FILTER]
    cmd += ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), str(dst)]
    r = subprocess.run(cmd, capture_output=True, creationflags=_no_window())
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size <= 44:
        dst.unlink(missing_ok=True)
        tail = r.stderr.decode("utf-8", "replace").strip()[-400:]
        raise IntakeError(f"ffmpeg could not convert {src.name}: {tail or 'empty output'}")


def _unzip_media(src: Path, into: Path) -> list[Path]:
    out = []
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            base = Path(info.filename).name          # flatten: never write outside `into`
            if info.is_dir() or not base or Path(base).suffix.lower() not in MEDIA_EXT:
                continue
            target = unique_path(into, Path(base).stem, Path(base).suffix.lower())
            with z.open(info) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            out.append(target)
    if not out:
        raise IntakeError(f"{src.name} has no audio or video inside")
    return out


def deliver(src: Path, dest_dir: Path, name: str, convert: str = "pcm16",
            trim_silence: bool = True, keep_original_in: Path | None = None) -> dict:
    """Put `src` (a media file or a zip of them) into dest_dir as <name>.wav (or <name>_2.wav …).

    convert="pcm16": 16-bit PCM, 48 kHz wav through ffmpeg (what AK Studio reads); "none": copy as is.
    keep_original_in: move `src` there afterwards (used for files picked out of Downloads);
    otherwise `src` is left where it is.
    Returns {"files": [...], "warnings": [...]}.
    """
    src = Path(src)
    if not src.is_file():
        raise IntakeError(f"not a file: {src}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_name(name)
    warnings: list[str] = []
    ff = ffmpeg_exe() if convert == "pcm16" else None
    if convert == "pcm16" and not ff:
        warnings.append("ffmpeg not found: copied without converting to 16-bit PCM")

    files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="firefly-buddy-") as tmp:
        if src.suffix.lower() in ARCHIVE_EXT:
            sources = _unzip_media(src, Path(tmp))
        elif src.suffix.lower() in MEDIA_EXT:
            sources = [src]
        else:
            raise IntakeError(f"not an audio/video file: {src.name}")
        for s in sources:
            if ff:
                target = unique_path(dest_dir, stem, ".wav")
                to_pcm16(s, target, trim_silence, ff)
            else:
                target = unique_path(dest_dir, stem, s.suffix.lower())
                shutil.copy2(s, target)
            files.append(target)

    out = {"files": [str(f) for f in files], "warnings": warnings}
    if keep_original_in is not None:
        for attempt in range(3):
            if move_aside(src, Path(keep_original_in)):
                break
            time.sleep(0.2)
        else:  # still open in the browser or a virus scanner: the caller tries again later
            out["pending_move"] = {"src": str(src), "keep": str(keep_original_in)}
    return out


def move_aside(src: Path, keep: Path) -> bool:
    """Move a picked-up download into `keep`. False while the file is still in use (Windows)."""
    src = Path(src)
    if not src.exists():
        return True
    keep.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(unique_path(keep, src.stem, src.suffix)))
        return True
    except OSError:
        return False
