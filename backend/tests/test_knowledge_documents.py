from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path


KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "data" / "knowledge"
EXPECTED_CODES = {
    "RULE-METRIC-001",
    "RULE-DELIVERY-001",
    "RULE-CTR-001",
    "RULE-CVR-001",
    "RULE-CPA-001",
    "RULE-ROAS-001",
    "RULE-SPEND-001",
}


def parse_document(path: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    text = path.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = {}
    for line in frontmatter.strip().splitlines():
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')

    parts = re.split(r"^##\s+", body, flags=re.MULTILINE)[1:]
    chunks = []
    for part in parts:
        section_title, content = part.split("\n", 1)
        chunks.append((section_title.strip(), content.strip()))
    return metadata, chunks


class KnowledgeDocumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = [parse_document(path) for path in sorted(KNOWLEDGE_DIR.glob("*.md"))]

    def test_sources_and_chunks_are_complete(self) -> None:
        self.assertEqual(7, len(self.documents))
        self.assertEqual(EXPECTED_CODES, {metadata["source_code"] for metadata, _ in self.documents})
        for metadata, chunks in self.documents:
            self.assertTrue(metadata["title"])
            self.assertTrue(metadata["category"])
            self.assertEqual("1.0", metadata["version"])
            self.assertTrue(metadata["keywords"])
            self.assertGreaterEqual(len(chunks), 3)
            self.assertTrue(all(title and content for title, content in chunks))

    def test_chinese_trigram_search(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5("
            "source_code UNINDEXED, title, section_title, content, keywords, tokenize='trigram')"
        )
        for metadata, chunks in self.documents:
            for section_title, content in chunks:
                connection.execute(
                    "INSERT INTO knowledge_fts VALUES (?, ?, ?, ?, ?)",
                    (
                        metadata["source_code"],
                        metadata["title"],
                        section_title,
                        content,
                        metadata["keywords"],
                    ),
                )

        cases = {
            "指标口径": "RULE-METRIC-001",
            "没有曝光": "RULE-DELIVERY-001",
            "点击很少": "RULE-CTR-001",
            "没有转化": "RULE-CVR-001",
            "转化成本": "RULE-CPA-001",
            "广告投产比": "RULE-ROAS-001",
            "消耗突然上涨": "RULE-SPEND-001",
        }
        for query, expected_code in cases.items():
            row = connection.execute(
                "SELECT source_code FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm25(knowledge_fts) LIMIT 1",
                (query,),
            ).fetchone()
            self.assertIsNotNone(row, query)
            self.assertEqual(expected_code, row[0], query)


if __name__ == "__main__":
    unittest.main()
