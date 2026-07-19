from plugins.qemu_bridge.models import TargetPlatform
from plugins.qemu_bridge.parser import parse_qemu_command
from plugins.qemu_bridge.translator import convert_config
def test_hvf_to_whpx():
    r=convert_config(parse_qemu_command('qemu-system-x86_64 -machine q35 -accel hvf -cpu host -smp 4 -m 8G'),TargetPlatform.WINDOWS)
    assert 'accel=whpx' in r.command and '8192' in r.command
def test_aarch64_machine_fix():
    r=convert_config(parse_qemu_command('qemu-system-aarch64 -machine q35 -cpu max -m 4096'),TargetPlatform.LINUX)
    assert 'virt,accel=kvm' in r.command
