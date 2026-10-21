"""OpenAlex 缺失摘要时，使用 Crossref 官方元数据补查，不依赖 Google 页面。"""
import re
from html.parser import HTMLParser
from importlib import import_module
from urllib.parse import quote

import requests
import user_settings

rules = import_module("1doi_lookup")
RULE_VERSION = "crossref-doi-or-exact-title-abstract-v1"


def clean_abstract(value):
    """清除 Crossref 摘要中的 JATS/XML 标签，保留文字与段落间隔。"""
    if not isinstance(value, str):
        return ""
    class TextParser(HTMLParser):
        """将 JATS 摘要转换成纯文本，不执行网页脚本。"""
        def handle_data(self, data):
            """保存标签之间的原文。"""
            self.parts.append(data)
        def handle_endtag(self, tag):
            """在段落和标题末尾保留分隔。"""
            if tag.split(":")[-1] in {"p", "title", "sec"}:
                self.parts.append(" ")
    parser = TextParser(convert_charrefs=True)
    parser.parts = []
    parser.feed(value)
    text = " ".join("".join(parser.parts).split())
    return re.sub(r"^abstract\s*[:.]?\s*", "", text, flags=re.I)


class CrossrefAbstractLookup:
    """复用任务内的 Crossref 会话，优先 DOI，其次完整标题，不核对作者和年份。"""

    def __init__(self, session=None):
        """初始化连接与有界请求配置，测试可注入离线会话。"""
        self.session = session if session is not None else requests.Session()
        self.owned = session is None
        self.config = user_settings.DOI_LOOKUP_SETTINGS
        self.config.validate()
        self.disabled = ""

    def request(self, url, params=None):
        """读取固定官方 API，不跟随重定向；限流后不再发送本次任务的摘要补查。"""
        response = self.session.get(url, params=params, timeout=self.config.timeout_seconds,
            headers={"User-Agent": "LiteratureReadingAssistant/1.0", "Accept": "application/json"},
            allow_redirects=False)
        if response.status_code in {403, 429}:
            self.disabled = f"Crossref 摘要补查受限（HTTP {response.status_code}），本次任务停止补查"
            raise PermissionError(self.disabled)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Crossref 未返回有效元数据")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise ValueError("Crossref 元数据格式无效")
        return data["message"]

    def lookup(self, title, *, doi=""):
        """返回已核对的摘要及来源；无摘要、标题歧义或网络失败均说明原因后跳过。"""
        empty = dict(abstract="", abstract_lookup_version=RULE_VERSION)
        if self.disabled:
            return dict(empty, abstract_lookup_status="blocked", abstract_lookup_note=self.disabled)
        try:
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(doi).strip(), flags=re.I)
            item = None
            source = ""
            if re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]+', doi, re.I):
                source = "https://api.crossref.org/works/" + quote(doi, safe="")
                item = self.request(source)
                if item is not None and str(item.get("DOI", "")).casefold() != doi.casefold():
                    raise ValueError("Crossref 返回的 DOI 与请求不一致")
            if item is None:
                source = "https://api.crossref.org/works"
                message = self.request(source, {"query.title": title, "rows": self.config.max_candidates})
                matches = {}
                items = message.get("items", []) if message else []
                if not isinstance(items, list):
                    raise ValueError("Crossref 候选列表格式无效")
                for candidate in items[:self.config.max_candidates]:
                    verified = rules.verified_candidate(candidate, {"title": title})
                    if verified:
                        matches.setdefault(verified["doi"].casefold(), candidate)
                if len(matches) != 1:
                    note = "Crossref 同标题候选存在多个 DOI，无法确定摘要" if matches else "Crossref 未找到完整标题一致的文献"
                    return dict(empty, abstract_lookup_status="not_found", abstract_lookup_note=note)
                item = next(iter(matches.values()))
                source += "/" + quote(item["DOI"], safe="")
            abstract = clean_abstract(item.get("abstract"))
            if len(abstract) < 20:
                return dict(empty, abstract_lookup_status="not_found", abstract_lookup_note="Crossref 未提供足够摘要，跳过筛选")
            return dict(empty, abstract=abstract, abstract_source=source, abstract_lookup_status="found",
                        abstract_lookup_note="已通过 Crossref 补齐摘要")
        except (requests.RequestException, ValueError, PermissionError) as exc:
            return dict(empty, abstract_lookup_status="blocked" if self.disabled else "error",
                        abstract_lookup_note=self.disabled or f"Crossref 摘要补查失败：{exc}")

    def close(self):
        """关闭本模块创建的会话。"""
        if self.owned:
            self.session.close()
