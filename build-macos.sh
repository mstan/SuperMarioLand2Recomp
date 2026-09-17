#!/usr/bin/env bash
# build-macos.sh — macOS build + .app/.dmg packaging for Super Mario Land 2.
#
# ############################################################################
# # UNTESTED ON THE DEVELOPMENT MACHINE. This repo is developed on Windows   #
# # with a Linux (WSL) cross-check; there is no Mac here, so this script has #
# # never been executed. It mirrors snesrecomp's tools/build-macos.sh, which #
# # has been run, adapted to the GB engine (SDL2 rather than SDL3, a         #
# # generated CMake project rather than the repo root, two recompiled bodies #
# # rather than one). Treat the first run on a Mac as a bring-up, not a      #
# # regression: expect to fix something. See RELEASE.md.                     #
# ############################################################################
#
# macOS counterpart to build-linux.sh and make_release.ps1. Same state policy:
# rom.cfg, keybinds.ini, runtime_prefs.ini, saves and save states live in the
# folder the user sees the .app sitting in — NOT inside the bundle, which an
# app update replaces wholesale. The engine anchors there itself
# (gb_host_paths.c walks Foo.app/Contents/MacOS up to the bundle's container),
# so the launcher script below only has to set the working directory.
#
# Usage:
#   bash build-macos.sh                     # .app + .dmg for the host arch
#   bash build-macos.sh --version 0.1.0
#   bash build-macos.sh --arch universal    # x86_64 + arm64
#   bash build-macos.sh --no-regen          # reuse an existing generated/ tree
#   bash build-macos.sh --no-dmg            # .app only
#   bash build-macos.sh --zip               # also produce a .zip of the .app
#
# Prereqs (Homebrew):
#   brew install cmake ninja sdl2 dylibbundler create-dmg
# create-dmg is optional (hdiutil is the fallback); dylibbundler is what makes
# the bundle relocatable by copying libSDL2 in and rewriting its install name.
#
# Generation needs the supported cart at
#   roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb   CRC32 D5EC24E4
# It is never packaged.
set -euo pipefail

# ============================ PER-GAME CONFIG ===============================
APP_NAME="Super Mario Land 2"
BUNDLE_NAME="SuperMarioLand2"              # .app / .dmg file name (no spaces)
RELEASE_SLUG="SuperMarioLand2Recomp"
CMAKE_TARGET="Super_Mario_Land_2"
BUNDLE_ID="com.mstan.supermarioland2recomp"
ROM_EXTS="gb gbc"
ROM_NAME="Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb"
ROM_CRC32="d5ec24e4"
BOXART="recomp/launcher/boxart.tga"
EXTRA_PAYLOAD=( "recomp/patches/sml2dx_v181.bps" "recomp/patches/SML2DX_readme.txt" )
EXTRA_DOCS=( "README.md" "DX.md" "ADAPTIVE.md" )
TOML_FAITHFUL="super_mario_land_2.toml"
TOML_DX="super_mario_land_2_dx.toml"
# ============================================================================

DO_REGEN=1; DO_DMG=1; DO_ZIP=0
VERSION=""
ARCH="$(uname -m)"          # arm64 on Apple Silicon, x86_64 on Intel
REPO="$(cd "$(dirname "$0")" && pwd)"
OUT="$REPO/release-macos"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2;;
    --regen) DO_REGEN=1; shift;;
    --no-regen) DO_REGEN=0; shift;;
    --no-dmg) DO_DMG=0; shift;;
    --zip) DO_ZIP=1; shift;;
    --arch) ARCH="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --jobs) JOBS="$2"; shift 2;;
    -h|--help) sed -n '2,35p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "ERROR: run this on macOS." >&2; exit 1; }
case "$ARCH" in
  universal) OSX_ARCHS="x86_64;arm64";;
  arm64|x86_64) OSX_ARCHS="$ARCH";;
  *) echo "--arch must be arm64, x86_64, or universal" >&2; exit 2;;
esac

