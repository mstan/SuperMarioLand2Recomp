# Release procedure

This file is the canonical *how to cut a release*. Per-version notes belong on
the GitHub release itself, not here.

## Assets: one archive per platform, only archives

| asset | built by | state |
|---|---|---|
| `SuperMarioLand2Recomp-windows-x64-v<Version>.zip` | `make_release.ps1` | verified end-to-end on this machine |
| `SuperMarioLand2Recomp-linux-<Version>-x86_64.AppImage` | `build-linux.sh` | verified end-to-end (WSL2 Ubuntu 24.04) |
| `SuperMarioLand2Recomp-macos-<Version>-<arch>.dmg` / `.zip` | `build-macos.sh` | **untested on this machine — no Mac here** |

Never publish a bare `Super_Mario_Land_2.exe`. It is useless without the
mingw-w64 runtime DLLs and the recomp-ui launcher `assets/` beside it, and it
is redundant next to the zip.

## What ships

* `Super_Mario_Land_2.exe` (one executable, **two** recompiled bodies: faithful
  V1.0 and DX colour — see `DX.md`)
* the seven mingw-w64 runtime DLLs: `SDL2.dll`, `libEGL.dll`, `libGLESv2.dll`,
  `zlib1.dll`, `libgcc_s_seh-1.dll`, `libstdc++-6.dll`, `libwinpthread-1.dll`.
  `libEGL.dll` is `dlopen`ed by SDL/ANGLE, so `ldd` does not list it — omit it
  and the game dies at startup with *"Could not load EGL library"* on any
  machine without msys2.
* `assets/` — the recomp-ui launcher's fonts and images. Without it the
  pre-boot launcher renders blank.
* `sml2dx_v181.bps` + `SML2DX_readme.txt` — the DX colour patch, applied to the
  player's own ROM **in memory at boot**.
* `README.md`, `DX.md`, `ADAPTIVE.md`, and `LICENSE` if the repo has one (it
  does not yet; `make_release.ps1` warns and continues).
* `roms/README.md` — a placeholder telling the player which cart to supply.

## What never ships

**No ROM, and nothing derived from one.** Not the supported cart, not the DX
image, not an `.extended.gbc` the engine may have written next to a developer's
build. All three packaging scripts sweep their staged output twice before
writing an archive:

1. by extension (`.gb`, `.gbc`, `.sgb`, `.sfc`, `.smc`), and
2. by content — every file between 32 KiB and 8 MiB is CRC32'd and the build
   fails if any of them is `D5EC24E4`, which catches a ROM renamed to something
   innocuous.

Also never staged: `rom.cfg`, `keybinds.ini`, `runtime_prefs.ini`,
`sml2-mods.ini`, `imgui.ini`, `*.sav`, `*.rtc`, `*.state*`. Those are user
state, and the game writes them next to itself — which is why the Windows
script runs its start-up check against a *copy* of the stage and archives the
pristine one.

## The CRC gate

There is exactly one supported cart:

```
Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb
CRC32  D5EC24E4      512 KiB
```

`extras.c:game_get_expected_crc32()` compiles that value into the binary, and
the launcher refuses any other ROM — including the V1.2 revision, whose bank 0
is this one shifted by +3 over `0x0049C`–`0x0383D`. The DX body derives its own
image from the same file, so there is one CRC for both bodies and the player is
never asked for the hack.

Every packaging script enforces the gate twice:

* **before generating** — the ROM at `roms/<name>` must CRC to `D5EC24E4`, or
  the build stops. A release generated from the wrong revision would produce a
  binary that cannot load the cart it advertises.
* **before packaging** — the built executable is searched for the little-endian
  bytes `E4 24 EC D5`. Absent means the binary was not built from this
  project's cart, and packaging fails. (Same check Tetris's README documents:
  `python -c "d=open('...exe','rb').read(); print(hex(d.find((0xD5EC24E4).to_bytes(4,'little'))))"`.)

## Versioning

`VERSION` at the repo root is the source of truth; it currently reads `0.1.0`.
All three scripts read it when no `--version` / `-Version` is passed, then fall
back to an exact-match git tag, then to `dev`.

Cutting `vX.Y.Z`:

1. edit `VERSION`, commit;
2. tag `vX.Y.Z` on that commit;
3. build all three platforms (below), or push the tag and let
   `.github/workflows/release.yml` do it.

The tag drives the workflow; the file drives a local build. Keep them equal —
nothing enforces it, because a tag does not exist yet while you are testing the
build that will carry it.

## Windows

```powershell
.\make_release.ps1                      # version from VERSION
.\make_release.ps1 -Version 0.2.0
.\make_release.ps1 -SkipBuild           # repackage what is already built
```

