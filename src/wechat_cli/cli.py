"""wechat: read and send WeChat for Mac messages from the terminal."""
from __future__ import annotations

import argparse
import json
import sys

from . import chats, core
from .mcp_server import user_confirms


def _print_messages(out: dict) -> None:
    print(f"# {out['title']}")
    for m in out["messages"]:
        if m["kind"] in ("time", "system", "gap"):
            print(f"\n-- {m['text']} --")
        elif m["from"] == "me":
            print(f"[me] {m['text']}")
        else:
            who = m.get("sender") or "them"
            print(f"[{who}] {m['text']}")


def main() -> None:
    p = argparse.ArgumentParser(prog="wechat", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Is WeChat running and logged in?")
    ls = sub.add_parser("list", help="Recent chats with unread counts")
    ls.add_argument("--pages", type=int, default=1)
    se = sub.add_parser("search", help="Search contacts and groups")
    se.add_argument("query")
    r = sub.add_parser("read", help="Open a chat and print its messages")
    r.add_argument("chat", help="Exact contact or group name")
    r.add_argument("--pages", type=int, default=0, help="Extra screens of history to scroll back")
    s = sub.add_parser("send", help="Send text or a file to a chat")
    s.add_argument("chat", help="Exact contact or group name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file", help="Read the message text from this file")
    g.add_argument("--attach", help="Send this file (photo, video or document)")
    s.add_argument("--yes", action="store_true", help="Required to proceed to the confirm dialog")
    for sp in (ls, se, r):
        sp.add_argument("--json", action="store_true")
    a = p.parse_args()

    try:
        if a.cmd == "status":
            print(json.dumps({"running": True, "logged_in": core.is_logged_in()}))
        elif a.cmd == "list":
            out = chats.list_chats(a.pages)
            if a.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                for c in out["chats"]:
                    badge = "" if c["unread"] == 0 else ("(•) " if c["unread"] < 0 else f"({c['unread']}) ")
                    print(f"{badge}{c['name']}  [{c['time']}]  {c['preview']}")
        elif a.cmd == "search":
            out = chats.search(a.query)
            if a.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                for x in out["results"]:
                    print(f"{x['section']:10} {x['name']}")
        elif a.cmd == "read":
            out = chats.read_chat(a.chat, a.pages)
            if a.json or not out.get("ok"):
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                _print_messages(out)
        elif a.cmd == "send":
            text = a.text if a.text is not None else (
                open(a.text_file, encoding="utf-8").read().rstrip("\n") if a.text_file else None)
            what = text if text is not None else f"file {a.attach}"
            if not a.yes:
                print(f"Would send to {a.chat!r}:\n---\n{what}\n---\nRe-run with --yes to get the confirm dialog.")
                sys.exit(2)
            src = chats.check_file(a.attach) if text is None else None
            snap, sha = chats.snapshot_file(src) if text is None else (None, None)
            if not user_confirms(a.chat, text=text, file_path=snap, source=src):
                print(json.dumps({"ok": False, "sent": False, "error": "Not confirmed in the macOS dialog"}))
                sys.exit(1)
            out = chats.send_text(a.chat, text) if text is not None else chats.send_file(a.chat, snap, expected_sha256=sha)
            print(json.dumps(out, ensure_ascii=False))
    except core.WeChatError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
