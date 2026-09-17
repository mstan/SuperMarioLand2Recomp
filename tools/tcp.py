"""Reusable client for the gb-recompiled TCP debug server.

One place that knows the wire protocol (JSON objects, one per line, on
127.0.0.1:4370) so probes and one-off sessions stop re-deriving it. The
command surface it wraps is documented in gb-recompiled/docs/DEBUG_SERVER.md.

Typical use:

    from tcp import Debug

    with Debug(port=4370) as d:
        d.pause()
        d.load_state(path="F:/.../dx_pause_pipe_repro.state1")
        d.run_to(d.frame() + 4)
        d.screenshot("logs/before.png")
        d.press("S")                 # Start, one frame
        d.run_to(d.frame() + 30)
        d.screenshot("logs/after.png")

The class does not launch the game; point it at a runner that is already up
(Launch.ps1, or subprocess it yourself) and it will retry the connect for
`timeout` seconds while the window comes up.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

DEFAULT_PORT = 4370

# Button letters, matching the runtime's own spelling (platform_sdl.cpp
# parse_buttons()/write_buttons() and the --input script route).
BUTTONS = {
    "R": 0x01,  # dpad right
    "L": 0x02,  # dpad left
    "U": 0x04,  # dpad up
    "D": 0x08,  # dpad down
    "A": 0x10,
    "B": 0x20,
    "T": 0x40,  # selecT
    "S": 0x80,  # Start
}


class DebugError(RuntimeError):
    """A command came back with ok:false."""


class Debug:
    """Line-oriented JSON client for one debug-server connection."""

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 timeout: float = 60.0, cwd: Path | str | None = None):
        self.port = port
        self.host = host
        # Where relative paths sent to the server land: the server resolves
        # them against the runner's working directory, which is the exe folder.
        self.cwd = Path(cwd) if cwd else None
        self._id = 0
        self._sock = None
        self._file = None
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                self._sock = socket.create_connection((host, port), timeout=5.0)
                break
            except OSError as exc:            # runner not listening yet
                last = exc
                time.sleep(0.25)
        if self._sock is None:
            raise DebugError(f"no debug server on {host}:{port}: {last}")
        self._sock.settimeout(120.0)
        self._file = self._sock.makefile("r", encoding="utf-8", newline="\n")

    # ---- plumbing -------------------------------------------------------

    def cmd(self, name: str, **args):
        """Send one command, return its reply object.

        Replies and asynchronous events share the stream, so this skips lines
        until it sees the matching `id`. Events seen on the way are dropped;
        use `event()` when you are waiting for one.
        """
        self._id += 1
        wanted = self._id
        payload = dict(cmd=name, id=wanted, **args)
        self._sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        while True:
            line = self._file.readline()
            if not line:
                raise DebugError(f"connection closed waiting for {name}")
            obj = json.loads(line)
            if obj.get("id") == wanted:
                if obj.get("ok") is False:
                    raise DebugError(f"{name}: {obj.get('error')} ({obj})")
                return obj

    def stream(self, name: str, done=None, **args):
        """Send a command whose reply is several lines; return them in order.

        Some commands (`dump_ram`, `peek`, and game commands such as
        `sml2_gate_log`) answer with many lines carrying the same `id`. By
        default this stops on a line with `"end":true`, or on a chunked read
        whose `offset + len` has covered `total`. Pass `done(obj) -> bool` for
        anything else (`dump_ram` sends no terminator: stop on your own count).
        """
        if done is None:
            def done(obj):
                if obj.get("end"):
                    return True
                if {"total", "offset", "len"} <= obj.keys():
                    return obj["offset"] + obj["len"] >= obj["total"]
                return False

        self._id += 1
        wanted = self._id
        self._sock.sendall(
            (json.dumps(dict(cmd=name, id=wanted, **args)) + "\n").encode("utf-8"))
        out = []
        while True:
            line = self._file.readline()
            if not line:
                raise DebugError(f"connection closed during {name}")
            obj = json.loads(line)
            if obj.get("id") != wanted:
                continue
            if obj.get("ok") is False:
                raise DebugError(f"{name}: {obj.get('error')} ({obj})")
            out.append(obj)
            if done(obj):
                return out

    def event(self, name: str):
        """Block until the named asynchronous event line arrives."""
        while True:
            line = self._file.readline()
            if not line:
                raise DebugError(f"connection closed waiting for event {name}")
            obj = json.loads(line)
            if obj.get("event") == name:
                return obj

    # ---- execution control ---------------------------------------------

    def ping(self):
        return self.cmd("ping")

    def frame(self) -> int:
        return self.cmd("frame")["frame"]

    def pause(self):
        return self.cmd("pause")

    def resume(self):
        return self.cmd("continue")

    def step(self, count: int = 1):
        self.cmd("step", count=count)
        return self.event("step_done")

    def run_to(self, frame: int):
        """Resume and pause again at `frame` (absolute frame number)."""
        self.cmd("run_to_frame", frame=frame)
        return self.event("run_to_done")

    def advance(self, frames: int):
        """run_to relative to now."""
        return self.run_to(self.frame() + frames)

    # ---- input ----------------------------------------------------------

    @staticmethod
    def mask(buttons) -> int:
        """Letters ('AB') or an int mask -> int mask."""
        if isinstance(buttons, int):
            return buttons
        return sum(BUTTONS[c] for c in buttons.upper() if c != "-")

    def press(self, buttons, frames: int = 1, settle: int = 0):
        """Hold `buttons` for `frames` guest frames, then release.

        The countdown runs in the runtime's per-frame record hook, so it only
        advances while the game is running: call this, then run_to/step. With
        `settle` > 0 this advances that many frames afterwards for you.
        """
        r = self.cmd("press", buttons=buttons if isinstance(buttons, str)
                     else f"0x{buttons:02x}", frames=frames)
        if settle:
            self.advance(frames + settle)
        return r

    def hold(self, buttons):
        return self.cmd("hold", buttons=buttons if isinstance(buttons, str)
                        else f"0x{buttons:02x}")

    def release(self, buttons=None):
        if buttons is None:
            return self.cmd("clear_input")
        return self.cmd("release", buttons=buttons if isinstance(buttons, str)
                        else f"0x{buttons:02x}")

    def set_input(self, buttons):
        return self.cmd("set_input", buttons=buttons if isinstance(buttons, str)
                        else f"0x{buttons:02x}")

    # ---- state ----------------------------------------------------------

    @staticmethod
    def _wire_path(path) -> str:
        # The server's JSON parser does not process escapes, so backslashes
        # would arrive verbatim and break on some APIs. Forward slashes work
        # everywhere on Windows.
        return str(path).replace("\\", "/")

    def save_state(self, path=None, slot: int | None = None):
        if slot is not None:
            return self.cmd("save_state", slot=slot)
        return self.cmd("save_state", path=self._wire_path(path))

    def load_state(self, path=None, slot: int | None = None):
        if slot is not None:
            return self.cmd("load_state", slot=slot)
        return self.cmd("load_state", path=self._wire_path(path))

    def slot_path(self, slot: int = 0):
        return self.cmd("save_slot_path", slot=slot)

    # ---- capture --------------------------------------------------------

    def screenshot(self, path, recompose: bool = False):
        """Write the presented frame. A .png suffix gets PNG, else PPM.

        `recompose` re-runs the custom compositor instead of reusing the last
        presented frame; the default answers "what is on screen".
        """
        args = {"path": self._wire_path(path)}
        if recompose:
            args["recompose"] = 1
        return self.cmd("screenshot", **args)

    # ---- memory ---------------------------------------------------------

    def read_ram(self, addr: int, length: int) -> bytes:
        out = b""
        while length:
            n = min(length, 256)
            out += bytes.fromhex(
                self.cmd("read_ram", addr=f"{addr:04x}", len=n)["hex"])
            addr += n
            length -= n
        return out

    # ---- teardown -------------------------------------------------------

    def quit(self):
        try:
            self.cmd("quit")
        except (DebugError, OSError):
            pass

    def close(self):
        for handle in (self._file, self._sock):
            try:
                if handle:
                    handle.close()
            except OSError:
                pass
        self._file = self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def ppm_to_png(ppm_path, png_path) -> None:
    """Convert a P6 PPM to PNG with a stored (uncompressed) zlib stream.

    Kept here so a probe that captured a .ppm can hand a .png to a human
    without pulling in Pillow. Prefer asking for a .png from `screenshot`
    directly -- the runtime writes a real compressed PNG via stb_image_write.
    """
    import struct
    import zlib

    data = Path(ppm_path).read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError(f"{ppm_path} is not a P6 PPM")
    fields, pos = [], 2
    while len(fields) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while data[pos:pos + 1] not in (b"\n", b""):
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1
    width, height, _maxval = fields
    pixels = data[pos:pos + width * height * 3]

    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    Path(png_path).write_bytes(png)
