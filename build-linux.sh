#!/usr/bin/env bash
# build-linux.sh — Linux x86_64 build + AppImage packaging for Super Mario Land 2.
#
# Linux counterpart to make_release.ps1. Same policy as the Windows zip, where
# everything the player owns lives next to the .exe: rom.cfg, keybinds.ini,
# runtime_prefs.ini, *.sav/*.rtc/*.stateN all live NEXT TO the .AppImage file.
# The engine anchors there itself — gb_host_paths.c prefers $APPIMAGE over
# /proc/self/exe — so state never resolves into the read-only squashfs mount,
# and an AppImage that is replaced or moved keeps finding the player's saves.
#
# Unlike the SNES scripts this one also GENERATES, because a GB recomp game has
# no committed source tree: generated/ and generated_dx/ come out of gbrecomp
# and are gitignored. Two bodies are generated — the faithful V1.0 build and
# the DX colour build (V1.0 + recomp/patches/sml2dx_v181.bps applied at
# generation time) — and both link into one executable. See DX.md.
#
# Generation needs the supported cart at
#   roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb   CRC32 D5EC24E4
# It is never packaged: the AppImage ships the BPS patch, never a ROM and never
# the DX hack.
#
# After packaging, test_appimage_layout.sh runs against the AppDir and the
# build FAILS if state lands inside the read-only payload, a user edit does not
# survive a relaunch, or a moved .AppImage does not re-anchor its state.
#
# Usage:
#   bash build-linux.sh                    # prod AppImage (default)
#   bash build-linux.sh --version 0.1.0    # stamp + name a release build
#   bash build-linux.sh --no-regen         # reuse an existing generated/ tree
#   bash build-linux.sh --no-package       # configure + build only
#   bash build-linux.sh --run              # launch the AppImage after building
#   bash build-linux.sh --out DIR          # where to drop the .AppImage
#   bash build-linux.sh --jobs N           # parallel build jobs (default: nproc)
#
# NOTE on --jobs: the generated bodies are multi-MB translation units. On a
# memory-constrained host too many concurrent jobs makes the compiler die with
# NO diagnostic (empty output, nonzero exit). Lower --jobs before suspecting
# the sources.
#
# Prereqs (Debian/Ubuntu):
#   sudo apt install build-essential cmake ninja-build pkg-config \
#        libsdl2-dev libgl1-mesa-dev patchelf file desktop-file-utils \
#        python3 curl imagemagick
# gb-recompiled is SDL2 (the SNES recomps are SDL3) — libsdl2-dev is correct.
# linuxdeploy/appimagetool are fetched into the build tree and verified against
# pinned SHA-256s, so packaging does not depend on what happens to be in PATH.
set -euo pipefail

# ============================ PER-GAME CONFIG ===============================
APP_NAME="SuperMarioLand2"
RELEASE_SLUG="SuperMarioLand2Recomp"       # matches the windows zip prefix
CMAKE_TARGET="Super_Mario_Land_2"
ROM_EXTS="gb gbc"
ROM_NAME="Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb"
ROM_CRC32="d5ec24e4"
BOXART="recomp/launcher/boxart.tga"        # AppImage icon source (optional)
# Read-only payload that must sit beside the executable inside the image. The
# DX body applies this BPS to the player's own V1.0 ROM in memory at boot.
EXTRA_PAYLOAD=( "recomp/patches/sml2dx_v181.bps" "recomp/patches/SML2DX_readme.txt" )
# Docs the launcher's Mods page refers the player to.
EXTRA_DOCS=( "README.md" "DX.md" "ADAPTIVE.md" )
TOML_FAITHFUL="super_mario_land_2.toml"
TOML_DX="super_mario_land_2_dx.toml"
# Super Mario Land 2's mods (Adaptive widescreen, DX colour) are compiled in
# via sml2_mods.c, not shipped as package manifests, so there is no mod catalog
# to stage — unlike Super Mario World's AppImage.
# ============================================================================

