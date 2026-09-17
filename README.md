# Super Mario Land 2: 6 Golden Coins — Recompiled

Static recompilation of the Game Boy title *Super Mario Land 2 - 6 Golden Coins
(UE) (V1.0)* (CRC32 `D5EC24E4`) to native code via
[gbrecompiled](https://github.com/mstan/gbrecompiled), with the shared
[recomp-ui](https://github.com/mstan/recomp-ui) launcher.

One executable, **two** recompiled bodies of that one cart: the faithful V1.0
build, and Super Mario Land 2 DX v1.8.1 derived from the same ROM in memory at
boot. The launcher's Mods page picks between them — see `DX.md`.

- `super_mario_land_2.toml` — faithful body (primary project)
- `super_mario_land_2_dx.toml` — DX body (V1.0 + `recomp/patches/sml2dx_v181.bps`)
- `extras.c` — game hooks (launcher name/platform, ROM CRC gate, mods)
- `recomp/launcher/boxart.tga` — launcher box art
- `ADAPTIVE.md` — opt-in adaptive widescreen mod (see Mods in the launcher)

The ROM is **not** included; supply your own at
`roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb`. That is the only
ROM this build accepts, for either body — the DX image is derived from it and is
never asked for.

## Build (Windows, MSYS2 MinGW64 first on PATH)

```powershell
git submodule update --init gb-recompiled recomp-ui
$T='C:\msys64\mingw64\bin'
cmake -G Ninja -S gb-recompiled -B gb-recompiled/build -DCMAKE_C_COMPILER=$T/gcc.exe -DCMAKE_CXX_COMPILER=$T/g++.exe -DCMAKE_MAKE_PROGRAM=$T/ninja.exe -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DRECOMP_UI_ROOT=$PWD/recomp-ui
ninja -C gb-recompiled/build gbrecomp
gb-recompiled/build/bin/gbrecomp.exe --config super_mario_land_2.toml
gb-recompiled/build/bin/gbrecomp.exe --config super_mario_land_2_dx.toml
cmake -G Ninja -S generated -B generated/build -DCMAKE_C_COMPILER=$T/gcc.exe -DCMAKE_CXX_COMPILER=$T/g++.exe -DCMAKE_MAKE_PROGRAM=$T/ninja.exe -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DGBRECOMP_LAUNCHER_CONSOLE=gb -DRECOMP_UI_ROOT=$PWD/recomp-ui
ninja -C generated/build
generated/build/Super_Mario_Land_2.exe
```

## Mods (opt-in, default off)

- **Adaptive widescreen** — Fit to window, 16:9, 21:9, 32:9. See `ADAPTIVE.md`.
- **DX color** — Super Mario Land 2 DX v1.8.1 by toruzz. Off, the game runs
  faithfully; on, the DX patch is applied to your ROM in memory at boot and the
  Game Boy Color build runs instead. Separate save file. Works together with
  Adaptive widescreen: the wide margins get the DX background attributes too.
  See `DX.md`.

Or from PowerShell: `.\Launch.ps1 -Build`, then `.\Launch.ps1 -DX` /
`.\Launch.ps1 -Faithful` to seed the toggle.

## Driving a build over TCP (probes)

The engine's debug server (`localhost:4370`, JSON-per-line) can load a save
state, step frames, press buttons and screenshot the presented frame. Its full
command list is `gb-recompiled/docs/DEBUG_SERVER.md`; `tools/tcp.py` is the
client every probe here imports:

```powershell
python tools\probe_pause_pipe_capture.py          # loads a fixture state, captures 32:9 PNGs
```

`sml2_save`, `sml2_load` and `sml2_capture` are **superseded** by the generic
`save_state`, `load_state` and `screenshot` commands; they survive only as thin
aliases that pin their historic default paths (`logs/probe.state`,
`logs/probe.ppm`) so older scripts keep working. New code should call the
generic commands and pass its own path.

## Running / packaging

For a release build, use `make_release.ps1` (Windows zip), `build-linux.sh`
(AppImage) or `build-macos.sh` (.app/.dmg) — they build, stage, gate on the ROM
CRC and verify the result. See `RELEASE.md`.

The build stages the mingw-w64 runtime DLLs (`SDL2.dll`, `libEGL.dll`, `libGLESv2.dll`,
`zlib1.dll`, `libgcc_s_seh-1.dll`, `libstdc++-6.dll`, `libwinpthread-1.dll`) and the
launcher `assets/` next to `generated/build/Super_Mario_Land_2.exe`, so it runs from a
bare double-click. Without them a PATH that lists another toolchain first (devkitPro)
fails with *entry point not found*. Ship the exe with those DLLs, `assets/` and
`sml2dx_v181.bps`.
