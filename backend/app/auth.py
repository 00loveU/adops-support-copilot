from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta


PBKDF2_ITERATIONS = 600_000
SESSION_HOURS = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError):
        return False


def create_session(connection: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(UTC)
    connection.execute(
        "INSERT INTO auth_sessions (user_id, token_hash, expires_at, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            user_id,
            token_hash,
            (now + timedelta(hours=SESSION_HOURS)).isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    connection.commit()
    return token


def user_for_token(connection: sqlite3.Connection, token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(UTC).isoformat()
    row = connection.execute(
        "SELECT u.id, u.username, u.display_name, u.role "
        "FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active'",
        (token_hash, now),
    ).fetchone()
    if row:
        connection.execute(
            "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash)
        )
        connection.commit()
    return dict(row) if row else None


def delete_session(connection: sqlite3.Connection, token: str | None) -> None:
    if token:
        connection.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        connection.commit()