# Pinned AppImage tooling, so packaging is reproducible and does not depend on
# whatever happens to be installed on the build host. Both projects publish only
# a rolling "continuous" release, so these digests DO rotate upstream: on a
# checksum mismatch, verify the new artifact and re-pin here (or export
# LINUXDEPLOY_SHA / APPIMAGETOOL_SHA for a one-off build). Verified 2026-09-16;
# appimagetool's digest is the same one the snesrecomp Linux releases pin.
LINUXDEPLOY_URL="${LINUXDEPLOY_URL:-https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage}"
LINUXDEPLOY_SHA="${LINUXDEPLOY_SHA:-36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62}"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage}"
APPIMAGETOOL_SHA="${APPIMAGETOOL_SHA:-a6d71e2b6cd66f8e8d16c37ad164658985e0cf5fcaa950c90a482890cb9d13e0}"

DO_REGEN=1
DO_RUN=0
DO_PACKAGE=1
VERSION=""
JOBS="$(nproc 2>/dev/null || echo 4)"
REPO="$(cd "$(dirname "$0")" && pwd)"
OUT="$REPO/release-linux"

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2;;
    --regen) DO_REGEN=1; shift;;
    --no-regen) DO_REGEN=0; shift;;
    --run) DO_RUN=1; shift;;
    --no-package) DO_PACKAGE=0; shift;;
    --out) OUT="$2"; shift 2;;
    --jobs) JOBS="$2"; shift 2;;
    -h|--help) sed -n '2,45p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# Default the version to the VERSION file, then to the checked-out tag, so a
# release build cannot ship stamped "dev" by accident; --version always wins.
if [ -z "$VERSION" ]; then
  if [ -f "$REPO/VERSION" ]; then
    VERSION="$(head -n1 "$REPO/VERSION" | tr -d ' \t\r\n' | sed 's/^v//')"
  fi
fi
if [ -z "$VERSION" ]; then
  VERSION="$(git -C "$REPO" describe --tags --exact-match 2>/dev/null | sed 's/^v//' || true)"
  [ -n "$VERSION" ] || VERSION="dev"
fi

ENGINE="$REPO/gb-recompiled"
UI="$REPO/recomp-ui"
ENGINE_BUILD="$REPO/build-release-engine-linux"
BUILD="$REPO/build-linux-prod"

echo "==================== $APP_NAME (linux x86_64, v$VERSION) ===================="
cd "$REPO"

[ -f "$ENGINE/runtime/include/gb_body.h" ] || {
  echo "ERROR: gb-recompiled is not initialized; run 'git submodule update --init --recursive' first." >&2
  exit 1
}
[ -f "$UI/recomp_ui.cmake" ] || {
  echo "ERROR: recomp-ui is not initialized; run 'git submodule update --init --recursive' first." >&2
  exit 1
}

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; return 0; }
trap cleanup EXIT

# ── generate ────────────────────────────────────────────────────────────────
if [ "$DO_REGEN" = "1" ]; then
  [ -f "$REPO/roms/$ROM_NAME" ] || {
    echo "ERROR: generation needs the supported cart at roms/$ROM_NAME" >&2
    echo "       (it is used to generate, and is never packaged)" >&2
    exit 1
  }
  got="$(python3 -c "import sys,zlib;print('%08x'%(zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff))" "$REPO/roms/$ROM_NAME")"
  [ "$got" = "$ROM_CRC32" ] || {
    echo "ERROR: roms/$ROM_NAME has CRC32 $got; this project only supports $ROM_CRC32" >&2
    exit 1
  }
  echo "[0/5] ROM CRC32 $got OK"

  echo "[1/5] build gbrecomp"
  cmake -S "$ENGINE" -B "$ENGINE_BUILD" -DCMAKE_BUILD_TYPE=Release \
      -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DRECOMP_UI_ROOT="$UI"
  cmake --build "$ENGINE_BUILD" --target gbrecomp -j"$JOBS"

  GBRECOMP="$(find "$ENGINE_BUILD" -maxdepth 3 -type f -name gbrecomp | head -1)"
  [ -n "$GBRECOMP" ] || { echo "ERROR: gbrecomp binary not found under $ENGINE_BUILD" >&2; exit 1; }

  echo "[2/5] generate both bodies"
  # Body 1 owns main() and is the CMake project; body 2 is [options] body_only
  # and links into it. Paths in the tomls are repo-relative, so run from $REPO.
  "$GBRECOMP" --config "$TOML_FAITHFUL"
  "$GBRECOMP" --config "$TOML_DX"
