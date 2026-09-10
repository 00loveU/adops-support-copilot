from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "adops.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'admin')),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advertisers (
    id INTEGER PRIMARY KEY,
    advertiser_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    campaign_code TEXT NOT NULL UNIQUE,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'ended')),
    daily_budget_cents INTEGER NOT NULL CHECK (daily_budget_cents >= 0),
    start_date TEXT NOT NULL,
    end_date TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_advertiser_status
ON campaigns(advertiser_id, status);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    metric_date TEXT NOT NULL,
    impressions INTEGER NOT NULL CHECK (impressions >= 0),
    clicks INTEGER NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    conversions INTEGER NOT NULL CHECK (conversions >= 0 AND conversions <= clicks),
    spend_cents INTEGER NOT NULL CHECK (spend_cents >= 0),
    revenue_cents INTEGER NOT NULL CHECK (revenue_cents >= 0),
    UNIQUE(campaign_id, metric_date)
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    id INTEGER PRIMARY KEY,
    source_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES knowledge_sources(id),
    section_title TEXT NOT NULL,
    content TEXT NOT NULL,
    chunk_order INTEGER NOT NULL,
    intent_tags TEXT,
    UNIQUE(source_id, chunk_order)
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    chunk_id UNINDEXED,
    source_code UNINDEXED,
    title,
    section_title,
    content,
    keywords,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS processing_records (
    id INTEGER PRIMARY KEY,
    trace_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    original_query TEXT NOT NULL,
    intent TEXT CHECK (intent IN ('metric_query', 'anomaly_diagnosis', 'rule_qa', 'unknown')),
    status TEXT NOT NULL CHECK (status IN ('processing', 'waiting_clarification', 'completed', 'degraded', 'failed')),
    request_params_json TEXT NOT NULL,
    result_json TEXT,
    final_answer TEXT,
    error_code TEXT,
    total_latency_ms INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_processing_records_user_created
ON processing_records(user_id, created_at);

CREATE TABLE IF NOT EXISTS clarification_sessions (
    id TEXT PRIMARY KEY,
    record_id INTEGER NOT NULL UNIQUE REFERENCES processing_records(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    intent TEXT NOT NULL,
    slots_json TEXT NOT NULL,
    missing_slots_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'expired', 'cancelled')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY,
    trace_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    input_json TEXT,
    output_json TEXT,
    latency_ms INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(trace_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES processing_records(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating TEXT NOT NULL CHECK (rating IN ('helpful', 'not_helpful')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(record_id, user_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
ON conversations(user_id, updated_at);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    record_id INTEGER REFERENCES processing_records(id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation
ON conversation_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS evaluation_cases (
    id INTEGER PRIMARY KEY,
    case_code TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    input_json TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_batches (
    id INTEGER PRIMARY KEY,
    batch_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    started_by INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    total_cases INTEGER NOT NULL DEFAULT 0,
    passed_cases INTEGER NOT NULL DEFAULT 0,
    failed_cases INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_results (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES evaluation_batches(id),
    case_id INTEGER NOT NULL REFERENCES evaluation_cases(id),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    actual_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    failure_reason TEXT,
    latency_ms INTEGER NOT NULL,
    trace_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_batches_created
ON evaluation_batches(created_at DESC);
"""


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_schema(path: str | Path = DEFAULT_DB_PATH) -> None:
    connection = connect(path)
    try:
        connection.executescript(SCHEMA)
        _replace_evaluation_cases(connection)
        connection.commit()
    finally:
        connection.close()


def parse_knowledge_document(path: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    text = path.read_text(encoding="utf-8")
    try:
        _, frontmatter, body = text.split("---", 2)
    except ValueError as exc:
        raise ValueError(f"知识文档缺少 frontmatter: {path.name}") from exc

    metadata: dict[str, str] = {}
    for line in frontmatter.strip().splitlines():
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')

    required = {"source_code", "title", "category", "version", "keywords"}
    if missing := required - metadata.keys():
        raise ValueError(f"知识文档 {path.name} 缺少字段: {', '.join(sorted(missing))}")

    chunks = []
    for part in re.split(r"^##\s+", body, flags=re.MULTILINE)[1:]:
        section_title, content = part.split("\n", 1)
        chunks.append((section_title.strip(), content.strip()))
    if not chunks:
        raise ValueError(f"知识文档没有二级标题分块: {path.name}")
    return metadata, chunks


def _replace_csv(connection: sqlite3.Connection, table: str, path: Path) -> None:
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        ([row[column] or None for column in columns] for row in rows),
    )


def _replace_knowledge(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM knowledge_fts")
    connection.execute("DELETE FROM knowledge_chunks")
    connection.execute("DELETE FROM knowledge_sources")

    for path in sorted((DATA_DIR / "knowledge").glob("*.md")):
        metadata, chunks = parse_knowledge_document(path)
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        cursor = connection.execute(
            "INSERT INTO knowledge_sources "
            "(source_code, title, category, version, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                metadata["source_code"],
                metadata["title"],
                metadata["category"],
                metadata["version"],
                content_hash,
                "2026-09-09T00:00:00Z",
            ),
        )
        source_id = cursor.lastrowid
        for order, (section_title, content) in enumerate(chunks, start=1):
            cursor = connection.execute(
                "INSERT INTO knowledge_chunks "
                "(source_id, section_title, content, chunk_order, intent_tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (source_id, section_title, content, order, metadata["keywords"]),
            )
            connection.execute(
                "INSERT INTO knowledge_fts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cursor.lastrowid,
                    metadata["source_code"],
                    metadata["title"],
                    section_title,
                    content,
                    metadata["keywords"],
                ),
            )


def _replace_evaluation_cases(connection: sqlite3.Connection) -> None:
    cases = json.loads((DATA_DIR / "evaluation_cases.json").read_text(encoding="utf-8"))
    for case in cases:
        connection.execute(
            "INSERT INTO evaluation_cases "
            "(case_code, category, input_json, expected_json, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(case_code) DO UPDATE SET category = excluded.category, "
            "input_json = excluded.input_json, expected_json = excluded.expected_json",
            (
                case["case_code"],
                case["category"],
                json.dumps(case["input"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(case["expected"], ensure_ascii=False, separators=(",", ":")),
                "2026-09-10T00:00:00Z",
            ),
        )


def initialize_database(path: str | Path = DEFAULT_DB_PATH) -> None:
    from backend.app.auth import hash_password

    connection = connect(path)
    try:
        connection.executescript(SCHEMA)
        demo_users = (
            (1, "operator1", "运营人员一", "Operator123!", "operator"),
            (2, "admin1", "系统管理员", "Admin123!", "admin"),
        )
        for user_id, username, display_name, password, role in demo_users:
            if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                continue
            connection.execute(
                "INSERT INTO users "
                "(id, username, display_name, password_hash, role, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    user_id,
                    username,
                    display_name,
                    hash_password(password),
                    role,
                    "2026-09-09T00:00:00Z",
                    "2026-09-09T00:00:00Z",
                ),
            )
        connection.execute("DELETE FROM daily_metrics")
        connection.execute("DELETE FROM campaigns")
        connection.execute("DELETE FROM advertisers")
        _replace_csv(connection, "advertisers", DATA_DIR / "advertisers.csv")
        _replace_csv(connection, "campaigns", DATA_DIR / "campaigns.csv")
        _replace_csv(connection, "daily_metrics", DATA_DIR / "daily_metrics.csv")
        _replace_knowledge(connection)
        _replace_evaluation_cases(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_advertisers(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT id, advertiser_code, name, industry, status "
        "FROM advertisers ORDER BY advertiser_code"
    ).fetchall()
    return [dict(row) for row in rows]


def list_campaigns(connection: sqlite3.Connection, advertiser_id: int) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT id, campaign_code, advertiser_id, name, status, daily_budget_cents, "
        "start_date, end_date FROM campaigns WHERE advertiser_id = ? ORDER BY campaign_code",
        (advertiser_id,),
    ).fetchall()
    return [dict(row) for row in rows]


if __name__ == "__main__":
    initialize_database()
    print(f"SQLite 初始化完成: {DEFAULT_DB_PATH}")