if [ -z "$VERSION" ] && [ -f "$REPO/VERSION" ]; then
  VERSION="$(head -n1 "$REPO/VERSION" | tr -d ' \t\r\n' | sed 's/^v//')"
fi
if [ -z "$VERSION" ]; then
  VERSION="$(git -C "$REPO" describe --tags --exact-match 2>/dev/null | sed 's/^v//' || true)"
  [ -n "$VERSION" ] || VERSION="dev"
fi

ENGINE="$REPO/gb-recompiled"
UI="$REPO/recomp-ui"
ENGINE_BUILD="$REPO/build-release-engine-macos"
BUILD="$REPO/build-macos-prod"

echo "==================== $APP_NAME (macOS $ARCH, v$VERSION) ===================="
cd "$REPO"

[ -f "$ENGINE/runtime/include/gb_body.h" ] || {
  echo "ERROR: gb-recompiled is not initialized; run 'git submodule update --init --recursive'." >&2
  exit 1
}
[ -f "$UI/recomp_ui.cmake" ] || {
  echo "ERROR: recomp-ui is not initialized; run 'git submodule update --init --recursive'." >&2
  exit 1
}

# ── generate ────────────────────────────────────────────────────────────────
if [ "$DO_REGEN" = "1" ]; then
  [ -f "$REPO/roms/$ROM_NAME" ] || {
    echo "ERROR: generation needs the supported cart at roms/$ROM_NAME (never packaged)" >&2
    exit 1
  }
  got="$(python3 -c "import sys,zlib;print('%08x'%(zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff))" "$REPO/roms/$ROM_NAME")"
  [ "$got" = "$ROM_CRC32" ] || {
    echo "ERROR: roms/$ROM_NAME has CRC32 $got; this project only supports $ROM_CRC32" >&2
    exit 1
  }
  echo "[0/5] ROM CRC32 $got OK"

  echo "[1/5] build gbrecomp"
  # The recompiler is a build-time tool: build it for the HOST arch only, never
  # as a universal binary (CMAKE_OSX_ARCHITECTURES is deliberately not passed).
  cmake -S "$ENGINE" -B "$ENGINE_BUILD" -DCMAKE_BUILD_TYPE=Release \
      -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON -DRECOMP_UI_ROOT="$UI"
  cmake --build "$ENGINE_BUILD" --target gbrecomp -j"$JOBS"
  GBRECOMP="$(find "$ENGINE_BUILD" -maxdepth 3 -type f -name gbrecomp | head -1)"
  [ -n "$GBRECOMP" ] || { echo "ERROR: gbrecomp not found under $ENGINE_BUILD" >&2; exit 1; }

  echo "[2/5] generate both bodies"
  "$GBRECOMP" --config "$TOML_FAITHFUL"
  "$GBRECOMP" --config "$TOML_DX"
else
  echo "[1/5] (--no-regen) reusing generated/"
  echo "[2/5] (--no-regen) reusing generated_dx/"
  [ -f "$REPO/generated/CMakeLists.txt" ] || {
    echo "ERROR: --no-regen but generated/CMakeLists.txt does not exist" >&2; exit 1; }
fi

# ── build ───────────────────────────────────────────────────────────────────
echo "[3/5] configure + build the game (-j$JOBS, $OSX_ARCHS)"
cmake -S "$REPO/generated" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES="$OSX_ARCHS" \
    -DGBRECOMP_RECOMP_UI=ON -DRECOMP_UI_ENABLE_MODS=ON \
    -DGBRECOMP_LAUNCHER_CONSOLE=gb -DRECOMP_UI_ROOT="$UI"
cmake --build "$BUILD" --target "$CMAKE_TARGET" -j"$JOBS"

BIN="$(find "$BUILD" -maxdepth 3 -type f -name "$CMAKE_TARGET" -perm +111 | head -1)"
[ -n "$BIN" ] || { echo "ERROR: no binary named '$CMAKE_TARGET' under $BUILD" >&2; exit 1; }
echo "      bin: $BIN"

