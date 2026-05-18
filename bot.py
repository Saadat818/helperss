#!/usr/bin/env python3
"""
Telegram Bot для Helper - отдельный процесс
Обрабатывает callback кнопки и сообщения в группах
"""

import os
import json
import traceback
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Загружаем переменные окружения
load_dotenv()

import re
import sqlite3
from datetime import datetime, timedelta
from html import escape as html_escape

APP_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('TEST_BOT_TOKEN') if APP_TEST_MODE and os.getenv('TEST_BOT_TOKEN') else os.getenv('BOT_TOKEN')
TECH_SUPPORT_CHAT_ID = int(os.getenv('TECH_SUPPORT_CHAT_ID', '0'))
NEW_TICKETS_THREAD_ID = int(os.getenv('NEW_TICKETS_THREAD_ID', '0'))
IN_PROGRESS_THREAD_ID = int(os.getenv('IN_PROGRESS_THREAD_ID', '0'))
SOLVED_TICKETS_THREAD_ID = int(os.getenv('SOLVED_TICKETS_THREAD_ID', '0'))


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int_from_value(value, default: int = 0) -> int:
    try:
        return int(str(value if value is not None else default).strip() or default)
    except (TypeError, ValueError):
        return default


CURRENT_DUTY_TELEGRAM_ID = _env_int('CURRENT_DUTY_TELEGRAM_ID', 0)
CURRENT_DUTY_USERNAME = os.getenv('CURRENT_DUTY_USERNAME', '').strip().lstrip('@')
CURRENT_DUTY_NAME = os.getenv('CURRENT_DUTY_NAME', '').strip()
OVERLOAD_TICKET_LIMIT = max(1, _env_int('OVERLOAD_TICKET_LIMIT', 5))
OVERLOAD_ALERT_THREAD_ID = _env_int('OVERLOAD_ALERT_THREAD_ID', 0)
SUPPORT_STAFF_IDS_STR = os.getenv('SUPPORT_STAFF_IDS', '')
SUPPORT_STAFF_IDS = [int(x.strip()) for x in SUPPORT_STAFF_IDS_STR.split(',') if x.strip().isdigit()]

# --- Аналитика: запись событий в БД ---
ANALYTICS_BACKEND = os.getenv('ANALYTICS_BACKEND', 'sqlite')
AUDIT_LOG_DB_PATH = os.getenv('AUDIT_LOG_DB', 'audit.log')

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

ANALYTICS_USE_POSTGRES = ANALYTICS_BACKEND == 'postgres' and psycopg2 is not None


def _pg_connect():
    return psycopg2.connect(
        host=os.getenv('POSTGRES_HOST', 'localhost'),
        port=int(os.getenv('POSTGRES_PORT', '5432')),
        database=os.getenv('POSTGRES_DB', 'helper_analytics'),
        user=os.getenv('POSTGRES_USER', 'ruslan'),
        password=os.getenv('POSTGRES_PASSWORD', ''),
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10
    )


def _sanitize_details(details):
    sanitized = {}
    for key, value in (details or {}).items():
        value = str(value)
        sanitized[str(key)] = value[:500] + ("...[truncated]" if len(value) > 500 else "")
    return sanitized


def _ensure_ticket_events_table():
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ticket_events (
                            id BIGSERIAL PRIMARY KEY,
                            created_at TIMESTAMP NOT NULL,
                            event_type TEXT NOT NULL,
                            ticket_number INTEGER,
                            problem TEXT,
                            problem_id TEXT,
                            subproblem_id TEXT,
                            department TEXT,
                            user_name TEXT,
                            workplace TEXT,
                            channel TEXT,
                            topic_name TEXT,
                            is_cisco INTEGER DEFAULT 0,
                            actor_name TEXT,
                            actor_username TEXT,
                            actor_role TEXT,
                            details_json JSONB
                        )
                    """)
                    cur.execute("ALTER TABLE ticket_events ADD COLUMN IF NOT EXISTS details_json JSONB")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS app_settings (
                            key TEXT PRIMARY KEY,
                            value TEXT,
                            updated_at TIMESTAMP,
                            updated_by TEXT
                        )
                    """)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticket_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        ticket_number INTEGER,
                        problem TEXT,
                        problem_id TEXT,
                        subproblem_id TEXT,
                        department TEXT,
                        user_name TEXT,
                        workplace TEXT,
                        channel TEXT,
                        topic_name TEXT,
                        is_cisco INTEGER DEFAULT 0,
                        actor_name TEXT,
                        actor_username TEXT,
                        actor_role TEXT,
                        details_json TEXT
                    )
                """)
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(ticket_events)")
                cols = {row[1] for row in cur.fetchall()}
                if 'details_json' not in cols:
                    cur.execute("ALTER TABLE ticket_events ADD COLUMN details_json TEXT")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT,
                        updated_by TEXT
                    )
                """)
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка миграции ticket_events: {e}")


