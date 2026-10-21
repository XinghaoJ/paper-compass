"""调研切换测试：保护旧资料、隔离数据库、保留共用连接及验证持久化恢复。"""
import json
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

ui = import_module("0ui_service")


class StudyTests(unittest.TestCase):
    """使用独立临时路径测试，不创建真实模型请求或修改用户资料。"""

    def setUp(self):
        """创建原调研设置和占位文件，方便检查切换不会覆盖已有数据库。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = ui.UIService(self.root / "settings.json")
        self.original = {"research": {"keywords": "original keywords", "output_file": str(self.root / "old.md")},
                         "download": {"output_dir": str(self.root / "old-pdf"), "bridge_config": str(self.root / "pair.json")},
                         "knowledge": {"database": str(self.root / "old.sqlite3"), "answer_dir": str(self.root / "old-answers")},
                         "qa": {"question": "original question"}}
        self.service.update(self.original, persist=True)
        (self.root / "old.sqlite3").write_bytes(b"original database")

    def tearDown(self):
        """测试结束后关闭任务并清理独立临时目录。"""
        self.service.stop()
        self.temp.cleanup()

    def create(self, name="新的调研", **kwargs):
        """创建使用临时父目录的新调研。"""
        return self.service.change_study({"name": name, "directory": str(self.root / "studies"), **kwargs}, create=True)

    def test_create_switch_restore_and_preserve_original_files(self):
        """新项目数据库独立，未应用表单随切换保存，重启恢复项目列表与当前项目。"""
        result = self.create(settings={"research": {"keywords": "unsaved original edits"}})
        identifier = result["studies"]["active_id"]
        new = result["settings"]
        project = self.root / "studies" / "新的调研"
        self.assertEqual(Path(new["download"]["output_dir"]), project / "pdf")
        self.assertEqual(Path(new["knowledge"]["database"]), project / "knowledge" / "index.sqlite3")
        self.assertEqual(Path(new["knowledge"]["answer_dir"]), project / "answers")
        self.assertEqual(Path(new["research"]["output_file"]), project / "screening.md")
        self.assertTrue((project / "pdf").is_dir())
        self.assertFalse((project / "knowledge" / "index.sqlite3").exists())
        self.assertEqual((self.root / "old.sqlite3").read_bytes(), b"original database")
        self.service.change_study({"id": "original", "settings": {
            "research": {"keywords": "new research"}, "qa": {"question": "new independent question"}}})
        self.assertEqual(self.service.settings["research"]["keywords"], "unsaved original edits")
        self.assertEqual(self.service.settings["qa"]["question"], "original question")
        self.assertEqual(self.service.settings["knowledge"]["database"], str(self.root / "old.sqlite3"))
        self.service.change_study({"id": identifier})
        restored = ui.UIService(self.service.settings_path)
        self.assertEqual(restored.load_error, "")
        self.assertEqual(restored.study_workspace["active_id"], identifier)
        self.assertEqual(len(restored.study_workspace["items"]), 2)
        self.assertEqual(restored.settings["research"]["keywords"], "new research")
        self.assertEqual(restored.settings["qa"]["question"], "new independent question")

    def test_new_study_blank_research_preserves_models_and_restores_draft(self):
        """新调研清空所有筛选输入，共用密钥及模型选择不变，空白草稿可持久化与切换。"""
        self.service.update({"models": {"screening": {"api_key": "test-private-key"}}})
        models = self.service.settings["models"].copy()
        result = self.create("空白草稿")
        identifier = result["studies"]["active_id"]
        research = self.service.settings["research"]
        self.assertEqual([research[k] for k in ("keywords", "focus_question")], ["", ""])
        self.assertEqual([research[k] for k in ("year_start", "year_end", "max_papers")], [None, None, None])
        self.assertEqual(self.service.settings["models"], models)
        self.assertEqual(self.service.settings["qa"]["question"], "")
        with self.assertRaisesRegex(ValueError, "请先填写"):
            self.service.start("ask", {}, {})
        with self.assertRaisesRegex(ValueError, "请填写"):
            self.service.start("search", {}, {})
        self.assertIsNone(self.service.job)
        self.service.change_study({"id": "original"})
        self.service.change_study({"id": identifier})
        restored = ui.UIService(self.service.settings_path)
        self.assertEqual(restored.load_error, "")
        self.assertIsNone(restored.settings["research"]["year_start"])
        self.assertEqual(restored.settings["research"]["keywords"], "")
        self.assertEqual(restored.settings["qa"]["question"], "")

    def test_shared_models_and_pairing_are_preserved_secrets_never_saved(self):
        """调研切换不丢失会话密钥与模型列表，磁盘上所有项目均不保存密钥。"""
        secret = "private-session-key"
        self.service.update({"models": {"screening": {"api_key": secret}, "qa": {"api_key": secret}}})
        self.service.model_catalog = [{"id": "test-model"}]
        catalog = self.service.model_catalog
        self.create()
        self.service.change_study({"id": "original"})
        self.assertEqual(self.service.settings["models"]["qa"]["api_key"], secret)
        self.assertIs(self.service.model_catalog, catalog)
        self.assertEqual(self.service.settings["download"]["bridge_config"], str(self.root / "pair.json"))
        saved = self.service.settings_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, saved)
        for item in json.loads(saved)["study_workspace"]["items"].values():
            self.assertNotIn("models", item["settings"])

    def test_busy_invalid_names_existing_directory_and_unknown_study_rejected(self):
        """拒绝运行中切换、目录穿越、覆盖已有目录和未知项目，并保留原设置。"""
        before = self.service.settings.copy()
        for name in ("../escape", "CON", "bad/name", "", "x."):
            with self.assertRaises(ValueError):
                self.create(name)
        (self.root / "studies" / "Existing").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.create("Existing")
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.service.change_study({"id": "missing"})
        self.service.job = {"status": "running"}
        with self.assertRaisesRegex(ValueError, "任务完成"):
            self.create()
        self.service.job = None
        self.assertEqual(self.service.settings, before)

    def test_settings_write_failure_does_not_switch_project(self):
        """原子保存失败时仍停留在原调研，不把内存指向未保存的新项目。"""
        with patch.object(ui._common, "save_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(self.service.study_workspace["active_id"], "original")
        self.assertEqual(len(self.service.study_workspace["items"]), 1)
        self.assertEqual(self.service.settings["research"]["keywords"], "original keywords")

    def test_download_snapshots_active_study_collection_name(self):
        """下载任务使用当前调研名称，即使调用方传入了其他分类名称也不能串库。"""
        self.create("调研甲")
        with patch.object(ui.subprocess, "Popen"), patch.object(ui.threading, "Thread") as thread:
            self.service.start("download", {"download": {"collection_name": "错误名称"}}, {})
            payload = thread.call_args.kwargs["args"][1]
            self.assertEqual(payload["settings"]["download"]["collection_name"], "调研甲")
        self.service.job = None
        self.service.process = None

    def test_task_start_persists_research_conditions_without_keys(self):
        """启动任务即保存条件，重启后下载仍对应相同清单，密钥继续只留在内存。"""
        with patch.object(ui.subprocess, "Popen"), patch.object(ui.threading, "Thread"):
            self.service.start("search", {"research": {"keywords": "Symmetric instability parameterization"},
                "models": {"screening": {"api_key": "private-test-key"}}}, {})
        self.service.job = None
        self.service.process = None
        restored = ui.UIService(self.service.settings_path)
        self.assertEqual(restored.settings["research"]["keywords"], "Symmetric instability parameterization")
        self.assertNotIn("private-test-key", self.service.settings_path.read_text(encoding="utf-8"))

    def test_corrupt_study_registry_not_overwritten(self):
        """项目列表损坏时保留原文件，不静默覆盖已有项目映射。"""
        saved = json.loads(self.service.settings_path.read_text(encoding="utf-8"))
        saved["study_workspace"]["active_id"] = "missing"
        original = json.dumps(saved)
        self.service.settings_path.write_text(original, encoding="utf-8")
        restored = ui.UIService(self.service.settings_path)
        with self.assertRaisesRegex(ValueError, "调研列表"):
            restored.update({}, persist=True)
        self.assertEqual(self.service.settings_path.read_text(encoding="utf-8"), original)
