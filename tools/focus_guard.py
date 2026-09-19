"""Run a command and fail it if anything ever steals the desktop foreground.

This is the gate on "headless" meaning what it says. It parks a sentinel window
(Notepad) in the foreground, samples GetForegroundWindow() every 10 ms for the
whole run, and reports every distinct window that took over -- with the name of
the process that owns it, so a culprit is named rather than guessed at.

    python tools/focus_guard.py python tools/probe_keybinds.py
    python tools/focus_guard.py python tools/probe_mods.py

Exit code: the command's own, unless the foreground moved, which is 3.

The cursor is sampled too, because SDL_WarpMouseInWindow drags the real pointer
and a probe that does that is still hijacking the screen. That reading is
ADVISORY: a human moving the mouse during the run is indistinguishable from a
warp, so it is reported, never failed on.

Requires a native Windows Python (ctypes.WinDLL).
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Without this the scheduler's default 15.6 ms tick sets the floor on how
# often the watcher can look, and a focus steal shorter than that could slip
# through unseen. 1 ms resolution makes the sampling gap a measured number
# rather than a hope -- the report prints the worst one.
try:
    ctypes.WinDLL("winmm").timeBeginPeriod(1)
except OSError:
    pass
user32.FindWindowW.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SAMPLE_SECONDS = 0.01


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def window_owner(hwnd) -> str:
    """'<pid> name.exe' for the process that owns `hwnd`, best effort."""
    if not hwnd:
        return "<none>"
    pid = wintypes.DWORD(window_pid(hwnd))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    name = "?"
    if handle:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = buf.value.rsplit("\\", 1)[-1]
        kernel32.CloseHandle(handle)
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(wintypes.HWND(hwnd), title, len(title))
    return f"{pid.value} {name} ({title.value!r})"


def park_sentinel():
    """Start Notepad and hold the foreground with it. Returns (proc, hwnd)."""
    proc = subprocess.Popen(["notepad.exe"])
    hwnd = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW("Notepad", None)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        raise RuntimeError("no Notepad window to park the foreground on")
    while time.monotonic() < deadline:
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        user32.SetActiveWindow(wintypes.HWND(hwnd))
        if user32.GetForegroundWindow() == hwnd:
            return proc, hwnd
        time.sleep(0.1)
    raise RuntimeError("could not park the foreground on Notepad")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    command = sys.argv[1:]

    sentinel, hwnd = park_sentinel()
    # Notepad owns more than one top-level window, and the shell hands the
    # foreground between them. Identity is the OWNING PROCESS, not the hwnd.
    sentinel_pid = window_pid(hwnd)
    start_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(start_cursor))
    print(f"foreground parked on {window_owner(hwnd)}")
    print(f"cursor at ({start_cursor.x}, {start_cursor.y})")
    print(f"running: {' '.join(command)}\n", flush=True)

    stop = threading.Event()
    thieves: dict[int, str] = {}
    samples = [0]
    worst_gap = [0.0]
    cursor_moves = [0]
    cursor_last = (start_cursor.x, start_cursor.y)

    def watch():
        nonlocal cursor_last
        previous = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            worst_gap[0] = max(worst_gap[0], now - previous)
            previous = now
            samples[0] += 1
            fg = user32.GetForegroundWindow()
            # 0 shows up for a few ms whenever focus is in transition; only a
            # different REAL window counts as a theft.
            if fg and window_pid(fg) != sentinel_pid and fg not in thieves:
                thieves[fg] = window_owner(fg)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            if (point.x, point.y) != cursor_last:
                cursor_moves[0] += 1
                cursor_last = (point.x, point.y)
            time.sleep(SAMPLE_SECONDS)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    began = time.monotonic()
    rc = subprocess.call(command)
    elapsed = time.monotonic() - began
    stop.set()
    watcher.join()
    try:
        sentinel.terminate()
    except Exception:
        pass

    end = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(end))
    print(f"\n--- focus guard: {samples[0]} samples over {elapsed:.1f}s, worst gap {worst_gap[0] * 1000:.0f} ms ---")
    print(f"command exit code: {rc}")
    print(f"cursor: ({start_cursor.x}, {start_cursor.y}) -> ({end.x}, {end.y}), "
          f"{cursor_moves[0]} moves (advisory: a human moving the mouse counts too)")
    if thieves:
        print(f"FOREGROUND STOLEN by {len(thieves)} window(s):")
        for win, who in thieves.items():
            print(f"  hwnd {win}: {who}")
        return 3
    print("FOREGROUND NEVER CHANGED: it stayed on the sentinel for the whole run")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