# The launcher's CRC gate is compiled in from extras.c; refuse to package a
# binary generated from a different cart. Same check the other two scripts make.
python3 - "$BIN" "$ROM_CRC32" <<'PY'
import sys
blob = open(sys.argv[1], 'rb').read()
needle = int(sys.argv[2], 16).to_bytes(4, 'little')
at = blob.find(needle)
if at < 0:
    sys.exit("ERROR: executable does not carry the expected ROM CRC %s" % sys.argv[2])
print("      baked CRC %s found at offset 0x%X" % (sys.argv[2], at))
PY

# ── bundle ──────────────────────────────────────────────────────────────────
echo "[4/5] bundle $BUNDLE_NAME.app"
mkdir -p "$OUT"
APPBUNDLE="$OUT/$BUNDLE_NAME.app"
rm -rf "$APPBUNDLE"
mkdir -p "$APPBUNDLE/Contents/MacOS" "$APPBUNDLE/Contents/Resources" "$APPBUNDLE/Contents/Frameworks"

cp "$BIN" "$APPBUNDLE/Contents/MacOS/$CMAKE_TARGET"

# The recomp-ui pre-boot launcher loads fonts and images from assets/ next to
# the executable (SDL_GetBasePath resolves to Contents/MacOS inside a bundle).
[ -d "$(dirname "$BIN")/assets" ] || {
  echo "ERROR: recomp-ui launcher assets/ missing beside $BIN" >&2; exit 1; }
cp -R "$(dirname "$BIN")/assets" "$APPBUNDLE/Contents/MacOS/assets"

for rel in "${EXTRA_PAYLOAD[@]}"; do
  [ -f "$REPO/$rel" ] || { echo "ERROR: payload missing: $REPO/$rel" >&2; exit 1; }
  cp "$REPO/$rel" "$APPBUNDLE/Contents/MacOS/$(basename "$rel")"
done
for rel in "${EXTRA_DOCS[@]}"; do
  [ -f "$REPO/$rel" ] && cp "$REPO/$rel" "$APPBUNDLE/Contents/Resources/$(basename "$rel")"
done

