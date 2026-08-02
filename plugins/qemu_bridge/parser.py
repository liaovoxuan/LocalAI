import plistlib
import re
import shlex
from pathlib import Path

from .models import (
    DiskConfig,
    EFIConfig,
    GraphicsConfig,
    NetworkConfig,
    SharedDirectoryConfig,
    SpiceConfig,
    USBConfig,
    VirtualMachineConfig,
)


ARCH = {
    "qemu-system-x86_64": "x86_64",
    "qemu-system-i386": "i386",
    "qemu-system-aarch64": "aarch64",
    "qemu-system-arm": "arm",
    "qemu-system-ppc": "ppc",
    "qemu-system-ppc64": "ppc64",
    "qemu-system-riscv64": "riscv64",
}

ARCH_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86-64": "x86_64",
    "x86": "i386",
    "i686": "i386",
    "arm64": "aarch64",
    "powerpc": "ppc",
    "power-pc": "ppc",
    "ppc32": "ppc",
    "powerpc64": "ppc64",
    "power-pc64": "ppc64",
    "ppc64le": "ppc64",
}

DEFAULT_MACHINES = {
    "x86_64": "q35",
    "i386": "pc",
    "aarch64": "virt",
    "arm": "virt",
    "ppc": "mac99",
    "ppc64": "pseries",
    "riscv64": "virt",
}


def parse_qemu_command(command: str) -> VirtualMachineConfig:
    tokens = split_command(command)
    if not tokens:
        raise ValueError("QEMU command is empty.")

    config = VirtualMachineConfig(architecture=normalize_architecture_name(ARCH.get(Path(tokens[0]).name, "x86_64")))
    i = 1
    while i < len(tokens):
        key = tokens[i]
        value = tokens[i + 1] if i + 1 < len(tokens) else None

        if key == "-m" and value:
            config.memory_mb = parse_memory(value)
            i += 2
        elif key == "-smp" and value:
            config.cpu_cores = parse_smp(value)
            i += 2
        elif key == "-cpu" and value:
            config.cpu_model = value
            i += 2
        elif key in {"-machine", "-M"} and value:
            parse_machine(value, config)
            i += 2
        elif key == "-accel" and value:
            config.accelerator = value.split(",", 1)[0]
            i += 2
        elif key == "-drive" and value:
            drive = parse_drive(value)
            if drive.media == "cdrom":
                config.cdroms.append(drive.path)
            else:
                config.disks.append(drive)
            i += 2
        elif key == "-blockdev" and value:
            config.unsupported_args.extend([key, value])
            i += 2
        elif key == "-cdrom" and value:
            config.cdroms.append(value)
            i += 2
        elif key == "-hda" and value:
            config.disks.append(DiskConfig(value, interface="ide"))
            i += 2
        elif key == "-bios" and value:
            config.firmware = value
            i += 2
        elif key == "-pflash" and value:
            apply_pflash(value, config)
            i += 2
        elif key == "-netdev" and value:
            config.networks.append(parse_netdev(value))
            i += 2
        elif key in {"-nic", "-net"} and value:
            config.networks.append(parse_nic(value))
            i += 2
        elif key == "-device" and value:
            parse_device(value, config)
            i += 2
        elif key == "-usb":
            config.usb.controller = config.usb.controller or "usb"
            i += 1
        elif key == "-usbdevice" and value:
            config.usb.controller = config.usb.controller or "usb"
            config.usb.devices.append(normalize_usbdevice(value))
            i += 2
        elif key == "-vga" and value:
            config.graphics.vga = value
            i += 2
        elif key == "-display" and value:
            config.graphics.display = value
            i += 2
        elif key == "-spice" and value:
            config.spice = parse_spice(value)
            i += 2
        elif key == "-virtfs" and value:
            config.shared_directories.append(parse_virtfs(value))
            i += 2
        elif key == "-fsdev" and value:
            config.unsupported_args.extend([key, value])
            i += 2
        else:
            if key.startswith("-") and value and not value.startswith("-"):
                config.unsupported_args.extend([key, value])
                i += 2
            else:
                config.unsupported_args.append(key)
                i += 1

    return config


def parse_utm_package(path: str | Path) -> VirtualMachineConfig:
    package = Path(path)
    config_path = package / "config.plist" if package.is_dir() else package
    if not config_path.exists():
        raise FileNotFoundError(f"UTM config.plist not found: {config_path}")

    with config_path.open("rb") as handle:
        data = plistlib.load(handle)

    return parse_utm_data(data, package)


