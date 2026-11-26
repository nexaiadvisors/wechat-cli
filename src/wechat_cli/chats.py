"""Everyday WeChat actions built on core: list chats, search, read with history,
group senders, send text and files.

Layout comes from the Accessibility tree's container frames (which WeChat
reports correctly even though it hides their contents). Content comes from OCR.
"""
from __future__ import annotations

import difflib
import os
import re
import time
import unicodedata

from ApplicationServices import AXUIElementCopyAttributeValue as _ax

from . import core
from .core import Line, WeChatError

LIST_WIDTH = 370  # chat-list width we drag the splitter to (window points)
MAX_TEXT = 1200
MAX_FILE_MB = 100

TARGET_SECTIONS = {"contacts", "groupchats", "联系人", "群聊", "mostused", "常用", "最常使用", "tophits"}
OTHER_SECTIONS = {
    "chathistory", "officialaccounts", "internetsearchresults", "more", "聊天记录", "公众号",
    "搜一搜", "更多", "miniprograms", "小程序", "serviceaccounts", "服务号", "channels", "视频号",
    "functions", "功能", "favorites", "收藏",
}
TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|yesterday.*|昨天.*|星期.|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day.*|\d{1,2}/\d{1,2} \d{1,2}:\d{2}|\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2})$",
    re.I,
)


def norm(s: str) -> str:
    """Whitespace-insensitive (OCR spacing varies) but case-sensitive."""
    return "".join(s.split()).translate(str.maketrans({"｜": "|", "（": "(", "）": ")"}))


# ---------------------------------------------------------------- geometry

def _frame(el) -> tuple[float, float, float, float] | None:
    _, p = _ax(el, "AXPosition", None)
    _, s = _ax(el, "AXSize", None)
    if p is None or s is None:
        return None
    pt = core.Quartz.CGPoint()
    sz = core.Quartz.CGSize()
    from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
    _, pt = AXValueGetValue(p, kAXValueCGPointType, None)
    _, sz = AXValueGetValue(s, kAXValueCGSizeType, None)
    return pt.x, pt.y, sz.width, sz.height


def geometry() -> dict:
    """Window-relative layout of the chat list, chat pane and input box."""
    info = core.window_info()
    w = core._ax_main_window()
    found: dict = {}

    def walk(e, d=0):
        if d > 14:
            return
        _, ident = _ax(e, "AXIdentifier", None)
        if ident == "session_list":
            found["list"] = _frame(e)
        elif ident == "qt_splithandle_":
            f = _frame(e)
            if f and f[2] <= 6:
                found["vhandle"] = f
            elif f and f[3] <= 6:
                found["hhandle"] = f
        _, kids = _ax(e, "AXChildren", None)
        for k in kids or []:
            walk(k, d + 1)

    walk(w)
    if "list" not in found:
        raise WeChatError("Chat list not found; is WeChat logged in?")
    wx, wy = info["x"], info["y"]
    lx, ly, lw, lh = found["list"]
    g = {
        "info": info,
        "list_left": lx - wx, "list_right": lx - wx + lw, "list_top": ly - wy, "list_bottom": ly - wy + lh,
        "chat_left": lx - wx + lw + 2, "chat_right": info["w"],
        "input_top": (found["hhandle"][1] - wy + 4) if "hhandle" in found else info["h"] - 140,
        "vhandle_x": (found["vhandle"][0] - wx) if "vhandle" in found else lx - wx + lw,
    }
    g["chat_mid"] = (g["chat_left"] + g["chat_right"]) / 2
    return g


def ensure_layout() -> dict:
    core.wait_for_idle()
    core.activate()
    core.place_window()
    time.sleep(0.25)
    g = geometry()
    width = g["list_right"] - g["list_left"]
    if abs(width - LIST_WIDTH) > 30:
        y = (g["list_top"] + g["list_bottom"]) / 2
        core.drag(g["info"], g["vhandle_x"] + 2, y, g["list_left"] + LIST_WIDTH + 2, y)
        time.sleep(0.4)
        g = geometry()
    return g


