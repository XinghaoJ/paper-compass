"""知识库离线测试：真实 PDF 解析配合模拟向量和回答接口，不调用网络。"""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from user_settings import KnowledgeSettings, ModelSettings, QASettings

kb = import_module("4knowledge_base")
qa = import_module("5ask_papers")
embedding = import_module("4embedding_api")
assistant = import_module("0assistant")


class KnowledgeTests(unittest.TestCase):
    """覆盖建库恢复、原文定位、检索一致性及引用拒绝条件。"""

    def setUp(self):
        """创建临时 PDF 库和模拟接口，每条测试使用独立数据库。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "pdf"
        self.source.mkdir()
        self.config = KnowledgeSettings(input_dir=self.source, database=self.root / "index.sqlite3",
                                        answer_dir=self.root / "answers", embedding_dimensions=3,
                                        chunk_chars=200, overlap_chars=30, top_k=3)
        self.api = ModelSettings(model="gemini-embedding-001")
        self.engine = Mock()
        self.engine.embed.side_effect = self.vectors
        self.client = Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "claims": [{"text": "波浪与混合有关。", "sources": ["S1"]}], "limitations": "仅依据检索片段。"}))
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()

    def tearDown(self):
        """关闭输出捕获并清理临时文献和索引。"""
        self.capture.__exit__(None, None, None)
        self.temp.cleanup()

    def vectors(self, texts, *, query=False):
        """模拟文本向量，测试关注流程与来源一致性，不将模拟值当作语义质量评估。"""
        return [[1, 0, 0] for text in texts]

    def pdf(self, name="waves.pdf", pages=None):
        """生成带真实文本层的多页 PDF，用于验证提取页码而非模拟 PDF 解析。"""
        path = self.source / name
        writer = PdfWriter()
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        for text in pages or ["Ocean waves and Stokes drift.", "Waves enhance mixing under specified conditions."]:
            page = writer.add_blank_page(width=600, height=800)
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(str(path))
        return path

    def build(self, **kwargs):
        """使用临时配置和模拟向量接口执行建库。"""
        return kb.build_knowledge_base(config=self.config, model_settings=self.api,
                                       embedder=self.engine, **kwargs)

    def test_missing_key_stops_before_parsing_or_modifying_index(self):
        """多篇 PDF 缺少密钥时只报一次任务错误，不解析任何 PDF，也不改写已有数据库。"""
        self.pdf("first.pdf")
        self.pdf("second.pdf")
        self.config.database.write_bytes(b"existing-index-must-stay-unchanged")
        with patch.dict("os.environ", {}, clear=True), patch.object(kb, "extract_pdf") as extract:
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                kb.build_knowledge_base(config=self.config, model_settings=replace(self.api, api_key=""))
        extract.assert_not_called()
        self.assertEqual(self.config.database.read_bytes(), b"existing-index-must-stay-unchanged")

    def test_index_preview_does_not_require_api_key(self):
        """预览不调用向量接口，所以无需密钥且不创建数据库。"""
        self.pdf("preview.pdf")
        with patch.dict("os.environ", {}, clear=True), patch.object(embedding._api, "create_client") as create:
            stats = kb.build_knowledge_base(config=self.config, model_settings=self.api, dry_run=True)
        self.assertEqual(stats["pending"], 1)
        create.assert_not_called()
        self.assertFalse(self.config.database.exists())

    def test_complex_content_stream_retry_preserves_text_and_configuration(self):
        """真实解析预算触发后只重试失败页面，保留完整文本及页码，并恢复默认资源限制。"""
        from pypdf import apply_configuration, get_configuration
        path = self.pdf(pages=["Text before figure.", "Text after figure."])
        original = get_configuration()
        with apply_configuration(stream_parsing_work_maximum_count=1):
            chunks, _, empty = kb.extract_pdf(path, self.config)
            self.assertEqual(get_configuration().stream_parsing_work_maximum_count, 1)
        self.assertEqual(get_configuration(), original)
        self.assertEqual(empty, [])
        self.assertEqual([chunk["page"] for chunk in chunks], [1, 2])
        self.assertEqual([chunk["text"] for chunk in chunks], ["Text before figure.", "Text after figure."])

    def test_complex_stream_retry_remains_bounded_without_partial_index(self):
        """提高预算后仍失败时不写入部分原文，也不调用向量模型，保存可重试失败状态。"""
        from pypdf import apply_configuration, get_configuration
        path = self.pdf()
        with apply_configuration(stream_parsing_work_maximum_count=1), \
             patch.object(kb, "PDF_STREAM_RETRY_LIMIT", 2):
            result = self.build()
            self.assertEqual(get_configuration().stream_parsing_work_maximum_count, 1)
        self.assertEqual(result["failed"], 1)
        self.engine.embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            error = db.execute("SELECT error FROM documents WHERE path=?", (str(path.resolve()),)).fetchone()[0]
        self.assertIn("第 1 页提高上限后仍无法完整解析", error)

    def retrieve(self, question="waves mixing", **kwargs):
        """从真实临时数据库检索，问题向量仍使用模拟接口。"""
        return kb.retrieve(question, config=self.config, model_settings=self.api,
                           embedder=self.engine, **kwargs)

    def ask(self, **kwargs):
        """使用模拟回答模型完成端到端本地问答。"""
        return qa.ask_papers("waves mixing", config=self.config, embedding_settings=self.api,
                             embedder=self.engine, client=self.client, **kwargs)

    def test_real_pdf_pages_incremental_and_changed_content(self):
        """真实 PDF 按页索引，未变化时跳过，修改后重新计算向量并替换旧片段。"""
        path = self.pdf()
        self.assertEqual(self.build()["indexed"], 1)
        self.assertEqual(self.build()["skipped"], 1)
        self.assertEqual(self.engine.embed.call_count, 1)
        hits = self.retrieve()
        self.assertEqual({item["page"] for item in hits}, {1, 2})
        self.assertTrue(all(item["path"] == str(path.resolve()) for item in hits))
        self.pdf(pages=["New waves evidence."])
        self.assertEqual(self.build()["indexed"], 1)
        self.assertEqual(len(self.retrieve()), 1)

    def test_download_metadata_citations_and_original_excerpts(self):
        """下载 DOI 和标题进入引用，报告与 JSON 保留准确页序和原文摘录。"""
        self.pdf()
        (self.source / ".download-state.json").write_text(json.dumps({"doi": {
            "file": "waves.pdf", "title": "Wave evidence", "doi": "10.1234/test"}}), encoding="utf-8")
        self.build()
        result = self.ask()
        report = Path(result["report"])
        text = report.read_text(encoding="utf-8")
        self.assertIn("Wave evidence", text)
        self.assertIn("10.1234/test", text)
        self.assertIn("PDF 第", text)
        self.assertIn("Waves enhance mixing", text)
        data = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertTrue(data["evidence"])
        self.assertNotIn("api_key", report.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(data["answer"]["claims"][0]["sources"], ["S1"])

    def test_failed_embedding_not_committed_and_retry(self):
        """向量调用失败不保存半篇索引，后续重试可恢复。"""
        self.pdf()
        self.engine.embed.side_effect = RuntimeError("offline")
        self.assertEqual(self.build()["failed"], 1)
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        self.engine.embed.side_effect = self.vectors
        self.assertEqual(self.build()["indexed"], 1)

    def test_index_stops_on_empty_credits_preserving_documents(self):
        """建库在余额耗尽时停止，保留成功文献和当前失败记录，第三篇不再请求向量。"""
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.pdf(name)
        error = kb._common.APICreditsExhaustedError("API额度耗尽")
        self.engine.embed.side_effect = [[[1, 0, 0], [1, 0, 0]], error]
        with self.assertRaises(kb._common.APICreditsExhaustedError):
            self.build()
        self.assertEqual(self.engine.embed.call_count, 2)
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            rows = db.execute("SELECT path, status FROM documents ORDER BY path").fetchall()
        self.assertEqual([(Path(path).name, status) for path, status in rows],
                         [("a.pdf", "ready"), ("b.pdf", "failed")])

    def test_dry_run_no_database_or_api(self):
        """预览不读取 PDF 内容、不创建数据库、不调用接口。"""
        self.pdf()
        with patch.object(kb, "extract_pdf", side_effect=AssertionError("unexpected")):
            self.assertEqual(self.build(dry_run=True)["pending"], 1)
        self.assertFalse(self.config.database.exists())
        self.engine.embed.assert_not_called()

    def test_index_configuration_mismatch_before_api(self):
        """向量维度或模型变更后拒绝复用旧库，避免不可比较的向量混用。"""
        self.pdf()
        self.build()
        self.engine.reset_mock()
        with self.assertRaisesRegex(ValueError, "配置"):
            kb.build_knowledge_base(config=replace(self.config, embedding_dimensions=4),
                                    model_settings=self.api, embedder=self.engine)
        with self.assertRaisesRegex(ValueError, "配置"):
            kb.retrieve("waves", config=replace(self.config, embedding_dimensions=4),
                        model_settings=self.api, embedder=self.engine)
        self.engine.embed.assert_not_called()

    def test_missing_database_does_not_create_file(self):
        """误用不存在的知识库时明确报错且不生成空库。"""
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.retrieve()
        self.assertFalse(self.config.database.exists())

    def test_changed_or_deleted_pdf_excluded(self):
        """原文改变后不能继续用旧证据，删除文件并重新建库后清除索引。"""
        path = self.pdf()
        self.build()
        path.write_bytes(b"changed")
        self.assertEqual(self.retrieve(), [])
        path.unlink()
        self.build()
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_empty_text_pdf_requires_ocr(self):
        """扫描式空文本 PDF 不算建库成功，错误说明需要 OCR。"""
        self.pdf(pages=[" "])
        self.assertEqual(self.build()["failed"], 1)
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            self.assertIn("OCR", db.execute("SELECT error FROM documents").fetchone()[0])

    def test_limit_skips_finished_papers_without_consuming_slot(self):
        """已完成文献不占新增文献上限，便于多次分批建库。"""
        self.pdf("a.pdf")
        self.pdf("b.pdf")
        self.assertEqual(self.build(limit=1)["indexed"], 1)
        stats = self.build(limit=1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["indexed"], 1)

    def test_bad_citation_or_uncited_claim_rejected(self):
        """伪造引用和无引用回答不允许成为成功报告。"""
        self.pdf()
        self.build()
        for sources in (["S99"], []):
            self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
                "claims": [{"text": "unsupported", "sources": sources}], "limitations": ""}))
            with self.assertRaisesRegex(ValueError, "引用"):
                self.ask()
        self.assertFalse(self.config.answer_dir.exists())

    def test_answer_json_fences_supported_but_extra_text_and_bad_sources_rejected(self):
        """完整代码框内的 JSON 可解析，混入额外说明、多份对象和无效引用仍然拒绝。"""
        evidence = [{"source_id": "S1"}]
        valid = {"claims": [{"text": "波浪影响混合。", "sources": ["S1"]}], "limitations": ""}
        text = json.dumps(valid, ensure_ascii=False)
        for wrapped in (text, "\ufeff" + text, "```json\n" + text + "\n```", "```\n" + text + "\n```"):
            self.assertEqual(qa.validate_answer(wrapped, evidence), valid)
        for invalid in ("结果：" + text, text + "\n说明文字", text[:-2], text + text):
            with self.assertRaisesRegex(ValueError, "未返回有效 JSON"):
                qa.validate_answer(invalid, evidence)
        invalid = {"claims": [{"text": "无依据", "sources": ["S99"]}], "limitations": ""}
        with self.assertRaisesRegex(ValueError, "引用"):
            qa.validate_answer("```json\n" + json.dumps(invalid) + "\n```", evidence)

    def test_qa_uses_json_mode_and_accepts_fenced_answer_without_extra_call(self):
        """问答请求明确要求 JSON，带代码框的有效回答一次调用即可保存。"""
        self.pdf()
        self.build()
        answer = self.client.models.generate_content.return_value.text
        self.client.models.generate_content.return_value.text = "```json\n" + answer + "\n```"
        result = self.ask()
        self.assertEqual(result["failed"], 0)
        self.client.models.generate_content.assert_called_once()
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["config"],
                         {"response_mime_type": "application/json"})

    def test_top_two_distinct_full_pdfs_are_attached_and_page_citations_saved(self):
        """同篇多个命中不重复上传，最多两篇 PDF，并保存全文引用的真实页码链接。"""
        paths = [self.pdf(f"paper{i}.pdf") for i in range(3)]
        hits = [dict(path=str(paths[i]), title=f"Paper {i}", doi=f"10.1234/{i}", page=1,
                     ordinal=1, text="Ocean waves.", score=1) for i in (0, 0, 1, 2)]
        self.client.models.generate_content.return_value.text = json.dumps({
            "claims": [{"text": "全文中的结论。", "sources": ["P2:2"]}], "limitations": ""})
        with patch.object(qa, "retrieve", return_value=hits):
            result = self.ask()
        contents = self.client.models.generate_content.call_args.kwargs["contents"]
        attachments = [item["inline_data"]["data"] for item in contents if isinstance(item, dict)]
        self.assertEqual(attachments, [paths[0].read_bytes(), paths[1].read_bytes()])
        report = json.loads(Path(result["report"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["full_documents"]), 2)
        citation = next(item for item in report["evidence"] if item["source_id"] == "P2:2")
        self.assertEqual((citation["path"], citation["page"]), (str(paths[1]), 2))
        self.assertEqual(result["full_pdfs"], 2)

    def test_full_pdf_citations_reject_unknown_files_and_pages(self):
        """全文引用不能越页、使用不存在的文献或非法页码。"""
        documents = [dict(document_id="P1", pages=2)]
        for source in ("P1:0", "P1:3", "P2:1", "P1:-1"):
            text = json.dumps({"claims": [{"text": "Claim", "sources": [source]}], "limitations": ""})
            with self.assertRaisesRegex(ValueError, "引用"):
                qa.validate_answer(text, [], documents)

    def test_full_pdf_selection_can_retrieve_second_document_beyond_snippet_quota(self):
        """即使片段配额只有一条，全文问答仍可检索到第二篇不同文献。"""
        self.pdf("first.pdf")
        self.pdf("second.pdf")
        self.build()
        hits = kb.retrieve("waves", config=replace(self.config, top_k=1), model_settings=self.api,
                           embedder=self.engine, min_documents=2)
        self.assertEqual(len({item["path"] for item in hits}), 2)

    def test_openai_pdf_content_and_unsupported_provider(self):
        """GPT 使用原生 PDF 文件内容；未适配接口报错且不发送请求。"""
        path = self.pdf()
        document = qa.select_full_pdfs([dict(path=str(path), title="Paper", doi="10.1234/a")])[0]
        api = ModelSettings(provider="chat_completions", model="pdf-capable-test", base_url="https://api.openai.com/v1")
        session = Mock()
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": '{}'}}]}
        qa._api.generate_text("Question", api, session, json_mode=True, pdfs=[document])
        content = session.post.call_args.kwargs["json"]["messages"][0]["content"]
        import base64
        self.assertEqual(base64.b64decode(content[-1]["file"]["file_data"].split(",", 1)[1]), path.read_bytes())
        session.post.reset_mock()
        with self.assertRaisesRegex(ValueError, "尚未适配"):
            qa._api.generate_text("Question", replace(api, base_url="https://api.deepseek.com"), session, pdfs=[document])
        session.post.assert_not_called()

    def test_changed_pdf_not_uploaded(self):
        """选取后文件变化时不上传，也不让旧索引对应到新全文。"""
        path = self.pdf()
        document = qa.select_full_pdfs([dict(path=str(path), title="Paper", doi="")])[0]
        path.write_bytes(b"%PDF-changed")
        with self.assertRaisesRegex(ValueError, "已变化"):
            qa._api.generate_text("Question", self.api, self.client, pdfs=[document])
        self.client.models.generate_content.assert_not_called()

    def test_truncated_answer_not_saved_or_retried(self):
        """模型报告输出长度限制时显示明确原因，不保存残缺 JSON，也不自动重复付费调用。"""
        self.pdf()
        self.build()
        self.client.models.generate_content.return_value = SimpleNamespace(
            text='{"claims": [', candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])
        with self.assertRaisesRegex(ValueError, "输出达到长度上限"):
            self.ask()
        self.client.models.generate_content.assert_called_once()
        self.assertFalse(self.config.answer_dir.exists())

    def test_retrieve_only_no_answer_model(self):
        """仅检索仍计算问题向量，但不调用回答模型。"""
        self.pdf()
        self.build()
        self.assertGreater(self.ask(retrieve_only=True)["found"], 0)
        self.client.models.generate_content.assert_not_called()
        self.assertTrue(self.engine.embed.call_args.kwargs["query"])

    def test_no_hits_does_not_call_answer_model(self):
        """没有有效证据时直接说明不足，不调用回答模型猜测。"""
        path = self.pdf()
        self.build()
        path.unlink()
        self.assertEqual(self.ask()["found"], 0)
        self.client.models.generate_content.assert_not_called()

    def test_chunk_boundaries_no_loss_or_infinite_loop(self):
        """切分长度可控，长串和密集空格不会丢失文字或陷入不推进循环。"""
        for text in ("word. " * 200, "中" * 1000, "a" * 60 + " " * 100 + "b" * 200):
            parts = kb.split_page(text, 100, 90)
            self.assertTrue(all(0 < len(part) <= 100 for part in parts))
            for char in set(text) - {" "}:
                self.assertIn(char, "".join(parts))

    def test_paragraph_reflow_and_complete_sentence_overlap(self):
        """排版换行合并，正常段落不拆开，重叠片段从完整句子开始且数学符号不变。"""
        text = "The SI scheme\nuses ﬂux and x².\n\nA second paragraph remains intact."
        self.assertEqual(kb.normalize_page_text(text),
                         "The SI scheme uses flux and x².\n\nA second paragraph remains intact.")
        first = "Waves affect mixing."
        second = "Instability changes energy."
        third = "Turbulence transports heat."
        parts = kb.split_page(" ".join([first, second, third]), 60, 30)
        self.assertEqual(parts, [first + "\n\n" + second, second + "\n\n" + third])
        paragraphs = ["A complete paragraph about SI.", "Another complete paragraph about waves."]
        self.assertEqual(kb.split_page("\n\n".join(paragraphs), 50, 10), paragraphs)

    def test_repeated_doi_and_domain_footer_removed_but_body_preserved(self):
        """跨页重复的 DOI 和网站页脚移除；正文句子、小节编号与数学符号保留。"""
        pages = ["2 Symmetric instability\n\nSI changes x².\nJiang et al. 10.3389/fmars.2022.985605\nFrontiers frontiersin.org02",
                 "3 Results\n\nWaves change mixing.\nJiang et al. 10.3389/fmars.2022.985605\nFrontiers frontiersin.org03"]
        cleaned = kb.remove_repeated_marginal_text(pages)
        self.assertEqual(cleaned, ["2 Symmetric instability\n\nSI changes x².",
                                   "3 Results\n\nWaves change mixing."])

    def test_parser_upgrade_reindexes_in_place(self):
        """仅文本解析算法升级可沿用数据库重建，不能把旧片段误认为已更新。"""
        self.pdf()
        self.build()
        signature = kb.index_signature(self.source, self.config, self.api)
        signature["parser"] = "pypdf-page-text-v1"
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            with db:
                db.execute("UPDATE metadata SET value=? WHERE key='signature'", (json.dumps(signature),))
        self.assertEqual(self.build()["indexed"], 1)
        self.assertEqual(self.build()["skipped"], 1)

    def test_embedding_adapter_tasks_normalization_and_errors(self):
        """适配器区分文献和问题任务，逐条向量归一化并拒绝格式错误的响应。"""
        client = Mock()
        client.models.embed_content.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=[3, 4, 0])])
        engine = embedding.GeminiEmbedder(self.api, dimensions=3, client=client)
        self.assertEqual(engine.embed(["text"]), [[0.6, 0.8, 0]])
        self.assertEqual(client.models.embed_content.call_args.kwargs["config"]["task_type"], "RETRIEVAL_DOCUMENT")
        engine.embed(["question"], query=True)
        self.assertEqual(client.models.embed_content.call_args.kwargs["config"]["task_type"], "RETRIEVAL_QUERY")
        client.models.embed_content.return_value.embeddings = []
        with self.assertRaises(ValueError):
            engine.embed(["text"])
        engine.close()
        client.close.assert_not_called()
        for vector in ([0, 0, 0], [float("nan"), 1, 2], [1, 2]):
            with self.assertRaises(ValueError):
                embedding.normalize_vector(vector, 3)

    def test_hybrid_ranking_boosts_exact_terms(self):
        """在语义排名接近时，准确的专业术语匹配提升对应证据。"""
        rows = [{"text": "unrelated Python software", "vector": "[1,0,0]"},
                {"text": "Stokes drift enhances mixing", "vector": "[0.9,0.1,0]"}]
        ranked = kb.rank_chunks(rows, "Stokes drift", [1, 0, 0], self.config)
        self.assertIn("Stokes", ranked[0]["text"])

    def test_cli_arguments(self):
        """验证编号脚本所用建库和问答入口参数。"""
        self.assertEqual(assistant.build_parser().parse_args(["index", "--dry-run"]).command, "index")
        args = assistant.build_parser().parse_args(["ask", "--question", "测试", "--retrieve-only"])
        self.assertTrue(args.retrieve_only)
        self.assertEqual(args.question, "测试")

    def test_independent_default_question_and_ui_override(self):
        """默认提问使用独立配置，界面传入的问题优先，筛选问题不会被修改。"""
        original_screening = qa.user_settings.RESEARCH_SETTINGS.focus_question
        with patch.object(qa.user_settings, "QA_SETTINGS", QASettings(question="独立问答问题")):
            for question, expected in ((None, "独立问答问题"), ("界面输入的问题", "界面输入的问题")):
                with patch.object(qa, "retrieve", return_value=[]) as search:
                    result = qa.ask_papers(question, config=self.config)
                    self.assertEqual(search.call_args.args[0], expected)
                    data = json.loads(Path(result["report"]).with_suffix(".json").read_text(encoding="utf-8"))
                    self.assertEqual(data["question"], expected)
        self.assertEqual(qa.user_settings.RESEARCH_SETTINGS.focus_question, original_screening)

    def test_empty_question_does_not_fall_back_to_screening(self):
        """独立配置或界面问题为空时明确报错，不用筛选问题替代，也不触发检索。"""
        with patch.object(qa.user_settings, "QA_SETTINGS", QASettings(question="")):
            with patch.object(qa, "retrieve") as search:
                for question in (None, "", "   "):
                    with self.assertRaisesRegex(ValueError, "问题不能为空"):
                        qa.ask_papers(question, config=self.config)
                search.assert_not_called()

    def test_owned_clients_closed_on_failure(self):
        """建库和回答异常时仍关闭本模块创建的客户端连接。"""
        self.pdf()
        owned = Mock()
        owned.embed.side_effect = RuntimeError("failed")
        with patch.object(kb, "GeminiEmbedder", return_value=owned):
            self.assertEqual(kb.build_knowledge_base(config=self.config, model_settings=self.api)["failed"], 1)
        owned.close.assert_called_once()
        self.build()
        client = Mock()
        client.models.generate_content.side_effect = ValueError("failed")
        with patch.object(qa._api, "create_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "failed"):
                qa.ask_papers("waves", config=self.config, embedding_settings=self.api, embedder=self.engine)
        client.close.assert_called_once()

    def test_metadata_update_without_reembedding_and_no_secret_storage(self):
        """元数据更新无需重算向量；索引兼容配置不包含密钥。"""
        self.pdf()
        self.build()
        self.engine.reset_mock()
        (self.source / ".download-state.json").write_text(json.dumps({"doi": {
            "file": "waves.pdf", "title": "Updated title", "doi": "10.1234/updated"}}), encoding="utf-8")
        self.assertEqual(self.build()["skipped"], 1)
        self.engine.embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            title, doi = db.execute("SELECT title, doi FROM documents").fetchone()
            self.assertEqual((title, doi), ("Updated title", "10.1234/updated"))
        signature = kb.index_signature(self.source, self.config, replace(self.api, api_key="fake-private-key"))
        self.assertNotIn("fake-private-key", json.dumps(signature))

    def test_invalid_vectors_not_saved(self):
        """错误维度或无效数值不能进入知识库。"""
        self.pdf()
        for vectors in ([[1, 0]], [[float("nan"), 1, 0], [1, 0, 0]]):
            self.engine.embed.side_effect = None
            self.engine.embed.return_value = vectors
            self.assertEqual(self.build()["failed"], 1)
        with contextlib.closing(sqlite3.connect(self.config.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_empty_claims_are_valid_when_evidence_insufficient(self):
        """检索到文章但无法回答时可以明确说明不足，不强制模型编造结论。"""
        self.pdf()
        self.build()
        self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "claims": [], "limitations": "这些片段没有提供问题所需的触发条件。"}))
        report = Path(self.ask()["report"])
        self.assertIn("不足以支持回答", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
