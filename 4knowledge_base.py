"""第四步知识库：按页提取 PDF 原文，增量建库，并结合语义和关键词检索证据。"""
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from importlib import import_module
from pathlib import Path

import user_settings

_common = import_module("99common")
file_digest = _common.file_digest
load_json = _common.load_json
digest = _common.digest
_embedding = import_module("4embedding_api")
GeminiEmbedder = _embedding.GeminiEmbedder
normalize_vector = _embedding.normalize_vector
PDF_STREAM_RETRY_LIMIT = 5_000_000


def index_signature(source, config, api):
    """记录索引兼容信息，不包含密钥；更换来源、模型、接口地址或切分方式需要新建数据库。"""
    return {"version": 1, "source": str(source.resolve()), "provider": api.provider,
            "model": api.model, "base_url": api.base_url,
            "dimensions": config.embedding_dimensions, "chunk_chars": config.chunk_chars,
            "overlap_chars": config.overlap_chars, "parser": "pypdf-page-paragraph-v2"}


def open_index(path, signature=None):
    """打开知识库，建库时检查配置兼容性；查询使用只读连接且不创建文件。"""
    path = Path(path).resolve()
    if signature is None:
        if not path.is_file():
            raise ValueError("知识库不存在，请先运行 index 命令")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        if signature is not None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, title TEXT NOT NULL,
                    doi TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
                    page INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL, vector TEXT NOT NULL);
            """)
            old = connection.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
            if old and json.loads(old[0]) != signature:
                previous = json.loads(old[0])
                if (previous.get("parser") == "pypdf-page-text-v1"
                        and dict(previous, parser=signature["parser"]) == signature):
                    # 仅解析算法升级时允许原位重建；旧片段在逐篇替换前保持可恢复但不再参与检索。
                    with connection:
                        connection.execute("UPDATE documents SET fingerprint='reparse:' || fingerprint")
                        connection.execute("UPDATE metadata SET value=? WHERE key='signature'",
                                           (json.dumps(signature, sort_keys=True),))
                    print("文本切分已升级，将重新提取原文并更新向量；PDF 文件保持不变")
                else:
                    raise ValueError("知识库配置已变化，请使用 --database 指定新数据库，避免混用不同向量")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES ('signature', ?)",
                               (json.dumps(signature, sort_keys=True),))
            connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def normalize_page_text(text):
    """合并段内排版换行、修复断行连字符和字体连字，保留空行与可识别的编号标题。"""
    # 只展开字体连字，不用整体 NFKC，避免把数学上标、下标等变成普通数字。
    text = text.replace("\x00", "").replace("\r\n", "\n").translate(
        str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}))
    text = re.sub(r"(?<=[A-Za-z])-\n\s*(?=[a-z])", "", text)
    paragraphs, lines = [], []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        heading = bool(re.match(r"^\d+(?:\.\d+)*\s+[A-Z][^.!?]{0,100}$", line))
        if not line or heading:
            if lines:
                paragraphs.append(" ".join(lines))
                lines = []
            if heading:
                paragraphs.append(line)
        else:
            lines.append(line)
    if lines:
        paragraphs.append(" ".join(lines))
    return "\n\n".join(paragraphs)


def split_page(text, chunk_chars, overlap_chars):
    """优先保留完整段落，长段按句切分；仅超长单句退回词边界，重叠不从半句开始。"""
    if chunk_chars <= 0 or not 0 <= overlap_chars < chunk_chars:
        raise ValueError("切分长度或重叠长度无效")
    text = normalize_page_text(text)
    units = []
    for paragraph in text.split("\n\n"):
        if not paragraph:
            continue
        sentences = ([paragraph] if len(paragraph) <= chunk_chars else
                     re.split(r"(?<=[.!?])\s+(?=[A-Z\"“])|(?<=[。！？])", paragraph))
        for sentence in sentences:
            sentence = sentence.strip()
            while len(sentence) > chunk_chars:
                boundary = sentence.rfind(" ", 0, chunk_chars + 1)
                boundary = boundary if boundary > 0 else chunk_chars
                units.append(sentence[:boundary].strip())
                sentence = sentence[boundary:].strip()
            if sentence:
                units.append(sentence)
    chunks, current = [], []
    for unit in units:
        if current and len("\n\n".join(current + [unit])) > chunk_chars:
            chunks.append("\n\n".join(current))
            overlap = []
            for previous in reversed(current):
                if len("\n\n".join([previous] + overlap)) > overlap_chars:
                    break
                overlap.insert(0, previous)
            current = overlap
            while current and len("\n\n".join(current + [unit])) > chunk_chars:
                current.pop(0)
        current.append(unit)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def extract_page_text(path, page, page_number):
    """超出默认内容流解析上限时，用新读取器在局部 500 万上限下重试一次，保留其他限制。"""
    from pypdf import PdfReader, apply_configuration, get_configuration
    from pypdf.errors import LimitReachedError
    try:
        return page.extract_text() or ""
    except LimitReachedError as exc:
        if str(exc) != "Reached maximum number of tokens per stream.":
            raise ValueError(f"PDF 第 {page_number} 页超出解析器资源限制：{exc}") from exc
        if get_configuration().stream_parsing_work_maximum_count >= PDF_STREAM_RETRY_LIMIT:
            raise ValueError(f"PDF 第 {page_number} 页内容流过于复杂，超出解析上限；请重新导出 PDF 后再导入") from exc
        print(f"提示：{Path(path).name} 第 {page_number} 页内容流较复杂，正在提高解析上限重试一次")
    try:
        with apply_configuration(stream_parsing_work_maximum_count=PDF_STREAM_RETRY_LIMIT):
            # 使用新读取器，避免失败解析留下的部分对象缓存影响文字完整性。
            with PdfReader(str(path)) as retry_reader:
                if retry_reader.is_encrypted:
                    retry_reader.decrypt("")
                return retry_reader.pages[page_number - 1].extract_text() or ""
    except LimitReachedError as exc:
        raise ValueError(f"PDF 第 {page_number} 页提高上限后仍无法完整解析；请重新导出 PDF 后再导入") from exc


def remove_repeated_marginal_text(page_texts):
    """仅清除跨页重复且含 DOI 或网站域名的页边短行，避免误删正文或小节标题。"""
    pages = [[line for line in text.splitlines() if line.strip()] for text in page_texts]
    counts = Counter()
    candidates = []
    for lines in pages:
        margins = {}
        for index in set(range(min(3, len(lines)))) | set(range(max(0, len(lines) - 3), len(lines))):
            line = " ".join(lines[index].split())
            if len(line) <= 160 and re.search(r"10\.\d{4,9}/|\.(?:org|com|edu)(?=\d|\b)", line, re.I):
                margins[index] = re.sub(r"\d+$", "", line.casefold())
        candidates.append(margins)
        counts.update(set(margins.values()))
    # 原有空行必须保留，按行内容匹配边缘行后再从原始页面中删除。
    cleaned = []
    for text, lines, margins in zip(page_texts, pages, candidates):
        removed = {lines[index] for index, key in margins.items() if counts[key] >= 2}
        cleaned.append("\n".join(line for line in text.splitlines() if line not in removed))
    return cleaned


def extract_pdf(path, config):
    """按 PDF 实际页序提取文字和切片；空白页单独记录，整篇无文字时提示需要 OCR。"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 已加密，需要先解密")
    chunks, empty_pages = [], []
    page_texts = [extract_page_text(path, page, number) for number, page in enumerate(reader.pages, 1)]
    for page_number, text in enumerate(remove_repeated_marginal_text(page_texts), 1):
        parts = split_page(text, config.chunk_chars, config.overlap_chars)
        if not parts:
            empty_pages.append(page_number)
        for ordinal, part in enumerate(parts, 1):
            chunks.append({"page": page_number, "ordinal": ordinal, "text": part})
    if not chunks:
        raise ValueError("PDF 未提取到文字，可能是扫描件，需要先进行 OCR")
    metadata = reader.metadata
    title = str((metadata.title if metadata else "") or "").strip()
    return chunks, title, empty_pages


