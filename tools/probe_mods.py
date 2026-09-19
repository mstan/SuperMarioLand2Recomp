"""Regression probe for the Super Mario Land 2 launcher Mods page.

Two packages share that page: Adaptive widescreen (presentation) and DX color
(which recompiled body boots). What is verified either way:

  * the provider offers both features, with their options and live values,
  * ticking a feature and choosing an aspect is persisted to sml2-mods.ini by
    the commit that Play performs,
  * a persisted selection survives a relaunch untouched,
  * an explicit launch preset (SML2_WIDESCREEN) loses to an unticked checkbox,
  * the committed selection is what the game actually boots with: the custom
    view, its width, and WHICH BODY runs (faithful vs DX),
  * DX color with no sml2dx_v181.bps staged REFUSES to commit, so the player is
    never sent to a body that cannot exist.

HEADLESS BY DEFAULT
-------------------
The ImGui Mods page lives in the recomp-ui pre-boot launcher, and that launcher
cannot be run without disturbing the desktop:

  * it needs a GL context, so SDL_VIDEODRIVER=dummy is out --
    "[launcher] SDL_CreateWindow failed: OpenGL support is either not configured
    in SDL or not available in current SDL video driver (dummy)";
  * it calls SDL_RaiseWindow unconditionally when it opens
    (recomp-ui src/common/launcher_platform_sdl2.c), so the window takes the
    foreground. Setting SDL's own SDL_WINDOW_NO_ACTIVATION_WHEN_SHOWN hint does
    not help: that governs ShowWindow, not the SetForegroundWindow that
    follows (measured, SDL 2.32.10).

So by default this probe drives the SAME provider vtable the page drives --
feature_enable / feature_set_option / commit / last_error, reached over the
debug server with `sml2_mod_enable`, `sml2_mod_option`, `sml2_mod_commit` (see
sml2_mods.c) -- in a headless run of the game, and then boots a second headless
run to check what the committed ini actually produces. Nothing appears on
screen and the foreground never changes.

What that does NOT cover is the ImGui page itself: the row layout, and which
control writes which option. `--headed` runs exactly that, the original
LNG_SCRIPT pass with real clicks, real screenshots and a real (focus-taking)
window.

    python tools/probe_mods.py            # headless provider + boot effect
    python tools/probe_mods.py --headed   # the real Mods page, takes focus
"""
import argparse
import configparser
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ROOT, EXE  # noqa: E402
from tcp import Debug  # noqa: E402

FOLDER = ROOT / "logs/mods-probe"

WIDESCREEN = ("sml2-adaptive-widescreen", "adaptive-widescreen")
DX = ("sml2-dx-color", "dx-color")


class PlayRefused(RuntimeError):
    """The launcher never handed control to the game. Expected when commit()
    vetoes the launch; a failure in every other case."""


# ── shared staging ──────────────────────────────────────────────────────────
def stage(folder: Path, *, stage_patch: bool = True) -> Path:
    """One sandbox: the exe, the DLLs it loads, the launcher assets, the ROM."""
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    exe = folder / EXE.name
    shutil.copy2(EXE, exe)
    for dll in EXE.parent.glob("*.dll"):
        shutil.copy2(dll, folder / dll.name)
    shutil.copytree(EXE.parent / "assets", folder / "assets", dirs_exist_ok=True)
    assert (folder / "assets/img/boxart.tga").read_bytes() == \
           (ROOT / "recomp/launcher/boxart.tga").read_bytes(), "launcher box art not staged"
    # The one supported ROM (CRC32 D5EC24E4); the launcher gate rejects
    # anything else, so Play would never light up with the wrong file.
    rom = ROOT / "roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb"
    assert rom.exists(), "supply the V1.0 ROM at " + str(rom)
    (folder / "rom.cfg").write_text(str(rom))
    # DX color is only offered when its patch sits next to the executable.
    patch = folder / "sml2dx_v181.bps"
    if stage_patch:
        shutil.copy2(EXE.parent / "sml2dx_v181.bps", patch)
    elif patch.exists():
        patch.unlink()
    return exe


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


def saved_ini(folder: Path) -> dict:
    parser = configparser.ConfigParser()
    parser.read(folder / "sml2-mods.ini")
    return dict(parser["Mods"])


