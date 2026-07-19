from __future__ import annotations

import plistlib
import platform
import shlex
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models import TargetPlatform, VirtualMachineConfig
from .parser import parse_input
from .translator import convert_config


APP_NAME = "QEMU Bridge"
IMAGE_SUFFIXES = {".qcow2", ".img", ".iso", ".raw", ".vhd", ".vhdx"}


class ExitRequested(Exception):
    pass


def main():
    print(f"=== {APP_NAME} ===")
    print("拖入路径时可直接把文件/文件夹拖到窗口里，也可以手动输入路径。")
    print("本工具只解析和生成配置文件，不启动 QEMU/UTM，也不要求本机已安装它们。")
    while True:
        try:
            run_once()
        except ExitRequested:
            print("已退出。")
            return
        except KeyboardInterrupt:
            print("\n已退出。")
            return
        except Exception as exc:
            print(f"转换失败：{exc}")
            print("False")


def run_once():
    target_dir = ask_target_dir()
    source_type = ask_choice("第二步：选择输入文件类型（UTM/QEMU）：", {"utm", "qemu"})
    target_type = ask_choice("第三步：输入目标转换类型（UTM/QEMU）：", {"utm", "qemu"})

    if source_type == "utm" and target_type == "utm":
        print("UTM 转 UTM 没有等价转换意义，已打回。")
        print("False")
        return

    source = ask_source_payload(source_type)
    config = load_source(source, source_type)
    config = prompt_image_paths(config, target_dir)

    if source_type == "qemu" and target_type == "qemu":
        output = write_qemu_output(config, target_dir)
        print(f"已生成 QEMU 命令文件：{output}")
        print("True")
        return

    if source_type == "utm" and target_type == "qemu":
        output = write_qemu_output(strip_utm_only_config(config), target_dir)
        print(f"已生成 QEMU 命令文件：{output}")
        print("True")
        return

    if source_type == "qemu" and target_type == "utm":
        guest_os = ask_choice("请选择客户机系统（linux/windows/macos/other，可回车默认 other）：", {"linux", "windows", "macos", "other"}, default="other")
        output, warnings = write_utm_output(config, target_dir, guest_os)
        print(f"已生成 UTM 配置包：{output}")
        for warning in warnings:
            print(f"提示：{warning}")
        print("True")
        return

    raise ValueError(f"暂不支持 {source_type.upper()} -> {target_type.upper()}。")


def ask_target_dir() -> Path:
    while True:
        raw = ask_text("第一步：拖入转换后的目标文件夹路径：")
        path = Path(clean_path(raw)).expanduser()
        if path.exists() and path.is_dir():
            return path
        if not path.exists():
            create = ask_choice("目标文件夹不存在，是否创建？（y/n）：", {"y", "n"}, default="n")
            if create == "y":
                path.mkdir(parents=True, exist_ok=True)
                return path
        print("请输入有效的目标文件夹。")


