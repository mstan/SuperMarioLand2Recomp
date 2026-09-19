"""Mario's fireballs must die where the composed view ends, and must be drawn
for exactly as long as they are alive.

Vanilla destroys a fireball when its EIGHT-BIT screen X lands in $C0..$CF
(00:327E `cp $C0`), which is world X = camX + 112..127 going right and
camX - 129..144 going left. In a 512-pixel view that is the middle of the
picture, so Fire Mario's shots wink out halfway across the screen. Under
`Enemy spawns = Extended` the compositor overrides that one immediate (see
sml2_map.h, SML2_FB_CP_X_PC) and the slot instead dies SML2_FIREBALL_OVERSHOOT
pixels past the edge of the composed view.

What this asserts, per body and per direction:

  * drawn <-> alive, every frame. While the slot is live the composed piece
    list carries its pieces AT its own world position and nowhere else; the
    frame it dies they are gone. A piece somewhere else is the exact failure
    an 8-bit screen X invites, because a wrapped OAM X puts a ghost 256 px
    away -- inside the native strip, which the compositor blits from the
    hardware framebuffer, so the ghost would be real pixels.
  * Original is vanilla: the death lands in the ROM's own $C0..$CF window, and
    the death world X and the number of frames the slot lived match a run of
    the same script with the whole mod off.
  * Extended dies just past the view edge, on both sides, and the boundary
    MOVES when the view width does -- which is what separates "the override
    fired" from "it happened to hit a wall".
  * an enemy beyond the vanilla death point is reached only if the fireball
    gets that far.

Method note: a fireball moves 3 px/frame and the view edge can be 256 px away,
so a shot fired the way Mario is running meets terrain long before it meets the
boundary. Every case fires BACKWARDS over ground Mario has just crossed and
then walks away from it.

Requires a native Windows Python (CREATE_NO_WINDOW).
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ROUTE, ACTORS, ACTOR_STRIDE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "recomp/fixtures"
DX_STATE = FIXTURES / "dx_under_pipe_repro.state1"

POWERUP = 0xA216          # sCurPowerup; 3 is Fire Mario
FIREBALL = 0xA880         # two slots, stride $10
SLOTS, STRIDE = 2, 0x10
OVERSHOOT = 32            # SML2_FIREBALL_OVERSHOOT
VANILLA_RIGHT = 112       # worldX - camX where the ROM's own window opens
VANILLA_LEFT = 129        # camX - worldX where it opens on the other side
VANILLA_WINDOW = (0xC0, 0xCF)   # the screen-X band 00:327E destroys in

WALK_AWAY = 70            # frames spent clearing ground before each shot
TURN = 6                  # frames facing the way the shot will go
CHASE = 320            # frames to follow one shot for
ATTEMPTS = 4          # shots per direction; terrain ends most of them early

# The pieces of a fireball. 00:3293 asks the shared emitter for metasprite
# $B0..$B7 and nothing else uses that range; on both bodies those metasprites
# are built from tiles 112..115, two per animation frame. Asserted rather than
# assumed -- a run that saw none of them while a slot was live fails on "alive
# but not drawn", and the tiles it did see are reported.
FB_TILES = (112, 113, 114, 115)
# How far a piece of a live fireball may sit from the slot's own world
# position: the metasprite is 8 wide and 16 tall, drawn up and left of the
# origin, and the composed list comes from the emitter pass of the frame being
# shown, which can be one 3 px step behind the slot the probe just read.
NEAR_X = (-28, 16)
NEAR_Y = (-48, 8)


def stream(p, cmd, **kw):
    """A game command that answers with several lines sharing one id."""
    p.id += 1
    want = p.id
    p.sock.sendall((json.dumps(dict(cmd=cmd, id=want, **kw)) + "\n").encode())
    out = []
    while True:
        r = p.line()
        if r.get("id") != want:
            continue
        out.append(r)
        if r.get("end"):
            return out


def slots(p):
    """Both $A880 slots, read straight out of cart RAM.

    `peek` and not `read_ram`: the first bypasses gb_read8 and so any custom
    read override, which means what comes back is the ROM's own state and never
    something the module under test answered."""
    raw = bytes.fromhex(p.command("peek", addr="%04x" % FIREBALL,
                                  len=SLOTS * STRIDE)["hex"])
    return [dict(slot=i, live=raw[i * STRIDE],
                 y=raw[i * STRIDE + 1] | (raw[i * STRIDE + 2] << 8),
                 x=raw[i * STRIDE + 3] | (raw[i * STRIDE + 4] << 8),
                 dir=raw[i * STRIDE + 5])
            for i in range(SLOTS)]


def live_slot(p):
    for sl in slots(p):
        if sl["live"]:
            return sl
    return None


def actors(p):
    """Live actors as {slot: (world_x, world_y)}."""
    return {slot: (x, y) for slot, x, y in p.actors()}


def camera(p):
    hexed = p.command("peek", addr="ffca", len=2)["hex"]
    return int(hexed[0:2], 16) | (int(hexed[2:4], 16) << 8)


MODE = 0xFF9B             # hGameMode; 4 is scrolling gameplay


def reach_gameplay(p, budget=3000, chunk=30):
    """Advance until the game is in scrolling gameplay, entering a level if it
    is sitting on the world map.

    Probe.ensure_gameplay() drives off sml2_view, which only says anything when
    the compositor is installed -- so it cannot steer a mod-off run, and a
    mod-off run that reached gameplay by a DIFFERENT number of frames could not
    be compared with an Original one anyway. This reads the game's own mode
    byte, so both runs take the identical path."""
    spent = 0
    while spent < budget:
        mode = int(p.command("peek", addr="%04x" % MODE, len=1)["hex"], 16)
        if mode == 4:
            return mode
        p.command("set_input", buttons="A" if mode in (12, 25, 26) else "-")
        p.step(2)
        p.command("set_input", buttons="-")
        p.step(chunk)
        spent += chunk + 2
    raise RuntimeError("never reached scrolling gameplay within %d frames"
                       % budget)


def ring_seq(p):
    """Where the always-on death ring stands, so one shot's deaths can be told
    from the previous shot's."""
    return stream(p, "sml2_fireballs")[-1]["seq"]


