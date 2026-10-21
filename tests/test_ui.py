"""交互界面离线测试：覆盖配置隔离、密钥不落盘、本机访问控制和真实预览子进程。"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

service_module = import_module("0ui_service")
web = import_module("0web_ui")


class UITests(unittest.TestCase):
    """本机交互服务测试，不调用真实模型或 Scholar。"""

    def setUp(self):
        """为每条测试创建独立偏好文件，避免覆盖用户实际界面设置。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = service_module.UIService(self.root / "settings.json")

    def tearDown(self):
        """停止仍在运行的测试任务，再删除测试目录。"""
        self.service.stop()
        self.temp.cleanup()

    def wait_for_job(self):
        """等待短时离线任务和采集线程完成，不依赖固定启动时长。"""
        deadline = time.monotonic() + 15
        while self.service.snapshot()["job"]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertNotEqual(self.service.snapshot()["job"]["status"], "running")

    def test_api_key_never_persisted_or_returned(self):
        """真实输入密钥仅存在服务内存，不进入偏好 JSON、状态接口或日志。"""
        key = "fake-private-key-for-ui-test"
        self.service.update({"models": {"qa": {"api_key": key}}, "qa": {"question": "独立问题"}}, persist=True)
        saved = self.service.settings_path.read_text(encoding="utf-8")
        self.assertNotIn(key, saved)
        self.assertNotIn(key, json.dumps(self.service.snapshot()))
        self.assertEqual(self.service.settings["models"]["qa"]["api_key"], key)
        self.assertIn("[密钥已隐藏]", self.service.redact("error " + key))
        restored = service_module.UIService(self.service.settings_path)
        self.assertEqual(restored.settings["qa"]["question"], "独立问题")
        self.assertEqual(restored.settings["models"]["qa"]["api_key"], "")

    def test_blank_key_retained_and_explicit_clear(self):
        """密码框空白保留当前密钥，清除开关才移除输入密钥。"""
        self.service.update({"models": {"qa": {"api_key": "fake-private-key"}}})
        self.service.update({"models": {"qa": {"api_key": ""}}})
        self.assertEqual(self.service.settings["models"]["qa"]["api_key"], "fake-private-key")
        self.service.update({"models": {"qa": {"clear_api_key": True}}})
        self.assertEqual(self.service.settings["models"]["qa"]["api_key"], "")

    def test_invalid_config_does_not_change_current_settings(self):
        """年份、问题、向量模型或数值类型不合法时拒绝保存并保留原配置。"""
        original = self.service.snapshot()["settings"]
        for update in ({"research": {"year_start": 2028, "year_end": 2020}},
                       {"qa": {"question": "x" * 4001}}, {"knowledge": {"top_k": "six"}},
                       {"models": {"embedding": {"model": "chat-model"}}}):
            with self.assertRaises(ValueError):
                self.service.update(update, persist=True)
        self.assertEqual(self.service.snapshot()["settings"], original)
        self.assertFalse(self.service.settings_path.exists())

    def test_ui_settings_do_not_mutate_cli_defaults(self):
        """编辑界面配置不改写 user_settings 全局对象，避免污染命令行或其他用户实例。"""
        before = service_module.user_settings.QA_SETTINGS.question
        self.service.update({"qa": {"question": "界面提问"}})
        self.assertEqual(service_module.user_settings.QA_SETTINGS.question, before)

    def test_retired_reading_preferences_do_not_reset_current_settings(self):
        """载入旧设置时移除退役阅读项，同时保留用户关键词和现有模型选择。"""
        saved = service_module.public_settings(self.service.settings, disk=True)
        saved["research"]["keywords"] = "preserve my research"
        saved["reading"] = {"input_dir": "old-path", "output_dir": "old-reports"}
        saved["models"]["reading"] = {"api_key": "obsolete-test-key"}
        self.service.settings_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = service_module.UIService(self.service.settings_path)
        self.assertEqual(restored.load_error, "")
        self.assertEqual(restored.settings["research"]["keywords"], "preserve my research")
        self.assertNotIn("reading", restored.settings)
        self.assertNotIn("reading", restored.settings["models"])
        with self.assertRaisesRegex(ValueError, "任务"):
            restored.start("read", {}, {})

    def test_search_source_is_managed_after_restore_and_update(self):
        """旧界面来源选项迁移为 OpenAlex，后续任务配置也始终使用既定主备策略。"""
        saved = service_module.public_settings(self.service.settings, disk=True)
        saved["research"]["search_source"] = "google_scholar"
        self.service.settings_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = service_module.UIService(self.service.settings_path)
        self.assertEqual(restored.settings["research"]["search_source"], "openalex")
        restored.update({"research": {"search_source": "google_scholar"}}, persist=True)
        self.assertEqual(restored.settings["research"]["search_source"], "openalex")
        direct = service_module.default_settings()
        direct["research"]["search_source"] = "google_scholar"
        objects = service_module.configuration_objects(direct)
        self.assertEqual(objects["RESEARCH_SETTINGS"].search_source, "openalex")

    def test_real_worker_dry_run_no_api_no_index(self):
        """启动真实子进程读取界面配置，知识库预览不读取 PDF 内容或创建数据库。"""
        source = self.root / "pdf"
        source.mkdir()
        (source / "test.pdf").write_bytes(b"not-a-real-pdf")
        database = self.root / "index.sqlite3"
        settings = {"download": {"output_dir": str(source)},
                    "knowledge": {"input_dir": str(self.root / "retired-input"), "database": str(database)}}
        self.service.start("index", settings, {"dry_run": True, "limit": 1})
        self.wait_for_job()
        job = self.service.snapshot()["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["stats"]["pending"], 1)
        self.assertIn("test.pdf", "\n".join(job["logs"]))
        self.assertFalse(database.exists())

    def test_pdf_directory_follows_download_path_after_restore_and_update(self):
        """旧输入目录不再生效，更新下载路径后建库沿用新路径，保留数据库与配对文件位置。"""
        saved = service_module.public_settings(self.service.settings, disk=True)
        saved["knowledge"]["input_dir"] = str(self.root / "old-pdf")
        saved["knowledge"]["database"] = str(self.root / "existing.sqlite3")
        saved["download"]["bridge_config"] = str(self.root / "existing-pair.json")
        self.service.settings_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = service_module.UIService(self.service.settings_path)
        self.assertEqual(restored.load_error, "")
        self.assertIsNone(restored.settings["knowledge"]["input_dir"])
        restored.update({"download": {"output_dir": str(self.root / "new-pdf")}}, persist=True)
        objects = service_module.configuration_objects(restored.settings)
        self.assertIsNone(objects["KNOWLEDGE_SETTINGS"].input_dir)
        self.assertEqual(objects["DOWNLOAD_SETTINGS"].output_dir, (self.root / "new-pdf").resolve())
        self.assertEqual(objects["KNOWLEDGE_SETTINGS"].database, (self.root / "existing.sqlite3").resolve())
        self.assertEqual(objects["DOWNLOAD_SETTINGS"].bridge_config, (self.root / "existing-pair.json").resolve())

    def test_only_one_job_and_stop(self):
        """同时启动被拒绝，停止任务会终止并回收子进程。"""
        fake = unittest.mock.Mock()
        fake.poll.return_value = None
        self.service.process = fake
        self.service.job = {"id": "one", "status": "running", "logs": []}
        with self.assertRaisesRegex(ValueError, "已有任务"):
            self.service.start("index", {}, {})
        self.service.stop()
        fake.terminate.assert_called_once()
        fake.wait.assert_called_once()
        self.assertEqual(self.service.job["status"], "cancelled")

    def test_question_result_has_registered_pdf_link(self):
        """问答结果中的页码链接只访问已登记的实际 PDF，不暴露任意本机路径接口。"""
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-test")
        report = self.root / "answer.json"
        report.write_text(json.dumps({"question": "q", "answer": {"claims": [], "limitations": ""},
                                       "evidence": [{"path": str(pdf), "page": 3}]}), encoding="utf-8")
        result = self.service.prepare_result({"stats": {}, "files": [str(report)]}, "ask")
        link = result["answer"]["evidence"][0]["pdf_url"]
        self.assertTrue(link.startswith("/api/file/"))
        self.assertTrue(link.endswith("#page=3"))


class HTTPTests(unittest.TestCase):
    """验证浏览器 API 的来源限制、会话令牌和文件访问范围。"""

    def setUp(self):
        """在系统分配的空闲回环端口启动测试服务。"""
        self.temp = tempfile.TemporaryDirectory()
        service = service_module.UIService(Path(self.temp.name) / "settings.json")
        self.server = web.LocalServer(0, service=service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        """关闭测试服务和监听线程，防止测试结束后遗留端口。"""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        """通过真实本机 HTTP 请求检查状态码和响应，不使用外部网络。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_static_and_state_without_credential_disclosure(self):
        """静态页与状态可加载，但任意本机文件路径不通过服务暴露。"""
        self.assertEqual(self.request("/")[0], 200)
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("token", json.loads(body))
        self.assertEqual(self.request("/../user_settings.py")[0], 404)
        self.assertEqual(self.request("/api/file/unknown")[0], 404)

    def test_mutations_require_token_and_trusted_origin(self):
        """缺少令牌、跨站 Origin 或伪造 Host 均不能修改配置。"""
        route = "/api/settings"
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request(route, "POST", {}, headers)[0], 403)
        headers["X-UI-Token"] = self.server.token
        headers["Origin"] = "https://untrusted.invalid"
        self.assertEqual(self.request(route, "POST", {}, headers)[0], 403)
        headers["Origin"] = self.server.origin
        headers["Host"] = "untrusted.invalid"
        self.assertEqual(self.request(route, "POST", {}, headers)[0], 403)
        headers.pop("Host")
        self.assertEqual(self.request(route, "POST", {}, headers)[0], 200)

    def test_study_routes_require_token_and_reject_stale_project(self):
        """新建切换接口受本机会话保护，旧页面不能把设置写入已切换的新调研。"""
        data = {"name": "HTTP research", "directory": self.temp.name}
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("/api/studies/create", "POST", data, headers)[0], 403)
        headers["X-UI-Token"] = self.server.token
        status, body = self.request("/api/studies/create", "POST", data, headers)
        self.assertEqual(status, 200)
        identifier = json.loads(body)["studies"]["active_id"]
        stale = {"study_id": "original", "settings": {"qa": {"question": "stale question"}}}
        self.assertEqual(self.request("/api/settings", "POST", stale, headers)[0], 400)
        self.assertNotEqual(self.server.service.settings["qa"]["question"], "stale question")
        status, body = self.request("/api/studies/switch", "POST", {"id": "original", "study_id": identifier}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["studies"]["active_id"], "original")

    def test_path_picker_requires_session_and_does_not_save_selection(self):
        """本地浏览需要会话令牌，选择或取消都不直接改写已保存的路径。"""
        before = self.server.service.snapshot()["settings"]
        data = {"field": "download.output_dir", "current": ""}
        headers = {"Content-Type": "application/json"}
        with patch.object(web._path_picker, "choose_path", return_value="D:\\文献资料") as picker:
            self.assertEqual(self.request("/api/browse-path", "POST", data, headers)[0], 403)
            picker.assert_not_called()
            headers["X-UI-Token"] = self.server.token
            status, body = self.request("/api/browse-path", "POST", data, headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["path"], "D:\\文献资料")
            picker.return_value = ""
            self.assertEqual(json.loads(self.request("/api/browse-path", "POST", data, headers)[1])["path"], "")
        self.assertEqual(self.server.service.snapshot()["settings"], before)

    def test_manual_upload_http_and_busy_guard(self):
        """通过真实 HTTP 上传 PDF，验证令牌、目录绑定、重复导入和并发任务保护。"""
        from tests.test_manual_import import pdf_bytes
        from urllib.parse import urlencode
        output = str((Path(self.temp.name) / "pdfs").resolve())
        headers = {"Content-Type": "application/json", "X-UI-Token": self.server.token}
        status, body = self.request("/api/manual-list", "POST", {"settings": {"download": {"output_dir": output}}}, headers)
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(listing["items"], [])
        self.assertEqual(self.request(listing["report"]["url"])[0], 200)
        route = "/api/import-pdf?" + urlencode(dict(filename="test.pdf", output_dir=output))
        def upload(extra_headers):
            """向测试端口提交原始 PDF 内容并读取响应。"""
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            try:
                connection.request("POST", route, body=pdf_bytes(), headers={"Content-Type": "application/pdf", **extra_headers})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        self.assertEqual(upload({})[0], 403)
        status, body = upload({"X-UI-Token": self.server.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "imported")
        self.assertEqual(json.loads(upload({"X-UI-Token": self.server.token})[1])["status"], "skipped")
        self.server.service.job = {"status": "running"}
        self.assertEqual(upload({"X-UI-Token": self.server.token})[0], 400)
        self.server.service.job = None

    def test_shared_model_endpoints(self):
        """真实本机路由需要令牌，连接和选版不泄露密钥，也拒绝列表外型号。"""
        headers = {"Content-Type": "application/json", "X-UI-Token": self.server.token}
        catalog = service_module._catalog
        models = [{"id": "gpt-6-sol", "label": "gpt-6-sol"}, {"id": "gpt-6-luna", "label": "gpt-6-luna"}]
        data = {"provider": "openai", "api_key": "fake-http-shared"}
        for route in ("/api/model-connect", "/api/model-selection"):
            self.assertEqual(self.request(route, "POST", data, {"Content-Type": "application/json"})[0], 403)
        with patch.object(catalog, "fetch_models", return_value=models):
            status, body = self.request("/api/model-connect", "POST", data, headers)
        self.assertEqual(status, 200)
        self.assertNotIn(b"fake-http-shared", body)
        selection = {"selection": {"screening": "gpt-6-luna", "qa": "gpt-6-sol"}}
        status, body = self.request("/api/model-selection", "POST", selection, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["connection"]["selection"], selection["selection"])
        selection["selection"]["qa"] = "unknown-model"
        self.assertEqual(self.request("/api/model-selection", "POST", selection, headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
