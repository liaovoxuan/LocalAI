"""Cross-platform packaging contracts for the LocalAI desktop release.

These tests never build a bundle and never edit a spec.  Each spec is *executed*
against stubbed PyInstaller symbols (``Analysis``/``PYZ``/``EXE``/``COLLECT``/``BUNDLE``),
so the real path arithmetic and argument lists are evaluated exactly as PyInstaller
would see them, while nothing is written anywhere.

Contract families:

1. every spec targets the canonical desktop entrypoint with repository-relative paths
2. the five merged cloud modules ship through normal transitive analysis
3. the collected payload contains no user data, vault, log, test, cache or secret
4. product name / windowed mode / onefile-vs-onedir / bundle id are consistent
5. source and frozen (``sys._MEIPASS``) resource lookup agree on the same destinations
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: Test-only indirection: pointed at a throw-away tree copied from the repository so
#: the sandbox mutation checks can vary a spec and prove each contract can fail.
#: Unset for normal runs, which then audit the real repository.
SANDBOX_ROOT = os.environ.get("LOCALAI_PACKAGING_ROOT")
USING_SANDBOX_ROOT = bool(SANDBOX_ROOT)

REPO_ROOT = Path(SANDBOX_ROOT).resolve() if SANDBOX_ROOT else Path(__file__).resolve().parents[2]
LOCALAI_DIR = REPO_ROOT / "projects" / "LocalAI"
PACKAGING_DIR = LOCALAI_DIR / "packaging"
DESKTOP_DIR = LOCALAI_DIR / "desktop"
ENTRYPOINT = (DESKTOP_DIR / "local_ai.py").resolve()
HOST_PATH = ENTRYPOINT

if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import local_ai  # noqa: E402  (path bootstrap must precede the import)

SPEC_PATHS = sorted(PACKAGING_DIR.glob("*.spec"))
CLOUD_MODULES = ("cloudattachments", "clouderrors", "cloudproviders", "cloudsecrets", "cloudusage")

#: target -> spec filename, as declared by tooling/package.py's PROJECTS["localai"].
DRIVER_SPECS = {
    "windows": "LocalAI.spec",
    "linux": "LocalAI_Linux.spec",
    "macos": "LocalAI_macOS.spec",
}

#: Resource the app resolves through ``resource_path`` at runtime.
REQUIRED_ICON_PNGS = ("assets/icons/localai_dark.png", "assets/icons/localai_light.png")
REQUIRED_DOCUMENT = "Readme.docx"

#: Bundled payload that is a shipped static resource, not a user file.
ALLOWED_STATIC_RESOURCES = {
    "shared/resources/config.json",
    "shared/resources/version.json",
    "projects/LocalAI/resources/Readme.docx",
}

FORBIDDEN_PAYLOAD_PARTS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules",
    "chats", "exports", "logs", "model_backups", "artifacts",
}
FORBIDDEN_PAYLOAD_NAMES = {
    "cloudai_secrets.json", "cloudai_config.json", "error.log",
    ".legacy_migrated", "search_cache.db", "localai_knowledge.db",
    "codex-insiders-api-key.conf",
}
FORBIDDEN_PAYLOAD_SUFFIXES = {".pyc", ".pyo", ".log", ".spec"}


def module_level_imports(path: Path) -> set[str]:
    """Import roots declared at module scope (what PyInstaller follows transitively)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class _Call:
    """One stubbed PyInstaller invocation."""

    def __init__(self, kind, args, kwargs):
        self.kind = kind
        self.args = args
        self.kwargs = kwargs


class _AnalysisResult:
    """Stand-in for PyInstaller's ``Analysis`` return value.

    The specs read ``.pure`` / ``.scripts`` / ``.binaries`` / ``.datas`` from it and
    hand them to ``PYZ``/``EXE``/``COLLECT``, so those attributes must exist.
    """

    def __init__(self, scripts, kwargs):
        self.scripts = scripts
        self.kwargs = kwargs
        self.pure = object()
        self.binaries: list = []
        self.datas: list = []
        self.zipfiles: list = []