else
  echo "[1/5] (--no-regen) reusing generated/"
  echo "[2/5] (--no-regen) reusing generated_dx/"
  [ -f "$REPO/generated/CMakeLists.txt" ] || {
    echo "ERROR: --no-regen but generated/CMakeLists.txt does not exist" >&2; exit 1; }
fi

# ── build ───────────────────────────────────────────────────────────────────
echo "[3/5] configure + build the game (-j$JOBS)"
cmake -S "$REPO/generated" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
    -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON \
    -DGBRECOMP_LAUNCHER_CONSOLE=gb -DRECOMP_UI_ROOT="$UI"
cmake --build "$BUILD" --target "$CMAKE_TARGET" -j"$JOBS"

# Locate the produced ELF by magic. On an NTFS mount every file reads as
# executable, so the exec bit proves nothing here.
BIN=""
while IFS= read -r f; do
  if [ "$(basename "$f")" = "$CMAKE_TARGET" ] && file -b "$f" 2>/dev/null | grep -q "ELF.*executable"; then
    BIN="$f"; break
  fi
done < <(find "$BUILD" -maxdepth 3 -type f)
[ -n "$BIN" ] || { echo "ERROR: no ELF named '$CMAKE_TARGET' under $BUILD" >&2; exit 1; }
echo "      ELF: $BIN ($(du -h "$BIN" | cut -f1))"

# The launcher's CRC gate is compiled in from extras.c. A binary generated from
# a different cart would not carry it; refuse to package one. Same check the
# Windows script makes, and the one Tetris/README.md documents.
python3 - "$BIN" "$ROM_CRC32" <<'PY'
import sys
blob = open(sys.argv[1], 'rb').read()
needle = int(sys.argv[2], 16).to_bytes(4, 'little')
at = blob.find(needle)
if at < 0:
    sys.exit("ERROR: executable does not carry the expected ROM CRC %s" % sys.argv[2])
print("      baked CRC %s found at offset 0x%X" % (sys.argv[2], at))
PY

if [ "$DO_PACKAGE" = "0" ]; then echo "      (--no-package) done."; exit 0; fi

# ── package ─────────────────────────────────────────────────────────────────
echo "[4/5] package AppImage"
mkdir -p "$OUT"
EXE="$(basename "$BIN")"
SLUG="$(echo "$APP_NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-')"
WORK="$(mktemp -d)"   # cleaned by the EXIT trap
APPDIR="$WORK/AppDir"; mkdir -p "$APPDIR"

