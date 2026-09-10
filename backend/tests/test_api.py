from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.database import connect, initialize_database
from backend.app.main import app, get_db


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.react_patcher = patch(
            "backend.app.records.compose_with_react",
            side_effect=lambda _connection, _question, baseline, _history: {
                "answer": baseline["answer"],
                "degraded": False,
                "tool_calls": [{"tool": "mock_tool", "parameters": {}, "summary": {}}],
            },
        )
        self.react_patcher.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "api-test.db"
        initialize_database(self.db_path)

        def override_db():
            connection = connect(self.db_path)
            try:
                yield connection
            finally:
                connection.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.temp_dir.cleanup()
        self.react_patcher.stop()

    def login(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "operator1", "password": "Operator123!"},
        )
        self.assertEqual(200, response.status_code)

    def test_authentication_lifecycle(self) -> None:
        unauthorized = self.client.get("/api/advertisers")
        self.assertEqual(401, unauthorized.status_code)
        self.assertEqual("AUTH_REQUIRED", unauthorized.json()["error"]["code"])

        wrong = self.client.post(
            "/api/auth/login", json={"username": "operator1", "password": "wrong"}
        )
        self.assertEqual(401, wrong.status_code)
        self.assertEqual("INVALID_CREDENTIALS", wrong.json()["error"]["code"])

        self.login()
        me = self.client.get("/api/auth/me")
        self.assertEqual("operator1", me.json()["data"]["user"]["username"])
        self.assertNotIn("password_hash", me.text)
        self.assertEqual(200, self.client.post("/api/auth/logout").status_code)
        self.assertEqual(401, self.client.get("/api/advertisers").status_code)

    def test_health_and_advertiser_selection(self) -> None:
        self.assertEqual({"status": "ok"}, self.client.get("/api/health").json()["data"])
        self.login()

        advertisers = self.client.get("/api/advertisers", params={"q": "星河"}).json()["data"]
        self.assertEqual(1, advertisers["total"])
        self.assertEqual("ADV001", advertisers["items"][0]["advertiser_code"])

        campaigns = self.client.get("/api/advertisers/1/campaigns").json()["data"]
        self.assertEqual(3, campaigns["total"])
        self.assertEqual("CMP001", campaigns["items"][0]["campaign_code"])

    def test_metrics_and_controlled_errors(self) -> None:
        self.login()
        response = self.client.get(
            "/api/campaigns/1/metrics",
            params={"start_date": "2026-09-07", "end_date": "2026-09-08"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(0.014852, response.json()["data"]["calculated"]["ctr"])

        missing = self.client.get(
            "/api/campaigns/999/metrics",
            params={"start_date": "2026-09-07", "end_date": "2026-09-08"},
        )
        self.assertEqual(404, missing.status_code)
        self.assertEqual("CAMPAIGN_NOT_FOUND", missing.json()["error"]["code"])

        invalid = self.client.get(
            "/api/campaigns/1/metrics",
            params={"start_date": "2026-09-09", "end_date": "2026-09-08"},
        )
        self.assertEqual(400, invalid.status_code)
        self.assertEqual("VALIDATION_ERROR", invalid.json()["error"]["code"])

    def test_metric_query_is_saved_with_trace_and_user_isolation(self) -> None:
        self.login()
        response = self.client.post(
            "/api/assistant/messages",
            json={
                "message": "查询 CMP001 最近两天的指标",
                "context": {
                    "advertiser_id": 1,
                    "campaign_id": 1,
                    "start_date": "2026-09-07",
                    "end_date": "2026-09-08",
                },
            },
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["trace_id"].startswith("tr_"))
        self.assertEqual("completed", payload["data"]["status"])
        self.assertEqual(0.014852, payload["data"]["metrics"]["calculated"]["ctr"])

        record_id = payload["data"]["record_id"]
        records = self.client.get("/api/records").json()["data"]
        self.assertGreaterEqual(records["total"], 1)
        detail = self.client.get(f"/api/records/{record_id}")
        self.assertEqual(200, detail.status_code)
        self.assertEqual(payload["trace_id"], detail.json()["data"]["trace_id"])

        with self.client as admin_client:
            admin_client.cookies.clear()
            admin_client.post(
                "/api/auth/login",
                json={"username": "admin1", "password": "Admin123!"},
            )
            self.assertEqual(404, admin_client.get(f"/api/records/{record_id}").status_code)

        connection = sqlite3.connect(self.db_path)
        try:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM trace_events WHERE trace_id = ?", (payload["trace_id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(5, event_count)

    def test_missing_parameter_is_clarified_in_same_record(self) -> None:
        self.login()
        first = self.client.post(
            "/api/assistant/messages",
            json={
                "message": "查询指标",
                "context": {},
            },
        ).json()
        self.assertEqual("waiting_clarification", first["data"]["status"])
        self.assertEqual(
            ["campaign_id", "start_date", "end_date"], first["data"]["missing_fields"]
        )

        second = self.client.post(
            f"/api/assistant/sessions/{first['data']['session_id']}/messages",
            json={"message": "计划 CMP001"},
        ).json()
        self.assertEqual("waiting_clarification", second["data"]["status"])
        self.assertEqual(["start_date", "end_date"], second["data"]["missing_fields"])
        self.assertEqual(first["data"]["record_id"], second["data"]["record_id"])

        completed = self.client.post(
            f"/api/assistant/sessions/{first['data']['session_id']}/messages",
            json={"message": "2026-09-07 至 2026-09-08"},
        ).json()
        self.assertEqual("completed", completed["data"]["status"])
        self.assertEqual(first["data"]["record_id"], completed["data"]["record_id"])
        self.assertEqual(first["trace_id"], completed["trace_id"])
        self.assertEqual(1, self.client.get("/api/records").json()["data"]["total"])

    def test_clarification_is_private_and_expires(self) -> None:
        self.login()
        first = self.client.post(
            "/api/assistant/messages",
            json={"message": "查询指标", "context": {}},
        ).json()
        session_id = first["data"]["session_id"]

        admin_client = TestClient(app)
        try:
            admin_client.post(
                "/api/auth/login",
                json={"username": "admin1", "password": "Admin123!"},
            )
            other_user = admin_client.post(
                f"/api/assistant/sessions/{session_id}/messages",
                json={"message": "CMP001"},
            )
            self.assertEqual(404, other_user.status_code)
            self.assertEqual(
                "CLARIFICATION_SESSION_NOT_FOUND", other_user.json()["error"]["code"]
            )
        finally:
            admin_client.close()

        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE clarification_sessions SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", session_id),
            )
            connection.commit()
        finally:
            connection.close()

        expired = self.client.post(
            f"/api/assistant/sessions/{session_id}/messages",
            json={"message": "CMP001"},
        )
        self.assertEqual(400, expired.status_code)
        self.assertEqual("CLARIFICATION_SESSION_EXPIRED", expired.json()["error"]["code"])

    def test_anomaly_diagnosis_uses_confirmed_rules_and_sources(self) -> None:
        self.login()
        response = self.client.post(
            "/api/assistant/messages",
            json={
                "message": "诊断 CMP013 在 2026-09-08 为什么异常",
                "context": {},
            },
        )
        self.assertEqual(200, response.status_code)
        data = response.json()["data"]
        self.assertEqual("anomaly_diagnosis", data["intent"])
        self.assertEqual(
            ["ANOM-LOW-CVR-001", "ANOM-HIGH-CPA-001", "ANOM-LOW-ROAS-001"],
            [item["rule_id"] for item in data["diagnosis"]["anomalies"]],
        )
        self.assertEqual(
            ["RULE-CVR-001", "RULE-CPA-001", "RULE-ROAS-001"],
            [source["source_code"] for source in data["sources"]],
        )

    def test_rule_qa_uses_fts_sources_without_clarification(self) -> None:
        self.login()
        response = self.client.post(
            "/api/assistant/messages",
            json={"message": "CTR 是什么意思？", "context": {}},
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        data = payload["data"]
        self.assertEqual("completed", data["status"])
        self.assertEqual("rule_qa", data["intent"])
        self.assertIsNone(data["session_id"])
        self.assertIsNone(data["metrics"])
        self.assertIsNone(data["diagnosis"])
        self.assertEqual("RULE-METRIC-001", data["sources"][0]["source_code"])

        detail = self.client.get(f"/api/records/{data['record_id']}").json()["data"]
        self.assertEqual("rule_qa", detail["intent"])
        self.assertEqual(payload["trace_id"], detail["trace_id"])

    def test_rule_qa_does_not_invent_unknown_platform_rules(self) -> None:
        self.login()
        data = self.client.post(
            "/api/assistant/messages",
            json={"message": "真实平台封号后怎么申诉？", "context": {}},
        ).json()["data"]
        self.assertEqual("rule_qa", data["intent"])
        self.assertEqual([], data["sources"])
        self.assertIn("没有该问题的可靠依据", data["answer"])

    def test_feedback_is_private_and_repeated_submission_updates(self) -> None:
        self.login()
        query = self.client.post(
            "/api/assistant/messages",
            json={"message": "CTR 是什么意思？", "context": {}},
        ).json()["data"]
        record_id = query["record_id"]

        first = self.client.post(
            f"/api/records/{record_id}/feedback", json={"rating": "helpful"}
        )
        self.assertEqual(200, first.status_code)
        updated = self.client.post(
            f"/api/records/{record_id}/feedback", json={"rating": "not_helpful"}
        )
        self.assertEqual("not_helpful", updated.json()["data"]["rating"])
        self.assertEqual(
            "not_helpful", self.client.get(f"/api/records/{record_id}").json()["data"]["feedback"]
        )
        self.assertEqual(
            "not_helpful", self.client.get("/api/records").json()["data"]["items"][0]["feedback"]
        )

        with self.client as admin_client:
            admin_client.cookies.clear()
            admin_client.post(
                "/api/auth/login", json={"username": "admin1", "password": "Admin123!"}
            )
            denied = admin_client.post(
                f"/api/records/{record_id}/feedback", json={"rating": "helpful"}
            )
            self.assertEqual(404, denied.status_code)

        connection = sqlite3.connect(self.db_path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM feedback WHERE record_id = ?", (record_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, count)

    def test_only_admin_can_view_knowledge_sources(self) -> None:
        self.login()
        denied = self.client.get("/api/admin/knowledge-sources")
        self.assertEqual(403, denied.status_code)
        self.assertEqual("ADMIN_REQUIRED", denied.json()["error"]["code"])

        self.client.cookies.clear()
        self.client.post(
            "/api/auth/login", json={"username": "admin1", "password": "Admin123!"}
        )
        response = self.client.get(
            "/api/admin/knowledge-sources", params={"category": "ctr"}
        )
        self.assertEqual(200, response.status_code)
        data = response.json()["data"]
        self.assertEqual(1, data["total"])
        self.assertEqual("RULE-CTR-001", data["items"][0]["source_code"])
        self.assertEqual(4, len(data["items"][0]["chunks"]))

    def test_conversation_is_persistent_and_private(self) -> None:
        self.login()
        first = self.client.post(
            "/api/assistant/messages",
            json={"message": "CTR 是什么意思？", "context": {}},
        ).json()["data"]
        conversation_id = first["conversation_id"]

        second = self.client.post(
            "/api/assistant/messages",
            json={
                "message": "ROAS 是什么意思？",
                "context": {},
                "conversation_id": conversation_id,
            },
        )
        self.assertEqual(200, second.status_code)
        self.assertEqual(conversation_id, second.json()["data"]["conversation_id"])

        conversations = self.client.get("/api/conversations").json()["data"]["items"]
        self.assertEqual(1, len(conversations))
        self.assertEqual(4, conversations[0]["message_count"])

        detail = self.client.get(f"/api/conversations/{conversation_id}").json()["data"]
        self.assertEqual(["user", "assistant", "user", "assistant"], [m["role"] for m in detail["messages"]])
        self.assertEqual(second.json()["data"]["record_id"], detail["latest_result"]["record_id"])

        self.client.cookies.clear()
        self.client.post(
            "/api/auth/login", json={"username": "admin1", "password": "Admin123!"}
        )
        self.assertEqual(
            404, self.client.get(f"/api/conversations/{conversation_id}").status_code
        )


if __name__ == "__main__":
    unittest.main()
