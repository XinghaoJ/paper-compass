"""第二步人工补充：校验用户选择的 PDF，复制到统一目录并更新下载记录。"""
import hashlib
import io
import os
import tempfile
from importlib import import_module
from pathlib import Path

download = import_module("2download_papers")
MAX_PDF_BYTES = 50 * 1024 * 1024


def import_pdf(data, filename, output_dir, doi=""):
    """导入单份 PDF；指定 DOI 时按用户所选文章关联，否则只按内容去重，不猜测 DOI。"""
    from pypdf import PdfReader

    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    if not name.lower().endswith(".pdf") or not 0 < len(data) <= MAX_PDF_BYTES:
        raise ValueError("请选择 PDF 文件，单份文件不能超过 50 MB")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请先导出可直接阅读的版本")
        pages = len(reader.pages)
        if not pages:
            raise ValueError("PDF 没有可读取的页面")
        # 读取页面对象，排除只有头部签名的错误文件；扫描件也可导入。
        for page in reader.pages:
            _ = page.mediabox
        metadata_title = str((reader.metadata.title if reader.metadata else "") or "").strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("文件不是可读取的 PDF，可能是登录网页或损坏文件") from exc
    destination = Path(output_dir).resolve()
    state = download.load_json(destination / ".download-state.json")
    doi = download.normalize_doi(doi) if doi else ""
    if doi and doi not in state:
        raise ValueError("所选文章已不在下载记录中，请刷新清单")
    checksum = hashlib.sha256(data).hexdigest()
    previous = state.get(doi, {})
    # 已有关联的不同 PDF 不覆盖，避免误选文件损坏现有知识库。
    if previous.get("status") == "downloaded" and previous.get("file"):
        old_path = (destination / previous["file"]).resolve()
        if old_path.is_relative_to(destination) and old_path.is_file():
            if download.file_digest(old_path) != checksum:
                raise ValueError("该文章已有另一份 PDF，已保留原文件；请核对所选文章")
    duplicate = None
    duplicate_key = None
    # 检查实际文件内容，不仅依赖旧状态，也识别用户手工放入目录的文件。
    for path in destination.rglob("*.pdf"):
        resolved = path.resolve()
        if resolved.is_relative_to(destination) and path.is_file() and download.file_digest(path) == checksum:
            duplicate = path
            break
    if duplicate:
        relative = duplicate.relative_to(destination).as_posix()
        for key, record in state.items():
            if record.get("file") == relative:
                duplicate_key = key
                if doi and record.get("doi") and record["doi"] != doi:
                    raise ValueError("相同 PDF 已关联另一篇文章，请核对后再导入")
        if not doi and duplicate_key:
            return dict(status="skipped", filename=name, pages=pages, file=relative)
        target = duplicate
    else:
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / ((download.digest(doi)[:24] if doi else "manual-" + checksum) + ".pdf")
        if target.exists():
            raise ValueError("目标文件已存在且内容不同，已保留原文件")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".part", delete=False) as handle:
                temporary = handle.name
                handle.write(data)
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    key = doi or "manual:" + checksum
    if doi and duplicate_key and duplicate_key != key and not state[duplicate_key].get("doi"):
        del state[duplicate_key]
    record = dict(previous, doi=doi, title=previous.get("title") or metadata_title or Path(name).stem,
                  status="downloaded", file=target.relative_to(destination).as_posix(), sha256=checksum,
                  download_source="manual", original_filename=name, pages=pages, error="", reason="")
    state[key] = record
    download.save_download_state(destination, state)
    return dict(status="linked" if duplicate else "imported", filename=name, pages=pages, file=record["file"])
