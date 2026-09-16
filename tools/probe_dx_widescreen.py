"""Adaptive widescreen on the DX body: does the margin get the RIGHT colour?

The geometry gate (block map -> BG tilemap) already scored perfectly on DX. What
was missing was the CGB attribute byte, and this probe is the evidence that the
compositor now derives it from the same data the hack does:

    attribute = MEM_WRAM_BANK2[$D000 + tile_index]

a per-tileset table the hack loads from ROM bank $21 ($4000 + [$A269] * $100)
and consumes in its extended VRAM-queue drain at 24:79B5 (reached from the 15
bytes it patched over ROM0 $0AFB).  See DX.md.

Three independent checks, all on the running DX body at 32:9:

 1. the compositor's own per-frame gate (`sml2_view` -> attr_score) is 357/357,
    and its always-on gate counters show zero rejections over the whole run;
 2. the SAME claim re-derived HERE in Python from raw host-side `peek` dumps of
    VRAM bank 0, VRAM bank 1 and WRAM bank 2 -- so a bug in the in-process gate
    cannot pass itself;
 3. the faithful body still reports cgb=0 / attr_score 0/0 and stays valid, i.e.
    nothing about the DMG path moved.

Captures land in logs/dx-widescreen/ as PPM and PNG.

Requires a native Windows Python (CREATE_NO_WINDOW).
"""
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_adaptive import Probe, ENTER_FRAME, PLAY_FRAME, ROUTE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Mushroom Zone level 1 is the first level a new file reaches; the shared ROUTE
# enters it and then runs right with a jump cadence.  A second sample is taken
# much later in the same run, after the camera has travelled far enough to have
# scrolled entirely new terrain (and, in this zone, new tilesets) into VRAM.
SAMPLE_FRAMES = (PLAY_FRAME + 120, PLAY_FRAME + 700, PLAY_FRAME + 1500,
                 PLAY_FRAME + 1900)

# 21 x 17 cells. The 18th BG tile row is behind the status-bar window (WY 136)
# on every gameplay frame, and the two images disagree about what they leave in
# it, so it is not scored; see validate_scene() in sml2_adaptive.c.
COLS, ROWS = 21, 17
CELLS = COLS * ROWS

BG_TILEMAP = 0x9800          # LCDC bit 3 is clear during gameplay (LCDC $E3)
DX_ATTR_BASE = 0xD000        # WRAM bank 2
DX_ATTR_BANK = 2
DX_TILESET = 0xA269
DX_ATTR_ROM_BANK = 0x21      # 21:730E / 21:732C: $4000 + [$A269] * $100 -> $D000


def ppm_to_png(ppm_path, png_path):
    """Minimal P6 -> PNG so the captures can be looked at directly."""
    data = ppm_path.read_bytes()
    fields, idx = [], 0
    while len(fields) < 4:
        while data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":
            while data[idx:idx + 1] not in (b"\n", b""):
                idx += 1
            continue
        start = idx
        while not data[idx:idx + 1].isspace():
            idx += 1
        fields.append(data[start:idx])
    idx += 1
    assert fields[0] == b"P6" and fields[3] == b"255", fields
    w, h = int(fields[1]), int(fields[2])
    pixels = data[idx:idx + w * h * 3]
    raw = b"".join(b"\x00" + pixels[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))
    return w, h


class DXProbe(Probe):
    def peek(self, addr, n, **banks):
        """Host-side read with explicit banks; never enters the emulated bus."""
        out = bytearray()
        want = n
        self.id += 1
        payload = dict(cmd="peek", id=self.id, addr=f"0x{addr:04X}", len=n, **banks)
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        while len(out) < want:
            r = self.line()
            if r.get("id") != self.id:
                continue
            if r.get("ok") is False:
                raise RuntimeError(("peek", r))
            out += bytes.fromhex(r["hex"])
        return bytes(out)

    def blockmap(self):
        return self.peek(0xB000, 0x1000) + self.peek(0xC000, 0x1000) + self.peek(0xD000, 0x1000)