def ask_text(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        check_exit(value)
        if value:
            return value


def ask_choice(prompt: str, choices: set[str], default: str | None = None) -> str:
    label = "/".join(sorted(choices))
    while True:
        raw = input(prompt).strip().lower()
        check_exit(raw)
        if not raw and default is not None:
            return default
        if raw in choices:
            return raw
        print(f"请输入：{label}")


def ask_source_payload(source_type: str) -> str:
    mode = ask_choice("第四步：选择输入方式（复制/导入文件）：", {"复制", "导入文件", "copy", "file"})
    if mode in {"导入文件", "file"}:
        return ask_text("请拖入文件，或手动输入文件路径：")
    print("请粘贴配置内容。多行内容粘贴完成后，单独输入 END 并回车。")
    return read_multiline_until_end()


def read_multiline_until_end() -> str:
    lines = []
    while True:
        line = input()
        check_exit(line)
        if line.strip().upper() in {"END", "完成", "DONE"}:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def clean_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    text = strip_wrapping_quotes(text)
    if not looks_like_windows_path(text):
        try:
            parts = shlex.split(text)
            if len(parts) == 1:
                text = parts[0]
        except ValueError:
            text = text.replace("\\ ", " ")
    text = strip_wrapping_quotes(text)

    if text.startswith("file://"):
        parsed = urlparse(text)
        path = unquote(parsed.path)
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        text = path
    else:
        text = unquote(text)
    return text.strip()


def strip_wrapping_quotes(value: str) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def looks_like_windows_path(value: str) -> bool:
    text = str(value or "")
    return len(text) >= 3 and text[1] == ":" and text[2] in {"\\", "/"}


def load_source(source: str, source_type: str) -> VirtualMachineConfig:
    cleaned = clean_path(source)
    path = Path(cleaned).expanduser()
    if source_type == "utm":
        config = parse_input(str(path) if safe_exists(path) else source)
        config.source_format = "utm"
        return normalize_architecture(config)

    if safe_is_file(path):
        command = command_from_file(path)
    else:
        command = source
    config = parse_input(command)
    config.source_format = "qemu"
    return normalize_architecture(config)


def normalize_architecture(config: VirtualMachineConfig) -> VirtualMachineConfig:
    arch = str(config.architecture or "x86_64").lower()
    if arch in {"amd64", "x64"}:
        arch = "x86_64"
    elif arch in {"arm64"}:
        arch = "aarch64"
    config.architecture = arch
    if arch == "aarch64" and config.machine in {"q35", "pc", ""}:
        config.machine = "virt"
    elif arch in {"x86_64", "i386"} and config.machine in {"virt", ""}:
        config.machine = "q35"
    return config


def prompt_image_paths(config: VirtualMachineConfig, target_dir: Path) -> VirtualMachineConfig:
    disks = []
    for index, disk in enumerate(config.disks, start=1):
        new_path = ask_image_path(f"磁盘 {index}", disk.path, target_dir)
        disks.append(replace(disk, path=new_path))
    cdroms = []
    for index, cdrom in enumerate(config.cdroms, start=1):
        cdroms.append(ask_image_path(f"光盘/ISO {index}", cdrom, target_dir))
    config.disks = disks
    config.cdroms = cdroms
    return config


def ask_image_path(label: str, current: str, target_dir: Path) -> str:
    fallback = default_target_image_path(current, target_dir)
    while True:
        prompt = f"{label} 当前路径：{current}\n请输入新镜像地址（回车使用 {fallback}）："
        raw = input(prompt).strip()
        check_exit(raw)
        if not raw:
            return str(fallback)
        cleaned = clean_path(raw)
        if is_probable_image_path(cleaned):
            return cleaned
        print("该输入不像磁盘/ISO 镜像路径，已拒绝。请拖入 .qcow2/.img/.iso/.raw/.vhd/.vhdx 文件，或直接回车使用默认路径。")


def default_target_image_path(current: str, target_dir: Path) -> Path:
    path = Path(clean_path(current or "disk.img"))
    name = path.name or "disk.img"
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        name = f"{name}.img"
    return target_dir / name


def is_probable_image_path(value: str) -> bool:
    path = Path(clean_path(value))
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return True
    try:
        return path.exists() and path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    except (OSError, ValueError):
        return False


def check_exit(value: str):
    if str(value or "").strip().lower() == "/exit":
        raise ExitRequested()


def strip_utm_only_config(config: VirtualMachineConfig) -> VirtualMachineConfig:
    config.source_format = "qemu"
    return normalize_architecture(config)


def write_qemu_output(config: VirtualMachineConfig, target_dir: Path) -> Path:
    target = current_target_platform()
    result = convert_config(normalize_architecture(config), target)
    suffix = ".cmd" if target == TargetPlatform.WINDOWS else ".sh"
    output = target_dir / f"qemu_bridge_converted_{config.architecture}{suffix}"
    body = result.command.strip() + "\n"
    if result.issues:
        warning_lines = [format_issue_comment(issue.message, target) for issue in result.issues]
        body = "\n".join(warning_lines) + "\n" + body
    output.write_text(body, encoding="utf-8")
    if target != TargetPlatform.WINDOWS:
        try:
            output.chmod(0o755)
        except OSError:
            pass
    return output


def current_target_platform() -> TargetPlatform:
    system = platform.system().lower()
    if system == "windows":
        return TargetPlatform.WINDOWS
    if system == "darwin":
        return TargetPlatform.MACOS
    return TargetPlatform.LINUX


def format_issue_comment(message: str, target: TargetPlatform) -> str:
    prefix = "REM" if target == TargetPlatform.WINDOWS else "#"
    return f"{prefix} {message}"


def write_utm_output(config: VirtualMachineConfig, target_dir: Path, guest_os: str) -> tuple[Path, list[str]]:
    config = normalize_architecture(config)
    package = unique_path(target_dir / "QEMU Bridge Converted.utm")
    data_dir = package / "Data"
    data_dir.mkdir(parents=True, exist_ok=False)
    warnings = []

    plist = build_utm_plist(config, guest_os, warnings)
    with (package / "config.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    return package, warnings


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem} {index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def safe_is_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file()
    except (OSError, ValueError):
        return False


def safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def build_utm_plist(config: VirtualMachineConfig, guest_os: str, warnings: list[str]) -> dict:
    arch = config.architecture
    machine = "virt" if arch == "aarch64" else "q35"
    open_gl = guest_os != "linux"
    if guest_os == "linux":
        warnings.append("Linux 客户机未启用 OpenGL 加速，以避免部分发行版或 Mesa 版本显示崩溃。")

    drives = []
    for disk in config.disks:
        drives.append(
            {
                "ImagePath": disk.path,
                "Interface": utm_disk_interface(disk.interface),
                "ReadOnly": bool(disk.readonly),
            }
        )
    for cdrom in config.cdroms:
        drives.append({"ImagePath": cdrom, "Interface": "USB", "ReadOnly": True, "Removable": True})

    network = {"NetworkMode": "Shared", "NetworkCard": "virtio-net-pci"}
    if config.networks:
        first = config.networks[0]
        if first.mac:
            network["MACAddress"] = first.mac

    return {
        "Backend": "QEMU",
        "Name": "QEMU Bridge Converted",
        "System": {
            "Architecture": arch,
            "Target": machine,
            "CPUCores": int(config.cpu_cores or 2),
            "MemorySize": int(config.memory_mb or 4096) * 1024 * 1024,
            "Hypervisor": True,
        },
        "Display": {
            "Hardware": "virtio-gpu-pci" if arch == "aarch64" else "virtio-vga",
            "OpenGL": open_gl,
            "ConsoleOnly": False,
        },
        "Input": {
            "USBTablet": True,
            "USBKeyboard": True,
        },
        "Network": network,
        "Drives": drives,
        "Sharing": build_utm_sharing(config),
        "QEMU": {
            "AdditionalArguments": sanitize_qemu_extra_args(config.extra_args + config.unsupported_args),
        },
    }


def utm_disk_interface(interface: str) -> str:
    mapping = {
        "virtio": "VirtIO",
        "ide": "IDE",
        "sata": "SATA",
        "scsi": "SCSI",
        "nvme": "NVMe",
        "usb": "USB",
    }
    return mapping.get(str(interface or "virtio").lower(), "VirtIO")


def build_utm_sharing(config: VirtualMachineConfig) -> dict:
    if not config.shared_directories:
        return {"DirectoryShareMode": "None"}
    first = config.shared_directories[0]
    return {"DirectoryShareMode": "Single", "DirectorySharePath": first.path}


def sanitize_qemu_extra_args(args: list[str]) -> list[str]:
    blocked = {"-accel", "-machine", "-M", "-cpu", "-smp", "-m", "-drive", "-cdrom", "-netdev", "-device"}
    cleaned = []
    iterator = iter(args)
    for item in iterator:
        if item in blocked:
            next(iterator, None)
            continue
        cleaned.append(str(item))
    return cleaned


def command_from_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("REM "):
            continue
        lines.append(stripped.rstrip("\\^"))
    return " ".join(lines)