class Shot:
    def __init__(self, direction):
        self.direction = direction
        self.frames = []
        self.death = None
        self.last = None
        self.first = None
        self.violations = []
        self.offsets = []
        self.tiles = []
        self.actors = {}
        self.derived_x = None
        self.derived_rel = None


def fire(p, direction):
    away = "L" if direction == "R" else "R"
    p.command("write_ram", addr="%04x" % POWERUP, val=3)
    p.command("set_input", buttons=away)
    p.step(WALK_AWAY)
    p.command("set_input", buttons=direction)
    p.step(TURN)
    p.command("write_ram", addr="%04x" % POWERUP, val=3)
    p.command("set_input", buttons=direction + "B")
    p.step(1)
    p.command("set_input", buttons=away)


def fireball_pieces(rows, sl, src):
    """Composed pieces that belong to a fireball, split into "at the slot" and
    "somewhere else"."""
    mine = [r for r in rows if r.get("src") == src and r.get("tile") in FB_TILES]
    if sl is None:
        return [], mine
    near, far = [], []
    for r in mine:
        dx, dy = r["x"] - sl["x"], r["y"] - sl["y"]
        (near if NEAR_X[0] <= dx <= NEAR_X[1] and NEAR_Y[0] <= dy <= NEAR_Y[1]
         else far).append(r)
    return near, far


