"""自动下载流程测试：无需安装或运行 Zotero，也不访问外部网络。"""
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from importlib import import_module
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

assistant = import_module("0assistant")
downloader = import_module("2download_papers")
builder = import_module("2build_zotero_bridge")
abstract_read = import_module("1abstract_read")
build_bridge, validate_manifest = builder.build_bridge, builder.validate_manifest
load_json = import_module("99common").load_json
from user_settings import DownloadSettings


class DownloadTests(unittest.TestCase):
    """验证 DOI 读取、插件通信、成功判据、断点恢复和串联命令。"""

    def setUp(self):
        """创建模拟输入、PDF 和 Zotero 连接，每项测试使用独立临时目录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "papers.json"
        self.input.write_text(json.dumps(dict(papers=[dict(doi="10.1234/a", title="Paper")])), encoding="utf-8")
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nTest PDF content")
        self.settings = DownloadSettings(output_dir=self.root / "pdfs", bridge_config=self.root / "pair.json")
        self.bridge = Mock()
        self.bridge.download.return_value = dict(status="downloaded", doi="10.1234/a", path=str(self.pdf),
                                                 item_key="ITEMKEY1", attachment_key="ATTACH01")
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()

    def tearDown(self):
        """恢复终端输出并清理临时文件。"""
        self.capture.__exit__(None, None, None)
        self.temporary.cleanup()

    def run_download(self, **kwargs):
        """用模拟 Zotero 接口执行下载，不启动真实软件。"""
        return downloader.download_papers(self.input, settings=self.settings, bridge=self.bridge, **kwargs)


    def test_verified_pdf_is_copied_and_resume_skips(self):
        """验证成功 PDF 被复制到知识库，状态记录哈希，再运行不重复下载。"""
        self.assertEqual(self.run_download()["downloaded"], 1)
        self.assertEqual(self.run_download()["skipped"], 1)
        self.bridge.download.assert_called_once()
        record = load_json(self.settings.output_dir / ".download-state.json")["10.1234/a"]
        self.assertEqual((self.settings.output_dir / record["file"]).read_bytes(), self.pdf.read_bytes())
        self.assertTrue(record["sha256"])
        self.assertTrue(self.pdf.exists())

    def test_corrupt_local_pdf_is_recovered(self):
        """验证已保存文件被改坏后重新从 Zotero 获取，而不是永久跳过。"""
        self.run_download()
        next(self.settings.output_dir.glob("*.pdf")).write_bytes(b"bad")
        self.assertEqual(self.run_download()["downloaded"], 1)
        self.assertEqual(self.bridge.download.call_count, 2)

    def test_html_is_not_counted_as_downloaded(self):
        """验证插件报告成功但附件实际为 HTML 时记为失败，不生成假 PDF。"""
        self.pdf.write_bytes(b"<html>login required</html>")
        self.assertEqual(self.run_download()["failed"], 1)
        self.assertEqual(list(self.settings.output_dir.glob("*.pdf")), [])

    def test_unavailable_is_distinct_from_failure(self):
        """验证无可用全文被单独统计，条目创建成功不等于 PDF 下载成功。"""
        self.bridge.download.return_value = dict(status="unavailable", item_key="ITEMKEY1", error="No accessible PDF")
        stats = self.run_download()
        self.assertEqual((stats["downloaded"], stats["unavailable"], stats["failed"]), (0, 1, 0))
        self.assertEqual(list(self.settings.output_dir.glob("*.pdf")), [])

    def test_unavailable_reason_logged_and_saved_without_inventing_cause(self):
        """插件只返回通用原因时明确说明诊断不足，并在日志、状态和 CSV 中保留原因。"""
        self.bridge.download.return_value = dict(status="unavailable", item_key="ITEMKEY1",
                                                 error="Zotero did not obtain an accessible PDF")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.run_download()
        record = load_json(self.settings.output_dir / ".download-state.json")["10.1234/a"]
        self.assertIn("插件未返回具体原因", record["reason"])
        self.assertEqual(record["error"], "Zotero did not obtain an accessible PDF")
        self.assertIn(record["reason"], output.getvalue())
        self.assertIn("DOI：10.1234/a", output.getvalue())
        self.assertNotIn("Paper", output.getvalue())
        self.assertIn(record["reason"], (self.settings.output_dir / "download_report.csv").read_text(encoding="utf-8-sig"))

    def test_specific_unavailable_reason_is_preserved(self):
        """有具体错误时原样显示，不替换为通用原因或错误推断。"""
        self.bridge.download.return_value = dict(status="unavailable", error="Publisher response: HTTP 403")
        self.run_download()
        record = load_json(self.settings.output_dir / ".download-state.json")["10.1234/a"]
        self.assertEqual(record["reason"], "Publisher response: HTTP 403")


    def test_dry_run_does_not_connect_or_write(self):
        """验证预览不需要配对文件、不连接 Zotero、也不创建目录。"""
        stats = downloader.download_papers(self.input, settings=self.settings, dry_run=True)
        self.assertEqual(stats["pending"], 1)
        self.assertFalse(self.settings.output_dir.exists())


    def test_doi_text_and_json_are_deduplicated(self):
        """验证 DOI 文本和清单按完整 DOI 去重，且拒绝格式错误的标识符。"""
        text = self.root / "dois.txt"
        text.write_text("10.1234/A\nhttps://doi.org/10.1234/a\n", encoding="utf-8")
        self.assertEqual(downloader.read_download_list(text), [dict(doi="10.1234/a")])
        text.write_text("not a DOI", encoding="utf-8")
        with self.assertRaises(ValueError):
            downloader.read_download_list(text)


    def test_limit_bounds_failed_attempts(self):
        """验证下载失败时仍遵守本次处理数量限制。"""
        self.input.write_text(json.dumps(dict(papers=[dict(doi=f"10.1234/{i}") for i in range(5)])), encoding="utf-8")
        self.bridge.download.side_effect = TimeoutError("download still running")
        self.assertEqual(self.run_download(limit=2)["failed"], 2)
        self.assertEqual(self.bridge.download.call_count, 2)

    def test_plugin_build_pairs_without_leaking_in_logs(self):
        """验证插件包含匹配令牌、重建复用配对信息，令牌不打印到终端。"""
        package = self.root / "bridge.xpi"
        build_bridge(self.settings.bridge_config, package)
        token = load_json(self.settings.bridge_config)["token"]
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(json.loads(archive.read("token.json"))["token"], token)
            self.assertIn("bootstrap.js", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["version"], "0.2.0")
            self.assertTrue(manifest["applications"]["zotero"]["update_url"].startswith("https://"))
            self.assertIsNone(archive.testzip())
        build_bridge(self.settings.bridge_config, package)
        self.assertEqual(load_json(self.settings.bridge_config)["token"], token)

    def test_manifest_requires_update_url_for_zotero_10(self):
        """验证构建时拒绝遗漏更新地址或使用不安全地址，覆盖本次安装失败的原因。"""
        manifest = dict(manifest_version=2, name="Test", version="1.0",
                        applications=dict(zotero=dict(id="test@local.invalid", strict_min_version="7.0",
                                                       strict_max_version="10.0.*")))
        with self.assertRaisesRegex(ValueError, "update_url"):
            validate_manifest(manifest)
        manifest["applications"]["zotero"]["update_url"] = "http://example.invalid/updates.json"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_manifest(manifest)

    def test_remote_zotero_address_is_rejected(self):
        """验证连接器拒绝远程地址和带凭据的地址，防止错误发送本机令牌。"""
        for url in ["https://remote.example", "http://127.0.0.1@remote.example", "http://localhost:23119/other"]:
            with self.assertRaises(ValueError):
                replace(self.settings, zotero_url=url).validate()

    def test_polling_checks_doi_and_waits_for_completion(self):
        """验证提交和轮询使用同一任务，并在得到成功状态后验证 DOI。"""
        self.settings.bridge_config.write_text(json.dumps(dict(token="a" * 64)), encoding="utf-8")
        session = Mock(headers={})
        bridge = downloader.ZoteroBridge(self.settings, session=session)
        bridge.request = Mock(side_effect=[dict(status="queued", job_id="id"),
                                          dict(status="downloaded", doi="10.1234/a", path=str(self.pdf))])
        with patch.object(downloader.time, "sleep"):
            self.assertEqual(bridge.download(dict(doi="10.1234/a"), "id")["status"], "downloaded")
        self.assertFalse(session.trust_env)
        bridge.request = Mock(return_value=dict(status="downloaded", doi="10.1234/wrong"))
        with self.assertRaisesRegex(ValueError, "DOI"):
            bridge.download(dict(doi="10.1234/a"), "id")

    def test_study_collection_payload_and_old_plugin_guard(self):
        """调研名称发送给新版插件；旧插件必须阻止下载，避免文献未分类。"""
        self.settings.bridge_config.write_text(json.dumps(dict(token="a" * 64)), encoding="utf-8")
        settings = replace(self.settings, collection_name="调研甲")
        bridge = downloader.ZoteroBridge(settings, session=Mock(headers={}))
        bridge.request = Mock(side_effect=[dict(capabilities=["collections"]),
            dict(status="downloaded", doi="10.1234/a", collection_name="调研甲", collection_key="COL1")])
        bridge.download(dict(doi="10.1234/a"), "id")
        self.assertEqual(bridge.request.call_args.args[1]["collection_name"], "调研甲")
        bridge.request = Mock(return_value=dict(bridge_version="0.1.2"))
        with self.assertRaisesRegex(RuntimeError, "安装新版"):
            bridge.download(dict(doi="10.1234/a"), "id")
        bridge.request.assert_called_once_with("/ping")

    def test_search_download_uses_same_report(self):
        """验证筛选和下载可一次命令完成，下载输入对应本次筛选报告而非另一用户任务。"""
        with patch.object(abstract_read, "auto_literature_review", return_value={"failed": 0}), \
                patch.object(downloader, "download_papers", return_value={"failed": 0}) as run:
            self.assertEqual(assistant.main(["search", "--output", "user-b.md", "--download"]), 0)
        run.assert_called_once_with(Path("user-b.openalex.papers.json"))


if __name__ == "__main__":
    unittest.main()
