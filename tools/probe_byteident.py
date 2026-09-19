"""With the mod off, the build must be pixel-for-pixel the faithful one.

Every hook this repo adds is opt-in: with `SML2_WIDESCREEN=off` no compositor,
no read tap and no read override is installed, and the `[[imm_override]]` site
at `00:327E` hands the ROM back its own `$C0`. That is a claim about the
generated code and the runtime, so it is checked the only way that settles it:
run the same input route on two builds and hash the frames.

Usage, from the game root with a native Windows Python:

    python tools/probe_byteident.py before      # on the reference build
    python tools/probe_byteident.py after       # on the build under test
    python tools/probe_byteident.py --compare before after

Each run writes `logs/byteident-<tag>.json`. `--compare` exits non-zero if any
frame differs and names the frames that did.
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import ROUTE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "generated/build/Super_Mario_Land_2.exe"
# Level entry, then scrolling gameplay with the route's jump cadence. The route
# enters the level at frame 2400 on purpose: --benchmark reaches it in over a
# second, which is ample time to connect and pause first. Frames before the
# attach are not capturable at all, which is why none is asked for.
FRAMES = (2500, 2700, 3000, 3400, 3800, 4200, 4600)


def capture(tag):
    folder = ROOT / "logs" / ("byteident-" + tag)
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "logs").mkdir(parents=True)
    exe = folder / EXE.name
    shutil.copy2(EXE, exe)
    if (EXE.parent / "sml2dx_v181.bps").exists():
        shutil.copy2(EXE.parent / "sml2dx_v181.bps", folder / "sml2dx_v181.bps")
    (folder / "rom.cfg").write_text(str(next((ROOT / "roms").glob("*.gb"))))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = os.environ.copy()
    env["PATH"] = "C:/msys64/mingw64/bin;" + env["PATH"]
    for key in list(env):
        if key.startswith("SML2_"):
            env.pop(key)
    # The faithful body with every hook off: that is what has to stay identical.
    env.update(GBRECOMP_DEBUG_PORT=str(port), GBRECOMP_NO_LAUNCHER="1",
               SML2_WIDESCREEN="off", SML2_DX="0")
    out = open(folder / "process.log", "wb")
    proc = subprocess.Popen(
        [str(exe), "--input", ROUTE, "--log-file", "logs/run.log", "--benchmark"],
        cwd=folder, env=env, stdout=out, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    until = time.monotonic() + 30
    while True:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=1)
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > until:
                raise RuntimeError("build did not start; see " + str(folder))
            time.sleep(0.02)
    conn.settimeout(120)
    reader = conn.makefile("r", encoding="utf8")
    state = dict(id=0)

    def cmd(name, **kw):
        state["id"] += 1
        want = state["id"]
        conn.sendall((json.dumps(dict(cmd=name, id=want, **kw)) + "\n").encode())
        while True:
            obj = json.loads(reader.readline())
            if obj.get("id") == want:
                return obj

    def event(name):
        while True:
            obj = json.loads(reader.readline())
            if obj.get("event") == name:
                return obj

    start = cmd("pause")["frame"]
    if start >= FRAMES[0]:
        raise RuntimeError("attached at frame %d, after the first capture point"
                           % start)
    hashes = {}
    try:
        for frame in FRAMES:
            cmd("run_to_frame", frame=frame)
            event("run_to_done")
            path = folder / ("f%d.ppm" % frame)
            r = cmd("screenshot", path=str(path).replace("\\", "/"))
            if not r.get("ok"):
                raise RuntimeError(("screenshot failed", frame, r))
            hashes[frame] = hashlib.sha256(path.read_bytes()).hexdigest()
        cmd("quit")
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.close()
        conn.close()
        out.close()
    report = ROOT / ("logs/byteident-%s.json" % tag)
    report.write_text(json.dumps(hashes, indent=1))
    for frame in FRAMES:
        print("%6d  %s" % (frame, hashes[frame]))
    print("wrote", report)
    return hashes


def compare(a, b):
    ha = json.loads((ROOT / ("logs/byteident-%s.json" % a)).read_text())
    hb = json.loads((ROOT / ("logs/byteident-%s.json" % b)).read_text())
    bad = [k for k in ha if ha[k] != hb.get(k)]
    for k in sorted(ha, key=int):
        print("%6s  %s  %s" % (k, "same" if ha[k] == hb.get(k) else "DIFFER",
                               ha[k][:16]))
    if bad or set(ha) != set(hb):
        print("\n%d of %d frames differ: %s" % (len(bad), len(ha), bad))
        raise SystemExit(1)
    print("\n%d/%d frames byte-identical" % (len(ha), len(ha)))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--compare"]:
        compare(sys.argv[2], sys.argv[3])
    else:
        capture(sys.argv[1] if len(sys.argv) > 1 else "run")
