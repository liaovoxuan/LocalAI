from dataclasses import dataclass, field
from enum import Enum


class TargetPlatform(str, Enum):
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"


@dataclass
class DiskConfig:
    path: str
    interface: str = "virtio"
    format: str | None = None
    readonly: bool = False
    media: str = "disk"


@dataclass
class NetworkConfig:
    mode: str = "user"
    model: str = "virtio-net-pci"
    mac: str | None = None
    bridge: str | None = None
    hostfwd: list[str] = field(default_factory=list)


@dataclass
class USBConfig:
    controller: str | None = None
    devices: list[str] = field(default_factory=list)


@dataclass
class GraphicsConfig:
    adapter: str | None = None
    display: str | None = None
    vga: str | None = None
    ram_mb: int | None = None


@dataclass
class SpiceConfig:
    enabled: bool = False
    port: int | None = None
    addr: str | None = None
    disable_ticketing: bool = False


@dataclass
class SharedDirectoryConfig:
    path: str
    tag: str = "share"
    security_model: str = "mapped-xattr"


@dataclass
class EFIConfig:
    code_path: str | None = None
    vars_path: str | None = None
    secure_boot: bool | None = None


@dataclass
class VirtualMachineConfig:
    architecture: str = "x86_64"
    machine: str = "q35"
    cpu_model: str = "max"
    cpu_cores: int = 2
    memory_mb: int = 4096
    accelerator: str = "auto"
    firmware: str | None = None
    disks: list[DiskConfig] = field(default_factory=list)
    cdroms: list[str] = field(default_factory=list)
    networks: list[NetworkConfig] = field(default_factory=list)
    usb: USBConfig = field(default_factory=USBConfig)
    graphics: GraphicsConfig = field(default_factory=GraphicsConfig)
    spice: SpiceConfig = field(default_factory=SpiceConfig)
    shared_directories: list[SharedDirectoryConfig] = field(default_factory=list)
    efi: EFIConfig = field(default_factory=EFIConfig)
    extra_args: list[str] = field(default_factory=list)
    unsupported_args: list[str] = field(default_factory=list)
    source_format: str = "qemu"


@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str


@dataclass
class ConversionResult:
    command: str
    config: VirtualMachineConfig
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self):
        return any(issue.level == "error" for issue in self.issues)
