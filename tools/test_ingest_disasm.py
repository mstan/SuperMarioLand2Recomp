"""A source/ROM-proved branch must not become an INCBIN data annotation."""
from pathlib import Path
import subprocess,sys,tempfile,unittest

class RawCodeImport(unittest.TestCase):
    def test_code_alias_and_unreferenced_raw_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);rom=bytearray(0x80000)
            rom[0x150:0x153]=bytes((0xC2,0x00,0x02));rom[0x200]=0xC9
            rom[0x180:0x183]=bytes((0xC3,0x00,0x04))
            (root/'rom.gb').write_bytes(rom)
            (root/'home.asm').write_text('''SECTION "bank0", ROM0[$0]
Start: ;$0150
    jp nz, UnknownJump_0x0200
    jp nz, UnknownJump_0x0500
UnknownJump_0x0200:
UnknownData_0x0200:
INCBIN "baserom.gb", $0200, $020C - $0200
UnknownJump_0x0300:
UnknownData_0x0300:
INCBIN "baserom.gb", $0300, $030C - $0300
UnknownJump_0x0400:
UnknownData_0x0400:
INCBIN "baserom.gb", $0400, $040C - $0400
UnknownJump_0x0500:
UnknownData_0x0500:
INCBIN "baserom.gb", $0500, $050C - $0500
''')
            subprocess.run([sys.executable,str(Path(__file__).with_name('ingest_disasm.py')),
                            str(root),str(root/'rom.gb'),str(root/'out.sym')],check=True,capture_output=True)
            symbols=(root/'out.sym').read_text()
            self.assertIn('00:0200 UnknownJump_0x0200 ; @label',symbols)
            self.assertNotIn('Data_00_0200',symbols)
            self.assertIn('00:0300 Data_00_0300 ; @data @size=12',symbols)
            self.assertIn('00:0400 Data_00_0400 ; @data @size=12',symbols)
            self.assertIn('00:0500 Data_00_0500 ; @data @size=12',symbols)

if __name__=='__main__':unittest.main()
