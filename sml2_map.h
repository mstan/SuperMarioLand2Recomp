/*
 * sml2_map.h -- host clone of Super Mario Land 2's block-map decode.
 *
 * Every constant here was verified against the UE V1.0 ROM (CRC32 0xD5EC24E4)
 * -- and against the SML2 DX v1.8.1 image the DX body runs, where all of them
 * are at the same address with the same bytes -- then re-confirmed against live
 * RAM through the TCP debug server: decoding the scored 21x17 tile grid out of
 * the block map reproduced the game's own BG tilemap 357/357 cells on both
 * bodies, and on the DX body the derived CGB attributes matched VRAM bank 1
 * 357/357 as well.
 *
 *   block map     MEM[$B000 + ((worldY>>4)&0xFF)*0x100 + ((worldX>>4)&0xFF)]
 *                 $B000-$BFFF is cart SRAM, $C000-$DFFF is WRAM; the level is
 *                 decompressed there once at load (ROM0 $0361/$0386) and is
 *                 mutated live (broken blocks, collected coins), so a host
 *                 renderer must read it every frame.
 *   block defs    MEM[$A600 + id*4 + q], q = ((wy>>3)&1)*2 + ((wx>>3)&1)
 *                 order TL, TR, BL, BR; four raw 8x8 tile indices, no
 *                 attribute byte (DMG). Loaded from bank 8 at level start.
 *   tile pixels   LCDC bit 4 is 0 during gameplay, so the index is signed:
 *                 VRAM address = 0x9000 + (int8_t)tile * 16.
 */
#pragma once
#include <stdint.h>

#define SML2_MAP_BASE      0xB000u   /* first byte of the decompressed level  */
#define SML2_MAP_END       0xE000u
#define SML2_MAP_SIZE      (SML2_MAP_END - SML2_MAP_BASE)   /* 0x3000        */
#define SML2_MAP_STRIDE    0x100u    /* block columns per block row           */
#define SML2_MAP_COLS      256       /* 4096 world pixels                     */
#define SML2_MAP_ROWS      48        /*  768 world pixels                     */
/* The level is decompressed with SVBK 0/1, so its $D000-$DFFF half -- block
 * rows 32..47 -- is WRAM bank 1. That has to be named explicitly rather than
 * read through the live SVBK: on the DX body the attribute half of the VRAM
 * queue drain parks SVBK on 2 (24:79FE sets it, 24:7A32 restores it) and can
 * still be running when the host takes its per-frame snapshot, in which case
 * the same addresses hand back the $D000 attribute table instead of the level.
 * Measured before the fix: 1213 of 1214 tile-gate rejections and 226 of 226
 * block-id rejections over a 16k-frame attract run had wram_bank == 2. */
#define SML2_LEVEL_WRAM_BANK 1
/* Cart SRAM bank the level's $A000-$BFFF state lives in. Observed 0 on every
 * scored frame of that run; the compositor refuses the frame rather than
 * decode through a different one. */
#define SML2_LEVEL_RAM_BANK  0
#define SML2_BLOCKDEF_BASE 0xA600u   /* 128 entries x 4 tiles                 */
#define SML2_BLOCKDEF_SIZE 0x200u
#define SML2_MAX_BLOCK_ID  0x7Fu     /* bit 7 is the level RLE flag           */

/* Camera is the screen CENTRE (ROM0 SetScroll $2062 on V1.0 and DX -- $2065 on
 * the V1.2 this project no longer targets -- writes SCX = camX-80 and
 * SCY = camY-72-shake), 16-bit little endian in HRAM. */
#define SML2_CAM_Y         0xFFC8u
#define SML2_CAM_X         0xFFCAu
#define SML2_MARIO_Y       0xFFC0u
#define SML2_MARIO_X       0xFFC2u
#define SML2_CAM_CENTRE_X  80
#define SML2_CAM_CENTRE_Y  72

/* 16x3 scroll boxes of 16x16 blocks, copied to RAM at level load from the map
 * bank (ROM0 $0424). Nibble is %BTLR; a set bit means that edge is closed. */
#define SML2_SCROLLBOX     0xA960u
#define SML2_SCROLLBOX_LEN 0x30u
#define SML2_BOX_RIGHT     0x01u
#define SML2_BOX_LEFT      0x02u
#define SML2_BOX_TOP       0x04u
#define SML2_BOX_BOTTOM    0x08u
#define SML2_SCREEN_PX     256       /* one scroll box in world pixels        */

