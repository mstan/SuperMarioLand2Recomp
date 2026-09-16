# Adaptive widescreen

An opt-in mod that renders Super Mario Land 2's world across the whole window
instead of the Game Boy's 160x144 crop. The emulated hardware stays native: the
PPU still renders 160x144, the save-state layout is unchanged, and with the mod
off the executable is byte-for-byte the faithful build.

Supported ROM: **Super Mario Land 2 - 6 Golden Coins (UE) (V1.2)**, CRC32
`0x635A9112`. Every address below was verified against that ROM.

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

Environment override (seeds the launcher controls, loses to the checkbox):
`SML2_WIDESCREEN=fit | 16:9 | 21:9 | 32:9 | off | <integer width>`.
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

Two independent gates, both must pass or the frame falls back to a centred
native 160x144 image:

* the game's own mode enum says scrolling gameplay, it is not a bonus room, and
  no pipe/door transition is in flight, with LCDC/WY/WX in their gameplay state;
* **the block map decode reproduces the BG tilemap the hardware is actually
  showing** over the visible 21x18 tile grid, at 95% or better.

The second gate is the important one: it proves, every frame, the exact claim
the margins rest on. Anything that reuses level RAM with different VRAM — the
pause menu, the world map, the level intro card, the file select — fails it.
Measured on every gameplay frame of every probe run: **378/378 cells, always.**

Pause deliberately falls back to native even though its frame is otherwise
identical to gameplay; that is a policy choice, not a limitation (one constant,
`SML2_MODE_PLAY`, in `sml2_adaptive.c`).

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
| `SetScroll` | `00:2065` | writes `sScrollY $A2B0 = camY−72−shake`, `sScrollX $A2B1 = camX−80`; VBlank `00:0154` copies them to SCY/SCX |
| Mario world X/Y | `$FFC2`/`$FFC3`, `$FFC0`/`$FFC1` | — |
| Level block map | `$B000`–`$DFFF` | `MEM[$B000 + ((wy>>4)&0xFF)*0x100 + ((wx>>4)&0xFF)]`, 256x48 blocks = 4096x768 px, IDs 0..127, mutated live |
| Block map loader | `00:0361` → `00:0386` | RLE (bit 7 = run flag) from the map bank named by level header byte `$0D`; expands to exactly `0x3000` bytes |
| Block map readers | `00:1F32`, `00:096C`, `00:0A38` | `GetBlockAt` and the VRAM row/column loaders — the address arithmetic this mod clones |
| Block definitions | `$A600`–`$A7FF` | 128 entries x 4 tile indices, order TL, TR, BL, BR, no attribute byte (DMG) |
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
| Spawn scanner | `02:4C31` / `02:4CEB` | **left vanilla** — spawn points and the bidirectional list cursor are untouched |
| Actor draw | `03:4000` | per-actor draw routine |
| Actor screen position | `03:4090` (reads `$A2B0`), `03:409B` (reads `$A2B1`) | screenY = actorY_lo + `$10` − SCY, screenX = actorX_lo + `$08` − SCX, both modulo 256 |
| Whole-actor drops | `03:4020`, `03:4025` | `cp $B8` on screen Y then screen X |
| Per-piece clips | `03:4055` (`cp $A0`), `03:4078` (`cp $A8`) | OAM Y / OAM X rejects |
| Metasprite tables | `03:40B1`, `03:4F11` (selected by `$AF06`), data from `03:4201` | 4 bytes per piece `(Yoff, Xoff, tile, attr)`, terminator `$80` in Yoff; flip bits 5/6 of the actor's attribute XOR rewrite the offsets as `~v − 7` |
| OAM shadow buffer | `$A100`–`$A19F`, DMA stub `$FFA0` | 40 entries, **no overflow guard anywhere in the ROM** |

### Hook sites

