"""调研清理离线测试：只操作临时文件，用重命名模拟回收站，不删除用户资料。"""
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

ui = import_module("0ui_service")
cleanup = import_module("0study_cleanup")


class StudyCleanupTests(unittest.TestCase):
    """覆盖清理预览、确认范围、文件变化、共享路径和运行中保护。"""

    def setUp(self):
        """创建两个独立调研和少量资料，所有数据位于临时目录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = ui.UIService(self.root / "settings.json")
        self.service.update({"research": {"output_file": str(self.root / "old.md")},
            "download": {"output_dir": str(self.root / "old-pdf")},
            "knowledge": {"database": str(self.root / "old.sqlite3"), "answer_dir": str(self.root / "old-answers")}}, persist=True)
        self.old = self.root / "old.sqlite3"
        self.old.write_bytes(b"old-index")
        self.service.change_study({"name": "当前调研", "directory": str(self.root / "studies")}, create=True)
        self.project = self.root / "studies" / "当前调研"
        for relative in ("pdf/paper.pdf", "pdf/.download-state.json", "screening.openalex.dois.txt",
                         "screening.openalex.md", "knowledge/index.sqlite3", "answers/answer.md"):
            (self.project / relative).write_bytes(b"test-data")

    def tearDown(self):
        """回收独立临时目录，不连接实际回收站。"""
        self.service.stop()
        self.temp.cleanup()

    def confirm(self, plan, **overrides):
        """提交对应预览的名称和令牌，支持覆盖字段模拟过期或错误确认。"""
        return self.service.cleanup_study_files(dict(name=plan["name"], token=plan["token"], **overrides), confirm=True)

    def test_preview_does_not_remove_files_and_confirm_preserves_models_and_other_study(self):
        """预览只读文件；确认后模拟移走整个专属目录，其他调研及共用密钥保持不变。"""
        self.service.update({"models": {"screening": {"api_key": "private-test-key"}}})
        plan = self.service.cleanup_study_files({})
        self.assertEqual(plan["files"], 6)
        self.assertEqual(plan["paths"], [str(self.project.resolve())])
        self.assertTrue((self.project / "pdf/paper.pdf").exists())
        destination = self.root / "simulated-recycle-bin"
        with patch.object(cleanup, "recycle_paths", side_effect=lambda paths: Path(paths[0]).rename(destination)):
            result = self.confirm(plan)
        self.assertEqual(result["cleared"], 6)
        self.assertFalse(self.project.exists())
        self.assertTrue((destination / "pdf/paper.pdf").exists())
        self.assertEqual(self.old.read_bytes(), b"old-index")
        self.assertEqual(self.service.settings["models"]["screening"]["api_key"], "private-test-key")
        self.assertEqual(len(self.service.study_workspace["items"]), 2)

    def test_name_token_and_changed_files_rejected_before_recycle(self):
        """错误名称、令牌以及确认后新增的文件均不能触发清理。"""
        plan = self.service.cleanup_study_files({})
        with patch.object(cleanup, "recycle_paths") as recycle:
            for data in ({"name": "其他调研", "token": plan["token"]}, {"name": plan["name"], "token": "wrong"}):
                with self.assertRaisesRegex(ValueError, "重新预览"):
                    self.service.cleanup_study_files(data, confirm=True)
            (self.project / "new.pdf").write_bytes(b"new")
            with self.assertRaisesRegex(ValueError, "重新预览"):
                self.confirm(plan)
            recycle.assert_not_called()

    def test_shared_output_and_workspace_paths_rejected(self):
        """配置指向其他调研或程序目录时阻止清理，不枚举或回收程序文件。"""
        shared = self.root / "old-pdf"
        shared.mkdir()
        (shared / "old.pdf").write_bytes(b"old")
        self.service.update({"download": {"output_dir": str(shared)}})
        with self.assertRaisesRegex(ValueError, "其他调研共用"):
            self.service.cleanup_study_files({})
        self.service.update({"download": {"output_dir": str(ui.ROOT)}})
        with self.assertRaisesRegex(ValueError, "不能清理"):
            self.service.cleanup_study_files({})
        self.assertTrue((shared / "old.pdf").exists())

    def test_running_task_and_nested_reparse_rejected(self):
        """运行中禁止清理，目录内部发现联接点时不能递归进入或移走目标。"""
        self.service.job = {"status": "running"}
        with self.assertRaisesRegex(ValueError, "停止任务"):
            self.service.cleanup_study_files({})
        self.service.job = None
        original = cleanup.is_reparse
        with patch.object(cleanup, "is_reparse", side_effect=lambda path: path.name == "paper.pdf" or original(path)):
            with self.assertRaisesRegex(ValueError, "联接点"):
                self.service.cleanup_study_files({})

    def test_legacy_report_matching_does_not_include_unrelated_files(self):
        """无专属根目录的旧调研只匹配程序输出文件和签名变体，不匹配同前缀其他资料。"""
        self.service.change_study({"id": "original"})
        (self.root / "old.openalex.0123456789abcdef.papers.json").write_text("{}")
        unrelated = self.root / "old.notes.md"
        unrelated.write_text("keep")
        plan = self.service.cleanup_study_files({})
        self.assertIn(str((self.root / "old.openalex.0123456789abcdef.papers.json").resolve()), plan["paths"])
        self.assertNotIn(str(unrelated.resolve()), plan["paths"])
