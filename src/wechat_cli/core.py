"""Drive WeChat for Mac (4.x) by window screenshots, Apple Vision OCR and keyboard input.

WeChat 4.x hides its chat UI from the macOS Accessibility API, so this module
never trusts the AX tree for content. It uses AX only to find, move and size
the main window. Everything it reads comes from OCR of a window-only screenshot.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, asdict

import AppKit
import Quartz
import Vision
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementSetAttributeValue,
    AXValueCreate,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Foundation import NSURL

BUNDLE_ID = "com.tencent.xinWeChat"
WIN_X, WIN_Y, WIN_W, WIN_H = 200, 80, 1100, 820
IDLE_REQUIRED = 3.0  # seconds of no human keyboard/mouse input before taking the screen

# Key codes (US layout)
K_RETURN, K_A, K_V, K_F, K_ESC = 36, 0, 9, 3, 53


class WeChatError(RuntimeError):
    pass


@dataclass
class Line:
    text: str
    x: float  # window points, left
    y: float  # window points, top
    w: float
    h: float
    conf: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


# ---------------------------------------------------------------- window

def _running_app():
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    if not apps:
        raise WeChatError("WeChat is not running")
    return apps[0]


def _ax_main_window():
    app = AXUIElementCreateApplication(_running_app().processIdentifier())
    err, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    if err or not wins:
        raise WeChatError(f"Cannot read WeChat windows via Accessibility (err {err})")
    for w in wins:
        _, title = AXUIElementCopyAttributeValue(w, "AXTitle", None)
        _, ident = AXUIElementCopyAttributeValue(w, "AXIdentifier", None)
        _, kids = AXUIElementCopyAttributeValue(w, "AXChildren", None)
        # The main window is the one with the session list split view.
        if kids and len(kids) > 0 and title in ("WeChat", "Weixin", "微信"):
            return w
    return wins[0]


def is_logged_in() -> bool:
    w = _ax_main_window()

    def walk(e, d=0):
        if d > 12:
            return False
        _, ident = AXUIElementCopyAttributeValue(e, "AXIdentifier", None)
        if ident == "session_list":
            return True
        _, kids = AXUIElementCopyAttributeValue(e, "AXChildren", None)
        return any(walk(k, d + 1) for k in (kids or []))

    return walk(w)


def place_window() -> None:
    w = _ax_main_window()
    AXUIElementSetAttributeValue(w, "AXPosition", AXValueCreate(kAXValueCGPointType, Quartz.CGPoint(WIN_X, WIN_Y)))
    AXUIElementSetAttributeValue(w, "AXSize", AXValueCreate(kAXValueCGSizeType, Quartz.CGSize(WIN_W, WIN_H)))


def window_info() -> dict:
    pid = _running_app().processIdentifier()
    best = None
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID):
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer") != 0:
            continue
        b = w["kCGWindowBounds"]
        if b["Width"] < 400 or b["Height"] < 400:
            continue
        if best is None or b["Width"] * b["Height"] > best["w"] * best["h"]:
            best = {"id": w["kCGWindowNumber"], "x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"]}
    if best is None:
        raise WeChatError("WeChat main window not found (is it minimized or closed?)")
    return best


# ---------------------------------------------------------------- reading

def screenshot(path: str | None = None) -> tuple[str, dict]:
    info = window_info()
    path = path or os.path.join(tempfile.gettempdir(), f"wechat-cli-{os.getpid()}.png")
    # Capture the screen rectangle, not the window alone: WeChat's search
    # results are a separate popup window that -l would leave out.
    rect = f"{int(info['x'])},{int(info['y'])},{int(info['w'])},{int(info['h'])}"
    r = subprocess.run(["screencapture", "-x", "-R", rect, path], capture_output=True)
    if r.returncode != 0 or not os.path.exists(path):
        raise WeChatError(f"screencapture failed: {r.stderr.decode(errors='replace')}")
    return path, info


def ocr(path: str, info: dict) -> list[Line]:
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise WeChatError(f"OCR failed: {err}")
    W, H = info["w"], info["h"]
    out = []
    for o in req.results() or []:
        c = o.topCandidates_(1)[0]
        b = o.boundingBox()  # normalized, origin bottom-left
        out.append(Line(
            text=str(c.string()),
            x=b.origin.x * W,
            y=(1 - b.origin.y - b.size.height) * H,
            w=b.size.width * W,
            h=b.size.height * H,
            conf=float(c.confidence()),
        ))
    out.sort(key=lambda l: (round(l.y), l.x))
    return out


def read_screen() -> tuple[list[Line], dict]:
    path, info = screenshot()
    try:
        return ocr(path, info), info
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- input

def hid_idle_seconds() -> float:
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    )


def wait_for_idle(timeout: float = 90.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if hid_idle_seconds() >= IDLE_REQUIRED:
            return
        time.sleep(0.5)
    raise WeChatError("Someone is using the keyboard or mouse; not taking the screen")


def frontmost_pid() -> int | None:
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True).stdout
    m = re.search(r"pid\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else None


def is_front() -> bool:
    return frontmost_pid() == _running_app().processIdentifier()


def activate() -> None:
    app = _running_app()
    ax = AXUIElementCreateApplication(app.processIdentifier())
    AXUIElementSetAttributeValue(ax, "AXFrontmost", True)
    for i in range(30):
        if is_front():
            time.sleep(0.2)
            return
        if i == 10:
            subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True)
        time.sleep(0.1)
    raise WeChatError("Could not bring WeChat to the front")


def ensure_front() -> None:
    if not is_front():
        raise WeChatError("WeChat lost focus; stopped before typing")


def key(code: int, cmd: bool = False) -> None:
    ensure_front()
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    flags = Quartz.kCGEventFlagMaskCommand if cmd else 0
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(src, code, down)
        Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.03)


def click(win: dict, x: float, y: float) -> None:
    """Click at window-relative point (x, y)."""
    ensure_front()
    p = Quartz.CGPoint(win["x"] + x, win["y"] + y)
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        e = Quartz.CGEventCreateMouseEvent(src, t, p, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.05)


class Clipboard:
    """Save the user's clipboard, put text on it, restore on exit."""

    def __init__(self, text: str):
        self.text = text
        self.pb = AppKit.NSPasteboard.generalPasteboard()
        self.saved = None

    def __enter__(self):
        self.saved = self.pb.stringForType_(AppKit.NSPasteboardTypeString)
        self.pb.clearContents()
        self.pb.setString_forType_(self.text, AppKit.NSPasteboardTypeString)
        return self

    def __exit__(self, *exc):
        time.sleep(0.2)
        self.pb.clearContents()
        if self.saved is not None:
            self.pb.setString_forType_(self.saved, AppKit.NSPasteboardTypeString)
        return False


