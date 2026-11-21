"""Tests that need no window and no Firefly: requests, the Downloads watcher, delivery, the tools."""
import asyncio
import math
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
from firefly_buddy import flow, intake, popup, store  # noqa: E402

HAS_FFMPEG = intake.ffmpeg_exe() is not None


def write_wav(path: Path, seconds=0.5, rate=44100, width=3, lead_silence=0.0, channels=1):
    """A sine tone (24-bit by default, like a typical download), optionally after silence."""
    n_sil, n = int(rate * lead_silence), int(rate * seconds)
    frames = bytearray()
    for i in range(n_sil + n):
        v = 0 if i < n_sil else int(0.5 * (2 ** (8 * width - 1) - 1) * math.sin(2 * math.pi * 440 * i / rate))
        frames += v.to_bytes(width, "little", signed=True) * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def wav_info(path: Path):
    with wave.open(str(path)) as w:
        return w.getsampwidth() * 8, w.getframerate(), w.getnframes() / w.getframerate()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        patcher = mock.patch.dict(os.environ, {"FIREFLY_BUDDY_HOME": str(self.root / "home"),
                                               "FIREFLY_BUDDY_DOWNLOADS": str(self.downloads)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        launch = mock.patch.object(server, "launch_popup", return_value="opened")
        self.launch = launch.start()
        self.addCleanup(launch.stop)

    def item(self, name="boom", prompt="short deep cinematic impact, dry", **kw):
        return server.SfxItem(name=name, prompt=prompt, **kw)

    def request(self, *items, title="テスト"):
        return asyncio.run(server.firefly_request_sfx(title, list(items) or [self.item()]))


class RequestTests(Base):
    def test_request_saves_and_opens_popup(self):
        dest = self.root / "fx" / "se" / "chapter"
        r = self.request(self.item(dest_dir=str(dest)), self.item(name="wipe"))
        self.assertEqual(r["popup"], "opened")
        self.assertEqual([i["id"] for i in r["items"]], [f"{r['batch_id']}-1", f"{r['batch_id']}-2"])
        self.assertEqual(r["items"][0]["dest_dir"], str(dest))
        self.assertTrue(r["items"][1]["dest_dir"].endswith(r["batch_id"]))
        st = asyncio.run(server.firefly_status(r["batch_id"]))
        self.assertEqual([i["status"] for i in st["batches"][0]["items"]], ["waiting", "waiting"])
        self.assertEqual(st["batches"][0]["pending"], 2)

    def test_rejects_bad_items(self):
        cases = [
            [],
            [self.item()] * 13,
            [self.item(prompt="重い衝撃音")],
            [self.item(prompt="x" * 751)],
            [self.item(prompt="   ")],
            [self.item(dest_dir="relative/dir")],
        ]
        for items in cases:
            with self.subTest(n=len(items)), self.assertRaises(ValueError):
                server.check_items(items)

    def test_duration_is_kept_and_checked(self):
        r = self.request(self.item(duration=0.8))
        req = store.load_request(r["batch_id"])
        self.assertEqual(req["items"][0]["duration"], 0.8)
        for bad in (0, -1, 61):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.item(duration=bad)

    def test_status_lists_pending_and_recent(self):
        a = self.request()["batch_id"]
        b = self.request()["batch_id"]
        flow.cancel_items(a)
        st = asyncio.run(server.firefly_status())
        self.assertEqual({x["batch_id"] for x in st["batches"]}, {a, b})

    def test_status_wait_returns_when_something_changes(self):
        r = self.request()
        iid = r["items"][0]["id"]
        threading.Timer(0.5, flow.skip_item, args=(iid,)).start()
        t0 = time.monotonic()
        st = asyncio.run(server.firefly_status(r["batch_id"], wait_seconds=20))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(st["batches"][0]["items"][0]["status"], "skipped")

    def test_cancel_with_empty_list_cancels_nothing(self):
        r = self.request(self.item(), self.item(name="b"))
        self.assertEqual(flow.cancel_items(r["batch_id"], []), [])
        st = asyncio.run(server.firefly_status(r["batch_id"]))["batches"][0]
        self.assertEqual(st["pending"], 2)

    def test_cancel_only_touches_waiting_items(self):
        r = self.request(self.item(), self.item(name="b"))
        flow.skip_item(r["items"][0]["id"])
        out = asyncio.run(server.firefly_cancel(r["batch_id"]))
        self.assertEqual(out["cancelled"], [r["items"][1]["id"]])
        with self.assertRaises(KeyError):
            flow.cancel_items(r["batch_id"], ["nope-9"])

    def test_parallel_result_updates_all_land(self):
        r = self.request(*[self.item(name=f"s{i}") for i in range(12)])
        ids = [i["id"] for i in r["items"]]
        threads = [threading.Thread(target=flow.skip_item, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        res = store.load_results(r["batch_id"])
        self.assertTrue(all(res["items"][i]["status"] == "skipped" for i in ids))


class WatcherTests(Base):
    def test_only_new_settled_media_files(self):
        (self.downloads / "old.wav").write_bytes(b"x" * 10)
        w = intake.DownloadWatcher(self.downloads)
        (self.downloads / "new.wav").write_bytes(b"x" * 10)
        (self.downloads / "notes.txt").write_text("x")
        (self.downloads / "half.wav.crdownload").write_bytes(b"x")
        self.assertEqual(w.poll(True), [])                      # first sight: size not settled yet
        self.assertEqual(w.poll(True), [])                      # same size once: still settling
        self.assertEqual([p.name for p in w.poll(True)], ["new.wav"])
        self.assertEqual(w.poll(True), [])                      # reported once

    def test_growing_file_waits(self):
        w = intake.DownloadWatcher(self.downloads)
        f = self.downloads / "take.wav"
        f.write_bytes(b"x" * 10)
        w.poll(True)
        f.write_bytes(b"x" * 20)
        self.assertEqual(w.poll(True), [])
        self.assertEqual(w.poll(True), [])
        self.assertEqual(len(w.poll(True)), 1)

    def test_files_arriving_while_idle_are_left_alone(self):
        w = intake.DownloadWatcher(self.downloads)
        (self.downloads / "unrelated.wav").write_bytes(b"x" * 10)
        self.assertEqual(w.poll(False), [])
        self.assertEqual(w.poll(True), [])
        self.assertEqual(w.poll(True), [])


    def poll_until_ready(self, w, n=3):
        out = []
        for _ in range(n):
            out += w.poll(True)
        return out

    def test_only_wav_by_default(self):
        w = intake.DownloadWatcher(self.downloads)
        for name in ("clip.mp4", "song.mp3", "pack.zip", "take.WAV"):
            (self.downloads / name).write_bytes(b"x" * 10)
        self.assertEqual([p.name for p in self.poll_until_ready(w)], ["take.WAV"])

    def test_watch_ext_can_be_widened(self):
        with mock.patch.dict(os.environ, {"FIREFLY_BUDDY_WATCH_EXT": "wav, .ZIP, exe"}):
            self.assertEqual(intake.watch_ext(), {".wav", ".zip"})     # unknown types are dropped
            w = intake.DownloadWatcher(self.downloads)
        (self.downloads / "pack.zip").write_bytes(b"x" * 10)
        self.assertEqual([p.name for p in self.poll_until_ready(w)], ["pack.zip"])
        with mock.patch.dict(os.environ, {"FIREFLY_BUDDY_WATCH_EXT": "exe"}):
            self.assertEqual(intake.watch_ext(), {".wav"})

    def test_a_name_that_left_the_folder_can_come_back(self):
        w = intake.DownloadWatcher(self.downloads)
        f = self.downloads / "take.wav"
        f.write_bytes(b"x" * 10)
        self.assertEqual(len(self.poll_until_ready(w)), 1)
        f.unlink()                                               # moved aside after receiving
        w.poll(True)
        f.write_bytes(b"y" * 12)                                 # same take downloaded again
        self.assertEqual([p.name for p in self.poll_until_ready(w)], ["take.wav"])


class DeliverTests(Base):
    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not found")
    def test_pcm16_48k_and_trims_leading_silence(self):
        src = write_wav(self.root / "in.wav", seconds=0.5, lead_silence=0.4)
        r = intake.deliver(src, self.root / "out", "chapter_boom")
        out = Path(r["files"][0])
        self.assertEqual(out.name, "chapter_boom.wav")
        bits, rate, dur = wav_info(out)
        self.assertEqual((bits, rate), (16, 48000))
        self.assertLess(dur, 0.7)
        self.assertTrue(src.exists())                           # not moved unless asked

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not found")
    def test_zip_is_flattened_and_numbered(self):
        z = self.root / "takes.zip"
        a = write_wav(self.root / "a.wav")
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(a, "../../evil/take1.wav")
            zf.write(a, "dir/take2.wav")
            zf.writestr("readme.txt", "x")
        r = intake.deliver(z, self.root / "out", "hit")
        self.assertEqual(sorted(Path(f).name for f in r["files"]), ["hit.wav", "hit_2.wav"])
        self.assertFalse((self.root / "evil").exists())

    def test_convert_none_copies_and_keeps_original_aside(self):
        src = self.downloads / "Firefly take.wav"
        write_wav(src)
        (self.root / "out").mkdir()
        (self.root / "out" / "boom.wav").write_bytes(b"old")
        r = intake.deliver(src, self.root / "out", "boom", convert="none",
                           keep_original_in=self.root / "orig")
        self.assertEqual(Path(r["files"][0]).name, "boom_2.wav")
        self.assertFalse(src.exists())
        self.assertTrue((self.root / "orig" / "Firefly take.wav").exists())

    def test_broken_zip_is_an_intake_error(self):
        z = self.root / "broken.zip"
        z.write_bytes(b"PK not really a zip")
        with self.assertRaises(intake.IntakeError):
            intake.deliver(z, self.root / "out", "x")

    def test_move_aside_copies_once_while_the_original_is_locked(self):
        src = write_wav(self.downloads / "locked.wav")
        target = self.root / "orig" / "locked.wav"
        real_remove = os.remove
        calls = {"n": 0}

        def flaky_remove(p):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(32, "in use")
            real_remove(p)
        with mock.patch.object(intake.os, "replace", side_effect=OSError("other drive")),                 mock.patch.object(intake.os, "remove", side_effect=flaky_remove):
            for _ in range(3):
                self.assertFalse(intake.move_aside(src, target))
            self.assertTrue(intake.move_aside(src, target))
        self.assertFalse(src.exists())
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["locked.wav"])

    def test_firefly_seconds_follow_the_slider(self):
        cases = {0.3: 1.0, 0.8: 1.0, 1.0: 1.0, 1.26: 1.3, 2.04: 2.0, 30: 30.0, 45: 30.0}
        for asked, slider in cases.items():
            with self.subTest(asked=asked):
                self.assertEqual(intake.firefly_seconds(asked), slider)

    def test_length_warning(self):
        self.assertIsNone(intake.length_warning(1.0, 0.8))          # shorter than 1 s is made as 1 s
        self.assertIsNone(intake.length_warning(1.2, 1.0))          # within 0.3 s
        self.assertIsNone(intake.length_warning(None, 1.0))
        self.assertIsNone(intake.length_warning(8.0, None))
        self.assertIn("8 秒のまま", intake.length_warning(8.0, 1.0))
        w = intake.length_warning(1.5, 1.0)
        self.assertIn("1.5 秒", w)
        self.assertNotIn("8 秒のまま", w)

    def test_media_seconds(self):
        self.assertAlmostEqual(intake.media_seconds(write_wav(self.root / "a.wav", seconds=0.5)), 0.5, places=2)
        junk = self.root / "junk.wav"
        junk.write_bytes(b"nope")
        self.assertIsNone(intake.media_seconds(junk))

    def test_rejects_non_media(self):
        f = self.root / "x.txt"
        f.write_text("x")
        with self.assertRaises(intake.IntakeError):
            intake.deliver(f, self.root / "out", "x")

    def test_safe_name(self):
        self.assertEqual(intake.safe_name('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(intake.safe_name("  "), "sfx")


class ControllerTests(Base):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "fx" / "se" / "slam"
        self.r = self.request(self.item(name="slam_hit", dest_dir=str(self.dest), convert="none"),
                              self.item(name="other", convert="none"))
        self.ctl = popup.Controller(intake.DownloadWatcher(self.downloads))

    def settle(self):
        self.ctl.tick()
        self.ctl.tick()
        return self.ctl.tick()

    def test_download_goes_to_the_active_item(self):
        iid = self.r["items"][0]["id"]
        self.ctl.activate(iid)
        self.ctl.tick()                                         # heartbeat: the server sees the window
        self.assertEqual(store.get_active()["item"], iid)
        write_wav(self.downloads / "firefly-sfx.wav")
        self.assertTrue(self.settle())
        res = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual(res["status"], "received")
        self.assertEqual([Path(f).name for f in res["files"]], ["slam_hit.wav"])
        self.assertTrue((self.dest / "slam_hit.wav").exists())
        self.assertFalse((self.downloads / "firefly-sfx.wav").exists())
        self.assertTrue((store.originals_dir() / self.r["batch_id"] / "firefly-sfx.wav").exists())

    def test_original_still_open_is_moved_later(self):
        iid = self.r["items"][0]["id"]
        self.ctl.activate(iid)
        f = self.downloads / "busy.wav"
        write_wav(f)
        with mock.patch.object(intake, "move_aside", return_value=False),                 mock.patch.object(intake.time, "sleep"):
            self.settle()
        self.assertTrue(f.exists())                              # browser still had it open
        self.assertEqual(len(self.ctl.pending_moves), 1)
        res = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual(res["status"], "received")
        self.assertEqual(res.get("warnings"), [])
        self.ctl.tick()                                          # released: moved on the next tick
        self.assertFalse(f.exists())
        self.assertEqual(self.ctl.pending_moves, [])
        self.assertTrue((store.originals_dir() / self.r["batch_id"] / "busy.wav").exists())

    def test_gives_up_moving_after_a_while(self):
        f = self.downloads / "stuck.wav"
        write_wav(f)
        self.ctl.pending_moves.append({"src": str(f), "target": str(self.root / "keep" / "stuck.wav"), "until": 0})
        with mock.patch.object(intake, "move_aside", return_value=False):
            self.ctl.tick()
        self.assertEqual(self.ctl.pending_moves, [])
        self.assertTrue(any("残しています" in m for m in self.ctl.messages))

    def test_nothing_is_taken_while_idle(self):
        write_wav(self.downloads / "unrelated.wav")
        self.settle()
        self.ctl.activate(self.r["items"][0]["id"])
        self.settle()
        self.assertTrue((self.downloads / "unrelated.wav").exists())
        self.assertFalse(self.dest.exists())

    def test_cancelled_active_item_stops_receiving(self):
        iid = self.r["items"][0]["id"]
        self.ctl.activate(iid)
        flow.cancel_items(self.r["batch_id"], [iid])
        self.ctl.tick()
        self.assertIsNone(self.ctl.active)

    def test_batches_stay_listed_after_they_finish(self):
        self.assertEqual(len(self.ctl.batches()), 1)
        flow.cancel_items(self.r["batch_id"])
        self.assertEqual(len(self.ctl.batches()), 1)
        self.assertTrue(self.ctl.all_done())

    def test_attach_copies_a_file_by_hand(self):
        src = write_wav(self.root / "elsewhere.wav")
        iid = self.r["items"][1]["id"]
        out = asyncio.run(server.firefly_attach(iid, str(src)))
        self.assertTrue(Path(out["files"][0]).exists())
        self.assertTrue(src.exists())
        self.assertEqual(store.load_results(self.r["batch_id"])["items"][iid]["status"], "received")

    def test_reject_and_feedback_reach_claude(self):
        a, b = (i["id"] for i in self.r["items"])
        self.ctl.activate(a)
        self.ctl.reject(a, "  全部ゴゴゴゴって地鳴りだった ")
        self.assertIsNone(self.ctl.active)
        self.assertFalse(self.ctl.feedback(b, "   "))
        flow.receive_file(b, write_wav(self.root / "t.wav"), from_downloads=False)
        self.assertTrue(self.ctl.feedback(b, "2 つ目が良い"))
        st = asyncio.run(server.firefly_status(self.r["batch_id"]))["batches"][0]
        rows = {x["id"]: x for x in st["items"]}
        self.assertEqual(rows[a]["status"], "rejected")
        self.assertEqual(rows[a]["feedback"], ["全部ゴゴゴゴって地鳴りだった"])
        self.assertEqual(rows[b]["status"], "received")
        self.assertEqual(rows[b]["feedback"], ["2 つ目が良い"])
        self.assertEqual(st["pending"], 0)
        flow.reject_item(b, "やっぱり全部ダメ")                 # received can still be rejected
        self.assertEqual(store.load_results(self.r["batch_id"])["items"][b]["status"], "rejected")

    def test_same_content_is_not_received_twice(self):
        a, b = (i["id"] for i in self.r["items"])
        src = write_wav(self.root / "take.wav")
        first = flow.receive_file(a, src, from_downloads=False)
        cases = [
            ("same file again", a, src),
            ("same content, other item", b, src),
            ("the delivered file itself", a, Path(first["files"][0])),
        ]
        for label, iid, path in cases:
            with self.subTest(label), self.assertRaises(flow.DuplicateFile):
                flow.receive_file(iid, path, from_downloads=False)
        with self.assertRaises(Exception) as cm:                 # the tool says why
            asyncio.run(server.firefly_attach(a, str(src)))
        self.assertIn("same content", str(cm.exception))
        other = write_wav(self.root / "other.wav", seconds=0.3)  # different content is fine
        flow.receive_file(a, other, from_downloads=False)
        st = store.load_results(self.r["batch_id"])["items"]
        self.assertEqual(len(st[a]["files"]), 2)
        self.assertNotIn("error", st[a])
        self.assertEqual(st[b]["status"], "waiting")

    def test_browser_copy_of_a_download_is_skipped_and_left_alone(self):
        iid = self.r["items"][0]["id"]
        self.ctl.activate(iid)
        write_wav(self.downloads / "take.wav")
        self.settle()
        write_wav(self.downloads / "take (1).wav")              # downloaded the same take again
        self.settle()
        st = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual(len(st["files"]), 1)
        self.assertTrue((self.downloads / "take (1).wav").exists())
        self.assertTrue(any("飛ばしました" in m for m in self.ctl.messages))

    def test_same_name_downloaded_again_is_received(self):
        iid = self.r["items"][0]["id"]
        self.ctl.activate(iid)
        write_wav(self.downloads / "Firefly_audio_variation1.wav")
        self.settle()
        write_wav(self.downloads / "Firefly_audio_variation1.wav", seconds=0.3)   # regenerated take
        self.settle()
        st = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual([Path(f).name for f in st["files"]], ["slam_hit.wav", "slam_hit_2.wav"])

    def test_broken_zip_by_hand_reports_why(self):
        z = self.root / "broken.zip"
        z.write_bytes(b"not a zip")
        iid = self.r["items"][0]["id"]
        with self.assertRaises(Exception) as cm:
            asyncio.run(server.firefly_attach(iid, str(z)))
        self.assertIn("zip", str(cm.exception))
        self.assertIn("zip", store.load_results(self.r["batch_id"])["items"][iid]["error"])

    def test_take_of_the_wrong_length_is_received_with_a_note(self):
        r = self.request(self.item(name="hit", duration=1, convert="none"))
        iid = r["items"][0]["id"]
        out = flow.receive_file(iid, write_wav(self.root / "long.wav", seconds=2.0), from_downloads=False)
        self.assertTrue(any("2.0 秒" in w for w in out["warnings"]))
        flow.receive_file(iid, write_wav(self.root / "ok.wav", seconds=1.0), from_downloads=False)
        row = asyncio.run(server.firefly_status(r["batch_id"]))["batches"][0]["items"][0]
        self.assertEqual(row["status"], "received")
        self.assertEqual(row["seconds"], {"hit.wav": 2.0, "hit_2.wav": 1.0})
        self.assertEqual(len(row["warnings"]), 1)
        flow.unreceive(iid, row["files"][0])                     # taking it back drops its note
        row = asyncio.run(server.firefly_status(r["batch_id"]))["batches"][0]["items"][0]
        self.assertNotIn("warnings", row)

    def test_unreceive_deletes_the_copy_and_allows_the_file_again(self):
        iid = self.r["items"][0]["id"]
        src = write_wav(self.root / "take.wav")
        got = flow.receive_file(iid, src, from_downloads=False)["files"][0]
        out = flow.unreceive(iid, got)
        self.assertEqual(out["status"], "waiting")
        self.assertFalse(Path(got).exists())
        self.assertTrue(src.exists())                            # the original is kept
        st = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual((st["files"], st["received"]), ([], []))
        again = flow.receive_file(iid, src, from_downloads=False)  # no longer a duplicate
        self.assertEqual(len(again["files"]), 1)
        with self.assertRaises(KeyError):
            flow.unreceive(iid, str(self.root / "never.wav"))

    def test_unreceive_one_of_two_keeps_the_item_received(self):
        iid = self.r["items"][0]["id"]
        a = flow.receive_file(iid, write_wav(self.root / "a.wav"), from_downloads=False)["files"][0]
        flow.receive_file(iid, write_wav(self.root / "b.wav", seconds=0.3), from_downloads=False)
        self.assertTrue(self.ctl.unreceive(iid, a))
        st = store.load_results(self.r["batch_id"])["items"][iid]
        self.assertEqual(st["status"], "received")
        self.assertEqual(len(st["files"]), 1)
        self.assertFalse(self.ctl.unreceive(iid, a))             # already gone: reported, no crash

    def test_cancelled_item_refuses_files(self):
        iid = self.r["items"][1]["id"]
        flow.cancel_items(self.r["batch_id"], [iid])
        with self.assertRaises(intake.IntakeError):
            flow.receive_file(iid, write_wav(self.root / "x.wav"), from_downloads=False)


class HeartbeatTests(Base):
    def test_alive_only_while_fresh(self):
        self.assertFalse(store.popup_alive())
        store.touch_heartbeat()
        self.assertTrue(store.popup_alive())
        old = time.time() - 60
        os.utime(store.heartbeat_path(), (old, old))
        self.assertFalse(store.popup_alive())
        self.assertIsNone(store.get_active()["item"])


if __name__ == "__main__":
    unittest.main()