class SpecRun:
    """Execute one spec with stubbed PyInstaller symbols and capture the calls."""

    def __init__(self, path: Path):
        self.path = path
        self.calls: list[_Call] = []

        def record(kind):
            def factory(*args, **kwargs):
                call = _Call(kind, args, kwargs)
                self.calls.append(call)
                return call
            return factory

        def analysis(scripts, **kwargs):
            result = _AnalysisResult(scripts, kwargs)
            self.calls.append(_Call("Analysis", (scripts,), kwargs))
            return result

        namespace = {
            "SPECPATH": str(path.parent),
            "__file__": str(path),
            "os": os,
            "Path": Path,
            "Analysis": analysis,
            "PYZ": record("PYZ"),
            "EXE": record("EXE"),
            "COLLECT": record("COLLECT"),
            "BUNDLE": record("BUNDLE"),
        }
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
        self.namespace = namespace

    # -- captured pieces --------------------------------------------------
    def _first(self, kind):
        for call in self.calls:
            if call.kind == kind:
                return call
        return None

    @property
    def analysis(self):
        return self._first("Analysis")

    @property
    def exe(self):
        return self._first("EXE")

    @property
    def collect(self):
        return self._first("COLLECT")

    @property
    def bundle(self):
        return self._first("BUNDLE")

    @property
    def source(self) -> Path:
        return Path(self.namespace["SOURCE"]).resolve()

    @property
    def pathex(self) -> list[Path]:
        return [Path(entry).resolve() for entry in self.analysis.kwargs["pathex"]]

    @property
    def hiddenimports(self) -> set[str]:
        return set(self.analysis.kwargs["hiddenimports"])

    @property
    def datas(self) -> list[tuple[str, str]]:
        return list(self.analysis.kwargs["datas"])

    @property
    def is_onefile(self) -> bool:
        return not self.exe.kwargs.get("exclude_binaries", False)


def spec_runs():
    return {path.name: SpecRun(path) for path in SPEC_PATHS}


class PackagingContractCase(unittest.TestCase):
    """Base case that snapshots the specs once per test class run."""

    @classmethod
    def setUpClass(cls):
        cls.runs = spec_runs()
        cls.host_module_imports = module_level_imports(HOST_PATH)

    def all_runs(self):
        return sorted(self.runs.items())

    def in_repo(self, path: Path) -> bool:
        return path.resolve().is_relative_to(REPO_ROOT)