# ── headless: the provider vtable, in a real (windowless) game process ──────
class HeadlessRun:
    """A headless run of the game with a debug connection, and nothing else."""

    def __init__(self, folder: Path, seed=None, dx=None, stage_patch=True):
        self.folder = folder
        self.exe = stage(folder, stage_patch=stage_patch)
        port = free_port()
        env = clean_env(GBRECOMP_DEBUG_PORT=str(port), GBRECOMP_NO_LAUNCHER="1",
                        GBRECOMP_HEADLESS="1")
        if seed:
            env["SML2_WIDESCREEN"] = seed
        if dx is not None:
            env["SML2_DX"] = "1" if dx else "0"
        self.output = open(folder / "process.log", "wb")
        self.process = subprocess.Popen(
            [str(self.exe), "--log-file", "logs/run.log"], cwd=folder, env=env,
            stdout=self.output, stderr=subprocess.STDOUT)
        try:
            self.debug = Debug(port=port, timeout=60)
        except Exception:
            self.close()
            raise
        # Halt the guest: nothing here needs it running, and a paused runner
        # answers every command at a frame boundary.
        self.debug.pause()

    # -- the Mods page's own vtable, over TCP --
    def features(self) -> dict:
        """{feature_id: feature record} exactly as the page would read them."""
        reply = self.debug.cmd("sml2_mod_features")
        return {f["feature"]: f for f in reply["features"]}

    def option(self, feature_id: str, option_id: str):
        return self.features()[feature_id]["options"]

    def option_value(self, feature_id: str, option_id: str):
        for opt in self.features()[feature_id]["options"]:
            if opt["id"] == option_id:
                return opt["value"]
        raise KeyError(option_id)

    def enable(self, feature, enabled: bool):
        package, fid = feature
        return self.debug.cmd("sml2_mod_enable", package=package, feature=fid,
                              enabled=1 if enabled else 0)

    def set_option(self, feature, option_id: str, value: str):
        package, fid = feature
        return self.debug.cmd("sml2_mod_option", package=package, feature=fid,
                              option=option_id, value=value)

    def commit(self) -> dict:
        """What PLAY does: resolve, veto or persist. Never raises on a veto."""
        return self.debug.cmd("sml2_mod_commit")

    def state(self) -> dict:
        return self.debug.cmd("sml2_mod_state")

    def close(self):
        debug = getattr(self, "debug", None)
        if debug is not None:
            try:
                debug.quit()
            except Exception:
                pass
            debug.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.output.close()


