"""本地选择器测试：无需打开交互窗口，验证路径传递、取消和失败后的并发锁释放。"""
import json
import subprocess
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

picker = import_module("0path_picker")


@unittest.skipUnless(picker.os.name == "nt", "Windows 本地选择器")
class PathPickerTests(unittest.TestCase):
    """验证本地对话框的输入输出边界，不访问用户文件内容。"""

    def test_path_is_data_and_missing_directory_uses_existing_parent(self):
        """中文、引号与 PowerShell 符号作为数据传递，不会拼入执行脚本。"""
        with tempfile.TemporaryDirectory() as root:
            current = str(Path(root) / "不存在" / "研究'$().md")
            with patch.object(picker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"path": current}))) as run:
                self.assertEqual(picker.choose_path("research.output_file", current), current)
                args = run.call_args
                config = json.loads(args.kwargs["env"]["LITERATURE_PATH_PICKER"])
                self.assertTrue(config["file"])
                self.assertEqual(config["directory"], str(Path(root).resolve()))
                self.assertEqual(config["filename"], "研究'$().md")
                self.assertNotIn(current, " ".join(args.args[0]))

    def test_cancel_failure_and_invalid_field(self):
        """取消返回空路径，进程失败释放锁，未知字段不能启动进程。"""
        with patch.object(picker.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                picker.choose_path("models.api_key")
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 1, "")
            with self.assertRaisesRegex(ValueError, "无法打开"):
                picker.choose_path("download.output_dir")
            run.return_value = subprocess.CompletedProcess([], 0, '{"path":""}')
            self.assertEqual(picker.choose_path("knowledge.answer_dir"), "")
            self.assertFalse(json.loads(run.call_args.kwargs["env"]["LITERATURE_PATH_PICKER"])["file"])
