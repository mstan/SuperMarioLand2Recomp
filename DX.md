# DX color — one executable, two recompiled games

`Super_Mario_Land_2.exe` contains **two complete static recompilations of the
same cartridge**. The launcher's Mods page picks which one boots:

| DX color | Body that runs | Hardware | Save id |
|---|---|---|---|
| off (default) | `Super_Mario_Land_2` | DMG, monochrome | `Super_Mario_Land_2` |
| on | `Super_Mario_Land_2_DX` | CGB, full colour | `Super_Mario_Land_2_DX` |

Both are built from **one ROM**, the only one this project ever asks for:

```
Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb
CRC32   D5EC24E4
SHA-256 5450dce1bd0c073964c374b5b5b5729dce8d00f2e807892c34af32b8bce1392e
512 KiB · header 0x143 = 00 (DMG) · 0x147 = 03 (MBC1+RAM+BAT) · 0x148 = 04
```

The DX body is that file plus [Super Mario Land 2 DX v1.8.1 by
toruzz](recomp/patches/SML2DX_readme.txt), applied **in memory at boot**:

```
recomp/patches/sml2dx_v181.bps        CRC32 2144DF1C · 652043 bytes
  → SML2 DX v1.8.1                    CRC32 F0799017 · 1 MiB
    SHA-256 1dd604a91b9ee99ef381cc795da3ae28c72d04ba9de78b355df829e56ce49c46
    header 0x143 = C0 (CGB-only) · 0x147 = 1B (MBC5+RAM+BAT) · 0x148 = 05
```

Nothing is written to disk, the player's ROM file is never modified, and the
patched image is never asked for. The CRC gate accepts `D5EC24E4` and nothing
else, whichever way the toggle is set.

## How the BPS was produced

toruzz distributes SML2 DX as an **IPS** patch against V1.0. `sml2dx_v181.bps`
is that patch converted to BPS, which — unlike IPS — carries a CRC32 of the
source ROM, of the target ROM, and of the patch itself. That is what makes the
in-memory path safe: `gb_bps_apply()` refuses to run against the wrong source
and refuses to hand back a wrong target, before the recompiled code ever sees a
byte. The recompiler then re-checks the result against the SHA-256 it baked in
at generation time. The author's original readme ships beside it as
`recomp/patches/SML2DX_readme.txt` and is staged next to the executable.

## Build

Two `gbrecomp` runs, one `cmake`:

```powershell
gbrecomp.exe --config super_mario_land_2.toml      # -> generated/      (primary)
gbrecomp.exe --config super_mario_land_2_dx.toml   # -> generated_dx/   (body)
cmake -G Ninja -S generated -B generated/build ... ; ninja -C generated/build
```

`Launch.ps1 -Build` does all of it; `Launch.ps1 -Build -NoDX` skips the second
run, and the executable is then an ordinary single-body faithful build (the DX
row disappears from the Mods page). `generated/` is the CMake project;
`game_build.cmake` picks up `generated_dx/Super_Mario_Land_2_DX_body.cmake` — a
generated source list — and compiles it into the same executable.

Both configs point `[rom] path` at the **same V1.0 file**. The DX config adds
`patch_file`, and the recompiler applies it before analysing, so the repository
never holds the hack and the provenance of the DX body is one line of config.

## Engine options this uses

All of them default off; an untouched config generates byte-identical output
(verified: Tetris 10/10 files and Super Mario Land 2 27/27 files SHA-256
identical before and after the engine change).

| Key | Where | Effect |
|---|---|---|
| `[rom] symbol_prefix` | either | namespace for every emitted global; empty = the output prefix |
| `[rom] patch_file` | DX | BPS applied to `path` at generation, and in memory at boot |
| `[options] dispatch_misses` | DX | this body's own Tier-0 seed manifest (pre-existing key; reused, not re-invented) |
| `[options] body_only` | DX | library body: no `main()`, no CMake project, namespaced symbols, emits `<prefix>_body.cmake` |
| `[options] multi_body` | faithful | primary project: `main()` boots whichever body `game_select_body()` returns |
| `[options] emit_main` | — | standalone override for the `main()` wrapper |

