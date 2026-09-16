/*
 * sml2_adaptive.c -- Super Mario Land 2 adaptive widescreen compositor.
 *
 * Host-only. The emulated PPU keeps its native 160x144 geometry and its
 * save-state layout; this module re-composes a wider frame from the game's own
 * world state through the gb_custom_view seam. When the mod is off no hook is
 * installed and output is byte-identical to the faithful build.
 *
 * The native 160 columns are copied verbatim from the PPU's own framebuffer, so
 * everything the hardware drew is preserved exactly; only the margins are
 * synthesised from the level's block map, and only actors the hardware refused
 * to draw are composited there. See ADAPTIVE.md for the verified ROM bindings.
 */
#include "sml2_adaptive.h"
#include "sml2_mods.h"
#include "sml2_map.h"
#include "gb_body.h"
#include "gb_custom_view.h"
#include "gbrt.h"
#include "ppu.h"
#include "debug_server.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SML2_MAX_SPRITES 1024
#define SML2_MAX_PIECES  64      /* per actor, guards a corrupt metasprite pointer */

/* ---- verified ROM bindings used by the hooks (see ADAPTIVE.md) ---- */
#define SML2_MODE            0xFF9Bu   /* hGameMode                                */
#define SML2_MODE_PLAY       0x04u
#define SML2_MODE_DEATH      0x09u
#define SML2_BONUS_ROOM      0xA28Bu   /* & 0xF0 -> bonus/minigame engine, no scroll */
#define SML2_TRANSITION      0xA20Eu   /* pipe/door transition in flight            */
#define SML2_GAMEPLAY_LCDC   0xE3u

/* bank 2 $4000 rebuilds these every frame from the camera (big-endian pairs). */
#define SML2_ACT_UPPER_HI    0xAF0Au   /* camX + 0x60 */
#define SML2_ACT_LOWER_LO    0xAF0Du
#define SML2_CULL_UPPER_HI   0xAF1Au   /* camX + 0xA0 */
#define SML2_CULL_LOWER_LO   0xAF1Du
#define SML2_ACT_HALF        0x60
#define SML2_CULL_HALF       0xA0
#define SML2_WINDOW_COPY_PC  0x3CAAu   /* ROM0 memcpy that feeds $AF02..$AF05 */

/* bank 3 actor draw routine */
#define SML2_ACTORS          0xAD00u
#define SML2_ACTOR_STRIDE    0x20u
#define SML2_ACTOR_SLOTS     16
#define SML2_DRAW_BANK       3   /* faithful V1.0; DX relocates -- see bindings below */
#define SML2_SPRITE_TABLE    0x40B1u
#define SML2_SPRITE_TABLE_ALT 0x4F11u
#define SML2_SPRITE_TABLE_SEL 0xAF06u
#define SML2_FRAME_HIDDEN    0x80u
#define SML2_TAP_ADDR        0xFFE2u   /* ldh a,[$FFE2] at 03:4019 */
#define SML2_TAP_PC          0x401Bu   /* generated code sets pc to the NEXT insn  */
#define SML2_SCX_SHADOW      0xA2B1u   /* read by 03:409B for the actor screen X   */
#define SML2_SCX_PC          0x409Eu

typedef struct {
    int x, y;            /* world pixels, top-left of the 8x8 piece */
    uint8_t tile, attr;
} Sml2Sprite;

static struct {
    GBContext *ctx;
    int frame, valid;
    int cam_x, cam_y;        /* world centre, as the ROM keeps it */
    int left, top;           /* world pixel of the native screen's top-left */
    int view_left, view_width;
    int extra_left, extra_right;
    int bound_left, bound_right;
    int score_hit, score_total;
    int mode;
    unsigned captures, capture_frames, fallbacks, rescues, ghosts;
    int count, build_count;
    uint8_t lcdc, scx, scy, wx, wy, bgp, obp0, obp1;
    uint8_t vram[VRAM_SIZE];
    uint8_t bg_pal[64], obj_pal[64];
    uint8_t oam[OAM_SIZE];
    uint8_t map[SML2_MAP_SIZE];
    uint8_t blockdef[SML2_BLOCKDEF_SIZE];
    uint8_t box[SML2_SCROLLBOX_LEN];
    Sml2Sprite sprite[SML2_MAX_SPRITES], build[SML2_MAX_SPRITES];
    uint8_t opaque[GB_CUSTOM_FRAME_SIZE];
} s;

/* ---- host inspection: never touches the emulated bus or advances time ---- */

