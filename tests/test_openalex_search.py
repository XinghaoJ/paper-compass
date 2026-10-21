"""OpenAlex 离线测试：摘要还原、分页、请求失败、来源隔离与下载清单衔接。"""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock, patch

from user_settings import ResearchSettings, ModelSettings

search = import_module("1openalex_search")
screening = import_module("1abstract_read")
download = import_module("2download_papers")


class OpenAlexTests(unittest.TestCase):
    """只使用模拟 HTTP 与模型，不消耗在线调用额度。"""

    def work(self):
        """准备一篇含倒排摘要与官方 DOI 的文献。"""
        return {"id": "https://openalex.org/W1", "title": "Ocean waves and instability",
                "doi": "https://doi.org/10.1234/example", "publication_year": 2024,
                "authorships": [{"author": {"display_name": "Author A"}}],
                "primary_location": None,
                "abstract_inverted_index": {"Ocean": [0], "waves": [1, 4], "drive": [2], "instability": [3]}}

    def test_fallback_preserves_results_deduplicates_and_limits(self):
        """分页故障后切换一次，重复候选不占最终名额，真实来源与禁用重复请求标记被保留。"""
        settings = ResearchSettings(max_papers=2)
        self.assertEqual(settings.search_source, "openalex")
        first = search.publication_record(self.work())
        other = search.publication_record(dict(self.work(), title="Another paper"))

        def partial():
            """返回一篇结果后模拟接口限流。"""
            yield first
            raise search.OpenAlexSearchError("HTTP 429")

        with patch.object(search, "search_openalex", return_value=partial()), \
             patch.object(screening, "search_scholar", return_value=iter([first, other])) as scholar:
            results = list(screening.search_publications(settings))
        scholar.assert_called_once()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "openalex")
        self.assertEqual(results[1]["source"], "google_scholar")
        self.assertTrue(results[1]["openalex_unavailable"])

    def test_fallback_only_on_service_errors_and_combines_failures(self):
        """正常空结果和程序错误不切换，主备均失败时同时报告两者原因。"""
        with patch.object(search, "search_openalex", return_value=iter([])), \
             patch.object(screening, "search_scholar") as scholar:
            self.assertEqual(list(screening.search_publications(ResearchSettings())), [])
            scholar.assert_not_called()
        with patch.object(search, "search_openalex", side_effect=search.OpenAlexSearchError("HTTP 429")), \
             patch.object(screening, "search_scholar", side_effect=RuntimeError("Scholar blocked")) as scholar:
            with self.assertRaisesRegex(RuntimeError, "均检索失败.*429.*Scholar blocked"):
                list(screening.search_publications(ResearchSettings()))
            scholar.assert_called_once()
        with patch.object(search, "search_openalex", side_effect=TypeError("bug")), \
             patch.object(screening, "search_scholar") as scholar:
            with self.assertRaises(TypeError):
                list(screening.search_publications(ResearchSettings()))
            scholar.assert_not_called()

    def test_doi_lookup_requires_unique_exact_title(self):
        """OpenAlex 补查只接收唯一完整同标题 DOI，不采用类似标题或歧义候选。"""
        session = Mock()
        record = {"title": "Ocean waves and instability"}
        item = self.work()
        for items, expected in [([item], "found"), ([dict(item, title="Other paper")], "skipped_no_doi"),
                                ([item, dict(item, doi="https://doi.org/10.1234/other")], "skipped_ambiguous_doi")]:
            session.get.return_value = Mock(status_code=200, json=Mock(return_value={"results": items}))
            result = search.lookup_openalex_doi(record, session=session)
            self.assertEqual(result["doi_status"], expected)

    def test_doi_priority_and_unavailable_service_skipped(self):
        """OpenAlex 补查优先于 Scholar DOI；已知服务故障时采用条目 DOI，不重复请求。"""
        publication = dict(search.publication_record(self.work()), source="google_scholar")
        preferred = {"doi": "10.1234/preferred", "doi_status": "found", "doi_source": "OpenAlex.lookup"}
        with patch.object(search, "lookup_openalex_doi", return_value=preferred) as lookup, \
             patch.object(screening, "lookup_crossref_doi") as crossref:
            record = {"title": self.work()["title"]}
            screening.resolve_doi(record, publication)
            self.assertEqual(record["doi"], "10.1234/preferred")
            screening.resolve_doi(record, publication)
            lookup.assert_called_once()
            crossref.assert_not_called()
        with patch.object(search, "lookup_openalex_doi") as lookup:
            record = {"title": self.work()["title"]}
            screening.resolve_doi(record, dict(publication, openalex_unavailable=True))
            lookup.assert_not_called()
            self.assertEqual(record["doi"], "10.1234/example")

    def test_openalex_missing_doi_uses_crossref(self):
        """OpenAlex 条目缺失 DOI 时继续 Crossref，同标题歧义则保守保留。"""
        publication = search.publication_record(dict(self.work(), doi=None))
        found = {"doi": "10.1234/crossref", "doi_source": "Crossref", "doi_status": "found"}
        with patch.object(screening, "lookup_crossref_doi", return_value=found) as crossref:
            record = {"title": self.work()["title"]}
            screening.resolve_doi(record, publication)
            crossref.assert_called_once()
            self.assertEqual(record["doi"], found["doi"])

    def test_abstract_and_metadata(self):
        """按位置而非字典顺序还原摘要，缺失与损坏摘要不生成虚构文本。"""
        record = search.publication_record(self.work())
        self.assertEqual(record["bib"]["abstract"], "Ocean waves drive instability waves")
        self.assertEqual(record["bib"]["author"], ["Author A"])
        self.assertEqual(record["source"], "openalex")
        for index in (None, {}, {"gap": [2]}, {"bad": [-1]}, {"a": [0], "b": [0]}):
            self.assertEqual(search.restore_abstract(index), "")

    def test_pagination_limit_years_and_key(self):
        """分页大小保持一致，最后一页只返回剩余数量；密钥不进入查询参数。"""
        session = Mock()
        session.get.side_effect = [Mock(status_code=200, json=Mock(return_value={"results": [self.work()] * 100})) for _ in range(2)]
        settings = ResearchSettings(search_source="openalex", max_papers=101, year_start=2020, year_end=2025)
        with patch.dict(os.environ, {"OPENALEX_API_KEY": "test-secret"}):
            records = list(search.search_openalex(settings, session=session))
        self.assertEqual(len(records), 101)
        self.assertEqual(session.get.call_count, 2)
        for page, call in enumerate(session.get.call_args_list, 1):
            self.assertEqual(call.kwargs["params"]["page"], page)
            self.assertEqual(call.kwargs["params"]["per_page"], 100)
            self.assertEqual(call.kwargs["params"]["filter"], "publication_year:2020-2025")
            self.assertNotIn("test-secret", str(call.kwargs["params"]))
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-secret")

    def test_empty_and_errors_do_not_retry(self):
        """空列表正常结束，限流及异常响应直接停止，避免重复请求。"""
        settings = ResearchSettings(search_source="openalex", max_papers=10)
        for code, body, error in [(200, {"results": []}, None), (429, {}, "额度"),
                                  (401, {}, "拒绝访问"), (500, {}, "HTTP 500"),
                                  (200, {"wrong": []}, "格式异常")]:
            session = Mock()
            session.get.return_value = Mock(status_code=code, json=Mock(return_value=body))
            if error:
                with self.assertRaisesRegex(RuntimeError, error):
                    list(search.search_openalex(settings, session=session))
            else:
                self.assertEqual(list(search.search_openalex(settings, session=session)), [])
            session.get.assert_called_once()

    def test_source_isolation_and_default_download_list(self):
        """OpenAlex 筛选不改 Scholar 文件，报告标注来源，下载自动读取新来源的清单。"""
        with tempfile.TemporaryDirectory() as root:
            original = Path(root) / "report.md"
            original.write_text("existing Scholar report", encoding="utf-8")
            settings = ResearchSettings(search_source="openalex", output_file=original, max_papers=10)
            with patch.object(screening, "evaluate_and_summarize", return_value={"relevant": True, "reason": "相关"}), \
                 patch.object(screening, "lookup_crossref_doi") as crossref:
                stats = screening.auto_literature_review(settings=settings, model_settings=ModelSettings(),
                    publications=[search.publication_record(self.work())], client=Mock())
            crossref.assert_not_called()
            self.assertEqual(stats["doi_found"], 1)
            self.assertEqual(original.read_text(encoding="utf-8"), "existing Scholar report")
            self.assertIn("检索网站：OpenAlex", settings.report_path.read_text(encoding="utf-8"))
            data = json.loads(settings.report_path.with_suffix(".papers.json").read_text(encoding="utf-8"))
            self.assertEqual(data["papers"][0]["doi_source"], "OpenAlex.doi")
            with patch.object(download.user_settings, "RESEARCH_SETTINGS", settings):
                result = download.download_papers(output_dir=Path(root) / "pdf", dry_run=True)
            self.assertEqual(result["found"], 1)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(replace(settings, output_file=settings.report_path).report_path, settings.report_path)
