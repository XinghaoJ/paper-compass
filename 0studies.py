"""调研项目配置：分别保存研究条件和知识库路径，模型与 Zotero 连接由界面共用。"""
import copy
import re
from pathlib import Path


def study_settings(settings):
    """只提取各调研独有字段，不包含模型密钥、模型连接和 Zotero 配对信息。"""
    return {"research": copy.deepcopy(settings["research"]),
            "knowledge": copy.deepcopy(settings["knowledge"]),
            "qa": copy.deepcopy(settings["qa"]),
            "download": {"output_dir": settings["download"]["output_dir"]}}


def study_name(value):
    """验证名称可用作 Windows 文件夹名，拒绝路径分隔符、保留名和空名称。"""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError("调研名称需为 1–80 个字符")
    value = value.strip()
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith("."):
        raise ValueError("调研名称不能包含路径分隔符或文件名特殊字符")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("请使用其他调研名称，该名称为 Windows 保留名称")
    return value


def new_study_settings(current, root):
    """清空新调研的筛选草稿，生成独立路径；共用模型配置由调用方保留。"""
    local = study_settings(current)
    local["research"]["output_file"] = str(root / "screening.md")
    local["research"].update(keywords="", focus_question="", year_start=None, year_end=None, max_papers=None)
    local["qa"]["question"] = ""
    local["download"]["output_dir"] = str(root / "pdf")
    local["knowledge"].update(input_dir=None, database=str(root / "knowledge" / "index.sqlite3"),
                              answer_dir=str(root / "answers"))
    return local


def restore_studies(saved, settings, merge, validate):
    """载入并校验项目列表；旧版设置原位登记为原有调研，不移动或重建任何资料。"""
    if saved is None:
        return {"active_id": "original", "items": {"original": {
            "id": "original", "name": "原有调研", "root": "", "settings": study_settings(settings)}}}
    if not isinstance(saved, dict) or not isinstance(saved.get("items"), dict) or not saved["items"]:
        raise ValueError("调研配置格式无效")
    items = {}
    for identifier, item in saved["items"].items():
        if not isinstance(identifier, str) or not isinstance(item, dict) or item.get("id") != identifier:
            raise ValueError("调研标识无效")
        name = study_name(item.get("name"))
        root = item.get("root", "")
        local = item.get("settings")
        if not isinstance(root, str) or not isinstance(local, dict) or set(local) != {"research", "knowledge", "qa", "download"}:
            raise ValueError("调研设置无效")
        if not isinstance(local["download"], dict) or set(local["download"]) != {"output_dir"}:
            raise ValueError("调研不能覆盖共用 Zotero 连接")
        candidate = merge(settings, local)
        validate(candidate)
        items[identifier] = {"id": identifier, "name": name, "root": root,
                             "settings": study_settings(candidate)}
    active = saved.get("active_id")
    if active not in items:
        raise ValueError("当前调研不存在")
    # 当前顶层配置兼容旧版本，作为活动项目的最新设置。
    items[active]["settings"] = study_settings(settings)
    return {"active_id": active, "items": items}