def extract_ticket_number(text: str):
    """Извлекает номер заявки из текста вида 'НОВАЯ ЗАЯВКА №123'."""
    if not text:
        return None
    match = re.search(r'№\s*(\d+)', text)
    return int(match.group(1)) if match else None


def parse_ticket_fields(text: str) -> dict:
    """Извлекает поля из текста заявки."""
    fields = {}
    for line in text.split('\n'):
        if ':' in line:
            key, _, val = line.partition(':')
            key = key.strip().lower()
            val = val.strip()
            if 'отдел' in key:
                fields['department'] = val
            elif 'имя' in key:
                fields['name'] = val
            elif 'рабочее место' in key:
                fields['workplace'] = val
            elif 'проблема' in key:
                fields['problem'] = val
    return fields


def log_ticket_event(event_type, ticket_number=None, problem='',
                     department='', user_name='', workplace='',
                     actor_name='', actor_username='', details=None):
    """Записывает событие заявки в БД."""
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        details_json = json.dumps(_sanitize_details(details), ensure_ascii=False)
        payload = (now, event_type, ticket_number, problem[:500],
                   department[:200], user_name[:200], workplace[:100],
                   '', '', 0, actor_name[:200], actor_username[:200], 'staff', details_json)
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ticket_events (
                            created_at, event_type, ticket_number, problem,
                            department, user_name, workplace,
                            channel, topic_name, is_cisco,
                            actor_name, actor_username, actor_role, details_json
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO ticket_events (
                        created_at, event_type, ticket_number, problem,
                        department, user_name, workplace,
                        channel, topic_name, is_cisco,
                        actor_name, actor_username, actor_role, details_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, payload)
                conn.commit()
        print(f"[analytics] Записано: {event_type} ticket_number={ticket_number}")
    except Exception as e:
        print(f"[analytics] Ошибка логирования: {e}")


def _parse_rejection_reason(text: str) -> str:
    match = re.search(r'отклон[её]н\w*\s*[:\-—]\s*(.+)$', text or '', flags=re.IGNORECASE | re.DOTALL)
    return (match.group(1).strip() if match else '')[:500]


def _default_duty_settings():
    return {
        'current_duty_name': CURRENT_DUTY_NAME,
        'current_duty_username': CURRENT_DUTY_USERNAME,
        'current_duty_telegram_id': str(CURRENT_DUTY_TELEGRAM_ID or ''),
        'overload_ticket_limit': str(OVERLOAD_TICKET_LIMIT),
        'overload_alert_thread_id': str(OVERLOAD_ALERT_THREAD_ID or ''),
    }


def _get_app_settings(keys=None):
    keys = keys or list(_default_duty_settings().keys())
    if not keys:
        return {}
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)", [keys])
                    return {row['key']: row.get('value') or '' for row in cur.fetchall()}
        placeholders = ",".join("?" * len(keys))
        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})", keys)
            return {row['key']: row['value'] or '' for row in cur.fetchall()}
    except Exception as e:
        print(f"[app_settings] Ошибка чтения настроек: {e}")
        return {}


def _get_duty_settings():
    settings = _default_duty_settings()
    settings.update(_get_app_settings(list(settings.keys())))
    settings['current_duty_username'] = settings.get('current_duty_username', '').strip().lstrip('@')
    settings['current_duty_name'] = settings.get('current_duty_name', '').strip()
    settings['current_duty_telegram_id'] = str(_env_int_from_value(settings.get('current_duty_telegram_id'), 0) or '')
    settings['overload_ticket_limit'] = str(max(1, _env_int_from_value(settings.get('overload_ticket_limit'), OVERLOAD_TICKET_LIMIT)))
    settings['overload_alert_thread_id'] = str(_env_int_from_value(settings.get('overload_alert_thread_id'), 0) or '')
    return settings


