import copy
import shlex

from .models import ConversionResult, TargetPlatform, ValidationIssue
from .validator import validate_config


ACCELERATORS = {
    TargetPlatform.MACOS: "hvf",
    TargetPlatform.WINDOWS: "whpx",
    TargetPlatform.LINUX: "kvm",
}

EXECUTABLES = {
    "x86_64": "qemu-system-x86_64",
    "i386": "qemu-system-i386",
    "aarch64": "qemu-system-aarch64",
    "arm": "qemu-system-arm",
    "ppc": "qemu-system-ppc",
    "ppc64": "qemu-system-ppc64",
    "riscv64": "qemu-system-riscv64",
}


def convert_config(source, target):
    target = TargetPlatform(target)
    config = copy.deepcopy(source)
    issues = validate_config(config, target)

    if config.accelerator in {"auto", "hvf", "whpx", "kvm"}:
        desired = ACCELERATORS[target]
        if config.accelerator not in {"auto", desired}:
            issues.append(ValidationIssue("warning", "accelerator_replaced", f"{config.accelerator} 已替换为 {desired}。"))
        config.accelerator = desired

    if config.cpu_model == "host" and config.accelerator == "tcg":
        issues.append(ValidationIssue("warning", "host_cpu_tcg", "TCG 无法等价使用 host CPU，已改为 max。"))
        config.cpu_model = "max"

    if config.architecture == "aarch64" and config.machine in {"q35", "pc"}:
        issues.append(ValidationIssue("warning", "machine_arch_mismatch", "aarch64 通常应使用 virt 机型，已自动调整。"))
        config.machine = "virt"

    if config.unsupported_args:
        issues.append(ValidationIssue("warning", "unsupported_args", "以下参数无法等价转换，已保留在命令末尾供手动检查：" + " ".join(config.unsupported_args)))

    return ConversionResult(render_qemu_command(config, target), config, issues)


def render_qemu_command(config, target):
    target = TargetPlatform(target)
    args = [
        EXECUTABLES.get(config.architecture, f"qemu-system-{config.architecture}"),
        "-machine",
        f"{config.machine},accel={config.accelerator}",
        "-cpu",
        config.cpu_model,
        "-smp",
        str(config.cpu_cores),
        "-m",
        str(config.memory_mb),
    ]

    args.extend(render_firmware(config))
    for disk in config.disks:
        parts = [f"file={disk.path}", f"if={disk.interface}"]
        if disk.format:
            parts.append(f"format={disk.format}")
        if disk.readonly:
            parts.append("readonly=on")
        args += ["-drive", ",".join(parts)]

    for cdrom in config.cdroms:
        args += ["-cdrom", cdrom]

    for index, network in enumerate(config.networks):
        netdev = normalize_netdev_mode(network.mode)
        net_id = f"net{index}"
        net_parts = [netdev, f"id={net_id}"]
        if network.bridge and netdev in {"bridge", "tap"}:
            net_parts.append(f"br={network.bridge}")
        for fwd in network.hostfwd:
            net_parts.append(f"hostfwd={fwd}")
        args += ["-netdev", ",".join(net_parts)]
        device_parts = [network.model or "virtio-net-pci", f"netdev={net_id}"]
        if network.mac:
            device_parts.append(f"mac={network.mac}")
        args += ["-device", ",".join(device_parts)]

    if config.usb.controller:
        if config.usb.controller == "usb":
            args.append("-usb")
        else:
            args += ["-device", config.usb.controller]
    for device in config.usb.devices:
        args += ["-device", device]

    if config.graphics.vga:
        args += ["-vga", config.graphics.vga]
    if config.graphics.adapter:
        args += ["-device", config.graphics.adapter]
    if config.graphics.display:
        args += ["-display", config.graphics.display]

    if config.spice.enabled:
        parts = []
        if config.spice.port:
            parts.append(f"port={config.spice.port}")
        if config.spice.addr:
            parts.append(f"addr={config.spice.addr}")
        if config.spice.disable_ticketing:
            parts.append("disable-ticketing=on")
        args += ["-spice", ",".join(parts) if parts else "disable-ticketing=on"]

    for share in config.shared_directories:
        args += [
            "-virtfs",
            f"local,path={share.path},mount_tag={share.tag},security_model={share.security_model}",
        ]

    args += config.extra_args
    args += config.unsupported_args
    return render_shell(args, target)


def render_firmware(config):
    args = []
    if config.firmware:
        args += ["-bios", config.firmware]
    if config.efi.code_path:
        args += ["-drive", f"if=pflash,format=raw,readonly=on,file={config.efi.code_path}"]
    if config.efi.vars_path:
        args += ["-drive", f"if=pflash,format=raw,file={config.efi.vars_path}"]
    return args


def render_shell(args, target):
    if target == TargetPlatform.WINDOWS:
        return " ^\n  ".join(quote_windows(value) for value in args)
    return " \\\n  ".join(shlex.quote(str(value)) for value in args)


def quote_windows(value):
    text = str(value)
    if not text:
        return '""'
    if any(ch in text for ch in ' \t"&()^%!<>|') or text.endswith("\\"):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def normalize_netdev_mode(value):
    text = str(value or "user").strip().lower().replace("_", "-")
    mapping = {
        "shared": "user",
        "share": "user",
        "nat": "user",
        "emulated": "user",
        "bridged": "bridge",
        "host": "vmnet-host",
        "host-only": "vmnet-host",
    }
    return mapping.get(text, text or "user")
