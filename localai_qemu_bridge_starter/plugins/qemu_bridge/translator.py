import copy, shlex
from .models import ConversionResult, TargetPlatform
from .validator import validate_config
ACC={TargetPlatform.MACOS:'hvf',TargetPlatform.WINDOWS:'whpx',TargetPlatform.LINUX:'kvm'}
EXE={'x86_64':'qemu-system-x86_64','aarch64':'qemu-system-aarch64','arm':'qemu-system-arm','ppc':'qemu-system-ppc','ppc64':'qemu-system-ppc64','riscv64':'qemu-system-riscv64'}
def convert_config(source,target):
    c=copy.deepcopy(source); issues=validate_config(c,target)
    if c.accelerator!='tcg': c.accelerator=ACC[target]
    if c.cpu_model=='host' and c.accelerator=='tcg': c.cpu_model='max'
    if c.architecture=='aarch64' and c.machine=='q35': c.machine='virt'
    return ConversionResult(render_qemu_command(c,target),c,issues)
def render_qemu_command(c,target):
    a=[EXE.get(c.architecture,f'qemu-system-{c.architecture}'),'-machine',f'{c.machine},accel={c.accelerator}','-cpu',c.cpu_model,'-smp',str(c.cpu_cores),'-m',str(c.memory_mb)]
    if c.firmware: a += ['-bios',c.firmware]
    for d in c.disks:
        p=[f'file={d.path}',f'if={d.interface}']
        if d.format: p.append(f'format={d.format}')
        if d.readonly: p.append('readonly=on')
        a += ['-drive',','.join(p)]
    for cd in c.cdroms: a += ['-cdrom',cd]
    a += c.extra_args
    if target==TargetPlatform.WINDOWS: return ' ^\n  '.join(_qw(x) for x in a)
    return ' \\
  '.join(shlex.quote(x) for x in a)
def _qw(v): return '"'+v.replace('"','\\"')+'"' if any(ch in v for ch in ' \t"&()') else v
