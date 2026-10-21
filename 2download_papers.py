"""第二步全文下载：通过 Zotero 获取 PDF，未完成的文章转入手动补充清单。"""
import csv
import io
import os
import re
import shutil
import tempfile
import time
import uuid
from importlib import import_module
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import user_settings
_common = import_module("99common")
digest = _common.digest
file_digest = _common.file_digest
load_json = _common.load_json
save_json = _common.save_json
atomic_write = _common.atomic_write
retry = _common.retry


def normalize_doi(value):
    """规范化已知 DOI：只处理完整 DOI 或 doi.org 链接，不根据标题搜索或猜测。"""
    text = str(value or "").strip()
    if text.startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/")):
        text = unquote(urlsplit(text).path.lstrip("/"))
    if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]+', text, re.I):
        raise ValueError(f"无效 DOI：{text}")
    return text.casefold()


def read_download_list(path):
    """读取 .papers.json 或逐行 DOI 文本，按 DOI 去重并保留原始元数据。"""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = load_json(path)
        papers = data.get("papers")
        if not isinstance(papers, list):
            raise ValueError("JSON 输入必须包含 papers 文献列表")
    else:
        papers = [dict(doi=line.strip()) for line in path.read_text(encoding="utf-8-sig").splitlines()
                  if line.strip()]
    result = {}
    for paper in papers:
        if not isinstance(paper, dict) or not paper.get("doi"):
            continue
        doi = normalize_doi(paper["doi"])
        result.setdefault(doi, dict(paper, doi=doi))
    return list(result.values())


def is_pdf(path):
    """检查文件签名，防止把登录页或错误页面当作已下载 PDF。"""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


