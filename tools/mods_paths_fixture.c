#include "sml2_mods.h"
#include "sml2_adaptive.h"
#include "gb_host_paths.h"
#include "recomp_launcher.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Android returns NULL; desktop packages can have a nonwritable base path.
 * Neither may replace the private state/asset policy. No window is needed. */
char *SDL_GetBasePath(void) {
    if (getenv("SML2_TEST_NULL_BASE")) return NULL;
    const char *path = getenv("SML2_TEST_BASE_PATH");
    char *copy = malloc(strlen(path) + 1);
    strcpy(copy, path);
    return copy;
}
void SDL_free(void *value) { free(value); }
void gb_debug_server_send_line(const char *text) { (void)text; abort(); }
void gb_debug_server_send_fmt(const char *format, ...) { (void)format; abort(); }
const char *sml2_adaptive_margin_note(const char *body) {
    (void)body;
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 6) return 2;
    const int wide = atoi(argv[2]), width = atoi(argv[3]);
    const int dx = atoi(argv[4]), spawns = atoi(argv[5]);
    /* Branding asks for settings BEFORE the launcher attaches its provider. */
    const SML2ModSettings *settings = sml2_mod_settings();
    const RecompLauncherCModProvider *provider =
        sml2_mod_provider(gb_host_state_dir());
    if (!strcmp(argv[1], "read")) {
        if (settings->widescreen != wide || settings->width != width ||
            settings->dx != dx || settings->spawns != spawns) {
            fprintf(stderr, "Early settings ignored private persisted choices\n");
            return 1;
        }
        if (!sml2_dx_patch_available()) {
            fprintf(stderr, "DX payload was not found in the asset directory\n");
            return 1;
        }
        return 0;
    }
    provider->feature_enable(provider->ctx, "sml2-adaptive-widescreen",
                             "adaptive-widescreen", wide);
    provider->feature_enable(provider->ctx, "sml2-dx-color", "dx-color", dx);
    provider->feature_set_option(provider->ctx, "sml2-adaptive-widescreen",
                                "adaptive-widescreen", "aspect",
                                width == 512 ? "32:9" : "21:9");
    provider->feature_set_option(provider->ctx, "sml2-adaptive-widescreen",
                                "adaptive-widescreen", "spawns",
                                spawns ? "Extended" : "Original");
    const int ok = provider->commit(provider->ctx, NULL);
    if (!strcmp(argv[1], "fail")) return ok ? 1 : 0;
    if (!ok) fprintf(stderr, "%s\n", provider->last_error(provider->ctx));
    return ok ? 0 : 1;
}
