import os
import sqlite3
import threading
import json
from typing import Any
from datetime import datetime, timedelta
from markupsafe import escape as m_escape
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from flask import Flask, render_template, request, session, redirect, url_for, flash, abort, jsonify, g
from dotenv import load_dotenv
import telebot
import werkzeug.routing
import traceback
import re
import json
from html import escape as html_escape
from functools import wraps
from time import time
from collections import defaultdict

# Загружаем переменные окружения ПЕРЕД импортом admin_manager
load_dotenv()

from flask_wtf.csrf import CSRFProtect
from admin_manager import admin_manager, AdminAuth, admins_manager, ROLE_SUPER_ADMIN, ROLE_EDITOR, ROLE_NAMES
from topics_manager import TopicsManager
from stats_manager import StatsManager
from trainer_manager import TrainerManager
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None

# ============================================
# RATE LIMITING
# ============================================

class RateLimiter:
    """Simple in-memory rate limiter"""
    def __init__(self):
        self.requests = defaultdict(list)
        self.login_attempts = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int = 60, window: int = 60) -> bool:
        """Check if request is allowed within rate limit"""
        now = time()
        # Clean old requests
        self.requests[key] = [req_time for req_time in self.requests[key]
                             if now - req_time < window]
        # Check limit
        if len(self.requests[key]) >= max_requests:
            return False
        self.requests[key].append(now)
        return True

    def check_login_attempt(self, ip: str, max_attempts: int = 5, window: int = 300) -> bool:
        """Check login attempts (stricter limit)"""
        now = time()
        self.login_attempts[ip] = [req_time for req_time in self.login_attempts[ip]
                                   if now - req_time < window]
        if len(self.login_attempts[ip]) >= max_attempts:
            return False
        self.login_attempts[ip].append(now)
        return True

rate_limiter = RateLimiter()
ticket_counter_lock = threading.Lock()
audit_log_lock = threading.Lock()