TOOLS_DIR="$BUILD/appimage-tools"
mkdir -p "$TOOLS_DIR"
fetch_tool() { # url sha dest
  local url="$1" sha="$2" dest="$3" got=""
  if [ ! -f "$dest" ] || [ "$(sha256sum "$dest" | awk '{print $1}')" != "$sha" ]; then
    echo "      fetching $(basename "$dest")"
    curl -fL --retry 3 --silent --show-error "$url" -o "$dest.tmp"
    got="$(sha256sum "$dest.tmp" | awk '{print $1}')"
    if [ "$got" != "$sha" ]; then
      rm -f "$dest.tmp"
      echo "ERROR: $(basename "$dest") checksum mismatch" >&2
      echo "       url      $url" >&2
      echo "       expected $sha" >&2
      echo "       got      $got" >&2
      echo "       Upstream publishes only a rolling 'continuous' release, so this" >&2
      echo "       digest rotates. Verify the artifact, then update the pin at the" >&2
      echo "       top of this script (or export the *_SHA variable for one build)." >&2
      exit 1
    fi
    mv "$dest.tmp" "$dest"
  fi
  chmod 0755 "$dest"
}
LINUXDEPLOY_BIN="$TOOLS_DIR/linuxdeploy-x86_64.AppImage"
APPIMAGETOOL_BIN="$TOOLS_DIR/appimagetool-x86_64.AppImage"
fetch_tool "$LINUXDEPLOY_URL" "$LINUXDEPLOY_SHA" "$LINUXDEPLOY_BIN"
fetch_tool "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA" "$APPIMAGETOOL_BIN"
# --appimage-extract-and-run: WSL and most containers have no FUSE, and both
# tools are themselves AppImages.
LINUXDEPLOY=("$LINUXDEPLOY_BIN" --appimage-extract-and-run)
APPIMAGETOOL=("$APPIMAGETOOL_BIN" --appimage-extract-and-run)

# Icon: the real box art when ImageMagick is available, a flat placeholder
# otherwise (an AppImage without an icon is rejected by appimagetool).
ICON="$WORK/$SLUG.png"
IMAGE_TOOL=""
command -v magick >/dev/null 2>&1 && IMAGE_TOOL=magick
[ -z "$IMAGE_TOOL" ] && command -v convert >/dev/null 2>&1 && IMAGE_TOOL=convert
if [ -n "$IMAGE_TOOL" ] && [ -f "$REPO/$BOXART" ]; then
  "$IMAGE_TOOL" "$REPO/$BOXART" -resize 240x240 -background transparent \
      -gravity center -extent 256x256 "$ICON"
else
  echo "      (no ImageMagick or no $BOXART — using a placeholder icon)"
  python3 - "$ICON" "$SLUG" <<'PY'
import sys, zlib, struct, hashlib
out, slug = sys.argv[1], sys.argv[2]
h = hashlib.md5(slug.encode()).digest()
r, g, b = h[0] | 0x30, h[1] | 0x30, h[2] | 0x30
N = 256; row = bytes([0]) + bytes([r, g, b]) * N; raw = row * N
def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
open(out, "wb").write(png)
PY
fi

cat > "$WORK/$SLUG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Super Mario Land 2
Exec=$EXE
Icon=$SLUG
Categories=Game;
Terminal=false
EOF

"${LINUXDEPLOY[@]}" --appdir "$APPDIR" --executable "$BIN" \
    --desktop-file "$WORK/$SLUG.desktop" --icon-file "$ICON"

# The ImGui pre-boot launcher loads fonts + images from assets/ next to the
# exe (SDL_GetBasePath resolves to usr/bin inside the mount). CMake's launcher
# POST_BUILD staged them beside the build ELF; carry them into the AppDir or
# the launcher comes up with no fonts at all.
[ -d "$(dirname "$BIN")/assets" ] || {
  echo "ERROR: recomp-ui launcher assets/ missing beside $BIN" >&2
  exit 1
}
echo "      staging launcher assets/ -> AppDir/usr/bin/assets"
cp -r "$(dirname "$BIN")/assets" "$APPDIR/usr/bin/assets"

for rel in "${EXTRA_PAYLOAD[@]}"; do
  [ -f "$REPO/$rel" ] || { echo "ERROR: payload missing: $REPO/$rel" >&2; exit 1; }
  echo "      staging payload       -> AppDir/usr/bin/$(basename "$rel")"
  cp "$REPO/$rel" "$APPDIR/usr/bin/$(basename "$rel")"
done
for rel in "${EXTRA_DOCS[@]}"; do
  [ -f "$REPO/$rel" ] || continue
  cp "$REPO/$rel" "$APPDIR/usr/bin/$(basename "$rel")"
done