def parse_utm_plist_text(text: str) -> VirtualMachineConfig:
    data = plistlib.loads(str(text).encode("utf-8"))
    return parse_utm_data(data, Path.cwd())


def parse_utm_data(data, package: Path) -> VirtualMachineConfig:
    config = VirtualMachineConfig(source_format="utm")
    flat = flatten_plist(data)
    lower = {key.lower(): value for key, value in flat.items()}

    config.architecture = normalize_architecture_name(first_value(lower, ["architecture", "system.architecture", "target.architecture"], "x86_64"))
    config.machine = str(first_value(lower, ["target", "machine", "system.target"], default_machine_for_arch(config.architecture)))
    config.cpu_cores = int_like(first_value(lower, ["cpucount", "cpucores", "cpu.count", "system.cpucount", "system.cpucores"], 2), 2)
    config.memory_mb = normalize_memory_mb(first_value(lower, ["memorysize", "memory", "system.memorysize"], 4096))
    config.cpu_model = normalize_cpu_model(first_value(lower, ["cpu", "cpumodel", "system.cpu"], "max"))
    config.cpu_model = apply_utm_cpu_flags(config.cpu_model, data)
    config.accelerator = parse_utm_accelerator(data)

    parse_utm_drives(data, config, package)
    parse_utm_network(data, config)
    parse_utm_sharing(data, config)
    parse_utm_display(data, config)
    parse_utm_usb(data, config)
    parse_utm_spice(data, config)
    parse_utm_efi(data, config, package)
    parse_utm_qemu_settings(data, config)
    parse_utm_sound(data, config)

    args = collect_qemu_arguments(data)
    if args:
        merge_qemu_arguments(args, config)

    return config


def parse_input(value: str) -> VirtualMachineConfig:
    text = str(value or "").strip()
    if looks_like_plist_text(text):
        return parse_utm_plist_text(text)
    path = Path(text)
    exists = safe_exists(path)
    if text.endswith(".utm") or (exists and path.suffix == ".utm"):
        return parse_utm_package(path)
    if exists and (path.name == "config.plist" or path.suffix == ".plist"):
        return parse_utm_package(path)
    return parse_qemu_command(text)


def looks_like_plist_text(text: str) -> bool:
    value = str(text or "").lstrip()
    return value.startswith("<?xml") or value.startswith("<plist") or value.startswith("<!DOCTYPE plist")


def parse_machine(value: str, config: VirtualMachineConfig):
    parts = split_opts(value)
    if parts:
        config.machine = parts[0]
    for part in parts[1:]:
        if part.startswith("accel="):
            config.accelerator = part.split("=", 1)[1].split(":", 1)[0]


def parse_memory(value: str) -> int:
    text = str(value).strip().lower()
    if text.endswith("g"):
        return int(float(text[:-1]) * 1024)
    if text.endswith("m"):
        return int(float(text[:-1]))
    if text.endswith("k"):
        return max(1, int(float(text[:-1]) / 1024))
    return int(float(text))


def normalize_architecture_name(value) -> str:
    text = str(value or "x86_64").strip().lower().replace(" ", "").replace("-", "_")
    return ARCH_ALIASES.get(text, text or "x86_64")


def default_machine_for_arch(arch: str) -> str:
    return DEFAULT_MACHINES.get(normalize_architecture_name(arch), "q35")


def normalize_memory_mb(value, default: int = 4096) -> int:
    memory = int_like(value, default)
    if memory > 1024 * 1024:
        return max(1, int(memory / 1024 / 1024))
    if memory > 1024 * 64:
        return max(1, int(memory / 1024))
    return max(1, memory)


def parse_smp(value: str) -> int:
    if str(value).isdigit():
        return int(value)
    options = parse_options(value)
    for key in ("cpus", "cores"):
        if key in options:
            return int_like(options[key], 2)
    return 2


def parse_drive(value: str) -> DiskConfig:
    options = parse_options(value)
    path = options.get("file", value)
    media = options.get("media", "disk")
    interface = options.get("if", options.get("interface", "virtio"))
    return DiskConfig(
        path=path,
        interface=interface,
        format=options.get("format"),
        readonly=str(options.get("readonly", "off")).lower() in {"on", "yes", "true"},
        media=media,
    )


