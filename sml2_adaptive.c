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
#define SML2_MODE_PAUSE      0x08u   /* Start: an overlay, not a new scene       */
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

/* ---- enemy spawn scanner (02:4C31 right / 02:4CEB left) -------------------
 *
 * Verified byte-for-byte at these addresses on BOTH images -- UE V1.0
 * (D5EC24E4) and SML2 DX v1.8.1 (F0799017) -- see ADAPTIVE.md:
 *
 *   02:4068  the builder compares the live camera against last frame's copy in
 *            $AF24/$AF25 and takes one of three branches;
 *   02:4085  scrolled right: $AF22 = +1, $AF00 = [$AF12], $AF01 = [$AF13] & $F8
 *   02:409A  did not scroll: $AF00 = $FF, and the scanner returns immediately
 *   02:40A4  scrolled left:  $AF22 = -1, $AF00 = [$AF14], $AF01 = [$AF15] & $F8
 *   02:417D  call 4C31, then latch the camera into $AF24/$AF25 for next frame
 *   02:4C31  dispatch on $AF22; 4C3C scans forward, 4CEB scans backward
 *   02:4C52  cp  the record's X high byte against $AF00
 *   02:4C56  hl += 6 and STORE it back to $AF1E/$AF1F -- the entry is consumed
 *   02:4C6E  cp  the record's X low byte against $AF01; below -> consume,
 *            above -> return, EQUAL -> spawn. Exact equality, nothing else.
 *
 * So the scan edge is (camX +- 112) with its low byte masked to 8 px, and any
 * list entry the edge steps over without landing on is eaten. Extended spawns
 * therefore overrides the builder's reads of $AF12..$AF15 -- one frame's scan
 * window -- and lets the ROM derive, align, compare and consume exactly as it
 * always does; the only requirement is that the edge we feed it never advances
 * more than 8 px between two frames the scanner actually runs. */
#define SML2_SPAWN_UPPER_HI  0xAF12u   /* camX + 112, big endian, right scan   */
#define SML2_SPAWN_UPPER_LO  0xAF13u
#define SML2_SPAWN_LOWER_HI  0xAF14u   /* camX - 112, clamped at 0, left scan  */
#define SML2_SPAWN_LOWER_LO  0xAF15u
#define SML2_SPAWN_HALF      112
#define SML2_SPAWN_R_HI_PC   0x408Au   /* ld a,[$AF12] in the right branch     */
#define SML2_SPAWN_R_LO_PC   0x4090u   /* ld a,[$AF13]                         */
#define SML2_SPAWN_L_HI_PC   0x40A9u   /* ld a,[$AF14] in the left branch      */
#define SML2_SPAWN_L_LO_PC   0x40AFu   /* ld a,[$AF15]                         */
#define SML2_SPAWN_RAMP      8         /* the scanner's own alignment quantum  */
#define SML2_SCAN_EDGE       0xAF00u   /* hi, then lo & $F8                    */
#define SML2_SCAN_NONE       0xFFu     /* edge HIGH byte only: do not scan     */
/* $AF22 is +1 (right) or $FF (left) on a frame that scans. On a frame that does
 * NOT -- 02:409A -- the branch stores the camera's own low byte there and marks
 * the frame by writing $FF into the edge's high byte alone, leaving the low
 * byte stale. The scanner's own test is cp $FF on $AF00 (02:4C3C / 02:4CEB), so
 * $AF22 must never be read as "did it scan". */
#define SML2_SCAN_DIR        0xAF22u
#define SML2_SPAWN_CURSOR    0xAF1Eu   /* big-endian pointer into the list     */
/* The spawn list is built in cart RAM at level load and the cursor is seeded to
 * $AB06 at 02:69AD, 02:6C76 and 03:6C4B (same three sites, same bytes, on both
 * images). $AB00 holds six $FF bytes so a backward scan stops there. */
#define SML2_SPAWN_LIST      0xAB00u
#define SML2_SPAWN_LIST_SIZE 0x200u
#define SML2_SPAWN_RECORD    6         /* X hi, X lo, flags, then 3 payload    */
/* +1: the last record that can start inside the region begins at offset 510,
 * which indexes 85, so the tag array needs 86 entries. */
#define SML2_SPAWN_RECORDS   (SML2_SPAWN_LIST_SIZE / SML2_SPAWN_RECORD + 1)
#define SML2_SPAWN_RING      64
#define SML2_SIDE_RIGHT      0
#define SML2_SIDE_LEFT       1

typedef struct {
    int x, y;            /* world pixels, top-left of the 8x8 piece */
    uint8_t tile, attr;
} Sml2Sprite;

/* Why a frame was refused the wide view. Every `return 0` in validate_scene()
 * names one of these, so "the view flickered" is always answerable with a
 * reason rather than a shrug. Order is the order the checks run in. */
enum {
    SML2_REJ_NONE = 0,
    SML2_REJ_MODE,        /* $FF9B is not scrolling gameplay / death        */
    SML2_REJ_BONUS,       /* $A28B & 0xF0: bonus or minigame engine         */
    SML2_REJ_TRANSITION,  /* $A20E: pipe/door room change in flight         */
    SML2_REJ_LCDC,        /* LCDC is not the gameplay $E3                   */
    SML2_REJ_WINDOW,      /* WY/WX are not the status bar's 136/7           */
    SML2_REJ_CAMERA,      /* camera outside the block map                   */
    SML2_REJ_NEGCOORD,    /* visible grid reaches negative world coords     */
    SML2_REJ_BLOCKID,     /* block id > $7F: level RAM is not a level       */
    SML2_REJ_TILE,        /* block-map decode vs hardware tilemap < 95%     */
    SML2_REJ_ATTR,        /* derived CGB attribute != hardware, bits 0-6    */
    SML2_REJ_NOTABLE,     /* CGB body with no DX attribute table            */
    SML2_REJ_RAMBANK,     /* cart SRAM is not the bank the level lives in   */
    SML2_REJ_COUNT
};

static const char *const k_reject_name[SML2_REJ_COUNT] = {
    "none", "mode", "bonus", "transition", "lcdc", "window", "camera",
    "negcoord", "blockid", "tile", "attr", "notable", "rambank"
};

/* One rejected frame, recorded as it happens. Always on, in every build: a
 * flicker is a handful of frames scattered through a ten-thousand-frame run,
 * and arming a trace after seeing it is exactly how you miss it. The probe
 * free-runs and then reads the ring backwards. */
/* Every wide <-> native transition, with what caused it. "Did it flicker" is
 * then a query on a ring that has been filling since boot, not a per-frame poll
 * over a TCP socket -- a probe can free-run ten thousand frames and read the
 * answer afterwards, and the count is exact rather than sampled. */
#define SML2_FLIP_LOG_CAP 64
typedef struct {
    unsigned frame;
    uint8_t to_wide, reason, mode, cgb;
    int16_t score_hit, score_total, attr_hit, attr_total;
    int16_t cam_x, cam_y;
    uint8_t tileset, fail_run;
} Sml2FlipEvent;

#define SML2_GATE_LOG_CAP 256
#define SML2_GATE_LOG_CELLS 4
typedef struct {
    unsigned frame;
    uint8_t reason, mode, lcdc, wy, wx, bonus, transition, cgb;
    uint8_t wram_bank, ram_bank, rom_bank, ly;
    int16_t cam_x, cam_y, left, top;
    int16_t score_hit, score_total, attr_hit, attr_total, prio_diff;
    uint8_t tileset, cells;
    struct {
        uint8_t tx, ty, block, tile, hw_tile, attr, hw_attr, flags;
    } cell[SML2_GATE_LOG_CELLS];
} Sml2GateEvent;

/* A single rejected frame must never be seen. The gate is a proof obligation,
 * not a presentation decision: it can go false for one frame because the guest
 * was midway through a VBlank update, or because a demo segment is changing
 * scene, and dropping to a pillarboxed 160 for that one frame is far worse to
 * look at than showing the previous frame's margins. So the gate result is
 * debounced -- N consecutive rejections before the view narrows, instant
 * return to wide on the first acceptance -- and during the debounce window the
 * margins are composed from the last frame that PASSED, while the native 160
 * columns keep coming from the live PPU as always.
 *
 * 6 frames is ~100 ms at 60 Hz: long enough to swallow every transient measured
 * over a 16k-frame attract run, short enough that a genuine scene change (the
 * world map, a pipe room, a demo segment boundary) narrows the view before the
 * player registers the wrong content. */
#define SML2_FALLBACK_DEBOUNCE 6

/* What a frozen frame carries: the WORLD -- geometry, the block map, the tile
 * bytes, the attribute table, the bounds. Deliberately NOT the palettes.
 *
 * DX dims the screen when the game is paused, by rewriting CGB BG palette RAM
 * through BCPD. The native 160 columns come from the live PPU and dim with it;
 * margins composed through a frozen palette snapshot do not, and the frame came
 * out as a dimmed strip between two bright margins -- measured at -38 mean
 * luminance over exactly columns 176..335 of a 512-wide frame, with both
 * margins bit-identical to the gameplay frame before it.
 *
 * A palette is a pure colour lookup over the tile data, so applying the LIVE
 * one to frozen tiles is both safe and the only self-consistent answer: the
 * whole width then dims, brightens or fades exactly as the hardware does to the
 * part of the world it is still showing. The same argument covers the DMG
 * registers, so BGP/OBP0/OBP1 stay live too and the faithful body behaves the
 * same way. LCDC stays frozen -- it selects which tile-data block an index
 * means, so a live one would reinterpret frozen indices. */
typedef struct {
    int cgb, attr_ok;
    int cam_x, cam_y, left, top, view_left, view_width;
    int extra_left, extra_right, bound_left, bound_right, count;
    uint8_t lcdc, scx, scy, wx, wy;
    uint8_t vram[VRAM_SIZE * 2];
    uint8_t oam[OAM_SIZE];
    uint8_t map[SML2_MAP_SIZE];
    uint8_t blockdef[SML2_BLOCKDEF_SIZE];
    uint8_t box[SML2_SCROLLBOX_LEN];
    uint8_t attr_tile[SML2_DX_ATTR_SIZE];
    Sml2Sprite sprite[SML2_MAX_SPRITES];
} Sml2Frame;

static Sml2Frame s_good;
static int s_good_valid;

