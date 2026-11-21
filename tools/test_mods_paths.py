"""Exercise the production mod provider with separate state/assets/library paths.

No ROM, firmware, display or game process is needed. The asset marker tests
file discovery only; actual BPS validation remains a game-runtime check.
"""
import argparse
import configparser
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
parser = argparse.ArgumentParser()
parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
parser.add_argument("--source", type=Path, default=ROOT / "sml2_mods.c")
args, remaining = parser.parse_known_args()


class ModPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="sml2-mod-paths-")
        cls.exe = Path(cls.build.name) / ("fixture.exe" if os.name == "nt" else "fixture")
        compiler = shutil.which(args.cc) or args.cc
        include = [ROOT, ROOT / "gb-recompiled/runtime/include", ROOT / "recomp-ui/src"]
        sdl = Path(compiler).resolve().parent.parent / "include/SDL2"
        if not sdl.is_dir():
            sdl = Path("/usr/include/SDL2")
        include.append(sdl)
        subprocess.run([compiler, "-std=c11", "-ffunction-sections", "-fdata-sections",
                        "-DDECLSPEC=", *["-I" + str(p) for p in include],
                        str(ROOT / "tools/mods_paths_fixture.c"), str(args.source),
                        str(ROOT / "gb-recompiled/runtime/src/gb_host_paths.c"),
                        "-Wl,--gc-sections", "-o", str(cls.exe)], check=True,
                       creationflags=HIDDEN)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sml2-private-state-")
        self.root = Path(self.tmp.name)
        self.state = self.root / "private state"
        self.assets = self.root / "payload"
        self.state.mkdir()
        self.assets.mkdir()
        (self.assets / "sml2dx_v181.bps").write_bytes(b"discovery-test-only")
        self.env = dict(os.environ, GBRECOMP_STATE_DIR=str(self.state),
                        GBRECOMP_ASSET_DIR=str(self.assets),
                        SML2_TEST_BASE_PATH=str(self.root / "nonwritable-library") + "/")
        for key in ("SML2_WIDESCREEN", "SML2_DX", "SML2_SPAWNS"):
            self.env.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_provider(self, mode, wide, width, dx, spawns):
        result = subprocess.run([str(self.exe), mode, str(wide), str(width), str(dx),
                                 str(spawns)], cwd=self.assets, env=self.env,
                                capture_output=True, text=True,
                                creationflags=HIDDEN)
        self.assertEqual(result.returncode, 0, result.stderr)

    def seed(self):
        text = "[Mods]\nAdaptiveWidescreen=1\nWidth=336\nDX=1\nSpawns=0\n"
        (self.state / "sml2-mods.ini").write_text(text)
        return text

    def test_first_play_writes_private_state(self):
        self.run_provider("write", 1, 336, 0, 1)
        self.assertTrue((self.state / "sml2-mods.ini").is_file())
        self.assertFalse((self.assets / "sml2-mods.ini").exists())

    def test_fresh_defaults_enable_fit(self):
        self.run_provider("read", 1, -1, 0, 1)

    def test_android_null_base_uses_private_state(self):
        self.env["SML2_TEST_NULL_BASE"] = "1"
        self.run_provider("write", 1, 336, 0, 1)
        self.assertTrue((self.state / "sml2-mods.ini").is_file())
        self.assertFalse((self.assets / "sml2-mods.ini").exists())

    def test_branding_reads_choices_and_assets_before_provider(self):
        self.seed()
        self.run_provider("read", 1, 336, 1, 0)

    def test_explicit_off_and_aspect_survive_a_new_process(self):
        self.seed()
        self.run_provider("write", 0, 512, 0, 1)
        self.run_provider("read", 0, 512, 0, 1)
        parser = configparser.ConfigParser()
        parser.read(self.state / "sml2-mods.ini")
        self.assertEqual(parser["Mods"]["width"], "512")

    def test_failed_commit_preserves_previous_settings(self):
        before = self.seed()
        (self.state / "sml2-mods.ini.tmp").mkdir()
        self.run_provider("fail", 0, 512, 0, 1)
        self.assertEqual((self.state / "sml2-mods.ini").read_text(), before)


if __name__ == "__main__":
    unittest.main(argv=[__file__, *remaining])
