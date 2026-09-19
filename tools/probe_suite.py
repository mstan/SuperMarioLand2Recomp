"""Run the headless probe suite in order and report one pass/fail table.

Every probe here runs without a window: the older ones already used
--benchmark, and probe_keybinds / probe_mods now default to the headless
debug-server path. Wrap the whole thing in the foreground gate to prove it:

    python tools/focus_guard.py python tools/probe_suite.py

Individual probes still run on their own; this only saves typing and makes the
gate one command. `--headed` forwards to the two probes that have a real-UI
pass, which DOES take the foreground.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# (module, extra args when --headed). Order matters only in that the cheap,
# most-specific probes run first, so a broken build says so quickly.
SUITE = [
    ("probe_keybinds", ["--headed"]),
    ("probe_mods", ["--headed"]),
    ("probe_dx", []),
    ("probe_adaptive", []),
    ("probe_fit", []),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true",
                    help="also run the real-UI passes (takes the foreground)")
    ap.add_argument("--only", nargs="*", help="run just these probes")
    args = ap.parse_args()

    results = []
    for name, headed_args in SUITE:
        if args.only and name not in args.only:
            continue
        command = [sys.executable, "-u", str(TOOLS / f"{name}.py")]
        if args.headed:
            command += headed_args
        print(f"\n=== {name}{' --headed' if args.headed and headed_args else ''} ===",
              flush=True)
        began = time.monotonic()
        rc = subprocess.call(command, cwd=TOOLS.parent)
        results.append((name, rc, time.monotonic() - began))

    print("\n--- probe suite ---")
    for name, rc, seconds in results:
        print(f"  {'PASS' if rc == 0 else f'FAIL ({rc})'}  {name}  {seconds:.1f}s")
    failed = [name for name, rc, _ in results if rc]
    print("FAILED: " + ", ".join(failed) if failed else "all probes passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
