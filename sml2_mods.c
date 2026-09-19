/* Built-in feature provider for the shared recomp-ui pre-boot launcher.
 *
 * Structured as a table of packages rather than a hard-coded index 0, so each
 * package is a row here plus its own option block -- no change to the provider
 * plumbing. Two packages today:
 *
 *   Adaptive widescreen  presentation only; works for either body.
 *   DX color             picks WHICH recompiled body boots. Off = the faithful
 *                        V1.0 build; on = Super Mario Land 2 DX v1.8.1, derived
 *                        from the same V1.0 ROM in memory at boot. See DX.md.
 */
#include "sml2_mods.h"
#include "sml2_adaptive.h"
#include "recomp_launcher.h"
#include "debug_server.h"
#include <SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#endif

#define COPY(field, text) snprintf(field, sizeof(field), "%s", text)
#define AUTHOR "Super Mario Land 2 Recomp contributors"
#define CONFIG_NAME "sml2-mods.ini"
#define ENV_WIDESCREEN "SML2_WIDESCREEN"
#define ENV_DX "SML2_DX"
#define ENV_SPAWNS "SML2_SPAWNS"

enum { PKG_WIDESCREEN = 0, PKG_DX, PKG_COUNT };

typedef struct {
    const char *package_id;
    const char *feature_id;
    const char *name;
    const char *group;
    const char *package_description;
    const char *feature_description;
    int option_count;
} Sml2Package;

static const Sml2Package packages[PKG_COUNT] = {
    [PKG_WIDESCREEN] = {
        "sml2-adaptive-widescreen", "adaptive-widescreen", "Adaptive widescreen",
        "Presentation",
        "Render Super Mario Land 2's world across the whole window instead of the "
        "Game Boy's 160x144 crop.",
        "Composes a wider view from the level's own block map, widens enemy "
        "activation to match, clamps to the level's scroll boxes and anchors the "
        "status bar to the window edges. The emulated hardware stays native.",
        2,
    },
    [PKG_DX] = {
        "sml2-dx-color", "dx-color", "DX color",
        "Presentation",
        "Run Super Mario Land 2 DX v1.8.1 by toruzz: the whole game in Game Boy "
        "Color, rebuilt from your own ROM at launch.",
        "Switches which recompiled build boots. Off, the game runs faithfully -- "
        "the original 1992 monochrome Game Boy release, untouched. On, the DX "
        "patch is applied to your ROM in memory at boot and the Game Boy Color "
        "build runs instead. Your ROM file is never modified, and each build "
        "keeps its own save.",
        0,
    },
};

static const char *const aspects[] = { "Fit", "16:9", "21:9", "32:9" };
static const int widths[] = { -1, 256, 336, 512 };
#define ASPECT_COUNT ((int)(sizeof aspects / sizeof *aspects))

/* Index is the SML2_SPAWNS_* value, so the stored integer, the choice list and
 * the environment override are one table. */
static const char *const spawn_modes[] = { "Original", "Extended" };
#define SPAWN_COUNT ((int)(sizeof spawn_modes / sizeof *spawn_modes))

static SML2ModSettings settings;
static int loaded;
static char config_path[1024], base_dir[1024], error[160];

static int valid_width(int width) { return width == -1 || (width >= 160 && width <= 4096); }