# ---------------------------------------------------------------- search

def _open_search(g: dict, query: str) -> list[Line]:
    core.key(core.K_ESC)
    time.sleep(0.2)
    core.click(g["info"], g["list_left"] + 80, g["list_top"] / 2 + 2)
    time.sleep(0.5)
    core.key(core.K_A, cmd=True)
    core.paste(query)
    time.sleep(1.6)
    lines, _ = core.read_screen()
    return lines


def _popup_rect(g: dict) -> tuple[float, float, float, float] | None:
    """Window-relative frame of WeChat's search-results popup (a separate window).
    Lines outside it are the chat list showing underneath, which must not count as results."""
    info = g["info"]
    pid = core._running_app().processIdentifier()
    best = None
    for w in core.Quartz.CGWindowListCopyWindowInfo(core.Quartz.kCGWindowListOptionOnScreenOnly,
                                                    core.Quartz.kCGNullWindowID):
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowNumber") == info["id"]:
            continue
        b = w["kCGWindowBounds"]
        x, y, ww, hh = b["X"] - info["x"], b["Y"] - info["y"], b["Width"], b["Height"]
        # The popup sits over the chat list, starting near the top of the window.
        if ww >= 200 and hh >= 60 and abs(x - g["list_left"]) < 60 and -10 < y < g["list_top"] + 40:
            if best is None or ww * hh > best[2] * best[3]:
                best = (x, y, ww, hh)
    return best


def _parse_search(lines: list[Line], g: dict) -> list[dict]:
    left, right = g["list_left"], g["list_right"] + 40
    top, bottom = g["list_top"] - 5, g["list_bottom"]
    pop = _popup_rect(g)
    if pop is not None:
        left, right = pop[0], pop[0] + pop[2]
        top, bottom = max(top, pop[1]), pop[1] + pop[3]
    out, section = [], None
    for l in sorted([l for l in lines if left - 2 <= l.x <= right and top < l.y < bottom],
                    key=lambda l: (l.y, l.x)):
        t = norm(l.text)
        tc = t.casefold()
        if tc in OTHER_SECTIONS:
            section = None  # e.g. "Internet search results" sits further right (after an icon)
            continue
        if l.x < left + 60 and tc in TARGET_SECTIONS:
            section = ("contact" if tc in ("contacts", "联系人") else
                       "group" if tc in ("groupchats", "群聊") else "most_used")
            continue
        if section is not None and l.x > left + 250 and TIME_RE.match(l.text.strip()):
            section = None  # a date at the right edge: chat-list rows showing below the popup
            continue
        if l.x < left + 60:
            continue  # other header-column text
        if section is None or l.x > left + 90:
            continue  # only the name column (not member lists or dates)
        if tc.startswith("include:") or t.startswith("包含"):
            continue
        out.append({"section": section, "name": l.text, "line": l})
    return out


def search(query: str) -> dict:
    """Search contacts and groups. Returns names only; opens nothing."""
    g = ensure_layout()
    try:
        res = _parse_search(_open_search(g, query), g)
    finally:
        core.key(core.K_ESC)
    return {"ok": True, "query": query, "results": [{"section": r["section"], "name": r["name"]} for r in res]}


# ---------------------------------------------------------------- opening a chat

def chat_title(lines: list[Line], g: dict) -> str | None:
    top = [l for l in lines if l.y < g["list_top"] - 4 and l.x > g["chat_left"] + 4 and l.x < g["chat_right"] - 110]
    if not top:
        return None
    return " ".join(l.text for l in sorted(top, key=lambda l: l.x)).strip()


def _title_matches(title: str | None, name: str, maybe_group: bool) -> bool:
    if not title:
        return False
    t = norm(title)
    if maybe_group:
        t = re.sub(r"\(\d+\)$", "", t)
    return t == norm(name)


