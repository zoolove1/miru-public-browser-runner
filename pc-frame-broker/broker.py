#!/usr/bin/env python3
"""MIRU PC Frame Broker v0.1.0.

RAM-only screen capture broker for a bounded observation -> decision -> key-input
feasibility test. Frames are never written to disk by this process.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ALLOWED_KEYS = {
    "LEFT", "RIGHT", "UP", "DOWN", "ENTER", "ESC", "SPACE", "TAB",
    "BACKSPACE", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J",
    "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V",
    "W", "X", "Y", "Z", "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
    "F9", "F10", "F11", "F12",
}
VK = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B,
    "SPACE": 0x20, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
}
EXTENDED_KEYS = {"LEFT", "RIGHT", "UP", "DOWN"}
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001


def now_ms() -> int:
    return int(time.time() * 1000)


def foreground_window() -> tuple[int, str]:
    if os.name != "nt":
        return 0, ""
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    length = int(user32.GetWindowTextLengthW(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return hwnd, buf.value


def tap_key(key: str, duration_ms: int = 70) -> None:
    if os.name != "nt":
        raise RuntimeError("Keyboard input is supported only on Windows")
    import ctypes

    key = key.upper()
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Key is not allowlisted: {key}")
    vk = VK[key]
    extra = KEYEVENTF_EXTENDEDKEY if key in EXTENDED_KEYS else 0
    ctypes.windll.user32.keybd_event(vk, 0, extra, 0)
    time.sleep(max(20, min(duration_ms, 1000)) / 1000.0)
    ctypes.windll.user32.keybd_event(vk, 0, extra | KEYEVENTF_KEYUP, 0)


def type_fixed_test() -> None:
    for ch in "MIRUTEST1":
        tap_key(ch, 35)
        time.sleep(0.025)


@dataclass(frozen=True)
class Frame:
    frame_id: int
    captured_ms: int
    jpeg: bytes
    width: int
    height: int


class BrokerState:
    def __init__(
        self,
        token: str,
        locked_hwnd: int,
        locked_title: str,
        fps: float,
        monitor: int,
        max_width: int,
        jpeg_quality: int,
        ring_size: int,
    ) -> None:
        self.token = token
        self.locked_hwnd = locked_hwnd
        self.locked_title = locked_title
        self.fps = fps
        self.monitor = monitor
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.frames: deque[Frame] = deque(maxlen=ring_size)
        self.frames_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.command_results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.command_count = 0
        self.started_ms = now_ms()
        self.stop_event = threading.Event()
        self.capture_error = ""

    def add_frame(self, frame: Frame) -> None:
        with self.frames_lock:
            self.frames.append(frame)

    def latest(self) -> Frame | None:
        with self.frames_lock:
            return self.frames[-1] if self.frames else None

    def recent(self, count: int) -> list[Frame]:
        with self.frames_lock:
            return list(self.frames)[-count:]

    def remember_command(self, nonce: str, result: dict[str, Any]) -> None:
        with self.command_lock:
            self.command_results[nonce] = result
            self.command_results.move_to_end(nonce)
            while len(self.command_results) > 100:
                self.command_results.popitem(last=False)

    def prior_command(self, nonce: str) -> dict[str, Any] | None:
        with self.command_lock:
            return self.command_results.get(nonce)


def encode_jpeg(image: Any, max_width: int, quality: int) -> tuple[bytes, int, int]:
    from PIL import Image

    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize(
            (max_width, max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), image.width, image.height


def capture_loop(state: BrokerState) -> None:
    try:
        import mss
        from PIL import Image

        interval = 1.0 / state.fps
        frame_id = 0
        with mss.mss() as grabber:
            if state.monitor < 1 or state.monitor >= len(grabber.monitors):
                raise RuntimeError(
                    f"Monitor {state.monitor} is unavailable; detected {len(grabber.monitors) - 1} monitor(s)"
                )
            region = grabber.monitors[state.monitor]
            while not state.stop_event.is_set():
                started = time.perf_counter()
                shot = grabber.grab(region)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                jpeg, width, height = encode_jpeg(
                    image, state.max_width, state.jpeg_quality
                )
                frame_id += 1
                state.add_frame(Frame(frame_id, now_ms(), jpeg, width, height))
                delay = interval - (time.perf_counter() - started)
                if delay > 0:
                    state.stop_event.wait(delay)
    except Exception as exc:  # noqa: BLE001
        state.capture_error = f"{type(exc).__name__}: {exc}"
        state.stop_event.set()


def make_burst(frames: list[Frame]) -> bytes:
    from PIL import Image, ImageDraw

    if not frames:
        raise RuntimeError("No frames available")
    decoded = [Image.open(io.BytesIO(frame.jpeg)).convert("RGB") for frame in frames]
    cell_w = max(image.width for image in decoded)
    cell_h = max(image.height for image in decoded)
    cols = min(3, len(decoded))
    rows = (len(decoded) + cols - 1) // cols
    label_h = 28
    canvas = Image.new("RGB", (cell_w * cols, (cell_h + label_h) * rows), "black")
    draw = ImageDraw.Draw(canvas)
    newest = frames[-1].captured_ms
    for index, (frame, image) in enumerate(zip(frames, decoded, strict=True)):
        x = (index % cols) * cell_w
        y = (index // cols) * (cell_h + label_h)
        canvas.paste(image, (x, y))
        delta = newest - frame.captured_ms
        draw.text((x + 8, y + cell_h + 6), f"frame={frame.frame_id}  -{delta} ms", fill="white")
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=72, optimize=True)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "MiruFrameBroker/0.1"

    @property
    def state(self) -> BrokerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Intentionally suppress request logging because the secret token is in the path.
        return

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _bytes(self, status: int, content_type: str, data: bytes) -> None:
        self._headers(status, content_type, len(data))
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "s" or parts[1] != self.state.token:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        action = parts[2]
        if action == "status.json" and len(parts) == 3:
            self._status()
            return
        if action == "latest.jpg" and len(parts) == 3:
            frame = self.state.latest()
            if frame is None:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "no_frame"})
                return
            self._bytes(HTTPStatus.OK, "image/jpeg", frame.jpeg)
            return
        if action == "burst.jpg" and len(parts) == 3:
            try:
                count = int(parse_qs(parsed.query).get("n", ["6"])[0])
            except ValueError:
                count = 6
            count = max(2, min(count, 9))
            frames = self.state.recent(count)
            if not frames:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "no_frame"})
                return
            self._bytes(HTTPStatus.OK, "image/jpeg", make_burst(frames))
            return
        if action == "cmd":
            self._command(parts, parsed.query)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _status(self) -> None:
        frame = self.state.latest()
        hwnd, title = foreground_window()
        payload = {
            "ok": True,
            "version": VERSION,
            "server_time_unix_ms": now_ms(),
            "uptime_ms": now_ms() - self.state.started_ms,
            "capture": {
                "fps_configured": self.state.fps,
                "monitor": self.state.monitor,
                "frame_id": frame.frame_id if frame else 0,
                "captured_unix_ms": frame.captured_ms if frame else 0,
                "age_ms": now_ms() - frame.captured_ms if frame else None,
                "width": frame.width if frame else 0,
                "height": frame.height if frame else 0,
                "ring_frames": len(self.state.recent(1000)),
                "error": self.state.capture_error,
            },
            "input": {
                "armed": True,
                "locked_hwnd": self.state.locked_hwnd,
                "locked_title": self.state.locked_title,
                "foreground_hwnd": hwnd,
                "foreground_title": title,
                "foreground_matches_lock": hwnd == self.state.locked_hwnd,
                "command_count": self.state.command_count,
                "allowed_keys": sorted(ALLOWED_KEYS),
            },
        }
        self._json(HTTPStatus.OK, payload)

    def _command(self, parts: list[str], query: str) -> None:
        # /s/<token>/cmd/<nonce>/press/<key>?ms=70
        # /s/<token>/cmd/<nonce>/type-test
        if len(parts) < 5:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_command_path"})
            return
        nonce = parts[3]
        if not NONCE_RE.fullmatch(nonce):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_nonce"})
            return
        prior = self.state.prior_command(nonce)
        if prior is not None:
            duplicate = dict(prior)
            duplicate["duplicate"] = True
            self._json(HTTPStatus.OK, duplicate)
            return

        hwnd, title = foreground_window()
        if hwnd != self.state.locked_hwnd:
            result = {
                "ok": False,
                "executed": False,
                "error": "target_not_foreground",
                "locked_hwnd": self.state.locked_hwnd,
                "foreground_hwnd": hwnd,
                "foreground_title": title,
                "server_time_unix_ms": now_ms(),
            }
            self.state.remember_command(nonce, result)
            self._json(HTTPStatus.CONFLICT, result)
            return

        try:
            command = parts[4]
            if command == "press" and len(parts) == 6:
                key = parts[5].upper()
                if key not in ALLOWED_KEYS:
                    raise ValueError("key_not_allowlisted")
                try:
                    duration_ms = int(parse_qs(query).get("ms", ["70"])[0])
                except ValueError:
                    duration_ms = 70
                duration_ms = max(20, min(duration_ms, 1000))
                tap_key(key, duration_ms)
                detail = {"command": "press", "key": key, "duration_ms": duration_ms}
            elif command == "type-test" and len(parts) == 5:
                type_fixed_test()
                detail = {"command": "type-test", "text": "MIRUTEST1"}
            else:
                raise ValueError("unknown_command")
            self.state.command_count += 1
            latest = self.state.latest()
            result = {
                "ok": True,
                "executed": True,
                "duplicate": False,
                "nonce": nonce,
                "detail": detail,
                "executed_unix_ms": now_ms(),
                "frame_id_before": latest.frame_id if latest else 0,
                "foreground_title": title,
            }
            self.state.remember_command(nonce, result)
            self._json(HTTPStatus.OK, result)
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "executed": False,
                "error": str(exc),
                "server_time_unix_ms": now_ms(),
            }
            self.state.remember_command(nonce, result)
            self._json(HTTPStatus.BAD_REQUEST, result)


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: BrokerState) -> None:
        super().__init__(address, Handler)
        self.state = state


def start_cloudflared(executable: Path, port: int, token: str) -> subprocess.Popen[str]:
    command = [
        str(executable), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def reader() -> None:
        assert process.stdout is not None
        shown = False
        for line in process.stdout:
            match = TUNNEL_RE.search(line)
            if match and not shown:
                shown = True
                root = match.group(0)
                print("\n=== COPY THIS LINE TO MIRU ===", flush=True)
                print(f"MIRU_BROKER_URL={root}/s/{token}", flush=True)
                print("==============================\n", flush=True)

    threading.Thread(target=reader, name="cloudflared-output", daemon=True).start()
    return process


def arm_target(target_title: str | None) -> tuple[int, str]:
    if target_title:
        print(f"Waiting up to 30 seconds for a foreground window containing: {target_title!r}")
        deadline = time.time() + 30
        while time.time() < deadline:
            hwnd, title = foreground_window()
            if target_title.casefold() in title.casefold():
                return hwnd, title
            time.sleep(0.25)
        raise RuntimeError(f"No foreground target matched {target_title!r}")

    print("\nTARGET LOCK")
    print("1) Press Enter here.")
    print("2) During the 5-second countdown, switch to the exact test window (Notepad is recommended).")
    input("Press Enter to begin target locking... ")
    for remaining in range(5, 0, -1):
        print(f"Locking in {remaining}...", flush=True)
        time.sleep(1)
    hwnd, title = foreground_window()
    if not hwnd or not title:
        raise RuntimeError("Could not lock a foreground window")
    return hwnd, title


def self_test() -> int:
    token = secrets.token_urlsafe(32)
    assert TOKEN_RE.fullmatch(token)
    assert "LEFT" in ALLOWED_KEYS and "F12" in ALLOWED_KEYS
    assert "ALT+F4" not in ALLOWED_KEYS
    assert NONCE_RE.fullmatch("testnonce123")
    assert not NONCE_RE.fullmatch("x")
    state = BrokerState(token, 123, "test", 5.0, 1, 1280, 70, 12)
    state.remember_command("testnonce123", {"ok": True})
    assert state.prior_command("testnonce123") == {"ok": True}
    print(json.dumps({"ok": True, "version": VERSION, "self_test": "PASS"}))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MIRU RAM-only PC frame broker")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--max-width", type=int, default=1280)
    parser.add_argument("--jpeg-quality", type=int, default=70)
    parser.add_argument("--ring-size", type=int, default=12)
    parser.add_argument("--target-title")
    parser.add_argument("--cloudflared", type=Path)
    parser.add_argument("--token")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return self_test()
    if os.name != "nt":
        print("This prototype must run on Windows.", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        raise SystemExit("Invalid port")
    if not 1.0 <= args.fps <= 30.0:
        raise SystemExit("fps must be between 1 and 30 for this feasibility build")

    token = args.token or secrets.token_urlsafe(32)
    if not TOKEN_RE.fullmatch(token):
        raise SystemExit("Token must be URL-safe and at least 32 characters")

    locked_hwnd, locked_title = arm_target(args.target_title)
    print(f"Locked input target HWND={locked_hwnd} title={locked_title!r}")
    state = BrokerState(
        token=token,
        locked_hwnd=locked_hwnd,
        locked_title=locked_title,
        fps=args.fps,
        monitor=args.monitor,
        max_width=args.max_width,
        jpeg_quality=max(40, min(args.jpeg_quality, 90)),
        ring_size=max(6, min(args.ring_size, 30)),
    )
    server = BrokerServer(("127.0.0.1", args.port), state)
    capture_thread = threading.Thread(target=capture_loop, args=(state,), name="capture", daemon=True)
    server_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    capture_thread.start()
    server_thread.start()

    print(f"Local status: http://127.0.0.1:{args.port}/s/{token}/status.json")
    print("Frames remain in process RAM only. Press Ctrl+C to stop and invalidate the URL.")

    cloudflared: subprocess.Popen[str] | None = None
    if args.cloudflared:
        if not args.cloudflared.exists():
            raise SystemExit(f"cloudflared not found: {args.cloudflared}")
        cloudflared = start_cloudflared(args.cloudflared, args.port, token)
    else:
        print(f"MIRU_BROKER_URL=http://127.0.0.1:{args.port}/s/{token}")

    def stop_handler(*_: Any) -> None:
        state.stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        while not state.stop_event.wait(0.25):
            pass
    finally:
        server.shutdown()
        server.server_close()
        if cloudflared and cloudflared.poll() is None:
            cloudflared.terminate()
            try:
                cloudflared.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cloudflared.kill()
        print("MIRU frame broker stopped. The session token is no longer usable.")
    return 0 if not state.capture_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
