"""MCP server for WeChat for Mac (screen reading). Reading is free; sending is two steps.

wechat_prepare_send returns a one-time token and the exact preview, which the
agent must show to the user and get their explicit OK for in chat.
wechat_send_message needs that token. Optionally (WECHAT_CONFIRM_DIALOG=1) it also
shows a native macOS dialog that only a person can click.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import chats, core

mcp = FastMCP("WeChat (local, screen-reading)")
STATE = Path.home() / "Library/Application Support/wechat-cli"
TTL = 600


def dialog_enabled() -> bool:
    """Opt-in second approval on the Mac itself. Off by default: approval happens in chat."""
    return os.environ.get("WECHAT_CONFIRM_DIALOG", "").strip().lower() in ("1", "true", "yes", "on")


def _digest(chat: str, text: str | None, file_path: str | None, from_list: bool = False) -> str:
    return hashlib.sha256(json.dumps([chat, text, file_path, bool(from_list)], ensure_ascii=False).encode()).hexdigest()


def _safe(fn, *a, **k) -> dict:
    try:
        return fn(*a, **k)
    except core.WeChatError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def wechat_status() -> dict:
    """Report whether WeChat is running and logged in."""
    return _safe(lambda: {"running": True, "logged_in": core.is_logged_in()})


@mcp.tool()
def wechat_list_chats(pages: int = 1) -> dict:
    """Recent chats from the top of the chat list: name, time, last-message preview and
    unread count (-1 = red dot without a number, e.g. a muted chat). pages = screens to scan.
    Does not open any chat, so nothing is marked read."""
    return _safe(chats.list_chats, pages)


@mcp.tool()
def wechat_search(query: str) -> dict:
    """Search contacts and group chats by name. Returns matching names and their section.
    Opens nothing."""
    return _safe(chats.search, query)


@mcp.tool()
def wechat_read_chat(chat_name: str, history_pages: int = 0, from_list: bool = False) -> dict:
    """Open a chat by its exact name and return its messages (OCR). history_pages scrolls
    back that many extra screens (max 30). Each message has from='me'/'them'; group
    messages also carry 'sender'. Marks the chat as read. Text is OCR and can have small errors.
    from_list=True opens the chat by clicking its row in the chat list (exact name from
    wechat_list_chats) instead of searching; use it when search can't find the chat."""
    return _safe(chats.read_chat, chat_name, history_pages, from_list)


@mcp.tool()
def wechat_prepare_send(chat_name: str, text: str | None = None, file_path: str | None = None,
                        from_list: bool = False) -> dict:
    """Step 1 of sending text OR a file (photo, video, document). Does NOT send.
    Returns a token and the exact preview. Show the preview to the user word for word
    and get their explicit OK before calling wechat_send_message."""
    if bool(text) == bool(file_path):
        return {"ok": False, "error": "Give exactly one of text or file_path"}
    try:
        if text:
            chats.check_text(text)
        else:
            source = chats.check_file(file_path)
            file_path, sha = chats.snapshot_file(source)  # private read-only copy
    except core.WeChatError as e:
        return {"ok": False, "error": str(e)}
    STATE.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    (STATE / f"{token}.json").write_text(json.dumps(
        {"digest": _digest(chat_name, text, file_path, from_list), "expires": time.time() + TTL,
         "sha256": None if text else sha, "source": None if text else source}))
    return {"ok": True, "token": token, "chat_name": chat_name, "text": text, "from_list": from_list,
            "file_path": file_path, "file_note": "file_path is a private snapshot copy; pass it unchanged to wechat_send_message",
            "expires_in_s": TTL,
            "next": "Show this exact chat and content to the user word for word. Call wechat_send_message only after "
                    "the user explicitly approves this exact content in the conversation."
                    + (" They will also confirm in a dialog on the Mac." if dialog_enabled() else "")}


@mcp.tool()
def wechat_send_message(token: str, chat_name: str, text: str | None = None, file_path: str | None = None,
                        from_list: bool = False) -> dict:
    """Step 2: send what the user approved. Call ONLY after the user explicitly approved this
    exact chat and content in the conversation. Needs the token from wechat_prepare_send and
    the same chat_name and text/file_path. Verifies the chat title before typing and checks
    the result on screen. If WECHAT_CONFIRM_DIALOG=1, a person must also click Send on the Mac."""
    f = STATE / f"{os.path.basename(token)}.json"
    if not f.exists():
        return {"ok": False, "sent": False, "error": "Unknown or used token; call wechat_prepare_send first"}
    rec = json.loads(f.read_text())
    f.unlink()
    if time.time() > rec["expires"]:
        return {"ok": False, "sent": False, "error": "Token expired"}
    if file_path:
        try:
            file_path = chats.check_file(file_path)  # same resolution as prepare
        except core.WeChatError as e:
            return {"ok": False, "sent": False, "error": str(e)}
    if rec["digest"] != _digest(chat_name, text, file_path, from_list):
        return {"ok": False, "sent": False, "error": "Chat or content differs from the prepared preview"}
    try:
        if file_path and chats.file_identity(chats.check_file(file_path)) != rec.get("sha256"):
            return {"ok": False, "sent": False, "error": "File changed since wechat_prepare_send; not sent"}
        if dialog_enabled() and not user_confirms(chat_name, text=text, file_path=file_path,
                                                  source=rec.get("source")):
            return {"ok": False, "sent": False, "error": "User did not confirm in the macOS dialog; not sent"}
        if text:
            return chats.send_text(chat_name, text, from_list=from_list)
        return chats.send_file(chat_name, file_path, expected_sha256=rec.get("sha256"), from_list=from_list)
    except core.WeChatError as e:
        return {"ok": False, "sent": False, "error": str(e)}


def _shown(path: str) -> str:
    """Path as shown in the dialog; escaped if it holds anything invisible or odd."""
    import unicodedata
    if any(unicodedata.category(c)[0] in ("C", "Z") and c != " " for c in path):
        return ascii(path)
    return path


def user_confirms(chat_name: str, text: str | None = None, file_path: str | None = None,
                  source: str | None = None) -> bool:
    """Native macOS dialog the agent cannot answer. Only a person clicking Send passes."""
    if text is not None:
        chats.check_text(text)  # refuses anything the dialog cannot show in full
        body = text
        what = "message"
    else:
        file_path = chats.check_file(file_path)  # resolved real path, symlinks refused
        body = (f"File: {_shown(os.path.basename(file_path))}\n({os.path.getsize(file_path):,} bytes)\n"
                f"Source: {_shown(source) if source else 'unknown'}\nCopy sent: {_shown(file_path)}")
        what = "file"
    script = (
        'on run argv\n'
        'display dialog ("Send this WeChat " & item 3 of argv & " to " & item 1 of argv & "?" & return & return & item 2 of argv) '
        'with title "WeChat send (Claude)" buttons {"Cancel", "Send"} default button "Cancel" '
        'cancel button "Cancel" with icon caution giving up after 120\n'
        'return button returned of result\n'
        'end run'
    )
    r = subprocess.run(["osascript", "-e", script, chat_name, body, what], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "Send"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