# Icon: convert the box art if a converter is around. sips reads TGA; iconutil
# builds the .icns. Both ship with macOS, so this normally just works.
ICONSET="$OUT/$BUNDLE_NAME.iconset"
if [ -f "$REPO/$BOXART" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for sz in 16 32 64 128 256 512; do
    sips -s format png -z "$sz" "$sz" "$REPO/$BOXART" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1 || true
  done
  if iconutil -c icns "$ICONSET" -o "$APPBUNDLE/Contents/Resources/$BUNDLE_NAME.icns" 2>/dev/null; then
    ICON_KEY="  <key>CFBundleIconFile</key><string>$BUNDLE_NAME</string>"
  else
    echo "      (iconutil failed — bundling without an icon)"
    ICON_KEY=""
  fi
  rm -rf "$ICONSET"
else
  echo "      (no sips/iconutil or no $BOXART — bundling without an icon)"
  ICON_KEY=""
fi

# A small launcher script is the bundle's CFBundleExecutable. Finder starts a
# .app with an arbitrary working directory (often /), so this sets it to the
# folder holding the .app before exec'ing the real binary. gb_host_paths.c
# resolves state to that same folder on its own, but a sane CWD also keeps any
# relative path a mod or script uses pointing where the player expects.
cat > "$APPBUNDLE/Contents/MacOS/$BUNDLE_NAME" <<EOF
#!/bin/sh
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
# The folder the user actually sees the .app sitting in (writable, user-facing).
HOSTDIR="\$(cd "\$DIR/../../.." && pwd)"
export SDL_JOYSTICK_HIDAPI_STEAM=1
# The read-only payload inside the bundle: sml2dx_v181.bps lives here.
export GBRECOMP_ASSET_DIR="\$DIR"
cd "\$HOSTDIR" 2>/dev/null || true
ROM=""
for ext in $ROM_EXTS; do
    for f in "\$HOSTDIR"/*."\$ext"; do [ -e "\$f" ] && ROM="\$f" && break 2; done
done
# Seed rom.cfg from a ROM dropped beside the .app rather than passing it as
# argv[1]: a positional ROM skips the GUI launcher, which is the only route to
# the Mods page (DX colour, Adaptive widescreen). Never clobber a rom.cfg that
# already resolves.
if [ -n "\$ROM" ]; then
    cached=""
    [ -f "\$HOSTDIR/rom.cfg" ] && cached="\$(head -n1 "\$HOSTDIR/rom.cfg" 2>/dev/null | tr -d '\\r\\n')"
    if [ -z "\$cached" ] || [ ! -f "\$cached" ]; then
        [ -w "\$HOSTDIR" ] && printf '%s\\n' "\$ROM" > "\$HOSTDIR/rom.cfg" 2>/dev/null || true
    fi
fi
exec "\$DIR/$CMAKE_TARGET" "\$@"
EOF
chmod +x "$APPBUNDLE/Contents/MacOS/$BUNDLE_NAME"

cat > "$APPBUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$BUNDLE_NAME</string>
$ICON_KEY
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.games</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF

# Copy + relink SDL2 (and any other non-system dylib) into the bundle so it
# runs on a Mac without Homebrew.
if command -v dylibbundler >/dev/null 2>&1; then
  dylibbundler -od -b -x "$APPBUNDLE/Contents/MacOS/$CMAKE_TARGET" \
      -d "$APPBUNDLE/Contents/Frameworks" -p @executable_path/../Frameworks
else
  echo "      WARNING: dylibbundler not found — the .app will need Homebrew's SDL2."
fi

# Refuse to ship a ROM. Extension sweep plus a CRC sweep, the same gate the
# Windows and Linux scripts apply.
python3 - "$APPBUNDLE" "$ROM_CRC32" <<'PY'
import os, sys, zlib
root, crc = sys.argv[1], int(sys.argv[2], 16)
bad = []
for base, _dirs, files in os.walk(root):
    for name in files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            continue
        if os.path.splitext(name)[1].lower() in ('.gb', '.gbc', '.sgb', '.sfc', '.smc'):
            bad.append(path); continue
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
    sys.exit("ERROR: ROM content in the bundle:\n  " + "\n  ".join(bad))
print("      bundle carries no ROM")
PY

# Ad-hoc signature so Gatekeeper lets it run locally; a Developer ID is only
# needed for distribution without the quarantine dance.
codesign --force --deep --sign - "$APPBUNDLE" 2>/dev/null || \
  echo "      (codesign skipped — run 'xattr -dr com.apple.quarantine' if Gatekeeper blocks it)"
echo "      BUILT: $APPBUNDLE"

# ── distribute ──────────────────────────────────────────────────────────────
echo "[5/5] package"
if [ "$DO_DMG" = "1" ]; then
  DMG="$OUT/$RELEASE_SLUG-macos-$VERSION-$ARCH.dmg"
  rm -f "$DMG"
  if command -v create-dmg >/dev/null 2>&1; then
    create-dmg --volname "$APP_NAME" --app-drop-link 420 180 "$DMG" "$APPBUNDLE" || \
      hdiutil create -volname "$APP_NAME" -srcfolder "$APPBUNDLE" -ov -format UDZO "$DMG"
  else
    hdiutil create -volname "$APP_NAME" -srcfolder "$APPBUNDLE" -ov -format UDZO "$DMG"
  fi
  echo "      BUILT: $DMG"
  shasum -a 256 "$DMG"
fi
if [ "$DO_ZIP" = "1" ]; then
  ZIP="$OUT/$RELEASE_SLUG-macos-$VERSION-$ARCH.zip"
  rm -f "$ZIP"
  # ditto preserves the bundle's symlinks and the ad-hoc signature; `zip` does
  # not, and an unsigned-looking bundle is refused by Gatekeeper.
  ditto -c -k --sequesterRsrc --keepParent "$APPBUNDLE" "$ZIP"
  echo "      BUILT: $ZIP"
  shasum -a 256 "$ZIP"
fi