| Hook | Kind | Scope | Effect |
|---|---|---|---|
| `$AF0A`–`$AF0D` | read override | bank 2, PC `$3CAA`/`$3CAB` | activation window becomes camX ± (`$60` + that side's view margin) |
| `$AF1A`–`$AF1D` | read override | bank 2, PC `$3CAA`/`$3CAB` | cull window becomes camX ± (`$A0` + that side's view margin) |
| `$A2B1` | read override | bank 3, PC `$409E` | an actor whose true offset from the native screen is outside `[−8, 176)` is handed a scroll shadow that puts it at screen X `$B8`, so the ROM's own `03:4025` drops it |
| `$FFE2` | read tap | bank 3, PC `$401B` | captures the metasprite in world coordinates after the ROM's state/visibility gates and before its clipping |

The third hook is what makes the second one safe. `03:409F` computes screen X
modulo 256, so an actor the widened activation keeps alive far off the native
screen would otherwise alias back into the visible 160 columns as a ghost.
Dropping it from the ROM's own OAM path also keeps the unguarded 40-entry OAM
buffer from overflowing into `$A1A0+`.

No `[[imm_override]]` sites were needed. The window constants are 8-bit
immediates (`ld e,$60` / `$70` / `$A0`) that cannot express the >255 pixel
offsets a 32:9 view needs, and overriding the resulting RAM window expresses the
intent exactly, in one place, for both the generated and interpreter paths.

## Engine change

One change to the shared engine (`gb-recompiled`), committed separately as
*"Extend gb_custom_read_override to external RAM, banked WRAM and HRAM"*:

`gb_custom_read_override` was only wired into the banked-ROM and `$C000`–`$CFFF`
read paths. Super Mario Land 2 keeps its camera in HRAM and its actor state in
cartridge RAM, so the seam could not reach them. The hook is now also consulted
for external RAM, `$D000`–`$DFFF` and HRAM — three `hook ? hook(...) : value`
branches. It is game-agnostic and inert when no hook is installed.

Verified inert: with the mod off, frames 300, 900, 2500, 2700, 3000, 3400, 3800,
4200 and 4600 of the same input route are **byte-identical** between a build with
the change reverted and a build with it applied (9/9 PPM captures).

## Validation

Run from the game root with a **native Windows** Python 3 (the probes use
`CREATE_NO_WINDOW` and `SetWindowPos`; an MSYS2 Python will not work):

```powershell
python tools/probe_adaptive.py
python tools/probe_fit.py
python tools/probe_mods.py
```

Each probe copies the executable into its own directory under `logs/` with its
own save file and TCP debug port, so a real save is never touched. Captures and
JSON reports land beside them.

Results from the current build:

| Check | Result |
|---|---|
| Block map decode vs. hardware BG tilemap | 378/378 cells on every gameplay frame, all widths |
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

Headless throughput on this machine (4500 frames, same route):

| Width | fps |
|---|---|
| off (160) | 1913 |
| 256 | 1720 |
| 512 | 1566 |
| 1024 | 1454 |
| 4096 | 770 |

## Known limits

* **Spawn points are vanilla.** Enemy activation and culling are widened, but the
  spawn scanner is not: it spawns on exact equality with an 8-pixel-aligned edge
  at camX + 112 and consumes any list entry it passes, so moving that edge in one
  step would silently eat enemies. The visible cost is that at wide aspects an
  enemy can appear inside the right margin rather than walking in from off view.
  Widening it safely needs a per-frame ramp of at most 8 px, which is not done.
* **16 actor slots and 40 OAM entries are ROM limits.** A very wide view cannot
  show more simultaneous actors than the game's own table holds.
* **Pause, the world map, title, file select, the level intro card, bonus and
  minigame rooms, and the credits all fall back to centred native output** by
  design. The credits in particular run a genuine per-scanline raster effect
  (bank `$1A`) that a flat wide render would break.
* **Exercised live on the Mushroom Zone intro level only** (the first level
  reachable from a new file). Boss rooms, pipe sub-rooms and the vertical levels
  use the same block map, scroll box and actor machinery and are covered by the
  per-frame decode gate, but have not been play-tested wide.
* Mario himself and the effects/particle table are drawn by emitters that never
  clip, so they are always inside the native strip; only the `$AD00` actors are
  composited into the margins.
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