def open_chat(name: str) -> dict:
    """Open a chat by exact name via search; verify the title by OCR."""
    g = ensure_layout()
    res = _parse_search(_open_search(g, name), g)
    exact = [r for r in res if norm(r["name"]) == norm(name)]
    # "Most used" repeats a contact or group that is also listed in its own section;
    # those entries are the same chat, not two chats with one name.
    if len(exact) > 1 and len({r["section"] for r in exact}) == len(exact) \
            and len([r for r in exact if r["section"] != "most_used"]) <= 1:
        exact = [next((r for r in exact if r["section"] != "most_used"), exact[0])]
    if len(exact) != 1:
        core.key(core.K_ESC)
        why = "No exact contact or group" if not exact else f"{len(exact)} chats share the exact name"
        return {"ok": False, "error": f"{why} {name!r}; refusing to guess",
                "candidates": [{"section": r["section"], "name": r["name"]} for r in res]}
    hit = exact[0]
    core.click(g["info"], hit["line"].x + 15, hit["line"].cy)
    time.sleep(1.2)
    out = verify_open(name, hit["section"] != "contact") | {"section": hit["section"]}
    if not out["ok"]:
        out["error"] = f"Could not confirm the opened chat is {name!r} (title read as {out['title']!r})"
    return out


def _selected_row(lines: list[Line], g: dict, bmp) -> str | None:
    """Name of the highlighted (green) row in the chat list, if it is on screen."""
    for r in _parse_list(lines, g, None):
        c = bmp.rep.colorAtX_y_(int((g["list_left"] + 8) * bmp.scale), int(r["_line"].cy * bmp.scale))
        if c is not None and c.greenComponent() > 0.55 and c.redComponent() < 0.5 and c.blueComponent() < 0.6:
            return r["name"]
    return None


def verify_open(name: str, maybe_group: bool) -> dict:
    """Confirm the open chat is `name`: by the title, or, when the title can't be read
    (OCR often misses a one-character title), by the highlighted chat-list row."""
    g = geometry()
    lines, _, bmp = core.capture()
    title = chat_title(lines, g)
    ok = _title_matches(title, name, maybe_group)
    how = "title"
    if not ok and title is None:
        sel = _selected_row(lines, g, bmp)
        ok = sel is not None and norm(sel) == norm(name)
        how = "highlighted row"
        title = sel if ok else None
    is_group = bool(title and re.search(r"\(\d+\)$", norm(title)))
    return {"ok": ok, "title": title, "verified_by": how, "is_group": is_group, "lines": lines, "g": g}


def open_from_list(name: str, pages: int = 5) -> dict:
    """Open a chat by clicking its row in the chat list (exact name match).
    Useful when search cannot find it, e.g. one-character names."""
    g = ensure_layout()
    core.key(core.K_ESC)
    time.sleep(0.2)
    x, y = (g["list_left"] + g["list_right"]) / 2, (g["list_top"] + g["list_bottom"]) / 2
    core.scroll(g["info"], x, y, 200)
    time.sleep(0.5)
    for i in range(max(1, min(pages, 20))):
        lines, _, bmp = core.capture()
        rows = [r for r in _parse_list(lines, g, None) if norm(r["name"]) == norm(name)]
        if len(rows) > 1:
            return {"ok": False, "error": f"{len(rows)} chat-list rows are named {name!r}; refusing to guess"}
        if rows:
            ln = rows[0]["_line"]
            core.click(g["info"], ln.x + 10, ln.cy)
            time.sleep(1.2)
            out = verify_open(name, True) | {"section": "list"}
            if not out["ok"]:
                out["error"] = f"Could not confirm the opened chat is {name!r} (title read as {out['title']!r})"
            return out
        core.scroll(g["info"], x, y, -8)
        time.sleep(0.5)
    return {"ok": False, "error": f"No chat-list row named {name!r} in the first {pages} screens"}