static void load(const char *base) {
    if (loaded) return;
    loaded = 1;
    settings = (SML2ModSettings){ 0, -1, 0, SML2_SPAWNS_EXTENDED };
    snprintf(base_dir, sizeof(base_dir), "%s%s", base ? base : "",
             base && base[0] && base[strlen(base) - 1] != '/' && base[strlen(base) - 1] != '\\'
                 ? "/" : "");
    snprintf(config_path, sizeof(config_path), "%s" CONFIG_NAME, base_dir);
    FILE *f = fopen(config_path, "r");
    if (f) {
        char line[256], key[64];
        int value;
        while (fgets(line, sizeof(line), f)) {
            if (sscanf(line, " %63[^= \t] = %d", key, &value) != 2) continue;
            if (!strcmp(key, "AdaptiveWidescreen") && (value == 0 || value == 1))
                settings.widescreen = value;
            if (!strcmp(key, "Width") && valid_width(value)) settings.width = value;
            if (!strcmp(key, "DX") && (value == 0 || value == 1)) settings.dx = value;
            if (!strcmp(key, "Spawns") && value >= 0 && value < SPAWN_COUNT)
                settings.spawns = value;
        }
        fclose(f);
    }
    /* An explicit CLI/test preset seeds the controls. The launcher checkbox stays
     * authoritative: load() runs once, so game init never overwrites a staged
     * selection the user made in the Mods page. */
    const char *aspect = getenv(ENV_WIDESCREEN);
    if (aspect && aspect[0]) {
        if (!strcmp(aspect, "off") || !strcmp(aspect, "Off")) {
            settings.widescreen = 0;
        } else {
            int width = 0;
            if (!strcmp(aspect, "fit") || !strcmp(aspect, "Fit")) width = -1;
            for (int i = 1; i < ASPECT_COUNT; i++)
                if (!strcmp(aspect, aspects[i])) width = widths[i];
            if (!width) width = atoi(aspect);
            if (valid_width(width)) {
                settings.width = width;
                settings.widescreen = 1;
            }
        }
    }
    const char *dx = getenv(ENV_DX);
    if (dx && dx[0]) settings.dx = atoi(dx) != 0;
    /* Accepts the choice label either way round -- SML2_SPAWNS=original and
     * SML2_SPAWNS=Original both select it. */
    const char *spawns = getenv(ENV_SPAWNS);
    if (spawns && spawns[0]) {
        for (int i = 0; i < SPAWN_COUNT; i++) {
            if (!strcmp(spawns, spawn_modes[i]) ||
                (spawns[0] == (char)(spawn_modes[i][0] | 0x20) &&
                 !strcmp(spawns + 1, spawn_modes[i] + 1)))
                settings.spawns = i;
        }
    }
}

/* The DX body derives its image from the user's ROM with this BPS at boot; with
 * the file missing there is nothing to boot, so the toggle must not be honoured
 * and the launcher has to say why. */
int sml2_dx_patch_available(void) {
    if (!loaded) (void)sml2_mod_settings();
    char path[1200];
    snprintf(path, sizeof(path), "%s" SML2_DX_PATCH_FILE, base_dir);
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    fclose(f);
    return 1;
}

const SML2ModSettings *sml2_mod_settings(void) {
    if (!loaded) {
        char *base = SDL_GetBasePath();
        load(base);
        SDL_free(base);
    }
    return &settings;
}

static int package_index(const char *package_id) {
    if (!package_id) return -1;
    for (int i = 0; i < PKG_COUNT; i++)
        if (!strcmp(package_id, packages[i].package_id)) return i;
    return -1;
}

static int feature_index(const char *package_id, const char *feature_id) {
    int i = package_index(package_id);
    if (i < 0 || !feature_id || strcmp(feature_id, packages[i].feature_id)) return -1;
    return i;
}

static int package_enabled(int i) {
    switch (i) {
        case PKG_WIDESCREEN: return settings.widescreen;
        case PKG_DX:         return settings.dx;
        default: return 0;
    }
}

static void package_set_enabled(int i, int enabled) {
    switch (i) {
        case PKG_WIDESCREEN: settings.widescreen = enabled != 0; break;
        case PKG_DX:         settings.dx = enabled != 0; break;
        default: break;
    }
}

static int count(void *ctx) { (void)ctx; return PKG_COUNT; }

static int package_get(void *ctx, int index, RecompLauncherCModPackage *out) {
    (void)ctx;
    if (!out || index < 0 || index >= PKG_COUNT) return 0;
    const Sml2Package *p = &packages[index];
    memset(out, 0, sizeof(*out));
    COPY(out->id, p->package_id);
    COPY(out->name, p->name);
    COPY(out->version, "1");
    COPY(out->author, AUTHOR);
    COPY(out->description, p->package_description);
    out->enabled = package_enabled(index);
    out->option_count = p->option_count;
    return 1;
}