def download_metadata(source):
    """读取下载状态中的标题和 DOI，并按 PDF 绝对路径关联；不从正文引文猜测 DOI。"""
    result = {}
    for state_path in source.rglob(".download-state.json"):
        for record in load_json(state_path).values():
            if isinstance(record, dict) and record.get("file"):
                path = (state_path.parent / record["file"]).resolve()
                if path.is_relative_to(source):
                    result[str(path)] = {"title": record.get("title") or "", "doi": record.get("doi") or ""}
    return result


def build_knowledge_base(input_dir=None, database=None, *, limit=None, dry_run=False,
                         config=None, model_settings=None, embedder=None):
    """增量建立知识库：每篇成功后提交事务，失败可重试，未变化文献不重复计算向量。"""
    config = config or user_settings.KNOWLEDGE_SETTINGS
    api = model_settings or user_settings.EMBEDDING_MODEL_SETTINGS
    config.validate()
    api.validate()
    source = Path(input_dir or config.input_dir or user_settings.DOWNLOAD_SETTINGS.output_dir).resolve()
    destination = Path(database or config.database).resolve()
    if not source.is_dir():
        raise ValueError(f"PDF 目录不存在：{source}")
    if limit is not None and limit < 1:
        raise ValueError("limit 必须为正整数")
    paths = sorted(path.resolve() for path in source.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
    stats = dict(found=len(paths), indexed=0, skipped=0, failed=0, pending=0)
    if dry_run:
        for path in paths[:limit]:
            print(f"待检查 PDF：{path}")
        stats["pending"] = len(paths[:limit])
        print(f"建库预览：{stats}；不读取 PDF 内容、不调用 API、不写入数据库")
        return stats
    metadata = download_metadata(source)
    engine = embedder or GeminiEmbedder(api, config.embedding_dimensions, config.embedding_batch_size)
    try:
        # 配置错误属于整个任务，必须在解析 PDF、修改索引及逐篇容错之前检查。
        if embedder is None:
            engine.prepare()
        with closing(open_index(destination, index_signature(source, config, api))) as db:
            live = {str(path) for path in paths}
            with db:
                for row in db.execute("SELECT path FROM documents").fetchall():
                    if row[0] not in live:
                        db.execute("DELETE FROM documents WHERE path=?", (row[0],))
            attempted = 0
            for path in paths:
                if limit is not None and attempted >= limit:
                    break
                key = str(path)
                fingerprint = ""
                info = metadata.get(key, {})
                try:
                    fingerprint = file_digest(path)
                    old = db.execute("SELECT * FROM documents WHERE path=?", (key,)).fetchone()
                    if old and old["fingerprint"] == fingerprint and old["status"] == "ready":
                        # 下载元数据改变不需要重新计算正文向量。
                        with db:
                            db.execute("UPDATE documents SET title=?, doi=? WHERE path=?",
                                       (info.get("title") or old["title"], info.get("doi", ""), key))
                        stats["skipped"] += 1
                        continue
                    attempted += 1
                    chunks, title, empty_pages = extract_pdf(path, config)
                    vectors = engine.embed([chunk["text"] for chunk in chunks])
                    if len(vectors) != len(chunks):
                        raise ValueError("向量数量与文本片段数量不匹配")
                    vectors = [normalize_vector(vector, config.embedding_dimensions) for vector in vectors]
                    if file_digest(path) != fingerprint:
                        raise ValueError("建库过程中 PDF 内容发生变化，请重新运行")
                    with db:
                        db.execute("DELETE FROM documents WHERE path=?", (key,))
                        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, 'ready', '')",
                                   (key, fingerprint, info.get("title") or title or path.stem, info.get("doi", "")))
                        db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)", [
                            (digest(f"{key}:{chunk['page']}:{chunk['ordinal']}"), key,
                             chunk["page"], chunk["ordinal"], chunk["text"], json.dumps(vector))
                            for chunk, vector in zip(chunks, vectors)])
                    stats["indexed"] += 1
                    print(f"已建库：{path.name}，{len(chunks)} 个片段")
                    if empty_pages:
                        print(f"提示：{path.name} 的 PDF 页 {empty_pages} 没有可提取文字，未建立这些页的索引")
                except Exception as exc:
                    if not fingerprint:
                        attempted += 1
                    with db:
                        db.execute("DELETE FROM documents WHERE path=?", (key,))
                        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, 'failed', ?)",
                                   (key, fingerprint, info.get("title") or path.stem, info.get("doi", ""), str(exc)))
                    stats["failed"] += 1
                    print(f"建库失败：{path.name}：{exc}")
                    if isinstance(exc, _common.APICreditsExhaustedError):
                        raise
    finally:
        if embedder is None:
            engine.close()
    stats["pending"] = stats["found"] - stats["indexed"] - stats["skipped"] - stats["failed"]
    print(f"建库统计：{stats}")
    return stats


