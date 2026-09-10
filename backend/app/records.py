from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from backend.app.diagnosis import diagnose_campaign
from backend.app.metrics import get_campaign_metrics
from backend.app.react_service import compose_with_react
from backend.app.retrieval import answer_rule_question


REQUIRED_SLOTS = ("campaign_id", "start_date", "end_date")


class ClarificationNotFound(Exception):
    pass


class ClarificationExpired(Exception):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _missing(slots: dict[str, object]) -> list[str]:
    return [name for name in REQUIRED_SLOTS if slots.get(name) is None]


def _question(missing: list[str]) -> str:
    if "campaign_id" in missing:
        return "请提供需要查询的广告计划，例如 CMP001。"
    if "start_date" in missing and "end_date" in missing:
        return "请提供查询日期范围，例如 2026-09-01 至 2026-09-08。"
    if "start_date" in missing:
        return "请提供开始日期，例如 2026-09-01。"
    return "请提供结束日期，例如 2026-09-08。"


def detect_intent(message: str) -> str:
    rule_words = ("是什么", "什么意思", "怎么计算", "如何计算", "口径", "怎么排查", "如何排查", "怎么办", "怎么优化", "如何优化", "申诉")
    if any(word in message for word in rule_words):
        return "rule_qa"
    diagnosis_words = ("诊断", "异常", "为什么", "偏低", "偏高", "没有曝光", "没有转化", "波动")
    return "anomaly_diagnosis" if any(word in message for word in diagnosis_words) else "metric_query"


def execute_rule_query(
    connection: sqlite3.Connection,
    user_id: int,
    message: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, object]]:
    started = time.perf_counter()
    trace_id = f"tr_{secrets.token_hex(12)}"
    retrieval_started = time.perf_counter()
    knowledge = answer_rule_question(connection, message)
    retrieval_latency = round((time.perf_counter() - retrieval_started) * 1000)
    sources = knowledge["sources"]
    answer = str(knowledge["answer"])
    agent_result: dict[str, object] = {
        "answer": answer,
        "degraded": False,
        "tool_calls": [],
    }
    if sources:
        agent_result = compose_with_react(
            connection,
            message,
            {"answer": answer, "sources": sources},
            conversation_history or [],
        )
        answer = str(agent_result["answer"])
    now = _now()

    cursor = connection.execute(
        "INSERT INTO processing_records "
        "(trace_id, user_id, original_query, intent, status, request_params_json, created_at) "
        "VALUES (?, ?, ?, 'rule_qa', 'processing', '{}', ?)",
        (trace_id, user_id, message, now),
    )
    record_id = cursor.lastrowid
    result = {
        "status": "completed",
        "record_id": record_id,
        "session_id": None,
        "intent": "rule_qa",
        "answer": answer,
        "metrics": None,
        "diagnosis": None,
        "sources": sources,
        "degraded": agent_result["degraded"],
    }
    if agent_result.get("degraded_reason"):
        result["degraded_reason"] = agent_result["degraded_reason"]
    total_latency = round((time.perf_counter() - started) * 1000)

    _add_event(connection, trace_id, "request_received", "assistant_message", {"message": message})
    _add_event(
        connection,
        trace_id,
        "knowledge_retrieval",
        "knowledge_searched",
        {"message": message},
        {"sources": sources},
        retrieval_latency,
    )
    _add_event(
        connection,
        trace_id,
        "answer_generation",
        "react_agent_completed" if sources else "grounded_template_applied",
        output_value={
            "source_count": len(sources),
            "degraded": agent_result["degraded"],
            "tool_calls": agent_result["tool_calls"],
        },
    )
    connection.execute(
        "UPDATE processing_records SET status = 'completed', result_json = ?, final_answer = ?, "
        "total_latency_ms = ?, completed_at = ? WHERE id = ? AND user_id = ?",
        (_json(result), answer, total_latency, now, record_id, user_id),
    )
    _add_event(
        connection, trace_id, "record_saved", "processing_record_saved", output_value={"record_id": record_id}
    )
    connection.commit()
    return trace_id, result


