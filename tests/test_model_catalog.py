"""共用模型连接离线测试：只使用假密钥和模拟模型列表，不访问真实服务商。"""
import copy
import json
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

catalog = import_module("0model_catalog")
service_module = import_module("0ui_service")


def response(data, status=200):
    """构造支持上下文管理的假 HTTP 响应。"""
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.json.return_value = data
    return result


class CatalogTests(unittest.TestCase):
    """检查官方目标限制、模型过滤、分页和安全错误提示。"""

    def test_provider_filtering(self):
        """GPT-6 Sol 可选择，排除专用或已停止的模型，Gemini 必须支持文本生成。"""
        self.assertIsNotNone(catalog.text_model("openai", {"id": "gpt-6-sol"}))
        for identifier in ("gpt-image-1", "text-embedding-3-small", "gpt-4o-audio-preview", "o3-pro", "gpt-5-codex", "gpt-3.5-turbo-instruct"):
            self.assertIsNone(catalog.text_model("openai", {"id": identifier}))
        self.assertIsNone(catalog.text_model("openai", {"id": "gpt-4", "shutdown_date": "2000-01-01"}))
        self.assertIsNone(catalog.text_model("gemini", {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]}))
        self.assertIsNone(catalog.text_model("deepseek", {"id": "gpt-6-sol"}))

    def test_gemini_pages(self):
        """所有分页使用同一官方目标及请求头，密钥不放进 URL。"""
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = [response({"models": [{"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "next"}), response({"models": [{"name": "models/gemini-other", "supportedGenerationMethods": ["generateContent"]}]})]
        with patch.object(catalog.requests, "Session", return_value=session):
            models = catalog.fetch_models("gemini", "fake-google-key")
        self.assertEqual({m["id"] for m in models}, {"gemini-test", "gemini-other"})
        self.assertEqual(session.get.call_count, 2)
        for call in session.get.call_args_list:
            self.assertEqual(call.args[0], catalog.PROVIDERS["gemini"]["models_url"])
            self.assertEqual(call.kwargs["headers"], {"x-goog-api-key": "fake-google-key"})
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_deepseek_auth_and_no_fallback(self):
        """只请求所选服务商，失败不将同一密钥发送到另一家。"""
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response({"data": [{"id": "deepseek-flash", "name": "DeepSeek Flash"}]})
        with patch.object(catalog.requests, "Session", return_value=session):
            self.assertEqual(catalog.fetch_models("deepseek", "fake-sk")[0]["id"], "deepseek-flash")
        self.assertEqual(session.get.call_args.args[0], "https://api.deepseek.com/models")
        self.assertEqual(session.get.call_args.kwargs["headers"], {"Authorization": "Bearer fake-sk"})
        session.get.reset_mock()
        session.get.return_value = response({"error": "fake-sk"}, 401)
        with patch.object(catalog.requests, "Session", return_value=session):
            with self.assertRaisesRegex(ValueError, "API Key 无效") as caught:
                catalog.fetch_models("deepseek", "fake-sk")
        self.assertNotIn("fake-sk", str(caught.exception))
        session.get.assert_called_once()

    def test_cross_provider_never_reuses_secret(self):
        """OpenAI 与 DeepSeek 不能互用已有密钥，也不复用中转密钥。"""
        old = {"provider": "chat_completions", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "api_key": "fake-openai"}
        with patch.dict(catalog.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "请输入"):
                catalog.resolve_key("deepseek", "", old)
            self.assertEqual(catalog.resolve_key("openai", "", old), "fake-openai")
            old["base_url"] = "https://gateway.invalid/v1"
            with self.assertRaisesRegex(ValueError, "请输入"):
                catalog.resolve_key("openai", "", old)


class SharedConnectionTests(unittest.TestCase):
    """检查一把密钥的同步、版本选择、失败回滚和不落盘。"""

    def setUp(self):
        """使用临时偏好文件和隔离环境变量，避免读取或修改用户密钥。"""
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(catalog.os.environ, {}, clear=True)
        self.env.start()
        self.service = service_module.UIService(Path(self.temp.name) / "settings.json")
        self.models = [{"id": "gpt-6-sol", "label": "gpt-6-sol"}, {"id": "gpt-6-luna", "label": "gpt-6-luna"}]

    def tearDown(self):
        """恢复环境变量并清理测试文件。"""
        self.env.stop()
        self.temp.cleanup()

    def connect(self, provider="openai", key="fake-shared-secret"):
        """模拟成功读取列表后应用共用连接。"""
        with patch.object(catalog, "fetch_models", return_value=self.models):
            return self.service.connect_models({"provider": provider, "api_key": key})

    def test_shared_key_and_independent_versions(self):
        """两个角色同密钥同地址但型号不同，模型列表和磁盘不包含真实密钥。"""
        self.connect()
        result = self.service.select_models({"selection": {"screening": "gpt-6-luna", "qa": "gpt-6-sol"}})
        for role in ("screening", "qa"):
            self.assertEqual(self.service.settings["models"][role]["api_key"], "fake-shared-secret")
            self.assertEqual(self.service.settings["models"][role]["base_url"], "https://api.openai.com/v1")
        self.assertEqual(self.service.settings["models"]["screening"]["model"], "gpt-6-luna")
        self.assertNotIn("fake-shared-secret", json.dumps(result))
        self.assertNotIn("fake-shared-secret", self.service.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(len(self.service.snapshot()["connection"]["models"]), 2)
        self.assertFalse(service_module.UIService(self.service.settings_path).snapshot()["connection"]["models"])

    def test_failed_connection_and_unknown_model_keep_settings(self):
        """错误密钥、网络失败或不在列表的型号均不覆盖之前能用的连接。"""
        self.connect()
        before = copy.deepcopy(self.service.settings)
        with patch.object(catalog, "fetch_models", side_effect=ValueError("无法连接")):
            with self.assertRaises(ValueError):
                self.service.connect_models({"provider": "deepseek", "api_key": "fake-new"})
        with self.assertRaises(ValueError):
            self.service.select_models({"selection": {"screening": "made-up-model", "qa": "gpt-6-sol"}})
        self.assertEqual(self.service.settings, before)

    def test_blank_key_provider_switch_and_gemini_embedding(self):
        """跨服务商留空拒绝提交，Gemini 密钥同时供内部 Gemini 向量接口使用。"""
        self.connect()
        with patch.object(catalog, "fetch_models") as fetch:
            with self.assertRaises(ValueError):
                self.service.connect_models({"provider": "deepseek", "api_key": ""})
            fetch.assert_not_called()
        self.models = [{"id": "gemini-test", "label": "Gemini Test"}]
        self.connect("gemini", "fake-gemini")
        self.assertEqual(self.service.settings["models"]["embedding"]["api_key"], "fake-gemini")
        self.assertEqual(self.service.settings["models"]["qa"]["api_key"], "fake-gemini")


if __name__ == "__main__":
    unittest.main()