def tokenize(text):
    """关键词分词：保留英文专业词和数字，中文按相邻双字切分，供本地 BM25 使用。"""
    tokens = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text.lower())
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(part[i:i + 2] for i in range(max(1, len(part) - 1)))
    return tokens


def rank_chunks(rows, question, query_vector, config):
    """结合余弦语义相似度和 BM25 关键词排名，通过倒数排名融合返回候选证据。"""
    query_terms = set(tokenize(question))
    counts = [Counter(tokenize(row["text"])) for row in rows]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(rows), 1) or 1
    frequencies = {term: sum(term in count for count in counts) for term in query_terms}
    semantic, lexical = [], []
    for i, row in enumerate(rows):
        vector = normalize_vector(json.loads(row["vector"]), len(query_vector))
        cosine = sum(a * b for a, b in zip(query_vector, vector))
        if cosine >= config.min_similarity:
            semantic.append((i, cosine))
        score = 0.0
        for term in query_terms:
            tf = counts[i][term]
            df = frequencies[term]
            idf = math.log(1 + (len(rows) - df + 0.5) / (df + 0.5))
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * lengths[i] / average))
        if score > 0:
            lexical.append((i, score))
    fused = Counter()
    for ranking in (semantic, lexical):
        ranking.sort(key=lambda item: item[1], reverse=True)
        for rank, (i, _) in enumerate(ranking[:max(30, config.top_k * 5)], 1):
            fused[i] += 1 / (60 + rank)
    return [dict(rows[i], score=score) for i, score in fused.most_common()]