/* Level header copy (ROM0 $0361 region); byte 0x0A is the level number. */
#define SML2_HEADER        0xA800u
#define SML2_LEVEL_BANK    0xA258u

/* ---- DX v1.8.1: BG attribute per TILE INDEX ------------------------------
 *
 * The hack colours the background without touching the level format. Both
 * images share the same block map, the same $A600 block definitions and the
 * same VRAM write queue at $AA00 (6-byte records: dest lo, dest hi, then the
 * four tile indices of one 16x16 block, terminated by dest hi == 0). V1.0
 * drains that queue at ROM0 $0AFB. DX replaces those 15 bytes with
 *
 *     00:0AFB  3E 24        ld a,$24
 *              EA 00 21     ld [$2100],a      ; MBC5 ROMB0 -> bank $24
 *              CD B5 79     call $79B5
 *              FA 4E A2     ld a,[$A24E]      ; restore the caller's bank
 *              EA 00 21     ld [$2100],a
 *              C9           ret
 *
 * and the extended drain at 24:79B5 writes the four tiles to the tilemap in
 * VRAM bank 0 exactly as before, then rewinds and writes the SAME four cells
 * in VRAM bank 1 with
 *
 *     24:79F6  06 D0        ld b,$D0
 *     24:79F9  21 4F FF / CB C6   set VBK = 1
 *     24:79FE  3E 02 / EA 70 FF   set SVBK = 2
 *     24:7A0A  1A 4F 0A 22  ld a,[de] / ld c,a / ld a,[bc] / ld [hl+],a
 *
 * i.e. attribute = MEM_WRAM_BANK2[$D000 + tile_index]. That is the whole
 * model: a flat 256-entry tile-index -> BG attribute byte table, and the
 * ordinary CGB attribute encoding (bits 0-2 palette, bit 3 tile VRAM bank,
 * bit 5 X flip, bit 6 Y flip, bit 7 BG-over-OBJ priority).
 *
 * The table is per TILESET, loaded by 21:730E / 21:732C as
 *
 *     a = [$A269]                       ; tileset index, the same selector the
 *     hl = $4000 + a*$0100              ; DX draw-bank dispatcher at 00:07EA
 *     de = $D000 ; bc = $0100           ; reads
 *     call $0336                        ; memcpy, with SVBK = 2
 *
 * from ROM bank $21, and re-loaded whenever the tileset changes, so reading
 * the live WRAM copy every frame follows mid-level changes for free.
 *
 * $D000 in WRAM bank 2 is NOT the block map: the level's own $D000-$DFFF is
 * WRAM bank 1 (the game runs with SVBK 0/1). Same address, different bank.
 */
#define SML2_DX_ATTR_BASE  0xD000u   /* WRAM bank 2, 256 entries              */
#define SML2_DX_ATTR_BANK  2
#define SML2_DX_ATTR_SIZE  0x100u
#define SML2_DX_TILESET    0xA269u   /* tileset index the table was built for  */

/* CGB BG attribute bits (mirrors ppu.c render_bg_segment). */
#define SML2_ATTR_PALETTE  0x07u
#define SML2_ATTR_BANK     0x08u
#define SML2_ATTR_FLIP_X   0x20u
#define SML2_ATTR_FLIP_Y   0x40u
#define SML2_ATTR_PRIORITY 0x80u

