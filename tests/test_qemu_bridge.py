import json
import plistlib

from plugins.qemu_bridge.models import TargetPlatform
from plugins.qemu_bridge.plugin import QEMUBridgePlugin, load_imported_source, program_convert_result
from plugins.qemu_bridge.parser import parse_input, parse_qemu_command, parse_utm_package
from plugins.qemu_bridge.ai_modify import build_ai_prompt, modify_with_ai_or_rules
from plugins.qemu_bridge.standalone import ExitRequested, check_exit, clean_path, command_from_file, is_probable_image_path, load_source, write_qemu_output, write_utm_output
from plugins.qemu_bridge.translator import convert_config


def test_hvf_to_whpx():
    result = convert_config(
        parse_qemu_command("qemu-system-x86_64 -machine q35 -accel hvf -cpu host -smp 4 -m 8G"),
        TargetPlatform.WINDOWS,
    )
    assert "accel=whpx" in result.command
    assert "8192" in result.command


def test_aarch64_machine_fix():
    result = convert_config(
        parse_qemu_command("qemu-system-aarch64 -machine q35 -cpu max -m 4096"),
        TargetPlatform.LINUX,
    )
    assert "virt,accel=kvm" in result.command


def test_network_usb_graphics_spice_share_and_efi_are_rendered():
    command = (
        "qemu-system-x86_64 -machine q35 -accel hvf -cpu host -smp 4 -m 4G "
        "-drive file=/vm/disk.qcow2,if=virtio,format=qcow2 "
        "-netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0,mac=52:54:00:12:34:56 "
        "-device qemu-xhci -device usb-tablet "
        "-vga virtio -display cocoa "
        "-spice port=5930,disable-ticketing=on "
        "-virtfs local,path=/Users/me/share,mount_tag=share0,security_model=mapped-xattr "
        "-pflash /usr/share/OVMF_CODE.fd -pflash /tmp/OVMF_VARS.fd"
    )
    result = convert_config(parse_qemu_command(command), TargetPlatform.LINUX)
    assert "-netdev" in result.command
    assert "hostfwd=tcp::2222-:22" in result.command
    assert "usb-tablet" in result.command
    assert "-vga" in result.command
    assert "-spice" in result.command
    assert "-virtfs" in result.command
    assert "OVMF_CODE.fd" in result.command
    assert any(issue.code == "shared_folder_guest_driver" for issue in result.issues)


def test_unsupported_device_is_warned_not_silently_dropped():
    result = convert_config(
        parse_qemu_command("qemu-system-x86_64 -m 2048 -device vfio-pci,host=01:00.0"),
        TargetPlatform.MACOS,
    )
    assert "vfio-pci" in result.command
    assert any(issue.code == "unsupported_args" for issue in result.issues)


def test_parse_realistic_utm_package(tmp_path):
    package = tmp_path / "Example.utm"
    data_dir = package / "Data"
    data_dir.mkdir(parents=True)
    config = {
        "System": {
            "Architecture": "aarch64",
            "Target": "virt",
            "CPUCores": 4,
            "MemorySize": 4294967296,
        },
        "Drives": [
            {"ImagePath": "disk.qcow2", "Interface": "VirtIO"},
            {"ImagePath": "installer.iso"},
        ],
        "Network": {"NetworkMode": "Shared", "NetworkCard": "virtio-net-pci", "MACAddress": "52:54:00:aa:bb:cc"},
        "Sharing": {"DirectorySharePath": "/Users/me/share"},
        "QEMU": {"AdditionalArguments": ["-device", "virtio-rng-pci"]},
    }
    with (package / "config.plist").open("wb") as handle:
        plistlib.dump(config, handle)

    parsed = parse_utm_package(package)
    result = convert_config(parsed, TargetPlatform.MACOS)

    assert parsed.architecture == "aarch64"
    assert parsed.cpu_cores == 4
    assert parsed.memory_mb == 4096
    assert parsed.disks and parsed.disks[0].path.endswith("disk.qcow2")
    assert parsed.cdroms and parsed.cdroms[0].endswith("installer.iso")
    assert parsed.networks
    assert parsed.shared_directories
    assert "virtio-rng-pci" in result.command


