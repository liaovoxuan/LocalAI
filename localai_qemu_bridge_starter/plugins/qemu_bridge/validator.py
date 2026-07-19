from .models import TargetPlatform, ValidationIssue
SUPPORTED={TargetPlatform.MACOS:{'hvf','tcg'},TargetPlatform.WINDOWS:{'whpx','tcg'},TargetPlatform.LINUX:{'kvm','tcg'}}
def validate_config(c,target):
    out=[]
    if c.cpu_cores<1: out.append(ValidationIssue('error','invalid_cpu_count','CPU 核心数必须至少为 1。'))
    if c.memory_mb<256: out.append(ValidationIssue('warning','low_memory','内存低于 256 MB。'))
    if c.accelerator not in {'auto',*SUPPORTED[target]}: out.append(ValidationIssue('warning','unsupported_accelerator',f'{target.value} 不支持 {c.accelerator}，将自动替换。'))
    if c.architecture=='aarch64' and c.machine=='q35': out.append(ValidationIssue('warning','machine_arch_mismatch','aarch64 通常应使用 virt 机型。'))
    return out