TICKET_STATUS_LABELS = {
    'in_work': 'В работе',
    'ready_for_feedback': 'Готово',
    'closed': 'Решено',
    'rejected': 'Отклонён',
    'mass_incident': 'Массовый инцидент',
    'closed_auto': 'Авто-закрыта',
    'unknown': 'Неизвестно',
}
HARD_FINAL_TICKET_STATUSES = {'rejected', 'mass_incident', 'closed_auto'}
LOCKED_TICKET_ACTION_STATUSES = {'ready_for_feedback', 'closed', 'rejected', 'mass_incident', 'closed_auto'}


def _get_ticket_status(ticket_number):
    if ticket_number is None:
        return 'unknown'
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_type
                        FROM ticket_events
                        WHERE ticket_number = %s
                        ORDER BY created_at ASC, id ASC
                    """, [ticket_number])
                    rows = [dict(row) for row in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT event_type
                    FROM ticket_events
                    WHERE ticket_number = ?
                    ORDER BY created_at ASC, id ASC
                """, [ticket_number])
                rows = [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[ticket_status] Ошибка проверки заявки {ticket_number}: {e}")
        return 'unknown'

    status = 'unknown'
    for row in rows:
        event_type = row.get('event_type') or ''
        if event_type == 'ticket_created':
            status = 'in_work'
        elif event_type in ('ticket_assigned_to_duty', 'ticket_assigned_to_staff'):
            if status not in LOCKED_TICKET_ACTION_STATUSES:
                status = 'in_work'
        elif event_type == 'ticket_reopened_by_user':
            if status not in HARD_FINAL_TICKET_STATUSES:
                status = 'in_work'
        elif event_type == 'ticket_ready_for_feedback':
            if status not in HARD_FINAL_TICKET_STATUSES:
                status = 'ready_for_feedback'
        elif event_type in ('ticket_user_confirmed_resolved', 'ticket_resolved_by_staff'):
            if status not in HARD_FINAL_TICKET_STATUSES:
                status = 'closed'
        elif event_type in ('ticket_rejected', 'ticket_not_relevant'):
            status = 'rejected'
        elif event_type == 'ticket_mass_incident':
            status = 'mass_incident'
        elif event_type == 'ticket_auto_closed_reset_call':
            status = 'closed_auto'
    return status


def _ticket_action_lock_reason(ticket_number):
    if ticket_number is None:
        return ''
    status = _get_ticket_status(ticket_number)
    if status in LOCKED_TICKET_ACTION_STATUSES:
        return f"Заявка №{ticket_number} уже обработана: {TICKET_STATUS_LABELS.get(status, status)}"
    return ''


def _remove_ticket_buttons(chat_id, message_id):
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception as e:
        print(f"[ticket_buttons] Не удалось убрать кнопки message_id={message_id}: {e}")


def _current_duty_actor():
    settings = _get_duty_settings()
    telegram_id = _env_int_from_value(settings.get('current_duty_telegram_id'), 0)
    username = settings.get('current_duty_username') or (str(telegram_id) if telegram_id else 'duty')
    name = settings.get('current_duty_name') or settings.get('current_duty_username') or 'Дежурный'
    return {'name': name, 'username': username}


def _format_duty_mention(actor):
    name = html_escape(actor.get('name') or actor.get('username') or 'дежурный')
    telegram_id = _env_int_from_value(_get_duty_settings().get('current_duty_telegram_id'), 0)
    if telegram_id:
        return f'<a href="tg://user?id={telegram_id}">{name}</a>'
    username = (actor.get('username') or '').strip().lstrip('@')
    return f'@{html_escape(username)}' if username and username != 'duty' else name


def _load_active_ticket_count(actor_username):
    if not actor_username:
        return 0, []
    if ANALYTICS_USE_POSTGRES:
        query = """
            SELECT id, created_at::text AS created_at, event_type, ticket_number, actor_username
            FROM ticket_events
            WHERE ticket_number IS NOT NULL
            ORDER BY ticket_number ASC, created_at ASC, id ASC
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = [dict(row) for row in cur.fetchall()]
    else:
        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT id, created_at, event_type, ticket_number, actor_username
                FROM ticket_events
                WHERE ticket_number IS NOT NULL
                ORDER BY ticket_number ASC, created_at ASC, id ASC
            """)
            rows = [dict(row) for row in cur.fetchall()]

    states = {}
    for row in rows:
        tn = row.get('ticket_number')
        state = states.setdefault(tn, {'status': 'unknown', 'assigned_username': ''})
        event_type = row.get('event_type') or ''
        if event_type == 'ticket_created':
            state['status'] = 'in_work'
        elif event_type in ('ticket_assigned_to_duty', 'ticket_assigned_to_staff'):
            state['assigned_username'] = row.get('actor_username') or state['assigned_username']
            if state['status'] not in ('ready_for_feedback', 'closed', 'rejected', 'mass_incident', 'closed_auto'):
                state['status'] = 'in_work'
        elif event_type == 'ticket_reopened_by_user':
            if state['status'] not in HARD_FINAL_TICKET_STATUSES:
                state['status'] = 'in_work'
        elif event_type == 'ticket_ready_for_feedback':
            if state['status'] not in HARD_FINAL_TICKET_STATUSES:
                state['status'] = 'ready_for_feedback'
        elif event_type in ('ticket_user_confirmed_resolved', 'ticket_resolved_by_staff'):
            if state['status'] not in HARD_FINAL_TICKET_STATUSES:
                state['status'] = 'closed'
        elif event_type in ('ticket_rejected', 'ticket_not_relevant'):
            state['status'] = 'rejected'
        elif event_type == 'ticket_mass_incident':
            state['status'] = 'mass_incident'
        elif event_type == 'ticket_auto_closed_reset_call':
            state['status'] = 'closed_auto'

    active = [tn for tn, st in states.items() if st['status'] == 'in_work' and st['assigned_username'] == actor_username]
    return len(active), sorted(active)