static int feature_get(void *ctx, int index, RecompLauncherCModFeature *out) {
    (void)ctx;
    if (!out || index < 0 || index >= PKG_COUNT) return 0;
    const Sml2Package *p = &packages[index];
    memset(out, 0, sizeof(*out));
    COPY(out->id, p->feature_id);
    COPY(out->package_id, p->package_id);
    COPY(out->package_name, p->name);
    COPY(out->package_version, "1");
    COPY(out->name, p->name);
    COPY(out->group, p->group);
    COPY(out->author, AUTHOR);
    COPY(out->description, p->feature_description);
    if (index == PKG_WIDESCREEN) {
        /* The wide margins are composed per body; the DX body cannot take them
         * (see sml2_adaptive.c's bindings table). Say so here, against the body
         * the player has currently chosen, rather than silently rendering
         * native-width after Play. */
        const char *blocked = sml2_adaptive_margin_note(
            settings.dx ? SML2_BODY_DX : SML2_BODY_FAITHFUL);
        if (blocked) {
            COPY(out->status, blocked);
            out->has_error = 1;
        } else {
            COPY(out->status, package_enabled(index) ? "Enabled" : "Disabled");
        }
    } else if (index == PKG_DX) {
        /* The one place the launcher tells the player which build will boot,
         * and the only place a missing patch file can be reported before Play
         * (recomp-ui draws feature.status unconditionally in the detail pane,
         * in the warning colour when has_error is set). */
        if (!sml2_dx_patch_available()) {
            COPY(out->status, "Unavailable: " SML2_DX_PATCH_FILE " is missing next to the "
                              "executable. The faithful build will run.");
            out->has_error = 1;
        } else if (package_enabled(index)) {
            COPY(out->status, "On: boots Super Mario Land 2 DX v1.8.1, patched from your "
                              "ROM in memory. Separate save file.");
        } else {
            COPY(out->status, "Off: the game runs faithfully, as the original monochrome "
                              "Game Boy release.");
        }
    } else {
        COPY(out->status, package_enabled(index) ? "Enabled" : "Disabled");
    }
    out->enabled = package_enabled(index);
    out->option_count = p->option_count;
    return 1;
}

static int option_get(void *ctx, const char *package_id, const char *feature_id, int index,
                      RecompLauncherCModOption *out) {
    (void)ctx;
    int pkg = feature_index(package_id, feature_id);
    if (!out || pkg < 0 || index < 0 || index >= packages[pkg].option_count) return 0;
    memset(out, 0, sizeof(*out));
    out->step = 1;
    if (pkg == PKG_WIDESCREEN && index == 0) {
        out->type = RECOMP_MOD_OPTION_CHOICE;
        out->choice_count = ASPECT_COUNT;
        COPY(out->id, "aspect");
        COPY(out->label, "Aspect ratio");
        COPY(out->description, "Fit follows the window, including 32:9 and wider.");
        COPY(out->value, "Fit");
        COPY(out->default_value, "Fit");
        for (int i = 0; i < ASPECT_COUNT; i++)
            if (settings.width == widths[i]) COPY(out->value, aspects[i]);
        return 1;
    }
    /* Spawn timing is gameplay, not presentation, so the default has to be the
     * game's own: the widened view alone never moves an enemy's spawn point. */
    if (pkg == PKG_WIDESCREEN && index == 1) {
        int mode = settings.spawns >= 0 && settings.spawns < SPAWN_COUNT
                       ? settings.spawns : SML2_SPAWNS_EXTENDED;
        out->type = RECOMP_MOD_OPTION_CHOICE;
        out->choice_count = SPAWN_COUNT;
        COPY(out->id, "spawns");
        COPY(out->label, "Enemy spawns");
        /* The option covers both things that are measured against the ORIGINAL
         * screen edge rather than the visible one: where an enemy is spawned,
         * and how far Fire Mario's fireballs travel before the game destroys
         * them. Both change gameplay, both are off under Original, so they
         * belong behind the same switch. */
        COPY(out->description,
             "Original keeps the game's own timing, so enemies appear at the "
             "original screen edge inside the wide view and fireballs vanish "
             "there too. Extended moves both out to the edge of what you can "
             "actually see, which changes gameplay.");
        COPY(out->value, spawn_modes[mode]);
        COPY(out->default_value, spawn_modes[SML2_SPAWNS_EXTENDED]);
        return 1;
    }
    return 0;
}