def _open(name: str, from_list: bool) -> dict:
    if from_list:
        return open_from_list(name)
    r = open_chat(name)
    if not r["ok"] and str(r.get("error", "")).startswith("No exact contact or group"):
        # Search misses some names (e.g. one-character names OCR can't read in the popup);
        # fall back to the chat list, which also requires an exact match and verifies the result.
        alt = open_from_list(name)
        if alt["ok"]:
            return alt
    return r


# ---------------------------------------------------------------- messages

def messages_from_screen(lines: list[Line], g: dict, is_group: bool) -> list[dict]:
    left, mid = g["chat_left"], g["chat_mid"]
    body = sorted([l for l in lines if g["list_top"] < l.y < g["input_top"] - 4 and l.x > left + 4],
                  key=lambda l: (l.y, l.x))
    msgs: list[dict] = []
    for l in body:
        centered = abs(l.cx - mid) < 70 and l.w < 240
        if centered and TIME_RE.match(l.text.strip()):
            msgs.append({"kind": "time", "text": l.text.strip(), "_y": l.y, "_b": l.y + l.h})
            continue
        if centered and re.search(r"(recalled a message|撤回了一条消息|joined the group|加入了群聊|invited|邀请)", l.text):
            msgs.append({"kind": "system", "text": l.text.strip(), "_y": l.y, "_b": l.y + l.h})
            continue
        # Their bubbles start at a fixed indent next to their avatar; mine are right-aligned
        # and start further right. Using the left edge (not the centre) keeps a short last
        # line of my long message on my side.
        side = "them" if l.x < left + 120 else "me"
        prev = msgs[-1] if msgs else None
        same_block = prev and prev["kind"] == "msg" and prev["from"] == side and l.y - prev["_b"] < 12
        if same_block:
            # A short line right above the first text line of a group message is the sender name.
            if (is_group and side == "them" and prev.get("sender") is None and prev["_n"] == 1
                    and prev["_h"] < 13.5 and prev["_h"] < l.h - 0.8 and len(prev["text"]) <= 24):
                prev["sender"] = prev["text"]
                prev["text"] = l.text
                prev["_h"] = l.h
            else:
                prev["text"] += ("\n" if l.y - prev["_ly"] > 4 else " ") + l.text
                prev["_n"] += 1
            prev["_b"], prev["_ly"] = l.y + l.h, l.y
        else:
            m = {"kind": "msg", "from": side, "text": l.text, "_y": l.y, "_b": l.y + l.h, "_ly": l.y, "_h": l.h, "_n": 1}
            if is_group and side == "them":
                m["sender"] = None
            msgs.append(m)
    if is_group:
        # WeChat prints the sender's name as a separate short line just above a
        # group message; fold it into the message that follows.
        folded: list[dict] = []
        i = 0
        while i < len(msgs):
            m = msgs[i]
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if (m["kind"] == "msg" and m["from"] == "them" and m["_n"] == 1 and len(m["text"]) <= 24
                    and nxt and nxt["kind"] == "msg" and nxt["from"] == "them" and 0 <= nxt["_y"] - m["_b"] < 45
                    and not nxt.get("sender") and m["_h"] <= nxt["_h"] + 1):
                nxt["sender"] = m["text"]
                folded.append(nxt)
                i += 2
                continue
            folded.append(m)
            i += 1
        msgs = folded
    for m in msgs:
        for k in ("_y", "_b", "_ly", "_h", "_n"):
            m.pop(k, None)
        if m.get("from") == "me":
            m.pop("sender", None)
    return msgs


def _key(m: dict) -> tuple:
    return (m["kind"], m.get("from"), norm(m["text"])[:40])


def _same(a: dict, b: dict) -> bool:
    if a["kind"] != b["kind"] or a.get("from") != b.get("from"):
        return False
    x, y = norm(a["text"])[:60], norm(b["text"])[:60]
    if not x or not y:
        return x == y
    # OCR of the same bubble varies a little between screens, and a bubble cut
    # at the screen edge shows only part of its text.
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.6 or x in y or y in x