static uint8_t peek(GBContext *ctx, unsigned a) {
    a &= 0xFFFFu;
    if (a < 0x4000u) return ctx->rom && a < ctx->rom_size ? ctx->rom[a] : 0;
    if (a < 0x8000u) {
        unsigned o = (unsigned)ctx->rom_bank * 0x4000u + (a - 0x4000u);
        return ctx->rom && o < ctx->rom_size ? ctx->rom[o] : 0;
    }
    if (a < 0xA000u) return ctx->vram ? ctx->vram[a - 0x8000u] : 0;
    if (a < 0xC000u) {
        unsigned o = (unsigned)ctx->ram_bank * 0x2000u + (a - 0xA000u);
        return ctx->eram && o < ctx->eram_size ? ctx->eram[o] : 0;
    }
    if (a < 0xD000u) return ctx->wram ? ctx->wram[a - 0xC000u] : 0;
    if (a < 0xE000u) return ctx->wram ? ctx->wram[(unsigned)ctx->wram_bank * 0x1000u + (a - 0xD000u)] : 0;
    if (a >= 0xFE00u && a < 0xFEA0u) return ctx->oam ? ctx->oam[a - 0xFE00u] : 0;
    if (a >= 0xFF00u && a < 0xFF80u) return ctx->io ? ctx->io[a - 0xFF00u] : 0;
    if (a >= 0xFF80u && a < 0xFFFFu) return ctx->hram ? ctx->hram[a - 0xFF80u] : 0;
    return 0;
}

static int peek16(GBContext *ctx, unsigned a) {
    return peek(ctx, a) | (peek(ctx, a + 1) << 8);
}

static void peek_block(GBContext *ctx, unsigned a, uint8_t *out, unsigned n) {
    for (unsigned i = 0; i < n; i++) out[i] = peek(ctx, a + i);
}

static uint8_t rom_byte(GBContext *ctx, int bank, unsigned addr) {
    unsigned o = (unsigned)bank * 0x4000u + (addr - 0x4000u);
    if (addr < 0x4000u || addr >= 0x8000u) return 0;
    return ctx->rom && o < ctx->rom_size ? ctx->rom[o] : 0;
}

/* ---- per-body ROM bindings -------------------------------------------------
 *
 * Every constant above was verified byte-for-byte on Super Mario Land 2 (UE)
 * V1.0 (CRC32 D5EC24E4) AND on the DX v1.8.1 image (F0799017) -- same address,
 * same bytes -- with two exceptions, both recorded here rather than in prose:
 *
 * 1. The actor draw routine. V1.0 selects bank 3 with a literal at 00:3C80
 *    (3E 03 / EA 4E A2 / EA 00 21 / CD 00 40). DX deletes that literal and
 *    calls a dispatcher at 00:07EA that returns bank 0x23, 0x28 or 0x3A from
 *    $A269, then calls $4000 in whichever bank it picked; the 32-byte draw
 *    entry signature occurs once in V1.0 (03:4000) and four times in DX
 *    (03:4000, 35:4000, 40:4000, 58:4000). The metasprite tables keep their
 *    addresses ($40B1 / $4F11) and all 168 entries still point inside
 *    $4000-$7FFF, so only the BANK moves. The fix is not a bigger constant, it
 *    is to stop using one: the tap only fires while the CPU is executing the
 *    draw routine, so the routine's bank is ctx->rom_bank at that instant, and
 *    the metasprite fetches follow the live bank. draw_banks[] below is only a
 *    sanity gate on which banks may host that routine at all.
 *
 * 2. Margin composition on DX -- gated off, and NOT because the geometry fails.
 *    Measured: with the draw-bank binding above fixed and the gate temporarily
 *    lifted, the DX body reaches gameplay (mode 4) at 32:9 and the block-map
 *    decode reproduces the game's own BG tilemap 378/378 cells, exactly as on
 *    the faithful body. Every geometric binding -- block map, block defs,
 *    scroll boxes, camera, activation/cull windows, the actor tap -- is correct
 *    on DX.
 *
 *    What is missing is COLOUR. The DX cart header says 0xC0 at 0x143: it is a
 *    CGB-only cart whose background cells carry an attribute byte (palette
 *    number, VRAM bank, flips, priority). This compositor snapshots VRAM bank 0
 *    only (memcpy of VRAM_SIZE = 0x2000) and draws every margin BG cell through
 *    BG palette 0 -- right on a DMG cart, wrong on a CGB one. The native 160
 *    columns would be in full colour and the synthesised margins beside them
 *    would not, which is worse than not widening at all.
 *
 *    Closing it is not a mechanical port: margins are synthesised from the
 *    LEVEL'S BLOCK MAP, not from the hardware BG map, so a margin cell has no
 *    attribute byte to read. Someone has to find where the hack stores per-block
 *    colour (it is not in the four-byte block defs at $A600, which are tile
 *    indices only) before bg_row() and the HUD path can honour it. Until then
 *    the Mods page says so rather than letting the player discover it. See DX.md.
 */
typedef struct {
    const char *body_id;        /* GBBody::id; NULL terminates the table       */
    const uint8_t *draw_banks;  /* banks that may host the actor draw routine  */
    int draw_bank_count;
    int margins_supported;      /* 0 -> compose nothing, stay at native width  */
    const char *margin_note;    /* shown on the Mods page when not supported   */
} Sml2Bindings;

static const uint8_t k_draw_banks_faithful[] = { 3 };
static const uint8_t k_draw_banks_dx[] = { 3, 35, 40, 58 };

static const Sml2Bindings k_bindings[] = {
    { SML2_BODY_FAITHFUL, k_draw_banks_faithful,
      (int)(sizeof k_draw_banks_faithful), 1, NULL },
    { SML2_BODY_DX, k_draw_banks_dx,
      (int)(sizeof k_draw_banks_dx), 0,
      "Unavailable while DX color is on: the wide margins are composed with the "
      "monochrome tile model, so they would not match the DX palettes." },
    { NULL, NULL, 0, 1, NULL },
};

