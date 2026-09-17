"""Pause and pipe transitions must not narrow the view.

Both were reported from live play on the DX body and both reproduce from the
owner's savestate fixture in `recomp/fixtures/`:

  * press Start -> the game goes to mode $08, which the scene gate refused; the
    6-frame debounce then expired and the view pillarboxed. Two transitions per
    pause, one on the way in and one on the way out.
  * press Down into a warp pipe -> `$A20E` is non-zero for about 24 frames,
    which simply outlasts the debounce. Two more transitions. The sub-room on
    the far side was never the problem: it scores 357/357 and composes wide.

Neither is a new scene. Both draw over a world this module has already proved,
so both are now held on the last accepted frame for as long as they last.

Asserted on the DX body from the fixture, and on the faithful body from the
shared route (which reaches pause; whether it reaches a pipe is reported):

  * zero wide <-> native transitions across pause and unpause;
  * zero across the pipe transition, and the sub-room composes wide;
  * `held_runs` actually counted the holds, so the assertions above are not
    passing because nothing happened.

Requires a native Windows Python (CREATE_NO_WINDOW).
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ENTER_FRAME, PLAY_FRAME  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "recomp/fixtures"
STATE = FIXTURES / "dx_pause_pipe_repro.state1"

# Level 1 by the shared route, then Start, then Down into whatever is under
# Mario. Used for the faithful body, which cannot load the DX fixture.
JUMPS = ",".join(f"{f}:A:18" for f in range(2620, 3400, 36))
PLAY_ROUTE = (f"60:S:4,140:S:4,220:S:4,{ENTER_FRAME}:A:4,"
              f"{PLAY_FRAME}:R:900," + JUMPS)


class PausePipe(Probe):
    def flip_log(self):
        self.id += 1
        self.sock.sendall((json.dumps(dict(cmd="sml2_flip_log", id=self.id)) + "\n").encode())
        head, events = None, []
        while True:
            r = self.line()
            if r.get("id") != self.id:
                continue
            if r.get("end"):
                return head, events
            if head is None:
                head = r
            else:
                events.append(r)

    def stage_fixture(self):
        """The owner's battery + savestate, where the body looks for them."""
        shutil.copy2(FIXTURES / "dx_pause_pipe_repro.sav",
                     self.folder / "Super_Mario_Land_2_DX.sav")
        shutil.copy2(FIXTURES / "dx_pause_pipe_repro.mods.ini",
                     self.folder / "sml2-mods.ini")
        (self.folder / "logs").mkdir(exist_ok=True)
        shutil.copy2(STATE, self.folder / "logs/probe.state")


def pause_cycle(p):
    """Start, hold a while, Start again. Returns flips spent."""
    before = p.view()["flips"]
    p.buttons(0x80)
    p.step(4)
    p.buttons(0)
    p.step(60)
    paused = p.view()
    p.buttons(0x80)
    p.step(4)
    p.buttons(0)
    p.step(40)
    resumed = p.view()
    return p.view()["flips"] - before, paused, resumed


def run_dx():
    p = PausePipe("pausepipe-dx", aspect="32:9", route="", env={"SML2_DX": "1"})
    try:
        p.stage_fixture()
        p.run_to(p.view()["frame"] + 30)
        assert p.command("sml2_load")["ok"], "the fixture savestate did not load"
        p.step(30)
        start = p.view()
        assert start["mode"] == 4 and start["wide"] == 1 and start["valid"] == 1, start

        flips, paused, resumed = pause_cycle(p)
        assert paused["mode"] == 8, ("Start did not pause", paused)
        assert paused["wide"] == 1, ("the view narrowed on pause", paused)
        assert paused["overlay"] == "mode", paused
        assert resumed["wide"] == 1 and resumed["valid"] == 1, resumed
        assert flips == 0, ("pause cost wide<->native transitions", flips)
        pause_result = dict(flips=flips, paused_mode=paused["mode"],
                            paused_wide=paused["wide"], held=paused["held"],
                            held_runs=paused["held_runs"],
                            paused_score=paused["score"])

        # Pipe: reload the same state and walk into it.
        p.command("sml2_load")
        p.step(30)
        before = p.view()
        held_before = before["held_runs"]
        p.buttons(0x08)
        seen_transition = False
        for _ in range(40):
            p.step(4)
            if p.view()["overlay"] == "transition":
                seen_transition = True
        p.buttons(0)
        p.step(60)
        after = p.view()
        assert seen_transition, "pressing Down never started a room transition"
        assert after["held_runs"] > held_before, (
            "the transition was never held -- the assertion below proves nothing", after)
        assert after["wide"] == 1 and after["valid"] == 1, (
            "the sub-room did not compose wide", after)
        assert after["flips"] == before["flips"], (
            "the pipe cost wide<->native transitions",
            before["flips"], after["flips"])
        pipe_result = dict(flips=after["flips"] - before["flips"],
                           subroom_mode=after["mode"], subroom_wide=after["wide"],
                           subroom_score=after["score"],
                           subroom_attr=after["attr_score"],
                           subroom_cam=[after["camera_x"], after["camera_y"]],
                           held_runs=after["held_runs"] - held_before)

        head, flip_events = p.flip_log()
        return dict(pause=pause_result, pipe=pipe_result,
                    total_flips=p.view()["flips"], flip_events=flip_events,
                    held=p.view()["held"], held_runs=p.view()["held_runs"])
    finally:
        p.close()


def run_faithful():
    """The faithful body cannot load a DX savestate, so drive it by route."""
    p = PausePipe("pausepipe-faithful", aspect="32:9", route=PLAY_ROUTE,
                  env={"SML2_DX": "0"})
    try:
        p.run_to(PLAY_FRAME + 400)
        start = p.view()
        assert start["mode"] == 4 and start["wide"] == 1, start
        flips, paused, resumed = pause_cycle(p)
        assert paused["mode"] == 8, ("Start did not pause", paused)
        assert paused["wide"] == 1, ("the view narrowed on pause", paused)
        assert resumed["wide"] == 1 and resumed["valid"] == 1, resumed
        assert flips == 0, ("pause cost wide<->native transitions", flips)

        # Walk right pressing Down, and if a transition happens it must be free.
        before = p.view()
        p.buttons(0x01)
        transitions = 0
        for _ in range(60):
            p.step(6)
            if p.view()["overlay"] == "transition":
                transitions += 1
        p.buttons(0x08)
        for _ in range(30):
            p.step(6)
            if p.view()["overlay"] == "transition":
                transitions += 1
        p.buttons(0)
        p.step(40)
        after = p.view()
        head, flip_events = p.flip_log()
        model = [f for f in flip_events
                 if not f["to_wide"] and f["reason"] in ("transition", "mode")
                 and f["frame"] > before["frame"]]
        assert not model, ("an overlay narrowed the view on the faithful body", model)
        return dict(pause_flips=flips, paused_mode=paused["mode"],
                    transition_frames_seen=transitions,
                    total_flips=after["flips"], held=after["held"],
                    held_runs=after["held_runs"], flip_events=flip_events)
    finally:
        p.close()


def main():
    assert STATE.exists(), (
        "missing fixture " + str(STATE) + " -- it ships in recomp/fixtures/")
    results = {"dx": run_dx(), "faithful": run_faithful()}
    (ROOT / "logs/pause-pipe-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