def retrieve(question, database=None, *, config=None, model_settings=None, embedder=None, min_documents=0):
    """检索原文证据：验证向量配置和候选 PDF 指纹，过期或删除的文献不用于回答。"""
    config = config or user_settings.KNOWLEDGE_SETTINGS
    api = model_settings or user_settings.EMBEDDING_MODEL_SETTINGS
    config.validate()
    api.validate()
    if not question.strip():
        raise ValueError("问题不能为空")
    with closing(open_index(database or config.database)) as db:
        signature_row = db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
        if not signature_row:
            raise ValueError("知识库缺少配置信息，请重新建库")
        signature = json.loads(signature_row[0])
        expected = index_signature(Path(signature["source"]), config, api)
        if signature != expected:
            if signature.get("parser") == "pypdf-page-text-v1" and dict(signature, parser=expected["parser"]) == expected:
                raise ValueError("原文段落切分已升级，请先点击更新知识库，重新提取原文并生成向量")
            raise ValueError("知识库配置与当前向量配置不匹配，请恢复原设置或新建数据库")
        rows = db.execute("""SELECT c.*, d.title, d.doi, d.fingerprint FROM chunks c
                             JOIN documents d ON c.path=d.path WHERE d.status='ready'
                             ORDER BY c.path, c.page, c.ordinal""").fetchall()
    if not rows:
        raise ValueError("知识库没有可用文献，请先完成建库并处理失败记录")
    engine = embedder or GeminiEmbedder(api, config.embedding_dimensions, config.embedding_batch_size)
    try:
        vectors = engine.embed([question], query=True)
        if len(vectors) != 1:
            raise ValueError("问题向量数量无效")
        query_vector = normalize_vector(vectors[0], config.embedding_dimensions)
    finally:
        if embedder is None:
            engine.close()
    hits, checked = [], {}
    for row in rank_chunks(rows, question, query_vector, config):
        key = row["path"]
        if key not in checked:
            try:
                checked[key] = file_digest(Path(key)) == row["fingerprint"]
            except OSError:
                checked[key] = False
            if not checked[key]:
                print(f"跳过过期或不可读文献，请重新建库：{key}")
        if checked[key]:
            # 问答需要两篇全文时，片段配额满后仅补足另一篇文献，不塞入同一篇的更多片段。
            selected_paths = {item["path"] for item in hits}
            if len(hits) >= config.top_k and key in selected_paths:
                continue
            row.pop("vector", None)
            row.pop("fingerprint", None)
            hits.append(row)
            if len(hits) >= config.top_k and len({item["path"] for item in hits}) >= min_documents:
                break
    return hits


if __name__ == "__main__":
    import sys
    raise SystemExit(import_module("0assistant").main(["index", *sys.argv[1:]]))
