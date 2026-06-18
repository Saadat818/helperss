"""
Менеджер справочника контактов КЦ.

Таблицы имеют префикс cc_ (contact center), чтобы не пересекаться с
мануалами, тематиками и сценариями в общей SQLite-базе topics.db.
"""

import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from db_backend import connect as db_connect, is_postgres_backend


CONTACT_DEPARTMENT_HIERARCHY = [
    {
        "name": "Департамент по клиентскому опыту и обслуживанию клиентов",
        "kind": "department",
        "children": [
            {
                "name": "Управление Контакт-Центра",
                "kind": "management",
                "aliases": [
                    "УПРАВЛЕНИЕ КОНТАКТ-ЦЕНТРА",
                    "УПРАВЛЕНИЯ КОНТАКТ-ЦЕНТРА",
                ],
                "children": [
                    {"name": "Отдел по работе с социальными сетями", "kind": "department"},
                    {
                        "name": "Отдел аналитики, отчетности и мониторинга показателей",
                        "kind": "department",
                        "aliases": [
                            "Управление Контакт-Центра / Отдел аналитики, отчетности и мониторинга показателей",
                        ],
                    },
                    {
                        "name": "Отдел обучения",
                        "kind": "department",
                        "aliases": ["Управление Контакт-Центра / Отдел обучения"],
                    },
                    {
                        "name": "Отдел сопровождения обслуживания (ГОПЗ)",
                        "kind": "department",
                        "aliases": ["Отдел сопровождения обслуживания"],
                        "children": [
                            {
                                "name": "Группа обработки претензий и запросов №1",
                                "kind": "group",
                                "aliases": ["Отдел сопровождения обслуживания / Группа обработки претензий и запросов"],
                            },
                            {"name": "Группа обработки претензий и запросов №2", "kind": "group"},
                            {"name": "Группа обработки претензий и запросов №3", "kind": "group"},
                            {"name": "Группа обработки претензий и запросов №4", "kind": "group"},
                            {"name": "Группа обработки претензий и запросов №5", "kind": "group"},
                            {
                                "name": "Группа по работе с обратной связью",
                                "kind": "group",
                                "aliases": ["Отдел сопровождения обслуживания / Группа по работе с обратной связью"],
                            },
                        ],
                    },
                    {
                        "name": "Отдел онлайн обслуживания",
                        "kind": "department",
                        "aliases": ["Отдел онлайн обращений"],
                        "children": [
                            {
                                "name": "Группа онлайн обслуживания высокодоходных клиентов",
                                "kind": "group",
                                "aliases": ["Отдел онлайн обращений / Группа обслуживания высокодоходных клиентов 1 и 2"],
                            },
                            {
                                "name": "Группа онлайн обращений №1",
                                "kind": "group",
                                "aliases": ["Отдел онлайн обращений / Группа онлайн обслуживания"],
                            },
                            {"name": "Группа онлайн обращений №2", "kind": "group"},
                            {"name": "Группа онлайн обращений №3", "kind": "group"},
                            {"name": "Группа онлайн обращений №4", "kind": "group"},
                        ],
                    },
                    {
                        "name": "Отдел оперативного обслуживания клиентов (ОООК)",
                        "kind": "department",
                        "children": [
                            {
                                "name": "Группа обслуживания высокодоходных клиентов №1",
                                "kind": "group",
                                "aliases": [
                                    "Отдел оперативного обслуживания клиентов (ОООК) / Группа обслуживания высокодоходных клиентов 1 и 2",
                                ],
                            },
                            {"name": "Группа обслуживания высокодоходных клиентов №2", "kind": "group"},
                            {
                                "name": "Группа оперативного обслуживания №1",
                                "kind": "group",
                                "aliases": ["Отдел оперативного обслуживания клиентов (ОООК) / Группа оперативного обслуживания"],
                            },
                            {"name": "Группа оперативного обслуживания №2", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №3", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №4", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №5", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №6", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №7", "kind": "group"},
                            {"name": "Группа оперативного обслуживания №8", "kind": "group"},
                            {
                                "name": "Группа обслуживания MIslamic",
                                "kind": "group",
                                "aliases": ["Отдел оперативного обслуживания клиентов (ОООК) / Группа обслуживания MIslamic"],
                            },
                            {
                                "name": "Группа Антифрод (предотвращение мошенничества)",
                                "kind": "group",
                                "aliases": ["Отдел оперативного обслуживания клиентов (ОООК) / Группа Антифрод"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Управление проектами клиентского опыта и автоматизации обслуживания",
                "kind": "management",
                "aliases": [
                    "Управление Проектами клиентского опыта и Автоматизации обслуживания",
                ],
                "children": [
                    {"name": "Отдел автоматизации процессов обслуживания", "kind": "department"},
                    {"name": "Отдел поддержки обслуживания", "kind": "department"},
                    {
                        "name": "Отдел клиентского опыта",
                        "kind": "department",
                        "aliases": ["Отдел управления клиентским опытом"],
                    },
                ],
            },
            {
                "name": "Отдел дистанционного обслуживания юридических лиц",
                "kind": "department",
                "aliases": ["Управление Контакт-Центра / Отдел дистанционного обслуживания юридических лиц"],
                "children": [
                    {
                        "name": "Группа приема звонков №1",
                        "kind": "group",
                        "aliases": ["Отдел дистанционного обслуживания юридических лиц / Группа приема звонков"],
                    },
                    {"name": "Группа приема звонков №2", "kind": "group"},
                    {
                        "name": "Группа онлайн обслуживания",
                        "kind": "group",
                        "aliases": ["Отдел дистанционного обслуживания юридических лиц / Группа онлайн обслуживания"],
                    },
                    {
                        "name": "Группа развития клиентов",
                        "kind": "group",
                        "aliases": ["Отдел дистанционного обслуживания юридических лиц / Группа развития клиентов"],
                    },
                ],
            },
            {
                "name": "Отдел развития обслуживания контакт-центра",
                "kind": "department",
                "aliases": [
                    "Отдел развития обслуживания",
                    "Управление Контакт-Центра / Отдел развития обслуживания",
                    "Управление Контакт-Центра / Отдел развития обслуживания контакт-центра",
                ],
            },
        ],
    }
]


def _contact_department_key(value: str) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = text.replace("№", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contact_department_anchor_key(value: str) -> str:
    text = _contact_department_key(value)
    text = re.sub(r"[^0-9a-zа-я]+", "-", text)
    return text.strip("-") or "department"


def _flatten_department_hierarchy(nodes, parent_path=(), parent_kinds=(), parent_order=()):
    records = []
    for index, node in enumerate(nodes):
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        kind = str(node.get("kind") or "department").strip() or "department"
        path = (*parent_path, name)
        kinds = (*parent_kinds, kind)
        sort_key = (*parent_order, index)
        records.append({
            "name": name,
            "kind": kind,
            "path": path,
            "kinds": kinds,
            "level": len(path) - 1,
            "sort_key": sort_key,
            "aliases": tuple(str(alias).strip() for alias in node.get("aliases", ()) if str(alias).strip()),
        })
        records.extend(_flatten_department_hierarchy(node.get("children", ()), path, kinds, sort_key))
    return records


CONTACT_HIERARCHY_RECORDS = _flatten_department_hierarchy(CONTACT_DEPARTMENT_HIERARCHY)
CONTACT_DEPARTMENT_ORDER = [record["name"] for record in CONTACT_HIERARCHY_RECORDS]
CONTACT_HIERARCHY_BY_KEY = {
    _contact_department_key(record["name"]): record
    for record in CONTACT_HIERARCHY_RECORDS
}
CONTACT_DEPARTMENT_CANONICAL_BY_KEY = {}
for _record in CONTACT_HIERARCHY_RECORDS:
    CONTACT_DEPARTMENT_CANONICAL_BY_KEY[_contact_department_key(_record["name"])] = _record["name"]
    for _alias in _record["aliases"]:
        CONTACT_DEPARTMENT_CANONICAL_BY_KEY[_contact_department_key(_alias)] = _record["name"]


CONTACT_POSITION_OPTIONS = [
    "Директор Департамента",
    "Руководитель",
    "Руководитель Управления",
    "Руководитель Управления Контакт-Центра",
    "Руководитель Управления проектами клиентского опыта и автоматизации обслуживания",
    "Начальник",
    "Начальник Управления",
    "Начальник отдела",
    "Руководитель группы",
    "Помощник Председателя Правления",
    "Менеджер по подбору и адаптации персонала",
    "Главный специалист по расчету премий и анализа",
    "Специалист по поддержке бизнес-процессов",
    "Главный аналитик",
    "Главный аналитик клиентских метрик",
    "Аналитик клиентских метрик",
    "Проектный менеджер клиентских решений",
    "Проектный менеджер по развитию систем",
    "Главный специалист по цифровизации и автоматизации обслуживания",
    "Главный эксперт по автоматизации обслуживания",
    "Главный специалист по контенту и сценариям чат-бота",
    "Главный специалист по оптимизации и стандартизации процессов",
    "Сервис дизайнер",
    "Менеджер клиентского опыта",
    "Коммуникатор",
    "Эксперт клиентского опыта",
    "Тестировщик",
    "Главный специалист по языковой поддержке",
    "Главный специалист по голосовым каналам связи",
    "Главный специалист по текстовым каналам связи",
    "Главный специалист по контентной поддержке",
    "Главный специалист по организации рабочих мест",
    "Главный эксперт по сценариям и качеству голосового ИИ-бота",
    "Главный специалист по сценариям и качеству голосового ИИ-бота",
    "Главный эксперт по развитию обслуживания",
    "Главный эксперт по управление качеством обслуживания",
    "Главный специалист по ресурсному планированию",
    "Главный специалист по мониторингу показателей",
    "Старший специалист по мониторингу показателей",
    "Старший специалист",
    "Ведущий специалист",
    "Главный эксперт",
    "Главный специалист",
    "Специалист",
    "Менеджер по обучению",
    "Графический дизайнер",
    "Методист по разработке и озвучиванию обучающих курсов",
    "Инспектор-архивариус",
]


class ContactsManager:
    """CRUD контактов КЦ."""

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        if not is_postgres_backend():
            self._init_db()

    def _connect(self):
        conn = db_connect(self.db_path, check_same_thread=False, timeout=10.0)
        if not is_postgres_backend():
            conn.create_function("unicode_casefold", 1, lambda value: str(value or "").casefold())
            conn.create_function("digits_only", 1, lambda value: ContactsManager._digits_only(value))
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
                    nickname TEXT DEFAULT '',
                    group_role TEXT DEFAULT 'specialist',
                    workplace TEXT DEFAULT '',
                    schedule TEXT DEFAULT '',
                    languages TEXT DEFAULT '',
                    responsibilities TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    photo_path TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    inactive_reason TEXT DEFAULT '',
                    inactive_date TEXT DEFAULT '',
                    inactive_comment TEXT DEFAULT '',
                    inactive_by TEXT DEFAULT '',
                    inactive_at TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    parent_name TEXT DEFAULT '',
                    kind TEXT DEFAULT 'group',
                    aliases TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_by TEXT DEFAULT '',
                    updated_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_department_overrides (
                    original_name TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    parent_name TEXT DEFAULT '',
                    kind TEXT DEFAULT 'group',
                    aliases TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS cc_section_visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    section TEXT NOT NULL,
                    visitor_key TEXT NOT NULL,
                    visitor_name TEXT DEFAULT '',
                    visit_date TEXT NOT NULL,
                    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    visit_count INTEGER DEFAULT 1,
                    last_path TEXT DEFAULT '',
                    UNIQUE(section, visitor_key, visit_date)
                )
            """)
            c.execute("PRAGMA table_info(cc_contacts)")
            existing_contact_cols = {row[1] for row in c.fetchall()}
            if "photo_path" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN photo_path TEXT DEFAULT ''")
            if "languages" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN languages TEXT DEFAULT ''")
            if "nickname" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN nickname TEXT DEFAULT ''")
            if "group_role" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN group_role TEXT DEFAULT 'specialist'")
            if "inactive_reason" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN inactive_reason TEXT DEFAULT ''")
            if "inactive_date" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN inactive_date TEXT DEFAULT ''")
            if "inactive_comment" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN inactive_comment TEXT DEFAULT ''")
            if "inactive_by" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN inactive_by TEXT DEFAULT ''")
            if "inactive_at" not in existing_contact_cols:
                c.execute("ALTER TABLE cc_contacts ADD COLUMN inactive_at TEXT DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_direction ON cc_contacts(direction_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_department ON cc_contacts(department)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_active ON cc_contacts(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_inactive_date ON cc_contacts(inactive_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_name ON cc_contacts(full_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_email ON cc_contacts(email)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contacts_nickname ON cc_contacts(nickname)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_directions_active ON cc_directions(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_departments_name ON cc_departments(name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_departments_parent ON cc_departments(parent_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_departments_active ON cc_departments(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_department_overrides_active ON cc_department_overrides(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact_likes_contact ON cc_contact_likes(contact_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact_likes_actor ON cc_contact_likes(actor_key)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_section_visits_section_date ON cc_section_visits(section, visit_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cc_section_visits_visitor ON cc_section_visits(visitor_key)")
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
    def _int_value(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _digits_only(value) -> str:
        return re.sub(r"\D+", "", str(value or ""))

    @staticmethod
    def _search_tokens(value: str, limit: int = 8) -> List[str]:
        text = ContactsManager._clean(value, 200).casefold()
        tokens = [token for token in re.split(r"\s+", text) if token]
        return tokens[:limit]

    @staticmethod
    def _department_key(value: str) -> str:
        return _contact_department_key(value)

    @staticmethod
    def _department_kind(value: str) -> str:
        kind = str(value or "").strip().lower()
        if kind in {"management", "управление"}:
            return "management"
        if kind in {"department", "отдел"}:
            return "department"
        return "group"

    @staticmethod
    def _department_aliases(value: str) -> tuple[str, ...]:
        return tuple(
            ContactsManager._clean(line, 220)
            for line in str(value or "").splitlines()
            if ContactsManager._clean(line, 220)
        )

    @staticmethod
    def _record_clone(record: Dict) -> Dict:
        item = dict(record)
        item["path"] = tuple(record.get("path") or ())
        item["kinds"] = tuple(record.get("kinds") or ())
        item["sort_key"] = tuple(record.get("sort_key") or ())
        item["aliases"] = tuple(record.get("aliases") or ())
        item.setdefault("is_custom", False)
        item.setdefault("id", None)
        return item

    def _custom_department_rows(self) -> List[Dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT id, name, parent_name, kind, aliases, sort_order, is_active,
                       created_by, updated_by, created_at, updated_at
                FROM cc_departments
                WHERE is_active = 1
                ORDER BY sort_order, name
            """).fetchall()]

    def _department_override_rows(self) -> Dict[str, Dict]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("""
                SELECT original_name, name, parent_name, kind, aliases, sort_order, is_active,
                       created_by, updated_by, created_at, updated_at
                FROM cc_department_overrides
                WHERE is_active = 1
            """).fetchall()]
        return {
            self._department_key(row.get("original_name") or ""): row
            for row in rows
            if self._department_key(row.get("original_name") or "")
        }

    @staticmethod
    def _unique_department_aliases(values: List[str]) -> tuple[str, ...]:
        seen = set()
        result = []
        for value in values:
            cleaned = ContactsManager._clean(value, 220)
            key = ContactsManager._department_key(cleaned)
            if cleaned and key and key not in seen:
                seen.add(key)
                result.append(cleaned)
        return tuple(result)

    def _department_records(self) -> List[Dict]:
        overrides = self._department_override_rows()
        base_display_by_original = {}
        for record in CONTACT_HIERARCHY_RECORDS:
            override = overrides.get(self._department_key(record["name"]))
            name = self._clean(override.get("name"), 220) if override else ""
            base_display_by_original[record["name"]] = name or record["name"]

        pending = []
        for record in CONTACT_HIERARCHY_RECORDS:
            original_name = record["name"]
            override = overrides.get(self._department_key(original_name))
            name = self._clean(override.get("name"), 220) if override else ""
            name = name or original_name
            parent_original = record["path"][-2] if len(record.get("path") or ()) > 1 else ""
            parent_name = (
                self._clean(override.get("parent_name"), 220)
                if override is not None
                else base_display_by_original.get(parent_original, parent_original)
            )
            if override is None and not parent_original:
                parent_name = ""
            original_aliases = list(record.get("aliases") or ())
            if self._department_key(name) != self._department_key(original_name):
                original_aliases.append(original_name)
            override_aliases = self._department_aliases(override.get("aliases")) if override else ()
            aliases = self._unique_department_aliases([*original_aliases, *override_aliases])
            try:
                sort_order = int(override.get("sort_order") if override else 0)
            except (TypeError, ValueError, IndexError):
                sort_order = 0
            pending.append({
                "source": "base",
                "source_id": self._department_key(original_name),
                "original_name": original_name,
                "id": None,
                "name": name,
                "parent_name": parent_name,
                "kind": self._department_kind(override.get("kind") if override else record.get("kind")),
                "aliases": aliases,
                "sort_order": sort_order,
                "original_sort_key": tuple(record.get("sort_key") or ()),
                "is_custom": False,
            })

        for row in self._custom_department_rows():
            pending.append({
                "source": "custom",
                "source_id": str(int(row.get("id") or 0)),
                "original_name": "",
                "id": int(row.get("id") or 0),
                "name": self._clean(row.get("name"), 220),
                "parent_name": self._clean(row.get("parent_name"), 220),
                "kind": self._department_kind(row.get("kind")),
                "aliases": self._department_aliases(row.get("aliases")),
                "sort_order": int(row.get("sort_order") or 0),
                "original_sort_key": (),
                "is_custom": True,
            })

        records = []
        by_key = {}
        canonical_by_key = {}
        dynamic_root_order = len(CONTACT_HIERARCHY_RECORDS) + 1000

        while pending:
            remaining = []
            added = False
            for row in pending:
                name = self._clean(row.get("name"), 220)
                key = self._department_key(name)
                if not name or not key or key in by_key:
                    continue

                parent_raw = self._clean(row.get("parent_name"), 220)
                parent_name = canonical_by_key.get(self._department_key(parent_raw), parent_raw)
                parent_record = by_key.get(self._department_key(parent_name)) if parent_name else None
                if parent_raw and not parent_record:
                    remaining.append(row)
                    continue

                try:
                    sort_order = int(row.get("sort_order") or 0)
                except (TypeError, ValueError):
                    sort_order = 0
                if row.get("source") == "base":
                    base_position = tuple(int(part) for part in (row.get("original_sort_key") or (sort_order,)))
                    position = (*base_position[:-1], base_position[-1] + sort_order * 1000) if base_position else (sort_order,)
                else:
                    position = (1000 + sort_order * 1000 + int(row.get("id") or 0),)
                path = (*parent_record["path"], name) if parent_record else (name,)
                kinds = (*parent_record["kinds"], self._department_kind(row.get("kind"))) if parent_record else (
                    self._department_kind(row.get("kind")),
                )
                sort_key = (*parent_record["sort_key"], position[-1]) if parent_record else (
                    *(position if row.get("source") == "base" else (dynamic_root_order + position[-1],)),
                )
                aliases = tuple(row.get("aliases") or ())
                record = {
                    "id": int(row.get("id") or 0),
                    "source": row.get("source") or "custom",
                    "source_id": row.get("source_id") or str(int(row.get("id") or 0)),
                    "original_name": row.get("original_name") or "",
                    "name": name,
                    "kind": self._department_kind(row.get("kind")),
                    "path": path,
                    "kinds": kinds,
                    "level": len(path) - 1,
                    "sort_key": sort_key,
                    "sort_order": sort_order,
                    "aliases": aliases,
                    "is_custom": bool(row.get("is_custom")),
                }
                records.append(record)
                by_key[key] = record
                canonical_by_key[key] = name
                for alias in aliases:
                    canonical_by_key[self._department_key(alias)] = name
                added = True

            if not remaining or not added:
                for row in remaining:
                    name = self._clean(row.get("name"), 220)
                    key = self._department_key(name)
                    if not name or not key or key in by_key:
                        continue
                    try:
                        sort_order = int(row.get("sort_order") or 0)
                    except (TypeError, ValueError):
                        sort_order = 0
                    position = 1000 + sort_order * 1000 + int(row.get("id") or 0)
                    aliases = tuple(row.get("aliases") or ())
                    record = {
                        "id": int(row.get("id") or 0),
                        "source": row.get("source") or "custom",
                        "source_id": row.get("source_id") or str(int(row.get("id") or 0)),
                        "original_name": row.get("original_name") or "",
                        "name": name,
                        "kind": self._department_kind(row.get("kind")),
                        "path": (name,),
                        "kinds": (self._department_kind(row.get("kind")),),
                        "level": 0,
                        "sort_key": (dynamic_root_order + position,),
                        "sort_order": sort_order,
                        "aliases": aliases,
                        "is_custom": bool(row.get("is_custom")),
                    }
                    records.append(record)
                    by_key[key] = record
                    canonical_by_key[key] = name
                    for alias in aliases:
                        canonical_by_key[self._department_key(alias)] = name
                break
            pending = remaining

        return sorted(records, key=lambda item: item.get("sort_key") or ())

    def _department_context(self) -> Dict:
        records = self._department_records()
        by_key = {}
        canonical_by_key = {}
        for record in records:
            key = self._department_key(record["name"])
            by_key[key] = record
            canonical_by_key[key] = record["name"]
            for alias in record.get("aliases", ()):
                canonical_by_key[self._department_key(alias)] = record["name"]
        return {
            "records": records,
            "by_key": by_key,
            "canonical_by_key": canonical_by_key,
        }

    def _canonical_department(self, value: str, context: Optional[Dict] = None) -> str:
        cleaned = self._clean(value, 220)
        if not cleaned:
            return ""
        context = context or self._department_context()
        return context["canonical_by_key"].get(self._department_key(cleaned), cleaned)

    def _department_record(self, department: str, context: Optional[Dict] = None) -> Optional[Dict]:
        context = context or self._department_context()
        return context["by_key"].get(self._department_key(self._canonical_department(department, context)))

    def _department_filter_values(self, department: str, context: Optional[Dict] = None) -> List[str]:
        context = context or self._department_context()
        canonical = self._canonical_department(department, context)
        if not canonical:
            return []
        record = self._department_record(canonical, context)
        if not record:
            return [canonical]

        values = []
        for candidate in context["records"]:
            if candidate["path"][:len(record["path"])] != record["path"]:
                continue
            values.append(candidate["name"])
            values.extend(candidate["aliases"])

        seen = set()
        result = []
        for value in values:
            key = self._department_key(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return result or [canonical]

    def _department_sort_key(self, department: str, context: Optional[Dict] = None):
        context = context or self._department_context()
        name = self._canonical_department(department, context) or "Без отдела"
        record = self._department_record(name, context)
        if record:
            sort_path = tuple((0, int(part)) for part in (record.get("sort_key") or ()))
        else:
            sort_path = ((1, len(context["records"])),)
        return (sort_path, self._department_key(name))

    @staticmethod
    def _role_from_position(position: str) -> str:
        text = str(position or "").strip().lower().replace("ё", "е")
        if any(marker in text for marker in ("директор", "руковод", "супервиз", "лидер", "team lead", "тимлид")):
            return "lead"
        if "началь" in text:
            return "head"
        if "главн" in text and "эксперт" in text:
            return "chief_expert"
        if "главн" in text:
            return "chief_specialist"
        if "старш" in text:
            return "senior"
        return "specialist"

    @classmethod
    def _contact_role_rank(cls, position: str) -> int:
        return cls._role_rank(cls._role_from_position(position))

    @staticmethod
    def _role_rank(role: str) -> int:
        if role == "lead":
            return 0
        if role == "head":
            return 1
        if role == "chief_expert":
            return 2
        if role == "chief_specialist":
            return 3
        if role == "senior":
            return 4
        return 5

    @staticmethod
    def _group_role(value: str) -> str:
        text = str(value or "").strip().lower()
        if text in {"lead", "leader", "manager", "руководитель"}:
            return "lead"
        if text in {"head", "chief_manager", "начальник"}:
            return "head"
        if text in {"chief_expert", "главный эксперт"}:
            return "chief_expert"
        if text in {"chief", "main", "главный", "главный специалист"}:
            return "chief_specialist"
        if text in {"senior", "старший", "старший специалист"}:
            return "senior"
        return "specialist"

    @classmethod
    def _group_role_rank(cls, role: str, position: str) -> int:
        return cls._contact_role_rank(position)

    @classmethod
    def _group_role_label(cls, role: str, position: str = "") -> str:
        effective = cls._role_from_position(position)
        return {
            "lead": "Руководитель",
            "head": "Начальник",
            "chief_expert": "Главный эксперт",
            "chief_specialist": "Главный специалист",
            "senior": "Старший специалист",
        }.get(effective, "")

    def _contact_sort_key(self, contact: Dict, context: Optional[Dict] = None):
        context = context or self._department_context()
        department = contact.get("department") or ""
        position = contact.get("position") or ""
        return (
            *self._department_sort_key(department, context),
            self._int_value(contact.get("sort_order"), 0),
            self._group_role_rank(contact.get("group_role") or "", position),
            str(contact.get("created_at") or ""),
            self._int_value(contact.get("id"), 0),
            str(contact.get("full_name") or "").lower(),
        )

    def _decorate_contact(self, contact: Dict, context: Optional[Dict] = None) -> Dict:
        context = context or self._department_context()
        raw_department = self._clean(contact.get("department"), 220)
        department = self._canonical_department(raw_department, context)
        record = self._department_record(department, context)
        group_role = self._group_role(contact.get("group_role") or "")
        role_rank = self._group_role_rank(group_role, contact.get("position") or "")
        contact["raw_department"] = raw_department
        contact["department"] = department
        contact["group_role"] = group_role
        contact["department_group"] = department or "Без отдела"
        contact["hierarchy_path"] = list(record["path"]) if record else ([department] if department else ["Без отдела"])
        contact["hierarchy_kinds"] = list(record["kinds"]) if record else ["department"]
        contact["contact_role_rank"] = role_rank
        contact["group_role_label"] = self._group_role_label(group_role, contact.get("position") or "")
        contact["contact_role_class"] = {
            0: "lead",
            1: "head",
            2: "chief",
            3: "chief",
            4: "senior",
        }.get(role_rank, "")
        contact["inactive_reason_label"] = self.inactive_reason_label(contact.get("inactive_reason") or "")
        contact["is_group_lead"] = role_rank in (0, 1)
        contact["is_senior_contact"] = role_rank in (2, 3, 4)
        return contact

    @staticmethod
    def _active_value(value) -> int:
        text = str(value or "").strip().lower()
        if text in {"0", "no", "false", "нет", "неактивен", "архив", "inactive"}:
            return 0
        return 1

    @staticmethod
    def _inactive_reason(value: str) -> str:
        text = str(value or "").strip().lower()
        if text in {"dismissed", "уволен", "уволена", "увольнение"}:
            return "dismissed"
        if text in {"transferred", "переведен", "переведена", "перевод"}:
            return "transferred"
        return "hidden"

    @staticmethod
    def inactive_reason_label(reason: str) -> str:
        return {
            "dismissed": "Уволен(а)",
            "transferred": "Переведён(а)",
            "hidden": "Скрыт(а)",
        }.get(str(reason or "").strip(), "Скрыт(а)")

    def get_departments(self, include_inactive: bool = True) -> List[Dict]:
        context = self._department_context()
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
            query += " GROUP BY lower(TRIM(department))"
            rows_by_key = {}
            for row in conn.execute(query, params).fetchall():
                item = dict(row)
                name = self._canonical_department(item.get("name") or "", context)
                if not name:
                    continue
                key = self._department_key(name)
                current = rows_by_key.setdefault(key, {
                    "name": name,
                    "contacts_count": 0,
                    "active_contacts_count": 0,
                })
                current["contacts_count"] = int(current.get("contacts_count") or 0) + int(item.get("contacts_count") or 0)
                current["active_contacts_count"] = (
                    int(current.get("active_contacts_count") or 0) + int(item.get("active_contacts_count") or 0)
                )
            for record in context["records"]:
                key = self._department_key(record["name"])
                rows_by_key.setdefault(key, {
                    "name": record["name"],
                    "contacts_count": 0,
                    "active_contacts_count": 0,
                })
            rows = list(rows_by_key.values())
            for item in rows:
                record = self._department_record(item.get("name") or "", context)
                item["kind"] = record["kind"] if record else "department"
                item["level"] = record["level"] if record else 0
                item["path"] = list(record["path"]) if record else [item.get("name") or ""]
                item["is_custom"] = bool(record.get("is_custom")) if record else False
                item["id"] = record.get("id") if record else None
            return sorted(rows, key=lambda item: self._department_sort_key(item.get("name") or "", context))

    def get_position_options(self) -> List[str]:
        options_by_key = {}
        for name in CONTACT_POSITION_OPTIONS:
            cleaned = self._clean(name, 220)
            if cleaned:
                options_by_key.setdefault(cleaned.casefold(), cleaned)
        return list(options_by_key.values())

    def build_department_hierarchy(self, departments: List[Dict]) -> List[Dict]:
        context = self._department_context()
        by_name = {self._canonical_department(item.get("name") or "", context): item for item in departments}
        roots = []
        nodes_by_path = {}

        for record in context["records"]:
            path = record["path"]
            key = path
            item = by_name.get(record["name"], {})
            node = {
                "name": record["name"],
                "kind": record["kind"],
                "level": record["level"],
                "key": _contact_department_anchor_key(" / ".join(path)),
                "path": list(path),
                "contacts_count": int(item.get("contacts_count") or 0),
                "active_contacts_count": int(item.get("active_contacts_count") or 0),
                "is_custom": bool(record.get("is_custom")),
                "id": record.get("id"),
                "children": [],
            }
            nodes_by_path[key] = node
            if len(path) == 1:
                roots.append(node)
            else:
                parent = nodes_by_path.get(path[:-1])
                if parent:
                    parent["children"].append(node)

        known = {record["name"] for record in context["records"]}
        for name, item in by_name.items():
            if not name or name in known:
                continue
            roots.append({
                "name": name,
                "kind": "department",
                "level": 0,
                "key": _contact_department_anchor_key(name),
                "path": [name],
                "contacts_count": int(item.get("contacts_count") or 0),
                "active_contacts_count": int(item.get("active_contacts_count") or 0),
                "is_custom": False,
                "id": None,
                "children": [],
            })
        return roots

    def build_contact_hierarchy(self, contacts: List[Dict]) -> List[Dict]:
        context = self._department_context()
        roots = []
        nodes_by_path = {}

        def ensure_path(path, kinds):
            for index, name in enumerate(path):
                current_path = tuple(path[:index + 1])
                if current_path in nodes_by_path:
                    continue
                kind = kinds[index] if index < len(kinds) else "department"
                node = {
                    "name": name,
                    "kind": kind,
                    "level": index,
                    "key": _contact_department_anchor_key(" / ".join(current_path)),
                    "children": [],
                    "contacts": [],
                }
                nodes_by_path[current_path] = node
                if index == 0:
                    roots.append(node)
                else:
                    parent = nodes_by_path.get(tuple(path[:index]))
                    if parent:
                        parent["children"].append(node)
            return nodes_by_path.get(tuple(path))

        def prune_empty(nodes):
            result = []
            for node in nodes:
                node["children"] = prune_empty(node["children"])
                if node["contacts"] or node["children"]:
                    result.append(node)
            return result

        for record in context["records"]:
            ensure_path(record["path"], record["kinds"])

        for contact in contacts:
            path = contact.get("hierarchy_path") or [contact.get("department_group") or "Без отдела"]
            kinds = contact.get("hierarchy_kinds") or ["department"] * len(path)

            node = ensure_path(path, kinds)
            if node:
                node["contacts"].append(contact)

        return prune_empty(roots)

    # ─── Подразделения ─────────────────────────────────────────────

    def get_custom_departments(self) -> List[Dict]:
        context = self._department_context()
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("""
                SELECT id, name, parent_name, kind, aliases, sort_order, created_by, updated_by, created_at, updated_at
                FROM cc_departments
                WHERE is_active = 1
                ORDER BY sort_order, name
            """).fetchall()]

            result = []
            for row in rows:
                name = self._clean(row.get("name"), 220)
                parent_name = self._canonical_department(row.get("parent_name") or "", context)
                record = self._department_record(name, context)
                contact_count_row = conn.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM cc_contacts
                    WHERE lower(TRIM(department)) = lower(TRIM(?))
                """, (name,)).fetchone()
                child_count_row = conn.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM cc_departments
                    WHERE is_active = 1 AND lower(TRIM(parent_name)) = lower(TRIM(?))
                """, (name,)).fetchone()
                result.append({
                    **row,
                    "name": name,
                    "parent_name": parent_name,
                    "kind": self._department_kind(row.get("kind")),
                    "path": list(record["path"]) if record else [name],
                    "level": record["level"] if record else 0,
                    "contacts_count": int(contact_count_row["cnt"] if contact_count_row else 0),
                    "children_count": int(child_count_row["cnt"] if child_count_row else 0),
                })

        return sorted(result, key=lambda item: self._department_sort_key(item.get("name") or "", context))

    def get_department_editor_items(self) -> List[Dict]:
        context = self._department_context()
        with self._connect() as conn:
            result = []
            for record in context["records"]:
                values = self._department_filter_values(record["name"], context)
                placeholders = ", ".join(["lower(TRIM(?))"] * len(values))
                contact_count_row = conn.execute(f"""
                    SELECT COUNT(*) AS cnt
                    FROM cc_contacts
                    WHERE lower(TRIM(department)) IN ({placeholders})
                """, values).fetchone()
                child_count = sum(
                    1 for candidate in context["records"]
                    if candidate.get("path", ())[:len(record.get("path", ()))] == record.get("path", ())
                    and candidate.get("name") != record.get("name")
                    and len(candidate.get("path", ())) == len(record.get("path", ())) + 1
                )
                result.append({
                    "source": record.get("source") or ("custom" if record.get("is_custom") else "base"),
                    "source_id": record.get("source_id") or str(record.get("id") or ""),
                    "id": record.get("id"),
                    "name": record.get("name") or "",
                    "original_name": record.get("original_name") or "",
                    "parent_name": record.get("path", ("", ""))[-2] if len(record.get("path", ())) > 1 else "",
                    "kind": self._department_kind(record.get("kind")),
                    "path": list(record.get("path") or (record.get("name") or "",)),
                    "level": int(record.get("level") or 0),
                    "sort_order": int(record.get("sort_order") or 0),
                    "aliases": "\n".join(record.get("aliases") or ()),
                    "contacts_count": int(contact_count_row["cnt"] if contact_count_row else 0),
                    "children_count": child_count,
                    "is_custom": bool(record.get("is_custom")),
                })
        return sorted(result, key=lambda item: self._department_sort_key(item.get("name") or "", context))

    def create_department(self, data: Dict, actor: str = "") -> Dict:
        context = self._department_context()
        name = self._clean(data.get("name"), 220)
        parent_name = self._canonical_department(data.get("parent_name") or "", context)
        kind = self._department_kind(data.get("kind"))
        aliases = "\n".join(self._department_aliases(data.get("aliases")))
        try:
            sort_order = int(data.get("sort_order") or 0)
        except (TypeError, ValueError):
            sort_order = 0

        if not name:
            return {"success": False, "error": "Название группы или отдела обязательно"}
        if self._department_key(name) in context["canonical_by_key"]:
            return {"success": False, "error": "Такое подразделение уже есть"}
        if parent_name and self._department_key(parent_name) not in context["by_key"]:
            return {"success": False, "error": "Родительский отдел не найден"}
        if parent_name and self._department_key(parent_name) == self._department_key(name):
            return {"success": False, "error": "Подразделение не может быть родителем самого себя"}

        with self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO cc_departments (
                    name, parent_name, kind, aliases, sort_order, is_active,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                name, parent_name, kind, aliases, sort_order,
                actor, actor, self._now(), self._now(),
            ))
            conn.commit()
            return {"success": True, "id": cur.lastrowid}

    def _department_record_by_source(self, source: str, source_id: str, context: Dict) -> Optional[Dict]:
        source = str(source or "").strip()
        source_id = str(source_id or "").strip()
        for record in context["records"]:
            if record.get("source") == source and str(record.get("source_id") or "") == source_id:
                return record
        return None

    def update_department(self, source: str, source_id: str, data: Dict, actor: str = "") -> Dict:
        context = self._department_context()
        source = str(source or "").strip()
        source_id = str(source_id or "").strip()
        current = self._department_record_by_source(source, source_id, context)
        if not current:
            return {"success": False, "error": "Подразделение не найдено"}

        old_name = self._clean(current.get("name"), 220)
        name = self._clean(data.get("name"), 220)
        parent_name = self._canonical_department(data.get("parent_name") or "", context)
        kind = self._department_kind(data.get("kind"))
        aliases = "\n".join(self._department_aliases(data.get("aliases")))
        try:
            sort_order = int(data.get("sort_order") or 0)
        except (TypeError, ValueError):
            sort_order = 0

        if not name:
            return {"success": False, "error": "Название группы или отдела обязательно"}

        name_key = self._department_key(name)
        existing_name = context["canonical_by_key"].get(name_key)
        existing_record = self._department_record(existing_name, context) if existing_name else None
        if existing_record and existing_record.get("source_id") != current.get("source_id"):
            return {"success": False, "error": "Такое подразделение уже есть"}

        if parent_name:
            parent_record = self._department_record(parent_name, context)
            if not parent_record:
                return {"success": False, "error": "Родительский отдел не найден"}
            if self._department_key(parent_name) in {self._department_key(old_name), name_key}:
                return {"success": False, "error": "Подразделение не может быть родителем самого себя"}
            if tuple(current.get("path") or ()) and tuple(parent_record.get("path") or ())[:len(current["path"])] == tuple(current["path"]):
                return {"success": False, "error": "Нельзя перенести подразделение внутрь его дочерней группы"}

        now = self._now()
        with self._connect() as conn:
            if source == "base":
                original_name = current.get("original_name") or old_name
                conn.execute("""
                    INSERT INTO cc_department_overrides (
                        original_name, name, parent_name, kind, aliases, sort_order, is_active,
                        created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(original_name) DO UPDATE SET
                        name = excluded.name,
                        parent_name = excluded.parent_name,
                        kind = excluded.kind,
                        aliases = excluded.aliases,
                        sort_order = excluded.sort_order,
                        is_active = 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                """, (
                    original_name, name, parent_name, kind, aliases, sort_order,
                    actor, actor, now, now,
                ))
            elif source == "custom":
                conn.execute("""
                    UPDATE cc_departments
                    SET name = ?, parent_name = ?, kind = ?, aliases = ?, sort_order = ?,
                        updated_by = ?, updated_at = ?
                    WHERE id = ? AND is_active = 1
                """, (name, parent_name, kind, aliases, sort_order, actor, now, int(source_id or 0)))
            else:
                return {"success": False, "error": "Неизвестный тип подразделения"}

            if self._department_key(old_name) != self._department_key(name):
                conn.execute("""
                    UPDATE cc_contacts
                    SET department = ?, updated_by = ?, updated_at = ?
                    WHERE lower(TRIM(department)) = lower(TRIM(?))
                """, (name, actor, now, old_name))
                conn.execute("""
                    UPDATE cc_departments
                    SET parent_name = ?, updated_by = ?, updated_at = ?
                    WHERE lower(TRIM(parent_name)) = lower(TRIM(?))
                """, (name, actor, now, old_name))
                conn.execute("""
                    UPDATE cc_department_overrides
                    SET parent_name = ?, updated_by = ?, updated_at = ?
                    WHERE lower(TRIM(parent_name)) = lower(TRIM(?))
                """, (name, actor, now, old_name))

            conn.commit()
            return {"success": True}

    def save_department_order(self, names: List[str], actor: str = "") -> Dict:
        context = self._department_context()
        ordered_records = []
        seen = set()
        for name in names:
            canonical = self._canonical_department(name, context)
            record = self._department_record(canonical, context)
            key = self._department_key(canonical)
            if not record or not key or key in seen:
                continue
            seen.add(key)
            ordered_records.append(record)

        if not ordered_records:
            return {"success": False, "error": "Нет отделов для сохранения порядка"}

        by_parent: Dict[tuple, List[Dict]] = {}
        for record in ordered_records:
            parent_path = tuple(record.get("path")[:-1])
            by_parent.setdefault(parent_path, []).append(record)

        now = self._now()
        updated = 0
        with self._connect() as conn:
            for siblings in by_parent.values():
                for index, record in enumerate(siblings, start=1):
                    sort_order = index * 10
                    if record.get("source") == "custom":
                        conn.execute("""
                            UPDATE cc_departments
                            SET sort_order = ?, updated_by = ?, updated_at = ?
                            WHERE id = ? AND is_active = 1
                        """, (sort_order, actor, now, int(record.get("id") or 0)))
                        updated += 1
                        continue

                    original_name = record.get("original_name") or record.get("name") or ""
                    parent_path = tuple(record.get("path")[:-1])
                    parent_name = parent_path[-1] if parent_path else ""
                    aliases = "\n".join(record.get("aliases") or ())
                    conn.execute("""
                        INSERT INTO cc_department_overrides (
                            original_name, name, parent_name, kind, aliases, sort_order, is_active,
                            created_by, updated_by, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        ON CONFLICT(original_name) DO UPDATE SET
                            name = excluded.name,
                            parent_name = excluded.parent_name,
                            kind = excluded.kind,
                            aliases = excluded.aliases,
                            sort_order = excluded.sort_order,
                            is_active = 1,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                    """, (
                        original_name,
                        record.get("name") or original_name,
                        parent_name,
                        self._department_kind(record.get("kind")),
                        aliases,
                        sort_order,
                        actor,
                        actor,
                        now,
                        now,
                    ))
                    updated += 1
            conn.commit()

        return {"success": True, "updated": updated}

    def delete_department(self, department_id: int) -> Dict:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT id, name
                FROM cc_departments
                WHERE id = ? AND is_active = 1
            """, (department_id,)).fetchone()
            if not row:
                return {"success": False, "error": "Подразделение не найдено"}

            name = self._clean(row["name"], 220)
            contact_count = conn.execute("""
                SELECT COUNT(*) AS cnt
                FROM cc_contacts
                WHERE lower(TRIM(department)) = lower(TRIM(?))
            """, (name,)).fetchone()
            if int(contact_count["cnt"] if contact_count else 0) > 0:
                return {"success": False, "error": "Сначала перенесите или удалите сотрудников из этой группы"}

            child_count = conn.execute("""
                SELECT COUNT(*) AS cnt
                FROM cc_departments
                WHERE is_active = 1 AND lower(TRIM(parent_name)) = lower(TRIM(?))
            """, (name,)).fetchone()
            if int(child_count["cnt"] if child_count else 0) > 0:
                return {"success": False, "error": "Сначала удалите дочерние группы или отделы"}

            conn.execute("DELETE FROM cc_departments WHERE id = ?", (department_id,))
            conn.commit()
            return {"success": True}

    def save_contact_order(self, contact_ids: List[int], actor: str = "") -> Dict:
        ids = []
        seen = set()
        for value in contact_ids:
            try:
                contact_id = int(value)
            except (TypeError, ValueError):
                continue
            if contact_id <= 0 or contact_id in seen:
                continue
            seen.add(contact_id)
            ids.append(contact_id)

        if not ids:
            return {"success": False, "error": "Нет сотрудников для сохранения порядка"}

        placeholders = ",".join("?" for _ in ids)
        now = self._now()
        context = self._department_context()
        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT id, department
                FROM cc_contacts
                WHERE id IN ({placeholders})
            """, ids).fetchall()
            departments_by_id = {
                int(row["id"]): self._department_key(
                    self._canonical_department(row["department"] or "", context)
                )
                for row in rows
            }
            by_department: Dict[str, List[int]] = {}
            for contact_id in ids:
                department_key = departments_by_id.get(contact_id)
                if department_key is None:
                    continue
                by_department.setdefault(department_key, []).append(contact_id)

            affected_departments = set(by_department.keys())
            all_rows = [
                self._decorate_contact(dict(row), context)
                for row in conn.execute("""
                    SELECT id, department, position, group_role, sort_order, created_at, full_name
                    FROM cc_contacts
                """).fetchall()
            ]
            all_rows.sort(key=lambda item: self._contact_sort_key(item, context))
            current_by_department: Dict[str, List[int]] = {}
            for row in all_rows:
                department_key = self._department_key(row.get("department") or "")
                if department_key in affected_departments:
                    current_by_department.setdefault(department_key, []).append(int(row.get("id") or 0))

            updated = 0
            for department_key, ordered_ids in by_department.items():
                current_ids = current_by_department.get(department_key) or []
                ordered_ids = [contact_id for contact_id in ordered_ids if contact_id in current_ids]
                if not ordered_ids:
                    continue

                ordered_set = set(ordered_ids)
                slots = [index for index, contact_id in enumerate(current_ids) if contact_id in ordered_set]
                department_ids = list(current_ids)
                for slot, contact_id in zip(slots, ordered_ids):
                    department_ids[slot] = contact_id

                for index, contact_id in enumerate(department_ids, start=1):
                    conn.execute("""
                        UPDATE cc_contacts
                        SET sort_order = ?, updated_by = ?, updated_at = ?
                        WHERE id = ?
                    """, (index * 10, actor, now, contact_id))
                    updated += 1
            conn.commit()

        return {"success": True, "updated": updated}

    # ─── Контакты ─────────────────────────────────────────────────

    def get_contacts(self, q: str = "", department: str = "", direction_id: int | None = None,
                     status: str = "active",
                     limit: int = 1000, offset: int = 0) -> List[Dict]:
        context = self._department_context()
        with self._connect() as conn:
            query, params = self._contacts_query(q, department, direction_id, status, count=False)
            rows = [self._decorate_contact(dict(r), context) for r in conn.execute(query, params).fetchall()]
            rows.sort(key=lambda item: self._contact_sort_key(item, context))
            safe_limit = max(1, min(int(limit or 1000), 5000))
            safe_offset = max(0, int(offset or 0))
            return rows[safe_offset:safe_offset + safe_limit]

    def get_inactive_contacts(self, q: str = "", limit: int = 5000, offset: int = 0) -> List[Dict]:
        context = self._department_context()
        with self._connect() as conn:
            query, params = self._contacts_query(q, "", None, "inactive", count=False)
            rows = [self._decorate_contact(dict(r), context) for r in conn.execute(query, params).fetchall()]
            for row in rows:
                row["inactive_reason_label"] = self.inactive_reason_label(row.get("inactive_reason") or "")
            rows.sort(key=lambda item: (
                item.get("inactive_date") or item.get("inactive_at") or "",
                item.get("updated_at") or "",
                item.get("full_name") or "",
            ), reverse=True)
            safe_limit = max(1, min(int(limit or 5000), 5000))
            safe_offset = max(0, int(offset or 0))
            return rows[safe_offset:safe_offset + safe_limit]

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
            department_values = self._department_filter_values(department)
            placeholders = ", ".join(["lower(?)"] * len(department_values))
            query += f" AND lower(TRIM(c.department)) IN ({placeholders})"
            params.extend(department_values)
        q = self._clean(q, 200)
        if q:
            text_expr = """
                unicode_casefold(
                    COALESCE(c.full_name, '') || ' ' ||
                    COALESCE(c.position, '') || ' ' ||
                    COALESCE(c.department, '') || ' ' ||
                    COALESCE(c.phone, '') || ' ' ||
                    COALESCE(c.extension, '') || ' ' ||
                    COALESCE(c.mobile, '') || ' ' ||
                    COALESCE(c.telegram, '') || ' ' ||
                    COALESCE(c.nickname, '') || ' ' ||
                    COALESCE(c.workplace, '') || ' ' ||
                    COALESCE(c.responsibilities, '') || ' ' ||
                    COALESCE(c.tags, '') || ' ' ||
                    COALESCE(c.inactive_reason, '') || ' ' ||
                    COALESCE(c.inactive_date, '') || ' ' ||
                    COALESCE(c.inactive_comment, '')
                )
            """
            phone_digits_expr = """
                digits_only(
                    COALESCE(c.phone, '') || ' ' ||
                    COALESCE(c.extension, '') || ' ' ||
                    COALESCE(c.mobile, '')
                )
            """
            token_conditions = []
            for token in self._search_tokens(q):
                token_digits = self._digits_only(token)
                condition = f"(POSITION(? IN {text_expr}) > 0"
                params.append(token)
                if token_digits:
                    condition += f" OR POSITION(? IN {phone_digits_expr}) > 0"
                    params.append(token_digits)
                condition += ")"
                token_conditions.append(condition)
            if token_conditions:
                query += " AND " + " AND ".join(token_conditions)
        return query, params

    def get_contact(self, contact_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT c.*,
                       (SELECT COUNT(*) FROM cc_contact_likes l WHERE l.contact_id = c.id) AS likes_count
                FROM cc_contacts c
                WHERE c.id = ?
            """, (contact_id,)).fetchone()
            return self._decorate_contact(dict(row)) if row else None

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
        try:
            sort_order = int(data.get("sort_order") or 0)
        except (TypeError, ValueError):
            sort_order = 0
        phone = self._clean(data.get("phone") or data.get("mobile"), 100)
        payload = {
            "direction_id": None,
            "full_name": self._clean(data.get("full_name"), 220),
            "position": self._clean(data.get("position"), 220),
            "department": self._clean(data.get("department"), 220),
            "phone": phone,
            "extension": self._clean(data.get("extension"), 50),
            "mobile": "",
            "email": self._clean(data.get("email"), 180),
            "telegram": self._clean(data.get("telegram"), 100),
            "nickname": self._clean(data.get("nickname"), 100),
            "group_role": self._group_role(data.get("group_role")),
            "workplace": self._clean(data.get("workplace"), 140),
            "schedule": self._clean(data.get("schedule"), 300),
            "languages": self._clean(data.get("languages"), 100),
            "responsibilities": self._clean(data.get("responsibilities"), 1500),
            "notes": self._clean(data.get("notes"), 1500),
            "tags": self._clean(data.get("tags"), 500),
            "is_active": self._active_value(data.get("is_active", "1")),
            "sort_order": sort_order,
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
                    email, telegram, nickname, group_role, workplace, schedule, languages, responsibilities, notes, tags,
                    is_active, sort_order, created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                payload["direction_id"], payload["full_name"], payload["position"], payload["department"],
                payload["phone"], payload["extension"], payload["mobile"], payload["email"],
                payload["telegram"], payload["nickname"], payload["group_role"], payload["workplace"], payload["schedule"], payload["languages"],
                payload["responsibilities"], payload["notes"], payload["tags"], payload["is_active"], payload["sort_order"],
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
                    extension = ?, mobile = ?, email = ?, telegram = ?, nickname = ?, group_role = ?, workplace = ?,
                    schedule = ?, languages = ?, responsibilities = ?, notes = ?, tags = ?,
                    is_active = ?, sort_order = ?,
                    inactive_reason = CASE WHEN ? = 1 THEN '' ELSE inactive_reason END,
                    inactive_date = CASE WHEN ? = 1 THEN '' ELSE inactive_date END,
                    inactive_comment = CASE WHEN ? = 1 THEN '' ELSE inactive_comment END,
                    inactive_by = CASE WHEN ? = 1 THEN '' ELSE inactive_by END,
                    inactive_at = CASE WHEN ? = 1 THEN '' ELSE inactive_at END,
                    updated_by = ?, updated_at = ?
                WHERE id = ?
            """, (
                payload["direction_id"], payload["full_name"], payload["position"], payload["department"],
                payload["phone"], payload["extension"], payload["mobile"], payload["email"],
                payload["telegram"], payload["nickname"], payload["group_role"], payload["workplace"], payload["schedule"], payload["languages"],
                payload["responsibilities"], payload["notes"], payload["tags"], payload["is_active"], payload["sort_order"],
                payload["is_active"], payload["is_active"], payload["is_active"], payload["is_active"], payload["is_active"],
                actor, self._now(), contact_id,
            ))
            conn.commit()
            return {"success": True}

    def set_contact_active(self, contact_id: int, is_active: bool, actor: str = "", archive_data: Dict | None = None) -> Dict:
        archive_data = archive_data or {}
        with self._connect() as conn:
            exists = conn.execute("SELECT id FROM cc_contacts WHERE id = ?", (contact_id,)).fetchone()
            if not exists:
                return {"success": False, "error": "Контакт не найден"}
            now = self._now()
            if is_active:
                conn.execute("""
                    UPDATE cc_contacts
                    SET is_active = 1,
                        inactive_reason = '',
                        inactive_date = '',
                        inactive_comment = '',
                        inactive_by = '',
                        inactive_at = '',
                        updated_by = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (actor, now, contact_id))
            else:
                reason = self._inactive_reason(archive_data.get("inactive_reason"))
                inactive_date = self._clean(archive_data.get("inactive_date"), 40)
                inactive_comment = self._clean(archive_data.get("inactive_comment"), 1500)
                if not inactive_date:
                    inactive_date = datetime.now().strftime("%Y-%m-%d")
                conn.execute("""
                    UPDATE cc_contacts
                    SET is_active = 0,
                        inactive_reason = ?,
                        inactive_date = ?,
                        inactive_comment = ?,
                        inactive_by = ?,
                        inactive_at = ?,
                        updated_by = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (reason, inactive_date, inactive_comment, actor, now, actor, now, contact_id))
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

    def record_directory_visit(self, visitor_key: str, visitor_name: str = "", path: str = "") -> Dict:
        visitor_key = self._clean(visitor_key, 220).lower()
        if not visitor_key:
            return {"success": False, "error": "Не указан пользователь"}

        now = self._now()
        visit_date = datetime.now().strftime("%Y-%m-%d")
        visitor_name = self._clean(visitor_name, 220)
        path = self._clean(path, 500)

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO cc_section_visits (
                    section, visitor_key, visitor_name, visit_date,
                    first_seen_at, last_seen_at, visit_count, last_path
                )
                VALUES ('contacts', ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(section, visitor_key, visit_date) DO UPDATE SET
                    visitor_name = excluded.visitor_name,
                    last_seen_at = excluded.last_seen_at,
                    visit_count = cc_section_visits.visit_count + 1,
                    last_path = excluded.last_path
            """, (visitor_key, visitor_name, visit_date, now, now, path))
            conn.commit()
            return {"success": True}

    def get_directory_usage_stats(self) -> Dict[str, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            today_row = conn.execute("""
                SELECT
                    COUNT(DISTINCT visitor_key) AS people,
                    COALESCE(SUM(visit_count), 0) AS visits
                FROM cc_section_visits
                WHERE section IN ('contacts', 'directions') AND visit_date = ?
            """, (today,)).fetchone()
            total_row = conn.execute("""
                SELECT
                    COUNT(DISTINCT visitor_key) AS people,
                    COALESCE(SUM(visit_count), 0) AS visits
                FROM cc_section_visits
                WHERE section IN ('contacts', 'directions')
            """).fetchone()

        return {
            "today_people": int(today_row["people"] if today_row else 0),
            "total_people": int(total_row["people"] if total_row else 0),
            "today_visits": int(today_row["visits"] if today_row else 0),
            "total_visits": int(total_row["visits"] if total_row else 0),
        }

    def get_stats(self) -> Dict:
        with self._connect() as conn:
            contacts = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END) AS inactive
                FROM cc_contacts
            """).fetchone()
            directory_usage = self.get_directory_usage_stats()
            return {
                "contacts_total": int(contacts["total"] or 0),
                "contacts_active": int(contacts["active"] or 0),
                "contacts_inactive": int(contacts["inactive"] or 0),
                "departments_total": len(self.get_departments(include_inactive=True)),
                "directory_today_people": directory_usage["today_people"],
                "directory_total_people": directory_usage["total_people"],
                "directory_today_visits": directory_usage["today_visits"],
                "directory_total_visits": directory_usage["total_visits"],
            }
