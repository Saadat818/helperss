import json
import os
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from db_backend import connect as db_connect, is_postgres_backend


class MyBoardManager:
    """Cache and render MyBoard schemas inside Helper.

    MyBoard stays the source of truth. Helper stores the latest normalized JSON
    so specialists can open boards even when the remote service is temporarily
    unavailable.
    """

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        if not is_postgres_backend():
            self._init_db()

    def _connect(self):
        conn = db_connect(self.db_path, check_same_thread=False, timeout=10.0)
        if not is_postgres_backend():
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS myboard_boards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    source_url TEXT DEFAULT '',
                    thumbnail_url TEXT DEFAULT '',
                    version TEXT DEFAULT '',
                    external_updated_at TEXT DEFAULT '',
                    synced_at TEXT DEFAULT '',
                    raw_json TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    last_error TEXT DEFAULT '',
                    view_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_myboard_status ON myboard_boards(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_myboard_category ON myboard_boards(category)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS myboard_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id INTEGER NOT NULL REFERENCES myboard_boards(id) ON DELETE CASCADE,
                    user_id TEXT DEFAULT '',
                    viewed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_myboard_views_board ON myboard_views(board_id)")
            conn.commit()

    # ------------------------------------------------------------------
    # Remote API
    # ------------------------------------------------------------------

    def sync_from_api(self, config: dict[str, Any] | None = None) -> dict:
        cfg = self._config(config or {})
        if not cfg["base_url"]:
            return {
                "success": False,
                "created": 0,
                "updated": 0,
                "errors": ["MYBOARD_API_BASE_URL is not configured"],
            }

        try:
            listing = self._request_json(cfg, cfg["list_path"])
        except Exception as e:
            return {"success": False, "created": 0, "updated": 0, "errors": [str(e)]}

        summaries = self._extract_board_summaries(listing)
        created = 0
        updated = 0
        errors: list[str] = []

        for summary in summaries:
            external_id = self._text(
                summary.get("id")
                or summary.get("_id")
                or summary.get("uuid")
                or summary.get("key")
                or summary.get("boardId")
            )
            if not external_id:
                errors.append("Skipped board without id")
                continue

            payload = summary
            if cfg["detail_path"]:
                try:
                    payload = self._request_json(
                        cfg,
                        cfg["detail_path"].format(
                            id=urllib.parse.quote(external_id, safe=""),
                            external_id=urllib.parse.quote(external_id, safe=""),
                        ),
                    )
                except Exception as e:
                    errors.append(f"{external_id}: {e}")

            saved = self.upsert_remote_board(external_id, payload, summary)
            if saved == "created":
                created += 1
            else:
                updated += 1

        return {
            "success": not errors,
            "created": created,
            "updated": updated,
            "total": created + updated,
            "errors": errors[:20],
            "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _config(self, overrides: dict[str, Any]) -> dict[str, Any]:
        return {
            "base_url": self._text(overrides.get("base_url") or os.getenv("MYBOARD_API_BASE_URL", "")).rstrip("/"),
            "token": self._text(overrides.get("token") or os.getenv("MYBOARD_API_TOKEN", "")),
            "token_header": self._text(overrides.get("token_header") or os.getenv("MYBOARD_API_TOKEN_HEADER", "Authorization")),
            "token_prefix": self._text(overrides.get("token_prefix") or os.getenv("MYBOARD_API_TOKEN_PREFIX", "Bearer")),
            "list_path": self._text(overrides.get("list_path") or os.getenv("MYBOARD_API_LIST_PATH", "/api/boards")),
            "detail_path": self._text(overrides.get("detail_path") or os.getenv("MYBOARD_API_DETAIL_PATH", "/api/boards/{id}")),
            "timeout": int(overrides.get("timeout") or os.getenv("MYBOARD_API_TIMEOUT", "20")),
            "verify_ssl": self._bool(overrides.get("verify_ssl"), os.getenv("MYBOARD_API_VERIFY_SSL", "true")),
        }

    def _request_json(self, cfg: dict[str, Any], path_or_url: str) -> Any:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else cfg["base_url"] + "/" + path_or_url.lstrip("/")
        headers = {"Accept": "application/json"}
        if cfg["token"]:
            header_value = cfg["token"]
            if cfg["token_prefix"]:
                header_value = f"{cfg['token_prefix']} {cfg['token']}"
            headers[cfg["token_header"]] = header_value

        req = urllib.request.Request(url, headers=headers, method="GET")
        context = None
        if not cfg["verify_ssl"]:
            context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"], context=context) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"MyBoard API HTTP {e.code}: {url}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"MyBoard API connection error: {e.reason}") from e

        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MyBoard API returned non-JSON response: {url}") from e

    # ------------------------------------------------------------------
    # Local cache
    # ------------------------------------------------------------------

    def upsert_remote_board(self, external_id: str, payload: Any, summary: dict | None = None) -> str:
        summary = summary or {}
        normalized = self.normalize_board(payload, summary)
        title = normalized.get("title") or f"Board {external_id}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        raw_json = json.dumps(payload, ensure_ascii=False)
        normalized_json = json.dumps(normalized, ensure_ascii=False)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM myboard_boards WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            values = (
                external_id,
                title,
                normalized.get("description", ""),
                normalized.get("category", ""),
                normalized.get("tags_text", ""),
                normalized.get("status", "active"),
                normalized.get("source_url", ""),
                normalized.get("thumbnail_url", ""),
                self._text(normalized.get("version")),
                self._text(normalized.get("updated_at")),
                now,
                raw_json,
                normalized_json,
                "",
            )
            if row:
                conn.execute("""
                    UPDATE myboard_boards
                    SET title=?, description=?, category=?, tags=?, status=?,
                        source_url=?, thumbnail_url=?, version=?, external_updated_at=?,
                        synced_at=?, raw_json=?, normalized_json=?, last_error=?
                    WHERE external_id=?
                """, values[1:] + (external_id,))
                conn.commit()
                return "updated"

            conn.execute("""
                INSERT INTO myboard_boards (
                    external_id, title, description, category, tags, status,
                    source_url, thumbnail_url, version, external_updated_at,
                    synced_at, raw_json, normalized_json, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            conn.commit()
            return "created"

    def get_boards(self, status: str = "active", category: str = "", search: str = "") -> list[dict]:
        query = """
            SELECT b.*,
                (SELECT COUNT(*) FROM myboard_views v
                 WHERE v.board_id = b.id
                   AND v.viewed_at >= datetime('now', '-30 days')) as views_30d
            FROM myboard_boards b
            WHERE 1=1
        """
        params: list[Any] = []
        if status:
            query += " AND b.status = ?"
            params.append(status)
        if category:
            query += " AND b.category = ?"
            params.append(category)
        if search:
            query += " AND (b.title LIKE ? OR b.description LIKE ? OR b.tags LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        query += " ORDER BY b.updated_at DESC, b.title ASC"
        with self._connect() as conn:
            return [self._row_with_meta(dict(row)) for row in conn.execute(query, params).fetchall()]

    def get_board(self, board_id: int, active_only: bool = False) -> dict | None:
        query = "SELECT * FROM myboard_boards WHERE id = ?"
        params: list[Any] = [board_id]
        if active_only:
            query += " AND status = 'active'"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
            return self._row_with_meta(dict(row)) if row else None

    def get_categories(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT COALESCE(NULLIF(TRIM(category), ''), 'Без категории') as name,
                       COUNT(*) as count
                FROM myboard_boards
                WHERE status = 'active'
                GROUP BY name
                ORDER BY name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_top_boards(self, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT b.*,
                    COUNT(v.id) as views_30d
                FROM myboard_boards b
                LEFT JOIN myboard_views v ON v.board_id = b.id
                    AND v.viewed_at >= datetime('now', '-30 days')
                WHERE b.status = 'active'
                GROUP BY b.id
                ORDER BY views_30d DESC, b.title ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return [self._row_with_meta(dict(row)) for row in rows]

    def log_view(self, board_id: int, user_id: str = ""):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO myboard_views (board_id, user_id) VALUES (?, ?)",
                (board_id, user_id),
            )
            conn.execute(
                "UPDATE myboard_boards SET view_count = view_count + 1 WHERE id = ?",
                (board_id,),
            )
            conn.commit()

    def update_status(self, board_id: int, status: str):
        status = status if status in {"active", "archived"} else "active"
        with self._connect() as conn:
            conn.execute("UPDATE myboard_boards SET status=? WHERE id=?", (status, board_id))
            conn.commit()

    def delete_board(self, board_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM myboard_boards WHERE id=?", (board_id,))
            conn.commit()

    def _row_with_meta(self, row: dict) -> dict:
        normalized = self._json_obj(row.get("normalized_json")) or {}
        row["node_count"] = len(normalized.get("nodes") or [])
        row["edge_count"] = len(normalized.get("edges") or [])
        row["normalized"] = normalized
        row["tags_list"] = [t.strip() for t in (row.get("tags") or "").split(",") if t.strip()]
        return row

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize_board(self, payload: Any, summary: dict | None = None) -> dict:
        summary = summary or {}
        root = self._first_dict(payload)
        board = self._pick_nested_board(root)
        meta = {**summary, **board}
        nodes_raw = self._find_first_list(board, ("nodes", "items", "cards", "elements", "shapes", "widgets", "blocks"))
        edges_raw = self._find_first_list(board, ("edges", "links", "connections", "connectors", "arrows"))

        nodes = [self._normalize_node(item, index) for index, item in enumerate(nodes_raw)]
        nodes = [node for node in nodes if node["id"]]
        node_ids = {node["id"] for node in nodes}
        edges = [
            edge for edge in (self._normalize_edge(item, index) for index, item in enumerate(edges_raw))
            if edge["from"] in node_ids and edge["to"] in node_ids
        ]
        if not edges:
            edges = self._edges_from_node_links(nodes_raw, node_ids)

        title = self._text(meta.get("title") or meta.get("name") or meta.get("boardName") or "Схема MyBoard")
        description = self._text(meta.get("description") or meta.get("summary") or meta.get("subtitle"))
        category = self._text(meta.get("category") or meta.get("folder") or meta.get("team") or "Без категории")
        tags = self._extract_tags(meta.get("tags") or meta.get("labels"))

        return {
            "title": title,
            "description": description,
            "category": category,
            "tags": tags,
            "tags_text": ", ".join(tags),
            "status": self._normalize_status(meta.get("status") or meta.get("state") or meta.get("published")),
            "source_url": self._text(meta.get("url") or meta.get("source_url") or meta.get("webUrl")),
            "thumbnail_url": self._text(meta.get("thumbnail") or meta.get("thumbnail_url") or meta.get("image")),
            "version": self._text(meta.get("version") or meta.get("rev") or meta.get("revision")),
            "updated_at": self._text(meta.get("updated_at") or meta.get("updatedAt") or meta.get("modified_at") or meta.get("modifiedAt")),
            "nodes": nodes,
            "edges": edges,
        }

    def _normalize_node(self, item: Any, index: int) -> dict:
        obj = self._first_dict(item)
        node_id = self._text(obj.get("id") or obj.get("_id") or obj.get("uuid") or obj.get("key") or f"node-{index + 1}")
        title = self._text(
            obj.get("title")
            or obj.get("name")
            or obj.get("label")
            or obj.get("text")
            or obj.get("plainText")
            or obj.get("content")
        )
        content = self._text(obj.get("content") or obj.get("description") or obj.get("body") or obj.get("note"))
        if content == title:
            content = ""
        position = obj.get("position") if isinstance(obj.get("position"), dict) else {}
        geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
        x = self._number(obj.get("x"), self._number(position.get("x"), self._number(geometry.get("x"), 90 + (index % 4) * 280)))
        y = self._number(obj.get("y"), self._number(position.get("y"), self._number(geometry.get("y"), 80 + (index // 4) * 180)))
        width = self._number(obj.get("width"), self._number(geometry.get("width"), 230))
        height = self._number(obj.get("height"), self._number(geometry.get("height"), 112))
        return {
            "id": node_id,
            "type": self._text(obj.get("type") or obj.get("shape") or obj.get("node_type") or "card").lower(),
            "title": title or f"Блок {index + 1}",
            "content": content,
            "x": x,
            "y": y,
            "width": max(150, min(width, 420)),
            "height": max(80, min(height, 260)),
            "color": self._text(obj.get("color") or obj.get("background") or obj.get("fill")),
        }

    def _normalize_edge(self, item: Any, index: int) -> dict:
        obj = self._first_dict(item)
        source = obj.get("from") or obj.get("from_id") or obj.get("source") or obj.get("source_id") or obj.get("start") or obj.get("startNodeId")
        target = obj.get("to") or obj.get("to_id") or obj.get("target") or obj.get("target_id") or obj.get("end") or obj.get("endNodeId")
        return {
            "id": self._text(obj.get("id") or obj.get("_id") or f"edge-{index + 1}"),
            "from": self._text(source),
            "to": self._text(target),
            "label": self._text(obj.get("label") or obj.get("title") or obj.get("text")),
        }

    def _edges_from_node_links(self, nodes_raw: list, node_ids: set[str]) -> list[dict]:
        edges = []
        for index, item in enumerate(nodes_raw):
            obj = self._first_dict(item)
            source = self._text(obj.get("id") or obj.get("_id") or obj.get("uuid") or obj.get("key") or f"node-{index + 1}")
            targets = obj.get("children") or obj.get("next") or obj.get("targets") or []
            if isinstance(targets, (str, int)):
                targets = [targets]
            if not isinstance(targets, list):
                continue
            for target in targets:
                target_id = self._text(target.get("id") if isinstance(target, dict) else target)
                if source in node_ids and target_id in node_ids:
                    edges.append({
                        "id": f"edge-{len(edges) + 1}",
                        "from": source,
                        "to": target_id,
                        "label": self._text(target.get("label") if isinstance(target, dict) else ""),
                    })
        return edges

    def _extract_board_summaries(self, payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [self._first_dict(item) for item in payload]
        root = self._first_dict(payload)
        for key in ("boards", "schemas", "diagrams", "items", "data", "results"):
            value = root.get(key)
            if isinstance(value, list):
                return [self._first_dict(item) for item in value]
            if isinstance(value, dict):
                nested = self._extract_board_summaries(value)
                if nested:
                    return nested
        return [root] if root else []

    def _pick_nested_board(self, root: dict) -> dict:
        for key in ("board", "schema", "diagram", "canvas", "data"):
            value = root.get(key)
            if isinstance(value, dict):
                nested = dict(root)
                nested.update(value)
                return nested
        return root

    def _find_first_list(self, obj: dict, keys: tuple[str, ...]) -> list:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, list):
                return value
        for value in obj.values():
            if isinstance(value, dict):
                found = self._find_first_list(value, keys)
                if found:
                    return found
        return []

    def _first_dict(self, value: Any) -> dict:
        if isinstance(value, dict):
            return value
        return {}

    def _extract_tags(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    text = self._text(item.get("name") or item.get("title") or item.get("label"))
                else:
                    text = self._text(item)
                if text:
                    result.append(text)
            return result[:12]
        return []

    def _normalize_status(self, value: Any) -> str:
        if isinstance(value, bool):
            return "active" if value else "archived"
        text = self._text(value).lower()
        if text in {"archived", "archive", "deleted", "inactive", "draft"}:
            return "archived"
        return "active"

    def _json_obj(self, value: Any) -> Any:
        try:
            return json.loads(value or "{}")
        except Exception:
            return {}

    def _text(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _number(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _bool(self, value: Any, default: Any = "true") -> bool:
        raw = default if value is None else value
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