def apply_pflash(value: str, config: VirtualMachineConfig):
    if not config.efi.code_path:
        config.efi.code_path = value
    elif not config.efi.vars_path:
        config.efi.vars_path = value
    else:
        config.unsupported_args.extend(["-pflash", value])


def parse_netdev(value: str) -> NetworkConfig:
    parts = split_opts(value)
    mode = parts[0] if parts else "user"
    options = parse_options(value)
    return NetworkConfig(
        mode=mode,
        model="virtio-net-pci",
        bridge=options.get("br") or options.get("bridge"),
        hostfwd=[item.split("=", 1)[1] for item in parts if item.startswith("hostfwd=")],
    )


def parse_nic(value: str) -> NetworkConfig:
    parts = split_opts(value)
    mode = parts[0] if parts else "user"
    options = parse_options(value)
    return NetworkConfig(
        mode=mode,
        model=options.get("model", "virtio-net-pci"),
        mac=options.get("mac"),
        bridge=options.get("br") or options.get("bridge"),
        hostfwd=[item.split("=", 1)[1] for item in parts if item.startswith("hostfwd=")],
    )


def parse_device(value: str, config: VirtualMachineConfig):
    parts = split_opts(value)
    name = parts[0] if parts else value
    options = parse_options(value)
    if name in {"virtio-net-pci", "e1000", "rtl8139", "vmxnet3"}:
        if config.networks:
            config.networks[-1].model = name
            if options.get("mac"):
                config.networks[-1].mac = options["mac"]
        else:
            config.networks.append(NetworkConfig(model=name, mac=options.get("mac")))
    elif name.startswith("usb-") or name in {"qemu-xhci", "nec-usb-xhci", "usb-ehci"}:
        if name in {"qemu-xhci", "nec-usb-xhci", "usb-ehci"}:
            config.usb.controller = name
        else:
            config.usb.devices.append(value)
    elif name in {"virtio-gpu-pci", "virtio-vga", "qxl-vga", "VGA", "ramfb"}:
        config.graphics.adapter = name
        if options.get("max_outputs"):
            config.extra_args.extend(["-device", value])
    elif name.startswith("virtio-9p") or name == "virtio-9p-pci":
        config.extra_args.extend(["-device", value])
    else:
        config.unsupported_args.extend(["-device", value])


def parse_spice(value: str) -> SpiceConfig:
    options = parse_options(value)
    return SpiceConfig(
        enabled=True,
        port=int_like(options.get("port"), None),
        addr=options.get("addr"),
        disable_ticketing=str(options.get("disable-ticketing", "off")).lower() in {"on", "yes", "true"},
    )


def parse_virtfs(value: str) -> SharedDirectoryConfig:
    options = parse_options(value)
    return SharedDirectoryConfig(
        path=options.get("path", ""),
        tag=options.get("mount_tag", options.get("tag", "share")),
        security_model=options.get("security_model", "mapped-xattr"),
    )


def parse_options(value: str) -> dict[str, str]:
    out = {}
    last_key = None
    for part in split_opts(value):
        if "=" in part:
            key, val = part.split("=", 1)
            last_key = key.strip()
            out[last_key] = strip_outer_quotes(val.strip())
        elif last_key:
            out[last_key] = f"{out[last_key]},{strip_outer_quotes(part)}"
    return out


def split_opts(value: str) -> list[str]:
    parts = []
    buf = []
    quote = None
    escaped = False
    for char in str(value):
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if char == "\\":
            buf.append(char)
            escaped = True
            continue
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            continue
        if char == ",":
            part = "".join(buf).strip()
            if part:
                parts.append(strip_outer_quotes(part))
            buf = []
            continue
        buf.append(char)
    part = "".join(buf).strip()
    if part:
        parts.append(strip_outer_quotes(part))
    return parts


def split_command(command: str) -> list[str]:
    text = str(command or "")
    posix = not bool(re.search(r"\b[A-Za-z]:\\", text))
    tokens = shlex.split(text, posix=posix)
    if not posix:
        tokens = [strip_outer_quotes(token) for token in tokens]
    return tokens


