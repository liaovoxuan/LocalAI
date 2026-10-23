"""Release test-suite isolation and determinism contracts.

The suite that gates the release must be hermetic and order independent: no test may
touch the real user config/vault/log locations, reach the network, depend on the
repository working directory, leak environment or ``sys.modules`` state, retain
non-daemon threads or Tk roots, or rely on a preceding module.

The order/leak matrix is run out-of-band; this module encodes the durable contracts so
a regression fails here:

1. source hygiene - test modules are filesystem-, network-, thread- and import-clean
2. in-process leaks - representative threaded and Tk tests leave nothing behind
3. fresh-interpreter hermeticity - a deterministically shuffled subset run in a clean
   subprocess changes nothing observable outside its own temp directories

The subprocess check exports ``LOCALAI_ISOLATION_CHILD=1`` for its child. The child
runs the same subset directly, and that flag only stops the nested subprocess from
recurring.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tooling" / "tests"
DESKTOP_DIR = REPO_ROOT / "projects" / "LocalAI" / "desktop"
SE_DESKTOP_DIR = REPO_ROOT / "projects" / "LocalAI-SE" / "desktop"

for _directory in (TESTS_DIR, DESKTOP_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import local_ai  # noqa: E402

TEST_MODULES = (
    "test_activation",
    "test_cloud_host_integration",
    "test_gui_interactions",
    "test_local_ai_regressions",
    "test_merge_integrity",
    "test_package",
    "test_packaging_integrity",
    "testcloudcontract",
    "testcloudsecretinterop",
)

#: Modules that exercise the leak-prone machinery (env, HTTP stub, threads, Tk,
#: sys.modules) and therefore carry the fresh-interpreter hermeticity check.
LEAK_PRONE_MODULES = (
    "test_cloud_host_integration",
    "test_gui_interactions",
    "test_merge_integrity",
    "testcloudsecretinterop",
)

#: Real per-user application-data locations a test must never address directly.
#: Anchored at the running user's home so synthetic fixtures (for example
#: "/home/user/..." used as probe input) are not mistaken for real locations.
REAL_USER_DATA_MARKERS = tuple(
    str(Path.home() / relative) for relative in (
        "Library/Application Support/LocalAI",
        "Library/Application Support/CloudAI",
        ".local/share/LocalAI",
    )
) + ("%LOCALAPPDATA%", "%APPDATA%")

#: Calls that persist into the per-user data directory; a module using any of these
#: must redirect ``local_ai``'s data-directory globals.
APP_DATA_WRITING_CALLS = ("save_config(", "load_config(", "ensure_app_dirs(", "ChatApp")
ISOLATED_ATTRIBUTES = ("APP_DATA_DIR", "CONFIG_FILE", "LOG_DIR")

ISOLATION_CHILD_FLAG = "LOCALAI_ISOLATION_CHILD"
SHUFFLE_SEED = 20261002

CHILD_SCRIPT = r'''
import hashlib, importlib, io, json, os, random, socket, sys, threading, unittest
from pathlib import Path

REPO = Path(sys.argv[1])
sys.path.insert(0, str(REPO / "tooling" / "tests"))
sys.path.insert(0, str(REPO))

import tkinter

MODULES = ["test_cloud_host_integration", "test_gui_interactions",
           "test_merge_integrity", "testcloudsecretinterop"]
USER_DIRS = [Path.home() / "Library" / "Application Support" / "LocalAI",
             Path.home() / "Library" / "Application Support" / "CloudAI"]


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def manifest(root):
    if not root.exists():
        return "absent"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        try:
            st = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def tk_state():
    root = getattr(tkinter, "_default_root", None)
    alive = False
    if root is not None:
        try:
            alive = bool(root.winfo_exists())
        except Exception:
            alive = False
    return {"root": root is not None, "alive": alive}


loader = unittest.defaultTestLoader
flat = []
for name in MODULES:
    flat.extend(flatten(loader.loadTestsFromModule(importlib.import_module(name))))
random.Random(int(sys.argv[2])).shuffle(flat)

env_before = dict(os.environ)
mods_before = set(sys.modules)
threads_before = {t.ident for t in threading.enumerate()}
cwd_before = os.getcwd()
user_before = {str(d): manifest(d) for d in USER_DIRS}
tk_before = tk_state()

violations = []
real_connect = socket.socket.connect


def guard(self, address):
    host = address[0] if isinstance(address, tuple) and address else None
    if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1", "") \
            and not host.startswith("127."):
        violations.append(str(address))
        raise OSError("blocked non-loopback connect")
    return real_connect(self, address)


socket.socket.connect = guard

stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=0).run(unittest.TestSuite(flat))

for _ in range(3):
    extra = [t for t in threading.enumerate() if t.ident not in threads_before]
    if not extra:
        break
    for thread in extra:
        thread.join(timeout=0.05)

print(json.dumps({
    "tests": result.testsRun,
    "ok": result.wasSuccessful(),
    "failures": len(result.failures),
    "errors": len(result.errors),
    "network_violations": violations,
    "env_diff": {
        "added": sorted(set(os.environ) - set(env_before)),
        "removed": sorted(set(env_before) - set(os.environ)),
        "changed": sorted(k for k in set(env_before) & set(os.environ)
                          if env_before[k] != os.environ[k]),
    },
    "sys_modules_extra": sorted(set(sys.modules) - mods_before),
    "threads": [{"name": t.name, "daemon": t.daemon}
                for t in threading.enumerate() if t.ident not in threads_before],
    "tk_before": tk_before,
    "tk_after": tk_state(),
    "cwd_changed": cwd_before != os.getcwd(),
    "user_changed": user_before != {str(d): manifest(d) for d in USER_DIRS},
    "tail": stream.getvalue().splitlines()[-3:],
}))
'''


def module_source(name: str) -> str:
    return (TESTS_DIR / f"{name}.py").read_text(encoding="utf-8")


def module_tree(name: str) -> ast.Module:
    return ast.parse(module_source(name))


def string_literals(tree: ast.Module) -> list[str]:
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def tk_root_alive() -> bool:
    import tkinter
    root = getattr(tkinter, "_default_root", None)
    if root is None:
        return False
    try:
        return bool(root.winfo_exists())
    except Exception:
        return False


class SuiteSourceHygieneTests(unittest.TestCase):
    """Static contracts over the test sources themselves."""

    def test_all_expected_test_modules_exist(self):
        found = {path.stem for path in TESTS_DIR.glob("test*.py")}
        self.assertTrue(set(TEST_MODULES) <= found,
                        f"missing modules: {sorted(set(TEST_MODULES) - found)}")

    def test_no_test_module_addresses_a_real_user_data_directory(self):
        for name in TEST_MODULES:
            with self.subTest(module=name):
                for literal in string_literals(module_tree(name)):
                    for marker in REAL_USER_DATA_MARKERS:
                        self.assertNotIn(
                            marker, literal,
                            f"{name} addresses the real user-data location {marker!r}: {literal!r}",
                        )

    def test_modules_that_write_app_data_isolate_its_directories(self):
        checked = 0
        for name in TEST_MODULES:
            source = module_source(name)
            if not any(call in source for call in APP_DATA_WRITING_CALLS):
                continue
            checked += 1
            with self.subTest(module=name):
                for attribute in ISOLATED_ATTRIBUTES:
                    self.assertIn(attribute, source,
                                  f"{name} writes app data but never isolates {attribute}")
        self.assertGreater(checked, 0, "no module was checked; the rule is vacuous")

    def test_environment_is_only_mutated_inside_a_patch_context(self):
        for name in TEST_MODULES:
            tree = module_tree(name)
            patched_ranges = []
            for node in ast.walk(tree):
                if isinstance(node, ast.With):
                    for item in node.items:
                        text = ast.unparse(item.context_expr)
                        if "patch.dict" in text and "environ" in text:
                            patched_ranges.append(
                                (node.lineno, getattr(node, "end_lineno", node.lineno)))
            for node in ast.walk(tree):
                mutates = False
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "pop" and ast.unparse(node.func.value) == "os.environ":
                        mutates = True
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript) and \
                                ast.unparse(target.value) == "os.environ":
                            mutates = True
                if not mutates:
                    continue
                inside = any(start <= node.lineno <= end for start, end in patched_ranges)
                self.assertTrue(
                    inside,
                    f"{name}:{node.lineno} mutates os.environ outside a mock.patch.dict block",
                )

    def test_the_only_environment_addition_comes_from_production(self):
        # Importing the application sets TK_SILENCE_DEPRECATION once via setdefault;
        # no test may add environment variables of its own.
        production = (DESKTOP_DIR / "local_ai.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")', production,
                      "the documented production environment addition disappeared")
        for name in TEST_MODULES:
            with self.subTest(module=name):
                self.assertNotIn("TK_SILENCE_DEPRECATION", module_source(name),
                                 f"{name} sets an environment variable of its own")

    def test_no_test_module_creates_an_unmanaged_temporary_directory(self):
        for name in TEST_MODULES:
            with self.subTest(module=name):
                self.assertNotIn("mkdtemp", module_source(name),
                                 f"{name} uses tempfile.mkdtemp instead of TemporaryDirectory")

    def test_no_test_module_performs_direct_network_io(self):
        forbidden = ("requests.get(", "requests.post(", "requests.put(", "urlopen(",
                     "socket.create_connection(")
        for name in TEST_MODULES:
            source = module_source(name)
            for needle in forbidden:
                with self.subTest(module=name, needle=needle):
                    self.assertNotIn(needle, source, f"{name} performs real network I/O")

    def test_loopback_stub_servers_shut_down_and_close(self):
        checked = 0
        for name in TEST_MODULES:
            source = module_source(name)
            if "BaseHTTPRequestHandler" not in source:
                continue
            checked += 1
            with self.subTest(module=name):
                self.assertIn("shutdown()", source, f"{name}: stub server is never shut down")
                self.assertIn("server_close()", source, f"{name}: stub socket is never closed")
        self.assertGreater(checked, 0, "no loopback stub was found; the rule is vacuous")

    def test_no_test_module_starts_a_non_daemon_worker(self):
        seen = 0
        for name in TEST_MODULES:
            for node in ast.walk(module_tree(name)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "Thread":
                    seen += 1
                    with self.subTest(module=name, line=node.lineno):
                        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
                        self.assertEqual(
                            keywords.get("daemon"), "True",
                            f"{name}:{node.lineno} starts a non-daemon thread; a slow worker "
                            "would outlive join(timeout=...) and stall interpreter exit",
                        )
        self.assertGreater(seen, 0, "no worker thread was found; the rule is vacuous")


class InProcessLeakTests(unittest.TestCase):
    """Run leak-prone existing tests in-process and inspect the aftermath.

    Environment, working-directory, user-data, network and ``sys.modules`` drift are
    covered by ``FreshInterpreterHermeticityTests`` for the same modules.
    """

    @staticmethod
    def _run(suite):
        import io
        stream = io.StringIO()
        return unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)

    def test_threaded_worker_tests_leave_no_thread_behind(self):
        import importlib
        before = {thread.ident for thread in threading.enumerate()}
        module = importlib.import_module("test_cloud_host_integration")
        suite = unittest.TestSuite(
            module.CloudTkIntegrationTests(name) for name in (
                "test_cloud_worker_sends_chunks_and_done_to_queue_without_tk_calls",
                "test_local_worker_sends_chunks_and_done_to_queue_without_tk_calls",
            )
        )
        result = self._run(suite)
        self.assertTrue(result.wasSuccessful(), [str(f) for f in result.failures + result.errors])
        leftovers = [t.name for t in threading.enumerate() if t.ident not in before]
        self.assertEqual(leftovers, [], f"worker threads outlived the test: {leftovers}")

    def test_gui_tests_leave_no_live_tk_root(self):
        import importlib
        module = importlib.import_module("test_gui_interactions")
        suite = unittest.TestSuite([
            module.ScrollbarAndScrollRegionTests("test_scrollbar_command_is_wired_to_the_canvas"),
        ])
        result = self._run(suite)
        self.assertTrue(result.wasSuccessful(), [str(f) for f in result.failures + result.errors])
        self.assertFalse(tk_root_alive(), "a Tk root survived the GUI test")


class FreshInterpreterHermeticityTests(unittest.TestCase):
    """Run a deterministically shuffled subset in a clean subprocess."""

    def test_shuffled_subset_is_hermetic_in_a_fresh_interpreter(self):
        if os.environ.get(ISOLATION_CHILD_FLAG) == "1":
            return  # the nested child must not spawn another child
        environment = dict(os.environ)
        environment[ISOLATION_CHILD_FLAG] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(DESKTOP_DIR), str(SE_DESKTOP_DIR), str(TESTS_DIR), str(REPO_ROOT)])
        completed = subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT, str(REPO_ROOT), str(SHUFFLE_SEED)],
            capture_output=True, text=True, timeout=900, env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        report = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertTrue(report["ok"], f"shuffled subset failed: {report.get('tail')}")
        self.assertGreater(report["tests"], 100, "the shuffled subset was unexpectedly small")
        self.assertEqual(report["network_violations"], [],
                         "a test reached a non-loopback address")
        self.assertEqual(report["env_diff"], {"added": [], "removed": [], "changed": []},
                         f"environment leaked across the shuffled run: {report['env_diff']}")
        self.assertEqual(report["threads"], [], "worker threads outlived the shuffled run")
        self.assertFalse(report["tk_after"]["alive"], "a Tk root survived the shuffled run")
        self.assertFalse(report["cwd_changed"], "the shuffled run changed the working directory")
        self.assertFalse(report["user_changed"],
                         "the shuffled run touched a real user data directory")
        leaked = [name for name in report["sys_modules_extra"] if name.startswith("_cloudai")]
        self.assertEqual(leaked, [], f"import state leaked into sys.modules: {leaked}")

    def test_recursion_guard_is_read_before_spawning_the_child(self):
        # The subprocess check runs this same module, so the exported flag must be
        # consulted before the child is spawned or the check would recurse forever.
        source = module_source("test_suite_isolation")
        guard = "os.environ.get(ISOLATION_CHILD_FLAG)"
        self.assertTrue(guard in source, "the recursion guard is missing from the module")
        self.assertLess(source.index(guard), source.index("subprocess.run("),
                        "the recursion guard must be read before spawning the child")


if __name__ == "__main__":
    unittest.main()