static const Sml2Bindings *bindings_for(const char *body_id) {
    if (!body_id) body_id = gb_body_active_id();
    if (body_id) {
        for (const Sml2Bindings *b = k_bindings; b->body_id; b++) {
            if (!strcmp(b->body_id, body_id)) return b;
        }
    }
    return &k_bindings[0];
}

static const Sml2Bindings *bindings(void) { return bindings_for(NULL); }

const char *sml2_adaptive_margin_note(const char *body_id) {
    return bindings_for(body_id)->margin_note;
}

/* Can the actor draw routine legitimately be running out of this bank? */
static int is_draw_bank(unsigned bank) {
    const Sml2Bindings *b = bindings();
    for (int i = 0; i < b->draw_bank_count; i++)
        if (b->draw_banks[i] == bank) return 1;
    return 0;
}

/* ---- palettes: mirror ppu.c so the margins match the native strip ---- */

static const uint16_t sml2_dmg_rgb555[4] = { 0x67DC, 0x32EE, 0x2A66, 0x0841 };

static uint32_t rgb555(uint16_t c) {
    uint8_t r = (uint8_t)(((c >> 0) & 0x1F) * 255 / 31);
    uint8_t g = (uint8_t)(((c >> 5) & 0x1F) * 255 / 31);
    uint8_t b = (uint8_t)(((c >> 10) & 0x1F) * 255 / 31);
    return 0xFF000000u | ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
}

static int cgb_mode(const GBContext *ctx) {
    return ctx && ctx->config.model == GB_MODEL_CGB && !ctx->config.cgb_compatibility_mode;
}

static int cgb_compat(const GBContext *ctx) {
    return ctx && ctx->config.model == GB_MODEL_CGB && ctx->config.cgb_compatibility_mode;
}

static uint32_t shade_color(const GBContext *ctx, const uint8_t *palette_ram,
                            int palette_number, int raw, uint8_t dmg_reg) {
    if (cgb_mode(ctx)) {
        int i = palette_number * 8 + raw * 2;
        return rgb555((uint16_t)(palette_ram[i] | (palette_ram[i + 1] << 8)));
    }
    int shade = (dmg_reg >> (raw * 2)) & 3;
    if (cgb_compat(ctx)) {
        int i = palette_number * 8 + shade * 2;
        return rgb555((uint16_t)(palette_ram[i] | (palette_ram[i + 1] << 8)));
    }
    return rgb555(sml2_dmg_rgb555[shade]);
}

/* ---- block map decode (see sml2_map.h for the verified bindings) ---- */

static uint8_t block_at(int wx, int wy) {
    unsigned bx = (unsigned)(wx >> 4) & 0xFFu;
    unsigned by = (unsigned)(wy >> 4) & 0xFFu;
    unsigned off = by * SML2_MAP_STRIDE + bx;
    return off < SML2_MAP_SIZE ? s.map[off] : 0;
}

static uint8_t tile_at(int wx, int wy) {
    unsigned id = block_at(wx, wy);
    unsigned q = (unsigned)(((wy >> 3) & 1) * 2 + ((wx >> 3) & 1));
    unsigned off = id * 4u + q;
    return off < SML2_BLOCKDEF_SIZE ? s.blockdef[off] : 0xFF;
}

/* VRAM address of tile row `row` of `tile`, honouring LCDC bit 4. */
static unsigned tile_row_addr(uint8_t tile, int row) {
    unsigned base = (s.lcdc & LCDC_TILE_DATA) ? (unsigned)tile * 16u
                                              : 0x1000u + (unsigned)(int)(int8_t)tile * 16u;
    return (base + (unsigned)row * 2u) & 0x1FFEu;
}

/* ---- scene validation ----------------------------------------------------
 * Two independent gates. The cheap one is the game's own mode enum plus the
 * flags that distinguish a bonus room or a pipe transition from scrolling
 * gameplay. The real one proves the claim the margins rest on: decode the
 * visible tile grid out of the block map and require it to reproduce the BG
 * tilemap the hardware is actually showing. Anything that reuses level RAM
 * with different VRAM -- a pause menu, the world map, an intro card -- fails
 * it, so the compositor falls back to a centred native frame.
 */
