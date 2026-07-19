from .models import TargetPlatform, ValidationIssue


SUPPORTED_ACCELERATORS = {
    TargetPlatform.MACOS: {"hvf", "tcg", "auto"},
    TargetPlatform.WINDOWS: {"whpx", "tcg", "auto"},
    TargetPlatform.LINUX: {"kvm", "tcg", "auto"},
}

UNSUPPORTED_GRAPHICS = {
    TargetPlatform.MACOS: {"qxl-vga"},
    TargetPlatform.WINDOWS: set(),
    TargetPlatform.LINUX: set(),
}


def validate_config(config, target):
    target = TargetPlatform(target)
    issues = []

    if config.cpu_cores < 1:
        issues.append(ValidationIssue("error", "invalid_cpu_count", "CPU 核心数必须至少为 1。"))
    if config.memory_mb < 256:
        issues.append(ValidationIssue("warning", "low_memory", "内存低于 256 MB，虚拟机可能无法启动。"))
    if config.accelerator not in SUPPORTED_ACCELERATORS[target]:
        issues.append(ValidationIssue("warning", "unsupported_accelerator", f"{target.value} 不支持 {config.accelerator}，将自动替换为平台默认加速器。"))
    if config.architecture == "aarch64" and config.machine in {"q35", "pc"}:
        issues.append(ValidationIssue("warning", "machine_arch_mismatch", "aarch64 与 q35/pc 不等价，通常需要 virt。"))

    for disk in config.disks:
        if disk.interface in {"nvme", "scsi"}:
            issues.append(ValidationIssue("warning", "disk_interface_check", f"磁盘接口 {disk.interface} 已保留，但目标平台可能需要对应控制器。"))
        if disk.path and disk.path.startswith("~"):
            issues.append(ValidationIssue("warning", "path_needs_expansion", f"路径 {disk.path} 包含 ~，请在目标系统手动确认。"))

    for network in config.networks:
        if network.mode in {"tap", "bridge"}:
            issues.append(ValidationIssue("warning", "network_privilege", f"{network.mode} 网络通常需要管理员权限和宿主机网桥配置。"))
        if network.mode not in {"user", "tap", "bridge", "socket", "vmnet-shared", "vmnet-bridged", "vmnet-host"}:
            issues.append(ValidationIssue("warning", "network_mode_unknown", f"网络模式 {network.mode} 未确认可等价转换。"))

    if config.usb.devices:
        issues.append(ValidationIssue("warning", "usb_passthrough_manual", "USB 设备参数已保留，但不同宿主系统的设备标识不可自动等价转换。"))

    adapter = config.graphics.adapter or config.graphics.vga
    if adapter in UNSUPPORTED_GRAPHICS[target]:
        issues.append(ValidationIssue("warning", "graphics_adapter_check", f"{target.value} 对 {adapter} 支持有限，请手动验证显示后端。"))

    if config.spice.enabled and target == TargetPlatform.MACOS:
        issues.append(ValidationIssue("warning", "spice_on_macos", "macOS 上 SPICE 依赖安装方式差异较大，已保留参数但请手动验证。"))

    if config.shared_directories:
        issues.append(ValidationIssue("warning", "shared_folder_guest_driver", "共享目录需要 guest 内安装 9p/virtiofs 支持，路径不会被 LocalAI 修改。"))

    if config.efi.secure_boot:
        issues.append(ValidationIssue("warning", "secure_boot_manual", "Secure Boot 无法完全自动等价转换，请手动检查 OVMF/变量盘。"))

    return issues