def strip_outer_quotes(value: str) -> str:
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def int_like(value, default):
    if value is None:
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def flatten_plist(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            out[name] = item
            out.update(flatten_plist(item, name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(flatten_plist(item, f"{prefix}.{index}"))
    return out


def first_value(flat: dict, keys: list[str], default=None):
    for key in keys:
        if key.lower() in flat and flat[key.lower()] not in (None, ""):
            return flat[key.lower()]
    for key, value in flat.items():
        tail = key.rsplit(".", 1)[-1]
        if tail in [candidate.lower() for candidate in keys] and value not in (None, ""):
            return value
    return default


def parse_utm_drives(data, config: VirtualMachineConfig, package: Path):
    for item in find_dicts_with_keys(data, {"ImagePath", "ImageName", "Name", "Path", "URL"}):
        if not looks_like_utm_drive(item):
            continue
        path = item.get("ImagePath") or item.get("ImageName") or item.get("Path") or item.get("URL") or item.get("Name")
        if not isinstance(path, str):
            continue
        if str(item.get("ImageType") or item.get("Type") or "").lower() in {"bios", "firmware"}:
            config.firmware = resolve_utm_path(path, package)
            continue
        if not is_drive_path(path) and not looks_like_removable_media(item):
            continue
        disk_path = resolve_utm_path(path, package)
        if looks_like_cdrom(path, item):
            if disk_path not in config.cdroms:
                config.cdroms.append(disk_path)
        elif all(d.path != disk_path for d in config.disks):
            interface = normalize_disk_interface(
                item.get("Interface")
                or item.get("InterfaceType")
                or item.get("Bus")
                or item.get("DriveInterface")
                or "virtio"
            )
            readonly = bool(item.get("ReadOnly") or item.get("Readonly") or item.get("Locked"))
            config.disks.append(DiskConfig(disk_path, interface=interface, format=guess_format(path), readonly=readonly))


def parse_utm_network(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"NetworkMode", "NetworkCard", "MACAddress"}):
        if not looks_like_utm_network(item):
            continue
        mode = normalize_network_mode(item.get("NetworkMode") or item.get("Mode") or "user")
        model = str(item.get("NetworkCard") or item.get("Hardware") or "virtio-net-pci")
        mac = item.get("MACAddress")
        bridge = item.get("BridgeInterface") or item.get("Interface")
        config.networks.append(
            NetworkConfig(
                mode=mode,
                model=normalize_network_model(model),
                mac=mac if isinstance(mac, str) else None,
                bridge=bridge if isinstance(bridge, str) else None,
                hostfwd=parse_utm_port_forwards(item),
            )
        )
    if not config.networks and bool(find_key(data, "Network")):
        config.networks.append(NetworkConfig())


def parse_utm_sharing(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"DirectorySharePath", "SharedDirectory", "SharePath"}):
        path = item.get("DirectorySharePath") or item.get("SharedDirectory") or item.get("SharePath")
        if isinstance(path, str) and path:
            config.shared_directories.append(SharedDirectoryConfig(path=path, tag=item.get("Tag", "share")))


def parse_utm_display(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"Display", "DisplayCard", "DisplayType", "RendererBackend", "OpenGL", "Hardware"}):
        if not looks_like_utm_display(item):
            continue
        hardware = item.get("Hardware") or item.get("DisplayCard") or item.get("DisplayType")
        if isinstance(hardware, str):
            config.graphics.adapter = normalize_graphics_adapter(hardware)
        backend = item.get("RendererBackend") or item.get("Backend") or item.get("Display")
        if isinstance(backend, str):
            config.graphics.display = normalize_display_backend(backend)
        ram = item.get("VRAM") or item.get("VRAMSize") or item.get("MemorySize")
        if ram:
            config.graphics.ram_mb = int_like(ram, None)
        break


def parse_utm_usb(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"USB", "USBTablet", "USBKeyboard", "Input", "UsbSharing", "UsbBusSupport", "MaximumUsbShare"}):
        if item.get("USBTablet") or item.get("Tablet") or item.get("USBKeyboard"):
            config.usb.controller = config.usb.controller or "qemu-xhci"
            if "usb-tablet" not in config.usb.devices:
                config.usb.devices.append("usb-tablet")
        if item.get("USB") is True or item.get("USBSharing") is True or item.get("UsbSharing") is True:
            config.usb.controller = config.usb.controller or "qemu-xhci"


