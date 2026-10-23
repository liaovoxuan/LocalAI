# Mini vMac Branch-Level Spawn/PID Scanner — Adversarial Regression Test Report

**Date:** 2026-10-07
**Scanner:** `tools/verify_minivmac_inprocess_path.py`
**Test File:** `tests/test_verify_minivmac_inprocess_path.py`

## Summary

| Metric | Value |
|--------|-------|
| Total test cases | **110** |
| Failures | **0** |
| Product tree scan | **RESULT: CLEAN** |
| Hard violations in product | **0** |
| Advisory hits in product | **1** (closeQmp) |
| QEMU-path hits in product | **139** |

## Test Categories

### 1. `strip_comments_and_strings` (9 tests)
- Line comment suppression
- Block comment suppression
- String literal suppression
- Char literal suppression
- Code preservation
- Escaped quote in string
- Line comment preserves newline
- Multiline block comment
- Raw string literal (documented edge case)

### 2. `find_issc_blocks` (11 tests)
- Simple if block detection
- if/else excludes else branch
- Early return (single-line)
- Early return with value
- `return isSystemSoftwareConfig(...)` NOT treated as ISSC block
- Assignment context NOT treated as ISSC block
- `else if` creates separate ISSC block
- QEMU handler lambda nesting flag
- Multiple ISSC blocks
- Nested braces within ISSC
- No ISSC blocks at all

### 3. `is_hit_in_issc_branch` (5 tests)
- Hit inside ISSC block
- Hit outside ISSC block
- `} else` line: API after `}` not in ISSC
- QEMU-nested flag propagation
- No blocks → always outside

### 4. `scan_dual_path_file` (7 tests)
- Clean ISSC branch (no violations)
- Violation in ISSC branch
- QEMU else branch allowed
- closeQmp advisory (not violation)
- QEMU handler ISSC not violation
- Nonexistent file
- Empty file

### 5. `scan_minivmac_only_file` (4 tests)
- Clean file
- Forbidden API anywhere = violation
- Advisory API allowed
- fork anywhere = violation

### 6. `scan_main_cpp` (2 tests)
- Clean main.cpp
- Violation in system-software branch

### 7. Adversarial Regression Tests

#### 7a. Comment/String False Positives (5 tests)
- Line comment QProcess
- Block comment fork
- String literal system
- String literal popen
- Comment inside ISSC block

#### 7b. Word-Boundary Precision (8 tests)
- QProcessTimer (not matched)
- process_state (not matched)
- filesystem (not matched)
- posix_spawnattr_t (not matched)
- posix_spawn_file_actions_t (not matched)
- execv_compat (not matched)
- systemCall (not matched)
- process_Id (not matched)

#### 7c. Hard Violations (24 tests)
Each forbidden API tested individually in ISSC branch:
QProcess, process_, startDetached, fork, vfork, execv, execve, execvp,
execl, execlp, execle, system, popen, posix_spawn, posix_spawnp,
processId, waitForStarted, waitForFinished, waitForReadyRead,
waitForBytesWritten, readAllStandardError, readAllStandardOutput,
sendQmp, sendMonitorCommand, screendump

#### 7d. Advisory Not Violation (7 tests)
- closeQmp, connectQmp, qmpSocket_, qmpCapabilitiesReady_,
  qmpConnectTimer_, processQmpData
- Advisory-only scan returns exit 0

#### 7e. Control Flow (8 tests)
- else-if QProcess allowed
- else QProcess allowed
- Early return + QProcess allowed
- Early return with brace + QProcess allowed
- Lambda-nested ISSC process_ allowed
- Lambda-nested ISSC fork allowed (QEMU context precedence)
- Nested if inside ISSC (fork = violation)
- Two ISSC blocks: one clean, one dirty

#### 7f. Macro Continuation (2 tests)
- Backslash continuation: QProcess
- Backslash continuation: fork

#### 7g. Alias Variables (2 tests)
- `auto& proc = process_` in ISSC
- `QProcess* p = new QProcess()` in ISSC

#### 7h. Non-If Context (3 tests)
- `return isSystemSoftwareConfig(...)` + QProcess → not violation
- Assignment context + QProcess → not violation
- Ternary context + QProcess → not violation

#### 7i. Multiple Violations (1 test)
- Three forbidden APIs in one ISSC block

#### 7j. QEMU Path Allowed (2 tests)
- QProcess outside ISSC
- fork outside ISSC

### 8. `run_scan` Exit Code (4 tests)
- Clean scan → exit 0
- Violation scan → exit 1
- Mini vMac-only violation → exit 1
- Advisory-only → exit 0

### 9. Product-Tree Integration (1 test)
- `src/` directory scans CLEAN (exit 0)

### 10. Subprocess End-to-End (4 tests)
- Clean → exit 0, stdout contains "RESULT: CLEAN"
- Violation → exit 1, stdout contains "RESULT: VIOLATION"
- Verbose mode → advisory shown
- Product tree subprocess → CLEAN

## Product Tree Scan Results

```
Total isSystemSoftwareConfig blocks scanned: 15
Mini vMac path violations (HARD): 0
Mini vMac path advisories (benign cleanup): 1
QEMU-path hits (allowed, outside isSystemSoftwareConfig): 139

Advisories (harmless QMP cleanup in Mini vMac branch — no gate failure):
  VirtualMachineWindow.cpp:1854 closeQmp :: closeQmp();

RESULT: CLEAN — Mini vMac path has no forbidden spawn/PID APIs
```

## Previously Fixed False-Positive Root Causes (Covered by Tests)

1. **ISSC_IF_RE only matches `if` context** — `return`/assignment/ternary with `isSystemSoftwareConfig()` no longer create ISSC blocks (TestFindIsscBlocks: test_return_statement_NOT_if, test_assignment_NOT_if; TestAdversarialNonIfContext)

2. **QEMU handler lambda marking** — ISSC blocks inside `connect(&process_, ...)` lambdas are flagged as QEMU-nested, preventing false violations (TestFindIsscBlocks: test_qemu_handler_nesting; TestAdversarialControlFlow: test_lambda_nested_issc_process_allowed)

3. **`} else` control flow** — APIs on the `} else {` line after the closing brace are correctly classified as NOT in the ISSC branch (TestIsHitInIsscBranch: test_else_line_api_after_brace_not_in_issc; TestAdversarialControlFlow: test_else_QProcess_allowed)