import json
import tempfile
import unittest
from pathlib import Path

from cloudai import CloudAIClient, ConfigStore, HttpResponse


class FakeHttp:
    def __init__(self):
        self.request = None

    def __call__(self, request):
        self.request = request
        return HttpResponse(200, '{"choices":[{"message":{"content":"hello from cloud"}}]}')


class CloudAITest(unittest.TestCase):
    def test_config_request_and_file_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ConfigStore(root)
            config = store.load_cloud_config()
            store.save_cloud_config(config)
            store.set_api_key(config.provider, "test-key")
            context = root / "context.txt"
            context.write_text("project context", encoding="utf-8")
            fake = FakeHttp()
            answer = CloudAIClient(store, fake).ask(({"role": "user", "content": "summarize"},), (context,))
            self.assertEqual(answer, "hello from cloud")
            self.assertEqual(fake.request.url, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(fake.request.headers["Authorization"], "Bearer test-key")
            self.assertIn("project context", json.loads(fake.request.body)["messages"][-1]["content"])
            self.assertEqual(store.get_api_key(config.provider), "test-key")

    def test_custom_provider_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.configure_provider("custom", "https://example.test/v1", "demo-model")
            store.set_api_key("custom", "custom-key")
            fake = FakeHttp()
            CloudAIClient(store, fake).ask(({"role": "user", "content": "hello"},))
            self.assertEqual(fake.request.url, "https://example.test/v1/chat/completions")
            self.assertEqual(fake.request.headers["Authorization"], "Bearer custom-key")


if __name__ == "__main__":
    unittest.main()
