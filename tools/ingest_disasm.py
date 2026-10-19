#!/usr/bin/env python3
"""Ingest froggestspirit/marioland2 (2014 RGBDS disassembly of SML2 (UE) V1.0)
into a gb-recompiled .sym with @function/@data directives.

The disassembly predates modern RGBDS syntax and does not assemble, so we do not
assemble it: we harvest only facts that carry absolute addresses.
  * `INCBIN "baserom.gb", $start, $end - $start`  -> raw ranges, unless code-referenced
  * `Name: ;$ROMOFFSET`                            -> named symbol at a ROM offset
  * `Unknown{Call,Jump,RJump}_0xADDR:` in a banked SECTION -> code entry/label
Everything is verified against the ROM bytes where possible (CALL/JP operands)
and emitted as `BB:AAAA Name ; @function|@label|@data @size=N`.

Usage: python tools/ingest_disasm.py <disasm_dir> <rom_v10.gb> <out.sym>
"""
import re, sys
from pathlib import Path

disasm, rom_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
rom = rom_path.read_bytes()
assert len(rom) == 0x80000

def rom_to_banked(off):
    bank = off >> 14
    addr = (off & 0x3FFF) | (0x4000 if bank else 0)
    return bank, addr

syms = {}   # (bank, addr) -> (name, kind, size)
raw_ranges = []
source_branch_targets = set()
def add(bank, addr, name, kind, size=1):
    key = (bank, addr)
    old = syms.get(key)
    if old and old[1] == 'data' and kind != 'data':
        return  # data wins over speculative code labels
    if old and not old[0].startswith('Unknown') and name.startswith('Unknown'):
        return
    syms[key] = (name, kind, size)

def harvest(text, default_bank):
    bank = default_bank
    for line in text.splitlines():
        m = re.match(r'^SECTION "bank([0-9A-Fa-f]+)"', line)
        if m:
            bank = int(m.group(1), 16); continue
        branch = re.match(r'^\s*(?:jp|jr|call)\s+(?:(?:nz|z|nc|c),\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:;.*)?$', line, re.I)
        if branch:
            source_branch_targets.add((bank, branch.group(1)))
        m = re.match(r'^INCBIN "baserom\.gb",\s*\$([0-9A-Fa-f]+),\s*\$([0-9A-Fa-f]+)\s*-\s*\$([0-9A-Fa-f]+)', line)
        if m and m.group(1) == m.group(3):
            a, b = int(m.group(1), 16), int(m.group(2), 16)
            if 0 <= a < b <= len(rom):
                bk, ad = rom_to_banked(a)
                # split at bank boundary if needed
                while a < b:
                    bk, ad = rom_to_banked(a)
                    end = min(b, (bk + 1) << 14)
                    raw_ranges.append((bk, ad, end - a))
                    a = end
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*):\s*(;.*)?$', line)
        if not m:
            continue
        name, comment = m.group(1), m.group(2) or ''
        om = re.search(r';\s*\$([0-9A-Fa-f]{4,6})', comment)
        if om:
            off = int(om.group(1), 16)
            if off < len(rom):
                bk, ad = rom_to_banked(off)
                # Names are NOT evidence of kind (ScrollLevelMap, LoadMarioGFX are
                # code). Exact data extents come from INCBIN ranges only; named
                # labels are code candidates and are verified against ROM bytes
                # below (dropped if nothing calls/jumps to them).
                if name.startswith('UnknownData'):
                    pass
                elif name.startswith('UnknownRJump') or name.startswith('UnknownJump'):
                    add(bk, ad, name, 'label')
                else:
                    add(bk, ad, name, 'function')
            continue
        um = re.match(r'^Unknown(Call|Jump|RJump|Data)_0x([0-9A-Fa-f]{4})$', name)
        if um:
            kind, ad = um.group(1), int(um.group(2), 16)
            bk = 0 if ad < 0x4000 else bank
            if kind == 'Data':
                pass  # extent unknown; INCBIN ranges carry the real data map
            elif kind == 'Call':
                add(bk, ad, name, 'function')
            else:
                add(bk, ad, name, 'label')

for f in ['home.asm', 'main.asm', 'owmovementpointers.asm', 'gfx/spritemappings.asm',
          'gfx/spritemappings2.asm', 'gfx/spritemappings3.asm', 'levels/levelheaders.asm',
          'levels/enemysets.asm', 'levels/tilesets.asm']:
    p = disasm / f
    if p.exists():
        harvest(p.read_text(errors='replace'), 0)

# Verify code entries: a CALL/JP with this operand must exist in the ROM (same bank or bank 0)
def referenced(bank, addr, opcodes):
    pats = [bytes([op, addr & 0xFF, addr >> 8]) for op in opcodes]
    regions = [(0, rom[:0x4000])] + ([(bank, rom[bank << 14:(bank + 1) << 14])] if bank else [])
    if any(p in r for p in pats for _, r in regions):
        return True
    # JR/JR cc (18/20/28/30/38) with a matching relative displacement
    for rbank, r in regions:
        base = 0 if rbank == 0 else 0x4000
        for i in range(len(r) - 1):
            if r[i] in (0x18, 0x20, 0x28, 0x30, 0x38):
                d = r[i + 1]; d = d - 256 if d > 127 else d
                if base + i + 2 + d == addr:
                    return True
    return False

# INCBIN means unassembled bytes, not necessarily data. A named direct-flow
# target in the source, also verified against the ROM, disproves a data-only
# classification for its raw block. Leave that block unclassified so ordinary
# control-flow analysis follows the branch; do not seed an interior function.
unclassified = []
for bank, start, size in raw_ranges:
    conflicts = [name for (bk, addr), (name, kind, _) in syms.items()
                 if bk == bank and start <= addr < start + size and kind != 'data'
                 and any(target == name and (source_bank == bank or bank == 0)
                         for source_bank, target in source_branch_targets)
                 and referenced(bank, addr, (0xCD, 0xC4, 0xCC, 0xD4, 0xDC,
                                             0xC3, 0xC2, 0xCA, 0xD2, 0xDA))]
    if conflicts:
        unclassified.append((bank, start, size, conflicts))
        continue
    add(bank, start, f'Data_{bank:02X}_{start:04X}', 'data', size)

kept, dropped = [], []
for (bank, addr), (name, kind, size) in sorted(syms.items()):
    if kind == 'function' and not referenced(bank, addr, (0xCD, 0xC4, 0xCC, 0xD4, 0xDC, 0xC3)):
        # keep well-known entry points unconditionally
        if name not in ('Start', 'VBlank', 'Init') and not name.startswith('_'):
            dropped.append((bank, addr, name)); continue
    kept.append((bank, addr, name, kind, size))

with out.open('w', newline='\n') as fh:
    fh.write('; Generated by tools/ingest_disasm.py from froggestspirit/marioland2 (SML2 (UE) V1.0)\n')
    for bank, addr, name, kind, size in kept:
        directive = f'@{kind}' + (f' @size={size}' if kind == 'data' and size > 1 else '')
        fh.write(f'{bank:02X}:{addr:04X} {name} ; {directive}\n')

from collections import Counter
c = Counter(k[3] for k in kept)
print(f'wrote {out}: {len(kept)} symbols ({dict(c)}), data bytes covered {sum(k[4] for k in kept if k[3]=="data")}, dropped unverified code labels {len(dropped)}')
for bank, start, size, conflicts in unclassified:
    print(f'left raw block {bank:02X}:{start:04X}+{size} unclassified: source/ROM branch targets {", ".join(conflicts)}')
