"""
Менеджер сценариев консультаций для операторов КЦ.
Интерактивное дерево решений — пошаговые алгоритмы консультаций.
Таблицы: cs_* (consultation scenarios)
"""

import json
import sqlite3
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from db_backend import connect as db_connect, is_postgres_backend


class ScenarioManager:
    """Управление сценариями консультаций КЦ"""

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        if not is_postgres_backend():
            self._init_db()

    def _connect(self):
        conn = db_connect(self.db_path, check_same_thread=False, timeout=10.0)
        if not is_postgres_backend():
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_column(self, cursor, table: str, column: str, definition: str):
        existing = {row['name'] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
            for column, definition in (
                ("myboard_id", "TEXT"),
                ("myboard_version", "INTEGER"),
                ("myboard_updated_at", "TEXT"),
                ("last_synced_at", "TEXT"),
                ("has_local_changes", "INTEGER DEFAULT 0"),
                ("has_draft_changes", "INTEGER DEFAULT 0"),
                ("draft_updated_by", "TEXT DEFAULT ''"),
                ("draft_updated_at", "TEXT"),
                ("edit_mode", "TEXT DEFAULT 'synced'"),
                ("is_imported_from_myboard", "INTEGER DEFAULT 0"),
                ("copied_from_scenario_id", "INTEGER"),
                ("copied_from_myboard_id", "TEXT"),
                ("copied_from_myboard_version", "INTEGER"),
            ):
                self._ensure_column(c, "cs_scenarios", column, definition)

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
                    pos_x REAL DEFAULT 0,
                    pos_y REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for column, definition in (
                ("pos_x", "REAL DEFAULT 0"),
                ("pos_y", "REAL DEFAULT 0"),
                ("source_node_id", "TEXT DEFAULT ''"),
                ("source_type", "TEXT DEFAULT ''"),
                ("raw_data", "TEXT DEFAULT ''"),
            ):
                self._ensure_column(c, "cs_nodes", column, definition)

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
            for column, definition in (
                ("condition", "TEXT DEFAULT ''"),
                ("source_edge_id", "TEXT DEFAULT ''"),
            ):
                self._ensure_column(c, "cs_edges", column, definition)

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
            self._ensure_column(c, "cs_versions", "comment", "TEXT DEFAULT ''")

            # Логи просмотров (для рейтинга популярности)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cs_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL REFERENCES cs_scenarios(id) ON DELETE CASCADE,
                    user_id TEXT DEFAULT '',
                    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS processed_webhook_events (
                    event_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'processing',
                    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT,
                    last_error TEXT DEFAULT ''
                )
            """)

            # Индексы
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_scenarios_status ON cs_scenarios(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_scenarios_myboard_id ON cs_scenarios(myboard_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_scenarios_edit_mode ON cs_scenarios(edit_mode)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_nodes_scenario ON cs_nodes(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_edges_scenario ON cs_edges(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_views_scenario ON cs_views(scenario_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cs_views_date ON cs_views(viewed_at)")

            conn.commit()

    # ─── Категории ────────────────────────────────────────────────

    def get_categories(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.*,
                    (SELECT COUNT(*)
                     FROM cs_scenarios s
                     WHERE s.category_id = c.id AND s.status = 'active') as active_count
                FROM cs_categories c
                ORDER BY c.sort_order, c.name
            """).fetchall()
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
                     AND NULLIF(v.viewed_at, '')::timestamp >= (CURRENT_TIMESTAMP - INTERVAL '30 days')) as views_30d,
                    (SELECT COUNT(*) FROM cs_nodes n
                     WHERE n.scenario_id = s.id) as node_count
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
                SELECT s.*, c.name as category_name, c.icon as category_icon,
                    (SELECT COUNT(*) FROM cs_nodes n WHERE n.scenario_id = s.id) as node_count
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

    def get_scenario_by_myboard_id(self, myboard_id: str) -> Optional[Dict]:
        if not myboard_id:
            return None
        with self._connect() as conn:
            row = conn.execute("""
                SELECT s.*, c.name as category_name, c.icon as category_icon
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                WHERE s.myboard_id = ?
                ORDER BY s.id DESC
                LIMIT 1
            """, (myboard_id,)).fetchone()
            return dict(row) if row else None

    def get_imported_myboard_index(self) -> Dict[str, Dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, title, myboard_id, myboard_version, myboard_updated_at,
                       has_local_changes, edit_mode, last_synced_at
                FROM cs_scenarios
                WHERE is_imported_from_myboard = 1 AND myboard_id IS NOT NULL
            """).fetchall()
            return {str(r['myboard_id']): dict(r) for r in rows if r['myboard_id']}

    def create_scenario(self, title: str, description: str = '', category_id: int = None,
                        tags: str = '', created_by: str = '') -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_scenarios (title, description, category_id, tags,
                    status, version, created_by, updated_by, has_draft_changes,
                    draft_updated_by, draft_updated_at)
                VALUES (?, ?, ?, ?, 'draft', 0, ?, ?, 1, ?, CURRENT_TIMESTAMP)
            """, (title, description, category_id, tags, created_by, created_by, created_by))
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

    def mark_scenario_local_change(self, scenario_id: int, updated_by: str = ''):
        self.mark_draft_change(scenario_id, updated_by)

    def mark_draft_change(self, scenario_id: int, updated_by: str = ''):
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios
                SET has_draft_changes = 1,
                    draft_updated_by = CASE WHEN ? != '' THEN ? ELSE draft_updated_by END,
                    draft_updated_at = CURRENT_TIMESTAMP,
                    has_local_changes = CASE WHEN is_imported_from_myboard = 1 THEN 1 ELSE has_local_changes END,
                    edit_mode = CASE
                        WHEN is_imported_from_myboard != 1 THEN edit_mode
                        WHEN edit_mode = 'conflict' THEN 'conflict'
                        WHEN edit_mode = 'detached' THEN 'detached'
                        ELSE 'local_modified'
                    END,
                    updated_by = CASE WHEN ? != '' THEN ? ELSE updated_by END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (updated_by, updated_by, updated_by, updated_by, scenario_id))
            conn.commit()

    def set_myboard_state(self, scenario_id: int, edit_mode: str,
                          remote_version: int | None = None,
                          remote_updated_at: str | None = None):
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios
                SET edit_mode = ?,
                    myboard_version = COALESCE(?, myboard_version),
                    myboard_updated_at = COALESCE(?, myboard_updated_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (edit_mode, remote_version, remote_updated_at, scenario_id))
            conn.commit()

    def publish_scenario(self, scenario_id: int, updated_by: str = '', comment: str = ''):
        """Публикация черновика → активный + создание версии"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM cs_scenarios WHERE id=?", (scenario_id,)
            ).fetchone()
            if not row:
                return False
            latest = conn.execute(
                "SELECT MAX(version) as version FROM cs_versions WHERE scenario_id=?",
                (scenario_id,)
            ).fetchone()
            new_version = int((latest or {})['version'] or 0) + 1
            # Сохраняем снапшот
            snapshot = self._build_snapshot(scenario_id, conn)
            conn.execute("""
                INSERT INTO cs_versions (scenario_id, version, snapshot_json, created_by, comment)
                VALUES (?, ?, ?, ?, ?)
            """, (scenario_id, new_version, json.dumps(snapshot, ensure_ascii=False), updated_by, comment))
            conn.execute("""
                UPDATE cs_scenarios SET status='active', version=?,
                    has_draft_changes=0, draft_updated_by='', draft_updated_at=NULL,
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
                INSERT INTO cs_scenarios (
                    title, description, category_id, tags, status, version,
                    created_by, updated_by, edit_mode, is_imported_from_myboard,
                    copied_from_scenario_id, copied_from_myboard_id,
                    copied_from_myboard_version
                )
                VALUES (?, ?, ?, ?, 'draft', 1, ?, ?, 'detached', 0, ?, ?, ?)
            """, (
                f"{orig['title']} (копия)",
                orig['description'],
                orig['category_id'],
                orig['tags'],
                created_by,
                created_by,
                scenario_id,
                orig['myboard_id'],
                orig['myboard_version'],
            ))
            new_id = cur.lastrowid

            # Копируем узлы
            nodes = conn.execute(
                "SELECT * FROM cs_nodes WHERE scenario_id=?", (scenario_id,)
            ).fetchall()
            node_map = {}  # old_id -> new_id
            for node in nodes:
                c2 = conn.execute("""
                    INSERT INTO cs_nodes (scenario_id, node_type, title, content, is_root,
                        sort_order, answer_text, final_answer, internal_note, documents, links,
                        pos_x, pos_y, source_node_id, source_type, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_id, node['node_type'], node['title'], node['content'],
                      node['is_root'], node['sort_order'], node['answer_text'],
                      node['final_answer'], node['internal_note'],
                      node['documents'], node['links'], node['pos_x'], node['pos_y'],
                      node['source_node_id'], node['source_type'], node['raw_data']))
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
                        INSERT INTO cs_edges (
                            scenario_id, from_node_id, to_node_id, label,
                            sort_order, condition, source_edge_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        new_id, new_from, new_to, edge['label'], edge['sort_order'],
                        edge['condition'], edge['source_edge_id']
                    ))

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
                    title: str = '', content: str = '', is_root: bool = False,
                    sort_order: int = 0, answer_text: str = '',
                    final_answer: str = '', internal_note: str = '',
                    documents: str = '', links: str = '', pos_x: float = 0,
                    pos_y: float = 0, source_node_id: str = '',
                    source_type: str = '', raw_data: str = '') -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_nodes (
                    scenario_id, node_type, title, content, is_root, sort_order,
                    answer_text, final_answer, internal_note, documents, links,
                    pos_x, pos_y, source_node_id, source_type, raw_data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scenario_id, node_type, title, content, 1 if is_root else 0,
                sort_order, answer_text, final_answer, internal_note, documents, links,
                pos_x, pos_y, source_node_id, source_type, raw_data
            ))
            conn.commit()
            return cur.lastrowid

    def update_node(self, node_id: int, data: dict):
        allowed = ['node_type', 'title', 'content', 'is_root', 'sort_order',
                   'answer_text', 'final_answer', 'internal_note', 'documents', 'links',
                   'pos_x', 'pos_y', 'source_node_id', 'source_type', 'raw_data']
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

    def update_layout(self, positions: list):
        """Сохранить позиции узлов на canvas: [{'id': 1, 'x': 100, 'y': 200}, ...]"""
        with self._connect() as conn:
            for item in positions:
                conn.execute(
                    "UPDATE cs_nodes SET pos_x=?, pos_y=? WHERE id=?",
                    (item.get('x', 0), item.get('y', 0), item['id'])
                )
            conn.commit()

    # ─── Рёбра ─────────────────────────────────────────────────────

    def create_edge(self, scenario_id: int, from_node_id: int,
                    to_node_id: int, label: str = '', sort_order: int = 0,
                    condition: str = '', source_edge_id: str = '') -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cs_edges (
                    scenario_id, from_node_id, to_node_id, label,
                    sort_order, condition, source_edge_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                scenario_id, from_node_id, to_node_id, label,
                sort_order, condition, source_edge_id
            ))
            conn.commit()
            return cur.lastrowid

    def get_edge(self, edge_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cs_edges WHERE id=?", (edge_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_edge(self, edge_id: int, label: str, sort_order: int = 0, condition: str = ''):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cs_edges SET label=?, sort_order=?, condition=? WHERE id=?",
                (label, sort_order, condition, edge_id)
            )
            conn.commit()

    def delete_edge(self, edge_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_edges WHERE id=?", (edge_id,))
            conn.commit()

    # ─── MyBoard import / sync ─────────────────────────────────────

    def import_myboard_scenario(self, converted: dict, imported_by: str = '',
                                overwrite_scenario_id: int | None = None,
                                copy_from_scenario_id: int | None = None) -> int:
        """Создать или перезаписать сценарий из нормализованного экспорта MyBoard."""
        title = converted.get('title') or 'Без названия'
        description = converted.get('description') or ''
        myboard_id = converted.get('myboard_id') or converted.get('id') or ''
        myboard_version = converted.get('version')
        myboard_updated_at = converted.get('updated_at') or ''
        status = 'active' if converted.get('status') in ('published', 'active') else 'draft'
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        linked_myboard_id = None if copy_from_scenario_id else myboard_id
        imported_flag = 0 if copy_from_scenario_id else 1
        edit_mode = 'detached' if copy_from_scenario_id else 'synced'

        with self._connect() as conn:
            if overwrite_scenario_id:
                existing = conn.execute(
                    "SELECT * FROM cs_scenarios WHERE id=?",
                    (overwrite_scenario_id,),
                ).fetchone()
                if not existing:
                    raise ValueError('Scenario not found')
                scenario_id = overwrite_scenario_id
                conn.execute("""
                    UPDATE cs_scenarios
                    SET title=?, description=?, status=?, myboard_id=?,
                        myboard_version=?, myboard_updated_at=?,
                        last_synced_at=?, has_local_changes=0,
                        edit_mode='synced', is_imported_from_myboard=1,
                        updated_by=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (
                    title, description, status, myboard_id, myboard_version,
                    myboard_updated_at, now, imported_by, scenario_id
                ))
                conn.execute("DELETE FROM cs_edges WHERE scenario_id=?", (scenario_id,))
                conn.execute("DELETE FROM cs_nodes WHERE scenario_id=?", (scenario_id,))
            else:
                cur = conn.execute("""
                    INSERT INTO cs_scenarios (
                        title, description, category_id, status, version, tags,
                        created_by, updated_by, myboard_id, myboard_version,
                        myboard_updated_at, last_synced_at, has_local_changes,
                        edit_mode, is_imported_from_myboard, copied_from_scenario_id,
                        copied_from_myboard_id, copied_from_myboard_version
                    )
                    VALUES (?, ?, NULL, ?, 1, '', ?, ?, ?, ?, ?, ?, 0,
                            ?, ?, ?, ?, ?)
                """, (
                    title, description, status, imported_by, imported_by,
                    linked_myboard_id, myboard_version, myboard_updated_at, now,
                    edit_mode, imported_flag,
                    copy_from_scenario_id, myboard_id if copy_from_scenario_id else None,
                    myboard_version if copy_from_scenario_id else None
                ))
                scenario_id = cur.lastrowid

            node_map: dict[str, int] = {}
            for idx, node in enumerate(converted.get('nodes') or []):
                node_type = node.get('node_type') or 'question'
                text = node.get('text') or ''
                final_answer = node.get('final_answer') or ''
                if node_type in ('final', 'end') and not final_answer:
                    final_answer = text
                cur = conn.execute("""
                    INSERT INTO cs_nodes (
                        scenario_id, node_type, title, content, is_root,
                        sort_order, answer_text, final_answer, internal_note,
                        documents, links, pos_x, pos_y, source_node_id,
                        source_type, raw_data
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    scenario_id,
                    node_type,
                    node.get('title') or '',
                    text,
                    1 if node.get('is_root') else 0,
                    idx,
                    node.get('answer_text') or '',
                    final_answer,
                    node.get('internal_note') or '',
                    node.get('documents') or '',
                    node.get('links') or '',
                    node.get('x') or 0,
                    node.get('y') or 0,
                    node.get('source_node_id') or '',
                    node.get('source_type') or '',
                    node.get('raw_data') or '',
                ))
                source_node_id = str(node.get('source_node_id') or '')
                if source_node_id:
                    node_map[source_node_id] = cur.lastrowid

            for idx, edge in enumerate(converted.get('edges') or []):
                from_id = node_map.get(str(edge.get('from') or ''))
                to_id = node_map.get(str(edge.get('to') or ''))
                if not from_id or not to_id:
                    continue
                conn.execute("""
                    INSERT INTO cs_edges (
                        scenario_id, from_node_id, to_node_id, label,
                        sort_order, condition, source_edge_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    scenario_id,
                    from_id,
                    to_id,
                    edge.get('label') or '',
                    idx,
                    edge.get('condition') or '',
                    edge.get('source_edge_id') or '',
                ))

            if status == 'active':
                latest = conn.execute(
                    "SELECT MAX(version) as version FROM cs_versions WHERE scenario_id=?",
                    (scenario_id,)
                ).fetchone()
                try:
                    publish_version = int(myboard_version or 0)
                except (TypeError, ValueError):
                    publish_version = 0
                if publish_version <= int((latest or {})['version'] or 0):
                    publish_version = int((latest or {})['version'] or 0) + 1
                if publish_version < 1:
                    publish_version = 1
                snapshot = self._build_snapshot(scenario_id, conn)
                conn.execute("""
                    INSERT INTO cs_versions (scenario_id, version, snapshot_json, created_by, comment)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    scenario_id,
                    publish_version,
                    json.dumps(snapshot, ensure_ascii=False),
                    imported_by,
                    'Imported from MyBoard',
                ))
                conn.execute("""
                    UPDATE cs_scenarios
                    SET version=?, has_draft_changes=0,
                        draft_updated_by='', draft_updated_at=NULL
                    WHERE id=?
                """, (publish_version, scenario_id))

            conn.commit()
            return scenario_id

    def start_webhook_event(self, event_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM processed_webhook_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row and row['status'] == 'processed':
                return 'processed'
            if row:
                conn.execute("""
                    UPDATE processed_webhook_events
                    SET status='processing', received_at=CURRENT_TIMESTAMP,
                        processed_at=NULL, last_error=''
                    WHERE event_id=?
                """, (event_id,))
            else:
                conn.execute("""
                    INSERT INTO processed_webhook_events (event_id, status, received_at)
                    VALUES (?, 'processing', CURRENT_TIMESTAMP)
                """, (event_id,))
            conn.commit()
            return 'processing'

    def finish_webhook_event(self, event_id: str, status: str = 'processed',
                             last_error: str = ''):
        with self._connect() as conn:
            conn.execute("""
                UPDATE processed_webhook_events
                SET status=?, processed_at=CURRENT_TIMESTAMP, last_error=?
                WHERE event_id=?
            """, (status, last_error, event_id))
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
                SELECT s.id, s.title, s.description, s.tags, s.category_id,
                    c.name as category_name, c.icon as category_icon,
                    COUNT(v.id) as views_30d,
                    (SELECT COUNT(*) FROM cs_nodes n WHERE n.scenario_id = s.id) as node_count
                FROM cs_scenarios s
                LEFT JOIN cs_categories c ON s.category_id = c.id
                LEFT JOIN cs_views v ON v.scenario_id = s.id
                    AND NULLIF(v.viewed_at, '')::timestamp >= (CURRENT_TIMESTAMP - INTERVAL '30 days')
                WHERE s.status = 'active'
                GROUP BY s.id, s.title, s.description, s.tags, s.category_id, c.name, c.icon, s.title, s.description, s.tags, s.category_id, c.name, c.icon
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

    def get_version(self, scenario_id: int, version_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT * FROM cs_versions WHERE scenario_id=? AND id=?
            """, (scenario_id, version_id)).fetchone()
            return dict(row) if row else None

    def get_latest_published_snapshot(self, scenario_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT * FROM cs_versions
                WHERE scenario_id=?
                ORDER BY version DESC, id DESC
                LIMIT 1
            """, (scenario_id,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row['snapshot_json'])
            except (TypeError, json.JSONDecodeError):
                return None

    def ensure_published_snapshot(self, scenario_id: int, created_by: str = '') -> bool:
        with self._connect() as conn:
            scenario = conn.execute(
                "SELECT * FROM cs_scenarios WHERE id=?", (scenario_id,)
            ).fetchone()
            if not scenario or scenario['status'] != 'active':
                return False
            existing = conn.execute(
                "SELECT id FROM cs_versions WHERE scenario_id=? LIMIT 1",
                (scenario_id,)
            ).fetchone()
            if existing:
                return True
            version = int(scenario['version'] or 1)
            if version < 1:
                version = 1
            snapshot = self._build_snapshot(scenario_id, conn)
            conn.execute("""
                INSERT INTO cs_versions (scenario_id, version, snapshot_json, created_by, comment)
                VALUES (?, ?, ?, ?, ?)
            """, (
                scenario_id,
                version,
                json.dumps(snapshot, ensure_ascii=False),
                created_by or scenario['updated_by'] or scenario['created_by'] or '',
                'Baseline before draft editing',
            ))
            conn.execute("""
                UPDATE cs_scenarios
                SET version=?, has_draft_changes=0,
                    draft_updated_by='', draft_updated_at=NULL
                WHERE id=?
            """, (version, scenario_id))
            conn.commit()
            return True

    def get_published_root_node(self, scenario_id: int) -> Optional[Dict]:
        snapshot = self.get_latest_published_snapshot(scenario_id)
        if not snapshot:
            return self.get_root_node(scenario_id)
        nodes = snapshot.get('nodes') or []
        root = next((n for n in nodes if int(n.get('is_root') or 0) == 1), None)
        if not root:
            root = next(iter(nodes), None)
        return dict(root) if root else None

    def get_published_nodes_count(self, scenario_id: int) -> int:
        snapshot = self.get_latest_published_snapshot(scenario_id)
        if not snapshot:
            return len(self.get_nodes(scenario_id))
        return len(snapshot.get('nodes') or [])

    def get_published_node_with_choices(self, scenario_id: int, node_id: int) -> Optional[dict]:
        snapshot = self.get_latest_published_snapshot(scenario_id)
        if not snapshot:
            node = self.get_node(node_id)
            if not node or node['scenario_id'] != scenario_id:
                return None
            return {'node': node, 'choices': self.get_node_choices(node_id)}

        nodes = {int(n.get('id')): dict(n) for n in snapshot.get('nodes') or [] if n.get('id') is not None}
        node = nodes.get(int(node_id))
        if not node:
            return None
        choices = []
        for edge in snapshot.get('edges') or []:
            if int(edge.get('from_node_id') or 0) != int(node_id):
                continue
            next_node = nodes.get(int(edge.get('to_node_id') or 0))
            if not next_node:
                continue
            choice = dict(edge)
            choice['next_title'] = next_node.get('title') or ''
            choice['next_type'] = next_node.get('node_type') or ''
            choices.append(choice)
        choices.sort(key=lambda item: (item.get('sort_order') or 0, item.get('id') or 0))
        return {'node': node, 'choices': choices}

    def validate_draft(self, scenario_id: int) -> dict:
        with self._connect() as conn:
            nodes = [dict(n) for n in conn.execute(
                "SELECT * FROM cs_nodes WHERE scenario_id=? ORDER BY sort_order, id",
                (scenario_id,)
            ).fetchall()]
            edges = [dict(e) for e in conn.execute(
                "SELECT * FROM cs_edges WHERE scenario_id=?",
                (scenario_id,)
            ).fetchall()]

        errors = []
        warnings = []
        if not nodes:
            errors.append('Нет блоков')
            return {'ok': False, 'errors': errors, 'warnings': warnings}

        node_ids = {int(n['id']) for n in nodes}
        roots = [n for n in nodes if int(n.get('is_root') or 0) == 1]
        if not roots:
            errors.append('Нет стартового блока')
        if len(roots) > 1:
            errors.append('Стартовый блок должен быть только один')

        for edge in edges:
            if not edge.get('from_node_id') or not edge.get('to_node_id'):
                errors.append(f"У перехода #{edge.get('id')} не выбран узел назначения")
                continue
            if int(edge['from_node_id']) not in node_ids or int(edge['to_node_id']) not in node_ids:
                errors.append(f"Переход #{edge.get('id')} указывает на несуществующий блок")

        for node in nodes:
            node_type = node.get('node_type') or 'question'
            title = (node.get('title') or '').strip()
            content = (node.get('content') or '').strip()
            final_answer = (node.get('final_answer') or '').strip()
            if node_type in ('question', 'message', 'condition', 'action') and not (title or content):
                errors.append(f"Блок #{node['id']} пустой")
            if node_type in ('final', 'end') and not (title or final_answer or content):
                errors.append(f"Финальный блок #{node['id']} пустой")

        if roots:
            reachable = set()
            graph = {}
            for edge in edges:
                if int(edge.get('from_node_id') or 0) in node_ids and int(edge.get('to_node_id') or 0) in node_ids:
                    graph.setdefault(int(edge['from_node_id']), []).append(int(edge['to_node_id']))
            stack = [int(roots[0]['id'])]
            while stack:
                current = stack.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                stack.extend(graph.get(current, []))
            unreachable = node_ids - reachable
            if unreachable:
                warnings.append(f"Недостижимых блоков: {len(unreachable)}")

        return {'ok': not errors, 'errors': errors, 'warnings': warnings}

    def replace_draft_from_snapshot(self, scenario_id: int, snapshot: dict, updated_by: str = ''):
        scenario_data = snapshot.get('scenario') or {}
        nodes = snapshot.get('nodes') or []
        edges = snapshot.get('edges') or []
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_edges WHERE scenario_id=?", (scenario_id,))
            conn.execute("DELETE FROM cs_nodes WHERE scenario_id=?", (scenario_id,))
            conn.execute("""
                UPDATE cs_scenarios
                SET title=COALESCE(NULLIF(?, ''), title),
                    description=?,
                    category_id=?,
                    tags=?,
                    has_draft_changes=1,
                    draft_updated_by=?,
                    draft_updated_at=CURRENT_TIMESTAMP,
                    updated_by=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                scenario_data.get('title') or '',
                scenario_data.get('description') or '',
                scenario_data.get('category_id'),
                scenario_data.get('tags') or '',
                updated_by,
                updated_by,
                scenario_id,
            ))
            node_map: dict[int, int] = {}
            for node in nodes:
                old_id = int(node.get('id') or 0)
                cur = conn.execute("""
                    INSERT INTO cs_nodes (
                        scenario_id, node_type, title, content, is_root,
                        sort_order, answer_text, final_answer, internal_note,
                        documents, links, pos_x, pos_y, source_node_id,
                        source_type, raw_data
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    scenario_id,
                    node.get('node_type') or 'question',
                    node.get('title') or '',
                    node.get('content') or '',
                    int(node.get('is_root') or 0),
                    int(node.get('sort_order') or 0),
                    node.get('answer_text') or '',
                    node.get('final_answer') or '',
                    node.get('internal_note') or '',
                    node.get('documents') or '',
                    node.get('links') or '',
                    node.get('pos_x') or 0,
                    node.get('pos_y') or 0,
                    node.get('source_node_id') or '',
                    node.get('source_type') or '',
                    node.get('raw_data') or '',
                ))
                if old_id:
                    node_map[old_id] = cur.lastrowid
            for edge in edges:
                from_id = node_map.get(int(edge.get('from_node_id') or 0))
                to_id = node_map.get(int(edge.get('to_node_id') or 0))
                if not from_id or not to_id:
                    continue
                conn.execute("""
                    INSERT INTO cs_edges (
                        scenario_id, from_node_id, to_node_id, label,
                        sort_order, condition, source_edge_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    scenario_id,
                    from_id,
                    to_id,
                    edge.get('label') or '',
                    int(edge.get('sort_order') or 0),
                    edge.get('condition') or '',
                    edge.get('source_edge_id') or '',
                ))
            conn.commit()

    def reset_draft_to_latest_published(self, scenario_id: int, updated_by: str = '') -> bool:
        snapshot = self.get_latest_published_snapshot(scenario_id)
        if not snapshot:
            return False
        self.replace_draft_from_snapshot(scenario_id, snapshot, updated_by)
        with self._connect() as conn:
            conn.execute("""
                UPDATE cs_scenarios
                SET has_draft_changes=0, draft_updated_by='', draft_updated_at=NULL,
                    updated_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (updated_by, scenario_id))
            conn.commit()
        return True

    def replace_draft_graph(self, scenario_id: int, data: dict, updated_by: str = ''):
        scenario_data = data.get('scenario') or {}
        nodes = data.get('nodes') or []
        edges = data.get('edges') or []
        with self._connect() as conn:
            conn.execute("DELETE FROM cs_edges WHERE scenario_id=?", (scenario_id,))
            conn.execute("DELETE FROM cs_nodes WHERE scenario_id=?", (scenario_id,))
            conn.execute("""
                UPDATE cs_scenarios
                SET title=COALESCE(NULLIF(?, ''), title),
                    description=?,
                    category_id=?,
                    tags=?,
                    has_draft_changes=1,
                    draft_updated_by=?,
                    draft_updated_at=CURRENT_TIMESTAMP,
                    updated_by=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                scenario_data.get('title') or '',
                scenario_data.get('description') or '',
                scenario_data.get('category_id'),
                scenario_data.get('tags') or '',
                updated_by,
                updated_by,
                scenario_id,
            ))
            node_map: dict[int, int] = {}
            for idx, node in enumerate(nodes):
                old_id = int(node.get('id') or 0)
                insert_id = old_id if old_id > 0 else None
                columns = [
                    'scenario_id', 'node_type', 'title', 'content', 'is_root',
                    'sort_order', 'answer_text', 'final_answer', 'internal_note',
                    'documents', 'links', 'pos_x', 'pos_y', 'source_node_id',
                    'source_type', 'raw_data'
                ]
                values = [
                    scenario_id,
                    node.get('node_type') or 'question',
                    node.get('title') or '',
                    node.get('content') or '',
                    int(node.get('is_root') or 0),
                    int(node.get('sort_order') if node.get('sort_order') is not None else idx),
                    node.get('answer_text') or '',
                    node.get('final_answer') or '',
                    node.get('internal_note') or '',
                    node.get('documents') or '',
                    node.get('links') or '',
                    node.get('pos_x') or 0,
                    node.get('pos_y') or 0,
                    node.get('source_node_id') or '',
                    node.get('source_type') or '',
                    node.get('raw_data') or '',
                ]
                if insert_id:
                    columns.insert(0, 'id')
                    values.insert(0, insert_id)
                placeholders = ','.join('?' for _ in values)
                cur = conn.execute(
                    f"INSERT INTO cs_nodes ({','.join(columns)}) VALUES ({placeholders})",
                    values
                )
                node_map[old_id] = insert_id or cur.lastrowid
            for idx, edge in enumerate(edges):
                old_id = int(edge.get('id') or 0)
                from_id = node_map.get(int(edge.get('from_node_id') or 0))
                to_id = node_map.get(int(edge.get('to_node_id') or 0))
                if not from_id or not to_id:
                    continue
                columns = [
                    'scenario_id', 'from_node_id', 'to_node_id', 'label',
                    'sort_order', 'condition', 'source_edge_id'
                ]
                values = [
                    scenario_id,
                    from_id,
                    to_id,
                    edge.get('label') or '',
                    int(edge.get('sort_order') if edge.get('sort_order') is not None else idx),
                    edge.get('condition') or '',
                    edge.get('source_edge_id') or '',
                ]
                if old_id > 0:
                    columns.insert(0, 'id')
                    values.insert(0, old_id)
                placeholders = ','.join('?' for _ in values)
                conn.execute(
                    f"INSERT INTO cs_edges ({','.join(columns)}) VALUES ({placeholders})",
                    values
                )
            conn.commit()

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
