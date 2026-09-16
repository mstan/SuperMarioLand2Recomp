/*
 * extras.c — Super Mario Land 2 game-specific hooks.
 *
 * The runtime (gbrt) provides a strong default for every game_* hook as its
 * own translation unit, so this partial override links cleanly.
 */
#include <stdint.h>
#include "game_extras.h"
#include "sml2_adaptive.h"
#include "sml2_mods.h"

/* Super Mario Land 2 is a DMG (original Game Boy) title. */
const char *game_get_platform(void) { return "gb"; }

const char *game_get_name(void) { return "Super Mario Land 2: 6 Golden Coins"; }

/* CRC32 of roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.2) [!].gb */
uint32_t game_get_expected_crc32(void) { return 0x635A9112u; }

/* The adaptive widescreen compositor installs itself here, and does nothing at
 * all unless the launcher's Mods page has it enabled. */
void game_on_init(struct GBContext *ctx) { sml2_adaptive_init(ctx); }

const struct RecompLauncherCModProvider *game_get_mods(const char *exe_dir) {
    return sml2_mod_provider(exe_dir);
}

int game_handle_debug_cmd(const char *cmd, int id, const char *json) {
    return sml2_adaptive_debug(cmd, id, json);
}
