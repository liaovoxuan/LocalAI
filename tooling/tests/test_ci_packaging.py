import contextlib
import importlib.util
import io
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tooling import package


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CIPackagingTests(unittest.TestCase):
    def test_all_product_specs_reference_existing_resources(self):
        def analysis(scripts, **kwargs):
            for source, destination in kwargs.get("datas", []):
                self.assertTrue(Path(source).exists(), f"Missing packaged resource: {source}")
            return SimpleNamespace(pure=[], scripts=scripts, binaries=[], datas=[])

        namespace = {"Analysis": analysis}
        for name in ("PYZ", "EXE", "COLLECT", "BUNDLE"):
            namespace[name] = lambda *args, **kwargs: SimpleNamespace()
        for product in package.PROJECTS.values():
            for platform in ("windows", "linux", "macos"):
                spec = package.spec_for(product, platform)
                with self.subTest(spec=spec):
                    runpy.run_path(str(spec), init_globals={**namespace, "SPECPATH": str(spec.parent)})

    def test_cpu_fallback_resets_accelerators_in_existing_cmake_cache(self):
        builder = load_module("llamacpp_ci_builder", ROOT / "tooling/scripts/build_llamacpp_runtime.py")
        cache = {"GGML_VULKAN": "ON", "GGML_METAL": "ON", "GGML_CUDA": "ON"}
        configurations = []

        def run(command, cwd=None):
            if "-S" not in command:
                return
            for value in command:
                if isinstance(value, str) and value.startswith("-D") and "=" in value:
                    key, option = value[2:].split("=", 1)
                    cache[key] = option
            configurations.append(dict(cache))
            if cache.get("GGML_VULKAN") == "ON":
                raise subprocess.CalledProcessError(1, command)
            for key in ("GGML_VULKAN", "GGML_BLAS", "GGML_METAL", "GGML_CUDA"):
                self.assertEqual(cache.get(key), "OFF", f"Fallback retained cached {key}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "llama-cli"
            binary.write_bytes(b"test runtime")
            with mock.patch.object(builder, "ROOT", root), \
                 mock.patch.object(builder, "RUNTIME", root / "runtime"), \
                 mock.patch.object(builder, "ensure_source"), \
                 mock.patch.object(builder, "run", side_effect=run), \
                 mock.patch.object(builder, "find_built_binary", side_effect=lambda directory, name: binary if name == "llama-cli" else None), \
                 mock.patch.dict(os.environ, {"LOCALAI_TARGET_OS": "linux", "LOCALAI_TARGET_ARCH": "x64"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                builder.main()
            self.assertEqual(len(configurations), 2)
            self.assertEqual((root / "runtime/linux/x64/bin/llama-cli").read_bytes(), binary.read_bytes())

    def test_archive_scanner_rejects_a_failed_object_scan(self):
        scanner = load_module("minivmac_ci_scan", ROOT / "projects/VirtualWorld/third_party/minivmac/inprocess/scan_spawn_apis.py")
        with mock.patch("sys.argv", ["scan_spawn_apis.py", "--archive", "production.lib"]), \
             mock.patch.object(scanner, "scan_sources", return_value=[]), \
             mock.patch.object(scanner, "scan_objects", return_value=([], "object scanner failed (exit 1)")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scanner.main(), 1)


if __name__ == "__main__":
    unittest.main()