Runtime seam: `gb-recompiled/runtime/include/gb_body.h`
(`GBBody`, `gb_body_resolve`, `gb_body_active_id`),
`game_extras.h` (`game_get_bodies`, `game_select_body`),
`launcher.h` (`launcher_apply_patch_in_memory`, `launcher_image_matches_sha256`,
`launcher_last_error`), `gbrt.h` (`gb_set_dispatch`, `gbrt_dispatch`),
`platform_sdl.h` (`gb_platform_preboot_launcher`).

## Gate matrix

The player supplies one ROM; the toggle decides the rest.

| ROM supplied | `sml2dx_v181.bps` beside the exe | DX color | Result |
|---|---|---|---|
| V1.0 `D5EC24E4` | present | off | faithful body boots, DMG branding, `Super_Mario_Land_2.sav` |
| V1.0 `D5EC24E4` | present | on | BPS applied in memory → DX body boots, GBC branding, `Super_Mario_Land_2_DX.sav` |
| V1.0 `D5EC24E4` | **missing** | on | Mods page shows the DX row in the warning colour ("Unavailable: sml2dx_v181.bps is missing…"); **Play is refused** with `last_error`; a launcher-less boot falls back to the faithful body |
| anything else | — | either | launcher CRC gate rejects the file before Play; the runtime's own `verify_rom` rejects it again and re-prompts |
| pre-patched DX image `F0799017` | — | on | only if `game_get_valid_crcs()` is widened to include it — the body's init already skips the patch when the image it was given is already the expected one (`launcher_image_matches_sha256`). Not enabled: one ROM, one CRC |

Saves never cross: the save id is `GBBody::id`, so `.sav`, `.rtc` and `.state`
files are per body by construction, not by convention.

## Branding lag (known)

`game_get_platform()` / `game_get_name()` are read by the pre-boot launcher
*before* the player can touch the toggle, so the theme ("GAME BOY" green vs
"GAME BOY COLOR" magenta) and the title reflect the **saved** choice and catch
up on the next launch. Closing that needs a recomp-ui ABI callback for "the mod
selection changed"; the body that actually boots is always correct.

## Adaptive widescreen × DX

**Both mods are on at once.** The wide margins on the DX body are drawn in the
same colours the hardware draws the native 160 columns in, and the executable
proves it every frame before it widens anything.

| run | body | model | width | valid | block-map score | attribute score |
|---|---|---|---|---|---|---|
| widescreen, DX off | `Super_Mario_Land_2` | dmg | 512 | 1 | 357 / 357 | 0 / 0 (DMG) |
| widescreen, DX on | `Super_Mario_Land_2_DX` | cgb | 512 | 1 | **357 / 357** | **357 / 357** |

Over the probe route's 2776 scored gameplay frames on each body: **0 rejections
by either gate.** Captures: `logs/dx-widescreen/`.

### How the hack colours the background

Margins are synthesised from the level's **block map**, not from the hardware BG
map, so a margin cell has no attribute byte to read. The colour is not stored
per block either -- the four-byte block definitions at `$A600` are tile indices
and nothing else, on both images. It is stored **per tile index**:

```
attribute = MEM_WRAM_BANK2[$D000 + tile_index]
```

a flat 256-entry table, one per tileset, holding the ordinary CGB BG attribute
byte (bits 0-2 palette, bit 3 tile VRAM bank, bit 5 X flip, bit 6 Y flip,
bit 7 BG-over-OBJ priority).

