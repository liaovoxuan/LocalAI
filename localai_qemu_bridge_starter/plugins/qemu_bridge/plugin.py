from .models import TargetPlatform
from .parser import parse_qemu_command
from .translator import convert_config
class QEMUBridgePlugin:
    plugin_id='localai.qemu_bridge'; name='QEMU Bridge'; version='0.1.0'
    def register(self,host): host.register_tool(name='qemu_bridge.convert_command',callback=self.convert_command,description='将 QEMU 命令转换为 macOS、Windows 或 Linux 版本。')
    def convert_command(self,command,target_platform):
        r=convert_config(parse_qemu_command(command),TargetPlatform(target_platform.lower()))
        return {'command':r.command,'has_errors':r.has_errors,'issues':[{'level':i.level,'code':i.code,'message':i.message} for i in r.issues]}
