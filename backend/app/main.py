from __future__ import annotations

import sqlite3
import os
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
    return ok({"items": list_batches(connection)})


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