| Binding | Where | Role |
|---|---|---|
| Attribute table | `$D000`-`$D0FF`, **WRAM bank 2** | `attr = table[tile]`. Same address as the level's own `$D000` block-map tail, which lives in WRAM bank 1 -- different bank, so a host read must name the bank |
| Table loader | `21:730E`, `21:732C` | `a = [$A269]` (tileset), `hl = $4000 + a*$100` in ROM bank `$21`, `de = $D000`, `bc = $0100`, `SVBK = 2`, `call $0336` |
| Tileset selector | `$A269` | the same byte the DX actor-draw-bank dispatcher at `00:07EA` reads |
| VRAM write queue | `$AA00`, 6-byte records | `dest lo, dest hi, tile0..tile3` for one 16x16 block; `dest hi == 0` terminates. **Unchanged from V1.0** -- so are the block-map readers `00:096C` / `00:0A38` that fill it |
| Queue drain, V1.0 | `00:0AFB` | writes the four tiles into the tilemap and returns |
| Queue drain, DX | `00:0AFB` -> `24:79B5` | DX replaces 15 bytes at `00:0AFB` with `3E 24 / EA 00 21 / CD B5 79 / FA 4E A2 / EA 00 21 / C9`. `24:79B5` writes the same four tiles, then rewinds `de` by 3 and `hl` by `$21`, sets `VBK = 1` (`24:79F9`) and `SVBK = 2` (`24:79FE`), and writes `table[tile]` into VRAM bank 1 for each of the four cells (`24:7A0A`: `ld b,$D0 / ld a,[de] / ld c,a / ld a,[bc] / ld [hl+],a`) |
| BG palette upload | `00:18AF` | `BCPS $FF68` / `BCPD $FF69` from the pointer at `$A1DC`/`$A1DD`; OBJ via `00:18DA` from `$A1DE`/`$A1DF`. This is where "the palettes are changed on the fly" happens |

Both inputs are re-read **every frame** -- the table out of WRAM bank 2, the
palettes out of the PPU's own CGB palette RAM -- so a mid-level tileset or
palette change is followed for free, with no cache to invalidate. All reads go
through the host peek helpers; nothing touches the emulated bus.

The live WRAM table is checked against its ROM source on every probe sample
(`rom_table_matches`), so the runtime copy is tied to `21:$4000 + tileset*$100`
rather than to "whatever happens to be in WRAM".

### The per-frame proof (fail closed)

`validate_scene()` in `sml2_adaptive.c` scores the native window every frame and
falls back to a centred 160x144 image unless both gates pass:

* **tiles** -- block-map decode vs. the hardware tilemap in VRAM bank 0, at 95%
  or better (unchanged policy);
* **colour** -- for **every** cell whose tile matched, the derived attribute must
  equal the hardware attribute in VRAM bank 1. No tolerance.

The grid is 21 x 17 = **357** cells, not 21 x 18. The 18th BG tile row sits
behind the status-bar window (`WY = 136`) on every gameplay frame and is never
drawn; V1.0 keeps writing the level into it and DX leaves it at tile `$FF` /
attribute 0, so scoring it would reject about one DX frame in five over pixels
that are not shown. `sml2_view` reports `score`, `attr_score`,
`attr_prio_diff`, `cgb`, `attr_table`, `tileset`, `sprite_pal_mask` and the
always-on counters `gate_scene`, `gate_tile_fail`, `gate_attr_fail`;
`sml2_score_map` prints the per-cell outcome plus the first mismatching cells in
full (block id, both tiles, both attributes).

### What bit 7 costs, and why it is not derivable

The colour gate is on bits 0-6. Bit 7 (BG-over-OBJ priority) selects no colour,
and it is **not a function of the level**: the hack patched each of the ROM's
*direct* tilemap writers with an attribute counterpart, and one of them forces
the bit.

```
01:5B40  V1.0's "Mario used this block" writer: stamps tiles $F8-$FB and
         sets block id 7, then (DX) jumps to 01:4100 instead of $59DF
01:411A  FA F8 D0   ld a,[$D0F8]
01:411D  F6 80      or $80          <- priority forced
01:411F  22 77 19 22 77              the 2x2 attribute quad
```

