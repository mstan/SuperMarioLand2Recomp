"""End-to-end regression probe for gb-recompiled's keyboard binding conflicts.

The bug this pins: runtime_prefs.ini keeps TWO keyboard slots per action and the
shipped defaults fill slot 1 with WASD/JK, so `keyboard.left.1 = key:4` is the A
key. A player who rebound the Game Boy A button to the keyboard A key (through
the launcher or the in-game menu, both of which only ever wrote slot 0) ended up
with A driving BOTH the A button and LEFT: pressing A to jump also walked Mario
left. `$FF80` read 0x21 (A | Left) instead of 0x01.

What is verified:

  * the playtester's prefs (a.0 = A key, b.0 = Z) produce 0x01 for the A key --
    never 0x21 -- because the conflicting `left.1` is dropped when the file is
    loaded,
  * the same prefs file on disk is SANITISED by that load (left.1 = none),
  * the untouched defaults still work: the Right arrow gives 0x10, and so does
    the D key (right.1), i.e. the conflict rule did not cost the secondaries,
  * the recomp-ui launcher's keybinds page -- the other rebinding UI, and the
    one that created the bad file -- now draws BOTH slots and takes a key away
    from whatever else held it when a binding is committed.

HOW THE KEYS GET IN
-------------------
By default this probe is HEADLESS and never touches the desktop. The game runs
with GBRECOMP_HEADLESS=1 (no window, no GL, SDL_VIDEODRIVER=dummy) and keys
arrive over the debug server's `sdl_event` command, which pushes a real
SDL_KEYDOWN into the same queue `gb_platform_poll_events()` drains. The binding
capture, the two-slot tables, the conflict rule and the joypad mapping under
test are therefore byte-for-byte the code a physical keystroke runs -- unlike
the server's `press` command, which overrides the RESOLVED joypad mask and would
pass even with the bug present. The debug server is still used only to READ
$FF80 (A=01 B=02 Select=04 Start=08 Right=10 Left=20 Up=40 Down=80).

Because there is no wall clock in the loop, each sample is `pause` + `step`:
inject, step the guest, read $FF80, release.

`--headed` restores the original behaviour: a real window, brought to the
foreground with SetForegroundWindow, and keys pushed through the Windows
keyboard stack with user32 keybd_event. That path needs an interactive desktop
and TAKES FOCUS; it exists so the OS keyboard stack itself stays covered.

CASE 3 (the launcher keybinds page) IS HEADED-ONLY
--------------------------------------------------
The recomp-ui pre-boot launcher cannot run without a window: it needs a GL
context (`SDL_CreateWindow failed: OpenGL support is ... not available in
current SDL video driver (dummy)`) and it raises its own window unconditionally
(`SDL_RaiseWindow` in recomp-ui src/common/launcher_platform_sdl2.c), so any run
of it takes the foreground. Under `--headed` the case still runs, but it is
driven entirely over TCP through the engine's pre-boot debug listener -- a
synthetic click and a synthetic key, no user32 and no SetForegroundWindow from
this probe.

    python tools/probe_keybinds.py            # headless, cases 1-2
    python tools/probe_keybinds.py --headed   # adds case 3, takes focus

Requires a native Windows Python for --headed (ctypes.WinDLL).
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tcp import Debug, DebugError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "generated/build/Super_Mario_Land_2.exe"
SANDBOX = ROOT / "logs/keybinds-probe"

# $FF80, the game's own held-buttons byte.
HELD = 0xFF80
BIT = {"A": 0x01, "B": 0x02, "SELECT": 0x04, "START": 0x08,
       "RIGHT": 0x10, "LEFT": 0x20, "UP": 0x40, "DOWN": 0x80}

# SDL scancodes (what runtime_prefs.ini stores) for the keys this probe uses.
SDL_A, SDL_D, SDL_Z, SDL_RIGHT = 4, 7, 29, 79

# ...and the PS/2 set-1 hardware scancodes keybd_event wants for them, used only
# by --headed. `extended` is the 0xE0 prefix the arrow cluster needs (the
# numeric keypad carries the same base codes without it).
HW = {
    SDL_A:     (0x1E, False),
    SDL_D:     (0x20, False),
    SDL_Z:     (0x2C, False),
    SDL_RIGHT: (0x4D, True),
}

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

_user32 = None


def user32():
    """Loaded lazily: only --headed needs it, and only Windows has it."""
    global _user32
    if _user32 is None:
        import ctypes
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
    return _user32


# ── window focus (--headed only) ─────────────────────────────────────────────
def _find_window(pid: int, deadline: float):
    """The first visible top-level window owned by `pid`."""
    import ctypes
    from ctypes import wintypes
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32().GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32().IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    while time.monotonic() < deadline:
        found.clear()
        user32().EnumWindows(visit, 0)
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
    u = user32()
    u.ShowWindow(hwnd, 9)          # SW_RESTORE
    while time.monotonic() < deadline:
        fg = u.GetForegroundWindow()
        if fg == hwnd:
            return
        target_thread = u.GetWindowThreadProcessId(hwnd, None)
        fg_thread = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_thread and fg_thread != target_thread and
                        u.AttachThreadInput(fg_thread, target_thread, True))
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetActiveWindow(hwnd)
        if attached:
            u.AttachThreadInput(fg_thread, target_thread, False)
        time.sleep(0.1)
    raise RuntimeError("could not bring the game window to the foreground; "
                       "a real interactive desktop session is required")


def _key(sdl_scancode: int, down: bool):
    """--headed only: a key through the real Windows keyboard stack."""
    scan, extended = HW[sdl_scancode]
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    user32().keybd_event(0, scan, flags, 0)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def clean_env(**extra) -> dict:
    env = os.environ.copy()
    env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
    for key in list(env):
        if key.startswith(("SML2_", "LNG_")):
            env.pop(key)
    env.update(extra)
    return env


def stage(folder: Path, *, assets: bool = False, patch: bool = False) -> Path:
    """Copy the executable (and what it loads) into its own sandbox folder."""
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    exe = folder / EXE.name
    shutil.copy2(EXE, exe)
    # SDL2/ANGLE/libstdc++ live beside the build output; a sandbox that lacks
    # them silently fails to start.
    for dll in EXE.parent.glob("*.dll"):
        shutil.copy2(dll, folder / dll.name)
    if assets and (EXE.parent / "assets").exists():
        shutil.copytree(EXE.parent / "assets", folder / "assets", dirs_exist_ok=True)
    if patch and (EXE.parent / "sml2dx_v181.bps").exists():
        shutil.copy2(EXE.parent / "sml2dx_v181.bps", folder / "sml2dx_v181.bps")
    # rom.cfg is read from the exe's directory, so it belongs in the sandbox.
    rom = next((ROOT / "roms").glob("*V1.0*.gb"))
    (folder / "rom.cfg").write_text(str(rom))
    return exe


class Game:
    """One sandboxed run of the game: own folder, own prefs, own debug port."""

    def __init__(self, name: str, prefs: str, headed: bool = False):
        self.headed = headed
        self.folder = SANDBOX / name
        self.exe = stage(self.folder)
        self.prefs_path = self.folder / "runtime_prefs.ini"
        self.prefs_path.write_text(prefs)
        self.port = free_port()

        env = clean_env(GBRECOMP_DEBUG_PORT=str(self.port),
                        GBRECOMP_NO_LAUNCHER="1")
        if not headed:
            # No window, no GL, no audio device -- but the SDL event queue is
            # live and drained through the ordinary handler, which is what makes
            # an injected key indistinguishable from a real one.
            env["GBRECOMP_HEADLESS"] = "1"
        self.log = open(self.folder / "process.log", "wb")
        self.process = subprocess.Popen(
            [str(self.exe), "--log-file", "logs/run.log"],
            cwd=self.folder, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        self.debug = Debug(port=self.port, timeout=60)
        if headed:
            # A REAL window: keybd_event reaches the focused window only.
            self.hwnd = _find_window(self.process.pid, deadline)
            _focus(self.hwnd, deadline)
        else:
            # Deterministic from here: the guest only advances when we step it.
            self.debug.pause()
            self.debug.step(8)
            # Every tap restarts from this exact guest state. SML2's title
            # screen runs an attract DEMO that writes $FF80 itself (measured:
            # it starts around frame 1100 and holds Right), so a probe that
            # just kept stepping would read the demo's input as if it were the
            # key under test. Reloading one snapshot per tap removes the
            # question entirely -- and removes wall-clock timing with it.
            self.base_state = self.folder / "probe_base.state"
            self.debug.save_state(path=str(self.base_state))
            if self.held():
                raise RuntimeError(
                    "$FF80 is already 0x%02x at the snapshot frame; the attract "
                    "demo has started and the probe would measure it"
                    % self.held())

    def held(self) -> int:
        return self.debug.read_ram(HELD, 1)[0]

    def tap(self, sdl_scancode: int, hold_s: float = 0.4) -> int:
        """Hold one key, sample $FF80 while it is down, release, return the byte.

        Sample repeatedly and keep the strongest reading either way: the game
        clears $FF80 between polls on some frames, and a single sample could
        land in that gap and read 0 whether or not the binding works.
        """
        if self.headed:
            _focus(self.hwnd, time.monotonic() + 10)
            _key(sdl_scancode, True)
            try:
                time.sleep(0.15)
                seen = 0
                for _ in range(12):
                    seen |= self.held()
                    time.sleep(0.03)
                return seen
            finally:
                _key(sdl_scancode, False)
                time.sleep(0.1)

        # Headless: a real SDL_KEYDOWN, then step the guest frame by frame,
        # from a guest state identical to every other tap's.
        self.debug.load_state(path=str(self.base_state))
        self.debug.step(2)
        quiet = self.held()
        self.debug.key(sdl_scancode, down=True)
        try:
            seen = 0
            for _ in range(16):
                self.debug.step(2)
                seen |= self.held()
            # A non-zero reading before the key went down means something other
            # than this key is driving $FF80; say so rather than fold it in.
            return seen if not quiet else (seen | 0x100)
        finally:
            self.debug.key(sdl_scancode, down=False)
            self.debug.step(2)

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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true",
                    help="drive a real focused window through user32 instead of "
                         "the headless debug-server path, and run case 3")
    args = ap.parse_args()

    if not EXE.exists():
        print(f"FAIL  build it first: {EXE}")
        return 2
    SANDBOX.mkdir(parents=True, exist_ok=True)
    failures = []
    print("mode: " + ("HEADED (takes focus)" if args.headed
                      else "headless (no window, no focus)"))

    def check(label, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
        if not ok:
            failures.append(label)

    # 1. The playtester's file: Game Boy A on the keyboard A key, B on Z, with
    #    the stock WASD/JK secondaries still present -- so left.1 is ALSO the A
    #    key. Before the fix this read 0x21 (A | Left).
    print("case 1: rebound a.0=key:4 (A), b.0=key:29 (Z), stock secondaries")
    game = Game("rebound", prefs(a0=SDL_A, b0=SDL_Z), headed=args.headed)
    try:
        held = game.tap(SDL_A)
        check("A key -> $FF80", f"0x{held:02x}", f"0x{BIT['A']:02x}")
        # The negative control: the whole point of the fix.
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
    game = Game("defaults", prefs(), headed=args.headed)
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
    if args.headed:
        print("case 3: launcher keybinds page (real window, driven over TCP)")
        for label, got, want in launcher_case():
            check(label, got, want)
    else:
        print("case 3: launcher keybinds page -- SKIPPED")
        print("  the recomp-ui launcher needs a GL context (no SDL dummy driver)")
        print("  and calls SDL_RaiseWindow unconditionally, so it cannot run")
        print("  without taking the foreground. Run with --headed to cover it.")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


# The launcher's CONTROLLER view at a pinned 1100x880: the LEFT row's PRIMARY
# chip. Read off logs/keybinds-probe/launcher/before.png, which this case writes
# every run — if the layout moves, that PNG shows where the click landed.
LAUNCHER_SIZE = (1100, 880)
LEFT_PRIMARY_CHIP = (848, 364)


def wait_for_file(path: Path, deadline: float, proc) -> None:
    """Block until `path` exists and has stopped growing."""
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 1000:
            return
        if proc.poll() is not None:
            raise RuntimeError(f"launcher exited before writing {path.name}")
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {path.name}")


def launcher_case():
    """Rebind LEFT to D through the real launcher page; report (label, got, want).

    The click and the key both go over TCP, through the engine's pre-boot debug
    listener (docs/DEBUG_SERVER.md, "The pre-boot launcher"): this probe sends
    no OS-level input and never calls SetForegroundWindow. The launcher window
    does still raise itself -- that is recomp-ui's own SDL_RaiseWindow, and it
    is why this case is headed-only.
    """
    folder = SANDBOX / "launcher"
    exe = stage(folder, assets=True)
    ini = folder / "runtime_prefs.ini"
    ini.write_text(prefs())
    port = free_port()
    w, h = LAUNCHER_SIZE
    before, after = folder / "before.png", folder / "after.png"

    # LNG_SCRIPT carries only what TCP cannot: the window size, the view switch
    # and the framebuffer captures (glReadPixels needs the launcher's own GL
    # context, so a screenshot can only come from its frame callback). The long
    # wait between the shots is the window this probe drives the page in.
    env = clean_env(
        GBRECOMP_LAUNCHER="1", GBRECOMP_NO_LAUNCHER="0",
        GBRECOMP_DEBUG_PORT=str(port),
        LNG_SCRIPT=f"size:{w}x{h};wait:10;view:controller;wait:10;"
                   f"shot:before.png;wait:900;shot:after.png;wait:5;quit")
    log = open(folder / "process.log", "wb")
    proc = subprocess.Popen([str(exe)], cwd=folder, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        debug = Debug(port=port, timeout=60)
        try:
            wait_for_file(before, deadline, proc)
            # Arm the capture by clicking the chip, exactly as a player does.
            # warp is off, so the host cursor never moves.
            debug.mouse_click(*LEFT_PRIMARY_CHIP)
            time.sleep(0.4)             # let the page redraw in "press a key"
            debug.key(SDL_D, down=True)
            time.sleep(0.05)
            debug.key(SDL_D, down=False)
            wait_for_file(after, deadline, proc)
        finally:
            debug.close()
        proc.wait(timeout=90)
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
        (f"launcher page screenshots written ({after})",
         before.exists() and after.exists(), True),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