def headless_main() -> dict:
    if FOLDER.exists():
        shutil.rmtree(FOLDER, ignore_errors=True)
    FOLDER.mkdir(parents=True, exist_ok=True)
    (FOLDER / "sml2-mods.ini").write_text("[Mods]\nAdaptiveWidescreen=0\nWidth=-1\n")
    results = {}

    def boot_effect(name, seed=None, dx=None):
        """A second run: what the committed ini actually makes the game do."""
        run = HeadlessRun(FOLDER, seed=seed, dx=dx)
        try:
            state = run.state()
            results[name] = state
            return state
        finally:
            run.close()

    # 1. Tick the feature, choose 32:9, Play.
    run = HeadlessRun(FOLDER)
    try:
        feats = run.features()
        assert set(feats) == {"adaptive-widescreen", "dx-color"}, feats
        assert feats["adaptive-widescreen"]["enabled"] == 0, feats
        assert run.option_value("adaptive-widescreen", "aspect") == "Fit", feats
        assert run.enable(WIDESCREEN, True)["enabled"] == 1
        assert run.set_option(WIDESCREEN, "aspect", "32:9")["value"] == "32:9"
        assert run.option_value("adaptive-widescreen", "aspect") == "32:9"
        assert run.features()["adaptive-widescreen"]["enabled"] == 1
        assert run.commit()["committed"] == 1
    finally:
        run.close()
    ini = saved_ini(FOLDER)
    assert int(ini["adaptivewidescreen"]) == 1, ini
    assert int(ini["width"]) == 512, ini
    assert int(ini["dx"]) == 0, ini
    state = boot_effect("enabled")
    assert (state["enabled"], state["width"]) == (1, 512), state
    assert state["dx"] == 0 and state["body"] == "Super_Mario_Land_2", state

    # 2. Selection persisted in sml2-mods.ini survives a relaunch untouched:
    #    open, commit nothing, and it is still there.
    run = HeadlessRun(FOLDER)
    try:
        assert run.features()["adaptive-widescreen"]["enabled"] == 1
        assert run.option_value("adaptive-widescreen", "aspect") == "32:9"
        assert run.commit()["committed"] == 1
    finally:
        run.close()
    ini = saved_ini(FOLDER)
    assert (int(ini["adaptivewidescreen"]), int(ini["width"]), int(ini["dx"])) == (1, 512, 0), ini
    state = boot_effect("remembered")
    assert (state["enabled"], state["width"]) == (1, 512), state

    # 3. An explicit launch preset must still lose to an unticked checkbox.
    run = HeadlessRun(FOLDER, seed="32:9")
    try:
        assert run.features()["adaptive-widescreen"]["enabled"] == 1, "preset should seed it"
        assert run.enable(WIDESCREEN, False)["enabled"] == 0
        assert run.features()["adaptive-widescreen"]["enabled"] == 0
        assert run.commit()["committed"] == 1
    finally:
        run.close()
    ini = saved_ini(FOLDER)
    assert int(ini["adaptivewidescreen"]) == 0, ini
    assert int(ini["width"]) == 512, ini          # the aspect choice is remembered
    assert int(ini["dx"]) == 0, ini
    state = boot_effect("disabled")
    assert (state["enabled"], state["width"]) == (0, 160), state

    # 4. ...and that stays unticked across a relaunch.
    run = HeadlessRun(FOLDER)
    try:
        assert run.features()["adaptive-widescreen"]["enabled"] == 0
        assert run.commit()["committed"] == 1
    finally:
        run.close()
    ini = saved_ini(FOLDER)
    assert (int(ini["adaptivewidescreen"]), int(ini["width"]), int(ini["dx"])) == (0, 512, 0), ini
    state = boot_effect("disabled_remembered")
    assert (state["enabled"], state["width"]) == (0, 160), state

    # 5. DX color with the patch staged: commit goes through, the DX body boots,
    #    and DX=1 is persisted alongside the widescreen selection.
    (FOLDER / "sml2-mods.ini").write_text("[Mods]\nAdaptiveWidescreen=0\nWidth=-1\nDX=0\n")
    run = HeadlessRun(FOLDER, dx=True)
    try:
        feats = run.features()
        assert feats["dx-color"]["enabled"] == 1, feats     # SML2_DX seeded it
        assert feats["dx-color"]["has_error"] == 0, feats
        assert run.enable(WIDESCREEN, True)["enabled"] == 1
        assert run.set_option(WIDESCREEN, "aspect", "32:9")["value"] == "32:9"
        assert run.commit()["committed"] == 1
    finally:
        run.close()
    ini = saved_ini(FOLDER)
    assert int(ini["dx"]) == 1, ini
    state = boot_effect("dx_on")
    assert state["dx"] == 1 and state["dx_available"] == 1, state
    assert state["body"] == "Super_Mario_Land_2_DX", state
    assert state["margins"] == 1, state   # widescreen composes on DX too now

    # 6. DX color with the patch REMOVED: commit must veto rather than let the
    #    player boot something that cannot exist. This is the Play refusal.
    refused = False
    run = HeadlessRun(FOLDER, dx=True, stage_patch=False)
    try:
        feats = run.features()
        assert feats["dx-color"]["enabled"] == 1, feats
        reply = run.commit()
        refused = reply["committed"] == 0
        results["dx_no_patch"] = {"play_refused": refused, "error": reply["error"]}
        assert "sml2dx_v181.bps" in reply["error"], reply
    finally:
        run.close()
    assert refused, "DX color with no patch staged must refuse to commit"
    # ...and the veto left the previously committed file alone.
    assert int(saved_ini(FOLDER)["dx"]) == 1, saved_ini(FOLDER)
    return results


# ── headed: the real ImGui Mods page, through LNG_SCRIPT ────────────────────
class LauncherProbe(Probe):
    def __init__(self, script, seed=None, dx=None, stage_patch=True):
        self.folder = FOLDER
        self.exe = stage(self.folder, stage_patch=stage_patch)
        port = free_port()
        env = clean_env(GBRECOMP_NO_LAUNCHER="0", GBRECOMP_LAUNCHER="1",
                        GBRECOMP_DEBUG_PORT=str(port), LNG_SCRIPT=script)
        if seed:
            env["SML2_WIDESCREEN"] = seed
        if dx is not None:
            env["SML2_DX"] = "1" if dx else "0"
        self.output = open(self.folder / "process.log", "wb")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1
        self.process = subprocess.Popen([str(self.exe), "--log-file", "logs/run.log"],
                                        cwd=self.folder, env=env, stdout=self.output,
                                        stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW,
                                        startupinfo=startup)
        # The pre-boot debug listener answers as soon as the launcher starts, so
        # "connected" no longer means "the game booted". Poll a GAME command:
        # it is refused with `pre-boot launcher` until the launcher hands over.
        until = time.monotonic() + 60
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() > until:
                    self.process.terminate()
                    self.process.wait(timeout=10)
                    self.output.close()
                    raise PlayRefused("Mods script never reached Play; inspect " + str(FOLDER))
                time.sleep(0.05)
        self.sock.settimeout(60)
        self.reader = self.sock.makefile("r", encoding="utf8")
        self.id = 0
        self.pending = ""
        while True:
            try:
                self.command("sml2_mod_state")
                break
            except RuntimeError as exc:
                if "pre-boot launcher" not in str(exc):
                    raise
                if self.process.poll() is not None or time.monotonic() > until:
                    self.close()
                    raise PlayRefused("Mods script never reached Play; inspect " + str(FOLDER))
                time.sleep(0.1)
        self.start_frame = self.command("pause")["frame"]
        self.step(4)


