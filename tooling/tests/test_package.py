import json
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tooling import package


class PackageToolTests(unittest.TestCase):
    def test_project_specs_exist(self):
        for project in package.PROJECTS.values():
            for target in ("macos", "windows", "linux"):
                self.assertTrue(package.spec_for(project, target).is_file())

    def test_host_target_is_normalized(self):
        with patch.object(platform, "system", return_value="Darwin"):
            self.assertEqual(package.host_target(), "macos")
        with patch.object(platform, "system", return_value="Windows"):
            self.assertEqual(package.host_target(), "windows")
        with patch.object(platform, "system", return_value="Linux"):
            self.assertEqual(package.host_target(), "linux")

    def test_verify_writes_manifest_and_hash(self):
        project = package.PROJECTS["localai"]
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp) / "dist"
            dist.mkdir()
            artifact = dist / "LocalAI"
            artifact.mkdir()
            (artifact / "LocalAI").write_bytes(b"package payload")
            with patch.object(package, "host_arch", return_value="arm64"):
                result = package.verify_artifact(project, "linux", dist)
            self.assertEqual(result, artifact)
            manifest = json.loads((dist / "artifact-manifest.json").read_text())
            self.assertEqual(manifest["project"], "localai")
            self.assertEqual(manifest["bytes"], len(b"package payload"))
            self.assertEqual(len(manifest["sha256"]), 64)

    def test_cross_platform_build_is_rejected(self):
        with patch.object(package, "host_target", return_value="macos"):
            with self.assertRaises(package.PackageError):
                package.check_target("windows", "arm64")

    def test_backend_auto_uses_wine_for_windows(self):
        with patch.object(package, "host_target", return_value="macos"), patch.object(
            package, "command_available", side_effect=lambda name: name == "wine"
        ):
            self.assertEqual(package.resolve_backend("windows", "auto"), "wine")

    def test_backend_auto_uses_container_for_linux(self):
        with patch.object(package, "host_target", return_value="darwin"), patch.object(
            package, "command_available", side_effect=lambda name: name == "docker"
        ):
            self.assertEqual(package.resolve_backend("linux", "auto"), "docker")

    def test_native_build_requires_tkinter(self):
        with patch.object(package, "tkinter_available", return_value=False):
            with self.assertRaisesRegex(package.PackageError, "Tkinter"):
                package.require_tkinter()

    def test_verify_detects_artifact_changed_since_manifest(self):
        project = package.PROJECTS["localai"]
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            artifact = dist / "LocalAI"
            artifact.mkdir()
            executable = artifact / "LocalAI"
            executable.write_bytes(b"original")
            package.verify_artifact(project, "linux", dist)
            executable.write_bytes(b"modified")
            with self.assertRaisesRegex(package.PackageError, "differs from its manifest"):
                package.verify_artifact(project, "linux", dist, check_existing=True)


if __name__ == "__main__":
    unittest.main()
