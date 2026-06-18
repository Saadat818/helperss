"""Database backend switch for Helper.

SQLite remains the default. PostgreSQL is opt-in via HELPER_DB_BACKEND=postgres.
This module intentionally does not run migrations or create schemas.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Iterable


POSTGRES_BACKENDS = {"postgres", "postgresql", "pg"}

TABLE_NAMES = {
    "app_settings",
    "audit_logs",
    "cc_contact_likes",
    "cc_contacts",
    "cc_department_overrides",
    "cc_departments",
    "cc_directions",
    "cc_section_visits",
    "cs_categories",
    "cs_edges",
    "cs_nodes",
    "cs_scenarios",
    "cs_versions",
    "cs_views",
    "employee_board_posts",
    "manual_feedback",
    "myboard_boards",
    "myboard_views",
    "processed_webhook_events",
    "search_cache",
    "ticket_events",
    "ticket_sequence",
    "topic_changes",
    "topic_search_events",
    "topics",
    "trainer_answers",
    "trainer_audit_log",
    "trainer_categories",
    "trainer_feedback",
    "trainer_levels",
    "trainer_results",
    "trainer_scenario_tags",
    "trainer_scenario_versions",
    "trainer_scenarios",
    "trainer_steps",
    "trainer_tags",
    "trainer_user_progress",
    "trainer_visits",
    "user_profiles",
}

TABLES_WITH_ID = {
    name for name in TABLE_NAMES
    if name not in {
        "app_settings",
        "processed_webhook_events",
        "search_cache",
        "trainer_scenario_tags",
        "user_profiles",
    }
}


def backend_name() -> str:
    return (os.getenv("HELPER_DB_BACKEND") or "sqlite").strip().lower() or "sqlite"


def is_postgres_backend() -> bool:
    return backend_name() in POSTGRES_BACKENDS


def pg_schema() -> str:
    return (os.getenv("HELPER_PG_SCHEMA") or "helper").strip() or "helper"


def connect(sqlite_path: str = "topics.db", **sqlite_kwargs):
    if is_postgres_backend():
        return PostgresConnection()
    conn = sqlite3.connect(sqlite_path, **sqlite_kwargs)
    conn.row_factory = sqlite3.Row
    return conn


def health_check() -> dict:
    """Return a small PostgreSQL health snapshot without exposing secrets."""
    if not is_postgres_backend():
        return {"backend": "sqlite", "ok": True}

    checks = {}
    with connect() as conn:
        cur = conn.execute("SELECT 1")
        checks["select_1"] = cur.fetchone()[0]
        schema = pg_schema()
        for table in ("topics", "trainer_results", "cc_contacts"):
            row = conn.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()
            checks[table] = int(row[0])
    return {"backend": "postgres", "ok": True, "checks": checks}


class PostgresConnection:
    def __init__(self):
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "HELPER_DB_BACKEND=postgres requires psycopg2/psycopg2-binary"
            ) from exc

        password = os.getenv("HELPER_PG_PASSWORD")
        if not password:
            raise RuntimeError("HELPER_PG_PASSWORD is required for PostgreSQL backend")

        self._psycopg2 = psycopg2
        self._cursor_factory = DictCursor
        self._password = password
        self._local = threading.local()

    def _connect_new(self):
        return self._psycopg2.connect(
            host=os.getenv("HELPER_PG_HOST", "10.10.90.57"),
            port=int(os.getenv("HELPER_PG_PORT", "5432")),
            dbname=os.getenv("HELPER_PG_DB", "apo"),
            user=os.getenv("HELPER_PG_USER", "r_koledin"),
            password=self._password,
            cursor_factory=self._cursor_factory,
        )

    def _get_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(conn, "closed", False):
            conn = self._connect_new()
            self._local.conn = conn
        return conn

    def _close_current(self):
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None and not getattr(conn, "closed", False):
            conn.close()

    def cursor(self):
        return PostgresCursor(self._get_conn().cursor(), self)

    def execute(self, sql: str, params: Iterable | None = None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(conn, "closed", False):
            return None
        try:
            return conn.commit()
        finally:
            self._close_current()

    def rollback(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(conn, "closed", False):
            return None
        try:
            return conn.rollback()
        finally:
            self._close_current()

    def close(self):
        return self._close_current()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


class PostgresCursor:
    def __init__(self, cursor, owner: PostgresConnection):
        self._cursor = cursor
        self._owner = owner
        self.lastrowid = None

    def execute(self, sql: str, params: Iterable | None = None):
        translated = translate_sql(sql)
        translated = self._add_returning_id(translated)
        self._cursor.execute(translated, params)
        self._capture_lastrowid(translated)
        return self

    def executemany(self, sql: str, seq_of_params):
        self._cursor.executemany(translate_sql(sql), seq_of_params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def _capture_lastrowid(self, sql: str):
        if not re.search(r"\bRETURNING\s+id\b", sql, flags=re.IGNORECASE):
            return
        row = self._cursor.fetchone()
        if row is not None:
            self.lastrowid = row[0]

    def _add_returning_id(self, sql: str) -> str:
        if re.search(r"\bRETURNING\b", sql, flags=re.IGNORECASE):
            return sql
        if not re.match(r"^\s*INSERT\s+INTO\s+", sql, flags=re.IGNORECASE):
            return sql
        match = re.match(
            r"^\s*INSERT\s+INTO\s+(?:[a-zA-Z_][\w]*\.)?([a-zA-Z_][\w]*)\b",
            sql,
            flags=re.IGNORECASE,
        )
        if not match or match.group(1) not in TABLES_WITH_ID:
            return sql
        return sql.rstrip().rstrip(";") + " RETURNING id"


def translate_sql(sql: str) -> str:
    text = sql
    text = _translate_sqlite_functions(text)
    text = _translate_insert_or_replace(text)
    text = text.replace("?", "%s")
    text = _qualify_tables(text)
    return text


def _translate_sqlite_functions(sql: str) -> str:
    text = sql
    text = re.sub(
        r"\bunicode_casefold\(\s*([^)]+?)\s*\)",
        r"LOWER(\1::text)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bdigits_only\(\s*([^)]+?)\s*\)",
        r"regexp_replace(\1::text, '\\D', '', 'g')",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"datetime\(\s*['\"]now['\"]\s*,\s*['\"]-(\d+)\s+days['\"]\s*\)",
        lambda match: f"(CURRENT_TIMESTAMP - INTERVAL '{match.group(1)} days')",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("datetime('now')", "CURRENT_TIMESTAMP")
    text = text.replace('datetime("now")', "CURRENT_TIMESTAMP")
    text = re.sub(
        r"PRAGMA\s+table_info\(\s*([a-zA-Z_][\w]*)\s*\)",
        lambda match: (
            "SELECT ordinal_position - 1 AS cid, column_name AS name "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{pg_schema()}' AND table_name = '{match.group(1)}' "
            "ORDER BY ordinal_position"
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"datetime\(\s*timestamp\s*,\s*'\+1 hour'\s*\)",
        "(timestamp::timestamp + INTERVAL '1 hour')",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _translate_insert_or_replace(sql: str) -> str:
    text = sql
    if re.match(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+search_cache\b", text, re.IGNORECASE):
        return (
            "INSERT INTO search_cache (query, results) VALUES (?, ?) "
            "ON CONFLICT (query) DO UPDATE SET "
            "results = EXCLUDED.results, timestamp = CURRENT_TIMESTAMP"
        )
    if re.match(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+trainer_user_progress\b", text, re.IGNORECASE):
        cols_match = re.search(r"\(([^)]+)\)\s*VALUES", text, re.IGNORECASE | re.DOTALL)
        if not cols_match:
            return text
        cols = [c.strip() for c in cols_match.group(1).split(",")]
        updates = ", ".join(
            f"{col} = EXCLUDED.{col}"
            for col in cols
            if col not in {"user_id", "level_code"}
        )
        return re.sub(
            r"^\s*INSERT\s+OR\s+REPLACE\s+INTO",
            "INSERT INTO",
            text,
            flags=re.IGNORECASE,
        ).rstrip().rstrip(";") + (
            f" ON CONFLICT (user_id, level_code) DO UPDATE SET {updates}"
            if updates else " ON CONFLICT (user_id, level_code) DO NOTHING"
        )
    return text


def _qualify_tables(sql: str) -> str:
    schema = pg_schema()
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for index in range(0, len(parts), 2):
        text = parts[index]
        for table in sorted(TABLE_NAMES, key=len, reverse=True):
            text = re.sub(
                rf"(?<![\w.]){re.escape(table)}(?![\w])",
                f"{schema}.{table}",
                text,
            )
        parts[index] = text
    return "".join(parts)
