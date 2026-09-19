"""End-to-end regression probe for gb-recompiled's keyboard binding conflicts.

The bug this pins: runtime_prefs.ini keeps TWO keyboard slots per action and the
shipped defaults fill slot 1 with WASD/JK, so `keyboard.left.1 = key:4` is the A
key. A player who rebound the Game Boy A button to the keyboard A key (through
the launcher or the in-game menu, both of which only ever wrote slot 0) ended up
with A driving BOTH the A button and LEFT: pressing A to jump also walked Mario
left. `$FF80` read 0x21 (A | Left) instead of 0x01.

What is verified, through the real window and the real Windows keyboard stack:

  * the playtester's prefs (a.0 = A key, b.0 = Z) produce 0x01 for the A key --
    never 0x21 -- because the conflicting `left.1` is dropped when the file is
    loaded,
  * the same prefs file on disk is SANITISED by that load (left.1 = none),
  * the untouched defaults still work: the Right arrow gives 0x10, and so does
    the D key (right.1), i.e. the conflict rule did not cost the secondaries,
  * the recomp-ui launcher's keybinds page -- the other rebinding UI, and the
    one that created the bad file -- now draws BOTH slots and takes a key away
    from whatever else held it when a binding is committed.

Keys are injected with user32 keybd_event using hardware scancodes against the
focused game window -- not through the debug server's `press` command, which
bypasses the whole binding layer and would pass even with the bug present. The
debug server is used only to READ $FF80 (A=01 B=02 Select=04 Start=08 Right=10
Left=20 Up=40 Down=80), which the game writes every frame from its own joypad
poll. Requires a native Windows Python.

    python tools/probe_keybinds.py
"""
from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tcp import Debug  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "generated/build/Super_Mario_Land_2.exe"
SANDBOX = ROOT / "logs/keybinds-probe"

# $FF80, the game's own held-buttons byte.
HELD = 0xFF80
BIT = {"A": 0x01, "B": 0x02, "SELECT": 0x04, "START": 0x08,
       "RIGHT": 0x10, "LEFT": 0x20, "UP": 0x40, "DOWN": 0x80}

# SDL scancodes (what runtime_prefs.ini stores) for the keys this probe uses.
SDL_A, SDL_D, SDL_Z, SDL_RIGHT = 4, 7, 29, 79

# ...and the PS/2 set-1 hardware scancodes keybd_event wants for them.
# `extended` is the 0xE0 prefix the arrow cluster needs (the numeric keypad
# carries the same base codes without it).
HW = {
    SDL_A:     (0x1E, False),
    SDL_D:     (0x20, False),
    SDL_Z:     (0x2C, False),
    SDL_RIGHT: (0x4D, True),
}

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

user32 = ctypes.WinDLL("user32", use_last_error=True)


