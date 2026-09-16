# Super Mario Land 2: 6 Golden Coins — Recompiled

Static recompilation of the Game Boy title *Super Mario Land 2 - 6 Golden Coins
(UE) (V1.2)* (CRC32 `635A9112`) to native code via
[gbrecompiled](https://github.com/mstan/gbrecompiled), with the shared
[recomp-ui](https://github.com/mstan/recomp-ui) launcher.

- `super_mario_land_2.toml` — recompiler configuration
- `extras.c` — game hooks (launcher name/platform, ROM CRC gate, mods)
- `recomp/launcher/boxart.tga` — launcher box art
- `ADAPTIVE.md` — opt-in adaptive widescreen mod (see Mods in the launcher)

The ROM is **not** included; supply your own at
`roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.2) [!].gb`.

## Build (Windows, MSYS2 MinGW64 first on PATH)

```powershell
git submodule update --init gb-recompiled recomp-ui
$T='C:\msys64\mingw64\bin'
cmake -G Ninja -S gb-recompiled -B gb-recompiled/build -DCMAKE_C_COMPILER=$T/gcc.exe -DCMAKE_CXX_COMPILER=$T/g++.exe -DCMAKE_MAKE_PROGRAM=$T/ninja.exe -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DRECOMP_UI_ROOT=$PWD/recomp-ui
ninja -C gb-recompiled/build gbrecomp
gb-recompiled/build/bin/gbrecomp.exe --config super_mario_land_2.toml
cmake -G Ninja -S generated -B generated/build -DCMAKE_C_COMPILER=$T/gcc.exe -DCMAKE_CXX_COMPILER=$T/g++.exe -DCMAKE_MAKE_PROGRAM=$T/ninja.exe -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DGBRECOMP_LAUNCHER_CONSOLE=gb -DRECOMP_UI_ROOT=$PWD/recomp-ui
ninja -C generated/build
generated/build/Super_Mario_Land_2.exe
```

## Mods (opt-in, default off)

- **Adaptive widescreen** — Fit to window, 16:9, 21:9, 32:9. See `ADAPTIVE.md`.
- **DX color** (planned) — Super Mario Land 2 DX v1.8.1 by toruzz. Requires the
  v1.0 (UE) ROM (No-Intro CRC32 `D5EC24E4`); the patch does not apply to V1.2.
