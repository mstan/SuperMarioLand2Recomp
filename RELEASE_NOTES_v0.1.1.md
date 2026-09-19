# Super Mario Land 2: 6 Golden Coins — Recompiled v0.1.1

v0.1.1 fixes the Linux ROM picker: the launcher now uses its built-in file browser on Linux (no zenity/kdialog needed), starts beside the AppImage, and a ROM dropped next to the program or in `roms/` is picked up automatically on every platform.

First release notes follow. A native static recompilation of *Super Mario Land 2 - 6 Golden Coins (UE) (V1.0)*
for Windows and Linux, with the shared Retro Launcher and two opt-in mods.

## What you need

Your own copy of **Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb** (CRC32 `D5EC24E4`).
Nothing else is accepted. No ROM or patched image ships in this release.

## Highlights

- **Faithful**: the original Game Boy game, recompiled to native code, running at the original
  frame cadence. All 8 inputs, save files and savestates (F5/F8, slots F6/F7) work.
- **DX color mod** (Mods page): applies *Super Mario Land 2 DX v1.8.1* by toruzz to your ROM in
  memory at boot and runs a second, fully recompiled Game Boy Color body — colour, Luigi,
  no slowdown. Off = faithful body. The patch (BPS) ships beside the executable; your ROM file is
  never modified.
- **Adaptive widescreen mod** (Mods page): Fit-to-window, 16:9, 21:9 or 32:9. The hardware PPU
  still renders 160×144; the margins are composited from the game's own level data, so terrain,
  enemies, coins and colour palettes match. Works with both bodies, holds through pause and warp
  pipes, and fails closed to the native picture on screens it cannot prove (title, map, bonus
  rooms, credits).
  - **Enemy spawns: Extended** (default) spawns enemies at the edge of what you can see and lets
    fireballs travel the whole view. **Original** keeps vanilla spawn timing and fireball range.

## Fixed during playtesting

- Keyboard: a rebound key no longer keeps driving a second action through a hidden secondary
  binding; the launcher shows both slots per action.
- Widescreen on DX: flicker and drop-outs in the attract demo, pillarboxing on pause, in warp
  pipes and under pipes, mismatched margins during a pipe dive.
- Fireballs vanishing part-way across the wide view.

## Known limits

- The pause-menu launcher branding (Game Boy vs Game Boy Color) reflects the DX setting of the
  previous launch.
- Widescreen has been play-tested most in Mushroom Zone; other zones are covered by the per-frame
  proof, not by hours of play. Report anything you see with the zone and mod settings.
- macOS build script is included but untested on real hardware.

## Install

**Windows**: unzip anywhere, put your ROM in `roms/` (or pick it in the launcher), run
`Super_Mario_Land_2.exe`. **Linux**: `chmod +x` the AppImage, put your ROM next to it, run it.
Saves, states and settings are written beside the executable / AppImage.
