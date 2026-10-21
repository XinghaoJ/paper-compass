"""摘要流程的离线验证：配置隔离、严格筛选、Scholar DOI 提取及模型接口适配。"""
import contextlib
import io
import json
import tempfile
import unittest
from importlib import import_module
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

abstract_read = import_module("1abstract_read")
assistant = import_module("0assistant")
model_api = import_module("1model_api")
load_json = import_module("99common").load_json
from user_settings import ModelSettings, ResearchSettings


class ScreeningTests(unittest.TestCase):
    """使用模拟模型、检索记录和 DOI 补查响应验证筛选流程，不发送真实网络请求。"""

    def setUp(self):
        """准备独立配置和模拟接口，不依赖本机密钥，也不发送真实网络请求。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = ResearchSettings(search_source="google_scholar", keywords="ocean instability", year_start=2020, year_end=2025,
                                         focus_question="波浪如何影响 SI？", output_file=self.root / "report.md")
        self.api = ModelSettings(model="test-model")
        self.client = Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(
            dict(relevant=True, reason="摘要中的波浪过程与关注问题相关")))
        self.doi_patch = patch.object(abstract_read, "extract_scholar_doi", wraps=abstract_read.extract_scholar_doi)
        self.match = self.doi_patch.start()
        self.lookup_patch = patch.object(abstract_read, "lookup_crossref_doi", return_value={
            "doi": "", "doi_status": "skipped_no_doi", "doi_source": "", "doi_source_value": "",
            "doi_lookup_status": "not_found", "doi_lookup_note": "未找到可靠 DOI"})
        self.lookup = self.lookup_patch.start()
        self.openalex_patch = patch.object(abstract_read._openalex, "lookup_openalex_doi", return_value={
            "doi": "", "doi_status": "skipped_no_doi"})
        self.openalex_patch.start()
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()

    def tearDown(self):
        """恢复模拟函数和终端输出，清理每次测试创建的临时文件。"""
        self.capture.__exit__(None, None, None)
        self.doi_patch.stop()
        self.lookup_patch.stop()
        self.openalex_patch.stop()
        self.temporary.cleanup()

    def publication(self, title="Ocean instability", year="2024"):
        """构造一条含标题、年份和摘要的 Scholar 搜索结果。"""
        return dict(bib=dict(title=title, pub_year=year, author=["Author"], abstract="abstract " * 20,
                             doi="10.1234/example"),
                    pub_url="https://example.org/paper")

    def run_review(self, publications=None, **kwargs):
        """用独立配置执行流程，默认只筛选一条模拟文献。"""
        return abstract_read.auto_literature_review(
            settings=self.settings, model_settings=self.api, client=self.client,
            publications=[self.publication()] if publications is None else publications, **kwargs)

    def test_scholar_failure_on_first_request_and_next_page(self):
        """首次请求和分页失败均显示中文原因，其他程序异常不被误报为访问限制。"""
        class FetchError(Exception):
            """模拟 scholarly 重试耗尽异常。"""
        fake = SimpleNamespace(scholarly=Mock(), MaxTriesExceededException=FetchError,
                               DOSException=FetchError)
        with patch.dict("sys.modules", {"scholarly": fake}):
            fake.scholarly.search_pubs.side_effect = FetchError("Cannot Fetch from Google Scholar.")
            with self.assertRaisesRegex(RuntimeError, "Google Scholar 检索受限或连接失败"):
                list(abstract_read.search_publications(self.settings))

            def interrupted_results():
                """模拟已返回一篇结果后下一页抓取失败。"""
                yield self.publication()
                raise FetchError("Cannot Fetch from Google Scholar.")

            fake.scholarly.search_pubs.side_effect = None
            fake.scholarly.search_pubs.return_value = interrupted_results()
            with self.assertRaisesRegex(RuntimeError, "已完成并保存"):
                self.run_review(publications=abstract_read.search_publications(self.settings))
            self.assertEqual(len(load_json(self.root / "report.papers.json")["papers"]), 1)
            fake.scholarly.search_pubs.side_effect = ValueError("unexpected parser error")
            with self.assertRaisesRegex(ValueError, "unexpected parser error"):
                list(abstract_read.search_publications(self.settings))

    def test_end_to_end_exports_and_secrets_not_saved(self):
        """验证合格文献输出报告、JSON、CSV 和 DOI 清单，且状态和导出文件不包含密钥。"""
        self.api = replace(self.api, api_key="private-test-key")
        stats = self.run_review()
        self.assertEqual((stats["relevant"], stats["doi_found"], stats["failed"]), (1, 1, 0))
        data = load_json(self.root / "report.papers.json")
        self.assertEqual(data["focus_question"], self.settings.focus_question)
        self.assertEqual(data["papers"][0]["doi"], "10.1234/example")
        self.assertEqual((self.root / "report.dois.txt").read_text().strip(), "10.1234/example")
        self.assertTrue((self.root / "report.papers.csv").exists())
        for path in self.root.iterdir():
            self.assertNotIn("private-test-key", path.read_text(encoding="utf-8-sig"))
        self.assertNotIn("private-test-key", repr(self.api))

    def test_year_filter_runs_before_ai_and_doi(self):
        """验证超出年份范围或缺少年份的文献不会发送给 AI，也不会查询 DOI。"""
        stats = self.run_review([self.publication(year="2019"), self.publication(year="")])
        self.assertEqual(stats["relevant"], 0)
        self.client.models.generate_content.assert_not_called()
        self.match.assert_not_called()
        self.lookup.assert_not_called()

    def test_unrelated_papers_never_query_doi(self):
        """验证模型判定无关时保留筛选记录，但不生成合格条目、不查询 DOI。"""
        self.client.models.generate_content.return_value.text = json.dumps(
            dict(relevant=False, reason="虽然出现相同关键词，但研究内容与关注问题无关"))
        self.assertEqual(self.run_review()["relevant"], 0)
        self.match.assert_not_called()
        self.lookup.assert_not_called()
        self.assertEqual(load_json(self.root / "report.papers.json")["papers"], [])

    def test_malformed_response_is_failed_instead_of_accepted(self):
        """验证非法 JSON、字符串形式的布尔值及缺少理由的回答都不会被误收录。"""
        for text in ["不知道", '{"relevant":"false","reason":"原因"}',
                     '{"relevant":true,"reason":""}']:
            with self.subTest(text=text):
                self.client.models.generate_content.return_value.text = text
                self.assertEqual(self.run_review()["screening_failed"], 1)
        self.match.assert_not_called()

    def test_relevance_only_response_is_accepted_and_doi_is_from_scholar(self):
        """验证无需回答问题或生成总结即可收录相关文章，并只使用 Scholar 提供的 DOI。"""
        self.client.models.generate_content.return_value.text = json.dumps(dict(
            relevant=True, reason="涉及相关机制，但摘要未给出该问题的直接答案，需要后续阅读全文",
            doi="10.9999/model-invented"))
        self.assertEqual(self.run_review()["doi_found"], 1)
        record = load_json(self.root / "report.papers.json")["papers"][0]
        self.assertEqual(record["doi"], "10.1234/example")
        self.assertEqual(record["abstract"], self.publication()["bib"]["abstract"].strip())
        self.assertNotIn("summary", record)

    def test_screening_rule_change_does_not_reuse_old_decisions(self):
        """验证提示词改变后不静默复用旧判断，避免较严格的旧规则继续排除相关文献。"""
        first = self.run_review()
        with patch.object(abstract_read, "SCREENING_PROMPT", abstract_read.SCREENING_PROMPT + "规则已调整"):
            updated = self.run_review()
        self.assertNotEqual(updated["report"], first["report"])
        self.assertEqual(updated["skipped"], 0)
        self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_missing_doi_unresolved_is_skipped(self):
        """验证补查仍未获得可靠 DOI 时跳过下载导出，保留问题筛选结果。"""
        pub = self.publication()
        del pub["bib"]["doi"]
        stats = self.run_review([pub])
        self.assertEqual((stats["relevant"], stats["doi_found"], stats["doi_skipped"], stats["failed"]), (1, 0, 1, 0))
        self.assertEqual(load_json(self.root / "report.papers.json")["papers"], [])
        self.assertEqual((self.root / "report.dois.txt").read_text(), "")
        record = next(iter(load_json(self.root / "report.md.state.json")["records"].values()))
        self.assertEqual(record["doi_status"], "skipped_no_doi")
        self.lookup.assert_called_once()

    def test_current_and_cumulative_counts_have_separate_scopes(self):
        """缩小检索上限后，本次只统计所见文章，历史记录单列，复用记录不重复累计。"""
        first = self.publication(title="First relevant paper")
        missing_doi = self.publication(title="Second relevant paper")
        del missing_doi["bib"]["doi"]
        unrelated = self.publication(title="Unrelated paper")
        self.client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(dict(relevant=related, reason="筛选理由")))
            for related in (True, True, False)]
        initial = self.run_review([first, missing_doi, unrelated])
        self.assertEqual(initial["cumulative"], dict(inspected=3, relevant=2, doi_found=1, doi_skipped=1))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            stats = self.run_review([first], max_papers=1)
        self.assertEqual(stats["current"], dict(inspected=1, relevant=1, doi_found=1, doi_skipped=0))
        self.assertEqual(stats["cumulative"], initial["cumulative"])
        self.assertEqual((stats["relevant"], stats["doi_found"], stats["doi_skipped"]), (1, 1, 0))
        self.assertEqual(self.client.models.generate_content.call_count, 3)
        self.assertIn("本次统计：", output.getvalue())
        self.assertIn("累计结果：", output.getvalue())
        self.assertNotIn("screening_failed", output.getvalue())
        empty = self.run_review([], max_papers=1)
        self.assertEqual(empty["current"], dict(inspected=0, relevant=0, doi_found=0, doi_skipped=0))
        self.assertEqual(empty["cumulative"], initial["cumulative"])

    def test_crossref_doi_exported_and_reused_without_rescreening(self):
        """Scholar 缺失 DOI 时导出补查结果，重跑不重复筛选或请求已核验 DOI。"""
        pub = self.publication()
        del pub["bib"]["doi"]
        self.lookup.return_value = dict(doi="10.1234/crossref", doi_status="found", doi_source="Crossref",
                                        doi_source_value="https://api.crossref.org/works/10.1234%2Fcrossref",
                                        doi_match_evidence={"rule": "verified"})
        self.assertEqual(self.run_review([pub])["doi_found"], 1)
        self.assertEqual(self.run_review([pub])["doi_found"], 1)
        self.lookup.assert_called_once()
        self.client.models.generate_content.assert_called_once()
        paper = load_json(self.root / "report.papers.json")["papers"][0]
        self.assertEqual((paper["doi"], paper["doi_source"]), ("10.1234/crossref", "Crossref"))

    def test_previous_no_doi_can_be_backfilled_without_model_call(self):
        """已相关但缺 DOI 的记录下次可补查成功，不再调用摘要模型。"""
        pub = self.publication()
        del pub["bib"]["doi"]
        self.run_review([pub])
        self.lookup.return_value = dict(doi="10.1234/later", doi_status="found", doi_source="Crossref",
                                        doi_source_value="https://api.crossref.org/works/10.1234%2Flater")
        self.assertEqual(self.run_review([pub])["doi_found"], 1)
        self.client.models.generate_content.assert_called_once()

    def test_new_scholar_metadata_supplies_doi_without_rescreening(self):
        """验证后续 Scholar 条目补充了 DOI 时可直接更新清单，不重复调用 AI。"""
        pub = self.publication()
        del pub["bib"]["doi"]
        self.run_review([pub])
        self.assertEqual(self.run_review()["doi_found"], 1)
        self.client.models.generate_content.assert_called_once()

    def test_direct_doi_fields_and_article_links(self):
        """验证明确 DOI 字段、DOI 解析链接和出版商文章链接可提取，并保存来源。"""
        entries = [dict(doi="10.1234/ABC"), dict(bib=dict(DOI="doi:10.1234/ABC")),
                   dict(pub_url="https://doi.org/10.1234/ABC"),
                   dict(pub_url="https://publisher.example/doi/abs/10.1234/ABC"),
                   dict(eprint_url="https://publisher.example/article/10.1234%2FABC")]
        for entry in entries:
            with self.subTest(entry=entry):
                result = abstract_read.extract_scholar_doi(entry)
                self.assertEqual(result["doi"], "10.1234/ABC")
                self.assertTrue(result["doi_source"].startswith("Scholar."))

    def test_cited_invalid_and_conflicting_dois_are_skipped(self):
        """验证不从摘要引文、模糊链接或非法字段猜 DOI，多个不同 DOI 时保守跳过。"""
        for entry in [dict(bib=dict(abstract="cites doi:10.1234/reference")),
                      dict(doi="not-a-doi"), dict(pub_url="https://example.org/refs/10.1234/reference")]:
            self.assertEqual(abstract_read.extract_scholar_doi(entry)["doi_status"], "skipped_no_doi")
        conflicting = abstract_read.extract_scholar_doi(dict(doi="10.1234/a", pub_url="https://doi.org/10.1234/b"))
        self.assertEqual(conflicting["doi_status"], "skipped_ambiguous_doi")
        self.assertEqual(conflicting["doi"], "")

    def test_duplicate_search_results_are_not_processed_twice(self):
        """验证同一批搜索中的重复条目只筛选、匹配一次。"""
        stats = self.run_review([self.publication(), self.publication()])
        self.assertEqual(stats["skipped"], 1)
        self.client.models.generate_content.assert_called_once()
        self.match.assert_called_once()

    def test_missing_abstract_is_not_evaluated(self):
        """验证摘要不足时记录原因，不将仅有标题的条目当作完整摘要交给 AI。"""
        pub = self.publication()
        pub["bib"]["abstract"] = ""
        self.run_review([pub])
        self.client.models.generate_content.assert_not_called()
        self.match.assert_not_called()

    def test_search_uses_configured_keywords_and_years(self):
        """验证网页检索使用配置中的关键词和年份，模型收到的是用户关注的问题。"""
        with patch.object(abstract_read, "search_publications", return_value=[self.publication()]) as search:
            abstract_read.auto_literature_review(settings=self.settings, model_settings=self.api,
                                                 client=self.client)
        search.assert_called_once_with(self.settings)
        contents = self.client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(self.settings.focus_question, contents)
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"], "test-model")

    def test_crossref_abstract_retries_legacy_missing_record_then_caches_success(self):
        """旧版缺摘要记录重新补查，同标题补齐后进入 AI 筛选，并在再次运行时复用结果。"""
        pub = self.publication()
        pub["bib"]["abstract"] = ""
        self.run_review([pub])
        pub["source"] = "openalex"
        engine = Mock()
        engine.lookup.return_value = dict(abstract="Recovered abstract about waves and ocean instability.",
            abstract_lookup_status="found", abstract_lookup_version=abstract_read._crossref_abstract.RULE_VERSION,
            abstract_source="https://publisher.example/paper", abstract_lookup_note="已补齐")
        with patch.object(abstract_read._crossref_abstract, "CrossrefAbstractLookup", return_value=engine) as factory:
            self.assertEqual(self.run_review([pub])["relevant"], 1)
            self.assertEqual(self.run_review([pub])["relevant"], 1)
        engine.lookup.assert_called_once_with(pub["bib"]["title"], doi="")
        engine.close.assert_called_once()
        factory.assert_called_once()
        self.client.models.generate_content.assert_called_once()
        records = load_json(self.settings.report_path.with_suffix(".md.state.json"))["records"]
        self.assertEqual(next(iter(records.values()))["abstract_source"], "https://publisher.example/paper")

    def test_crossref_abstract_not_found_skips_without_model_and_is_cached(self):
        """Crossref 没找到摘要则放弃，明确记录原因，不调用 AI；已完成的失败搜索不重复请求。"""
        pub = self.publication()
        pub.update(source="openalex")
        pub["bib"]["abstract"] = ""
        engine = Mock()
        engine.lookup.return_value = dict(abstract="", abstract_lookup_status="not_found",
            abstract_lookup_version=abstract_read._crossref_abstract.RULE_VERSION, abstract_lookup_note="Crossref 未提供足够摘要")
        with patch.object(abstract_read._crossref_abstract, "CrossrefAbstractLookup", return_value=engine):
            self.assertEqual(self.run_review([pub])["relevant"], 0)
            self.run_review([pub])
        engine.lookup.assert_called_once()
        self.client.models.generate_content.assert_not_called()

    def test_separate_users_and_cli_overrides(self):
        """验证客户端配置互不修改，命令行关注问题覆盖选项保留旧 --topic 别名。"""
        other = replace(self.settings, focus_question="另一个问题", output_file=self.root / "other.md")
        abstract_read.auto_literature_review(settings=other, model_settings=self.api,
                                             client=self.client, publications=[self.publication()])
        self.assertEqual(self.settings.focus_question, "波浪如何影响 SI？")
        self.assertEqual(assistant.build_parser().parse_args(["search", "--topic", "问题"]).question, "问题")
        with patch.object(abstract_read, "auto_literature_review", return_value={}) as run:
            self.assertEqual(assistant.main(["search"]), 0)
        self.assertTrue(all(x is None for x in run.call_args.args))

    def test_invalid_config_fails_before_requests(self):
        """验证错误年份、空问题或不支持的接口在开始网络操作前报错。"""
        for config in [replace(self.settings, year_start=2026), replace(self.settings, focus_question=" ")]:
            with self.assertRaises(ValueError):
                abstract_read.auto_literature_review(settings=config, client=self.client, publications=[])
        with self.assertRaises(ValueError):
            replace(self.api, provider="unknown").validate()
        self.client.models.generate_content.assert_not_called()

    def test_chat_completions_interface(self):
        """验证兼容接口使用用户指定的地址、模型和超时发送消息，并正确提取回答。"""
        api = ModelSettings(provider="chat_completions", model="custom-model", base_url="https://api.example/v1")
        session = Mock()
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": " result "}}]}
        self.assertEqual(model_api.generate_text("prompt", api, session), "result")
        args = session.post.call_args
        self.assertEqual(args.args[0], "https://api.example/v1/chat/completions")
        self.assertEqual(args.kwargs["json"]["model"], "custom-model")

    def test_changed_conditions_keep_old_files_resume_separately_and_download_current_dois(self):
        """更换关键词自动重开独立计数，旧文件不变，下载读取新 DOI，改回条件仍可续接。"""
        first = self.run_review()
        old_report = Path(first["report"])
        original_files = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        changed = replace(self.settings, keywords="different keywords")
        publication = self.publication(title="Another paper")
        publication["bib"]["doi"] = "10.1234/another"
        kwargs = {"settings": changed, "model_settings": self.api, "client": self.client, "publications": [publication]}
        new = abstract_read.auto_literature_review(**kwargs)
        self.assertNotEqual(new["report"], str(old_report))
        self.assertEqual((new["inspected"], new["relevant"], new["cumulative"]["inspected"]), (1, 1, 1))
        for path, content in original_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(Path(new["report"]).with_suffix(".dois.txt").read_text().strip(), "10.1234/another")
        call_count = self.client.models.generate_content.call_count
        resumed = abstract_read.auto_literature_review(**kwargs)
        self.assertEqual(resumed["report"], new["report"])
        self.assertEqual(resumed["skipped"], 1)
        self.assertEqual(self.client.models.generate_content.call_count, call_count)
        download = import_module("2download_papers")
        with patch.object(download.user_settings, "RESEARCH_SETTINGS", changed), \
             patch.object(download.user_settings, "MODEL_SETTINGS", self.api), \
             patch.object(download, "read_download_list", wraps=download.read_download_list) as read:
            result = download.download_papers(output_dir=self.root / "pdf", dry_run=True)
            read.assert_called_once_with(Path(new["report"]).with_suffix(".papers.json"))
            self.assertEqual(result["found"], 1)
        restored = self.run_review()
        self.assertEqual(restored["report"], first["report"])
        self.assertEqual(restored["skipped"], 1)

    def test_condition_identity_includes_focus_years_and_model_but_not_limit_or_key(self):
        """问题、年份或筛选模型改变时隔离；扩大检索上限及更换密钥继续原进度。"""
        first = self.run_review()
        base = Path(first["report"])
        self.assertEqual(abstract_read.screening_report_path(replace(self.settings, max_papers=200), self.api), base)
        self.assertEqual(abstract_read.screening_report_path(self.settings, replace(self.api, api_key="new-key")), base)
        paths = {
            abstract_read.screening_report_path(replace(self.settings, focus_question="new focus"), self.api),
            abstract_read.screening_report_path(replace(self.settings, year_start=2021), self.api),
            abstract_read.screening_report_path(self.settings, replace(self.api, model="another-model")),
        }
        self.assertEqual(len(paths), 3)
        self.assertNotIn(base, paths)

    def test_chat_json_mode_and_truncation(self):
        """兼容接口请求 JSON 对象，输出截断时明确报错并停止，不进行格式重试。"""
        api = ModelSettings(provider="chat_completions", model="custom-model", base_url="https://api.example/v1")
        session = Mock()
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": '{}'}, "finish_reason": "stop"}]}
        self.assertEqual(model_api.generate_text("Return JSON", api, session, json_mode=True), '{}')
        self.assertEqual(session.post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        session.post.reset_mock()
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": '{'}, "finish_reason": "length"}]}
        with self.assertRaisesRegex(ValueError, "输出达到长度上限"):
            model_api.generate_text("Return JSON", api, session, json_mode=True)
        session.post.assert_called_once()

    def test_custom_key_env_and_direct_key(self):
        """验证不同用户可选不同密钥变量或内存密钥，创建 HTTP 会话时正确设置鉴权。"""
        api = ModelSettings(provider="chat_completions", base_url="https://api.example/v1", api_key_env="CUSTOM_KEY")
        session = Mock(headers={})
        fake_requests = SimpleNamespace(Session=Mock(return_value=session))
        with patch.dict("sys.modules", {"requests": fake_requests}):
            with patch.dict("os.environ", {"CUSTOM_KEY": "test-token"}, clear=True):
                self.assertIs(model_api.create_client(api), session)
                self.assertEqual(session.headers["Authorization"], "Bearer test-token")
            with patch.dict("os.environ", {}, clear=True):
                model_api.create_client(replace(api, api_key="in-memory-key"))
                self.assertEqual(session.headers["Authorization"], "Bearer in-memory-key")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "CUSTOM_KEY"):
                model_api.create_client(api)


if __name__ == "__main__":
    unittest.main()
