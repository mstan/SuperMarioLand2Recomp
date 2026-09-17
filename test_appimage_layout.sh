#!/bin/sh
# test_appimage_layout.sh <AppDir> — prove the packaged layout cannot trap user
# state inside the AppImage.
#
# State policy under test (the same one the Windows zip has, where everything
# lives next to the exe): rom.cfg, keybinds.ini, runtime_prefs.ini, saves and
# save states live NEXT TO the .AppImage file. The engine anchors there because
# gb_host_paths.c prefers $APPIMAGE over /proc/self/exe. This script simulates
# the AppImage runtime against a READ-ONLY AppDir and asserts:
#
#   1. first launch seeds keybinds.ini beside the (simulated) .AppImage — in a
#      writable dir, not the payload;
#   2. a user edit to keybinds.ini survives a relaunch (an AppImage update
#      refreshes its own payload, never user state);
#   3. moving the .AppImage re-anchors state beside the new location;
#   4. a ROM dropped beside the .AppImage seeds rom.cfg, and a rom.cfg that
#      already resolves is not repointed;
#   5. nothing is ever written inside the read-only AppDir payload.
#
# The game is run headless with no usable ROM. That is deliberate and
# sufficient: keybinds.ini is written by gb_platform_init(), which runs before
# ROM resolution, so the seeding assertions hold while the process still exits
# early. Nonzero exits are expected and ignored.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /path/to/AppDir" >&2
    exit 2
fi

appdir=$(CDPATH= cd -- "$1" && pwd)
exe=$(basename "$(find "$appdir/usr/bin" -maxdepth 1 -type f -name 'Super_Mario_Land_2' | head -1)")
[ -n "$exe" ] || { echo "no Super_Mario_Land_2 ELF under $appdir/usr/bin" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'chmod -R u+w "$appdir" "$tmp" 2>/dev/null || true; rm -rf "$tmp"' EXIT HUP INT TERM

# Simulate the AppImage runtime: $APPIMAGE set to the image path, headless SDL,
# GUI launcher skipped, and a frame limit so a run that DOES find a ROM still
# terminates. The game exits nonzero without a valid ROM; that is fine —
# keybinds seeding happens first.
run_apprun() { # simulated_appimage_path
    sim=$1
    mkdir -p "$(dirname "$sim")"
    ( cd "$(dirname "$sim")" && \
      APPIMAGE=$sim \
      SDL_VIDEODRIVER=dummy SDL_VIDEO_DRIVER=dummy \
      SDL_AUDIODRIVER=dummy SDL_AUDIO_DRIVER=dummy \
      GBRECOMP_NO_LAUNCHER=1 \
      timeout 30 "$appdir/AppRun" --benchmark --limit-frames 120 >/dev/null 2>&1 ) || true
}

chmod -R a-w "$appdir"

state1=$tmp/state1
run_apprun "$state1/SuperMarioLand2.AppImage"

# 1. First launch seeded state beside the simulated .AppImage.
test -f "$state1/keybinds.ini" || {
    echo "FAIL: keybinds.ini not seeded beside the AppImage" >&2; exit 1; }

# 2. A user edit survives a relaunch.
printf '\n# user-owned marker\n' >> "$state1/keybinds.ini"
before=$(cat "$state1/keybinds.ini")
run_apprun "$state1/SuperMarioLand2.AppImage"
test "$(cat "$state1/keybinds.ini")" = "$before" || {
    echo "FAIL: user keybinds.ini edit clobbered by relaunch" >&2; exit 1; }

# 3. Moving the .AppImage re-anchors state beside the new location.
state2=$tmp/state2
run_apprun "$state2/moved.AppImage"
test -f "$state2/keybinds.ini" || {
    echo "FAIL: moved AppImage did not re-anchor its state" >&2; exit 1; }

# 4. A ROM dropped beside the .AppImage seeds rom.cfg.
state3=$tmp/state3
mkdir -p "$state3"
: > "$state3/pretend.gb"
run_apprun "$state3/SuperMarioLand2.AppImage"
test -f "$state3/rom.cfg" || {
    echo "FAIL: adjacent ROM did not seed rom.cfg" >&2; exit 1; }
test "$(head -n1 "$state3/rom.cfg")" = "$state3/pretend.gb" || {
    echo "FAIL: rom.cfg does not point at the adjacent ROM: $(cat "$state3/rom.cfg")" >&2
    exit 1; }
# AppRun must not REPOINT a cache that already resolves — the game owns rom.cfg
# and may rewrite it with the same resolved path, so assert the target rather
# than byte-identity.
: > "$state3/other.gb"
printf '%s\n' "$state3/other.gb" > "$state3/rom.cfg"
run_apprun "$state3/SuperMarioLand2.AppImage"
test "$(head -n1 "$state3/rom.cfg")" = "$state3/other.gb" || {
    echo "FAIL: AppRun repointed a rom.cfg that already resolved: \
$(head -n1 "$state3/rom.cfg")" >&2; exit 1; }

# 5. The read-only payload stayed pristine: no state files anywhere in AppDir.
for leak in rom.cfg keybinds.ini runtime_prefs.ini sml2-mods.ini imgui.ini saves; do
    found=$(find "$appdir" -name "$leak" | grep -v '^$' || true)
    [ -z "$found" ] || { echo "FAIL: state leaked into the payload: $found" >&2; exit 1; }
done
found=$(find "$appdir" \( -name '*.sav' -o -name '*.rtc' -o -name '*.state*' \) | grep -v '^$' || true)
[ -z "$found" ] || { echo "FAIL: save data leaked into the payload: $found" >&2; exit 1; }

echo "AppImage layout test passed: state anchors beside the .AppImage, user files survive relaunch, moved image re-anchors, payload stays read-only"