def _recent_overload_alert_sent(actor_username):
    since = (datetime.now() - timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
    if ANALYTICS_USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS c FROM ticket_events
                    WHERE event_type='ticket_overload_alert_sent'
                      AND actor_username=%s
                      AND created_at >= %s
                """, [actor_username, since])
                row = cur.fetchone()
                return int(row['c'] if isinstance(row, dict) else row[0]) > 0
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM ticket_events
            WHERE event_type='ticket_overload_alert_sent'
              AND actor_username=?
              AND created_at >= ?
        """, [actor_username, since])
        return int(cur.fetchone()[0] or 0) > 0


def _maybe_send_overload_alert(actor):
    count, tickets = _load_active_ticket_count(actor.get('username') or '')
    settings = _get_duty_settings()
    limit = max(1, _env_int_from_value(settings.get('overload_ticket_limit'), OVERLOAD_TICKET_LIMIT))
    alert_thread_id = _env_int_from_value(settings.get('overload_alert_thread_id'), 0)
    if count <= limit or _recent_overload_alert_sent(actor.get('username') or ''):
        return
    bot.send_message(
        TECH_SUPPORT_CHAT_ID,
        f"⚠️ <b>Перегрузка дежурного</b>\n\n"
        f"{_format_duty_mention(actor)}: в работе {count} заявок.\n"
        f"Порог: {limit}.\n"
        f"Заявки: {', '.join('№' + str(n) for n in tickets[:15])}",
        message_thread_id=alert_thread_id or IN_PROGRESS_THREAD_ID or NEW_TICKETS_THREAD_ID,
        parse_mode='HTML'
    )
    log_ticket_event(
        'ticket_overload_alert_sent',
        actor_name=actor.get('name') or '',
        actor_username=actor.get('username') or '',
        details={'open_count': count, 'ticket_numbers': tickets}
    )


def _assign_ticket(ticket_number, problem, actor, event_type='ticket_assigned_to_staff'):
    if not ticket_number:
        return
    log_ticket_event(
        event_type,
        ticket_number=ticket_number,
        problem=problem,
        actor_name=actor.get('name') or '',
        actor_username=actor.get('username') or '',
        details={'assigned_to': actor.get('username') or actor.get('name') or ''}
    )
    _maybe_send_overload_alert(actor)


_ensure_ticket_events_table()

