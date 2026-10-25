import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package_cloudai_linux.py"
SPEC = importlib.util.spec_from_file_location("cloudai_linux_packaging", SCRIPT)
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)


def write_elf(path, machine, elf_class=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + bytes([elf_class, 1]) + b"\0" * 12 + struct.pack("<H", machine))


class CloudAILinuxPackagingTests(unittest.TestCase):
    def make_bundle(self, root, machine):
        for name in ("CloudAI", "_internal/libpython3.12.so.1.0",
                     "_internal/python3.12/lib-dynload/_tkinter.so", "_internal/PIL/_imaging.so"):
            write_elf(root / name, machine)

    def test_all_native_libraries_must_match_target(self):
        for arch, (machine, _) in packaging.ARCHES.items():
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.make_bundle(root, machine)
                self.assertEqual(packaging.verify_bundle(root, arch), 4)
                write_elf(root / "_internal/PIL/accidentally-copied-x86.so", 62)
                with self.assertRaisesRegex(ValueError, "Wrong ELF architecture"):
                    packaging.verify_bundle(root, arch)

    def test_rejects_wrong_bootloader_and_missing_tk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_bundle(root, 183)
            with self.assertRaisesRegex(ValueError, "does not match riscv64"):
                packaging.verify_bundle(root, "riscv64")
            (root / "_internal/python3.12/lib-dynload/_tkinter.so").unlink()
            with self.assertRaisesRegex(ValueError, "Missing native runtime"):
                packaging.verify_bundle(root, "arm64")

    def test_rejects_32_bit_and_truncated_elf(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "binary"
            write_elf(path, 183, elf_class=1)
            with self.assertRaisesRegex(ValueError, "64-bit ELF"):
                packaging.elf_machine(path)
            path.write_bytes(b"\x7fELF")
            with self.assertRaises(ValueError):
                packaging.elf_machine(path)

    def test_smoke_test_must_be_frozen_and_match_target(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "smoke-test.json"
            valid = {"arch": "arm64", "frozen": True, "status": "ok"}
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(packaging.verify_smoke(path, "arm64"), valid)
            for patch in ({"arch": "riscv64"}, {"frozen": False}, {"status": "failed"}):
                path.write_text(json.dumps({**valid, **patch}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    packaging.verify_smoke(path, "arm64")


if __name__ == "__main__":
    unittest.main()
