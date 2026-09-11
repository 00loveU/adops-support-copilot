from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth import create_session, delete_session, user_for_token, verify_password
from backend.app.database import (
    DEFAULT_DB_PATH,
    connect,
    ensure_schema,
    initialize_database,
    list_advertisers,
    list_campaigns,
)
from backend.app.conversations import (
    ConversationNotFound,
    conversation_for_clarification,
    get_conversation,
    list_conversations,
    recent_messages,
    save_exchange,
)
from backend.app.metrics import get_campaign_metrics
from backend.app.evaluation import compare_batches, create_batch, get_batch, list_batches, run_batch
from backend.app.records import (
    ClarificationExpired,
    ClarificationNotFound,
    continue_clarification,
    create_clarification,
    detect_intent,
    execute_query,
    execute_rule_query,
    fill_slots_from_message,
    get_record,
    list_records,
    save_feedback,
)


if not DEFAULT_DB_PATH.exists():
    initialize_database()
else:
    ensure_schema()

app = FastAPI(title="AdOps Support Copilot API", version="0.1.0")


class LoginRequest(BaseModel):
    username: str
    password: str


class QueryContext(BaseModel):
    advertiser_id: int | None = None
    campaign_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None


class AssistantMessageRequest(BaseModel):
    message: str
    context: QueryContext
    conversation_id: str | None = None


class ClarificationMessageRequest(BaseModel):
    message: str


class FeedbackRequest(BaseModel):
    rating: Literal["helpful", "not_helpful"]


class EvaluationRequest(BaseModel):
    name: str = "离线回归评估"


class EvaluationCandidateRequest(BaseModel):
    category: Literal["metric", "diagnosis", "retrieval", "clarification"]
    expected_intent: Literal["metric_query", "anomaly_diagnosis", "rule_qa", "unknown"]
    expected_tool: str | None = None
    expected_result: dict[str, object] = Field(default_factory=dict)
    notes: str = ""


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


@app.exception_handler(ApiError)
def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return error(exc.status_code, exc.code, exc.message)


def get_db() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def ok(data: object, trace_id: str | None = None) -> dict[str, object | None]:
    return {"data": data, "trace_id": trace_id, "error": None}


def error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "data": None,
            "trace_id": None,
            "error": {"code": code, "message": message, "details": {}},
        },
    )


def save_conversation_result(
    connection: sqlite3.Connection,
    user_id: int,
    user_message: str,
    result: dict[str, object],
    conversation_id: str | None,
) -> None:
    assistant_message = str(
        result.get("answer") or result.get("clarification_question") or "任务已处理。"
    )
    result["conversation_id"] = save_exchange(
        connection,
        user_id,
        user_message,
        assistant_message,
        int(result["record_id"]),
        conversation_id,
    )


def require_user(
    request: Request,
    connection: sqlite3.Connection = Depends(get_db),
) -> dict[str, object]:
    user = user_for_token(connection, request.cookies.get("session_token"))
    if user is None:
        raise ApiError(401, "AUTH_REQUIRED", "请先登录。")
    return user