if not BOT_TOKEN:
    print("❌ Ошибка: BOT_TOKEN не найден в .env файле")
    exit(1)

if TECH_SUPPORT_CHAT_ID == 0:
    print("⚠️  Предупреждение: TECH_SUPPORT_CHAT_ID не настроен")

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN)

print("=" * 60)
print("🤖 Telegram Bot для Helper")
print("=" * 60)
print(f"Bot Token: ***REDACTED***")
print(f"Tech Support Chat ID: {TECH_SUPPORT_CHAT_ID}")
print(f"NEW_TICKETS_THREAD_ID: {NEW_TICKETS_THREAD_ID}")
print(f"IN_PROGRESS_THREAD_ID: {IN_PROGRESS_THREAD_ID}")
print(f"SOLVED_TICKETS_THREAD_ID: {SOLVED_TICKETS_THREAD_ID}")
print("=" * 60)


# ============================================================================
# Обработчик кнопки "Готово"
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "ticket_done")
def handle_ticket_done(call):
    """Обработка нажатия кнопки 'Готово'"""
    print(f"🔔 Получен callback 'Готово'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        resolver_name = call.from_user.first_name or call.from_user.username or str(call.from_user.id)
        ticket_number = extract_ticket_number(original_message)
        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_done] {lock_reason}")
            return

        # Отправляем одно объединенное сообщение в раздел "В работе"
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"✅ ЗАЯВКА ГОТОВА, ЖДЁТ ПОДТВЕРЖДЕНИЯ ✅\n\n"
            f"{original_message}\n\n"
            f"👤 Отметил готово: {resolver_name}",
            message_thread_id=IN_PROGRESS_THREAD_ID
        )

        # Убираем кнопку с оригинального сообщения
        _remove_ticket_buttons(call.message.chat.id, call.message.message_id)

        # Логируем в БД
        parsed = parse_ticket_fields(original_message)
        log_ticket_event(
            event_type='ticket_resolved_by_staff',
            ticket_number=ticket_number,
            problem=parsed.get('problem', original_message),
            department=parsed.get('department', ''),
            user_name=parsed.get('name', ''),
            workplace=parsed.get('workplace', ''),
            actor_name=resolver_name,
            actor_username=call.from_user.username or str(call.from_user.id)
        )
        log_ticket_event(
            event_type='ticket_ready_for_feedback',
            ticket_number=ticket_number,
            problem=parsed.get('problem', original_message),
            department=parsed.get('department', ''),
            user_name=parsed.get('name', ''),
            workplace=parsed.get('workplace', ''),
            actor_name=resolver_name,
            actor_username=call.from_user.username or str(call.from_user.id),
            details={'source': 'telegram'}
        )

        bot.answer_callback_query(call.id, "✅ Заявка ждёт подтверждения инициатора")
        print("✅ Callback 'Готово' обработан успешно")

    except Exception as e:
        print(f"❌ Ошибка в handle_ticket_done: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, "❌ Ошибка при обработке")


# ============================================================================
# Обработчик кнопки "Не актуально"
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data in ("ticket_reject_prompt", "ticket_not_relevant"))
def handle_ticket_reject_prompt(call):
    """Запрос обязательной причины отклонения."""
    print(f"🔔 Получен callback 'Отклонён'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or ""
        ticket_number = extract_ticket_number(original_message)
        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_reject_prompt] {lock_reason}")
            return
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"❌ Для отклонения заявки №{ticket_number or '—'} ответьте на исходную заявку текстом:\n"
            f"<code>Отклонён: причина отклонения</code>",
            message_thread_id=NEW_TICKETS_THREAD_ID,
            parse_mode='HTML',
            reply_to_message_id=call.message.message_id
        )
        bot.answer_callback_query(call.id, "Укажите причину отклонения ответом на заявку")
    except Exception as e:
        print(f"❌ Ошибка в handle_ticket_reject_prompt: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, "❌ Ошибка при обработке")


@bot.callback_query_handler(func=lambda call: call.data == "ticket_mass_incident")
def handle_ticket_mass_incident(call):
    """Обработка статуса 'Массовый инцидент'."""
    print(f"🔔 Получен callback 'Массовый инцидент'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or ""
        ticket_number = extract_ticket_number(original_message)
        parsed = parse_ticket_fields(original_message)
        resolver_name = call.from_user.first_name or call.from_user.username or str(call.from_user.id)
        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_mass_incident] {lock_reason}")
            return
        _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"⚠️ МАССОВЫЙ ИНЦИДЕНТ ⚠️\n\n{original_message}\n\n👤 Отметил: {resolver_name}",
            message_thread_id=IN_PROGRESS_THREAD_ID
        )
        log_ticket_event(
            event_type='ticket_mass_incident',
            ticket_number=ticket_number,
            problem=parsed.get('problem', original_message),
            department=parsed.get('department', ''),
            user_name=parsed.get('name', ''),
            workplace=parsed.get('workplace', ''),
            actor_name=resolver_name,
            actor_username=call.from_user.username or str(call.from_user.id)
        )
        bot.answer_callback_query(call.id, "⚠️ Массовый инцидент зафиксирован")
    except Exception as e:
        print(f"❌ Ошибка в handle_ticket_mass_incident: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, "❌ Ошибка при обработке")


# ============================================================================
# Обработчик фото (для получения file_id)
# ============================================================================
@bot.message_handler(content_types=['photo'])
def handle_photo_upload(message):
    """Получает photo file_id для добавления в мануалы"""
    try:
        photo_id = message.photo[-1].file_id  # Берем самое большое фото
        file_size = message.photo[-1].file_size
        file_size_mb = file_size / (1024 * 1024)

        response_text = (
            f"✅ <b>Photo file_id получен:</b>\n\n"
            f"<code>{photo_id}</code>\n\n"
            f"Размер: {file_size_mb:.2f} MB\n\n"
            f"Скопируйте file_id выше и добавьте в manuals_data.json"
        )

        bot.reply_to(message, response_text, parse_mode='HTML')
        print(f"✅ Photo file_id: {photo_id} (Size: {file_size_mb:.2f}MB)")

    except Exception as e:
        print(f"❌ Ошибка при обработке фото: {e}")
        traceback.print_exc()
        bot.reply_to(message, "❌ Ошибка при получении file_id фото")


# ============================================================================
# Обработчик видео (для получения file_id)
# ============================================================================
@bot.message_handler(content_types=['video'])
def handle_video_upload(message):
    """Получает video file_id для добавления в мануалы"""
    try:
        video_id = message.video.file_id
        file_size = message.video.file_size
        file_size_mb = file_size / (1024 * 1024)
        duration = message.video.duration

        response_text = (
            f"✅ <b>Video file_id получен:</b>\n\n"
            f"<code>{video_id}</code>\n\n"
            f"Размер: {file_size_mb:.2f} MB\n"
            f"Длительность: {duration} сек\n\n"
            f"Скопируйте file_id выше и добавьте в manuals_data.json"
        )

        bot.reply_to(message, response_text, parse_mode='HTML')
        print(f"✅ Video file_id: {video_id} (Size: {file_size_mb:.2f}MB, Duration: {duration}s)")

    except Exception as e:
        print(f"❌ Ошибка при обработке видео: {e}")
        traceback.print_exc()
        bot.reply_to(message, "❌ Ошибка при получении file_id видео")


# ============================================================================
# Обработчик сообщений в группах/каналах
# ============================================================================
@bot.message_handler(func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_channel_messages(message):
    """Обработка сообщений в группах - пересылка заявок"""
    try:
        print(f"📨 Получено сообщение в группе: {message.text[:50] if message.text else 'N/A'} от {message.from_user.id}")
        if SUPPORT_STAFF_IDS and message.from_user.id not in SUPPORT_STAFF_IDS:
            print("❌ Сообщение не от сотрудника техподдержки")
            return

        # Проверяем есть ли reply (ответ на сообщение)
        if message.reply_to_message:
            original_message_id = message.reply_to_message.message_id
            text = message.text.lower() if message.text else ""
            original_ticket_text = message.reply_to_message.text or message.reply_to_message.caption or ''
            ticket_number = extract_ticket_number(original_ticket_text)
            parsed = parse_ticket_fields(original_ticket_text)
            actor = {
                'name': message.from_user.first_name or message.from_user.username or str(message.from_user.id),
                'username': message.from_user.username or str(message.from_user.id)
            }
            problem = parsed.get('problem', original_ticket_text)
            rejection_reason = _parse_rejection_reason(message.text or '')

            # Проверяем ключевые слова для перемещения заявки
            if "массовый инцидент" in text:
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                log_ticket_event(
                    'ticket_mass_incident',
                    ticket_number=ticket_number,
                    problem=problem,
                    department=parsed.get('department', ''),
                    user_name=parsed.get('name', ''),
                    workplace=parsed.get('workplace', ''),
                    actor_name=actor['name'],
                    actor_username=actor['username']
                )
                _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"⚠️ Заявка №{ticket_number or '—'} отмечена как массовый инцидент.",
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )
            elif "отклон" in text:
                if not rejection_reason:
                    bot.reply_to(message, "Для отклонения укажите причину в формате: Отклонён: причина")
                    return
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                details = {'reason': rejection_reason}
                for event_type in ('ticket_rejected', 'ticket_not_relevant'):
                    log_ticket_event(
                        event_type,
                        ticket_number=ticket_number,
                        problem=problem,
                        department=parsed.get('department', ''),
                        user_name=parsed.get('name', ''),
                        workplace=parsed.get('workplace', ''),
                        actor_name=actor['name'],
                        actor_username=actor['username'],
                        details=details
                    )
                _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"❌ ЗАЯВКА ОТКЛОНЕНА ❌\n\n"
                    f"№{ticket_number or '—'}\n"
                    f"Причина: {html_escape(rejection_reason)}\n"
                    f"Сотрудник: {html_escape(actor['name'])}",
                    message_thread_id=NEW_TICKETS_THREAD_ID,
                    parse_mode='HTML',
                    reply_to_message_id=original_message_id
                )
            elif "готово" in text or "решена" in text:
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                for event_type in ('ticket_resolved_by_staff', 'ticket_ready_for_feedback'):
                    log_ticket_event(
                        event_type,
                        ticket_number=ticket_number,
                        problem=problem,
                        department=parsed.get('department', ''),
                        user_name=parsed.get('name', ''),
                        workplace=parsed.get('workplace', ''),
                        actor_name=actor['name'],
                        actor_username=actor['username'],
                        details={'source': 'telegram_reply'} if event_type == 'ticket_ready_for_feedback' else None
                    )
                _remove_ticket_buttons(message.reply_to_message.chat.id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"✅ Заявка №{ticket_number or '—'} готова и ожидает подтверждения инициатора.",
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )
            elif "в работе" in text or "в процессе" in text:
                print("➡️  Пересылаем заявку в 'В работе'")

                # Пересылаем оригинальное сообщение
                bot.copy_message(
                    chat_id=TECH_SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=original_message_id,
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )

                # Добавляем комментарий
                safe_text = message.text[:1000] if message.text else "N/A"
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"💬 Статус по заявке: {safe_text}",
                    message_thread_id=IN_PROGRESS_THREAD_ID,
                    parse_mode=None
                )
                log_ticket_event(
                    'ticket_status_update_by_staff',
                    ticket_number=ticket_number,
                    problem=problem,
                    department=parsed.get('department', ''),
                    user_name=parsed.get('name', ''),
                    workplace=parsed.get('workplace', ''),
                    actor_name=actor['name'],
                    actor_username=actor['username'],
                    details={'status': 'in_work'}
                )
                _assign_ticket(ticket_number, problem, actor, 'ticket_assigned_to_staff')
                print("✅ Заявка перемещена в 'В работе'")

    except Exception as e:
        print(f"❌ Ошибка в handle_channel_messages: {e}")
        traceback.print_exc()


# ============================================================================
# Запуск бота
# ============================================================================
if __name__ == '__main__':
    print("\n🚀 Запуск Telegram бота...")
    print("🔍 Ожидание callback запросов от кнопок...")
    print(f"📊 Зарегистрировано handlers:")
    print(f"   - Message handlers: {len(bot.message_handlers)}")
    print(f"   - Callback handlers: {len(bot.callback_query_handlers)}")
    print("=" * 60)
    print("✅ Бот готов к работе!\n")

    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\n\n🛑 Получен сигнал остановки (Ctrl+C)")
        print("👋 Бот остановлен")
    except Exception as e:
        print(f"\n❌ Критическая ошибка в bot polling: {e}")
        traceback.print_exc()
        exit(1)
