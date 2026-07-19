import shlex
from pathlib import Path
from .models import DiskConfig, VirtualMachineConfig
ARCH={'qemu-system-x86_64':'x86_64','qemu-system-aarch64':'aarch64','qemu-system-arm':'arm','qemu-system-ppc':'ppc','qemu-system-ppc64':'ppc64','qemu-system-riscv64':'riscv64'}
def parse_qemu_command(command:str)->VirtualMachineConfig:
    t=shlex.split(command,posix=True)
    if not t: raise ValueError('QEMU command is empty.')
    c=VirtualMachineConfig(architecture=ARCH.get(Path(t[0]).name,'x86_64')); i=1
    while i<len(t):
        x=t[i]
        if x=='-m' and i+1<len(t): c.memory_mb=_mem(t[i+1]); i+=2
        elif x=='-smp' and i+1<len(t): c.cpu_cores=_smp(t[i+1]); i+=2
        elif x=='-cpu' and i+1<len(t): c.cpu_model=t[i+1]; i+=2
        elif x=='-machine' and i+1<len(t):
            p=t[i+1].split(','); c.machine=p[0]
            for q in p[1:]:
                if q.startswith('accel='): c.accelerator=q.split('=',1)[1]
            i+=2
        elif x=='-accel' and i+1<len(t): c.accelerator=t[i+1].split(',',1)[0]; i+=2
        elif x=='-drive' and i+1<len(t): c.disks.append(_drive(t[i+1])); i+=2
        elif x=='-cdrom' and i+1<len(t): c.cdroms.append(t[i+1]); i+=2
        elif x in {'-bios','-pflash'} and i+1<len(t): c.firmware=t[i+1]; i+=2
        else:
            c.extra_args.append(x)
            if x.startswith('-') and i+1<len(t) and not t[i+1].startswith('-'): c.extra_args.append(t[i+1]); i+=2
            else: i+=1
    return c
def _mem(v):
    v=v.lower(); return int(float(v[:-1])*1024) if v.endswith('g') else int(float(v[:-1])) if v.endswith('m') else int(v)
def _smp(v):
    if v.isdigit(): return int(v)
    for p in v.split(','):
        if p.startswith(('cpus=','cores=')): return int(p.split('=',1)[1])
    return 2
def _drive(v):
    d={}
    for p in v.split(','):
        if '=' in p:
            k,val=p.split('=',1); d[k]=val
    return DiskConfig(d.get('file',v),d.get('if','virtio'),d.get('format'),d.get('readonly','off') in {'on','yes','true'})
