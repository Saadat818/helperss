import os
import sqlite3
from datetime import datetime
from typing import Any


class EmployeeBoardManager:
    """Stores white/black board posts for Helper."""

    BOARD_TYPES = {"white", "black"}
    STATUSES = {"draft", "published", "archived"}

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS employee_board_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_type TEXT NOT NULL CHECK(board_type IN ('white', 'black')),
                    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'published', 'archived')),
                    full_name TEXT DEFAULT '',
                    public_title TEXT DEFAULT '',
                    current_position TEXT DEFAULT '',
                    start_position TEXT DEFAULT '',
                    tenure TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    role_before TEXT DEFAULT '',
                    incident TEXT DEFAULT '',
                    actions_taken TEXT DEFAULT '',
                    lesson TEXT DEFAULT '',
                    photo_path TEXT DEFAULT '',
                    public_photo_path TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_employee_board_public
                ON employee_board_posts(board_type, status, published_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_employee_board_admin
                ON employee_board_posts(status, updated_at)
            """)
            conn.commit()

    def create_post(self, data: dict[str, Any], actor: str = "") -> dict:
        prepared = self._prepare_data(data)
        if not prepared["success"]:
            return prepared

        now = self._now()
        status = prepared["data"]["status"]
        published_at = now if status == "published" else ""
        values = (
            prepared["data"]["board_type"],
            status,
            prepared["data"]["full_name"],
            prepared["data"]["public_title"],
            prepared["data"]["current_position"],
            prepared["data"]["start_position"],
            prepared["data"]["tenure"],
            prepared["data"]["summary"],
            prepared["data"]["description"],
            prepared["data"]["role_before"],
            prepared["data"]["incident"],
            prepared["data"]["actions_taken"],
            prepared["data"]["lesson"],
            prepared["data"]["photo_path"],
            prepared["data"]["public_photo_path"],
            actor,
            actor,
            now,
            now,
            published_at,
        )

        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO employee_board_posts (
                    board_type, status, full_name, public_title, current_position,
                    start_position, tenure, summary, description, role_before,
                    incident, actions_taken, lesson, photo_path, public_photo_path,
                    created_by, updated_by, created_at, updated_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            conn.commit()
            return {"success": True, "id": cursor.lastrowid}

    def update_post(self, post_id: int, data: dict[str, Any], actor: str = "") -> dict:
        current = self.get_post(post_id)
        if not current:
            return {"success": False, "error": "Запись не найдена"}

        merged = dict(current)
        merged.update(data)
        prepared = self._prepare_data(merged)
        if not prepared["success"]:
            return prepared

        now = self._now()
        status = prepared["data"]["status"]
        published_at = current.get("published_at") or ""
        if status == "published" and not published_at:
            published_at = now
        if status != "published":
            published_at = ""

        values = (
            prepared["data"]["board_type"],
            status,
            prepared["data"]["full_name"],
            prepared["data"]["public_title"],
            prepared["data"]["current_position"],
            prepared["data"]["start_position"],
            prepared["data"]["tenure"],
            prepared["data"]["summary"],
            prepared["data"]["description"],
            prepared["data"]["role_before"],
            prepared["data"]["incident"],
            prepared["data"]["actions_taken"],
            prepared["data"]["lesson"],
            prepared["data"]["photo_path"],
            prepared["data"]["public_photo_path"],
            actor,
            now,
            published_at,
            post_id,
        )

        with self._connect() as conn:
            conn.execute("""
                UPDATE employee_board_posts
                SET board_type=?, status=?, full_name=?, public_title=?,
                    current_position=?, start_position=?, tenure=?, summary=?,
                    description=?, role_before=?, incident=?, actions_taken=?,
                    lesson=?, photo_path=?, public_photo_path=?, updated_by=?,
                    updated_at=?, published_at=?
                WHERE id=?
            """, values)
            conn.commit()
            return {"success": True, "id": post_id}

    def set_photo_paths(self, post_id: int, photo_path: str = "", public_photo_path: str = "", actor: str = "") -> dict:
        current = self.get_post(post_id)
        if not current:
            return {"success": False, "error": "Запись не найдена"}
        with self._connect() as conn:
            conn.execute("""
                UPDATE employee_board_posts
                SET photo_path=?, public_photo_path=?, updated_by=?, updated_at=?
                WHERE id=?
            """, (self._text(photo_path), self._text(public_photo_path), actor, self._now(), post_id))
            conn.commit()
        return {"success": True, "id": post_id}

    def set_status(self, post_id: int, status: str, actor: str = "") -> dict:
        if status not in self.STATUSES:
            return {"success": False, "error": "Некорректный статус"}
        current = self.get_post(post_id)
        if not current:
            return {"success": False, "error": "Запись не найдена"}

        now = self._now()
        published_at = current.get("published_at") or ""
        if status == "published" and not published_at:
            published_at = now
        if status != "published":
            published_at = ""

        with self._connect() as conn:
            conn.execute("""
                UPDATE employee_board_posts
                SET status=?, updated_by=?, updated_at=?, published_at=?
                WHERE id=?
            """, (status, actor, now, published_at, post_id))
            conn.commit()
        return {"success": True, "id": post_id}

    def delete_post(self, post_id: int) -> dict:
        current = self.get_post(post_id)
        if not current:
            return {"success": False, "error": "Запись не найдена"}
        with self._connect() as conn:
            conn.execute("DELETE FROM employee_board_posts WHERE id=?", (post_id,))
            conn.commit()
        return {"success": True, "post": current}

    def get_post(self, post_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM employee_board_posts WHERE id=?", (post_id,)).fetchone()
            return self._row(row) if row else None

    def list_posts(
        self,
        board_type: str = "",
        status: str = "",
        search: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        query = "SELECT * FROM employee_board_posts WHERE 1=1"
        params: list[Any] = []
        if board_type in self.BOARD_TYPES:
            query += " AND board_type=?"
            params.append(board_type)
        if status in self.STATUSES:
            query += " AND status=?"
            params.append(status)
        if search:
            like = f"%{search}%"
            query += """
                AND (
                    full_name LIKE ? OR public_title LIKE ? OR current_position LIKE ?
                    OR start_position LIKE ? OR summary LIKE ? OR description LIKE ?
                    OR role_before LIKE ? OR incident LIKE ? OR actions_taken LIKE ? OR lesson LIKE ?
                )
            """
            params.extend([like] * 10)
        query += """
            ORDER BY
                CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                COALESCE(NULLIF(published_at, ''), updated_at) DESC,
                id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._connect() as conn:
            return [self._row(row) for row in conn.execute(query, params).fetchall()]

    def public_posts(self, board_type: str = "") -> list[dict]:
        query = """
            SELECT * FROM employee_board_posts
            WHERE status='published'
        """
        params: list[Any] = []
        if board_type in self.BOARD_TYPES:
            query += " AND board_type=?"
            params.append(board_type)
        query += " ORDER BY COALESCE(NULLIF(published_at, ''), updated_at) DESC, id DESC"
        with self._connect() as conn:
            return [self._public_row(row) for row in conn.execute(query, params).fetchall()]

    def stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT board_type, status, COUNT(*) AS count
                FROM employee_board_posts
                GROUP BY board_type, status
            """).fetchall()
        stats = {
            "white_published": 0,
            "white_draft": 0,
            "white_archived": 0,
            "black_published": 0,
            "black_draft": 0,
            "black_archived": 0,
            "total": 0,
        }
        for row in rows:
            key = f"{row['board_type']}_{row['status']}"
            stats[key] = int(row["count"] or 0)
            stats["total"] += int(row["count"] or 0)
        return stats

    def _prepare_data(self, data: dict[str, Any]) -> dict:
        board_type = self._text(data.get("board_type") or "white").lower()
        status = self._text(data.get("status") or "draft").lower()
        if board_type not in self.BOARD_TYPES:
            return {"success": False, "error": "Выберите Белую или Чёрную доску"}
        if status not in self.STATUSES:
            return {"success": False, "error": "Некорректный статус записи"}

        full_name = self._clip(data.get("full_name"), 180)
        public_title = self._clip(data.get("public_title"), 180)
        current_position = self._clip(data.get("current_position"), 220)
        start_position = self._clip(data.get("start_position"), 220)
        tenure = self._clip(data.get("tenure"), 120)
        summary = self._clip(data.get("summary"), 360)
        description = self._clip(data.get("description"), 4000)
        role_before = self._clip(data.get("role_before"), 220)
        incident = self._clip(data.get("incident"), 4000)
        actions_taken = self._clip(data.get("actions_taken"), 2000)
        lesson = self._clip(data.get("lesson"), 2000)

        if board_type == "white":
            if not full_name:
                return {"success": False, "error": "Для Белой доски укажите ФИО"}
            if not current_position:
                return {"success": False, "error": "Для Белой доски укажите текущую должность"}
            public_title = public_title or full_name
            role_before = ""
            incident = ""
            actions_taken = ""
            lesson = ""
        else:
            full_name = ""
            current_position = ""
            start_position = ""
            tenure = ""
            description = description or ""
            public_title = public_title or "Кейс без персональных данных"
            if not incident:
                return {"success": False, "error": "Для Чёрной доски опишите, что произошло"}
            if not actions_taken:
                return {"success": False, "error": "Для Чёрной доски укажите принятые меры"}

        return {
            "success": True,
            "data": {
                "board_type": board_type,
                "status": status,
                "full_name": full_name,
                "public_title": public_title,
                "current_position": current_position,
                "start_position": start_position,
                "tenure": tenure,
                "summary": summary,
                "description": description,
                "role_before": role_before,
                "incident": incident,
                "actions_taken": actions_taken,
                "lesson": lesson,
                "photo_path": self._text(data.get("photo_path")),
                "public_photo_path": self._text(data.get("public_photo_path")),
            },
        }

    def _row(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["type_label"] = "Белая доска" if data.get("board_type") == "white" else "Чёрная доска"
        data["status_label"] = {
            "draft": "Черновик",
            "published": "Опубликовано",
            "archived": "Архив",
        }.get(data.get("status"), data.get("status", ""))
        return data

    def _public_row(self, row: sqlite3.Row) -> dict:
        data = self._row(row)
        if data.get("board_type") == "black":
            data["full_name"] = ""
            data["current_position"] = ""
            data["start_position"] = ""
            data["tenure"] = ""
            data["photo_path"] = ""
        return data

    def _clip(self, value: Any, limit: int) -> str:
        text = self._text(value)
        if len(text) <= limit:
            return text
        return text[:limit].rstrip()

    def _text(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