def observe(p, shot, wide, watch_actors, since):
    """Follow one shot until the slot is empty, then one frame more: the
    composed list is built from the emitter pass of the frame being shown, so
    the frame the slot dies only reaches the screen after one more step."""
    empty = 0
    for _ in range(CHASE):
        p.step(1)
        sl = live_slot(p)
        cam = camera(p)
        frame = p.command("frame")["frame"]
        if sl is None:
            # Two things lag the slot read by exactly one frame, and both are
            # why this steps once more instead of asking straight away:
            #   * the composed sprite list is copied at PPU line 0 from the
            #     emitter pass of the frame before, so the picture that has no
            #     fireball in it has not been built yet;
            #   * so is the death ring's slot diff, which is what records a
            #     death the 00:327E override did not decide (a block, an
            #     enemy, the vertical window).
            # The extra step is taken WHETHER OR NOT the view is wide, so a
            # mod-off run and an Original run of the same script advance the
            # guest by exactly the same number of frames. They did not, once:
            # the wide run stepped one frame more after its first shot, Mario
            # walked 2 px further before the second, and the two runs' second
            # deaths "disagreed" by 2 px with nothing wrong at all.
            empty += 1
            if empty >= 2:
                if wide:
                    rows = stream(p, "sml2_sprites")
                    left = (fireball_pieces(rows, None, "tap")[1] +
                            fireball_pieces(rows, None, "oam")[1])
                    if left:
                        shot.violations.append(
                            ("dead but still drawn",
                             dict(frame=frame, cam=cam),
                             [(r["x"], r["tile"], r["src"]) for r in left[:6]]))
                break
            continue
        if wide:
            rows = stream(p, "sml2_sprites")
            near, far = fireball_pieces(rows, sl, "tap")
            oam_near, oam_far = fireball_pieces(rows, sl, "oam")
            here = dict(frame=frame, x=sl["x"], cam=cam)
            if len(near) < 2:
                shot.violations.append(
                    ("alive but not drawn", here, len(near), len(far)))
            if far or oam_far:
                shot.violations.append(
                    ("fireball piece composed away from the slot", here,
                     [(r["x"] - sl["x"], r["tile"], r["src"])
                      for r in (far + oam_far)[:6]]))
            if near:
                shot.offsets.append((min(r["x"] for r in near) - sl["x"],
                                     max(r["x"] for r in near) - sl["x"]))
                for r in near:
                    if r["tile"] not in shot.tiles:
                        shot.tiles.append(r["tile"])
        sample = dict(frame=frame, x=sl["x"], y=sl["y"], dir=sl["dir"], cam=cam,
                      rel=sl["x"] - cam + 80)
        if wide:
            view = p.command("sml2_view")
            sample.update(view_left=view["view_left"], width=view["width"],
                          reach=view["fb_reach"], valid=view["valid"],
                          wide=view["wide"])
        if shot.first is None:
            shot.first = sample
        shot.last = sample
        shot.frames.append(sample)
        if watch_actors:
            now = actors(p)
            for i, pos in now.items():
                rec = shot.actors.setdefault(
                    i, dict(first=pos, first_cam=cam, first_frame=frame))
                rec["last"] = pos
            for i, rec in shot.actors.items():
                if i not in now and "gone" not in rec:
                    ax, ay = rec["last"]
                    rec["gone"] = dict(frame=frame, cam=cam,
                                       actor=(ax, ay), fireball=(sl["x"], sl["y"]),
                                       dx=sl["x"] - ax, dy=sl["y"] - ay)

    # The slot the probe finds empty was zeroed during the frame it just
    # stepped, AFTER 00:332E had already advanced the position by its 3 px, so
    # the world X the despawn test actually saw is one step past the last live
    # sample. Cross-checked below against the ring's own record wherever there
    # is one -- which is what makes the same derivation trustworthy on a
    # mod-off run, where there is no ring at all.
    if shot.last:
        step = 3 if shot.direction == "R" else -3
        shot.derived_x = shot.last["x"] + step
        shot.derived_rel = shot.derived_x - camera(p) + 80
    if wide:
        deaths = [r for r in stream(p, "sml2_fireballs", since=since)
                  if r.get("death")]
        if deaths:
            shot.death = deaths[-1]
    return shot