def derive_check(p, view):
    """Re-derive the attribute map here, from raw dumps, and compare.

    Independent of the compositor: block map + $A600 block definitions give the
    tile index for each of the 21x18 visible cells, the WRAM bank 2 table gives
    the attribute, and VRAM bank 1 says what the hardware is actually showing.
    """
    attr_table = p.peek(DX_ATTR_BASE, 0x100, wram_bank=DX_ATTR_BANK)
    # ...and prove where it came from: the live WRAM copy must be byte-for-byte
    # ROM bank $21 at $4000 + tileset * $100, which is the binding 21:730E /
    # 21:732C encodes. Ties the runtime table to a fixed ROM address rather than
    # to "whatever happens to be in WRAM".
    tileset = p.ram(DX_TILESET, 1)[0]
    rom_table = p.peek(0x4000 + (tileset & 0x3F) * 0x100, 0x100,
                       rom_bank=DX_ATTR_ROM_BANK)
    blockdef = p.peek(0xA600, 0x200)
    bmap = p.blockmap()
    tilemap = p.peek(BG_TILEMAP, 0x400, vram_bank=0)
    attrmap = p.peek(BG_TILEMAP, 0x400, vram_bank=1)
    left, top = view["left"], view["top"]
    # The compositor pins its world origin to the scroll registers the PPU
    # LATCHED for the frame it composed (`s.left += (int8_t)(scx - s.left)`), so
    # left & 0xFF IS that frame's SCX.  Reading FF42/FF43 here instead would
    # sample whatever the guest wrote for the NEXT frame during the VBlank the
    # probe is paused in, and shift every cell.
    scx, scy = left & 0xFF, top & 0xFF

    # (a) The MODEL, with no dependence on this module's own decode and no
    # timing skew at all: over the whole 32x32 BG map the hardware is holding,
    # is every attribute byte in bank 1 exactly table[tile byte in bank 0]?
    # This is the claim the margins rest on, stated in the hardware's own
    # terms.  Bit 7 is excluded and counted separately: the hack's direct
    # "used block" writer at 01:5B40 -> 01:4100 forces it (`ld a,[$D0F8] /
    # or $80`) on cells the ordinary queue drain would leave clear, so the same
    # tile legitimately carries $07 and $87 in different cells.  It selects no
    # colour, only OBJ-behind-BG.
    model_total = model_hit = model_prio = 0
    model_misses = []
    for cell in range(0x400):
        model_total += 1
        diff = attrmap[cell] ^ attr_table[tilemap[cell]]
        if diff & 0x80:
            model_prio += 1
        if not diff & 0x7F:
            model_hit += 1
        elif len(model_misses) < 8:
            model_misses.append(dict(cell=cell, tile=tilemap[cell],
                                     derived=attr_table[tilemap[cell]],
                                     hardware=attrmap[cell]))

    # (b) The block-map path, over the 21 x 17 grid the gate scores. This one is
    # read one frame later than the compositor's own snapshot, so a few
    # just-streamed cells can legitimately disagree; the zero-skew version of
    # this check is the compositor's per-frame gate.
    tile_hit = attr_hit = total = 0
    misses = []
    for ty in range(ROWS):
        for tx in range(21):
            wx, wy = left + tx * 8, top + ty * 8
            block = bmap[((wy >> 4) & 0xFF) * 0x100 + ((wx >> 4) & 0xFF)]
            quad = ((wy >> 3) & 1) * 2 + ((wx >> 3) & 1)
            tile = blockdef[block * 4 + quad]
            cell = (((scy + ty * 8) & 0xFF) >> 3) * 32 + (((scx + tx * 8) & 0xFF) >> 3)
            total += 1
            if tilemap[cell] != tile:
                continue          # scored only where the tile is ours, as the gate does
            tile_hit += 1
            if not (attrmap[cell] ^ attr_table[tile]) & 0x7F:
                attr_hit += 1
            elif len(misses) < 8:
                misses.append(dict(tx=tx, ty=ty, tile=tile,
                                   derived=attr_table[tile], hardware=attrmap[cell]))
    return dict(total=total, tile_hit=tile_hit, attr_hit=attr_hit, misses=misses,
                model_total=model_total, model_hit=model_hit, model_prio=model_prio,
                model_misses=model_misses,
                tileset=tileset, rom_table_matches=(rom_table == attr_table),
                attr_table_nonzero=sum(1 for b in attr_table if b),
                palettes_used=sorted({b & 7 for b in attr_table}),
                attr_bits_seen=sorted({b & ~0x07 for b in attr_table}))


