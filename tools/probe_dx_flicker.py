"""Does the wide view ever flicker, and is it ever pillarboxed during gameplay?

The DX body's ATTRACT DEMO is the hard case, and the one the owner saw break:
it cycles several demo levels with different tilesets, at camera positions
Mushroom Zone level 1 never reaches, and it runs for minutes without input.
A second phase drives the normal route into level 1.

Both phases FREE-RUN and then query rings that have been filling since the body
booted -- `sml2_flip_log` (every wide <-> native transition with its cause) and
`sml2_gate_log` (every rejected frame with its reason, the live banks, the
scores and the offending cells). Nothing is armed, so a one-frame flicker ten
thousand frames ago is still readable, and the counts are exact rather than
sampled.

Asserted, on both bodies:

  * `pillarbox_model` == 0 -- the view never snapped from wide to pillarbox
    because this module's model of the level failed. Rejections whose reason is
    mode / bonus / transition are the gate correctly refusing a scene that is
    not scrolling gameplay, and those may narrow the view.
  * no wide <-> native transition happened for a model reason.
  * the per-reason gate totals for blockid / attr / rambank / notable -- the
    classes this fix closed -- are all zero.

Requires a native Windows Python (CREATE_NO_WINDOW).
"""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ENTER_FRAME, PLAY_FRAME  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

END_FRAME = 16000        # well past the first full attract cycle

# Reasons that mean "this module got the level wrong", as opposed to "this is
# not a scrolling-gameplay scene". Only the first kind may never narrow the view.
MODEL_REASONS = ("tile", "attr", "blockid", "negcoord", "rambank", "notable")

# Level 1 under real input: entry, the ? blocks, a long scroll, and a death.
JUMPS = ",".join(f"{f}:A:18" for f in range(2620, 9000, 36))
PLAY_ROUTE = (f"60:S:4,140:S:4,220:S:4,{ENTER_FRAME}:A:4,"
              f"{PLAY_FRAME}:R:6000," + JUMPS)


class FlickerProbe(Probe):
    def _ring(self, cmd):
        self.id += 1
        self.sock.sendall((json.dumps(dict(cmd=cmd, id=self.id)) + "\n").encode())
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

    def flip_log(self):
        return self._ring("sml2_flip_log")

    def gate_log(self):
        return self._ring("sml2_gate_log")


def run(name, dx, route):
    p = FlickerProbe(name, aspect="32:9", route=route, env={"SML2_DX": "1" if dx else "0"})
    try:
        while p.view()["frame"] < END_FRAME:
            p.run_to(min(END_FRAME, p.view()["frame"] + 1000))
        v = p.view()
        fhead, flips = p.flip_log()
        ghead, _ = p.gate_log()
        result = dict(
            body=p.command("sml2_mod_state")["body"],
            frames=v["frame"], width=v["width"], cgb=v["cgb"],
            debounce=v["debounce"],
            flips=v["flips"], debounced=v["debounced"],
            narrowed=v["narrowed"], narrowed_model=v["narrowed_model"],
            pillarbox_model=v["pillarbox_model"],
            attr_byte_diff=v["attr_byte_diff"],
            gate_frames=v["gate_frames"], scored=v["gate_scene"],
            reasons=ghead["reasons"],
            flip_events=len(flips),
            flip_causes=dict(collections.Counter(
                (f["reason"] if not f["to_wide"] else "recover") for f in flips)),
            model_flips=[f for f in flips
                         if not f["to_wide"] and f["reason"] in MODEL_REASONS],
        )
        return result
    finally:
        p.close()


def main():
    results = {}
    for phase, route in (("attract", ""), ("play", PLAY_ROUTE)):
        for body, dx in (("dx", True), ("faithful", False)):
            key = f"{phase}-{body}"
            r = results[key] = run("flicker-" + key, dx, route)
            assert r["frames"] >= END_FRAME, (key, r)
            # The flicker number: a frame that was wide and snapped to
            # pillarbox because the model failed. `narrowed_model` also counts
            # model failures on frames that were ALREADY narrow (the first
            # frame of a demo segment, before the game has filled VRAM), which
            # change nothing on screen.
            assert r["pillarbox_model"] == 0, (
                key, "the view snapped to pillarbox on a model failure", r)
            assert not r["model_flips"], (key, "the view flickered", r["model_flips"])
            # The classes this fix closed must be gone outright. `tile` and
            # `negcoord` can still fire on a scene boundary -- the first frame
            # of a level, before the game has filled VRAM, or a camera sitting
            # on the world origin -- and the two assertions above already
            # require that such a frame never snaps a wide view to pillarbox.
            for reason in ("blockid", "attr", "rambank", "notable"):
                assert r["reasons"][reason] == 0, (key, reason, r["reasons"])
    (ROOT / "logs/dx-flicker-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