# --------------------------------------------------------------------------- #
# 1. entrypoint and path hygiene
# --------------------------------------------------------------------------- #
class SpecEntrypointAndPathTests(PackagingContractCase):
    def test_every_localai_spec_exists_and_is_audited(self):
        found = {path.name for path in SPEC_PATHS}
        self.assertEqual(found, {"LocalAI.spec", "LocalAI_Linux.spec", "LocalAI_macOS.spec",
                                 "local_ai.spec"}, f"unexpected spec set: {sorted(found)}")

    def test_every_spec_targets_the_canonical_desktop_entrypoint(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertEqual(run.source, ENTRYPOINT)
                self.assertEqual(Path(run.analysis.args[0][0]).resolve(), ENTRYPOINT)

    def test_packaging_driver_maps_the_audited_specs(self):
        from tooling import package
        project = package.PROJECTS["localai"]
        for target, filename in DRIVER_SPECS.items():
            with self.subTest(target=target):
                self.assertEqual(project.specs[target], filename)
                self.assertEqual(PACKAGING_DIR / filename, PACKAGING_DIR / project.specs[target])
        self.assertNotIn("local_ai.spec", set(project.specs.values()),
                         "local_ai.spec is not referenced by the packaging driver")
        if not USING_SANDBOX_ROOT:
            # tooling/package.py resolves against its own checkout, so only cross-check
            # the filesystem when we are auditing the real repository.
            for target, filename in DRIVER_SPECS.items():
                self.assertEqual(package.spec_for(project, target), PACKAGING_DIR / filename)

    def test_all_spec_paths_stay_inside_the_repository(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertTrue(self.in_repo(run.source))
                self.assertTrue(self.in_repo(run.namespace["SHARED_DIR"]))
                for entry in run.pathex:
                    self.assertTrue(self.in_repo(entry), f"{name}: pathex escaped the repo: {entry}")
                for source, _dest in run.datas:
                    self.assertTrue(self.in_repo(Path(source)),
                                    f"{name}: datas source escaped the repo: {source}")
                icon = run.exe.kwargs.get("icon")
                if icon:
                    self.assertTrue(self.in_repo(Path(icon)), f"{name}: icon escaped the repo: {icon}")

    def test_specs_declare_no_absolute_developer_machine_paths(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for literal in string_literals(run.path):
                    self.assertFalse(literal.startswith("/"),
                                     f"{name}: absolute POSIX path literal {literal!r}")
                    self.assertFalse(
                        len(literal) > 2 and literal[0].isalpha() and literal[1] == ":" and literal[2] in "\\/",
                        f"{name}: absolute Windows path literal {literal!r}")
                    for marker in ("/Users/", "/home/", "\\Users\\", "liaoboxuan"):
                        self.assertNotIn(marker, literal, f"{name}: developer path {literal!r}")

    def test_pathex_covers_the_desktop_package_directory(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertEqual(run.pathex, [DESKTOP_DIR],
                                 f"{name}: the analysis path must be exactly the desktop dir")

    def test_every_referenced_resource_exists_for_every_platform(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for source, dest in run.datas:
                    self.assertEqual(dest, dest.strip(), f"{name}: padded datas destination")
                    self.assertFalse(Path(dest).is_absolute(),
                                     f"{name}: absolute datas destination {dest!r}")
                    self.assertNotIn("..", Path(dest).parts,
                                     f"{name}: datas destination escapes the bundle: {dest!r}")
                    self.assertTrue(Path(source).exists(), f"{name}: missing resource {source}")
                icon = run.exe.kwargs.get("icon")
                if icon:
                    self.assertTrue(Path(icon).is_file(), f"{name}: missing icon {icon}")
                bundle = run.bundle
                if bundle is not None:
                    bundle_icon = bundle.kwargs.get("icon")
                    self.assertTrue(bundle_icon, f"{name}: the app bundle declares no icon")
                    self.assertTrue(Path(bundle_icon).is_file(),
                                    f"{name}: missing bundle icon {bundle_icon}")


# --------------------------------------------------------------------------- #
# 2. merged cloud modules must ship
# --------------------------------------------------------------------------- #
class CloudModuleInclusionTests(PackagingContractCase):
    def test_host_imports_the_five_cloud_modules_at_module_scope(self):
        for module in CLOUD_MODULES:
            with self.subTest(module=module):
                self.assertIn(module, self.host_module_imports,
                              f"{module} must be a module-level import for transitive analysis")
                self.assertTrue((DESKTOP_DIR / f"{module}.py").is_file())

    def test_cloud_modules_ship_through_normal_transitive_analysis(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertIn(DESKTOP_DIR, run.pathex,
                              f"{name}: the desktop dir must be on pathex")
                for module in CLOUD_MODULES:
                    self.assertIn(module, self.host_module_imports)

    def test_cloud_modules_are_bundled_transitively_or_explicitly(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for module in CLOUD_MODULES:
                    transitive = module in self.host_module_imports and DESKTOP_DIR in run.pathex
                    self.assertTrue(
                        transitive or module in run.hiddenimports,
                        f"{name}: {module} would not be bundled",
                    )

    def test_hidden_imports_cover_the_lazy_third_party_imports(self):
        lazy = set()
        tree = ast.parse(HOST_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lazy |= module_level_imports_from_nodes(node.body)
        third_party_lazy = {
            name for name in lazy
            if name not in sys.stdlib_module_names and name not in CLOUD_MODULES
        }
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                missing = third_party_lazy - run.hiddenimports
                self.assertEqual(missing, set(),
                                 f"{name}: lazily imported {sorted(missing)} are not hidden imports")
                self.assertIn("tkinter", run.hiddenimports,
                              f"{name}: tkinter is imported lazily and must be a hidden import")
                self.assertIn("cpuinfo", run.hiddenimports,
                              f"{name}: cpuinfo is imported lazily and must be a hidden import")

    def test_hidden_imports_are_not_stale(self):
        all_imports = set()
        tree = ast.parse(HOST_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                all_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                all_imports.add(node.module.split(".")[0])
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for entry in run.hiddenimports:
                    root = entry.split(".")[0]
                    self.assertIn(root, all_imports,
                                  f"{name}: hidden import {entry!r} is not imported anywhere")


def module_level_imports_from_nodes(nodes) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# --------------------------------------------------------------------------- #
# 3. collected payload hygiene
# --------------------------------------------------------------------------- #
class BundledPayloadHygieneTests(PackagingContractCase):
    def test_no_user_data_vault_log_or_cache_is_collected(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for source, dest in run.datas:
                    path = Path(source)
                    relative = path.resolve().relative_to(REPO_ROOT).as_posix()
                    if relative in ALLOWED_STATIC_RESOURCES:
                        continue
                    self.assertNotIn(path.name, FORBIDDEN_PAYLOAD_NAMES,
                                     f"{name}: user/secret file collected: {source}")
                    self.assertNotIn(path.suffix, FORBIDDEN_PAYLOAD_SUFFIXES,
                                     f"{name}: cache/log/spec collected: {source}")
                    self.assertFalse(
                        FORBIDDEN_PAYLOAD_PARTS & set(path.parts),
                        f"{name}: forbidden directory collected: {source}",
                    )
                    self.assertNotIn("tests", path.parts,
                                     f"{name}: test material collected: {source}")

    def test_the_repository_test_tree_is_never_bundled(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for source, _dest in run.datas:
                    resolved = Path(source).resolve()
                    self.assertFalse(
                        resolved.is_relative_to(REPO_ROOT / "tooling"),
                        f"{name}: tooling tree collected: {source}",
                    )

    def test_no_spec_or_hidden_import_points_at_the_cloudai_application(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for module in run.hiddenimports:
                    self.assertFalse(module.lower().startswith(("cloudai", "cloud_ai")),
                                     f"{name}: hidden import pulls the CloudAI app: {module}")
                for source, _dest in run.datas:
                    self.assertNotIn("projects/CloudAI", Path(source).as_posix())
                text = run.path.read_text(encoding="utf-8")
                self.assertNotIn("projects/CloudAI", text)
                self.assertNotIn("cloud_ai.py", text)

    def test_no_plaintext_secret_is_bundled(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                for source, _dest in run.datas:
                    path = Path(source)
                    if not path.is_file() or path.suffix.lower() != ".json":
                        continue
                    try:
                        data = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for key in ("api_key", "openai_api_key", "access_token", "secret"):
                        for match in json_string_values(data, key):
                            self.assertEqual(match, "",
                                             f"{name}: bundled {path.name} carries a {key}")


def json_string_values(text: str, key: str):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    values = []
    for name, value in data.items():
        if name == key and isinstance(value, str):
            values.append(value)
    return values


# --------------------------------------------------------------------------- #
# 4. product identity and bundle layout
# --------------------------------------------------------------------------- #
class PlatformConsistencyTests(PackagingContractCase):
    def test_every_platform_build_is_windowed(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertIs(run.exe.kwargs.get("console"), False,
                              f"{name}: a desktop GUI must not open a console")

    def test_onefile_and_onedir_layouts_are_internally_consistent(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                if run.is_onefile:
                    self.assertIsNone(run.collect,
                                      f"{name}: onefile EXE must not use COLLECT")
                    self.assertTrue(run.exe.kwargs.get("runtime_tmpdir") is None)
                else:
                    self.assertIsNotNone(run.collect,
                                         f"{name}: exclude_binaries requires a COLLECT stage")

    def test_platforms_use_the_expected_layout(self):
        self.assertTrue(self.runs["LocalAI.spec"].is_onefile, "Windows spec should stay onefile")
        self.assertTrue(self.runs["local_ai.spec"].is_onefile)
        self.assertFalse(self.runs["LocalAI_Linux.spec"].is_onefile, "Linux spec is onedir")
        self.assertFalse(self.runs["LocalAI_macOS.spec"].is_onefile, "macOS spec is onedir")

    def test_only_macos_produces_an_app_bundle(self):
        bundles = {name: run.bundle for name, run in self.runs.items() if run.bundle is not None}
        self.assertEqual(list(bundles), ["LocalAI_macOS.spec"],
                         f"unexpected .app bundles: {sorted(bundles)}")

    def test_macos_bundle_identity_is_well_formed(self):
        bundle = self.runs["LocalAI_macOS.spec"].bundle.kwargs
        self.assertEqual(bundle["name"], "LocalAI.app")
        identifier = bundle["bundle_identifier"]
        self.assertRegex(identifier, r"^[A-Za-z0-9]+(\.[A-Za-z0-9-]+)+$",
                         f"bundle identifier is not reverse-DNS: {identifier!r}")
        self.assertGreaterEqual(identifier.count("."), 2)

    def test_release_specs_share_one_product_name(self):
        for target, filename in DRIVER_SPECS.items():
            run = self.runs[filename]
            with self.subTest(target=target):
                self.assertEqual(run.exe.kwargs["name"], "LocalAI",
                                 f"{filename}: executable name must match the product name")
        self.assertEqual(self.runs["LocalAI_Linux.spec"].collect.kwargs["name"], "LocalAI")
        self.assertEqual(self.runs["LocalAI_macOS.spec"].collect.kwargs["name"], "LocalAI")

    def test_platform_icons_use_the_platform_appropriate_format(self):
        self.assertTrue(self.runs["LocalAI.spec"].exe.kwargs["icon"].endswith(".ico"))
        self.assertTrue(self.runs["LocalAI_Linux.spec"].exe.kwargs["icon"].endswith(".ico"))
        self.assertTrue(
            self.runs["LocalAI_macOS.spec"].bundle.kwargs["icon"].endswith(".icns"),
            "the macOS app bundle needs a .icns icon",
        )

    def test_target_arch_is_passed_through_for_cross_arch_builds(self):
        for name, run in self.all_runs():
            with self.subTest(spec=name):
                self.assertIn("target_arch", run.exe.kwargs)
                self.assertEqual(
                    run.exe.kwargs["target_arch"],
                    run.namespace["TARGET_ARCH"],
                )


# --------------------------------------------------------------------------- #
# 5. source vs frozen resource lookup
# --------------------------------------------------------------------------- #
class FrozenResourceLookupTests(PackagingContractCase):
    def test_datas_destinations_match_the_runtime_lookups(self):
        destinations = {
            Path(source).name: dest for source, dest in self.runs["LocalAI.spec"].datas
        }
        self.assertEqual(destinations.get("Readme.docx"), ".",
                         "Readme.docx must land beside the resource root")
        icons = [dest for source, dest in self.runs["LocalAI.spec"].datas
                 if Path(source).name == "icons" and Path(source).is_dir()]
        self.assertEqual(icons, ["assets/icons"],
                         "the icon tree must land at assets/icons to match resource_path()")

    def test_frozen_lookup_resolves_the_bundled_document_and_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            for relative in REQUIRED_ICON_PNGS + (REQUIRED_DOCUMENT,):
                target = bundle / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"resource")
            with mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
                self.assertTrue(Path(local_ai.app_icon_path("dark")).is_relative_to(bundle))
                self.assertTrue(Path(local_ai.app_icon_path("light")).is_relative_to(bundle))
                self.assertTrue(Path(local_ai.bundled_document_path(REQUIRED_DOCUMENT))
                                .is_relative_to(bundle))

    def test_resource_lookup_does_not_assume_a_fixed_temporary_directory(self):
        source = HOST_PATH.read_text(encoding="utf-8")
        start = source.index("def resource_path(")
        body = source[start:source.index("\ndef ", start + 1)]
        self.assertIn("_MEIPASS", body, "resource_path must consult sys._MEIPASS when frozen")
        for hardcoded in ("/tmp/_MEI", "_MEI123", "/var/folders", "C:\\\\Temp"):
            self.assertNotIn(hardcoded, source,
                             f"production hardcodes a frozen temp directory: {hardcoded}")

    def test_resource_lookup_accepts_a_brand_new_meipass_each_run(self):
        for index in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / f"_MEI{index}"
                icon = bundle / REQUIRED_ICON_PNGS[0]
                icon.parent.mkdir(parents=True, exist_ok=True)
                icon.write_bytes(b"icon")
                document = bundle / REQUIRED_DOCUMENT
                document.write_bytes(b"doc")
                with mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
                    self.assertTrue(Path(local_ai.app_icon_path("dark")).is_relative_to(bundle))
                    self.assertTrue(Path(local_ai.bundled_document_path(REQUIRED_DOCUMENT))
                                    .is_relative_to(bundle))

    def test_icon_and_document_sources_exist_for_the_frozen_lookup(self):
        icons_dir = REPO_ROOT / "shared" / "assets" / "icons"
        for relative in REQUIRED_ICON_PNGS:
            with self.subTest(icon=relative):
                self.assertTrue((icons_dir / Path(relative).name).is_file())
        self.assertTrue((LOCALAI_DIR / "resources" / REQUIRED_DOCUMENT).is_file())


if __name__ == "__main__":
    unittest.main()
