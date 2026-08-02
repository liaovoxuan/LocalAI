from __future__ import annotations

import copy
import plistlib
import re
import shlex
from dataclasses import replace
from pathlib import Path

from .models import ConversionResult, DiskConfig, TargetPlatform, ValidationIssue, VirtualMachineConfig
from .parser import default_machine_for_arch, normalize_architecture_name, normalize_memory_mb, parse_input
from .standalone import build_utm_plist
from .translator import convert_config


TUTORIAL = """QEMU Bridge 转换教程：
1. QEMU 输出必须是一条完整 qemu-system-* 命令，不能包含解释文字。
2. UTM 输出必须保留 QEMU 可表达的硬件：架构、机型、CPU、内存、磁盘、ISO、网络、显示、USB、SPICE、共享目录和 EFI。
3. UTM 专属字段不能直接写进 QEMU 命令；无法等价转换的字段必须转为兼容性警告或 AdditionalArguments。
4. 架构必须匹配：x86_64/i386 使用 q35 或 pc；aarch64/arm64 使用 virt；powerpc/ppc 使用 mac99/g3beige/prep；ppc64 使用 pseries/mac99。
5. 镜像路径必须保留或按用户要求替换，不能静默删除磁盘。
6. 如果用户没有要求修改架构、内存、CPU 核心或磁盘，必须保留原配置中的这些值。
7. 生成内容必须可被 QEMU Bridge 重新解析。"""


def build_ai_prompt(source_text: str, target_format: str, user_instruction: str) -> str:
    return f"""{TUTORIAL}

用户要转换成：{target_format.upper()}
用户指令：
{user_instruction.strip() or "按当前配置进行等价转换。"}

原始配置：
{source_text.strip()}

请只输出最终配置。
如果目标是 QEMU，只输出 qemu-system-* 命令。
如果目标是 UTM，只输出 JSON 对象，字段尽量接近 UTM config.plist，可包含 System、Drives、Network、Display、Input、Sharing、QEMU。
不要输出 Markdown、解释、列表或多余文字。"""


def modify_with_ai_or_rules(source_text: str, target_format: str, user_instruction: str, ai_text: str = "") -> ConversionResult:
    target_format = normalize_format(target_format)
    original = parse_input(source_text)
    ai_candidate = extract_candidate(ai_text)
    rejection_issues = []
    if ai_candidate:
        try:
            checked = validate_candidate(ai_candidate, target_format, original, user_instruction)
            if not checked.has_errors:
                return checked
            rejection_issues = checked.issues
        except Exception as exc:
            rejection_issues = [ValidationIssue("warning", "ai_candidate_rejected", f"AI 输出无法通过转换检查，已改用程序转换：{exc}")]

    config = apply_instruction_rules(original, user_instruction)
    if target_format == "utm":
        warnings = []
        issues = validate_utm_plist(build_utm_plist(config, guess_guest_os(user_instruction), warnings))
        issues = [ValidationIssue("warning", "utm_conversion_note", item) for item in warnings] + issues
        return ConversionResult(render_utm_json(config, user_instruction), config, rejection_issues + issues)
    target = platform_from_instruction(user_instruction)
    result = convert_config(config, target)
    result.issues = rejection_issues + result.issues
    return result


def validate_candidate(candidate: str, target_format: str, original: VirtualMachineConfig | None = None, user_instruction: str = "") -> ConversionResult:
    if target_format == "utm":
        config = parse_input(candidate) if candidate.lstrip().startswith("qemu-system-") else parse_minimal_utm_json(candidate)
        warnings = []
        issues = validate_utm_plist(build_utm_plist(config, guess_guest_os(candidate), warnings))
        issues = [ValidationIssue("warning", "utm_conversion_note", item) for item in warnings] + issues
        issues.extend(evaluate_candidate_against_request(original, config, user_instruction))
        return ConversionResult(render_utm_json(config, candidate), config, issues)
    if not candidate.lstrip().startswith("qemu-system-"):
        raise ValueError("QEMU candidate must start with qemu-system-*.")
    config = parse_input(candidate)
    result = convert_config(config, platform_from_instruction(user_instruction or candidate))
    if not result.config.disks and config.disks:
        result.issues.append(ValidationIssue("error", "disk_lost", "转换后磁盘配置丢失。"))
    result.issues.extend(evaluate_candidate_against_request(original, result.config, user_instruction))
    return result