# Refuse to publish an image carrying a ROM. The extension sweep catches the
# obvious mistake; the CRC sweep catches a ROM renamed to something innocuous.
python3 - "$APPDIR" "$ROM_CRC32" <<'PY'
import os, sys, zlib
appdir, crc = sys.argv[1], int(sys.argv[2], 16)
bad = []
for root, _dirs, files in os.walk(appdir):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        if os.path.splitext(name)[1].lower() in ('.gb', '.gbc', '.sgb', '.sfc', '.smc'):
            bad.append(path)
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if not (32 * 1024 <= size <= 8 * 1024 * 1024):
            continue
        with open(path, 'rb') as handle:
            if zlib.crc32(handle.read()) & 0xffffffff == crc:
                bad.append(path)
if bad:
    sys.exit("ERROR: ROM content in the AppDir payload:\n  " + "\n  ".join(bad))
print("      payload carries no ROM")
PY

# Custom AppRun. State policy: everything user-visible lives NEXT TO the
# .AppImage, exactly like the Windows zip keeps it next to the exe. The engine
# anchors there itself (gb_host_paths.c prefers $APPIMAGE over /proc/self/exe),
# so this script must keep APPIMAGE exported. Here we only:
#   * read the controller natively on a Steam Deck,
#   * point the DX body at the BPS carried inside the read-only payload,
#   * seed rom.cfg from a ROM the player dropped beside the .AppImage,
#   * run from that folder.
rm -f "$APPDIR/AppRun"   # linuxdeploy leaves it a symlink to the real exe
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
export LD_LIBRARY_PATH="\$HERE/usr/lib:\${LD_LIBRARY_PATH}"
# Steam Deck: read the built-in pad as a real gamepad instead of letting Steam's
# desktop layout retype it as a keyboard (which otherwise sends Esc on B, etc.).
export SDL_JOYSTICK_HIDAPI_STEAM=1
export SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD=1
# The read-only payload beside the real executable. gb_host_paths.c looks here
# for sml2dx_v181.bps and falls back to the folder holding the .AppImage, so a
# player can also drop their own patch next to the image.
export GBRECOMP_ASSET_DIR="\$HERE/usr/bin"
SELF="\${APPIMAGE:-\$0}"
ROMDIR="\$(dirname "\$(readlink -f "\$SELF")")"
ROM=""
for ext in $ROM_EXTS; do
    for f in "\$ROMDIR"/*."\$ext"; do [ -e "\$f" ] && ROM="\$f" && break 2; done
done
cd "\$ROMDIR" 2>/dev/null || true
# Seed rom.cfg from a ROM dropped beside the .AppImage rather than passing it
# as argv[1]: a positional ROM skips the GUI launcher entirely, which leaves no
# route to Settings or the Mods page (where DX colour and Adaptive widescreen
# live) — and with no zenity/kdialog/yad on the host the launcher's own Browse
# button cannot open a picker either. Seeding the cache means the launcher
# opens with the ROM already resolved. Never clobber a rom.cfg that resolves.
if [ -n "\$ROM" ]; then
    cached=""
    [ -f "\$ROMDIR/rom.cfg" ] && cached="\$(head -n1 "\$ROMDIR/rom.cfg" 2>/dev/null | tr -d '\\r\\n')"
    if [ -z "\$cached" ] || [ ! -f "\$cached" ]; then
        [ -w "\$ROMDIR" ] && printf '%s\\n' "\$ROM" > "\$ROMDIR/rom.cfg" 2>/dev/null || true
    fi
fi
exec "\$HERE/usr/bin/$EXE" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

APP="$OUT/$RELEASE_SLUG-linux-$VERSION-x86_64.AppImage"
rm -f "$APP"
ARCH=x86_64 "${APPIMAGETOOL[@]}" "$APPDIR" "$APP"
chmod +x "$APP"
echo "      BUILT: $APP ($(du -h "$APP" | cut -f1))"

echo "[5/5] layout test (state next to the .AppImage, payload stays read-only)"
bash "$REPO/test_appimage_layout.sh" "$APPDIR"

sha256sum "$APP"

if [ "$DO_RUN" = "1" ]; then echo "[run] $APP"; "$APP" || true; fi
