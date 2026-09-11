from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.database import connect, initialize_database
from backend.app.evaluation import compare_batches, create_batch, get_batch, run_batch


def fake_react(_connection, _question, baseline, _history):
    intent = baseline.get("intent")
    tool = "diagnose_campaign" if intent == "anomaly_diagnosis" else "query_campaign_metrics"
    if intent is None:
        tool = "search_knowledge"
    return {"answer": baseline["answer"], "degraded": False, "tool_calls": [{"tool": tool}]}


class EvaluationTest(unittest.TestCase):
    def test_fixed_suite_runs_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.db"
            initialize_database(path)
            connection = connect(path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM evaluation_cases").fetchone()[0]
                self.assertEqual(15, count)
                batch = create_batch(connection, "回归测试", 2)
                self.assertEqual("react-v1", batch["snapshot"]["prompt_version"])
                self.assertEqual(5, batch["snapshot"]["max_steps"])
                self.assertEqual(12, len(batch["snapshot"]["knowledge_version"]))
                self.assertEqual(12, len(batch["snapshot"]["evaluation_set_version"]))
            finally:
                connection.close()

            with patch("backend.app.records.compose_with_react", side_effect=fake_react):
                run_batch(path, int(batch["batch_id"]))

            connection = connect(path)
            try:
                detail = get_batch(connection, int(batch["batch_id"]))
                self.assertEqual("completed", detail["status"])
                self.assertEqual(15, len(detail["results"]))
                self.assertEqual(15, detail["passed_cases"])
                self.assertEqual(1.0, detail["summary"]["overall_pass_rate"])
                self.assertEqual(batch["snapshot"], detail["snapshot"])
                comparison = compare_batches(
                    connection, int(batch["batch_id"]), int(batch["batch_id"])
                )
                self.assertEqual(0.0, comparison["differences"]["overall_pass_rate"])
                self.assertEqual(batch["snapshot"], comparison["left_batch"]["snapshot"])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
