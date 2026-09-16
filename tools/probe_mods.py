"""Drive the real recomp-ui Mods page, press Play, and check runtime + persistence.

Clicks the actual launcher controls (no model shortcuts), so this fails if the
provider stops appearing, the aspect choices change, or the saved
sml2-mods.ini stops round-tripping.
"""
import configparser, json, os, shutil, socket, subprocess, time

from probe_adaptive import Probe, ROOT, EXE

FOLDER = ROOT / "logs/mods-probe"


class LauncherProbe(Probe):
    def __init__(self, script, seed=None):
        self.folder = FOLDER
        (self.folder / "logs").mkdir(parents=True, exist_ok=True)
        self.exe = self.folder / EXE.name
        shutil.copy2(EXE, self.exe)
        shutil.copytree(ROOT / "generated/build/assets", self.folder / "assets", dirs_exist_ok=True)
        assert (self.folder / "assets/img/boxart.tga").read_bytes() == \
               (ROOT / "recomp/launcher/boxart.tga").read_bytes(), "launcher box art not staged"
        (self.folder / "rom.cfg").write_text(str(next((ROOT / "roms").glob("*.gb"))))
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
                    raise RuntimeError("Mods script never reached Play; inspect " + str(FOLDER))
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
            saved = configparser.ConfigParser()
            saved.read(FOLDER / "sml2-mods.ini")
            assert saved["Mods"].getint("AdaptiveWidescreen") == enabled, (name, dict(saved["Mods"]))
            assert saved["Mods"].getint("Width") == 512, (name, dict(saved["Mods"]))
        finally:
            p.close()
    for name in ("dashboard", "disabled", "aspects", "enabled-32", "remembered",
                 "unchecked", "unchecked-remembered"):
        data = (FOLDER / (name + ".png")).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 10000, \
            "missing or blank UI capture: " + name
    (ROOT / "logs/mods-probe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