static struct {
    GBContext *ctx;
    int frame, valid;
    int cam_x, cam_y;        /* world centre, as the ROM keeps it */
    int left, top;           /* world pixel of the native screen's top-left */
    int view_left, view_width;
    int extra_left, extra_right;
    int bound_left, bound_right;
    int score_hit, score_total, paint_hit;
    int attr_hit, attr_total, attr_prio_diff;
    int mode;
    unsigned captures, capture_frames, fallbacks, rescues, ghosts;
    /* Always-on gate accounting, so "how often does each gate reject a frame"
     * is a query rather than an experiment. gate_scene counts frames that
     * reached the two scoring gates at all. */
    unsigned gate_scene, gate_tile_fail, gate_attr_fail;
    unsigned gate_reason[SML2_REJ_COUNT];
    int wide;              /* presentation decision after debouncing        */
    int fail_run;          /* consecutive rejected frames                   */
    /* ---- overlay scenes -----------------------------------------------
     * Pause and a pipe/door transition do not replace the world, they draw
     * over it: the level RAM, the camera and the bounds are all still the
     * ones the last accepted frame was composed from, and the game is about
     * to hand them straight back. Narrowing for them is the wrong answer
     * twice over -- it pillarboxes on every Start press, and the pipe
     * transition simply outlasts the debounce (measured: ~24 frames against a
     * 6-frame window, so every pipe cost two transitions).
     *
     * While an overlay is in effect the view is HELD on the last proven-good
     * frame for as long as it lasts, with no expiry, and the debounce counter
     * is held at zero so a genuine failure afterwards still gets its full
     * window. The native 160 columns keep coming from the live PPU, so the
     * pause screen and the pipe animation are shown exactly as the hardware
     * draws them -- only the margins are frozen. */
    int overlay;           /* SML2_REJ_MODE (pause) / _TRANSITION, or 0     */
    /* Incremental scroll tracking: the previous frame's answer, so a stale
     * camera cannot put the decode on the wrong 256-pixel page. */
    int scroll_tracked, scroll_cam_x, scroll_cam_y, scroll_left, scroll_top;
    uint8_t scroll_scx, scroll_scy;
    unsigned scroll_offpage;   /* frames the camera would have got wrong     */
    int holding;           /* the previous frame was held                   */
    unsigned held;         /* frames held wide on an overlay                */
    unsigned held_runs;    /* distinct overlays held through                */
    unsigned debounced;    /* frames shown wide on last-good margins        */
    unsigned narrowed;     /* frames actually pillarboxed                   */
    /* Of those, the ones pillarboxed because the MODEL failed rather than
     * because the scene genuinely stopped being scrolling gameplay. A mode /
     * bonus / transition rejection is the gate doing its job; a tile / attr /
     * blockid rejection is this module being wrong. */
    unsigned narrowed_model;
    /* ...and the subset of those the viewer could actually SEE happen: the
     * previous frame was wide, so the view visibly snapped to pillarbox. This
     * is the flicker number, and it is the one that must be zero. A model
     * failure on a frame that was already narrow -- the first frame of a demo
     * segment, where the game has loaded the level but not yet filled VRAM --
     * changes nothing on screen. */
    unsigned pillarbox_model;
    /* Cells whose attribute BYTE differed from the hardware's but whose
     * painted pixels did not. Reported, never a rejection: the difference is
     * real and worth seeing, it just is not visible. */
    unsigned attr_byte_diff;
    unsigned gate_frames;          /* frames validate_scene() was called on  */
    unsigned flips;                /* wide <-> native transitions            */
    int reject;                    /* this frame's reason, 0 when accepted   */
    Sml2GateEvent gate_log[SML2_GATE_LOG_CAP];
    unsigned gate_log_seq;         /* total events ever recorded             */
    Sml2FlipEvent flip_log[SML2_FLIP_LOG_CAP];
    unsigned flip_log_seq;
    /* Which CGB OBJ palettes and which tile-data bank the margin sprites
     * actually used on the last composed frame. Margin sprites come from
     * captured metasprite pieces, whose attribute byte is the ROM's own, so
     * this is the evidence that they are NOT all falling through palette 0. */
    unsigned sprite_pal_mask, sprite_bank1;
    /* Per-cell outcome of the last scored frame, kept always -- 'the gate
     * failed' is useless without 'where'. 0 = both matched, 't' = tile
     * mismatch, 'a' = attribute mismatch, 'b' = both. Queried by
     * sml2_score_map; costs one byte and one store per cell. */
    uint8_t cell_miss[21 * 18];
    int count, build_count;
    uint8_t lcdc, scx, scy, wx, wy, bgp, obp0, obp1;
    /* Both VRAM banks: bank 0 at [0], bank 1 (CGB BG attribute map + the
     * second half of the tile data) at [VRAM_SIZE]. */
    uint8_t vram[VRAM_SIZE * 2];
    int cgb;                       /* body runs in true CGB mode             */
    int attr_ok;                   /* the DX tile->attribute table is live   */
    uint8_t attr_tile[SML2_DX_ATTR_SIZE];
    uint8_t bg_pal[64], obj_pal[64];
    uint8_t oam[OAM_SIZE];
    uint8_t map[SML2_MAP_SIZE];
    uint8_t blockdef[SML2_BLOCKDEF_SIZE];
    uint8_t box[SML2_SCROLLBOX_LEN];
    /* ---- enemy spawn policy ------------------------------------------------
     * spawn_extend is the user's choice (Original leaves every scanner input
     * vanilla and installs nothing). scan_edge[] is the edge actually handed to
     * the builder on the last frame the scanner ran in that direction, which is
     * what the <= 8 px ramp is measured against; -1 means "engage from vanilla".
     *
     * The accounting below is ALWAYS ON whenever the compositor is installed --
     * in BOTH policies, not armed by a probe -- because "did we eat an entry"
     * is only answerable by watching every frame: the scanner consumes
     * silently, and by the time anyone asks, the entry is gone. Each frame it
     * re-reads the cursor and the edge the ROM just used and classifies every
     * record the cursor stepped over as spawned (its X equalled the edge) or
     * jumped (it did not). With the mod OFF nothing is installed at all, by
     * design -- that build is the faithful one -- so tools/probe_spawns.py
     * derives the same ledger from the same RAM for its reference run, and
     * cross-checks this one against it. */
    int spawn_extend;
    int scan_edge[2], scan_pick[2], scan_pick_frame[2];
    int scan_reach_max[2], scan_lag_max[2];
    unsigned scan_hi[2], scan_lo[2], scan_unpaired, scan_resets;
    unsigned spawn_ungated;   /* builder reads handed back vanilla: !s.valid */
    int spawn_cursor, spawn_level;
    /* The aligned edge the guest's scanner actually used on the last frame it
     * scanned in each direction. Pure observation of the ROM, independent of
     * the policy and of the gate, and the only way to tell the two kinds of
     * "consumed without spawning" apart. */
    int scan_seen_edge[2];
    unsigned spawn_passed, spawn_spawned, spawn_jumped, spawn_scans;
    /* Of the jumped ones: a SEEK is the cursor catching up to a camera it was
     * far behind -- every level load does it, in vanilla too, and nothing is
     * lost that the level was ever going to give. A STEPPED-OVER entry lay
     * between the edge's previous position and this one: the edge crossed it
     * without landing on it, and it is gone. That second number is the one the
     * 8 px ramp exists to keep at zero. */
    unsigned spawn_seek, spawn_stepped;
    uint8_t spawn_list[SML2_SPAWN_LIST_SIZE];
    uint8_t spawn_seen[SML2_SPAWN_RECORDS];   /* 1 spawned, 2 jumped over */
    struct { int frame, cam_x, edge, prev_edge, x, addr, stepped; }
        spawn_ring[SML2_SPAWN_RING];
    unsigned spawn_ring_n;
    int cam_prev, cam_prev_ok;
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

/* $D000-$DFFF out of an EXPLICIT WRAM bank. The DX attribute table lives in
 * bank 2 while the guest is running with SVBK 0/1, so peek() -- which follows
 * ctx->wram_bank -- would read the level's block map at the same address. */
static uint8_t peek_wram_bank(GBContext *ctx, unsigned bank, unsigned a) {
    if (!ctx->wram || a < 0xD000u || a >= 0xE000u) return 0;
    if (bank == 0) bank = 1;                       /* SVBK 0 aliases bank 1 */
    return ctx->wram[(bank & 7u) * 0x1000u + (a - 0xD000u)];
}

/* $A000-$BFFF out of an EXPLICIT cart RAM bank, for the same reason
 * peek_wram_bank exists for WRAM. The compositor can afford to follow the live
 * bank because validate_scene() refuses any frame where it is not the level's
 * (SML2_REJ_RAMBANK); the spawn ledger cannot, because it runs on EVERY frame,
 * including the ones the gate refuses, and a misread there invents entries that
 * were never consumed. */
static uint8_t peek_eram_bank(GBContext *ctx, unsigned bank, unsigned a) {
    if (!ctx->eram || a < 0xA000u || a >= 0xC000u) return 0;
    unsigned o = bank * 0x2000u + (a - 0xA000u);
    return ctx->eram && o < ctx->eram_size ? ctx->eram[o] : 0;
}

/* Every read of the game's LEVEL state goes through this, never through the
 * live SVBK. See SML2_LEVEL_WRAM_BANK in sml2_map.h: the DX attribute drain
 * leaves SVBK on 2 across the host's snapshot point often enough to turn block
 * rows 32..47 into noise, which is what made the wide view flicker on the DX
 * body and only there. */
static uint8_t peek_level(GBContext *ctx, unsigned a) {
    if (a >= 0xD000u && a < 0xE000u)
        return peek_wram_bank(ctx, SML2_LEVEL_WRAM_BANK, a);
    return peek(ctx, a);
}

static void peek_level_block(GBContext *ctx, unsigned a, uint8_t *out, unsigned n) {
    for (unsigned i = 0; i < n; i++) out[i] = peek_level(ctx, a + i);
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
 * 2. Margin COLOUR on DX. The geometry was never the problem -- the block-map
 *    decode reproduces the game's own BG tilemap on the DX body exactly as on
 *    the faithful one -- the tile-score histogram over the probe route is
 *    identical on the two bodies. What was missing was the CGB attribute
 *    byte: margins are synthesised from the LEVEL'S BLOCK MAP, not from the
 *    hardware BG map, so a margin cell has no attribute to read out of VRAM.
 *
 *    It is not stored per block. The hack keeps a flat 256-entry
 *    tile-index -> attribute table in WRAM bank 2 at $D000, reloaded per
 *    tileset from ROM bank $21 ($4000 + [$A269]*$100) by 21:730E / 21:732C,
 *    and its extended VRAM-queue drain at 24:79B5 -- reached from the 15 bytes
 *    it patched into ROM0 $0AFB -- writes attribute = table[tile] into VRAM
 *    bank 1 for every tilemap cell it writes to bank 0. So the host derives a
 *    margin cell's attribute from exactly the same two inputs the hack uses:
 *    the tile index out of the block definitions, and that live table. See
 *    sml2_map.h for the disassembly and DX.md for the write-up.
 *
 *    That claim is re-proved every frame: validate_scene() compares the
 *    derived attribute against the hardware attribute map in VRAM bank 1 over
 *    the scored 21x17 grid and requires every cell to match, or the frame
 *    falls back to a centred native image. A body that is not in CGB mode
 *    scores 0/0 and the check is vacuous, which is what the faithful body
 *    wants.
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
      (int)(sizeof k_draw_banks_dx), 1, NULL },
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

/* The DX BG attribute for a raw tile index: a straight lookup in the live copy
 * of the hack's per-tileset table (see sml2_map.h). Zero on the faithful body,
 * which is exactly the DMG attribute.
 *
 * Bits 0-6 -- palette, tile VRAM bank, both flips -- are exactly what the hack
 * writes, for every cell, proved every frame by the gate in validate_scene().
 *
 * Bit 7 (BG-over-OBJ priority) is NOT a function of the block map, and no
 * amount of table reading will make it one. The hack patched each of the ROM's
 * direct tilemap writers with an attribute counterpart, and one of them --
 * 01:5B40, the "Mario used this block" writer that stamps tiles $F8-$FB and
 * sets block id 7 -- jumps to 01:4100, which writes `[$D0F8] | $80`:
 *
 *     01:411A  FA F8 D0   ld a,[$D0F8]
 *     01:411D  F6 80      or $80
 *     01:411F  22 77 19 22 77   the 2x2 attribute quad
 *
 * The SAME block id, scrolled in from the authored level data through the
 * ordinary queue drain, gets the table value with bit 7 clear. Measured on the
 * DX body over the probe route: tiles $F8-$FB appear in VRAM bank 1 with
 * attribute $87 (174 samples) AND with $07 (166 samples), same tile, same
 * block id. The difference is which routine last wrote the cell -- history the
 * host cannot read out of the level.
 *
 * Bit 7 changes no colour, only whether an OBJ pixel is hidden behind a
 * non-zero BG pixel. So it is derived from the table (the value the queue
 * drain -- the writer that redraws everything on the next scroll -- would
 * produce), the gate is on the seven bits that decide colour, and bit-7-only
 * divergences are counted and reported as attr_prio_diff rather than passed
 * over in silence. */
static uint8_t attr_for_tile(uint8_t tile) {
    return s.attr_ok ? s.attr_tile[tile] : 0u;
}

/* VRAM offset of tile row `row` of `tile`, honouring LCDC bit 4 and the CGB
 * attribute's tile-data bank (bit 3). The bank is added AFTER the 0x1FFE wrap
 * so it cannot be masked away. */
static unsigned tile_row_addr(uint8_t tile, int row, uint8_t attr) {
    unsigned base = (s.lcdc & LCDC_TILE_DATA) ? (unsigned)tile * 16u
                                              : 0x1000u + (unsigned)(int)(int8_t)tile * 16u;
    unsigned off = (base + (unsigned)row * 2u) & 0x1FFEu;
    if (s.cgb && (attr & SML2_ATTR_BANK)) off += VRAM_SIZE;
    return off;
}

/* Record one rejection into the ring. Called on every `return 0` path. */
static void gate_reject(GBContext *ctx, int reason) {
    s.reject = reason;
    if (reason > 0 && reason < SML2_REJ_COUNT) s.gate_reason[reason]++;
    Sml2GateEvent *e = &s.gate_log[s.gate_log_seq % SML2_GATE_LOG_CAP];
    memset(e, 0, sizeof *e);
    e->frame = (unsigned)s.frame;
    e->reason = (uint8_t)reason;
    e->mode = (uint8_t)s.mode;
    e->lcdc = s.lcdc;
    e->wy = s.wy;
    e->wx = s.wx;
    e->bonus = peek(ctx, SML2_BONUS_ROOM);
    e->transition = peek(ctx, SML2_TRANSITION);
    e->cgb = (uint8_t)s.cgb;
    /* The banks that were selected when snapshot() read the level. The block
     * map spans $B000-$BFFF (cart SRAM, ram_bank) and $C000-$DFFF (WRAM, the
     * $D000 half following SVBK), so a bank that is not the one the level
     * lives in turns the decode into noise. */
    e->wram_bank = (uint8_t)ctx->wram_bank;
    e->ram_bank = (uint8_t)ctx->ram_bank;
    e->rom_bank = (uint8_t)ctx->rom_bank;
    e->ly = ((GBPPU *)ctx->ppu)->ly;
    e->cam_x = (int16_t)s.cam_x;
    e->cam_y = (int16_t)s.cam_y;
    e->left = (int16_t)s.left;
    e->top = (int16_t)s.top;
    e->score_hit = (int16_t)s.score_hit;
    e->score_total = (int16_t)s.score_total;
    e->attr_hit = (int16_t)s.attr_hit;
    e->attr_total = (int16_t)s.attr_total;
    e->prio_diff = (int16_t)s.attr_prio_diff;
    e->tileset = peek(ctx, SML2_DX_TILESET);
    /* ...and the first few offending cells in full, so the reason comes with
     * the evidence: which tile the block map decoded, which the hardware is
     * showing, and both attribute bytes. */
    if (reason == SML2_REJ_TILE || reason == SML2_REJ_ATTR) {
        int rows = (s.wy + 7) / 8;
        if (rows > 18) rows = 18;
        for (int ty = 0; ty < rows && e->cells < SML2_GATE_LOG_CELLS; ty++) {
            for (int tx = 0; tx < 21 && e->cells < SML2_GATE_LOG_CELLS; tx++) {
                uint8_t m = s.cell_miss[ty * 21 + tx];
                if (!m) continue;
                int wx = s.left + tx * 8, wy = s.top + ty * 8;
                unsigned sx = (unsigned)(s.scx + tx * 8) & 0xFFu;
                unsigned sy = (unsigned)(s.scy + ty * 8) & 0xFFu;
                unsigned cell = 0x1800u + (sy >> 3) * 32u + (sx >> 3);
                uint8_t tile = tile_at(wx, wy);
                int i = e->cells++;
                e->cell[i].tx = (uint8_t)tx;
                e->cell[i].ty = (uint8_t)ty;
                e->cell[i].block = block_at(wx, wy);
                e->cell[i].tile = tile;
                e->cell[i].hw_tile = s.vram[cell];
                e->cell[i].attr = attr_for_tile(tile);
                e->cell[i].hw_attr = s.vram[VRAM_SIZE + cell];
                e->cell[i].flags = m;
            }
        }
    }
    s.gate_log_seq++;
}

/* Would the derived attribute paint this cell EXACTLY as the hardware's
 * attribute does?
 *
 * The claim the margins rest on is about COLOUR, not about attribute bytes, and
 * the two are not the same claim. A cell whose tile is blank in the bank both
 * attributes select shows nothing but colour 0 of its palette, so two different
 * palette numbers that agree on colour 0 paint identical pixels. That is not a
 * hypothetical: over the attract demo the DX body leaves whole regions of
 * blank tile $FF carrying attribute 1 where the hack's table says 0 -- tile $FF
 * is all zeroes in VRAM bank 0 and both palettes have white at index 0, so not
 * one pixel differs, yet a byte comparison rejected 2492 frames for it and
 * pillarboxed the view for sixty frames at a time.
 *
 * So compare what gets painted. The fast path is the byte compare (true for
 * essentially every cell); only a differing cell is rendered both ways, 64
 * pixels, and that happens a handful of times per frame at most. This also
 * makes the bit-7 exemption fall out rather than be asserted: BG-over-OBJ
 * priority selects no BG colour, so it can never change the answer here.
 */
static int cell_paints_same(GBContext *ctx, uint8_t ta, uint8_t da,
                            uint8_t tb, uint8_t ha) {
    if (ta == tb && !((da ^ ha) & 0x7Fu)) return 1;
    for (int row = 0; row < 8; row++) {
        unsigned aa = tile_row_addr(ta, (da & SML2_ATTR_FLIP_Y) ? 7 - row : row, da);
        unsigned ab = tile_row_addr(tb, (ha & SML2_ATTR_FLIP_Y) ? 7 - row : row, ha);
        uint8_t alo = s.vram[aa], ahi = s.vram[aa + 1];
        uint8_t blo = s.vram[ab], bhi = s.vram[ab + 1];
        for (int x = 0; x < 8; x++) {
            int ba = (da & SML2_ATTR_FLIP_X) ? x : 7 - x;
            int bb = (ha & SML2_ATTR_FLIP_X) ? x : 7 - x;
            int ca = ((alo >> ba) & 1) | (((ahi >> ba) & 1) << 1);
            int cb = ((blo >> bb) & 1) | (((bhi >> bb) & 1) << 1);
            if (shade_color(ctx, s.bg_pal, da & SML2_ATTR_PALETTE, ca, s.bgp) !=
                shade_color(ctx, s.bg_pal, ha & SML2_ATTR_PALETTE, cb, s.bgp))
                return 0;
        }
    }
    return 1;
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
    s.attr_hit = s.attr_total = 0;
    s.reject = SML2_REJ_NONE;
    s.gate_frames++;
    s.mode = peek(ctx, SML2_MODE);
#define REJECT(r) do { gate_reject(ctx, (r)); return 0; } while (0)
    /* Classify the overlay scenes FIRST, so that whichever check ends up
     * refusing the frame, snapshot() still knows the world underneath is the
     * one it already proved. */
    s.overlay = 0;
    if (s.mode == SML2_MODE_PAUSE) s.overlay = SML2_REJ_MODE;

    if (!s.overlay && s.mode != SML2_MODE_PLAY && s.mode != SML2_MODE_DEATH)
        REJECT(SML2_REJ_MODE);
    if (ctx->ram_bank != SML2_LEVEL_RAM_BANK) REJECT(SML2_REJ_RAMBANK);
    if (peek(ctx, SML2_BONUS_ROOM) & 0xF0u) REJECT(SML2_REJ_BONUS);
    /* The pause screen rebuilds the window layer and repoints LCDC, so these
     * two say nothing about the world while it is up. */
    if (!s.overlay && s.lcdc != SML2_GAMEPLAY_LCDC) REJECT(SML2_REJ_LCDC);
    if (!s.overlay && (s.wy != SML2_HUD_WY || s.wx != SML2_HUD_WX))
        REJECT(SML2_REJ_WINDOW);
    if (s.cam_x < SML2_CAM_CENTRE_X || s.cam_x >= SML2_MAP_COLS * 16) REJECT(SML2_REJ_CAMERA);
    if (s.cam_y < SML2_CAM_CENTRE_Y || s.cam_y >= SML2_MAP_ROWS * 16) REJECT(SML2_REJ_CAMERA);

    /* Score only the BG rows the status-bar window does NOT cover. The bottom
     * tile row (y 136..143 with WY = 136) is behind the window on every
     * gameplay frame, and the two images disagree about it: V1.0 keeps writing
     * the level into it, DX leaves it as $FF / attribute 0 because nothing can
     * ever see it. Scoring it would reject ~1 DX frame in 5 over something
     * that is not drawn -- the compositor overwrites those rows with the
     * window layer in step 4 of render(). Both bodies score 21 x 17 = 357.  */
    int rows = (s.wy + 7) / 8;
    if (rows > 18) rows = 18;
    memset(s.cell_miss, 0, sizeof s.cell_miss);
    int hit = 0, total = 0, ahit = 0, atotal = 0, aprio = 0, phit = 0;
    for (int ty = 0; ty < rows; ty++) {
        for (int tx = 0; tx < 21; tx++) {
            int wx = s.left + tx * 8, wy = s.top + ty * 8;
            if (wx < 0 || wy < 0) REJECT(SML2_REJ_NEGCOORD);
            if (block_at(wx, wy) > SML2_MAX_BLOCK_ID) REJECT(SML2_REJ_BLOCKID);
            unsigned sx = (unsigned)(s.scx + tx * 8) & 0xFFu;
            unsigned sy = (unsigned)(s.scy + ty * 8) & 0xFFu;
            unsigned cell = 0x1800u + (sy >> 3) * 32u + (sx >> 3);
            uint8_t tile = tile_at(wx, wy);
            uint8_t hw_tile = s.vram[cell];
            uint8_t da = attr_for_tile(tile);
            uint8_t ha = s.cgb ? s.vram[VRAM_SIZE + cell] : 0u;
            uint8_t miss = 0;
            total++;
            int tile_ok = hw_tile == tile;
            if (tile_ok) hit++; else miss |= 1u;
            /* THE gate: would this cell be painted the way the hardware paints
             * it? Tile index and attribute are both only proxies for that, and
             * both are proxies the ROM breaks.
             *
             * Block $7F -- every warp pipe -- is stamped into the tilemap
             * directly by 01:5C4F rather than read out of the $A600 table,
             * whose entry for it is a stale 122, and DX attributes that stamp
             * from [$D07F]. Standing under the Mushroom Zone pipe, 24 of the
             * 357 scored cells read (block $7F, table tile 122, hardware tile
             * $7F/attr 5) and the tile gate fell to 333/357 = 93.3%, under its
             * 95% threshold, so the view pillarboxed for as long as the player
             * stood there -- 2940 rejections in 5876 scored frames in the
             * owner's session.
             *
             * Both tiles paint the same thing. Tile 122 is $FF00 x8 (every
             * pixel colour 1) and palette 0 colour 1 is (74,164,246); tile $7F
             * is all zeroes (every pixel colour 0) and palette 5 colour 0 is
             * (74,164,246). Identical sky blue, 64 pixels out of 64. Forcing
             * the block to tile $7F is not the fix either -- measured, the same
             * block id renders as 122 in 8 other visible cells, so the block
             * map genuinely cannot tell the two apart and no table lookup will.
             *
             * Comparing the painted pixels answers the question that actually
             * matters, and subsumes the attribute-byte case this already
             * handled. The fast path is the byte compare, true for nearly every
             * cell; only a differing cell is rendered both ways. */
            if (cell_paints_same(ctx, tile, da, hw_tile, ha)) phit++;
            else miss |= 4u;
            /* Second, independent proof, and the one that makes the DX body
             * safe to widen: the attribute this module would paint the margin
             * with must be the attribute the hardware is showing in bank 1. */
            /* Score the attribute only where the TILE matched -- i.e. on the
             * cells this module actually claims to have decoded. Scoring the
             * rest would fold a pre-existing, body-independent gap in the tile
             * model into the colour gate: the ROM's direct tilemap writers
             * (block id $7F stamped as four copies of tile $7F by 24:7B80's
             * V1.0 original, block id 7 as $F8-$FB by 01:5B40) bypass the
             * $A600 block definitions, so a just-changed block reads back one
             * tile on hardware and another out of the block map until the next
             * scroll redraws it. Measured identically on BOTH bodies over the
             * probe route: 2025 frames at 357/357, 8 at 355, 67 at 353. The
             * 95% tile gate already covers that; the colour gate is 100% of
             * what is left.
             *
             * Bit 7 (BG-over-OBJ priority) is the one attribute bit the block
             * map cannot answer -- see the note above attr_for_tile. It
             * changes no colour, so the gate is on the seven bits that do, and
             * bit-7-only divergences are counted rather than passed over. */
            if (s.cgb && tile_ok) {
                uint8_t diff = (uint8_t)(da ^ ha);
                atotal++;
                if (!(diff & 0x7Fu)) ahit++; else miss |= 2u;
                if (diff & SML2_ATTR_PRIORITY) aprio++;
                if (diff & 0x7Fu) s.attr_byte_diff++;
            }
            s.cell_miss[ty * 21 + tx] = miss;
        }
    }
    s.score_hit = hit;
    s.score_total = total;
    s.attr_hit = ahit;
    s.attr_total = atotal;
    s.attr_prio_diff = aprio;
    s.paint_hit = phit;
    s.gate_scene++;
    /* An overlay scored: the numbers are in the ring, but the frame is still
     * refused -- the world may be intact while VRAM is not (the pause screen
     * swaps in font tiles), and holding the proven frame is always right and
     * never draws anything unproven. */
    if (s.overlay) REJECT(s.overlay);
    /* Fail closed on what is drawn. The old 95% tile threshold is gone: a
     * pixel-exact question does not want a tolerance, and the debounce already
     * absorbs the one-frame transients it used to cover. */
    if (total <= 0 || phit != total) { s.gate_tile_fail++; REJECT(SML2_REJ_TILE); }
    /* Fail closed on colour: a single wrong attribute in the native window
     * means the derivation is wrong somewhere, so no margin is trustworthy. */
    if (s.cgb && !s.attr_ok) { s.gate_attr_fail++; REJECT(SML2_REJ_NOTABLE); }
    /* Attribute-byte disagreement is now reporting only: if it changed a
     * pixel, the paint gate above already refused the frame. */
#undef REJECT
    return 1;
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

/* ---- enemy spawn policy --------------------------------------------------
 *
 * Extended target: the scanner's own 112 px reach measured from the edge of
 * what the player can actually see, not from the native 160 px strip. The view
 * is already clamped to the run of scroll boxes the camera may reach, so
 * extra_left/extra_right carry the level's bounds; the extra clamp here is the
 * level's own 4096 px extent.
 *
 * Ramp: the value returned climbs toward that target by at most 8 px per call,
 * and a call happens exactly once per frame the builder takes that direction's
 * branch -- i.e. once per frame the scanner actually runs that way. The edge
 * the ROM derives is this value with its low byte masked to 8 px, so a step of
 * at most 8 lands on every 8-px-aligned value in between and no list entry is
 * stepped over. Lagging BEHIND vanilla is safe and is left alone: a lower edge
 * only makes the scanner stop earlier, and nothing is consumed. */
static int spawn_edge(int side) {
    if (s.scan_pick_frame[side] == s.frame) return s.scan_pick[side];
    int vanilla = side == SML2_SIDE_RIGHT ? s.cam_x + SML2_SPAWN_HALF
                                          : s.cam_x - SML2_SPAWN_HALF;
    int target = side == SML2_SIDE_RIGHT ? vanilla + s.extra_right
                                         : vanilla - s.extra_left;
    if (target < 0) target = 0;
    if (target > SML2_MAP_COLS * 16) target = SML2_MAP_COLS * 16;
    int prev = s.scan_edge[side] < 0 ? vanilla : s.scan_edge[side];
    int edge = target;
    if (side == SML2_SIDE_RIGHT) {
        if (edge > prev + SML2_SPAWN_RAMP) edge = prev + SML2_SPAWN_RAMP;
    } else if (edge < prev - SML2_SPAWN_RAMP) {
        edge = prev - SML2_SPAWN_RAMP;
    }
    if (edge < 0) edge = 0;
    if (edge > 0xFFFF) edge = 0xFFFF;
    s.scan_edge[side] = edge;
    s.scan_pick[side] = edge;
    s.scan_pick_frame[side] = s.frame;
    int reach = side == SML2_SIDE_RIGHT ? edge - vanilla : vanilla - edge;
    if (reach > s.scan_reach_max[side]) s.scan_reach_max[side] = reach;
    if (-reach > s.scan_lag_max[side]) s.scan_lag_max[side] = -reach;
    return edge;
}

static void spawn_forget(void) {
    s.scan_edge[SML2_SIDE_RIGHT] = s.scan_edge[SML2_SIDE_LEFT] = -1;
    s.scan_pick_frame[SML2_SIDE_RIGHT] = s.scan_pick_frame[SML2_SIDE_LEFT] = -1;
}

/* One frame of the scanner's history, read back from the state it left behind:
 * $AF1E/$AF1F is where its cursor stopped and $AF00/$AF01 is the edge it used,
 * neither of which anything else in the ROM touches between two of these calls.
 * Every record the cursor stepped over was evaluated; the ones whose X equalled
 * the edge spawned, the rest were consumed and are gone. */
static void spawn_account(GBContext *ctx) {
#define SPAWN_PEEK(a) peek_eram_bank(ctx, SML2_LEVEL_RAM_BANK, (a))
    int level = SPAWN_PEEK(SML2_HEADER + 0x0A) | (SPAWN_PEEK(SML2_LEVEL_BANK) << 8);
    if (level != s.spawn_level) {
        s.spawn_level = level;
        s.spawn_cursor = -1;
        s.scan_seen_edge[0] = s.scan_seen_edge[1] = -1;
        memset(s.spawn_seen, 0, sizeof s.spawn_seen);
        spawn_forget();
    }
    int cursor = (SPAWN_PEEK(SML2_SPAWN_CURSOR) << 8) | SPAWN_PEEK(SML2_SPAWN_CURSOR + 1);
    uint8_t edge_hi = SPAWN_PEEK(SML2_SCAN_EDGE);
    int edge = ((edge_hi << 8) | SPAWN_PEEK(SML2_SCAN_EDGE + 1)) & 0xFFF8;
    int lo = (int)SML2_SPAWN_LIST, hi = lo + (int)SML2_SPAWN_LIST_SIZE;
    if (edge_hi != SML2_SCAN_NONE &&
        s.spawn_cursor >= lo && s.spawn_cursor < hi && cursor >= lo && cursor < hi &&
        cursor != s.spawn_cursor && (cursor - s.spawn_cursor) % SML2_SPAWN_RECORD == 0) {
        int step = cursor > s.spawn_cursor ? SML2_SPAWN_RECORD : -SML2_SPAWN_RECORD;
        int n = (cursor - s.spawn_cursor) / step;
        int dir = step > 0 ? SML2_SIDE_RIGHT : SML2_SIDE_LEFT;
        int prev_edge = s.scan_seen_edge[dir];
        s.scan_seen_edge[dir] = edge;
        s.spawn_scans++;
        for (int k = 0; k < n; k++) {
            int addr = s.spawn_cursor + step * k;
            int off = addr - lo;
            int x = (s.spawn_list[off] << 8) | s.spawn_list[off + 1];
            s.spawn_passed++;
            if (x == edge) {
                s.spawn_spawned++;
                s.spawn_seen[off / SML2_SPAWN_RECORD] |= 1u;
            } else {
                /* Between where this direction's edge was last time and where
                 * it is now = the edge crossed it. Anything else is the cursor
                 * seeking to a camera it was behind. */
                int stepped = prev_edge >= 0 &&
                              (dir == SML2_SIDE_RIGHT ? (x > prev_edge && x < edge)
                                                      : (x < prev_edge && x > edge));
                s.spawn_jumped++;
                if (stepped) s.spawn_stepped++; else s.spawn_seek++;
                s.spawn_seen[off / SML2_SPAWN_RECORD] |= (uint8_t)(stepped ? 4u : 2u);
                unsigned slot = s.spawn_ring_n++ % SML2_SPAWN_RING;
                s.spawn_ring[slot].frame = s.frame;
                s.spawn_ring[slot].cam_x = s.cam_x;
                s.spawn_ring[slot].edge = edge;
                s.spawn_ring[slot].prev_edge = prev_edge;
                s.spawn_ring[slot].x = x;
                s.spawn_ring[slot].addr = addr;
                s.spawn_ring[slot].stepped = stepped;
            }
        }
    }
    s.spawn_cursor = cursor;
    for (unsigned i = 0; i < SML2_SPAWN_LIST_SIZE; i++)
        s.spawn_list[i] = SPAWN_PEEK(SML2_SPAWN_LIST + i);
#undef SPAWN_PEEK
}

static void read_tap(GBContext *ctx, uint16_t address) {
    if (address != SML2_TAP_ADDR || !is_draw_bank(ctx->rom_bank)) return;
    if (ctx->pc != SML2_TAP_PC && ctx->pc != SML2_TAP_PC - 2) return;
    if (!s.wide || gb_custom_width <= GB_SCREEN_WIDTH) return;
    capture_actor(ctx);
}

/* ---- read overrides ------------------------------------------------------
 * 1. Enemy activation ($AF0A..$AF0D) and horizontal culling ($AF1A..$AF1D) are
 *    rebuilt each frame by 02:4000 as camX +- 0x60 / +- 0xA0 and consumed
 *    through the ROM0 memcpy at $3CAA. Widening them by the view's own margins
 *    lets vanilla-spawned actors act and stay alive across the whole view. The
 *    spawn scanner's own window is widened only when the player asks for it
 *    (2, below); with the default Original policy it is untouched, so spawn
 *    points and the list cursor stay exactly vanilla.
 * 2. Enemy spawns, when the player asked for Extended. The builder's own reads
 *    of the spawn-scan window ($AF12/$AF13 scrolling right, $AF14/$AF15
 *    scrolling left) are answered with a window that reaches the visible view
 *    edge, ramped by at most 8 px per scanning frame. The ROM still does the
 *    $F8 alignment, the direction choice, the equality test, the difficulty
 *    filter and the slot allocation -- only the edge moves. Original installs
 *    nothing here and every scanner input stays exactly vanilla.
 * 3. The actor draw routine computes a screen X modulo 256 (03:409F). An actor
 *    that the widened activation kept alive far off the native screen would
 *    alias back into it as a ghost. Presenting 03:409B with a scroll shadow
 *    that puts such an actor at screen X 0xB8 makes the ROM's own 03:4025 test
 *    drop it -- which also keeps the 40-entry OAM buffer from overflowing --
 *    and the host draws it in the margin instead.
 */
static uint8_t read_override(GBContext *ctx, uint16_t address, uint8_t value) {
    if (!s.wide || gb_custom_width <= GB_SCREEN_WIDTH) return value;
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

    /* s.wide (the caller's gate) is the PRESENTATION decision and stays true
     * through the debounce window on a stale world; s.valid is the proof that
     * this frame's world was actually decoded. Pixels may be stale for six
     * frames; a spawn may not be, because the scanner consumes what it passes.
     * So this override alone additionally requires s.valid, and every read it
     * declines is counted rather than passed over in silence. */
    if (s.spawn_extend && ctx->rom_bank == 2 &&
        address >= SML2_SPAWN_UPPER_HI && address <= SML2_SPAWN_LOWER_LO) {
        int side = address <= SML2_SPAWN_UPPER_LO ? SML2_SIDE_RIGHT : SML2_SIDE_LEFT;
        int high = address == SML2_SPAWN_UPPER_HI || address == SML2_SPAWN_LOWER_HI;
        unsigned site = high ? (side ? SML2_SPAWN_L_HI_PC : SML2_SPAWN_R_HI_PC)
                             : (side ? SML2_SPAWN_L_LO_PC : SML2_SPAWN_R_LO_PC);
        /* ld a,[nn] is three bytes; the generated code reports the NEXT
         * instruction, the interpreter reports the instruction itself. */
        if (pc != site && pc != site + 3) return value;
        if (!s.valid) {
            /* The builder's own read, on a frame the gate did not prove: hand
             * back the guest's byte and count it, so "how often did the gate
             * cost us the widened window" is a query, not a guess. */
            s.spawn_ungated++;
            return value;
        }
        if (high) {
            s.scan_hi[side]++;
            return (uint8_t)(spawn_edge(side) >> 8);
        }
        /* The low byte must come from the SAME chosen edge as the high byte the
         * builder read six bytes ago, never from a fresh ramp step. */
        if (s.scan_pick_frame[side] < 0) return value;
        s.scan_lo[side]++;
        if (s.scan_pick_frame[side] != s.frame) s.scan_unpaired++;
        return (uint8_t)s.scan_pick[side];
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

#define SML2_FRAME_FIELDS(OP) \
    OP(cgb); OP(attr_ok); OP(cam_x); OP(cam_y); OP(left); OP(top); \
    OP(view_left); OP(view_width); OP(extra_left); OP(extra_right); \
    OP(bound_left); OP(bound_right); OP(count); OP(lcdc); OP(scx); OP(scy); \
    OP(wx); OP(wy)

#define SML2_FRAME_ARRAYS(OP) \
    OP(vram); OP(oam); OP(map); OP(blockdef); \
    OP(box); OP(attr_tile)

static void frame_save(void) {
#define SAVE_SCALAR(f) s_good.f = s.f
#define SAVE_ARRAY(f)  memcpy(s_good.f, s.f, sizeof s_good.f)
    SML2_FRAME_FIELDS(SAVE_SCALAR);
    SML2_FRAME_ARRAYS(SAVE_ARRAY);
#undef SAVE_SCALAR
#undef SAVE_ARRAY
    if (s.count > 0)
        memcpy(s_good.sprite, s.sprite, (size_t)s.count * sizeof(Sml2Sprite));
    s_good_valid = 1;
}

static void frame_restore(void) {
#define LOAD_SCALAR(f) s.f = s_good.f
#define LOAD_ARRAY(f)  memcpy(s.f, s_good.f, sizeof s_good.f)
    SML2_FRAME_FIELDS(LOAD_SCALAR);
    SML2_FRAME_ARRAYS(LOAD_ARRAY);
#undef LOAD_SCALAR
#undef LOAD_ARRAY
    if (s.count > 0)
        memcpy(s.sprite, s_good.sprite, (size_t)s.count * sizeof(Sml2Sprite));
}

/* ---- per-frame snapshot (PPU line 0) ---- */

static void snapshot(GBContext *ctx) {
    GBPPU *p = (GBPPU *)ctx->ppu;
    s.ctx = ctx;
    s.frame++;
    s.lcdc = p->lcdc; s.scx = p->scx; s.scy = p->scy;
    s.wx = p->wx; s.wy = p->wy;
    s.bgp = p->bgp; s.obp0 = p->obp0; s.obp1 = p->obp1;
    /* Both VRAM banks: on a CGB body bank 1 carries the BG attribute map the
     * gate checks against, and the second half of the tile data. */
    memcpy(s.vram, ctx->vram, sizeof s.vram);
    memcpy(s.oam, ctx->oam, OAM_SIZE);
    memcpy(s.bg_pal, p->bg_palette_ram, 64);
    memcpy(s.obj_pal, p->obj_palette_ram, 64);
    s.cgb = cgb_mode(ctx);
    /* Re-read the hack's tile -> attribute table every frame out of WRAM bank
     * 2, so a mid-level tileset (and therefore palette) change follows for
     * free. Host read with an explicit bank: the guest is running with SVBK
     * 0/1 and the level's own block map sits at the same address there. */
    s.attr_ok = 0;
    if (s.cgb) {
        for (unsigned i = 0; i < SML2_DX_ATTR_SIZE; i++)
            s.attr_tile[i] = peek_wram_bank(ctx, SML2_DX_ATTR_BANK,
                                            SML2_DX_ATTR_BASE + i);
        s.attr_ok = 1;
    } else {
        memset(s.attr_tile, 0, sizeof s.attr_tile);
    }
    peek_level_block(ctx, SML2_MAP_BASE, s.map, SML2_MAP_SIZE);
    peek_block(ctx, SML2_BLOCKDEF_BASE, s.blockdef, SML2_BLOCKDEF_SIZE);
    peek_block(ctx, SML2_SCROLLBOX, s.box, SML2_SCROLLBOX_LEN);

    s.cam_x = peek16(ctx, SML2_CAM_X);
    s.cam_y = peek16(ctx, SML2_CAM_Y);
    /* Always on, in both policies: charge the previous frame's scanner run
     * before anything this frame can move the cursor again. */
    spawn_account(ctx);
    /* A camera that moved further than the scanner's own 8 px quantum in one
     * frame -- a warp, a room change, a state load -- is a discontinuity the
     * ramp cannot bridge, so re-engage from vanilla rather than chase it. */
    if (s.cam_prev_ok && abs(s.cam_x - s.cam_prev) > SML2_SPAWN_RAMP) {
        spawn_forget();
        s.scan_resets++;
    }
    s.cam_prev = s.cam_x;
    s.cam_prev_ok = 1;
    /* ---- where the screen's top-left actually is in the world -------------
     *
     * The camera ($FFC8/$FFCA) is the usual answer, corrected onto the scroll
     * register the hardware is displaying, because screen shake moves SCY
     * without moving the camera. That correction is a single int8_t delta, and
     * it can only express +-127.
     *
     * A warp-pipe dive breaks it. The game scrolls SCY from 120 to 252 while
     * leaving $FFC8 at its pre-dive value, a shift of 132, which wraps to -124
     * and lands the decode a whole 256-pixel block row away from the world.
     * The 21-column score cannot see that -- it compares cells that alias
     * modulo 256 -- so it passed anyway, and the margins would have been drawn
     * from the wrong part of the level.
     *
     * So the register is followed INCREMENTALLY from the previous frame, where
     * per-frame motion is single digits and always fits, and the camera is
     * re-anchored to only when it is the thing that moved. Both candidates
     * satisfy (uint8_t)origin == register by construction; they differ only in
     * which 256-pixel page, and that is exactly the question the camera stops
     * being able to answer mid-dive. */
    int cam_left = s.cam_x - SML2_CAM_CENTRE_X;
    int cam_top = s.cam_y - SML2_CAM_CENTRE_Y;
    cam_left += (int8_t)(uint8_t)(s.scx - (uint8_t)cam_left);
    cam_top += (int8_t)(uint8_t)(s.scy - (uint8_t)cam_top);
    int cam_moved = !s.scroll_tracked ||
                    s.cam_x != s.scroll_cam_x || s.cam_y != s.scroll_cam_y;
    if (cam_moved) {
        s.left = cam_left;
        s.top = cam_top;
    } else {
        s.left = s.scroll_left + (int8_t)(uint8_t)(s.scx - s.scroll_scx);
        s.top = s.scroll_top + (int8_t)(uint8_t)(s.scy - s.scroll_scy);
        if (s.left != cam_left || s.top != cam_top) s.scroll_offpage++;
    }
    s.scroll_tracked = 1;
    s.scroll_cam_x = s.cam_x;
    s.scroll_cam_y = s.cam_y;
    s.scroll_scx = s.scx;
    s.scroll_scy = s.scy;
    s.scroll_left = s.left;
    s.scroll_top = s.top;

    /* The sprite list built during the frame that is about to be shown. */
    s.count = s.build_count;
    if (s.count) {
        memcpy(s.sprite, s.build, (size_t)s.count * sizeof(Sml2Sprite));
        s.capture_frames++;
    }
    s.build_count = 0;

    int was_wide = s.wide;
    s.valid = validate_scene(ctx);
    if (s.valid) {
        s.fail_run = 0;
        s.holding = 0;
        s.wide = 1;                     /* recover instantly */
    } else if (s.overlay && s_good_valid && s.wide) {
        /* Held: the scene is drawn over a world we already proved. No expiry
         * and no debounce credit spent. */
        if (!s.holding) s.held_runs++;
        s.holding = 1;
        s.fail_run = 0;
        s.held++;
        frame_restore();
        return;
    } else {
        s.holding = 0;
        s.fail_run++;
        /* Hold the wide view on the last proven-good margins until the run of
         * rejections is long enough to be a real scene change. */
        if (s.fail_run >= SML2_FALLBACK_DEBOUNCE || !s_good_valid) s.wide = 0;
    }
    if (s.wide != was_wide && gb_custom_width > GB_SCREEN_WIDTH) {
        Sml2FlipEvent *f = &s.flip_log[s.flip_log_seq % SML2_FLIP_LOG_CAP];
        f->frame = (unsigned)s.frame;
        f->to_wide = (uint8_t)s.wide;
        f->reason = (uint8_t)s.reject;
        f->mode = (uint8_t)s.mode;
        f->cgb = (uint8_t)s.cgb;
        f->score_hit = (int16_t)s.score_hit;
        f->score_total = (int16_t)s.score_total;
        f->attr_hit = (int16_t)s.attr_hit;
        f->attr_total = (int16_t)s.attr_total;
        f->cam_x = (int16_t)s.cam_x;
        f->cam_y = (int16_t)s.cam_y;
        f->tileset = peek(ctx, SML2_DX_TILESET);
        f->fail_run = (uint8_t)(s.fail_run > 255 ? 255 : s.fail_run);
        s.flip_log_seq++;
        s.flips++;
    }
    if (!s.wide) {
        s.bound_left = s.bound_right = 0;
        s.extra_left = s.extra_right = 0;
        s.count = 0;
        s.fallbacks++;
        s.narrowed++;
        switch (s.reject) {
            case SML2_REJ_TILE: case SML2_REJ_ATTR: case SML2_REJ_BLOCKID:
            case SML2_REJ_NEGCOORD: case SML2_REJ_RAMBANK: case SML2_REJ_NOTABLE:
                s.narrowed_model++;
                if (was_wide) s.pillarbox_model++;
                break;
            default: break;
        }
        if (!s.valid) s_good_valid = 0;   /* nothing good to hold on to now */
        /* Fail closed on spawns: the gate refused this frame, so the widened
         * scan edge is unproven. The override hands back the guest's own
         * camX +- 112 and the ramp re-engages from the vanilla edge on the
         * next frame the gate accepts, instead of resuming a stale one. */
        spawn_forget();
        return;
    }
    if (!s.valid) {
        /* Debounce window: present the last accepted frame's world.
         *
         * That world is deliberately STALE -- which is right for pixels and
         * wrong for spawns. frame_restore() puts the last accepted frame's
         * cam_x and extra_left/extra_right back into s, so a widened edge
         * computed here would be measured from a camera the guest has already
         * left, and the spawn scanner CONSUMES what it passes: an entry given
         * away on an unproven frame cannot be taken back, unlike a pixel. So
         * the debounce window widens the view but never the spawn window --
         * same fail-closed rule as a rejected frame, and the ramp re-engages
         * from vanilla when the gate proves a frame again. */
        frame_restore();
        s.debounced++;
        spawn_forget();
        return;
    }
    compute_bounds();
    s.view_width = gb_custom_width > GB_SCREEN_WIDTH ? gb_custom_width : GB_SCREEN_WIDTH;
    s.view_left = view_left_for(s.left, s.view_width);
    s.extra_left = s.left - s.view_left;
    s.extra_right = (s.view_left + s.view_width) - (s.left + GB_SCREEN_WIDTH);
    if (s.extra_left < 0) s.extra_left = 0;
    if (s.extra_right < 0) s.extra_right = 0;

    frame_save();

    if (getenv("SML2_ADAPTIVE_TRACE") && s.frame % 120 == 0) {
        fprintf(stderr,
                "[ADAPTIVE] frame=%d valid=%d mode=%02X cam=%d,%d left=%d top=%d "
                "view=%d+%d bounds=%d..%d score=%d/%d attr=%d/%d sprites=%d "
                "captures=%u widened=%u dropped=%u fallbacks=%u\n",
                s.frame, s.valid, s.mode, s.cam_x, s.cam_y, s.left, s.top,
                s.view_left, s.view_width, s.bound_left, s.bound_right,
                s.score_hit, s.score_total, s.attr_hit, s.attr_total, s.count,
                s.captures, s.rescues, s.ghosts, s.fallbacks);
    }
}

/* ---- sprite composition -------------------------------------------------- */

static void draw_sprite(uint32_t *out, int width, Sml2Sprite sp) {
    int h = (s.lcdc & LCDC_OBJ_SIZE) ? 16 : 8;
    uint8_t tile = sp.tile;
    if (!(s.lcdc & LCDC_OBJ_ENABLE)) return;
    if (s.cgb) {
        s.sprite_pal_mask |= 1u << (sp.attr & OAM_CGB_PALETTE);
        if (sp.attr & OAM_CGB_BANK) s.sprite_bank1++;
    }
    if (h == 16) tile &= 0xFEu;
    for (int y = 0; y < h; y++) {
        int sy = sp.y - s.top + y;
        if (sy < 0 || sy >= GB_SCREEN_HEIGHT) continue;
        int py = (sp.attr & OAM_FLIP_Y) ? h - 1 - y : y;
        /* Mask to one bank FIRST, then select the bank: masking afterwards
         * would clear bit 13 and send every bank-1 sprite back to bank 0. */
        unsigned a = ((unsigned)tile * 16u + (unsigned)py * 2u) & 0x1FFEu;
        if (s.cgb && (sp.attr & OAM_CGB_BANK)) a += VRAM_SIZE;
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
            /* s.opaque holds the BG pixel this module drew: raw colour in bits
             * 0-1, the cell's CGB priority bit in bit 7. Mirrors ppu.c
             * render_sprites_segment: on CGB either the BG attribute's
             * priority or the sprite's own hides the sprite behind a non-zero
             * BG pixel, unless LCDC bit 0 is clear. */
            if (s.cgb) {
                if ((s.opaque[o] & 3u) && (s.lcdc & LCDC_BG_ENABLE) &&
                    ((s.opaque[o] & SML2_ATTR_PRIORITY) || (sp.attr & OAM_PRIORITY)))
                    continue;
            } else if ((sp.attr & OAM_PRIORITY) && (s.lcdc & LCDC_BG_ENABLE) &&
                       (s.opaque[o] & 3u)) {
                continue;
            }
            int pal_no = s.cgb ? (sp.attr & OAM_CGB_PALETTE)
                               : ((sp.attr & OAM_PALETTE) ? 1 : 0);
            uint8_t reg = (sp.attr & OAM_PALETTE) ? s.obp1 : s.obp0;
            out[o] = shade_color(s.ctx, s.obj_pal, pal_no, c, reg);
        }
    }
}

/* ---- the compositor ---- */

static int render(GBContext *ctx, uint32_t *out, int width, const uint32_t *native) {
    if (!s.wide || !ctx->rom || width <= GB_SCREEN_WIDTH) return 0;
    if (width != s.view_width) {
        s.view_width = width;
        s.view_left = view_left_for(s.left, width);
    }

    /* 1. background, straight from the world block map. On a CGB body each
     * cell also carries the DX attribute derived from its tile index, which
     * validate_scene() has already proved against VRAM bank 1 this frame. */
    uint32_t bg[8][4];
    for (int p = 0; p < (s.cgb ? 8 : 1); p++)
        for (int i = 0; i < 4; i++) bg[p][i] = shade_color(ctx, s.bg_pal, p, i, s.bgp);
    uint32_t black = rgb555(0);
    for (int y = 0; y < GB_SCREEN_HEIGHT; y++) {
        int wy = s.top + y;
        int row = wy & 7;
        int row_ok = wy >= 0 && wy < SML2_MAP_ROWS * 16;
        uint32_t *line = out + (size_t)y * width;
        uint8_t *op = s.opaque + (size_t)y * width;
        int cached_tx = -0x7FFFFFFF;
        uint8_t lo = 0, hi = 0, attr = 0;
        const uint32_t *pal = bg[0];
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
                uint8_t tile = tile_at(wx, wy);
                attr = attr_for_tile(tile);
                int trow = (attr & SML2_ATTR_FLIP_Y) ? 7 - row : row;
                unsigned a = tile_row_addr(tile, trow, attr);
                lo = s.vram[a];
                hi = s.vram[a + 1];
                pal = bg[attr & SML2_ATTR_PALETTE];
            }
            int bit = (attr & SML2_ATTR_FLIP_X) ? (wx & 7) : 7 - (wx & 7);
            int c = ((lo >> bit) & 1) | (((hi >> bit) & 1) << 1);
            line[x] = pal[c];
            op[x] = (uint8_t)(c | (attr & SML2_ATTR_PRIORITY));
        }
    }

    s.sprite_pal_mask = 0;
    s.sprite_bank1 = 0;

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
        int right_start = width - (SML2_HUD_COLS - SML2_HUD_SPLIT) * 8;
        for (int y = s.wy; y < GB_SCREEN_HEIGHT; y++) {
            int wl = y - s.wy;
            unsigned row_base = map + (unsigned)(wl >> 3) * 32u;
            /* The status bar is the real window layer, so unlike the margins
             * its attributes need no derivation: they are the hardware's own,
             * in VRAM bank 1 at the same offset. The padded gap repeats the
             * bar's blank cell, attribute included. */
            uint8_t fill = s.vram[row_base + SML2_HUD_FILL_COL];
            uint8_t fill_attr = s.cgb ? s.vram[VRAM_SIZE + row_base + SML2_HUD_FILL_COL] : 0u;
            uint32_t *line = out + (size_t)y * width;
            for (int x = origin > 0 ? origin : 0; x < width; x++) {
                int src = -1, col, bit;
                uint8_t t, attr;
                int wxp = x - origin;
                if (wxp < SML2_HUD_SPLIT * 8) src = wxp;
                else if (x >= right_start) src = SML2_HUD_COLS * 8 - (width - x);
                if (src >= 0 && src < SML2_HUD_COLS * 8) {
                    col = src >> 3;
                    t = s.vram[row_base + (unsigned)col];
                    attr = s.cgb ? s.vram[VRAM_SIZE + row_base + (unsigned)col] : 0u;
                    bit = src & 7;
                } else {
                    t = fill;
                    attr = fill_attr;
                    bit = x & 7;
                }
                if (!(attr & SML2_ATTR_FLIP_X)) bit = 7 - bit;
                int trow = (attr & SML2_ATTR_FLIP_Y) ? 7 - (wl & 7) : (wl & 7);
                unsigned a = tile_row_addr(t, trow, attr);
                int c = ((s.vram[a] >> bit) & 1) | (((s.vram[a + 1] >> bit) & 1) << 1);
                line[x] = bg[attr & SML2_ATTR_PALETTE][c];
            }
        }
    }
    return 1;
}

