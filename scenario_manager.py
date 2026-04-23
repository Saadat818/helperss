"""
Менеджер сценариев консультаций для операторов КЦ.
Интерактивное дерево решений — пошаговые алгоритмы консультаций.
Таблицы: cs_* (consultation scenarios)
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime


class ScenarioManager:
    """Управление сценариями консультаций КЦ"""

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
            c = conn.cursor()

            # Категории сценариев
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    icon TEXT DEFAULT '📁',
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Сценарии
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_scenarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    category_id INTEGER REFERENCES cs_categories(id),
                    status TEXT DEFAULT 'draft',
                    version INTEGER DEFAULT 1,
                    tags TEXT DEFAULT '',
                    view_count INTEGER DEFAULT 0,
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Узлы сценария (шаги дерева решений)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL REFERENCES cs_scenarios(id) ON DELETE CASCADE,
                    node_type TEXT DEFAULT 'question',
                    title TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    is_root INTEGER DEFAULT 0,
                    sort_order INTEGER DEFAULT 0,
                    answer_text TEXT DEFAULT '',
                    final_answer TEXT DEFAULT '',
                    internal_note TEXT DEFAULT '',
                    documents TEXT DEFAULT '',
                    links TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Переходы между узлами (ребра дерева)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL REFERENCES cs_scenarios(id) ON DELETE CASCADE,
                    from_node_id INTEGER NOT NULL REFERENCES cs_nodes(id) ON DELETE CASCADE,
                    to_node_id INTEGER NOT NULL REFERENCES cs_nodes(id) ON DELETE CASCADE,
                    label TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0
                )
            """)

            # Версии сценариев (снапшоты)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL REFERENCES cs_scenarios(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_by TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Логи просмотров (для рейтинга популярности)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL REFERENCES cs_scenarios(id) ON DELETE CASCADE,
                    user_id TEXT DEFAULT '',
                    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Индексы
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_scenarios_status ON cs_scenarios(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_nodes_scenario ON cs_nodes(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_edges_scenario ON cs_edges(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_views_scenario ON cs_views(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_views_date ON cs_views(viewed_at)")

            conn.commit()

    # ─── Категории ────────────────────────────────────────────────

    def get_categories(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cs_categories ORDER BY sort_order, name"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_category(self, name: str, icon: str = '📁') -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cs_categories (name, icon) VALUES (?, ?)", (name, icon)
            )
            conn.commit()
            return cur.lastrowid

    def update_category(self, cat_id: int, name: str, icon: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cs_categories SET name=?, icon=? WHERE id=?", (name, icon, cat_id)
            )
            conn.commit()

    def delete_category(self, cat_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_categories WHERE id=?", (cat_id,))
            conn.commit()

    # ─── Сценарии ─────────────────────────────────────────────────

    def get_scenarios(self, status: str = 'active', category_id: int = None,
                      search: str = None) -> List[Dict]:
        with self._connect() as conn:
            query = """
                SELECT s.*, c.name as category_name, c.icon as category_icon,
                    (SELECT COUNT(*) FROM cs_views v
                     WHERE v.scenario_id = s.id
                     AND v.viewed_at >= datetime('now', '-30 days')) as views_30d
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                WHERE 1=1
            """
            params = []
            if status:
                query += " AND s.status = ?"
                params.append(status)
            if category_id:
                query += " AND s.category_id = ?"
                params.append(category_id)
            if search:
                query += " AND (s.title LIKE ? OR s.description LIKE ? OR s.tags LIKE ?)"
                params += [f'%{search}%', f'%{search}%', f'%{search}%']
            query += " ORDER BY views_30d DESC, s.updated_at DESC"

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_all_scenarios_admin(self) -> List[Dict]:
        """Все сценарии для админа включая черновики и архив"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT s.*, c.name as category_name, c.icon as category_icon
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                ORDER BY s.updated_at DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_scenario(self, scenario_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT s.*, c.name as category_name, c.icon as category_icon
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                WHERE s.id = ?
            """, (scenario_id,)).fetchone()
            return dict(row) if row else None

    def create_scenario(self, title: str, description: str = '', category_id: int = None,
                        tags: str = '', created_by: str = '') -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_scenarios (title, description, category_id, tags,
                    status, version, created_by, updated_by)
                VALUES (?, ?, ?, ?, 'draft', 1, ?, ?)
            """, (title, description, category_id, tags, created_by, created_by))
            conn.commit()
            return cur.lastrowid

    def update_scenario(self, scenario_id: int, title: str, description: str,
                        category_id: int, tags: str, updated_by: str = ''):
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios
                SET title=?, description=?, category_id=?, tags=?,
                    updated_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (title, description, category_id, tags, updated_by, scenario_id))
            conn.commit()

    def publish_scenario(self, scenario_id: int, updated_by: str = ''):
        """Публикация черновика → активный + создание версии"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM cs_scenarios WHERE id=?", (scenario_id,)
            ).fetchone()
            if not row:
                return False
            new_version = (row['version'] or 1) + 1
            # Сохраняем снапшот
            snapshot = self._build_snapshot(scenario_id, conn)
            conn.execute("""
                INSERT INTO cs_versions (scenario_id, version, snapshot_json, created_by)
                VALUES (?, ?, ?, ?)
            """, (scenario_id, new_version, json.dumps(snapshot, ensure_ascii=False), updated_by))
            conn.execute("""
                UPDATE cs_scenarios SET status='active', version=?,
                    updated_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (new_version, updated_by, scenario_id))
            conn.commit()
            return True

    def archive_scenario(self, scenario_id: int, updated_by: str = ''):
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios SET status='archived',
                    updated_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (updated_by, scenario_id))
            conn.commit()

    def unarchive_scenario(self, scenario_id: int, updated_by: str = ''):
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios SET status='draft',
                    updated_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (updated_by, scenario_id))
            conn.commit()

    def delete_scenario(self, scenario_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_scenarios WHERE id=?", (scenario_id,))
            conn.commit()

    def duplicate_scenario(self, scenario_id: int, created_by: str = '') -> Optional[int]:
        """Копирование сценария со всеми узлами и рёбрами"""
        with self._connect() as conn:
            orig = conn.execute(
                "SELECT * FROM cs_scenarios WHERE id=?", (scenario_id,)
            ).fetchone()
            if not orig:
                return None
            cur = conn.execute("""
                INSERT INTO cs_scenarios (title, description, category_id, tags,
                    status, version, created_by, updated_by)
                VALUES (?, ?, ?, ?, 'draft', 1, ?, ?)
            """, (f"{orig['title']} (копия)", orig['description'],
                  orig['category_id'], orig['tags'], created_by, created_by))
            new_id = cur.lastrowid

            # Копируем узлы
            nodes = conn.execute(
                "SELECT * FROM cs_nodes WHERE scenario_id=?", (scenario_id,)
            ).fetchall()
            node_map = {}  # old_id -> new_id
            for node in nodes:
                c2 = conn.execute("""
                    INSERT INTO cs_nodes (scenario_id, node_type, title, content, is_root,
                        sort_order, answer_text, final_answer, internal_note, documents, links)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_id, node['node_type'], node['title'], node['content'],
                      node['is_root'], node['sort_order'], node['answer_text'],
                      node['final_answer'], node['internal_note'],
                      node['documents'], node['links']))
                node_map[node['id']] = c2.lastrowid

            # Копируем рёбра
            edges = conn.execute(
                "SELECT * FROM cs_edges WHERE scenario_id=?", (scenario_id,)
            ).fetchall()
            for edge in edges:
                new_from = node_map.get(edge['from_node_id'])
                new_to = node_map.get(edge['to_node_id'])
                if new_from and new_to:
                    conn.execute("""
                        INSERT INTO cs_edges (scenario_id, from_node_id, to_node_id, label, sort_order)
                        VALUES (?, ?, ?, ?, ?)
                    """, (new_id, new_from, new_to, edge['label'], edge['sort_order']))

            conn.commit()
            return new_id

    # ─── Узлы ──────────────────────────────────────────────────────

    def get_nodes(self, scenario_id: int) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cs_nodes WHERE scenario_id=? ORDER BY sort_order, id",
                (scenario_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_root_node(self, scenario_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cs_nodes WHERE scenario_id=? AND is_root=1 LIMIT 1",
                (scenario_id,)
            ).fetchone()
            if not row:
                # Если нет корневого — берём первый
                row = conn.execute(
                    "SELECT * FROM cs_nodes WHERE scenario_id=? ORDER BY sort_order, id LIMIT 1",
                    (scenario_id,)
                ).fetchone()
            return dict(row) if row else None

    def get_node(self, node_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cs_nodes WHERE id=?", (node_id,)).fetchone()
            return dict(row) if row else None

    def get_node_choices(self, node_id: int) -> List[Dict]:
        """Получить варианты выбора (рёбра) из узла"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT e.*, n.title as next_title, n.node_type as next_type
                FROM cs_edges e
                JOIN cs_nodes n ON e.to_node_id = n.id
                WHERE e.from_node_id = ?
                ORDER BY e.sort_order, e.id
            """, (node_id,)).fetchall()
            return [dict(r) for r in rows]

    def create_node(self, scenario_id: int, node_type: str = 'question',
                    title: str = '', content: str = '', is_root: bool = False) -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_nodes (scenario_id, node_type, title, content, is_root)
                VALUES (?, ?, ?, ?, ?)
            """, (scenario_id, node_type, title, content, 1 if is_root else 0))
            conn.commit()
            return cur.lastrowid

    def update_node(self, node_id: int, data: dict):
        allowed = ['node_type', 'title', 'content', 'is_root', 'sort_order',
                   'answer_text', 'final_answer', 'internal_note', 'documents', 'links']
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return
        set_clause = ', '.join(f"{k}=?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE cs_nodes SET {set_clause} WHERE id=?",
                list(fields.values()) + [node_id]
            )
            conn.commit()

    def delete_node(self, node_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_edges WHERE from_node_id=? OR to_node_id=?",
                         (node_id, node_id))
            conn.execute("DELETE FROM cs_nodes WHERE id=?", (node_id,))
            conn.commit()

    # ─── Рёбра ─────────────────────────────────────────────────────

    def create_edge(self, scenario_id: int, from_node_id: int,
                    to_node_id: int, label: str = '', sort_order: int = 0) -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_edges (scenario_id, from_node_id, to_node_id, label, sort_order)
                VALUES (?, ?, ?, ?, ?)
            """, (scenario_id, from_node_id, to_node_id, label, sort_order))
            conn.commit()
            return cur.lastrowid

    def update_edge(self, edge_id: int, label: str, sort_order: int = 0):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cs_edges SET label=?, sort_order=? WHERE id=?",
                (label, sort_order, edge_id)
            )
            conn.commit()

    def delete_edge(self, edge_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_edges WHERE id=?", (edge_id,))
            conn.commit()

    # ─── Просмотры / рейтинг ──────────────────────────────────────

    def log_view(self, scenario_id: int, user_id: str = ''):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cs_views (scenario_id, user_id) VALUES (?, ?)",
                (scenario_id, user_id)
            )
            conn.execute(
                "UPDATE cs_scenarios SET view_count = view_count + 1 WHERE id=?",
                (scenario_id,)
            )
            conn.commit()

    def get_top_scenarios(self, limit: int = 10) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT s.id, s.title, s.category_id,
                    c.name as category_name, c.icon as category_icon,
                    COUNT(v.id) as views_30d
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                LEFT JOIN cs_views v ON v.scenario_id = s.id
                    AND v.viewed_at >= datetime('now', '-30 days')
                WHERE s.status = 'active'
                GROUP BY s.id
                ORDER BY views_30d DESC, s.title
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ─── Версии ────────────────────────────────────────────────────

    def get_versions(self, scenario_id: int) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM cs_versions WHERE scenario_id=? ORDER BY version DESC
            """, (scenario_id,)).fetchall()
            return [dict(r) for r in rows]

    # ─── Снапшот ───────────────────────────────────────────────────

    def _build_snapshot(self, scenario_id: int, conn) -> dict:
        scenario = conn.execute(
            "SELECT * FROM cs_scenarios WHERE id=?", (scenario_id,)
        ).fetchone()
        nodes = conn.execute(
            "SELECT * FROM cs_nodes WHERE scenario_id=? ORDER BY sort_order, id",
            (scenario_id,)
        ).fetchall()
        edges = conn.execute(
            "SELECT * FROM cs_edges WHERE scenario_id=?", (scenario_id,)
        ).fetchall()
        return {
            'scenario': dict(scenario) if scenario else {},
            'nodes': [dict(n) for n in nodes],
            'edges': [dict(e) for e in edges],
            'exported_at': datetime.now().isoformat()
        }

    def get_full_scenario(self, scenario_id: int) -> Optional[dict]:
        """Полный сценарий с узлами и рёбрами для редактора"""
        with self._connect() as conn:
            return self._build_snapshot(scenario_id, conn)