def require_admin(user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    if user["role"] != "admin":
        raise ApiError(403, "ADMIN_REQUIRED", "需要管理员权限。")
    return user


def admin_overview_data(connection: sqlite3.Connection) -> dict[str, object]:
    business_timezone = timezone(timedelta(hours=8))
    local_today = datetime.now(business_timezone).date()
    start_local = datetime.combine(local_today, time.min, business_timezone)
    start = start_local.astimezone(UTC).isoformat()
    end = (start_local + timedelta(days=1)).astimezone(UTC).isoformat()
    totals = dict(connection.execute(
        "SELECT COUNT(*) AS requests, "
        "COALESCE(SUM(CASE WHEN status = 'completed' AND COALESCE(json_extract(result_json, '$.degraded'), 0) = 0 THEN 1 ELSE 0 END), 0) AS completed, "
        "COALESCE(SUM(CASE WHEN status = 'completed' AND json_extract(result_json, '$.degraded') = 1 THEN 1 ELSE 0 END), 0) AS degraded, "
        "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed, "
        "COALESCE(ROUND(AVG(CASE WHEN status = 'completed' THEN total_latency_ms END)), 0) AS average_latency_ms "
        "FROM processing_records WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone())
    intent_counts = {name: 0 for name in ("metric_query", "anomaly_diagnosis", "rule_qa", "unknown")}
    for row in connection.execute(
        "SELECT intent, COUNT(*) AS count FROM processing_records "
        "WHERE created_at >= ? AND created_at < ? GROUP BY intent", (start, end)
    ):
        intent_counts[row["intent"] or "unknown"] = row["count"]
    feedback = dict(connection.execute(
        "SELECT COALESCE(SUM(CASE WHEN f.rating = 'helpful' THEN 1 ELSE 0 END), 0) AS helpful, "
        "COALESCE(SUM(CASE WHEN f.rating = 'not_helpful' THEN 1 ELSE 0 END), 0) AS not_helpful "
        "FROM processing_records r LEFT JOIN feedback f ON f.record_id = r.id "
        "WHERE r.created_at >= ? AND r.created_at < ?", (start, end)
    ).fetchone())
    conversations = connection.execute(
        "SELECT COUNT(*) FROM conversations WHERE created_at >= ? AND created_at < ?", (start, end)
    ).fetchone()[0]
    batches = list_batches(connection)
    return {
        "date": local_today.isoformat(),
        "timezone": "Asia/Shanghai",
        "totals": {**totals, "conversations": conversations},
        "intent_counts": intent_counts,
        "feedback": feedback,
        "latest_evaluation": batches[0] if batches else None,
    }


def _read_json(value: str | None) -> object | None:
    return json.loads(value) if value else None


def _trace_status(record: sqlite3.Row) -> str:
    result = _read_json(record["result_json"])
    if isinstance(result, dict) and result.get("degraded") is True:
        return "degraded"
    return str(record["status"])


def _candidate_data(row: sqlite3.Row) -> dict[str, object]:
    item = dict(row)
    for source, target in (
        ("input_json", "input"),
        ("actual_json", "actual"),
        ("expected_result_json", "expected_result"),
    ):
        item[target] = _read_json(item.pop(source))
    return item


@app.get("/api/health")
def health() -> dict[str, object | None]:
    return ok({"status": "ok"})


@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response, connection: sqlite3.Connection = Depends(get_db)):
    user = connection.execute(
        "SELECT id, username, display_name, password_hash, role, status FROM users WHERE username = ?",
        (body.username,),
    ).fetchone()
    if user is None or not verify_password(body.password, user["password_hash"]):
        return error(401, "INVALID_CREDENTIALS", "用户名或密码错误。")
    if user["status"] != "active":
        return error(403, "ACCOUNT_DISABLED", "账号已停用。")

    token = create_session(connection, user["id"])
    response.set_cookie(
        "session_token",
        token,
        max_age=8 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE") == "1",
    )
    return ok({"user": {key: user[key] for key in ("id", "username", "display_name", "role")}})


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, connection: sqlite3.Connection = Depends(get_db)):
    delete_session(connection, request.cookies.get("session_token"))
    response.delete_cookie("session_token")
    return ok({"logged_out": True})


@app.get("/api/auth/me")
def current_user(user: dict[str, object] = Depends(require_user)):
    return ok({"user": user})


@app.get("/api/advertisers")
def advertisers(
    q: str = "",
    status: str | None = Query(default=None, pattern="^(active|inactive)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    connection: sqlite3.Connection = Depends(get_db),
    _user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None]:
    items = list_advertisers(connection)
    if q:
        needle = q.casefold()
        items = [
            item
            for item in items
            if needle in str(item["advertiser_code"]).casefold()
            or needle in str(item["name"]).casefold()
        ]
    if status:
        items = [item for item in items if item["status"] == status]
    total = len(items)
    start = (page - 1) * page_size
    return ok({"items": items[start : start + page_size], "page": page, "page_size": page_size, "total": total})


@app.get("/api/advertisers/{advertiser_id}/campaigns", response_model=None)
def campaigns(
    advertiser_id: int,
    status: str | None = Query(default=None, pattern="^(draft|active|paused|ended)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    connection: sqlite3.Connection = Depends(get_db),
    _user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    advertiser = connection.execute("SELECT 1 FROM advertisers WHERE id = ?", (advertiser_id,)).fetchone()
    if advertiser is None:
        return error(404, "ADVERTISER_NOT_FOUND", f"未找到广告主 {advertiser_id}。")
    items = list_campaigns(connection, advertiser_id)
    if status:
        items = [item for item in items if item["status"] == status]
    total = len(items)
    start = (page - 1) * page_size
    return ok({"items": items[start : start + page_size], "page": page, "page_size": page_size, "total": total})


@app.get("/api/campaigns/{campaign_id}/metrics", response_model=None)
def campaign_metrics(
    campaign_id: int,
    start_date: str,
    end_date: str,
    connection: sqlite3.Connection = Depends(get_db),
    _user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    campaign = connection.execute(
        "SELECT campaign_code FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    if campaign is None:
        return error(404, "CAMPAIGN_NOT_FOUND", f"未找到广告计划 {campaign_id}。")
    try:
        return ok(get_campaign_metrics(connection, campaign["campaign_code"], start_date, end_date))
    except ValueError as exc:
        return error(400, "VALIDATION_ERROR", str(exc))


@app.post("/api/assistant/messages", response_model=None)
def assistant_message(
    body: AssistantMessageRequest,
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    try:
        history = recent_messages(
            connection, body.conversation_id, int(user["id"]), limit=8
        )
        intent = detect_intent(body.message)
        if intent == "rule_qa":
            trace_id, result = execute_rule_query(
                connection, int(user["id"]), body.message, history
            )
            save_conversation_result(
                connection, int(user["id"]), body.message, result, body.conversation_id
            )
            return ok(result, trace_id)
        slots = fill_slots_from_message(
            connection,
            body.message,
            {
                "campaign_id": body.context.campaign_id,
                "start_date": body.context.start_date,
                "end_date": body.context.end_date,
            },
        )
        if any(slots.get(name) is None for name in ("campaign_id", "start_date", "end_date")):
            trace_id, result = create_clarification(
                connection, int(user["id"]), body.message, slots, intent
            )
            save_conversation_result(
                connection, int(user["id"]), body.message, result, body.conversation_id
            )
            return ok(result, trace_id)
        trace_id, result = execute_query(
            connection,
            int(user["id"]),
            body.message,
            int(slots["campaign_id"]),
            str(slots["start_date"]),
            str(slots["end_date"]),
            intent,
            conversation_history=history,
        )
        save_conversation_result(
            connection, int(user["id"]), body.message, result, body.conversation_id
        )
        return ok(result, trace_id)
    except ConversationNotFound:
        return error(404, "CONVERSATION_NOT_FOUND", "未找到当前用户的会话。")
    except LookupError as exc:
        return error(404, "CAMPAIGN_NOT_FOUND", str(exc))
    except ValueError as exc:
        return error(400, "VALIDATION_ERROR", str(exc))


@app.post("/api/assistant/sessions/{session_id}/messages", response_model=None)
def clarification_message(
    session_id: str,
    body: ClarificationMessageRequest,
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    try:
        conversation_id = conversation_for_clarification(
            connection, session_id, int(user["id"])
        )
        history = recent_messages(connection, conversation_id, int(user["id"]), limit=8)
        trace_id, result = continue_clarification(
            connection, int(user["id"]), session_id, body.message, history
        )
        save_conversation_result(
            connection, int(user["id"]), body.message, result, conversation_id
        )
        return ok(result, trace_id)
    except ClarificationNotFound:
        return error(404, "CLARIFICATION_SESSION_NOT_FOUND", "未找到可继续的参数澄清会话。")
    except ClarificationExpired:
        return error(400, "CLARIFICATION_SESSION_EXPIRED", "参数澄清会话已过期，请重新发起查询。")
    except LookupError as exc:
        return error(404, "CAMPAIGN_NOT_FOUND", str(exc))
    except ValueError as exc:
        return error(400, "VALIDATION_ERROR", str(exc))


@app.get("/api/conversations")
def conversations(
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None]:
    return ok({"items": list_conversations(connection, int(user["id"]))})


@app.get("/api/conversations/{conversation_id}", response_model=None)
def conversation_detail(
    conversation_id: str,
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    conversation = get_conversation(connection, conversation_id, int(user["id"]))
    if conversation is None:
        return error(404, "CONVERSATION_NOT_FOUND", "未找到当前用户的会话。")
    return ok(conversation)


@app.get("/api/records")
def records(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None]:
    return ok(list_records(connection, int(user["id"]), page, page_size))


@app.get("/api/records/{record_id}", response_model=None)
def record_detail(
    record_id: int,
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    record = get_record(connection, record_id, int(user["id"]))
    if record is None:
        return error(404, "RECORD_NOT_FOUND", f"未找到处理记录 {record_id}。")
    return ok(record, str(record["trace_id"]))


@app.post("/api/records/{record_id}/feedback", response_model=None)
def submit_feedback(
    record_id: int,
    body: FeedbackRequest,
    connection: sqlite3.Connection = Depends(get_db),
    user: dict[str, object] = Depends(require_user),
) -> dict[str, object | None] | JSONResponse:
    try:
        result = save_feedback(connection, record_id, int(user["id"]), body.rating)
    except ValueError as exc:
        return error(409, "RECORD_NOT_COMPLETED", str(exc))
    if result is None:
        return error(404, "RECORD_NOT_FOUND", f"未找到处理记录 {record_id}。")
    return ok(result)


@app.get("/api/admin/overview")
def admin_overview(
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None]:
    return ok(admin_overview_data(connection))


@app.get("/api/admin/knowledge-sources")
def admin_knowledge_sources(
    category: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None]:
    where = " WHERE category = ?" if category else ""
    params: tuple[object, ...] = (category,) if category else ()
    total = connection.execute(
        f"SELECT COUNT(*) FROM knowledge_sources{where}", params
    ).fetchone()[0]
    rows = connection.execute(
        f"SELECT id, source_code, title, category, version, created_at "
        f"FROM knowledge_sources{where} ORDER BY source_code LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        chunks = connection.execute(
            "SELECT id, section_title, content, chunk_order FROM knowledge_chunks "
            "WHERE source_id = ? ORDER BY chunk_order",
            (row["id"],),
        ).fetchall()
        item["chunks"] = [dict(chunk) for chunk in chunks]
        items.append(item)
    return ok({"items": items, "page": page, "page_size": page_size, "total": total})


@app.get("/api/admin/traces")
def admin_traces(
    status: Literal["all", "completed", "degraded", "failed"] = "all",
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None]:
    conditions = {
        "all": "",
        "completed": "WHERE r.status = 'completed' AND COALESCE(json_extract(r.result_json, '$.degraded'), 0) = 0",
        "degraded": "WHERE json_extract(r.result_json, '$.degraded') = 1",
        "failed": "WHERE r.status = 'failed'",
    }
    rows = connection.execute(
        "SELECT r.id, r.trace_id, r.original_query, r.intent, r.status, r.result_json, "
        "r.total_latency_ms, r.created_at, u.username, u.display_name "
        "FROM processing_records r JOIN users u ON u.id = r.user_id "
        f"{conditions[status]} ORDER BY r.id DESC LIMIT 100"
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["status"] = _trace_status(row)
        item["degraded"] = item["status"] == "degraded"
        item.pop("result_json")
        items.append(item)
    return ok({"items": items, "total": len(items), "status": status})


@app.get("/api/admin/traces/{trace_id}", response_model=None)
def admin_trace_detail(
    trace_id: str,
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    row = connection.execute(
        "SELECT r.*, u.username, u.display_name FROM processing_records r "
        "JOIN users u ON u.id = r.user_id WHERE r.trace_id = ?",
        (trace_id,),
    ).fetchone()
    if row is None:
        return error(404, "TRACE_NOT_FOUND", f"未找到 Trace {trace_id}。")

    item = dict(row)
    item["status"] = _trace_status(row)
    item["degraded"] = item["status"] == "degraded"
    for source, target in (
        ("request_params_json", "request_params"),
        ("result_json", "result"),
    ):
        item[target] = _read_json(item.pop(source))
    feedback = connection.execute(
        "SELECT rating FROM feedback WHERE record_id = ?", (row["id"],)
    ).fetchone()
    item["feedback"] = feedback["rating"] if feedback else None
    candidate = connection.execute(
        "SELECT id, status FROM evaluation_candidates WHERE record_id = ?", (row["id"],)
    ).fetchone()
    item["evaluation_candidate"] = dict(candidate) if candidate else None
    events = connection.execute(
        "SELECT sequence_no, stage, event_type, status, input_json, output_json, "
        "latency_ms, error_code, created_at FROM trace_events "
        "WHERE trace_id = ? ORDER BY sequence_no",
        (trace_id,),
    ).fetchall()
    item["events"] = [
        {
            **{key: event[key] for key in (
                "sequence_no", "stage", "event_type", "status", "latency_ms",
                "error_code", "created_at",
            )},
            "input": _read_json(event["input_json"]),
            "output": _read_json(event["output_json"]),
        }
        for event in events
    ]
    return ok(item, trace_id)


@app.post("/api/admin/traces/{trace_id}/evaluation-candidate", status_code=201, response_model=None)
def create_evaluation_candidate(
    trace_id: str,
    connection: sqlite3.Connection = Depends(get_db),
    admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    record = connection.execute(
        "SELECT r.*, (SELECT rating FROM feedback f WHERE f.record_id = r.id) AS feedback "
        "FROM processing_records r WHERE r.trace_id = ?",
        (trace_id,),
    ).fetchone()
    if record is None:
        return error(404, "TRACE_NOT_FOUND", f"未找到 Trace {trace_id}。")
    existing = connection.execute(
        "SELECT id, status FROM evaluation_candidates WHERE record_id = ?", (record["id"],)
    ).fetchone()
    if existing:
        return error(409, "CANDIDATE_ALREADY_EXISTS", f"该 Trace 已加入候选 #{existing['id']}。")

    effective_status = _trace_status(record)
    if effective_status not in ("degraded", "failed") and record["feedback"] != "not_helpful":
        return error(409, "TRACE_NOT_ELIGIBLE", "只有失败、降级或收到‘没帮助’反馈的 Trace 可以加入候选。")

    params = _read_json(record["request_params_json"]) or {}
    context: dict[str, object] = {}
    if isinstance(params, dict):
        if campaign_id := params.get("campaign_id"):
            campaign = connection.execute(
                "SELECT campaign_code FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if campaign:
                context["campaign_code"] = campaign["campaign_code"]
        for key in ("start_date", "end_date"):
            if params.get(key):
                context[key] = params[key]
    category = {
        "metric_query": "metric",
        "anomaly_diagnosis": "diagnosis",
        "rule_qa": "retrieval",
    }.get(record["intent"], "clarification")
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        "INSERT INTO evaluation_candidates "
        "(record_id, created_by, category, input_json, actual_json, expected_intent, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["id"], int(admin["id"]), category,
            json.dumps({"message": record["original_query"], "context": context}, ensure_ascii=False),
            record["result_json"], record["intent"] or "unknown", now, now,
        ),
    )
    connection.commit()
    return ok({"id": cursor.lastrowid, "status": "draft"}, trace_id)


@app.get("/api/admin/evaluation-candidates")
def evaluation_candidates(
    status: Literal["all", "draft", "promoted"] = "all",
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None]:
    where = "" if status == "all" else "WHERE c.status = ?"
    params: tuple[object, ...] = () if status == "all" else (status,)
    rows = connection.execute(
        "SELECT c.*, r.trace_id, r.original_query, u.display_name AS source_user_name "
        "FROM evaluation_candidates c JOIN processing_records r ON r.id = c.record_id "
        "JOIN users u ON u.id = r.user_id "
        f"{where} ORDER BY c.id DESC",
        params,
    ).fetchall()
    items = [_candidate_data(row) for row in rows]
    return ok({"items": items, "total": len(items), "status": status})


@app.put("/api/admin/evaluation-candidates/{candidate_id}", response_model=None)
def update_evaluation_candidate(
    candidate_id: int,
    body: EvaluationCandidateRequest,
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    candidate = connection.execute(
        "SELECT status FROM evaluation_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if candidate is None:
        return error(404, "CANDIDATE_NOT_FOUND", f"未找到评估候选 {candidate_id}。")
    if candidate["status"] != "draft":
        return error(409, "CANDIDATE_ALREADY_PROMOTED", "已加入正式评估集的候选不能修改。")
    connection.execute(
        "UPDATE evaluation_candidates SET category = ?, expected_intent = ?, expected_tool = ?, "
        "expected_result_json = ?, notes = ?, updated_at = ? WHERE id = ?",
        (
            body.category, body.expected_intent, (body.expected_tool or "").strip() or None,
            json.dumps(body.expected_result, ensure_ascii=False), body.notes.strip(),
            datetime.now(UTC).isoformat(), candidate_id,
        ),
    )
    connection.commit()
    return ok({"id": candidate_id, "status": "draft"})


@app.post("/api/admin/evaluation-candidates/{candidate_id}/promote", response_model=None)
def promote_evaluation_candidate(
    candidate_id: int,
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    candidate = connection.execute(
        "SELECT * FROM evaluation_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if candidate is None:
        return error(404, "CANDIDATE_NOT_FOUND", f"未找到评估候选 {candidate_id}。")
    if candidate["status"] != "draft":
        return error(409, "CANDIDATE_ALREADY_PROMOTED", "该候选已经加入正式评估集。")
    expected_result = _read_json(candidate["expected_result_json"]) or {}
    if not candidate["expected_tool"] and not expected_result:
        return error(409, "CANDIDATE_INCOMPLETE", "请至少填写预期工具或预期关键结果。")
    running = connection.execute(
        "SELECT 1 FROM evaluation_batches WHERE status IN ('pending', 'running') LIMIT 1"
    ).fetchone()
    if running:
        return error(409, "EVALUATION_RUNNING", "评估运行期间不能修改正式评估集。")

    expected = {**expected_result, "intent": candidate["expected_intent"]}
    if candidate["expected_tool"]:
        expected["expected_tool"] = candidate["expected_tool"]
    now = datetime.now(UTC).isoformat()
    case_code = f"EVAL-CUSTOM-{candidate_id:03d}"
    cursor = connection.execute(
        "INSERT INTO evaluation_cases "
        "(case_code, category, input_json, expected_json, enabled, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (
            case_code, candidate["category"], candidate["input_json"],
            json.dumps(expected, ensure_ascii=False), now,
        ),
    )
    connection.execute(
        "UPDATE evaluation_candidates SET status = 'promoted', promoted_case_id = ?, "
        "promoted_at = ?, updated_at = ? WHERE id = ?",
        (cursor.lastrowid, now, now, candidate_id),
    )
    connection.commit()
    return ok({"id": candidate_id, "status": "promoted", "case_code": case_code})


@app.post("/api/admin/evaluations", status_code=201, response_model=None)
def start_evaluation(
    body: EvaluationRequest,
    background_tasks: BackgroundTasks,
    connection: sqlite3.Connection = Depends(get_db),
    admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    try:
        batch = create_batch(connection, body.name, int(admin["id"]))
    except RuntimeError:
        return error(409, "EVALUATION_ALREADY_RUNNING", "已有评估批次正在运行。")
    database_path = connection.execute("PRAGMA database_list").fetchone()["file"]
    background_tasks.add_task(run_batch, database_path, int(batch["batch_id"]))
    return ok(batch)


@app.get("/api/admin/evaluations")
def evaluations(
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None]:
    case_count = connection.execute(
        "SELECT COUNT(*) FROM evaluation_cases WHERE enabled = 1"
    ).fetchone()[0]
    return ok({"items": list_batches(connection), "case_count": case_count})


@app.get("/api/admin/evaluations/compare", response_model=None)
def evaluation_comparison(
    left: int,
    right: int,
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    comparison = compare_batches(connection, left, right)
    if comparison is None:
        return error(404, "EVALUATION_NOT_FOUND", "未找到需要比较的评估批次。")
    return ok(comparison)


@app.get("/api/admin/evaluations/{batch_id}", response_model=None)
def evaluation_detail(
    batch_id: int,
    connection: sqlite3.Connection = Depends(get_db),
    _admin: dict[str, object] = Depends(require_admin),
) -> dict[str, object | None] | JSONResponse:
    batch = get_batch(connection, batch_id)
    if batch is None:
        return error(404, "EVALUATION_NOT_FOUND", f"未找到评估批次 {batch_id}。")
    return ok(batch)