def parse_utm_spice(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"Spice", "SPICE", "SpicePort", "ClipboardSharing"}):
        kind = str(item.get("Type") or item.get("DeviceType") or "").lower()
        if item.get("Spice") or item.get("SPICE") or item.get("SpicePort") or kind == "spice":
            config.spice.enabled = True
            config.spice.port = int_like(item.get("SpicePort") or item.get("Port"), config.spice.port)
            config.spice.disable_ticketing = bool(item.get("DisableTicketing") or True)
            return


def parse_utm_efi(data, config: VirtualMachineConfig, package: Path):
    if bool(first_existing_key(data, ["UEFIBoot", "UEFI", "EfiBoot", "EFIBoot"])):
        config.efi.secure_boot = bool(first_existing_key(data, ["SecureBoot", "UEFISecureBoot"]))
    code = first_existing_key(data, ["EFIROMPath", "UEFICodePath", "OVMFCodePath"])
    vars_path = first_existing_key(data, ["EFIVarsPath", "UEFIVariablesPath", "OVMFVarsPath"])
    if isinstance(code, str) and code:
        config.efi.code_path = resolve_utm_path(code, package)
    if isinstance(vars_path, str) and vars_path:
        config.efi.vars_path = resolve_utm_path(vars_path, package)


def parse_utm_qemu_settings(data, config: VirtualMachineConfig):
    qemu = find_key(data, "QEMU")
    if not isinstance(qemu, dict):
        return
    if qemu.get("Hypervisor") is False:
        config.accelerator = "tcg"
    machine_override = qemu.get("MachinePropertyOverride")
    if isinstance(machine_override, str) and machine_override.strip():
        append_machine_properties(config, machine_override)
    if qemu.get("RNGDevice"):
        config.extra_args.extend(["-device", "virtio-rng-pci"])
    if qemu.get("RTCLocalTime"):
        config.extra_args.extend(["-rtc", "base=localtime"])
    # q35/pc usually provide PS/2 plumbing implicitly; forcing i8042 is not
    # accepted by every QEMU build, so keep this as an implicit compatibility setting.
    if qemu.get("UEFIBoot"):
        config.efi.secure_boot = bool(qemu.get("UEFISecureBoot") or qemu.get("SecureBoot"))


def parse_utm_sound(data, config: VirtualMachineConfig):
    for item in find_dicts_with_keys(data, {"Sound", "Audio", "Hardware"}):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("Type") or item.get("DeviceType") or "").lower()
        hardware = item.get("Hardware") or item.get("Audio")
        if hardware and ("sound" in kind or str(hardware).lower() in {"intel-hda", "ich9-intel-hda", "ac97"}):
            config.extra_args.extend(["-device", normalize_sound_device(hardware)])
            if str(hardware).lower() in {"intel-hda", "ich9-intel-hda"}:
                config.extra_args.extend(["-device", "hda-duplex"])
            return