/* ---- the generic sprite emitter, and Mario's fireballs -------------------
 *
 * The $AD00 actor draw routine is not the only thing that puts sprites on the
 * screen. Mario, his fireballs, enemy fire and thrown items all go through one
 * shared emitter, reached from the ROM0 trampoline $2CF4:
 *
 *     00:2CF4  3E 01 / EA 4E A2 / EA 00 21   ld a,1 -> $A24E, ROMB0
 *              C3 97 52                      jp $5297  -> bank 1
 *
 * whose body decodes a metasprite in exactly the actor format -- four bytes
 * per piece (Yoff, Xoff, tile, attr), $80 in Yoff terminates:
 *
 *     01:529F  F0 C6        ldh a,[$FFC6]     ; metasprite index
 *     01:52A8  21 00 40     ld hl,$4000       ; table in bank 1, 2 bytes/entry
 *     01:52B0  26 A1 / F0 8D ld h,$A1 / ldh a,[$FF8D]  ; OAM shadow cursor
 *     01:52B5  F0 C4        ldh a,[$FFC4]     ; screen Y  -> b
 *     01:52B8  F0 C5        ldh a,[$FFC5]     ; screen X  -> c
 *     01:52BB  1A / FE 80   ld a,[de] / cp $80
 *     01:52C0  80 / 22      add a,b / ld [hl+],a      ; OAM Y = screenY + Yoff
 *     01:52C4  81 / 22      add a,c / ld [hl+],a      ; OAM X = screenX + Xoff
 *
 * Both screen coordinates are EIGHT BIT, so anything the emitter draws is
 * confined to the native 160 columns however wide the composed view is. That
 * is why Fire Mario's fireballs vanish partway across a wide frame.
 *
 * Fireballs additionally keep their own table, and it holds the real 16-bit
 * world position, so their pieces need no unwrapping at all:
 *
 *     00:32C1  spawn: hKeysPressed bit 1, sCurPowerup == $03, $A291/$A24F clear
 *              ld hl,$A880 ; two slots, stride $10, scanned until l == $A0
 *              +0 active, +1/+2 world Y (LE), +3/+4 world X (LE), +5 direction
 *     00:3261  per-slot update and draw; screenX = X - [$FFCA] + 80 -> $FFC5,
 *              screenY = Y - [$FFC8] + 70 -> $FFC4, then `and $F0 / cp $C0`
 *              on each: landing in $C0..$CF DESTROYS the fireball.
 *
 * Measured on recomp/fixtures/dx_under_pipe_repro.state1 with sCurPowerup
 * forced to 3: the fireball travels 3 px per frame, reaches screen X 189, and
 * is gone the next frame -- it cannot step over the 16-wide despawn window. So
 * vanilla destroys it at worldX = camX + 112, exactly 32 px past the right
 * edge of the native screen.
 */
/* The emitter body exists in several copies, and DX relocates it exactly as it
 * relocates the actor draw routine. Searching both images for the six bytes
 * `F0 C4 47 F0 C5 4F` (ldh a,[$FFC4] / ld b,a / ldh a,[$FFC5] / ld c,a) finds
 *
 *     V1.0   01:52B5, 01:5E58
 *     DX     01:52B5, 01:5E58, 2C:5D86, 2D:5E3A
 *
 * and DX reaches its copies through 01:5297 -> 01:465A, which far-calls bank
 * $2D or $2C depending on [$FFF6] & $0F. So the tap keys on the PC, not on a
 * bank: the CPU is inside the emitter when it fires, so ctx->rom_bank IS the
 * emitter's bank, and the metasprite pointer table at $4000 is read out of it.
 * These are the instructions AFTER `ldh a,[$FFC5]`, which is where the
 * generated code leaves PC. */
#define SML2_EMIT_PCS       { 0x52BAu, 0x5E5Du, 0x5D8Bu, 0x5E3Fu }
#define SML2_EMIT_SCREEN_Y  0xFFC4u
#define SML2_EMIT_SCREEN_X  0xFFC5u
#define SML2_EMIT_INDEX     0xFFC6u
#define SML2_EMIT_PALETTE   0xFFC7u
#define SML2_EMIT_TABLE     0x4000u  /* metasprite pointers, bank 1          */
/* Screen X is 8-bit. Values at or above this are the ROM's way of saying "left
 * of the screen": the despawn window sits at $C0..$CF, so nothing legitimately
 * lives between $C0 and here. */
#define SML2_EMIT_X_NEGATIVE 0xD0

#define SML2_FIREBALL       0xA880u
#define SML2_FIREBALL_STRIDE 0x10u
#define SML2_FIREBALL_SLOTS 2
#define SML2_FIREBALL_DESPAWN 0xC0u  /* `and $F0 / cp $C0` at 00:3261        */
#define SML2_FIREBALL_OVERSHOOT 32   /* how far past the edge vanilla lets it go */

/* Status bar: one 8px window row at the bottom, window map $9C00, WX = 7. */
#define SML2_HUD_WY        136
#define SML2_HUD_WX        7
#define SML2_HUD_COLS      20
#define SML2_HUD_SPLIT     15   /* cols 0..14 hug the left, 15..19 the right  */
#define SML2_HUD_FILL_COL  4    /* blank separator tile used to pad the gap   */