static int choice_get(void *ctx, const char *package_id, const char *feature_id,
                      const char *option, int index, RecompLauncherCModChoice *out) {
    (void)ctx;
    int pkg = feature_index(package_id, feature_id);
    if (!out || pkg != PKG_WIDESCREEN || !option) return 0;
    if (!strcmp(option, "aspect")) {
        if (index < 0 || index >= ASPECT_COUNT) return 0;
        memset(out, 0, sizeof(*out));
        COPY(out->value, aspects[index]);
        COPY(out->label, index ? aspects[index] : "Fit to window");
        return 1;
    }
    if (!strcmp(option, "spawns")) {
        if (index < 0 || index >= SPAWN_COUNT) return 0;
        memset(out, 0, sizeof(*out));
        COPY(out->value, spawn_modes[index]);
        COPY(out->label, index == SML2_SPAWNS_ORIGINAL ? "Original (vanilla timing)"
                                                       : "Extended (to the view edge)");
        return 1;
    }
    return 0;
}

static int enable(void *ctx, const char *package_id, const char *feature_id, int enabled) {
    (void)ctx;
    int pkg = feature_index(package_id, feature_id);
    if (pkg < 0) return 0;
    package_set_enabled(pkg, enabled);
    return 1;
}

static int set_option(void *ctx, const char *package_id, const char *feature_id,
                      const char *option, const char *value) {
    (void)ctx;
    int pkg = feature_index(package_id, feature_id);
    if (pkg < 0 || !option || !value) return 0;
    if (pkg == PKG_WIDESCREEN && !strcmp(option, "aspect")) {
        for (int i = 0; i < ASPECT_COUNT; i++)
            if (!strcmp(value, aspects[i])) {
                settings.width = widths[i];
                return 1;
            }
    }
    if (pkg == PKG_WIDESCREEN && !strcmp(option, "spawns")) {
        for (int i = 0; i < SPAWN_COUNT; i++)
            if (!strcmp(value, spawn_modes[i])) {
                settings.spawns = i;
                return 1;
            }
    }
    return 0;
}

/* Package-oriented (legacy) surface: the launcher may use either. */
static int package_option_get(void *ctx, const char *package_id, int index,
                              RecompLauncherCModOption *out) {
    int pkg = package_index(package_id);
    if (pkg < 0) return 0;
    return option_get(ctx, package_id, packages[pkg].feature_id, index, out);
}

static int package_choice_get(void *ctx, const char *package_id, const char *option,
                              int index, RecompLauncherCModChoice *out) {
    int pkg = package_index(package_id);
    if (pkg < 0) return 0;
    return choice_get(ctx, package_id, packages[pkg].feature_id, option, index, out);
}

static int package_set_enabled_cb(void *ctx, const char *package_id, int enabled) {
    int pkg = package_index(package_id);
    if (pkg < 0) return 0;
    return enable(ctx, package_id, packages[pkg].feature_id, enabled);
}

static int package_set_option(void *ctx, const char *package_id, const char *option,
                              const char *value) {
    int pkg = package_index(package_id);
    if (pkg < 0) return 0;
    return set_option(ctx, package_id, packages[pkg].feature_id, option, value);
}

/* Called when the player presses Play, with the ROM the launcher resolved.
 * Returning 0 cancels the launch and surfaces last_error() on the Mods page. */