def _merge(older: list[dict], newer: list[dict]) -> list[dict]:
    """Join two overlapping screens. Items cut at a screen edge may be partial,
    so allow dropping up to 2 items at older's bottom and newer's top."""
    best = (0, 0, 0)  # (k, i, j)
    for i in range(0, 3):
        for j in range(0, 3):
            o = older[: len(older) - i] if i else older
            n = newer[j:]
            for k in range(min(len(o), len(n)), 0, -1):
                if all(_same(a, b) for a, b in zip(o[-k:], n[:k])):
                    if k > best[0]:
                        best = (k, i, j)
                    break
    k, i, j = best
    if k == 0:
        return older + [{"kind": "gap", "text": "(possible gap: screens did not overlap)"}] + newer
    return older[: len(older) - i] + newer[j + k:] if i else older + newer[j + k:]


def read_chat(name: str, pages: int = 0, from_list: bool = False) -> dict:
    """Open a chat and return visible messages, plus `pages` extra screens of history."""
    r = _open(name, from_list)
    if not r["ok"]:
        r.pop("lines", None)
        r.pop("g", None)
        return r
    g, is_group = r["g"], r["is_group"]
    msgs = messages_from_screen(r["lines"], g, is_group)
    y = (g["list_top"] + g["input_top"]) / 2
    sig = [l.text for l in r["lines"] if g["list_top"] < l.y < g["input_top"]]
    for _ in range(max(0, min(pages, 30))):
        core.scroll(g["info"], g["chat_mid"], y, 5)
        time.sleep(0.8)
        lines, _ = core.read_screen()
        new_sig = [l.text for l in lines if g["list_top"] < l.y < g["input_top"]]
        if new_sig == sig:
            break  # nothing moved: reached the start of the chat
        sig = new_sig
        msgs = _merge(messages_from_screen(lines, g, is_group), msgs)
    if pages:
        core.scroll(g["info"], g["chat_mid"], y, -200)  # back to the newest messages
    return {"ok": True, "title": r["title"], "is_group": is_group, "messages": msgs}


# ---------------------------------------------------------------- chat list

def _parse_list(lines: list[Line], g: dict, bmp=None) -> list[dict]:
    left, right = g["list_left"], g["list_right"]
    region = [l for l in lines if left <= l.x < right and g["list_top"] <= l.y < g["list_bottom"]]
    times = [l for l in region if l.x + l.w > right - 30 and l.x > left + 150 and TIME_RE.match(l.text.strip())]
    rows = []
    for t in sorted(times, key=lambda l: l.y):
        name = [l for l in region if abs(l.cy - t.cy) < 7 and left + 45 < l.x < t.x - 2]
        prev = [l for l in region if t.y + 8 < l.y < t.y + 34 and l.x > left + 45]
        badge = [l for l in region if l.x < left + 50 and t.y - 18 < l.y < t.y + 8 and l.text.strip().isdigit()]
        if not name:
            continue
        unread = int(badge[0].text.strip()) if badge else 0
        if not badge and bmp is not None:
            # Small red badge on the avatar's top-right corner.
            bx0, by0, bx1, by1 = left + 34, t.y - 16, left + 60, t.y + 6
            n = core.badge_count(bmp, bx0, by0, bx1, by1)
            unread = 0 if n is None else n  # -1: red dot without a number (e.g. muted chat)
        rows.append({
            "_line": sorted(name, key=lambda l: l.x)[0],
            "name": " ".join(l.text for l in sorted(name, key=lambda l: l.x)),
            "time": t.text.strip(),
            "preview": " ".join(l.text for l in sorted(prev, key=lambda l: l.x)),
            "unread": unread,
        })
    return rows


