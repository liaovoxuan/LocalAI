from __future__ import annotations

import copy
import plistlib
import re
import shlex
from dataclasses import replace
from pathlib import Path

from .models import ConversionResult, DiskConfig, TargetPlatform, ValidationIssue, VirtualMachineConfig
from .parser import parse_input
from .standalone import build_utm_plist
from .translator import convert_config


TUTORIAL = """QEMU Bridge 转换教程：
1. QEMU 输出必须是一条完整 qemu-system-* 命令，不能包含解释文字。
2. UTM 输出必须保留 QEMU 可表达的硬件：架构、机型、CPU、内存、磁盘、ISO、网络、显示、USB、SPICE、共享目录和 EFI。
3. UTM 专属字段不能直接写进 QEMU 命令；无法等价转换的字段必须转为兼容性警告或 AdditionalArguments。
4. 架构必须匹配：x86_64/i386 使用 q35 或 pc；aarch64/arm64 使用 virt。
5. 镜像路径必须保留或按用户要求替换，不能静默删除磁盘。
6. 生成内容必须可被 QEMU Bridge 重新解析。"""


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
    if ai_candidate:
        try:
            checked = validate_candidate(ai_candidate, target_format)
            if not checked.has_errors:
                return checked
        except Exception:
            pass

    config = apply_instruction_rules(original, user_instruction)
    if target_format == "utm":
        issues = validate_utm_plist(build_utm_plist(config, guess_guest_os(user_instruction), []))
        return ConversionResult(render_utm_json(config, user_instruction), config, issues)
    target = platform_from_instruction(user_instruction)
    return convert_config(config, target)


def validate_candidate(candidate: str, target_format: str) -> ConversionResult:
    if target_format == "utm":
        config = parse_input(candidate) if candidate.lstrip().startswith("qemu-system-") else parse_minimal_utm_json(candidate)
        issues = validate_utm_plist(build_utm_plist(config, guess_guest_os(candidate), []))
        return ConversionResult(render_utm_json(config, candidate), config, issues)
    if not candidate.lstrip().startswith("qemu-system-"):
        raise ValueError("QEMU candidate must start with qemu-system-*.")
    config = parse_input(candidate)
    result = convert_config(config, platform_from_instruction(candidate))
    if not result.config.disks and config.disks:
        result.issues.append(ValidationIssue("error", "disk_lost", "转换后磁盘配置丢失。"))
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
        config.architecture = arch
    if config.architecture in {"aarch64", "arm64"}:
        config.architecture = "aarch64"
        config.machine = "virt"
    elif config.architecture in {"x86_64", "amd64", "x64", "i386"}:
        config.architecture = "x86_64" if config.architecture in {"amd64", "x64"} else config.architecture
        if config.machine in {"virt", ""}:
            config.machine = "q35"

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
    config.architecture = str(system.get("Architecture", "x86_64")).lower()
    config.machine = str(system.get("Target", "virt" if config.architecture == "aarch64" else "q35"))
    config.cpu_cores = int(system.get("CPUCores", 2))
    memory = int(system.get("MemorySize", 4096))
    config.memory_mb = int(memory / 1024 / 1024) if memory > 1024 * 1024 else memory
    for item in data.get("Drives", []):
        path = item.get("ImagePath") or item.get("Path")
        if path:
            config.disks.append(DiskConfig(path=str(path), interface=str(item.get("Interface", "virtio")).lower()))
    return config


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