The *same* block id, scrolled in from the authored level data through the
ordinary queue drain, gets the table value with bit 7 clear. Measured on the DX
body over the probe route: tiles `$F8`-`$FB` appear in VRAM bank 1 with
attribute `$87` (174 samples) **and** with `$07` (166 samples) -- same tile,
same block id, different write history. The host derives bit 7 from the table,
i.e. the value the queue drain would produce (and does produce again on the next
scroll), and reports the divergence as `attr_prio_diff` rather than hiding it.
The visible cost is confined to whether a sprite passes in front of or behind a
just-used block **in a margin**.

### Sprites in the margins

Margin sprites are captured metasprite pieces and carry the ROM's own attribute
byte, so their CGB palette and tile bank need no derivation. Measured over the
probe samples: **7 of the 8 OBJ palettes** used (`sprite_pal_mask`), and 4-5
pieces per frame fetched from **VRAM bank 1**. That second number is why
`draw_sprite()` had to stop masking the tile address with `0x1FFE` *after*
adding the bank offset -- the mask cleared bit 13 and sent every bank-1 sprite
back to bank 0. Inert on the faithful body, which has one bank.

### Two things this measurement disproved

* *"The attribute table is mutated during play."* It is not: 400 consecutive
  frames of the DX body, byte-comparing `$D000`-`$D0FF` in WRAM bank 2 -- zero
  changes. The bit-7 divergence is a second writer, not a moving table.
* *"The DX block-map decode dips because DX streams VRAM differently."* It does
  not. With the hidden row excluded, the tile-score histogram over the probe
  route is **identical on both bodies**: 2025 frames at 357/357, 8 at 355, 67 at
  353. The residue is a pre-existing, body-independent gap -- the ROM's direct
  writers stamp block id `$7F` as four copies of tile `$7F` and block id 7 as
  `$F8`-`$FB`, bypassing the `$A600` block definitions, so a just-changed block
  reads back one tile on hardware and another out of the block map until the
  next scroll redraws it. The 95% tile gate already covers it, which is why the
  colour gate scores only the cells whose tile matched.

### Two things that made the DX view flicker

Both were found in the **attract demo**, which the Level-1 validation route
never reaches: it cycles several demo levels on tilesets 14, 17 and 20, at
camera positions Mushroom Zone level 1 never visits.

**1. The host read the block map through the guest's live SVBK.** The level's
`$D000`-`$DFFF` half -- block rows 32..47 -- is WRAM bank 1. The DX attribute
drain parks SVBK on **2** while it writes (`24:79FE` sets it, `24:7A32`
restores it) and can still be running when the host takes its per-frame
snapshot at PPU line 0, so those rows came back as the `$D000` attribute table
instead of the level. Any level whose camera sits below block row 32 decoded to
noise on exactly those frames. Mushroom Zone level 1 sits at row 28 and never
hit it; the faithful body never writes SVBK at all, which is why only DX
flickered.

Measured before the fix, over a 16k-frame attract run: **1213 of 1214** tile-gate
rejections and **226 of 226** block-id rejections had `wram_bank == 2`, every one
of them with the visible grid inside the `$D000` half. `ram_bank` was 0 on every
frame, so cart SRAM was never the problem. Fixed by reading the block map with
the bank the level lives in (`SML2_LEVEL_WRAM_BANK`), never the live one, and by
refusing the frame outright if cart SRAM is not on its expected bank.

**2. The colour gate compared attribute bytes when the claim is about colour.**
The demo levels leave whole regions of the blank tile `$FF` carrying attribute
1 where the hack's table says 0. Tile `$FF` is all zeroes in VRAM bank 0, so
every pixel is colour 0 -- and colour 0 is identical (`FFFFFF`) in palettes 0
and 1. Not one pixel differed, yet the byte comparison rejected the frame, and
because the condition is a property of the map rather than of timing it held for
**sixty consecutive frames at a time**: the sustained drop-out, not the flicker.

The gate now asks the question it means: would the derived attribute paint this
cell exactly as the hardware's attribute does? The byte compare stays as the
fast path; only a differing cell is rendered both ways, 64 pixels, a handful of
times per frame at most. The bit-7 exemption then falls out instead of being
asserted -- BG-over-OBJ priority selects no BG colour, so it can never change
the answer. `sml2_view` reports `attr_byte_diff`, the running count of cells
whose attribute byte differed but whose pixels did not: **26690** over that run.

