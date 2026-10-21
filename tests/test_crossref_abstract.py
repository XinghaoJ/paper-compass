"""Crossref 摘要补查离线测试，不发送真实网络请求。"""
import unittest
from importlib import import_module
from unittest.mock import Mock

module = import_module("1crossref_abstract")


class CrossrefAbstractTests(unittest.TestCase):
    """验证 DOI、完整标题、JATS 清理和服务限流。"""

    def response(self, message, status=200):
        """构造官方 API 的模拟响应。"""
        return Mock(status_code=status, json=Mock(return_value={"message": message}))

    def test_exact_doi_and_jats_abstract(self):
        """已有 DOI 使用单条接口，并清理 JATS 标记而不丢失行内文字。"""
        session = Mock()
        session.get.return_value = self.response({"DOI": "10.1234/test", "abstract":
            "<jats:p>Artificial <jats:italic>intelligence</jats:italic> improves CFD &amp; simulation.</jats:p>"})
        engine = module.CrossrefAbstractLookup(session)
        result = engine.lookup("Paper", doi="https://doi.org/10.1234/test")
        self.assertEqual(result["abstract"], "Artificial intelligence improves CFD & simulation.")
        self.assertEqual(result["abstract_lookup_status"], "found")
        self.assertTrue(session.get.call_args.args[0].endswith("10.1234%2Ftest"))
        session.get.assert_called_once()

    def test_title_only_requires_unique_full_title(self):
        """只匹配完整标题，同标题多个 DOI 则不选其中一个摘要。"""
        candidate = {"DOI": "10.1234/a", "title": ["CFD and AI"], "abstract": "A research abstract about computational fluid dynamics."}
        session = Mock()
        session.get.return_value = self.response({"items": [candidate]})
        engine = module.CrossrefAbstractLookup(session)
        self.assertEqual(engine.lookup("CFD and AI")["abstract_lookup_status"], "found")
        self.assertEqual(session.get.call_args.kwargs["params"]["query.title"], "CFD and AI")
        self.assertEqual(engine.lookup("Other paper")["abstract_lookup_status"], "not_found")
        session.get.return_value = self.response({"items": [candidate, dict(candidate, DOI="10.1234/b")]})
        self.assertEqual(engine.lookup("CFD and AI")["abstract_lookup_status"], "not_found")

    def test_doi_not_registered_falls_back_to_title(self):
        """DOI 未在 Crossref 登记时允许完整标题补查。"""
        session = Mock()
        session.get.side_effect = [self.response({}, 404), self.response({"items": [
            {"DOI": "10.1234/a", "title": ["Paper"], "abstract": "A complete research abstract about CFD and AI."}]})]
        result = module.CrossrefAbstractLookup(session).lookup("Paper", doi="10.48550/arxiv.1234")
        self.assertEqual(result["abstract_lookup_status"], "found")
        self.assertEqual(session.get.call_count, 2)

    def test_missing_abstract_and_wrong_doi_not_accepted(self):
        """记录没有摘要则跳过，接口返回其他 DOI 时明确报错。"""
        session = Mock()
        session.get.return_value = self.response({"DOI": "10.1234/a"})
        engine = module.CrossrefAbstractLookup(session)
        self.assertEqual(engine.lookup("Paper", doi="10.1234/a")["abstract_lookup_status"], "not_found")
        self.assertEqual(engine.lookup("Paper", doi="10.1234/b")["abstract_lookup_status"], "error")

    def test_rate_limit_stops_remaining_abstract_requests(self):
        """429 后本次任务不再发起摘要补查请求。"""
        session = Mock()
        session.get.return_value = self.response({}, 429)
        engine = module.CrossrefAbstractLookup(session)
        self.assertEqual(engine.lookup("One")["abstract_lookup_status"], "blocked")
        self.assertEqual(engine.lookup("Two")["abstract_lookup_status"], "blocked")
        session.get.assert_called_once()