static int validate_scene(GBContext *ctx) {
    s.score_hit = s.score_total = 0;
    s.mode = peek(ctx, SML2_MODE);
    if (s.mode != SML2_MODE_PLAY && s.mode != SML2_MODE_DEATH) return 0;
    if (peek(ctx, SML2_BONUS_ROOM) & 0xF0u) return 0;
    if (peek(ctx, SML2_TRANSITION)) return 0;
    if (s.lcdc != SML2_GAMEPLAY_LCDC) return 0;
    if (s.wy != SML2_HUD_WY || s.wx != SML2_HUD_WX) return 0;
    if (s.cam_x < SML2_CAM_CENTRE_X || s.cam_x >= SML2_MAP_COLS * 16) return 0;
    if (s.cam_y < SML2_CAM_CENTRE_Y || s.cam_y >= SML2_MAP_ROWS * 16) return 0;

    int hit = 0, total = 0;
    for (int ty = 0; ty < 18; ty++) {
        for (int tx = 0; tx < 21; tx++) {
            int wx = s.left + tx * 8, wy = s.top + ty * 8;
            if (wx < 0 || wy < 0) return 0;
            if (block_at(wx, wy) > SML2_MAX_BLOCK_ID) return 0;
            unsigned sx = (unsigned)(s.scx + tx * 8) & 0xFFu;
            unsigned sy = (unsigned)(s.scy + ty * 8) & 0xFFu;
            total++;
            if (s.vram[0x1800u + (sy >> 3) * 32u + (sx >> 3)] == tile_at(wx, wy)) hit++;
        }
    }
    s.score_hit = hit;
    s.score_total = total;
    return total > 0 && hit * 100 >= total * 95;
}

/* ---- horizontal extent ---------------------------------------------------
 * The ROM clamps the camera inside one 256px scroll box whenever that box's
 * edge bit is set (ROM0 $0B61/$0BE8). The widened view may therefore span the
 * run of boxes reachable without crossing a closed edge, checked from both
 * sides so a one-sided authoring mistake cannot open a wall.
 */
static void compute_bounds(void) {
    int col = (s.cam_x >> 8) & 0x0F;
    int row = (s.cam_y >> 8) & 0x03;
    int lo = col, hi = col;
    if (row <= 2) {
        while (lo > 0 && !(s.box[row * 16 + lo] & SML2_BOX_LEFT) &&
               !(s.box[row * 16 + lo - 1] & SML2_BOX_RIGHT)) lo--;
        while (hi < 15 && !(s.box[row * 16 + hi] & SML2_BOX_RIGHT) &&
               !(s.box[row * 16 + hi + 1] & SML2_BOX_LEFT)) hi++;
    }
    s.bound_left = lo * SML2_SCREEN_PX;
    s.bound_right = (hi + 1) * SML2_SCREEN_PX;
    if (s.bound_left < 0) s.bound_left = 0;
    if (s.bound_right > SML2_MAP_COLS * 16) s.bound_right = SML2_MAP_COLS * 16;
}

static int view_left_for(int left, int width) {
    int span = s.bound_right - s.bound_left;
    int v;
    if (span <= width) {
        v = s.bound_left - (width - span) / 2;
    } else {
        v = left - (width - GB_SCREEN_WIDTH) / 2;
        if (v < s.bound_left) v = s.bound_left;
        if (v > s.bound_right - width) v = s.bound_right - width;
    }
    /* The native strip must stay inside the composed view. */
    if (v > left) v = left;
    if (v < left + GB_SCREEN_WIDTH - width) v = left + GB_SCREEN_WIDTH - width;
    return v;
}

/* ---- actor capture -------------------------------------------------------
 * Tapped at 03:4019, the instant the ROM's own actor draw routine has already
 * applied every gate that is about to be honoured (state == 2, the $AF3E
 * redraw gate) and is about to apply the ones that must not be (the 0xB8
 * whole-actor drops and the 0xA0/0xA8 per-piece clips). The metasprite is
 * decoded exactly as 03:4042..03:408E does, but into world coordinates:
 * piece world = (actorX + Xoff, actorY + Yoff).
 */
static void capture_actor(GBContext *ctx) {
    if (peek(ctx, SML2_TAP_ADDR) == SML2_FRAME_HIDDEN) return;
    int ax = (peek(ctx, 0xFFD0u) << 8) | peek(ctx, 0xFFD1u);
    int ay = (peek(ctx, 0xFFD3u) << 8) | peek(ctx, 0xFFD4u);
    unsigned idx = peek(ctx, 0xFFDBu);
    uint8_t flip = (uint8_t)(peek(ctx, 0xFFDCu) ^ peek(ctx, 0xFFDDu) ^ peek(ctx, 0xFFDEu));
    unsigned table = peek(ctx, SML2_SPRITE_TABLE_SEL) ? SML2_SPRITE_TABLE_ALT : SML2_SPRITE_TABLE;
    unsigned entry = table + idx * 2u;
    /* The metasprite tables live in the SAME bank as the draw routine that is
     * executing right now -- bank 3 on the faithful body, one of 35/40/58 on
     * DX. read_tap() has already established we are inside that routine, so
     * ctx->rom_bank is that bank; a compile-time 3 would read the wrong bank's
     * table on DX. */
    int draw_bank = (int)ctx->rom_bank;
    unsigned de = (unsigned)rom_byte(ctx, draw_bank, entry) |
                  ((unsigned)rom_byte(ctx, draw_bank, entry + 1) << 8);
    if (de < 0x4000u || de >= 0x8000u) return;
    for (int n = 0; n < SML2_MAX_PIECES && de + 3 < 0x8000u; n++, de += 4) {
        uint8_t yraw = rom_byte(ctx, draw_bank, de);
        if (yraw == SML2_FRAME_HIDDEN) break;
        uint8_t xraw = rom_byte(ctx, draw_bank, de + 1);
        int yoff = (flip & 0x40u) ? (int8_t)(uint8_t)((~yraw & 0xFFu) - 7) : (int8_t)yraw;
        int xoff = (flip & 0x20u) ? (int8_t)(uint8_t)((~xraw & 0xFFu) - 7) : (int8_t)xraw;
        if (s.build_count >= SML2_MAX_SPRITES) break;
        Sml2Sprite *sp = &s.build[s.build_count++];
        sp->x = ax + xoff;
        sp->y = ay + yoff;
        sp->tile = rom_byte(ctx, draw_bank, de + 2);
        sp->attr = (uint8_t)(rom_byte(ctx, draw_bank, de + 3) ^ flip);
        s.captures++;
    }
}