def _add_event(
    connection: sqlite3.Connection,
    trace_id: str,
    stage: str,
    event_type: str,
    input_value: object = None,
    output_value: object = None,
    latency_ms: int = 0,
) -> None:
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM trace_events WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO trace_events "
        "(trace_id, sequence_no, stage, event_type, status, input_json, output_json, latency_ms, created_at) "
        "VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, ?)",
        (
            trace_id,
            sequence,
            stage,
            event_type,
            _json(input_value) if input_value is not None else None,
            _json(output_value) if output_value is not None else None,
            latency_ms,
            _now(),
        ),
    )


def fill_slots_from_message(
    connection: sqlite3.Connection, message: str, slots: dict[str, object]
) -> dict[str, object]:
    slots = dict(slots)
    if slots.get("campaign_id") is None and (match := re.search(r"\bCMP\d{3}\b", message, re.I)):
        campaign = connection.execute(
            "SELECT id FROM campaigns WHERE campaign_code = ?", (match.group().upper(),)
        ).fetchone()
        if campaign is None:
            raise LookupError(f"未找到广告计划 {match.group().upper()}。")
        slots["campaign_id"] = campaign["id"]

    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)
    if len(dates) >= 2:
        if slots.get("start_date") is None:
            slots["start_date"] = dates[0]
        if slots.get("end_date") is None:
            slots["end_date"] = dates[1]
    elif len(dates) == 1:
        if slots.get("start_date") is None and slots.get("end_date") is None:
            slots["start_date"] = slots["end_date"] = dates[0]
        elif slots.get("start_date") is None:
            slots["start_date"] = dates[0]
        elif slots.get("end_date") is None:
            slots["end_date"] = dates[0]
    return slots