### Flicker, measured

16000 frames per phase, 32:9, both bodies, attract demo (no input) and the
Level-1 route:

| | before | after |
|---|---|---|
| wide <-> native transitions, attract DX | 1546 | **10** |
| ...caused by a model failure | most of them | **0** |
| `blockid` rejections | 226 | **0** |
| `attr` rejections | 2492 | **0** |
| `tile` rejections | 1214 | **1** (first frame of a demo segment, before VRAM is filled) |
| frames pillarboxed from a wide view by a model failure | many | **0** |

The 10 remaining transitions are the demo entering and leaving gameplay: 5
recoveries, 4 `mode`, 1 `transition`.

### Pause and pipes

Reported from live play on the DX body and reproduced from the savestate
fixture in `recomp/fixtures/` (`tools/probe_pause_pipe.py`): pressing Start
pillarboxed the view, and walking into a warp pipe pillarboxed it again until
the sub-room settled. Neither was a DX-specific model failure -- the pause frame
and the sub-room both score 357/357 -- and both are fixed for both bodies by
holding the view through overlay scenes. See `ADAPTIVE.md`.

### Bindings, V1.0 vs DX

The one binding the hack moved is the actor draw routine:

| Binding | V1.0 | DX v1.8.1 |
|---|---|---|
| `00:3CAA` window memcpy | `2A 12 1C 05 20 FA C9` | same bytes, same address |
| `03:4019` `ldh a,[$FFE2]` tap | `F0 E2 FE 80 C8 …` | same bytes, same address |
| `03:409B` `$A2B1` SCX read | `FA B1 A2 47 F0 D1 …` | same bytes, same address |
| `$40B1` / `$4F11` metasprite tables | bank 3 | same addresses, **relocated banks** |
| draw routine bank | `3`, literal at `00:3C80` (`3E 03 / EA 4E A2 / EA 00 21 / CD 00 40`) | dispatcher at `00:07EA` returns `0x23`/`0x28`/`0x3A` from `$A269`; the 32-byte draw entry signature occurs once in V1.0 and **four times** in DX (`03:4000`, `35:4000`, `40:4000`, `58:4000`) |
| `02:41F7` / `02:426D` activation & cull copies | `21 0A AF / 11 02 AF / 06 04 / CD AA 3C` | byte-identical |
| `SetScroll` | `00:2062` (V1.2 had `00:2065`; bank 0 of V1.2 is V1.0 shifted `+3` over `0x0049C–0x0383D`) | `00:2062` |

The fix was not a bigger constant. The tap only fires while the CPU is inside
the draw routine, so that routine's bank *is* `ctx->rom_bank` at that instant,
and the metasprite fetches now follow the live bank; `draw_banks[]` in the
per-body table is only a sanity gate on which banks may host it at all.

## Known gaps

1. **BG priority (attribute bit 7) is not derivable for margin cells** written
   by the hack's direct block writers, as measured above. Colour is exact; only
   sprite-behind-block layering in a margin can differ.
2. **Live play coverage is Mushroom Zone level 1 (tileset 0)**, the same level
   the faithful body's widescreen validation uses. Other tilesets are covered by
   construction -- the table is re-read each frame from
   `21:$4000 + [$A269]*$100` -- and by the per-frame gate, not by a play-test.
3. **Branding lags one launch** (above).
4. **`recomp/sml2_v10.sym` is faithful-only.** The disassembly targets the
   unpatched V1.0; the DX config deliberately omits `symbols`.
5. **The pre-patched DX image is not accepted as a user ROM.** The body already
   skips its patch step when handed an image that is already the expected one
   (`launcher_image_matches_sha256`), so this is one CRC away in
   `game_get_valid_crcs()` — deliberately not taken, to keep "one supported
   cart" true.
