"""Capture the DX pause / warp-pipe repro at 32:9 over the generic TCP commands.

Observation tooling only -- it loads recomp/fixtures/dx_pause_pipe_repro.state1
into the DX body, then screenshots the presented frame before pausing, after
`press S` (pause), and after `press D` (down into the warp pipe). It asserts
only that the capture path works (wide frames came back); judging the picture is
a human/agent job. See gb-recompiled/docs/DEBUG_SERVER.md.

    python tools/probe_pause_pipe_capture.py [--window] [--out logs/pausepipe]
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tcp import Debug  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "generated/build/Super_Mario_Land_2.exe"
FIXTURE_DIR = ROOT / "recomp/fixtures"
# The .state1/.sav fixtures are gitignored, so a worktree only has the .mods.ini.
# Fall back to the primary checkout, which is where they live.
FALLBACK_FIXTURES = Path(r"F:/Projects/gbcrecomp/Super Mario Land 2/recomp/fixtures")


def fixture(name: str) -> Path:
    local = FIXTURE_DIR / name
    return local if local.exists() else FALLBACK_FIXTURES / name


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/pausepipe")
    ap.add_argument("--window", action="store_true",
                    help="run with a real window instead of --benchmark")
    ap.add_argument("--aspect", default="32:9")
    ap.add_argument("--settle", type=int, default=8,
                    help="frames to advance after each input before capturing")
    args = ap.parse_args()

    folder = ROOT / args.out
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "logs").mkdir(parents=True, exist_ok=True)

    exe = folder / EXE.name
    shutil.copy2(EXE, exe)
    if (EXE.parent / "sml2dx_v181.bps").exists():
        shutil.copy2(EXE.parent / "sml2dx_v181.bps", folder / "sml2dx_v181.bps")
    (folder / "rom.cfg").write_text(str(next((ROOT / "roms").glob("*.gb"))))
    shutil.copy2(fixture("dx_pause_pipe_repro.mods.ini"), folder / "sml2-mods.ini")
    # A stale battery save would fight the state we are about to load.
    for stray in folder.glob("*.sav"):
        stray.unlink()

    env = os.environ.copy()
    env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
    for key in [k for k in env if k.startswith("SML2_")]:
        env.pop(key)
    port = free_port()
    env.update(GBRECOMP_DEBUG_PORT=str(port), GBRECOMP_NO_LAUNCHER="1",
               SML2_WIDESCREEN=args.aspect, SML2_DX="1")

    cmd = [str(exe), "--log-file", "logs/run.log"]
    if not args.window:
        cmd.append("--benchmark")
    log = open(folder / "process.log", "wb")
    proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=log,
                            stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    shots, views = {}, {}

    def capture(d, tag):
        """Screenshot + the compositor's own view of that same frame."""
        shots[tag] = d.screenshot(f"logs/{folder.name}_{tag}.png")
        views[tag] = d.cmd("sml2_view")
        return shots[tag]

    try:
        with Debug(port=port, timeout=45) as d:
            d.pause()
            # Boot far enough that the body is live before swapping timelines.
            d.advance(30)

            slot = d.slot_path(0)
            state = fixture("dx_pause_pipe_repro.state1")
            loaded = d.load_state(path=state)
            d.advance(args.settle)
            capture(d, "before")

            d.press("S")                       # Start -> pause
            d.advance(args.settle)
            capture(d, "pause")

            d.press("S")                       # un-pause
            d.advance(args.settle)
            capture(d, "unpause")

            d.press("D", frames=20)            # down into the warp pipe
            d.advance(20)
            capture(d, "pipe_enter")
            d.advance(40)
            capture(d, "pipe")

            # Round-trip the runtime's own slot naming: save to slot 0, reload
            # it, and confirm the file the runtime's save key would use.
            saved = d.save_state(slot=0)
            reloaded = d.load_state(slot=0)
            d.quit()
    finally:
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    print("slot0        :", slot)
    print("loaded state :", loaded)
    print("slot save    :", saved)
    print("slot reload  :", reloaded)
    for name, shot in shots.items():
        v = views[name]
        print(f"{name:<11}: frame={shot['frame']:>5} {shot['width']}x{shot['height']} "
              f"src={shot['source']}  valid={v['valid']} wide={v['wide']} "
              f"mode={v['mode']} reject={v['reject']} "
              f"score={v['score']} fail_run={v['fail_run']}")
    bad = [n for n, s in shots.items() if s["width"] != 512 or s["height"] != 144]
    if bad:
        print("NOT 32:9 (512x144):", bad)
        return 1
    print("all captures 512x144; PNGs under", folder / "logs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
