"""firefly-buddy-mcp: a stdio MCP server that asks the human to make sound effects in Adobe Firefly.

Claude cannot use the Firefly web app's generative credits directly (there is no API for them,
and Adobe's guidelines forbid scripted use). So Claude writes the prompts, and this server puts a
window on the desktop: the human copies each prompt, generates in Firefly, and downloads the takes
they like. Files that land in Downloads while an item is being received are converted and put
where Claude asked. Claude polls with firefly_status.
"""
from __future__ import annotations

import functools
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import anyio
from pydantic import BaseModel, Field

from firefly_buddy import flow, intake, store

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger("firefly-buddy-mcp")

MAX_ITEMS = 12
PROMPT_CHARS = 750         # Firefly's limit for sound-effect prompts
MAX_WAIT = 110             # Claude Code moves tool calls longer than ~120 s to the background
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

INSTRUCTIONS = """\
Adobe Firefly（Web）のクレジットで効果音を作るための、人間にお願いするツール。
Claude は Firefly を直接操作しない（API が無く、規約で自動操作は禁止）。firefly_request_sfx で頼むと
ユーザーのデスクトップに窓が出て、ユーザーがプロンプトをコピー → Firefly で生成 → 気に入った案をダウンロードする。
ダウンロードフォルダに届いたファイルは自動で受け取られ、dest_dir に入る
（convert=pcm16 なら 16bit PCM・48kHz の wav に変え、頭の無音を切る）。

- 頼むのはまとめて 1 回に（1 回 12 件まで）。1 件ずつ何度も頼まない。
- prompt は英語だけ（Firefly の効果音は英語のプロンプトしか受け付けない。750 字まで）。
  音源・材質・動き・長さ・余韻・空間を具体的に書く。
  例: "A single sharp metallic clang, like a hammer striking a steel anvil once, stops instantly, about 0.8 seconds"。
  ジャンル名だけ（"sci-fi sound"）は避ける。作品名・曲名・実在の人物は入れない。
  "cinematic impact"・"sub boom"・"decaying tail" は長い地鳴り（ゴゴゴゴ）になりやすい（2026-11-21 の試しで 4 案とも地鳴り）。
  一発の音は "single hit"・"stops instantly"・身近な物の例え（ハンマーと金床など）で書く。
  否定（"no rumble"）は効きにくく、かえってその音が入ることがあるので、欲しい音を肯定で書く。
  Firefly の画面の助言は「短く具体的に、音の 1 つの要素に絞る」。
- duration（秒）は必ず付ける。Firefly の Duration は既定が 8 秒で、そのままだと一発の音も 8 秒に引き伸ばされて地鳴りになる。
  窓に「Duration を ◯ 秒に」と出るので、人間がスライダーを合わせる。prompt にも "about 0.8 seconds" のように書く。
- Firefly には参考のファイルを渡す欄は無い（Audio guide はマイクで録音して、タイミングと音量の目安にするもの）。
  リズムや形が大事な音なら、note に「声でこうまねて録音すると良い」と書いてもよい。
- note は日本語で、何に使うか・どんな音なら当たりかを人間向けに一言（例:「章タイトルのドン。重く短く」）。
- 頼んだら待ち続けずに他の作業を続け、あとで firefly_status で受け取りを確かめる（wait_seconds は 110 秒まで）。
- status が rejected（どれもダメ）なら、feedback（人間のひとこと）を読んでプロンプトを直し、頼み直す。
  received の項目にも feedback（「2 つ目が良い」など）が付くことがあるので、必ず読む。
- 自分では音を聞けない。聞き比べは gemini-buddy の gemini_analyze_media か、ユーザーに頼む。
- AK Studio の効果音に使うときは dest_dir を <回のフォルダ>/fx/se/<種類>（chapter・slam・wipe・blip など）にすると、
  「効果音の設定」の候補に自動で並ぶ。鳴らす音を変える（se_set）のは、ユーザーが聞いて選んでから。
- ダウンロードを拾えなかったときは firefly_attach でファイルを渡す。いらなくなった依頼は firefly_cancel。
"""


class SfxItem(BaseModel):
    name: str = Field(description="Base name of the result file, no extension (e.g. 'chapter_boom'). "
                                  "Repeats get _2, _3 …")
    prompt: str = Field(description="English prompt to paste into Firefly Generate Sound Effects (<= 750 chars).")
    note: str = Field("", description="Short Japanese note for the human: what it is for, what a good take sounds like.")
    dest_dir: str | None = Field(None, description="Absolute folder for the result (created if missing). "
                                                   "Default: ~/.firefly-buddy-mcp/outbox/<batch_id>/")
    want: int = Field(1, ge=1, le=4, description="How many of Firefly's 4 takes the human should keep.")
    convert: Literal["pcm16", "none"] = Field(
        "pcm16", description="pcm16: 16-bit PCM 48 kHz wav (AK Studio reads only this). none: keep Firefly's file.")
    trim_silence: bool = Field(True, description="Cut silence at the start (pcm16 only).")
    duration: float | None = Field(None, gt=0, le=60, description="Length in seconds. Always give it: Firefly's "
                                   "Duration slider defaults to 8 s. Shown on the window; say it in the prompt too.")


