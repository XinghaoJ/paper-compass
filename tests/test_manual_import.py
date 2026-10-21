"""人工补充全文测试：本机导入、关联、内容去重和无网络断点续跑。"""
import io
import tempfile
import unittest
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

from pypdf import PdfWriter
import user_settings

manual = import_module("2manual_import")
download = import_module("2download_papers")
knowledge = import_module("4knowledge_base")


def pdf_bytes(title="测试文献"):
    """生成真实可解析的单页 PDF，无需联网或调用模型。"""
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_metadata({"/Title": title})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class ManualImportTests(unittest.TestCase):
    """验证手动下载和现有下载、知识库的衔接。"""

    def setUp(self):
        """为每项测试创建待下载记录和独立目录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "pdf"
        download.save_download_state(self.output, {"10.1234/a": {
            "doi": "10.1234/a", "title": "目标文章", "status": "unavailable", "reason": "需要机构登录"}})

    def tearDown(self):
        """清理测试目录，不修改用户文献。"""
        self.temp.cleanup()

    def state(self):
        """读取当前下载状态供结果断言使用。"""
        return download.load_json(self.output / ".download-state.json")

    def test_targeted_import_updates_reports_and_index_metadata(self):
        """按文章导入后移除待下载项，保留 DOI 和标题供知识库引用。"""
        result = manual.import_pdf(pdf_bytes(), "../../下载.pdf", self.output, "10.1234/a")
        self.assertEqual(result["status"], "imported")
        record = self.state()["10.1234/a"]
        self.assertEqual(record["download_source"], "manual")
        self.assertEqual(download.manual_download_items(self.state()), [])
        self.assertIn("共 0 篇", (self.output / "manual_downloads.md").read_text(encoding="utf-8"))
        self.assertTrue((self.output / record["file"]).is_file())
        metadata = knowledge.download_metadata(self.output)
        self.assertEqual(metadata[str((self.output / record["file"]).resolve())],
                         {"title": "目标文章", "doi": "10.1234/a"})
        with self.assertRaisesRegex(ValueError, "已有另一份"):
            manual.import_pdf(pdf_bytes("另一篇"), "another.pdf", self.output, "10.1234/a")

    def test_generic_import_then_link_and_resume_without_network(self):
        """批量导入不猜 DOI；再次关联复用同一文件，后续自动下载正确跳过。"""
        content = pdf_bytes()
        first = manual.import_pdf(content, "one.pdf", self.output)
        self.assertEqual(manual.import_pdf(content, "renamed.pdf", self.output)["status"], "skipped")
        linked = manual.import_pdf(content, "one.pdf", self.output, "10.1234/a")
        self.assertEqual(linked["status"], "linked")
        self.assertEqual(first["file"], linked["file"])
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 1)
        self.assertFalse(any(key.startswith("manual:") for key in self.state()))
        source = self.root / "dois.txt"
        source.write_text("10.1234/a", encoding="utf-8")
        bridge = Mock()
        stats = download.download_papers(source, settings=replace(user_settings.DOWNLOAD_SETTINGS, output_dir=self.output), bridge=bridge)
        self.assertEqual(stats["skipped"], 1)
        bridge.download.assert_not_called()

    def test_invalid_file_preserves_state(self):
        """HTML、伪 PDF、超限文件及未知文章均不能污染目录或下载记录。"""
        before = self.state()
        for data, name, doi in [(b"<html>login</html>", "a.pdf", "10.1234/a"),
                                (b"%PDF-1.7\nnot a pdf", "a.pdf", "10.1234/a"),
                                (pdf_bytes(), "a.exe", "10.1234/a"),
                                (pdf_bytes(), "a.pdf", "10.1234/missing")]:
            with self.assertRaises(ValueError):
                manual.import_pdf(data, name, self.output, doi)
        self.assertEqual(self.state(), before)
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_same_pdf_cannot_be_assigned_to_two_articles(self):
        """重复内容已关联 DOI 时拒绝再关联另一篇，避免错误引用。"""
        content = pdf_bytes()
        manual.import_pdf(content, "a.pdf", self.output, "10.1234/a")
        state = self.state()
        state["10.1234/b"] = dict(doi="10.1234/b", status="failed")
        download.save_download_state(self.output, state)
        with self.assertRaisesRegex(ValueError, "另一篇"):
            manual.import_pdf(content, "b.pdf", self.output, "10.1234/b")
        self.assertEqual(self.state()["10.1234/b"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
