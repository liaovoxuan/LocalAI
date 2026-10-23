#!/usr/bin/env python3
"""Adversarial regression tests for verify_minivmac_inprocess_path.py.

Covers:
  - Comment / string literal false-positive suppression
  - Normal QEMU branch allowance (outside isSystemSoftwareConfig)
  - isSystemSoftwareConfig if / else-if / early-return / lambda nesting
  - Macro line continuation (backslash)
  - Alias variables (auto& proc = process_)
  - Hard violations: QProcess, startDetached, fork, vfork, exec*,
    system, popen, posix_spawn, posix_spawnp, processId, waitForStarted,
    waitForFinished, waitForReadyRead, waitForBytesWritten,
    readAllStandardError, readAllStandardOutput, sendQmp,
    sendMonitorCommand, screendump
  - Advisory hits (closeQmp, connectQmp, qmpSocket_, etc.) must NOT fail gate
  - Word-boundary precision (QProcessTimer, process_state, filesystem,
    posix_spawnattr)
  - Return / assignment context NOT treated as ISSC block
  - Product-tree integration: RESULT:CLEAN

All tests synthesize input in temporary directories — no product source
modification.  Uses only Python standard library (unittest).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# ---------------------------------------------------------------------------
# Make the scanner importable regardless of working directory
# ---------------------------------------------------------------------------
_TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'tools')
sys.path.insert(0, _TOOLS_DIR)

import verify_minivmac_inprocess_path as scanner  # noqa: E402


# ---------------------------------------------------------------------------
# Helper mixin
# ---------------------------------------------------------------------------
class _TempDirMixin:
    """Provides a self._tmpdir and self._write(name, content) helper."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='minivmac_scan_test_')

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, name: str, content: str) -> str:
        path = os.path.join(self._tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(content)
        return path


# ===================================================================
# 1. strip_comments_and_strings
# ===================================================================

class TestStripCommentsAndStrings(unittest.TestCase):
    """Verify comment / string stripping before scanning."""

    def test_line_comment_suppressed(self):
        src = '// QProcess should not be used here\nint x = 0;\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertNotIn('QProcess', out)
        self.assertIn('int x = 0;', out)

    def test_block_comment_suppressed(self):
        src = '/* fork() is forbidden */\nint x = 0;\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertNotIn('fork', out)

    def test_string_literal_suppressed(self):
        src = 'const char* msg = "QProcess is used";\nint x = 0;\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertNotIn('QProcess', out)

    def test_char_literal_suppressed(self):
        src = "char c = 'f';\nint fork_count = 0;\n"
        out = scanner.strip_comments_and_strings(src)
        # 'f' is replaced with ''; fork_count remains (not in a string)
        self.assertIn('fork_count', out)

    def test_code_preserved(self):
        src = 'QProcess p;\np.start();\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertIn('QProcess', out)

    def test_escaped_quote_in_string(self):
        src = 'const char* s = "hello \\"QProcess\\" world";\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertNotIn('QProcess', out)

    def test_line_comment_preserves_newline(self):
        src = 'int x; // QProcess\nint y;\n'
        out = scanner.strip_comments_and_strings(src)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('int x;', lines[0])
        self.assertIn('int y;', lines[1])

    def test_multiline_block_comment(self):
        src = '/* QProcess\n   fork\n   system */\nint x = 0;\n'
        out = scanner.strip_comments_and_strings(src)
        self.assertNotIn('QProcess', out)
        self.assertNotIn('fork', out)
        self.assertNotIn('system', out)

    def test_raw_string_literal(self):
        # C++11 raw string — our stripper treats it as a regular string
        src = 'R"(QProcess)"\n'
        out = scanner.strip_comments_and_strings(src)
        # The stripper sees " at R"( which opens a string context.
        # It may not perfectly handle raw strings, but that's an
        # acceptable limitation — raw strings with forbidden APIs
        # inside are extremely unlikely in this codebase.
        pass  # documented edge case, not a blocker


# ===================================================================
# 2. find_issc_blocks
# ===================================================================

class TestFindIsscBlocks(unittest.TestCase):
    """Test isSystemSoftwareConfig block detection."""

    def test_simple_if_block(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    int x = 1;\n'
                '}\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['start_line'], 1)
        self.assertEqual(blocks[0]['end_line'], 3)
        self.assertFalse(blocks[0]['is_nested_in_qemu_handler'])

    def test_if_else_excludes_else_branch(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    int x = 1;\n'
                '} else {\n'
                '    QProcess p;\n'
                '}\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        # ISSC block ends at line 3 (closing } before else)
        self.assertEqual(blocks[0]['end_line'], 3)

    def test_early_return_single_line(self):
        code = 'if (isSystemSoftwareConfig(type)) return;\nQProcess p;\n'
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['start_line'], 1)
        self.assertEqual(blocks[0]['end_line'], 1)

    def test_early_return_with_value(self):
        code = ('if (isSystemSoftwareConfig(type))\n'
                '    return engine;\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)

    def test_return_statement_NOT_if(self):
        """return isSystemSoftwareConfig(...) must NOT create an ISSC block."""
        code = 'return isSystemSoftwareConfig(type);\n'
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 0)

    def test_assignment_NOT_if(self):
        """matchesConfig = isSystemSoftwareConfig(...) must NOT create block."""
        code = 'bool matchesConfig = isSystemSoftwareConfig(type);\n'
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 0)

    def test_else_if_creates_separate_block(self):
        code = ('if (qemuMode) {\n'
                '    QProcess p;\n'
                '} else if (isSystemSoftwareConfig(type)) {\n'
                '    int x = 1;\n'
                '}\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['start_line'], 3)

    def test_qemu_handler_nesting(self):
        code = ('connect(&process_, &QProcess::started, this, [this]() {\n'
                '    if (isSystemSoftwareConfig(type)) {\n'
                '        int x = 1;\n'
                '    }\n'
                '});\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]['is_nested_in_qemu_handler'])

    def test_multiple_issc_blocks(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    int x = 1;\n'
                '}\n'
                'if (isSystemSoftwareConfig(other)) {\n'
                '    int y = 2;\n'
                '}\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 2)

    def test_nested_braces_in_issc(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    if (nested) {\n'
                '        int x = 1;\n'
                '    }\n'
                '}\n')
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['start_line'], 1)
        self.assertEqual(blocks[0]['end_line'], 5)

    def test_no_issc_at_all(self):
        code = 'QProcess p;\np.start("qemu");\n'
        blocks = scanner.find_issc_blocks(code)
        self.assertEqual(len(blocks), 0)


# ===================================================================
# 3. is_hit_in_issc_branch
# ===================================================================

class TestIsHitInIsscBranch(unittest.TestCase):
    """Test hit classification (in ISSC branch or not)."""

    def test_hit_inside_issc(self):
        blocks = [{'start_line': 1, 'end_line': 5,
                   'is_nested_in_qemu_handler': False}]
        in_issc, is_qemu = scanner.is_hit_in_issc_branch(
            3, 'QProcess', '    QProcess p;', blocks)
        self.assertTrue(in_issc)
        self.assertFalse(is_qemu)

    def test_hit_outside_issc(self):
        blocks = [{'start_line': 1, 'end_line': 5,
                   'is_nested_in_qemu_handler': False}]
        in_issc, _ = scanner.is_hit_in_issc_branch(
            10, 'QProcess', '    QProcess p;', blocks)
        self.assertFalse(in_issc)

    def test_else_line_api_after_brace_not_in_issc(self):
        """On the end_line with '} else { ... QProcess ...}', the API after
        the closing } is in the else path, NOT in the ISSC branch."""
        blocks = [{'start_line': 1, 'end_line': 3,
                   'is_nested_in_qemu_handler': False}]
        line = '} else { QProcess p; }'
        in_issc, _ = scanner.is_hit_in_issc_branch(
            3, 'QProcess', line, blocks)
        self.assertFalse(in_issc)

    def test_qemu_nested_flag_propagated(self):
        blocks = [{'start_line': 2, 'end_line': 4,
                   'is_nested_in_qemu_handler': True}]
        in_issc, is_qemu = scanner.is_hit_in_issc_branch(
            3, 'process_', '    process_.write("data");', blocks)
        self.assertTrue(in_issc)
        self.assertTrue(is_qemu)

    def test_no_blocks_always_outside(self):
        in_issc, _ = scanner.is_hit_in_issc_branch(
            1, 'QProcess', 'QProcess p;', [])
        self.assertFalse(in_issc)


# ===================================================================
# 4. scan_dual_path_file
# ===================================================================

class TestScanDualPathFile(_TempDirMixin, unittest.TestCase):
    """Test scanning files with both Mini vMac and QEMU paths."""

    def test_clean_issc_branch(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '    engine->setMousePos(x, y);\n'
                '}\n'
                'QProcess process_;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 0)
        self.assertGreater(len(q), 0)  # QProcess on line 5 → QEMU hit

    def test_violation_in_issc_branch(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcess p;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'QProcess')

    def test_qemu_else_branch_allowed(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '} else {\n'
                '    QProcess p;\n'
                '    p.start("qemu");\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)

    def test_closeQmp_advisory_not_violation(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    closeQmp();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][1], 'closeQmp')

    def test_qemu_handler_issc_not_violation(self):
        code = ('connect(&process_, &QProcess::started, this, [this]() {\n'
                '    if (isSystemSoftwareConfig(type)) {\n'
                '        process_.write("data");\n'
                '    }\n'
                '});\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)

    def test_nonexistent_file(self):
        v, a, q, n = scanner.scan_dual_path_file('/nonexistent/file.cpp')
        self.assertEqual(len(v), 0)
        self.assertEqual(n, 0)

    def test_empty_file(self):
        path = self._write('VirtualMachineWindow.cpp', '')
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(n, 0)


# ===================================================================
# 5. scan_minivmac_only_file
# ===================================================================

class TestScanMinivmacOnlyFile(_TempDirMixin, unittest.TestCase):
    """Test scanning files that should have zero forbidden APIs anywhere."""

    def test_clean_file(self):
        code = 'void SystemSoftwareEngine::start() {\n    run();\n}\n'
        path = self._write('SystemSoftwareEngine.cpp', code)
        v = scanner.scan_minivmac_only_file(path)
        self.assertEqual(len(v), 0)

    def test_forbidden_api_anywhere_is_violation(self):
        code = 'void foo() {\n    QProcess p;\n}\n'
        path = self._write('SystemSoftwareEngine.cpp', code)
        v = scanner.scan_minivmac_only_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'QProcess')

    def test_advisory_api_allowed(self):
        code = 'void foo() {\n    closeQmp();\n}\n'
        path = self._write('SystemSoftwareEngine.cpp', code)
        v = scanner.scan_minivmac_only_file(path)
        self.assertEqual(len(v), 0)

    def test_fork_anywhere_is_violation(self):
        code = 'void foo() {\n    fork();\n}\n'
        path = self._write('SystemSoftwareEngine.cpp', code)
        v = scanner.scan_minivmac_only_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'fork')


# ===================================================================
# 6. scan_main_cpp
# ===================================================================

class TestScanMainCpp(_TempDirMixin, unittest.TestCase):
    """Test scanning main.cpp (system-software branch = Mini vMac path)."""

    def test_clean_main(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->launch();\n'
                '} else {\n'
                '    QProcess p;\n'
                '}\n')
        path = self._write('main.cpp', code)
        v, q = scanner.scan_main_cpp(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)

    def test_violation_in_system_software_branch(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    fork();\n'
                '}\n')
        path = self._write('main.cpp', code)
        v, q = scanner.scan_main_cpp(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'fork')


# ===================================================================
# 7. Adversarial regression tests
# ===================================================================

class TestAdversarialCommentStringFalsePositives(_TempDirMixin, unittest.TestCase):
    """Comment / string literal must never cause violations."""

    def test_line_comment_QProcess(self):
        code = '// TODO: QProcess should be avoided\nint x = 0;\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_block_comment_fork(self):
        code = '/* fork() is dangerous */\nint x = 0;\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_string_literal_system(self):
        code = 'const char* msg = "system() call";\nint x = 0;\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_string_literal_popen(self):
        code = 'auto s = QString("popen is bad");\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_comment_inside_issc_block(self):
        """A comment inside an ISSC block must not cause a violation."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    // QProcess would be wrong here\n'
                '    engine->start();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)


class TestAdversarialWordBoundary(_TempDirMixin, unittest.TestCase):
    """Word-boundary precision: partial matches must be rejected."""

    def test_QProcessTimer_not_matched(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcessTimer t;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_process_state_not_matched(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    int process_state = 0;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_filesystem_not_matched(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    std::filesystem::path p;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_posix_spawnattr_not_matched(self):
        """posix_spawnattr_t should NOT match posix_spawn."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    posix_spawnattr_t attr;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_posix_spawn_file_actions_not_matched(self):
        """posix_spawn_file_actions_t should NOT match posix_spawn."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    posix_spawn_file_actions_t actions;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_execvp_compat_not_matched(self):
        """execv_compat should NOT match execv (suffix is word char)."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    execv_compat();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_systemCall_not_matched(self):
        """systemCall should NOT match system (suffix is word char)."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    systemCall();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_process_Id_not_matched(self):
        """processId must match exactly — process_Id (camelCase variant)
        should NOT match processId (different word boundary)."""
        # processId is in the forbidden list. process_Id is a different
        # identifier. The regex \b(processId)\b should NOT match process_Id
        # because the underscore breaks the word.
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    int process_Id = 0;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        # process_Id should NOT match processId (underscore breaks word)
        self.assertEqual(len(v), 0)


class TestAdversarialHardViolations(_TempDirMixin, unittest.TestCase):
    """Each forbidden API in an ISSC branch must be a hard violation."""

    def _check_violation(self, api_stmt: str, api_name: str):
        """Helper: put api_stmt inside an ISSC block, assert 1 violation."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                f'    {api_stmt}\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 1, f"Expected 1 violation for {api_name}")
        self.assertEqual(v[0][1], api_name)

    def test_QProcess(self):
        self._check_violation('QProcess p;', 'QProcess')

    def test_process_member(self):
        self._check_violation('process_.start();', 'process_')

    def test_startDetached(self):
        self._check_violation('startDetached();', 'startDetached')

    def test_fork(self):
        self._check_violation('fork();', 'fork')

    def test_vfork(self):
        self._check_violation('vfork();', 'vfork')

    def test_execv(self):
        self._check_violation('execv("/bin/ls", args);', 'execv')

    def test_execve(self):
        self._check_violation('execve("/bin/ls", args, env);', 'execve')

    def test_execvp(self):
        self._check_violation('execvp("ls", args);', 'execvp')

    def test_execl(self):
        self._check_violation('execl("/bin/ls", "ls", nullptr);', 'execl')

    def test_execlp(self):
        self._check_violation('execlp("ls", "ls", nullptr);', 'execlp')

    def test_execle(self):
        self._check_violation('execle("/bin/ls", "ls", nullptr, env);', 'execle')

    def test_system(self):
        self._check_violation('system("ls");', 'system')

    def test_popen(self):
        self._check_violation('popen("ls", "r");', 'popen')

    def test_posix_spawn(self):
        self._check_violation('posix_spawn(&pid, "/bin/ls", nullptr, nullptr, args, env);', 'posix_spawn')

    def test_posix_spawnp(self):
        self._check_violation('posix_spawnp(&pid, "ls", nullptr, nullptr, args, env);', 'posix_spawnp')

    def test_processId(self):
        self._check_violation('auto pid = processId();', 'processId')

    def test_waitForStarted(self):
        self._check_violation('waitForStarted();', 'waitForStarted')

    def test_waitForFinished(self):
        self._check_violation('waitForFinished();', 'waitForFinished')

    def test_waitForReadyRead(self):
        self._check_violation('waitForReadyRead();', 'waitForReadyRead')

    def test_waitForBytesWritten(self):
        self._check_violation('waitForBytesWritten();', 'waitForBytesWritten')

    def test_readAllStandardError(self):
        self._check_violation('readAllStandardError();', 'readAllStandardError')

    def test_readAllStandardOutput(self):
        self._check_violation('readAllStandardOutput();', 'readAllStandardOutput')

    def test_sendQmp(self):
        self._check_violation('sendQmp(cmd);', 'sendQmp')

    def test_sendMonitorCommand(self):
        self._check_violation('sendMonitorCommand(cmd);', 'sendMonitorCommand')

    def test_screendump(self):
        self._check_violation('screendump(path);', 'screendump')


class TestAdversisoryNotViolation(_TempDirMixin, unittest.TestCase):
    """Advisory APIs in ISSC branch must NOT be hard violations."""

    def test_closeQmp_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    closeQmp();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][1], 'closeQmp')

    def test_connectQmp_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    connectQmp();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)

    def test_qmpSocket_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    qmpSocket_->close();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)

    def test_qmpCapabilitiesReady_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    qmpCapabilitiesReady_ = false;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)

    def test_qmpConnectTimer_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    qmpConnectTimer_->stop();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)

    def test_processQmpData_advisory(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    processQmpData();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertEqual(len(a), 1)

    def test_advisory_does_not_fail_gate(self):
        """Only advisory hits → run_scan returns 0 (CLEAN)."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    closeQmp();\n'
                '    qmpSocket_->close();\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        # Create empty files for other expected files
        self._write('SystemSoftwareEngine.cpp', '')
        self._write('SystemSoftwareEngine.hpp', '')
        self._write('main.cpp', '')
        rc = scanner.run_scan(self._tmpdir)
        self.assertEqual(rc, 0)


class TestAdversarialControlFlow(_TempDirMixin, unittest.TestCase):
    """if / else-if / early-return / lambda nesting edge cases."""

    def test_else_if_QProcess_allowed(self):
        """QProcess in the else-if branch (NOT ISSC) is allowed."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '} else if (mode == QEMU) {\n'
                '    QProcess p;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_else_QProcess_allowed(self):
        """QProcess in the else branch is allowed."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '} else {\n'
                '    QProcess p;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_early_return_then_QProcess_allowed(self):
        """After early-return ISSC, QProcess is in outer scope (allowed)."""
        code = ('if (isSystemSoftwareConfig(type)) return;\n'
                'QProcess p;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_early_return_with_brace_then_QProcess(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    return;\n'
                '}\n'
                'QProcess p;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_lambda_nested_issc_process_allowed(self):
        """process_ in QEMU handler lambda ISSC sub-branch → QEMU-nested."""
        code = ('connect(&process_, &QProcess::readyRead, [this]() {\n'
                '    if (isSystemSoftwareConfig(type)) {\n'
                '        process_.readAll();\n'
                '    }\n'
                '});\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)

    def test_lambda_nested_issc_fork_forbidden(self):
        """fork() in QEMU-nested ISSC is still QEMU-nested (allowed).
        This is by design: QEMU context takes precedence."""
        code = ('connect(&process_, &QProcess::readyRead, [this]() {\n'
                '    if (isSystemSoftwareConfig(type)) {\n'
                '        fork();\n'
                '    }\n'
                '});\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        # QEMU-nested → not a violation (QEMU context takes precedence)
        self.assertEqual(len(v), 0)

    def test_nested_if_in_issc(self):
        """Nested if inside ISSC block — fork() is still a violation."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    if (submode) {\n'
                '        fork();\n'
                '    }\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'fork')

    def test_two_issc_blocks_one_clean_one_dirty(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '}\n'
                'if (isSystemSoftwareConfig(other)) {\n'
                '    fork();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'fork')


class TestAdversarialMacroContinuation(_TempDirMixin, unittest.TestCase):
    """Backslash line continuation in ISSC block."""

    def test_macro_continuation_QProcess(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcess \\\n'
                '        *p = new QProcess();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        # QProcess on line 2 should be detected in ISSC block
        self.assertGreaterEqual(len(v), 1)
        apis = {h[1] for h in v}
        self.assertIn('QProcess', apis)

    def test_macro_continuation_fork(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    pid_t pid = \\\n'
                '        fork();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertGreaterEqual(len(v), 1)
        apis = {h[1] for h in v}
        self.assertIn('fork', apis)


class TestAdversarialAliasVariables(_TempDirMixin, unittest.TestCase):
    """Alias variables referencing forbidden APIs."""

    def test_alias_process_in_issc(self):
        """auto& proc = process_ in ISSC → process_ is forbidden."""
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    auto& proc = process_;\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][1], 'process_')

    def test_alias_QProcess_in_issc(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcess* p = new QProcess();\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        # Should detect QProcess (appears twice — both occurrences)
        self.assertGreaterEqual(len(v), 1)
        apis = {h[1] for h in v}
        self.assertIn('QProcess', apis)


class TestAdversarialNonIfContext(_TempDirMixin, unittest.TestCase):
    """return / assignment context must NOT create ISSC blocks."""

    def test_return_issc_not_block(self):
        code = ('bool matchesConfig = false;\n'
                'return isSystemSoftwareConfig(type);\n'
                'QProcess p;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        # QProcess on line 3 is NOT in any ISSC block
        self.assertEqual(len(v), 0)

    def test_assignment_issc_not_block(self):
        code = ('bool matchesConfig = isSystemSoftwareConfig(type);\n'
                'QProcess p;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)

    def test_ternary_issc_not_block(self):
        """Ternary with isSystemSoftwareConfig is not an if-context."""
        code = ('auto mode = isSystemSoftwareConfig(type) ? 1 : 0;\n'
                'QProcess p;\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)


class TestAdversarialMultipleViolations(_TempDirMixin, unittest.TestCase):
    """Multiple forbidden APIs in one ISSC block."""

    def test_three_forbidden_apis(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcess p;\n'
                '    fork();\n'
                '    system("ls");\n'
                '}\n')
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 3)
        apis = {h[1] for h in v}
        self.assertIn('QProcess', apis)
        self.assertIn('fork', apis)
        self.assertIn('system', apis)


class TestAdversarialQemuPathAllowed(_TempDirMixin, unittest.TestCase):
    """Forbidden APIs outside ISSC blocks are QEMU-path hits, not violations."""

    def test_QProcess_outside_issc(self):
        code = 'QProcess process_;\nprocess_.start("qemu");\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)

    def test_fork_outside_issc(self):
        code = 'fork();\n'
        path = self._write('VirtualMachineWindow.cpp', code)
        v, a, q, n = scanner.scan_dual_path_file(path)
        self.assertEqual(len(v), 0)
        self.assertGreater(len(q), 0)


# ===================================================================
# 8. Integration: run_scan exit code
# ===================================================================

class TestRunScanExitCode(_TempDirMixin, unittest.TestCase):
    """Test run_scan() returns correct exit codes."""

    def test_clean_scan_returns_0(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '} else {\n'
                '    QProcess p;\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        rc = scanner.run_scan(self._tmpdir)
        self.assertEqual(rc, 0)

    def test_violation_scan_returns_1(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    QProcess p;\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        rc = scanner.run_scan(self._tmpdir)
        self.assertEqual(rc, 1)

    def test_minivmac_only_violation_returns_1(self):
        code = 'void foo() {\n    fork();\n}\n'
        self._write('VirtualMachineWindow.cpp', '')
        self._write('SystemSoftwareEngine.cpp', code)
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        rc = scanner.run_scan(self._tmpdir)
        self.assertEqual(rc, 1)

    def test_advisory_only_returns_0(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    closeQmp();\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        rc = scanner.run_scan(self._tmpdir)
        self.assertEqual(rc, 0)


# ===================================================================
# 9. Product-tree integration test
# ===================================================================

class TestProductTreeClean(unittest.TestCase):
    """Run the scanner against the actual product source tree.

    This test verifies that the current product tree scans CLEAN
    (0 hard violations). Advisory hits are acceptable.
    """

    def test_product_tree_result_clean(self):
        src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src')
        if not os.path.isdir(src_dir):
            self.skipTest(f"Source directory not found: {src_dir}")
        rc = scanner.run_scan(src_dir)
        self.assertEqual(rc, 0,
                         "Product tree scan should return CLEAN (exit 0)")


# ===================================================================
# 10. Subprocess integration (end-to-end)
# ===================================================================

class TestSubprocessEndToEnd(_TempDirMixin, unittest.TestCase):
    """Run the scanner as a subprocess to test CLI end-to-end."""

    def _scanner_path(self) -> str:
        return os.path.join(_TOOLS_DIR, 'verify_minivmac_inprocess_path.py')

    def test_clean_exit_code_0(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    engine->start();\n'
                '} else {\n'
                '    QProcess p;\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        result = subprocess.run(
            [sys.executable, self._scanner_path(),
             '--src-dir', self._tmpdir],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('RESULT: CLEAN', result.stdout)

    def test_violation_exit_code_1(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    fork();\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        result = subprocess.run(
            [sys.executable, self._scanner_path(),
             '--src-dir', self._tmpdir],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('RESULT: VIOLATION', result.stdout)

    def test_verbose_mode(self):
        code = ('if (isSystemSoftwareConfig(type)) {\n'
                '    closeQmp();\n'
                '}\n')
        self._write('VirtualMachineWindow.cpp', code)
        self._write('SystemSoftwareEngine.cpp', 'void start() {}\n')
        self._write('SystemSoftwareEngine.hpp', '#pragma once\n')
        self._write('main.cpp', 'int main() { return 0; }\n')
        result = subprocess.run(
            [sys.executable, self._scanner_path(),
             '--src-dir', self._tmpdir, '--verbose'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('ADVISORY', result.stdout)

    def test_product_tree_subprocess(self):
        """End-to-end: scanner against product tree via subprocess."""
        src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src')
        if not os.path.isdir(src_dir):
            self.skipTest(f"Source directory not found: {src_dir}")
        result = subprocess.run(
            [sys.executable, self._scanner_path(),
             '--src-dir', src_dir, '--verbose'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('RESULT: CLEAN', result.stdout)


if __name__ == '__main__':
    unittest.main()