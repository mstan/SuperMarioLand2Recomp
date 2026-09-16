#pragma once

/* Runtime selections owned by the launcher Mods provider. One struct per
 * package so a second package (e.g. a future "DX color" mod) only has to add
 * its own fields and a row in the package table in sml2_mods.c. */
typedef struct {
    int widescreen;   /* adaptive-widescreen enabled */
    int width;        /* -1 = fit window, else fixed game-pixel width */
} SML2ModSettings;

struct RecompLauncherCModProvider;

const SML2ModSettings *sml2_mod_settings(void);
const struct RecompLauncherCModProvider *sml2_mod_provider(const char *exe_dir);