def paste(text: str) -> None:
    with Clipboard(text):
        key(K_V, cmd=True)
        time.sleep(0.3)


def as_dicts(lines: list[Line]) -> list[dict]:
    return [asdict(l) for l in lines]


# ---------------------------------------------------------------- gestures

def drag(win: dict, x1: float, y1: float, x2: float, y2: float) -> None:
    ensure_front()
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    p1 = Quartz.CGPoint(win["x"] + x1, win["y"] + y1)
    p2 = Quartz.CGPoint(win["x"] + x2, win["y"] + y2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, p1, 0))
    time.sleep(0.05)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseDown, p1, 0))
    for i in range(1, 11):
        p = Quartz.CGPoint(p1.x + (p2.x - p1.x) * i / 10, p1.y + (p2.y - p1.y) * i / 10)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseDragged, p, 0))
        time.sleep(0.03)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseUp, p2, 0))


def scroll(win: dict, x: float, y: float, lines: int) -> None:
    """Scroll at window point; positive lines = up (older / toward top)."""
    ensure_front()
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    p = Quartz.CGPoint(win["x"] + x, win["y"] + y)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, p, 0))
    step = 1 if lines > 0 else -1
    for _ in range(abs(lines)):
        e = Quartz.CGEventCreateScrollWheelEvent(src, Quartz.kCGScrollEventUnitLine, 1, step * 3)
        Quartz.CGEventSetLocation(e, p)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.02)