def test_parse_modern_utm_devices_and_qemu_arguments(tmp_path):
    package = tmp_path / "Modern.utm"
    data_dir = package / "Data"
    data_dir.mkdir(parents=True)
    (data_dir / "disk-0.qcow2").write_text("", encoding="utf-8")
    (data_dir / "installer.iso").write_text("", encoding="utf-8")
    config = {
        "Architecture": "x86_64",
        "Target": "q35",
        "MemorySize": 2147483648,
        "Devices": [
            {"Type": "Drive", "ImageName": "disk-0.qcow2", "Interface": "SATA"},
            {"Type": "Drive", "ImageName": "installer.iso", "Removable": True, "Interface": "USB"},
            {
                "Type": "Network",
                "NetworkMode": "Shared",
                "NetworkCard": "e1000",
                "MACAddress": "52:54:00:11:22:33",
                "PortForwarding": [{"Protocol": "tcp", "HostPort": 2222, "GuestPort": 22}],
            },
            {"Type": "Display", "Hardware": "virtio-vga", "RendererBackend": "cocoa", "OpenGL": True},
            {"Type": "Input", "USBTablet": True},
            {"Type": "SPICE", "SpicePort": 5930, "ClipboardSharing": True},
        ],
        "UEFIBoot": True,
        "QEMU": {"AdditionalArguments": ["-smp", "6", "-m", "3G", "-device", "virtio-rng-pci"]},
    }
    with (package / "config.plist").open("wb") as handle:
        plistlib.dump(config, handle)

    parsed = parse_utm_package(package)
    result = convert_config(parsed, TargetPlatform.MACOS)

    assert parsed.cpu_cores == 6
    assert parsed.memory_mb == 3072
    assert parsed.disks[0].interface == "sata"
    assert parsed.cdroms and parsed.cdroms[0].endswith("installer.iso")
    assert parsed.networks[0].model == "e1000"
    assert parsed.networks[0].hostfwd == ["tcp::2222-:22"]
    assert parsed.graphics.adapter == "virtio-vga"
    assert parsed.graphics.display == "cocoa"
    assert parsed.usb.controller == "qemu-xhci"
    assert parsed.spice.enabled is True
    assert "hostfwd=tcp::2222-:22" in result.command
    assert "virtio-rng-pci" in result.command


