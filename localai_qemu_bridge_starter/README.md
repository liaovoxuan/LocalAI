# LocalAI QEMU Bridge Starter

第一版代码骨架，适合交给 Codex 接入 LocalAI。

已实现：常见 QEMU 命令解析、统一配置模型、HVF/WHPX/KVM/TCG 映射、风险提示、插件入口草案、CLI 和基础测试。

测试：
```bash
python -m plugins.qemu_bridge.cli --command "qemu-system-x86_64 -machine q35 -accel hvf -cpu host -smp 4 -m 8G" --target windows
```

交给 Codex：读取 LocalAI 真实插件接口，只改适配层；增加 GUI；补真实 UTM .utm 解析；无法等价转换时必须警告；不得启动虚拟机或修改镜像。
