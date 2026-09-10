from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.database import connect, initialize_database
from backend.app.retrieval import NO_RESULT_ANSWER, answer_rule_question, search_knowledge


class KnowledgeRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "retrieval-test.db"
        initialize_database(self.db_path)
        self.connection = connect(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_confirmed_queries_retrieve_expected_primary_source(self) -> None:
        cases = (
            ("CTR 是什么意思", "RULE-METRIC-001"),
            ("广告计划没有曝光", "RULE-DELIVERY-001"),
            ("曝光很多但是点击很少", "RULE-CTR-001"),
            ("有点击但是没有转化", "RULE-CVR-001"),
            ("转化成本太高", "RULE-CPA-001"),
            ("广告投产比很低", "RULE-ROAS-001"),
            ("昨天消耗突然上涨", "RULE-SPEND-001"),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                chunks = search_knowledge(self.connection, question)
                self.assertTrue(chunks)
                self.assertEqual(expected, chunks[0]["source_code"])

    def test_unknown_platform_rule_has_safe_empty_result(self) -> None:
        result = answer_rule_question(self.connection, "真实平台封号后怎么申诉？")
        self.assertEqual(NO_RESULT_ANSWER, result["answer"])
        self.assertEqual([], result["sources"])
        self.assertEqual([], result["retrieved_chunks"])


if __name__ == "__main__":
    unittest.main()