static void reset(GBContext *ctx) {
    GBPPU *ppu = (GBPPU *)ctx->ppu;
    if (ppu->view_stride != GB_SCREEN_WIDTH) ppu_set_view_margins(ppu, 0, 0);
    /* A state load rewinds the guest but not this module: drop the frozen
     * frame rather than compose the new world with the old one's margins. */
    s_good_valid = 0;
    s.wide = 0;
    s.fail_run = 0;
    s.holding = 0;
    s.overlay = 0;
    s.scroll_tracked = 0;
    s.valid = 0;
    s.count = s.build_count = 0;
    s.bound_left = s.bound_right = 0;
    s.extra_left = s.extra_right = 0;
    s.cam_prev_ok = 0;
    s.spawn_cursor = -1;
    s.scan_seen_edge[0] = s.scan_seen_edge[1] = -1;
    spawn_forget();
}

/* ---- install ---- */

void sml2_adaptive_init(GBContext *ctx) {
    memset(&s, 0, sizeof s);
    memset(&s_good, 0, sizeof s_good);
    s_good_valid = 0;
    s.ctx = ctx;
    s.spawn_cursor = -1;
    s.spawn_level = -1;
    s.scan_seen_edge[0] = s.scan_seen_edge[1] = -1;
    spawn_forget();
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
    s.spawn_extend = mods->spawns == SML2_SPAWNS_EXTENDED;
    gb_custom_render = render;
    gb_custom_snapshot = snapshot;
    gb_custom_reset = reset;
    gb_custom_read_tap = read_tap;
    gb_custom_read_override = read_override;
    fprintf(stderr,
            "[ADAPTIVE] Super Mario Land 2 compositor installed, width=%d, spawns=%s\n",
            gb_custom_requested_width, s.spawn_extend ? "extended" : "original");
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
            "\"dx\":%d,\"dx_available\":%d,\"body\":\"%s\",\"margins\":%d,"
            "\"spawns\":%d,\"spawns_hooked\":%d}",
            id, gb_custom_render != NULL, gb_custom_width,
            gb_custom_requested_width,
            m->dx, sml2_dx_patch_available(), body ? body : "", note == 0,
            m->spawns, s.spawn_extend);
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
    /* sml2_save / sml2_load / sml2_capture are SUPERSEDED by the engine's
     * generic save_state / load_state / screenshot commands (see
     * gb-recompiled/docs/DEBUG_SERVER.md). They survive only as thin aliases
     * that pin the historic default paths, so the existing probes and any
     * older script keep working; there is one implementation, in the engine.
     * New code should call the generic commands and pass its own path. */
    if (!strcmp(cmd, "sml2_save"))
        return gb_debug_server_save_state(
            id, strstr(json ? json : "", "\"path\"") ? json
                                                    : "{\"path\":\"logs/probe.state\"}");
    if (!strcmp(cmd, "sml2_load"))
        return gb_debug_server_load_state(
            id, strstr(json ? json : "", "\"path\"") ? json
                                                    : "{\"path\":\"logs/probe.state\"}");
    if (!strcmp(cmd, "sml2_capture"))
        /* recompose:1 keeps the old semantics exactly -- the compositor is
         * re-run against current VRAM/OAM rather than the last presented
         * frame being reused, which probe_dx_widescreen.py relies on when it
         * re-reads sml2_view right after the capture. */
        return gb_debug_server_screenshot(
            id, strstr(json ? json : "", "\"path\"")
                    ? json
                    : "{\"path\":\"logs/probe.ppm\",\"recompose\":1}");
    if (!strcmp(cmd, "sml2_flip_log")) {
        unsigned seq = s.flip_log_seq;
        unsigned first = seq > SML2_FLIP_LOG_CAP ? seq - SML2_FLIP_LOG_CAP : 0;
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":true,\"seq\":%u,\"first\":%u,\"flips\":%u,"
            "\"debounced\":%u,\"narrowed\":%u,\"debounce\":%d}",
            id, seq, first, s.flips, s.debounced, s.narrowed,
            SML2_FALLBACK_DEBOUNCE);
        for (unsigned k = first; k < seq; k++) {
            const Sml2FlipEvent *f = &s.flip_log[k % SML2_FLIP_LOG_CAP];
            gb_debug_server_send_fmt(
                "{\"id\":%d,\"ok\":true,\"seq\":%u,\"frame\":%u,\"to_wide\":%u,"
                "\"reason\":\"%s\",\"mode\":%u,\"cgb\":%u,\"tileset\":%u,"
                "\"fail_run\":%u,\"cam\":[%d,%d],\"score\":[%d,%d],"
                "\"attr_score\":[%d,%d]}",
                id, k, f->frame, f->to_wide,
                f->reason < SML2_REJ_COUNT ? k_reject_name[f->reason] : "?",
                f->mode, f->cgb, f->tileset, f->fail_run, f->cam_x, f->cam_y,
                f->score_hit, f->score_total, f->attr_hit, f->attr_total);
        }
        gb_debug_server_send_fmt("{\"id\":%d,\"ok\":true,\"end\":true,\"seq\":%u}",
                                 id, seq);
        return 1;
    }
    if (!strcmp(cmd, "sml2_gate_log")) {
        /* Query the always-on rejection ring: {"since":N} returns every event
         * recorded from sequence N onward (clamped to what the ring still
         * holds), newest last, plus the running per-reason totals. Nothing is
         * armed and nothing is cleared -- the ring has been filling since the
         * body booted. */
        int since = json_int(json, "\"since\"", 0);
        unsigned seq = s.gate_log_seq;
        unsigned first = seq > SML2_GATE_LOG_CAP ? seq - SML2_GATE_LOG_CAP : 0;
        if ((unsigned)since > first) first = (unsigned)since;
        int want = json_int(json, "\"limit\"", 64);
        if (want < 1) want = 1;
        if (want > SML2_GATE_LOG_CAP) want = SML2_GATE_LOG_CAP;
        if (seq - first > (unsigned)want) first = seq - (unsigned)want;

        char reasons[512];
        int n = 0;
        for (int i = 0; i < SML2_REJ_COUNT; i++)
            n += snprintf(reasons + n, sizeof reasons - (size_t)n, "%s\"%s\":%u",
                          i ? "," : "", k_reject_name[i], s.gate_reason[i]);
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":true,\"seq\":%u,\"first\":%u,\"dropped\":%u,"
            "\"frames\":%u,\"scored\":%u,\"flips\":%u,\"fallbacks\":%u,"
            "\"debounced\":%u,\"narrowed\":%u,\"narrowed_model\":%u,"
            "\"pillarbox_model\":%u,\"held\":%u,\"held_runs\":%u,"
            "\"attr_byte_diff\":%u,"
            "\"reasons\":{%s}}",
            id, seq, first,
            seq > SML2_GATE_LOG_CAP && (unsigned)since < seq - SML2_GATE_LOG_CAP
                ? (seq - SML2_GATE_LOG_CAP) - (unsigned)since : 0u,
            s.gate_frames, s.gate_scene, s.flips, s.fallbacks,
            s.debounced, s.narrowed, s.narrowed_model, s.pillarbox_model,
            s.held, s.held_runs, s.attr_byte_diff, reasons);
        for (unsigned k = first; k < seq; k++) {
            const Sml2GateEvent *e = &s.gate_log[k % SML2_GATE_LOG_CAP];
            char cells[512];
            int c = 0;
            cells[c++] = '[';
            for (int i = 0; i < e->cells; i++)
                c += snprintf(cells + c, sizeof cells - (size_t)c,
                              "%s{\"tx\":%u,\"ty\":%u,\"block\":%u,\"tile\":%u,"
                              "\"hw_tile\":%u,\"attr\":%u,\"hw_attr\":%u,\"flags\":%u}",
                              i ? "," : "", e->cell[i].tx, e->cell[i].ty,
                              e->cell[i].block, e->cell[i].tile, e->cell[i].hw_tile,
                              e->cell[i].attr, e->cell[i].hw_attr, e->cell[i].flags);
            cells[c++] = ']';
            cells[c] = '\0';
            gb_debug_server_send_fmt(
                "{\"id\":%d,\"ok\":true,\"seq\":%u,\"frame\":%u,\"reason\":\"%s\","
                "\"mode\":%u,\"lcdc\":%u,\"wy\":%u,\"wx\":%u,\"bonus\":%u,"
                "\"transition\":%u,\"cgb\":%u,\"tileset\":%u,\"wram_bank\":%u,"
                "\"ram_bank\":%u,\"rom_bank\":%u,\"ly\":%u,\"cam\":[%d,%d],"
                "\"left\":%d,\"top\":%d,\"score\":[%d,%d],\"attr_score\":[%d,%d],"
                "\"prio_diff\":%d,\"cells\":%s}",
                id, k, e->frame,
                e->reason < SML2_REJ_COUNT ? k_reject_name[e->reason] : "?",
                e->mode, e->lcdc, e->wy, e->wx, e->bonus, e->transition, e->cgb,
                e->tileset, e->wram_bank, e->ram_bank, e->rom_bank, e->ly,
                e->cam_x, e->cam_y, e->left, e->top,
                e->score_hit, e->score_total, e->attr_hit, e->attr_total,
                e->prio_diff, cells);
        }
        gb_debug_server_send_fmt("{\"id\":%d,\"ok\":true,\"end\":true,\"seq\":%u}",
                                 id, seq);
        return 1;
    }
    if (!strcmp(cmd, "sml2_score_map")) {
        /* 18 rows of 21 characters: '.' both matched, 't' tile mismatch,
         * 'a' attribute mismatch, 'b' both. From the snapshot the last scored
         * frame actually used, so it needs no arming and no stepping. */
        char rows[18 * 25 + 8];   /* 18 x ("," + quote + 21 + quote) + NUL */
        int n = 0;
        for (int ty = 0; ty < 18; ty++) {
            if (ty) rows[n++] = ',';
            rows[n++] = '"';
            for (int tx = 0; tx < 21; tx++)
                rows[n++] = ".tab"[s.cell_miss[ty * 21 + tx] & 3u];
            rows[n++] = '"';
        }
        rows[n] = '\0';
        /* ...plus the first few mismatching cells in full, so "where" comes
         * with "what": the tile this module decoded, the tile the hardware is
         * showing, the attribute derived from the table and the attribute in
         * VRAM bank 1. */
        char detail[2048];
        int d = 0, shown = 0;
        detail[d++] = '[';
        for (int ty = 0; ty < 18 && shown < 12; ty++) {
            for (int tx = 0; tx < 21 && shown < 12; tx++) {
                if (!s.cell_miss[ty * 21 + tx]) continue;
                int wx = s.left + tx * 8, wy = s.top + ty * 8;
                unsigned sx = (unsigned)(s.scx + tx * 8) & 0xFFu;
                unsigned sy = (unsigned)(s.scy + ty * 8) & 0xFFu;
                unsigned cell = 0x1800u + (sy >> 3) * 32u + (sx >> 3);
                uint8_t tile = tile_at(wx, wy);
                d += snprintf(detail + d, sizeof detail - (size_t)d,
                              "%s{\"tx\":%d,\"ty\":%d,\"wx\":%d,\"wy\":%d,"
                              "\"cell\":%u,\"block\":%u,\"tile\":%u,\"hw_tile\":%u,"
                              "\"attr\":%u,\"hw_attr\":%u}",
                              shown ? "," : "", tx, ty, wx, wy,
                              (unsigned)(cell - 0x1800u), block_at(wx, wy), tile,
                              s.vram[cell], attr_for_tile(tile), s.vram[VRAM_SIZE + cell]);
                shown++;
            }
        }
        detail[d++] = ']';
        detail[d] = '\0';
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":true,\"score\":[%d,%d],\"attr_score\":[%d,%d],"
            "\"scx\":%d,\"scy\":%d,\"left\":%d,\"top\":%d,\"map\":[%s],"
            "\"misses\":%s}",
            id, s.score_hit, s.score_total, s.attr_hit, s.attr_total,
            s.scx, s.scy, s.left, s.top, rows, detail);
        return 1;
    }
    if (!strcmp(cmd, "sml2_spawn_state")) {
        /* The whole spawn list as the game holds it, each record tagged with
         * what the scanner has done with it so far, plus the tail of the
         * always-on ring of entries the cursor stepped over without spawning.
         * No arming and no stepping: every frame since boot is already in it. */
        /* snprintf returns the length it WOULD have written, so neither of
         * these loops may add it to the cursor unchecked -- past the end of a
         * long spawn list that is exactly how the cursor walks off the buffer
         * and the JSON comes back truncated mid-token. Both append through
         * emit(), which stops on the first entry that does not fit and says so
         * in the payload rather than silently dropping it. */
        static char recs[8192], ring[5120];
        int n = 0, count = 0, recs_full = 0;
        recs[n++] = '[';
        for (unsigned off = SML2_SPAWN_RECORD;
             off + SML2_SPAWN_RECORD <= SML2_SPAWN_LIST_SIZE;
             off += SML2_SPAWN_RECORD) {
            if (s.spawn_list[off] == 0xFFu) break;
            int room = (int)sizeof recs - n - 2;        /* keep ']' and NUL */
            int k = snprintf(recs + n, (size_t)(room > 0 ? room : 0),
                             "%s{\"a\":%u,\"x\":%u,\"f\":%u,\"seen\":%u}",
                             count ? "," : "", SML2_SPAWN_LIST + off,
                             (unsigned)((s.spawn_list[off] << 8) | s.spawn_list[off + 1]),
                             s.spawn_list[off + 2],
                             s.spawn_seen[off / SML2_SPAWN_RECORD]);
            if (k < 0 || k >= room) { recs[n] = '\0'; recs_full = 1; break; }
            n += k;
            count++;
        }
        recs[n++] = ']';
        recs[n] = '\0';
        int r = 0, shown = 0, ring_full = 0;
        unsigned total = s.spawn_ring_n;
        unsigned first = total > SML2_SPAWN_RING ? total - SML2_SPAWN_RING : 0;
        ring[r++] = '[';
        for (unsigned i = first; i < total; i++) {
            unsigned slot = i % SML2_SPAWN_RING;
            int room = (int)sizeof ring - r - 2;
            int k = snprintf(ring + r, (size_t)(room > 0 ? room : 0),
                             "%s{\"frame\":%d,\"cam_x\":%d,\"edge\":%d,"
                             "\"prev_edge\":%d,\"x\":%d,\"a\":%d,\"stepped\":%d}",
                             shown ? "," : "", s.spawn_ring[slot].frame,
                             s.spawn_ring[slot].cam_x, s.spawn_ring[slot].edge,
                             s.spawn_ring[slot].prev_edge,
                             s.spawn_ring[slot].x, s.spawn_ring[slot].addr,
                             s.spawn_ring[slot].stepped);
            if (k < 0 || k >= room) { ring[r] = '\0'; ring_full = 1; break; }
            r += k;
            shown++;
        }
        ring[r++] = ']';
        ring[r] = '\0';
        gb_debug_server_send_fmt(
            "{\"id\":%d,\"ok\":true,\"extend\":%d,\"level\":%d,\"cursor\":%d,"
            "\"passed\":%u,\"spawned\":%u,\"jumped\":%u,\"seek\":%u,"
            "\"stepped_over\":%u,\"scans\":%u,"
            "\"ungated\":%u,\"records\":%s,\"records_shown\":%d,"
            "\"records_truncated\":%d,\"skips\":%s,\"skips_shown\":%d,"
            "\"skips_truncated\":%d,\"skip_total\":%u}",
            id, s.spawn_extend, s.spawn_level, s.spawn_cursor,
            s.spawn_passed, s.spawn_spawned, s.spawn_jumped, s.spawn_seek,
            s.spawn_stepped, s.spawn_scans,
            s.spawn_ungated, recs, count, recs_full, ring, shown, ring_full,
            s.spawn_ring_n);
        return 1;
    }
    if (strcmp(cmd, "sml2_view")) return 0;
    gb_debug_server_send_fmt(
        "{\"id\":%d,\"valid\":%d,\"mode\":%d,\"width\":%d,\"left\":%d,\"top\":%d,"
        "\"view_left\":%d,\"camera_x\":%d,\"camera_y\":%d,\"bounds\":[%d,%d],"
        "\"score\":[%d,%d],\"attr_score\":[%d,%d],\"paint_score\":[%d,%d],"
        "\"attr_prio_diff\":%d,"
        "\"cgb\":%d,\"attr_table\":%d,"
        "\"tileset\":%d,\"sprites\":%d,\"captures\":%u,\"capture_frames\":%u,"
        "\"widened\":%u,\"dropped\":%u,\"fallbacks\":%u,"
        "\"gate_scene\":%u,\"gate_tile_fail\":%u,\"gate_attr_fail\":%u,"
        "\"reject\":\"%s\",\"flips\":%u,\"gate_frames\":%u,\"gate_log_seq\":%u,"
        "\"attr_byte_diff\":%u,\"wide\":%d,\"fail_run\":%d,"
        "\"debounced\":%u,\"narrowed\":%u,\"narrowed_model\":%u,"
        "\"pillarbox_model\":%u,\"debounce\":%d,"
        "\"overlay\":\"%s\",\"held\":%u,\"held_runs\":%u,"
        /* The palette the LAST COMPOSED frame was painted with. A hold freezes
         * the world but must never freeze this, or the margins stop following
         * a pause dim or a fade the native strip is already showing. */
        "\"used_bgp\":%u,\"used_bg_pal0\":%u,\"scroll_offpage\":%u,"
        /* The LIVE camera and scroll registers, read fresh. camera_x/camera_y
         * above belong to the composed frame, which during a hold is a frozen
         * one -- reading those and calling them "the camera right now" is how
         * this module twice talked itself into a wrong diagnosis. */
        "\"camera_live\":[%d,%d],\"scy_live\":%d,\"scx_live\":%d,"
        "\"transition_flag\":%d,"
        "\"sprite_pal_mask\":%u,\"sprite_bank1\":%u,"
        "\"spawn_extend\":%d,\"spawn_edge\":[%d,%d],\"spawn_reach\":[%d,%d],"
        "\"spawn_lag\":[%d,%d],\"spawn_reads\":[%u,%u,%u,%u],"
        "\"spawn_unpaired\":%u,\"spawn_resets\":%u,\"spawn_cursor\":%d,"
        "\"spawn_passed\":%u,\"spawn_spawned\":%u,\"spawn_jumped\":%u,"
        "\"spawn_seek\":%u,\"spawn_stepped_over\":%u,"
        "\"spawn_scans\":%u,\"spawn_ungated\":%u,\"frame\":%d}",
        id, s.valid, s.mode, gb_custom_width, s.left, s.top, s.view_left, s.cam_x,
        s.cam_y, s.bound_left, s.bound_right, s.score_hit, s.score_total,
        s.attr_hit, s.attr_total, s.paint_hit, s.score_total,
        s.attr_prio_diff, s.cgb, s.attr_ok,
        s.ctx ? peek(s.ctx, SML2_DX_TILESET) : 0, s.count,
        s.captures, s.capture_frames, s.rescues, s.ghosts, s.fallbacks,
        s.gate_scene, s.gate_tile_fail, s.gate_attr_fail,
        s.reject >= 0 && s.reject < SML2_REJ_COUNT ? k_reject_name[s.reject] : "?",
        s.flips, s.gate_frames, s.gate_log_seq, s.attr_byte_diff,
        s.wide, s.fail_run, s.debounced, s.narrowed, s.narrowed_model,
        s.pillarbox_model, SML2_FALLBACK_DEBOUNCE,
        s.overlay > 0 && s.overlay < SML2_REJ_COUNT ? k_reject_name[s.overlay] : "",
        s.held, s.held_runs,
        s.bgp, (unsigned)(s.bg_pal[0] | (s.bg_pal[1] << 8)), s.scroll_offpage,
        s.ctx ? peek16(s.ctx, SML2_CAM_X) : 0, s.ctx ? peek16(s.ctx, SML2_CAM_Y) : 0,
        s.ctx && s.ctx->ppu ? ((GBPPU *)s.ctx->ppu)->scy : 0,
        s.ctx && s.ctx->ppu ? ((GBPPU *)s.ctx->ppu)->scx : 0,
        s.ctx ? peek(s.ctx, SML2_TRANSITION) : 0,
        s.sprite_pal_mask, s.sprite_bank1,
        s.spawn_extend, s.scan_edge[SML2_SIDE_RIGHT], s.scan_edge[SML2_SIDE_LEFT],
        s.scan_reach_max[SML2_SIDE_RIGHT], s.scan_reach_max[SML2_SIDE_LEFT],
        s.scan_lag_max[SML2_SIDE_RIGHT], s.scan_lag_max[SML2_SIDE_LEFT],
        s.scan_hi[SML2_SIDE_RIGHT], s.scan_lo[SML2_SIDE_RIGHT],
        s.scan_hi[SML2_SIDE_LEFT], s.scan_lo[SML2_SIDE_LEFT],
        s.scan_unpaired, s.scan_resets, s.spawn_cursor,
        s.spawn_passed, s.spawn_spawned, s.spawn_jumped, s.spawn_seek,
        s.spawn_stepped, s.spawn_scans, s.spawn_ungated, s.frame);
    return 1;
}