static int commit(void *ctx, const char *image) {
    (void)ctx;
    (void)image;
    error[0] = 0;
    if (settings.dx && !sml2_dx_patch_available()) {
        COPY(error, "DX color needs " SML2_DX_PATCH_FILE " next to the executable. "
                    "Turn DX color off to play the faithful build.");
        return 0;
    }
    char temporary[1050];
    snprintf(temporary, sizeof(temporary), "%s.tmp", config_path);
    FILE *f = fopen(temporary, "w");
    int ok = f && fprintf(f, "[Mods]\nAdaptiveWidescreen=%d\nWidth=%d\nDX=%d\nSpawns=%d\n",
                          settings.widescreen, settings.width, settings.dx,
                          settings.spawns) > 0;
    if (f && fclose(f) != 0) ok = 0;
    if (ok) {
#ifdef _WIN32
        ok = MoveFileExA(temporary, config_path,
                         MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
#else
        ok = rename(temporary, config_path) == 0;
#endif
    }
    if (!ok) COPY(error, "Unable to save " CONFIG_NAME);
    return ok;
}

static const char *last_error(void *ctx) { (void)ctx; return error; }

const RecompLauncherCModProvider *sml2_mod_provider(const char *exe_dir) {
    static RecompLauncherCModProvider provider;
    load(exe_dir);
    memset(&provider, 0, sizeof(provider));
    provider.package_count = count;
    provider.package_get = package_get;
    provider.option_get = package_option_get;
    provider.choice_get = package_choice_get;
    provider.set_enabled = package_set_enabled_cb;
    provider.set_option = package_set_option;
    provider.feature_count = count;
    provider.feature_get = feature_get;
    provider.feature_option_get = option_get;
    provider.feature_choice_get = choice_get;
    provider.feature_enable = enable;
    provider.feature_set_option = set_option;
    provider.commit = commit;
    provider.last_error = last_error;
    provider.archive_extension = ".gbmod";
    provider.archive_description = "Game Boy mod package (.gbmod)";
    return &provider;
}

/* ---- headless probe seam --------------------------------------------------
 *
 * The recomp-ui Mods page is only reachable through the pre-boot launcher, and
 * that launcher cannot run headlessly: it needs a GL context (so no
 * SDL_VIDEODRIVER=dummy) and recomp-ui raises its window unconditionally
 * (launcher_platform_sdl2.c, SDL_RaiseWindow), so any run of it takes the
 * user's foreground away. See gb-recompiled/docs/DEBUG_SERVER.md.
 *
 * These commands drive the SAME provider vtable the Mods page drives --
 * set_enabled / set_option / commit / last_error, resolved through
 * sml2_mod_provider() -- so a headless probe covers the provider surface and
 * the sml2-mods.ini round-trip without a window. What it does NOT cover is the
 * ImGui page itself (row layout, which control writes which option); that stays
 * the job of `probe_mods.py --headed`.
 */

/* Quote-safe copy for a JSON reply: the provider's own strings (status lines,
 * option labels) are free text and one stray quote would invalidate the line. */
static void mods_json_quote(const char *in, char *out, int cap) {
    int i = 0;
    for (const char *p = in ? in : ""; *p && i < cap - 2; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '"' || c == '\\') {
            if (i >= cap - 3) break;
            out[i++] = '\\';
            out[i++] = (char)c;
        } else if (c < 0x20) {
            out[i++] = ' ';
        } else {
            out[i++] = (char)c;
        }
    }
    out[i] = '\0';
}

/* One line describing every feature the Mods page would draw, with each
 * feature's live option values. This is the read side of the same vtable the
 * page reads. */
static void mods_reply_features(int id) {
    const RecompLauncherCModProvider *p = sml2_mod_provider(NULL);
    const int total = p->feature_count ? p->feature_count(p->ctx) : 0;
    char buf[8192];
    int n = snprintf(buf, sizeof(buf),
                     "{\"id\":%d,\"ok\":true,\"count\":%d,\"features\":[",
                     id, total);
    for (int i = 0; i < total; i++) {
        RecompLauncherCModFeature f;
        memset(&f, 0, sizeof(f));
        if (!p->feature_get || !p->feature_get(p->ctx, i, &f)) break;
        char name[256], group[192], status[512];
        mods_json_quote(f.name, name, sizeof(name));
        mods_json_quote(f.group, group, sizeof(group));
        mods_json_quote(f.status, status, sizeof(status));
        n += snprintf(buf + n, sizeof(buf) - (size_t)n,
                      "%s{\"package\":\"%s\",\"feature\":\"%s\",\"name\":\"%s\","
                      "\"group\":\"%s\",\"enabled\":%d,\"has_error\":%d,"
                      "\"status\":\"%s\",\"options\":[",
                      i ? "," : "", f.package_id, f.id, name, group,
                      f.enabled, f.has_error, status);
        for (int o = 0; o < f.option_count; o++) {
            RecompLauncherCModOption opt;
            memset(&opt, 0, sizeof(opt));
            if (!p->feature_option_get ||
                !p->feature_option_get(p->ctx, f.package_id, f.id, o, &opt)) break;
            char label[256], value[256];
            mods_json_quote(opt.label, label, sizeof(label));
            mods_json_quote(opt.value, value, sizeof(value));
            n += snprintf(buf + n, sizeof(buf) - (size_t)n,
                          "%s{\"id\":\"%s\",\"label\":\"%s\",\"value\":\"%s\"}",
                          o ? "," : "", opt.id, label, value);
        }
        n += snprintf(buf + n, sizeof(buf) - (size_t)n, "]}");
    }
    snprintf(buf + n, sizeof(buf) - (size_t)n, "]}");
    gb_debug_server_send_line(buf);
}