def list_chats(pages: int = 1) -> dict:
    """Recent chats from the top of the list: name, time, last-message preview, unread count.
    Unread counts come from the red badges and can be missed by OCR."""
    g = ensure_layout()
    core.key(core.K_ESC)
    x, y = (g["list_left"] + g["list_right"]) / 2, (g["list_top"] + g["list_bottom"]) / 2
    core.scroll(g["info"], x, y, 200)
    time.sleep(0.5)
    seen, rows = set(), []
    for i in range(max(1, min(pages, 20))):
        lines, _, bmp = core.capture()
        new = 0
        for r in _parse_list(lines, g, bmp):
            r.pop("_line", None)
            k = (r["name"], r["time"])
            if k not in seen:
                seen.add(k)
                rows.append(r)
                new += 1
        if i + 1 < pages:
            if new == 0:
                break
            core.scroll(g["info"], x, y, -8)
            time.sleep(0.5)
    core.scroll(g["info"], x, y, 200)
    return {"ok": True, "chats": rows}


# ---------------------------------------------------------------- sending

def check_text(text: str) -> None:
    """Refuse text the confirm dialog cannot show faithfully."""
    if not text.strip():
        raise WeChatError("Empty message")
    if len(text) > MAX_TEXT:
        raise WeChatError(f"Message longer than {MAX_TEXT} characters; split it")
    if "\r" in text:
        raise WeChatError("Message contains carriage returns; use plain newlines")
    lines = text.split("\n")
    if len(lines) > 25:
        raise WeChatError("Message has more than 25 lines; split it")
    for a, b in zip(lines, lines[1:]):
        if not a.strip() and not b.strip():
            raise WeChatError("Message contains consecutive blank lines; refusing")
    for ch in text:
        if ch == "\n":
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn") or (cat == "Zs" and ch not in (" ", "　")) \
                or ch in "ㅤᅟᅠﾠ⠀":
            raise WeChatError(f"Message contains a hidden or control character (U+{ord(ch):04X}); refusing")


def file_identity(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SNAP_ROOT = os.path.expanduser("~/Library/Application Support/wechat-cli/outgoing")


def snapshot_file(path: str) -> tuple[str, str]:
    """Copy the file into a private 0700 folder and hash the copy. The copy is what
    gets confirmed and pasted, so later changes to the original cannot slip in.
    Copies older than a day are cleaned up (WeChat may still be uploading a fresh one)."""
    import shutil
    import tempfile
    src = check_file(path)
    os.makedirs(SNAP_ROOT, mode=0o700, exist_ok=True)
    now = time.time()
    for d in os.listdir(SNAP_ROOT):
        full = os.path.join(SNAP_ROOT, d)
        if os.path.isdir(full) and now - os.path.getmtime(full) > 86400:
            shutil.rmtree(full, ignore_errors=True)
    folder = tempfile.mkdtemp(dir=SNAP_ROOT)  # created 0700
    dst = os.path.join(folder, os.path.basename(src))
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fin, open(dst, "xb") as fout:
        shutil.copyfileobj(fin, fout)
    os.chmod(dst, 0o400)
    return dst, file_identity(dst)


def check_file(path: str) -> str:
    raw = os.path.abspath(os.path.expanduser(path))
    if os.path.islink(raw):
        raise WeChatError("Refusing to send a symlink; pass the real file")
    path = os.path.realpath(raw)
    if not os.path.isfile(path):
        raise WeChatError(f"Not a file: {path}")
    if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
        raise WeChatError(f"File larger than {MAX_FILE_MB} MB")
    base = os.path.basename(path)
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in base):
        raise WeChatError("File name contains hidden characters; rename it first")
    return path


def _focus_input(g: dict) -> None:
    core.click(g["info"], g["chat_mid"], g["input_top"] + 45)
    time.sleep(0.2)


def _press_send(g: dict, lines: list[Line]) -> None:
    btn = next((l for l in lines if norm(l.text).casefold() in ("send", "发送") and l.y > g["input_top"]), None)
    core.wait_for_idle(timeout=10)
    core.activate()
    if btn is not None:
        core.click(g["info"], btn.cx, btn.cy)
    else:
        core.key(core.K_RETURN, cmd=True)