static void read_tap(GBContext *ctx, uint16_t address) {
    if (address != SML2_TAP_ADDR || !is_draw_bank(ctx->rom_bank)) return;
    if (ctx->pc != SML2_TAP_PC && ctx->pc != SML2_TAP_PC - 2) return;
    if (!s.valid || gb_custom_width <= GB_SCREEN_WIDTH) return;
    capture_actor(ctx);
}

/* ---- read overrides ------------------------------------------------------
 * 1. Enemy activation ($AF0A..$AF0D) and horizontal culling ($AF1A..$AF1D) are
 *    rebuilt each frame by 02:4000 as camX +- 0x60 / +- 0xA0 and consumed
 *    through the ROM0 memcpy at $3CAA. Widening them by the view's own margins
 *    lets vanilla-spawned actors act and stay alive across the whole view. The
 *    spawn scanner's own window ($AF12..$AF15) is deliberately untouched, so
 *    spawn points and the list cursor stay exactly vanilla.
 * 2. The actor draw routine computes a screen X modulo 256 (03:409F). An actor
 *    that the widened activation kept alive far off the native screen would
 *    alias back into it as a ghost. Presenting 03:409B with a scroll shadow
 *    that puts such an actor at screen X 0xB8 makes the ROM's own 03:4025 test
 *    drop it -- which also keeps the 40-entry OAM buffer from overflowing --
 *    and the host draws it in the margin instead.
 */
static uint8_t read_override(GBContext *ctx, uint16_t address, uint8_t value) {
    if (!s.valid || gb_custom_width <= GB_SCREEN_WIDTH) return value;
    unsigned pc = ctx->pc;

    if (address >= SML2_ACT_UPPER_HI && address <= SML2_CULL_LOWER_LO &&
        ctx->rom_bank == 2 && (pc == SML2_WINDOW_COPY_PC || pc == SML2_WINDOW_COPY_PC + 1)) {
        int half, upper, lower;
        if (address >= SML2_ACT_UPPER_HI && address <= SML2_ACT_LOWER_LO) half = SML2_ACT_HALF;
        else if (address >= SML2_CULL_UPPER_HI && address <= SML2_CULL_LOWER_LO) half = SML2_CULL_HALF;
        else return value;
        upper = s.cam_x + half + s.extra_right;
        lower = s.cam_x - half - s.extra_left;
        if (lower < 0) lower = 0;
        if (upper > 0xFFFF) upper = 0xFFFF;
        s.rescues++;
        switch (address & 0x0Fu) {
            case 0x0A: return (uint8_t)(upper >> 8);
            case 0x0B: return (uint8_t)upper;
            case 0x0C: return (uint8_t)(lower >> 8);
            case 0x0D: return (uint8_t)lower;
            default: return value;
        }
    }

    if (address == SML2_SCX_SHADOW && is_draw_bank(ctx->rom_bank) &&
        (pc == SML2_SCX_PC || pc == SML2_SCX_PC - 3)) {
        int ax = (peek(ctx, 0xFFD0u) << 8) | peek(ctx, 0xFFD1u);
        int rel = ax - s.left;
        if (rel >= -8 && rel < 176) return value;
        s.ghosts++;
        return (uint8_t)((peek(ctx, 0xFFD1u) + 8 - 0xB8) & 0xFF);
    }
    return value;
}

/* ---- per-frame snapshot (PPU line 0) ---- */