def rate_limit(max_requests: int = 60, window: int = 60):
    """Rate limiting decorator"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Security Fix: Get client IP safely (validate trusted proxy)
            # Only trust X-Forwarded-For if request comes from trusted proxy
            trusted_proxies = set()
            if TRUSTED_PROXY_IP:
                trusted_proxies = {ip.strip() for ip in TRUSTED_PROXY_IP.split(',') if ip.strip()}

            if request.remote_addr in trusted_proxies and request.headers.get('X-Forwarded-For'):
                ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
            else:
                ip = request.remote_addr

            key = f"{ip}:{f.__name__}"
            if not rate_limiter.is_allowed(key, max_requests, window):
                return jsonify({
                    'success': False,
                    'error': 'Слишком много запросов. Пожалуйста, подождите.'
                }), 429

            return f(*args, **kwargs)
        return decorated_function
    return decorator

app = Flask(__name__)

# Secret key must be set in environment variables
FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
if not FLASK_SECRET_KEY:
    print("CRITICAL ERROR: FLASK_SECRET_KEY not found in environment variables!")
    print("Please set FLASK_SECRET_KEY in your .env file")
    exit(1)
app.secret_key = FLASK_SECRET_KEY

# CSRF Configuration
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_TIME_LIMIT'] = None  # No time limit for CSRF tokens
csrf = CSRFProtect(app)

# Security configurations for production
# Security Fix: Always use secure cookies in production
IS_DEVELOPMENT = os.getenv('FLASK_ENV', 'production') == 'development'
default_secure_cookie = 'false' if IS_DEVELOPMENT else 'true'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', default_secure_cookie).lower() == 'true'
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent JavaScript access to session cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Lax for better compatibility
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour session timeout
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max request size (DoS protection)

# Security headers
@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'  # Changed from SAMEORIGIN to DENY for better security
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    # Security Fix: Add HSTS header for HTTPS enforcement
    if request.is_secure or not IS_DEVELOPMENT:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    # Security Fix: Improved CSP - consider removing unsafe-inline in future iterations
    # TODO: Remove unsafe-inline by using nonces or hashes for inline scripts/styles
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://api.telegram.org data:; "
        "media-src 'self' https://api.telegram.org; "
        "font-src 'self'; "
        "frame-ancestors 'none'; "  # Changed from 'self' to 'none'
        "base-uri 'self'; "  # Added base-uri restriction
        "form-action 'self';"  # Added form-action restriction
    )

    # Security Fix: Add additional security headers
    response.headers['X-Permitted-Cross-Domain-Policies'] = 'none'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'

    return response


@app.before_request
def mark_request_start():
    """Отмечает старт запроса для расчета времени выполнения."""
    g.request_started_at = time()


@app.after_request
def audit_request(response):
    """Аудит всех действий пользователей/администраторов с IP и временем."""
    try:
        if request.path.startswith('/static/'):
            return response

        request_duration_ms = int((time() - getattr(g, 'request_started_at', time())) * 1000)
        request_data = {}

        if request.args:
            request_data.update({f"arg:{k}": v for k, v in request.args.items()})

        if request.form:
            request_data.update({f"form:{k}": v for k, v in request.form.items()})

        if request.is_json:
            payload = request.get_json(silent=True) or {}
            if isinstance(payload, dict):
                request_data.update({f"json:{k}": v for k, v in payload.items()})

        if request.files:
            for key, file in request.files.items():
                request_data[f"file:{key}"] = f"{file.filename}|{file.mimetype}"

        request_data['duration_ms'] = request_duration_ms
        request_data['user_agent'] = request.headers.get('User-Agent', '')[:250]

        action = f"{request.method} {request.path}"
        write_audit_log(action=action, status_code=response.status_code, details=request_data)
    except Exception as e:
        print(f"[audit_log] Ошибка post-request аудита: {e}")

    return response

APP_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('TEST_BOT_TOKEN') if APP_TEST_MODE and os.getenv('TEST_BOT_TOKEN') else os.getenv('BOT_TOKEN')

TRUSTED_PROXY_IP = os.getenv("TRUSTED_PROXY_IP")
TICKET_COUNTER_DB_PATH = os.getenv('TICKET_COUNTER_DB', 'topics.db')
TICKET_NUMBER_START = int(os.getenv('TICKET_NUMBER_START', '125'))
AUDIT_LOG_DB_PATH = os.getenv('AUDIT_LOG_DB', 'topics.db')
ANALYTICS_BACKEND = os.getenv('ANALYTICS_BACKEND', 'postgres').lower()
ANALYTICS_USE_POSTGRES = ANALYTICS_BACKEND == 'postgres' and psycopg2 is not None
POSTGRES_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': os.getenv('POSTGRES_PORT', '5432'),
    'database': os.getenv('POSTGRES_DB', 'helper_analytics'),
    'user': os.getenv('POSTGRES_USER', 'ruslan'),
    'password': os.getenv('POSTGRES_PASSWORD', ''),
    'connect_timeout': 10,
    'sslmode': os.getenv('POSTGRES_SSLMODE', 'prefer')
}

# Security Fix: Safe integer conversion with validation
try:
    TECH_SUPPORT_CHAT_ID = int(os.getenv('TECH_SUPPORT_CHAT_ID', '0'))
    NEW_TICKETS_THREAD_ID = int(os.getenv('NEW_TICKETS_THREAD_ID', '0'))
    IN_PROGRESS_THREAD_ID = int(os.getenv('IN_PROGRESS_THREAD_ID', '0'))
    SOLVED_TICKETS_THREAD_ID = int(os.getenv('SOLVED_TICKETS_THREAD_ID', '0'))
    CISCO_TICKETS_THREAD_ID = int(os.getenv('CISCO_TICKETS_THREAD_ID', '0'))

    if not all([TECH_SUPPORT_CHAT_ID, NEW_TICKETS_THREAD_ID, IN_PROGRESS_THREAD_ID, SOLVED_TICKETS_THREAD_ID]):
        print("ПРЕДУПРЕЖДЕНИЕ: Не все ID чатов/топиков Telegram установлены!")
except (ValueError, TypeError) as e:
    print(f"ОШИБКА: Некорректные значения ID в переменных окружения: {e}")
    exit(1)

# Список ID пользователей техподдержки (загружается из env)
SUPPORT_STAFF_IDS_STR = os.getenv('SUPPORT_STAFF_IDS', '')
SUPPORT_STAFF_IDS = [int(x.strip()) for x in SUPPORT_STAFF_IDS_STR.split(',') if x.strip().isdigit()]

if not BOT_TOKEN:
    print("Ошибка: BOT_TOKEN не найден в переменных окружения. Пожалуйста, проверьте ваш .env файл.")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)


def _init_ticket_counter_table():
    """Инициализация таблицы для инкрементного номера заявок."""
    try:
        with sqlite3.connect(TICKET_COUNTER_DB_PATH, timeout=10.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_sequence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"[ticket_counter] Ошибка инициализации таблицы: {e}")


def get_next_ticket_number() -> int:
    """Возвращает следующий инкрементный номер заявки."""
    with ticket_counter_lock:
        with sqlite3.connect(TICKET_COUNTER_DB_PATH, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ticket_sequence (created_at) VALUES (?)",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
            )
            conn.commit()
            seq_id = cursor.lastrowid
            return TICKET_NUMBER_START + seq_id - 1


def _pg_connect():
    """Открывает новое подключение к PostgreSQL для аналитики."""
    if not ANALYTICS_USE_POSTGRES or not psycopg2:
        return None
    return psycopg2.connect(**POSTGRES_CONFIG, cursor_factory=RealDictCursor)


def _init_audit_log_table():
    """Инициализация таблицы аудита действий пользователей и администраторов."""
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS audit_logs (
                            id BIGSERIAL PRIMARY KEY,
                            created_at TIMESTAMP NOT NULL,
                            ip_address TEXT,
                            method TEXT,
                            path TEXT,
                            endpoint TEXT,
                            status_code INTEGER,
                            actor_type TEXT,
                            actor_username TEXT,
                            actor_name TEXT,
                            actor_role TEXT,
                            action TEXT,
                            details_json JSONB
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_username)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_path ON audit_logs(path)")
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        ip_address TEXT,
                        method TEXT,
                        path TEXT,
                        endpoint TEXT,
                        status_code INTEGER,
                        actor_type TEXT,
                        actor_username TEXT,
                        actor_name TEXT,
                        actor_role TEXT,
                        action TEXT,
                        details_json TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_username)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_path ON audit_logs(path)")
                conn.commit()
    except Exception as e:
        print(f"[audit_log] Ошибка инициализации таблицы: {e}")


def _init_analytics_tables():
    """Инициализация таблиц аналитики для dashboard."""
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
                            department TEXT,
                            user_name TEXT,
                            workplace TEXT,
                            channel TEXT,
                            topic_name TEXT,
                            is_cisco INTEGER DEFAULT 0,
                            actor_name TEXT,
                            actor_username TEXT,
                            actor_role TEXT
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_type ON ticket_events(event_type)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_number)")

                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS topic_changes (
                            id BIGSERIAL PRIMARY KEY,
                            created_at TIMESTAMP NOT NULL,
                            action TEXT NOT NULL,
                            topic_id INTEGER,
                            channel TEXT,
                            full_topic TEXT,
                            actor_name TEXT,
                            actor_username TEXT,
                            actor_role TEXT,
                            details_json JSONB
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_changes_created_at ON topic_changes(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_changes_action ON topic_changes(action)")

                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS topic_search_events (
                            id BIGSERIAL PRIMARY KEY,
                            created_at TIMESTAMP NOT NULL,
                            query_text TEXT,
                            channel TEXT,
                            results_count INTEGER DEFAULT 0,
                            actor_name TEXT,
                            actor_username TEXT,
                            actor_role TEXT
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_created_at ON topic_search_events(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_channel ON topic_search_events(channel)")
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
                        department TEXT,
                        user_name TEXT,
                        workplace TEXT,
                        channel TEXT,
                        topic_name TEXT,
                        is_cisco INTEGER DEFAULT 0,
                        actor_name TEXT,
                        actor_username TEXT,
                        actor_role TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_type ON ticket_events(event_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_number)")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS topic_changes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        topic_id INTEGER,
                        channel TEXT,
                        full_topic TEXT,
                        actor_name TEXT,
                        actor_username TEXT,
                        actor_role TEXT,
                        details_json TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_changes_created_at ON topic_changes(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_changes_action ON topic_changes(action)")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS topic_search_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        query_text TEXT,
                        channel TEXT,
                        results_count INTEGER DEFAULT 0,
                        actor_name TEXT,
                        actor_username TEXT,
                        actor_role TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_created_at ON topic_search_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_channel ON topic_search_events(channel)")
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка инициализации таблиц: {e}")


def get_client_ip() -> str:
    """Безопасно определяет клиентский IP с учетом доверенных прокси."""
    trusted_proxies = set()
    if TRUSTED_PROXY_IP:
        trusted_proxies = {ip.strip() for ip in TRUSTED_PROXY_IP.split(',') if ip.strip()}

    if request.remote_addr in trusted_proxies and request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or ''


def _sanitize_audit_payload(data: dict) -> dict:
    """Удаляет чувствительные поля и ограничивает длину значений для аудита."""
    sensitive_markers = ('password', 'token', 'secret', 'csrf')
    sanitized = {}
    for key, value in data.items():
        key_str = str(key)
        if any(marker in key_str.lower() for marker in sensitive_markers):
            sanitized[key_str] = "***REDACTED***"
            continue
        value_str = str(value)
        if len(value_str) > 500:
            value_str = value_str[:500] + "...[truncated]"
        sanitized[key_str] = value_str
    return sanitized


def write_audit_log(action: str, status_code: int, details: dict | None = None):
    """Пишет запись аудита в БД."""
    try:
        if details is None:
            details = {}

        actor_type = 'guest'
        actor_username = ''
        actor_name = ''
        actor_role = ''

        if session.get('admin_logged_in'):
            actor_type = 'admin'
            actor_username = session.get('admin_username', '')
            actor_name = session.get('admin_username', '')
            actor_role = session.get('admin_role', '')
        elif session.get('authenticated') and session.get('user_info'):
            actor_type = 'user'
            user_info = session.get('user_info', {})
            actor_username = user_info.get('username', '')
            actor_name = user_info.get('name', '')
            actor_role = 'user'

        record = {
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'ip_address': get_client_ip(),
            'method': request.method,
            'path': request.path,
            'endpoint': request.endpoint or '',
            'status_code': int(status_code),
            'actor_type': actor_type,
            'actor_username': actor_username,
            'actor_name': actor_name,
            'actor_role': actor_role,
            'action': action,
            'details_json': json.dumps(_sanitize_audit_payload(details), ensure_ascii=False)
        }

        with audit_log_lock:
            if ANALYTICS_USE_POSTGRES:
                with _pg_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO audit_logs (
                                created_at, ip_address, method, path, endpoint, status_code,
                                actor_type, actor_username, actor_name, actor_role, action, details_json
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        """, (
                            record['created_at'], record['ip_address'], record['method'], record['path'],
                            record['endpoint'], record['status_code'], record['actor_type'],
                            record['actor_username'], record['actor_name'], record['actor_role'],
                            record['action'], record['details_json']
                        ))
                    conn.commit()
            else:
                with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                    conn.execute("""
                        INSERT INTO audit_logs (
                            created_at, ip_address, method, path, endpoint, status_code,
                            actor_type, actor_username, actor_name, actor_role, action, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        record['created_at'], record['ip_address'], record['method'], record['path'],
                        record['endpoint'], record['status_code'], record['actor_type'],
                        record['actor_username'], record['actor_name'], record['actor_role'],
                        record['action'], record['details_json']
                    ))
                    conn.commit()
    except Exception as e:
        print(f"[audit_log] Ошибка записи: {e}")


def _current_actor():
    """Возвращает данные текущего пользователя/админа."""
    if session.get('admin_logged_in'):
        return {
            'name': session.get('admin_username', ''),
            'username': session.get('admin_username', ''),
            'role': session.get('admin_role', '')
        }
    if session.get('authenticated') and session.get('user_info'):
        user_info = session.get('user_info', {})
        return {
            'name': user_info.get('name', ''),
            'username': user_info.get('username', ''),
            'role': 'user'
        }
    return {'name': '', 'username': '', 'role': 'guest'}


def log_ticket_event(event_type: str, ticket_number: int | None = None, problem: str = '',
                     channel: str = '', topic_name: str = '', is_cisco: bool = False):
    """Логирует событие по заявке для аналитики."""
    try:
        actor = _current_actor()
        user_info = session.get('user_info', {})
        payload = (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            event_type,
            ticket_number,
            str(problem)[:500],
            str(user_info.get('department', ''))[:200],
            str(user_info.get('name', ''))[:200],
            str(user_info.get('workplace', ''))[:100],
            str(channel)[:150],
            str(topic_name)[:500],
            1 if is_cisco else 0,
            str(actor['name'])[:200],
            str(actor['username'])[:200],
            str(actor['role'])[:100]
        )
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ticket_events (
                            created_at, event_type, ticket_number, problem, department, user_name, workplace,
                            channel, topic_name, is_cisco, actor_name, actor_username, actor_role
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO ticket_events (
                        created_at, event_type, ticket_number, problem, department, user_name, workplace,
                        channel, topic_name, is_cisco, actor_name, actor_username, actor_role
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, payload)
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка логирования ticket_event: {e}")

def log_manual_open(manual_title: str, has_video: bool):
    """Логирует факт открытия инструкции (видео/текст)."""
    event_type = 'manual_opened_video' if has_video else 'manual_opened_text'
    log_ticket_event(
        event_type=event_type,
        ticket_number=None,
        problem=str(manual_title or '')[:500]
    )


def log_topic_change(action: str, topic_id: int | None = None, channel: str = '',
                     full_topic: str = '', details: dict | None = None):
    """Логирует изменения по тематикам."""
    try:
        actor = _current_actor()
        details = details or {}
        details_json = json.dumps(_sanitize_audit_payload(details), ensure_ascii=False)
        payload = (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            action,
            topic_id,
            str(channel)[:150],
            str(full_topic)[:500],
            str(actor['name'])[:200],
            str(actor['username'])[:200],
            str(actor['role'])[:100],
            details_json
        )
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO topic_changes (
                            created_at, action, topic_id, channel, full_topic,
                            actor_name, actor_username, actor_role, details_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO topic_changes (
                        created_at, action, topic_id, channel, full_topic,
                        actor_name, actor_username, actor_role, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, payload)
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка логирования topic_change: {e}")


def log_topic_search(query_text: str, channel: str, results_count: int):
    """Логирует поиски тематик для dashboard."""
    try:
        actor = _current_actor()
        payload = (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            str(query_text)[:500],
            str(channel)[:150],
            int(results_count),
            str(actor['name'])[:200],
            str(actor['username'])[:200],
            str(actor['role'])[:100]
        )
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO topic_search_events (
                            created_at, query_text, channel, results_count,
                            actor_name, actor_username, actor_role
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO topic_search_events (
                        created_at, query_text, channel, results_count,
                        actor_name, actor_username, actor_role
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, payload)
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка логирования topic_search: {e}")


def extract_ticket_number_from_text(text: str) -> int | None:
    """Извлекает номер заявки из текста вида 'НОВАЯ ЗАЯВКА №123'."""
    if not text:
        return None
    match = re.search(r'№\s*(\d+)', text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, TypeError):
        return None

# Инициализация TopicsManager
tm = TopicsManager("topics.db")

# Инициализация TrainerManager
trainer_mgr = TrainerManager("topics.db")

# Инициализация счётчика заявок
_init_ticket_counter_table()
_init_audit_log_table()
_init_analytics_tables()

# TODO: СТАТИСТИКА В РАЗРАБОТКЕ
# Инициализация StatsManager для сбора аналитики
# ВНИМАНИЕ: Модуль статистики находится в стадии разработки и тестирования
# Используется PostgreSQL для хранения данных аналитики
# В production окружении убедитесь, что база данных настроена корректно
# ОТКЛЮЧЕНО: Раскомментируйте когда настроите PostgreSQL
# sm = StatsManager()
sm = None

# Константы результатов обращения (используются даже когда StatsManager отключен)
RESULT_VIDEO_HELPED = "video_helped"
RESULT_VIDEO_NOT_HELPED = "video_not_helped"
RESULT_SOLVED_BY_HELPER = "solved_by_helper"
RESULT_TICKET_CREATED = "ticket_created"
RESULT_TICKET_DONE = "ticket_done"
RESULT_TICKET_NOT_RELEVANT = "ticket_not_relevant"

# Импорт тематик при первом запуске (если база пустая)
stats = tm.get_statistics()
if stats['total_topics'] == 0:
    try:
        # Сначала пробуем загрузить полную базу
        import os
        if os.path.exists("topics_full.csv"):
            print("📊 База данных тематик пустая, импортирую topics_full.csv...")
            result = tm.import_from_csv("topics_full.csv", encoding="utf-8")
        else:
            print("📊 База данных тематик пустая, импортирую example_topics.csv...")
            result = tm.import_from_csv("example_topics.csv", encoding="utf-8")

        if result['success']:
            print(f"✅ Импортировано тематик: {result['imported']}")
        else:
            print(f"⚠️ Ошибка импорта: {result.get('error', 'Неизвестная ошибка')}")
    except Exception as e:
        print(f"⚠️ Не удалось импортировать данные: {e}")

def deep_escape(obj: Any) -> Any:
    """Рекурсивно экранирует все строковые поля (dict, list, tuple, str)."""
    if isinstance(obj, str):
        return m_escape(obj)
    if isinstance(obj, dict):
        return {k: deep_escape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_escape(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(deep_escape(v) for v in obj)
    return obj

# Загружаем мануалы из JSON файла через admin_manager
def load_manuals():
    """Загружает мануалы из JSON файла при каждом запросе"""
    return admin_manager.load_manuals()

def create_ticket_buttons():
    """Создает кнопки для заявки: Готово и Не актуально"""
    markup = InlineKeyboardMarkup(row_width=2)
    button_done = InlineKeyboardButton("Готово ✅", callback_data="ticket_done")
    button_not_relevant = InlineKeyboardButton("Не актуально ❌", callback_data="ticket_not_relevant")
    markup.add(button_done, button_not_relevant)
    return markup

# Функция для получения URL изображения
# Note: This function returns Telegram API URLs that contain the bot token.
# These URLs are safe to use in server-side rendering but should not be exposed
# in client-side JavaScript or cached publicly. Telegram file URLs expire after ~1 hour.
def get_file_url(file_id):
    try:
        if not file_id:
            return None
        # Validate file_id format to prevent injection
        if not isinstance(file_id, str) or len(file_id) > 200:
            return None

        # Check if it's a local video file (stored in static/videos/)
        if file_id.endswith('.MOV') or file_id.endswith('.mov') or file_id.endswith('.mp4'):
            # Return URL for static file
            return url_for('static', filename=f'videos/{file_id}')

        # Otherwise, it's a Telegram file_id - get it from Telegram API
        file_info = bot.get_file(file_id)
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    except telebot.apihelper.ApiTelegramException as e:
        # Don't log file_id in production - could be user input
        print(f"Telegram API error getting file URL")
        return None
    except Exception as e:
        print(f"Error getting file URL")
        return None

def send_ticket(problem, screenshots=None, topic_info=None, video=None, thread_id=None):
    user_info = session.get('user_info', {})
    department = user_info.get('department', 'Неизвестно')
    name = user_info.get('name', 'Неизвестно')
    workplace = user_info.get('workplace', '')
    ticket_number = get_next_ticket_number()
    session['current_ticket_number'] = ticket_number
    session.modified = True

    # Формируем сообщение
    support_message = (
        f"🚨 **НОВАЯ ЗАЯВКА №{ticket_number}** 🚨\n"
        f"Отдел: {department}\n"
        f"Имя: {name}\n"
    )

    # Добавляем рабочее место если оно указано
    if workplace:
        support_message += f"Рабочее место: {workplace}\n"

    support_message += f"Проблема: {problem}\n"

    # Тематика НЕ отправляется в Telegram - только для маркировки в CRM
    # topic_info используется только на стороне веб-приложения

    try:
        target_thread_id = thread_id or NEW_TICKETS_THREAD_ID
        print(f"[send_ticket] Отправка новой заявки в чат {TECH_SUPPORT_CHAT_ID}")
        msg = bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            support_message,
            message_thread_id=target_thread_id,
            parse_mode='Markdown',
            reply_markup=create_ticket_buttons()  # <- добавляем кнопки
        )
        print(f"[send_ticket] OK, message_id={getattr(msg, 'message_id', 'unknown')}")

        # Отправляем скриншоты, если они есть
        if screenshots:
            try:
                if len(screenshots) > 1:
                    media = []
                    for i, screenshot in enumerate(screenshots, 1):
                        if i == 1:
                            media.append(InputMediaPhoto(screenshot, caption="Скриншоты"))
                        else:
                            media.append(InputMediaPhoto(screenshot))
                    bot.send_media_group(
                        TECH_SUPPORT_CHAT_ID,
                        media,
                        message_thread_id=target_thread_id
                    )
                    print(f"[send_ticket] Отправлены скриншоты альбомом ({len(screenshots)})")
                else:
                    bot.send_photo(
                        TECH_SUPPORT_CHAT_ID,
                        screenshots[0],
                        caption="Скриншот 1",
                        message_thread_id=target_thread_id
                    )
                    print("[send_ticket] Отправлен скриншот 1")
            except Exception as e:
                print(f"[send_ticket] Ошибка при отправке скриншотов: {e}")

        if video:
            try:
                bot.send_video(
                    TECH_SUPPORT_CHAT_ID,
                    video,
                    caption="Видео",
                    message_thread_id=target_thread_id
                )
                print("[send_ticket] Отправлено видео")
            except Exception as e:
                print(f"[send_ticket] Ошибка при отправке видео: {e}")

        # Логируем в PostgreSQL для статистики
        topic_id = None
        topic_name = None
        if topic_info:
            topic_name = topic_info.get('topic')
            # Можно попробовать извлечь topic_id из session или topic_info
            try:
                from flask import request
                if request.method == 'POST':
                    topic_id = request.form.get('selected_topic_id')
                    if topic_id:
                        topic_id = int(topic_id)
            except:
                pass

        if sm:
            sm.log_request(
                result_type=RESULT_TICKET_CREATED,
                problem_description=problem,
                department=department,
                name=name,
                workplace=workplace,
                problem_id=session.get('problem_id'),
                subproblem_id=session.get('current_subproblem_id'),
                topic_id=topic_id,
                topic_name=topic_name
            )

        log_ticket_event(
            event_type='ticket_created',
            ticket_number=ticket_number,
            problem=problem,
            channel=topic_info.get('channel', '') if topic_info else '',
            topic_name=topic_name or '',
            is_cisco=(target_thread_id == CISCO_TICKETS_THREAD_ID and CISCO_TICKETS_THREAD_ID != 0)
        )

        return msg
    except Exception as e:
        print("[send_ticket] Ошибка при отправке заявки:", e)
        traceback.print_exc()
        return None
# обработчик кнопки
# обработчик кнопки "Готово"
@bot.callback_query_handler(func=lambda call: call.data == "ticket_done")
def handle_ticket_done(call):
    print(f"🔔 Получен callback от кнопки 'Готово'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        resolver_name = call.from_user.first_name or call.from_user.username or str(call.from_user.id)
        ticket_number = extract_ticket_number_from_text(original_message)

        # Отправляем одно объединенное сообщение в раздел "В работе"
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"✅ НОВАЯ ЗАЯВКА РЕШЕНА ✅\n\n"
            f"{original_message}\n\n"
            f"👤 Решена сотрудником: {resolver_name}",
            message_thread_id=IN_PROGRESS_THREAD_ID
        )

        # Убираем кнопку с оригинального сообщения
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )

        log_ticket_event(
            event_type='ticket_resolved_by_staff',
            ticket_number=ticket_number,
            problem=original_message,
            channel='',
            topic_name=''
        )

        print("✅ Кнопка 'Готово' успешно обработана!")

    except Exception as e:
        print(f"❌ Ошибка при обработке кнопки 'Готово': {e}")
        traceback.print_exc()

# обработчик кнопки "Не актуально"
@bot.callback_query_handler(func=lambda call: call.data == "ticket_not_relevant")
def handle_ticket_not_relevant(call):
    print(f"🔔 Получен callback от кнопки 'Не актуально'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        ticket_number = extract_ticket_number_from_text(original_message)
        # Убираем кнопки с оригинального сообщения
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )

        # Отправляем отдельное сообщение о том, что заявка не актуальна
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"❌ ЗАЯВКА НЕ АКТУАЛЬНА ❌\n\n"
            f"Заявка отмечена сотрудником {call.from_user.first_name} как не актуальная.\n"
            f"Решение не требуется.",
            message_thread_id=NEW_TICKETS_THREAD_ID,
            parse_mode='Markdown',
            reply_to_message_id=call.message.message_id
        )

        log_ticket_event(
            event_type='ticket_not_relevant',
            ticket_number=ticket_number,
            problem=original_message
        )

        print("✅ Кнопка 'Не актуально' успешно обработана!")

    except Exception as e:
        print(f"❌ Ошибка при обработке кнопки 'Не актуально': {e}")
        traceback.print_exc()

def send_solved_ticket(problem):
    user_info = session.get('user_info')
    if user_info:
        department = user_info.get('department', 'Неизвестно')
        name = user_info.get('name', 'Неизвестно')
        workplace = user_info.get('workplace', 'Неизвестно')

        support_message = (
            f"✅ **ПРОБЛЕМА РЕШЕНА Помощником** ✅\n"
            f"Отдел: {department}\n"
            f"Имя: {name}\n"
            f"Рабочее место: {workplace}\n"
            f"Проблема: {problem}"
        )
        try:
            bot.send_message(
                TECH_SUPPORT_CHAT_ID,
                support_message,
                message_thread_id=SOLVED_TICKETS_THREAD_ID,
                parse_mode='Markdown'
            )

            # Логируем в PostgreSQL для статистики
            if sm:
                sm.log_request(
                    result_type=RESULT_SOLVED_BY_HELPER,
                    problem_description=problem,
                    department=department,
                    name=name,
                    workplace=workplace,
                    problem_id=session.get('problem_id'),
                    subproblem_id=session.get('current_subproblem_id')
                )
            log_ticket_event(
                # Текстовый мануал/шаги помогли (результат до создания заявки)
                event_type='manual_helped',
                ticket_number=session.get('current_ticket_number'),
                problem=problem
            )
        except Exception as e:
            print(f"Ошибка при отправке решённой заявки: {e}")
            traceback.print_exc()

def send_video_feedback(problem, helped):
    """Отправляет уведомление в ТГ о том, помогло ли видео-мануал"""
    user_info = session.get('user_info')
    if user_info:
        department = user_info.get('department', 'Неизвестно')
        name = user_info.get('name', 'Неизвестно')
        workplace = user_info.get('workplace', 'Неизвестно')

        if helped:
            support_message = (
                f"📹 **ВИДЕО-МАНУАЛ ПОМОГ** ✅\n"
                f"Отдел: {department}\n"
                f"Имя: {name}\n"
                f"Рабочее место: {workplace}\n"
                f"Проблема: {problem}"
            )
            thread_id = SOLVED_TICKETS_THREAD_ID
            result_type = RESULT_VIDEO_HELPED
        else:
            support_message = (
                f"📹 **ВИДЕО-МАНУАЛ НЕ ПОМОГ** ❌\n"
                f"Отдел: {department}\n"
                f"Имя: {name}\n"
                f"Рабочее место: {workplace}\n"
                f"Проблема: {problem}\n"
                f"Пользователь перешел к пошаговой инструкции"
            )
            thread_id = SOLVED_TICKETS_THREAD_ID
            result_type = RESULT_VIDEO_NOT_HELPED

        try:
            bot.send_message(
                TECH_SUPPORT_CHAT_ID,
                support_message,
                message_thread_id=thread_id,
                parse_mode='Markdown'
            )

            # Логируем в PostgreSQL для статистики
            if sm:
                sm.log_request(
                    result_type=result_type,
                    problem_description=problem,
                    department=department,
                    name=name,
                    workplace=workplace,
                    problem_id=session.get('problem_id'),
                    subproblem_id=session.get('current_subproblem_id')
                )
            log_ticket_event(
                event_type='video_helped' if helped else 'video_not_helped',
                ticket_number=session.get('current_ticket_number'),
                problem=problem
            )
        except Exception as e:
            print(f"Ошибка при отправке фидбека по видео: {e}")
            traceback.print_exc()

@app.route('/video_feedback/<string:result>')
def video_feedback(result):
    """Обработка фидбека по видео-мануалу"""
    if 'user_info' not in session:
        return redirect(url_for('index'))

    problem_description = session.get('problem_title', 'Неизвестная проблема')

    if result == 'helped':
        # Видео помогло - отправляем уведомление и завершаем
        send_video_feedback(problem_description, helped=True)
        return render_template('success.html')
    elif result == 'not_helped':
        # Видео не помогло - отправляем уведомление и показываем страницу с инструкцией
        send_video_feedback(problem_description, helped=False)
        session['video_not_helped'] = True
        return redirect(url_for('show_manual_steps'))
    else:
        return redirect(url_for('show_problems'))

@app.route('/manual_steps')
def show_manual_steps():
    """Показывает только пошаговую инструкцию (без видео) после того как видео не помогло"""
    if 'user_info' not in session:
        return redirect(url_for('index'))

    problem_id = session.get('problem_id')
    subproblem_id = session.get('current_subproblem_id')

    if not problem_id:
        return redirect(url_for('show_problems'))

    manuals = load_manuals()
    problem_data = manuals.get(problem_id, {})

    # Получаем данные подпроблемы или основной проблемы
    if subproblem_id:
        subproblems = problem_data.get('subproblems', {})
        data = subproblems.get(subproblem_id, {})
    else:
        data = problem_data

    manual_title = session.get('problem_title', 'Инструкция')
    # Фиксируем просмотр текстовой инструкции (после видео или без видео)
    log_manual_open(manual_title, has_video=False)

    # Получаем фото
    photo_urls_with_captions = []
    for photo in data.get('photos', []):
        url = get_file_url(photo.get('id'))
        if not url:
            continue
        caption = photo.get('caption', '')
        safe_caption = m_escape(str(caption).strip()[:300])
        photo_urls_with_captions.append({'url': url, 'caption': safe_caption})

    safe_manual_data = deep_escape(data)
    safe_photos = deep_escape(photo_urls_with_captions)

    return render_template(
        'manual.html',
        manual=safe_manual_data,
        manual_title=manual_title,
        photo_urls_with_captions=safe_photos,
        video_data=None,  # Не показываем видео
        skip_video_feedback=True  # Флаг чтобы не показывать опрос по видео
    )

@app.route('/')
def index():
    """Главная страница - редирект на логин если не авторизован"""
    if 'user_info' not in session:
        return redirect(url_for('user_login'))
    return redirect(url_for('choose_help_type'))

@app.route('/submit_user_info', methods=['POST'])
def submit_user_info():
    """Устаревший маршрут - теперь используется AD аутентификация"""
    return redirect(url_for('user_login'))

@app.route('/choose_help_type')
def choose_help_type():
    """Страница выбора типа помощи после авторизации"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))
    return render_template('choose_help_type.html', user_info=session['user_info'])

@app.route('/search_topics')
def search_topics():
    """Страница поиска тематик обращений - workplace не требуется"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    # Получаем список каналов из БД
    channels = tm.get_all_channels()
    return render_template('search_topics.html', channels=channels)

@app.route('/submit_selected_topic', methods=['POST'])
def submit_selected_topic():
    """Обработка выбранной тематики и отправка в Telegram"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    try:
        selected_topic_id = request.form.get('selected_topic_id')
        selected_topic_name = request.form.get('selected_topic_name')
        selected_topic_similarity = request.form.get('selected_topic_similarity')

        if not selected_topic_id or not selected_topic_name:
            flash('Не выбрана тематика')
            return redirect(url_for('search_topics'))

        # Формируем topic_info для отправки
        topic_info = {
            'topic': selected_topic_name,
            'similarity': selected_topic_similarity
        }

        # Отправляем заявку с выбранной тематикой
        send_ticket(f"Запрос по тематике: {selected_topic_name}", None, topic_info)

        # Очищаем сессию и показываем страницу успеха
        session.clear()
        return render_template('ticket_sent.html')

    except Exception as e:
        print(f"[submit_selected_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при отправке заявки')
        return redirect(url_for('search_topics'))

@app.route('/problems')
def show_problems():
    """Страница мануалов - требует указания рабочего места"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    # Проверяем наличие workplace, если нет - запрашиваем
    if not session['user_info'].get('workplace'):
        session['next_after_workplace'] = 'show_problems'
        return redirect(url_for('enter_workplace'))

    # Загружаем актуальные мануалы из JSON
    return render_template('problems.html', manuals=load_manuals())

@app.route('/select_problem/<string:problem_id>')
def select_problem(problem_id):
    # Проверяем авторизацию по сессии
    if 'user_info' not in session or not session.get('authenticated'):
        print("[select_problem] No user_info in session, redirecting to login")
        return redirect(url_for('user_login'))

    # Проверяем наличие workplace
    if not session['user_info'].get('workplace'):
        session['next_after_workplace'] = 'show_problems'
        return redirect(url_for('enter_workplace'))

    # --- Проверяем корректность problem_id ---
    if not re.match(r'^\d+$', problem_id):
        print(f"[select_problem] Invalid problem_id format: {problem_id}")
        abort(404)

    # Загружаем актуальные мануалы из JSON
    manuals = load_manuals()

    # Проверяем что мануал существует
    if problem_id not in manuals:
        print(f"[select_problem] problem_id not in manuals: {problem_id}")
        flash('Выбрана несуществующая проблема.')
        return redirect(url_for('show_problems'))

    problem_data = manuals.get(problem_id, {})

    # --- Есть подпроблемы ---
    if 'subproblems' in problem_data and isinstance(problem_data['subproblems'], dict):
        session['problem_id'] = problem_id
        safe_problem_id = m_escape(problem_id)

        sanitized_subproblems = {}
        for sid, sub in problem_data['subproblems'].items():
            safe_sid = str(sid)
            title = sub.get('title', '')
            safe_title = m_escape(str(title).strip()[:200])
            sanitized_subproblems[safe_sid] = {'title': safe_title}

        # Получаем и экранируем подсказки по версиям, если они есть
        version_hints = None
        if 'version_hints' in problem_data:
            hints_data = problem_data['version_hints']
            version_hints = {
                'title': m_escape(str(hints_data.get('title', '')).strip()[:200]),
                'hints': []
            }
            for hint in hints_data.get('hints', []):
                hint_item = {
                    'version': m_escape(str(hint.get('version', '')).strip()[:100]),
                    'description': m_escape(str(hint.get('description', '')).strip()[:500])
                }
                # Добавляем фото если есть
                if 'photo' in hint:
                    photo_data = hint['photo']
                    photo_id = photo_data.get('id')
                    photo_url = get_file_url(photo_id) if photo_id else None
                    hint_item['photo'] = {
                        'url': photo_url,
                        'caption': m_escape(str(photo_data.get('caption', '')).strip()[:300])
                    }
                version_hints['hints'].append(hint_item)

        print(f"[select_problem] Rendering subproblems.html for problem_id: {problem_id}")
        return render_template(
            'subproblems.html',
            subproblems=sanitized_subproblems,
            problem_id=safe_problem_id,
            version_hints=version_hints
        )

    # --- Нет подпроблем — показываем мануал ---
    else:
        raw_manual_title = problem_data.get('title', 'Проблема')
        manual_title = m_escape(str(raw_manual_title).strip()[:200])
        session['problem_title'] = manual_title

        # Если выбрана "Другая проблема" или "CISCO" — редиректим
        raw_title = str(raw_manual_title)
        raw_title_lower = raw_title.lower()
        if 'другая проблема' in raw_title_lower:
            session['other_problem_type'] = 'other'
            print(f"[select_problem] Redirecting to other_problem (other) for problem_id: {problem_id}")
            return redirect(url_for('other_problem'))
        if 'cisco' in raw_title_lower:
            session['other_problem_type'] = 'cisco'
            print(f"[select_problem] Redirecting to other_problem (cisco) for problem_id: {problem_id}")
            return redirect(url_for('other_problem'))

        # --- Обрабатываем фото (показываем все шаги, даже без фото) ---
        photo_urls_with_captions = []
        for photo in problem_data.get('photos', []):
            photo_id = photo.get('id')
            url = get_file_url(photo_id) if photo_id else None
            caption = photo.get('caption', '')
            safe_caption = m_escape(str(caption).strip()[:300])
            # Добавляем ВСЕ шаги, даже если фото отсутствует (url = None)
            photo_urls_with_captions.append({'url': url, 'caption': safe_caption})

        # --- Обрабатываем видео если есть ---
        video_data = None
        if 'video' in problem_data and problem_data['video'] is not None:
            video_id = problem_data['video'].get('id')
            if video_id:
                video_url = get_file_url(video_id)
                if video_url:
                    video_data = {
                        'url': video_url,
                        'caption': m_escape(str(problem_data['video'].get('caption', 'Видео-инструкция')).strip()[:300])
                    }

        # --- Экранируем и передаём безопасные данные ---
        safe_manual_data = deep_escape(problem_data)
        safe_photos = deep_escape(photo_urls_with_captions)

        print(f"[select_problem] Rendering manual.html for problem_id: {problem_id}")
        # Фиксируем просмотр инструкции (видео/текст)
        log_manual_open(manual_title, has_video=bool(video_data))
        return render_template(
            'manual.html',
            manual=safe_manual_data,
            manual_title=manual_title,
            photo_urls_with_captions=safe_photos,
            video_data=video_data
        )

@app.route('/show_manual/<string:subproblem_id>')
def show_manual(subproblem_id):
    if 'user_info' not in session or 'problem_id' not in session:
        return redirect(url_for('index'))

    problem_id = session.get('problem_id')

    # --- Проверка формата subproblem_id (только цифра.цифра, например "1.2") ---
    if not re.match(r'^\d\.\d$', subproblem_id):
        flash('Неверный идентификатор подпроблемы.')
        return redirect(url_for('show_problems'))

    # Получаем данные основной проблемы - загружаем актуальные мануалы из JSON
    manuals = load_manuals()
    problem_data = manuals.get(problem_id, {})

    # Проверяем, существует ли указанная подпроблема
    subproblems = problem_data.get('subproblems', {})
    if subproblem_id not in subproblems:
        flash('Выбрана несуществующая подпроблема.')
        return redirect(url_for('show_problems'))

    # Получаем данные подпроблемы
    subproblem_data = subproblems.get(subproblem_id, {})

    # --- Экранируем и валидируем заголовок ---
    raw_manual_title = subproblem_data.get('title', 'Инструкция')
    # Обрезаем лишние символы и экранируем HTML
    manual_title = m_escape(str(raw_manual_title).strip()[:200])  # ограничим длину, защита от XSS
    session['problem_title'] = manual_title

    # Сбрасываем флаги отправки при выборе нового мануала
    session.pop('ticket_sent', None)
    session.pop('solved_sent', None)
    session.modified = True
    session['current_subproblem_id'] = subproblem_id  # Сохраняем для возврата после опроса по видео

    # Проверяем, нужна ли форма для добавления скриншотов
    can_add_screenshots = subproblem_data.get('can_add_screenshots', False)

    # Если это подпроблема с возможностью добавления скриншотов и нет фотографий
    if can_add_screenshots and not subproblem_data.get('photos'):
        session['other_problem_type'] = 'other'
        return render_template('other_problem.html', is_cisco=False)

    photo_urls_with_captions = []
    for photo in subproblem_data.get('photos', []):
        photo_id = photo.get('id')
        url = get_file_url(photo_id) if photo_id else None
        caption = photo.get('caption', '')
        safe_caption = m_escape(str(caption).strip()[:300])
        # Добавляем ВСЕ шаги, даже если фото удалено (url = None)
        photo_urls_with_captions.append({'url': url, 'caption': safe_caption})

    # Получаем видео если есть
    video_data = None
    if 'video' in subproblem_data and subproblem_data['video'] is not None:
        video_id = subproblem_data['video'].get('id')
        if video_id:
            video_url = get_file_url(video_id)
            if video_url:
                video_data = {
                    'url': video_url,
                    'caption': m_escape(str(subproblem_data['video'].get('caption', 'Видео-инструкция')).strip()[:300])
                }

    safe_manual_data = deep_escape(subproblem_data)
    safe_photos = deep_escape(photo_urls_with_captions)
    safe_video = deep_escape(video_data) if video_data else None

    # Фиксируем просмотр инструкции (видео/текст)
    log_manual_open(manual_title, has_video=bool(safe_video))

    return render_template(
        'manual.html',
        manual=safe_manual_data,
        manual_title=manual_title,
        photo_urls_with_captions=safe_photos,
        video_data=safe_video
    )


@app.route('/other_problem', methods=['GET', 'POST'])
@rate_limit(max_requests=10, window=60)  # Security Fix: Add rate limiting to prevent DoS via file uploads
def other_problem():
    if 'user_info' not in session:
        return redirect(url_for('index'))
    other_problem_type = (
        request.args.get('type')
        or request.form.get('problem_type')
        or session.get('other_problem_type', 'other')
    )
    if other_problem_type not in ['other', 'cisco']:
        other_problem_type = 'other'
    session['other_problem_type'] = other_problem_type
    is_cisco = other_problem_type == 'cisco'
    if request.method == 'POST':
        problem_description = request.form.get('problem')

        # Получаем выбранную тематику (если есть)
        topic_info = None
        selected_topic_id = request.form.get('selected_topic_id')
        if selected_topic_id:
            try:
                # Получаем полную информацию о тематике из БД
                topic_data = tm.get_topic_by_id(int(selected_topic_id))
                if topic_data:
                    topic_info = {
                        'topic': topic_data.get('full_topic', 'Неизвестно'),
                        'similarity': request.form.get('selected_topic_similarity', '0')
                    }
            except Exception as e:
                print(f"[other_problem] Ошибка получения тематики: {e}")

        # Security Fix: File upload vulnerability - check size before loading into memory
        screenshots = []
        max_file_size = 10 * 1024 * 1024  # 10 МБ
        allowed_image_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

        for i in range(1, 4):  # screenshot1, screenshot2, screenshot3
            file_key = f'screenshot{i}'
            if file_key in request.files:
                file = request.files[file_key]
                if file and file.filename:
                    # Security Fix: Validate content type before reading
                    if not file.content_type or file.content_type not in allowed_image_types:
                        flash(f'Файл {file.filename} имеет недопустимый тип. Разрешены: JPEG, PNG, GIF, WebP')
                        continue

                    # Security Fix: Check content-length header first (before loading into memory)
                    content_length = request.content_length
                    if content_length and content_length > max_file_size:
                        flash(f'Файл {file.filename} слишком большой. Максимальный размер: 10 МБ')
                        continue

                    # Read file with size limit
                    file.seek(0, os.SEEK_END)
                    file_size = file.tell()
                    file.seek(0)

                    if file_size > max_file_size:
                        flash(f'Файл {file.filename} слишком большой. Максимальный размер: 10 МБ')
                        continue

                    screenshots.append(file)

        video_file = None
        if is_cisco and 'video' in request.files:
            video = request.files.get('video')
            if video and video.filename:
                allowed_video_types = {
                    'video/mp4',
                    'video/quicktime',
                    'video/webm',
                    'video/x-msvideo'
                }
                max_video_size = 50 * 1024 * 1024  # 50 МБ

                if not video.content_type or video.content_type not in allowed_video_types:
                    flash(f'Файл {video.filename} имеет недопустимый тип. Разрешены: MP4, MOV, WEBM, AVI')
                else:
                    video.seek(0, os.SEEK_END)
                    video_size = video.tell()
                    video.seek(0)
                    if video_size > max_video_size:
                        flash(f'Видео {video.filename} слишком большое. Максимальный размер: 50 МБ')
                    else:
                        video_file = video

        if is_cisco:
            target_thread_id = CISCO_TICKETS_THREAD_ID or NEW_TICKETS_THREAD_ID
        else:
            target_thread_id = NEW_TICKETS_THREAD_ID
        send_ticket(problem_description, screenshots, topic_info, video=video_file, thread_id=target_thread_id)
        # Не сбрасываем user_info/workplace — сохраняем авторизацию
        for key in [
            'problem_id',
            'problem_title',
            'current_subproblem_id',
            'other_problem_type',
            'selected_topic_id',
            'selected_topic_name',
            'selected_topic_similarity',
            'next_after_workplace'
        ]:
            session.pop(key, None)
        return render_template('ticket_sent.html')
    return render_template('other_problem.html', is_cisco=is_cisco)

@app.route('/send_final_ticket')
def send_final_ticket():
    try:
        # Проверяем флаг - была ли уже отправлена заявка
        if session.get('ticket_sent'):
            # Заявка уже отправлена, просто показываем страницу
            return render_template('ticket_sent.html')

        # Отправляем заявку только если флаг не установлен
        problem_description = session.get('problem_title', 'Неизвестная проблема')
        send_ticket(problem_description)

        # Явно фиксируем, что текстовый мануал не помог (пользователь эскалировал в заявку)
        # Это нужно, чтобы "Не помогло / Заявки" корректно считалось даже без доп.статуса.
        log_ticket_event(
            event_type='manual_not_helped',
            ticket_number=session.get('current_ticket_number'),
            problem=problem_description
        )

        # Устанавливаем флаг что заявка отправлена
        session['ticket_sent'] = True
        session.modified = True

        return render_template('ticket_sent.html')
    except Exception as e:
        print(f"Ошибка при отправке заявки: {e}")
        return "Произошла ошибка при отправке заявки. Пожалуйста, попробуйте еще раз."

# --- Кнопка «На главную» после решения проблемы ---
@app.route('/finish_solved')
def finish_solved():
    try:
        # Проверяем флаг - было ли уже отправлено уведомление
        if session.get('solved_sent'):
            # Уведомление уже отправлено, редирект на страницу успеха
            return redirect(url_for('show_success'))

        # Отправляем уведомление только если флаг не установлен
        problem_description = session.get('problem_title', 'Неизвестная проблема')
        send_solved_ticket(problem_description)

        # Устанавливаем флаг что уведомление отправлено
        session['solved_sent'] = True
        session.modified = True

        # Редирект на страницу успеха (POST-Redirect-GET pattern)
        return redirect(url_for('show_success'))

    except Exception as e:
        print(f"Ошибка при отправке сообщения: {e}")
        return "Произошла ошибка, но сессия сохранена."


@app.route('/success')
def show_success():
    """Страница успешного решения проблемы"""
    return render_template('success.html')


# --- Обновлённый маршрут finish_unsolved с логированием ---
@app.route('/finish_unsolved')
def finish_unsolved():
    # Security: require user_info in session
    if 'user_info' not in session:
        return redirect(url_for('index'))

    try:
        # Security: only use session data, not query params (prevent injection)
        problem_description = session.get('problem_title', 'Неизвестная проблема')
        # Sanitize before sending
        problem_description = m_escape(str(problem_description)[:500])
        send_ticket(problem_description)
        log_ticket_event(
            event_type='manual_not_helped',
            ticket_number=session.get('current_ticket_number'),
            problem=problem_description
        )
        return render_template('ticket_sent.html')
    except Exception as e:
        print("[finish_unsolved] Error sending ticket")
        traceback.print_exc()
        return render_template('ticket_sent.html')

@app.route('/go_home')
def go_home():
    # Сбрасываем флаги отправки при возврате на главную
    session.pop('ticket_sent', None)
    session.pop('solved_sent', None)
    session.modified = True

    # Security: don't log session content
    if 'user_info' in session:
        return redirect(url_for('show_problems'))
    else:
        return redirect(url_for('index'))

# ============================================
# API ДЛЯ ПОИСКА ТЕМАТИК
# ============================================

@app.route('/api/get_all_topics', methods=['GET'])
@csrf.exempt  # Exempted but protected by rate limiting
@rate_limit(max_requests=30, window=60)  # Security Fix: Add rate limiting
def get_all_topics_api():
    """API для получения всех тематик"""
    try:
        # Получаем все тематики без жесткого лимита
        topics = tm.get_all_topics()

        formatted_results = []
        for topic in topics:
            formatted_results.append({
                'id': topic['id'],
                'topic': topic['full_topic'],
                'channel': topic['channel'],
                'similarity': 100,  # Для всех тематик = 100%
                'sr1': topic.get('sr1', ''),
                'sr2': topic.get('sr2', ''),
                'sr3': topic.get('sr3', ''),
                'sr4': topic.get('sr4', '')
            })

        return jsonify({
            'success': True,
            'count': len(formatted_results),
            'results': formatted_results
        })

    except Exception as e:
        print(f"[get_all_topics_api] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Внутренняя ошибка сервера'
        })

@app.route('/api/admin/check-password', methods=['POST'])
@rate_limit(max_requests=10, window=60)
def api_admin_check_password():
    """API для быстрой проверки пароля админа из модального окна"""
    try:
        data = request.get_json()
        password = data.get('password', '')
        section = data.get('section', '')

        # В тестовом режиме принимаем упрощенные пароли (как в /admin/login)
        TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
        if TEST_MODE and password in ['admin', '123', 'test']:
            session['admin_logged_in'] = True
            session['admin_username'] = os.getenv('ADMIN_USERNAME', 'admin')
            session['admin_role'] = ROLE_SUPER_ADMIN
            session['admin_token'] = AdminAuth.generate_session_token()
            return jsonify({'success': True})

        # Проверяем пароль через AdminAuth с username из .env
        admin_username = os.getenv('ADMIN_USERNAME', 'admin')
        admin_data = AdminAuth.verify_admin(admin_username, password)

        if admin_data:
            # Успешная авторизация - сохраняем в сессию
            session['admin_user'] = admin_data
            session['admin_logged_in'] = True
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'Неверный пароль'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/search_topic', methods=['POST'])
@rate_limit(max_requests=30, window=60)  # Security Fix: Add rate limiting
def search_topic_api():
    """API для поиска тематики по описанию проблемы"""
    try:
        # Security Fix: Validate Content-Type header
        if request.content_type != 'application/json':
            return jsonify({
                'success': False,
                'error': 'Content-Type must be application/json'
            }), 400

        data = request.json
        query = data.get('query', '').strip()
        channel = data.get('channel', '').strip()  # Получаем выбранный канал

        # Security Fix: Validate maximum query length
        if len(query) > 500:
            return jsonify({
                'success': False,
                'error': 'Запрос слишком длинный (максимум 500 символов)'
            }), 400
        if len(channel) > 200:
            return jsonify({
                'success': False,
                'error': 'Название канала слишком длинное'
            }), 400

        # Если query пустой, но канал выбран - возвращаем все тематики канала
        if not query and channel:
            topics = tm.get_topics_by_channel(channel)
            formatted_results = []
            for r in topics:
                formatted_results.append({
                    'id': r['id'],
                    'topic': r['full_topic'],
                    'channel': r['channel'],
                    'similarity': 100,  # Все тематики канала = 100%
                    'sr1': r.get('sr1', ''),
                    'sr2': r.get('sr2', ''),
                    'sr3': r.get('sr3', ''),
                    'sr4': r.get('sr4', '')
                })
            log_topic_search(query_text='', channel=channel, results_count=len(formatted_results))
            return jsonify({
                'success': True,
                'query': '',
                'channel': channel,
                'count': len(formatted_results),
                'results': formatted_results
            })

        if not query or len(query) < 3:
            return jsonify({
                'success': False,
                'error': 'Запрос слишком короткий (минимум 3 символа)'
            })

        # Поиск тематик
        results = tm.search(
            query=query,
            limit=300,  # Показываем больше результатов в live-поиске
            threshold=0.2,  # Низкий порог для большего кол-ва результатов
            use_cache=True
        )

        # Фильтруем результаты по каналу если он выбран
        if channel:
            # Точная фильтрация: только тематики из выбранного канала
            filtered_results = []
            for r in results:
                # Точное совпадение названия канала
                if r['channel'].strip() == channel.strip():
                    filtered_results.append(r)

            results = filtered_results

        # Форматируем результаты
        formatted_results = []
        for r in results:
            formatted_results.append({
                'id': r['id'],
                'topic': r['full_topic'],
                'channel': r['channel'],
                'similarity': round(r['similarity'] * 100, 1),  # В процентах
                'sr1': r.get('sr1', ''),
                'sr2': r.get('sr2', ''),
                'sr3': r.get('sr3', ''),
                'sr4': r.get('sr4', '')
            })

        log_topic_search(query_text=query, channel=channel, results_count=len(formatted_results))
        return jsonify({
            'success': True,
            'query': query,
            'channel': channel,
            'count': len(formatted_results),
            'results': formatted_results
        })

    except Exception as e:
        print(f"[search_topic_api] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Внутренняя ошибка сервера'
        })

# Обработчик для получения photo file_id (для админов)
@bot.message_handler(content_types=['photo'])
def handle_photo_upload(message):
    """Получает photo file_id для добавления в мануалы"""
    try:
        # Берем самую большую версию фото
        photo_id = message.photo[-1].file_id
        file_size_mb = message.photo[-1].file_size / (1024 * 1024) if message.photo[-1].file_size else 0

        response_text = (
            f"📷 <b>Photo File ID получен!</b>\n\n"
            f"<code>{photo_id}</code>\n\n"
            f"📊 Размер: {file_size_mb:.2f} MB\n\n"
            f"Скопируйте file_id выше и добавьте в manuals_data.json"
        )

        bot.reply_to(message, response_text, parse_mode='HTML')
        print(f"✅ Photo file_id: {photo_id} (Size: {file_size_mb:.2f}MB)")

    except Exception as e:
        print(f"❌ Ошибка при обработке фото: {e}")
        traceback.print_exc()
        bot.reply_to(message, "❌ Ошибка при получении file_id фото")

# Обработчик для получения video file_id (для админов)
@bot.message_handler(content_types=['video'])
def handle_video_upload(message):
    """Получает video file_id для добавления в мануалы"""
    try:
        video_id = message.video.file_id
        file_size_mb = message.video.file_size / (1024 * 1024) if message.video.file_size else 0
        duration = message.video.duration if message.video.duration else 0

        response_text = (
            f"📹 <b>Video File ID получен!</b>\n\n"
            f"<code>{video_id}</code>\n\n"
            f"📊 Размер: {file_size_mb:.2f} MB\n"
            f"⏱ Длительность: {duration} сек\n\n"
            f"Скопируйте file_id выше и добавьте в manuals_data.json"
        )

        bot.reply_to(message, response_text, parse_mode='HTML')
        print(f"✅ Video file_id: {video_id} (Size: {file_size_mb:.2f}MB, Duration: {duration}s)")

    except Exception as e:
        print(f"❌ Ошибка при обработке видео: {e}")
        traceback.print_exc()
        bot.reply_to(message, "❌ Ошибка при получении file_id видео")

@bot.message_handler(func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_channel_messages(message):
    try:
        thread_id = getattr(message, 'message_thread_id', None)
        if thread_id:
            print(f"[thread] message_thread_id={thread_id}")
        print(f"Получено сообщение: {message.text} от {message.from_user.id}")
        if message.reply_to_message:
            print(f"Это ответ на сообщение ID {message.reply_to_message.message_id}")
        else:
            print("❌ Сообщение не является ответом")

        if message.reply_to_message and message.from_user.id in SUPPORT_STAFF_IDS:
            text = message.text.lower()
            original_message_id = message.reply_to_message.message_id

            if "в работе" in text or "в процессе" in text or "решена" in text or "готово" in text:
                print("➡ Пересылаем в IN_PROGRESS_THREAD")
                bot.copy_message(
                    chat_id=TECH_SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=original_message_id,
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )
                # Security Fix: Limit text length and sanitize
                safe_text = html_escape(message.text[:1000])  # Limit to 1000 chars
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"💬 Статус по заявки на помощь: {safe_text}",
                    message_thread_id=IN_PROGRESS_THREAD_ID,
                    parse_mode='HTML'
                )
        else:
            print("❌ Не прошли проверки (нет reply_to_message или ID не в SUPPORT_STAFF_IDS)")
    except Exception as e:
        print(f"Ошибка при обработке сообщения в канале: {e}")

# ============================================
# ТРЕНАЖЕР ОПЕРАТОРОВ
# ============================================

@app.route('/trainer')
def trainer_menu():
    """Главная страница тренажера с уровнями"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    user_id = session['user_info'].get('username', 'anonymous')
    levels = trainer_mgr.get_all_levels()
    progress = trainer_mgr.get_user_progress(user_id)

    return render_template('trainer_menu.html', levels=levels, progress=progress)


@app.route('/trainer/level/<level_code>')
def trainer_level(level_code):
    """Список сценариев уровня"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    user_id = session['user_info'].get('username', 'anonymous')
    level = trainer_mgr.get_level_by_code(level_code)

    if not level:
        flash('Уровень не найден')
        return redirect(url_for('trainer_menu'))

    # Проверяем доступ к уровню
    if not trainer_mgr.check_level_unlocked(user_id, level_code):
        flash('Этот уровень ещё заблокирован')
        return redirect(url_for('trainer_menu'))

    # Получаем фильтр по категории
    category_id = request.args.get('category', type=int)

    scenarios = trainer_mgr.get_scenarios_by_level(level_code, category_id)
    categories = trainer_mgr.get_all_categories()

    # Получаем результаты пользователя для каждого сценария
    user_results = {}
    for scenario in scenarios:
        result = trainer_mgr.get_scenario_user_result(user_id, scenario['id'])
        if result:
            user_results[scenario['id']] = result

    # Считаем статистику уровня
    completed_count = len(user_results)
    total_count = len(scenarios)
    avg_percent = 0
    if user_results:
        avg_percent = round(sum(r['percent'] for r in user_results.values()) / len(user_results), 1)

    return render_template('trainer_scenarios.html',
                         level=level,
                         scenarios=scenarios,
                         categories=categories,
                         current_category=category_id,
                         user_results=user_results,
                         completed_count=completed_count,
                         total_count=total_count,
                         avg_percent=avg_percent)


@app.route('/trainer/play/<int:scenario_id>')
def trainer_play(scenario_id):
    """Страница прохождения сценария"""
    # Check for preview mode
    preview_mode = request.args.get('preview') == '1'

    # In preview mode, admin must be logged in
    if preview_mode:
        if not session.get('admin_logged_in'):
            flash('Доступ запрещён')
            return redirect(url_for('admin_login'))
    else:
        # Regular mode - user must be authenticated
        if 'user_info' not in session or not session.get('authenticated'):
            return redirect(url_for('user_login'))

    user_id = session.get('user_info', {}).get('username', 'admin_preview') if not preview_mode else 'admin_preview'
    scenario = trainer_mgr.get_scenario(scenario_id)

    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('trainer_menu') if not preview_mode else url_for('admin_trainer'))

    # Check level access (skip in preview mode)
    if not preview_mode and not trainer_mgr.check_level_unlocked(user_id, scenario['level_code']):
        flash('Этот уровень ещё заблокирован')
        return redirect(url_for('trainer_menu'))

    total_steps = trainer_mgr.get_steps_count(scenario_id)

    if total_steps == 0:
        flash('В этом сценарии пока нет шагов')
        return redirect(url_for('admin_trainer_edit', scenario_id=scenario_id) if preview_mode else url_for('trainer_level', level_code=scenario['level_code']))

    return render_template('trainer_play.html',
                         scenario=scenario,
                         total_steps=total_steps,
                         preview_mode=preview_mode)


@app.route('/api/trainer/step/<int:scenario_id>/<int:step_num>')
@rate_limit(max_requests=120, window=60)
def trainer_get_step(scenario_id, step_num):
    """API: получить шаг сценария"""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    step = trainer_mgr.get_step_by_num(scenario_id, step_num)

    if not step:
        return jsonify({'success': False, 'error': 'Шаг не найден'})

    # Получаем информацию о сценарии для таймера и карточки клиента
    scenario = trainer_mgr.get_scenario(scenario_id)

    # Не отправляем информацию о правильности ответов
    safe_answers = []
    for answer in step.get('answers', []):
        safe_answers.append({
            'id': answer['id'],
            'answer_text': answer['answer_text'],
            'order_num': answer['order_num']
        })

    # Парсим карточку клиента из JSON
    client_info = None
    if scenario and scenario.get('client_info_json'):
        try:
            client_info = json.loads(scenario['client_info_json'])
        except:
            pass

    return jsonify({
        'success': True,
        'step': {
            'id': step['id'],
            'step_num': step['step_num'],
            'client_message': step['client_message'],
            'client_avatar': step['client_avatar'],
            'client_name': step['client_name'],
            'initial_mood': step.get('initial_mood', 'neutral'),
            'answers': safe_answers
        },
        'timer_seconds': scenario.get('timer_seconds', 15) if scenario else 15,
        'initial_loyalty': scenario.get('initial_loyalty', 100) if scenario else 100,
        'client_info': client_info
    })


@app.route('/api/trainer/answer', methods=['POST'])
@rate_limit(max_requests=60, window=60)
def trainer_submit_answer():
    """API: отправить ответ"""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    try:
        data = request.get_json()
        scenario_id = data.get('scenario_id')
        step_num = data.get('step_num')
        answer_id = data.get('answer_id')
        response_time_ms = data.get('response_time_ms', 0)
        is_timeout = data.get('is_timeout', False)
        current_loyalty = data.get('current_loyalty', 100)

        if not all([scenario_id, step_num, answer_id]):
            return jsonify({'success': False, 'error': 'Неполные данные'})

        # Получаем шаг и ответы
        step = trainer_mgr.get_step_by_num(scenario_id, step_num)
        if not step:
            return jsonify({'success': False, 'error': 'Шаг не найден'})

        # Находим выбранный ответ
        selected_answer = None
        for answer in step.get('answers', []):
            if answer['id'] == answer_id:
                selected_answer = answer
                break

        if not selected_answer:
            return jsonify({'success': False, 'error': 'Ответ не найден'})

        # Вычисляем влияние на лояльность
        mood_impact = selected_answer.get('mood_impact', 0)
        if is_timeout:
            mood_impact = -20  # Штраф за таймаут

        new_loyalty = max(0, min(200, current_loyalty + mood_impact))
        is_game_over = new_loyalty <= 0

        # Определяем новое настроение на основе лояльности
        if new_loyalty >= 80:
            new_mood = 'delight' if new_loyalty >= 120 else 'satisfaction'
        elif new_loyalty >= 50:
            new_mood = 'neutral'
        elif new_loyalty >= 25:
            new_mood = 'irritation'
        else:
            new_mood = 'anger'

        return jsonify({
            'success': True,
            'is_correct': bool(selected_answer['is_correct']),
            'is_partial': bool(selected_answer['is_partial']),
            'points_earned': selected_answer['points'],
            'feedback': selected_answer['feedback'] or '',
            'mood_impact': mood_impact,
            'new_mood': new_mood,
            'new_loyalty': new_loyalty,
            'knowledge_link': selected_answer.get('knowledge_link'),
            'is_game_over': is_game_over
        })

    except Exception as e:
        print(f"[trainer_submit_answer] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500


@app.route('/api/trainer/complete', methods=['POST'])
@rate_limit(max_requests=30, window=60)
def trainer_complete():
    """API: завершить сценарий"""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    try:
        data = request.get_json()
        scenario_id = data.get('scenario_id')
        score = data.get('score', 0)
        max_score = data.get('max_score', 100)
        answers = data.get('answers', [])
        final_loyalty = data.get('final_loyalty')
        is_game_over = data.get('is_game_over', False)
        timeout_count = data.get('timeout_count', 0)
        selected_topic_id = data.get('selected_topic_id')
        selected_topic_name = data.get('selected_topic_name')

        if not scenario_id:
            return jsonify({'success': False, 'error': 'Не указан сценарий'})

        user_id = session['user_info'].get('username', 'anonymous')

        # Сохраняем результат с новыми полями геймификации
        result = trainer_mgr.save_result(
            user_id, scenario_id, score, max_score, answers,
            final_loyalty=final_loyalty,
            is_game_over=is_game_over,
            timeout_count=timeout_count,
            selected_topic_id=selected_topic_id,
            selected_topic_name=selected_topic_name
        )

        return jsonify({
            'success': True,
            'result_id': result['id'],
            'percent': result['percent'],
            'grade': result['grade'],
            'is_game_over': is_game_over
        })

    except Exception as e:
        print(f"[trainer_complete] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500


@app.route('/trainer/results/<int:result_id>')
def trainer_results(result_id):
    """Страница результатов прохождения"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    result = trainer_mgr.get_result_by_id(result_id)

    if not result:
        flash('Результат не найден')
        return redirect(url_for('trainer_menu'))

    # Проверяем что это результат текущего пользователя
    user_id = session['user_info'].get('username', 'anonymous')
    if result['user_id'] != user_id:
        flash('Доступ запрещен')
        return redirect(url_for('trainer_menu'))

    grade_info = trainer_mgr.get_grade_info(result['grade'])

    # Получаем детализацию ответов
    answers_detail = []
    if result.get('answers'):
        for ans in result['answers']:
            step = trainer_mgr.get_step_by_num(result['scenario_id'], ans['step_num'])
            if step:
                for answer in step.get('answers', []):
                    if answer['id'] == ans['answer_id']:
                        answers_detail.append({
                            'step_num': ans['step_num'],
                            'answer_text': answer['answer_text'],
                            'points': ans['points'],
                            'is_correct': ans['is_correct'],
                            'is_partial': answer.get('is_partial', False),
                            'is_timeout': ans.get('is_timeout', False),
                            'mood_impact': ans.get('mood_impact', 0),
                            'knowledge_link': ans.get('knowledge_link') or answer.get('knowledge_link'),
                            'feedback': answer.get('feedback', '')
                        })
                        break

    # Находим следующий сценарий
    scenarios = trainer_mgr.get_scenarios_by_level(result['level_code'])
    next_scenario = None
    found_current = False
    for s in scenarios:
        if found_current:
            next_scenario = s
            break
        if s['id'] == result['scenario_id']:
            found_current = True

    return render_template('trainer_results.html',
                         result=result,
                         grade_info=grade_info,
                         answers_detail=answers_detail,
                         next_scenario=next_scenario)


# ============================================
# АДМИН-ПАНЕЛЬ ТРЕНАЖЕРА
# ============================================

@app.route('/admin/trainer')
@AdminAuth.login_required
def admin_trainer():
    """Админка: список сценариев тренажера"""
    stats = trainer_mgr.get_statistics()
    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()
    tags = trainer_mgr.get_all_tags()

    # Фильтры
    level_code = request.args.get('level')
    category_id = request.args.get('category', type=int)
    tag_id = request.args.get('tag', type=int)

    scenarios = trainer_mgr.get_all_scenarios(include_inactive=True)

    # Применяем фильтры
    if level_code:
        scenarios = [s for s in scenarios if s['level_code'] == level_code]
    if category_id:
        scenarios = [s for s in scenarios if s['category_id'] == category_id]

    # Добавляем количество шагов и теги
    for scenario in scenarios:
        scenario['steps_count'] = trainer_mgr.get_steps_count(scenario['id'])
        scenario['tags'] = trainer_mgr.get_scenario_tags(scenario['id'])

    # Фильтр по тегу (после получения тегов)
    if tag_id:
        scenarios = [s for s in scenarios if any(t['id'] == tag_id for t in s['tags'])]

    return render_template('admin_trainer.html',
                         stats=stats,
                         levels=levels,
                         categories=categories,
                         tags=tags,
                         scenarios=scenarios,
                         current_level=level_code,
                         current_category=category_id,
                         current_tag=tag_id)


@app.route('/admin/trainer/scenario/create', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_trainer_create():
    """Создание нового сценария"""
    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()

    if request.method == 'POST':
        data = {
            'level_id': request.form.get('level_id', type=int),
            'category_id': request.form.get('category_id', type=int) or None,
            'title': request.form.get('title', '').strip(),
            'description': request.form.get('description', '').strip(),
            'estimated_time': request.form.get('estimated_time', 5, type=int),
            'total_points': request.form.get('total_points', 100, type=int),
            'is_active': 1 if request.form.get('is_active') else 0,
            'order_num': request.form.get('order_num', 0, type=int)
        }

        if not data['title']:
            flash('Название обязательно')
            tags = trainer_mgr.get_all_tags()
            return render_template('admin_trainer_edit.html', scenario=None, levels=levels, categories=categories, steps=[], tags=tags, scenario_tag_ids=[])

        result = trainer_mgr.create_scenario(data)

        if result['success']:
            # Логируем создание (берём AD учётку пользователя)
            user_info = session.get('user_info', {})
            trainer_mgr.log_action(
                user_id=user_info.get('username') or user_info.get('name', 'admin'),
                action='create',
                entity_type='scenario',
                entity_id=result['id'],
                entity_name=data['title'],
                changes=data,
                ip_address=request.remote_addr
            )
            flash('Сценарий успешно создан!')
            return redirect(url_for('admin_trainer_edit', scenario_id=result['id']))
        else:
            flash(f'Ошибка: {result.get("error")}')

    tags = trainer_mgr.get_all_tags()
    return render_template('admin_trainer_edit.html', scenario=None, levels=levels, categories=categories, steps=[], tags=tags, scenario_tag_ids=[])


@app.route('/admin/trainer/scenario/<int:scenario_id>/edit', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_trainer_edit(scenario_id):
    """Редактирование сценария"""
    scenario = trainer_mgr.get_scenario(scenario_id)

    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('admin_trainer'))

    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()

    if request.method == 'POST':
        # Собираем карточку клиента в JSON
        client_info = {}
        if request.form.get('client_name'):
            client_info['name'] = request.form.get('client_name', '').strip()
        if request.form.get('client_tariff'):
            client_info['tariff'] = request.form.get('client_tariff', '').strip()
        if request.form.get('client_balance'):
            client_info['balance'] = request.form.get('client_balance', '').strip()

        # Парсим дополнительные поля JSON
        client_extra = request.form.get('client_extra', '').strip()
        if client_extra:
            try:
                extra_data = json.loads(client_extra)
                client_info.update(extra_data)
            except:
                pass

        client_info_json = json.dumps(client_info, ensure_ascii=False) if client_info else None

        # Обновляем основную информацию сценария
        data = {
            'level_id': request.form.get('level_id', type=int),
            'category_id': request.form.get('category_id', type=int) or None,
            'title': request.form.get('title', '').strip(),
            'description': request.form.get('description', '').strip(),
            'estimated_time': request.form.get('estimated_time', 5, type=int),
            'total_points': request.form.get('total_points', 100, type=int),
            'is_active': 1 if request.form.get('is_active') else 0,
            'order_num': request.form.get('order_num', 0, type=int),
            'timer_seconds': request.form.get('timer_seconds', 15, type=int),
            'initial_loyalty': request.form.get('initial_loyalty', 100, type=int),
            'client_info_json': client_info_json
        }

        result = trainer_mgr.update_scenario(scenario_id, data)

        if result['success']:
            # Сохраняем теги
            tag_ids = request.form.getlist('tags')
            tag_ids = [int(t) for t in tag_ids if t.isdigit()]
            trainer_mgr.set_scenario_tags(scenario_id, tag_ids)

            # Логируем изменение (берём AD учётку пользователя)
            user_info = session.get('user_info', {})
            trainer_mgr.log_action(
                user_id=user_info.get('username') or user_info.get('name', 'admin'),
                action='edit',
                entity_type='scenario',
                entity_id=scenario_id,
                entity_name=data['title'],
                changes=data,
                ip_address=request.remote_addr
            )

            # Обновляем шаги и ответы
            for key in request.form:
                # Обновление шагов
                if key.startswith('step_') and key.endswith('_message'):
                    step_id = int(key.split('_')[1])
                    trainer_mgr.update_step(step_id, {
                        'client_message': request.form.get(key, '').strip(),
                        'client_avatar': request.form.get(f'step_{step_id}_avatar', ''),
                        'client_name': request.form.get(f'step_{step_id}_name', 'Клиент'),
                        'initial_mood': request.form.get(f'step_{step_id}_mood', 'neutral')
                    })

                # Обновление ответов
                if key.startswith('answer_') and key.endswith('_text'):
                    answer_id = int(key.split('_')[1])
                    trainer_mgr.update_answer(answer_id, {
                        'answer_text': request.form.get(key, '').strip(),
                        'is_correct': 1 if request.form.get(f'answer_{answer_id}_correct') else 0,
                        'is_partial': 1 if request.form.get(f'answer_{answer_id}_partial') else 0,
                        'points': request.form.get(f'answer_{answer_id}_points', 0, type=int),
                        'feedback': request.form.get(f'answer_{answer_id}_feedback', '').strip(),
                        'mood_impact': request.form.get(f'answer_{answer_id}_mood_impact', 0, type=int),
                        'knowledge_link': request.form.get(f'answer_{answer_id}_knowledge_link', '').strip() or None
                    })

            flash('Сценарий успешно обновлен!')
        else:
            flash(f'Ошибка: {result.get("error")}')

        # Перезагружаем данные
        scenario = trainer_mgr.get_scenario(scenario_id)

    # Получаем шаги с ответами
    steps = trainer_mgr.get_scenario_steps(scenario_id)
    for step in steps:
        step['answers'] = trainer_mgr.get_step_answers(step['id'])

    # Парсим карточку клиента
    client_info = None
    client_extra = None
    if scenario.get('client_info_json'):
        try:
            client_info = json.loads(scenario['client_info_json'])
            # Отделяем стандартные поля от дополнительных
            standard_fields = ['name', 'tariff', 'balance']
            extra_fields = {k: v for k, v in client_info.items() if k not in standard_fields}
            if extra_fields:
                client_extra = json.dumps(extra_fields, ensure_ascii=False, indent=2)
        except:
            pass

    # Получаем теги
    tags = trainer_mgr.get_all_tags()
    scenario_tags = trainer_mgr.get_scenario_tags(scenario_id)
    scenario_tag_ids = [t['id'] for t in scenario_tags]

    return render_template('admin_trainer_edit.html',
                         scenario=scenario,
                         levels=levels,
                         categories=categories,
                         steps=steps,
                         client_info=client_info,
                         client_extra=client_extra,
                         tags=tags,
                         scenario_tag_ids=scenario_tag_ids)


@app.route('/admin/trainer/scenario/<int:scenario_id>/delete', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_delete(scenario_id):
    """Удаление сценария"""
    # Получаем информацию о сценарии перед удалением
    scenario = trainer_mgr.get_scenario(scenario_id)
    scenario_title = scenario['title'] if scenario else f"ID {scenario_id}"

    result = trainer_mgr.delete_scenario(scenario_id)

    if result['success']:
        # Логируем удаление (берём AD учётку пользователя)
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='delete',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario_title,
            ip_address=request.remote_addr
        )
        flash('Сценарий удален')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return redirect(url_for('admin_trainer'))


@app.route('/admin/trainer/scenario/<int:scenario_id>/step/create', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_create_step(scenario_id):
    """Создание шага сценария"""
    data = {
        'client_message': request.form.get('client_message', 'Сообщение клиента'),
        'client_avatar': request.form.get('client_avatar', '👤'),
        'client_name': request.form.get('client_name', 'Клиент')
    }

    result = trainer_mgr.create_step(scenario_id, data)

    if result['success']:
        flash('Шаг добавлен')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return redirect(url_for('admin_trainer_edit', scenario_id=scenario_id))


@app.route('/admin/trainer/step/<int:step_id>/delete', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_delete_step(step_id):
    """Удаление шага"""
    result = trainer_mgr.delete_step(step_id)

    if result['success']:
        flash('Шаг удален')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return redirect(request.referrer or url_for('admin_trainer'))


@app.route('/admin/trainer/step/<int:step_id>/answer/create', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_create_answer(step_id):
    """Создание варианта ответа"""
    data = {
        'answer_text': request.form.get('answer_text', 'Новый ответ'),
        'is_correct': 0,
        'is_partial': 0,
        'points': request.form.get('points', 0, type=int),
        'feedback': ''
    }

    result = trainer_mgr.create_answer(step_id, data)

    if result['success']:
        flash('Ответ добавлен')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return redirect(request.referrer or url_for('admin_trainer'))


@app.route('/admin/trainer/answer/<int:answer_id>/delete', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_delete_answer(answer_id):
    """Удаление варианта ответа"""
    result = trainer_mgr.delete_answer(answer_id)

    if result['success']:
        flash('Ответ удален')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return redirect(request.referrer or url_for('admin_trainer'))


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual')
@AdminAuth.login_required
def admin_trainer_visual(scenario_id):
    """Визуальный редактор сценария (No-Code)"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('admin_trainer'))

    # Получаем шаги с ответами для инициализации визуального редактора
    steps = trainer_mgr.get_scenario_steps(scenario_id)
    for step in steps:
        step['answers'] = trainer_mgr.get_step_answers(step['id'])

    return render_template('admin_trainer_visual.html',
                         scenario=scenario,
                         steps=steps)


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual/save', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_visual_save(scenario_id):
    """Сохранение визуальной структуры сценария"""
    import json

    scenario = trainer_mgr.get_scenario(scenario_id)
    if not scenario:
        return jsonify({'success': False, 'error': 'Сценарий не найден'})

    try:
        data = request.get_json()
        nodes = data.get('nodes', [])
        connections = data.get('connections', [])

        # Очищаем существующие шаги
        for step in trainer_mgr.get_scenario_steps(scenario_id):
            trainer_mgr.delete_step(step['id'])

        # Создаем словарь для маппинга временных ID узлов к реальным ID шагов
        node_to_step = {}

        # Находим все узлы типа "client" (реплики клиента) - это будут шаги
        client_nodes = [n for n in nodes if n.get('type') == 'client']

        # Сортируем узлы по позиции Y для определения порядка
        client_nodes.sort(key=lambda n: n.get('y', 0))

        for idx, node in enumerate(client_nodes):
            step_data = {
                'client_message': node.get('label', 'Сообщение клиента'),
                'client_avatar': '👤',
                'client_name': node.get('clientName', 'Клиент'),
                'initial_mood': node.get('mood', 'neutral'),
                'step_number': idx + 1
            }

            result = trainer_mgr.create_step(scenario_id, step_data)
            if result['success']:
                step_id = result['step_id']
                node_to_step[node['id']] = step_id

                # Создаём ответы из поля answers узла client
                node_answers = node.get('answers', [])
                for answer in node_answers:
                    answer_data = {
                        'answer_text': answer.get('text', 'Ответ оператора'),
                        'is_correct': 1 if answer.get('isCorrect', False) else 0,
                        'is_partial': 1 if answer.get('isPartial', False) else 0,
                        'points': answer.get('points', 0),
                        'feedback': answer.get('feedback', ''),
                        'mood_impact': answer.get('moodImpact', 0),
                        'knowledge_link': answer.get('knowledgeLink', '')
                    }
                    trainer_mgr.create_answer(step_id, answer_data)

        # Также обрабатываем отдельные узлы типа "answer" (для обратной совместимости)
        answer_nodes = [n for n in nodes if n.get('type') == 'answer']

        for answer_node in answer_nodes:
            # Находим связь от клиентского узла к этому ответу
            parent_connection = next(
                (c for c in connections if c.get('toId') == answer_node['id']),
                None
            )

            if parent_connection:
                parent_node_id = parent_connection.get('fromId')
                step_id = node_to_step.get(parent_node_id)

                if step_id:
                    is_correct = answer_node.get('isCorrect', False)
                    mood_impact = answer_node.get('moodImpact', 0)

                    answer_data = {
                        'answer_text': answer_node.get('label', 'Ответ оператора'),
                        'is_correct': 1 if is_correct else 0,
                        'is_partial': 0,
                        'points': 10 if is_correct else 0,
                        'feedback': answer_node.get('feedback', ''),
                        'mood_impact': mood_impact,
                        'knowledge_link': answer_node.get('knowledgeLink', '')
                    }

                    trainer_mgr.create_answer(step_id, answer_data)

        # Сохраняем визуальную структуру для последующего восстановления
        visual_data = {
            'nodes': nodes,
            'connections': connections
        }

        # Сохраняем визуальные данные в отдельное поле сценария
        cursor = trainer_mgr.conn.cursor()

        # Проверяем существует ли колонка visual_data
        cursor.execute("PRAGMA table_info(trainer_scenarios)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'visual_data' not in columns:
            cursor.execute("ALTER TABLE trainer_scenarios ADD COLUMN visual_data TEXT")
            trainer_mgr.conn.commit()

        cursor.execute(
            "UPDATE trainer_scenarios SET visual_data = ? WHERE id = ?",
            (json.dumps(visual_data, ensure_ascii=False), scenario_id)
        )
        trainer_mgr.conn.commit()

        # Логируем изменение через визуальный редактор (берём AD учётку)
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='edit',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario['title'],
            changes={'source': 'visual_editor', 'steps_count': len(client_nodes)},
            ip_address=request.remote_addr
        )

        return jsonify({'success': True, 'message': 'Сценарий сохранен'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual/load')
@AdminAuth.login_required
def admin_trainer_visual_load(scenario_id):
    """Загрузка визуальной структуры сценария с синхронизацией из БД"""
    import json

    scenario = trainer_mgr.get_scenario(scenario_id)
    if not scenario:
        return jsonify({'success': False, 'error': 'Сценарий не найден'})

    try:
        # Получаем сохраненные позиции узлов (если есть)
        cursor = trainer_mgr.conn.cursor()
        cursor.execute("PRAGMA table_info(trainer_scenarios)")
        columns = [col[1] for col in cursor.fetchall()]

        saved_positions = {}  # id узла -> {x, y}
        saved_connections = []

        if 'visual_data' in columns:
            cursor.execute("SELECT visual_data FROM trainer_scenarios WHERE id = ?", (scenario_id,))
            row = cursor.fetchone()
            if row and row[0]:
                visual_data = json.loads(row[0])
                # Сохраняем только позиции узлов
                for node in visual_data.get('nodes', []):
                    saved_positions[node.get('id')] = {'x': node.get('x', 200), 'y': node.get('y', 100)}
                saved_connections = visual_data.get('connections', [])

        # ВСЕГДА генерируем узлы из актуальных данных БД
        steps = trainer_mgr.get_scenario_steps(scenario_id)
        nodes = []
        connections = []

        y_offset = 100
        for step in steps:
            step_id = f"step_{step['id']}"

            # Используем сохраненную позицию или дефолтную
            pos = saved_positions.get(step_id, {'x': 200, 'y': y_offset})

            # Узел реплики клиента с полными данными
            nodes.append({
                'id': step_id,
                'type': 'client',
                'x': pos['x'],
                'y': pos['y'],
                'label': step.get('client_message', ''),
                'mood': step.get('initial_mood', 'neutral'),
                'stepId': step['id'],
                'stepNum': step.get('step_num', 1),
                'clientName': step.get('client_name', 'Клиент'),
                'answers': []  # Будет заполнено ниже
            })

            # Узлы ответов
            answers = trainer_mgr.get_step_answers(step['id'])
            answer_x = pos['x'] + 300
            answer_y_offset = 0
            node_answers = []

            for answer in answers:
                answer_id = f"answer_{answer['id']}"
                ans_pos = saved_positions.get(answer_id, {'x': answer_x, 'y': pos['y'] + answer_y_offset})

                # Добавляем ответ в список ответов узла клиента
                node_answers.append({
                    'id': answer['id'],
                    'text': answer.get('answer_text', ''),
                    'isCorrect': bool(answer.get('is_correct', 0)),
                    'isPartial': bool(answer.get('is_partial', 0)),
                    'points': answer.get('points', 0),
                    'moodImpact': answer.get('mood_impact', 0)
                })

                answer_y_offset += 80

            # Обновляем ответы в узле клиента
            nodes[-1]['answers'] = node_answers

            y_offset += 200

        return jsonify({
            'success': True,
            'nodes': nodes,
            'connections': saved_connections if saved_connections else []
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/trainer/stats')
@AdminAuth.login_required
def admin_trainer_stats():
    """Статистика тренажера"""
    stats = trainer_mgr.get_statistics()
    return render_template('admin_trainer_stats.html', stats=stats)


# ============================================
# ТЕГИ СЦЕНАРИЕВ
# ============================================

@app.route('/admin/trainer/tags', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_trainer_tags():
    """Получить все теги или создать новый"""
    if request.method == 'GET':
        tags = trainer_mgr.get_all_tags()
        return jsonify({'success': True, 'tags': tags})

    # POST - создать тег
    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'error': 'Название обязательно'})

    # Генерируем случайный цвет
    import random
    colors = ['#2196F3', '#4CAF50', '#FF9800', '#9C27B0', '#00BCD4', '#795548', '#E91E63', '#607D8B']
    icons = ['🏷️', '📌', '⭐', '🔖', '📋', '🎯']

    result = trainer_mgr.create_tag(
        name=name,
        color=random.choice(colors),
        icon=random.choice(icons)
    )

    if result['success']:
        tag = trainer_mgr.get_tag_by_id(result['id'])
        return jsonify({'success': True, 'tag': tag})

    return jsonify(result)


@app.route('/admin/trainer/tags/<int:tag_id>', methods=['PUT', 'DELETE'])
@AdminAuth.login_required
def admin_trainer_tag_detail(tag_id):
    """Обновить или удалить тег"""
    if request.method == 'DELETE':
        result = trainer_mgr.delete_tag(tag_id)
        return jsonify(result)

    # PUT - обновить
    data = request.get_json()
    result = trainer_mgr.update_tag(tag_id, data)
    return jsonify(result)


@app.route('/admin/trainer/export')
@AdminAuth.login_required
def admin_trainer_export():
    """Экспорт статистики в Excel"""
    try:
        import tempfile
        from flask import send_file
        from datetime import datetime
        import pandas as pd

        stats = trainer_mgr.get_statistics()
        users_progress = trainer_mgr.get_all_users_progress()
        detailed_results = trainer_mgr.get_detailed_results()

        # Создаем Excel файл
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        tmp_path = tmp_file.name
        tmp_file.close()

        with pd.ExcelWriter(tmp_path, engine='openpyxl') as writer:
            # Лист 1: Общая статистика
            general_df = pd.DataFrame([{
                'Всего сценариев': stats['total_scenarios'],
                'Всего прохождений': stats['total_completions'],
                'Уникальных пользователей': stats['unique_users'],
                'Средний балл (%)': stats['avg_score']
            }])
            general_df.to_excel(writer, sheet_name='Общая статистика', index=False)

            # Лист 2: Прогресс сотрудников
            if users_progress:
                users_df = pd.DataFrame(users_progress)
                users_df.columns = [
                    'Сотрудник',
                    'Всего прохождений',
                    'Уникальных сценариев',
                    'Средний балл (%)',
                    'Лучший результат (%)',
                    'Первое прохождение',
                    'Последнее прохождение',
                    'Отлично (80%+)',
                    'Хорошо (60-79%)',
                    'Требует работы (<60%)'
                ]
                users_df.to_excel(writer, sheet_name='Прогресс сотрудников', index=False)

            # Лист 3: Детальные результаты
            if detailed_results:
                results_df = pd.DataFrame(detailed_results)
                results_df.columns = [
                    'Сотрудник',
                    'Сценарий',
                    'Уровень',
                    'Баллы',
                    'Макс. баллов',
                    'Процент (%)',
                    'Дата прохождения',
                    'Game Over',
                    'Лояльность клиента'
                ]
                # Преобразуем Game Over в понятный формат
                results_df['Game Over'] = results_df['Game Over'].apply(lambda x: 'Да' if x else 'Нет')
                results_df.to_excel(writer, sheet_name='Все прохождения', index=False)

            # Лист 4: Статистика по уровням
            if stats['levels']:
                levels_df = pd.DataFrame(stats['levels'])
                levels_df.columns = ['Уровень', 'Код', 'Сценариев', 'Прохождений', 'Средний балл (%)']
                levels_df.to_excel(writer, sheet_name='По уровням', index=False)

        return send_file(
            tmp_path,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'trainer_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        )

    except Exception as e:
        print(f"[admin_trainer_export] Ошибка: {e}")
        traceback.print_exc()
        flash('Ошибка экспорта')
        return redirect(url_for('admin_trainer_stats'))


@app.route('/admin/trainer/audit')
@AdminAuth.login_required
def admin_trainer_audit():
    """Журнал изменений (аудит)"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page

    logs = trainer_mgr.get_audit_log(limit=per_page, offset=offset)
    stats = trainer_mgr.get_audit_stats()

    return render_template('admin_trainer_audit.html', logs=logs, stats=stats, page=page)


@app.route('/admin/trainer/audit/export')
@AdminAuth.login_required
def admin_trainer_audit_export():
    """Экспорт журнала аудита в Excel"""
    try:
        import tempfile
        from flask import send_file
        from datetime import datetime
        import pandas as pd

        logs = trainer_mgr.get_audit_log(limit=10000)

        # Создаем Excel файл
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        tmp_path = tmp_file.name
        tmp_file.close()

        if logs:
            df = pd.DataFrame([{
                'Дата/время': log['timestamp'],
                'Пользователь': log['user_id'],
                'Действие': log['action'],
                'Тип': log['entity_type'],
                'ID объекта': log['entity_id'],
                'Название': log['entity_name'],
                'IP адрес': log['ip_address']
            } for log in logs])
            df.to_excel(tmp_path, index=False, sheet_name='Журнал аудита')
        else:
            pd.DataFrame([{'Сообщение': 'Нет записей'}]).to_excel(tmp_path, index=False)

        return send_file(
            tmp_path,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'audit_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        )

    except Exception as e:
        print(f"[admin_trainer_audit_export] Ошибка: {e}")
        flash('Ошибка экспорта')
        return redirect(url_for('admin_trainer_audit'))


# ============================================
# ИМПОРТ СЦЕНАРИЕВ ИЗ EXCEL
# ============================================

@app.route('/admin/trainer/import')
@AdminAuth.login_required
def admin_trainer_import():
    """Страница импорта сценариев"""
    return render_template('admin_trainer_import.html')


@app.route('/admin/trainer/import/template')
@AdminAuth.login_required
def admin_trainer_import_template():
    """Скачать шаблон Excel для импорта"""
    import tempfile
    from flask import send_file
    import pandas as pd

    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    tmp_path = tmp_file.name
    tmp_file.close()

    with pd.ExcelWriter(tmp_path, engine='openpyxl') as writer:
        # Лист 1: Сценарий
        scenario_df = pd.DataFrame([{
            'Название': 'Пример сценария',
            'Описание': 'Описание ситуации',
            'Уровень': 'basic',
            'Теги': 'Карты, Переводы',
            'Время на ответ (сек)': 15,
            'Клиент: Имя': 'Иван Петров',
            'Клиент: Баланс': '5 000 руб.'
        }])
        scenario_df.to_excel(writer, sheet_name='Сценарий', index=False)

        # Лист 2: Шаги
        steps_df = pd.DataFrame([
            {'Номер шага': 1, 'Сообщение клиента': 'Здравствуйте! У меня проблема...', 'Эмоция': 'neutral'},
            {'Номер шага': 2, 'Сообщение клиента': 'Всё ещё не работает!', 'Эмоция': 'irritation'}
        ])
        steps_df.to_excel(writer, sheet_name='Шаги', index=False)

        # Лист 3: Ответы
        answers_df = pd.DataFrame([
            {'Номер шага': 1, 'Текст ответа': 'Добрый день! Давайте разберёмся.', 'Тип': 'correct', 'Баллы': 10, 'Влияние': 10, 'Feedback': 'Отличное приветствие!'},
            {'Номер шага': 1, 'Текст ответа': 'Что случилось?', 'Тип': 'partial', 'Баллы': 5, 'Влияние': 0, 'Feedback': 'Можно вежливее'},
            {'Номер шага': 1, 'Текст ответа': 'Ждите.', 'Тип': 'wrong', 'Баллы': 0, 'Влияние': -20, 'Feedback': 'Грубый ответ'},
            {'Номер шага': 2, 'Текст ответа': 'Понимаю вас, сейчас решим!', 'Тип': 'correct', 'Баллы': 10, 'Влияние': 10, 'Feedback': 'Хорошо!'}
        ])
        answers_df.to_excel(writer, sheet_name='Ответы', index=False)

    return send_file(
        tmp_path,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='trainer_import_template.xlsx'
    )


@app.route('/admin/trainer/import/preview', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_import_preview():
    """Превью импортируемого файла"""
    import pandas as pd

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не загружен'})

    file = request.files['file']

    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({'success': False, 'error': 'Поддерживаются только файлы Excel'})

    try:
        # Читаем Excel
        xlsx = pd.ExcelFile(file)

        # Проверяем наличие листов
        required_sheets = ['Сценарий', 'Шаги', 'Ответы']
        missing = [s for s in required_sheets if s not in xlsx.sheet_names]
        if missing:
            return jsonify({'success': False, 'errors': [f'Отсутствуют листы: {", ".join(missing)}']})

        # Читаем данные
        scenario_df = pd.read_excel(xlsx, 'Сценарий')
        steps_df = pd.read_excel(xlsx, 'Шаги')
        answers_df = pd.read_excel(xlsx, 'Ответы')

        # Валидация сценария
        errors = []
        if scenario_df.empty:
            errors.append('Лист "Сценарий" пустой')
        if steps_df.empty:
            errors.append('Лист "Шаги" пустой')

        if errors:
            return jsonify({'success': False, 'errors': errors})

        # Парсим данные
        scenario_row = scenario_df.iloc[0]

        title = str(scenario_row.get('Название', '')).strip()
        if not title or title == 'nan':
            errors.append('Название сценария обязательно')
            return jsonify({'success': False, 'errors': errors})

        description = str(scenario_row.get('Описание', '')).strip()
        if description == 'nan':
            description = ''

        level = str(scenario_row.get('Уровень', 'basic')).strip().lower()
        if level not in ['basic', 'medium', 'advanced', 'hard']:
            level = 'basic'

        tags = str(scenario_row.get('Теги', '')).strip()
        if tags == 'nan':
            tags = ''

        timer = scenario_row.get('Время на ответ (сек)', 15)
        try:
            timer = int(timer)
        except:
            timer = 15

        client_name = str(scenario_row.get('Клиент: Имя', '')).strip()
        if client_name == 'nan':
            client_name = ''

        client_balance = str(scenario_row.get('Клиент: Баланс', '')).strip()
        if client_balance == 'nan':
            client_balance = ''

        # Парсим шаги
        steps = []
        for _, row in steps_df.iterrows():
            step_num = row.get('Номер шага', 0)
            try:
                step_num = int(step_num)
            except:
                continue

            message = str(row.get('Сообщение клиента', '')).strip()
            if not message or message == 'nan':
                continue

            mood = str(row.get('Эмоция', 'neutral')).strip().lower()
            if mood not in ['neutral', 'anger', 'irritation', 'satisfaction', 'delight']:
                mood = 'neutral'

            # Получаем ответы для этого шага
            step_answers = []
            for _, ans_row in answers_df[answers_df['Номер шага'] == step_num].iterrows():
                ans_text = str(ans_row.get('Текст ответа', '')).strip()
                if not ans_text or ans_text == 'nan':
                    continue

                ans_type = str(ans_row.get('Тип', 'wrong')).strip().lower()
                if ans_type not in ['correct', 'partial', 'wrong']:
                    ans_type = 'wrong'

                points = ans_row.get('Баллы', 0)
                try:
                    points = int(points)
                except:
                    points = 0

                impact = ans_row.get('Влияние', 0)
                try:
                    impact = int(impact)
                except:
                    impact = 0

                feedback = str(ans_row.get('Feedback', '')).strip()
                if feedback == 'nan':
                    feedback = ''

                step_answers.append({
                    'text': ans_text,
                    'type': ans_type,
                    'points': points,
                    'impact': impact,
                    'feedback': feedback
                })

            steps.append({
                'num': step_num,
                'message': message,
                'mood': mood,
                'answers': step_answers
            })

        if not steps:
            errors.append('Нет валидных шагов')
            return jsonify({'success': False, 'errors': errors})

        # Сортируем шаги по номеру
        steps.sort(key=lambda x: x['num'])

        return jsonify({
            'success': True,
            'data': {
                'title': title,
                'description': description,
                'level': level,
                'tags': tags,
                'timer': timer,
                'client_name': client_name,
                'client_balance': client_balance,
                'steps': steps
            }
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Ошибка чтения файла: {str(e)}'})


@app.route('/admin/trainer/import/confirm', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_import_confirm():
    """Подтвердить и выполнить импорт"""
    try:
        data = request.get_json()

        # Получаем level_id по коду
        level = trainer_mgr.get_level_by_code(data.get('level', 'basic'))
        if not level:
            return jsonify({'success': False, 'error': 'Неверный уровень'})

        # Подготавливаем карточку клиента
        client_info = {}
        if data.get('client_name'):
            client_info['name'] = data['client_name']
        if data.get('client_balance'):
            client_info['balance'] = data['client_balance']

        # Создаём сценарий
        scenario_data = {
            'level_id': level['id'],
            'title': data.get('title', 'Импортированный сценарий'),
            'description': data.get('description', ''),
            'timer_seconds': data.get('timer', 15),
            'is_active': 0,  # Неактивен по умолчанию
            'client_info_json': json.dumps(client_info, ensure_ascii=False) if client_info else None
        }

        result = trainer_mgr.create_scenario(scenario_data)

        if not result['success']:
            return jsonify({'success': False, 'error': result.get('error', 'Ошибка создания сценария')})

        scenario_id = result['id']

        # Обрабатываем теги
        if data.get('tags'):
            tag_names = [t.strip() for t in data['tags'].split(',') if t.strip()]
            all_tags = trainer_mgr.get_all_tags()
            tag_ids = []

            for tag_name in tag_names:
                # Ищем существующий тег
                existing = next((t for t in all_tags if t['name'].lower() == tag_name.lower()), None)
                if existing:
                    tag_ids.append(existing['id'])
                else:
                    # Создаём новый тег
                    import random
                    colors = ['#2196F3', '#4CAF50', '#FF9800', '#9C27B0', '#00BCD4', '#795548']
                    tag_result = trainer_mgr.create_tag(tag_name, random.choice(colors), '🏷️')
                    if tag_result['success']:
                        tag_ids.append(tag_result['id'])

            if tag_ids:
                trainer_mgr.set_scenario_tags(scenario_id, tag_ids)

        # Создаём шаги и ответы
        for step_data in data.get('steps', []):
            step_result = trainer_mgr.create_step(scenario_id, {
                'client_message': step_data['message'],
                'client_name': data.get('client_name', 'Клиент'),
                'initial_mood': step_data.get('mood', 'neutral')
            })

            if step_result['success']:
                step_id = step_result['id']

                for answer in step_data.get('answers', []):
                    trainer_mgr.create_answer(step_id, {
                        'answer_text': answer['text'],
                        'is_correct': 1 if answer['type'] == 'correct' else 0,
                        'is_partial': 1 if answer['type'] == 'partial' else 0,
                        'points': answer.get('points', 0),
                        'mood_impact': answer.get('impact', 0),
                        'feedback': answer.get('feedback', '')
                    })

        # Пересчитываем total_points
        steps = trainer_mgr.get_scenario_steps(scenario_id)
        total_points = 0
        for step in steps:
            answers = trainer_mgr.get_step_answers(step['id'])
            max_step_points = max([a['points'] for a in answers], default=0)
            total_points += max_step_points

        trainer_mgr.update_scenario(scenario_id, {'total_points': total_points})

        # Логируем
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='import',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=data.get('title'),
            ip_address=request.remote_addr
        )

        flash(f'Сценарий "{data.get("title")}" успешно импортирован!')
        return jsonify({
            'success': True,
            'scenario_id': scenario_id,
            'redirect_url': url_for('admin_trainer_edit', scenario_id=scenario_id)
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


# ============================================
# АДМИН-ПАНЕЛЬ
# ============================================

@app.route('/login', methods=['GET', 'POST'])
def user_login():
    """Страница входа для всех пользователей через AD"""
    # Проверяем, нужно ли показать ошибку (только после POST запроса)
    show_error = request.args.get('error')
    error_message = None

    if show_error == 'invalid':
        error_message = 'Неверный логин или пароль'
    elif show_error == 'rate_limit':
        error_message = 'Слишком много попыток входа. Попробуйте через 15 минут.'
    elif show_error == 'credentials':
        error_message = 'Некорректные учётные данные'

    if request.method == 'POST':
        # Rate limiting для защиты от brute force
        ip = get_client_ip()
        if not rate_limiter.check_login_attempt(ip, max_attempts=5, window=900):
            return redirect(url_for('user_login', error='rate_limit')), 429

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Валидация длины
        if len(username) > 100 or len(password) > 128:
            return redirect(url_for('user_login', error='credentials'))

        # Тестовый режим (без AD) - для локальной разработки
        # По умолчанию включен если нет AD_SERVER в .env
        TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

        if TEST_MODE:
            # Тестовая авторизация: любой логин/пароль где пароль = "test" или "123"
            if password in ['test', '123', 'password']:
                session['user_info'] = {
                    'username': username,
                    'name': username.title(),
                    'department': 'Тестовый отдел',
                    'email': f'{username}@test.local',
                    'workplace': ''
                }
                session['authenticated'] = True
                session.permanent = True
                return redirect(url_for('choose_help_type'))
            else:
                return redirect(url_for('user_login', error='invalid'))

        # Аутентификация через AD (продакшен)
        from ad_auth import ad_auth
        ad_result = ad_auth.verify_credentials(username, password)

        if ad_result:
            # Успешная аутентификация - сохраняем данные в сессию
            session['user_info'] = {
                'username': ad_result.get('username', username),
                'name': ad_result.get('display_name', username),
                'department': ad_result.get('department', ''),
                'email': ad_result.get('email', ''),
                'workplace': ''  # Будет заполнено позже при необходимости
            }
            session['authenticated'] = True
            session.permanent = True

            # Переходим к выбору типа помощи
            return redirect(url_for('choose_help_type'))
        else:
            return redirect(url_for('user_login', error='invalid'))

    return render_template('user_login.html', error_message=error_message)

@app.route('/enter_workplace', methods=['GET', 'POST'])
def enter_workplace():
    """Страница ввода рабочего места (только для работы с мануалами)"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    if request.method == 'POST':
        workplace = request.form.get('workplace', '').strip()

        # Валидация рабочего места
        if not re.fullmatch(r".{1,50}", workplace):
            flash('Некорректное рабочее место')
            return redirect(url_for('enter_workplace'))

        # Обновляем workplace в сессии
        session['user_info']['workplace'] = workplace
        session.modified = True

        # Возвращаемся туда, откуда пришли (или на главную)
        next_page = session.pop('next_after_workplace', 'choose_help_type')
        return redirect(url_for(next_page))

    return render_template('enter_workplace.html', user_info=session['user_info'])

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Страница авторизации администратора"""
    if request.method == 'POST':
        # Security Fix: Stricter rate limiting for login attempts to prevent brute force
        ip = get_client_ip()
        if not rate_limiter.check_login_attempt(ip, max_attempts=5, window=900):  # 5 attempts per 15 minutes
            flash('Слишком много попыток входа. Попробуйте через 15 минут.')
            return redirect(url_for('admin_login')), 429

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Валидация длины
        if len(username) > 50 or len(password) > 100:
            flash('Некорректные учётные данные')
            return redirect(url_for('admin_login'))

        # Тестовый режим для админа
        TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

        if TEST_MODE and password in ['admin', '123', 'test']:
            session['admin_logged_in'] = True
            session['admin_username'] = username
            session['admin_role'] = ROLE_SUPER_ADMIN
            session['admin_token'] = AdminAuth.generate_session_token()
            session.permanent = True
            flash(f'Успешная авторизация (тестовый режим). Роль: Супер-Админ')
            return redirect(url_for('admin_dashboard'))

        admin_data = AdminAuth.verify_admin(username, password)
        if admin_data:
            session['admin_logged_in'] = True
            session['admin_username'] = username
            session['admin_role'] = admin_data.get('role', ROLE_EDITOR)
            session['admin_token'] = AdminAuth.generate_session_token()
            session.permanent = True  # Use permanent session with timeout
            flash(f'Успешная авторизация. Роль: {ROLE_NAMES.get(admin_data.get("role"), "Редактор")}')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Неверный логин или пароль')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    """Выход из админ-панели"""
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    session.pop('admin_role', None)
    session.pop('admin_token', None)
    flash('Вы вышли из системы')
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
@AdminAuth.login_required
def admin_dashboard():
    """Главная страница админ-панели"""
    manuals = admin_manager.load_manuals()
    return render_template('admin_dashboard.html', manuals=manuals)


@app.route('/admin/manual/create', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_create_manual():
    """Создание нового мануала"""
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        manual_type = request.form.get('manual_type', 'with_subproblems').strip()

        # Валидация
        if not title:
            flash('Название обязательно для заполнения')
            return render_template('admin_create_manual.html')

        # Загружаем существующие мануалы
        manuals = admin_manager.load_manuals()

        # Автоматически находим следующий свободный ID
        existing_ids = []
        for mid in manuals.keys():
            try:
                existing_ids.append(int(mid))
            except ValueError:
                pass

        # Находим следующий свободный номер
        manual_id = '1'
        if existing_ids:
            manual_id = str(max(existing_ids) + 1)

        # Добавляем номер к названию (если его там ещё нет)
        sanitized_title = admin_manager.sanitize_text(title, 200)
        if not sanitized_title.startswith(f"{manual_id}."):
            sanitized_title = f"{manual_id}. {sanitized_title}"

        # Создаём новый мануал в зависимости от типа
        if manual_type == 'simple':
            # Простой мануал без подпроблем
            manuals[manual_id] = {
                "title": sanitized_title,
                "photos": []
            }
        else:
            # Мануал с подпроблемами
            manuals[manual_id] = {
                "title": sanitized_title,
                "subproblems": {}
            }

        # Сохраняем
        if admin_manager.save_manuals(manuals):
            flash(f'Мануал "{title}" успешно создан!')
            return redirect(url_for('admin_edit_manual', manual_id=manual_id))
        else:
            flash('Ошибка при сохранении мануала')
            return render_template('admin_create_manual.html')

    # GET request - показываем форму
    return render_template('admin_create_manual.html')


@app.route('/admin/manual/<string:manual_id>/edit')
@AdminAuth.login_required
def admin_edit_manual(manual_id):
    """Страница редактирования мануала - теперь показывает список подпроблем"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Если есть поле subproblems - показываем список подпроблем (даже если пустой)
    if 'subproblems' in manual:
        return render_template('admin_manual_subproblems.html', manual_id=manual_id, manual=manual)

    # Если нет поля subproblems - это простой мануал
    return redirect(url_for('admin_edit_simple_manual', manual_id=manual_id))


@app.route('/admin/manual/<string:manual_id>/subproblem/create', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_create_subproblem(manual_id):
    """Создание новой подпроблемы"""
    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()

        # Валидация
        if not title:
            flash('Название обязательно для заполнения')
            return render_template('admin_create_subproblem.html', manual_id=manual_id, manual=manual)

        # Проверка на существование
        if 'subproblems' not in manual:
            manual['subproblems'] = {}

        # Автоматически находим следующий свободный номер
        existing_nums = []
        for subp_id in manual['subproblems'].keys():
            if '.' in subp_id:
                try:
                    num = int(subp_id.split('.')[1])
                    existing_nums.append(num)
                except ValueError:
                    pass

        # Находим следующий свободный номер
        next_num = max(existing_nums) + 1 if existing_nums else 1

        # Формируем полный ID подпроблемы
        subproblem_id = f"{manual_id}.{next_num}"

        # Создаём новую подпроблему
        manual['subproblems'][subproblem_id] = {
            "title": admin_manager.sanitize_text(title, 200),
            "photos": [],
            "video": None
        }

        # Сохраняем
        if admin_manager.save_manuals(manuals):
            flash(f'Подпроблема "{title}" успешно создана!')
            return redirect(url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id))
        else:
            flash('Ошибка при сохранении подпроблемы')
            return render_template('admin_create_subproblem.html', manual_id=manual_id, manual=manual)

    # GET request - показываем форму
    return render_template('admin_create_subproblem.html', manual_id=manual_id, manual=manual)


@app.route('/admin/manual/<string:manual_id>/delete', methods=['POST'])
@AdminAuth.login_required
def admin_delete_manual(manual_id):
    """Удаление мануала"""
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    manuals = admin_manager.load_manuals()

    if manual_id not in manuals:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    manual_title = manuals[manual_id].get('title', 'Неизвестный мануал')

    # Удаляем мануал
    del manuals[manual_id]

    if admin_manager.save_manuals(manuals):
        flash(f'Мануал "{manual_title}" успешно удалён')
    else:
        flash('Ошибка при удалении мануала')

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/manual/<string:manual_id>/subproblem/<string:subproblem_id>/delete', methods=['POST'])
@AdminAuth.login_required
def admin_delete_subproblem(manual_id, subproblem_id):
    """Удаление подпроблемы"""
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_dashboard'))

    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
        flash('Подпроблема не найдена')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    subproblem_title = manual['subproblems'][subproblem_id].get('title', 'Неизвестная подпроблема')

    # Удаляем подпроблему
    del manual['subproblems'][subproblem_id]

    if admin_manager.save_manuals(manuals):
        flash(f'Подпроблема "{subproblem_title}" успешно удалена')
    else:
        flash('Ошибка при удалении подпроблемы')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/manual/<string:manual_id>/edit-simple')
@AdminAuth.login_required
def admin_edit_simple_manual(manual_id):
    """Страница редактирования простого мануала (без подпроблем)"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))
    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Проверяем что это простой мануал
    if 'subproblems' in manual:
        flash('Этот мануал содержит подпроблемы')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    # Получаем URLs для фотографий
    photo_urls = []
    if 'photos' in manual:
        for photo in manual['photos']:
            url = get_file_url(photo.get('id'))
            photo_urls.append(url)

    # Получаем URL для видео если есть
    video_url = None
    if 'video' in manual and manual['video'] is not None:
        video_id = manual['video'].get('id')
        if video_id:
            video_url = get_file_url(video_id)

    # Используем тот же template что и для подпроблем, но передаём manual вместо subproblem
    return render_template('admin_edit_subproblem.html',
                         manual_id=manual_id,
                         manual_title=manual.get('title', ''),
                         subproblem_id=manual_id,  # Для простых мануалов subproblem_id = manual_id
                         subproblem=manual,  # Передаём сам мануал как "подпроблему"
                         photo_urls=photo_urls,
                         video_url=video_url,
                         is_simple_manual=True)  # Флаг что это простой мануал


@app.route('/admin/manual/<string:manual_id>/subproblem/<string:subproblem_id>/edit')
@AdminAuth.login_required
def admin_edit_subproblem(manual_id, subproblem_id):
    """Страница редактирования отдельной подпроблемы"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_dashboard'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Проверяем существование подпроблемы
    if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
        flash('Подпроблема не найдена')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    subproblem = manual['subproblems'][subproblem_id]

    # Получаем URLs для фотографий чтобы показать preview
    photo_urls = []
    if 'photos' in subproblem:
        for photo in subproblem['photos']:
            url = get_file_url(photo.get('id'))
            photo_urls.append(url)

    # Получаем URL для видео если есть
    video_url = None
    if 'video' in subproblem and subproblem['video'] is not None:
        video_id = subproblem['video'].get('id')
        if video_id:
            video_url = get_file_url(video_id)

    return render_template('admin_edit_subproblem.html',
                         manual_id=manual_id,
                         manual_title=manual.get('title', ''),
                         subproblem_id=subproblem_id,
                         subproblem=subproblem,
                         photo_urls=photo_urls,
                         video_url=video_url)


@app.route('/admin/manual/<string:manual_id>/update', methods=['POST'])
@AdminAuth.login_required
def admin_update_manual(manual_id):
    """Обновление мануала (только заголовок, подпроблемы редактируются отдельно)"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    title = request.form.get('title', '').strip()

    # Валидация заголовка
    title = admin_manager.sanitize_text(title, max_length=200)
    if not title:
        flash('Заголовок не может быть пустым')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Обновляем только заголовок
    manual['title'] = title

    # Сохраняем изменения
    if admin_manager.update_manual(manual_id, title, manual):
        flash('Заголовок мануала успешно обновлён')
    else:
        flash('Ошибка при сохранении изменений')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/manual/<string:manual_id>/subproblem/<string:subproblem_id>/update', methods=['POST'])
@AdminAuth.login_required
def admin_update_subproblem(manual_id, subproblem_id):
    """Обновление отдельной подпроблемы"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Определяем тип мануала и получаем нужный объект
    if 'subproblems' in manual:
        # Мануал с подпроблемами
        if subproblem_id not in manual['subproblems']:
            flash('Подпроблема не найдена')
            return redirect(url_for('admin_edit_manual', manual_id=manual_id))
        target_obj = manual['subproblems'][subproblem_id]
        redirect_url = url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id)
        success_message = 'Подпроблема успешно обновлена'
    else:
        # Простой мануал
        target_obj = manual
        redirect_url = url_for('admin_edit_simple_manual', manual_id=manual_id)
        success_message = 'Мануал успешно обновлён'

    # Обновляем подписи к фото
    if 'photos' in target_obj:
        for photo_index, photo in enumerate(target_obj['photos']):
            caption_field = f'caption_{photo_index}'
            if caption_field in request.form:
                new_caption = request.form.get(caption_field, '').strip()
                new_caption = admin_manager.sanitize_text(new_caption, max_length=300)
                photo['caption'] = new_caption

    # Обновляем подпись к видео если есть
    if 'video' in target_obj:
        video_caption_field = 'video_caption'
        if video_caption_field in request.form:
            new_video_caption = request.form.get(video_caption_field, '').strip()
            new_video_caption = admin_manager.sanitize_text(new_video_caption, max_length=300)
            target_obj['video']['caption'] = new_video_caption

    # Сохраняем изменения
    if admin_manager.update_manual(manual_id, manual.get('title', ''), manual):
        flash(success_message)
    else:
        flash('Ошибка при сохранении изменений')

    return redirect(redirect_url)


@app.route('/admin/delete-photo', methods=['POST'])
@AdminAuth.login_required
def admin_delete_photo():
    """Удаление фото из мануала"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    photo_index_str = request.form.get('photo_index', '0')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_dashboard'))

    try:
        photo_index = int(photo_index_str)
        if photo_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс фото')
        return redirect(url_for('admin_dashboard'))

    # Удаляем фото
    if admin_manager.delete_photo(manual_id, subproblem_id, photo_index):
        flash('Фото успешно удалено')
    else:
        flash('Ошибка при удалении фото')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/delete-step', methods=['POST'])
@AdminAuth.login_required
def admin_delete_step():
    """Удаление всего шага (фото + описание) из подпроблемы"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    step_index_str = request.form.get('step_index', '0')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
    # if not admin_manager.validate_subproblem_id(subproblem_id):
    #     flash('Некорректный ID подпроблемы')
    #     return redirect(url_for('admin_dashboard'))

    try:
        step_index = int(step_index_str)
        if step_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс шага')
        return redirect(url_for('admin_dashboard'))

    # Загружаем мануалы
    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_dashboard'))

    # Определяем тип мануала и получаем нужный объект
    if 'subproblems' in manual:
        # Мануал с подпроблемами
        if subproblem_id not in manual['subproblems']:
            flash('Подпроблема не найдена')
            return redirect(url_for('admin_dashboard'))
        target_obj = manual['subproblems'][subproblem_id]
        redirect_url = url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id)
    else:
        # Простой мануал
        target_obj = manual
        redirect_url = url_for('admin_edit_simple_manual', manual_id=manual_id)

    if 'photos' not in target_obj or not isinstance(target_obj['photos'], list):
        flash('Шаги не найдены')
        return redirect(redirect_url)

    # Проверяем индекс
    if step_index >= len(target_obj['photos']):
        flash('Шаг не найден')
        return redirect(redirect_url)

    # Удаляем шаг
    del target_obj['photos'][step_index]

    # Сохраняем
    if admin_manager.save_manuals(manuals):
        flash(f'Шаг {step_index + 1} успешно удалён')
    else:
        flash('Ошибка при удалении шага')

    return redirect(redirect_url)


@app.route('/admin/delete-video', methods=['POST'])
@AdminAuth.login_required
def admin_delete_video():
    """Удаление видео из подпроблемы"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_dashboard'))

    # Удаляем видео
    if admin_manager.delete_video(manual_id, subproblem_id):
        flash('Видео успешно удалено')
    else:
        flash('Ошибка при удалении видео')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/upload-photo', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_upload_photo():
    """Загрузка нового скриншота"""
    if request.method == 'GET':
        manual_id = request.args.get('manual_id', '')
        subproblem_id = request.args.get('subproblem_id', '')
        photo_index = request.args.get('photo_index', '0')

        # Валидация параметров
        if not admin_manager.validate_manual_id(manual_id):
            flash('Некорректный ID мануала')
            return redirect(url_for('admin_dashboard'))

        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_dashboard'))

        try:
            photo_index = int(photo_index)
            if photo_index < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash('Некорректный индекс фото')
            return redirect(url_for('admin_dashboard'))

        return render_template('admin_upload_photo.html',
                             manual_id=manual_id,
                             subproblem_id=subproblem_id,
                             photo_index=photo_index)

    # POST - обработка загрузки
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    photo_index_str = request.form.get('photo_index', '0')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_dashboard'))

    try:
        photo_index = int(photo_index_str)
        if photo_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс фото')
        return redirect(url_for('admin_dashboard'))

    # Security Fix: Improved file upload validation
    allowed_image_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    max_file_size = 10 * 1024 * 1024  # 10 MB

    if 'photo' not in request.files:
        flash('Файл не был загружен')
        return redirect(request.url)

    file = request.files['photo']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(request.url)

    # Security Fix: Strict content type validation
    if not file.content_type or file.content_type not in allowed_image_types:
        flash('Можно загружать только изображения (JPEG, PNG, GIF, WebP)')
        return redirect(request.url)

    # Security Fix: Check content-length header first
    if request.content_length and request.content_length > max_file_size:
        flash('Файл слишком большой (максимум 10 МБ)')
        return redirect(request.url)

    # Проверка размера (максимум 10MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size > max_file_size:
        flash('Файл слишком большой (максимум 10 МБ)')
        return redirect(request.url)

    try:
        # Отправляем фото в Telegram чтобы получить file_id
        msg = bot.send_photo(TECH_SUPPORT_CHAT_ID, file)

        # Получаем file_id самой большой версии фото
        if msg.photo:
            new_photo_id = msg.photo[-1].file_id

            # Получаем текущую подпись
            manual = admin_manager.get_manual(manual_id)
            if not manual:
                flash('Мануал не найден')
                return redirect(url_for('admin_dashboard'))

            current_caption = ""
            if 'subproblems' in manual and subproblem_id in manual['subproblems']:
                subproblem = manual['subproblems'][subproblem_id]
                if 'photos' in subproblem and photo_index < len(subproblem['photos']):
                    current_caption = subproblem['photos'][photo_index].get('caption', '')

            # Обновляем фото
            if admin_manager.update_photo(manual_id, subproblem_id, photo_index, new_photo_id, current_caption):
                flash('Скриншот успешно обновлён')
            else:
                flash('Ошибка при сохранении изменений')
        else:
            flash('Не удалось получить file_id от Telegram')

    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        traceback.print_exc()
        flash('Ошибка при загрузке файла')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/add-new-step', methods=['POST'])
@AdminAuth.login_required
def admin_add_new_step():
    """Добавление нового шага в подпроблему"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    caption = request.form.get('caption', '').strip()
    after_index_str = request.form.get('after_index', '-1')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    # Для простых мануалов subproblem_id = manual_id, для подпроблем проверяем формат X.Y
    manual = admin_manager.get_manual(manual_id)
    if manual and 'subproblems' in manual:
        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_dashboard'))

    caption = admin_manager.sanitize_text(caption, max_length=300)
    if not caption:
        flash('Описание шага не может быть пустым')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    # Парсим индекс
    try:
        after_index = int(after_index_str)
    except (ValueError, TypeError):
        after_index = -1

    # Добавляем новый шаг
    if admin_manager.add_new_step(manual_id, subproblem_id, caption, after_index):
        if after_index == -1:
            flash('Новый шаг успешно добавлен в конец')
        else:
            flash(f'Новый шаг успешно добавлен после шага {after_index + 1}')
    else:
        flash('Ошибка при добавлении шага')

    # Редирект обратно на страницу редактирования
    manual = admin_manager.get_manual(manual_id)
    if manual and 'subproblems' in manual:
        return redirect(url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id))
    return redirect(url_for('admin_edit_simple_manual', manual_id=manual_id))


@app.route('/admin/upload-video', methods=['GET', 'POST'])
@AdminAuth.login_required
def admin_upload_video():
    """Загрузка видео-мануала"""
    if request.method == 'GET':
        manual_id = request.args.get('manual_id', '')
        subproblem_id = request.args.get('subproblem_id', '')

        # Валидация параметров
        if not admin_manager.validate_manual_id(manual_id):
            flash('Некорректный ID мануала')
            return redirect(url_for('admin_dashboard'))

        # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
        # if not admin_manager.validate_subproblem_id(subproblem_id):
        #     flash('Некорректный ID подпроблемы')
        #     return redirect(url_for('admin_dashboard'))

        return render_template('admin_upload_video.html',
                             manual_id=manual_id,
                             subproblem_id=subproblem_id)

    # POST - обработка загрузки
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    caption = request.form.get('caption', '').strip()

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_dashboard'))

    # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
    # if not admin_manager.validate_subproblem_id(subproblem_id):
    #     flash('Некорректный ID подпроблемы')
    #     return redirect(url_for('admin_dashboard'))

    # Security Fix: Improved video upload validation
    allowed_video_types = {'video/mp4', 'video/mpeg', 'video/quicktime', 'video/x-msvideo', 'video/webm'}
    max_file_size = 50 * 1024 * 1024  # 50 MB

    if 'video' not in request.files:
        flash('Файл не был загружен')
        return redirect(request.url)

    file = request.files['video']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(request.url)

    # Security Fix: Strict content type validation
    if not file.content_type or file.content_type not in allowed_video_types:
        flash('Можно загружать только видео (MP4, MPEG, MOV, AVI, WebM)')
        return redirect(request.url)

    # Security Fix: Check content-length header first
    if request.content_length and request.content_length > max_file_size:
        flash('Файл слишком большой (максимум 50 МБ)')
        return redirect(request.url)

    # Проверка размера (максимум 50MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size > max_file_size:
        flash('Файл слишком большой (максимум 50 МБ)')
        return redirect(request.url)

    try:
        # Отправляем видео в Telegram чтобы получить file_id
        msg = bot.send_video(TECH_SUPPORT_CHAT_ID, file)

        # Получаем file_id видео
        if msg.video:
            video_file_id = msg.video.file_id

            # Sanitize caption
            caption = admin_manager.sanitize_text(caption, max_length=300) if caption else 'Видео-инструкция'

            # Добавляем видео в подпроблему
            if admin_manager.add_video_to_subproblem(manual_id, subproblem_id, video_file_id, caption):
                flash('Видео успешно загружено')
            else:
                flash('Ошибка при сохранении изменений')
        else:
            flash('Не удалось получить file_id от Telegram')

    except Exception as e:
        print(f"Ошибка при загрузке видео: {e}")
        traceback.print_exc()
        flash('Ошибка при загрузке файла')

    # Редиректим правильно в зависимости от типа мануала
    manual = admin_manager.get_manual(manual_id)
    if manual and 'subproblems' in manual:
        # Мануал с подпроблемами - редирект на страницу редактирования подпроблемы
        return redirect(url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id))
    else:
        # Простой мануал - редирект на страницу редактирования простого мануала
        return redirect(url_for('admin_edit_simple_manual', manual_id=manual_id))


# ============================================
# УПРАВЛЕНИЕ ТЕМАТИКАМИ
# ============================================

@app.route('/admin/topics')
@AdminAuth.login_required
def admin_topics():
    """Страница управления тематиками"""
    stats = tm.get_statistics()
    channels = tm.get_all_channels()
    return render_template('admin_topics.html', stats=stats, channels=channels)


@app.route('/admin/topics/add', methods=['POST'])
@AdminAuth.login_required
def admin_add_topic():
    """Добавление новой тематики"""
    try:
        channel = request.form.get('channel', '').strip()
        sr1 = request.form.get('sr1', '').strip() or None
        sr2 = request.form.get('sr2', '').strip() or None
        sr3 = request.form.get('sr3', '').strip() or None
        sr4 = request.form.get('sr4', '').strip() or None
        full_topic = request.form.get('full_topic', '').strip() or None

        # Валидация
        if not channel:
            flash('Канал обязателен для заполнения')
            return redirect(url_for('admin_topics'))

        # Ограничение длины полей
        if len(channel) > 100:
            flash('Канал слишком длинный (макс. 100 символов)')
            return redirect(url_for('admin_topics'))

        for field, value in [('SR1', sr1), ('SR2', sr2), ('SR3', sr3), ('SR4', sr4)]:
            if value and len(value) > 200:
                flash(f'{field} слишком длинный (макс. 200 символов)')
                return redirect(url_for('admin_topics'))

        if full_topic and len(full_topic) > 500:
            flash('Полная тематика слишком длинная (макс. 500 символов)')
            return redirect(url_for('admin_topics'))

        # Добавляем тематику
        result = tm.add_topic(
            channel=channel,
            sr1=sr1,
            sr2=sr2,
            sr3=sr3,
            sr4=sr4,
            full_topic=full_topic
        )

        if result['success']:
            flash(f'Тематика успешно добавлена (ID: {result["id"]})')
            log_topic_change(
                action='topic_created',
                topic_id=result.get('id'),
                channel=channel,
                full_topic=full_topic or '',
                details={'sr1': sr1 or '', 'sr2': sr2 or '', 'sr3': sr3 or '', 'sr4': sr4 or ''}
            )
        else:
            flash(f'Ошибка при добавлении тематики: {result.get("error", "Неизвестная ошибка")}')

    except Exception as e:
        print(f"[admin_add_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при добавлении тематики')

    return redirect(url_for('admin_topics'))


@app.route('/admin/topics/delete/<int:topic_id>', methods=['POST'])
@AdminAuth.login_required
def admin_delete_topic(topic_id):
    """Удаление тематики"""
    try:
        topic_before = None
        try:
            cursor = tm.conn.cursor()
            cursor.execute("SELECT id, channel, full_topic, sr1, sr2, sr3, sr4 FROM topics WHERE id = ?", (topic_id,))
            row = cursor.fetchone()
            if row:
                topic_before = dict(row)
        except Exception:
            topic_before = None

        result = tm.delete_topic(topic_id)

        if result['success']:
            flash(f'Тематика успешно удалена')
            log_topic_change(
                action='topic_deleted',
                topic_id=topic_id,
                channel=(topic_before or {}).get('channel', ''),
                full_topic=(topic_before or {}).get('full_topic', ''),
                details=topic_before or {}
            )
        else:
            flash(f'Ошибка при удалении тематики: {result.get("error", "Неизвестная ошибка")}')

    except Exception as e:
        print(f"[admin_delete_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при удалении тематики')

    return redirect(url_for('admin_topics'))


@app.route('/admin/topics/list')
@AdminAuth.login_required
def admin_list_topics():
    """Список всех тематик"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    channel = request.form.get('channel', '').strip()

    if channel:
        topics = tm.get_topics_by_channel(channel, limit=1000)
    else:
        topics = tm.get_all_topics(limit=1000)

    # Простая пагинация
    total = len(topics)
    start = (page - 1) * per_page
    end = start + per_page
    topics_page = topics[start:end]

    return render_template('admin_topics_list.html',
                         topics=topics_page,
                         page=page,
                         total=total,
                         per_page=per_page)


@app.route('/admin/topics/import', methods=['GET'])
@AdminAuth.login_required
def admin_import_topics():
    """Страница импорта тематик из Excel"""
    stats = tm.get_statistics()
    return render_template('admin_import_topics.html', stats=stats)


@app.route('/admin/topics/import', methods=['POST'])
@AdminAuth.login_required
@rate_limit(max_requests=5, window=60)
def admin_import_topics_upload():
    """Обработка загрузки Excel файла с тематиками"""
    try:
        # Проверяем наличие файла
        if 'file' not in request.files:
            flash('Файл не выбран', 'error')
            return redirect(url_for('admin_import_topics'))

        file = request.files['file']
        if file.filename == '':
            flash('Файл не выбран', 'error')
            return redirect(url_for('admin_import_topics'))

        # Проверяем расширение файла
        if not (file.filename.lower().endswith('.xlsx') or file.filename.lower().endswith('.xls')):
            flash('Неверный формат файла. Поддерживаются только .xlsx и .xls', 'error')
            return redirect(url_for('admin_import_topics'))

        # Получаем параметры
        sheet_name = request.form.get('sheet_name', 'subject_category').strip()
        clear_existing = request.form.get('clear_existing') == 'on'

        # Сохраняем файл временно
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
            file.save(tmp_file.name)
            tmp_path = tmp_file.name

        try:
            # Удаляем все существующие тематики если требуется
            if clear_existing:
                cursor = tm.conn.cursor()
                cursor.execute("SELECT COUNT(*) as cnt FROM topics")
                before_count_row = cursor.fetchone()
                before_count = int(before_count_row['cnt']) if before_count_row else 0
                cursor.execute("DELETE FROM topics")
                tm.conn.commit()
                print(f"[admin_import_topics_upload] Все существующие тематики удалены")
                log_topic_change(
                    action='topics_cleared_before_import',
                    topic_id=None,
                    channel='',
                    full_topic='',
                    details={'deleted_count': before_count}
                )

            # Импортируем из Excel
            result = tm.import_from_excel(tmp_path, sheet_name=sheet_name)

            if result['success']:
                flash(f'✅ Успешно импортировано тематик: {result["imported"]}', 'success')
                print(f"[admin_import_topics_upload] Импортировано: {result['imported']} тематик")
                log_topic_change(
                    action='topics_imported',
                    topic_id=None,
                    channel='',
                    full_topic='',
                    details={
                        'imported': int(result.get('imported', 0)),
                        'errors_count': len(result.get('errors') or []),
                        'sheet_name': sheet_name,
                        'clear_existing': clear_existing
                    }
                )
                if result.get('errors'):
                    flash(f'⚠️ Ошибки при импорте: {len(result["errors"])} строк', 'warning')
                    print(f"[admin_import_topics_upload] Ошибок: {len(result['errors'])}")
            else:
                flash(f'❌ Ошибка импорта: {result.get("error", "Неизвестная ошибка")}', 'error')
                print(f"[admin_import_topics_upload] Ошибка: {result.get('error')}")

        finally:
            # Удаляем временный файл
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        print(f"[admin_import_topics_upload] Ошибка: {e}")
        traceback.print_exc()
        flash(f'Произошла ошибка при импорте: {str(e)}', 'error')

    return redirect(url_for('admin_import_topics'))


@app.route('/admin/topics/export')
@AdminAuth.login_required
def admin_export_topics():
    """Экспорт всех тематик в Excel"""
    try:
        import tempfile
        import os
        from flask import send_file
        from datetime import datetime

        # Создаем временный файл
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        tmp_path = tmp_file.name
        tmp_file.close()

        # Экспортируем в Excel
        result = tm.export_to_excel(tmp_path)

        if result['success']:
            print(f"[admin_export_topics] Экспортировано: {result['exported']} тематик")
            # Отправляем файл пользователю
            return send_file(
                tmp_path,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=f'topics_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            )
        else:
            flash(f'Ошибка экспорта: {result.get("error", "Неизвестная ошибка")}', 'error')
            return redirect(url_for('admin_topics'))

    except Exception as e:
        print(f"[admin_export_topics] Ошибка: {e}")
        traceback.print_exc()
        flash(f'Произошла ошибка при экспорте: {str(e)}', 'error')
        return redirect(url_for('admin_topics'))


# ============================================
# СТАТИСТИКА И АНАЛИТИКА
# ============================================
# TODO: МОДУЛЬ В РАЗРАБОТКЕ
# Данный функционал находится в стадии разработки и тестирования
# Требуется настройка PostgreSQL базы данных (см. переменные POSTGRES_* в .env)
# В production окружении убедитесь в корректной настройке БД перед использованием


def _resolve_period_range():
    """Возвращает границы периода для статистики."""
    period = (request.args.get('period', '') or '').strip().lower()
    days_param = request.args.get('days', type=int)
    date_from = (request.args.get('date_from', '') or '').strip()
    date_to = (request.args.get('date_to', '') or '').strip()
    now = datetime.now()

    if date_from and date_to:
        start = f"{date_from} 00:00:00"
        end = f"{date_to} 23:59:59"
        return start, end

    if period == '1d':
        days = 1
    elif period == '7d':
        days = 7
    elif period == '30d':
        days = 30
    else:
        days = days_param if days_param else 30

    days = max(1, min(days, 365))
    start_dt = now - timedelta(days=days - 1)
    return start_dt.strftime('%Y-%m-%d 00:00:00'), now.strftime('%Y-%m-%d %H:%M:%S')


def _load_ticket_dashboard_data(start_at: str, end_at: str):
    """Загружает и агрегирует события обращений для dashboard.

    "Всего обращений" считается по открытиям инструкций + созданным заявкам, чтобы
    учитывать пользователей, которые не нажали "помогло/не помогло".
    """
    base_events = {'manual_opened_video', 'manual_opened_text', 'ticket_created'}
    helped_events = {'video_helped', 'manual_helped', 'ticket_solved_by_helper'}  # legacy supported
    not_helped_events = {
        'video_not_helped',
        'manual_not_helped',
        'ticket_created',  # эскалация в заявку
        'ticket_not_relevant',
        'ticket_resolved_by_staff'
    }

    # Summary counts
    counts_by_type: dict[str, int] = {}

    # Timeline (date -> counters)
    timeline_map: dict[str, dict] = {}

    # Group stats
    problems_map: dict[str, dict] = {}
    departments_map: dict[str, dict] = {}

    def _bump_group(m: dict, key: str, total_inc: int, helped_inc: int, not_helped_inc: int, key_name: str):
        if key not in m:
            m[key] = {key_name: key, 'count': 0, 'helped': 0, 'not_helped': 0}
        m[key]['count'] += total_inc
        m[key]['helped'] += helped_inc
        m[key]['not_helped'] += not_helped_inc

    if ANALYTICS_USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                # counts by type
                cur.execute("""
                    SELECT event_type, COUNT(*)::int as c
                    FROM ticket_events
                    WHERE created_at BETWEEN %s AND %s
                    GROUP BY event_type
                """, (start_at, end_at))
                for row in cur.fetchall():
                    counts_by_type[row.get('event_type')] = int(row.get('c') or 0)

                # timeline by type
                cur.execute("""
                    SELECT to_char(created_at::date, 'YYYY-MM-DD') as d, event_type, COUNT(*)::int as c
                    FROM ticket_events
                    WHERE created_at BETWEEN %s AND %s
                    GROUP BY created_at::date, event_type
                    ORDER BY created_at::date ASC
                """, (start_at, end_at))
                for row in cur.fetchall():
                    d = row.get('d')
                    et = row.get('event_type')
                    c = int(row.get('c') or 0)
                    if d not in timeline_map:
                        timeline_map[d] = {'date': d, 'total': 0, 'helped': 0, 'not_helped': 0}
                    if et in base_events:
                        timeline_map[d]['total'] += c
                    if et in helped_events:
                        timeline_map[d]['helped'] += c
                    if et in not_helped_events:
                        timeline_map[d]['not_helped'] += c

                # group by problem/event_type
                cur.execute("""
                    SELECT COALESCE(NULLIF(TRIM(problem), ''), 'Не указано') as problem,
                           event_type,
                           COUNT(*)::int as c
                    FROM ticket_events
                    WHERE created_at BETWEEN %s AND %s
                    GROUP BY problem, event_type
                """, (start_at, end_at))
                for row in cur.fetchall():
                    p = row.get('problem') or 'Не указано'
                    et = row.get('event_type')
                    c = int(row.get('c') or 0)
                    total_inc = c if et in base_events else 0
                    helped_inc = c if et in helped_events else 0
                    not_helped_inc = c if et in not_helped_events else 0
                    if total_inc or helped_inc or not_helped_inc:
                        _bump_group(problems_map, p, total_inc, helped_inc, not_helped_inc, 'problem')

                # group by department/event_type
                cur.execute("""
                    SELECT COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                           event_type,
                           COUNT(*)::int as c
                    FROM ticket_events
                    WHERE created_at BETWEEN %s AND %s
                    GROUP BY department, event_type
                """, (start_at, end_at))
                for row in cur.fetchall():
                    dep = row.get('department') or 'Не указан'
                    et = row.get('event_type')
                    c = int(row.get('c') or 0)
                    total_inc = c if et in base_events else 0
                    helped_inc = c if et in helped_events else 0
                    not_helped_inc = c if et in not_helped_events else 0
                    if total_inc or helped_inc or not_helped_inc:
                        if dep not in departments_map:
                            departments_map[dep] = {'department': dep, 'total': 0, 'helped': 0, 'not_helped': 0}
                        departments_map[dep]['total'] += total_inc
                        departments_map[dep]['helped'] += helped_inc
                        departments_map[dep]['not_helped'] += not_helped_inc
    else:
        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT event_type, COUNT(*) as c
                FROM ticket_events
                WHERE created_at BETWEEN ? AND ?
                GROUP BY event_type
            """, (start_at, end_at))
            for row in cur.fetchall():
                counts_by_type[row['event_type']] = int(row['c'] or 0)

            cur.execute("""
                SELECT substr(created_at,1,10) as d, event_type, COUNT(*) as c
                FROM ticket_events
                WHERE created_at BETWEEN ? AND ?
                GROUP BY d, event_type
                ORDER BY d ASC
            """, (start_at, end_at))
            for row in cur.fetchall():
                d = row['d']
                et = row['event_type']
                c = int(row['c'] or 0)
                if d not in timeline_map:
                    timeline_map[d] = {'date': d, 'total': 0, 'helped': 0, 'not_helped': 0}
                if et in base_events:
                    timeline_map[d]['total'] += c
                if et in helped_events:
                    timeline_map[d]['helped'] += c
                if et in not_helped_events:
                    timeline_map[d]['not_helped'] += c

            cur.execute("""
                SELECT COALESCE(NULLIF(TRIM(problem), ''), 'Не указано') as problem,
                       event_type,
                       COUNT(*) as c
                FROM ticket_events
                WHERE created_at BETWEEN ? AND ?
                GROUP BY problem, event_type
            """, (start_at, end_at))
            for row in cur.fetchall():
                p = row['problem']
                et = row['event_type']
                c = int(row['c'] or 0)
                total_inc = c if et in base_events else 0
                helped_inc = c if et in helped_events else 0
                not_helped_inc = c if et in not_helped_events else 0
                if total_inc or helped_inc or not_helped_inc:
                    _bump_group(problems_map, p, total_inc, helped_inc, not_helped_inc, 'problem')

            cur.execute("""
                SELECT COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                       event_type,
                       COUNT(*) as c
                FROM ticket_events
                WHERE created_at BETWEEN ? AND ?
                GROUP BY department, event_type
            """, (start_at, end_at))
            for row in cur.fetchall():
                dep = row['department']
                et = row['event_type']
                c = int(row['c'] or 0)
                total_inc = c if et in base_events else 0
                helped_inc = c if et in helped_events else 0
                not_helped_inc = c if et in not_helped_events else 0
                if total_inc or helped_inc or not_helped_inc:
                    if dep not in departments_map:
                        departments_map[dep] = {'department': dep, 'total': 0, 'helped': 0, 'not_helped': 0}
                    departments_map[dep]['total'] += total_inc
                    departments_map[dep]['helped'] += helped_inc
                    departments_map[dep]['not_helped'] += not_helped_inc

    total_requests = sum(counts_by_type.get(et, 0) for et in base_events)
    helped_total = sum(counts_by_type.get(et, 0) for et in helped_events)
    not_helped_total = sum(counts_by_type.get(et, 0) for et in not_helped_events)
    rejected_total = int(counts_by_type.get('ticket_not_relevant', 0) or 0)

    summary = {
        'total': int(total_requests),
        'helped': int(helped_total),
        'not_helped': int(not_helped_total),
        'self_solved': int(helped_total),
        'rejected': rejected_total
    }

    timeline = [timeline_map[k] for k in sorted(timeline_map.keys())]
    top_problems = sorted(problems_map.values(), key=lambda x: x['count'], reverse=True)
    departments = sorted(departments_map.values(), key=lambda x: x['total'], reverse=True)

    return summary, timeline, top_problems, departments

@app.route('/admin/stats')
@AdminAuth.login_required
def admin_stats_dashboard():
    """Страница статистики с dashboard"""
    return render_template('admin_stats_dashboard.html')


@app.route('/api/stats/summary')
@AdminAuth.login_required
def api_stats_summary():
    """API для получения общей статистики"""
    try:
        start_at, end_at = _resolve_period_range()
        stats, _, _, _ = _load_ticket_dashboard_data(start_at, end_at)

        # Доп. breakdown по типам (видео/текст/тикеты/циско)
        breakdown = {
            # tickets_created - удобное поле для UI, вычисляется из ticket_created
            'tickets_created': 0,
            'tickets_created_cisco': 0,
            # Ниже ключи совпадают с event_type в ticket_events
            'ticket_created': 0,
            'manual_opened_video': 0,
            'manual_opened_text': 0,
            'video_helped': 0,
            'video_not_helped': 0,
            'manual_helped': 0,
            'manual_not_helped': 0,
            'ticket_not_relevant': 0,
            'ticket_resolved_by_staff': 0
        }

        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_type, COUNT(*)::int as c
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                        GROUP BY event_type
                    """, (start_at, end_at))
                    for row in cur.fetchall():
                        et = row.get('event_type')
                        if et in breakdown:
                            breakdown[et] = int(row.get('c') or 0)

                    # Совместимость со старым названием (если было)
                    cur.execute("""
                        SELECT COUNT(*)::int as c
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s AND event_type = 'ticket_solved_by_helper'
                    """, (start_at, end_at))
                    legacy_manual_helped = int((cur.fetchone() or {}).get('c') or 0)
                    breakdown['manual_helped'] += legacy_manual_helped

                    cur.execute("""
                        SELECT COUNT(*)::int as c
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s AND event_type = 'ticket_created' AND is_cisco = 1
                    """, (start_at, end_at))
                    breakdown['tickets_created_cisco'] = int((cur.fetchone() or {}).get('c') or 0)
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT event_type, COUNT(*) as c
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ?
                    GROUP BY event_type
                """, (start_at, end_at))
                for row in cur.fetchall():
                    et = row['event_type']
                    if et in breakdown:
                        breakdown[et] = int(row['c'] or 0)
                cur.execute("""
                    SELECT COUNT(*) as c
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ? AND event_type = 'ticket_solved_by_helper'
                """, (start_at, end_at))
                breakdown['manual_helped'] += int(cur.fetchone()['c'] or 0)
                cur.execute("""
                    SELECT COUNT(*) as c
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ? AND event_type = 'ticket_created' AND is_cisco = 1
                """, (start_at, end_at))
                breakdown['tickets_created_cisco'] = int(cur.fetchone()['c'] or 0)

        # Заполняем удобное поле для UI
        breakdown['tickets_created'] = int(breakdown.get('ticket_created') or 0)

        stats['breakdown'] = breakdown
        return jsonify({
            'success': True,
            'data': stats
        })
    except Exception as e:
        print(f"[api_stats_summary] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Ошибка получения статистики'
        }), 500


@app.route('/api/stats/top_problems')
@AdminAuth.login_required
def api_stats_top_problems():
    """API для получения топ проблем"""
    try:
        limit = request.args.get('limit', 10, type=int)
        limit = max(1, min(limit, 50))
        start_at, end_at = _resolve_period_range()
        _, _, problems, _ = _load_ticket_dashboard_data(start_at, end_at)
        return jsonify({
            'success': True,
            'data': problems[:limit]
        })
    except Exception as e:
        print(f"[api_stats_top_problems] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Ошибка получения топ проблем'
        }), 500


@app.route('/api/stats/departments')
@AdminAuth.login_required
def api_stats_departments():
    """API для получения статистики по отделам"""
    try:
        start_at, end_at = _resolve_period_range()
        _, _, _, departments = _load_ticket_dashboard_data(start_at, end_at)
        return jsonify({
            'success': True,
            'data': departments
        })
    except Exception as e:
        print(f"[api_stats_departments] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Ошибка получения статистики по отделам'
        }), 500


@app.route('/api/stats/timeline')
@AdminAuth.login_required
def api_stats_timeline():
    """API для получения статистики по дням (для графика)"""
    try:
        start_at, end_at = _resolve_period_range()
        _, timeline, _, _ = _load_ticket_dashboard_data(start_at, end_at)
        return jsonify({
            'success': True,
            'data': timeline
        })
    except Exception as e:
        print(f"[api_stats_timeline] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Ошибка получения timeline'
        }), 500


@app.route('/api/stats/staff')
@AdminAuth.login_required
def api_stats_staff():
    """Статистика по специалистам техподдержки."""
    try:
        start_at, end_at = _resolve_period_range()
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT actor_name, event_type, COUNT(*) as cnt
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN ('ticket_resolved_by_staff', 'ticket_not_relevant')
                        GROUP BY actor_name, event_type
                    """, (start_at, end_at))
                    rows = cur.fetchall()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT actor_name, event_type, COUNT(*) as cnt
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ?
                      AND event_type IN ('ticket_resolved_by_staff', 'ticket_not_relevant')
                    GROUP BY actor_name, event_type
                """, (start_at, end_at))
                rows = cur.fetchall()

        staff_map = {}
        for row in rows:
            name = row['actor_name'] or 'Неизвестно'
            if name not in staff_map:
                staff_map[name] = {'staff': name, 'resolved': 0, 'not_relevant': 0, 'total_actions': 0}
            if row['event_type'] == 'ticket_resolved_by_staff':
                staff_map[name]['resolved'] += row['cnt']
            elif row['event_type'] == 'ticket_not_relevant':
                staff_map[name]['not_relevant'] += row['cnt']
            staff_map[name]['total_actions'] += row['cnt']

        data = sorted(staff_map.values(), key=lambda x: x['total_actions'], reverse=True)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        print(f"[api_stats_staff] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статистики по специалистам'}), 500


@app.route('/api/stats/topics/summary')
@AdminAuth.login_required
def api_stats_topics_summary():
    """Сводная статистика по поискам тематик."""
    try:
        start_at, end_at = _resolve_period_range()
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            COUNT(*) as total_searches,
                            COUNT(DISTINCT CASE WHEN TRIM(COALESCE(query_text, '')) != '' THEN query_text END) as unique_queries,
                            AVG(results_count) as avg_results
                        FROM topic_search_events
                        WHERE created_at BETWEEN %s AND %s
                    """, (start_at, end_at))
                    row = cur.fetchone()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        COUNT(*) as total_searches,
                        COUNT(DISTINCT CASE WHEN TRIM(COALESCE(query_text, '')) != '' THEN query_text END) as unique_queries,
                        AVG(results_count) as avg_results
                    FROM topic_search_events
                    WHERE created_at BETWEEN ? AND ?
                """, (start_at, end_at))
                row = cur.fetchone()

        data = {
            'total_searches': int((row['total_searches'] or 0) if row else 0),
            'unique_queries': int((row['unique_queries'] or 0) if row else 0),
            'avg_results': round(float((row['avg_results'] or 0.0) if row else 0.0), 2)
        }
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        print(f"[api_stats_topics_summary] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статистики по тематикам'}), 500


@app.route('/api/stats/topics/top')
@AdminAuth.login_required
def api_stats_topics_top():
    """Топ поисковых запросов по тематикам."""
    try:
        start_at, end_at = _resolve_period_range()
        limit = max(1, min(request.args.get('limit', 10, type=int), 50))
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT query_text as query, COUNT(*) as count
                        FROM topic_search_events
                        WHERE created_at BETWEEN %s AND %s
                          AND TRIM(COALESCE(query_text, '')) != ''
                        GROUP BY query_text
                        ORDER BY count DESC
                        LIMIT %s
                    """, (start_at, end_at, limit))
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT query_text as query, COUNT(*) as count
                    FROM topic_search_events
                    WHERE created_at BETWEEN ? AND ?
                      AND TRIM(COALESCE(query_text, '')) != ''
                    GROUP BY query_text
                    ORDER BY count DESC
                    LIMIT ?
                """, (start_at, end_at, limit))
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        print(f"[api_stats_topics_top] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения топа поисков'}), 500


@app.route('/api/stats/topics/channels')
@AdminAuth.login_required
def api_stats_topics_channels():
    """Топ каналов по количеству поисков тематик."""
    try:
        start_at, end_at = _resolve_period_range()
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            CASE WHEN TRIM(COALESCE(channel, '')) = '' THEN 'Без канала' ELSE channel END as channel,
                            COUNT(*) as count
                        FROM topic_search_events
                        WHERE created_at BETWEEN %s AND %s
                        GROUP BY channel
                        ORDER BY count DESC
                        LIMIT 20
                    """, (start_at, end_at))
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        CASE WHEN TRIM(COALESCE(channel, '')) = '' THEN 'Без канала' ELSE channel END as channel,
                        COUNT(*) as count
                    FROM topic_search_events
                    WHERE created_at BETWEEN ? AND ?
                    GROUP BY channel
                    ORDER BY count DESC
                    LIMIT 20
                """, (start_at, end_at))
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        print(f"[api_stats_topics_channels] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статистики по каналам'}), 500


@app.route('/api/stats/topics/history')
@AdminAuth.login_required
def api_stats_topics_history():
    """История изменений тематик: добавления/удаления/импорты."""
    try:
        start_at, end_at = _resolve_period_range()
        limit = max(1, min(request.args.get('limit', 200, type=int), 1000))
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT created_at::text as created_at, action, topic_id, channel, full_topic, actor_name, actor_username, details_json
                        FROM topic_changes
                        WHERE created_at BETWEEN %s AND %s
                        ORDER BY id DESC
                        LIMIT %s
                    """, (start_at, end_at, limit))
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT created_at, action, topic_id, channel, full_topic, actor_name, actor_username, details_json
                    FROM topic_changes
                    WHERE created_at BETWEEN ? AND ?
                    ORDER BY id DESC
                    LIMIT ?
                """, (start_at, end_at, limit))
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        print(f"[api_stats_topics_history] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения истории тематик'}), 500


# ============================================
# УПРАВЛЕНИЕ УЧЕТНЫМИ ЗАПИСЯМИ АДМИНИСТРАТОРОВ
# ============================================

@app.route('/admin/users')
@AdminAuth.super_admin_required
def admin_users():
    """Список всех администраторов (только для супер-админа)"""
    admins = admins_manager.load_admins()
    return render_template('admin_users.html', admins=admins, role_names=ROLE_NAMES)


@app.route('/admin/users/add', methods=['GET', 'POST'])
@AdminAuth.super_admin_required
def admin_add_user():
    """Добавление нового администратора"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        role = request.form.get('role', ROLE_EDITOR)

        # Валидация
        if not username or not password:
            flash('Логин и пароль обязательны для заполнения')
            return redirect(url_for('admin_add_user'))

        if password != password_confirm:
            flash('Пароли не совпадают')
            return redirect(url_for('admin_add_user'))

        # Создаем администратора
        created_by = session.get('admin_username', 'system')
        result = admins_manager.create_admin(username, password, role, created_by)

        if result['success']:
            flash(f'Администратор {username} успешно создан')
            return redirect(url_for('admin_users'))
        else:
            flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    return render_template('admin_add_user.html', roles={'super_admin': ROLE_SUPER_ADMIN, 'editor': ROLE_EDITOR}, role_names=ROLE_NAMES)


@app.route('/admin/users/<string:username>/change_password', methods=['GET', 'POST'])
@AdminAuth.super_admin_required
def admin_change_user_password(username):
    """Изменение пароля администратора"""
    admin = admins_manager.get_admin_by_username(username)
    if not admin:
        flash('Администратор не найден')
        return redirect(url_for('admin_users'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        password_confirm = request.form.get('password_confirm', '')

        if not new_password:
            flash('Новый пароль обязателен для заполнения')
            return redirect(url_for('admin_change_user_password', username=username))

        if new_password != password_confirm:
            flash('Пароли не совпадают')
            return redirect(url_for('admin_change_user_password', username=username))

        result = admins_manager.update_admin_password(username, new_password)

        if result['success']:
            flash(f'Пароль для {username} успешно изменен')
            return redirect(url_for('admin_users'))
        else:
            flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    return render_template('admin_change_password.html', admin=admin)


@app.route('/admin/users/<string:username>/change_role', methods=['POST'])
@AdminAuth.super_admin_required
def admin_change_user_role(username):
    """Изменение роли администратора"""
    new_role = request.form.get('role', '')

    if not new_role:
        flash('Роль обязательна для заполнения')
        return redirect(url_for('admin_users'))

    result = admins_manager.change_admin_role(username, new_role)

    if result['success']:
        flash(f'Роль для {username} успешно изменена на {ROLE_NAMES.get(new_role, new_role)}')
    else:
        flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    return redirect(url_for('admin_users'))


@app.route('/admin/users/<string:username>/delete', methods=['POST'])
@AdminAuth.super_admin_required
def admin_delete_user(username):
    """Удаление администратора"""
    # Защита от удаления самого себя
    current_username = session.get('admin_username')
    if username == current_username:
        flash('Нельзя удалить самого себя')
        return redirect(url_for('admin_users'))

    result = admins_manager.delete_admin(username)

    if result['success']:
        flash(f'Администратор {username} успешно удален')
    else:
        flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    return redirect(url_for('admin_users'))


# --- Запуск ---
def run_flask():
    # Security: debug=False in production, host binding from env
    flask_host = os.getenv('FLASK_HOST', '0.0.0.0')
    flask_port = int(os.getenv('FLASK_PORT', '5003'))
    flask_debug = IS_DEVELOPMENT  # Debug mode enabled in development
    # use_reloader=False because Flask runs in a thread and reloader doesn't work in threads
    app.run(host=flask_host, port=flask_port, debug=flask_debug, use_reloader=False)

def run_bot():
    print("🤖 Telegram бот запущен и слушает обновления...")
    print("🔍 Ожидание callback запросов от кнопок...")
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        print(f"❌ Ошибка в bot polling: {e}")
        traceback.print_exc()


if __name__ == '__main__':
    print("=" * 60)
    print("Запуск приложения Helper Bot")
    print("=" * 60)
    # Security Fix: Do not log any information about tokens
    print("Bot Token: ***REDACTED***")
    print(f"Tech Support Chat ID: {TECH_SUPPORT_CHAT_ID}")
    print(f"Flask будет доступен на: http://0.0.0.0:5003")
    print(f"Telegram bot handlers: {len(bot.message_handlers)} message handlers")
    print(f"Callback handlers: {len(bot.callback_query_handlers)} callback handlers")
    print("=" * 60)

    flask_thread = threading.Thread(target=run_flask)
    bot_thread = threading.Thread(target=run_bot)
    flask_thread.start()
    bot_thread.start()
