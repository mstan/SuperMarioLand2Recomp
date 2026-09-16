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
| `[rom] dispatch_manifest` | DX | this body's own Tier-0 seed manifest |
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

Widescreen composes the margins itself, from the level's block map, with the
monochrome tile model: it snapshots VRAM bank 0 only and draws every margin
cell through BG palette 0. That is exactly right on the faithful DMG body and
wrong on DX, whose cart header says `0xC0` — CGB-only, with per-tile attribute
bytes (palette number, VRAM bank, flips, priority) in VRAM bank 1. The native
160 columns would be in full colour and the synthesised margins beside them
would not.

So the two mods are mutually exclusive today, stated in the launcher rather
than discovered after Play: with DX color on, the Adaptive widescreen row shows
"Unavailable while DX color is on…" in the warning colour, and
`sml2_adaptive_init()` refuses to install the compositor. Lifting it needs the
margin renderer to snapshot both VRAM banks and honour the attribute byte per
cell — see `sml2_adaptive.c`'s bindings table for the exact list.

Everything *else* in the compositor is already body-correct. The one binding the
hack moved is the actor draw routine:

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

1. **The DX body has not been booted.** It hangs at boot (interpreted loop
   `24:7875` ↔ `00:38B3`); a separate fix is in flight. Everything here is
   verified structurally — the body links, registers, and is selected — but no
   DX frame has been rendered through this seam.
2. **Widescreen on DX** is gated off, as above.
3. **`dispatch_misses_dx.toml` is empty.** The DX body has no Tier-0 seeds yet;
   harvest them against the DX body once it boots. Do **not** reuse the faithful
   body's manifest — the hack relocates code.
4. **`recomp/sml2_v10.sym` is faithful-only.** The disassembly targets V1.0; the
   DX config deliberately omits `symbols`.
