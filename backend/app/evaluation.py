from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from backend.app.database import connect
from backend.app.records import (
    create_clarification,
    detect_intent,
    execute_query,
    execute_rule_query,
    fill_slots_from_message,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_batch(connection: sqlite3.Connection, name: str, user_id: int) -> dict[str, object]:
    running = connection.execute(
        "SELECT id FROM evaluation_batches WHERE status IN ('pending', 'running') LIMIT 1"
    ).fetchone()
    if running:
        raise RuntimeError("EVALUATION_ALREADY_RUNNING")
    total = connection.execute(
        "SELECT COUNT(*) FROM evaluation_cases WHERE enabled = 1"
    ).fetchone()[0]
    code = f"EVAL-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    now = _now()
    cursor = connection.execute(
        "INSERT INTO evaluation_batches "
        "(batch_code, name, started_by, status, total_cases, started_at, created_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (code, name.strip() or "离线回归评估", user_id, total, now, now),
    )
    connection.commit()
    return {"batch_id": cursor.lastrowid, "batch_code": code, "status": "running", "total_cases": total}


def _run_actual(source: sqlite3.Connection, case_input: dict[str, object], user_id: int) -> dict[str, object]:
    sandbox = sqlite3.connect(":memory:")
    sandbox.row_factory = sqlite3.Row
    sandbox.execute("PRAGMA foreign_keys = ON")
    source.backup(sandbox)
    try:
        message = str(case_input["message"])
        context = dict(case_input.get("context") or {})
        intent = detect_intent(message)
        if intent == "rule_qa":
            _, result = execute_rule_query(sandbox, user_id, message)
            return result

        campaign_code = context.get("campaign_code")
        campaign_id = None
        if campaign_code:
            row = sandbox.execute(
                "SELECT id FROM campaigns WHERE campaign_code = ?", (campaign_code,)
            ).fetchone()
            campaign_id = row["id"] if row else None
        slots = fill_slots_from_message(
            sandbox,
            message,
            {
                "campaign_id": campaign_id,
                "start_date": context.get("start_date"),
                "end_date": context.get("end_date"),
            },
        )
        missing = [key for key in ("campaign_id", "start_date", "end_date") if slots.get(key) is None]
        if missing:
            _, result = create_clarification(sandbox, user_id, message, slots, intent)
            return result

        _, result = execute_query(
            sandbox,
            user_id,
            message,
            int(slots["campaign_id"]),
            str(slots["start_date"]),
            str(slots["end_date"]),
            intent,
        )
        result["params"] = {
            "campaign_code": campaign_code,
            "start_date": slots["start_date"],
            "end_date": slots["end_date"],
        }
        return result
    finally:
        sandbox.close()


def _same_value(actual: object, expected: object) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= 0.000001
    return actual == expected


def score_case(actual: dict[str, object], expected: dict[str, object]) -> tuple[bool, dict[str, bool], str | None]:
    scores: dict[str, bool] = {"intent": actual.get("intent") == expected.get("intent")}

    if "params" in expected:
        scores["parameters"] = all(
            _same_value((actual.get("params") or {}).get(key), value)
            for key, value in expected["params"].items()
        )
    if "metrics" in expected:
        calculated = ((actual.get("metrics") or {}).get("calculated") or {})
        scores["metrics"] = all(
            _same_value(calculated.get(key), value) for key, value in expected["metrics"].items()
        )
    if "anomaly_rule_ids" in expected:
        anomaly_ids = [item["rule_id"] for item in (actual.get("diagnosis") or {}).get("anomalies", [])]
        scores["diagnosis"] = set(anomaly_ids) == set(expected["anomaly_rule_ids"])
    if "source_codes" in expected:
        source_codes = [item["source_code"] for item in actual.get("sources", [])]
        scores["retrieval"] = set(source_codes) == set(expected["source_codes"])
    if "primary_source" in expected:
        sources = actual.get("sources", [])
        primary = sources[0]["source_code"] if sources else None
        scores["retrieval"] = primary == expected["primary_source"]
    if "missing_fields" in expected:
        scores["clarification"] = set(actual.get("missing_fields", [])) == set(expected["missing_fields"])
    if "warning_contains" in expected:
        warnings = (actual.get("diagnosis") or {}).get("sample_warnings", [])
        scores["warning"] = all(
            any(fragment in warning for warning in warnings)
            for fragment in expected["warning_contains"]
        )

    called_tools = [item.get("tool") for item in actual.get("tool_calls", [])]
    if expected.get("expected_tool"):
        scores["tool_selection"] = expected["expected_tool"] in called_tools
        scores["availability"] = not bool(actual.get("degraded"))
    elif expected.get("require_no_tool"):
        scores["tool_selection"] = not called_tools

    failed = [name for name, passed in scores.items() if not passed]
    return not failed, scores, "未通过：" + "、".join(failed) if failed else None


def _summary(results: list[dict[str, object]]) -> dict[str, object]:
    def rate(items: list[bool]) -> float:
        return round(sum(items) / len(items), 4) if items else 0.0

    categories: dict[str, list[bool]] = {}
    score_groups: dict[str, list[bool]] = {}
    for result in results:
        categories.setdefault(str(result["category"]), []).append(bool(result["passed"]))
        for name, passed in result["scores"].items():
            score_groups.setdefault(name, []).append(bool(passed))
    return {
        "overall_pass_rate": rate([bool(item["passed"]) for item in results]),
        "category_rates": {name: rate(items) for name, items in categories.items()},
        "score_rates": {name: rate(items) for name, items in score_groups.items()},
        "average_latency_ms": round(sum(int(item["latency_ms"]) for item in results) / len(results)) if results else 0,
        "degraded_cases": sum(bool(item["actual"].get("degraded")) for item in results),
    }


def run_batch(database_path: str | Path, batch_id: int) -> None:
    connection = connect(database_path)
    results: list[dict[str, object]] = []
    try:
        batch = connection.execute(
            "SELECT started_by FROM evaluation_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        cases = connection.execute(
            "SELECT id, case_code, category, input_json, expected_json "
            "FROM evaluation_cases WHERE enabled = 1 ORDER BY case_code"
        ).fetchall()
        for case in cases:
            started = time.perf_counter()
            expected = json.loads(case["expected_json"])
            try:
                actual = _run_actual(connection, json.loads(case["input_json"]), int(batch["started_by"]))
                passed, scores, failure = score_case(actual, expected)
            except Exception as exc:
                actual = {"error_type": type(exc).__name__}
                passed, scores, failure = False, {"execution": False}, "执行失败"
            latency = round((time.perf_counter() - started) * 1000)
            item = {
                "category": case["category"], "passed": passed, "scores": scores,
                "actual": actual, "latency_ms": latency,
            }
            results.append(item)
            connection.execute(
                "INSERT INTO evaluation_results "
                "(batch_id, case_id, passed, actual_json, scores_json, failure_reason, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, case["id"], int(passed), _json(actual), _json(scores), failure, latency, _now()),
            )
            connection.commit()

        passed_count = sum(bool(item["passed"]) for item in results)
        connection.execute(
            "UPDATE evaluation_batches SET status = 'completed', passed_cases = ?, failed_cases = ?, "
            "summary_json = ?, completed_at = ? WHERE id = ?",
            (passed_count, len(results) - passed_count, _json(_summary(results)), _now(), batch_id),
        )
        connection.commit()
    except Exception:
        connection.execute(
            "UPDATE evaluation_batches SET status = 'failed', completed_at = ? WHERE id = ?",
            (_now(), batch_id),
        )
        connection.commit()
    finally:
        connection.close()


def list_batches(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT b.id, b.batch_code, b.name, b.status, b.total_cases, b.passed_cases, "
        "b.failed_cases, b.summary_json, b.started_at, b.completed_at, b.created_at, u.display_name AS started_by_name "
        "FROM evaluation_batches b JOIN users u ON u.id = b.started_by ORDER BY b.id DESC"
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json")) if item["summary_json"] else None
        items.append(item)
    return items


def get_batch(connection: sqlite3.Connection, batch_id: int) -> dict[str, object] | None:
    batch = connection.execute(
        "SELECT b.*, u.display_name AS started_by_name FROM evaluation_batches b "
        "JOIN users u ON u.id = b.started_by WHERE b.id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        return None
    item = dict(batch)
    item["summary"] = json.loads(item.pop("summary_json")) if item["summary_json"] else None
    rows = connection.execute(
        "SELECT c.case_code, c.category, c.input_json, c.expected_json, r.passed, r.actual_json, "
        "r.scores_json, r.failure_reason, r.latency_ms FROM evaluation_results r "
        "JOIN evaluation_cases c ON c.id = r.case_id WHERE r.batch_id = ? ORDER BY c.case_code",
        (batch_id,),
    ).fetchall()
    item["results"] = [
        {
            **{key: row[key] for key in ("case_code", "category", "passed", "failure_reason", "latency_ms")},
            "input": json.loads(row["input_json"]),
            "expected": json.loads(row["expected_json"]),
            "actual": json.loads(row["actual_json"]),
            "scores": json.loads(row["scores_json"]),
        }
        for row in rows
    ]
    return item


def compare_batches(connection: sqlite3.Connection, left_id: int, right_id: int) -> dict[str, object] | None:
    left, right = get_batch(connection, left_id), get_batch(connection, right_id)
    if left is None or right is None:
        return None
    left_summary, right_summary = left.get("summary") or {}, right.get("summary") or {}
    keys = ("overall_pass_rate", "average_latency_ms", "degraded_cases")
    differences = {
        key: round(float(right_summary.get(key, 0)) - float(left_summary.get(key, 0)), 4)
        for key in keys
    }
    categories = set((left_summary.get("category_rates") or {})) | set((right_summary.get("category_rates") or {}))
    differences["category_rates"] = {
        key: round(float((right_summary.get("category_rates") or {}).get(key, 0)) - float((left_summary.get("category_rates") or {}).get(key, 0)), 4)
        for key in sorted(categories)
    }
    return {
        "left_batch": {"id": left_id, "batch_code": left["batch_code"], "summary": left_summary},
        "right_batch": {"id": right_id, "batch_code": right["batch_code"], "summary": right_summary},
        "differences": differences,
    }
