"""Enemy spawn policy probe: Original vs Extended, against a mod-off reference.

The adaptive widescreen mod renders a view wider than the 160 px the Game Boy
draws, but the ROM's spawn scanner still spawns on exact equality with an
8-px-aligned edge at camX + 112 and CONSUMES every list entry it walks past
(02:4C31 / 02:4CEB -- see ADAPTIVE.md). This probe proves the two policies:

  Original  no override is installed at all, so the edge, the direction and the
            list cursor are the ROM's own: the same records are spawned at the
            same camera positions as with the mod off.
  Extended  the edge reaches the visible view edge instead, ramped by at most
            8 px per scanning frame, and spawns a SUPERSET of Original's
            records without the edge ever stepping over one.

Every configuration is measured the same way: the scanner's own state is read
back out of RAM each frame -- cursor $AF1E/$AF1F, edge $AF00/$AF01, direction
$AF22 -- so the three runs are comparable without trusting the module's own
counters, which are then cross-checked against it.

Runs are compared by CONTENT, not by frame index. A widescreen process
occasionally slips one host frame against a native one (the compositor is not
free), which shifts the whole camera trace by a frame without changing a single
decision; asserting frame-for-frame equality would measure the host's frame
pacing, not the mod.

Also runs the attract-mode demo, which is where the pop-in was noticed: the
title screen plays Level 1 on its own with no input at all.

Requires a native Windows Python (CREATE_NO_WINDOW / SetWindowPos).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ROOT, ROUTE, PLAY_FRAME  # noqa: E402

# Spawn list: 6-byte records, world X big endian at +0/+1, difficulty/"already
# spawned" flags at +2. Head is $AB06; $AB00 is six $FF bytes so a leftward
# scan stops there. Verified against live RAM.
LIST, LIST_SIZE, RECORD = 0xAB00, 0x200, 6
SCAN_EDGE, SCAN_DIR, CURSOR = 0xAF00, 0xAF22, 0xAF1E
# 02:409A signals "the camera did not move, do not scan" by writing $FF into
# the edge's HIGH byte only -- it leaves the low byte alone, and it stores the
# camera's low byte into $AF22, so the direction byte is meaningless on such a
# frame. The scanner's own test is cp $FF on $AF00 (02:4C3C / 02:4CEB), so that
# is the test used here too.
NO_SCAN_HI = 0xFF
VANILLA_HALF = 112          # 02:4020  ld e,112
RAMP = 8                    # the scanner's own 8 px alignment quantum
WIDTH = 512                 # 32:9
SKEW = 4                    # host frames of pacing slip tolerated between runs

ATTRACT_PLAY = 1140         # the title demo reaches scrolling gameplay by here
LONG_FRAMES = 16000         # a full attract cycle, the run that exercises the
                            # debounce window (see probe_dx_flicker.py)


def scanner_trace(probe, frames):
    """Frame-by-frame ledger of what the ROM's spawn scanner did.

    Returns the per-frame rows plus the set of list records it spawned, the set
    it consumed without spawning, and -- the number that matters -- the ones the
    edge actually stepped OVER between two frames, which are entries this route
    can never get back.
    """
    raw = probe.ram(LIST, LIST_SIZE)
    x_of = {LIST + o: (raw[o] << 8) | raw[o + 1]
            for o in range(0, LIST_SIZE, RECORD) if raw[o] != 0xFF}
    cursor = None
    last_edge = {1: None, 0xFF: None}
    rows, spawned, jumped, skipped, lurched = [], {}, set(), [], []
    reach = 0
    for _ in range(frames):
        probe.step(1)
        window = probe.ram(SCAN_EDGE, 0x30)
        cam = int.from_bytes(probe.ram(0xFFCA, 2), "little")
        edge = ((window[0] << 8) | window[1]) & 0xFFF8
        direction = window[SCAN_DIR - SCAN_EDGE]
        cur = (window[CURSOR - SCAN_EDGE] << 8) | window[CURSOR - SCAN_EDGE + 1]
        scanning = window[0] != NO_SCAN_HI
        previous = last_edge.get(direction) if scanning else None
        moved = []
        if cursor is not None and cur != cursor and (cur - cursor) % RECORD == 0:
            stride = RECORD if cur > cursor else -RECORD
            for k in range((cur - cursor) // stride):
                addr = cursor + stride * k
                x = x_of.get(addr)
                if x is None:
                    continue
                if x == edge:
                    spawned.setdefault(addr, cam)
                    reach = max(reach, abs(x - cam))
                    moved.append(("spawn", addr, x))
                else:
                    jumped.add(addr)
                    moved.append(("eaten", addr, x))
                    # Stepped over: the edge was behind it the last time this
                    # direction scanned and is past it now. Anything else is the
                    # cursor seeking forward to the camera at level entry.
                    if previous is not None and (
                            (stride > 0 and x > previous) or (stride < 0 and x < previous)):
                        skipped.append(dict(addr=addr, x=x, cam=cam, edge=edge,
                                            previous=previous, frame=len(rows)))
        # The ramp invariant, measured straight off the guest: between two
        # frames that scan the same way the edge may not move more than 8 px,
        # or an 8-px-aligned list entry can fall between them unseen.
        if scanning and direction in last_edge:
            if previous is not None and abs(edge - previous) > RAMP:
                lurched.append(dict(frame=len(rows), cam=cam, edge=edge,
                                    previous=previous, dir=direction))
            last_edge[direction] = edge
        rows.append(dict(cam=cam, edge=edge, cursor=cur, dir=direction,
                         scanning=scanning, moved=moved))
        cursor = cur
    return dict(rows=rows, spawned=spawned, jumped=jumped, skipped=skipped,
                lurched=lurched, reach=reach, x_of=x_of)


def vanilla_edge_violations(trace):
    """Frames where the edge was NOT the ROM's own camX +- 112, 8-px aligned.
    Empty is the whole claim behind the Original policy."""
    bad = []
    for i, r in enumerate(trace["rows"]):
        if not r["scanning"] or r["dir"] not in (1, 0xFF):
            continue
        want = ((r["cam"] + VANILLA_HALF) if r["dir"] == 1
                else (r["cam"] - VANILLA_HALF)) & 0xFFF8
        if r["edge"] != want:
            bad.append(dict(frame=i, cam=r["cam"], edge=r["edge"], want=want, dir=r["dir"]))
    return bad


def run(name, aspect, spawns, frames, route=ROUTE, settle=PLAY_FRAME - 40):
    probe = Probe(name, aspect=aspect, route=route,
                  env={"SML2_SPAWNS": spawns} if spawns else None)
    try:
        assert probe.start_frame < settle, (
            name, f"probe attached at frame {probe.start_frame}, past the settle point")
        probe.run_to(settle)
        trace = scanner_trace(probe, frames)
        trace["view"] = probe.capture(name)
        trace["ledger"] = probe.command("sml2_spawn_state")
        trace["mod_state"] = probe.command("sml2_mod_state")
    finally:
        probe.close()
    return trace


def long_run(name, spawns, dx):
    """Free-run a whole attract cycle and QUERY the always-on ledger.

    This is the run that actually exercises the debounce window: the demo
    changes scene repeatedly, so the gate goes false for runs shorter than six
    frames and the view stays wide on a frozen world. Nothing is armed and
    nothing is stepped -- the ledger has been filling since the body booted, so
    the counts are exact rather than sampled.
    """
    probe = Probe(name, aspect="32:9", route="",
                  env={"SML2_SPAWNS": spawns, "SML2_DX": "1" if dx else "0"})
    try:
        while probe.view()["frame"] < LONG_FRAMES:
            probe.run_to(min(LONG_FRAMES, probe.view()["frame"] + 1000))
        view = probe.view()
        ledger = probe.command("sml2_spawn_state")
        body = probe.command("sml2_mod_state")["body"]
    finally:
        probe.close()
    return dict(body=body, frames=view["frame"], spawns=spawns,
                debounced=view["debounced"], narrowed=view["narrowed"],
                pillarbox_model=view["pillarbox_model"], flips=view["flips"],
                spawn_extend=view["spawn_extend"], spawn_reach=view["spawn_reach"],
                spawn_reads=view["spawn_reads"], spawn_ungated=view["spawn_ungated"],
                spawn_unpaired=view["spawn_unpaired"], spawn_resets=view["spawn_resets"],
                passed=ledger["passed"], spawned=ledger["spawned"],
                jumped=ledger["jumped"], seek=ledger["seek"],
                stepped_over=ledger["stepped_over"], scans=ledger["scans"])


def summary(trace):
    return dict(spawned=len(trace["spawned"]), eaten=len(trace["jumped"]),
                stepped_over=len(trace["skipped"]), edge_lurches=len(trace["lurched"]),
                max_spawn_distance=trace["reach"],
                camera=[trace["rows"][0]["cam"], trace["rows"][-1]["cam"]])


def main():
    frames = 900
    results = {}

    off = run("spawns-off", "off", None, frames)
    original = run("spawns-original", "32:9", "original", frames)
    extended = run("spawns-extended", "32:9", "extended", frames)
    for tag, t in (("off", off), ("original", original), ("extended", extended)):
        results[tag] = summary(t)

    # 1. In NO policy may the edge step over a list entry, and in no policy may
    #    it move more than the scanner's own 8 px quantum between two frames
    #    that scan the same way. This is the failure a naive widening causes.
    for tag, t in (("off", off), ("original", original), ("extended", extended)):
        assert not t["skipped"], (tag, "the scan edge stepped over list entries",
                                  t["skipped"][:6])
        assert not t["lurched"], (tag, "the scan edge moved more than 8 px in a frame",
                                  t["lurched"][:6])

    # 2. Original is vanilla, proved two ways: the edge is the ROM's own
    #    camX +- 112 on every single frame, and the same records are spawned at
    #    the same camera positions as with the mod off.
    results["off_edge_violations"] = vanilla_edge_violations(off)
    results["original_edge_violations"] = vanilla_edge_violations(original)
    assert not results["off_edge_violations"], results["off_edge_violations"][:4]
    assert not results["original_edge_violations"], ("Original moved the scan edge",
                                                     results["original_edge_violations"][:4])
    assert set(original["spawned"]) == set(off["spawned"]), (
        "Original spawned a different set of records than the mod off",
        sorted(set(original["spawned"]) ^ set(off["spawned"])))
    assert original["jumped"] == off["jumped"], "Original consumed a different set"
    assert original["reach"] <= VANILLA_HALF + RAMP, ("Original reached past vanilla",
                                                      original["reach"])
    for addr, cam in original["spawned"].items():
        assert abs(cam - off["spawned"][addr]) <= SKEW, (
            "Original spawned a record at a different camera position", addr, cam,
            off["spawned"][addr])
    skew = max(abs(a["cam"] - b["cam"])
               for a, b in zip(off["rows"], original["rows"]))
    results["off_vs_original_camera_skew"] = skew
    assert skew <= SKEW, ("Original changed the simulation, not just host frame pacing", skew)

    # 3. Extended spawns a superset and never eats one Original kept.
    #
    #    Compared on the module's ALWAYS-ON ledger, which has been tagging every
    #    record since the level loaded -- not on the traced window, which would
    #    miss anything Extended reached before the window opened, and not on a
    #    frame index, since Extended's extra enemies shift the run slightly. The
    #    comparison is bounded by the camera each run actually reached: a record
    #    the vanilla edge never got within 112 px of was never Original's to
    #    spawn.
    def tagged(trace, bit):
        return {r["a"] for r in trace["ledger"]["records"] if r["seen"] & bit}

    x_of = {r["a"]: r["x"] for r in original["ledger"]["records"]}
    limit = min(original["rows"][-1]["cam"], extended["rows"][-1]["cam"]) + VANILLA_HALF
    orig_spawned, ext_spawned = tagged(original, 1), tagged(extended, 1)
    ext_eaten_only = tagged(extended, 2) - ext_spawned
    in_range = {a for a in orig_spawned if x_of.get(a, 0) <= limit}
    results["camera_limit"] = limit
    results["original_records"] = sorted(in_range)
    results["extended_records"] = sorted(ext_spawned)
    results["extended_missed"] = sorted(in_range - ext_spawned)
    results["extended_ate_a_kept_record"] = sorted(in_range & ext_eaten_only)
    assert not results["extended_missed"], (
        "Extended failed to spawn a record Original spawned", results["extended_missed"])
    assert not results["extended_ate_a_kept_record"], (
        "Extended ate a record Original spawned", results["extended_ate_a_kept_record"])
    # Earlier, never later: inside the traced window, where both runs recorded a
    # camera position for the spawn, Extended must reach a record no further
    # along the level than Original did.
    both = sorted(set(original["spawned"]) & set(extended["spawned"]))
    results["extended_lead"] = {str(a): original["spawned"][a] - extended["spawned"][a]
                                for a in both}
    assert all(v >= -SKEW for v in results["extended_lead"].values()), results["extended_lead"]

    # 4. Extended actually reaches the wide view edge. The vanilla scanner sits
    #    112 px past the camera centre; at 32:9 the view reaches 176 px further
    #    still once centred, and up to 352 while clamped against a level wall.
    view = extended["view"]
    assert view["width"] == WIDTH and view["valid"] == 1, view
    assert view["spawn_extend"] == 1, view
    assert extended["reach"] > VANILLA_HALF + 128, (
        "Extended never spawned meaningfully past the native edge", extended["reach"])
    assert view["spawn_reach"][0] >= (WIDTH - 160) // 2, (
        "the ramp never reached the view edge", view)
    assert view["spawn_unpaired"] == 0, ("edge high/low byte reads did not pair", view)
    assert view["spawn_reads"][0] == view["spawn_reads"][1] > 0, view   # right hi == lo
    assert view["spawn_reads"][2] == view["spawn_reads"][3], view       # left  hi == lo
    assert original["view"]["spawn_extend"] == 0, original["view"]
    assert original["view"]["spawn_reads"] == [0, 0, 0, 0], (
        "Original installed a spawn override", original["view"])
    assert off["mod_state"]["spawns_hooked"] == 0 and off["view"]["width"] == 160, off["view"]
    results["extended_view"] = view
    results["original_view"] = original["view"]

    # 5. The module's own always-on ledger must agree with this RAM-derived one.
    for tag, t in (("original", original), ("extended", extended)):
        ledger = t["ledger"]
        results[tag + "_ledger"] = dict(passed=ledger["passed"], spawned=ledger["spawned"],
                                        jumped=ledger["jumped"], scans=ledger["scans"],
                                        skip_total=ledger["skip_total"])
        seen = {r["a"] for r in ledger["records"] if r["seen"] & 1}
        assert set(t["spawned"]) <= seen, (tag, "module ledger missed a spawn",
                                           sorted(set(t["spawned"]) - seen))
        assert ledger["skip_total"] == ledger["jumped"], (tag, ledger)
        assert ledger["extend"] == (1 if tag == "extended" else 0), (tag, ledger)

    # 6. Attract mode: the title demo plays Level 1 with no input at all, which
    #    is where the pop-in was noticed. Both policies must hold there too.
    attract = {}
    for tag, spawns in (("original", "original"), ("extended", "extended")):
        t = run("spawns-attract-" + tag, "32:9", spawns, 600, route="",
                settle=ATTRACT_PLAY + 60)
        attract[tag] = summary(t)
        # `spawned` counts only what the traced window saw; the ledger has been
        # running since the demo loaded, and Extended reaches records before the
        # window opens, so the two numbers are not the same measurement.
        attract[tag].update(valid=t["view"]["valid"], mode=t["view"]["mode"],
                            reach=t["view"]["spawn_reach"],
                            ledger_spawned=t["ledger"]["spawned"],
                            ledger_jumped=t["ledger"]["jumped"])
        assert not t["skipped"], ("attract", tag, t["skipped"][:6])
        assert not t["lurched"], ("attract", tag, t["lurched"][:6])
        assert t["view"]["valid"] == 1, ("attract demo is not composing wide", t["view"])
        assert not vanilla_edge_violations(t) or tag == "extended", t
    results["attract"] = attract
    assert attract["extended"]["max_spawn_distance"] > attract["original"]["max_spawn_distance"], (
        "attract demo: Extended did not reach further than Original", attract)
    assert attract["original"]["max_spawn_distance"] <= VANILLA_HALF + RAMP, attract
    assert attract["extended"]["ledger_spawned"] >= attract["original"]["ledger_spawned"], attract
    assert attract["extended"]["ledger_jumped"] == attract["original"]["ledger_jumped"] == 0, attract

    # 7. The debounce window. The gate can go false for a few frames at a time
    #    while the view stays wide on a frozen world (see "The fallback is
    #    debounced" in ADAPTIVE.md). Pixels may be stale there; a spawn may
    #    not, because the scanner consumes. Over a whole attract cycle on both
    #    bodies: the window is genuinely entered, and NOTHING is consumed
    #    without spawning in either policy.
    long = {}
    for body, dx in (("faithful", False), ("dx", True)):
        for spawns in ("original", "extended"):
            key = f"{body}-{spawns}"
            r = long[key] = long_run(f"spawns-long-{key}", spawns, dx)
            assert r["frames"] >= LONG_FRAMES, (key, r)
            # `jumped` is NOT the number to assert on over a multi-level run:
            # every level load makes the cursor seek forward from the list head
            # to wherever the camera starts, consuming everything behind it, in
            # vanilla exactly as much as here. The number that matters is the
            # one the ramp exists to hold down -- entries the edge CROSSED --
            # and Extended may not have more of them than vanilla does.
            assert r["seek"] + r["stepped_over"] == r["jumped"], (key, r)
            # The whole guarantee, over a full attract cycle, in both policies:
            # the scan edge never crossed a list entry. `jumped` is much larger
            # and says nothing -- it is dominated by the cursor seeking from the
            # list head to the camera at every one of the demo's level loads,
            # which vanilla does too.
            assert r["stepped_over"] == 0, (
                key, "the scan edge crossed a list entry", r)
            assert r["spawn_unpaired"] == 0, (key, r)
            assert r["pillarbox_model"] == 0, (key, r)
            if spawns == "original":
                assert r["spawn_reads"] == [0, 0, 0, 0] and r["spawn_ungated"] == 0, (key, r)
    results["long"] = long
    assert any(r["debounced"] > 0 for r in long.values()), (
        "no debounce window was entered, so this section proved nothing", long)
    # NOT asserted here: that Extended spawned at least as many records as
    # Original. The attract demo replays a fixed input script, so the extra
    # enemies Extended spawns change what the demo Mario runs into and the two
    # runs end up in different segments of it -- `narrowed` and `flips` differ
    # too. The superset claim is section 3's, on the controlled single-level
    # route where both runs see the same world. What this section proves is the
    # loss metric, which is meaningful however far the runs diverge.
    for body in ("faithful", "dx"):
        ext = long[body + "-extended"]
        assert ext["spawn_reach"][0] > 0, (body, "the ramp never widened", ext)

    (ROOT / "logs/spawns-probe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
