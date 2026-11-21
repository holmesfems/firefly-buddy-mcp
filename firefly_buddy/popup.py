"""The desktop window that asks the human to make the sounds in Firefly.

Run as `python -m firefly_buddy.popup` (the server starts it detached). One window at a time:
it touches popup.alive every second, and a second copy exits when it finds a fresh one.

Flow for the human: press 「コピーして受け取り開始」 on an item -> paste into Firefly -> generate ->
download the takes they like. Files that land in Downloads while an item is being received go
to that item (moved out of Downloads, converted, put in its dest_dir).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import flow, intake, store

log = logging.getLogger("firefly-buddy-popup")

STATUS_JA = {store.WAITING: "待ち", store.RECEIVED: "受け取り済み", store.REJECTED: "ダメだった",
             store.SKIPPED: "スキップ", store.CANCELLED: "取り消し"}


class Controller:
    """Everything the window does that is not drawing (kept apart so it can be tested)."""

    def __init__(self, watcher: intake.DownloadWatcher):
        self.watcher = watcher
        self.active: str | None = None
        self.shown: set[str] = set()      # batches shown in this window (kept after they finish)
        self.messages: list[str] = []
        self.pending_moves: list[dict] = []   # originals still open in the browser; moved on later ticks

    # what to show
    def batches(self) -> list[tuple[dict, dict]]:
        out = []
        for bid in store.list_batch_ids():
            req = store.load_request(bid)
            if not req:
                continue
            res = store.load_results(bid)
            if store.pending_count(req, res) or bid in self.shown:
                self.shown.add(bid)
                out.append((req, res))
        return out

    def all_done(self) -> bool:
        return all(store.pending_count(req, res) == 0 for req, res in self.batches())

    # actions
    def activate(self, item_id: str) -> None:
        self.active = item_id
        store.set_active(item_id, time.time())

    def deactivate(self) -> None:
        self.active = None
        store.set_active(None)

    def skip(self, item_id: str) -> None:
        flow.skip_item(item_id)
        if self.active == item_id:
            self.deactivate()

    def reject(self, item_id: str, feedback: str) -> None:
        flow.reject_item(item_id, feedback)
        if self.active == item_id:
            self.deactivate()
        self.messages.append("「どれもダメ」を Claude に返しました" + (f": {feedback.strip()}" if feedback.strip() else ""))

    def feedback(self, item_id: str, text: str) -> bool:
        if not text.strip():
            return False
        flow.add_feedback(item_id, text)
        self.messages.append(f"ひとことを Claude に送りました: {text.strip()}")
        return True

    def receive(self, item_id: str, path: Path, from_downloads: bool) -> bool:
        try:
            r = flow.receive_file(item_id, path, from_downloads)
        except flow.DuplicateFile as e:
            self.messages.append(f"同じ中身のファイルは受け取り済みなので飛ばしました: {Path(path).name}"
                                 f"（{e.item_name} の {Path(e.delivered).name} と同じ）")
            return False
        except (intake.IntakeError, KeyError, OSError) as e:
            self.messages.append(f"受け取れませんでした（{Path(path).name}）: {e}")
            return False
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the files after it
            log.exception("receive failed: %s", path)
            self.messages.append(f"受け取れませんでした（{Path(path).name}）: {type(e).__name__}: {e}")
            return False
        names = ", ".join(Path(f).name for f in r["files"])
        self.messages.append(f"受け取りました: {names}")
        self.messages.extend(f"注意: {w}" for w in r["warnings"])
        if r.get("pending_move"):
            self.pending_moves.append({**r["pending_move"], "until": time.time() + self.MOVE_GIVE_UP})
        elif from_downloads:
            self.watcher.forget(Path(path))
        return True

    MOVE_GIVE_UP = 60.0   # seconds to keep trying to move an original out of Downloads

    def retry_moves(self) -> None:
        left = []
        for m in self.pending_moves:
            if intake.move_aside(Path(m["src"]), Path(m["target"])):
                self.watcher.forget(Path(m["src"]))
                continue
            if time.time() > m["until"]:
                self.messages.append(f"元のファイルを移せなかったので、ダウンロードに残しています: {Path(m['src']).name}")
            else:
                left.append(m)
        self.pending_moves = left

    def tick(self) -> bool:
        """One poll: heartbeat, drop a cancelled active item, take finished downloads."""
        store.touch_heartbeat()
        changed = False
        if self.pending_moves:
            self.retry_moves()
        if self.active:
            try:
                req, _ = store.find_item(self.active)
                st = store.item_state(store.load_results(req["batch_id"]), self.active)
                if not store.is_open(st):
                    self.deactivate()
                    changed = True
            except KeyError:
                self.deactivate()
                changed = True
        for path in self.watcher.poll(accepting=self.active is not None):
            self.receive(self.active, path, from_downloads=True)
            changed = True
        return changed


# ---------------------------------------------------------------- the window

class App:
    POLL_MS = 1000

    def __init__(self, ctl: Controller):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.ctl = ctl
        self.root = tk.Tk()
        self.root.title("Firefly Buddy — 効果音のお願い")
        k = self.root.winfo_fpixels("1i") / 96.0          # screen scaling (1.5 at 150 %)
        self.wrap = int(680 * k)
        self.root.geometry(f"{int(780 * k)}x{int(860 * k)}")
        self.root.minsize(int(560 * k), int(420 * k))
        self.font = ("Yu Gothic UI", 10)
        self.bold = ("Yu Gothic UI", 10, "bold")
        self.mono = ("Consolas", 10)
        self.root.option_add("*Font", self.font)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._sig = None
        self._batches_seen: set[str] = set()
        self.fb_vars: dict = {}     # item id -> StringVar, kept across redraws so typing survives
        self._build_frame()

    def _build_frame(self) -> None:
        tk, ttk = self.tk, self.ttk
        head = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        self.headline = ttk.Label(head, text="", font=("Yu Gothic UI", 12, "bold"))
        self.headline.pack(anchor="w")
        steps = ("① 「コピーして受け取り開始」を押す → ② Firefly に貼って生成 → "
                 "③ 気に入った案をダウンロード（自動で受け取ります）")
        ttk.Label(head, text=steps, wraplength=self.wrap, foreground="#555").pack(anchor="w", pady=(2, 6))
        row = ttk.Frame(head)
        row.pack(fill="x")
        ttk.Button(row, text="Firefly（効果音）を開く", command=self.open_firefly).pack(side="left")
        ttk.Label(row, text=f"見張るフォルダ: {self.ctl.watcher.folder}", foreground="#777").pack(
            side="left", padx=10)

        self.status = ttk.Label(self.root, text="", padding=(12, 4), foreground="#0a5")
        self.status.pack(fill="x")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.log = tk.Text(self.root, height=3, wrap="word", font=self.font, relief="flat",
                           background="#f4f4f4", state="disabled")
        self.log.pack(fill="x", padx=12, pady=(4, 10))

    # actions from buttons
    def open_firefly(self) -> None:
        webbrowser.open(store.FIREFLY_SFX_URL)

    def copy_and_start(self, item: dict) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(item["prompt"])
        self.ctl.activate(item["id"])
        self.ctl.messages.append(f"プロンプトをコピーしました: {item['name']}（Firefly に貼り付けてください）")
        self.refresh(force=True)

    def pick_file(self, item: dict) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=f"{item['name']} のファイルを選ぶ", initialdir=str(self.ctl.watcher.folder),
            filetypes=[("音声・動画", " ".join(f"*{e}" for e in sorted(intake.PICK_EXT))), ("すべて", "*.*")])
        if path:
            self.ctl.receive(item["id"], Path(path), from_downloads=False)
            self.refresh(force=True)

    def reject(self, item: dict) -> None:
        var = self.fb_vars.get(item["id"])
        self.ctl.reject(item["id"], var.get() if var else "")
        if var:
            var.set("")
        self.refresh(force=True)

    def send_feedback(self, item: dict) -> None:
        var = self.fb_vars.get(item["id"])
        if var and self.ctl.feedback(item["id"], var.get()):
            var.set("")
            self.refresh(force=True)

    def stop_receiving(self) -> None:
        self.ctl.deactivate()
        self.refresh(force=True)

    def close(self) -> None:
        _STOP.set()
        store.set_active(None)
        store.clear_heartbeat()
        self.root.destroy()

    # drawing
    def refresh(self, force: bool = False) -> None:
        sig = (store.signature(), self.ctl.active)
        if force or sig != self._sig:
            self._sig = sig
            self._draw()
        self._draw_messages()

    def _draw(self) -> None:
        tk, ttk = self.tk, self.ttk
        for w in self.inner.winfo_children():
            w.destroy()
        batches = self.ctl.batches()
        new = {req["batch_id"] for req, _ in batches} - self._batches_seen
        self._batches_seen |= new
        if new:
            self.alert()
        pending = sum(store.pending_count(req, res) for req, res in batches)
        self.headline.configure(
            text=f"Claude から効果音のお願い：残り {pending} 件" if pending
            else "全部そろいました。この窓は閉じて大丈夫です")
        if self.ctl.active:
            try:
                _, it = store.find_item(self.ctl.active)
                self.status.configure(text=f"● 受け取り中: {it['name']}（ダウンロードを待っています）")
            except KeyError:
                self.status.configure(text="")
        else:
            self.status.configure(text="受け取りは止まっています（項目のボタンを押すと始まります）")

        for req, res in batches:
            box = ttk.LabelFrame(self.inner, text=f" {req.get('title') or req['batch_id']} ", padding=8)
            box.pack(fill="x", padx=4, pady=6)
            for item in req["items"]:
                self._draw_item(box, item, store.item_state(res, item["id"]))

    def _draw_item(self, parent, item: dict, st: dict) -> None:
        tk, ttk = self.tk, self.ttk
        active = self.ctl.active == item["id"]
        frame = tk.Frame(parent, background="#e8f6ee" if active else "#ffffff",
                         highlightthickness=1, highlightbackground="#3a7" if active else "#ddd")
        frame.pack(fill="x", pady=4)
        bg = frame["background"]
        top = tk.Frame(frame, background=bg)
        top.pack(fill="x", padx=8, pady=(6, 0))
        want = item.get("want", 1)
        got = len(st["files"])
        badge = STATUS_JA.get(st["status"], st["status"])
        if st["status"] == store.RECEIVED:
            badge += f" {got}/{want}"
        if active:
            badge = f"受け取り中 {got}/{want}" if got else "受け取り中"
        tk.Label(top, text=f"[{badge}]", background=bg, font=self.bold,
                 foreground="#0a5" if active or st["status"] == store.RECEIVED else "#555").pack(side="left")
        tk.Label(top, text=item["name"], background=bg, font=self.bold).pack(side="left", padx=6)
        if want > 1:
            tk.Label(top, text=f"（気に入った案を {want} つまで）", background=bg, foreground="#555").pack(side="left")
        if item.get("note"):
            tk.Label(frame, text=item["note"], background=bg, wraplength=self.wrap - 40, justify="left").pack(
                anchor="w", padx=8)
        tk.Label(frame, text=item["prompt"], background=bg, foreground="#246", font=self.mono,
                 wraplength=self.wrap - 40, justify="left").pack(anchor="w", padx=8, pady=(2, 4))
        if item.get("duration"):
            tk.Label(frame, text=f"Duration を {item['duration']:g} 秒に（Firefly の既定は 8 秒）",
                     background=bg, font=self.bold, foreground="#a50").pack(anchor="w", padx=8)
        else:
            tk.Label(frame, text="Duration の指定なし（Firefly の既定は 8 秒。短い音なら縮めてください）",
                     background=bg, foreground="#a50").pack(anchor="w", padx=8)
        if item.get("dest_dir"):
            tk.Label(frame, text=f"入れる先: {item['dest_dir']}", background=bg, foreground="#888").pack(
                anchor="w", padx=8)
        for f in st["files"]:
            tk.Label(frame, text=f"✓ {Path(f).name}", background=bg, foreground="#0a5").pack(anchor="w", padx=8)
        if st.get("error"):
            tk.Label(frame, text=f"× {st['error']}", background=bg, foreground="#c33",
                     wraplength=self.wrap - 40, justify="left").pack(anchor="w", padx=8)
        for fb in st.get("feedback", []):
            tk.Label(frame, text=f"💬 {fb['text']}", background=bg, foreground="#555",
                     wraplength=self.wrap - 40, justify="left").pack(anchor="w", padx=8)
        if st["status"] != store.CANCELLED:
            var = self.fb_vars.setdefault(item["id"], tk.StringVar())
            row = tk.Frame(frame, background=bg)
            row.pack(fill="x", padx=8, pady=(4, 0))
            tk.Label(row, text="Claude へのひとこと:", background=bg).pack(side="left")
            entry = ttk.Entry(row, textvariable=var)
            entry.pack(side="left", fill="x", expand=True, padx=6)
            entry.bind("<Return>", lambda e, it=item: self.send_feedback(it))
            ttk.Button(row, text="送る", command=lambda it=item: self.send_feedback(it)).pack(side="left")
        if store.is_open(st):
            btns = tk.Frame(frame, background=bg)
            btns.pack(fill="x", padx=8, pady=(2, 8))
            if active:
                ttk.Button(btns, text="受け取りを止める", command=self.stop_receiving).pack(side="left")
            else:
                label = "コピーして受け取り開始" if st["status"] == store.WAITING else "もう一度（追加で受け取る）"
                ttk.Button(btns, text=label, command=lambda it=item: self.copy_and_start(it)).pack(side="left")
            ttk.Button(btns, text="ファイルを選ぶ…", command=lambda it=item: self.pick_file(it)).pack(
                side="left", padx=6)
            ttk.Button(btns, text="どれもダメ（ひとことを付けて返す）",
                       command=lambda it=item: self.reject(it)).pack(side="left")
        else:
            tk.Frame(frame, height=6, background=bg).pack()

    def _draw_messages(self) -> None:
        if not self.ctl.messages:
            return
        self.log.configure(state="normal")
        for m in self.ctl.messages:
            self.log.insert("end", time.strftime("%H:%M:%S ") + m + "\n")
        self.ctl.messages.clear()
        self.log.see("end")
        self.log.configure(state="disabled")

    def alert(self) -> None:
        """Bring the window forward for a new request (topmost for a moment, flash, beep)."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(3000, lambda: self.root.attributes("-topmost", False))
            self.root.bell()
            if sys.platform == "win32":
                import ctypes
                hwnd = int(self.root.wm_frame(), 16)
                ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:  # noqa: BLE001 - attention is best effort
            pass

    def loop(self) -> None:
        try:
            self.ctl.tick()
        except Exception as e:  # noqa: BLE001 - keep the window alive
            log.exception("tick failed")
            self.ctl.messages.append(f"エラー: {e}")
        self.refresh()
        self.root.after(self.POLL_MS, self.loop)

    def run(self) -> None:
        self.refresh(force=True)
        self.root.after(self.POLL_MS, self.loop)
        self.root.mainloop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if store.popup_alive() and "--force" not in sys.argv:
        log.info("another popup is open; exiting")
        return
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    store.touch_heartbeat()
    store.set_active(None)
    beat = threading.Thread(target=_heartbeat, daemon=True)
    beat.start()
    ctl = Controller(intake.DownloadWatcher(intake.downloads_dir()))
    try:
        App(ctl).run()
    finally:
        _STOP.set()
        store.clear_heartbeat()


_STOP = threading.Event()


def _heartbeat() -> None:
    """Touch popup.alive from a thread: a modal dialog (ファイルを選ぶ…) blocks the Tk loop, and the
    server must not take the window for closed and open a second one meanwhile."""
    while not _STOP.wait(1.0):
        try:
            store.touch_heartbeat()
        except OSError:
            pass


if __name__ == "__main__":
    main()