def run_case(tag, body, aspect, spawns, directions=("R", "L"), watch_actors=False):
    wide = aspect != "off"
    env = {"SML2_DX": "1" if body == "dx" else "0", "SML2_SPAWNS": spawns}
    p = Probe(tag, aspect=aspect, route="" if body == "dx" else ROUTE, env=env)
    out = dict(tag=tag, body=body, aspect=aspect, spawns=spawns, shots=[])
    try:
        if body == "dx":
            # The owner's savestate, staged where the body looks for it. Probe
            # rmtree's the folder per run, so no stray .sav can change what the
            # state loads into.
            shutil.copy2(DX_STATE, p.folder / "Super_Mario_Land_2_DX.state1")
            p.command("load_state", slot=0)
            p.step(4)
        else:
            # The faithful body cannot load the DX fixture, so it goes in by
            # the shared route and is then steered by the game's own mode byte.
            p.run_to(2700)
            reach_gameplay(p)
        for d in directions:
            # Terrain, not the boundary, is what usually ends a shot: a block
            # or an enemy in the way destroys it early, and where those are is
            # the level's business. So fire again from further along until one
            # shot gets the whole way, and keep every attempt in the record --
            # a case that never manages it is a reported fact, not a silent
            # pass. The retry is driven by the guest's own outcome, so both
            # runs of a mod-off/Original pair take the same number of them.
            attempts = []
            for _ in range(ATTEMPTS):
                since = ring_seq(p) if wide else 0
                fire(p, d)
                shot = observe(p, Shot(d), wide, watch_actors, since)
                rec = dict(
                    direction=d, tiles=sorted(shot.tiles),
                    frames=len(shot.frames),
                    derived_x=shot.derived_x, derived_rel=shot.derived_rel,
                    first=shot.first, last=shot.last, death=shot.death,
                    violations=shot.violations,
                    offsets=sorted(set(shot.offsets)),
                    actors=shot.actors)
                attempts.append(rec)
                # The same acceptance test in every case, so a mod-off run and
                # an Original run retry in lockstep: for a vanilla policy it is
                # the ROM's own $C0..$CF window, which a mod-off run can also
                # measure; for Extended it is the ring saying the widened
                # boundary is what fired.
                if spawns == "Original" or not wide:
                    if shot.derived_rel is not None and \
                            VANILLA_WINDOW[0] <= (shot.derived_rel & 0xFF) <= VANILLA_WINDOW[1]:
                        break
                elif shot.death and shot.death["cause"] == "extended":
                    break
            best = attempts[-1]
            best["attempts"] = len(attempts)
            best["rejected"] = [dict(x=a["derived_x"],
                                     cause=a["death"]["cause"] if a["death"] else None,
                                     frames=a["frames"])
                                for a in attempts[:-1]]
            out["shots"].append(best)
        if wide:
            v = p.command("sml2_view")
            out["view"] = {k: v[k] for k in (
                "fb_kept", "fb_killed", "fb_vanilla", "fb_failclosed",
                "fb_unmatched", "fb_hidden", "fb_deaths", "width")}
    finally:
        p.close()
    return out


def summarise(case):
    for shot in case["shots"]:
        shot["death_x"] = shot["derived_x"]
        shot["death_rel"] = shot["derived_rel"]
        shot["cause"] = shot["death"]["cause"] if shot["death"] else "n/a"
        if shot["death"] and shot["derived_x"] is not None:
            # The ring recorded the position the compare itself saw. If the
            # derivation disagrees, every mod-off number in the table is
            # suspect, so it is an assertion and not a footnote.
            shot["ring_vs_derived"] = shot["death"]["x"] - shot["derived_x"]
        if shot["last"]:
            shot["reach"] = shot["last"].get("reach")
    return case


def check(case, failures):
    def fail(msg, *ctx):
        failures.append((case["tag"], msg) + ctx)

    for shot in case["shots"]:
        d, last, death = shot["direction"], shot["last"], shot["death"]
        if last is None:
            fail("no fireball was ever live", d)
            continue
        if shot["violations"]:
            fail("drawn/alive mismatch", d, len(shot["violations"]),
                 shot["violations"][:2])
        if shot.get("ring_vs_derived") not in (None, 0):
            fail("the ring and the derived death point disagree", d,
                 shot["ring_vs_derived"], death, last)
        if case["spawns"] == "Original" or case["aspect"] == "off":
            lo, hi = VANILLA_WINDOW
            if not lo <= (shot["death_rel"] & 0xFF) <= hi:
                fail("vanilla death outside the $C0..$CF window", d,
                     shot["death_rel"])
            # camX + 112 is where the window OPENS; which value inside it the
            # slot lands on depends on the phase between its own 3 px step and
            # the camera's, so the window is the assertion and the exact
            # offset is reported.
            if d == "R" and not VANILLA_RIGHT <= shot["death_rel"] - 80 \
                    <= VANILLA_RIGHT + 15:
                fail("vanilla right-hand death outside camX+112..+127", d,
                     shot["death_rel"] - 80)
            if death and death["cause"] != "vanilla":
                fail("a vanilla run reported a non-vanilla cause", d, death)
        else:
            if not death:
                fail("Extended: no death recorded in the ring", d, last)
                continue
            if death["cause"] != "extended":
                fail("Extended death did not come from the widened boundary",
                     d, death)
                continue
            edge = (death["view_left"] - OVERSHOOT if d == "L" else
                    death["view_left"] + death["view_width"] + OVERSHOOT)
            # At most one 3 px step past the boundary: the slot is tested once
            # a frame and moves 3 px between tests.
            gap = death["x"] - edge if d == "R" else edge - death["x"]
            if not 0 <= gap <= 3:
                fail("death is not just past the view edge", d, gap,
                     death["x"], edge)
            if abs(shot["death_rel"] - 80) <= VANILLA_RIGHT:
                fail("Extended never got the shot past the vanilla range", d,
                     shot["death_rel"] - 80)