def check_items(items: list[SfxItem]) -> None:
    if not items:
        raise ValueError("items is empty")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per request (got {len(items)}); split it")
    for i, it in enumerate(items, 1):
        p = it.prompt.strip()
        if not p:
            raise ValueError(f"item {i} ({it.name}): prompt is empty")
        if len(p) > PROMPT_CHARS:
            raise ValueError(f"item {i} ({it.name}): prompt is {len(p)} chars; Firefly takes {PROMPT_CHARS}")
        if CJK.search(p):
            raise ValueError(f"item {i} ({it.name}): Firefly sound effects take English prompts only "
                             "(put Japanese in note)")
        if it.dest_dir and not Path(it.dest_dir).is_absolute():
            raise ValueError(f"item {i} ({it.name}): dest_dir must be an absolute path")


def popup_command() -> list[str]:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), "-m", "firefly_buddy.popup"]


def launch_popup() -> str:
    """Start the window detached from this server's stdio. Returns 'opened' or 'already_open'."""
    if store.popup_alive():
        return "already_open"
    store.home().mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(store.home() / "popup.log", "ab") as err:
        subprocess.Popen(popup_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=err, cwd=str(Path(__file__).resolve().parent),
                         creationflags=flags, close_fds=True)
    return "opened"


def describe_batch(batch_id: str) -> dict:
    req = store.load_request(batch_id)
    if not req:
        raise KeyError(f"no such batch: {batch_id}")
    res = store.load_results(batch_id)
    items = []
    for it in req["items"]:
        st = store.item_state(res, it["id"])
        row = {"id": it["id"], "name": it["name"], "status": st["status"], "files": st["files"]}
        if st.get("feedback"):
            row["feedback"] = [fb["text"] for fb in st["feedback"]]
        for k in ("warnings", "error"):
            if st.get(k):
                row[k] = st[k]
        items.append(row)
    return {"batch_id": batch_id, "title": req.get("title", ""), "created": req.get("created"),
            "pending": store.pending_count(req, res), "items": items}


def recent_batches(limit: int = 3) -> list[str]:
    """Batches with pending items, plus the last few."""
    ids = store.list_batch_ids()
    pending = []
    for bid in ids:
        req = store.load_request(bid)
        if req and store.pending_count(req, store.load_results(bid)):
            pending.append(bid)
    return sorted(set(pending) | set(ids[-limit:]))


mcp = FastMCP("firefly-buddy-mcp", instructions=INSTRUCTIONS)


def expected_errors(fn):
    """Show the message of an expected failure to the client (other exceptions are hidden)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except KeyError as e:
            raise ToolError(e.args[0] if e.args else str(e)) from e
        except (ValueError, intake.IntakeError, TimeoutError) as e:
            raise ToolError(str(e)) from e
    return wrapper


@mcp.tool()
@expected_errors
async def firefly_request_sfx(title: str, items: list[SfxItem], open_popup: bool = True) -> dict:
    """Ask the human to make sound effects in Adobe Firefly (web). Returns at once with a batch_id.

    title: what the batch is for, in Japanese (shown on the window, e.g. "an395 の効果音の見直し").
    items: up to 12 sounds; each gets an English prompt the human pastes into Firefly.
    A window opens on the user's desktop; files they download are converted and put in dest_dir.
    Do other work meanwhile and check with firefly_status.
    """
    check_items(items)
    batch_id = store.new_batch_id()
    req = {"batch_id": batch_id, "title": title.strip(), "created": store.now_iso(),
           "items": [{"id": f"{batch_id}-{i}", **it.model_dump(), "prompt": it.prompt.strip()}
                     for i, it in enumerate(items, 1)]}
    store.save_request(req)
    popup = launch_popup() if open_popup else "not_opened"
    return {"batch_id": batch_id,
            "items": [{"id": it["id"], "name": it["name"],
                       "dest_dir": str(flow.dest_for(req, it))} for it in req["items"]],
            "popup": popup, "firefly_url": store.FIREFLY_SFX_URL,
            "next": "The human makes these in Firefly; this takes minutes. Keep working and call "
                    "firefly_status later."}


@mcp.tool()
@expected_errors
async def firefly_status(batch_id: str | None = None, wait_seconds: int = 0,
                         reopen_popup: bool = False) -> dict:
    """What has come back. batch_id: one batch (default: batches with pending items + the last 3).

    wait_seconds (0-110): wait until something changes (a file arrives, an item is skipped) or the
    time runs out. reopen_popup: open the window again if the human closed it with items pending.
    Each item: status waiting / received / rejected (none of the takes was good) / skipped / cancelled,
    the delivered files, and feedback (notes the human typed for Claude: why it failed, which take is best).
    """
    wait = max(0, min(int(wait_seconds), MAX_WAIT))
    if wait:
        start = store.signature()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and store.signature() == start:
            await anyio.sleep(1)
    ids = [batch_id] if batch_id else recent_batches()
    batches = [describe_batch(b) for b in ids]
    popup = store.popup_alive()
    out = {"batches": batches, "popup_open": popup, "receiving": store.get_active()["item"]}
    if reopen_popup and not popup and any(b["pending"] for b in batches):
        out["popup"] = launch_popup()
    return out


@mcp.tool()
@expected_errors
async def firefly_attach(item_id: str, file_path: str) -> dict:
    """Hand a file to an item by hand (when the window did not pick the download up).

    The file is copied (not moved), converted as the item asked, and put in its dest_dir.
    """
    p = Path(file_path).expanduser()
    r = await anyio.to_thread.run_sync(lambda: flow.receive_file(item_id, p, from_downloads=False))
    return {"item_id": item_id, **r}


@mcp.tool()
@expected_errors
async def firefly_cancel(batch_id: str, item_ids: list[str] | None = None) -> dict:
    """Cancel items that are still waiting (all of the batch when item_ids is omitted)."""
    done = flow.cancel_items(batch_id, item_ids)
    return {"batch_id": batch_id, "cancelled": done}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