/* Copy a JSON string argument. The engine's parser is not reachable from here,
 * and the values involved are ids and short labels ("32:9"), never paths. */
static int mods_json_str(const char *json, const char *key, char *out, int cap) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = json ? strstr(json, pattern) : NULL;
    if (!p) return 0;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return 0;
    ++p;
    while (*p == ' ') ++p;
    if (*p != '"') return 0;
    ++p;
    int i = 0;
    while (*p && *p != '"' && i < cap - 1) out[i++] = *p++;
    out[i] = '\0';
    return 1;
}

static int mods_json_int(const char *json, const char *key, int fallback) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = json ? strstr(json, pattern) : NULL;
    if (!p) return fallback;
    p = strchr(p + strlen(pattern), ':');
    return p ? atoi(p + 1) : fallback;
}

int sml2_mods_debug(const char *cmd, int id, const char *json) {
    (void)sml2_mod_settings();          /* resolve base_dir / config_path once */
    const RecompLauncherCModProvider *p = sml2_mod_provider(NULL);

    if (!strcmp(cmd, "sml2_mod_features")) {
        mods_reply_features(id);
        return 1;
    }

    if (!strcmp(cmd, "sml2_mod_enable")) {
        char pkg[96], feat[96];
        if (!mods_json_str(json, "package", pkg, sizeof(pkg)) ||
            !mods_json_str(json, "feature", feat, sizeof(feat))) {
            gb_debug_server_send_fmt(
                "{\"id\":%d,\"ok\":false,\"error\":\"need package and feature\"}", id);
            return 1;
        }
        const int enabled = mods_json_int(json, "enabled", 1);
        const int ok = p->feature_enable &&
                       p->feature_enable(p->ctx, pkg, feat, enabled);
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":%s,\"package\":\"%s\",\"feature\":\"%s\","
            "\"enabled\":%d,\"error\":\"%s\"}",
            id, ok ? "true" : "false", pkg, feat, enabled,
            ok ? "" : (p->last_error ? p->last_error(p->ctx) : "rejected"));
        return 1;
    }

    if (!strcmp(cmd, "sml2_mod_option")) {
        char pkg[96], feat[96], opt[96], val[128];
        if (!mods_json_str(json, "package", pkg, sizeof(pkg)) ||
            !mods_json_str(json, "feature", feat, sizeof(feat)) ||
            !mods_json_str(json, "option", opt, sizeof(opt)) ||
            !mods_json_str(json, "value", val, sizeof(val))) {
            gb_debug_server_send_fmt(
                "{\"id\":%d,\"ok\":false,\"error\":"
                "\"need package, feature, option and value\"}", id);
            return 1;
        }
        const int ok = p->feature_set_option &&
                       p->feature_set_option(p->ctx, pkg, feat, opt, val);
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":%s,\"option\":\"%s\",\"value\":\"%s\","
            "\"error\":\"%s\"}",
            id, ok ? "true" : "false", opt, val,
            ok ? "" : (p->last_error ? p->last_error(p->ctx) : "rejected"));
        return 1;
    }

    if (!strcmp(cmd, "sml2_mod_commit")) {
        /* Exactly what PLAY does: resolve the staged selection, veto an
         * impossible one, and persist sml2-mods.ini. */
        const int ok = p->commit && p->commit(p->ctx, NULL);
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":true,\"committed\":%d,\"error\":\"%s\"}",
            id, ok, ok ? "" : (p->last_error ? p->last_error(p->ctx) : "rejected"));
        return 1;
    }

    return 0;
}
