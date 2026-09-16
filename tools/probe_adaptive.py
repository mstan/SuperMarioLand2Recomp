"""Isolated TCP regression probe for the Super Mario Land 2 adaptive widescreen mod.

Runs its own copy of the executable in its own directory with its own save file
and debug port, so a developer's real save and window are never touched.
Requires a native Windows Python (CREATE_NO_WINDOW / SetWindowPos).
"""
import json, os, shutil, socket, subprocess, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "generated/build/Super_Mario_Land_2.exe"

# Power-on -> title -> file select -> world map -> level 1, then run right with a
# steady jump cadence. The level is entered late on purpose: in --benchmark mode
# the game reaches frame 2400 in well over a second, which is ample time for the
# probe to connect and pause before anything interesting happens.
ENTER_FRAME = 2400
PLAY_FRAME = 2600
JUMPS = ",".join(f"{f}:A:18" for f in range(2620, 4561, 36))
ROUTE = f"60:S:4,140:S:4,220:S:4,{ENTER_FRAME}:A:4,{PLAY_FRAME}:R:2000," + JUMPS

# Actor table: 16 slots of 0x20 bytes; +0/+1 world X big-endian, +8 state
# (0 free, 1 dormant, 2 live). Verified against the ROM and live RAM.
ACTORS, ACTOR_STRIDE, ACTOR_SLOTS = 0xAD00, 0x20, 16
VANILLA_CULL_HALF = 0xA0  # 02:4049 -- an actor can never be live past this