def test_parse_raw_utm_plist_xml_from_user_example(tmp_path):
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    for name in (
        "OVMF.bin",
        "EFI-LEGACY.qcow2",
        "disk-0.qcow2",
        "7642317D-5857-4CBB-B999-EFBCD4CE9BAA.qcow2",
        "E32C7FA0-DB04-467F-9AC8-46AB5D64D04A.qcow2",
    ):
        (data_dir / name).write_text("", encoding="utf-8")
    plist_data = {
        "Backend": "QEMU",
        "Display": [{"Hardware": "virtio-vga-gl", "DynamicResolution": True}],
        "Drive": [
            {"Identifier": "0", "ImageName": "OVMF.bin", "ImageType": "BIOS", "Interface": "None", "ReadOnly": False},
            {"Identifier": "4", "ImageType": "CD", "Interface": "IDE", "ReadOnly": True},
            {"Identifier": "1", "ImageName": "EFI-LEGACY.qcow2", "ImageType": "Disk", "Interface": "USB", "ReadOnly": False},
            {"Identifier": "3", "ImageName": "disk-0.qcow2", "ImageType": "Disk", "Interface": "IDE", "ReadOnly": False},
            {"Identifier": "764", "ImageName": "7642317D-5857-4CBB-B999-EFBCD4CE9BAA.qcow2", "ImageType": "Disk", "Interface": "USB", "ReadOnly": True},
            {"Identifier": "E32", "ImageName": "E32C7FA0-DB04-467F-9AC8-46AB5D64D04A.qcow2", "ImageType": "Disk", "Interface": "IDE", "ReadOnly": False},
        ],
        "Input": {"UsbBusSupport": "2.0", "UsbSharing": True},
        "Network": [],
        "QEMU": {
            "AdditionalArguments": ["-usbdevice keyboard"],
            "Hypervisor": False,
            "MachinePropertyOverride": "vmport=off",
            "PS2Controller": True,
            "RNGDevice": True,
            "RTCLocalTime": True,
            "UEFIBoot": True,
        },
        "Sharing": {"ClipboardSharing": True, "DirectoryShareMode": "None"},
        "Sound": [{"Hardware": "intel-hda"}],
        "System": {
            "Architecture": "x86_64",
            "CPU": "Penryn",
            "CPUCount": 4,
            "CPUFlagsAdd": ["ssse3", "sse4.1", "sse4.2"],
            "CPUFlagsRemove": [],
            "MemorySize": 4096,
            "Target": "q35",
        },
    }
    xml = plistlib.dumps(plist_data).decode("utf-8")
    parsed = parse_input(xml)
    result = convert_config(parsed, TargetPlatform.MACOS)

    assert parsed.cpu_cores == 4
    assert parsed.cpu_model == "Penryn,+ssse3,+sse4.1,+sse4.2"
    assert parsed.accelerator == "tcg"
    assert parsed.machine == "q35,vmport=off"
    assert parsed.firmware.endswith("OVMF.bin")
    assert len(parsed.disks) == 4
    assert parsed.disks[1].interface == "ide"
    assert parsed.disks[2].readonly is True
    assert parsed.graphics.adapter == "virtio-vga"
    assert parsed.usb.controller == "usb"
    assert "usb-kbd" in parsed.usb.devices
    assert "version=1.0" not in result.command
    assert "encoding=UTF-8" not in result.command
    assert "Penryn,+ssse3,+sse4.1,+sse4.2" in result.command
    assert "q35,vmport=off,accel=tcg" in result.command
    assert "usb-kbd" in result.command
    assert "i8042" not in result.command
    assert "-spice" not in result.command
    assert "intel-hda" in result.command
    assert "hda-duplex" in result.command


def test_standalone_load_source_accepts_copied_utm_plist_xml():
    xml = plistlib.dumps(
        {
            "System": {"Architecture": "x86_64", "CPUCount": 4, "MemorySize": 4096, "Target": "q35"},
            "Drive": [{"ImageName": "disk-0.qcow2", "ImageType": "Disk", "Interface": "IDE"}],
        }
    ).decode("utf-8")
    parsed = load_source(xml, "utm")
    assert parsed.source_format == "utm"
    assert parsed.cpu_cores == 4
    assert parsed.disks[0].path.endswith("disk-0.qcow2")


def test_utm_config_plist_path_is_parsed(tmp_path):
    package = tmp_path / "Example.utm"
    package.mkdir()
    config_path = package / "config.plist"
    with config_path.open("wb") as handle:
        plistlib.dump({"System": {"Architecture": "x86_64", "MemorySize": 2147483648}}, handle)

    parsed = parse_input(str(config_path))
    assert parsed.source_format == "utm"
    assert parsed.memory_mb == 2048


def test_utm_shared_network_renders_as_qemu_user_network(tmp_path):
    package = tmp_path / "Network.utm"
    package.mkdir()
    with (package / "config.plist").open("wb") as handle:
        plistlib.dump({"Network": {"NetworkMode": "Shared", "NetworkCard": "virtio-net-pci"}}, handle)

    result = convert_config(parse_utm_package(package), TargetPlatform.MACOS)
    assert "-netdev" in result.command
    assert "user,id=net0" in result.command
    assert "shared,id=net0" not in result.command


