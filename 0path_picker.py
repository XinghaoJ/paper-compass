"""本机路径选择器：用独立进程显示 Windows 对话框，只返回路径，不读取或写入文件。"""
import base64
import json
import os
import subprocess
import threading
from pathlib import Path

FIELDS = {"download.output_dir": "命中文献下载路径",
          "research.output_file": "文献筛选报告路径",
          "knowledge.answer_dir": "问答报告路径", "study.directory": "新调研保存位置"}
_dialog_lock = threading.Lock()
_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$config = $env:LITERATURE_PATH_PICKER | ConvertFrom-Json
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
if ($config.file) {
    $dialog = New-Object System.Windows.Forms.SaveFileDialog
    $dialog.Title = $config.title
    $dialog.InitialDirectory = $config.directory
    $dialog.FileName = $config.filename
    $dialog.Filter = 'Markdown (*.md)|*.md'
    $dialog.DefaultExt = 'md'
    $dialog.AddExtension = $true
    $dialog.OverwritePrompt = $false
} else {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $config.title
    $dialog.SelectedPath = $config.directory
    $dialog.ShowNewFolderButton = $true
}
try {
    $selected = ''
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        if ($config.file) { $selected = $dialog.FileName }
        else { $selected = $dialog.SelectedPath }
    }
    @{path=$selected} | ConvertTo-Json -Compress
} finally { $dialog.Dispose(); $owner.Dispose() }
'''


def choose_path(field, current=""):
    """选择三个受支持的本地路径之一；取消返回空字符串，旧路径不存在时打开最近的上级目录。"""
    if not isinstance(field, str) or field not in FIELDS:
        raise ValueError("不支持的路径设置")
    if not isinstance(current, str) or len(current) > 32767 or "\0" in current:
        raise ValueError("当前路径无效")
    if os.name != "nt":
        raise ValueError("本地浏览目前支持 Windows，请手动填写路径")
    if not _dialog_lock.acquire(blocking=False):
        raise ValueError("已有路径选择窗口，请先选择或取消")
    try:
        is_file = field == "research.output_file"
        path = Path(current).expanduser() if current.strip() else Path.home()
        directory = path.parent if is_file and current.strip() else path
        while not directory.is_dir() and directory != directory.parent:
            directory = directory.parent
        if not directory.is_dir():
            directory = Path.home()
        config = dict(file=is_file, title=FIELDS[field], directory=str(directory.resolve()),
                      filename=path.name if is_file and current.strip() else "abstract_screening.md")
        environment = os.environ.copy()
        environment["LITERATURE_PATH_PICKER"] = json.dumps(config, ensure_ascii=False)
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-STA", "-EncodedCommand",
             base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")],
            env=environment, capture_output=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise ValueError("无法打开本地选择窗口，请手动填写路径")
        selected = json.loads(result.stdout.lstrip("\ufeff"))["path"]
        if not isinstance(selected, str):
            raise ValueError("路径选择结果无效")
        return selected
    finally:
        _dialog_lock.release()
