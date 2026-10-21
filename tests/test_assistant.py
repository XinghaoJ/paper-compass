import contextlib
import io
import json
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

abstract_read = import_module("1abstract_read")
assistant = import_module("0assistant")
common = import_module("99common")


class AssistantTests(unittest.TestCase):
    """助手功能的离线测试：使用临时文件和模拟接口验证流程，不调用真实 AI 服务。"""

    def test_current_cli_commands(self):
        """命令入口保留当前流程，已退役的独立全文报告命令不再暴露。"""
        for command in ("search", "download", "zotero-check", "index", "ask"):
            self.assertEqual(assistant.build_parser().parse_args([command]).command, command)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            assistant.build_parser().parse_args(["read"])

    def setUp(self):
        """测试准备：为每个测试创建临时目录、模拟 AI 客户端，并收集终端输出。"""
        self.temporary = tempfile.TemporaryDirectory()
        from dataclasses import replace
        self.research_patch = patch.object(abstract_read.user_settings, "RESEARCH_SETTINGS",
            replace(abstract_read.user_settings.RESEARCH_SETTINGS, search_source="google_scholar"))
        self.research_patch.start()
        self.addCleanup(self.research_patch.stop)
        self.root = Path(self.temporary.name)
        self.client = Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(text="研究结果 [S1]")
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()

    def tearDown(self):
        """测试清理：恢复终端输出，并删除本次测试创建的临时文件。"""
        self.capture.__exit__(None, None, None)
        self.temporary.cleanup()


    def test_empty_response_not_success(self):
        """验证模型返回空文本时记为失败，不保存看似成功的空总结。"""
        self.client.models.generate_content.return_value = SimpleNamespace(text=None)
        with self.assertRaisesRegex(ValueError, "模型未返回文本"):
            common.generate(self.client, "问题", "test-model")


    def test_transient_retries_but_not_auth(self):
        """验证限流和连接异常会重试，非临时错误直接抛出。"""
        temporary_error = RuntimeError("busy")
        temporary_error.code = 429
        operation = Mock(side_effect=[temporary_error, "ok"])
        with patch.object(common.time, "sleep") as sleep:
            self.assertEqual(common.retry(operation), "ok")
            sleep.assert_called_once_with(2)
        operation = Mock(side_effect=ValueError("bad key"))
        with self.assertRaises(ValueError):
            common.retry(operation)
        self.assertEqual(operation.call_count, 1)
        network_error = type("ConnectError", (Exception,), {})
        operation = Mock(side_effect=[network_error("offline"), "ok"])
        with patch.object(common.time, "sleep"):
            self.assertEqual(common.retry(operation), "ok")

    def test_credit_exhausted_has_chinese_message_without_retry(self):
        """预付额度耗尽返回简明中文提示，不重复发送相同的失败请求。"""
        coded_error = RuntimeError("RESOURCE_EXHAUSTED")
        coded_error.code = 402
        text_error = RuntimeError("Your prepayment credits are depleted.")
        for error in (coded_error, text_error):
            with self.subTest(error=str(error)):
                operation = Mock(side_effect=error)
                with self.assertRaisesRegex(RuntimeError, "^API额度耗尽$"):
                    common.retry(operation)
                self.assertEqual(operation.call_count, 1)

    def test_rate_limit_is_not_reported_as_empty_credits(self):
        """429 限流与余额耗尽不同，保留限流错误，不误报为 API 额度耗尽。"""
        error = RuntimeError("429 RESOURCE_EXHAUSTED: rate limit")
        error.code = 429
        operation = Mock(side_effect=error)
        with patch.object(common.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "rate limit"):
                common.retry(operation)
        self.assertEqual(operation.call_count, 3)

    def test_screening_stops_on_empty_credits_preserving_progress(self):
        """筛选在余额耗尽时保留已完成和当前失败记录，不读取或调用后续文献。"""
        publications = iter([{"bib": {"title": f"Paper {i}", "abstract": "long abstract " * 10,
                                      "pub_year": "2024", "doi": f"10.1234/paper{i}"}} for i in range(3)])
        error = RuntimeError("prepayment credits are depleted")
        error.code = 402
        response = SimpleNamespace(text=json.dumps({"relevant": True, "reason": "相关"}))
        self.client.models.generate_content.side_effect = [response, error]
        report = self.root / "screen.md"
        with self.assertRaises(common.APICreditsExhaustedError):
            abstract_read.auto_literature_review("q", "topic", 2020, 2026, 3,
                                                report, publications=publications, client=self.client)
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        self.assertEqual(next(publications)["bib"]["title"], "Paper 2")
        records = common.load_json(report.with_suffix(".md.state.json"))["records"]
        self.assertEqual(sorted(r["status"] for r in records.values()), ["failed", "relevant"])
        self.assertIn("API额度耗尽", report.with_suffix(".md.state.json").read_text(encoding="utf-8"))


    def test_missing_abstracts_count_toward_search_limit(self):
        """验证缺失摘要的条目也计入检索上限，并且不调用 AI。"""
        pubs = iter([{"bib": {"title": f"Paper {i}"}} for i in range(10)])
        report = self.root / "screen.md"
        stats = abstract_read.auto_literature_review("q", "topic", 2020, 2026, 2,
                                                    report, publications=pubs, client=self.client)
        self.assertEqual(stats["inspected"], 2)
        self.assertEqual(next(pubs)["bib"]["title"], "Paper 2")
        self.client.models.generate_content.assert_not_called()

    def test_search_resume_and_failed_entry_retry(self):
        """重试失败条目并跳过成功条目，修改条件后自动隔离记录，不覆盖原报告。"""
        pubs = [{"bib": {"title": "Paper", "abstract": "long abstract " * 10,
                         "pub_year": "2024", "doi": "10.1234/test"}}]
        report = self.root / "screen.md"
        self.client.models.generate_content.side_effect = RuntimeError("error")
        first = abstract_read.auto_literature_review("q", "topic", 2020, 2026, 2,
                                                   report, publications=pubs, client=self.client)
        self.assertEqual(first["failed"], 1)
        self.client.models.generate_content.side_effect = None
        self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(
            dict(relevant=True, reason="摘要与关注问题相关")))
        for _ in range(2):
            stats = abstract_read.auto_literature_review("q", "topic", 2020, 2026, 2,
                                                       report, publications=pubs, client=self.client)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        self.assertIn("## [1] Paper", report.read_text(encoding="utf-8"))
        original = report.read_bytes()
        changed = abstract_read.auto_literature_review("q2", "topic", 2020, 2026, 2,
                                                     report, publications=pubs, client=self.client)
        self.assertNotEqual(Path(changed["report"]), report)
        self.assertEqual(report.read_bytes(), original)
        self.assertEqual(changed["skipped"], 0)

    def test_corrupt_state_is_not_silently_discarded(self):
        """验证状态文件损坏时明确报错，避免丢弃进度后重新处理全部文献。"""
        state = self.root / "state.json"
        state.write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "状态文件"):
            common.load_json(state)

    def test_atomic_write_preserves_existing_file_if_replace_fails(self):
        """验证替换输出失败时保留原有文件，并清理未完成的临时文件。"""
        output = self.root / "test.md"
        output.write_text("old", encoding="utf-8")
        with patch.object(common.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                common.atomic_write(output, "new")
        self.assertEqual(output.read_text(), "old")
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