def test_utm_disk_interface_is_preserved_when_supported(tmp_path):
    package = tmp_path / "Disk.utm"
    data_dir = package / "Data"
    data_dir.mkdir(parents=True)
    with (package / "config.plist").open("wb") as handle:
        plistlib.dump({"Drives": [{"ImagePath": "legacy.img", "Interface": "IDE"}]}, handle)

    parsed = parse_utm_package(package)
    assert parsed.disks[0].interface == "ide"


def test_multiple_networks_use_unique_ids():
    command = (
        "qemu-system-x86_64 -m 2G "
        "-netdev user,id=n0 -device virtio-net-pci,netdev=n0 "
        "-netdev user,id=n1 -device e1000,netdev=n1"
    )
    result = convert_config(parse_qemu_command(command), TargetPlatform.LINUX)
    assert "user,id=net0" in result.command
    assert "user,id=net1" in result.command
    assert result.command.count("id=net0") == 1


def test_quoted_comma_inside_drive_path_is_not_split():
    parsed = parse_qemu_command('qemu-system-x86_64 -drive file="/tmp/vm,disk.qcow2",if=virtio,format=qcow2')
    assert parsed.disks[0].path == "/tmp/vm,disk.qcow2"
    assert parsed.disks[0].interface == "virtio"
    assert parsed.disks[0].format == "qcow2"


def test_windows_style_path_is_kept_during_parse():
    parsed = parse_qemu_command(r"qemu-system-x86_64 -drive file=C:\VMs\disk.qcow2,if=virtio -m 1G")
    assert parsed.disks[0].path == r"C:\VMs\disk.qcow2"


def test_plugin_registers_tool_and_gui_action():
    class Host:
        def __init__(self):
            self.tools = {}
            self.gui_actions = []

        def register_tool(self, name, callback, description=""):
            self.tools[name] = callback

        def register_gui_action(self, plugin_id, label_key, callback):
            self.gui_actions.append((plugin_id, label_key, callback))

    host = Host()
    QEMUBridgePlugin().register(host)
    assert "qemu_bridge.convert_command" in host.tools
    assert host.gui_actions
    payload = host.tools["qemu_bridge.convert_command"]("qemu-system-x86_64 -m 1G", "linux")
    assert "qemu-system-x86_64" in payload["command"]


def test_standalone_writes_qemu_command_with_updated_image_path(tmp_path):
    config = parse_qemu_command("qemu-system-x86_64 -m 1G -drive file=/old/disk.qcow2,if=virtio")
    config.disks[0].path = str(tmp_path / "disk.qcow2")
    output = write_qemu_output(config, tmp_path)
    body = output.read_text(encoding="utf-8")
    assert output.exists()
    assert str(tmp_path / "disk.qcow2") in body
    assert "qemu-system-x86_64" in body


def test_standalone_writes_utm_package_and_disables_linux_opengl(tmp_path):
    config = parse_qemu_command("qemu-system-aarch64 -machine virt -m 2G -drive file=/vm/linux.qcow2,if=virtio")
    package, warnings = write_utm_output(config, tmp_path, "linux")
    plist = plistlib.load((package / "config.plist").open("rb"))
    assert package.suffix == ".utm"
    assert plist["System"]["Architecture"] == "aarch64"
    assert plist["Display"]["OpenGL"] is False
    assert warnings


def test_standalone_command_file_reader_strips_comments_and_line_continuation(tmp_path):
    command_file = tmp_path / "run.sh"
    command_file.write_text(
        "# comment\nqemu-system-x86_64 \\\n  -m 1G \\\n  -drive file=/vm/disk.qcow2,if=virtio\n",
        encoding="utf-8",
    )
    command = command_from_file(command_file)
    assert "# comment" not in command
    assert "qemu-system-x86_64" in command
    assert "-drive file=/vm/disk.qcow2,if=virtio" in command


def test_standalone_clean_path_accepts_dragged_macos_path():
    assert clean_path(r"/Users/me/My\ VM/测试.utm") == "/Users/me/My VM/测试.utm"


def test_standalone_clean_path_accepts_file_uri_and_percent_encoding():
    assert clean_path("file:///Users/me/My%20VM/%E6%B5%8B%E8%AF%95.utm") == "/Users/me/My VM/测试.utm"