class Probe:
    def __init__(self, name, aspect="32:9", route=ROUTE, window=False, env=None):
        self.folder = ROOT / "logs" / name
        if self.folder.exists():
            shutil.rmtree(self.folder, ignore_errors=True)
        (self.folder / "logs").mkdir(parents=True, exist_ok=True)
        self.exe = self.folder / EXE.name
        shutil.copy2(EXE, self.exe)
        (self.folder / "rom.cfg").write_text(str(next((ROOT / "roms").glob("*.gb"))))
        port_socket = socket.socket()
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
        port_socket.close()
        environ = os.environ.copy()
        environ["PATH"] = "C:/msys64/mingw64/bin;" + environ["PATH"]
        for key in list(environ):
            if key.startswith("SML2_"):
                environ.pop(key)
        environ.update(GBRECOMP_DEBUG_PORT=str(port), GBRECOMP_NO_LAUNCHER="1")
        if aspect:
            environ["SML2_WIDESCREEN"] = aspect
        if env:
            environ.update(env)
        args = [str(self.exe), "--input", route, "--log-file", "logs/run.log"]
        if not window:
            args.append("--benchmark")
        self.output = open(self.folder / "process.log", "wb")
        self.process = subprocess.Popen(args, cwd=self.folder, env=environ,
                                        stdout=self.output, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        until = time.monotonic() + 30
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() > until:
                    raise RuntimeError("probe did not start; see " + str(self.folder))
                time.sleep(0.02)
        self.sock.settimeout(60)
        self.reader = self.sock.makefile("r", encoding="utf8")
        self.id = 0
        self.pending = ""
        self.start_frame = self.command("pause")["frame"]

    def line(self):
        line = self.pending or self.reader.readline()
        self.pending = ""
        if not line:
            raise RuntimeError("debug socket closed")
        result, end = json.JSONDecoder().raw_decode(line)
        self.pending = line[end:].strip()
        return result

    def command(self, cmd, **kwargs):
        self.id += 1
        self.sock.sendall((json.dumps(dict(cmd=cmd, id=self.id, **kwargs)) + "\n").encode())
        while True:
            r = self.line()
            if r.get("id") == self.id:
                if "error" in r or r.get("ok") is False:
                    raise RuntimeError((cmd, r))
                return r

    def event(self, event):
        while True:
            r = self.line()
            if r.get("event") == event:
                return r

    def run_to(self, frame):
        self.command("run_to_frame", frame=frame)
        self.event("run_to_done")

    def step(self, n=1):
        self.command("step", count=n)
        self.event("step_done")

    def buttons(self, mask):
        self.command("sml2_buttons", buttons=mask)

    def ram(self, addr, n):
        out = b""
        while n:
            k = min(n, 256)
            out += bytes.fromhex(self.command("read_ram", addr=f"{addr:04x}", len=k)["hex"])
            addr += k
            n -= k
        return out

    def view(self):
        return self.command("sml2_view")

    def capture(self, name):
        self.command("sml2_capture")
        shutil.copy2(self.folder / "logs/probe.ppm", self.folder / (name + ".ppm"))
        return self.view()

    def actors(self):
        """Live actors as (slot, world_x, world_y)."""
        table = self.ram(ACTORS, ACTOR_STRIDE * ACTOR_SLOTS)
        out = []
        for slot in range(ACTOR_SLOTS):
            r = table[slot * ACTOR_STRIDE: (slot + 1) * ACTOR_STRIDE]
            if r[8] == 2:
                out.append((slot, (r[0] << 8) | r[1], (r[3] << 8) | r[4]))
        return out

    def close(self):
        try:
            self.command("quit")
        except (OSError, RuntimeError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.reader.close()
        self.sock.close()
        self.output.close()


def main():
    results = {}
    p = Probe("adaptive-probe")
    try:
        assert p.start_frame < ENTER_FRAME, (
            f"probe attached at frame {p.start_frame}, after the level was entered")
        results["attach_frame"] = p.start_frame

        p.run_to(PLAY_FRAME + 120)
        entry = p.capture("entry-32x9")
        results["gameplay_entry"] = entry
        assert entry["width"] == 512, entry
        assert entry["valid"] == 1, entry
        assert entry["mode"] == 4, entry
        assert entry["score"][0] == entry["score"][1] == 378, entry
        # Level 1 starts hard against the left wall: the view cannot be centred,
        # so it sits flush on the level's left bound with no black padding.
        assert entry["view_left"] == entry["bounds"][0] == 0, entry
        assert 0 <= entry["left"] - entry["view_left"] <= 512 - 160, entry

        # Width sweep. Each width must keep the world renderer live and keep the
        # native strip inside the composed view.
        sweep = {}
        for width in (256, 336, 512, 768, 1024, 160, 512):
            p.command("sml2_width", width=width)
            p.step(6)
            state = p.capture(f"width-{width}")
            sweep[width] = state
            assert state["width"] == width, state
            assert state["valid"] == 1, ("fell back to native during gameplay", state)
            lo, hi = state["bounds"]
            left, view_left = state["left"], state["view_left"]
            assert view_left <= left, state
            assert view_left + width >= left + 160, state
            if hi - lo >= width:
                assert lo <= view_left <= hi - width, state
            else:
                assert abs((lo - view_left) - (view_left + width - hi)) <= 1, state
        results["width_sweep"] = sweep

        # Scroll right and prove the widened activation/cull bounds are live:
        # vanilla frees any actor further than 0xA0 from the camera centre.
        p.command("sml2_width", width=512)
        p.step(6)
        far = 0
        scroll = []
        for _ in range(28):
            p.step(30)
            state = p.view()
            scroll.append(state)
            for _slot, ax, _ay in p.actors():
                far = max(far, abs(ax - state["camera_x"]))
        results["max_live_actor_distance"] = far
        results["widened_reads"] = scroll[-1]["widened"]
        results["ghosts_dropped"] = scroll[-1]["dropped"]
        results["camera_travel"] = [scroll[0]["camera_x"], scroll[-1]["camera_x"]]
        assert scroll[-1]["camera_x"] > scroll[0]["camera_x"] + 160, "camera never scrolled"
        assert far > VANILLA_CULL_HALF, (
            f"no actor lived past the vanilla {VANILLA_CULL_HALF}px cull window (max {far})")
        assert scroll[-1]["widened"] > 0, "activation/cull window override never fired"
        assert all(v["valid"] == 1 for v in scroll), "fell back to native while scrolling"
        assert all(v["score"][0] == v["score"][1] for v in scroll), "block map decode drifted"
        p.capture("scrolled-32x9")

        # Save/load replay equality at 32:9. Take control away from the
        # frame-anchored route first: a state load rewinds the emulator but not
        # the host frame counter, so only a cycle-anchored hold replays the same
        # inputs on both passes.
        p.buttons(0x01)
        p.step(4)
        p.command("sml2_save")
        p.step(40)
        p.capture("replay-a")
        ram_a = p.ram(0xAD00, 256) + p.ram(0xA200, 256) + p.ram(0xFF80, 128)
        p.command("sml2_load")
        p.step(40)
        p.capture("replay-b")
        ram_b = p.ram(0xAD00, 256) + p.ram(0xA200, 256) + p.ram(0xFF80, 128)
        results["save_replay_ram_equal"] = ram_a == ram_b
        results["save_replay_pixels_equal"] = (
            (p.folder / "replay-a.ppm").read_bytes() == (p.folder / "replay-b.ppm").read_bytes())
        assert results["save_replay_ram_equal"]
        assert results["save_replay_pixels_equal"]

        # Pause is not gameplay: the compositor must fail closed to native.
        p.buttons(0x80)
        p.step(4)
        p.buttons(0)
        p.step(40)
        paused = p.capture("paused")
        results["paused"] = paused
        assert paused["mode"] == 8, paused
        assert paused["valid"] == 0, "pause must fall back to centred native output"
        p.buttons(0x80)
        p.step(4)
        p.buttons(0)
        p.step(40)
        resumed = p.capture("resumed")
        results["resumed"] = resumed
        assert resumed["valid"] == 1, "world renderer did not recover after pause"
    finally:
        p.close()

    # A second process with the mod off must install no hooks at all.
    off = Probe("adaptive-off", aspect="off")
    try:
        state = off.command("sml2_mod_state")
        results["mod_off"] = state
        assert state["enabled"] == 0 and state["width"] == 160, state
    finally:
        off.close()

    (ROOT / "logs/adaptive-probe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
