"""第二步，插件准备：为当前电脑生成带配对令牌的 Zotero 插件安装包。"""
import json
import secrets
import zipfile
from importlib import import_module
from pathlib import Path

_common = import_module("99common")
ROOT = _common.ROOT
load_json = _common.load_json
save_json = _common.save_json


def validate_manifest(manifest):
    """检查 Zotero 安装必需的字段，避免 ZIP 正常但插件加载器仍拒绝安装。"""
    app = manifest.get("applications", {}).get("zotero", {})
    for name in ("id", "update_url", "strict_min_version", "strict_max_version"):
        if not isinstance(app.get(name), str) or not app[name].strip():
            raise ValueError(f"插件 manifest 缺少必填字段 applications.zotero.{name}")
    if not app["update_url"].startswith("https://"):
        raise ValueError("插件 update_url 必须使用 HTTPS，否则 Zotero 可能禁用插件")
    if manifest.get("manifest_version") != 2 or not manifest.get("name") or not manifest.get("version"):
        raise ValueError("插件 manifest_version、name 或 version 无效")


def build_bridge(config_path=ROOT / ".zotero-bridge.json", output=ROOT / "dist/literature-bridge.xpi"):
    """打包插件并生成本机配对配置；重建时复用令牌，避免已安装插件失去连接。"""
    config_path, output = Path(config_path), Path(output)
    source = ROOT / "zotero_bridge"
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    validate_manifest(manifest)
    config = load_json(config_path)
    token = config.get("token") or secrets.token_hex(32)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("本机桥接配置中的令牌无效")
    save_json(config_path, dict(token=token))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.write(source / "bootstrap.js", "bootstrap.js")
        archive.writestr("token.json", json.dumps(dict(token=token)))
    print(f"插件安装包：{output}\n本机配对文件：{config_path}\n这两个文件含配对令牌，不要上传 GitHub。")
    return output


if __name__ == "__main__":
    build_bridge()
