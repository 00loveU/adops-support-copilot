from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app.database import connect, initialize_database, list_advertisers
from backend.app.metrics import get_campaign_metrics


class DatabaseMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        initialize_database(self.db_path)
        self.connection = connect(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_import_is_complete_and_idempotent(self) -> None:
        expected_counts = {
            "users": 2,
            "advertisers": 5,
            "campaigns": 15,
            "daily_metrics": 450,
            "knowledge_sources": 7,
            "knowledge_chunks": 28,
        }
        for table, expected in expected_counts.items():
            actual = self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(expected, actual, table)

        self.connection.close()
        initialize_database(self.db_path)
        self.connection = connect(self.db_path)
        self.assertEqual(450, self.connection.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0])

    def test_advertiser_query_and_fts(self) -> None:
        advertisers = list_advertisers(self.connection)
        self.assertEqual(5, len(advertisers))
        self.assertEqual("ADV001", advertisers[0]["advertiser_code"])
        source = self.connection.execute(
            "SELECT source_code FROM knowledge_fts WHERE knowledge_fts MATCH ? "
            "ORDER BY bm25(knowledge_fts) LIMIT 1",
            ("没有曝光",),
        ).fetchone()
        self.assertEqual("RULE-DELIVERY-001", source[0])

    def test_metric_uses_aggregated_raw_values(self) -> None:
        result = get_campaign_metrics(self.connection, "CMP001", "2026-09-07", "2026-09-08")
        self.assertEqual(40532, result["raw"]["impressions"])
        self.assertEqual(602, result["raw"]["clicks"])
        self.assertEqual(14, result["raw"]["conversions"])
        self.assertEqual(60000, result["raw"]["spend_cents"])
        self.assertEqual(0.014852, result["calculated"]["ctr"])
        self.assertEqual(4286, result["calculated"]["cpa_cents"])

    def test_zero_denominator_returns_null_and_reason(self) -> None:
        result = get_campaign_metrics(self.connection, "CMP015", "2026-09-08", "2026-09-08")
        self.assertIsNone(result["calculated"]["ctr"])
        self.assertIsNone(result["calculated"]["roas"])
        self.assertEqual({"ctr", "cvr", "cpc", "cpa", "roas"}, {
            item["metric"] for item in result["unavailable_metrics"]
        })

    def test_invalid_date_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "start_date"):
            get_campaign_metrics(self.connection, "CMP001", "2026-09-09", "2026-09-08")


if __name__ == "__main__":
    unittest.main()