def report_actors(case):
    """An enemy beyond the vanilla death point is only struck if the fireball
    gets that far.

    An actor slot emptying is not by itself a kill -- the activation window
    frees them too -- so a STRIKE is an actor that vanished on a frame when the
    fireball was on top of it. The claim under test is one-directional and
    provable: Extended can strike an enemy past camX + 112, Original cannot,
    because its fireball has already been destroyed by then."""
    out = []
    for shot in case["shots"]:
        cam0 = shot["first"]["cam"] if shot["first"] else None
        for slot, rec in (shot.get("actors") or {}).items():
            ax = rec["last"][0]
            beyond = (ax - cam0 - VANILLA_RIGHT if shot["direction"] == "R"
                      else cam0 - VANILLA_LEFT - ax) if cam0 is not None else None
            gone = rec.get("gone")
            struck = bool(gone and abs(gone["dx"]) <= 16 and abs(gone["dy"]) <= 24)
            if beyond is None or beyond <= 0:
                continue
            out.append(dict(slot=slot, direction=shot["direction"], actor_x=ax,
                            past_vanilla_by=beyond, struck=struck,
                            reached=shot["death_x"] is not None and (
                                shot["death_x"] >= ax if shot["direction"] == "R"
                                else shot["death_x"] <= ax),
                            gone=gone))
    return out


MARIO_X, MARIO_Y = 0xFFC2, 0xFFC0


def actor_state(p, slot):
    """The actor's own state byte (+8): 0 free, 1 dormant, 2 live. An actor
    leaving the live set is not proof of a kill -- the cull window frees them
    too -- so the state it landed in is reported next to the distance."""
    addr = ACTORS + slot * ACTOR_STRIDE
    return bytes.fromhex(p.command("peek", addr="%04x" % addr, len=16)["hex"])[8]


def mario(p):
    hexed = p.command("peek", addr="%04x" % MARIO_Y, len=4)["hex"]
    b = bytes.fromhex(hexed)
    return (b[2] | (b[3] << 8), b[0] | (b[1] << 8))