def collect_qemu_arguments(data):
    args = []
    for key in ("QEMUArguments", "AdditionalArguments", "Arguments"):
        value = find_key(data, key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    args.extend(shlex.split(item))
                else:
                    args.append(str(item))
        elif isinstance(value, str):
            args.extend(shlex.split(value))
    return args


def merge_qemu_arguments(args, config: VirtualMachineConfig):
    if not args:
        return
    command = [f"qemu-system-{config.architecture}"] + [str(item) for item in args]
    parsed = parse_qemu_command(" ".join(shlex.quote(item) for item in command))
    merge_config(config, parsed)


def merge_config(target: VirtualMachineConfig, source: VirtualMachineConfig):
    if source.machine != "q35" or target.machine in {"", "q35"}:
        target.machine = source.machine
    if source.cpu_model != "max" or target.cpu_model in {"", "max"}:
        target.cpu_model = source.cpu_model
    if source.cpu_cores != 2 or target.cpu_cores == 2:
        target.cpu_cores = source.cpu_cores
    if source.memory_mb != 4096 or target.memory_mb == 4096:
        target.memory_mb = source.memory_mb
    if source.accelerator != "auto":
        target.accelerator = source.accelerator
    if source.disks:
        target.disks = merge_unique_disks(target.disks, source.disks)
    if source.cdroms:
        target.cdroms = merge_unique_values(target.cdroms, source.cdroms)
    if source.networks:
        target.networks = source.networks
    if source.usb.controller:
        target.usb.controller = source.usb.controller
    if source.usb.devices:
        target.usb.devices = merge_unique_values(target.usb.devices, source.usb.devices)
    if source.graphics.adapter:
        target.graphics.adapter = source.graphics.adapter
    if source.graphics.display:
        target.graphics.display = source.graphics.display
    if source.graphics.vga:
        target.graphics.vga = source.graphics.vga
    if source.spice.enabled:
        target.spice = source.spice
    if source.shared_directories:
        target.shared_directories = source.shared_directories
    if source.efi.code_path:
        target.efi.code_path = source.efi.code_path
    if source.efi.vars_path:
        target.efi.vars_path = source.efi.vars_path
    if source.unsupported_args:
        target.unsupported_args.extend(source.unsupported_args)


def merge_unique_disks(existing, incoming):
    out = list(existing)
    seen = {disk.path for disk in out}
    for disk in incoming:
        if disk.path not in seen:
            out.append(disk)
            seen.add(disk.path)
    return out


def merge_unique_values(existing, incoming):
    out = list(existing)
    for item in incoming:
        if item not in out:
            out.append(item)
    return out


def find_key(value, wanted):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == wanted.lower():
                return item
            found = find_key(item, wanted)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_key(item, wanted)
            if found is not None:
                return found
    return None


def find_dicts_with_keys(value, keys):
    matches = []
    if isinstance(value, dict):
        if keys.intersection(value.keys()):
            matches.append(value)
        for item in value.values():
            matches.extend(find_dicts_with_keys(item, keys))
    elif isinstance(value, list):
        for item in value:
            matches.extend(find_dicts_with_keys(item, keys))
    return matches


def first_existing_key(value, keys):
    for key in keys:
        found = find_key(value, key)
        if found not in (None, ""):
            return found
    return None


def looks_like_utm_drive(item: dict) -> bool:
    kind = str(item.get("Type") or item.get("DeviceType") or item.get("Class") or "").lower()
    if any(token in kind for token in ("drive", "disk", "storage", "cd", "dvd")):
        return True
    return any(key in item for key in ("ImagePath", "ImageName", "DriveInterface", "ReadOnly", "Removable"))


def looks_like_removable_media(item: dict) -> bool:
    text = " ".join(str(item.get(key, "")) for key in ("Type", "DeviceType", "DriveType", "ImageType", "Media", "Name")).lower()
    return bool(item.get("Removable")) or any(token in text for token in ("cd", "dvd", "iso"))


def looks_like_cdrom(path: str, item: dict) -> bool:
    return str(path).lower().endswith(".iso") or looks_like_removable_media(item)


def is_drive_path(path: str) -> bool:
    text = str(path).lower()
    return any(text.endswith(ext) for ext in (".qcow2", ".img", ".iso", ".raw", ".vhd", ".vhdx"))


def resolve_utm_path(path: str, package: Path) -> str:
    text = str(path)
    value = Path(text)
    if value.is_absolute():
        return text
    candidates = [package / "Data" / text, package / text]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return str(candidates[0].resolve())


def looks_like_utm_network(item: dict) -> bool:
    kind = str(item.get("Type") or item.get("DeviceType") or item.get("Class") or "").lower()
    if any(token in kind for token in ("network", "ethernet", "nic")):
        return True
    return any(key in item for key in ("NetworkMode", "NetworkCard", "MACAddress", "PortForward", "PortForwarding"))


def normalize_network_model(value: str) -> str:
    text = str(value or "virtio-net-pci").strip().lower().replace("_", "-")
    mapping = {
        "virtio": "virtio-net-pci",
        "virtio-net": "virtio-net-pci",
        "virtio-net-pci": "virtio-net-pci",
        "e1000": "e1000",
        "rtl8139": "rtl8139",
        "vmxnet3": "vmxnet3",
    }
    return mapping.get(text, text or "virtio-net-pci")


def parse_utm_port_forwards(item: dict) -> list[str]:
    forwards = []
    candidates = item.get("PortForward") or item.get("PortForwarding") or item.get("PortForwards") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if isinstance(candidates, list):
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            proto = str(entry.get("Protocol") or entry.get("Proto") or "tcp").lower()
            host = entry.get("HostPort") or entry.get("ExternalPort") or entry.get("SourcePort")
            guest = entry.get("GuestPort") or entry.get("InternalPort") or entry.get("DestinationPort")
            guest_addr = entry.get("GuestAddress") or entry.get("DestinationAddress") or ""
            if host and guest:
                middle = str(guest_addr) if guest_addr else ""
                forwards.append(f"{proto}::{host}-{middle}:{guest}")
    return forwards


def looks_like_utm_display(item: dict) -> bool:
    kind = str(item.get("Type") or item.get("DeviceType") or item.get("Class") or "").lower()
    if any(token in kind for token in ("display", "gpu", "graphics")):
        return True
    return any(key in item for key in ("Hardware", "DisplayCard", "DisplayType", "RendererBackend", "OpenGL", "VRAM", "VRAMSize"))


def normalize_graphics_adapter(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    mapping = {
        "virtio": "virtio-vga",
        "virtio-gpu": "virtio-gpu-pci",
        "virtio-gpu-pci": "virtio-gpu-pci",
        "virtio-vga": "virtio-vga",
        "virtio-vga-gl": "virtio-vga",
        "virtio-gpu-gl": "virtio-gpu-pci",
        "qxl": "qxl-vga",
        "qxl-vga": "qxl-vga",
        "vga": "VGA",
        "ramfb": "ramfb",
    }
    return mapping.get(text, text or "virtio-vga")


def normalize_display_backend(value: str) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "cocoa": "cocoa",
        "sdl": "sdl",
        "gtk": "gtk",
        "spice": "spice-app",
        "spice-app": "spice-app",
        "none": "none",
    }
    return mapping.get(text, text)


def normalize_cpu_model(value) -> str:
    text = str(value or "max").strip()
    mapping = {
        "Default": "max",
        "default": "max",
    }
    return mapping.get(text, text or "max")


def apply_utm_cpu_flags(cpu_model: str, data) -> str:
    flags = []
    add = first_existing_key(data, ["CPUFlagsAdd"])
    remove = first_existing_key(data, ["CPUFlagsRemove"])
    if isinstance(add, list):
        flags.extend(f"+{item}" for item in add if item)
    if isinstance(remove, list):
        flags.extend(f"-{item}" for item in remove if item)
    if not flags:
        return cpu_model
    return ",".join([cpu_model] + flags)


def parse_utm_accelerator(data) -> str:
    qemu = find_key(data, "QEMU")
    if isinstance(qemu, dict) and qemu.get("Hypervisor") is False:
        return "tcg"
    value = first_existing_key(data, ["Accelerator", "Hypervisor"])
    if value is True:
        return "auto"
    if value is False:
        return "tcg"
    return str(value or "auto").lower()


def append_machine_properties(config: VirtualMachineConfig, properties: str):
    existing = [part for part in str(config.machine or "q35").split(",") if part]
    for part in split_opts(properties):
        if part and part not in existing:
            existing.append(part)
    config.machine = ",".join(existing)


def normalize_sound_device(value) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "intel-hda": "intel-hda",
        "ich9-intel-hda": "ich9-intel-hda",
        "ac97": "AC97",
    }
    return mapping.get(text, text or "intel-hda")


