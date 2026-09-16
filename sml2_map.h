/*
 * sml2_map.h -- host clone of Super Mario Land 2's block-map decode.
 *
 * Every constant here was verified against the UE V1.2 ROM (CRC32 0x635A9112)
 * and re-confirmed against live RAM through the TCP debug server: decoding the
 * visible 21x18 tile grid out of the block map reproduced the game's own BG
 * tilemap 378/378 cells on four independent gameplay frames.
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
#define SML2_BLOCKDEF_BASE 0xA600u   /* 128 entries x 4 tiles                 */
#define SML2_BLOCKDEF_SIZE 0x200u
#define SML2_MAX_BLOCK_ID  0x7Fu     /* bit 7 is the level RLE flag           */

/* Camera is the screen CENTRE (ROM0 SetScroll $2065 writes SCX = camX-80 and
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

/* Status bar: one 8px window row at the bottom, window map $9C00, WX = 7. */
#define SML2_HUD_WY        136
#define SML2_HUD_WX        7
#define SML2_HUD_COLS      20
#define SML2_HUD_SPLIT     15   /* cols 0..14 hug the left, 15..19 the right  */
#define SML2_HUD_FILL_COL  4    /* blank separator tile used to pad the gap   */