def enemy_case(tag, body, aspect, spawns, budget=120, chunk=10, tries=3):
    """Walk until a live enemy is standing BEYOND the vanilla death point, then
    shoot at it and see how far the shot gets.

    This is the difference a player would notice, and the half of it that can be
    asserted is one-directional: with Extended a fireball can still be alive
    past camX + 112 and keep going; with Original it has been destroyed by then
    and cannot, whatever the view is doing. The widened ACTIVATION window is
    installed in BOTH policies, so both runs have live actors out there to aim
    at -- only the shot's reach differs.

    Whether the shot actually kills what it reaches is reported, not asserted:
    the fireball hugs the ground and bounces, so whether it meets a particular
    enemy is the level's geometry talking, and a block in between ends the
    flight early. Each attempt records how close it got."""
    env = {"SML2_DX": "1" if body == "dx" else "0", "SML2_SPAWNS": spawns}
    p = Probe(tag, aspect=aspect, route="" if body == "dx" else ROUTE, env=env)
    out = dict(tag=tag, body=body, aspect=aspect, spawns=spawns, attempts=[],
               struck=False, best_rel=None)
    try:
        if body == "dx":
            shutil.copy2(DX_STATE, p.folder / "Super_Mario_Land_2_DX.state1")
            p.command("load_state", slot=0)
            p.step(4)
        else:
            p.run_to(2700)
            reach_gameplay(p)
        for _ in range(tries):
            target = None
            for _ in range(budget // tries):
                p.command("write_ram", addr="%04x" % POWERUP, val=3)
                p.command("set_input", buttons="R")
                p.step(chunk)
                cam = camera(p)
                mx, my = mario(p)
                here = actors(p)
                # Beyond where vanilla's own fireball dies, inside the widest
                # view, and at the height a ground-hugging shot travels at.
                cands = [(s, x, y) for s, (x, y) in here.items()
                         if cam + VANILLA_RIGHT + 12 <= x <= cam + 230
                         and abs(y - my) <= 20]
                if cands:
                    target = min(cands, key=lambda c: c[1])
                    break
            if target is None:
                continue
            slot, ax, ay = target
            cam = camera(p)
            since = ring_seq(p) if aspect != "off" else 0
            p.command("set_input", buttons="R")
            p.step(2)
            p.command("write_ram", addr="%04x" % POWERUP, val=3)
            p.command("set_input", buttons="RB")
            p.step(1)
            p.command("set_input", buttons="-")    # stand still: camera stops
            rec = dict(target=dict(slot=slot, x=ax, y=ay, cam=cam,
                                   past_vanilla_by=ax - cam - VANILLA_RIGHT),
                       struck=False, closest=None, reached=None)
            last = (ax, ay)
            for _ in range(CHASE):
                p.step(1)
                sl = live_slot(p)
                here = actors(p)
                if sl is None:
                    break
                rec["reached"] = max(rec["reached"] or sl["x"], sl["x"])
                if slot in here:
                    last = here[slot]
                    d = (abs(sl["x"] - last[0]), abs(sl["y"] - last[1]))
                    if rec["closest"] is None or sum(d) < sum(rec["closest"]):
                        rec["closest"] = d
                elif not rec["struck"]:
                    rec["struck"] = (abs(sl["x"] - last[0]) <= 16 and
                                     abs(sl["y"] - last[1]) <= 24)
                    rec["gone"] = dict(actor=last, fireball=(sl["x"], sl["y"]),
                                       state=actor_state(p, slot))
            rec["reached_target"] = rec["reached"] is not None and                 rec["reached"] >= ax - 8
            rec["max_rel"] = None if rec["reached"] is None else                 rec["reached"] - cam + 80
            rec["target_state"] = actor_state(p, slot)
            if aspect != "off":
                rec["deaths"] = [r for r in stream(p, "sml2_fireballs",
                                                   since=since) if r.get("death")]
            out["attempts"].append(rec)
            out["struck"] = out["struck"] or rec["struck"]
            if rec["max_rel"] is not None:
                out["best_rel"] = max(out["best_rel"] or 0, rec["max_rel"])
            if rec["struck"]:
                break
        if not out["attempts"]:
            out["note"] = "no enemy ever stood beyond the vanilla death point"
    finally:
        p.close()
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = args[0] if args else None
    enemies_only = "--enemies" in sys.argv
    cases = []
    for body in ("dx", "faithful"):
        if only and only != body:
            continue
        cases.append(("fb-%s-off" % body, body, "off", "Extended", ("R", "L"), True))
        cases.append(("fb-%s-orig" % body, body, "32:9", "Original", ("R", "L"), True))
        cases.append(("fb-%s-ext32" % body, body, "32:9", "Extended", ("R", "L"), True))
        cases.append(("fb-%s-ext16" % body, body, "16:9", "Extended", ("R", "L"), True))

    results, failures = [], []
    for tag, body, aspect, spawns, dirs, watch in ([] if enemies_only else cases):
        case = summarise(run_case(tag, body, aspect, spawns, dirs, watch))
        case["enemies"] = report_actors(case)
        check(case, failures)
        struck = [e for e in case["enemies"] if e["struck"]]
        case["struck_beyond_vanilla"] = struck
        if struck and (case["spawns"] == "Original" or case["aspect"] == "off"):
            failures.append((tag, "a vanilla fireball struck an enemy beyond its "
                                  "own death point", struck))
        results.append(case)
        print("%-18s %-5s %-9s %s" % (
            tag, aspect, spawns,
            "   ".join("%s: x=%s rel%+d cause=%s frames=%d viol=%d" % (
                s["direction"], s.get("death_x"),
                (s.get("death_rel") or 80) - 80, s.get("cause"), s["frames"],
                len(s["violations"])) for s in case["shots"])))
        if case.get("view"):
            print("%-18s %s" % ("", case["view"]))
        for e in case["enemies"]:
            print("%-18s enemy %s" % ("", e))
        if case["enemies"]:
            print("%-18s beyond-vanilla enemies: %d seen, %d struck" % (
                "", len(case["enemies"]), len(case["struck_beyond_vanilla"])))

    by_tag = {c["tag"]: c for c in results}

    def key(shot):
        return (shot.get("death_x"), shot["frames"])

    for body in ("dx", "faithful"):
        off, orig = by_tag.get("fb-%s-off" % body), by_tag.get("fb-%s-orig" % body)
        if off and orig:
            for a, b in zip(off["shots"], orig["shots"]):
                # Absolute frame numbers are not comparable: each run attaches
                # wherever --benchmark reached before the probe paused it. The
                # death world X and how long the slot lived are the guest's own
                # behaviour, and those must be identical.
                same = key(a) == key(b)
                print("%-18s %s mod-off=%s Original=%s %s" % (
                    "compare-" + body, a["direction"], key(a), key(b),
                    "MATCH" if same else "DIFFER"))
                if not same:
                    failures.append(("compare-" + body,
                                     "Original did not reproduce the mod-off death",
                                     a["direction"], key(a), key(b)))
        w32, w16 = by_tag.get("fb-%s-ext32" % body), by_tag.get("fb-%s-ext16" % body)
        if w32 and w16:
            for a, b in zip(w32["shots"], w16["shots"]):
                if a.get("death_x") is None or b.get("death_x") is None:
                    continue
                print("%-18s %s 512-wide x=%s vs 256-wide x=%s" % (
                    "width-" + body, a["direction"], a["death_x"], b["death_x"]))
                if a["death_x"] == b["death_x"]:
                    failures.append((
                        "width-" + body,
                        "the death point did not move when the view width did",
                        a["direction"], a["death_x"]))

    # The enemy cases: one Original and one Extended per body, aimed at a real
    # actor standing past the vanilla death point.
    enemies = []
    for body in ("dx", "faithful"):
        if only and only != body:
            continue
        for spawns in ("Original", "Extended"):
            e = enemy_case("fb-%s-enemy-%s" % (body, spawns.lower()), body,
                           "32:9", spawns)
            enemies.append(e)
            print("%-26s %-9s best_rel=%s struck=%s %s" % (
                e["tag"], spawns, e["best_rel"], e["struck"], e.get("note", "")))
            for a in e["attempts"]:
                print("%-26s   target=%s reached=%s reached_target=%s "
                      "closest=%s struck=%s state=%s" % (
                          "", a["target"], a["reached"], a["reached_target"],
                          a["closest"], a["struck"], a["target_state"]))
            if not e["attempts"]:
                continue
            if spawns == "Original":
                # Vanilla cannot keep a shot alive past its own window, so it
                # can never have been anywhere near a target beyond it.
                if e["best_rel"] is not None and e["best_rel"] - 80 > 127:
                    failures.append((e["tag"], "a vanilla fireball got past "
                                     "camX + 127", e["best_rel"] - 80))
                if e["struck"]:
                    failures.append((e["tag"], "a vanilla fireball struck an "
                                     "enemy beyond its own death point", e))
            else:
                if e["best_rel"] is None or e["best_rel"] - 80 <= VANILLA_RIGHT:
                    failures.append((e["tag"], "Extended never kept a shot "
                                     "alive past camX + 112 with a target out "
                                     "there", e))
    results.append(dict(tag="enemies", cases=enemies))

    out = ROOT / "logs/fireball-results.json"
    out.write_text(json.dumps(results, indent=1))
    print("\nwrote", out)
    if failures:
        print("\nFAILURES")
        for f in failures:
            print(" ", f)
        raise SystemExit(1)
    print("\nall fireball assertions passed")


if __name__ == "__main__":
    main()