def paste_file(path: str) -> None:
    """Put a file on the clipboard (as Finder does), paste it, restore the clipboard."""
    pb = AppKit.NSPasteboard.generalPasteboard()
    saved = pb.stringForType_(AppKit.NSPasteboardTypeString)
    pb.clearContents()
    pb.writeObjects_([NSURL.fileURLWithPath_(os.path.abspath(path))])
    try:
        key(K_V, cmd=True)
        time.sleep(0.8)
    finally:
        time.sleep(0.3)
        pb.clearContents()
        if saved is not None:
            pb.setString_forType_(saved, AppKit.NSPasteboardTypeString)


class Bitmap:
    """Pixel access to a screenshot, in window points."""

    def __init__(self, path: str, info: dict):
        data = AppKit.NSData.dataWithContentsOfFile_(path)
        self.rep = AppKit.NSBitmapImageRep.imageRepWithData_(data)
        self.scale = self.rep.pixelsWide() / info["w"]
        self.cg = self.rep.CGImage()

    def red_pixels(self, x0: float, y0: float, x1: float, y1: float) -> int:
        s, n = self.scale, 0
        for py in range(int(y0 * s), int(y1 * s), 2):
            for px in range(int(x0 * s), int(x1 * s), 2):
                c = self.rep.colorAtX_y_(px, py)
                if c is None:
                    continue
                r, g, b = c.redComponent(), c.greenComponent(), c.blueComponent()
                if r > 0.8 and g < 0.4 and b < 0.4:
                    n += 1
        return n

    def ocr_region(self, x0: float, y0: float, x1: float, y1: float) -> str:
        s = self.scale
        rect = Quartz.CGRectMake(x0 * s, y0 * s, (x1 - x0) * s, (y1 - y0) * s)
        crop = Quartz.CGImageCreateWithImageInRect(self.cg, rect)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)
        req.setMinimumTextHeight_(0.2)
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(crop, None)
        h.performRequests_error_([req], None)
        return " ".join(str(o.topCandidates_(1)[0].string()) for o in (req.results() or []))


def capture() -> tuple[list[Line], dict, "Bitmap"]:
    path, info = screenshot()
    try:
        return ocr(path, info), info, Bitmap(path, info)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _upscale(cg, f: int = 6):
    w, h = Quartz.CGImageGetWidth(cg) * f, Quartz.CGImageGetHeight(cg) * f
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
                                       Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg)
    return Quartz.CGBitmapContextCreateImage(ctx)


def badge_count(bmp: "Bitmap", x0: float, y0: float, x1: float, y1: float) -> int | None:
    """Read the number in a red unread badge. None if no badge, -1 if a dot without a number."""
    s = bmp.scale
    xs, ys = [], []
    for py in range(int(y0 * s), int(y1 * s)):
        for px in range(int(x0 * s), int(x1 * s)):
            c = bmp.rep.colorAtX_y_(px, py)
            if c is not None and c.redComponent() > 0.8 and c.greenComponent() < 0.4 and c.blueComponent() < 0.4:
                xs.append(px)
                ys.append(py)
    if len(xs) < 40:
        return None
    crop = Quartz.CGImageCreateWithImageInRect(
        bmp.cg, Quartz.CGRectMake(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1))
    big = _upscale(crop)
    for level in (Vision.VNRequestTextRecognitionLevelAccurate, Vision.VNRequestTextRecognitionLevelFast):
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(level)
        req.setUsesLanguageCorrection_(False)
        Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(big, None).performRequests_error_([req], None)
        txt = "".join(str(o.topCandidates_(1)[0].string()) for o in (req.results() or []))
        digits = re.sub(r"\D", "", txt.replace("O", "0").replace("o", "0"))
        if digits:
            return int(digits)
    return -1