def normalize_format(value: str) -> str:
    text = str(value or "").strip().lower()
    if text not in {"qemu", "utm"}:
        raise ValueError("target_format must be QEMU or UTM.")
    return text


def extract_candidate(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    fenced = re.search(r"```(?:\w+)?\s*(.*?)```", raw, flags=re.S)
    if fenced:
        raw = fenced.group(1).strip()
    if "qemu-system-" in raw:
        return raw[raw.find("qemu-system-") :].strip()
    brace = raw.find("{")
    if brace >= 0:
        return raw[brace:].strip()
    return raw


def apply_instruction_rules(config: VirtualMachineConfig, instruction: str) -> VirtualMachineConfig:
    config = copy.deepcopy(config)
    text = str(instruction or "").lower()
    arch = detect_arch(text)
    if arch:
        config.architecture = normalize_architecture_name(arch)
    else:
        config.architecture = normalize_architecture_name(config.architecture)
    if config.architecture == "aarch64":
        config.machine = "virt"
    elif config.architecture in {"x86_64", "i386"}:
        if config.machine in {"virt", ""}:
            config.machine = "q35"
    elif config.architecture in {"ppc", "ppc64"} and machine_base(config.machine) in {"q35", "pc", "virt", ""}:
        config.machine = default_machine_for_arch(config.architecture)

    memory = detect_memory_mb(text)
    if memory:
        config.memory_mb = memory
    cpu_cores = detect_cpu_cores(text)
    if cpu_cores:
        config.cpu_cores = cpu_cores
    disk_path = detect_path(instruction)
    if disk_path and config.disks:
        config.disks[0] = replace(config.disks[0], path=disk_path)
    return config


def detect_arch(text: str) -> str | None:
    if any(item in text for item in ("ppc64", "powerpc64", "power pc 64")):
        return "ppc64"
    if any(item in text for item in ("powerpc", "power pc", "ppc")):
        return "ppc"
    if any(item in text for item in ("aarch64", "arm64", "apple silicon", "arm")):
        return "aarch64"
    if any(item in text for item in ("x86_64", "amd64", "x64")):
        return "x86_64"
    if "i386" in text or "x86" in text:
        return "i386"
    return None


def detect_memory_mb(text: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(gb|g|mb|m)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    return int(value * 1024) if unit in {"gb", "g"} else int(value)


def detect_cpu_cores(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:core|cores|核|线程|cpu)", text)
    if not match:
        return None
    return max(1, int(match.group(1)))


def detect_path(instruction: str) -> str | None:
    try:
        parts = shlex.split(str(instruction or ""))
    except ValueError:
        parts = str(instruction or "").split()
    for part in parts:
        if Path(part).suffix.lower() in {".qcow2", ".img", ".raw", ".vhd", ".vhdx"}:
            return part
    return None


def platform_from_instruction(text: str) -> TargetPlatform:
    lowered = str(text or "").lower()
    if "windows" in lowered or "whpx" in lowered:
        return TargetPlatform.WINDOWS
    if "linux" in lowered or "kvm" in lowered:
        return TargetPlatform.LINUX
    return TargetPlatform.MACOS


def guess_guest_os(text: str) -> str:
    lowered = str(text or "").lower()
    for name in ("linux", "windows", "macos"):
        if name in lowered:
            return name
    return "other"


def parse_minimal_utm_json(candidate: str) -> VirtualMachineConfig:
    import json

    data = json.loads(candidate)
    system = data.get("System", data)
    config = VirtualMachineConfig(source_format="utm")
    config.architecture = normalize_architecture_name(system.get("Architecture", "x86_64"))
    config.machine = str(system.get("Target", default_machine_for_arch(config.architecture)))
    config.cpu_cores = int(system.get("CPUCores") or system.get("CPUCount") or 2)
    config.memory_mb = normalize_memory_mb(system.get("MemorySize", 4096))
    for item in data.get("Drives", []):
        path = item.get("ImagePath") or item.get("Path")
        if path:
            config.disks.append(DiskConfig(path=str(path), interface=str(item.get("Interface", "virtio")).lower()))
    return config


def evaluate_candidate_against_request(original: VirtualMachineConfig | None, candidate: VirtualMachineConfig, instruction: str) -> list[ValidationIssue]:
    if original is None:
        return []
    issues = []
    text = str(instruction or "").lower()
    requested_arch = detect_arch(text)
    expected_arch = normalize_architecture_name(requested_arch or original.architecture)
    actual_arch = normalize_architecture_name(candidate.architecture)
    if actual_arch != expected_arch:
        issues.append(ValidationIssue("error", "architecture_mismatch", f"架构不匹配：期望 {expected_arch}，实际 {actual_arch}。"))

    requested_memory = detect_memory_mb(text)
    expected_memory = requested_memory or original.memory_mb
    if expected_memory and int(candidate.memory_mb or 0) != int(expected_memory):
        issues.append(ValidationIssue("error", "memory_mismatch", f"内存不匹配：期望 {expected_memory} MB，实际 {candidate.memory_mb} MB。"))

    requested_cores = detect_cpu_cores(text)
    expected_cores = requested_cores or original.cpu_cores
    if expected_cores and int(candidate.cpu_cores or 0) != int(expected_cores):
        issues.append(ValidationIssue("error", "cpu_count_mismatch", f"CPU 核心数不匹配：期望 {expected_cores}，实际 {candidate.cpu_cores}。"))

    if original.disks and len(candidate.disks) < len(original.disks) and not detect_path(instruction):
        issues.append(ValidationIssue("error", "disk_lost", "AI 转换结果缺少原配置中的磁盘。"))
    requested_path = detect_path(instruction)
    if requested_path and all(disk.path != requested_path for disk in candidate.disks):
        issues.append(ValidationIssue("error", "disk_path_mismatch", f"用户要求的镜像路径未写入：{requested_path}。"))

    expected_machine = detect_machine(text)
    if expected_machine and machine_base(candidate.machine) != expected_machine:
        issues.append(ValidationIssue("error", "machine_mismatch", f"机型不匹配：期望 {expected_machine}，实际 {candidate.machine}。"))
    elif expected_arch in {"ppc", "ppc64"}:
        original_machine = machine_base(original.machine)
        actual_machine = machine_base(candidate.machine)
        invalid = {"q35", "pc", "virt", ""}
        if actual_machine in invalid:
            issues.append(ValidationIssue("error", "machine_arch_mismatch", f"{expected_arch} 不能使用 {actual_machine or '空'} 机型。"))
        elif original_machine and original_machine not in invalid and actual_machine != original_machine:
            issues.append(ValidationIssue("error", "machine_mismatch", f"PPC 机型被意外改变：期望 {original_machine}，实际 {actual_machine}。"))

    if issues:
        issues.insert(0, ValidationIssue("warning", "ai_candidate_rejected", "AI 输出未通过原配置和用户要求的一致性检查，已改用程序转换。"))
    return issues


def detect_machine(text: str) -> str | None:
    for machine in ("mac99", "g3beige", "pseries", "prep", "q35", "pc", "virt"):
        if machine in text:
            return machine
    return None


def machine_base(machine: str) -> str:
    return str(machine or "").split(",", 1)[0].strip()


def validate_utm_plist(data: dict) -> list[ValidationIssue]:
    issues = []
    try:
        plistlib.dumps(data, sort_keys=False)
    except Exception as exc:
        issues.append(ValidationIssue("error", "invalid_utm_plist", f"UTM 配置无法写入 plist：{exc}"))
    system = data.get("System", {})
    if not system.get("Architecture"):
        issues.append(ValidationIssue("error", "missing_architecture", "UTM 配置缺少架构。"))
    if not data.get("Drives"):
        issues.append(ValidationIssue("warning", "missing_drives", "UTM 配置没有磁盘。"))
    return issues


def render_utm_json(config: VirtualMachineConfig, instruction: str) -> str:
    import json

    plist = build_utm_plist(config, guess_guest_os(instruction), [])
    return json.dumps(plist, ensure_ascii=False, indent=2)
