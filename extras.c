/*
 * extras.c — Super Mario Land 2 game-specific hooks.
 *
 * The runtime (gbrt) provides a strong default for every game_* hook as its
 * own translation unit, so this partial override links cleanly.
 */
#include <stdint.h>
#include "game_extras.h"

/* Super Mario Land 2 is a DMG (original Game Boy) title. */
const char *game_get_platform(void) { return "gb"; }

const char *game_get_name(void) { return "Super Mario Land 2: 6 Golden Coins"; }

/* CRC32 of roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.2) [!].gb */
uint32_t game_get_expected_crc32(void) { return 0x635A9112u; }