static void snapshot(GBContext *ctx) {
    GBPPU *p = (GBPPU *)ctx->ppu;
    s.ctx = ctx;
    s.frame++;
    s.lcdc = p->lcdc; s.scx = p->scx; s.scy = p->scy;
    s.wx = p->wx; s.wy = p->wy;
    s.bgp = p->bgp; s.obp0 = p->obp0; s.obp1 = p->obp1;
    memcpy(s.vram, ctx->vram, VRAM_SIZE);
    memcpy(s.oam, ctx->oam, OAM_SIZE);
    memcpy(s.bg_pal, p->bg_palette_ram, 64);
    memcpy(s.obj_pal, p->obj_palette_ram, 64);
    peek_block(ctx, SML2_MAP_BASE, s.map, SML2_MAP_SIZE);
    peek_block(ctx, SML2_BLOCKDEF_BASE, s.blockdef, SML2_BLOCKDEF_SIZE);
    peek_block(ctx, SML2_SCROLLBOX, s.box, SML2_SCROLLBOX_LEN);

    s.cam_x = peek16(ctx, SML2_CAM_X);
    s.cam_y = peek16(ctx, SML2_CAM_Y);
    s.left = s.cam_x - SML2_CAM_CENTRE_X;
    s.top = s.cam_y - SML2_CAM_CENTRE_Y;
    /* Follow the scroll registers the hardware is actually displaying: screen
     * shake subtracts from SCY without moving the camera. */
    s.left += (int8_t)(uint8_t)(s.scx - (uint8_t)s.left);
    s.top += (int8_t)(uint8_t)(s.scy - (uint8_t)s.top);

    /* The sprite list built during the frame that is about to be shown. */
    s.count = s.build_count;
    if (s.count) {
        memcpy(s.sprite, s.build, (size_t)s.count * sizeof(Sml2Sprite));
        s.capture_frames++;
    }
    s.build_count = 0;

    s.valid = validate_scene(ctx);
    if (!s.valid) {
        s.bound_left = s.bound_right = 0;
        s.extra_left = s.extra_right = 0;
        s.count = 0;
        s.fallbacks++;
        return;
    }
    compute_bounds();
    s.view_width = gb_custom_width > GB_SCREEN_WIDTH ? gb_custom_width : GB_SCREEN_WIDTH;
    s.view_left = view_left_for(s.left, s.view_width);
    s.extra_left = s.left - s.view_left;
    s.extra_right = (s.view_left + s.view_width) - (s.left + GB_SCREEN_WIDTH);
    if (s.extra_left < 0) s.extra_left = 0;
    if (s.extra_right < 0) s.extra_right = 0;

    if (getenv("SML2_ADAPTIVE_TRACE") && s.frame % 120 == 0) {
        fprintf(stderr,
                "[ADAPTIVE] frame=%d valid=%d mode=%02X cam=%d,%d left=%d top=%d "
                "view=%d+%d bounds=%d..%d score=%d/%d sprites=%d captures=%u "
                "widened=%u dropped=%u fallbacks=%u\n",
                s.frame, s.valid, s.mode, s.cam_x, s.cam_y, s.left, s.top,
                s.view_left, s.view_width, s.bound_left, s.bound_right,
                s.score_hit, s.score_total, s.count, s.captures, s.rescues,
                s.ghosts, s.fallbacks);
    }
}

/* ---- sprite composition -------------------------------------------------- */

static void draw_sprite(uint32_t *out, int width, Sml2Sprite sp) {
    int h = (s.lcdc & LCDC_OBJ_SIZE) ? 16 : 8;
    uint8_t tile = sp.tile;
    if (!(s.lcdc & LCDC_OBJ_ENABLE)) return;
    if (h == 16) tile &= 0xFEu;
    for (int y = 0; y < h; y++) {
        int sy = sp.y - s.top + y;
        if (sy < 0 || sy >= GB_SCREEN_HEIGHT) continue;
        int py = (sp.attr & OAM_FLIP_Y) ? h - 1 - y : y;
        unsigned a = ((sp.attr & OAM_CGB_BANK) && cgb_mode(s.ctx) ? 0x2000u : 0u)
                   + ((unsigned)tile * 16u + (unsigned)py * 2u);
        a &= 0x1FFEu;
        uint8_t lo = s.vram[a], hi = s.vram[a + 1];
        for (int x = 0; x < 8; x++) {
            int world_x = sp.x + x;
            int sx = world_x - s.view_left;
            if (sx < 0 || sx >= width) continue;
            if (world_x < s.bound_left || world_x >= s.bound_right) continue;
            int px = (sp.attr & OAM_FLIP_X) ? x : 7 - x;
            int c = ((lo >> px) & 1) | (((hi >> px) & 1) << 1);
            if (!c) continue;
            int o = sy * width + sx;
            if ((sp.attr & OAM_PRIORITY) && (s.lcdc & LCDC_BG_ENABLE) && s.opaque[o]) continue;
            int pal_no = cgb_mode(s.ctx) ? (sp.attr & OAM_CGB_PALETTE)
                                         : ((sp.attr & OAM_PALETTE) ? 1 : 0);
            uint8_t reg = (sp.attr & OAM_PALETTE) ? s.obp1 : s.obp0;
            out[o] = shade_color(s.ctx, s.obj_pal, pal_no, c, reg);
        }
    }
}

/* ---- the compositor ---- */

