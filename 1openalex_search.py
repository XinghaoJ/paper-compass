"""OpenAlex 官方检索：按关键词和年份获取文献元数据，供现有摘要筛选流程使用。"""
import os
import requests
from importlib import import_module


class OpenAlexSearchError(RuntimeError):
    """OpenAlex 服务访问或返回数据异常，可由上层触发备用检索。"""


def restore_abstract(index):
    """按词语位置还原 OpenAlex 倒排摘要；缺失或损坏的摘要返回空白，不猜测原文。"""
    if not isinstance(index, dict) or not index:
        return ""
    words = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            return ""
        for position in positions:
            if type(position) is not int or not 0 <= position < 50000:
                return ""
            if position in words and words[position] != word:
                return ""
            words[position] = word
    if not words or len(words) != max(words) + 1:
        return ""
    return " ".join(words[position] for position in range(len(words)))


def publication_record(work):
    """将 OpenAlex 元数据映射为统一筛选记录，保留真实 DOI、作者、年份及来源标识。"""
    location = work.get("primary_location") or {}
    authors = [item.get("author", {}).get("display_name", "")
               for item in (work.get("authorships") or []) if isinstance(item, dict)]
    return dict(
        source="openalex", source_id=work.get("id") or "", doi=work.get("doi") or "",
        pub_url=location.get("landing_page_url") or work.get("doi") or work.get("id") or "",
        bib=dict(title=work.get("title") or work.get("display_name") or "",
                 pub_year=work.get("publication_year") or "", author=[name for name in authors if name],
                 abstract=restore_abstract(work.get("abstract_inverted_index"))),
    )


def search_openalex(settings, *, session=None):
    """分页检索并严格限制返回篇数；限流或连接失败时停止，不自动切换来源或无限重试。"""
    settings.validate()
    owns_session = session is None
    session = session or requests.Session()
    headers = {"User-Agent": "LiteratureAssistant/1.0", "Accept": "application/json"}
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    per_page = min(settings.max_papers, 100)
    count = 0
    try:
        for page in range(1, (settings.max_papers + per_page - 1) // per_page + 1):
            try:
                response = session.get("https://api.openalex.org/works", params={
                    "search": settings.keywords,
                    "filter": f"publication_year:{settings.year_start}-{settings.year_end}",
                    "page": page, "per_page": per_page,
                    "select": "id,title,doi,publication_year,authorships,primary_location,abstract_inverted_index",
                }, headers=headers, timeout=30, allow_redirects=False)
            except requests.RequestException:
                raise OpenAlexSearchError("OpenAlex 连接失败，请检查网络连接后重试") from None
            if response.status_code == 429:
                raise OpenAlexSearchError("OpenAlex 请求受限或每日额度已用尽，请稍后重试；可配置自己的 OPENALEX_API_KEY 提高额度")
            if response.status_code in {401, 403}:
                raise OpenAlexSearchError("OpenAlex 拒绝访问，请检查 OPENALEX_API_KEY 是否有效并已激活")
            if response.status_code != 200:
                raise OpenAlexSearchError(f"OpenAlex 检索失败（HTTP {response.status_code}），请稍后重试")
            try:
                data = response.json()
            except ValueError:
                raise OpenAlexSearchError("OpenAlex 返回的数据无法解析") from None
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or any(not isinstance(work, dict) for work in results):
                raise OpenAlexSearchError("OpenAlex 返回的文献列表格式异常")
            for work in results:
                try:
                    record = publication_record(work)
                except (TypeError, AttributeError, ValueError):
                    raise OpenAlexSearchError("OpenAlex 返回的文献字段格式异常") from None
                yield record
                count += 1
                if count >= settings.max_papers:
                    return
            if len(results) < per_page:
                return
    finally:
        if owns_session:
            session.close()


def lookup_openalex_doi(record, settings=None, *, session=None):
    """为备用来源的文章补查 OpenAlex DOI，只接受完整标题一致的唯一结果，不核对作者与年份。"""
    import user_settings
    rules = import_module("1doi_lookup")
    config = settings or user_settings.DOI_LOOKUP_SETTINGS
    config.validate()
    empty = dict(doi="", doi_status="skipped_no_doi", doi_source="", doi_source_value="")
    title = record.get("title", "")
    if not config.enabled or not rules.normalize_text(title):
        return empty
    owns_session = session is None
    connection = session or requests.Session()
    headers = {"User-Agent": "LiteratureAssistant/1.0", "Accept": "application/json"}
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        response = connection.get("https://api.openalex.org/works", params={
            "search.title": title, "per_page": config.max_candidates, "select": "id,title,doi"},
            headers=headers, timeout=config.timeout_seconds, allow_redirects=False)
        if response.status_code != 200:
            return dict(empty, doi_lookup_note=f"OpenAlex DOI 补查失败（HTTP {response.status_code}）")
        data = response.json()
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return dict(empty, doi_lookup_note="OpenAlex DOI 候选格式异常")
        candidates = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            doi = item.get("doi") or ""
            if not isinstance(doi, str):
                continue
            doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            candidate = rules.verified_candidate({"DOI": doi, "title": [item.get("title")]}, record)
            if candidate:
                candidates.setdefault(doi.casefold(), (doi, item.get("id", "")))
        if len(candidates) == 1:
            doi, source = next(iter(candidates.values()))
            return dict(doi=doi, doi_status="found", doi_source="OpenAlex.lookup", doi_source_value=source,
                        doi_lookup_note="OpenAlex 完整标题一致，未核对作者和年份")
        if candidates:
            return dict(empty, doi_status="skipped_ambiguous_doi", doi_lookup_note="OpenAlex 同标题对应多个 DOI，需人工核查")
        return empty
    except (requests.RequestException, ValueError):
        return dict(empty, doi_lookup_note="OpenAlex DOI 补查连接或解析失败")
    finally:
        if owns_session:
            connection.close()