class ZoteroBridge:
    """本机插件连接器：进行令牌鉴权、提交下载任务并轮询状态。"""

    def __init__(self, settings, session=None):
        """读取本机配对令牌，创建不会经过系统代理的本地 HTTP 会话。"""
        settings.validate()
        self.settings = settings
        config = load_json(settings.bridge_config)
        token = config.get("token")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("请先运行 python 2build_zotero_bridge.py，并在 Zotero 安装生成的插件")
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.session.trust_env = False
        self.session.headers.update({"X-Literature-Token": token})
        self.base = settings.zotero_url.rstrip("/") + "/literature-assistant"

    def request(self, endpoint, data=None):
        """发送一次短请求并处理临时连接失败，下载本身由 Zotero 后台任务执行。"""
        def send():
            """执行 HTTP 请求并解析 JSON 响应，拒绝错误状态或无效响应。"""
            if data is None:
                response = self.session.get(self.base + endpoint, timeout=15, allow_redirects=False)
            else:
                response = self.session.post(self.base + endpoint, json=data, timeout=15, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ValueError("本机 Zotero 接口不应重定向到其他地址")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("Zotero 插件返回了无效响应")
            return value
        return retry(send)

    def ping(self):
        """检查插件是否在线；未启动 Zotero、未装插件或令牌错误时提供配置提示。"""
        try:
            return self.request("/ping")
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code == 404:
                detail = "Zotero 主程序已响应，但插件接口不存在（HTTP 404）。请安装新版插件并重启 Zotero"
            elif code == 403:
                detail = "插件拒绝配对令牌（HTTP 403）。请安装与当前 .zotero-bridge.json 配套的插件包"
            else:
                detail = ("请打开 Zotero 并确认已安装 dist/literature-bridge.xpi；"
                          f"底层错误：{type(exc).__name__}：{exc}")
            raise RuntimeError(f"无法连接 Zotero 下载插件：{detail}") from exc

    def download(self, paper, job_id):
        """提交单篇任务并等待完成，超时只停止等待，不假定 Zotero 已取消后台操作。"""
        payload = {k: paper[k] for k in ("doi", "title", "authors", "year", "abstract") if k in paper}
        if self.settings.collection_name.strip():
            status = self.ping()
            if "collections" not in status.get("capabilities", []):
                raise RuntimeError("当前 Zotero 插件不支持按调研分类，请安装新版 dist/literature-bridge.xpi 并重启 Zotero")
            payload["collection_name"] = self.settings.collection_name.strip()
        job = self.request("/jobs", dict(payload, action="download", job_id=job_id))
        deadline = time.monotonic() + self.settings.timeout_seconds
        while job.get("status") in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待 Zotero 下载超时；任务可能仍在 Zotero 中继续，下次运行会复用已有条目")
            time.sleep(self.settings.poll_seconds)
            job = self.request("/jobs", dict(action="status", job_id=job["job_id"]))
        if job.get("status") not in {"downloaded", "unavailable", "failed"}:
            raise ValueError("Zotero 插件返回未知任务状态")
        if normalize_doi(job.get("doi")) != paper["doi"]:
            raise ValueError("Zotero 返回的任务 DOI 与请求不一致")
        if payload.get("collection_name") and job.get("collection_name") != payload["collection_name"]:
            raise ValueError("Zotero 返回的分类名称与当前调研不一致")
        return job

    def close(self):
        """关闭本机 HTTP 会话，不关闭 Zotero 本身。"""
        self.session.close()


def copy_pdf(source, destination):
    """验证并安全复制 Zotero 下载的 PDF，不移动原附件、不写入不完整文件。"""
    if not is_pdf(source):
        raise ValueError("Zotero 返回的附件不存在或不是 PDF")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
            temporary = handle.name
        shutil.copyfile(source, temporary)
        if not is_pdf(temporary):
            raise ValueError("复制后的文件不是 PDF")
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def unavailable_reason(job):
    """说明未获取全文的已知原因，翻译插件通用信息，不把付费、登录或网络问题猜成已确认原因。"""
    raw = job.get("reason") or job.get("error") or ""
    detail = raw.strip() if isinstance(raw, str) else ""
    generic = {"", "no accessible pdf", "zotero did not obtain an accessible pdf"}
    if detail.casefold().rstrip(".") in generic:
        return "Zotero 未获取到可访问的 PDF；插件未返回具体原因，暂无法确定是否与访问权限或网络有关。"
    return detail


def manual_download_items(state):
    """整理尚未完成下载的文章，使用 DOI 官方跳转链接，避免保存会过期的签名下载地址。"""
    result = []
    for record in state.values():
        if record.get("status") not in {"failed", "unavailable", "pending"} or not record.get("doi"):
            continue
        doi = normalize_doi(record["doi"])
        reason = record.get("reason") or record.get("error") or "上次任务尚未完成，可手动补充 PDF"
        result.append(dict(doi=doi, title=record.get("title") or "（清单未提供标题）",
                           url="https://doi.org/" + quote(doi, safe="/"), reason=reason))
    return result


def save_download_state(destination, state):
    """保存下载状态、完整 CSV 和待手动下载清单；手动导入后同步移除已完成条目。"""
    destination = Path(destination)
    save_json(destination / ".download-state.json", state)
    buffer = io.StringIO(newline="")
    fields = ["doi", "title", "status", "file", "sha256", "item_key", "attachment_key",
              "collection_name", "collection_key", "download_source", "reason", "error"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(state.values())
    atomic_write(destination / "download_report.csv", "\ufeff" + buffer.getvalue())
    items = manual_download_items(state)
    lines = ["# 待手动下载文献", "", f"共 {len(items)} 篇。通过校园 VPN 或机构登录获取 PDF 后，在软件中点击对应文章的“导入 PDF”。",
             "", "导入只复制到 PDF 下载目录，不调用模型；完成后点击更新知识库。", ""]
    for number, item in enumerate(items, 1):
        title = re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", item["title"].replace("\n", " "))
        lines.extend([f"## {number}. {title}", "", f"DOI：{item['doi']}", "",
                      f"[打开文章入口]({item['url']})", "", "未下载原因：", "",
                      *["> " + line for line in str(item['reason']).splitlines()], ""])
    atomic_write(destination / "manual_downloads.md", "\n".join(lines))


def download_papers(input_file=None, output_dir=None, *, settings=None, limit=None, dry_run=False, bridge=None):
    """通过 Zotero 下载并记录失败原因，未完成项供手动补充；已校验的本地文件直接复用。"""
    settings = settings or user_settings.DOWNLOAD_SETTINGS
    if output_dir is not None:
        settings = replace(settings, output_dir=Path(output_dir))
    settings.validate()
    if limit is not None and limit < 1:
        raise ValueError("limit 必须为正整数")
    source = Path(input_file) if input_file else import_module("1abstract_read").screening_report_path().with_suffix(".papers.json")
    papers = read_download_list(source)
    print(f"下载清单：{source}（去重后 {len(papers)} 篇）")
    destination = Path(settings.output_dir)
    state_path = destination / ".download-state.json"
    state = load_json(state_path)
    stats = dict(found=len(papers), downloaded=0, skipped=0, unavailable=0, failed=0, pending=0)
    attempted = 0
    owns_bridge = bridge is None
    connection_error = ""

    def persist():
        """保存逐篇状态与 CSV 结果，明确区分 PDF 成功、无可用全文和程序错误。"""
        save_download_state(destination, state)

    try:
        for paper in papers:
            doi = paper["doi"]
            previous = state.get(doi, {})
            target = destination / (digest(doi)[:24] + ".pdf")
            existing = (destination / (previous.get("file") or target.name)).resolve()
            if existing.is_relative_to(destination.resolve()):
                target = existing
            if (previous.get("status") == "downloaded" and is_pdf(target)
                    and previous.get("sha256") == file_digest(target)):
                stats["skipped"] += 1
                continue
            if limit is not None and attempted >= limit:
                break
            attempted += 1
            print(f"{'待下载' if dry_run else '获取全文'}：{doi}")
            if dry_run:
                stats["pending"] += 1
                continue
            record = dict(doi=doi, title=paper.get("title", ""), status="pending", job_id=uuid.uuid4().hex)
            state[doi] = record
            persist()
            try:
                job = {}
                try:
                    if connection_error:
                        raise RuntimeError(connection_error)
                    if bridge is None:
                        try:
                            bridge = ZoteroBridge(settings)
                            bridge.ping()
                        except Exception as exc:
                            connection_error = str(exc)
                            raise
                    job = bridge.download(paper, record["job_id"])
                    if job["status"] == "downloaded":
                        copy_pdf(job["path"], target)
                except Exception as exc:
                    job = dict(job, status="failed", error=str(exc))
                record.update(status=job["status"], item_key=job.get("item_key", ""),
                              collection_name=job.get("collection_name", ""), collection_key=job.get("collection_key", ""),
                              attachment_key=job.get("attachment_key", ""), error=job.get("error", ""))
                if job["status"] == "downloaded":
                    record.update(download_source="Zotero")
                elif job["status"] == "unavailable":
                    record["reason"] = unavailable_reason(job)
                else:
                    record["reason"] = record["error"] or "Zotero 任务失败，插件未返回具体原因"
                if record["status"] == "downloaded":
                    record.update(file=target.name, sha256=file_digest(target))
                    stats["downloaded"] += 1
                elif record["status"] == "unavailable":
                    record.setdefault("reason", unavailable_reason(job))
                    stats["unavailable"] += 1
                    print(f"  DOI：{doi}")
                    print(f"  原因：{record['reason']}")
                else:
                    stats["failed"] += 1
                    print(f"获取失败：{doi}：{record.get('reason') or record.get('error') or '未返回具体原因'}")
            except Exception as exc:
                record.update(status="failed", error=str(exc))
                stats["failed"] += 1
                print(f"获取失败：{doi}：{exc}")
            persist()
    finally:
        if owns_bridge and bridge is not None:
            bridge.close()
    if not dry_run:
        persist()
    print(f"下载统计：{stats}")
    return stats


if __name__ == "__main__":
    main = import_module("0assistant").main
    import sys
    raise SystemExit(main(["download", *sys.argv[1:]]))