def normalize_usbdevice(value) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "keyboard": "usb-kbd",
        "kbd": "usb-kbd",
        "mouse": "usb-mouse",
        "tablet": "usb-tablet",
    }
    return mapping.get(text, text)


def guess_format(path: str):
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix if suffix in {"qcow2", "raw", "vhd", "vhdx"} else None


def normalize_disk_interface(value) -> str:
    text = str(value or "virtio").strip().lower().replace("_", "-")
    mapping = {
        "virtio": "virtio",
        "virtio-blk": "virtio",
        "virtio-block": "virtio",
        "ide": "ide",
        "sata": "sata",
        "scsi": "scsi",
        "nvme": "nvme",
        "usb": "usb",
    }
    return mapping.get(text, "virtio")


def normalize_network_mode(value) -> str:
    text = str(value or "user").strip().lower().replace("_", "-")
    mapping = {
        "shared": "user",
        "share": "user",
        "nat": "user",
        "user": "user",
        "emulated": "user",
        "bridged": "bridge",
        "bridge": "bridge",
        "tap": "tap",
        "host": "vmnet-host",
        "host-only": "vmnet-host",
        "vmnet-host": "vmnet-host",
        "vmnet-shared": "vmnet-shared",
        "vmnet-bridged": "vmnet-bridged",
    }
    return mapping.get(text, text or "user")