def test_standalone_clean_path_keeps_windows_dragged_path():
    assert clean_path(r'"C:\Users\me\My VM\run.cmd"') == r"C:\Users\me\My VM\run.cmd"


def test_standalone_rejects_non_image_replacement_text():
    assert is_probable_image_path("/tmp/disk.qcow2")
    assert not is_probable_image_path("/tmp/Start Boot Option")


def test_standalone_exit_command_raises():
    try:
        check_exit("/exit")
    except ExitRequested:
        return
    raise AssertionError("/exit should request shutdown")


def test_ai_modify_prompt_contains_tutorial_and_user_instruction():
    prompt = build_ai_prompt("qemu-system-x86_64 -m 1G", "qemu", "改成 4GB 内存")
    assert "QEMU Bridge 转换教程" in prompt
    assert "改成 4GB 内存" in prompt
    assert "qemu-system-x86_64" in prompt


def test_ai_modify_uses_valid_model_qemu_output():
    result = modify_with_ai_or_rules(
        "qemu-system-x86_64 -m 1G",
        "qemu",
        "转换为 Linux 平台",
        "qemu-system-x86_64 -machine q35 -cpu max -m 2048",
    )
    assert "qemu-system-x86_64" in result.command
    assert "2048" in result.command
    assert not result.has_errors


def test_ai_modify_falls_back_when_model_output_is_invalid():
    result = modify_with_ai_or_rules(
        "qemu-system-x86_64 -m 1G",
        "qemu",
        "改成 4GB 内存，目标 Linux",
        "这不是有效配置",
    )
    assert "4096" in result.command
    assert "accel=kvm" in result.command
    assert not result.has_errors


def test_ai_modify_generates_valid_utm_json():
    result = modify_with_ai_or_rules(
        "qemu-system-aarch64 -machine virt -m 2G -drive file=/vm/linux.qcow2,if=virtio",
        "utm",
        "转换为 UTM，Linux 客户机",
        "",
    )
    data = json.loads(result.command)
    plistlib.dumps(data, sort_keys=False)
    assert data["System"]["Architecture"] == "aarch64"


def test_plugin_program_convert_qemu_uses_selected_target_platform():
    result = program_convert_result("qemu-system-x86_64 -m 1G", "qemu", "linux")
    assert "accel=kvm" in result.command
    assert not result.has_errors


def test_plugin_program_convert_utm_generates_saveable_json():
    result = program_convert_result("qemu-system-aarch64 -machine virt -m 2G", "utm", "macos")
    data = json.loads(result.command)
    plistlib.dumps(data, sort_keys=False)
    assert data["System"]["Architecture"] == "aarch64"


def test_plugin_program_convert_uses_fixed_utm_parser_for_plist_xml():
    xml = plistlib.dumps(
        {
            "Drive": [
                {"ImageName": "disk-0.qcow2", "ImageType": "Disk", "Interface": "IDE"},
            ],
            "QEMU": {"Hypervisor": False, "PS2Controller": True},
            "Sharing": {"ClipboardSharing": True},
            "System": {
                "Architecture": "x86_64",
                "CPU": "Penryn",
                "CPUCount": 4,
                "CPUFlagsAdd": ["ssse3"],
                "MemorySize": 4096,
                "Target": "q35",
            },
        }
    ).decode("utf-8")
    result = program_convert_result(xml, "qemu", "macos")
    assert "version=1.0" not in result.command
    assert "encoding=UTF-8" not in result.command
    assert "i8042" not in result.command
    assert "-spice" not in result.command
    assert "Penryn,+ssse3" in result.command
    assert "disk-0.qcow2" in result.command


def test_plugin_load_imported_source_reads_plist_content(tmp_path):
    plist_path = tmp_path / "config.plist"
    plist_path.write_text("<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict/></plist>", encoding="utf-8")
    assert load_imported_source(str(plist_path)).startswith("<?xml")
