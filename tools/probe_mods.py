"""Drive the real recomp-ui Mods page, press Play, and check runtime + persistence.

Clicks the actual launcher controls (no model shortcuts), so this fails if the
provider stops appearing, the aspect choices change, or the saved
sml2-mods.ini stops round-tripping.

Two packages now share that page: Adaptive widescreen (presentation) and DX
color (which recompiled body boots). The DX cases here deliberately avoid
clicking the DX checkbox -- its row coordinate would be a guess that silently
rots when the list reflows -- and drive it through SML2_DX instead, which is the
same settings struct the checkbox writes. What they DO exercise through the real
UI is the part that can only be tested there: commit() persisting DX to
sml2-mods.ini on Play, and commit() REFUSING Play when DX is on with no patch
staged. tools/probe_dx.py covers body selection itself, headlessly.
"""
import configparser, json, os, shutil, socket, subprocess, time

from probe_adaptive import Probe, ROOT, EXE

FOLDER = ROOT / "logs/mods-probe"


class PlayRefused(RuntimeError):
    """The launcher never handed control to the game. Expected when commit()
    vetoes the launch; a failure in every other case."""


class LauncherProbe(Probe):
    def __init__(self, script, seed=None, dx=None, stage_patch=True):
        self.folder = FOLDER
        (self.folder / "logs").mkdir(parents=True, exist_ok=True)
        self.exe = self.folder / EXE.name
        shutil.copy2(EXE, self.exe)
        shutil.copytree(ROOT / "generated/build/assets", self.folder / "assets", dirs_exist_ok=True)
        assert (self.folder / "assets/img/boxart.tga").read_bytes() == \
               (ROOT / "recomp/launcher/boxart.tga").read_bytes(), "launcher box art not staged"
        # The one supported ROM (CRC32 D5EC24E4); the launcher gate rejects
        # anything else, so Play would never light up with the wrong file.
        rom = ROOT / "roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb"
        assert rom.exists(), "supply the V1.0 ROM at " + str(rom)
        (self.folder / "rom.cfg").write_text(str(rom))
        # DX color is only offered when its patch sits next to the executable.
        patch = self.folder / "sml2dx_v181.bps"
        if stage_patch:
            shutil.copy2(EXE.parent / "sml2dx_v181.bps", patch)
        elif patch.exists():
            patch.unlink()
        port_socket = socket.socket()
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
        port_socket.close()
        env = os.environ.copy()
        env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
        for key in list(env):
            if key.startswith(("SML2_", "LNG_")):
                env.pop(key)
        env.update(GBRECOMP_NO_LAUNCHER="0", GBRECOMP_LAUNCHER="1",
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
        self.start_frame = self.command("pause")["frame"]
        self.step(4)


def main():
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
            saved = configparser.ConfigParser()
            saved.read(FOLDER / "sml2-mods.ini")
            assert saved["Mods"].getint("AdaptiveWidescreen") == enabled, (name, dict(saved["Mods"]))
            assert saved["Mods"].getint("Width") == 512, (name, dict(saved["Mods"]))
            assert saved["Mods"].getint("DX") == 0, (name, dict(saved["Mods"]))
        finally:
            p.close()

    # ---- DX color through the real launcher --------------------------------
    # 1. DX on with the patch staged: Play goes through, the DX body boots, and
    #    commit() persists DX=1 alongside the widescreen selection.
    (FOLDER / "sml2-mods.ini").write_text("[Mods]\nAdaptiveWidescreen=0\nWidth=-1\nDX=0\n")
    p = LauncherProbe(prefix + mods + "shot:dx-available.png;" + play, dx=True)
    try:
        state = p.command("sml2_mod_state")
        results["dx_on"] = state
        assert state["dx"] == 1 and state["dx_available"] == 1, state
        assert state["body"] == "Super_Mario_Land_2_DX", state
        assert state["margins"] == 1, state   # widescreen composes on DX too now
        saved = configparser.ConfigParser()
        saved.read(FOLDER / "sml2-mods.ini")
        assert saved["Mods"].getint("DX") == 1, dict(saved["Mods"])
    finally:
        p.close()

    # 2. DX on with the patch REMOVED: commit() must veto Play rather than let
    #    the player boot something that cannot exist. The launcher stays up (the
    #    script runs out and quits), so the game never reaches the debug server.
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
    (ROOT / "logs/mods-probe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
