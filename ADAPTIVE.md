# Adaptive widescreen

An opt-in mod that renders Super Mario Land 2's world across the whole window
instead of the Game Boy's 160x144 crop. The emulated hardware stays native: the
PPU still renders 160x144, the save-state layout is unchanged, and with the mod
off the executable is byte-for-byte the faithful build.

Supported ROM: **Super Mario Land 2 - 6 Golden Coins (UE) (V1.0)**, CRC32
`0xD5EC24E4` — the one cart this project asks for. Every address below was
re-verified byte-for-byte against V1.0 after the re-base from V1.2, and against
the SML2 DX v1.8.1 image the DX body runs.

Only one binding moved between the three images, and only one of those moves
matters to this module:

* `SetScroll` sits at `00:2062` in V1.0 and in DX; V1.2 had it at `00:2065`
  (bank 0 of V1.2 is V1.0 shifted `+3` over file `0x0049C–0x0383D`). It is cited
  below for provenance only — no code here binds to that address.
* The **actor draw routine** is in bank 3 on V1.0 but relocated on DX: the hack
  replaces the literal bank select at `00:3C80` with a dispatcher at `00:07EA`
  returning bank `0x23`/`0x28`/`0x3A` from `$A269`. `sml2_adaptive.c` therefore
  follows the LIVE `ctx->rom_bank` inside the tap rather than a compile-time
  `3`; the per-body `draw_banks[]` table is only a sanity gate.

Every other constant — the block map, block definitions, scroll boxes, camera,
activation/cull windows, the `$FFE2` tap and the `$A2B1` override — is at the
same address with the same bytes in V1.0 and in DX.

## Bodies

The executable carries two recompiled bodies (see `DX.md`) and this mod runs on
**either**. On the faithful DMG body a margin cell has no attribute and is drawn
through BGP, as the hardware does. On the CGB-only DX body every margin cell
also gets a CGB BG attribute, derived from the same data the hack itself uses:
a flat 256-entry tile-index -> attribute table the hack keeps in **WRAM bank 2
at `$D000`**, loaded per tileset from ROM bank `$21` and consumed by its
extended VRAM-queue drain at `24:79B5`. Palette, tile VRAM bank and both flips
are exact and re-proved every frame against the hardware attribute map in VRAM
bank 1; `DX.md` has the full derivation, the addresses, and the one attribute
bit that is not a function of the level.

## Run

```powershell
./Launch.ps1 -Build -Aspect fit
./Launch.ps1 -Aspect '32:9'
./Launch.ps1 -Aspect off
./Launch.ps1                     # keep whatever the Mods page last saved
```

`-Build` builds the recompiler, regenerates the C from the ROM/config, and
builds the game. Output: `generated/build/Super_Mario_Land_2.exe`. The MSYS2
MinGW64 toolchain at `C:\msys64\mingw64\bin` is called directly so devkitPro's
cmake/gcc on PATH cannot win.

Open **Mods → Presentation → Adaptive widescreen** in the launcher and tick it.
The **Aspect ratio** option offers **Fit to window**, **16:9**, **21:9** and
**32:9**. Pressing Play writes the choice to `sml2-mods.ini` beside the
executable. **The mod is off by default.**

The game pixel height is always 144. 16:9 is 256 pixels wide, 21:9 is 336, 32:9
is 512; Fit follows the live window aspect from 160 to 4096 and re-resolves when
the window is resized or made fullscreen. A window taller than 10:9 stays at
native width and letterboxes.

