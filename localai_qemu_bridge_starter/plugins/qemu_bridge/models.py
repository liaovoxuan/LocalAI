from dataclasses import dataclass, field
from enum import Enum
class TargetPlatform(str, Enum):
    MACOS='macos'; WINDOWS='windows'; LINUX='linux'
@dataclass
class DiskConfig:
    path:str; interface:str='virtio'; format:str|None=None; readonly:bool=False
@dataclass
class VirtualMachineConfig:
    architecture:str='x86_64'; machine:str='q35'; cpu_model:str='max'; cpu_cores:int=2; memory_mb:int=4096; accelerator:str='auto'; firmware:str|None=None; disks:list[DiskConfig]=field(default_factory=list); cdroms:list[str]=field(default_factory=list); extra_args:list[str]=field(default_factory=list)
@dataclass
class ValidationIssue:
    level:str; code:str; message:str
@dataclass
class ConversionResult:
    command:str; config:VirtualMachineConfig; issues:list[ValidationIssue]=field(default_factory=list)
    @property
    def has_errors(self): return any(i.level=='error' for i in self.issues)