static int render(GBContext *ctx, uint32_t *out, int width, const uint32_t *native) {
    if (!s.valid || !ctx->rom || width <= GB_SCREEN_WIDTH) return 0;
    if (width != s.view_width) {
        s.view_width = width;
        s.view_left = view_left_for(s.left, width);
    }

    /* 1. background, straight from the world block map */
    uint32_t bg[4];
    for (int i = 0; i < 4; i++) bg[i] = shade_color(ctx, s.bg_pal, 0, i, s.bgp);
    uint32_t black = rgb555(0);
    for (int y = 0; y < GB_SCREEN_HEIGHT; y++) {
        int wy = s.top + y;
        int row = wy & 7;
        int row_ok = wy >= 0 && wy < SML2_MAP_ROWS * 16;
        uint32_t *line = out + (size_t)y * width;
        uint8_t *op = s.opaque + (size_t)y * width;
        int cached_tx = -0x7FFFFFFF;
        uint8_t lo = 0, hi = 0;
        for (int x = 0; x < width; x++) {
            int wx = s.view_left + x;
            if (!row_ok || wx < s.bound_left || wx >= s.bound_right) {
                line[x] = black;
                op[x] = 0;
                continue;
            }
            int tx = wx >> 3;
            if (tx != cached_tx) {
                cached_tx = tx;
                unsigned a = tile_row_addr(tile_at(wx, wy), row);
                lo = s.vram[a];
                hi = s.vram[a + 1];
            }
            int bit = 7 - (wx & 7);
            int c = ((lo >> bit) & 1) | (((hi >> bit) & 1) << 1);
            line[x] = bg[c];
            op[x] = (uint8_t)c;
        }
    }

    /* 2. actors in world space, then anything the hardware did draw. Both are
     * overwritten inside the native strip in step 3, so they only matter in the
     * margins -- including the parts of an edge sprite the hardware clipped. */
    for (int i = s.count - 1; i >= 0; i--) draw_sprite(out, width, s.sprite[i]);
    for (int i = OAM_SIZE / 4 - 1; i >= 0; i--) {
        const uint8_t *e = s.oam + i * 4;
        if (!e[0] || e[0] >= 160) continue;
        Sml2Sprite sp = { s.left + e[1] - 8, s.top + e[0] - 16, e[2], e[3] };
        draw_sprite(out, width, sp);
    }

    /* 3. the native 160 columns, verbatim from the hardware framebuffer */
    int offset = s.left - s.view_left;
    if (offset < 0) offset = 0;
    if (offset > width - GB_SCREEN_WIDTH) offset = width - GB_SCREEN_WIDTH;
    for (int y = 0; y < GB_SCREEN_HEIGHT; y++)
        memcpy(out + (size_t)y * width + offset, native + (size_t)y * GB_SCREEN_WIDTH,
               GB_SCREEN_WIDTH * sizeof(uint32_t));

    /* 4. status bar: lives/coins/collectible hug the left edge, the timer hugs
     * the right edge, and the gap is padded with the bar's own blank tile. */
    if ((s.lcdc & LCDC_WINDOW_ENABLE) && s.wy < GB_SCREEN_HEIGHT && s.wx < 167) {
        unsigned map = (s.lcdc & LCDC_WINDOW_TILEMAP) ? 0x1C00u : 0x1800u;
        int origin = (int)s.wx - 7;
        uint8_t fill = s.vram[map + SML2_HUD_FILL_COL];
        int right_start = width - (SML2_HUD_COLS - SML2_HUD_SPLIT) * 8;
        for (int y = s.wy; y < GB_SCREEN_HEIGHT; y++) {
            int wl = y - s.wy;
            unsigned row_base = map + (unsigned)(wl >> 3) * 32u;
            uint32_t *line = out + (size_t)y * width;
            for (int x = origin > 0 ? origin : 0; x < width; x++) {
                int src = -1, bit;
                uint8_t t;
                int wxp = x - origin;
                if (wxp < SML2_HUD_SPLIT * 8) src = wxp;
                else if (x >= right_start) src = SML2_HUD_COLS * 8 - (width - x);
                if (src >= 0 && src < SML2_HUD_COLS * 8) {
                    t = s.vram[row_base + (unsigned)(src >> 3)];
                    bit = 7 - (src & 7);
                } else {
                    t = fill;
                    bit = 7 - (x & 7);
                }
                unsigned a = tile_row_addr(t, wl & 7);
                int c = ((s.vram[a] >> bit) & 1) | (((s.vram[a + 1] >> bit) & 1) << 1);
                line[x] = bg[c];
            }
        }
    }
    return 1;
}

static void reset(GBContext *ctx) {
    GBPPU *ppu = (GBPPU *)ctx->ppu;
    if (ppu->view_stride != GB_SCREEN_WIDTH) ppu_set_view_margins(ppu, 0, 0);
    s.valid = 0;
    s.count = s.build_count = 0;
    s.bound_left = s.bound_right = 0;
    s.extra_left = s.extra_right = 0;
}

/* ---- install ---- */

void sml2_adaptive_init(GBContext *ctx) {
    memset(&s, 0, sizeof s);
    s.ctx = ctx;
    gb_custom_render = NULL;
    gb_custom_snapshot = NULL;
    gb_custom_reset = NULL;
    gb_custom_read_tap = NULL;
    gb_custom_read_override = NULL;
    gbrt_imm_override_hook = NULL;
    gb_custom_requested_width = 0;
    gb_custom_width = GB_SCREEN_WIDTH;

    const SML2ModSettings *mods = sml2_mod_settings();
    if (!mods->widescreen) return;
    if (!bindings()->margins_supported) {
        /* The body that actually booted cannot be composed wide. Stay at the
         * native 160 rather than synthesise margins that would not match it;
         * the Mods page already said so (sml2_adaptive_margin_note). */
        fprintf(stderr, "[ADAPTIVE] %s\n", bindings()->margin_note);
        return;
    }
    gb_custom_requested_width = mods->width;
    gb_custom_render = render;
    gb_custom_snapshot = snapshot;
    gb_custom_reset = reset;
    gb_custom_read_tap = read_tap;
    gb_custom_read_override = read_override;
    fprintf(stderr, "[ADAPTIVE] Super Mario Land 2 compositor installed, width=%d\n",
            gb_custom_requested_width);
}

