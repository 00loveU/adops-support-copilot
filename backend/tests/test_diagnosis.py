from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.database import connect, initialize_database
from backend.app.diagnosis import diagnose_campaign


class DiagnosisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "diagnosis-test.db"
        initialize_database(cls.db_path)
        cls.connection = connect(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()
        cls.temp_dir.cleanup()

    def rules(self, campaign_code: str) -> list[str]:
        result = diagnose_campaign(
            self.connection, campaign_code, "2026-09-08", "2026-09-08"
        )
        return [item["rule_id"] for item in result["diagnosis"]["anomalies"]]

    def test_confirmed_anchor_rules(self) -> None:
        self.assertEqual([], self.rules("CMP001"))
        self.assertEqual(["ANOM-LOW-CTR-001"], self.rules("CMP002"))
        self.assertEqual(["ANOM-SPEND-CHANGE-001"], self.rules("CMP003"))
        self.assertEqual(["ANOM-LOW-CVR-001"], self.rules("CMP004"))
        self.assertEqual(["ANOM-HIGH-CPA-001"], self.rules("CMP005"))
        self.assertEqual(
            ["ANOM-NO-CONVERSION-001", "ANOM-LOW-ROAS-001"], self.rules("CMP007")
        )
        self.assertEqual(["ANOM-LOW-ROAS-001"], self.rules("CMP008"))
        self.assertEqual(["ANOM-SPEND-CHANGE-001"], self.rules("CMP011"))
        self.assertEqual(
            ["ANOM-LOW-CVR-001", "ANOM-HIGH-CPA-001", "ANOM-LOW-ROAS-001"],
            self.rules("CMP013"),
        )

    def test_no_impression_sample_warning_and_paused_boundary(self) -> None:
        self.assertIn("ANOM-NO-IMPRESSION-001", self.rules("CMP010"))
        self.assertEqual([], self.rules("CMP014"))
        result = diagnose_campaign(
            self.connection, "CMP014", "2026-09-08", "2026-09-08"
        )
        self.assertTrue(result["diagnosis"]["sample_warnings"])
        self.assertNotIn("ANOM-NO-IMPRESSION-001", self.rules("CMP015"))

    def test_sources_are_real_database_rows(self) -> None:
        result = diagnose_campaign(
            self.connection, "CMP007", "2026-09-08", "2026-09-08"
        )
        self.assertEqual(
            ["RULE-CVR-001", "RULE-ROAS-001"],
            [source["source_code"] for source in result["sources"]],
        )
        self.assertTrue(all(source["section_title"] == "排查步骤" for source in result["sources"]))


if __name__ == "__main__":
    unittest.main()