The **Enemy spawns** option offers **Extended** (default) and **Original**, and
is saved as `Spawns=` in the same file. See [Enemy spawns](#enemy-spawns). It
covers both of the things the game measures against the ORIGINAL screen edge
rather than the visible one: Extended moves the spawn point out to the edge of
what the player can actually see, so enemies walk in rather than pop in, and it
lets Fire Mario's fireballs cross the whole composed view instead of winking
out partway across it. Original keeps both untouched for anyone who wants
vanilla gameplay decisions.

Environment overrides (seed the launcher controls, lose to the checkbox):
`SML2_WIDESCREEN=fit | 16:9 | 21:9 | 32:9 | off | <integer width>` and
`SML2_SPAWNS=original | extended`.
`SML2_ADAPTIVE_TRACE=1` prints a per-120-frame state line to stderr.

## How it renders

Super Mario Land 2 decompresses the **entire** level into RAM at load time and
never streams more from ROM, so the compositor never has to guess: every block
of the level is resident, live, and already reflects blocks Mario has broken or
coins he has taken.

1. **Background.** For each pixel of the wide view the world coordinate is
   converted to a 16x16 block, the block to one of its four 8x8 tiles, and the
   tile to pixels out of the level's VRAM tile data. This is a host clone of the
   ROM's own `GetBlockAt` and VRAM column loader, so the margins are made of the
   same bytes the hardware would show if the screen were wider. Nothing depends
   on the 256x256 VRAM scrolling ring, which only ever holds about one screen of
   valid map either side.
2. **The native 160 columns are copied verbatim** from the PPU's framebuffer, so
   whatever the hardware drew — sprites, palette fades, the status bar — is
   preserved exactly, and the centre of a wide frame is pixel-identical to the
   faithful build.
3. **Actors.** Enemy activation and horizontal culling are widened to the view,
   and each actor's metasprite is captured in world coordinates just before the
   ROM applies its own 160-pixel clipping, so enemies act and draw across the
   whole view. Sprite pieces the hardware clipped at the screen edge are
   completed in the margin.
4. **Status bar.** The bar is the window layer, one 8-pixel tile row at the
   bottom. Lives / coins / the collectible counter stay flush left, the timer
   stays flush right, and the gap between them is padded with the bar's own
   blank tile.
5. **Bounds.** The view is clamped to the run of 256-pixel scroll boxes the ROM
   itself would let the camera reach, checked from both sides. An area narrower
   than the requested view is centred with black padding.

## Scene gate (fail closed)

Three independent gates, all must pass or the frame falls back to a centred
native 160x144 image:

* the game's own mode enum says scrolling gameplay, it is not a bonus room, and
  no pipe/door transition is in flight, with LCDC/WY/WX in their gameplay state;
* **the block map decode reproduces the BG tilemap the hardware is actually
  showing** over the visible tile grid, at 95% or better;
* on a CGB body, **every cell whose tile matched also matches on attribute**
  (bits 0-6: palette, tile VRAM bank, both flips) against the hardware attribute
  map in VRAM bank 1. No tolerance -- one wrong cell and the frame falls back.

The scoring grid is 21 x 17 = **357** cells. The 18th BG tile row lies behind the
status-bar window (`WY = 136`) on every gameplay frame and is never drawn; the
two images leave different things in it, so it is not scored. Measured on every
gameplay frame of every probe run: **357/357 on both bodies**, with the always-on
counters `gate_tile_fail` and `gate_attr_fail` both **0 of 2776 scored frames**.

The colour gate is scored only on cells whose tile matched, which keeps it from
inheriting the tile gate's 95% tolerance. That tolerance exists because the ROM's
*direct* block writers bypass the `$A600` block definitions -- block id `$7F` is
stamped as four copies of tile `$7F`, block id 7 as `$F8`-`$FB` -- so a
just-changed block reads back one tile on hardware and another out of the block
map until the next scroll redraws it. Measured identically on both bodies over
the probe route: 2025 frames at 357/357, 8 at 355, 67 at 353.

Anything that reuses level RAM with different VRAM -- the world map, the level
intro card, the file select -- fails the gates.

## The gate asks what gets painted

The margins are correct exactly when every cell is painted the way the hardware
paints it. Tile index and attribute byte are both only proxies for that, and the
ROM breaks both, so the gate compares the **pixels**: the byte compare is the
fast path, and only a differing cell is rendered both ways, 64 pixels.

Several block ids are stamped straight into the tilemap by the ROM rather than
read out of the `$A600` definitions, and the block map does not record which
writer last touched a cell:

| block | what the ROM stamps | where |
|---|---|---|
| `$7F` | four copies of tile `$7F` | `01:5A96` sets the id, `01:5C4F` stamps; DX attributes it from `[$D07F]` (`24:7B80`) |
| 7 | tiles `$F8`-`$FB`, DX forcing BG priority | `01:5B40` -> `01:4100` (`ld a,[$D0F8] / or $80`) |
| erase | four copies of tile `$FF` | `01:5C24`, DX from `[$D0FF]` (`24:7B43`) |

Block `$7F` is every warp pipe, and its `$A600` entry is a stale
`[122,122,122,122]`. Standing under the Mushroom Zone pipe, 24 of the 357
scored cells read (block `$7F`, table tile 122, hardware tile `$7F` attr 5): the
old tile-byte gate scored **333/357 = 93.3%**, under its 95% threshold, and the
view pillarboxed for as long as the player stood there -- 2940 rejections in
5876 scored frames in the owner's session. Both tiles paint the same thing:
tile 122 is `$FF00` x8, every pixel colour 1, and palette 0 colour 1 is
`(74,164,246)`; tile `$7F` is all zeroes, every pixel colour 0, and palette 5
colour 0 is `(74,164,246)`. Identical sky blue, 64 pixels of 64. The paint
gate scores that spot **357/357**.

Forcing block `$7F` to tile `$7F` is **not** the fix and was measured and
rejected: the same block id renders as 122 in 8 other cells of the same screen,
so the block map genuinely cannot tell them apart.

The 95% tolerance stays, on the new quantity. Dropping it cost **16**
pillarboxes over a 16000-frame faithful attract run, where the erase writer
above leaves four cells that really do paint differently for hundreds of
consecutive frames.

## Overlay scenes are held, not narrowed

Pause (`$FF9B` = `$08`) does not replace the world: the level RAM, the camera
and the bounds are the ones the last accepted frame was composed from, and the
game hands them straight back. Refusing it narrowed the view twice per pause,
which is what the owner saw. While pause is up the view is **held** on the last
accepted frame for as long as it lasts, with no expiry, and the debounce counter
is held at zero so a genuine failure afterwards still gets its full window.

| event | before | after |
|---|---|---|
| Start, then Start again | 2 wide<->native transitions | **0** (63 frames held) |
| Down into a warp pipe | 2 | **0** (composed live, 80 frames) |

The pipe was a separate problem: `$A20E` stays non-zero for about **24 frames**,
which outlasts the 6-frame debounce on its own, so the view always narrowed and
came back. It is no longer treated as an overlay at all -- the dive composes
live, every frame, so the margins scroll with the native strip instead of
freezing. The sub-room on the far side was never the problem: it scores 357/357
at camera (1460, 623).

A frozen frame carries the **world** -- geometry, the block map, the tile bytes,
the attribute table, the bounds -- and deliberately **not the palettes**. DX
dims the screen while the game is paused by rewriting CGB BG palette RAM through
BCPD, and the native 160 columns, which come from the live PPU, dim with it.
Margins composed through a frozen palette snapshot did not: the frame came out
as a dimmed strip between two bright margins, measured at **-38 mean luminance
over exactly columns 176..335** of a 512-wide frame with both margins
bit-identical to the gameplay frame before them. A palette is a pure colour
lookup over the tile data, so applying the live one to frozen tiles is the only
self-consistent answer. BGP/OBP0/OBP1 are live for the same reason. LCDC stays
frozen -- it selects which tile-data block an index means.

### Disproved, so it is not re-tried

The dive was first blamed on the scroll anchor. `s.top` is derived from the
camera and corrected onto SCY with a single `int8_t` delta, and during a dive
SCY runs 120 -> 252 while the camera *appeared* pinned -- a shift of 132 that
would wrap. The camera is not pinned. The "stale camera" was this module's own
frozen frame being reported back, because `cam_x`/`cam_y` are part of the state
a hold restores; `s.mode` had already fooled the same reading once. With the
live camera read separately (`sml2_view` now reports `camera_live`, `scy_live`,
`scx_live` beside the composed values) the camera moves every frame and the
anchor is correct on all 80 dive frames.

An incremental version -- follow the register from the previous frame, re-anchor
only when the camera moves -- was written, measured and reverted: it latched a
stale 256-pixel page across a level reload, where the scroll teleports while the
camera holds still, and the compositor returned `left = -256` for a camera at
80. What is kept is the detector: `scroll_offpage` counts frames where the
camera stood still while the register moved further than an `int8_t` can
express, the only situation in which the anchor could be wrong. It has stayed 0.

## Enemy spawns

**Original** installs nothing. Every scanner input, every window and every
despawn test is the ROM's own, so the game's decisions are exactly vanilla; an
enemy simply appears inside the margin instead of walking in from off view.

**Extended** (the default) widens two things, both fail-closed — the moment the
scene gate refuses a frame, or the compositor is not composing wide, the ROM
gets its own value back:

1. **Where enemies spawn.** The spawn-scan edge moves out to the visible view
   edge, ramped at most 8 px per scanning frame so no list entry is stepped
   over. Details in the `read_override` comment in `sml2_adaptive.c`.
2. **How far Mario's fireballs travel.** They now reach the edge of the
   composed view and die 32 px past it, instead of vanishing halfway across it.

### Fireballs travel to the view edge

Fire Mario's shots are not `$AD00` actors. They live in their own two-slot
table at `$A880` (stride `$10`: `+0` active, `+1/+2` world Y LE, `+3/+4` world
X LE, `+5` direction, `$FF` = left), spawned at `00:32C1` and updated and drawn
per slot by `00:3261`. That routine computes

```
screenX = (slotX_lo - camX_lo + 80) & $FF        ; 00:3271..00:327A
          and $F0 / cp $C0                       ; 00:327C / 00:327E
          jr z -> pop hl / xor a / ld [hl],a     ; 00:3280 -> 00:32BD
```

and the identical test on screen Y at `00:328F`. Landing in `$C0..$CF` destroys
the slot. Measured, at the fireball's 3 px per frame:

| | vanilla death | measured | |
|---|---|---|---|
| right | world X = camX + **112**..127 | +112 faithful, +116 DX | 32 px past the native screen's right edge |
| left | world X = camX − **129**..144 | −134 on both | 49 px past its left edge |

The window is 16 wide and the shot steps 3 px, so which value inside it the
slot lands on is the phase between its own step and the camera's -- the range
is the binding, the measurement is one sample of it. The asymmetry between the
two sides is the 8-bit space's, not the game's: the kill window is a fixed band
at `$C0` and the screen is 160 columns.

**Every input to that test is modulo 256** — the camera's low byte, the slot's
low byte, `+80`, `and $F0` — so no lie about its inputs can express "512 pixels
away". The only expressible change is the compared immediate, which makes this
the project's first and only `[[imm_override]]` site. At `00:327E` the hook
hands the ROM either `A` (Z set, so the ROM runs its **own** destroy path at
`00:32BD`, unchanged) or `A ^ $10` (Z clear, so it keeps the slot), and decides
which in 16-bit world space:

```
die  <=>  slotX >= view_left + view_width + 32      (travelling right)
     or   slotX <= view_left - 32                   (travelling left)
```

Control flow, cycle counts and the destroy sequence stay the ROM's; only `Z`
changes, and `00:3282` reloads `A` immediately, so the carry the compare also
sets is dead. The Y test at `00:328F` is left completely alone. The site is
declared in **both** `super_mario_land_2.toml` and
`super_mario_land_2_dx.toml`: `00:324F`–`00:32C0` is byte-identical in the DX
image, so the hack did not relocate this routine and one address serves both
bodies.

Fail-closed has a wrinkle the spawn edge does not have. A fireball the widened
boundary has already carried past `rel` `[-64, 207]` is in territory vanilla
cannot produce, and handing it back to an 8-bit test would let it alias across
the screen rather than die. So when the gate is off, such a slot is destroyed
at once; everything still inside vanilla's own reach gets the ROM's `$C0` back
untouched.

### The shared sprite emitter, and its OAM copy

The `$AD00` actor draw routine is not the only thing that puts sprites on
screen: Mario, his fireballs, enemy fire and thrown items all go through one
shared emitter reached from `00:2CF4`, whose screen X and screen Y (`$FFC5` /
`$FFC4`) are **eight bit**. Without help, everything it draws stops existing at
the edge of the native 160 columns however wide the view is. It is now tapped
(`SML2_EMIT_PCS`, the instruction after `ldh a,[$FFC5]` in each of the four
copies the two images contain) and every piece is captured in world
coordinates. For a fireball the tap uses the slot's own 16-bit position rather
than unwrapping the 8-bit one, which is what lets Extended push it past 255
without aliasing; the metasprite index (`$B0..$B7`, and nothing else uses that
range) is part of the match, so a fireball a whole 256-pixel page away is never
confused with Mario.

That leaves the guest's **own** OAM write. Once a fireball is more than a
screen from the camera, `screenX` wraps and the entry the emitter writes lands
somewhere else entirely — possibly inside the native strip, which the
compositor blits verbatim from the hardware framebuffer, so the ghost would be
real pixels. Under Extended the emitter's `$FFC4` read is therefore answered
with `$F0` for exactly those pieces: every OAM entry it then writes sits at
OAM Y ≥ `$E0`, which the hardware never shows and the compositor's OAM pass
skips. The host's world-coordinate copy is untouched, and the ROM's own Y
despawn test — a different instruction — never sees it.

Independently, the compositor's OAM pass now ignores entries outside OAM X
`1..167`. An entry the hardware clips away entirely contributes nothing to the
native strip, so placing it at `s.left + X - 8` in a margin asserts a world
position the 8-bit OAM X never carried.

That one is not a hypothetical and it is not Extended's fault. **Measured on
the DX body with `Enemy spawns = Original`** — the policy that changes nothing
about the guest — a single left-travelling fireball produced **20** OAM entries
carrying its own tiles at OAM X `$D0..$FD`, every one of which the old pass
would have drawn **244 to 253 pixels to the RIGHT** of where the fireball
actually was, and **all 20 inside the 512-pixel view**. Vanilla writes those
itself: a fireball at screen X −20 is OAM X `$E8`, invisible to the hardware
and meaningless as a world position. The bound is therefore a compositor fix
that applies in both policies, and `sml2_sprites` reports what it refused as
`src: "oam_clipped"` rather than dropping it silently.

### Enemy projectiles are unchanged, deliberately

Enemy fire goes through the same emitter, so it is captured and drawn in the
margins correctly — but its **range** is vanilla. It is not extended, and the
reason is that the two cases are not symmetric:

* Mario's fireball dying mid-screen is a **presentation** bug: the player sees
  it vanish in open air at a place that means nothing. An enemy's shot that
  reaches further is a **difficulty** change — it lets an off-view enemy hit
  the player, which no amount of extra view makes fair.
* Extended already spawns enemies further out. Giving their projectiles the
  same extra reach would compound the two into a real change in how the game
  plays, which is the line this mod does not cross.
* Enemy projectiles are not one table with one despawn test. They are spread
  across the actor system and several per-type routines, so "extend them too"
  is not one binding but a survey — and the first bullet says it should not be
  done anyway.

If that is ever revisited, the same shape applies: find the per-type `cp`
against a screen-space constant, declare it as an `[[imm_override]]` site, and
decide in world space against `view_left`/`view_width`.

## Verified ROM bindings

Read taps and overrides preserve the ROM's control flow and cycle accounting;
host inspection reads memory directly and never advances the emulated bus.
`bank:addr` is the mapped address. Sites marked **hook** are where this mod
attaches.

### World and camera

| Binding | Where | Role |
|---|---|---|
| Camera centre X | `$FFCA`/`$FFCB` (16-bit LE) | screen left = camX − 80 |
| Camera centre Y | `$FFC8`/`$FFC9` | screen top = camY − 72 |
| `SetScroll` | `00:2062` | writes `sScrollY $A2B0 = camY−72−shake`, `sScrollX $A2B1 = camX−80`; VBlank `00:0154` copies them to SCY/SCX |
| Mario world X/Y | `$FFC2`/`$FFC3`, `$FFC0`/`$FFC1` | — |
| Level block map | `$B000`–`$DFFF` | `MEM[$B000 + ((wy>>4)&0xFF)*0x100 + ((wx>>4)&0xFF)]`, 256x48 blocks = 4096x768 px, IDs 0..127, mutated live |
| Block map loader | `00:0361` → `00:0386` | RLE (bit 7 = run flag) from the map bank named by level header byte `$0D`; expands to exactly `0x3000` bytes |
| Block map readers | `00:1F32`, `00:096C`, `00:0A38` | `GetBlockAt` and the VRAM row/column loaders — the address arithmetic this mod clones |
| Block definitions | `$A600`–`$A7FF` | 128 entries x 4 tile indices, order TL, TR, BL, BR, no attribute byte on either image |
| BG attribute table (DX) | `$D000`–`$D0FF`, **WRAM bank 2** | `attr = table[tile]`; loaded per tileset from ROM `21:$4000 + [$A269]*$100` by `21:730E`/`21:732C`, consumed by the DX queue drain `00:0AFB` → `24:79B5`. See `DX.md` |
| Block definition loader | `00:03F1` | from bank 8, pointer in level header bytes `$0B`/`$0C` |
| Scroll boxes | `$A960`–`$A98F` | 16x3 boxes of 16x16 blocks, nibble `%BTLR`, set bit = edge closed |
| Scroll box loader | `00:0424` | from the map bank, `+ (header[$12] & 0x0F) * 0x30` |
| Camera clamps | `00:0B61`, `00:0BE8` | `camX_lo <= $B0` / `>= $50` inside a closed box |
| Level header copy | `$A800`–`$A813` | 20 bytes; `$0F`..`$11` are BGP/OBP0/OBP1 |

### Scene state

| Binding | Where | Role |
|---|---|---|
| Game mode | `$FF9B` | `$04` scrolling gameplay, `$08` pause, `$09` death, `$0C` world map, `$19`/`$1A` file select, `$00`/`$01` title |
| Mode dispatch | `00:02AD`, table `00:02B0` | 36 entries |
| Bonus/minigame room | `$A28B & 0xF0` | non-zero selects the bank-2 `$4DA9` engine and disables scrolling (`00:0655`, `00:0B2D`) |
| Pipe/door transition | `$A20E` | non-zero while a room change is in flight |
| LCDC / WY forcing | `01:5D2F`, `01:5D39` | LCDC `$E3`, WY `$88` every gameplay frame |
| WX | `00:0251`, `0F:41F7` | `$07` |
| Status bar tilemap | `$9C00`–`$9C13` | 20 tiles, row 0; digits are `$80 + nibble` |
| Status bar refresh | `00:076E` (template `00:07DE`) | returns immediately unless `$FF9B == 4` |

Status bar columns: 0 life icon, 1 `x`, 2-3 lives, 4 blank, 5 coin icon, 6 `x`,
7-9 coins, 10 blank, 11 star icon, 12 `x`, 13-14 collectible, 15 blank, 16 `T`,
17-19 timer. Columns 0-14 hug the left edge, 16-19 the right.

### Actors

| Binding | Where | Role |
|---|---|---|
| Actor table | `$AD00`, stride `$20`, 16 slots | `+0/+1` world X (big-endian), `+3/+4` world Y, `+5` type, `+8` state (0 free, 1 dormant, 2 live), `+$0B` metasprite index, `+$0C..+$0E` attribute XOR, `+$12` anim frame (`$80` = invisible) |
| Window builder | `02:4000` | rebuilds `$AF0A`..`$AF1D` each frame as camX ± `$60` (activate), ± `$70` (spawn scan), ± `$A0` (cull) |
| Activation test | `02:41F7` (copy via `00:3CAA`) | `$AF0A`..`$AF0D` → state 1 becomes state 2 |
| Horizontal cull | `02:426D` (copy via `00:3CAA`) | `$AF1A`..`$AF1D` → slot freed, list entry rearmed |
| Spawn window | `$AF12`/`$AF13`, `$AF14`/`$AF15` | `camX +- 112`, big endian, rebuilt by `02:4000` (`02:4020`, `ld e,112`) |
| Scan direction + edge | `$AF22`, `$AF00`/`$AF01` | picked from that pair by `02:4085` (right), `02:409A` (no scroll, edge hi `$FF`), `02:40A4` (left); the low byte is masked `& $F8` |
| Spawn list | `$AB06`, 6-byte records | `+0/+1` world X big-endian, `+2` difficulty/"already spawned" flags, terminator `$FF`; `$AB00` is six `$FF` bytes so a backward scan stops there. built in RAM at level load; the cursor is seeded to `$AB06` at `02:69AD`, `02:6C76` and `03:6C4B` |
| Spawn list cursor | `$AF1E`/`$AF1F` | big-endian pointer; `02:4C64` / `02:4D13` store it forward/backward — an entry the cursor passes is consumed |
| Spawn scanner | `02:4C31` / `02:4CEB` | forward / backward walk; spawns only on **exact equality** with the 8-px-aligned edge (`02:4C6E` / `02:4D1D`), called from `02:417D` |
| Actor draw | `03:4000` | per-actor draw routine |
| Actor screen position | `03:4090` (reads `$A2B0`), `03:409B` (reads `$A2B1`) | screenY = actorY_lo + `$10` − SCY, screenX = actorX_lo + `$08` − SCX, both modulo 256 |
| Whole-actor drops | `03:4020`, `03:4025` | `cp $B8` on screen Y then screen X |
| Per-piece clips | `03:4055` (`cp $A0`), `03:4078` (`cp $A8`) | OAM Y / OAM X rejects |
| Metasprite tables | `03:40B1`, `03:4F11` (selected by `$AF06`), data from `03:4201` | 4 bytes per piece `(Yoff, Xoff, tile, attr)`, terminator `$80` in Yoff; flip bits 5/6 of the actor's attribute XOR rewrite the offsets as `~v − 7` |
| OAM shadow buffer | `$A100`–`$A19F`, DMA stub `$FFA0` | 40 entries, **no overflow guard anywhere in the ROM** |

### Mario's fireballs and the shared emitter

| Binding | Where | Role |
|---|---|---|
| Fireball table | `$A880`, stride `$10`, 2 slots | `+0` active, `+1/+2` world Y **little**-endian, `+3/+4` world X LE, `+5` direction (`$FF` = left) |
| Fireball spawn | `00:32C1` | `hKeysPressed` bit 1 (B) and `sCurPowerup $A216 == 3`, then first free slot; position = Mario's + (`$10`, `$1C`) |
| Fireball per-slot loop | `00:324F` | `hl` walks `$A880` by `$10` until `l == $A0`, calling `00:3261` for each live slot |
| Fireball update + draw | `00:3261` | copies the slot's low bytes to `$A25D`/`$A25F`/`$A212`, computes 8-bit screen X and Y, draws via `00:2CF4`, then world-space collision via `00:2FED` |
| Fireball despawn X | `00:327E` (`cp $C0`) | `(screenX & $F0) == $C0` destroys the slot. **hook** |
| Fireball despawn Y | `00:328F` (`cp $C0`) | the same test on screen Y — never overridden |
| Fireball motion | `00:332E` | 3 px/frame, bounces; block collision through `00:1EFA`, all world-space |
| Fireball metasprites | `$B0`–`$B3` (right), `$B4`–`$B7` (left) | `00:3293`; index = base + `(($FF97 & 6) >> 1)`; tiles 112–115, attr `$60` |
| Shared emitter | `00:2CF4` → `01:5297` | metasprite in the actor format (`Yoff, Xoff, tile, attr`, `$80` terminator) at screen (`$FFC5`, `$FFC4`), index `$FFC6`, palette flag `$FFC7`; both coordinates **8-bit** |
| Emitter bodies | V1.0 `01:52B5`, `01:5E58`; DX also `2C:5D86`, `2D:5E3A` | found by the byte pattern `F0 C4 47 F0 C5 4F`; DX reaches its copies via `01:5297` → `01:465A` on `[$FFF6] & $0F`, so the tap keys on the PC and reads the `$4000` pointer table out of `ctx->rom_bank` |

### Hook sites

| Hook | Kind | Scope | Effect |
|---|---|---|---|
| `$AF0A`–`$AF0D` | read override | bank 2, PC `$3CAA`/`$3CAB` | activation window becomes camX ± (`$60` + that side's view margin) |
| `$AF1A`–`$AF1D` | read override | bank 2, PC `$3CAA`/`$3CAB` | cull window becomes camX ± (`$A0` + that side's view margin) |
| `$A2B1` | read override | draw bank (3 on V1.0), PC `$409E` | an actor whose true offset from the native screen is outside `[−8, 176)` is handed a scroll shadow that puts it at screen X `$B8`, so the ROM's own `03:4025` drops it |
| `$AF12`–`$AF15` | read override | bank 2, PC `$408A`/`$4090` (right), `$40A9`/`$40AF` (left) | **Extended spawns only.** spawn-scan window becomes camX ± (`112` + that side's view margin), ramped ≤ 8 px per scanning frame |
| `$FFE2` | read tap | draw bank (3 on V1.0), PC `$401B` | captures the `$AD00` actor's metasprite in world coordinates, after the ROM's state/visibility gates and before its clipping |
| `$FFC5` | read tap | any emitter body, PC after `ldh a,[$FFC5]` (`$52BA`, `$5E5D`, and on DX `$5D8B`, `$5E3F`) | captures everything the SHARED emitter draws — Mario, fireballs, enemy fire, thrown items — in world coordinates, so an 8-bit screen X stops confining them to the native 160 columns |
| `00:327E` | **`[[imm_override]]`** | bank 0, PC `$327E` | **Extended spawns only.** the fireball despawn compare is answered with `A` (the ROM destroys the slot) or `A ^ $10` (it keeps it), decided in 16-bit world space against `view_left`/`view_width` ± 32 |
| `$FFC4` | read override | any emitter body, PC of / after `ldh a,[$FFC4]` (tap PC − 5 / − 3) | **Extended spawns only.** a fireball piece whose 8-bit screen X has wrapped is answered `$F0`, so the guest's own OAM entry lands off-screen instead of as a ghost inside the native strip |

The `$A2B1` hook is what makes the activation one safe. `03:409F` computes
screen X modulo 256, so an actor the widened activation keeps alive far off the
native screen would otherwise alias back into the visible 160 columns as a
ghost. Dropping it from the ROM's own OAM path also keeps the unguarded
40-entry OAM buffer from overflowing into `$A1A0+`. The `$FFC4` hook is the
same idea for the shared emitter, which has no drop test of its own.

**One `[[imm_override]]` site, and only one.** For the actor windows none was
needed: the constants are 8-bit immediates (`ld e,$60` / `$70` / `$A0`) that
cannot express the >255 pixel offsets a 32:9 view needs anyway, and overriding
the resulting RAM window expresses the intent exactly, in one place, for both
the generated and interpreter paths. The fireball despawn at `00:327E` is the
opposite case: there is no RAM window at all, every input is modulo 256, and
the compared immediate is the only thing about the test that can be changed.
Both `.toml`s declare it; the hook is installed whenever the widescreen mod is
on and returns the ROM's own `$C0` unless Extended is selected, so a default
Original build runs the same byte the literal would have been.

## Engine changes

Two changes to the shared engine (`gb-recompiled`), each committed separately.

### `debug server: peek command with explicit ROM/ERAM/WRAM/VRAM banks`

`read_ram` goes through `gb_read8`, so it can only see the banks the guest has
mapped -- and a game module's own read override can intercept it. The DX colour
work needs VRAM bank 1 (the BG attribute map) and WRAM bank 2 (the attribute
table) while the guest is running with VBK 0 and SVBK 1. `peek` reads the
backing arrays directly with explicit `rom_bank` / `ram_bank` / `wram_bank` /
`vram_bank` (omit any to follow the live one), chunked like `dump_ram`.
Read-only, game-agnostic, inert unless called.

### `Extend gb_custom_read_override to external RAM, banked WRAM and HRAM`

`gb_custom_read_override` was only wired into the banked-ROM and `$C000`–`$CFFF`
read paths. Super Mario Land 2 keeps its camera in HRAM and its actor state in
cartridge RAM, so the seam could not reach them. The hook is now also consulted
for external RAM, `$D000`–`$DFFF` and HRAM — three `hook ? hook(...) : value`
branches. It is game-agnostic and inert when no hook is installed.

Verified inert: with the mod off, frames 300, 900, 2500, 2700, 3000, 3400, 3800,
4200 and 4600 of the same input route are **byte-identical** between a build with
the change reverted and a build with it applied (9/9 PPM captures).

## Debug-server commands this module adds

`gb-recompiled/docs/DEBUG_SERVER.md` documents the engine's own commands and
sends game-specific ones here. All of these are **queries against always-on
state** — nothing has to be armed, and nothing is cleared by reading it.

| Command | Args | Answers |
|---|---|---|
| `sml2_view` | — | one line of everything the compositor decided this frame: gate result and reason, view geometry, scores, hold/debounce counters, the live camera and scroll registers read fresh, the spawn ledger, and the fireball ledger (`fb_reach`, `fb_kept`, `fb_killed`, `fb_vanilla`, `fb_failclosed`, `fb_unmatched`, `fb_hidden`, `fb_deaths`) |
| `sml2_sprites` | — | **streams** the composed sprite list for the frame on screen: every piece the two taps captured in WORLD coordinates (`src: "tap"`), then what the hardware left in OAM — `src: "oam"` for the entries the compositor places, `src: "oam_clipped"` for the ones it declines because the hardware shows no column of them. "The projectile is alive but not drawn" is only answerable by looking at this next to the tables |
| `sml2_fireballs` | `since` (int, optional) | **streams** the two `$A880` slots as they are now, the world-X boundary the `00:327E` override is holding them to, the counters, and then the always-on death ring from `since` — `{frame, slot, x, y, rel, dir, cam_x, view_left, view_width, cause}` per death, `cause` one of `vanilla` / `extended` / `failclosed` / `other`. A fireball's whole life is under a second, so this ring is the only honest answer to "where did it die" |
| `sml2_gate_log` | `since`, `limit` | the scene gate's rejection ring plus per-reason totals |
| `sml2_flip_log` | — | every wide ↔ native transition, with the reason and the scores behind it |
| `sml2_spawn_state` | — | the spawn ledger: the list, which records were spawned, jumped or stepped over, and the ring of edge crossings |
| `sml2_score_map` | — | the per-cell tile/attribute outcome of the last scored frame |
| `sml2_mod_state` | — | what the Mods page currently has selected |
| `sml2_width` | `width` (int) | re-resolve the composed width at runtime, for the width sweep in `tools/probe_adaptive.py` |
| `sml2_buttons` | `buttons` (mask) | installs a held input script; prefer the engine's own `set_input` in new code |
| `sml2_save` / `sml2_load` / `sml2_capture` | — | deprecated aliases for the engine's `save_state` / `load_state` / `screenshot`, pinning their historic default paths |

The `cause` field of a fireball death is worth spelling out, because three of
the four are this module talking and one is not. `vanilla` is the ROM's own
`$C0..$CF` window matching (Original always, Extended never). `extended` is the
widened boundary firing. `failclosed` is a slot destroyed because the gate went
off while it was already outside vanilla's reach. `other` is everything the
horizontal compare is not — a block, an enemy, or the vertical window at
`00:328F` — and it is seen by diffing the slots once a frame rather than by a
hook, which is what keeps the ring complete.

## Validation

Run from the game root with a **native Windows** Python 3 (the probes use
`CREATE_NO_WINDOW` and `SetWindowPos`; an MSYS2 Python will not work):

```powershell
python tools/probe_adaptive.py
python tools/probe_fit.py
python tools/probe_mods.py
python tools/probe_dx_widescreen.py   # both bodies at 32:9, colour gate + captures
python tools/probe_dx_flicker.py      # attract demo + play, 16k frames, no flicker
python tools/probe_spawns.py          # Original vs Extended vs mod off, incl. attract mode
python tools/probe_pause_pipe.py      # pause + warp pipe hold the wide view
python tools/probe_fireball.py        # fireball reach + drawn <-> alive, both bodies
```

Each probe copies the executable into its own directory under `logs/` with its
own save file and TCP debug port, so a real save is never touched. Captures and
JSON reports land beside them.

Results from the current build:

| Check | Result |
|---|---|
| Block map decode vs. hardware BG tilemap | 357/357 cells on every gameplay frame, all widths, both bodies |
| DX attribute decode vs. hardware VRAM bank 1 | 357/357 cells; `gate_attr_fail` 0 of 2776 scored frames |
| DX attribute model, re-derived independently | 1024/1024 cells of the whole 32x32 BG map, every sample |
| DX margin sprite palettes | 7 of the 8 CGB OBJ palettes reached; 4–5 pieces per frame out of VRAM bank 1 |
| Widths 256 / 336 / 512 / 768 / 1024 / 160 | view stays in bounds, never falls back to native |
| Fit resizing 1280x720 / 1600x450 / 1800x400 / 800x800 / 1536x432 | 256 / 512 / 648 / 160 / 512 px, each matching the live client aspect |
| Left wall clamp at level start | `view_left == bound_left == 0`, no black padding |
| Enemy reach | a live actor 328 px from the camera centre — impossible in vanilla, which frees anything past 160 |
| Activation/cull override | 2748 reads widened over an 840-frame scroll |
| Ghost guard | 730 aliased actors dropped from the ROM's OAM path over the same run |
| Camera travel | 262 → 1100 px under real guest input |
| Save / load replay | RAM and pixels identical across the replay |
| Pause | mode `$08`, renderer falls back to native; resumes wide afterwards |
| Mod off | no hooks installed, width 160 |
| Mods page | checkbox + aspect dropdown clicked in the real launcher, `sml2-mods.ini` round-trips, an explicit `SML2_WIDESCREEN=32:9` preset still loses to an unticked box |
| Long run | 9600 frames, camera to 3140 px, 14 sprite pieces composited, zero fallbacks after level entry |
| Spawn edge is the ROM's own in Original | 0 of 900 frames deviated from camX ± 112, and 0 with the mod off |
| Original vs mod off | identical spawned set (3 records) and identical consumed set, each at the same camera position; camera traces within 2 px (host frame pacing, not a decision) |
| Original installs no spawn hook | `spawn_reads` `[0, 0, 0, 0]`, `spawn_extend` 0 |
| Extended spawns a superset | all 3 of Original's records plus 1 more; 0 missed, 0 eaten |
| Extended reaches further | first contact 323 px from the camera centre vs 112 px (vanilla cannot exceed 112); ramp reach 275 px, lag 0 px; each record reached 174–211 px of camera travel earlier |
| Entries stepped over by the edge | 0 / 0 / 0 (mod off / Original / Extended), over 3 / 3 / 4 entries reached |
| Edge never moves more than 8 px between scanning frames | 0 / 0 / 0 violations |
| Attract-mode demo, no input at all | Original first contact 112 px, Extended 287 px; 0 / 0 entries stepped over, 0 / 0 consumed without spawning |
| Module ledger vs RAM-derived ledger | agree on every spawned record in both policies; `spawn_unpaired` 0, edge high/low reads paired 806 / 806 |
| Spawns during a debounce window | the window is genuinely entered — 25 debounced frames per 16 001-frame attract cycle on both bodies. `spawn_ungated` is 0 because none of those frames was also one the builder took a scan branch on, so the widened edge was never offered to an unproven frame in the first place; the decline path is there for the case that does coincide. `spawn_unpaired` 0, `pillarbox_model` 0 |
| Entries the scan edge CROSSED, 16 001-frame attract cycle | 0 / 0 faithful, 0 / 0 DX (Original / Extended), of 99 / 140 and 99 / 140 consumed — the rest are cursor seeks at the demo's level loads, which vanilla does too |
| Extended ramp over that cycle | reach 277 px right and 176 px left on the faithful body, 277 / 176 px on DX; edge high/low reads paired 5650/5650 right and 113/113 left |
| Fireball, mod off vs Original | identical death world X **and** identical frames alive, both directions, both bodies — `(1368, 22)` / `(1254, 29)` on DX, `(192, 31)` / `(42, 29)` on the faithful body |
| Fireball, vanilla despawn point | right camX + **112** (faithful) / + **116** (DX, the phase between its 3 px step and the camera's own), left camX − **134**; every one inside the ROM's `$C0..$CF` window |
| Fireball, Extended at 512 px | dies 0–3 px past `view_left ± 32`: DX right x **1503**, left **1235**; faithful right **544**, left **107** — all `cause: "extended"` in the ring |
| Fireball, Extended at 256 px | DX right **1404**, left **1225**; faithful right **288**, left **101** — every one different from the 512 px figure, so the boundary tracks the view and is not a wall |
| Fireball, drawn ⇔ alive | 0 violations over the 12 shots that have a composed list to check (the four mod-off shots have none): while a slot is live its two pieces are in the list at its own world position and nowhere else, and the frame it dies they are gone. The composed list lags the slot by one frame — it is copied at PPU line 0 from the previous frame's emitter pass — so the check steps one frame past the death before demanding an empty list |
| Fireball, aliased OAM suppressed | `fb_hidden` 123 (DX 512), 191 (faithful 512), 19 / 44 at 256 — and 0 in every Original run, which never gets far enough to alias |
| Fireball, no unmatched compares | `fb_unmatched` 0 and `fb_failclosed` 0 in every run; the ring's own death record agreed with the independently derived one every time |
| Fireball vs an enemy past the vanilla death point | faithful body, Extended: a shot stayed alive to **camX + 294** and passed within **1 px** in X of a live actor standing 106 px beyond where vanilla destroys it. It did not kill it — the actor was on a platform 51 px up and the shot hugs the ground — so the kill itself is **reported, not asserted**. The Original control never found an actor out there to aim at in the same walk, so the negative half is **not exercised**; what is asserted for Original is the stronger and always-available fact that its shot never survives past camX + 127 |
| Margin ghost from an aliased OAM X | 20 fireball OAM entries in one Original left-hand shot, every one 244–253 px right of the slot and every one inside the 512 px view; all 20 now reported as `oam_clipped` and drawn by nobody |
| Mod off is byte-identical | `tools/probe_byteident.py`: 7/7 PPM SHA-256 hashes (frames 2500, 2700, 3000, 3400, 3800, 4200, 4600 of the shared route, faithful body, `SML2_WIDESCREEN=off`) identical between a build with the whole branch checked out away and the branch tip — including the `[[imm_override]]` call now compiled into `00:327E` |

Headless throughput on this machine (4500 frames, same route):

| Width | fps |
|---|---|
| off (160) | 1913 |
| 256 | 1720 |
| 512 | 1566 |
| 1024 | 1454 |
| 4096 | 770 |

## Known limits

* **Extended spawns change spawn timing.** The default Extended moves the
  scan edge out to the composed view and ramps it at 8 px per scanning frame so
  nothing is eaten, but enemies do become active earlier than on hardware.
  Choose Original to keep the spawn scanner untouched; an enemy then appears
  inside the margin rather than walking in from off view. See
  [Enemy spawns](#enemy-spawns).
* **Extended is bounded by the game's own 16 actor slots.** The scanner returns
  without advancing its cursor when no slot is free, so a crowded screen simply
  defers a spawn — that is vanilla behaviour, but a wide view reaches more
  spawn points at once and meets it sooner. Nothing is lost when it happens;
  the entry is spawned when a slot frees, or consumed once the edge has moved
  past it, exactly as the ROM already does.
* **Original is not bit-identical to the mod being off, and cannot be.** It
  installs no spawn hook, so every scanner input is vanilla, but the widened
  activation and cull windows keep more actors alive, and the scanner's
  free-slot search sees that table. Measured over the probe route the spawned
  and consumed sets are identical either way; the coupling is structural, not
  observed.
* **16 actor slots and 40 OAM entries are ROM limits.** A very wide view cannot
  show more simultaneous actors than the game's own table holds.
* **Pause, the world map, title, file select, the level intro card, bonus and
  minigame rooms, and the credits all fall back to centred native output** by
  design. The credits in particular run a genuine per-scanline raster effect
  (bank `$1A`) that a flat wide render would break.
* **Exercised live on the Mushroom Zone intro level only** (the first level
  reachable from a new file), on both bodies; that level is tileset 0, so the
  DX attribute path has one tileset of live coverage. Other tilesets are
  covered by construction (the table is re-read each frame from its ROM
  source) and by the per-frame gate, not by a play-test. Boss rooms, pipe sub-rooms and the vertical levels
  use the same block map, scroll box and actor machinery and are covered by the
  per-frame decode gate, but have not been play-tested wide.
* **Enemy projectiles keep their vanilla range.** They are captured and drawn
  correctly in the margins, but how far they fly is untouched — extending them
  would be a difficulty change rather than a presentation fix. See
  [Enemy projectiles are unchanged, deliberately](#enemy-projectiles-are-unchanged-deliberately).
* **Mario's fireballs reach the view edge under Extended only.** Under Original
  they die where the ROM kills them, which in a wide view is partway across the
  picture — that is vanilla, and it is the price of vanilla gameplay decisions.
* Audio, timing and save data are untouched.

## Reverse-engineering notes

`F:\Projects\gbcrecomp\marioland2-disasm` (froggestspirit, 2014) has a
byte-identical `baserom.gb`, so its listing is usable, but **its label addresses
drift by several bytes** past roughly `$0386` and must not be cited; every
address here was taken from ROM bytes and confirmed against live RAM through the
runtime's TCP debug server.

Corrections found while doing this, recorded so they are not re-derived:

* `sScrollX $A2B1` / `sScrollY $A2B0` are **not** the camera. They are recomputed
  every frame from `$FFCA`/`$FFC8` and only shadow SCX/SCY.
* The level header's first eight bytes are **Y before X**, not X before Y as
  `research.txt` and `levels/levelheaders.asm` state. Level 1 reads camY = 448,
  camX = 80 — which the running game confirms exactly.
* There is **no sliding window and no mid-level streaming**; the whole level is
  resident at `$B000`–`$DFFF`.
* `levels/enemysets.asm` is a per-level enemy *graphics* table, not the placement
  list; placements are packed 3-byte records reached through `03:6037`.
* Gameplay metasprite entries are `(Yoff, Xoff, tile, attr)` with a `$80`
  terminator and no count byte — `research.txt` describes the overworld emitter.
* The status bar is the plain window layer. `LYC $FF45` is never written anywhere
  in the ROM and STAT interrupts are never enabled, so there is no raster split.
* The bonus game is **not** its own game mode; it is mode `$04` with
  `$A28B & 0xF0` set, which is why a mode-only scene check is not enough.