# ── window focus ────────────────────────────────────────────────────────────
def _find_window(pid: int, deadline: float):
    """The first visible top-level window owned by `pid`."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    while time.monotonic() < deadline:
        found.clear()
        user32.EnumWindows(visit, 0)
        if found:
            return found[0]
        time.sleep(0.05)
    raise RuntimeError(f"no visible window for pid {pid}")


def _focus(hwnd, deadline: float):
    """Raise `hwnd` to the foreground so SDL sees keyboard events at all.

    SetForegroundWindow is refused for a process that does not already own the
    foreground, so the caller's input queue is attached to the foreground
    thread's first -- the documented way to get the grant.
    """
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    while time.monotonic() < deadline:
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_thread and fg_thread != target_thread and
                        user32.AttachThreadInput(fg_thread, target_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        if attached:
            user32.AttachThreadInput(fg_thread, target_thread, False)
        time.sleep(0.1)
    raise RuntimeError("could not bring the game window to the foreground; "
                       "a real interactive desktop session is required")


# ── key injection ───────────────────────────────────────────────────────────
def _key(sdl_scancode: int, down: bool):
    scan, extended = HW[sdl_scancode]
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    user32.keybd_event(0, scan, flags, 0)


class Game:
    """One sandboxed run of the game: own folder, own prefs, own debug port."""

    def __init__(self, name: str, prefs: str):
        self.folder = SANDBOX / name
        if self.folder.exists():
            shutil.rmtree(self.folder, ignore_errors=True)
        (self.folder / "logs").mkdir(parents=True, exist_ok=True)
        self.exe = self.folder / EXE.name
        shutil.copy2(EXE, self.exe)
        assets = EXE.parent / "assets"
        if assets.exists():
            shutil.copytree(assets, self.folder / "assets", dirs_exist_ok=True)
        patch = EXE.parent / "sml2dx_v181.bps"
        if patch.exists():
            shutil.copy2(patch, self.folder / patch.name)
        # rom.cfg is read from the exe's directory, so it belongs in the sandbox.
        rom = next((ROOT / "roms").glob("*.gb"))
        (self.folder / "rom.cfg").write_text(str(rom))
        self.prefs_path = self.folder / "runtime_prefs.ini"
        self.prefs_path.write_text(prefs)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        env = os.environ.copy()
        env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
        for key in list(env):
            if key.startswith(("SML2_", "LNG_")):
                env.pop(key)
        env.update(GBRECOMP_DEBUG_PORT=str(self.port), GBRECOMP_NO_LAUNCHER="1")
        self.log = open(self.folder / "process.log", "wb")
        # A REAL window: --benchmark forces the dummy video driver, and a
        # dummy window never receives a keystroke.
        self.process = subprocess.Popen(
            [str(self.exe), "--log-file", "logs/run.log"],
            cwd=self.folder, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        self.debug = Debug(port=self.port, timeout=60)
        self.hwnd = _find_window(self.process.pid, deadline)
        _focus(self.hwnd, deadline)

    def held(self) -> int:
        return self.debug.read_ram(HELD, 1)[0]

    def tap(self, sdl_scancode: int, hold_s: float = 0.4) -> int:
        """Hold one key, sample $FF80 while it is down, release, return the byte."""
        _focus(self.hwnd, time.monotonic() + 10)
        _key(sdl_scancode, True)
        try:
            # Sample repeatedly and keep the strongest reading: the game clears
            # $FF80 between polls on some frames, and a single sample could land
            # in that gap and read 0 whether or not the binding works.
            time.sleep(0.15)
            seen = 0
            for _ in range(12):
                seen |= self.held()
                time.sleep(0.03)
            return seen
        finally:
            _key(sdl_scancode, False)
            time.sleep(0.1)

    def close(self):
        try:
            self.debug.quit()
        except Exception:
            pass
        self.debug.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


# ── prefs files ─────────────────────────────────────────────────────────────
# gb-recompiled's set_default_input_bindings(), as save_runtime_preferences()
# writes it. Slot 1 of the four directions and A/B is the WASD/JK secondary that
# made the reported conflict possible.
DEFAULT_BINDS = {
    "right": (79, 7), "left": (80, 4), "up": (82, 26), "down": (81, 22),
    "a": (29, 13), "b": (27, 14), "select": (42, 229), "start": (40, None),
    "fast_forward": (43, None), "toggle_max_speed": (53, None),
    "save_state": (62, None), "load_state": (65, None),
    "previous_state_slot": (63, None), "next_state_slot": (64, None),
    "toggle_overlay": (58, None), "toggle_mute": (16, None),
    "toggle_menu": (67, None),
}


def prefs(**overrides) -> str:
    """Render a runtime_prefs.ini. `a0=4` overrides keyboard.a.0 to key:4."""
    lines = ["audio.enabled=1", "audio.muted=1", "launcher.skip=1"]
    for action, slots in DEFAULT_BINDS.items():
        for slot, code in enumerate(slots):
            code = overrides.get(f"{action}{slot}", code)
            lines.append(f"keyboard.{action}.{slot}="
                         + ("none" if code is None else f"key:{code}"))
    return "\n".join(lines) + "\n"


def binding_of(text: str, action: str, slot: int) -> str:
    want = f"keyboard.{action}.{slot}="
    for line in text.splitlines():
        if line.startswith(want):
            return line[len(want):]
    return "<missing>"


# ── cases ───────────────────────────────────────────────────────────────────
def main() -> int:
    if not EXE.exists():
        print(f"FAIL  build it first: {EXE}")
        return 2
    SANDBOX.mkdir(parents=True, exist_ok=True)
    failures = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
        if not ok:
            failures.append(label)

    # 1. The playtester's file: Game Boy A on the keyboard A key, B on Z, with
    #    the stock WASD/JK secondaries still present -- so left.1 is ALSO the A
    #    key. Before the fix this read 0x21 (A | Left).
    print("case 1: rebound a.0=key:4 (A), b.0=key:29 (Z), stock secondaries")
    game = Game("rebound", prefs(a0=SDL_A, b0=SDL_Z))
    try:
        held = game.tap(SDL_A)
        check("A key -> $FF80", f"0x{held:02x}", f"0x{BIT['A']:02x}")
        check("A key does not also drive Left", bool(held & BIT["LEFT"]), False)
        held = game.tap(SDL_Z)
        check("Z key -> $FF80", f"0x{held:02x}", f"0x{BIT['B']:02x}")
    finally:
        game.close()
    # The load sanitises the file itself, so the stale duplicate is gone for
    # every later reader (including the launcher's keybinds page).
    saved = game.prefs_path.read_text()
    check("sanitised keyboard.left.1", binding_of(saved, "left", 1), "none")
    check("kept keyboard.a.0", binding_of(saved, "a", 0), f"key:{SDL_A}")
    check("untouched keyboard.right.1", binding_of(saved, "right", 1), f"key:{SDL_D}")

    # 2. Stock defaults: the conflict rule must not cost the secondaries.
    print("case 2: stock defaults")
    game = Game("defaults", prefs())
    try:
        held = game.tap(SDL_RIGHT)
        check("Right arrow -> $FF80", f"0x{held:02x}", f"0x{BIT['RIGHT']:02x}")
        held = game.tap(SDL_D)
        check("D key (right.1) -> $FF80", f"0x{held:02x}", f"0x{BIT['RIGHT']:02x}")
        held = game.tap(SDL_Z)
        check("Z key (a.0) -> $FF80", f"0x{held:02x}", f"0x{BIT['A']:02x}")
    finally:
        game.close()
    check("defaults left keyboard.left.1 alone",
          binding_of(game.prefs_path.read_text(), "left", 1), f"key:{SDL_A}")

    # 3. The other rebinding UI: the recomp-ui launcher's keybinds page. It used
    #    to write slot 0 only and never look at slot 1, which is how a player
    #    created the case-1 file in the first place.
    print("case 3: launcher keybinds page")
    for label, got, want in launcher_case():
        check(label, got, want)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


# The launcher's CONTROLLER view at a pinned 1100x880: the LEFT row's PRIMARY
# chip. Read off logs/keybinds-probe/launcher/before.png, which this case writes
# every run — if the layout moves, that PNG shows where the click landed.
LAUNCHER_SIZE = (1100, 880)
LEFT_PRIMARY_CHIP = (848, 364)


def launcher_case():
    """Rebind LEFT to D through the real launcher page; report (label, got, want)."""
    folder = SANDBOX / "launcher"
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EXE, folder / EXE.name)
    assets = EXE.parent / "assets"
    if assets.exists():
        shutil.copytree(assets, folder / "assets", dirs_exist_ok=True)
    rom = next((ROOT / "roms").glob("*.gb"))
    (folder / "rom.cfg").write_text(str(rom))
    ini = folder / "runtime_prefs.ini"
    ini.write_text(prefs())

    env = os.environ.copy()
    env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
    for key in list(env):
        if key.startswith(("SML2_", "LNG_")):
            env.pop(key)
    w, h = LAUNCHER_SIZE
    x, y = LEFT_PRIMARY_CHIP
    env.update(
        GBRECOMP_LAUNCHER="1", GBRECOMP_NO_LAUNCHER="0",
        LNG_SCRIPT=f"size:{w}x{h};wait:10;view:controller;wait:10;shot:before.png;"
                   f"wait:5;click:{x},{y};wait:600;shot:after.png;wait:5;quit")
    log = open(folder / "process.log", "wb")
    proc = subprocess.Popen([str(folder / EXE.name)], cwd=folder, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        hwnd = _find_window(proc.pid, deadline)
        _focus(hwnd, deadline)
        time.sleep(2.5)          # let the script reach the click and arm capture
        _focus(hwnd, time.monotonic() + 10)
        _key(SDL_D, True)
        time.sleep(0.05)
        _key(SDL_D, False)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()

    saved = ini.read_text()
    return [
        ("launcher wrote the captured key to keyboard.left.0",
         binding_of(saved, "left", 0), f"key:{SDL_D}"),
        ("launcher cleared the conflicting keyboard.right.1",
         binding_of(saved, "right", 1), "none"),
        ("launcher left keyboard.a.1 alone",
         binding_of(saved, "a", 1), "key:13"),
        (f"launcher page screenshots written ({folder / 'after.png'})",
         (folder / "before.png").exists() and (folder / "after.png").exists(), True),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
