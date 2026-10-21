"""第一步 DOI 补查：只按完整标题匹配 Crossref DOI，不要求作者或年份信息。"""
import html
import re
import unicodedata
from importlib import import_module
from urllib.parse import quote

import user_settings

_common = import_module("99common")
RULE_VERSION = "crossref-exact-title-only-v2"


def normalize_text(text):
    """规范化书目文字：忽略 HTML、大小写、重音及排版标点，但保留所有字母和数字。"""
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text)))
    text = unicodedata.normalize("NFKD", text).casefold()
    return "".join(char for char in text if char.isalnum())


def lookup_signature(record):
    """仅按规范化标题和匹配规则标识补查缓存，作者或年份变化不触发重新补查。"""
    return _common.digest(str((RULE_VERSION, normalize_text(record.get("title", "")))))


def verified_candidate(item, record):
    """只比较完整标题，标题一致且 DOI 格式有效时返回候选，不检查作者、年份或文献类型。"""
    if not isinstance(item, dict):
        return None
    doi = item.get("DOI")
    if not isinstance(doi, str) or not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]+', doi):
        return None
    source_title = normalize_text(record.get("title", ""))
    titles = item.get("title", [])
    subtitles = item.get("subtitle", [])
    if not isinstance(titles, list):
        return None
    alternatives = [title for title in titles if isinstance(title, str)]
    if isinstance(subtitles, list) and subtitles and isinstance(subtitles[0], str):
        alternatives += [title + " " + subtitles[0] for title in alternatives]
    matched_title = next((title for title in alternatives if normalize_text(title) == source_title), "")
    if not source_title or not matched_title:
        return None
    return dict(doi=doi, title=matched_title, rule=RULE_VERSION)


def lookup_crossref_doi(record, settings=None, session=None):
    """仅凭标题查询 Crossref 并采用唯一同标题 DOI，歧义或网络错误不阻断摘要筛选。"""
    config = settings or user_settings.DOI_LOOKUP_SETTINGS
    config.validate()
    empty = dict(doi="", doi_status="skipped_no_doi", doi_source="", doi_source_value="")
    if not config.enabled:
        return dict(empty, doi_lookup_status="disabled", doi_lookup_note="未启用 Crossref 补查")
    title = record.get("title")
    if not isinstance(title, str) or not normalize_text(title):
        return dict(empty, doi_lookup_status="metadata_insufficient", doi_lookup_note="缺少有效文章标题，无法补查 Crossref")
    owned = session is None
    connection = session
    try:
        if connection is None:
            import requests
            connection = requests.Session()
        params = {"query.title": title, "rows": config.max_candidates}

        def request():
            """向固定的 Crossref 公共接口请求候选，不携带模型密钥，不自动跟随重定向。"""
            response = connection.get("https://api.crossref.org/works", params=params,
                                      headers={"User-Agent": "LiteratureReadingAssistant/1.0", "Accept": "application/json"},
                                      timeout=config.timeout_seconds, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Crossref 未返回有效元数据")
            data = response.json()
            items = data.get("message", {}).get("items")
            if not isinstance(items, list):
                raise ValueError("Crossref 返回的候选格式无效")
            return items[:config.max_candidates]

        items = _common.retry(request, attempts=2, delay=1)
        verified = {}
        for item in items:
            candidate = verified_candidate(item, record)
            if candidate:
                verified.setdefault(candidate["doi"].casefold(), candidate)
        if len(verified) == 1:
            candidate = next(iter(verified.values()))
            return dict(doi=candidate["doi"], doi_status="found", doi_source="Crossref",
                        doi_source_value="https://api.crossref.org/works/" + quote(candidate["doi"], safe=""),
                        doi_lookup_status="found", doi_lookup_note="完整标题一致，未核对作者和年份",
                        doi_match_evidence=candidate)
        if len(verified) > 1:
            return dict(empty, doi_status="skipped_ambiguous_doi", doi_lookup_status="ambiguous",
                        doi_lookup_note="Crossref 存在多个符合核验条件的 DOI，需人工核查",
                        doi_candidates=list(verified.values()))
        return dict(empty, doi_lookup_status="not_found", doi_lookup_note="Crossref 未找到完整标题一致的候选")
    except Exception as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {code}" if code else type(exc).__name__
        return dict(empty, doi_lookup_status="error", doi_lookup_note=f"Crossref 补查失败：{detail}；可稍后重试")
    finally:
        if owned and connection is not None:
            connection.close()