/* ---- debug commands (probe surface) ---- */

static int json_int(const char *json, const char *key, int fallback) {
    const char *p = json ? strstr(json, key) : NULL;
    if (!p) return fallback;
    p = strchr(p, ':');
    return p ? atoi(p + 1) : fallback;
}

int sml2_adaptive_debug(const char *cmd, int id, const char *json) {
    if (!strcmp(cmd, "sml2_mod_state")) {
        const SML2ModSettings *m = sml2_mod_settings();
        const char *body = gb_body_active_id();
        const char *note = sml2_adaptive_margin_note(NULL);
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"enabled\":%d,\"width\":%d,\"requested\":%d,"
            "\"dx\":%d,\"dx_available\":%d,\"body\":\"%s\",\"margins\":%d}",
            id, gb_custom_render != NULL, gb_custom_width,
            gb_custom_requested_width,
            m->dx, sml2_dx_patch_available(), body ? body : "", note == 0);
        return 1;
    }
    if (!strcmp(cmd, "sml2_width")) {
        int width = json_int(json, "\"width\"", 0);
        if (width == -1 || (width >= GB_SCREEN_WIDTH && width <= GB_CUSTOM_MAX_WIDTH))
            gb_custom_requested_width = width;
        gb_debug_server_send_fmt("{\"id\":%d,\"requested\":%d}", id, gb_custom_requested_width);
        return 1;
    }
    if (!strcmp(cmd, "sml2_buttons")) {
        unsigned mask = (unsigned)json_int(json, "\"buttons\"", 0);
        char script[80] = "c0:", *q = script + 3;
        const char keys[] = "RLUDABTS";
        for (int i = 0; i < 8; i++) if (mask & (1u << i)) *q++ = keys[i];
        if (!mask) *q++ = '-';
        strcpy(q, ":4294967295");
        gb_platform_set_input_script(script);
        gb_debug_server_send_fmt("{\"id\":%d,\"ok\":true}", id);
        return 1;
    }
    if (!strcmp(cmd, "sml2_save") || !strcmp(cmd, "sml2_load")) {
        int ok = !strcmp(cmd, "sml2_save")
                     ? gb_context_save_state_file(s.ctx, "logs/probe.state")
                     : gb_context_load_state_file(s.ctx, "logs/probe.state");
        gb_debug_server_send_fmt("{\"id\":%d,\"ok\":%s}", id, ok ? "true" : "false");
        return 1;
    }
    if (!strcmp(cmd, "sml2_capture")) {
        static uint32_t pixels[GB_CUSTOM_FRAME_SIZE];
        int width = gb_custom_width > GB_SCREEN_WIDTH ? gb_custom_width : GB_SCREEN_WIDTH;
        const uint32_t *native = ((GBPPU *)s.ctx->ppu)->rgb_framebuffer;
        if (!render(s.ctx, pixels, width, native)) {
            for (int i = 0; i < width * GB_SCREEN_HEIGHT; i++) pixels[i] = 0xFF000000u;
            for (int y = 0; y < GB_SCREEN_HEIGHT; y++)
                memcpy(pixels + (size_t)y * width + (width - GB_SCREEN_WIDTH) / 2,
                       native + (size_t)y * GB_SCREEN_WIDTH,
                       GB_SCREEN_WIDTH * sizeof(uint32_t));
        }
        FILE *f = fopen("logs/probe.ppm", "wb");
        if (f) {
            fprintf(f, "P6\n%d %d\n255\n", width, GB_SCREEN_HEIGHT);
            for (int i = 0; i < width * GB_SCREEN_HEIGHT; i++) {
                uint8_t rgb[3] = { (uint8_t)(pixels[i] >> 16), (uint8_t)(pixels[i] >> 8),
                                   (uint8_t)pixels[i] };
                fwrite(rgb, 1, 3, f);
            }
            fclose(f);
        }
        gb_debug_server_send_fmt("{\"id\":%d,\"ok\":%s}", id, f ? "true" : "false");
        return 1;
    }
    if (strcmp(cmd, "sml2_view")) return 0;
    gb_debug_server_send_fmt(
        "{\"id\":%d,\"valid\":%d,\"mode\":%d,\"width\":%d,\"left\":%d,\"top\":%d,"
        "\"view_left\":%d,\"camera_x\":%d,\"camera_y\":%d,\"bounds\":[%d,%d],"
        "\"score\":[%d,%d],\"sprites\":%d,\"captures\":%u,\"capture_frames\":%u,"
        "\"widened\":%u,\"dropped\":%u,\"fallbacks\":%u,\"frame\":%d}",
        id, s.valid, s.mode, gb_custom_width, s.left, s.top, s.view_left, s.cam_x,
        s.cam_y, s.bound_left, s.bound_right, s.score_hit, s.score_total, s.count,
        s.captures, s.capture_frames, s.rescues, s.ghosts, s.fallbacks, s.frame);
    return 1;
}