def headed_main() -> dict:
    if FOLDER.exists():
        shutil.rmtree(FOLDER, ignore_errors=True)
    FOLDER.mkdir(parents=True, exist_ok=True)
    (FOLDER / "sml2-mods.ini").write_text("[Mods]\nAdaptiveWidescreen=0\nWidth=-1\n")
    prefix = "size:1280x800;wait:12;"
    mods = "click:1200,48;wait:12;"
    play = "click:1150,740;wait:20"
    scripts = [
        # Tick the feature, open the aspect dropdown, choose 32:9, Play.
        ("enabled", prefix + "shot:dashboard.png;" + mods + "shot:disabled.png;"
                    "click:64,304;wait:6;click:744,442;wait:6;shot:aspects.png;"
                    "click:700,570;wait:6;shot:enabled-32.png;" + play, 1, 512, None),
        # Selection persisted in sml2-mods.ini survives a relaunch untouched.
        ("remembered", prefix + mods + "shot:remembered.png;" + play, 1, 512, None),
        # An explicit launch preset must still lose to an unticked checkbox.
        ("disabled", prefix + mods + "click:64,304;wait:5;shot:unchecked.png;" + play,
         0, 160, "32:9"),
        ("disabled_remembered", prefix + mods + "shot:unchecked-remembered.png;" + play,
         0, 160, None),
    ]
    results = {}
    for name, script, enabled, width, seed in scripts:
        p = LauncherProbe(script, seed)
        try:
            state = p.command("sml2_mod_state")
            results[name] = state
            assert (state["enabled"], state["width"]) == (enabled, width), (name, state)
            assert state["dx"] == 0, (name, state)
            assert state["body"] == "Super_Mario_Land_2", (name, state)
            saved = saved_ini(FOLDER)
            assert int(saved["adaptivewidescreen"]) == enabled, (name, saved)
            assert int(saved["width"]) == 512, (name, saved)
            assert int(saved["dx"]) == 0, (name, saved)
        finally:
            p.close()

    # DX on with the patch staged: Play goes through, the DX body boots, and
    # commit() persists DX=1 alongside the widescreen selection.
    (FOLDER / "sml2-mods.ini").write_text("[Mods]\nAdaptiveWidescreen=0\nWidth=-1\nDX=0\n")
    p = LauncherProbe(prefix + mods + "shot:dx-available.png;" + play, dx=True)
    try:
        state = p.command("sml2_mod_state")
        results["dx_on"] = state
        assert state["dx"] == 1 and state["dx_available"] == 1, state
        assert state["body"] == "Super_Mario_Land_2_DX", state
        assert state["margins"] == 1, state   # widescreen composes on DX too now
        assert int(saved_ini(FOLDER)["dx"]) == 1, saved_ini(FOLDER)
    finally:
        p.close()

    # DX on with the patch REMOVED: commit() must veto Play rather than let the
    # player boot something that cannot exist. The launcher stays up (the script
    # runs out and quits), so the game never reaches the debug server.
    refused = False
    try:
        p = LauncherProbe(prefix + mods + "shot:dx-missing.png;" + play,
                          dx=True, stage_patch=False)
        try:
            results["dx_no_patch"] = p.command("sml2_mod_state")
        finally:
            p.close()
    except PlayRefused:
        refused = True
    assert refused, "DX color with no patch staged must refuse Play"
    results["dx_no_patch"] = {"play_refused": True}
    for name in ("dashboard", "disabled", "aspects", "enabled-32", "remembered",
                 "unchecked", "unchecked-remembered", "dx-available", "dx-missing"):
        data = (FOLDER / (name + ".png")).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 10000, \
            "missing or blank UI capture: " + name
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true",
                    help="drive the real ImGui Mods page in a launcher window "
                         "(needs an interactive desktop and TAKES FOCUS)")
    args = ap.parse_args()
    print("mode: " + ("HEADED (real Mods page, takes focus)"
                      if args.headed else "headless (provider seam, no window)"))
    results = headed_main() if args.headed else headless_main()
    (ROOT / "logs/mods-probe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
