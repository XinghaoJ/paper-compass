"""调研文件清理：先审查专属路径，再移入 Windows 回收站，保留配置和其他调研资料。"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


def study_paths(item, settings):
    """列出调研根目录及当前配置的 PDF、问答、数据库与筛选输出位置。"""
    paths = []
    if item.get("root"):
        paths.append(Path(item["root"]))
    paths.extend([Path(settings["download"]["output_dir"]), Path(settings["knowledge"]["answer_dir"])])
    database = Path(settings["knowledge"]["database"])
    paths.extend(Path(str(database) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
    output = Path(settings["research"]["output_file"])
    bases = {output.stem, output.stem if output.stem.endswith(".openalex") else output.stem + ".openalex"}
    # 只匹配程序生成的报告和变体，避免把同目录的其他文档一起清理。
    for path in output.parent.iterdir() if output.parent.is_dir() else []:
        if any(re.fullmatch(re.escape(base) + r"(?:\.[a-f0-9]{16})?(?:\.(?:md|txt)(?:\.state\.json)?|\.papers\.(?:json|csv)|\.dois\.txt)", path.name)
               for base in bases):
            paths.append(path)
    return [path.absolute() for path in paths]


def is_reparse(path):
    """识别符号链接和 Windows 联接点，清理时不跟随它们进入其他目录。"""
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def inspect_cleanup(item, settings, others, workspace, pairing):
    """检查绝对路径、共享目录和联接点，返回文件数量及用于确认后复查的清单指纹。"""
    candidates = study_paths(item, settings)
    targets = []
    forbidden = [Path(workspace).resolve(), Path(pairing).resolve()]
    protected = [path.resolve() for other, config in others for path in study_paths(other, config)]
    for original in sorted(set(candidates), key=lambda path: len(path.parts)):
        if not original.exists() and not original.is_symlink():
            continue
        if is_reparse(original):
            raise ValueError(f"清理目标本身是符号链接或联接点，无法自动清理：{original}")
        # 上层 papers 可以是用户配置的联接目录；预览展示其真实绝对路径，再按此范围确认。
        target = original.resolve()
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(target.anchor)) != 3:
                raise ValueError(f"仅支持本地固定磁盘的回收站清理，请手动处理此位置：{target}")
        if target == Path(target.anchor) or any(path == target or path.is_relative_to(target) for path in forbidden):
            raise ValueError(f"该路径包含程序或共用配对文件，不能清理：{target}")
        if any(path == target or path.is_relative_to(target) or target.is_relative_to(path) for path in protected):
            raise ValueError(f"该路径与其他调研共用，请先为当前调研设置独立路径：{target}")
        if not any(target.is_relative_to(parent) for parent in targets):
            targets.append(target)
    entries, total_bytes = [], 0
    for target in targets:
        paths = [target]
        if target.is_dir():
            for directory, folders, files in os.walk(target, followlinks=False):
                for name in folders + files:
                    if is_reparse(Path(directory) / name):
                        raise ValueError(f"目录中包含符号链接或联接点，无法自动清理：{Path(directory) / name}")
                paths.extend(Path(directory) / name for name in folders + files)
        for path in paths:
            if is_reparse(path):
                raise ValueError(f"目录中包含符号链接或联接点，无法自动清理：{path}")
            resolved = path.resolve()
            if resolved != target and not resolved.is_relative_to(target):
                raise ValueError("清理范围发生变化，请重新预览")
            stat = path.stat()
            is_file = path.is_file()
            entries.append((str(resolved), is_file, stat.st_size if is_file else 0, stat.st_mtime_ns))
            if is_file:
                total_bytes += stat.st_size
    encoded = json.dumps(sorted(entries), ensure_ascii=False).encode("utf-8")
    return dict(paths=[str(path) for path in targets], files=sum(row[1] for row in entries),
                bytes=total_bytes, fingerprint=hashlib.sha256(encoded).hexdigest())


def recycle_paths(paths):
    """把已审查的绝对路径移入 Windows 回收站；不提供绕过回收站的永久删除后备方式。"""
    if os.name != "nt":
        raise ValueError("当前文件清理仅支持 Windows 回收站")
    # JSON 经标准输入传递，不把用户目录拼成 PowerShell 代码，也不跨 shell 组合删除命令。
    script = r"""$ErrorActionPreference='Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
$targets = ConvertFrom-Json ([Console]::In.ReadToEnd())
foreach ($target in $targets) {
    if ([System.IO.Directory]::Exists($target)) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($target, 'OnlyErrorDialogs', 'SendToRecycleBin', 'ThrowException')
    } elseif ([System.IO.File]::Exists($target)) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($target, 'OnlyErrorDialogs', 'SendToRecycleBin', 'ThrowException')
    }
}
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            input=json.dumps(paths, ensure_ascii=True), text=True, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("清理未全部完成，可能有文件被占用或回收站不可用；请重新预览剩余文件。" + result.stderr[-600:])