It builds exactly what `Launch.ps1 -Build` builds — `gbrecomp`, then the
faithful body, then the DX body, then cmake + ninja — into build directories of
its own (`build-release-engine/`, `generated/build-release/`) so packaging never
disturbs a development build. The profile is `CMAKE_BUILD_TYPE=Release` with
`GBRECOMP_GENERATED_OPT_LEVEL=1`: the host runtime, launcher and DX/widescreen
code get `-O3`, while the generated ROM bodies keep the `-O1` profile every
playtest of this game has used. `-GeneratedOptLevel 2` raises it if a release
ever wants to, at a large cost in build time.

Then, from a **clean PATH** (`System32` only, so a DLL missing from the zip
cannot be papered over by msys2 or devkitPro being on the developer's PATH), it
runs a copy of the staged tree twice:

* headless (`GBRECOMP_NO_LAUNCHER=1`, SDL dummy drivers, `--benchmark`), and
  answers `{"cmd":"ping"}` on the always-on TCP debug server
  (`GBRECOMP_DEBUG_PORT`, default 4370). The reply carries a frame counter, so
  a nonzero frame number proves the recompiled body is *executing*, not merely
  that the process loaded;
* again with `--limit-frames 600`, asserting exit code 0 and that the launcher
  reported resolving a ROM.

The zip is written with portable (`/`) entry names and then re-read to reject
any Windows-only name. With `Compress-Archive`'s backslash names a Proton or
Steam Deck user's extractor produces files literally called
`assets\fonts\LatoLatin-Regular.ttf` and the launcher finds no fonts at all.

The zip lands in `release-stage\` (gitignored).

## Linux

```bash
bash build-linux.sh                     # version from VERSION
bash build-linux.sh --version 0.2.0 --jobs 8
bash build-linux.sh --no-regen          # reuse an existing generated/ tree
```

Needs `build-essential cmake ninja-build pkg-config libsdl2-dev
libgl1-mesa-dev patchelf file desktop-file-utils imagemagick python3 curl`.
gb-recompiled is **SDL2** (the snesrecomp games are SDL3), so `libsdl2-dev` is
the right package.

The AppImage lands in `release-linux/`. The script:

* generates both bodies and builds them, then finds the ELF by `file` magic —
  on an NTFS mount every file reads as executable, so the exec bit proves
  nothing;
* fetches `linuxdeploy` + `appimagetool` into the build tree and verifies them
  against pinned SHA-256s. Both upstreams publish only a rolling `continuous`
  release, so those digests **do** rotate: on a mismatch the script prints the
  new digest and stops, and you re-pin at the top of the file after verifying
  the artifact. Both tools run with `--appimage-extract-and-run`, because WSL
  and most containers have no FUSE;
* stages the launcher `assets/` into `usr/bin` (the ImGui launcher loads them
  via `SDL_GetBasePath`, which resolves inside the mount) and the DX patch
  beside them, pointing the engine at that payload with `GBRECOMP_ASSET_DIR`;
* writes an AppRun that keeps `$APPIMAGE` exported, exports the SDL hints that
  make a Steam Deck's pad read as a real gamepad, and seeds `rom.cfg` from a
  ROM the player dropped beside the `.AppImage`;
* runs `test_appimage_layout.sh` and **fails the build** if state leaks into
  the read-only payload, a user edit does not survive a relaunch, or a moved
  `.AppImage` does not re-anchor its state.

### Why the ROM is seeded into `rom.cfg` and not passed as `argv[1]`

A positional ROM counts as an explicit ROM and skips the GUI launcher
entirely — which is the only route to Settings and to the Mods page where DX
colour and Adaptive widescreen live. On a desktop with no zenity/kdialog/yad
the launcher's own Browse button cannot open a picker either, so the player
would be stuck with no way in at all. Seeding the cache instead means the
launcher opens with the ROM already resolved.

### Building it from Windows

There is no Linux box here; the AppImage is built through WSL2:

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/Projects/gbcrecomp/<worktree> && bash build-linux.sh --jobs 8"
```

Ubuntu 24.04 under WSL2 builds and packages fine, and the produced AppImage
runs there headless. Note that an AppImage links against the **build host's**
glibc: the CI job uses `ubuntu-22.04` for that reason, and an AppImage built
from 24.04 will refuse to start on older distributions.

## macOS

```bash
bash build-macos.sh                     # .app + .dmg for the host arch
bash build-macos.sh --arch universal --zip
```

Needs `brew install cmake ninja sdl2 dylibbundler create-dmg`.

**This script has never been run.** There is no Mac on this machine. It mirrors
snesrecomp's `tools/build-macos.sh`, which has been run, adapted to this
engine: SDL2 instead of SDL3, a generated CMake project instead of the repo
root, two recompiled bodies instead of one, and an `.icns` built from
`recomp/launcher/boxart.tga` with `sips` + `iconutil`. Treat the first run on a
Mac as a bring-up — expect to fix something — not as a regression.

The bundle's `CFBundleExecutable` is a small shell launcher: Finder starts a
`.app` with an arbitrary working directory, so it `cd`s to the folder holding
the `.app` and seeds `rom.cfg` from a ROM sitting there. `dylibbundler` copies
`libSDL2` into `Contents/Frameworks` and rewrites its install name so the
bundle runs without Homebrew, and the bundle is ad-hoc codesigned so Gatekeeper
allows it locally (a Developer ID is only needed to distribute without the
`xattr -dr com.apple.quarantine` dance).

## Where state lives — and the engine change that made it true

All three packages follow one rule: **everything the player owns lives beside
the thing they double-click.** `rom.cfg`, `keybinds.ini`, `runtime_prefs.ini`,
`sml2-mods.ini`, `*.sav`, `*.rtc`, `*.state*` sit next to the `.exe`, next to
the `.AppImage`, and next to the `.app`.

That is enforced in the engine, not in the packaging: `gb_host_paths.c`
(`gb_host_state_dir()` / `gb_host_asset_dir()`) resolves the state directory
from `$GBRECOMP_STATE_DIR`, then `$APPIMAGE`, then a `.app` bundle's container,
then the executable's own directory. Read-only payload — the DX patch, the
launcher assets — resolves separately from `$GBRECOMP_ASSET_DIR` or the
executable's directory.

Before that module existed, `rom.cfg` and `keybinds.ini` resolved against the
**process working directory** on everything except Windows, and prefs and saves
resolved against `SDL_GetBasePath()` — which inside an AppImage is `usr/bin` in
the read-only squashfs. A Linux build launched from a desktop entry cached its
ROM path into `$HOME` and lost every setting on the next launch. Any GB recomp
game picking up the current engine gets the fix; nothing game-specific was
needed.

## CI

`.github/workflows/release.yml` runs all three scripts on `v*` tags (and on
`workflow_dispatch`), uploads each platform's archive as an artifact, and opens
a **draft** release with them. It never publishes: the notes and a real
play-test are a human step.

snesrecomp's game repos have no release workflow to mirror — releases there are
cut by hand — so this is the first one, and it deliberately shells out to the
same three scripts a developer runs locally.

### The ROM secret

A GB recomp game has no committed source tree: `generated/` and `generated_dx/`
come out of `gbrecomp` and need the cart. CI therefore needs the ROM, and the
only sane place for it is a repository secret:

```bash
gh secret set SML2_ROM_B64 --repo mstan/SuperMarioLand2Recomp \
   < <(base64 -w0 "roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb")
```

Every job decodes it, and refuses to continue if it is missing. It never
reaches an artifact — the ROM sweeps described above run in all three scripts.
Without the secret, releases have to be cut locally.

### Submodules

`gb-recompiled` and `recomp-ui` are **public** repositories
(`mstan/gbrecompiled`, `mstan/recomp-ui`), and `.gitmodules` points at `https://`
URLs so `actions/checkout` with `submodules: recursive` clones them with the
default token. The SSH (`git@github.com:`) URLs that used to be there could not
be cloned by Actions at all. If either repo is ever made private, this workflow
needs a deploy key (`ssh-key:` on `actions/checkout`) instead — there is no
token that reaches another private repo by default.

Locally these two are NTFS junctions into the sibling working trees, not real
submodule checkouts; changing the recorded URL does not disturb that.

## Cutting a release: the checklist

1. The tree is the release commit, `gb-recompiled` and `recomp-ui` at their
   intended pins, `disasm/marioland2` initialized
   (`git submodule update --init --recursive`). `generated/` and
   `generated_dx/` are untracked and are regenerated by each script.
2. `VERSION` holds the version being cut.
3. Build and package all three platforms.
4. Smoke-test from a scratch directory on at least Windows and Linux: extract
   or `chmod +x`, drop a ROM beside it, launch, reach World 1. Confirm the
   launcher renders (proves `assets/` resolved), that the Mods page toggles DX
   colour and Adaptive widescreen, and that a save survives a relaunch.
5. Write the release notes and publish — only after sign-off on the artifacts.

```powershell
gh release create vX.Y.Z `
    release-stage\SuperMarioLand2Recomp-windows-x64-vX.Y.Z.zip `
    release-linux\SuperMarioLand2Recomp-linux-X.Y.Z-x86_64.AppImage `
    --title "vX.Y.Z — <headline>" --notes-file <notes.md>
```

## Install (boilerplate for the notes)

1. Extract the zip (Windows) or `chmod +x` the `.AppImage` (Linux).
2. Supply your own dump of *Super Mario Land 2 - 6 Golden Coins (UE) (V1.0)*
   (CRC32 `D5EC24E4`). On Linux, drop it beside the `.AppImage` and it is
   picked up automatically; on Windows, put it in `roms/` and pick it in the
   launcher. The path is remembered in `rom.cfg`.
3. Turn on **DX colour** or **Adaptive widescreen** in the launcher's Mods
   page. DX needs no second ROM — the patch is applied to yours in memory at
   boot, and DX has its own save file.
4. Saves, keybinds and settings all live next to the executable / `.AppImage`.
