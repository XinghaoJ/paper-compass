"""DOI 补查离线测试：模拟 Crossref 元数据，防止同名、近似标题或版本歧义导致误配。"""
import copy
import unittest
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch

from user_settings import DOILookupSettings

lookup = import_module("1doi_lookup")
screening = import_module("1abstract_read")


class DOILookupTests(unittest.TestCase):
    """验证 DOI 只能来自经过书目核验的唯一候选，所有请求均为模拟。"""

    def setUp(self):
        """准备完整文章身份和 Crossref 候选，注入模拟 HTTP 连接。"""
        self.config = DOILookupSettings()
        self.record = dict(title="Observations of high-frequency internal waves near submesoscale fronts",
                           authors=["S. M. Taylor", "J. Smith"], year=2024)
        self.item = {"DOI": "10.1234/verified", "type": "journal-article", "title": [self.record["title"]],
                     "author": [{"given": "S. M.", "family": "Taylor"}, {"family": "Smith"}],
                     "published-print": {"date-parts": [[2025, 1, 1]]}}
        self.session = Mock()
        self.response = self.session.get.return_value
        self.response.status_code = 200

    def resolve(self, items=None, record=None, config=None):
        """在模拟接口上运行补查，默认返回一条可验证候选。"""
        self.response.json.return_value = {"message": {"items": [self.item] if items is None else items}}
        return lookup.lookup_crossref_doi(record or self.record, config or self.config, self.session)

    def test_unique_verified_candidate_and_audit_evidence(self):
        """完整标题一致时接受唯一 DOI，并保存标题匹配证据与来源。"""
        result = self.resolve()
        self.assertEqual((result["doi"], result["doi_source"]), ("10.1234/verified", "Crossref"))
        self.assertEqual(result["doi_match_evidence"]["title"], self.record["title"])
        self.assertNotIn("first_author", result["doi_match_evidence"])
        self.assertNotIn("years", result["doi_match_evidence"])
        request = self.session.get.call_args
        self.assertEqual(request.args[0], "https://api.crossref.org/works")
        self.assertEqual(request.kwargs["params"]["query.title"], self.record["title"])
        self.assertNotIn("filter", request.kwargs["params"])
        self.assertNotIn("Authorization", request.kwargs["headers"])
        self.assertFalse(request.kwargs["allow_redirects"])
        self.session.close.assert_not_called()

    def test_typography_normalization_and_subtitle(self):
        """标题标点、HTML 和重音差异可规范化，但不能省略或替换实质词语。"""
        self.item["title"] = ["Observations of <i>high‐frequency</i> internal waves"]
        self.item["subtitle"] = ["Near submesoscale fronts"]
        self.assertEqual(self.resolve()["doi"], "10.1234/verified")
        self.assertEqual(lookup.normalize_text("García"), lookup.normalize_text("Garcia"))

    def test_similar_or_missing_title_rejected(self):
        """标题仅相似或缺失时不能自动采用搜索第一条 DOI。"""
        variants = []
        wrong_title = copy.deepcopy(self.item)
        wrong_title["title"] = [self.record["title"] + " in a different ocean"]
        variants.append(wrong_title)
        for key in ("title",):
            item = copy.deepcopy(self.item)
            item.pop(key)
            variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                result = self.resolve([item])
                self.assertFalse(result["doi"])
                self.assertEqual(result["doi_lookup_status"], "not_found")

    def test_author_year_missing_or_different_do_not_block_match(self):
        """源记录和 Crossref 的作者、年份缺失或相互矛盾时，标题一致仍可补查成功。"""
        for record in ({"title": self.record["title"]}, dict(self.record, authors=[], year="未提供"),
                       dict(self.record, authors=["Wrong Author"], year=1900)):
            with self.subTest(record=record):
                minimal = {"title": [self.record["title"]], "DOI": "10.1234/title-only"}
                self.assertEqual(self.resolve([minimal], record=record)["doi"], "10.1234/title-only")
                self.assertEqual(self.resolve(record=record)["doi"], "10.1234/verified")

    def test_short_title_and_cache_independent_of_author_year(self):
        """短标题也按一致性匹配，作者和年份变化不改变 DOI 补查缓存标识。"""
        record = {"title": "Ocean waves"}
        item = {"title": ["Ocean waves"], "DOI": "10.1234/short"}
        self.assertEqual(self.resolve([item], record=record)["doi"], "10.1234/short")
        self.assertEqual(lookup.lookup_signature(record), lookup.lookup_signature(dict(record, authors=[], year="wrong")))

    def test_multiple_verified_dois_are_ambiguous(self):
        """预印本与期刊版等多个 DOI 都符合时不能自动挑选第一条。"""
        other = copy.deepcopy(self.item)
        other.update(DOI="10.1234/preprint", type="posted-content")
        result = self.resolve([self.item, other])
        self.assertFalse(result["doi"])
        self.assertEqual(result["doi_status"], "skipped_ambiguous_doi")
        self.assertEqual(len(result["doi_candidates"]), 2)

    def test_same_doi_duplicate_metadata_is_not_ambiguous(self):
        """同一 DOI 的重复记录按不区分大小写去重，不误判为不同版本。"""
        other = copy.deepcopy(self.item)
        other["DOI"] = self.item["DOI"].upper()
        self.assertEqual(self.resolve([self.item, other])["doi"], "10.1234/verified")

    def test_incomplete_source_metadata_or_disabled_no_request(self):
        """只有缺少有效标题或关闭补查时，才不发送标题查询。"""
        for update in ({"title": ""}, {"title": None}, {"title": "---"}):
            record = dict(self.record, **update)
            self.assertEqual(self.resolve(record=record)["doi_lookup_status"], "metadata_insufficient")
        self.assertEqual(self.resolve(config=replace(self.config, enabled=False))["doi_lookup_status"], "disabled")
        self.session.get.assert_not_called()

    def test_network_error_recorded_without_guessing(self):
        """网络超时只标记补查失败，重试有上限，不能误报论文不存在或终止摘要筛选。"""
        self.session.get.side_effect = TimeoutError("timeout")
        with patch.object(lookup._common.time, "sleep"):
            result = self.resolve()
        self.assertEqual((result["doi"], result["doi_lookup_status"]), ("", "error"))
        self.assertEqual(self.session.get.call_count, 2)

    def test_owned_http_client_closed(self):
        """补查结束后释放自行创建的 HTTP 会话，避免长批次连接泄漏。"""
        self.response.json.return_value = {"message": {"items": [self.item]}}
        with patch.dict("sys.modules", {"requests": SimpleNamespace(Session=Mock(return_value=self.session))}):
            lookup.lookup_crossref_doi(self.record)
        self.session.close.assert_called_once()

    def test_scholar_conflict_does_not_get_overridden(self):
        """Scholar 明确包含多个不同 DOI 时保留冲突，不用 Crossref 替用户挑选。"""
        publication = {"doi": "10.1234/one", "bib": {"doi": "10.1234/two"}}
        record = dict(self.record)
        with patch.object(screening, "lookup_crossref_doi") as request:
            screening.resolve_doi(record, publication)
        request.assert_not_called()
        self.assertEqual(record["doi_status"], "skipped_ambiguous_doi")

    def test_missing_doi_can_be_disabled_in_settings(self):
        """关闭补查时恢复只从 Scholar 提取 DOI 的行为。"""
        record = dict(self.record)
        with patch.object(screening.user_settings, "DOI_LOOKUP_SETTINGS", replace(self.config, enabled=False)):
            with patch.object(screening, "lookup_crossref_doi") as request:
                screening.resolve_doi(record, {})
        request.assert_not_called()
        self.assertEqual(record["doi_status"], "skipped_no_doi")


if __name__ == "__main__":
    unittest.main()
