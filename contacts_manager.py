"""
Менеджер справочника контактов КЦ.

Таблицы имеют префикс cc_ (contact center), чтобы не пересекаться с
мануалами, тематиками и сценариями в общей SQLite-базе topics.db.
"""

import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional


class ContactsManager:
    """CRUD контактов КЦ."""

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_directions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    color TEXT DEFAULT '#16a34a',
                    sort_order INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction_id INTEGER REFERENCES cc_directions(id) ON DELETE SET NULL,
                    full_name TEXT NOT NULL,
                    position TEXT DEFAULT '',
                    department TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    extension TEXT DEFAULT '',
                    mobile TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    telegram TEXT DEFAULT '',
                    workplace TEXT DEFAULT '',
                    schedule TEXT DEFAULT '',
                    responsibilities TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    photo_path TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_contact_likes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL REFERENCES cc_contacts(id) ON DELETE CASCADE,
                    actor_key TEXT NOT NULL,
                    actor_name TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(contact_id, actor_key)
                )
            """)
            c.execute("PRAGMA table_info(cc_contacts)")
            existing_contact_cols = {row[1] for row in c.fetchall()}
            if "photo_path" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN photo_path TEXT DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_direction ON cc_contacts(direction_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_department ON cc_contacts(department)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_active ON cc_contacts(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_name ON cc_contacts(full_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_email ON cc_contacts(email)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_directions_active ON cc_directions(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact_likes_contact ON cc_contact_likes(contact_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact_likes_actor ON cc_contact_likes(actor_key)")
            c.execute("""
                UPDATE cc_contacts
                SET department = (
                    SELECT d.name
                    FROM cc_directions d
                    WHERE d.id = cc_contacts.direction_id
                )
                WHERE TRIM(COALESCE(department, '')) = ''
                  AND direction_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM cc_directions d
                      WHERE d.id = cc_contacts.direction_id
                  )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _clean(value, limit: int = 1000) -> str:
        text = str(value or "").strip()
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text[:limit]

    @staticmethod
    def _active_value(value) -> int:
        text = str(value or "").strip().lower()
        if text in {"0", "no", "false", "нет", "неактивен", "архив", "inactive"}:
            return 0
        return 1

    def get_departments(self, include_inactive: bool = True) -> List[Dict]:
        with self._connect() as conn:
            query = """
                SELECT
                    MIN(TRIM(department)) AS name,
                    COUNT(*) AS contacts_count,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_contacts_count
                FROM cc_contacts
                WHERE TRIM(COALESCE(department, '')) <> ''
            """
            params = []
            if not include_inactive:
                query += " AND is_active = 1"
            query += " GROUP BY lower(TRIM(department)) ORDER BY lower(MIN(TRIM(department)))"
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    # ─── Контакты ─────────────────────────────────────────────────

    def get_contacts(self, q: str = "", department: str = "", direction_id: int | None = None,
                     status: str = "active",
                     limit: int = 1000, offset: int = 0) -> List[Dict]:
        with self._connect() as conn:
            query, params = self._contacts_query(q, department, direction_id, status, count=False)
            query += """
                ORDER BY
                    CASE WHEN TRIM(COALESCE(c.department, '')) = '' THEN 1 ELSE 0 END,
                    lower(TRIM(c.department)),
                    c.sort_order,
                    c.full_name
                LIMIT ? OFFSET ?
            """
            params.extend([max(1, min(int(limit or 1000), 5000)), max(0, int(offset or 0))])
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def count_contacts(self, q: str = "", department: str = "", direction_id: int | None = None,
                       status: str = "active") -> int:
        with self._connect() as conn:
            query, params = self._contacts_query(q, department, direction_id, status, count=True)
            row = conn.execute(query, params).fetchone()
            return int(row["cnt"] if row else 0)

    def _contacts_query(self, q: str, department: str, direction_id: int | None,
                        status: str, count: bool):
        select = "SELECT COUNT(*) AS cnt" if count else """
            SELECT c.*,
                   (SELECT COUNT(*) FROM cc_contact_likes l WHERE l.contact_id = c.id) AS likes_count
        """
        query = f"""
            {select}
            FROM cc_contacts c
            WHERE 1=1
        """
        params: List = []
        if status == "active":
            query += " AND c.is_active = 1"
        elif status == "inactive":
            query += " AND c.is_active = 0"
        if direction_id:
            query += " AND c.direction_id = ?"
            params.append(direction_id)
        department = self._clean(department, 220)
        if department:
            query += " AND lower(TRIM(c.department)) = lower(?)"
            params.append(department)
        q = self._clean(q, 200)
        if q:
            like = f"%{q}%"
            query += """
                AND (
                    c.full_name LIKE ? OR c.position LIKE ? OR c.department LIKE ?
                    OR c.phone LIKE ? OR c.extension LIKE ? OR c.mobile LIKE ?
                    OR c.email LIKE ? OR c.telegram LIKE ? OR c.workplace LIKE ?
                    OR c.responsibilities LIKE ? OR c.tags LIKE ?
                )
            """
            params.extend([like] * 11)
        return query, params

    def get_contact(self, contact_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT c.*,
                       (SELECT COUNT(*) FROM cc_contact_likes l WHERE l.contact_id = c.id) AS likes_count
                FROM cc_contacts c
                WHERE c.id = ?
            """, (contact_id,)).fetchone()
            return dict(row) if row else None

    def get_liked_contact_ids(self, actor_key: str, contact_ids: List[int]) -> set[int]:
        actor_key = self._clean(actor_key, 200)
        ids = [int(x) for x in contact_ids if x]
        if not actor_key or not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT contact_id
                FROM cc_contact_likes
                WHERE actor_key = ? AND contact_id IN ({placeholders})
            """, [actor_key] + ids).fetchall()
            return {int(r["contact_id"]) for r in rows}

    def toggle_like(self, contact_id: int, actor_key: str, actor_name: str = "") -> Dict:
        actor_key = self._clean(actor_key, 200)
        actor_name = self._clean(actor_name, 220)
        if not actor_key:
            return {"success": False, "error": "Пользователь не определён"}

        with self._connect() as conn:
            contact = conn.execute("SELECT id FROM cc_contacts WHERE id = ?", (contact_id,)).fetchone()
            if not contact:
                return {"success": False, "error": "Контакт не найден"}

            existing = conn.execute("""
                SELECT id FROM cc_contact_likes
                WHERE contact_id = ? AND actor_key = ?
            """, (contact_id, actor_key)).fetchone()
            if existing:
                conn.execute("DELETE FROM cc_contact_likes WHERE id = ?", (existing["id"],))
                liked = False
            else:
                conn.execute("""
                    INSERT INTO cc_contact_likes (contact_id, actor_key, actor_name, created_at)
                    VALUES (?, ?, ?, ?)
                """, (contact_id, actor_key, actor_name, self._now()))
                liked = True

            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM cc_contact_likes WHERE contact_id = ?",
                (contact_id,)
            ).fetchone()
            conn.commit()
            return {"success": True, "liked": liked, "likes_count": int(row["cnt"] if row else 0)}

    def _contact_payload(self, data: Dict, conn=None, actor: str = "") -> Dict:
        payload = {
            "direction_id": None,
            "full_name": self._clean(data.get("full_name"), 220),
            "position": self._clean(data.get("position"), 220),
            "department": self._clean(data.get("department"), 220),
            "phone": self._clean(data.get("phone"), 100),
            "extension": self._clean(data.get("extension"), 50),
            "mobile": self._clean(data.get("mobile"), 100),
            "email": self._clean(data.get("email"), 180),
            "telegram": self._clean(data.get("telegram"), 100),
            "workplace": self._clean(data.get("workplace"), 140),
            "schedule": self._clean(data.get("schedule"), 300),
            "responsibilities": self._clean(data.get("responsibilities"), 1500),
            "notes": self._clean(data.get("notes"), 1500),
            "tags": self._clean(data.get("tags"), 500),
            "is_active": self._active_value(data.get("is_active", "1")),
            "sort_order": int(data.get("sort_order") or 0),
        }
        return payload

    def create_contact(self, data: Dict, actor: str = "") -> Dict:
        with self._connect() as conn:
            payload = self._contact_payload(data, conn=conn, actor=actor)
            if not payload["full_name"]:
                return {"success": False, "error": "ФИО сотрудника обязательно"}

            cur = conn.execute("""
                INSERT INTO cc_contacts (
                    direction_id, full_name, position, department, phone, extension, mobile,
                    email, telegram, workplace, schedule, responsibilities, notes, tags,
                    is_active, sort_order, created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                payload["direction_id"], payload["full_name"], payload["position"], payload["department"],
                payload["phone"], payload["extension"], payload["mobile"], payload["email"],
                payload["telegram"], payload["workplace"], payload["schedule"], payload["responsibilities"],
                payload["notes"], payload["tags"], payload["is_active"], payload["sort_order"],
                actor, actor, self._now(), self._now(),
            ))
            conn.commit()
            return {"success": True, "id": cur.lastrowid}

    def update_contact(self, contact_id: int, data: Dict, actor: str = "") -> Dict:
        with self._connect() as conn:
            exists = conn.execute("SELECT id FROM cc_contacts WHERE id = ?", (contact_id,)).fetchone()
            if not exists:
                return {"success": False, "error": "Контакт не найден"}
            payload = self._contact_payload(data, conn=conn, actor=actor)
            if not payload["full_name"]:
                return {"success": False, "error": "ФИО сотрудника обязательно"}

            conn.execute("""
                UPDATE cc_contacts
                SET direction_id = ?, full_name = ?, position = ?, department = ?, phone = ?,
                    extension = ?, mobile = ?, email = ?, telegram = ?, workplace = ?,
                    schedule = ?, responsibilities = ?, notes = ?, tags = ?,
                    is_active = ?, sort_order = ?, updated_by = ?, updated_at = ?
                WHERE id = ?
            """, (
                payload["direction_id"], payload["full_name"], payload["position"], payload["department"],
                payload["phone"], payload["extension"], payload["mobile"], payload["email"],
                payload["telegram"], payload["workplace"], payload["schedule"], payload["responsibilities"],
                payload["notes"], payload["tags"], payload["is_active"], payload["sort_order"],
                actor, self._now(), contact_id,
            ))
            conn.commit()
            return {"success": True}

    def set_contact_active(self, contact_id: int, is_active: bool, actor: str = "") -> Dict:
        with self._connect() as conn:
            conn.execute("""
                UPDATE cc_contacts
                SET is_active = ?, updated_by = ?, updated_at = ?
                WHERE id = ?
            """, (1 if is_active else 0, actor, self._now(), contact_id))
            conn.commit()
            return {"success": True}

    def update_contact_photo(self, contact_id: int, photo_path: str = "", actor: str = "") -> Dict:
        with self._connect() as conn:
            exists = conn.execute("SELECT id FROM cc_contacts WHERE id = ?", (contact_id,)).fetchone()
            if not exists:
                return {"success": False, "error": "Контакт не найден"}
            conn.execute("""
                UPDATE cc_contacts
                SET photo_path = ?, updated_by = ?, updated_at = ?
                WHERE id = ?
            """, (self._clean(photo_path, 500), actor, self._now(), contact_id))
            conn.commit()
            return {"success": True}

    def delete_contact(self, contact_id: int) -> Dict:
        with self._connect() as conn:
            conn.execute("DELETE FROM cc_contacts WHERE id = ?", (contact_id,))
            conn.commit()
            return {"success": True}

    def get_stats(self) -> Dict:
        with self._connect() as conn:
            contacts = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END) AS inactive
                FROM cc_contacts
            """).fetchone()
            departments = conn.execute("""
                SELECT COUNT(*) AS total
                FROM (
                    SELECT lower(TRIM(department)) AS department_key
                    FROM cc_contacts
                    WHERE TRIM(COALESCE(department, '')) <> ''
                    GROUP BY lower(TRIM(department))
                )
            """).fetchone()
            return {
                "contacts_total": int(contacts["total"] or 0),
                "contacts_active": int(contacts["active"] or 0),
                "contacts_inactive": int(contacts["inactive"] or 0),
                "departments_total": int(departments["total"] or 0),
            }
