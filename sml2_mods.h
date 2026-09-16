#pragma once

/* Runtime selections owned by the launcher Mods provider. One struct per
 * package so a second package only has to add its own fields and a row in the
 * package table in sml2_mods.c. */
typedef struct {
    int widescreen;   /* adaptive-widescreen enabled */
    int width;        /* -1 = fit window, else fixed game-pixel width */
    int dx;           /* DX colour body selected (0 = faithful V1.0 body) */
} SML2ModSettings;

struct RecompLauncherCModProvider;

const SML2ModSettings *sml2_mod_settings(void);
const struct RecompLauncherCModProvider *sml2_mod_provider(const char *exe_dir);

/* GBBody::id of each recompiled body = its [rom] output_prefix in the matching
 * TOML. One definition, shared by extras.c (the body table), sml2_adaptive.c
 * (the per-body bindings table) and sml2_mods.c (the launcher status lines). */
#define SML2_BODY_FAITHFUL "Super_Mario_Land_2"
#define SML2_BODY_DX       "Super_Mario_Land_2_DX"

/* BPS that turns the user's V1.0 ROM into the SML2 DX v1.8.1 image, staged
 * next to the executable by game_build.cmake. Shared with extras.c so the
 * "is DX actually available" check and the body descriptor agree on one name. */
#define SML2_DX_PATCH_FILE "sml2dx_v181.bps"

/* 1 when the DX patch is present next to the executable, so the DX body can
 * actually derive its image at boot. 0 means the toggle must not be honoured. */
int sml2_dx_patch_available(void);
