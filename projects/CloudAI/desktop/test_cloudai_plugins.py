import json
import tempfile
import unittest
from pathlib import Path

from cloudai_agent import CloudAgentSession
from cloudai_plugins import CloudAIPluginHost


class DummyApp:
    def __init__(self):
        self.notices = []

    def show_plugin_notice(self, message, error=False):
        self.notices.append((message, error))


class CloudAIPluginTests(unittest.TestCase):
    def test_plugin_registers_settings_action_and_agent_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_dir = root / "sample"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(json.dumps({
                "id": "sample",
                "name": "Sample",
                "version": "1.0.0",
                "api_version": 1,
                "entry": "plugin.py",
            }), encoding="utf-8")
            (plugin_dir / "plugin.py").write_text(
                "def register(api):\n"
                "    api.register_action('Ping', ping)\n"
                "    api.register_tool('reverse', reverse, parameters={'type': 'object'})\n"
                "def ping(arguments, context):\n"
                "    context.notify('pong')\n"
                "def reverse(arguments, context):\n"
                "    return {'value': arguments.get('value', '')[::-1]}\n",
                encoding="utf-8",
            )
            app = DummyApp()
            host = CloudAIPluginHost(app, [str(root)])
            self.assertEqual(host.records[0]["status"], "loaded")
            self.assertEqual(len(host.actions), 1)
            self.assertEqual(host.invoke_tool("plugin.sample.reverse", {"value": "abc"}), {"value": "cba"})
            host.invoke_action(host.actions[0])
            self.assertEqual(app.notices, [("pong", False)])

    def test_plugin_tool_is_confirmed_by_agent_protocol(self):
        calls = []

        def model_call(messages):
            calls.append(messages)
            if len(calls) == 1:
                return '{"action":"plugin.demo.echo","arguments":{"value":"ok"}}'
            return '{"action":"final","content":"done"}'

        session = CloudAgentSession(
            model_call,
            {"language": "zh_cn"},
            confirm_callback=lambda action, payload: True,
            extra_tools=[{
                "action": "plugin.demo.echo",
                "description": "Echo",
                "parameters": {"type": "object"},
            }],
            extra_tool_executor=lambda action, arguments: {"echo": arguments.get("value")},
        )
        self.assertIn("done", session.run("test"))
        self.assertGreaterEqual(len(calls), 2)
        session.extra_tool_executor = lambda _action, _arguments: "ok"
        scalar_result = session.execute({
            "action": "plugin.demo.echo",
            "arguments": {"value": "ok"},
        })
        self.assertEqual(scalar_result, {"ok": True, "result": "ok"})


if __name__ == "__main__":
    unittest.main()