def run_body(name, dx, aspect="32:9", capture_dir=None):
    results = {}
    p = DXProbe(name, aspect=aspect, route=ROUTE, env={"SML2_DX": "1" if dx else "0"})
    try:
        state = p.command("sml2_mod_state")
        results["mod_state"] = state
        assert state["body"] == ("Super_Mario_Land_2_DX" if dx else "Super_Mario_Land_2"), state
        assert state["margins"] == 1, ("margins still gated off for this body", state)
        assert state["enabled"] == 1, ("compositor not installed", state)

        samples = []
        for frame in SAMPLE_FRAMES:
            p.run_to(frame)
            view = p.view()
            if view["valid"] != 1:
                samples.append(dict(frame=frame, view=view, skipped="not gameplay"))
                continue
            derived = derive_check(p, view)
            tag = f"{name}-f{frame}"
            p.capture(tag)
            if capture_dir:
                capture_dir.mkdir(parents=True, exist_ok=True)
                ppm = capture_dir / (tag + ".ppm")
                ppm.write_bytes((p.folder / (tag + ".ppm")).read_bytes())
                ppm_to_png(ppm, capture_dir / (tag + ".png"))
            # sml2_capture() re-rendered the frame just now, so the sprite
            # statistics below describe the picture that was written out.
            view = p.view()
            samples.append(dict(frame=frame, view=view, derived=derived))

            assert view["width"] == 512, view
            assert view["score"] == [CELLS, CELLS], view
            # This dump is read one frame after the compositor's own snapshot,
            # so a handful of just-streamed cells may differ; the zero-skew
            # version of the check is the gate itself, asserted above.
            assert derived["total"] == CELLS and derived["tile_hit"] >= CELLS - 8, derived
            # Every cell whose tile this probe decoded must carry the derived
            # attribute -- no tolerance at all on colour.
            assert derived["attr_hit"] == derived["tile_hit"], derived
            if dx:
                assert view["cgb"] == 1 and view["attr_table"] == 1, view
                assert view["attr_score"] == [CELLS, CELLS], view
                # attr_prio_diff is reported, not asserted to zero: it counts
                # cells where only BG-over-OBJ priority differs, which the hack
                # sets from write history rather than from the level (see the
                # attr_for_tile comment in sml2_adaptive.c). It selects no
                # colour.
                assert derived["model_hit"] == derived["model_total"] == 0x400, derived
                assert derived["rom_table_matches"], derived
            else:
                assert view["cgb"] == 0, view
                assert view["attr_score"] == [0, 0], view
        results["samples"] = samples

        # Keep running and require the colour gate to hold every sample, not
        # just at the chosen frames: a single wrong attribute anywhere would
        # have dropped `valid` to 0 and shown a narrow native frame instead.
        scroll = []
        for _ in range(24):
            p.step(30)
            scroll.append(p.view())
        results["scroll_valid"] = sum(1 for v in scroll if v["valid"] == 1)
        results["scroll_frames"] = len(scroll)
        results["scroll_attr_perfect"] = sum(
            1 for v in scroll if v["valid"] == 1 and v["attr_score"][0] == v["attr_score"][1])
        results["scroll_camera"] = [scroll[0]["camera_x"], scroll[-1]["camera_x"]]
        results["max_attr_prio_diff"] = max(v["attr_prio_diff"] for v in scroll)
        # Margin sprites carry the ROM's own attribute byte; on a CGB body they
        # must reach more than one OBJ palette, or they are silently all
        # rendering through palette 0.
        pal_mask = 0
        for s_ in results["samples"]:
            pal_mask |= s_["view"].get("sprite_pal_mask", 0)
        results["sprite_pal_mask"] = pal_mask
        results["sprite_palettes_used"] = [i for i in range(8) if pal_mask >> i & 1]
        results["gate_scene"] = scroll[-1]["gate_scene"]
        results["gate_tile_fail"] = scroll[-1]["gate_tile_fail"]
        results["gate_attr_fail"] = scroll[-1]["gate_attr_fail"]
        assert scroll[-1]["gate_tile_fail"] == 0, scroll[-1]
        assert scroll[-1]["gate_attr_fail"] == 0, scroll[-1]
        results["scroll_tilesets"] = sorted({s["tileset"] for s in scroll})
        assert results["scroll_valid"] == results["scroll_frames"], (
            "compositor fell back to native mid-scroll", scroll)
        if dx:
            assert results["scroll_attr_perfect"] == results["scroll_valid"], scroll
        tag = f"{name}-scrolled"
        p.capture(tag)
        if capture_dir:
            ppm = capture_dir / (tag + ".ppm")
            ppm.write_bytes((p.folder / (tag + ".ppm")).read_bytes())
            ppm_to_png(ppm, capture_dir / (tag + ".png"))
    finally:
        p.close()
    return results


def main():
    captures = ROOT / "logs/dx-widescreen"
    results = {
        "dx": run_body("dxws-dx", dx=True, capture_dir=captures),
        "faithful": run_body("dxws-faithful", dx=False, capture_dir=captures),
    }
    (ROOT / "logs/dx-widescreen-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