def _recheck_title(name: str, r: dict) -> tuple[list[Line], dict] | None:
    v = verify_open(name, r["section"] != "contact")
    return (v["lines"], v["g"]) if v["ok"] else None


def send_text(name: str, text: str, from_list: bool = False) -> dict:
    check_text(text)
    r = _open(name, from_list)
    if not r["ok"]:
        return {k: v for k, v in r.items() if k not in ("lines", "g")} | {"sent": False}
    g = r["g"]
    before = [m for m in messages_from_screen(r["lines"], g, r["is_group"]) if m.get("from") == "me"]
    _focus_input(g)
    core.key(core.K_A, cmd=True)
    core.paste(text)
    time.sleep(0.5)
    chk = _recheck_title(name, r)
    if chk is None:
        return {"ok": False, "sent": False, "error": "Chat changed before send; not sent"}
    lines, g = chk
    box = " ".join(l.text for l in lines if l.y > g["input_top"] and l.x > g["chat_left"])
    probe = norm(text.splitlines()[0])[:6]
    if probe and probe not in norm(box):
        return {"ok": False, "sent": False, "error": "Pasted text not visible in the input box; not sent (text left in the box)"}
    _press_send(g, lines)
    time.sleep(1.2)
    lines3, _ = core.read_screen()
    mine = [m for m in messages_from_screen(lines3, g, r["is_group"]) if m.get("from") == "me"]
    seen = bool(mine) and probe in norm(mine[-1]["text"])
    box_after = " ".join(l.text for l in lines3 if l.y > g["input_top"] and l.x > g["chat_left"])
    return {"ok": seen, "sent": True, "verified_on_screen": seen, "input_box_cleared": probe not in norm(box_after),
            "title": r["title"]}


def send_file(name: str, path: str, expected_sha256: str | None = None, from_list: bool = False) -> dict:
    """Send a photo, video or file. Images paste inline; other files paste as a file card."""
    path = check_file(path)
    if expected_sha256 and file_identity(path) != expected_sha256:
        raise WeChatError("File changed after it was confirmed; not sent")
    base = os.path.basename(path)
    r = _open(name, from_list)
    if not r["ok"]:
        return {k: v for k, v in r.items() if k not in ("lines", "g")} | {"sent": False}
    g = r["g"]
    before_lines = r["lines"]
    _focus_input(g)
    core.key(core.K_A, cmd=True)
    core.key(51)  # Delete: clear any draft text so only the file goes
    core.paste_file(path)
    time.sleep(0.8)
    chk = _recheck_title(name, r)
    if chk is None:
        return {"ok": False, "sent": False, "error": "Chat changed before send; not sent"}
    lines, g = chk
    box_lines = [l for l in lines if l.y > g["input_top"] and l.x > g["chat_left"]]
    box_before = [l for l in before_lines if l.y > g["input_top"] and l.x > g["chat_left"]]
    is_image = os.path.splitext(base)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".bmp")
    if not is_image and not any(norm(os.path.splitext(base)[0])[:8] in norm(l.text) for l in box_lines):
        return {"ok": False, "sent": False,
                "error": "File did not appear in the input box; not sent (check the box before retrying)"}
    _press_send(g, lines)
    time.sleep(2.0)
    lines3, _ = core.read_screen()
    body3 = " ".join(l.text for l in lines3 if g["list_top"] < l.y < g["input_top"] and l.cx > g["chat_mid"])
    stem = norm(os.path.splitext(base)[0])[:8]
    seen_name = bool(stem) and stem in norm(body3)
    return {"ok": True, "sent": True, "file": base, "title": r["title"],
            "verified_on_screen": seen_name,
            "note": None if seen_name else "Images show as a picture, so the file name is not checked on screen"}
