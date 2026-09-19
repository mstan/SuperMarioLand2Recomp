/*
 * extras.c — Super Mario Land 2 game-specific hooks.
 *
 * The runtime (gbrt) provides a strong default for every game_* hook as its
 * own translation unit, so this partial override links cleanly.
 *
 * This executable carries TWO recompiled bodies of the same cart (see DX.md and
 * gb-recompiled/runtime/include/gb_body.h). Both are built from the one ROM the
 * player supplies — Super Mario Land 2 (UE) (V1.0), CRC32 D5EC24E4:
 *
 *   Super_Mario_Land_2       faithful: that ROM, recompiled as-is (DMG).
 *   Super_Mario_Land_2_DX    that ROM + sml2dx_v181.bps, applied in memory at
 *                            boot (CGB). Nothing is written to disk and the CRC
 *                            gate still only ever accepts D5EC24E4.
 *
 * The launcher's Mods page picks between them; game_select_body() below is
 * where that choice becomes the body that boots.
 */
#include <stdint.h>
#include "game_extras.h"
#include "gb_body.h"
#include "sml2_adaptive.h"
#include "sml2_mods.h"

#ifndef SML2_HAVE_DX_BODY
#define SML2_HAVE_DX_BODY 0
#endif

/* Emitted by the recompiler into each body's generated tree. game_build.cmake
 * defines SML2_HAVE_DX_BODY only when generated_dx/ exists, so a faithful-only
 * checkout (no DX regen yet) still links: the table is then one entry long and
 * every DX path below collapses to "off". */
extern const GBBody Super_Mario_Land_2_body;
#if SML2_HAVE_DX_BODY
extern const GBBody Super_Mario_Land_2_DX_body;
enum { BODY_FAITHFUL = 0, BODY_DX, BODY_COUNT };
#else
enum { BODY_FAITHFUL = 0, BODY_DX = -1, BODY_COUNT = 1 };
#endif

static const GBBody *const k_bodies[BODY_COUNT] = {
    [BODY_FAITHFUL] = &Super_Mario_Land_2_body,
#if SML2_HAVE_DX_BODY
    [BODY_DX]       = &Super_Mario_Land_2_DX_body,
#endif
};

const GBBody *const *game_get_bodies(int *out_count) {
    if (out_count) *out_count = BODY_COUNT;
    return k_bodies;
}

/* Called once, after the pre-boot launcher has run, before the context exists.
 * DX color on AND the patch actually present -> the DX body; anything else ->
 * the faithful body. commit() already refuses Play in the "on but missing"
 * case, so this is the belt to that braces (a skipped launcher, SML2_DX=1 in
 * the environment, a patch deleted between launcher and boot). */
int game_select_body(const GBBody *const *bodies, int count) {
    (void)bodies;
    (void)count;
#if SML2_HAVE_DX_BODY
    const SML2ModSettings *mods = sml2_mod_settings();
    if (mods->dx && sml2_dx_patch_available()) return BODY_DX;
#endif
    return BODY_FAITHFUL;
}

/* Which body the launcher branding and window title should describe. Once
 * gb_body_resolve() has latched a body this is exact; before that (the pre-boot
 * launcher runs first) it reflects the saved sml2-mods.ini / SML2_DX choice, so
 * a toggle made in the launcher re-brands on the NEXT launch. */
static int dx_body_selected(void) {
#if SML2_HAVE_DX_BODY
    const GBBody *active = gb_body_active();
    if (active) return active == &Super_Mario_Land_2_DX_body;
    const SML2ModSettings *mods = sml2_mod_settings();
    return mods->dx && sml2_dx_patch_available();
#else
    return 0;
#endif
}

/* The faithful body is a DMG title; the DX body is a Game Boy Color cart, and
 * the launcher themes itself from this string. */
const char *game_get_platform(void) { return dx_body_selected() ? "gbc" : "gb"; }

const char *game_get_name(void) {
    return dx_body_selected() ? "Super Mario Land 2 DX"
                              : "Super Mario Land 2: 6 Golden Coins";
}

/* The ONE ROM this build accepts, for either body:
 *   D5EC24E4  Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!]
 * The DX image (F0799017) is derived from it in memory and is never asked for,
 * so it is deliberately NOT in this list. */
uint32_t game_get_expected_crc32(void) { return 0xD5EC24E4u; }
const uint32_t *game_get_valid_crcs(int *out_count) {
    static const uint32_t crcs[] = { 0xD5EC24E4u };
    if (out_count) *out_count = 1;
    return crcs;
}

/* The adaptive widescreen compositor installs itself here, and does nothing at
 * all unless the launcher's Mods page has it enabled. */
void game_on_init(struct GBContext *ctx) { sml2_adaptive_init(ctx); }

const struct RecompLauncherCModProvider *game_get_mods(const char *exe_dir) {
    return sml2_mod_provider(exe_dir);
}

int game_handle_debug_cmd(const char *cmd, int id, const char *json) {
    /* The Mods provider seam first: it answers the sml2_mod_* commands the
     * headless probe drives, and falls through for everything else. */
    if (sml2_mods_debug(cmd, id, json)) return 1;
    return sml2_adaptive_debug(cmd, id, json);
}