def create_clarification(
    connection: sqlite3.Connection,
    user_id: int,
    message: str,
    slots: dict[str, object],
    intent: str,
) -> tuple[str, dict[str, object]]:
    trace_id = f"tr_{secrets.token_hex(12)}"
    session_id = f"cs_{secrets.token_hex(12)}"
    now = datetime.now(UTC)
    missing = _missing(slots)
    cursor = connection.execute(
        "INSERT INTO processing_records "
        "(trace_id, user_id, original_query, intent, status, request_params_json, created_at) "
        "VALUES (?, ?, ?, ?, 'waiting_clarification', ?, ?)",
        (trace_id, user_id, message, intent, _json(slots), now.isoformat()),
    )
    record_id = cursor.lastrowid
    connection.execute(
        "INSERT INTO clarification_sessions "
        "(id, record_id, user_id, intent, slots_json, missing_slots_json, status, expires_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (
            session_id,
            record_id,
            user_id,
            intent,
            _json(slots),
            _json(missing),
            (now + timedelta(minutes=30)).isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    _add_event(connection, trace_id, "request_received", "assistant_message", {"message": message})
    _add_event(
        connection,
        trace_id,
        "slot_validation",
        "parameters_missing",
        slots,
        {"missing_fields": missing},
    )
    connection.commit()
    return trace_id, {
        "status": "waiting_clarification",
        "record_id": record_id,
        "session_id": session_id,
        "intent": intent,
        "known_params": {key: value for key, value in slots.items() if value is not None},
        "missing_fields": missing,
        "clarification_question": _question(missing),
    }


def execute_query(
    connection: sqlite3.Connection,
    user_id: int,
    message: str,
    campaign_id: int,
    start_date: str,
    end_date: str,
    intent: str = "metric_query",
    record_id: int | None = None,
    trace_id: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, object]]:
    campaign = connection.execute(
        "SELECT campaign_code FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    if campaign is None:
        raise LookupError(f"未找到广告计划 {campaign_id}。")

    started = time.perf_counter()
    metrics_started = time.perf_counter()
    if intent == "anomaly_diagnosis":
        analysis = diagnose_campaign(connection, campaign["campaign_code"], start_date, end_date)
        metrics = analysis["metrics"]
        diagnosis = analysis["diagnosis"]
        sources = analysis["sources"]
    else:
        metrics = get_campaign_metrics(connection, campaign["campaign_code"], start_date, end_date)
        diagnosis = None
        sources = []
    metrics_latency = round((time.perf_counter() - metrics_started) * 1000)
    trace_id = trace_id or f"tr_{secrets.token_hex(12)}"
    params = {"campaign_id": campaign_id, "start_date": start_date, "end_date": end_date}
    calculated = metrics["calculated"]
    ctr_text = (
        f"{calculated['ctr'] * 100:.2f}%" if calculated["ctr"] is not None else "无法计算"
    )
    roas_text = str(calculated["roas"]) if calculated["roas"] is not None else "无法计算"
    if diagnosis is None:
        answer = (
            f"计划 {campaign['campaign_code']} 在 {start_date} 至 {end_date} 的 "
            f"CTR 为 {ctr_text}，ROAS 为 {roas_text}。"
        )
    elif diagnosis["anomalies"]:
        names = "、".join(item["name"] for item in diagnosis["anomalies"])
        answer = f"计划 {campaign['campaign_code']} 命中以下模拟异常：{names}。"
    else:
        answer = f"计划 {campaign['campaign_code']} 未命中已确认的模拟异常规则。"
    baseline = {
        "answer": answer,
        "intent": intent,
        "metrics": {
            "raw": metrics["raw"],
            "calculated": metrics["calculated"],
            "unavailable_metrics": metrics["unavailable_metrics"],
        },
        "diagnosis": diagnosis,
        "sources": sources,
    }
    agent_question = (
        f"{message}\n已确认页面参数：campaign_code={campaign['campaign_code']}，"
        f"start_date={start_date}，end_date={end_date}。"
    )
    agent_result = compose_with_react(
        connection, agent_question, baseline, conversation_history or []
    )
    answer = str(agent_result["answer"])
    result = {
        "status": "completed",
        "session_id": None,
        "intent": intent,
        "answer": answer,
        "metrics": {
            "raw": metrics["raw"],
            "calculated": metrics["calculated"],
            "unavailable_metrics": metrics["unavailable_metrics"],
        },
        "diagnosis": diagnosis,
        "sources": sources,
        "degraded": agent_result["degraded"],
    }
    if agent_result.get("degraded_reason"):
        result["degraded_reason"] = agent_result["degraded_reason"]
    now = _now()
    total_latency = round((time.perf_counter() - started) * 1000)

    if record_id is None:
        cursor = connection.execute(
            "INSERT INTO processing_records "
            "(trace_id, user_id, original_query, intent, status, request_params_json, created_at) "
            "VALUES (?, ?, ?, ?, 'processing', ?, ?)",
            (trace_id, user_id, message, intent, _json(params), now),
        )
        record_id = cursor.lastrowid
        _add_event(connection, trace_id, "request_received", "assistant_message", {"message": message})

    result["record_id"] = record_id
    connection.execute(
        "UPDATE processing_records SET status = 'completed', request_params_json = ?, result_json = ?, "
        "final_answer = ?, total_latency_ms = ?, completed_at = ? WHERE id = ? AND user_id = ?",
        (_json(params), _json(result), answer, total_latency, now, record_id, user_id),
    )
    _add_event(connection, trace_id, "slot_validation", "parameters_validated", params)
    _add_event(
        connection,
        trace_id,
        "metric_query",
        "metrics_calculated",
        params,
        {"raw": metrics["raw"], "calculated": metrics["calculated"]},
        metrics_latency,
    )
    if diagnosis is not None:
        _add_event(
            connection,
            trace_id,
            "anomaly_diagnosis",
            "rules_evaluated",
            params,
            {"anomalies": diagnosis["anomalies"]},
        )
    _add_event(
        connection,
        trace_id,
        "agent_orchestration",
        "react_agent_completed",
        output_value={
            "degraded": agent_result["degraded"],
            "tool_calls": agent_result["tool_calls"],
        },
    )
    _add_event(
        connection, trace_id, "record_saved", "processing_record_saved", output_value={"record_id": record_id}
    )
    connection.commit()
    return trace_id, result


def continue_clarification(
    connection: sqlite3.Connection,
    user_id: int,
    session_id: str,
    message: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, object]]:
    session = connection.execute(
        "SELECT c.*, r.trace_id FROM clarification_sessions c "
        "JOIN processing_records r ON r.id = c.record_id "
        "WHERE c.id = ? AND c.user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if session is None or session["status"] not in ("active", "expired"):
        raise ClarificationNotFound
    if session["status"] == "expired" or session["expires_at"] <= _now():
        now = _now()
        connection.execute(
            "UPDATE clarification_sessions SET status = 'expired', updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        connection.execute(
            "UPDATE processing_records SET status = 'failed', error_code = ?, completed_at = ? WHERE id = ?",
            ("CLARIFICATION_SESSION_EXPIRED", now, session["record_id"]),
        )
        connection.commit()
        raise ClarificationExpired

    slots = fill_slots_from_message(connection, message, json.loads(session["slots_json"]))
    missing = _missing(slots)
    _add_event(
        connection, session["trace_id"], "request_received", "clarification_message", {"message": message}
    )
    if missing:
        now = _now()
        connection.execute(
            "UPDATE clarification_sessions SET slots_json = ?, missing_slots_json = ?, updated_at = ? "
            "WHERE id = ?",
            (_json(slots), _json(missing), now, session_id),
        )
        connection.execute(
            "UPDATE processing_records SET request_params_json = ? WHERE id = ?",
            (_json(slots), session["record_id"]),
        )
        _add_event(
            connection,
            session["trace_id"],
            "slot_validation",
            "parameters_missing",
            slots,
            {"missing_fields": missing},
        )
        connection.commit()
        return session["trace_id"], {
            "status": "waiting_clarification",
            "record_id": session["record_id"],
            "session_id": session_id,
            "intent": session["intent"],
            "known_params": {key: value for key, value in slots.items() if value is not None},
            "missing_fields": missing,
            "clarification_question": _question(missing),
        }

    connection.execute(
        "UPDATE clarification_sessions SET slots_json = ?, missing_slots_json = '[]', "
        "status = 'completed', updated_at = ? WHERE id = ?",
        (_json(slots), _now(), session_id),
    )
    return execute_query(
        connection,
        user_id,
        message,
        int(slots["campaign_id"]),
        str(slots["start_date"]),
        str(slots["end_date"]),
        session["intent"],
        session["record_id"],
        session["trace_id"],
        conversation_history,
    )


def list_records(
    connection: sqlite3.Connection, user_id: int, page: int, page_size: int
) -> dict[str, object]:
    total = connection.execute(
        "SELECT COUNT(*) FROM processing_records WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT r.id, r.trace_id, r.original_query, r.intent, r.status, "
        "r.final_answer AS answer_summary, f.rating AS feedback, r.created_at, r.completed_at "
        "FROM processing_records r LEFT JOIN feedback f "
        "ON f.record_id = r.id AND f.user_id = r.user_id WHERE r.user_id = ? "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
        (user_id, page_size, (page - 1) * page_size),
    ).fetchall()
    items = [dict(row) for row in rows]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def get_record(
    connection: sqlite3.Connection, record_id: int, user_id: int
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT r.id, r.trace_id, r.original_query, r.intent, r.status, r.request_params_json, "
        "r.result_json, r.final_answer, r.error_code, r.total_latency_ms, f.rating AS feedback, "
        "r.created_at, r.completed_at FROM processing_records r LEFT JOIN feedback f "
        "ON f.record_id = r.id AND f.user_id = r.user_id WHERE r.id = ? AND r.user_id = ?",
        (record_id, user_id),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["request_params"] = json.loads(record.pop("request_params_json"))
    result_json = record.pop("result_json")
    record["result"] = json.loads(result_json) if result_json else None
    return record


def save_feedback(
    connection: sqlite3.Connection, record_id: int, user_id: int, rating: str
) -> dict[str, object] | None:
    record = connection.execute(
        "SELECT status FROM processing_records WHERE id = ? AND user_id = ?",
        (record_id, user_id),
    ).fetchone()
    if record is None:
        return None
    if record["status"] != "completed":
        raise ValueError("只能评价已完成的处理记录。")

    now = _now()
    connection.execute(
        "INSERT INTO feedback (record_id, user_id, rating, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(record_id, user_id) DO UPDATE SET "
        "rating = excluded.rating, updated_at = excluded.updated_at",
        (record_id, user_id, rating, now, now),
    )
    connection.commit()
    return {"record_id": record_id, "rating": rating, "updated_at": now}
