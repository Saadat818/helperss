import os
import random
import sqlite3
import threading
import json
import logging
import ipaddress
from logging.handlers import RotatingFileHandler
from typing import Any
from datetime import datetime, timedelta
from markupsafe import escape as m_escape
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from flask import Flask, render_template, request, session, redirect, url_for, flash, abort, jsonify, g, has_request_context, Response
from dotenv import load_dotenv
import telebot
import werkzeug.routing
import traceback
import re
import uuid
from html import escape as html_escape
from functools import wraps
from time import time, sleep, process_time
from collections import defaultdict
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        REGISTRY,
        generate_latest,
    )
    PROMETHEUS_CLIENT_AVAILABLE = True
except Exception:
    CONTENT_TYPE_LATEST = 'text/plain; version=0.0.4; charset=utf-8'
    Counter = Gauge = Histogram = None
    REGISTRY = None
    generate_latest = None
    PROMETHEUS_CLIENT_AVAILABLE = False

# Загружаем переменные окружения ПЕРЕД импортом admin_manager
load_dotenv()

# Абсолютный путь к директории приложения — нужен для корректной работы на сервере
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================
# АУДИТ-ЛОГ В ФАЙЛ (RotatingFileHandler)
# ============================================
AUDIT_LOG_FILE = os.getenv('AUDIT_LOG_FILE', 'audit.log')
AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 МБ максимум на файл
AUDIT_LOG_BACKUP_COUNT = 5  # Хранить 5 ротированных файлов (итого ~50 МБ макс)

_audit_file_logger = logging.getLogger('audit_file')
_audit_file_logger.setLevel(logging.INFO)
_audit_file_logger.propagate = False  # Не дублировать в stdout

_audit_handler = RotatingFileHandler(
    AUDIT_LOG_FILE,
    maxBytes=AUDIT_LOG_MAX_BYTES,
    backupCount=AUDIT_LOG_BACKUP_COUNT,
    encoding='utf-8'
)
_audit_handler.setFormatter(logging.Formatter('%(message)s'))
_audit_file_logger.addHandler(_audit_handler)

# Ограничиваем права: только владелец читает/пишет (защита от утечки логов)
try:
    os.chmod(AUDIT_LOG_FILE, 0o600)
except OSError:
    pass

from flask_wtf.csrf import CSRFProtect
from admin_manager import admin_manager, AdminAuth, admins_manager, ROLE_SUPER_ADMIN, ROLE_EDITOR, ROLE_NAMES, ROLE_ADMIN_MANUALS, ROLE_ADMIN_TOPICS, ROLE_ADMIN_SCENARIOS, ROLE_ADMIN_TRAINER, ROLE_TRAINER_VIEWER, ALL_ADMIN_ROLES
from topics_manager import TopicsManager
from stats_manager import StatsManager
from trainer_manager import TrainerManager
from scenario_manager import ScenarioManager
from contacts_manager import ContactsManager
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

    def check_login_attempt(self, ip: str, max_attempts: int = 10, window: int = 300) -> bool:
        """Check login attempts (stricter limit)"""
        now = time()
        self.login_attempts[ip] = [req_time for req_time in self.login_attempts[ip]
                                   if now - req_time < window]
        if len(self.login_attempts[ip]) >= max_attempts:
            return False
        self.login_attempts[ip].append(now)
        return True

    def reset_login_attempts(self, ip: str) -> None:
        """Сбросить блокировку для конкретного IP"""
        self.login_attempts[ip] = []

    def reset_all_login_attempts(self) -> None:
        """Сбросить все блокировки"""
        self.login_attempts.clear()

rate_limiter = RateLimiter()
ticket_counter_lock = threading.Lock()
audit_log_lock = threading.Lock()

PROMETHEUS_METRICS_ENABLED = os.getenv('PROMETHEUS_METRICS_ENABLED', 'true').lower() not in ('0', 'false', 'no', 'off')
PROMETHEUS_SERVICE_NAME = os.getenv('PROMETHEUS_SERVICE_NAME', 'helper').strip() or 'helper'
PROMETHEUS_ENV = os.getenv('PROMETHEUS_ENV', os.getenv('FLASK_ENV', 'prod')).strip() or 'prod'
PROMETHEUS_METRICS_TOKEN = os.getenv('PROMETHEUS_METRICS_TOKEN', '').strip()
PROMETHEUS_METRICS_ALLOWED_IPS = os.getenv(
    'PROMETHEUS_METRICS_ALLOWED_IPS',
    '127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16'
)


class _NoopMetric:
    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def dec(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None

    def observe(self, *args, **kwargs):
        return None


def _metric_name_exists(name: str) -> bool:
    if not (PROMETHEUS_CLIENT_AVAILABLE and REGISTRY):
        return False
    names = getattr(REGISTRY, '_names_to_collectors', {})
    if name in names:
        return True
    if name.endswith('_total') and name[:-6] in names:
        return True
    return False


def _new_metric(metric_cls, name: str, *args, **kwargs):
    if not (PROMETHEUS_METRICS_ENABLED and PROMETHEUS_CLIENT_AVAILABLE and metric_cls):
        return _NoopMetric()
    if _metric_name_exists(name):
        return _NoopMetric()
    return metric_cls(name, *args, **kwargs)


HTTP_REQUESTS_TOTAL = _new_metric(
    Counter,
    'http_requests_total',
    'Total HTTP requests handled by Helper.',
    ['service', 'env', 'method', 'route', 'status']
)
HTTP_REQUEST_DURATION_SECONDS = _new_metric(
    Histogram,
    'http_request_duration_seconds',
    'HTTP request duration in seconds.',
    ['service', 'env', 'method', 'route', 'status'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
)
HTTP_REQUESTS_IN_PROGRESS = _new_metric(
    Gauge,
    'http_requests_in_progress',
    'HTTP requests currently being processed.',
    ['service', 'env', 'method', 'route']
)
HTTP_ERRORS_TOTAL = _new_metric(
    Counter,
    'http_errors_total',
    'HTTP 5xx errors returned by Helper.',
    ['service', 'env', 'method', 'route', 'status']
)
BOT_UPDATES_TOTAL = _new_metric(
    Counter,
    'bot_updates_total',
    'Telegram bot updates received.',
    ['service', 'env', 'update_type']
)
BOT_MESSAGES_TOTAL = _new_metric(
    Counter,
    'bot_messages_total',
    'Telegram bot messages by direction and kind.',
    ['service', 'env', 'direction', 'kind']
)
BOT_ERRORS_TOTAL = _new_metric(
    Counter,
    'bot_errors_total',
    'Telegram bot processing errors.',
    ['service', 'env', 'handler', 'error_type']
)
BOT_HANDLER_DURATION_SECONDS = _new_metric(
    Histogram,
    'bot_handler_duration_seconds',
    'Telegram bot update processing duration in seconds.',
    ['service', 'env', 'handler'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
)
BOT_EXTERNAL_API_ERRORS_TOTAL = _new_metric(
    Counter,
    'bot_external_api_errors_total',
    'Telegram API errors raised by bot methods.',
    ['service', 'env', 'method', 'error_type']
)
BOT_LAST_UPDATE_TIMESTAMP = _new_metric(
    Gauge,
    'bot_last_update_timestamp',
    'Unix timestamp of the last Telegram update processed.',
    ['service', 'env']
)
BOT_POLLING_UP = _new_metric(
    Gauge,
    'bot_polling_up',
    'Whether Telegram polling loop is running.',
    ['service', 'env']
)
BOT_POLLING_ERRORS_TOTAL = _new_metric(
    Counter,
    'bot_polling_errors_total',
    'Telegram polling loop errors.',
    ['service', 'env', 'error_type']
)
PROCESS_CPU_SECONDS_TOTAL = _new_metric(
    Gauge,
    'process_cpu_seconds_total',
    'Total user and system CPU time spent by the Helper process.',
)
PROCESS_RESIDENT_MEMORY_BYTES = _new_metric(
    Gauge,
    'process_resident_memory_bytes',
    'Resident memory size in bytes for the Helper process.',
)


def _metric_base_labels() -> tuple[str, str]:
    return PROMETHEUS_SERVICE_NAME, PROMETHEUS_ENV


def _request_metric_route() -> str:
    if request.url_rule and request.url_rule.rule:
        return request.url_rule.rule
    if request.path.startswith('/static/'):
        return '/static/<path>'
    return 'unmatched'


def _skip_request_metrics() -> bool:
    return (
        not PROMETHEUS_METRICS_ENABLED
        or request.path == '/metrics'
        or request.path.startswith('/static/')
    )


def _metrics_allowed_networks():
    networks = []
    for item in PROMETHEUS_METRICS_ALLOWED_IPS.split(','):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            pass
    return networks


def _metrics_request_allowed() -> bool:
    if not PROMETHEUS_METRICS_ENABLED:
        return False

    if PROMETHEUS_METRICS_TOKEN:
        auth_header = request.headers.get('Authorization', '')
        bearer = f'Bearer {PROMETHEUS_METRICS_TOKEN}'
        if auth_header == bearer or request.args.get('token') == PROMETHEUS_METRICS_TOKEN:
            return True

    remote_addr = request.remote_addr or ''
    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(remote_ip in network for network in _metrics_allowed_networks())


def _current_process_rss_bytes() -> int:
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ('cb', wintypes.DWORD),
                    ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(ProcessMemoryCounters)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            ok = psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb
            )
            return int(counters.WorkingSetSize) if ok else 0
        except Exception:
            return 0

    try:
        with open('/proc/self/statm', 'r', encoding='utf-8') as statm:
            parts = statm.read().split()
        resident_pages = int(parts[1])
        return resident_pages * os.sysconf('SC_PAGE_SIZE')
    except Exception:
        return 0


def _refresh_process_metrics():
    PROCESS_CPU_SECONDS_TOTAL.set(process_time())
    rss_bytes = _current_process_rss_bytes()
    if rss_bytes:
        PROCESS_RESIDENT_MEMORY_BYTES.set(rss_bytes)

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
IS_LOCAL_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
default_secure_cookie = 'false' if (IS_DEVELOPMENT or IS_LOCAL_TEST_MODE) else 'true'
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
    if request.is_secure or not (IS_DEVELOPMENT or IS_LOCAL_TEST_MODE):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    # Security Fix: Improved CSP - consider removing unsafe-inline in future iterations
    # TODO: Remove unsafe-inline by using nonces or hashes for inline scripts/styles
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://api.telegram.org data: blob:; "
        "media-src 'self' https://api.telegram.org; "
        "font-src 'self'; "
        "frame-ancestors 'none'; "  # Changed from 'self' to 'none'
        "base-uri 'self'; "  # Added base-uri restriction
        "form-action 'self';"  # Added form-action restriction
    )

    # Security Fix: Add additional security headers
    response.headers['X-Permitted-Cross-Domain-Policies'] = 'none'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'

    # Inject favicon в HTML ответы
    if response.content_type and 'text/html' in response.content_type:
        try:
            data = response.get_data(as_text=True)
            if '<head>' in data and 'favicon' not in data:
                favicon_tag = '<link rel="icon" type="image/svg+xml" href="/static/images/helper_logo.svg">'
                data = data.replace('<head>', '<head>' + favicon_tag, 1)
                response.set_data(data)
        except Exception:
            pass

    return response


def safe_redirect(fallback_endpoint='index'):
    """
    Безопасный redirect по request.referrer.
    Проверяет что referrer ведёт на тот же хост (защита от Open Redirect).
    Если referrer невалидный — редирект на fallback_endpoint.
    """
    referrer = request.referrer
    if referrer:
        parsed = urlparse(referrer)
        # Проверяем что referrer ведёт на наш хост
        if parsed.netloc == '' or parsed.netloc == request.host:
            return redirect(referrer)
    return redirect(url_for(fallback_endpoint))


def validated_redirect(url, fallback_endpoint='admin_dashboard'):
    """
    Безопасный redirect по URL — защита от Open Redirect.
    Проверяет что URL является внутренним (относительным) путём.
    """
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return redirect(url_for(fallback_endpoint))
    return redirect(url)


def is_working_hours(now: datetime | None = None):
    """Проверяет, можно ли сейчас отправлять заявки в техподдержку.
    Возвращает (True, '') если рабочее время, иначе (False, сообщение).
    """
    now = now or datetime.now()
    settings = _get_ticket_schedule_settings()
    today = now.strftime('%Y-%m-%d')
    current_time = now.time()

    holiday = next((item for item in settings['holidays'] if item.get('date') == today), None)
    if holiday:
        name = holiday.get('name') or 'праздничный день'
        return False, (
            f'Сегодня нерабочий день: {name}. '
            'Заявки в техподдержку не принимаются. '
            'Вы по-прежнему можете пользоваться инструкциями, видео и тренажёром.'
        )

    day_override = next((item for item in settings['workday_overrides'] if item.get('date') == today), None)
    is_workday = bool(day_override) or now.weekday() in settings['workdays']
    if not is_workday:
        return False, 'Сегодня нерабочий день. Сотрудники ОПО не на рабочем месте. Просьба постараться решить проблему самостоятельно с помощью инструкций.'

    work_start = _parse_schedule_time((day_override or {}).get('start') or settings['work_start'], '08:30')
    work_end = _parse_schedule_time((day_override or {}).get('end') or settings['work_end'], '17:30')
    if current_time < work_start or current_time > work_end:
        return False, 'Сейчас нерабочее время. Просьба постараться решить проблему самостоятельно с помощью инструкций.'

    lunch_enabled = settings['lunch_enabled']
    if day_override and 'lunch_enabled' in day_override:
        lunch_enabled = bool(day_override.get('lunch_enabled'))
    if lunch_enabled:
        lunch_start = _parse_schedule_time((day_override or {}).get('lunch_start') or settings['lunch_start'], '12:00')
        lunch_end = _parse_schedule_time((day_override or {}).get('lunch_end') or settings['lunch_end'], '13:00')
        if lunch_start <= current_time < lunch_end:
            return False, (
                f'Сейчас обеденный перерыв ОПО ({lunch_start.strftime("%H:%M")}–{lunch_end.strftime("%H:%M")}). '
                'Просьба воспользоваться инструкциями или отправить заявку после перерыва.'
            )
    return True, ''


# ============================================
# ТРЕКИНГ ОНЛАЙН-ПОЛЬЗОВАТЕЛЕЙ
# ============================================
# Словарь: { "username_or_ip": { "last_seen": timestamp, "username": str, "ip": str, "path": str } }
import threading as _thr
_online_users = {}
_online_lock = _thr.Lock()
_ONLINE_TIMEOUT = 300  # 5 минут — считаем пользователя онлайн


def _track_user_activity():
    """Обновляет информацию об активности текущего пользователя."""
    try:
        if request.path.startswith('/static/') or request.path == '/metrics':
            return

        ip = get_client_ip()
        username = ''
        display_name = ''

        if session.get('admin_logged_in'):
            username = session.get('admin_username', '')
            display_name = username
        elif session.get('authenticated') and session.get('user_info'):
            user_info = session.get('user_info', {})
            username = user_info.get('username', '')
            display_name = user_info.get('name', username)

        # Ключ — username если залогинен, иначе IP
        key = username if username else f"guest_{ip}"

        with _online_lock:
            _online_users[key] = {
                'last_seen': time(),
                'username': username,
                'display_name': display_name,
                'ip': ip,
                'path': request.path,
                'is_admin': bool(session.get('admin_logged_in')),
            }

            # Чистим устаревших (старше 5 минут)
            now = time()
            stale_keys = [k for k, v in _online_users.items() if now - v['last_seen'] > _ONLINE_TIMEOUT]
            for k in stale_keys:
                del _online_users[k]
    except Exception:
        pass


def get_online_stats():
    """Возвращает статистику онлайн-пользователей."""
    now = time()
    with _online_lock:
        active = {k: v for k, v in _online_users.items() if now - v['last_seen'] <= _ONLINE_TIMEOUT}

    users_list = []
    total_online = 0
    guests = 0
    logged_in = 0
    admins_online = 0

    for key, info in active.items():
        total_online += 1
        if info['username']:
            logged_in += 1
            if info['is_admin']:
                admins_online += 1
        else:
            guests += 1

        users_list.append({
            'key': key,
            'username': info['username'] or 'Гость',
            'display_name': info['display_name'] or 'Гость',
            'ip': info['ip'],
            'path': info['path'],
            'is_admin': info['is_admin'],
            'seconds_ago': int(now - info['last_seen']),
        })

    # Сортируем: сначала последние активные
    users_list.sort(key=lambda x: x['seconds_ago'])

    return {
        'total_online': total_online,
        'logged_in': logged_in,
        'guests': guests,
        'admins_online': admins_online,
        'users': users_list,
    }


@app.before_request
def mark_request_start():
    """Отмечает старт запроса и трекает активность пользователя."""
    g.request_started_at = time()
    g.prometheus_skip = _skip_request_metrics()
    if not g.prometheus_skip:
        route = _request_metric_route()
        g.prometheus_route = route
        HTTP_REQUESTS_IN_PROGRESS.labels(
            *_metric_base_labels(),
            request.method,
            route
        ).inc()
    _track_user_activity()


TRAINER_MAINTENANCE = os.getenv('TRAINER_MAINTENANCE', 'false').lower() == 'true'

@app.before_request
def trainer_maintenance_check():
    """Заглушка тренажёра — режим 'В разработке'. Админы тренажёра проходят."""
    if TRAINER_MAINTENANCE and request.path.startswith('/trainer') and not request.path.startswith('/static/'):
        perms = session.get('admin_permissions', [])
        if 'super_admin' in perms or 'admin_trainer' in perms:
            return None
        return render_template('trainer_maintenance.html'), 503


@app.after_request
def no_cache_protected(response):
    """Запрет кэширования админских и пользовательских страниц — защита от Alt+← после logout."""
    no_cache_paths = ('/admin', '/trainer', '/send_final_ticket', '/finish_solved',
                      '/finish_unsolved', '/success', '/select_problem', '/manual/',
                      '/choose_help_type', '/show_problems', '/login', '/enter_telegram_username')
    if any(request.path.startswith(p) for p in no_cache_paths):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.after_request
def audit_request(response):
    """Аудит всех действий пользователей/администраторов с IP и временем."""
    try:
        if request.path.startswith('/static/') or request.path == '/metrics':
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


@app.after_request
def record_prometheus_metrics(response):
    """Записывает Prometheus HTTP-метрики после обработки запроса."""
    if getattr(g, 'prometheus_skip', True):
        return response

    route = getattr(g, 'prometheus_route', _request_metric_route())
    method = request.method
    status = str(response.status_code)
    duration = max(0.0, time() - getattr(g, 'request_started_at', time()))
    base_labels = _metric_base_labels()
    HTTP_REQUESTS_TOTAL.labels(*base_labels, method, route, status).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(*base_labels, method, route, status).observe(duration)
    if response.status_code >= 500:
        HTTP_ERRORS_TOTAL.labels(*base_labels, method, route, status).inc()
    HTTP_REQUESTS_IN_PROGRESS.labels(*base_labels, method, route).dec()
    return response


@app.route('/metrics')
def prometheus_metrics():
    """Prometheus scrape endpoint."""
    if not _metrics_request_allowed():
        abort(404)
    if not PROMETHEUS_CLIENT_AVAILABLE:
        return Response(
            'prometheus_client is not installed. Install requirements.txt dependencies.\n',
            status=503,
            mimetype='text/plain'
        )
    _refresh_process_metrics()
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)

APP_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('TEST_BOT_TOKEN') if APP_TEST_MODE and os.getenv('TEST_BOT_TOKEN') else os.getenv('BOT_TOKEN')
TELEGRAM_FILE_LOOKUP_ENABLED = os.getenv(
    'TELEGRAM_FILE_LOOKUP_ENABLED',
    'false' if APP_TEST_MODE else 'true'
).lower() not in ('0', 'false', 'no', 'off')
TELEGRAM_PROXY_URL = (
    os.getenv('TELEGRAM_PROXY_URL')
    or os.getenv('HTTPS_PROXY')
    or os.getenv('https_proxy')
    or os.getenv('HTTP_PROXY')
    or os.getenv('http_proxy')
    or ''
).strip()
TRUSTED_PROXY_IP = os.getenv("TRUSTED_PROXY_IP")
TICKET_COUNTER_DB_PATH = os.getenv('TICKET_COUNTER_DB', os.path.join(BASE_DIR, 'topics.db'))
TICKET_NUMBER_START = int(os.getenv('TICKET_NUMBER_START', '125'))
AUDIT_LOG_DB_PATH = os.getenv('AUDIT_LOG_DB', os.path.join(BASE_DIR, 'topics.db'))
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


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip() or default)
    except (TypeError, ValueError):
        return default


CURRENT_DUTY_TELEGRAM_ID = _env_int('CURRENT_DUTY_TELEGRAM_ID', 0)
CURRENT_DUTY_USERNAME = os.getenv('CURRENT_DUTY_USERNAME', '').strip().lstrip('@')
CURRENT_DUTY_NAME = os.getenv('CURRENT_DUTY_NAME', '').strip()
OVERLOAD_TICKET_LIMIT = max(1, _env_int('OVERLOAD_TICKET_LIMIT', 5))
OVERLOAD_ALERT_THREAD_ID = _env_int('OVERLOAD_ALERT_THREAD_ID', 0)
USER_FEEDBACK_TIMEOUT_SECONDS = max(60, _env_int('USER_FEEDBACK_TIMEOUT_SECONDS', 300))

if not BOT_TOKEN:
    print("Ошибка: BOT_TOKEN не найден в переменных окружения. Пожалуйста, проверьте ваш .env файл.")
    exit()

if TELEGRAM_PROXY_URL:
    telebot.apihelper.proxy = {
        'http': TELEGRAM_PROXY_URL,
        'https': TELEGRAM_PROXY_URL
    }
    print("[telegram] Прокси для Telegram API настроен")

bot = telebot.TeleBot(BOT_TOKEN)


def _telegram_update_type(update) -> str:
    for attr in (
        'message',
        'edited_message',
        'callback_query',
        'channel_post',
        'edited_channel_post',
        'inline_query',
        'chosen_inline_result',
        'poll',
        'poll_answer',
        'my_chat_member',
        'chat_member',
    ):
        if getattr(update, attr, None) is not None:
            return attr
    return type(update).__name__


def _telegram_message_kind(update) -> str:
    message = getattr(update, 'message', None) or getattr(update, 'edited_message', None)
    if message is not None:
        return getattr(message, 'content_type', None) or 'message'
    if getattr(update, 'callback_query', None) is not None:
        return 'callback_query'
    return _telegram_update_type(update)


def _instrument_telegram_bot(bot_instance):
    """Добавляет базовые Prometheus-метрики вокруг pyTelegramBotAPI."""
    if getattr(bot_instance, '_helper_prometheus_instrumented', False):
        return
    bot_instance._helper_prometheus_instrumented = True

    original_process_updates = bot_instance.process_new_updates

    @wraps(original_process_updates)
    def monitored_process_new_updates(updates):
        start = time()
        base_labels = _metric_base_labels()
        try:
            for update in updates or []:
                update_type = _telegram_update_type(update)
                BOT_UPDATES_TOTAL.labels(*base_labels, update_type).inc()
                BOT_MESSAGES_TOTAL.labels(*base_labels, 'in', _telegram_message_kind(update)).inc()
                BOT_LAST_UPDATE_TIMESTAMP.labels(*base_labels).set(time())
            return original_process_updates(updates)
        except Exception as e:
            BOT_ERRORS_TOTAL.labels(*base_labels, 'process_new_updates', type(e).__name__).inc()
            raise
        finally:
            BOT_HANDLER_DURATION_SECONDS.labels(*base_labels, 'process_new_updates').observe(time() - start)

    bot_instance.process_new_updates = monitored_process_new_updates

    def wrap_api_method(method_name: str):
        if not hasattr(bot_instance, method_name):
            return
        original = getattr(bot_instance, method_name)

        @wraps(original)
        def monitored_api_method(*args, **kwargs):
            base_labels = _metric_base_labels()
            try:
                result = original(*args, **kwargs)
                if method_name.startswith('send_'):
                    BOT_MESSAGES_TOTAL.labels(*base_labels, 'out', method_name).inc()
                return result
            except Exception as e:
                error_type = type(e).__name__
                BOT_EXTERNAL_API_ERRORS_TOTAL.labels(*base_labels, method_name, error_type).inc()
                BOT_ERRORS_TOTAL.labels(*base_labels, method_name, error_type).inc()
                raise

        setattr(bot_instance, method_name, monitored_api_method)

    for api_method in (
        'send_message',
        'send_photo',
        'send_video',
        'send_media_group',
        'answer_callback_query',
        'edit_message_reply_markup',
        'edit_message_text',
    ):
        wrap_api_method(api_method)


_instrument_telegram_bot(bot)


def _sanitize_exception_text(text: str) -> str:
    safe = str(text or '')
    if BOT_TOKEN:
        safe = safe.replace(BOT_TOKEN, '<bot_token>')
    safe = re.sub(r'/bot[^/\s]+/', '/bot<bot_token>/', safe)
    safe = re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot<bot_token>', safe)
    return safe


def _log_exception_safely(context: str, error: Exception | None = None):
    text = traceback.format_exc()
    if not text or text.strip() == 'NoneType: None':
        text = str(error or '')
    print(f"[{context}] {_sanitize_exception_text(text)[:2500]}")


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
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_type ON ticket_events(event_type)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_number)")
                    cur.execute("ALTER TABLE ticket_events ADD COLUMN IF NOT EXISTS problem_id TEXT")
                    cur.execute("ALTER TABLE ticket_events ADD COLUMN IF NOT EXISTS subproblem_id TEXT")
                    cur.execute("ALTER TABLE ticket_events ADD COLUMN IF NOT EXISTS details_json JSONB")

                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS app_settings (
                            key TEXT PRIMARY KEY,
                            value TEXT,
                            updated_at TIMESTAMP,
                            updated_by TEXT
                        )
                    """)

                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS user_profiles (
                            username TEXT PRIMARY KEY,
                            telegram_username TEXT,
                            created_at TIMESTAMP,
                            updated_at TIMESTAMP
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_telegram ON user_profiles(telegram_username)")

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
                            department TEXT,
                            user_name TEXT,
                            workplace TEXT,
                            actor_name TEXT,
                            actor_username TEXT,
                            actor_role TEXT
                        )
                    """)
                    # Миграции (на случай если таблица уже была создана без новых колонок).
                    # Важно: сначала добавляем колонки, потом создаем индексы (иначе CREATE INDEX упадет).
                    cur.execute("ALTER TABLE topic_search_events ADD COLUMN IF NOT EXISTS department TEXT")
                    cur.execute("ALTER TABLE topic_search_events ADD COLUMN IF NOT EXISTS user_name TEXT")
                    cur.execute("ALTER TABLE topic_search_events ADD COLUMN IF NOT EXISTS workplace TEXT")

                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_created_at ON topic_search_events(created_at)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_channel ON topic_search_events(channel)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_department ON topic_search_events(department)")
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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_type ON ticket_events(event_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_number)")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT,
                        updated_by TEXT
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        username TEXT PRIMARY KEY,
                        telegram_username TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_telegram ON user_profiles(telegram_username)")

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
                        department TEXT,
                        user_name TEXT,
                        workplace TEXT,
                        actor_name TEXT,
                        actor_username TEXT,
                        actor_role TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_created_at ON topic_search_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_search_channel ON topic_search_events(channel)")

                # Миграции SQLite: добавляем недостающие колонки
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(ticket_events)")
                existing_ticket_cols = {row[1] for row in cur.fetchall()}
                for col_name, col_type in (
                    ("problem_id", "TEXT"),
                    ("subproblem_id", "TEXT"),
                    ("details_json", "TEXT"),
                ):
                    if col_name not in existing_ticket_cols:
                        cur.execute(f"ALTER TABLE ticket_events ADD COLUMN {col_name} {col_type}")

                cur.execute("PRAGMA table_info(topic_search_events)")
                existing_cols = {row[1] for row in cur.fetchall()}
                for col_name, col_type in (
                    ("department", "TEXT"),
                    ("user_name", "TEXT"),
                    ("workplace", "TEXT"),
                ):
                    if col_name not in existing_cols:
                        conn.execute(f"ALTER TABLE topic_search_events ADD COLUMN {col_name} {col_type}")
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

        # Дублируем в файл (без sensitive данных — details уже прошёл _sanitize_audit_payload)
        _audit_file_logger.info(
            "%s | %s | %-6s | %s | %s | %s | %s | %d",
            record['created_at'],
            record['ip_address'],
            record['actor_type'],
            record['actor_username'] or '-',
            record['method'],
            record['path'],
            record['action'],
            record['status_code']
        )
    except Exception as e:
        print(f"[audit_log] Ошибка записи: {e}")


def _current_actor():
    """Возвращает данные текущего пользователя/админа."""
    if not has_request_context():
        return {'name': '', 'username': '', 'role': 'guest'}
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


def _parse_ticket_text_fields(text: str) -> dict:
    """Best-effort парсинг полей из текста заявки (отдел/имя/рабочее место/проблема).

    Нужно для аналитики событий, которые приходят из Telegram (polling), где нет Flask session.
    """
    if not text:
        return {'department': '', 'name': '', 'workplace': '', 'problem': ''}
    # Убираем маркдаун-обрамление для упрощения регулярных выражений.
    t = str(text).replace('**', '')
    def _m(pat: str) -> str:
        m = re.search(pat, t, flags=re.IGNORECASE | re.MULTILINE)
        return (m.group(1).strip() if m else '')
    return {
        'department': _m(r'^\s*Отдел:\s*(.+?)\s*$'),
        'name': _m(r'^\s*Имя:\s*(.+?)\s*$'),
        'workplace': _m(r'^\s*Рабочее место:\s*(.+?)\s*$'),
        'problem': _m(r'^\s*Проблема:\s*(.+?)\s*$')
    }


def log_ticket_event(event_type: str, ticket_number: int | None = None, problem: str = '',
                     channel: str = '', topic_name: str = '', is_cisco: bool = False,
                     actor_override: dict | None = None, user_info_override: dict | None = None,
                     details: dict | None = None,
                     problem_id_override: str | None = None,
                     subproblem_id_override: str | None = None):
    """Логирует событие по заявке для аналитики."""
    try:
        actor = actor_override or _current_actor()
        if has_request_context():
            user_info = session.get('user_info', {}) or {}
        else:
            user_info = {}
        if user_info_override:
            # Override only known user_info keys to avoid unexpected payload.
            for k in ('department', 'name', 'workplace'):
                if k in user_info_override:
                    user_info[k] = user_info_override.get(k)
        problem_id = ''
        subproblem_id = ''
        if has_request_context():
            problem_id = str(session.get('problem_id', '') or '')[:100]
            subproblem_id = str(session.get('current_subproblem_id', '') or '')[:100]
        if problem_id_override is not None:
            problem_id = str(problem_id_override or '')[:100]
        if subproblem_id_override is not None:
            subproblem_id = str(subproblem_id_override or '')[:100]
        payload = (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            event_type,
            ticket_number,
            str(problem)[:500],
            problem_id,
            subproblem_id,
            str(user_info.get('department', ''))[:200],
            str(user_info.get('name', ''))[:200],
            str(user_info.get('workplace', ''))[:100],
            str(channel)[:150],
            str(topic_name)[:500],
            1 if is_cisco else 0,
            str(actor['name'])[:200],
            str(actor['username'])[:200],
            str(actor['role'])[:100],
            json.dumps(_sanitize_audit_payload(details or {}), ensure_ascii=False)
        )
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ticket_events (
                            created_at, event_type, ticket_number, problem, problem_id, subproblem_id,
                            department, user_name, workplace, channel, topic_name, is_cisco,
                            actor_name, actor_username, actor_role, details_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO ticket_events (
                        created_at, event_type, ticket_number, problem, problem_id, subproblem_id,
                        department, user_name, workplace, channel, topic_name, is_cisco,
                        actor_name, actor_username, actor_role, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, payload)
                conn.commit()
    except Exception as e:
        print(f"[analytics] Ошибка логирования ticket_event: {e}")


def _profile_username_key(username: str | None) -> str:
    return str(username or '').strip().lower()


def _normalize_telegram_username(value: str | None) -> str | None:
    login = str(value or '').strip()
    login = re.sub(r'^https?://t\.me/', '', login, flags=re.IGNORECASE).strip()
    login = login.strip('/').split('?', 1)[0].strip()
    login = login.lstrip('@').strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', login):
        return None
    return login


def _load_user_profile(username: str | None) -> dict:
    key = _profile_username_key(username)
    if not key:
        return {}
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT username, telegram_username
                        FROM user_profiles
                        WHERE username = %s
                    """, [key])
                    row = cur.fetchone()
                    return dict(row) if row else {}

        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT username, telegram_username
                FROM user_profiles
                WHERE username = ?
            """, [key])
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        print(f"[user_profile] Ошибка чтения профиля: {e}")
        return {}


def _save_user_telegram_username(username: str | None, telegram_username: str) -> bool:
    key = _profile_username_key(username)
    normalized = _normalize_telegram_username(telegram_username)
    if not key or not normalized:
        return False
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_profiles (username, telegram_username, created_at, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (username) DO UPDATE SET
                            telegram_username = EXCLUDED.telegram_username,
                            updated_at = EXCLUDED.updated_at
                    """, [key, normalized, now, now])
                conn.commit()
            return True

        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.execute("""
                INSERT INTO user_profiles (username, telegram_username, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    telegram_username = excluded.telegram_username,
                    updated_at = excluded.updated_at
            """, [key, normalized, now, now])
            conn.commit()
        return True
    except Exception as e:
        print(f"[user_profile] Ошибка сохранения Telegram username: {e}")
        return False


def _get_session_telegram_username() -> str:
    if not has_request_context() or not session.get('authenticated') or not session.get('user_info'):
        return ''

    user_info = dict(session.get('user_info') or {})
    current = _normalize_telegram_username(user_info.get('telegram_username'))
    if current:
        return current

    profile = _load_user_profile(user_info.get('username'))
    saved = _normalize_telegram_username(profile.get('telegram_username'))
    if saved:
        user_info['telegram_username'] = saved
        session['user_info'] = user_info
        session.modified = True
        return saved

    return ''


def _require_telegram_username_for_ticket(next_endpoint: str, next_args: dict | None = None):
    if _get_session_telegram_username():
        return None
    session['next_after_telegram_username'] = next_endpoint
    session['next_after_telegram_username_args'] = next_args or {}
    session.modified = True
    return redirect(url_for('enter_telegram_username'))


TICKET_STATUS_LABELS = {
    'in_work': 'В работе',
    'ready_for_feedback': 'Готово',
    'closed': 'Решено',
    'rejected': 'Отклонён',
    'mass_incident': 'Массовый инцидент',
    'transferred_up': 'Передано выше',
    'closed_auto': 'Авто-закрыта',
    'unknown': 'Неизвестно',
}
HARD_FINAL_TICKET_STATUSES = {'rejected', 'mass_incident', 'transferred_up', 'closed_auto'}


def _parse_event_details(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return {}


def _load_ticket_states(ticket_numbers: list[int] | None = None) -> dict[int, dict]:
    """Собирает текущие статусы заявок из event log без перезаписи старых данных."""
    if ticket_numbers is not None:
        ticket_numbers = [int(n) for n in ticket_numbers if n is not None]
        if not ticket_numbers:
            return {}

    if ANALYTICS_USE_POSTGRES:
        where = "WHERE ticket_number IS NOT NULL"
        params: list[Any] = []
        if ticket_numbers is not None:
            where += " AND ticket_number = ANY(%s)"
            params.append(ticket_numbers)
        query = f"""
            SELECT id, created_at::text AS created_at, event_type, ticket_number, problem,
                   problem_id, subproblem_id, department, user_name, workplace, is_cisco,
                   actor_name, actor_username, actor_role, details_json
            FROM ticket_events
            {where}
            ORDER BY ticket_number ASC, created_at ASC, id ASC
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = [dict(row) for row in cur.fetchall()]
    else:
        where = "WHERE ticket_number IS NOT NULL"
        params = []
        if ticket_numbers is not None:
            placeholders = ",".join("?" * len(ticket_numbers))
            where += f" AND ticket_number IN ({placeholders})"
            params.extend(ticket_numbers)
        query = f"""
            SELECT id, created_at, event_type, ticket_number, problem,
                   problem_id, subproblem_id, department, user_name, workplace, is_cisco,
                   actor_name, actor_username, actor_role, details_json
            FROM ticket_events
            {where}
            ORDER BY ticket_number ASC, created_at ASC, id ASC
        """
        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]

    states: dict[int, dict] = {}
    for row in rows:
        ticket_number = row.get('ticket_number')
        if ticket_number is None:
            continue
        ticket_number = int(ticket_number)
        details = _parse_event_details(row.get('details_json'))
        state = states.setdefault(ticket_number, {
            'ticket_number': ticket_number,
            'status': 'unknown',
            'status_label': TICKET_STATUS_LABELS['unknown'],
            'problem': '',
            'problem_id': '',
            'subproblem_id': '',
            'department': '',
            'user_name': '',
            'workplace': '',
            'is_cisco': False,
            'created_at': '',
            'updated_at': '',
            'ready_at': '',
            'closed_at': '',
            'resolved_by': '',
            'assigned_name': '',
            'assigned_username': '',
            'creator_name': '',
            'creator_username': '',
            'reject_reason': '',
            'transferred_by': '',
            'resubmitted_from': None,
            'resubmitted_ticket_number': None,
            'details': {},
        })

        event_type = row.get('event_type') or ''
        created_at = str(row.get('created_at') or '')[:19]
        state['updated_at'] = created_at or state['updated_at']
        if row.get('problem'):
            state['problem'] = row.get('problem') or state['problem']
        if row.get('problem_id'):
            state['problem_id'] = row.get('problem_id') or state['problem_id']
        if row.get('subproblem_id'):
            state['subproblem_id'] = row.get('subproblem_id') or state['subproblem_id']
        if row.get('department'):
            state['department'] = row.get('department') or state['department']
        if row.get('user_name'):
            state['user_name'] = row.get('user_name') or state['user_name']
        if row.get('workplace'):
            state['workplace'] = row.get('workplace') or state['workplace']
        state['is_cisco'] = bool(row.get('is_cisco')) or state['is_cisco']

        if event_type == 'ticket_created':
            state['created_at'] = state['created_at'] or created_at
            state['status'] = 'in_work'
            state['creator_name'] = row.get('actor_name') or state['creator_name']
            state['creator_username'] = row.get('actor_username') or state['creator_username']
            if details.get('resubmitted_from'):
                try:
                    state['resubmitted_from'] = int(details.get('resubmitted_from'))
                except (TypeError, ValueError):
                    state['resubmitted_from'] = details.get('resubmitted_from')
        elif event_type in ('ticket_assigned_to_duty', 'ticket_assigned_to_staff'):
            state['assigned_name'] = row.get('actor_name') or state['assigned_name']
            state['assigned_username'] = row.get('actor_username') or state['assigned_username']
            if state['status'] not in ('ready_for_feedback', 'closed', 'rejected', 'mass_incident', 'transferred_up', 'closed_auto'):
                state['status'] = 'in_work'
        elif event_type == 'ticket_reopened_by_user':
            if state['status'] in HARD_FINAL_TICKET_STATUSES:
                continue
            state['status'] = 'in_work'
            state['ready_at'] = ''
            state['closed_at'] = ''
            state['resolved_by'] = ''
            state['details'] = details
        elif event_type == 'ticket_ready_for_feedback':
            if state['status'] in HARD_FINAL_TICKET_STATUSES:
                continue
            state['status'] = 'ready_for_feedback'
            state['ready_at'] = created_at
            state['resolved_by'] = row.get('actor_name') or state['resolved_by']
        elif event_type == 'ticket_resolved_by_staff':
            if state['status'] in HARD_FINAL_TICKET_STATUSES:
                continue
            # Legacy close marker and SLA endpoint. New flow also writes ticket_ready_for_feedback after it.
            state['status'] = 'closed'
            state['ready_at'] = created_at
            state['closed_at'] = created_at
            state['resolved_by'] = row.get('actor_name') or state['resolved_by']
        elif event_type == 'ticket_user_confirmed_resolved':
            if state['status'] in HARD_FINAL_TICKET_STATUSES:
                continue
            state['status'] = 'closed'
            state['closed_at'] = created_at
            state['details'] = details
        elif event_type in ('ticket_rejected', 'ticket_not_relevant'):
            state['status'] = 'rejected'
            state['closed_at'] = created_at
            state['reject_reason'] = details.get('reason') or state['reject_reason']
        elif event_type == 'ticket_mass_incident':
            state['status'] = 'mass_incident'
            state['details'] = details
        elif event_type == 'ticket_transferred_up':
            state['status'] = 'transferred_up'
            state['closed_at'] = created_at
            state['transferred_by'] = row.get('actor_name') or state['transferred_by']
            state['details'] = details
        elif event_type == 'ticket_auto_closed_reset_call':
            state['status'] = 'closed_auto'
            state['closed_at'] = created_at
            state['details'] = details
        elif event_type == 'ticket_resubmitted_by_user':
            new_ticket_number = details.get('new_ticket_number')
            if new_ticket_number:
                try:
                    state['resubmitted_ticket_number'] = int(new_ticket_number)
                except (TypeError, ValueError):
                    state['resubmitted_ticket_number'] = new_ticket_number
            state['details'] = details

        state['status_label'] = TICKET_STATUS_LABELS.get(state['status'], TICKET_STATUS_LABELS['unknown'])

    legacy_text_map = _resolution_problem_text_map()
    for state in states.values():
        _attach_resolution_problem_fields(state, legacy_text_map)

    return states


def _get_ticket_state(ticket_number: int) -> dict | None:
    return _load_ticket_states([ticket_number]).get(int(ticket_number))


def _default_duty_settings() -> dict:
    return {
        'current_duty_name': CURRENT_DUTY_NAME,
        'current_duty_username': CURRENT_DUTY_USERNAME,
        'current_duty_telegram_id': str(CURRENT_DUTY_TELEGRAM_ID or ''),
        'overload_ticket_limit': str(OVERLOAD_TICKET_LIMIT),
        'overload_alert_thread_id': str(OVERLOAD_ALERT_THREAD_ID or ''),
    }


def _default_ticket_schedule_settings() -> dict:
    return {
        'ticket_workdays': '0,1,2,3,4',
        'ticket_work_start': '08:30',
        'ticket_work_end': '17:30',
        'ticket_lunch_enabled': 'true',
        'ticket_lunch_start': '12:00',
        'ticket_lunch_end': '13:00',
        'ticket_holidays_json': '[]',
        'ticket_workday_overrides_json': '[]',
    }


def _get_app_settings(keys: list[str] | None = None) -> dict:
    keys = keys or list(_default_duty_settings().keys())
    if not keys:
        return {}
    result = {}
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                        [keys]
                    )
                    result = {row['key']: row.get('value') or '' for row in cur.fetchall()}
        else:
            placeholders = ",".join("?" * len(keys))
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})", keys)
                result = {row['key']: row['value'] or '' for row in cur.fetchall()}
    except Exception as e:
        print(f"[app_settings] Ошибка чтения настроек: {e}")
    return result


def _set_app_settings(values: dict, updated_by: str = ''):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    clean_values = {str(k): str(v or '')[:10000] for k, v in values.items()}
    if not clean_values:
        return
    if ANALYTICS_USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                for key, value in clean_values.items():
                    cur.execute("""
                        INSERT INTO app_settings (key, value, updated_at, updated_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (key) DO UPDATE SET
                            value = EXCLUDED.value,
                            updated_at = EXCLUDED.updated_at,
                            updated_by = EXCLUDED.updated_by
                    """, [key, value, now, updated_by])
            conn.commit()
    else:
        with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
            for key, value in clean_values.items():
                conn.execute("""
                    INSERT INTO app_settings (key, value, updated_at, updated_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                """, [key, value, now, updated_by])
            conn.commit()


def _get_duty_settings() -> dict:
    settings = _default_duty_settings()
    settings.update(_get_app_settings(list(settings.keys())))
    settings['current_duty_username'] = settings.get('current_duty_username', '').strip().lstrip('@')
    settings['current_duty_name'] = settings.get('current_duty_name', '').strip()
    settings['current_duty_telegram_id'] = str(_env_int_from_value(settings.get('current_duty_telegram_id'), 0) or '')
    settings['overload_ticket_limit'] = str(max(1, _env_int_from_value(settings.get('overload_ticket_limit'), OVERLOAD_TICKET_LIMIT)))
    settings['overload_alert_thread_id'] = str(_env_int_from_value(settings.get('overload_alert_thread_id'), 0) or '')
    return settings


def _bool_from_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ('1', 'true', 'yes', 'on', 'да'):
        return True
    if normalized in ('0', 'false', 'no', 'off', 'нет'):
        return False
    return default


def _parse_schedule_time(value: Any, fallback: str):
    text = str(value or fallback).strip()
    if not re.fullmatch(r'\d{2}:\d{2}', text):
        text = fallback
    try:
        return datetime.strptime(text, '%H:%M').time()
    except ValueError:
        return datetime.strptime(fallback, '%H:%M').time()


def _normalize_time_text(value: Any, fallback: str) -> str:
    return _parse_schedule_time(value, fallback).strftime('%H:%M')


def _valid_schedule_date(value: Any) -> str:
    text = str(value or '').strip()[:10]
    try:
        return datetime.strptime(text, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        return ''


def _load_json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _normalize_ticket_holidays(items: list) -> list[dict]:
    result = {}
    for item in items[:150]:
        if not isinstance(item, dict):
            continue
        date_text = _valid_schedule_date(item.get('date'))
        if not date_text:
            continue
        result[date_text] = {
            'date': date_text,
            'name': str(item.get('name') or 'Праздничный день').strip()[:120]
        }
    return [result[key] for key in sorted(result.keys())]


def _normalize_ticket_workday_overrides(items: list) -> list[dict]:
    result = {}
    for item in items[:120]:
        if not isinstance(item, dict):
            continue
        date_text = _valid_schedule_date(item.get('date'))
        if not date_text:
            continue
        start = _normalize_time_text(item.get('start'), '08:30')
        end = _normalize_time_text(item.get('end'), '17:30')
        if start >= end:
            start, end = '08:30', '17:30'
        lunch_start = _normalize_time_text(item.get('lunch_start'), '12:00')
        lunch_end = _normalize_time_text(item.get('lunch_end'), '13:00')
        if lunch_start >= lunch_end:
            lunch_start, lunch_end = '12:00', '13:00'
        result[date_text] = {
            'date': date_text,
            'name': str(item.get('name') or 'Рабочий день').strip()[:120],
            'start': start,
            'end': end,
            'lunch_enabled': _bool_from_value(item.get('lunch_enabled'), True),
            'lunch_start': lunch_start,
            'lunch_end': lunch_end,
        }
    return [result[key] for key in sorted(result.keys())]


def _normalize_ticket_workdays(value: Any) -> list[int]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or '').split(',')
    days = set()
    for item in raw_items:
        try:
            day = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            days.add(day)
    return sorted(days) or [0, 1, 2, 3, 4]


def _get_ticket_schedule_settings() -> dict:
    settings = _default_ticket_schedule_settings()
    settings.update(_get_app_settings(list(settings.keys())))

    work_start = _normalize_time_text(settings.get('ticket_work_start'), '08:30')
    work_end = _normalize_time_text(settings.get('ticket_work_end'), '17:30')
    if work_start >= work_end:
        work_start, work_end = '08:30', '17:30'

    lunch_start = _normalize_time_text(settings.get('ticket_lunch_start'), '12:00')
    lunch_end = _normalize_time_text(settings.get('ticket_lunch_end'), '13:00')
    if lunch_start >= lunch_end:
        lunch_start, lunch_end = '12:00', '13:00'

    return {
        'workdays': _normalize_ticket_workdays(settings.get('ticket_workdays')),
        'work_start': work_start,
        'work_end': work_end,
        'lunch_enabled': _bool_from_value(settings.get('ticket_lunch_enabled'), True),
        'lunch_start': lunch_start,
        'lunch_end': lunch_end,
        'holidays': _normalize_ticket_holidays(_load_json_list(settings.get('ticket_holidays_json'))),
        'workday_overrides': _normalize_ticket_workday_overrides(_load_json_list(settings.get('ticket_workday_overrides_json'))),
    }


def _ticket_schedule_summary() -> str:
    settings = _get_ticket_schedule_settings()
    day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    days = ', '.join(day_names[day] for day in settings['workdays'])
    summary = f"{days}: {settings['work_start']}–{settings['work_end']}"
    if settings['lunch_enabled']:
        summary += f"; обед {settings['lunch_start']}–{settings['lunch_end']}"
    if settings['holidays']:
        summary += f"; праздников: {len(settings['holidays'])}"
    return summary


def _env_int_from_value(value: Any, default: int = 0) -> int:
    try:
        return int(str(value if value is not None else default).strip() or default)
    except (TypeError, ValueError):
        return default


def _current_duty_actor() -> dict:
    settings = _get_duty_settings()
    telegram_id = _env_int_from_value(settings.get('current_duty_telegram_id'), 0)
    username = settings.get('current_duty_username') or (str(telegram_id) if telegram_id else 'duty')
    name = settings.get('current_duty_name') or settings.get('current_duty_username') or 'Дежурный'
    return {'name': name, 'username': username, 'role': 'support_duty'}


def _format_duty_mention(actor: dict) -> str:
    name = html_escape(actor.get('name') or actor.get('username') or 'дежурный')
    telegram_id = _env_int_from_value(_get_duty_settings().get('current_duty_telegram_id'), 0)
    if telegram_id:
        return f'<a href="tg://user?id={telegram_id}">{name}</a>'
    username = (actor.get('username') or '').strip().lstrip('@')
    return f'@{html_escape(username)}' if username and username != 'duty' else name


def _send_support_message(text: str, thread_id: int | None = None, parse_mode: str | None = None, **kwargs):
    try:
        alert_thread_id = _env_int_from_value(_get_duty_settings().get('overload_alert_thread_id'), 0)
        return bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            text,
            message_thread_id=thread_id or alert_thread_id or IN_PROGRESS_THREAD_ID or NEW_TICKETS_THREAD_ID,
            parse_mode=parse_mode,
            **kwargs
        )
    except Exception as e:
        print(f"[telegram] Ошибка отправки служебного сообщения: {e}")
        return None


def _recent_overload_alert_sent(actor_username: str) -> bool:
    if not actor_username:
        return False
    since = (datetime.now() - timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
    if ANALYTICS_USE_POSTGRES:
        query = """
            SELECT COUNT(*) AS c
            FROM ticket_events
            WHERE event_type = 'ticket_overload_alert_sent'
              AND actor_username = %s
              AND created_at >= %s
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [actor_username, since])
                row = cur.fetchone()
                return int(row['c'] if isinstance(row, dict) else row[0]) > 0
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM ticket_events
            WHERE event_type = 'ticket_overload_alert_sent'
              AND actor_username = ?
              AND created_at >= ?
        """, [actor_username, since])
        return int(cur.fetchone()[0] or 0) > 0


def _maybe_send_overload_alert(actor: dict):
    username = actor.get('username') or ''
    if not username:
        return
    states = _load_ticket_states()
    active = [
        s for s in states.values()
        if s.get('status') == 'in_work' and (s.get('assigned_username') or '') == username
    ]
    limit = max(1, _env_int_from_value(_get_duty_settings().get('overload_ticket_limit'), OVERLOAD_TICKET_LIMIT))
    if len(active) <= limit or _recent_overload_alert_sent(username):
        return

    ticket_numbers = sorted(s['ticket_number'] for s in active)
    mention = _format_duty_mention(actor)
    _send_support_message(
        f"⚠️ <b>Перегрузка дежурного</b>\n\n"
        f"{mention}: в работе {len(active)} заявок.\n"
        f"Порог: {limit}.\n"
        f"Заявки: {', '.join('№' + str(n) for n in ticket_numbers[:15])}",
        parse_mode='HTML'
    )
    log_ticket_event(
        event_type='ticket_overload_alert_sent',
        actor_override=actor,
        details={'open_count': len(active), 'ticket_numbers': ticket_numbers}
    )


def _assign_ticket(ticket_number: int, problem: str, actor: dict, event_type: str):
    if not ticket_number:
        return
    log_ticket_event(
        event_type=event_type,
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details={'assigned_to': actor.get('username') or actor.get('name') or ''}
    )
    _maybe_send_overload_alert(actor)


def _assign_ticket_to_current_duty(ticket_number: int, problem: str):
    _assign_ticket(ticket_number, problem, _current_duty_actor(), 'ticket_assigned_to_duty')


def _format_reopened_ticket_message(ticket_number: int, state: dict, actor: dict) -> str:
    parts = [
        f"🔁 *ПОВТОРНО ОТКРЫТА ЗАЯВКА №{ticket_number}* 🔁",
        f"Отдел: {escape_markdown(state.get('department') or 'Неизвестно')}",
        f"Имя: {escape_markdown(state.get('user_name') or 'Неизвестно')}",
    ]
    if state.get('workplace'):
        parts.append(f"Рабочее место: {escape_markdown(state.get('workplace'))}")
    parts.extend([
        f"Проблема: {escape_markdown(state.get('problem') or 'Неизвестная проблема')}",
        "",
        f"Инициатор нажал: {escape_markdown('Не решено')}",
        f"Назначено: {escape_markdown(actor.get('name') or actor.get('username') or 'Дежурный')}",
    ])
    return "\n".join(parts)


def _send_reopened_ticket_card(ticket_number: int, state: dict, actor: dict):
    return _send_support_message(
        _format_reopened_ticket_message(ticket_number, state, actor),
        thread_id=NEW_TICKETS_THREAD_ID or IN_PROGRESS_THREAD_ID,
        parse_mode='Markdown',
        reply_markup=create_ticket_buttons()
    )


def _is_reset_call_ticket(problem: str, topic_name: str = '') -> bool:
    text = f"{problem or ''} {topic_name or ''}".lower().replace('ё', 'е')
    return 'сброс звонка' in text


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
        user_info = session.get('user_info', {}) if has_request_context() else {}
        payload = (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            str(query_text)[:500],
            str(channel)[:150],
            int(results_count),
            str((user_info or {}).get('department', ''))[:200],
            str((user_info or {}).get('name', ''))[:200],
            str((user_info or {}).get('workplace', ''))[:100],
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
                            department, user_name, workplace,
                            actor_name, actor_username, actor_role
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO topic_search_events (
                        created_at, query_text, channel, results_count,
                        department, user_name, workplace,
                        actor_name, actor_username, actor_role
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
tm = TopicsManager(os.path.join(BASE_DIR, "topics.db"))

# Инициализация TrainerManager
trainer_mgr = TrainerManager(os.path.join(BASE_DIR, "topics.db"))

# Инициализация ScenarioManager (сценарии консультаций КЦ)
scenario_mgr = ScenarioManager(os.path.join(BASE_DIR, "topics.db"))
contacts_mgr = ContactsManager(os.path.join(BASE_DIR, "topics.db"))

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
    """Создает кнопки статусов для заявки."""
    markup = InlineKeyboardMarkup(row_width=2)
    button_done = InlineKeyboardButton("Готово ✅", callback_data="ticket_done")
    button_reject = InlineKeyboardButton("Отклонён ❌", callback_data="ticket_reject_prompt")
    button_transfer = InlineKeyboardButton("Передано выше ⬆️", callback_data="ticket_transferred_up")
    button_mass = InlineKeyboardButton("Массовый инцидент ⚠️", callback_data="ticket_mass_incident")
    markup.add(button_done, button_reject)
    markup.add(button_transfer, button_mass)
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

        if not TELEGRAM_FILE_LOOKUP_ENABLED:
            return None

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

def escape_markdown(text):
    """Экранирует спецсимволы Telegram Markdown."""
    if not text:
        return text
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def _send_ticket_screenshots(screenshots, thread_id: int | None = None):
    """Отправляет скриншоты к заявке: 2+ фото строго одним Telegram-альбомом."""
    if not screenshots:
        return None

    if len(screenshots) == 1:
        screenshot = screenshots[0]
        stream = getattr(screenshot, 'stream', screenshot)
        try:
            if hasattr(stream, 'seek'):
                stream.seek(0)
        except Exception:
            pass
        msg = bot.send_photo(
            TECH_SUPPORT_CHAT_ID,
            stream,
            caption="Скриншот 1",
            message_thread_id=thread_id
        )
        print("[send_ticket] Отправлен скриншот 1")
        return msg

    media = []
    for index, screenshot in enumerate(screenshots[:10], 1):
        stream = getattr(screenshot, 'stream', screenshot)
        try:
            if hasattr(stream, 'seek'):
                stream.seek(0)
        except Exception:
            pass
        caption = "Скриншоты" if index == 1 else None
        media.append(InputMediaPhoto(stream, caption=caption))

    result = bot.send_media_group(
        TECH_SUPPORT_CHAT_ID,
        media,
        message_thread_id=thread_id
    )
    print(f"[send_ticket] Отправлены скриншоты альбомом ({len(media)})")
    return result


def send_ticket(problem, screenshots=None, topic_info=None, video=None, thread_id=None,
                *, resubmitted_from: int | None = None, resubmit_reason: str = '',
                user_info_override: dict | None = None, extra_details: dict | None = None,
                problem_id_override: str | None = None, subproblem_id_override: str | None = None):
    user_info = dict(session.get('user_info', {}) or {})
    if user_info_override:
        for key in ('department', 'name', 'workplace'):
            if user_info_override.get(key) is not None:
                user_info[key] = user_info_override.get(key) or ''
    department = user_info.get('department', 'Неизвестно')
    name = user_info.get('name', 'Неизвестно')
    workplace = user_info.get('workplace', '')
    telegram_username = _get_session_telegram_username()
    ticket_number = get_next_ticket_number()
    session['current_ticket_number'] = ticket_number
    session.modified = True
    is_resubmission = resubmitted_from is not None

    # Формируем сообщение (экранируем пользовательские данные для Markdown)
    title = (
        f"🔁 *ПОВТОРНАЯ ЗАЯВКА №{ticket_number}* 🔁"
        if is_resubmission
        else f"🚨 *НОВАЯ ЗАЯВКА №{ticket_number}* 🚨"
    )
    support_message = (
        f"{title}\n"
        f"Отдел: {escape_markdown(department)}\n"
        f"Имя: {escape_markdown(name)}\n"
    )

    if telegram_username:
        support_message += f"Telegram: @{escape_markdown(telegram_username)}\n"

    if is_resubmission:
        support_message += f"Предыдущая заявка: №{escape_markdown(str(resubmitted_from))}\n"
        support_message += "Важно: инициатору показано предупреждение быть на рабочем месте, иначе повторная заявка может быть отклонена.\n"
        if resubmit_reason:
            support_message += f"Причина прошлого отклонения: {escape_markdown(str(resubmit_reason)[:400])}\n"

    # Добавляем рабочее место если оно указано
    if workplace:
        support_message += f"Рабочее место: {escape_markdown(workplace)}\n"

    support_message += f"Проблема: {escape_markdown(problem)}\n"
    event_details = dict(extra_details or {})
    if telegram_username:
        event_details['telegram_username'] = telegram_username
    if is_resubmission:
        event_details['resubmitted_from'] = resubmitted_from
        if resubmit_reason:
            event_details['reject_reason'] = str(resubmit_reason)[:500]

    # Тематика НЕ отправляется в Telegram - только для маркировки в CRM
    # topic_info используется только на стороне веб-приложения
    topic_id = None
    topic_name = None
    if topic_info:
        topic_name = topic_info.get('topic')
        topic_id = topic_info.get('id')
        try:
            if topic_id:
                topic_id = int(topic_id)
            if has_request_context() and request.method == 'POST':
                form_topic_id = request.form.get('selected_topic_id')
                if form_topic_id:
                    topic_id = int(form_topic_id)
        except Exception:
            topic_id = None

    target_thread_id = thread_id or NEW_TICKETS_THREAD_ID
    is_cisco_ticket = (target_thread_id == CISCO_TICKETS_THREAD_ID and CISCO_TICKETS_THREAD_ID != 0)

    try:
        if _is_reset_call_ticket(problem, topic_name or ''):
            print(f"[send_ticket] Авто-закрытие заявки №{ticket_number}: сброс звонка")
            msg = bot.send_message(
                TECH_SUPPORT_CHAT_ID,
                support_message + "\nСтатус: авто-закрыта (сброс звонка)",
                message_thread_id=SOLVED_TICKETS_THREAD_ID or target_thread_id,
                parse_mode='Markdown'
            )
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
                is_cisco=is_cisco_ticket,
                user_info_override=user_info,
                details=event_details or None,
                problem_id_override=problem_id_override,
                subproblem_id_override=subproblem_id_override
            )
            system_actor = {'name': 'Helper', 'username': 'helper-system', 'role': 'system'}
            log_ticket_event(
                event_type='ticket_resolved_by_staff',
                ticket_number=ticket_number,
                problem=problem,
                actor_override=system_actor,
                user_info_override=user_info,
                details={'auto_close_reason': 'reset_call'},
                problem_id_override=problem_id_override,
                subproblem_id_override=subproblem_id_override
            )
            log_ticket_event(
                event_type='ticket_auto_closed_reset_call',
                ticket_number=ticket_number,
                problem=problem,
                actor_override=system_actor,
                user_info_override=user_info,
                details={'reason': 'сброс звонка'},
                problem_id_override=problem_id_override,
                subproblem_id_override=subproblem_id_override
            )
            return msg

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
                _send_ticket_screenshots(screenshots, target_thread_id)
            except Exception as e:
                print(f"[send_ticket] Ошибка при отправке скриншотов: {type(e).__name__}")
                _log_exception_safely('send_ticket_screenshots', e)

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
            is_cisco=is_cisco_ticket,
            user_info_override=user_info,
            details=event_details or None,
            problem_id_override=problem_id_override,
            subproblem_id_override=subproblem_id_override
        )
        _assign_ticket_to_current_duty(ticket_number, problem)

        return msg
    except Exception as e:
        print(f"[send_ticket] Ошибка при отправке заявки №{ticket_number}: {type(e).__name__}")
        _log_exception_safely('send_ticket', e)
        log_ticket_event(
            event_type='ticket_send_failed',
            ticket_number=ticket_number,
            problem=problem,
            channel=topic_info.get('channel', '') if topic_info else '',
            topic_name=topic_name or '',
            is_cisco=is_cisco_ticket,
            user_info_override=user_info,
            details={
                'error_type': type(e).__name__,
                'thread_id': target_thread_id,
                'telegram_username': telegram_username,
                **event_details
            },
            problem_id_override=problem_id_override,
            subproblem_id_override=subproblem_id_override
        )
        return None


def _staff_actor_from_call(call) -> dict:
    return {
        'name': call.from_user.first_name or call.from_user.username or str(call.from_user.id),
        'username': call.from_user.username or str(call.from_user.id),
        'role': 'staff'
    }


def _staff_actor_from_message(message) -> dict:
    return {
        'name': message.from_user.first_name or message.from_user.username or str(message.from_user.id),
        'username': message.from_user.username or str(message.from_user.id),
        'role': 'staff'
    }


def _parse_rejection_reason(text: str) -> str:
    if not text:
        return ''
    match = re.search(r'отклон[её]н\w*\s*[:\-—]\s*(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    return (match.group(1).strip() if match else '')[:500]


def _plain_rejection_reason(text: str) -> str:
    reason = (text or '').strip()
    if not reason:
        return ''
    lowered = reason.lower()
    non_rejection_keywords = (
        'массовый инцидент',
        'отклон',
        'готов',
        'решен',
        'решён',
        'решена',
        'решено',
        'в работе',
        'в процессе',
        'передано выше',
        'передан выше',
        'передана выше',
        'передал выше',
        'передали выше',
        'передать выше',
        'эскалир',
        '2 линия',
        'вторая линия',
        'l2',
    )
    if any(keyword in lowered for keyword in non_rejection_keywords):
        return ''
    return reason[:500]


def _is_transfer_up_text(text: str) -> bool:
    normalized = (text or '').strip().lower().replace('ё', 'е')
    return any(keyword in normalized for keyword in (
        'передано выше',
        'передан выше',
        'передана выше',
        'передал выше',
        'передали выше',
        'передать выше',
        'эскалир',
        '2 линия',
        'вторая линия',
        'l2',
    ))


def _mark_ticket_ready_for_feedback(ticket_number: int | None, problem: str, original_message: str, actor: dict):
    if ticket_number is None:
        return
    log_ticket_event(
        event_type='ticket_resolved_by_staff',
        ticket_number=ticket_number,
        problem=problem,
        channel='',
        topic_name='',
        actor_override=actor
    )
    log_ticket_event(
        event_type='ticket_ready_for_feedback',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details={'source': 'telegram', 'original_message': original_message[:500]}
    )


def _mark_ticket_rejected(ticket_number: int | None, problem: str, actor: dict, reason: str):
    if ticket_number is None:
        return
    details = {'reason': reason}
    log_ticket_event(
        event_type='ticket_rejected',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details=details
    )
    # Legacy event for old counters/exports.
    log_ticket_event(
        event_type='ticket_not_relevant',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details=details
    )


def _mark_ticket_mass_incident(ticket_number: int | None, problem: str, actor: dict):
    if ticket_number is None:
        return
    log_ticket_event(
        event_type='ticket_mass_incident',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor
    )


def _mark_ticket_transferred_up(ticket_number: int | None, problem: str, actor: dict, original_message: str = ''):
    if ticket_number is None:
        return
    log_ticket_event(
        event_type='ticket_transferred_up',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details={
            'source': 'telegram',
            'original_message': (original_message or '')[:500]
        }
    )


LOCKED_TICKET_ACTION_STATUSES = {'ready_for_feedback', 'closed', 'rejected', 'mass_incident', 'transferred_up', 'closed_auto'}


def _ticket_action_lock_reason(ticket_number: int | None) -> str:
    """Не даём повторно менять заявку после финального/полуфинального статуса."""
    if ticket_number is None:
        return ''
    try:
        state = _get_ticket_state(ticket_number)
        if not state:
            return ''
        status = state.get('status')
        if status in LOCKED_TICKET_ACTION_STATUSES:
            label = state.get('status_label') or TICKET_STATUS_LABELS.get(status, status)
            return f"Заявка №{ticket_number} уже обработана: {label}"
    except Exception as e:
        print(f"[ticket_action_lock] Ошибка проверки заявки {ticket_number}: {e}")
    return ''


def _remove_ticket_buttons(chat_id: int, message_id: int):
    try:
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None
        )
    except Exception as e:
        print(f"[ticket_buttons] Не удалось убрать кнопки message_id={message_id}: {e}")


def _record_ticket_reject_prompt(ticket_number: int | None, problem: str, original_message: str,
                                 prompt_message_id: int | None, original_message_id: int | None,
                                 original_chat_id: int | None, actor: dict):
    if ticket_number is None or not prompt_message_id:
        return
    log_ticket_event(
        event_type='ticket_reject_requested',
        ticket_number=ticket_number,
        problem=problem,
        actor_override=actor,
        details={
            'prompt_message_id': prompt_message_id,
            'original_message_id': original_message_id,
            'original_chat_id': original_chat_id,
            'original_message': (original_message or '')[:1000],
        }
    )


def _find_ticket_reject_prompt(prompt_message_id: int | None) -> dict | None:
    if not prompt_message_id:
        return None
    try:
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT ticket_number, problem, details_json
                        FROM ticket_events
                        WHERE event_type = 'ticket_reject_requested'
                        ORDER BY created_at DESC, id DESC
                        LIMIT 200
                    """)
                    rows = [dict(row) for row in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT ticket_number, problem, details_json
                    FROM ticket_events
                    WHERE event_type = 'ticket_reject_requested'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                """)
                rows = [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[ticket_reject_prompt] Ошибка поиска подсказки: {e}")
        return None

    for row in rows:
        details = _parse_event_details(row.get('details_json'))
        if str(details.get('prompt_message_id') or '') == str(prompt_message_id):
            return {
                'ticket_number': int(row.get('ticket_number') or 0) or None,
                'problem': row.get('problem') or '',
                'original_message_id': _env_int_from_value(details.get('original_message_id'), 0),
                'original_chat_id': _env_int_from_value(details.get('original_chat_id'), 0),
                'original_message': details.get('original_message') or '',
            }
    return None


def _reply_ticket_number_required(message):
    bot.reply_to(
        message,
        "Не нашёл номер заявки. Ответьте на исходную заявку или на подсказку, где указан номер заявки."
    )


# обработчик кнопки
# обработчик кнопки "Готово"
@bot.callback_query_handler(func=lambda call: call.data == "ticket_done")
def handle_ticket_done(call):
    print(f"🔔 Получен callback от кнопки 'Готово'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        resolver_name = call.from_user.first_name or call.from_user.username or str(call.from_user.id)
        ticket_number = extract_ticket_number_from_text(original_message)
        parsed = _parse_ticket_text_fields(original_message)

        if ticket_number is None:
            print(f"⚠️ [handle_ticket_done] Не удалось извлечь номер заявки из текста: {original_message[:100]}")
            bot.answer_callback_query(call.id, "Не нашёл номер заявки")
            return

        print(f"📋 [handle_ticket_done] ticket_number={ticket_number}, resolver={resolver_name}")

        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_done] {lock_reason}")
            return

        actor_override = _staff_actor_from_call(call)

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

        problem = parsed.get('problem') or original_message
        _mark_ticket_ready_for_feedback(ticket_number, problem, original_message, actor_override)
        bot.answer_callback_query(call.id, "✅ Заявка ждёт подтверждения инициатора")

        print(f"✅ Кнопка 'Готово' успешно обработана! ticket_number={ticket_number}")

    except Exception as e:
        print(f"❌ Ошибка при обработке кнопки 'Готово': {e}")
        traceback.print_exc()

# обработчик кнопки "Не актуально"
@bot.callback_query_handler(func=lambda call: call.data in ("ticket_reject_prompt", "ticket_not_relevant"))
def handle_ticket_reject_prompt(call):
    print(f"🔔 Получен callback от кнопки 'Отклонён'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        ticket_number = extract_ticket_number_from_text(original_message)
        if ticket_number is None:
            bot.answer_callback_query(call.id, "Не нашёл номер заявки")
            return
        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_reject_prompt] {lock_reason}")
            return
        bot.answer_callback_query(call.id, "Укажите причину отклонения ответом на заявку")
        prompt_msg = bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"❌ Для отклонения заявки №{ticket_number or '—'} ответьте на исходную заявку причиной:\n"
            f"<code>причина отклонения</code>",
            message_thread_id=NEW_TICKETS_THREAD_ID,
            parse_mode='HTML',
            reply_to_message_id=call.message.message_id
        )
        _record_ticket_reject_prompt(
            ticket_number,
            (_parse_ticket_text_fields(original_message).get('problem') or original_message),
            original_message,
            getattr(prompt_msg, 'message_id', None),
            call.message.message_id,
            call.message.chat.id,
            _staff_actor_from_call(call)
        )
        print(f"ℹ️ Запрошена причина отклонения ticket_number={ticket_number}")
    except Exception as e:
        print(f"❌ Ошибка при запросе причины отклонения: {e}")
        traceback.print_exc()


@bot.callback_query_handler(func=lambda call: call.data == "ticket_mass_incident")
def handle_ticket_mass_incident(call):
    print(f"🔔 Получен callback от кнопки 'Массовый инцидент'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        ticket_number = extract_ticket_number_from_text(original_message)
        if ticket_number is None:
            bot.answer_callback_query(call.id, "Не нашёл номер заявки")
            return
        parsed = _parse_ticket_text_fields(original_message)
        actor_override = _staff_actor_from_call(call)

        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_mass_incident] {lock_reason}")
            return

        _remove_ticket_buttons(call.message.chat.id, call.message.message_id)

        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"⚠️ МАССОВЫЙ ИНЦИДЕНТ ⚠️\n\n"
            f"{original_message}\n\n"
            f"👤 Отметил: {actor_override['name']}",
            message_thread_id=IN_PROGRESS_THREAD_ID
        )

        _mark_ticket_mass_incident(ticket_number, parsed.get('problem') or original_message, actor_override)
        bot.answer_callback_query(call.id, "⚠️ Массовый инцидент зафиксирован")
        print(f"✅ Массовый инцидент обработан ticket_number={ticket_number}")
    except Exception as e:
        print(f"❌ Ошибка при обработке массового инцидента: {e}")
        traceback.print_exc()


@bot.callback_query_handler(func=lambda call: call.data == "ticket_transferred_up")
def handle_ticket_transferred_up(call):
    print(f"🔔 Получен callback от кнопки 'Передано выше'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        original_message = call.message.text or call.message.caption or "Детали заявки недоступны"
        ticket_number = extract_ticket_number_from_text(original_message)
        if ticket_number is None:
            bot.answer_callback_query(call.id, "Не нашёл номер заявки")
            return
        parsed = _parse_ticket_text_fields(original_message)
        actor_override = _staff_actor_from_call(call)

        lock_reason = _ticket_action_lock_reason(ticket_number)
        if lock_reason:
            _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, lock_reason)
            print(f"⛔ [handle_ticket_transferred_up] {lock_reason}")
            return

        _remove_ticket_buttons(call.message.chat.id, call.message.message_id)
        _mark_ticket_transferred_up(
            ticket_number,
            parsed.get('problem') or original_message,
            actor_override,
            original_message
        )
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"⬆️ ЗАЯВКА ПЕРЕДАНА ВЫШЕ ⬆️\n\n"
            f"{original_message}\n\n"
            f"👤 Передал: {actor_override['name']}",
            message_thread_id=IN_PROGRESS_THREAD_ID
        )
        bot.answer_callback_query(call.id, "⬆️ Заявка отмечена как переданная выше")
        print(f"✅ Передача выше обработана ticket_number={ticket_number}")
    except Exception as e:
        print(f"❌ Ошибка при обработке передачи выше: {e}")
        traceback.print_exc()


def send_solved_ticket(problem):
    user_info = session.get('user_info')
    if user_info:
        department = user_info.get('department', 'Неизвестно')
        name = user_info.get('name', 'Неизвестно')
        workplace = user_info.get('workplace', 'Неизвестно')

        support_message = (
            f"✅ *ПРОБЛЕМА РЕШЕНА Помощником* ✅\n"
            f"Отдел: {escape_markdown(department)}\n"
            f"Имя: {escape_markdown(name)}\n"
            f"Рабочее место: {escape_markdown(workplace)}\n"
            f"Проблема: {escape_markdown(problem)}"
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
                f"📹 *ВИДЕО-МАНУАЛ ПОМОГ* ✅\n"
                f"Отдел: {escape_markdown(department)}\n"
                f"Имя: {escape_markdown(name)}\n"
                f"Рабочее место: {escape_markdown(workplace)}\n"
                f"Проблема: {escape_markdown(problem)}"
            )
            thread_id = SOLVED_TICKETS_THREAD_ID
            result_type = RESULT_VIDEO_HELPED
        else:
            support_message = (
                f"📹 *ВИДЕО-МАНУАЛ НЕ ПОМОГ* ❌\n"
                f"Отдел: {escape_markdown(department)}\n"
                f"Имя: {escape_markdown(name)}\n"
                f"Рабочее место: {escape_markdown(workplace)}\n"
                f"Проблема: {escape_markdown(problem)}\n"
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
        photo_id = photo.get('id')
        url = get_file_url(photo_id) if photo_id else None
        caption = photo.get('caption', '')
        safe_caption = m_escape(str(caption).strip()[:300])
        # Добавляем ВСЕ шаги, даже если фото отсутствует (url = None)
        photo_urls_with_captions.append({'url': url, 'caption': safe_caption})

    safe_manual_data = deep_escape(data)
    safe_photos = deep_escape(photo_urls_with_captions)

    # Формируем back_url в зависимости от типа мануала
    if subproblem_id:
        back_url = url_for('select_problem', problem_id=problem_id)
    else:
        back_url = url_for('show_problems')

    return render_template(
        'manual.html',
        manual=safe_manual_data,
        manual_title=manual_title,
        photo_urls_with_captions=safe_photos,
        video_data=None,  # Не показываем видео
        skip_video_feedback=True,  # Флаг чтобы не показывать опрос по видео
        problem_id=problem_id,
        back_url=back_url
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


@app.route('/my_tickets')
def my_tickets():
    """Страница заявок текущего пользователя."""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))
    return render_template('my_tickets.html', user_info=session['user_info'])


@app.route('/contacts_kc')
def contacts_kc():
    """Страница контактов контакт-центра."""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    user_info = session.get('user_info') or {}
    q = request.args.get('q', '').strip()
    department = request.args.get('department', '').strip()
    selected_department = contacts_mgr._canonical_department(department) if department else ''
    per_page = 5000
    page = 1
    departments = contacts_mgr.get_departments(include_inactive=False)
    page_data = _contacts_page_data(q, department, page, per_page, user_info)
    contact_tree = contacts_mgr.build_contact_hierarchy(page_data['contacts'])
    department_tree = contacts_mgr.build_department_hierarchy(departments)
    stats = contacts_mgr.get_stats()
    return render_template(
        'contacts_kc.html',
        user_info=user_info,
        departments=departments,
        department_tree=department_tree,
        contacts=page_data['contacts'],
        contact_tree=contact_tree,
        stats=stats,
        q=q,
        selected_department=selected_department,
        page=page_data['page'],
        per_page=page_data['per_page'],
        total=page_data['total'],
        total_pages=page_data['total_pages'],
        has_more=page_data['has_more']
    )


def _contacts_page_args():
    page = max(1, request.args.get('page', 1, type=int))
    per_page = request.args.get('per_page', 48, type=int)
    if per_page not in (24, 48, 96, 5000):
        per_page = 48
    return page, per_page


def _contacts_page_data(q: str, department: str, page: int, per_page: int, user_info: dict):
    total = contacts_mgr.count_contacts(q=q, department=department, status='active')
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, int(page or 1)), total_pages)
    offset = (page - 1) * per_page
    contacts = contacts_mgr.get_contacts(
        q=q,
        department=department,
        status='active',
        limit=per_page,
        offset=offset
    )
    actor_key = _contact_like_actor_key(user_info)
    liked_ids = contacts_mgr.get_liked_contact_ids(actor_key, [c.get('id') for c in contacts])
    for contact in contacts:
        contact['liked_by_user'] = contact.get('id') in liked_ids
    return {
        'contacts': contacts,
        'page': page,
        'per_page': per_page,
        'total': total,
        'total_pages': total_pages,
        'has_more': page < total_pages,
    }


def _contact_api_row(contact: dict) -> dict:
    photo_path = contact.get('photo_path') or ''
    return {
        'id': contact.get('id'),
        'full_name': contact.get('full_name') or '',
        'position': contact.get('position') or '',
        'department': contact.get('department') or '',
        'department_group': contact.get('department_group') or contact.get('department') or 'Без отдела',
        'hierarchy_path': contact.get('hierarchy_path') or [],
        'hierarchy_kinds': contact.get('hierarchy_kinds') or [],
        'phone': contact.get('phone') or '',
        'extension': contact.get('extension') or '',
        'mobile': contact.get('mobile') or '',
        'email': contact.get('email') or '',
        'telegram': contact.get('telegram') or '',
        'nickname': contact.get('nickname') or '',
        'group_role': contact.get('group_role') or 'specialist',
        'group_role_label': contact.get('group_role_label') or '',
        'contact_role_class': contact.get('contact_role_class') or '',
        'workplace': contact.get('workplace') or '',
        'schedule': contact.get('schedule') or '',
        'languages': contact.get('languages') or '',
        'responsibilities': contact.get('responsibilities') or '',
        'notes': contact.get('notes') or '',
        'tags': contact.get('tags') or '',
        'photo_url': url_for('static', filename=photo_path) if photo_path else '',
        'liked_by_user': bool(contact.get('liked_by_user')),
        'likes_count': int(contact.get('likes_count') or 0),
        'is_group_lead': bool(contact.get('is_group_lead')),
        'is_senior_contact': bool(contact.get('is_senior_contact')),
    }


@app.route('/api/contacts_kc')
@rate_limit(max_requests=60, window=60)
def api_contacts_kc():
    """Порционная загрузка контактов для бесконечной прокрутки."""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    user_info = session.get('user_info') or {}
    q = request.args.get('q', '').strip()
    department = request.args.get('department', '').strip()
    page, per_page = _contacts_page_args()
    data = _contacts_page_data(q, department, page, per_page, user_info)
    return jsonify({
        'success': True,
        'contacts': [_contact_api_row(contact) for contact in data['contacts']],
        'page': data['page'],
        'per_page': data['per_page'],
        'total': data['total'],
        'total_pages': data['total_pages'],
        'has_more': data['has_more'],
    })


def _contact_like_actor_key(user_info: dict | None = None) -> str:
    user_info = user_info if user_info is not None else (session.get('user_info') or {})
    return str(
        user_info.get('username')
        or user_info.get('email')
        or user_info.get('name')
        or ''
    ).strip().lower()


@app.route('/contacts_kc/<int:contact_id>/like', methods=['POST'])
def contacts_kc_toggle_like(contact_id):
    """Поставить или снять лайк с сотрудника."""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    user_info = session.get('user_info') or {}
    actor_key = _contact_like_actor_key(user_info)
    actor_name = user_info.get('name') or user_info.get('username') or actor_key
    result = contacts_mgr.toggle_like(contact_id, actor_key, actor_name)
    status = 200 if result.get('success') else 400
    return jsonify(result), status


def _contact_form_data() -> dict:
    return {
        'full_name': request.form.get('full_name', '').strip(),
        'position': request.form.get('position', '').strip(),
        'department': request.form.get('department', '').strip(),
        'phone': request.form.get('phone', '').strip(),
        'extension': request.form.get('extension', '').strip(),
        'mobile': request.form.get('mobile', '').strip(),
        'email': request.form.get('email', '').strip(),
        'telegram': request.form.get('telegram', '').strip(),
        'nickname': request.form.get('nickname', '').strip(),
        'group_role': request.form.get('group_role', 'specialist').strip(),
        'workplace': request.form.get('workplace', '').strip(),
        'schedule': request.form.get('schedule', '').strip(),
        'languages': request.form.get('languages', '').strip(),
        'responsibilities': request.form.get('responsibilities', '').strip(),
        'notes': request.form.get('notes', '').strip(),
        'tags': request.form.get('tags', '').strip(),
        'sort_order': request.form.get('sort_order', '0').strip(),
        'is_active': '1' if request.form.get('is_active') else '0',
    }


def _normalize_department_key(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def _contact_department_options() -> dict:
    options = {}
    for item in contacts_mgr.get_departments(include_inactive=True):
        name = str(item.get('name') or '').strip()
        key = _normalize_department_key(name)
        if key and key not in options:
            options[key] = name
    return options


def _validated_contact_form_data():
    data = _contact_form_data()
    departments = _contact_department_options()
    department_key = _normalize_department_key(data.get('department'))
    if not department_key or department_key not in departments:
        return data, 'Выберите отдел из списка'
    data['department'] = departments[department_key]
    return data, None


CONTACT_PHOTO_UPLOAD_DIR = os.path.join(BASE_DIR, 'static', 'uploads', 'contact_photos')
CONTACT_PHOTO_URL_PREFIX = 'uploads/contact_photos'
CONTACT_PHOTO_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'jfif', 'avif'}
CONTACT_PHOTO_MAX_BYTES = 10 * 1024 * 1024
CONTACT_PHOTO_MIME_EXTENSIONS = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'image/bmp': 'bmp',
    'image/avif': 'avif',
}


def _contact_photo_selected(file) -> bool:
    return bool(file and (file.filename or (file.mimetype or '').lower().startswith('image/')))


def _contact_photo_stream_size(file) -> int:
    stream = getattr(file, 'stream', None)
    if not stream:
        return 0
    try:
        current = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current)
        return int(size)
    except (OSError, ValueError):
        return int(request.content_length or 0)


def _contact_photo_header(file, length: int = 64) -> bytes:
    stream = getattr(file, 'stream', None)
    if not stream:
        return b''
    try:
        current = stream.tell()
        stream.seek(0)
        header = stream.read(length) or b''
        stream.seek(current)
        return header
    except (OSError, ValueError):
        return b''


def _detect_contact_photo_extension(header: bytes) -> str:
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if header.startswith(b'BM'):
        return 'bmp'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    if len(header) >= 12 and header[4:8] == b'ftyp' and (b'avif' in header[:32] or b'avis' in header[:32]):
        return 'avif'
    return ''


def _validate_contact_photo(file) -> str:
    if not _contact_photo_selected(file):
        return ''

    original_name = secure_filename(file.filename or 'screenshot')
    filename_ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    if filename_ext in ('jpeg', 'jfif'):
        filename_ext = 'jpg'
    if filename_ext and filename_ext not in CONTACT_PHOTO_EXTENSIONS:
        raise ValueError('Фото должно быть в формате JPG, PNG, WEBP, GIF, BMP, JFIF или AVIF')

    size = _contact_photo_stream_size(file)
    if size > CONTACT_PHOTO_MAX_BYTES:
        raise ValueError('Фото должно быть не больше 10 МБ')

    detected_ext = _detect_contact_photo_extension(_contact_photo_header(file))
    if not detected_ext:
        raise ValueError('Файл не похож на изображение JPG, PNG, WEBP, GIF, BMP или AVIF')

    if filename_ext and filename_ext != detected_ext:
        # JFIF is a JPEG container, so it is already normalized to jpg above.
        raise ValueError('Расширение файла не совпадает с фактическим форматом изображения')

    try:
        file.stream.seek(0)
    except (OSError, ValueError, AttributeError):
        pass
    return detected_ext


def _contact_photo_error() -> str:
    try:
        _validate_contact_photo(request.files.get('photo'))
    except ValueError as e:
        return str(e)
    return ''


def _safe_remove_contact_photo(photo_path: str):
    if not photo_path:
        return
    normalized = str(photo_path).replace('\\', '/').lstrip('/')
    if not normalized.startswith(CONTACT_PHOTO_URL_PREFIX + '/'):
        return
    full_path = os.path.join(BASE_DIR, 'static', *normalized.split('/'))
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
    except OSError as e:
        print(f"[contacts] Не удалось удалить фото {full_path}: {e}")


def _save_contact_photo(contact_id: int) -> str:
    file = request.files.get('photo')
    if not _contact_photo_selected(file):
        return ''

    ext = _validate_contact_photo(file)

    os.makedirs(CONTACT_PHOTO_UPLOAD_DIR, exist_ok=True)
    filename = f"{contact_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
    full_path = os.path.join(CONTACT_PHOTO_UPLOAD_DIR, filename)
    file.save(full_path)
    return f"{CONTACT_PHOTO_URL_PREFIX}/{filename}"


def _apply_contact_photo(contact_id: int):
    actor = session.get('admin_username', '')
    current = contacts_mgr.get_contact(contact_id)
    if not current:
        return

    try:
        photo_path = _save_contact_photo(contact_id)
    except ValueError as e:
        flash(str(e), 'error')
        return

    if photo_path:
        _safe_remove_contact_photo(current.get('photo_path') or '')
        contacts_mgr.update_contact_photo(contact_id, photo_path, actor=actor)
        return

    if request.form.get('clear_photo') == 'on':
        _safe_remove_contact_photo(current.get('photo_path') or '')
        contacts_mgr.update_contact_photo(contact_id, '', actor=actor)


def _admin_contacts_return():
    args = {}
    for key in ('q', 'department', 'status', 'page'):
        value = request.form.get(f'return_{key}', '').strip()
        if value:
            args[key] = value
    return redirect(url_for('admin_contacts_kc', **args))


@app.route('/admin/contacts')
@app.route('/admin/contacts_kc')
@AdminAuth.manuals_required
def admin_contacts_kc():
    """Админка контактов КЦ."""
    q = request.args.get('q', '').strip()
    department = request.args.get('department', '').strip()
    status = request.args.get('status', 'active').strip()
    if status not in ('active', 'inactive', 'all'):
        status = 'active'
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 100
    offset = (page - 1) * per_page

    total = contacts_mgr.count_contacts(q=q, department=department, status=status)
    contacts = contacts_mgr.get_contacts(
        q=q,
        department=department,
        status=status,
        limit=per_page,
        offset=offset
    )
    departments = contacts_mgr.get_departments(include_inactive=True)
    custom_departments = contacts_mgr.get_custom_departments()
    position_options = contacts_mgr.get_position_options()
    stats = contacts_mgr.get_stats()

    return render_template(
        'admin_contacts_kc.html',
        contacts=contacts,
        departments=departments,
        custom_departments=custom_departments,
        position_options=position_options,
        stats=stats,
        q=q,
        selected_department=department,
        status=status,
        page=page,
        per_page=per_page,
        total=total
    )


@app.route('/admin/contacts_kc/departments/create', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_department_create():
    data = {
        'name': request.form.get('name', '').strip(),
        'parent_name': request.form.get('parent_name', '').strip(),
        'kind': request.form.get('kind', 'group').strip(),
        'sort_order': request.form.get('sort_order', '0').strip(),
    }
    result = contacts_mgr.create_department(data, actor=session.get('admin_username', ''))
    if result.get('success'):
        flash('Группа или отдел добавлены', 'success')
        write_audit_log('contact_department_created', 200, {'department_id': result.get('id')})
    else:
        flash(result.get('error', 'Не удалось добавить группу или отдел'), 'error')
    return _admin_contacts_return()


@app.route('/admin/contacts_kc/departments/<int:department_id>/delete', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_department_delete(department_id):
    result = contacts_mgr.delete_department(department_id)
    if result.get('success'):
        flash('Группа или отдел удалены', 'success')
        write_audit_log('contact_department_deleted', 200, {'department_id': department_id})
    else:
        flash(result.get('error', 'Не удалось удалить группу или отдел'), 'error')
    return _admin_contacts_return()


@app.route('/admin/contacts_kc/contacts/create', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_create():
    data, error = _validated_contact_form_data()
    if error:
        flash(error, 'error')
        return _admin_contacts_return()

    photo_error = _contact_photo_error()
    if photo_error:
        flash(photo_error, 'error')
        return _admin_contacts_return()

    result = contacts_mgr.create_contact(data, actor=session.get('admin_username', ''))
    if result.get('success'):
        _apply_contact_photo(int(result.get('id')))
        flash('Контакт добавлен', 'success')
        write_audit_log('contact_created', 200, {'contact_id': result.get('id')})
    else:
        flash(result.get('error', 'Не удалось добавить контакт'), 'error')
    return _admin_contacts_return()


@app.route('/admin/contacts_kc/contacts/<int:contact_id>/update', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_update(contact_id):
    data, error = _validated_contact_form_data()
    if error:
        flash(error, 'error')
        return _admin_contacts_return()

    photo_error = _contact_photo_error()
    if photo_error:
        flash(photo_error, 'error')
        return _admin_contacts_return()

    result = contacts_mgr.update_contact(contact_id, data, actor=session.get('admin_username', ''))
    if result.get('success'):
        _apply_contact_photo(contact_id)
        flash('Контакт обновлён', 'success')
        write_audit_log('contact_updated', 200, {'contact_id': contact_id})
    else:
        flash(result.get('error', 'Не удалось обновить контакт'), 'error')
    return _admin_contacts_return()


@app.route('/admin/contacts_kc/contacts/<int:contact_id>/toggle', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_toggle(contact_id):
    is_active = request.form.get('is_active') == '1'
    contacts_mgr.set_contact_active(contact_id, is_active, actor=session.get('admin_username', ''))
    flash('Статус контакта обновлён', 'success')
    write_audit_log('contact_status_changed', 200, {'contact_id': contact_id, 'is_active': is_active})
    return _admin_contacts_return()


@app.route('/admin/contacts_kc/contacts/<int:contact_id>/delete', methods=['POST'])
@AdminAuth.manuals_required
def admin_contact_delete(contact_id):
    contact = contacts_mgr.get_contact(contact_id)
    if contact:
        _safe_remove_contact_photo(contact.get('photo_path') or '')
    contacts_mgr.delete_contact(contact_id)
    flash('Контакт удалён', 'success')
    write_audit_log('contact_deleted', 200, {'contact_id': contact_id})
    return _admin_contacts_return()


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

    # Проверка рабочего времени
    working, off_hours_msg = is_working_hours()
    if not working:
        return render_template('off_hours.html', message=off_hours_msg, schedule_info=_ticket_schedule_summary())

    try:
        selected_topic_id = request.form.get('selected_topic_id')
        selected_topic_name = request.form.get('selected_topic_name')
        selected_topic_similarity = request.form.get('selected_topic_similarity')

        if not selected_topic_id or not selected_topic_name:
            flash('Не выбрана тематика')
            return redirect(url_for('search_topics'))

        session['pending_selected_topic_ticket'] = {
            'id': selected_topic_id,
            'name': selected_topic_name,
            'similarity': selected_topic_similarity
        }
        session.modified = True

        telegram_redirect = _require_telegram_username_for_ticket('submit_pending_selected_topic')
        if telegram_redirect:
            return telegram_redirect

        return _send_selected_topic_ticket(selected_topic_id, selected_topic_name, selected_topic_similarity)

    except Exception as e:
        print(f"[submit_selected_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при отправке заявки')
        return redirect(url_for('search_topics'))


def _send_selected_topic_ticket(selected_topic_id, selected_topic_name, selected_topic_similarity):
    topic_info = {
        'id': selected_topic_id,
        'topic': selected_topic_name,
        'similarity': selected_topic_similarity
    }

    msg = send_ticket(f"Запрос по тематике: {selected_topic_name}", None, topic_info)
    if msg is None:
        return render_template(
            'ticket_send_failed.html',
            ticket_number=session.get('current_ticket_number')
        ), 503

    session.pop('pending_selected_topic_ticket', None)
    # Очищаем сессию и показываем страницу успеха: сохраняем существующее поведение этого маршрута.
    session.clear()
    return render_template('ticket_sent.html')


@app.route('/submit_pending_selected_topic')
def submit_pending_selected_topic():
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    pending = session.get('pending_selected_topic_ticket') or {}
    selected_topic_id = pending.get('id')
    selected_topic_name = pending.get('name')
    selected_topic_similarity = pending.get('similarity')
    if not selected_topic_id or not selected_topic_name:
        return redirect(url_for('search_topics'))

    working, off_hours_msg = is_working_hours()
    if not working:
        return render_template('off_hours.html', message=off_hours_msg, schedule_info=_ticket_schedule_summary())

    telegram_redirect = _require_telegram_username_for_ticket('submit_pending_selected_topic')
    if telegram_redirect:
        return telegram_redirect

    return _send_selected_topic_ticket(selected_topic_id, selected_topic_name, selected_topic_similarity)

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
    safe_problem_id = m_escape(problem_id)

    # --- Есть подпроблемы ---
    if 'subproblems' in problem_data and isinstance(problem_data['subproblems'], dict):
        session['problem_id'] = problem_id

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
        session['problem_id'] = problem_id
        session.pop('current_subproblem_id', None)  # Очищаем старый subproblem_id

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
            video_data=video_data,
            problem_id=safe_problem_id,
            back_url=url_for('show_problems')
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
        telegram_redirect = _require_telegram_username_for_ticket('show_manual', {'subproblem_id': subproblem_id})
        if telegram_redirect:
            return telegram_redirect
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
        video_data=safe_video,
        problem_id=problem_id,
        back_url=url_for('select_problem', problem_id=problem_id)
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

    telegram_redirect = _require_telegram_username_for_ticket('other_problem', {'type': other_problem_type})
    if telegram_redirect:
        return telegram_redirect

    # Проверка графика приёма заявок.
    working, off_hours_msg = is_working_hours()
    if not working:
        return render_template('off_hours.html', message=off_hours_msg, schedule_info=_ticket_schedule_summary())

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
        msg = send_ticket(problem_description, screenshots, topic_info, video=video_file, thread_id=target_thread_id)
        if msg is None:
            return render_template(
                'ticket_send_failed.html',
                ticket_number=session.get('current_ticket_number')
            ), 503
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
        # Проверка рабочего времени (эскалация после мануала — не cisco)
        working, off_hours_msg = is_working_hours()
        if not working:
            return render_template('off_hours.html', message=off_hours_msg, schedule_info=_ticket_schedule_summary())

        # Проверяем флаг - была ли уже отправлена заявка
        if session.get('ticket_sent'):
            # Заявка уже отправлена, просто показываем страницу
            return render_template('ticket_sent.html')

        telegram_redirect = _require_telegram_username_for_ticket('send_final_ticket')
        if telegram_redirect:
            return telegram_redirect

        # Отправляем заявку только если флаг не установлен
        problem_description = session.get('problem_title', 'Неизвестная проблема')
        msg = send_ticket(problem_description)

        # Явно фиксируем, что текстовый мануал не помог (пользователь эскалировал в заявку)
        # Это нужно, чтобы "Не помогло / Заявки" корректно считалось даже без доп.статуса.
        log_ticket_event(
            event_type='manual_not_helped',
            ticket_number=session.get('current_ticket_number'),
            problem=problem_description,
            details={
                'source': 'manual_feedback',
                'feedback': 'manual_not_helped',
                'next_step': 'ticket',
                'ticket_delivery': 'sent' if msg is not None else 'failed'
            }
        )

        if msg is None:
            return render_template(
                'ticket_send_failed.html',
                ticket_number=session.get('current_ticket_number'),
                retry_url=url_for('send_final_ticket')
            ), 503

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


def _session_owns_ticket(ticket_number: int) -> bool:
    try:
        ticket_number = int(ticket_number)
        if int(session.get('current_ticket_number') or 0) == ticket_number:
            return True
        return _ticket_belongs_to_current_user(ticket_number)
    except (TypeError, ValueError):
        return False


def _ticket_belongs_to_current_user(ticket_number: int, state: dict | None = None) -> bool:
    if 'user_info' not in session or not session.get('authenticated'):
        return False
    user_info = session.get('user_info', {}) or {}
    username = str(user_info.get('username') or '').strip().lower()
    name = str(user_info.get('name') or '').strip().lower()
    department = str(user_info.get('department') or '').strip().lower()
    workplace = str(user_info.get('workplace') or '').strip().lower()

    state = state or _get_ticket_state(ticket_number)
    if not state:
        return False

    creator_username = str(state.get('creator_username') or '').strip().lower()
    if username and creator_username and username == creator_username:
        return True

    state_name = str(state.get('user_name') or state.get('creator_name') or '').strip().lower()
    state_department = str(state.get('department') or '').strip().lower()
    state_workplace = str(state.get('workplace') or '').strip().lower()
    if name and state_name and name == state_name:
        if department and state_department and department != state_department:
            return False
        if workplace and state_workplace and workplace != state_workplace:
            return False
        return True
    return False


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def _auto_confirm_if_feedback_expired(state: dict, actor: dict | None = None) -> bool:
    if not state or state.get('status') != 'ready_for_feedback':
        return False
    ready_at = _parse_datetime(state.get('ready_at') or '')
    if not ready_at:
        return False
    if (datetime.now() - ready_at).total_seconds() < USER_FEEDBACK_TIMEOUT_SECONDS:
        return False

    ticket_number = int(state.get('ticket_number') or 0)
    if not ticket_number:
        return False
    system_actor = actor or {'name': 'Helper', 'username': 'helper-system', 'role': 'system'}
    log_ticket_event(
        event_type='ticket_user_confirmed_resolved',
        ticket_number=ticket_number,
        problem=state.get('problem') or '',
        actor_override=system_actor,
        details={
            'feedback': 'auto_resolved',
            'reason': 'feedback_timeout',
            'timeout_seconds': USER_FEEDBACK_TIMEOUT_SECONDS
        }
    )
    _send_support_message(
        f"✅ <b>Заявка авто-подтверждена</b>\n\n"
        f"№{ticket_number}\n"
        f"Инициатор не ответил за {USER_FEEDBACK_TIMEOUT_SECONDS // 60} мин.\n"
        f"Статус: Решено",
        thread_id=SOLVED_TICKETS_THREAD_ID or IN_PROGRESS_THREAD_ID,
        parse_mode='HTML'
    )
    return True


def _auto_confirm_expired_feedback_tickets():
    states = _load_ticket_states()
    for state in states.values():
        try:
            _auto_confirm_if_feedback_expired(state)
        except Exception as e:
            print(f"[ticket_auto_confirm] Ошибка обработки заявки {state.get('ticket_number')}: {e}")


def _ticket_auto_confirm_worker():
    print(f"[ticket_auto_confirm] Worker запущен, timeout={USER_FEEDBACK_TIMEOUT_SECONDS}s")
    while True:
        sleep(60)
        try:
            _auto_confirm_expired_feedback_tickets()
        except Exception as e:
            print(f"[ticket_auto_confirm] Ошибка фоновой проверки: {e}")


def _start_ticket_auto_confirm_worker():
    enabled = os.getenv('ENABLE_TICKET_AUTO_CONFIRM_WORKER', 'true').lower() not in ('0', 'false', 'no')
    if APP_TEST_MODE or not enabled:
        return
    worker = threading.Thread(target=_ticket_auto_confirm_worker, name='ticket-auto-confirm', daemon=True)
    worker.start()


_start_ticket_auto_confirm_worker()


@app.route('/api/ticket_status/<int:ticket_number>')
@rate_limit(max_requests=30, window=60)
def api_ticket_status(ticket_number: int):
    if 'user_info' not in session or not session.get('authenticated') or not _session_owns_ticket(ticket_number):
        return jsonify({'success': False, 'error': 'Недоступно'}), 403
    try:
        state = _get_ticket_state(ticket_number)
        if not state:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
        if _auto_confirm_if_feedback_expired(state):
            state = _get_ticket_state(ticket_number) or state
        return jsonify({
            'success': True,
            'ticket_number': ticket_number,
            'status': state.get('status'),
            'status_label': state.get('status_label'),
            'can_feedback': state.get('status') == 'ready_for_feedback',
            'can_resubmit': state.get('status') == 'rejected' and not state.get('resubmitted_ticket_number'),
            'resubmitted_ticket_number': state.get('resubmitted_ticket_number'),
            'ready_at': state.get('ready_at') or '',
            'assigned_name': state.get('assigned_name') or '',
            'assigned_username': state.get('assigned_username') or '',
            'transferred_by': state.get('transferred_by') or '',
        })
    except Exception as e:
        print(f"[api_ticket_status] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статуса заявки'}), 500


@app.route('/api/my_tickets')
@rate_limit(max_requests=30, window=60)
def api_my_tickets():
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Недоступно'}), 403
    try:
        rows = []
        for state in _load_ticket_states().values():
            if not _ticket_belongs_to_current_user(state.get('ticket_number'), state):
                continue
            if _auto_confirm_if_feedback_expired(state):
                state = _get_ticket_state(state.get('ticket_number')) or state
            rows.append({
                'ticket_number': state.get('ticket_number'),
                'status': state.get('status') or 'unknown',
                'status_label': state.get('status_label') or TICKET_STATUS_LABELS['unknown'],
                'problem': state.get('problem') or 'Без описания',
                'department': state.get('department') or '',
                'workplace': state.get('workplace') or '',
                'created_at': state.get('created_at') or '',
                'updated_at': state.get('updated_at') or '',
                'ready_at': state.get('ready_at') or '',
                'closed_at': state.get('closed_at') or '',
                'assigned_name': state.get('assigned_name') or '',
                'assigned_username': state.get('assigned_username') or '',
                'resolved_by': state.get('resolved_by') or '',
                'transferred_by': state.get('transferred_by') or '',
                'reject_reason': state.get('reject_reason') or '',
                'resubmitted_from': state.get('resubmitted_from'),
                'resubmitted_ticket_number': state.get('resubmitted_ticket_number'),
                'is_cisco': bool(state.get('is_cisco')),
                'can_feedback': state.get('status') == 'ready_for_feedback',
                'can_resubmit': state.get('status') == 'rejected' and not state.get('resubmitted_ticket_number')
            })
        rows.sort(key=lambda item: item.get('created_at') or item.get('updated_at') or '', reverse=True)
        return jsonify({'success': True, 'data': rows, 'total': len(rows)})
    except Exception as e:
        print(f"[api_my_tickets] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения заявок'}), 500


@app.route('/api/ticket_resubmit/<int:ticket_number>', methods=['POST'])
@rate_limit(max_requests=5, window=60)
def api_ticket_resubmit(ticket_number: int):
    if 'user_info' not in session or not session.get('authenticated') or not _session_owns_ticket(ticket_number):
        return jsonify({'success': False, 'error': 'Недоступно'}), 403
    try:
        state = _get_ticket_state(ticket_number)
        if not state:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
        if state.get('status') != 'rejected':
            return jsonify({'success': False, 'error': 'Повторно можно отправить только отклонённую заявку'}), 409

        existing_resubmission = state.get('resubmitted_ticket_number')
        if existing_resubmission:
            return jsonify({
                'success': True,
                'ticket_number': ticket_number,
                'new_ticket_number': existing_resubmission,
                'status': state.get('status'),
                'status_label': state.get('status_label'),
                'message': f'Заявка уже отправлена повторно как №{existing_resubmission}'
            })

        working, off_hours_msg = is_working_hours()
        if not working:
            return jsonify({'success': False, 'error': off_hours_msg or 'Сейчас заявки не принимаются'}), 409

        telegram_redirect = _require_telegram_username_for_ticket('my_tickets')
        if telegram_redirect:
            return jsonify({
                'success': False,
                'error': 'Перед повторной отправкой укажите Telegram username',
                'redirect_url': url_for('enter_telegram_username')
            }), 409

        user_info = session.get('user_info', {}) or {}
        actor = {
            'name': user_info.get('name') or user_info.get('username') or 'Инициатор',
            'username': user_info.get('username') or '',
            'role': 'user'
        }
        original_user_info = {
            'department': state.get('department') or user_info.get('department') or '',
            'name': state.get('user_name') or user_info.get('name') or user_info.get('username') or '',
            'workplace': state.get('workplace') or user_info.get('workplace') or '',
        }
        problem = state.get('problem') or 'Повторная заявка'
        target_thread_id = CISCO_TICKETS_THREAD_ID if state.get('is_cisco') and CISCO_TICKETS_THREAD_ID else NEW_TICKETS_THREAD_ID

        msg = send_ticket(
            problem,
            thread_id=target_thread_id,
            resubmitted_from=ticket_number,
            resubmit_reason=state.get('reject_reason') or '',
            user_info_override=original_user_info,
            extra_details={'source': 'rejected_ticket_resubmit'},
            problem_id_override=state.get('problem_id') or '',
            subproblem_id_override=state.get('subproblem_id') or ''
        )
        new_ticket_number = int(session.get('current_ticket_number') or 0)
        if msg is None or not new_ticket_number:
            return jsonify({
                'success': False,
                'error': 'Не удалось отправить повторную заявку'
            }), 503

        log_ticket_event(
            event_type='ticket_resubmitted_by_user',
            ticket_number=ticket_number,
            problem=problem,
            is_cisco=bool(state.get('is_cisco')),
            actor_override=actor,
            user_info_override=original_user_info,
            details={
                'new_ticket_number': new_ticket_number,
                'source': 'my_tickets',
                'warning_acknowledged': True,
                'old_status': state.get('status') or ''
            },
            problem_id_override=state.get('problem_id') or '',
            subproblem_id_override=state.get('subproblem_id') or ''
        )
        write_audit_log('ticket_resubmitted', 200, {
            'ticket_number': ticket_number,
            'new_ticket_number': new_ticket_number
        })

        return jsonify({
            'success': True,
            'ticket_number': ticket_number,
            'new_ticket_number': new_ticket_number,
            'message': f'Повторная заявка №{new_ticket_number} отправлена'
        })
    except Exception as e:
        print(f"[api_ticket_resubmit] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка повторной отправки заявки'}), 500


@app.route('/api/ticket_feedback/<int:ticket_number>', methods=['POST'])
@rate_limit(max_requests=10, window=60)
def api_ticket_feedback(ticket_number: int):
    if 'user_info' not in session or not session.get('authenticated') or not _session_owns_ticket(ticket_number):
        return jsonify({'success': False, 'error': 'Недоступно'}), 403
    try:
        state = _get_ticket_state(ticket_number)
        if not state:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
        if _auto_confirm_if_feedback_expired(state):
            return jsonify({'success': False, 'error': 'Заявка уже автоматически закрыта'}), 409
        if state.get('status') != 'ready_for_feedback':
            return jsonify({'success': False, 'error': 'Заявка пока не ожидает подтверждения'}), 409

        payload = request.get_json(silent=True) or {}
        action = str(payload.get('action') or '').strip()
        problem = state.get('problem') or session.get('problem_title') or ''
        user_info = session.get('user_info', {}) or {}
        actor = {
            'name': user_info.get('name') or user_info.get('username') or 'Инициатор',
            'username': user_info.get('username') or '',
            'role': 'user'
        }

        if action == 'resolved':
            log_ticket_event(
                event_type='ticket_user_confirmed_resolved',
                ticket_number=ticket_number,
                problem=problem,
                actor_override=actor,
                details={'feedback': 'resolved'}
            )
            _send_support_message(
                f"✅ <b>Заявка подтверждена инициатором</b>\n\n"
                f"№{ticket_number}\n"
                f"Сотрудник: {html_escape(actor['name'])}\n"
                f"Статус: Решено",
                thread_id=SOLVED_TICKETS_THREAD_ID or IN_PROGRESS_THREAD_ID,
                parse_mode='HTML'
            )
        elif action == 'not_resolved':
            log_ticket_event(
                event_type='ticket_reopened_by_user',
                ticket_number=ticket_number,
                problem=problem,
                actor_override=actor,
                details={'feedback': 'not_resolved'}
            )
            duty_actor = _current_duty_actor()
            _assign_ticket_to_current_duty(ticket_number, problem)
            _send_reopened_ticket_card(ticket_number, state, duty_actor)
            _send_support_message(
                f"🔁 <b>Заявка переоткрыта инициатором</b>\n\n"
                f"№{ticket_number}\n"
                f"Сотрудник: {html_escape(actor['name'])}\n"
                f"Назначено: {_format_duty_mention(duty_actor)}",
                thread_id=NEW_TICKETS_THREAD_ID or IN_PROGRESS_THREAD_ID,
                parse_mode='HTML'
            )
        else:
            return jsonify({'success': False, 'error': 'Неверное действие'}), 400

        new_state = _get_ticket_state(ticket_number) or {}
        return jsonify({
            'success': True,
            'ticket_number': ticket_number,
            'status': new_state.get('status'),
            'status_label': new_state.get('status_label')
        })
    except Exception as e:
        print(f"[api_ticket_feedback] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка сохранения обратной связи'}), 500


# --- Обновлённый маршрут finish_unsolved с логированием ---
@app.route('/finish_unsolved')
def finish_unsolved():
    # Security: require user_info in session
    if 'user_info' not in session:
        return redirect(url_for('index'))

    try:
        working, off_hours_msg = is_working_hours()
        if not working:
            return render_template('off_hours.html', message=off_hours_msg, schedule_info=_ticket_schedule_summary())

        telegram_redirect = _require_telegram_username_for_ticket('finish_unsolved')
        if telegram_redirect:
            return telegram_redirect

        # Security: only use session data, not query params (prevent injection)
        problem_description = session.get('problem_title', 'Неизвестная проблема')
        # Sanitize before sending
        problem_description = m_escape(str(problem_description)[:500])
        msg = send_ticket(problem_description)
        log_ticket_event(
            event_type='manual_not_helped',
            ticket_number=session.get('current_ticket_number'),
            problem=problem_description,
            details={
                'source': 'manual_feedback',
                'feedback': 'manual_not_helped',
                'next_step': 'ticket',
                'ticket_delivery': 'sent' if msg is not None else 'failed'
            }
        )
        if msg is None:
            return render_template(
                'ticket_send_failed.html',
                ticket_number=session.get('current_ticket_number'),
                retry_url=url_for('finish_unsolved')
            ), 503
        return render_template('ticket_sent.html')
    except Exception as e:
        print("[finish_unsolved] Error sending ticket")
        traceback.print_exc()
        return render_template('ticket_sent.html')

@app.route('/go_home')
def go_home():
    # НЕ сбрасываем ticket_sent/solved_sent здесь — они очищаются при выборе нового мануала
    # (строка 1724-1726). Это защищает от повторной отправки заявки при возврате через стрелки браузера.

    # Security: don't log session content
    if 'user_info' in session:
        return redirect(url_for('show_problems'))
    else:
        return redirect(url_for('index'))


@app.route('/user_logout')
def user_logout():
    """Выход обычного пользователя из системы."""
    session.clear()
    return redirect(url_for('user_login'))


# ============================================
# API ДЛЯ ПОИСКА ТЕМАТИК
# ============================================

@app.route('/api/get_all_topics', methods=['GET'])
@csrf.exempt  # Exempted but protected by rate limiting
@rate_limit(max_requests=60, window=60)  # Увеличен лимит для пагинации
def get_all_topics_api():
    """API для получения всех тематик с пагинацией"""
    try:
        # Параметры пагинации
        limit = request.args.get('limit', 200, type=int)
        offset = request.args.get('offset', 0, type=int)

        # Валидация параметров
        if limit < 1 or limit > 500:
            limit = 200
        if offset < 0:
            offset = 0

        # Получаем общее количество тематик
        total_count = tm.get_topics_count()

        # Получаем порцию тематик
        topics = tm.get_all_topics(limit=limit, offset=offset)

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

        has_more = (offset + limit) < total_count

        return jsonify({
            'success': True,
            'count': len(formatted_results),
            'total': total_count,
            'offset': offset,
            'limit': limit,
            'has_more': has_more,
            'results': formatted_results
        })

    except Exception as e:
        print(f"[get_all_topics_api] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': 'Внутренняя ошибка сервера'
        })

@app.route('/api/get_channel_topics', methods=['GET'])
@csrf.exempt  # Exempted but protected by rate limiting
@rate_limit(max_requests=60, window=60)
def get_channel_topics_api():
    """API для получения тематик канала с пагинацией"""
    try:
        channel = request.args.get('channel', '', type=str).strip()
        limit = request.args.get('limit', 200, type=int)
        offset = request.args.get('offset', 0, type=int)

        if not channel or len(channel) > 200:
            return jsonify({'success': False, 'error': 'Не указан канал'}), 400

        # Валидация параметров
        if limit < 1 or limit > 500:
            limit = 200
        if offset < 0:
            offset = 0

        # Получаем общее количество тематик в канале
        total_count = tm.get_channel_topics_count(channel)

        # Получаем порцию тематик
        topics = tm.get_topics_by_channel(channel, limit=limit, offset=offset)

        formatted_results = []
        for topic in topics:
            formatted_results.append({
                'id': topic['id'],
                'topic': topic['full_topic'],
                'channel': topic['channel'],
                'similarity': 100,
                'sr1': topic.get('sr1', ''),
                'sr2': topic.get('sr2', ''),
                'sr3': topic.get('sr3', ''),
                'sr4': topic.get('sr4', '')
            })

        has_more = (offset + limit) < total_count

        return jsonify({
            'success': True,
            'count': len(formatted_results),
            'total': total_count,
            'channel': channel,
            'offset': offset,
            'limit': limit,
            'has_more': has_more,
            'results': formatted_results
        })

    except Exception as e:
        print(f"[get_channel_topics_api] Ошибка: {e}")
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
        data = request.get_json(silent=True) or {}
        password = data.get('password', '')
        section = str(data.get('section') or '').strip()

        # Берём username из сессии (пользователь уже залогинен)
        username = ''
        if session.get('user_info'):
            username = session['user_info'].get('username', '')
        if not username:
            username = session.get('admin_username', '')

        # В тестовом режиме принимаем упрощенные пароли
        TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
        if TEST_MODE and password in ['admin', '123', 'test']:
            from ad_auth import ad_auth
            lower_user = username.lower() if username else ''
            test_permissions = []
            if lower_user in ad_auth.super_admin_logins:
                test_permissions.append('super_admin')
            if lower_user in ad_auth.admins_manuals:
                test_permissions.append('admin_manuals')
            if lower_user in ad_auth.admins_topics:
                test_permissions.append('admin_topics')
            if lower_user in ad_auth.admins_scenarios:
                test_permissions.append('admin_scenarios')
            if lower_user in ad_auth.admins_trainer:
                test_permissions.append('admin_trainer')
            if lower_user in ad_auth.trainer_viewers:
                test_permissions.append('trainer_viewer')

            test_permissions, admin_role, trainer_segments = _merge_admin_permissions(username, test_permissions)

            if not test_permissions:
                return jsonify({'success': False, 'error': 'У вас нет прав администратора'})
            if not _admin_section_allowed(section, test_permissions):
                return jsonify({'success': False, 'error': 'Нет прав для этого раздела'})

            session['admin_logged_in'] = True
            session['admin_username'] = username
            session['admin_role'] = admin_role
            session['admin_permissions'] = test_permissions
            session['trainer_segments'] = trainer_segments
            session['admin_token'] = AdminAuth.generate_session_token()
            return jsonify({'success': True})

        # Проверка через AD (основной способ)
        from ad_auth import ad_auth
        if ad_auth.is_configured() and username:
            ad_result = ad_auth.verify_credentials(username, password)
            if ad_result:
                ad_permissions, admin_role, trainer_segments = _merge_admin_permissions(
                    ad_result.get('username', username),
                    ad_result.get('permissions', [])
                )
                if ad_permissions:
                    if not _admin_section_allowed(section, ad_permissions):
                        return jsonify({'success': False, 'error': 'Нет прав для этого раздела'})
                    session['admin_logged_in'] = True
                    session['admin_username'] = ad_result.get('username', username)
                    session['admin_role'] = admin_role
                    session['admin_permissions'] = ad_permissions
                    session['trainer_segments'] = trainer_segments
                    session['admin_token'] = AdminAuth.generate_session_token()
                    return jsonify({'success': True})
                else:
                    return jsonify({'success': False, 'error': 'У вас нет прав администратора'})
            return jsonify({'success': False, 'error': 'Неверный пароль'})

        # Fallback: проверка через admins.json (если AD не настроен)
        admin_username = os.getenv('ADMIN_USERNAME', 'admin')
        admin_data = AdminAuth.verify_admin(admin_username, password)

        if admin_data:
            admin_permissions = admins_manager.normalize_permissions(
                admin_data.get('permissions'),
                admin_data.get('role', ROLE_EDITOR)
            )
            if not _admin_section_allowed(section, admin_permissions):
                return jsonify({'success': False, 'error': 'Нет прав для этого раздела'})
            session['admin_logged_in'] = True
            session['admin_username'] = admin_data.get('username', admin_username)
            session['admin_role'] = admins_manager.role_from_permissions(admin_permissions)
            session['admin_permissions'] = admin_permissions
            session['trainer_segments'] = admin_data.get('trainer_segments', ['kc', 'branch'])
            session['admin_token'] = AdminAuth.generate_session_token()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'Неверный пароль'})

    except Exception as e:
        print(f"[API] Ошибка проверки пароля: {e}")
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'})


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
            text = (message.text or '').lower()
            reply_message = message.reply_to_message
            original_message_id = reply_message.message_id
            original_chat_id = reply_message.chat.id
            original_ticket_text = reply_message.text or reply_message.caption or ''
            prompt_context = _find_ticket_reject_prompt(original_message_id)
            if prompt_context:
                original_message_id = prompt_context.get('original_message_id') or original_message_id
                original_chat_id = prompt_context.get('original_chat_id') or original_chat_id
                original_ticket_text = prompt_context.get('original_message') or original_ticket_text
            ticket_number = extract_ticket_number_from_text(original_ticket_text)
            if prompt_context and prompt_context.get('ticket_number'):
                ticket_number = prompt_context.get('ticket_number')
            parsed = _parse_ticket_text_fields(original_ticket_text)
            actor_override = _staff_actor_from_message(message)
            problem = parsed.get('problem') or original_ticket_text
            rejection_reason = _parse_rejection_reason(message.text or '')
            is_ticket_message = bool(re.search(
                r'заявк[аеиу]\s*№\s*\d+',
                original_ticket_text or '',
                flags=re.IGNORECASE
            ))
            is_ticket_reply = (
                ticket_number is not None and (prompt_context is not None or is_ticket_message)
            )
            plain_rejection_reason = _plain_rejection_reason(message.text or '') if is_ticket_reply else ''
            if not rejection_reason:
                rejection_reason = plain_rejection_reason
            is_rejection_reply = bool(rejection_reason) and (
                'отклон' in text or bool(plain_rejection_reason)
            )
            is_ticket_action = (
                "массовый инцидент" in text or
                _is_transfer_up_text(text) or
                "отклон" in text or
                is_rejection_reply or
                "готово" in text or
                "решена" in text
            )
            if is_ticket_action and ticket_number is None:
                _reply_ticket_number_required(message)
                return

            if "массовый инцидент" in text:
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(original_chat_id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                _mark_ticket_mass_incident(ticket_number, problem, actor_override)
                _remove_ticket_buttons(original_chat_id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"⚠️ Заявка №{ticket_number or '—'} отмечена как массовый инцидент.",
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )
            elif _is_transfer_up_text(text):
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(original_chat_id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                _mark_ticket_transferred_up(ticket_number, problem, actor_override, original_ticket_text)
                _remove_ticket_buttons(original_chat_id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"⬆️ Заявка №{ticket_number or '—'} передана выше.\n"
                    f"Сотрудник: {html_escape(actor_override['name'])}",
                    message_thread_id=IN_PROGRESS_THREAD_ID,
                    parse_mode='HTML'
                )
            elif "отклон" in text or is_rejection_reply:
                if not rejection_reason:
                    bot.reply_to(
                        message,
                        "Для отклонения ответьте на заявку текстом причины."
                    )
                    return
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(original_chat_id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                _mark_ticket_rejected(ticket_number, problem, actor_override, rejection_reason)
                _remove_ticket_buttons(original_chat_id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"❌ ЗАЯВКА ОТКЛОНЕНА ❌\n\n"
                    f"№{ticket_number or '—'}\n"
                    f"Причина: {html_escape(rejection_reason)}\n"
                    f"Сотрудник: {html_escape(actor_override['name'])}",
                    message_thread_id=NEW_TICKETS_THREAD_ID,
                    parse_mode='HTML',
                    reply_to_message_id=original_message_id
                )
            elif "готово" in text or "решена" in text:
                lock_reason = _ticket_action_lock_reason(ticket_number)
                if lock_reason:
                    _remove_ticket_buttons(original_chat_id, original_message_id)
                    bot.reply_to(message, lock_reason)
                    return
                _mark_ticket_ready_for_feedback(ticket_number, problem, original_ticket_text, actor_override)
                _remove_ticket_buttons(original_chat_id, original_message_id)
                bot.send_message(
                    TECH_SUPPORT_CHAT_ID,
                    f"✅ Заявка №{ticket_number or '—'} готова и ожидает подтверждения инициатора.",
                    message_thread_id=IN_PROGRESS_THREAD_ID
                )
            elif "в работе" in text or "в процессе" in text:
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

                # Аналитика: фиксируем обновление статуса от техпода (polling, нет Flask session).
                log_ticket_event(
                    event_type='ticket_status_update_by_staff',
                    ticket_number=ticket_number,
                    problem=problem,
                    actor_override=actor_override,
                    user_info_override={
                        'department': parsed.get('department', ''),
                        'name': parsed.get('name', ''),
                        'workplace': parsed.get('workplace', '')
                    },
                    details={'status': 'in_work'}
                )
                _assign_ticket(ticket_number, problem, actor_override, 'ticket_assigned_to_staff')
        else:
            print("❌ Не прошли проверки (нет reply_to_message или ID не в SUPPORT_STAFF_IDS)")
    except Exception as e:
        print(f"Ошибка при обработке сообщения в канале: {e}")

# ============================================
# ТРЕНАЖЕР ОПЕРАТОРОВ
# ============================================

@app.route('/trainer')
def trainer_menu():
    """Страница выбора сегмента тренажера (КЦ / Филиалы)"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))
    return render_template('trainer_segments.html', is_admin=session.get('admin_logged_in', False))


TRAINER_SEGMENTS = {
    'kc': {'name': 'Контакт Центр', 'icon': '🎧', 'color': '#00a651'},
    'branch': {'name': 'Филиалы', 'icon': '🏦', 'color': '#2196F3'},
}


@app.route('/trainer/<segment>')
def trainer_segment_menu(segment):
    """Главная страница тренажера для выбранного сегмента"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    if segment not in TRAINER_SEGMENTS:
        return redirect(url_for('trainer_menu'))

    # Филиалы в разработке — доступны только администраторам
    if segment == 'branch' and not session.get('admin_logged_in'):
        return render_template('under_construction.html', segment_name='Филиалы')

    user_id = session['user_info'].get('username', 'anonymous')
    levels = trainer_mgr.get_all_levels()
    progress = trainer_mgr.get_user_progress(user_id, segment=segment)
    stats = trainer_mgr.get_statistics(segment=segment)
    seg_info = TRAINER_SEGMENTS[segment]

    return render_template('trainer_menu.html', levels=levels, progress=progress,
                         top_users=stats['top_users'], current_user_id=user_id,
                         segment=segment, seg_info=seg_info)


@app.route('/trainer/<segment>/level/<level_code>')
def trainer_level(segment, level_code):
    """Список сценариев уровня в сегменте"""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    if segment not in TRAINER_SEGMENTS:
        return redirect(url_for('trainer_menu'))

    # Филиалы в разработке — доступны только администраторам
    if segment == 'branch' and not session.get('admin_logged_in'):
        return render_template('under_construction.html', segment_name='Филиалы')

    user_id = session['user_info'].get('username', 'anonymous')
    level = trainer_mgr.get_level_by_code(level_code)

    if not level:
        flash('Уровень не найден')
        return redirect(url_for('trainer_segment_menu', segment=segment))

    # Проверяем доступ к уровню
    if not trainer_mgr.check_level_unlocked(user_id, level_code, segment=segment):
        flash('Этот уровень ещё заблокирован')
        return redirect(url_for('trainer_segment_menu', segment=segment))

    # Получаем фильтр по категории
    category_id = request.args.get('category', type=int)

    scenarios = trainer_mgr.get_scenarios_by_level(level_code, category_id, segment=segment)
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

    seg_info = TRAINER_SEGMENTS[segment]
    return render_template('trainer_scenarios.html',
                         level=level,
                         scenarios=scenarios,
                         categories=categories,
                         current_category=category_id,
                         user_results=user_results,
                         completed_count=completed_count,
                         total_count=total_count,
                         avg_percent=avg_percent,
                         segment=segment,
                         seg_info=seg_info)


@app.route('/trainer/play/<int:scenario_id>')
def trainer_play(scenario_id):
    """Страница прохождения сценария"""
    # Check for preview mode
    preview_mode = request.args.get('preview') == '1'
    # Откуда пришли: 'visual' или 'edit' (для кнопки возврата из предпросмотра)
    back_editor = request.args.get('back', 'edit')
    # Сегмент (kc / branch) — для правильного редиректа после прохождения
    play_segment = request.args.get('segment', 'kc')

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

    # Логируем факт открытия сценария (для отчёта посещений)
    if not preview_mode and user_id != 'admin_preview':
        trainer_mgr.log_visit(user_id, scenario_id)
        # Запоминаем время начала прохождения
        session[f'scenario_start_{scenario_id}'] = datetime.now().isoformat()

    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('trainer_menu') if not preview_mode else url_for('admin_trainer'))

    # Черновики и скрытые сценарии недоступны для обычных пользователей
    if not preview_mode:
        if scenario.get('is_draft'):
            flash('Сценарий недоступен')
            return redirect(url_for('trainer_segment_menu', segment=play_segment))
        if not scenario.get('is_active'):
            flash('Сценарий недоступен')
            return redirect(url_for('trainer_segment_menu', segment=play_segment))

    # Check level access (skip in preview mode)
    if not preview_mode and not trainer_mgr.check_level_unlocked(user_id, scenario['level_code'], segment=play_segment):
        flash('Этот уровень ещё заблокирован')
        return redirect(url_for('trainer_segment_menu', segment=play_segment))

    total_steps = trainer_mgr.get_steps_count(scenario_id)

    if total_steps == 0:
        flash('В этом сценарии пока нет шагов')
        return redirect(url_for('admin_trainer_edit', scenario_id=scenario_id) if preview_mode else url_for('trainer_level', segment=play_segment, level_code=scenario['level_code']))

    # Парсим эталонные тематики для пост-обработки
    correct_topics = []
    if scenario.get('correct_topics'):
        try:
            correct_topics = json.loads(scenario['correct_topics'])
        except:
            pass

    # Парсим аватары
    avatar_images = {}
    if scenario.get('avatar_images'):
        try:
            avatar_images = json.loads(scenario['avatar_images'])
        except:
            pass

    # Парсим имя клиента
    client_name = 'Максим'
    if scenario.get('client_info_json'):
        try:
            ci = json.loads(scenario['client_info_json'])
            if ci.get('name'):
                client_name = ci['name']
        except:
            pass

    # URL для кнопки «Вернуться» в режиме предпросмотра
    if preview_mode:
        if back_editor == 'visual':
            back_url = url_for('admin_trainer_visual', scenario_id=scenario_id)
        else:
            back_url = url_for('admin_trainer_edit', scenario_id=scenario_id)
    else:
        back_url = None

    return render_template('trainer_play.html',
                         scenario=scenario,
                         total_steps=total_steps,
                         preview_mode=preview_mode,
                         back_url=back_url,
                         correct_topics=correct_topics,
                         avatar_images=avatar_images,
                         client_name=client_name,
                         segment=play_segment)


@app.route('/api/trainer/step/<int:scenario_id>/<int:step_num>')
@rate_limit(max_requests=120, window=60)
def trainer_get_step(scenario_id, step_num):
    """API: получить шаг сценария"""
    if ('user_info' not in session or not session.get('authenticated')) and not session.get('admin_logged_in'):
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
    # Перемешиваем ответы — нельзя запомнить позицию правильного
    random.shuffle(safe_answers)

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


@app.route('/api/trainer/step_by_id/<int:step_id>')
@rate_limit(max_requests=120, window=60)
def trainer_get_step_by_id(step_id):
    """API: получить шаг по ID (для ветвления диалога)"""
    if ('user_info' not in session or not session.get('authenticated')) and not session.get('admin_logged_in'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    step = trainer_mgr.get_step_by_id(step_id)
    if not step:
        return jsonify({'success': False, 'error': 'Шаг не найден'})

    scenario = trainer_mgr.get_scenario(step['scenario_id'])

    # Не отправляем информацию о правильности ответов
    safe_answers = []
    for answer in step.get('answers', []):
        safe_answers.append({
            'id': answer['id'],
            'answer_text': answer['answer_text'],
            'order_num': answer['order_num']
        })
    # Перемешиваем ответы — нельзя запомнить позицию правильного
    random.shuffle(safe_answers)

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
        'timer_seconds': scenario.get('timer_seconds', 15) if scenario else 15
    })


@app.route('/api/trainer/answer', methods=['POST'])
@rate_limit(max_requests=60, window=60)
def trainer_submit_answer():
    """API: отправить ответ"""
    if ('user_info' not in session or not session.get('authenticated')) and not session.get('admin_logged_in'):
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
            'irritation_impact': selected_answer.get('irritation_impact', 0),
            'new_mood': new_mood,
            'new_loyalty': new_loyalty,
            'knowledge_link': selected_answer.get('knowledge_link'),
            'is_game_over': is_game_over,
            'next_step_id': selected_answer.get('next_step_id')
        })

    except Exception as e:
        print(f"[trainer_submit_answer] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500


@app.route('/api/trainer/complete', methods=['POST'])
@rate_limit(max_requests=30, window=60)
def trainer_complete():
    """API: завершить сценарий"""
    is_admin_preview = session.get('admin_logged_in') and 'user_info' not in session
    if ('user_info' not in session or not session.get('authenticated')) and not session.get('admin_logged_in'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    # В режиме предпросмотра результаты не сохраняем
    if is_admin_preview:
        return jsonify({'success': True, 'result_id': None, 'percent': 0, 'grade': 'preview', 'is_game_over': False})

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

        # Извлекаем время начала из сессии
        started_at = session.pop(f'scenario_start_{scenario_id}', None)

        # Сохраняем результат с новыми полями геймификации
        result = trainer_mgr.save_result(
            user_id, scenario_id, score, max_score, answers,
            final_loyalty=final_loyalty,
            is_game_over=is_game_over,
            timeout_count=timeout_count,
            selected_topic_id=selected_topic_id,
            selected_topic_name=selected_topic_name,
            started_at=started_at
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

    # Определяем, нужен ли снимок старой версии
    scenario_data = trainer_mgr.get_scenario(result['scenario_id'])
    result_version = result.get('scenario_version')
    current_version = scenario_data.get('version', 1) if scenario_data else 1
    version_outdated = False
    snapshot_steps = None

    if result_version and result_version != current_version:
        version_outdated = True
        ver_snap = trainer_mgr.get_scenario_version_snapshot(result['scenario_id'], result_version)
        if ver_snap and ver_snap.get('snapshot'):
            snapshot_steps = ver_snap['snapshot'].get('steps', [])

    # Получаем детализацию ответов
    answers_detail = []
    if result.get('answers'):
        for ans in result['answers']:
            step_num = ans.get('step_num') or ans.get('step')
            if not step_num:
                continue

            # Если есть снимок старой версии — используем данные из него
            step = None
            if snapshot_steps:
                for ss in snapshot_steps:
                    if ss.get('step_num') == step_num:
                        step = ss
                        break

            if not step:
                step = trainer_mgr.get_step_by_num(result['scenario_id'], step_num)

            if step:
                for answer in step.get('answers', []):
                    if answer['id'] == ans.get('answer_id'):
                        answers_detail.append({
                            'step_num': step_num,
                            'answer_text': answer['answer_text'],
                            'points': ans['points'],
                            'is_correct': ans['is_correct'],
                            'is_partial': answer.get('is_partial', False),
                            'is_timeout': ans.get('is_timeout', False),
                            'mood_impact': ans.get('mood_impact', 0),
                            'knowledge_link': ans.get('knowledge_link') or answer.get('knowledge_link') or '',
                            'feedback': answer.get('feedback') or ans.get('feedback') or ''
                        })
                        break

    # Сегмент берём из данных сценария
    result_segment = scenario_data.get('segment', 'kc') if scenario_data else 'kc'

    # Находим следующий сценарий (в том же сегменте)
    scenarios = trainer_mgr.get_scenarios_by_level(result['level_code'], segment=result_segment)
    next_scenario = None
    found_current = False
    for s in scenarios:
        if found_current:
            next_scenario = s
            break
        if s['id'] == result['scenario_id']:
            found_current = True

    # Получаем эталонные тематики сценария
    correct_topics = []
    if scenario_data and scenario_data.get('correct_topics'):
        try:
            correct_topics = json.loads(scenario_data['correct_topics'])
        except:
            pass

    # Проверяем совпадение выбранной тематики с эталонными
    topic_match = False
    if result.get('selected_topic_id') and correct_topics:
        topic_match = any(
            str(t.get('id')) == str(result['selected_topic_id'])
            for t in correct_topics
        )

    # Бейджи пользователя
    user_badges = trainer_mgr.get_user_badges(user_id)

    return render_template('trainer_results.html',
                         result=result,
                         grade_info=grade_info,
                         answers_detail=answers_detail,
                         next_scenario=next_scenario,
                         correct_topics=correct_topics,
                         topic_match=topic_match,
                         version_outdated=version_outdated,
                         result_version=result_version,
                         current_version=current_version,
                         user_badges=user_badges,
                         segment=result_segment)


# ============================================
# АДМИН-ПАНЕЛЬ ТРЕНАЖЕРА
# ============================================

def _admin_trainer_redirect(scenario_id=None, segment=None):
    """Редирект в нужный сегмент админки после операции над сценарием"""
    if not segment and scenario_id:
        sc = trainer_mgr.get_scenario(scenario_id)
        segment = sc.get('segment', 'kc') if sc else 'kc'
    if segment in TRAINER_SEGMENTS:
        return redirect(url_for('admin_trainer_segment', segment=segment))
    return redirect(url_for('admin_trainer'))


@app.route('/admin/trainer')
@AdminAuth.trainer_required
def admin_trainer():
    """Редирект в первый доступный сегмент"""
    allowed_segments = session.get('trainer_segments', ['kc', 'branch'])
    first_segment = allowed_segments[0] if allowed_segments else 'kc'
    return redirect(url_for('admin_trainer_segment', segment=first_segment))


@app.route('/admin/trainer/<segment>')
@AdminAuth.login_required
def admin_trainer_segment(segment):
    """Админка: список сценариев тренажера по сегменту"""
    if segment not in TRAINER_SEGMENTS:
        return redirect(url_for('admin_trainer'))

    # Проверяем доступ к сегменту
    allowed_segments = session.get('trainer_segments', ['kc', 'branch'])
    if segment not in allowed_segments:
        # Перенаправляем в первый доступный сегмент
        if allowed_segments:
            return redirect(url_for('admin_trainer_segment', segment=allowed_segments[0]))
        flash('У вас нет доступа ни к одному сегменту тренажёра')
        return redirect(url_for('admin_dashboard'))

    seg_info = TRAINER_SEGMENTS[segment]
    stats = trainer_mgr.get_statistics(segment=segment)
    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()
    tags = trainer_mgr.get_all_tags()

    # Фильтры
    level_code = request.args.get('level')
    category_id = request.args.get('category', type=int)
    tag_id = request.args.get('tag', type=int)

    scenarios = trainer_mgr.get_all_scenarios(include_inactive=True, segment=segment)

    # Применяем фильтры
    if level_code:
        scenarios = [s for s in scenarios if s['level_code'] == level_code]
    if category_id:
        scenarios = [s for s in scenarios if s['category_id'] == category_id]

    # Добавляем количество шагов и теги
    for scenario in scenarios:
        scenario['steps_count'] = trainer_mgr.get_steps_count(scenario['id'])
        scenario['tags'] = trainer_mgr.get_scenario_tags(scenario['id'])

    # Фильтр по тегу
    if tag_id:
        scenarios = [s for s in scenarios if any(t['id'] == tag_id for t in s['tags'])]

    # Архивные в конец
    archived_scenarios = trainer_mgr.get_archived_scenarios(segment=segment)
    for s in archived_scenarios:
        s['steps_count'] = trainer_mgr.get_steps_count(s['id'])
        s['tags'] = trainer_mgr.get_scenario_tags(s['id'])
    scenarios = scenarios + archived_scenarios

    unread_feedback = trainer_mgr.get_unread_feedback_count(segment=segment)
    draft_count = trainer_mgr.get_draft_count(segment=segment)

    return render_template('admin_trainer.html',
                         stats=stats,
                         levels=levels,
                         categories=categories,
                         tags=tags,
                         scenarios=scenarios,
                         current_level=level_code,
                         current_category=category_id,
                         current_tag=tag_id,
                         segment=segment,
                         seg_info=seg_info,
                         unread_feedback=unread_feedback,
                         draft_count=draft_count)


@app.route('/admin/trainer/scenario/create', methods=['GET', 'POST'])
@AdminAuth.trainer_required
def admin_trainer_create():
    """Создание нового сценария"""
    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()

    if request.method == 'POST':
        is_draft = 1 if request.form.get('is_draft') else 0
        data = {
            'level_id': request.form.get('level_id', type=int),
            'category_id': request.form.get('category_id', type=int) or None,
            'title': request.form.get('title', '').strip(),
            'description': request.form.get('description', '').strip(),
            'estimated_time': request.form.get('estimated_time', 5, type=int),
            'total_points': request.form.get('total_points', 100, type=int),
            'is_active': 0 if is_draft else (1 if request.form.get('is_active') else 0),
            'order_num': request.form.get('order_num', 0, type=int),
            'is_draft': is_draft,
            'segment': request.form.get('segment', 'kc'),
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
            if is_draft:
                flash('Черновик создан! Добавьте шаги и опубликуйте когда будет готов.')
            else:
                flash('Сценарий успешно создан!')
            return redirect(url_for('admin_trainer_edit', scenario_id=result['id']))
        else:
            flash(f'Ошибка: {result.get("error")}')

    tags = trainer_mgr.get_all_tags()
    return render_template('admin_trainer_edit.html', scenario=None, levels=levels, categories=categories, steps=[], tags=tags, scenario_tag_ids=[])


@app.route('/admin/trainer/scenario/<int:scenario_id>/edit', methods=['GET', 'POST'])
@AdminAuth.trainer_required
def admin_trainer_edit(scenario_id):
    """Редактирование сценария"""
    scenario = trainer_mgr.get_scenario(scenario_id)

    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('admin_trainer'))

    sc_segment = scenario.get('segment', 'kc')
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
        correct_topics_raw = request.form.get('correct_topics', '').strip()
        correct_topics_val = correct_topics_raw if correct_topics_raw else None

        is_draft = 1 if request.form.get('is_draft') else 0
        data = {
            'level_id': request.form.get('level_id', type=int),
            'category_id': request.form.get('category_id', type=int) or None,
            'title': request.form.get('title', '').strip(),
            'description': request.form.get('description', '').strip(),
            'estimated_time': request.form.get('estimated_time', 5, type=int),
            'total_points': request.form.get('total_points', 100, type=int),
            'is_active': 0 if is_draft else (1 if request.form.get('is_active') else 0),
            'order_num': request.form.get('order_num', 0, type=int),
            'timer_seconds': request.form.get('timer_seconds', 15, type=int),
            'initial_loyalty': request.form.get('initial_loyalty', 100, type=int),
            'client_info_json': client_info_json,
            'correct_topics': correct_topics_val,
            'silence_messages': request.form.get('silence_messages', '').strip(),
            'is_draft': is_draft,
            'emotion_timeout_penalty': request.form.get('emotion_timeout_penalty', 20, type=int),
            'emotion_passive_rate': request.form.get('emotion_passive_rate', 0, type=int),
            'segment': request.form.get('segment', 'kc'),
        }
        # Сохраняем снимок текущей версии перед обновлением
        user_info = session.get('user_info', {})
        editor = user_info.get('username') or user_info.get('name', 'admin')
        trainer_mgr.save_version_snapshot(scenario_id, changed_by=editor)

        # Обработка аватаров (5 эмоций)
        avatar_images = {}
        if scenario.get('avatar_images'):
            try:
                avatar_images = json.loads(scenario['avatar_images'])
            except:
                pass

        emotion_keys = ['angry', 'irritated', 'neutral', 'satisfied', 'delighted']
        avatars_dir = os.path.join(app.static_folder, 'uploads', 'avatars')
        os.makedirs(avatars_dir, exist_ok=True)
        for emo in emotion_keys:
            # Удаление аватара
            if request.form.get(f'avatar_{emo}_delete') == '1':
                old_path = avatar_images.pop(emo, None)
                if old_path:
                    full_path = os.path.join(app.static_folder, old_path)
                    if os.path.exists(full_path):
                        os.remove(full_path)
                continue
            # Загрузка нового аватара
            file = request.files.get(f'avatar_{emo}')
            if file and file.filename:
                from werkzeug.utils import secure_filename
                ext = os.path.splitext(file.filename)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                    fname = f"scenario_{scenario_id}_{emo}{ext}"
                    fpath = os.path.join(avatars_dir, fname)
                    file.save(fpath)
                    avatar_images[emo] = f"uploads/avatars/{fname}"

        data['avatar_images'] = json.dumps(avatar_images, ensure_ascii=False) if avatar_images else ''

        result = trainer_mgr.update_scenario(scenario_id, data)
        if result['success']:
            # Сохраняем теги
            tag_ids = request.form.getlist('tags')
            tag_ids = [int(t) for t in tag_ids if t.isdigit()]
            trainer_mgr.set_scenario_tags(scenario_id, tag_ids)

            # Логируем изменение (берём AD учётку пользователя)
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
                        'irritation_impact': request.form.get(f'answer_{answer_id}_irritation_impact', 0, type=int),
                        'knowledge_link': request.form.get(f'answer_{answer_id}_knowledge_link', '').strip() or None,
                        'next_step_id': request.form.get(f'answer_{answer_id}_next_step_id', type=int) or None
                    })

            flash('Сценарий успешно обновлен!')
            # Если нажали «Предпросмотр» — сохранить и перейти в предпросмотр
            if request.form.get('next_action') == 'preview':
                return redirect(url_for('trainer_play', scenario_id=scenario_id, preview=1, back='edit'))
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

    # Парсим эталонные тематики
    correct_topics = []
    if scenario.get('correct_topics'):
        try:
            correct_topics = json.loads(scenario['correct_topics'])
        except:
            pass

    # Получаем историю версий
    version_history = trainer_mgr.get_scenario_version_history(scenario_id)

    # Парсим аватары
    avatar_images = {}
    if scenario.get('avatar_images'):
        try:
            avatar_images = json.loads(scenario['avatar_images'])
        except:
            pass

    return render_template('admin_trainer_edit.html',
                         scenario=scenario,
                         levels=levels,
                         categories=categories,
                         steps=steps,
                         client_info=client_info,
                         client_extra=client_extra,
                         tags=tags,
                         scenario_tag_ids=scenario_tag_ids,
                         correct_topics=correct_topics,
                         version_history=version_history,
                         avatar_images=avatar_images)


@app.route('/admin/trainer/scenario/<int:scenario_id>/versions')
@AdminAuth.login_required
def admin_trainer_versions(scenario_id):
    """Просмотр истории версий сценария"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    if not scenario:
        flash('Сценарий не найден')
        return redirect(url_for('admin_trainer'))

    version_history = trainer_mgr.get_scenario_version_history(scenario_id)
    return render_template('admin_trainer_versions.html',
                         scenario=deep_escape(scenario),
                         version_history=deep_escape(version_history))


@app.route('/admin/trainer/scenario/<int:scenario_id>/versions/<int:version>')
@AdminAuth.login_required
def admin_trainer_version_detail(scenario_id, version):
    """Просмотр конкретной версии сценария (JSON)"""
    snapshot = trainer_mgr.get_scenario_version_snapshot(scenario_id, version)
    if not snapshot:
        return jsonify({'success': False, 'error': 'Версия не найдена'}), 404
    return jsonify({
        'success': True,
        'version': snapshot.get('version'),
        'changed_by': snapshot.get('changed_by'),
        'changed_at': snapshot.get('changed_at'),
        'snapshot': snapshot.get('snapshot')
    })


@app.route('/admin/trainer/scenario/<int:scenario_id>/delete', methods=['POST'])
@AdminAuth.trainer_required
def admin_trainer_delete(scenario_id):
    """Удаление сценария"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    scenario_title = scenario['title'] if scenario else f"ID {scenario_id}"
    sc_segment = scenario.get('segment', 'kc') if scenario else 'kc'

    result = trainer_mgr.delete_scenario(scenario_id)

    if result['success']:
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

    return _admin_trainer_redirect(segment=sc_segment)


@app.route('/admin/trainer/drafts')
@AdminAuth.login_required
def admin_trainer_drafts():
    """Черновики сценариев"""
    segment = request.args.get('segment', 'kc')
    seg_info = TRAINER_SEGMENTS.get(segment, TRAINER_SEGMENTS['kc'])
    drafts = trainer_mgr.get_draft_scenarios(segment=segment)
    for d in drafts:
        d['steps_count'] = trainer_mgr.get_steps_count(d['id'])
        d['tags'] = trainer_mgr.get_scenario_tags(d['id'])
    levels = trainer_mgr.get_all_levels()
    categories = trainer_mgr.get_all_categories()
    return render_template('admin_trainer_drafts.html',
                           drafts=drafts,
                           levels=levels,
                           categories=categories,
                           segment=segment,
                           seg_info=seg_info)


@app.route('/admin/trainer/scenario/<int:scenario_id>/publish', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_publish(scenario_id):
    """Опубликовать черновик"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    sc_segment = scenario.get('segment', 'kc') if scenario else 'kc'
    result = trainer_mgr.publish_draft(scenario_id)
    if result['success']:
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='publish',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario['title'] if scenario else f'ID {scenario_id}',
            ip_address=request.remote_addr
        )
        flash('Сценарий опубликован!')
    else:
        flash(f'Ошибка: {result.get("error")}')
    return redirect(url_for('admin_trainer_drafts', segment=sc_segment))


@app.route('/admin/trainer/scenario/<int:scenario_id>/archive', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_archive(scenario_id):
    """Отправить сценарий в архив"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    sc_segment = scenario.get('segment', 'kc') if scenario else 'kc'
    result = trainer_mgr.archive_scenario(scenario_id)
    if result['success']:
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='archive',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario['title'] if scenario else f'ID {scenario_id}',
            ip_address=request.remote_addr
        )
        flash('Сценарий перемещён в архив.')
    else:
        flash(f'Ошибка: {result.get("error")}')
    return _admin_trainer_redirect(segment=sc_segment)


@app.route('/admin/trainer/scenario/<int:scenario_id>/restore', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_restore(scenario_id):
    """Восстановить сценарий из архива"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    sc_segment = scenario.get('segment', 'kc') if scenario else 'kc'
    result = trainer_mgr.restore_from_archive(scenario_id)
    if result['success']:
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='restore',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario['title'] if scenario else f'ID {scenario_id}',
            ip_address=request.remote_addr
        )
        flash('Сценарий восстановлен из архива и снова активен.')
    else:
        flash(f'Ошибка: {result.get("error")}')
    return _admin_trainer_redirect(segment=sc_segment)


@app.route('/admin/trainer/scenario/<int:scenario_id>/duplicate', methods=['POST'])
@AdminAuth.login_required
def admin_trainer_duplicate(scenario_id):
    """Дублировать сценарий в черновики"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    result = trainer_mgr.duplicate_scenario(scenario_id)
    if result['success']:
        user_info = session.get('user_info', {})
        trainer_mgr.log_action(
            user_id=user_info.get('username') or user_info.get('name', 'admin'),
            action='duplicate',
            entity_type='scenario',
            entity_id=scenario_id,
            entity_name=scenario['title'] if scenario else f'ID {scenario_id}',
            ip_address=request.remote_addr
        )
        flash('Сценарий продублирован и сохранён в черновиках!')
        return redirect(url_for('admin_trainer_edit', scenario_id=result['id']))
    else:
        sc_segment = scenario.get('segment', 'kc') if scenario else 'kc'
        flash(f'Ошибка дублирования: {result.get("error")}')
        return _admin_trainer_redirect(segment=sc_segment)


@app.route('/admin/trainer/scenario/<int:scenario_id>/step/create', methods=['POST'])
@AdminAuth.trainer_required
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
@AdminAuth.trainer_required
def admin_trainer_delete_step(step_id):
    """Удаление шага"""
    result = trainer_mgr.delete_step(step_id)

    if result['success']:
        flash('Шаг удален')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return safe_redirect('admin_trainer')


@app.route('/admin/trainer/step/<int:step_id>/answer/create', methods=['POST'])
@AdminAuth.trainer_required
def admin_trainer_create_answer(step_id):
    """Создание варианта ответа"""
    data = {
        'answer_text': request.form.get('answer_text', 'Новый ответ'),
        'is_correct': 0,
        'is_partial': 0,
        'points': request.form.get('points', 0, type=int),
        'feedback': '',
        'mood_impact': request.form.get('mood_impact', 0, type=int),
        'irritation_impact': request.form.get('irritation_impact', 0, type=int),
    }

    result = trainer_mgr.create_answer(step_id, data)

    if result['success']:
        flash('Ответ добавлен')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return safe_redirect('admin_trainer')


@app.route('/admin/trainer/answer/<int:answer_id>/delete', methods=['POST'])
@AdminAuth.trainer_required
def admin_trainer_delete_answer(answer_id):
    """Удаление варианта ответа"""
    result = trainer_mgr.delete_answer(answer_id)

    if result['success']:
        flash('Ответ удален')
    else:
        flash(f'Ошибка: {result.get("error")}')

    return safe_redirect('admin_trainer')


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual')
@AdminAuth.trainer_required
def admin_trainer_visual(scenario_id):
    """Визуальный редактор сценария (No-Code)"""
    scenario = trainer_mgr.get_scenario(scenario_id)
    if not scenario:
        flash('Сценарий не найден')
        return _admin_trainer_redirect()

    # Получаем шаги с ответами для инициализации визуального редактора
    steps = trainer_mgr.get_scenario_steps(scenario_id)
    for step in steps:
        step['answers'] = trainer_mgr.get_step_answers(step['id'])

    return render_template('admin_trainer_visual.html',
                         scenario=scenario,
                         steps=steps)


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual/save', methods=['POST'])
@AdminAuth.trainer_required
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

        # Сохраняем снимок текущей версии перед изменениями
        user_info = session.get('user_info', {})
        editor = user_info.get('username') or user_info.get('name', 'admin')
        trainer_mgr.save_version_snapshot(scenario_id, changed_by=editor,
                                          change_summary='visual_editor')

        # Инкремент версии сценария
        cursor_v = trainer_mgr.conn.cursor()
        cursor_v.execute(
            "UPDATE trainer_scenarios SET version = COALESCE(version, 1) + 1 WHERE id = ?",
            (scenario_id,)
        )
        trainer_mgr.conn.commit()

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
                step_id = result['id']
                node_to_step[node['id']] = step_id

        # Обрабатываем отдельные узлы типа "answer" (ответы — отдельные узлы-дерево)
        answer_nodes = [n for n in nodes if n.get('type') == 'answer']
        # Маппинг временных ID answer-узлов к реальным ID в БД
        node_to_answer = {}

        for answer_node in answer_nodes:
            # Находим связь от клиентского узла к этому ответу
            parent_connection = next(
                (c for c in connections if c.get('toId') == answer_node['id'] and node_to_step.get(c.get('fromId'))),
                None
            )

            if parent_connection:
                parent_node_id = parent_connection.get('fromId')
                step_id = node_to_step.get(parent_node_id)

                if step_id:
                    answer_data = {
                        'answer_text': answer_node.get('label', 'Ответ оператора'),
                        'is_correct': 1 if answer_node.get('isCorrect', False) else 0,
                        'is_partial': 1 if answer_node.get('isPartial', False) else 0,
                        'points': answer_node.get('points', 0),
                        'feedback': answer_node.get('feedback', ''),
                        'mood_impact': answer_node.get('moodImpact', 0),
                        'knowledge_link': answer_node.get('knowledgeLink', '')
                    }

                    result = trainer_mgr.create_answer(step_id, answer_data)
                    if result.get('success'):
                        node_to_answer[answer_node['id']] = result['id']

        # Второй проход: связи answer→client = next_step_id (ветвление)
        for conn in connections:
            from_id = conn.get('fromId', '')
            to_id = conn.get('toId', '')
            answer_db_id = node_to_answer.get(from_id)
            target_step_id = node_to_step.get(to_id)
            if answer_db_id and target_step_id:
                trainer_mgr.update_answer(answer_db_id, {'next_step_id': target_step_id})

        # Строим маппинг старых visual-ID → новых DB-ID
        # (нужен потому что DELETE+CREATE присваивает новые ID шагам/ответам)
        id_remap = {}
        for old_id, new_step_id in node_to_step.items():
            id_remap[old_id] = f"step_{new_step_id}"
        for old_id, new_answer_id in node_to_answer.items():
            id_remap[old_id] = f"answer_{new_answer_id}"

        # Обновляем ID узлов в visual_data на актуальные DB-ID
        updated_nodes = []
        for node in nodes:
            new_node = dict(node)
            new_node['id'] = id_remap.get(node['id'], node['id'])
            updated_nodes.append(new_node)

        # Обновляем fromId/toId в соединениях
        updated_connections = []
        for conn in connections:
            new_conn = dict(conn)
            new_conn['fromId'] = id_remap.get(conn.get('fromId', ''), conn.get('fromId', ''))
            new_conn['toId'] = id_remap.get(conn.get('toId', ''), conn.get('toId', ''))
            new_conn['id'] = f"conn_{new_conn['fromId']}_{new_conn['toId']}"
            updated_connections.append(new_conn)

        # Сохраняем визуальную структуру для последующего восстановления
        visual_data = {
            'nodes': updated_nodes,
            'connections': updated_connections
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

        return jsonify({'success': True, 'message': 'Сценарий сохранен', 'id_remap': id_remap})

    except Exception as e:
        print(f"[API] Ошибка сохранения сценария: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сохранения сценария'})


@app.route('/admin/trainer/scenario/<int:scenario_id>/visual/load')
@AdminAuth.trainer_required
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

            # Узел реплики клиента (без answers — они отдельные узлы)
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
                'answers': []
            })

            # Ответы — отдельные узлы типа "answer"
            answers = trainer_mgr.get_step_answers(step['id'])
            answer_x = pos['x'] + 300
            answer_y_offset = 0

            for answer in answers:
                answer_id = f"answer_{answer['id']}"
                ans_pos = saved_positions.get(answer_id, {'x': answer_x, 'y': pos['y'] + answer_y_offset})

                nodes.append({
                    'id': answer_id,
                    'type': 'answer',
                    'x': ans_pos['x'],
                    'y': ans_pos['y'],
                    'label': answer.get('answer_text', ''),
                    'isCorrect': bool(answer.get('is_correct', 0)),
                    'isPartial': bool(answer.get('is_partial', 0)),
                    'points': answer.get('points', 0),
                    'moodImpact': answer.get('mood_impact', 0),
                    'feedback': answer.get('feedback', ''),
                    'knowledgeLink': answer.get('knowledge_link', '')
                })

                # Связь: реплика клиента → ответ
                connections.append({
                    'id': f"conn_{step_id}_{answer_id}",
                    'fromId': step_id,
                    'toId': answer_id
                })

                # Связь ветвления: ответ → следующий шаг клиента
                next_sid = answer.get('next_step_id')
                if next_sid:
                    target_step_id = f"step_{next_sid}"
                    connections.append({
                        'id': f"branch_{answer_id}_{target_step_id}",
                        'fromId': answer_id,
                        'toId': target_step_id,
                        'type': 'branch'
                    })

                answer_y_offset += 100

            y_offset += max(200, len(answers) * 100 + 50)

        # Добавляем сохранённые connections (например client→client переходы)
        # которых нет в сгенерированных из БД
        generated_conn_keys = {(c['fromId'], c['toId']) for c in connections}
        for sc in saved_connections:
            key = (sc.get('fromId'), sc.get('toId'))
            if key not in generated_conn_keys:
                connections.append(sc)

        return jsonify({
            'success': True,
            'nodes': nodes,
            'connections': connections
        })

    except Exception as e:
        print(f"[API] Ошибка загрузки визуального редактора: {e}")
        return jsonify({'success': False, 'error': 'Ошибка загрузки данных'})


@app.route('/admin/trainer/stats')
@AdminAuth.trainer_view_required
def admin_trainer_stats():
    """Статистика тренажера"""
    segment = request.args.get('segment', 'kc')
    seg_info = TRAINER_SEGMENTS.get(segment, TRAINER_SEGMENTS['kc'])
    stats = trainer_mgr.get_statistics(segment=segment)
    heatmap = trainer_mgr.get_step_error_heatmap(limit=20, segment=segment)
    return render_template('admin_trainer_stats.html',
                           stats=stats,
                           heatmap=heatmap,
                           segment=segment,
                           seg_info=seg_info)


@app.route('/api/admin/trainer/user/<user_id>/results')
@AdminAuth.login_required
def admin_trainer_user_results(user_id):
    """Получить историю прохождений конкретного пользователя"""
    results = trainer_mgr.get_user_results(user_id)
    badges = trainer_mgr.get_user_badges(user_id)
    return jsonify({
        'success': True,
        'user_id': user_id,
        'results': results,
        'badges': badges
    })


# ============================================
# ТЕГИ СЦЕНАРИЕВ
# ============================================

@app.route('/admin/trainer/tags', methods=['GET', 'POST'])
@AdminAuth.trainer_required
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
    import secrets
    colors = ['#2196F3', '#4CAF50', '#FF9800', '#9C27B0', '#00BCD4', '#795548', '#E91E63', '#607D8B']
    icons = ['🏷️', '📌', '⭐', '🔖', '📋', '🎯']

    result = trainer_mgr.create_tag(
        name=name,
        color=secrets.choice(colors),
        icon=secrets.choice(icons)
    )

    if result['success']:
        tag = trainer_mgr.get_tag_by_id(result['id'])
        return jsonify({'success': True, 'tag': tag})

    return jsonify(result)


@app.route('/admin/trainer/tags/<int:tag_id>', methods=['PUT', 'DELETE'])
@AdminAuth.trainer_required
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
@AdminAuth.trainer_required
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
                    'Название кейса',
                    'Уровень',
                    'Баллы',
                    'Макс. баллов',
                    'Процент (%)',
                    'Время начала',
                    'Время окончания',
                    'Game Over',
                    'Лояльность клиента',
                    'Ответ сотрудника'
                ]
                # Преобразуем Game Over в понятный формат
                results_df['Game Over'] = results_df['Game Over'].apply(lambda x: 'Да' if x else 'Нет')
                # Вычисляем длительность прохождения
                def calc_duration(row):
                    try:
                        if row['Время начала'] and row['Время окончания']:
                            from datetime import datetime as dt
                            fmt = '%Y-%m-%d %H:%M:%S'
                            start = dt.fromisoformat(str(row['Время начала']))
                            end = dt.fromisoformat(str(row['Время окончания']))
                            secs = int((end - start).total_seconds())
                            mins, s = divmod(abs(secs), 60)
                            return f'{mins} мин {s} сек'
                    except Exception:
                        pass
                    return '—'
                results_df['Длительность'] = results_df.apply(calc_duration, axis=1)
                # Итоговый порядок колонок
                results_df = results_df[[
                    'Сотрудник', 'Название кейса', 'Уровень',
                    'Баллы', 'Макс. баллов', 'Процент (%)',
                    'Время начала', 'Время окончания', 'Длительность',
                    'Game Over', 'Лояльность клиента', 'Ответ сотрудника'
                ]]
                results_df.to_excel(writer, sheet_name='Все прохождения', index=False)
                # Выделяем заголовок колонки «Ответ сотрудника» жёлтым цветом
                from openpyxl.styles import PatternFill as _PF, Font as _Fnt, Alignment as _Aln
                _ws = writer.sheets['Все прохождения']
                _ans_col = results_df.columns.get_loc('Ответ сотрудника') + 1  # 1-based
                _header_cell = _ws.cell(row=1, column=_ans_col)
                _header_cell.fill = _PF('solid', fgColor='FFFF00')
                _header_cell.font = _Fnt(bold=True)
                _header_cell.alignment = _Aln(horizontal='center', vertical='center', wrap_text=True)
                # Делаем колонку широкой для удобства чтения
                from openpyxl.utils import get_column_letter as _gcl
                _ws.column_dimensions[_gcl(_ans_col)].width = 60

            # Лист 4: Статистика по уровням
            if stats['levels']:
                levels_df = pd.DataFrame(stats['levels'])
                levels_df.columns = ['Уровень', 'Код', 'Сценариев', 'Прохождений', 'Средний балл (%)']
                levels_df.to_excel(writer, sheet_name='По уровням', index=False)

            # Лист 5: Матрица "Пройдено / Не пройдено" по уровням
            try:
                from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
                from openpyxl.utils import get_column_letter

                matrix_data = trainer_mgr.get_completion_matrix(passing_percent=70)
                ws = writer.book.create_sheet('Пройдено - Не пройдено')

                # Цвета
                fill_passed      = PatternFill('solid', fgColor='C8E6C9')  # зелёный
                fill_failed      = PatternFill('solid', fgColor='FFCDD2')  # красный
                fill_visited     = PatternFill('solid', fgColor='FFF9C4')  # жёлтый — зашёл, не завершил
                fill_not_started = PatternFill('solid', fgColor='F5F5F5')  # серый
                fill_header      = PatternFill('solid', fgColor='1A237E')  # тёмно-синий
                fill_level       = PatternFill('solid', fgColor='3949AB')  # синий уровень
                fill_summary     = PatternFill('solid', fgColor='E8EAF6')  # светло-синий

                font_white  = Font(color='FFFFFF', bold=True)
                font_bold   = Font(bold=True)
                font_passed = Font(color='1B5E20', bold=True)
                font_failed = Font(color='B71C1C')
                font_grey   = Font(color='9E9E9E')

                thin = Side(style='thin', color='DDDDDD')
                border = Border(left=thin, right=thin, top=thin, bottom=thin)
                center = Alignment(horizontal='center', vertical='center', wrap_text=True)

                levels    = matrix_data['levels']
                users     = matrix_data['users']
                matrix    = matrix_data['matrix']
                summary   = matrix_data['summary']
                passing_p = matrix_data['passing_percent']

                # === Строка 1: Заголовок ===
                total_cols = 1 + sum(len(lv['scenarios']) for lv in levels) + 2  # сотрудник + сценарии + итого пройдено + %
                ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
                title_cell = ws.cell(row=1, column=1,
                    value=f'Матрица прохождения сценариев (порог: {passing_p}%)')
                title_cell.fill = fill_header
                title_cell.font = Font(color='FFFFFF', bold=True, size=12)
                title_cell.alignment = center

                # === Строка 2: Группировка по уровням ===
                col = 2
                ws.cell(row=2, column=1, value='Сотрудник').fill = fill_header
                ws.cell(row=2, column=1).font = font_white
                ws.cell(row=2, column=1).alignment = center

                level_col_ranges = []  # для итогов по уровням
                for lv in levels:
                    n = len(lv['scenarios'])
                    if n == 0:
                        continue
                    ws.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col + n - 1)
                    lv_cell = ws.cell(row=2, column=col, value=lv['name'])
                    lv_cell.fill = fill_level
                    lv_cell.font = font_white
                    lv_cell.alignment = center
                    level_col_ranges.append({'level': lv, 'start_col': col, 'end_col': col + n - 1})
                    col += n

                ws.cell(row=2, column=col, value='Пройдено').fill = fill_header
                ws.cell(row=2, column=col).font = font_white
                ws.cell(row=2, column=col).alignment = center
                ws.cell(row=2, column=col + 1, value='% выполнения').fill = fill_header
                ws.cell(row=2, column=col + 1).font = font_white
                ws.cell(row=2, column=col + 1).alignment = center

                # === Строка 3: Названия сценариев ===
                ws.cell(row=3, column=1).fill = fill_header
                col = 2
                for lv in levels:
                    for sc in lv['scenarios']:
                        sc_cell = ws.cell(row=3, column=col, value=sc['title'])
                        sc_cell.fill = fill_summary
                        sc_cell.font = font_bold
                        sc_cell.alignment = center
                        col += 1
                ws.cell(row=3, column=col).fill = fill_summary
                ws.cell(row=3, column=col + 1).fill = fill_summary

                # Фиксируем ширину первого столбца
                ws.column_dimensions['A'].width = 20
                for c in range(2, total_cols + 1):
                    ws.column_dimensions[get_column_letter(c)].width = 14

                # === Строки данных: по одной на пользователя ===
                for row_idx, uid in enumerate(users):
                    data_row = 4 + row_idx
                    # Имя пользователя
                    name_cell = ws.cell(row=data_row, column=1, value=uid)
                    name_cell.font = font_bold
                    name_cell.alignment = Alignment(vertical='center')
                    name_cell.border = border

                    col = 2
                    for lv in levels:
                        for sc in lv['scenarios']:
                            cell_data = matrix[uid].get(sc['id'], {'status': 'not_started', 'best_percent': None, 'attempts': 0})
                            status = cell_data['status']
                            pct    = cell_data['best_percent']
                            att    = cell_data['attempts']

                            if status == 'passed':
                                text  = f'✓ {pct}%'
                                fill  = fill_passed
                                fnt   = font_passed
                            elif status == 'failed':
                                text  = f'✗ {pct}%\n({att} поп.)'
                                fill  = fill_failed
                                fnt   = font_failed
                            elif status == 'visited':
                                vc = cell_data.get('visit_count', 1)
                                text  = f'👁 открывал\n({vc} раз)'
                                fill  = fill_visited
                                fnt   = Font(color='F57F17')
                            else:
                                text  = '—'
                                fill  = fill_not_started
                                fnt   = font_grey

                            cell = ws.cell(row=data_row, column=col, value=text)
                            cell.fill = fill
                            cell.font = fnt
                            cell.alignment = center
                            cell.border = border
                            col += 1

                    # Итог по пользователю
                    sm = summary[uid]
                    parts = [f'✓{sm["passed"]}']
                    if sm['failed']:   parts.append(f'✗{sm["failed"]}')
                    if sm['visited']:  parts.append(f'👁{sm["visited"]}')
                    if sm['not_started']: parts.append(f'—{sm["not_started"]}')
                    total_cell = ws.cell(row=data_row, column=col,
                        value=' / '.join(parts))
                    total_cell.font = font_bold
                    total_cell.alignment = center
                    total_cell.border = border

                    pct_cell = ws.cell(row=data_row, column=col + 1,
                        value=f'{sm["percent_done"]}%')
                    pct_cell.alignment = center
                    pct_cell.border = border
                    if sm['percent_done'] == 100:
                        pct_cell.fill = fill_passed
                        pct_cell.font = font_passed
                    elif sm['percent_done'] >= 50:
                        pct_cell.fill = PatternFill('solid', fgColor='FFF9C4')
                        pct_cell.font = Font(color='F57F17', bold=True)
                    else:
                        pct_cell.fill = fill_failed
                        pct_cell.font = font_failed

                # === Строки высота ===
                ws.row_dimensions[1].height = 22
                ws.row_dimensions[2].height = 20
                ws.row_dimensions[3].height = 40
                for i in range(len(users)):
                    ws.row_dimensions[4 + i].height = 32

                # Закрепляем первые 3 строки и первый столбец
                ws.freeze_panes = 'B4'

            except Exception as e_matrix:
                print(f'[export] Ошибка листа матрицы: {e_matrix}')
                import traceback as tb
                tb.print_exc()

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
@AdminAuth.trainer_view_required
def admin_trainer_audit():
    """Журнал изменений (аудит)"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page

    logs = trainer_mgr.get_audit_log(limit=per_page, offset=offset)
    stats = trainer_mgr.get_audit_stats()

    return render_template('admin_trainer_audit.html', logs=deep_escape(logs), stats=deep_escape(stats), page=page)


@app.route('/admin/trainer/audit/export')
@AdminAuth.trainer_view_required
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
# ОБРАТНАЯ СВЯЗЬ ОТ СПЕЦИАЛИСТОВ
# ============================================

@app.route('/api/trainer/feedback', methods=['POST'])
@rate_limit(max_requests=10, window=60)
def trainer_submit_feedback():
    """API: отправить обратную связь"""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401

    try:
        data = request.get_json()
        message = (data.get('message') or '').strip()
        level_code = data.get('level_code')
        segment = data.get('segment', 'kc')

        if not message:
            return jsonify({'success': False, 'error': 'Сообщение не может быть пустым'})

        if len(message) > 2000:
            return jsonify({'success': False, 'error': 'Сообщение слишком длинное (макс. 2000 символов)'})

        user_id = session['user_info'].get('username', 'anonymous')
        result = trainer_mgr.add_feedback(user_id, message, level_code, segment=segment)
        return jsonify(result)
    except Exception as e:
        print(f"[API] Ошибка отправки отзыва: {e}")
        return jsonify({'success': False, 'error': 'Ошибка отправки отзыва'}), 500


@app.route('/admin/trainer/feedback')
@AdminAuth.login_required
def admin_trainer_feedback():
    """Страница обратной связи от специалистов"""
    segment = request.args.get('segment', 'kc')
    seg_info = TRAINER_SEGMENTS.get(segment, TRAINER_SEGMENTS['kc'])
    feedback_list = trainer_mgr.get_all_feedback(segment=segment)
    unread_count = trainer_mgr.get_unread_feedback_count(segment=segment)
    return render_template('admin_trainer_feedback.html',
                           feedback_list=feedback_list,
                           unread_count=unread_count,
                           segment=segment,
                           seg_info=seg_info)


@app.route('/api/admin/trainer/feedback/<int:feedback_id>/read', methods=['POST'])
@csrf.exempt
@AdminAuth.login_required
def admin_trainer_feedback_mark_read(feedback_id):
    """API: пометить обратную связь как прочитанную"""
    result = trainer_mgr.mark_feedback_read(feedback_id)
    return jsonify(result)


@app.route('/api/trainer/my-feedback')
def trainer_my_feedback():
    """API: получить обратную связь текущего пользователя"""
    if 'user_info' not in session or not session.get('authenticated'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    try:
        user_id = session['user_info'].get('username', 'anonymous')
        segment = request.args.get('segment')
        feedback_list = trainer_mgr.get_user_feedback(user_id, segment=segment)
        return jsonify({'success': True, 'feedback': feedback_list})
    except Exception as e:
        print(f"[trainer_my_feedback] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500


# ============================================
# ИМПОРТ СЦЕНАРИЕВ ИЗ EXCEL
# ============================================

@app.route('/admin/trainer/import')
@AdminAuth.trainer_required
def admin_trainer_import():
    """Страница импорта сценариев"""
    return render_template('admin_trainer_import.html')


@app.route('/admin/trainer/import/template')
@AdminAuth.trainer_required
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
@AdminAuth.trainer_required
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
        print(f"[trainer_import_preview] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка чтения файла. Проверьте формат Excel.'})


@app.route('/admin/trainer/import/confirm', methods=['POST'])
@AdminAuth.trainer_required
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
        return jsonify({'success': False, 'error': 'Ошибка импорта сценария'})


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
        def failed_login(error_code: str):
            session['login_failed_attempts'] = int(session.get('login_failed_attempts') or 0) + 1
            return redirect(url_for('user_login', error=error_code))

        # Rate limiting для защиты от brute force
        ip = get_client_ip()
        if not rate_limiter.check_login_attempt(ip, max_attempts=10, window=300):
            return redirect(url_for('user_login', error='rate_limit')), 429

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Валидация длины
        if len(username) > 100 or len(password) > 128:
            return failed_login('credentials')

        # Тестовый режим (без AD) - для локальной разработки
        # По умолчанию включен если нет AD_SERVER в .env
        TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

        if TEST_MODE:
            # Тестовая авторизация: любой логин/пароль где пароль = "test" или "123"
            if password in ['test', '123', 'password']:
                session.clear()
                session['user_info'] = {
                    'username': username,
                    'name': username.title(),
                    'department': 'Тестовый отдел',
                    'email': f'{username}@test.local',
                    'workplace': ''
                }
                session['authenticated'] = True

                # Определяем роли из .env (как в AD режиме)
                from ad_auth import ad_auth
                lower_user = username.lower()
                test_permissions = []
                if lower_user in ad_auth.super_admin_logins:
                    test_permissions.append('super_admin')
                if lower_user in ad_auth.admins_manuals:
                    test_permissions.append('admin_manuals')
                if lower_user in ad_auth.admins_topics:
                    test_permissions.append('admin_topics')
                if lower_user in ad_auth.admins_scenarios:
                    test_permissions.append('admin_scenarios')
                if lower_user in ad_auth.admins_trainer:
                    test_permissions.append('admin_trainer')
                if lower_user in ad_auth.trainer_viewers:
                    test_permissions.append('trainer_viewer')

                test_permissions, admin_role, trainer_segments = _merge_admin_permissions(username, test_permissions)

                if test_permissions:
                    session['admin_logged_in'] = True
                    session['admin_username'] = username
                    session['admin_role'] = admin_role
                    session['admin_permissions'] = test_permissions
                    session['trainer_segments'] = trainer_segments
                    session['admin_token'] = AdminAuth.generate_session_token()

                session.pop('login_failed_attempts', None)
                return redirect(url_for('choose_help_type'))
            else:
                return failed_login('invalid')

        # Аутентификация через AD (продакшен)
        from ad_auth import ad_auth
        ad_result = ad_auth.verify_credentials(username, password)

        if ad_result:
            # Успешная аутентификация - очищаем старую сессию и сохраняем данные
            session.clear()
            session['user_info'] = {
                'username': ad_result.get('username', username),
                'name': ad_result.get('display_name', username),
                'department': ad_result.get('department', ''),
                'email': ad_result.get('email', ''),
                'workplace': ''  # Будет заполнено позже при необходимости
            }
            session['authenticated'] = True
            # Автоматический вход в админку если есть права
            ad_permissions, admin_role, trainer_segments = _merge_admin_permissions(
                ad_result.get('username', username),
                ad_result.get('permissions', [])
            )
            if ad_permissions:
                session['admin_logged_in'] = True
                session['admin_username'] = ad_result.get('username', username)
                session['admin_role'] = admin_role
                session['admin_permissions'] = ad_permissions
                session['trainer_segments'] = trainer_segments
                session['admin_token'] = AdminAuth.generate_session_token()
            session.pop('login_failed_attempts', None)

            # Переходим к выбору типа помощи
            return redirect(url_for('choose_help_type'))
        else:
            return failed_login('invalid')

    failed_attempts = int(session.get('login_failed_attempts') or 0)
    return render_template('user_login.html', error_message=error_message, failed_attempts=failed_attempts)

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


@app.route('/enter_telegram_username', methods=['GET', 'POST'])
def enter_telegram_username():
    """Однократный ввод Telegram username перед первой отправкой заявки."""
    if 'user_info' not in session or not session.get('authenticated'):
        return redirect(url_for('user_login'))

    user_info = dict(session.get('user_info') or {})

    if request.method == 'POST':
        telegram_username = _normalize_telegram_username(request.form.get('telegram_username'))
        if not telegram_username:
            flash('Укажите Telegram username в формате @username, от 5 до 32 символов.')
            return redirect(url_for('enter_telegram_username'))

        user_info['telegram_username'] = telegram_username
        session['user_info'] = user_info
        session.modified = True
        _save_user_telegram_username(user_info.get('username'), telegram_username)

        next_endpoint = session.pop('next_after_telegram_username', 'choose_help_type')
        next_args = session.pop('next_after_telegram_username_args', {}) or {}
        try:
            return redirect(url_for(next_endpoint, **next_args))
        except werkzeug.routing.BuildError:
            return redirect(url_for('choose_help_type'))

    current_username = _get_session_telegram_username()
    return render_template(
        'enter_telegram_username.html',
        user_info=user_info,
        current_username=current_username
    )


def _merge_admin_permissions(username: str, permissions: list[str] | None = None):
    """Дополняет права из .env правами AD-логина, назначенными через админку."""
    base_permissions = admins_manager.normalize_permissions(permissions or [])
    local_admin = admins_manager.get_admin_by_username(username)

    if local_admin and local_admin.get('active', True) and admins_manager.is_ad_admin(local_admin):
        local_permissions = admins_manager.normalize_permissions(
            local_admin.get('permissions'),
            local_admin.get('role', '')
        )
        base_permissions = admins_manager.normalize_permissions(base_permissions + local_permissions)

    role = admins_manager.role_from_permissions(base_permissions)
    trainer_segments = (local_admin or {}).get('trainer_segments', ['kc', 'branch'])
    if role == ROLE_SUPER_ADMIN:
        trainer_segments = ['kc', 'branch']

    return base_permissions, role, trainer_segments


def _admin_default_endpoint(permissions: list[str] | None = None) -> str:
    permissions = admins_manager.normalize_permissions(permissions or session.get('admin_permissions', []))
    if ROLE_SUPER_ADMIN in permissions:
        return 'admin_dashboard'
    if ROLE_ADMIN_MANUALS in permissions:
        return 'admin_dashboard'
    if ROLE_ADMIN_TOPICS in permissions:
        return 'admin_topics'
    if ROLE_ADMIN_SCENARIOS in permissions:
        return 'admin_scenarios'
    if ROLE_ADMIN_TRAINER in permissions:
        return 'admin_trainer'
    if ROLE_TRAINER_VIEWER in permissions:
        return 'admin_trainer_stats'
    return 'admin_login'


def _admin_env_permissions(username: str) -> list[str]:
    """Права из env/AD-тестовых списков без повторного bind в AD."""
    try:
        from ad_auth import ad_auth
        lower_user = str(username or '').strip().lower()
        permissions = []
        if lower_user in ad_auth.super_admin_logins:
            permissions.append(ROLE_SUPER_ADMIN)
        if lower_user in ad_auth.admins_manuals:
            permissions.append(ROLE_ADMIN_MANUALS)
        if lower_user in ad_auth.admins_topics:
            permissions.append(ROLE_ADMIN_TOPICS)
        if lower_user in ad_auth.admins_scenarios:
            permissions.append(ROLE_ADMIN_SCENARIOS)
        if lower_user in ad_auth.admins_trainer:
            permissions.append(ROLE_ADMIN_TRAINER)
        if lower_user in ad_auth.trainer_viewers:
            permissions.append(ROLE_TRAINER_VIEWER)
        return admins_manager.normalize_permissions(permissions)
    except Exception:
        return []


def _admin_section_allowed(section: str, permissions: list[str] | None) -> bool:
    """Проверяет, можно ли подтверждать вход в конкретный админ-раздел."""
    normalized = admins_manager.normalize_permissions(permissions or [])
    if ROLE_SUPER_ADMIN in normalized:
        return True

    required_by_section = {
        'manuals': [ROLE_ADMIN_MANUALS],
        'topics': [ROLE_ADMIN_TOPICS],
        'scenarios': [ROLE_ADMIN_SCENARIOS],
        'trainer': [ROLE_ADMIN_TRAINER],
        'trainer_stats': [ROLE_ADMIN_TRAINER, ROLE_TRAINER_VIEWER],
    }
    required = required_by_section.get(str(section or '').strip())
    if required:
        return any(permission in normalized for permission in required)
    return bool(normalized)


@app.before_request
def refresh_admin_session_permissions():
    """Применяет изменённые права админа к активной сессии без повторного входа."""
    if not session.get('admin_logged_in') or request.path.startswith('/static/'):
        return None

    admin_paths = ('/admin', '/api/admin', '/api/stats')
    if not request.path.startswith(admin_paths):
        return None

    username = session.get('admin_username', '')
    if not username:
        return None

    local_admin = admins_manager.get_admin_by_username(username)
    env_permissions = _admin_env_permissions(username)
    new_permissions = None
    trainer_segments = session.get('trainer_segments', ['kc', 'branch'])

    if local_admin:
        if not local_admin.get('active', True):
            new_permissions = env_permissions if admins_manager.is_ad_admin(local_admin) else []
        else:
            local_permissions = admins_manager.normalize_permissions(
                local_admin.get('permissions'),
                local_admin.get('role', '')
            )
            if admins_manager.is_ad_admin(local_admin):
                new_permissions = admins_manager.normalize_permissions(env_permissions + local_permissions)
            else:
                new_permissions = local_permissions
            trainer_segments = local_admin.get('trainer_segments', trainer_segments)
    elif env_permissions:
        new_permissions = env_permissions

    if new_permissions is None:
        return None

    if not new_permissions:
        for key in ('admin_logged_in', 'admin_username', 'admin_role', 'admin_permissions', 'admin_token', 'trainer_segments'):
            session.pop(key, None)
        session.modified = True
        return redirect(url_for('admin_login'))

    if ROLE_SUPER_ADMIN in new_permissions:
        trainer_segments = ['kc', 'branch']

    current_permissions = admins_manager.normalize_permissions(session.get('admin_permissions', []))
    if current_permissions != new_permissions or session.get('trainer_segments') != trainer_segments:
        session['admin_permissions'] = new_permissions
        session['admin_role'] = admins_manager.role_from_permissions(new_permissions)
        session['trainer_segments'] = trainer_segments
        session.modified = True

    return None

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Страница авторизации администратора"""
    # Если уже залогинен через AD с правами админа — сразу в дашборд
    if session.get('admin_logged_in') and session.get('admin_permissions'):
        return redirect(url_for(_admin_default_endpoint()))

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
            # Определяем роли из .env
            from ad_auth import ad_auth
            lower_user = username.lower()
            test_permissions = []
            if lower_user in ad_auth.super_admin_logins:
                test_permissions.append('super_admin')
            if lower_user in ad_auth.admins_manuals:
                test_permissions.append('admin_manuals')
            if lower_user in ad_auth.admins_topics:
                test_permissions.append('admin_topics')
            if lower_user in ad_auth.admins_scenarios:
                test_permissions.append('admin_scenarios')
            if lower_user in ad_auth.admins_trainer:
                test_permissions.append('admin_trainer')
            if lower_user in ad_auth.trainer_viewers:
                test_permissions.append('trainer_viewer')

            test_permissions, admin_role, trainer_segments = _merge_admin_permissions(username, test_permissions)

            if not test_permissions:
                flash('У вас нет прав администратора')
                return redirect(url_for('admin_login'))

            session['admin_logged_in'] = True
            session['admin_username'] = username
            session['admin_role'] = admin_role
            session['admin_permissions'] = test_permissions
            session['trainer_segments'] = trainer_segments
            session['admin_token'] = AdminAuth.generate_session_token()
            session.permanent = True
            role_names = ', '.join(test_permissions)
            flash(f'Успешная авторизация (тестовый режим). Права: {role_names}')
            return redirect(url_for(_admin_default_endpoint(test_permissions)))

        # Проверка через AD (основной способ)
        from ad_auth import ad_auth
        if ad_auth.is_configured():
            ad_result = ad_auth.verify_credentials(username, password)
            if ad_result:
                ad_permissions, admin_role, trainer_segments = _merge_admin_permissions(
                    ad_result.get('username', username),
                    ad_result.get('permissions', [])
                )
                if ad_permissions:
                    session['admin_logged_in'] = True
                    session['admin_username'] = ad_result.get('username', username)
                    session['admin_role'] = admin_role
                    session['admin_permissions'] = ad_permissions
                    session['trainer_segments'] = trainer_segments
                    session['admin_token'] = AdminAuth.generate_session_token()
                    session.permanent = True
                    # Также ставим user_info чтобы сессия была полной
                    if not session.get('authenticated'):
                        session['user_info'] = {
                            'username': ad_result.get('username', username),
                            'name': ad_result.get('display_name', username),
                            'department': ad_result.get('department', ''),
                            'email': ad_result.get('email', ''),
                            'workplace': ''
                        }
                        session['authenticated'] = True
                    role_names = ', '.join(ad_permissions)
                    flash(f'Успешная авторизация (AD). Права: {role_names}')
                    return redirect(url_for(_admin_default_endpoint(ad_permissions)))
                else:
                    flash('У вас нет прав администратора')
                    return redirect(url_for('admin_login'))
            # AD проверка не прошла — пароль неверный
            flash('Неверный логин или пароль')
            return redirect(url_for('admin_login'))

        # Fallback: проверка через admins.json (если AD не настроен)
        admin_data = AdminAuth.verify_admin(username, password)
        if admin_data:
            admin_permissions = admins_manager.normalize_permissions(
                admin_data.get('permissions'),
                admin_data.get('role', ROLE_EDITOR)
            )
            session['admin_logged_in'] = True
            session['admin_username'] = username
            session['admin_role'] = admins_manager.role_from_permissions(admin_permissions)
            session['admin_permissions'] = admin_permissions
            session['admin_token'] = AdminAuth.generate_session_token()
            # Сегменты тренажёра: супер-админ всегда видит все
            if ROLE_SUPER_ADMIN in admin_permissions:
                session['trainer_segments'] = ['kc', 'branch']
            else:
                session['trainer_segments'] = admin_data.get('trainer_segments', ['kc', 'branch'])
            session.permanent = True
            flash(f'Успешная авторизация. Роль: {ROLE_NAMES.get(admin_data.get("role"), "Редактор")}')
            return redirect(url_for(_admin_default_endpoint(admin_permissions)))
        else:
            flash('Неверный логин или пароль')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    """Выход из админ-панели"""
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    session.pop('admin_role', None)
    session.pop('admin_permissions', None)
    session.pop('admin_token', None)
    flash('Вы вышли из системы')
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
@AdminAuth.login_required
def admin_dashboard():
    """Новая главная страница админ-панели."""
    permissions = session.get('admin_permissions', [])
    if ROLE_SUPER_ADMIN not in permissions and ROLE_ADMIN_MANUALS not in permissions:
        return redirect(url_for(_admin_default_endpoint(permissions)))
    return render_template('admin_dashboard_new.html', admin_permissions=permissions)


@app.route('/admin/dashboard-new')
@AdminAuth.login_required
def admin_dashboard_new():
    """Совместимость со старой ссылкой на новый dashboard."""
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/manuals')
@AdminAuth.manuals_required
def admin_manuals():
    """Старая страница управления мануалами."""
    manuals = admin_manager.load_manuals()
    return render_template('admin_dashboard.html', manuals=manuals)


@app.route('/admin/manual/create', methods=['GET', 'POST'])
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
def admin_edit_manual(manual_id):
    """Страница редактирования мануала - теперь показывает список подпроблем"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

    # Если есть поле subproblems - показываем список подпроблем (даже если пустой)
    if 'subproblems' in manual:
        return render_template('admin_manual_subproblems.html', manual_id=manual_id, manual=manual)

    # Если нет поля subproblems - это простой мануал
    return redirect(url_for('admin_edit_simple_manual', manual_id=manual_id))


@app.route('/admin/manual/<string:manual_id>/subproblem/create', methods=['GET', 'POST'])
@AdminAuth.manuals_required
def admin_create_subproblem(manual_id):
    """Создание новой подпроблемы"""
    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

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
@AdminAuth.manuals_required
def admin_delete_manual(manual_id):
    """Удаление мануала"""
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    manuals = admin_manager.load_manuals()

    if manual_id not in manuals:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

    manual_title = manuals[manual_id].get('title', 'Неизвестный мануал')

    # Удаляем мануал
    del manuals[manual_id]

    if admin_manager.save_manuals(manuals):
        flash(f'Мануал "{manual_title}" успешно удалён')
    else:
        flash('Ошибка при удалении мануала')

    return redirect(url_for('admin_manuals'))


@app.route('/admin/manual/<string:manual_id>/subproblem/<string:subproblem_id>/delete', methods=['POST'])
@AdminAuth.manuals_required
def admin_delete_subproblem(manual_id, subproblem_id):
    """Удаление подпроблемы"""
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_manuals'))

    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

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
@AdminAuth.manuals_required
def admin_edit_simple_manual(manual_id):
    """Страница редактирования простого мануала (без подпроблем)"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))
    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

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
@AdminAuth.manuals_required
def admin_edit_subproblem(manual_id, subproblem_id):
    """Страница редактирования отдельной подпроблемы"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    if not admin_manager.validate_subproblem_id(subproblem_id):
        flash('Некорректный ID подпроблемы')
        return redirect(url_for('admin_manuals'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

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
@AdminAuth.manuals_required
def admin_update_manual(manual_id):
    """Обновление мануала (только заголовок, подпроблемы редактируются отдельно)"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    title = request.form.get('title', '').strip()

    # Валидация заголовка
    title = admin_manager.sanitize_text(title, max_length=200)
    if not title:
        flash('Заголовок не может быть пустым')
        return redirect(url_for('admin_edit_manual', manual_id=manual_id))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

    # Обновляем только заголовок
    manual['title'] = title

    # Сохраняем изменения
    if admin_manager.update_manual(manual_id, title, manual):
        flash('Заголовок мануала успешно обновлён')
    else:
        flash('Ошибка при сохранении изменений')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/manual/<string:manual_id>/subproblem/<string:subproblem_id>/update', methods=['POST'])
@AdminAuth.manuals_required
def admin_update_subproblem(manual_id, subproblem_id):
    """Обновление отдельной подпроблемы"""
    # Валидация ID
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    manual = admin_manager.get_manual(manual_id)
    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

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

    if redirect_url.startswith('/'):
        return redirect(redirect_url)
    else:
        abort(400, "Invalid redirect URL")


@app.route('/admin/delete-photo', methods=['POST'])
@AdminAuth.manuals_required
def admin_delete_photo():
    """Удаление фото из мануала"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    photo_index_str = request.form.get('photo_index', '0')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    # Для простых мануалов subproblem_id = manual_id, пропускаем проверку формата X.Y
    manual_check = admin_manager.get_manual(manual_id)
    if manual_check and 'subproblems' in manual_check:
        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_manuals'))

    try:
        photo_index = int(photo_index_str)
        if photo_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс фото')
        return redirect(url_for('admin_manuals'))

    # Удаляем фото
    if admin_manager.delete_photo(manual_id, subproblem_id, photo_index):
        flash('Фото успешно удалено')
    else:
        flash('Ошибка при удалении фото')

    return redirect(url_for('admin_edit_manual', manual_id=manual_id))


@app.route('/admin/delete-step', methods=['POST'])
@AdminAuth.manuals_required
def admin_delete_step():
    """Удаление всего шага (фото + описание) из подпроблемы"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    step_index_str = request.form.get('step_index', '0')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
    # if not admin_manager.validate_subproblem_id(subproblem_id):
    #     flash('Некорректный ID подпроблемы')
    #     return redirect(url_for('admin_manuals'))

    try:
        step_index = int(step_index_str)
        if step_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс шага')
        return redirect(url_for('admin_manuals'))

    # Загружаем мануалы
    manuals = admin_manager.load_manuals()
    manual = manuals.get(manual_id)

    if not manual:
        flash('Мануал не найден')
        return redirect(url_for('admin_manuals'))

    # Определяем тип мануала и получаем нужный объект
    if 'subproblems' in manual:
        # Мануал с подпроблемами
        if subproblem_id not in manual['subproblems']:
            flash('Подпроблема не найдена')
            return redirect(url_for('admin_manuals'))
        target_obj = manual['subproblems'][subproblem_id]
        redirect_url = url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id)
    else:
        # Простой мануал
        target_obj = manual
        redirect_url = url_for('admin_edit_simple_manual', manual_id=manual_id)

    if 'photos' not in target_obj or not isinstance(target_obj['photos'], list):
        flash('Шаги не найдены')
        if redirect_url.startswith('/'):
            return redirect(redirect_url)
        else:
            abort(400, "Invalid redirect URL")

    # Проверяем индекс
    if step_index >= len(target_obj['photos']):
        flash('Шаг не найден')
        if redirect_url.startswith('/'):
            return redirect(redirect_url)
        else:
            abort(400, "Invalid redirect URL")

    # Удаляем шаг
    del target_obj['photos'][step_index]

    # Сохраняем
    if admin_manager.save_manuals(manuals):
        flash(f'Шаг {step_index + 1} успешно удалён')
    else:
        flash('Ошибка при удалении шага')

    if redirect_url.startswith('/'):
        return redirect(redirect_url)
    else:
        abort(400, "Invalid redirect URL")


@app.route('/admin/delete-video', methods=['POST'])
@AdminAuth.manuals_required
def admin_delete_video():
    """Удаление видео из подпроблемы"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    # Для простых мануалов subproblem_id = manual_id, пропускаем проверку формата X.Y
    manual_check = admin_manager.get_manual(manual_id)
    if manual_check and 'subproblems' in manual_check:
        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_manuals'))

    # Удаляем видео
    if admin_manager.delete_video(manual_id, subproblem_id):
        flash('Видео успешно удалено')
    else:
        flash('Ошибка при удалении видео')

    _rurl = url_for('admin_edit_manual', manual_id=manual_id)
    if _rurl.startswith('/'):
        return redirect(_rurl)
    else:
        abort(400, "Invalid redirect URL")


@app.route('/admin/upload-photo', methods=['GET', 'POST'])
@AdminAuth.manuals_required
def admin_upload_photo():
    """Загрузка нового скриншота"""
    if request.method == 'GET':
        manual_id = request.args.get('manual_id', '')
        subproblem_id = request.args.get('subproblem_id', '')
        photo_index = request.args.get('photo_index', '0')

        # Валидация параметров
        if not admin_manager.validate_manual_id(manual_id):
            flash('Некорректный ID мануала')
            return redirect(url_for('admin_manuals'))

        # Для простых мануалов subproblem_id = manual_id (только цифры), пропускаем проверку формата X.Y
        manual = admin_manager.get_manual(manual_id)
        if manual and 'subproblems' in manual:
            if not admin_manager.validate_subproblem_id(subproblem_id):
                flash('Некорректный ID подпроблемы')
                return redirect(url_for('admin_manuals'))

        try:
            photo_index = int(photo_index)
            if photo_index < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash('Некорректный индекс фото')
            return redirect(url_for('admin_manuals'))

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
        return redirect(url_for('admin_manuals'))

    # Для простых мануалов subproblem_id = manual_id, пропускаем проверку формата X.Y
    manual_for_check = admin_manager.get_manual(manual_id)
    if manual_for_check and 'subproblems' in manual_for_check:
        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_manuals'))

    try:
        photo_index = int(photo_index_str)
        if photo_index < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Некорректный индекс фото')
        return redirect(url_for('admin_manuals'))

    # Security Fix: Improved file upload validation
    allowed_image_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    max_file_size = 10 * 1024 * 1024  # 10 MB

    # Security Fix: Safe redirect URL via url_for (prevents Open Redirect)
    _safe_redirect = url_for('admin_upload_photo', manual_id=manual_id, subproblem_id=subproblem_id, photo_index=photo_index_str)

    if 'photo' not in request.files:
        flash('Файл не был загружен')
        return redirect(_safe_redirect)

    file = request.files['photo']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(_safe_redirect)

    # Security Fix: Strict content type validation
    if not file.content_type or file.content_type not in allowed_image_types:
        flash('Можно загружать только изображения (JPEG, PNG, GIF, WebP)')
        return redirect(_safe_redirect)

    # Security Fix: Check content-length header first
    if request.content_length and request.content_length > max_file_size:
        flash('Файл слишком большой (максимум 10 МБ)')
        return redirect(_safe_redirect)

    # Проверка размера (максимум 10MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size > max_file_size:
        flash('Файл слишком большой (максимум 10 МБ)')
        return redirect(_safe_redirect)

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
                return redirect(url_for('admin_manuals'))

            current_caption = ""
            if 'subproblems' in manual and subproblem_id in manual['subproblems']:
                target_obj = manual['subproblems'][subproblem_id]
            else:
                target_obj = manual
            if 'photos' in target_obj and photo_index < len(target_obj['photos']):
                current_caption = target_obj['photos'][photo_index].get('caption', '')

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
@AdminAuth.manuals_required
def admin_add_new_step():
    """Добавление нового шага в подпроблему"""
    manual_id = request.form.get('manual_id', '')
    subproblem_id = request.form.get('subproblem_id', '')
    caption = request.form.get('caption', '').strip()
    after_index_str = request.form.get('after_index', '-1')

    # Валидация
    if not admin_manager.validate_manual_id(manual_id):
        flash('Некорректный ID мануала')
        return redirect(url_for('admin_manuals'))

    # Для простых мануалов subproblem_id = manual_id, для подпроблем проверяем формат X.Y
    manual = admin_manager.get_manual(manual_id)
    if manual and 'subproblems' in manual:
        if not admin_manager.validate_subproblem_id(subproblem_id):
            flash('Некорректный ID подпроблемы')
            return redirect(url_for('admin_manuals'))

    caption = admin_manager.sanitize_text(caption, max_length=300)
    if not caption:
        flash('Описание шага не может быть пустым')
        _rurl = url_for('admin_edit_manual', manual_id=manual_id)
        if _rurl.startswith('/'):
            return redirect(_rurl)
        else:
            abort(400, "Invalid redirect URL")

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
        _rurl = url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id)
        if _rurl.startswith('/'):
            return redirect(_rurl)
        else:
            abort(400, "Invalid redirect URL")
    _rurl = url_for('admin_edit_simple_manual', manual_id=manual_id)
    if _rurl.startswith('/'):
        return redirect(_rurl)
    else:
        abort(400, "Invalid redirect URL")


@app.route('/admin/upload-video', methods=['GET', 'POST'])
@AdminAuth.manuals_required
def admin_upload_video():
    """Загрузка видео-мануала"""
    if request.method == 'GET':
        manual_id = request.args.get('manual_id', '')
        subproblem_id = request.args.get('subproblem_id', '')

        # Валидация параметров
        if not admin_manager.validate_manual_id(manual_id):
            flash('Некорректный ID мануала')
            return redirect(url_for('admin_manuals'))

        # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
        # if not admin_manager.validate_subproblem_id(subproblem_id):
        #     flash('Некорректный ID подпроблемы')
        #     return redirect(url_for('admin_manuals'))

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
        return redirect(url_for('admin_manuals'))

    # Не валидируем subproblem_id так как для простых мануалов он равен manual_id
    # if not admin_manager.validate_subproblem_id(subproblem_id):
    #     flash('Некорректный ID подпроблемы')
    #     return redirect(url_for('admin_manuals'))

    # Security Fix: Improved video upload validation
    allowed_video_types = {'video/mp4', 'video/mpeg', 'video/quicktime', 'video/x-msvideo', 'video/webm'}
    max_file_size = 50 * 1024 * 1024  # 50 MB

    # Security Fix: Safe redirect URL via url_for (prevents Open Redirect)
    _safe_redirect = url_for('admin_upload_video', manual_id=manual_id, subproblem_id=subproblem_id)

    if 'video' not in request.files:
        flash('Файл не был загружен')
        return redirect(_safe_redirect)

    file = request.files['video']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(_safe_redirect)

    # Security Fix: Strict content type validation
    if not file.content_type or file.content_type not in allowed_video_types:
        flash('Можно загружать только видео (MP4, MPEG, MOV, AVI, WebM)')
        return redirect(_safe_redirect)

    # Security Fix: Check content-length header first
    if request.content_length and request.content_length > max_file_size:
        flash('Файл слишком большой (максимум 50 МБ)')
        return redirect(_safe_redirect)

    # Проверка размера (максимум 50MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size > max_file_size:
        flash('Файл слишком большой (максимум 50 МБ)')
        return redirect(_safe_redirect)

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
        _rurl = url_for('admin_edit_subproblem', manual_id=manual_id, subproblem_id=subproblem_id)
        if _rurl.startswith('/'):
            return redirect(_rurl)
        else:
            abort(400, "Invalid redirect URL")
    else:
        # Простой мануал - редирект на страницу редактирования простого мануала
        _rurl = url_for('admin_edit_simple_manual', manual_id=manual_id)
        if _rurl.startswith('/'):
            return redirect(_rurl)
        else:
            abort(400, "Invalid redirect URL")


# ============================================
# УПРАВЛЕНИЕ ТЕМАТИКАМИ
# ============================================

@app.route('/admin/topics')
@AdminAuth.topics_required
def admin_topics():
    """Страница управления тематиками"""
    stats = tm.get_statistics()
    channels = tm.get_all_channels()
    archived_count = tm.get_archived_count()
    return render_template('admin_topics.html', stats=stats, channels=channels, archived_count=archived_count)


@app.route('/admin/topics/add', methods=['POST'])
@AdminAuth.topics_required
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
@AdminAuth.topics_required
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
@AdminAuth.topics_required
def admin_list_topics():
    """Список всех тематик"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    channel = request.args.get('channel', '').strip()
    channels = tm.get_all_channels()

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
                         per_page=per_page,
                         channels=channels,
                         current_channel=channel)


@app.route('/admin/topics/import', methods=['GET'])
@AdminAuth.topics_required
def admin_import_topics():
    """Страница импорта тематик из Excel"""
    stats = tm.get_statistics()
    return render_template('admin_import_topics.html', stats=stats)


@app.route('/admin/topics/import', methods=['POST'])
@AdminAuth.topics_required
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
        flash('Произошла ошибка при импорте. Проверьте формат файла.', 'error')

    return redirect(url_for('admin_import_topics'))


@app.route('/admin/topics/export')
@AdminAuth.topics_required
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
        flash('Произошла ошибка при экспорте. Попробуйте позже.', 'error')
        return redirect(url_for('admin_topics'))


# ============================================
# АРХИВ ТЕМАТИК И РЕДАКТИРОВАНИЕ
# ============================================


@app.route('/admin/topics/archive')
@AdminAuth.topics_required
def admin_topics_archive():
    """Страница архива тематик"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    archived = tm.get_archived_topics(limit=1000)
    total = len(archived)
    start = (page - 1) * per_page
    end = start + per_page
    archived_page = archived[start:end]
    return render_template('admin_topics_archive.html',
                         topics=archived_page, page=page,
                         total=total, per_page=per_page)


@app.route('/admin/topics/do-archive/<int:topic_id>', methods=['POST'])
@AdminAuth.topics_required
def admin_archive_topic(topic_id):
    """Архивирование одной тематики"""
    try:
        actor = _current_actor()
        archived_by = actor.get('username', 'unknown')
        result = tm.archive_topic(topic_id, archived_by)
        if result['success']:
            topic_data = result.get('topic', {})
            flash('Тематика перемещена в архив')
            log_topic_change(
                action='topic_archived',
                topic_id=topic_id,
                channel=topic_data.get('channel', ''),
                full_topic=topic_data.get('full_topic', ''),
                details={'archived_by': archived_by}
            )
        else:
            flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')
    except Exception as e:
        print(f"[admin_archive_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при архивации')
    return redirect(url_for('admin_list_topics'))


@app.route('/admin/topics/archive-bulk', methods=['POST'])
@AdminAuth.topics_required
def admin_archive_topics_bulk():
    """Массовое архивирование тематик"""
    try:
        topic_ids_raw = request.form.get('topic_ids', '')
        topic_ids = [int(x) for x in topic_ids_raw.split(',') if x.strip().isdigit()]
        if not topic_ids:
            flash('Не выбраны тематики для архивации')
            return redirect(url_for('admin_list_topics'))

        actor = _current_actor()
        archived_by = actor.get('username', 'unknown')
        result = tm.archive_topics_bulk(topic_ids, archived_by)
        if result['success']:
            flash(f'Архивировано тематик: {result["archived"]}')
            log_topic_change(
                action='topics_archived_bulk',
                topic_id=None, channel='', full_topic='',
                details={'archived_by': archived_by, 'count': result['archived']}
            )
        else:
            flash(f'Ошибка: {result.get("error")}')
    except Exception as e:
        print(f"[admin_archive_topics_bulk] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при массовой архивации')
    return redirect(url_for('admin_list_topics'))


@app.route('/admin/topics/restore/<int:topic_id>', methods=['POST'])
@AdminAuth.topics_required
def admin_restore_topic(topic_id):
    """Восстановление тематики из архива"""
    try:
        result = tm.restore_topic(topic_id)
        if result['success']:
            topic_data = result.get('topic', {})
            flash('Тематика восстановлена из архива')
            log_topic_change(
                action='topic_restored',
                topic_id=topic_id,
                channel=topic_data.get('channel', ''),
                full_topic=topic_data.get('full_topic', ''),
                details={'restored_by': _current_actor().get('username', '')}
            )
        else:
            flash(f'Ошибка: {result.get("error")}')
    except Exception as e:
        print(f"[admin_restore_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при восстановлении')
    return redirect(url_for('admin_topics_archive'))


@app.route('/admin/topics/edit/<int:topic_id>', methods=['GET'])
@AdminAuth.topics_required
def admin_edit_topic(topic_id):
    """Страница редактирования тематики"""
    topic = tm.get_topic_by_id(topic_id)
    if not topic:
        flash('Тематика не найдена')
        return redirect(url_for('admin_list_topics'))
    channels = tm.get_all_channels()
    return render_template('admin_edit_topic.html', topic=topic, channels=channels)


@app.route('/admin/topics/edit/<int:topic_id>', methods=['POST'])
@AdminAuth.topics_required
def admin_update_topic(topic_id):
    """Сохранение изменений тематики"""
    try:
        topic_before = tm.get_topic_by_id(topic_id)
        if not topic_before:
            flash('Тематика не найдена')
            return redirect(url_for('admin_list_topics'))

        channel = request.form.get('channel', '').strip()
        sr1 = request.form.get('sr1', '').strip() or None
        sr2 = request.form.get('sr2', '').strip() or None
        sr3 = request.form.get('sr3', '').strip() or None
        sr4 = request.form.get('sr4', '').strip() or None
        full_topic = request.form.get('full_topic', '').strip() or None

        if not channel:
            flash('Канал обязателен для заполнения')
            return redirect(url_for('admin_edit_topic', topic_id=topic_id))

        if len(channel) > 100:
            flash('Канал слишком длинный (макс. 100 символов)')
            return redirect(url_for('admin_edit_topic', topic_id=topic_id))

        for field, value in [('SR1', sr1), ('SR2', sr2), ('SR3', sr3), ('SR4', sr4)]:
            if value and len(value) > 200:
                flash(f'{field} слишком длинный (макс. 200 символов)')
                return redirect(url_for('admin_edit_topic', topic_id=topic_id))

        if full_topic and len(full_topic) > 500:
            flash('Полная тематика слишком длинная (макс. 500 символов)')
            return redirect(url_for('admin_edit_topic', topic_id=topic_id))

        result = tm.update_topic(topic_id, channel=channel, sr1=sr1, sr2=sr2,
                                  sr3=sr3, sr4=sr4, full_topic=full_topic)
        if result['success']:
            flash('Тематика успешно обновлена')
            log_topic_change(
                action='topic_updated',
                topic_id=topic_id,
                channel=channel,
                full_topic=full_topic or '',
                details={
                    'updated_by': _current_actor().get('username', ''),
                    'before': {'channel': topic_before.get('channel'), 'full_topic': topic_before.get('full_topic')},
                    'after': {'channel': channel, 'full_topic': full_topic}
                }
            )
        else:
            flash(f'Ошибка: {result.get("error")}')
    except Exception as e:
        print(f"[admin_update_topic] Ошибка: {e}")
        traceback.print_exc()
        flash('Произошла ошибка при обновлении')
    return redirect(url_for('admin_list_topics'))


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
    quarter = (request.args.get('quarter', '') or '').strip().upper()
    year_param = request.args.get('year', type=int)
    now = datetime.now()

    def _parse_iso_date(value: str):
        if not value:
            return None
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d')
        except (TypeError, ValueError):
            return None

    start_date = _parse_iso_date(date_from)
    end_date = _parse_iso_date(date_to)
    if start_date or end_date:
        if not start_date:
            start_date = end_date
        if not end_date:
            end_date = now
        start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = end_date.replace(hour=23, minute=59, second=59, microsecond=0)
        if start_dt > end_dt:
            start_dt, end_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0), start_dt.replace(hour=23, minute=59, second=59, microsecond=0)
        return start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S')

    period_quarter = period.upper() if period.upper() in {'Q1', 'Q2', 'Q3', 'Q4'} else ''
    quarter = quarter if quarter in {'Q1', 'Q2', 'Q3', 'Q4'} else period_quarter
    if quarter:
        year = year_param if year_param else now.year
        year = max(2000, min(year, 2100))
        quarter_index = int(quarter[1])
        start_month = (quarter_index - 1) * 3 + 1
        start_dt = datetime(year, start_month, 1)
        if quarter_index == 4:
            next_quarter = datetime(year + 1, 1, 1)
        else:
            next_quarter = datetime(year, start_month + 3, 1)
        end_dt = next_quarter - timedelta(seconds=1)
        return start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S')

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


def _problem_key_sql(alias: str = 'c') -> str:
    """Единый ключ проблемы для фильтрации и агрегации."""
    return f"""
        CASE
            WHEN NULLIF(TRIM(COALESCE({alias}.subproblem_id, '')), '') IS NOT NULL
                THEN 'sid:' || TRIM({alias}.subproblem_id)
            WHEN NULLIF(TRIM(COALESCE({alias}.problem_id, '')), '') IS NOT NULL
                THEN 'pid:' || TRIM({alias}.problem_id)
            ELSE 'txt:' || COALESCE(NULLIF(TRIM({alias}.problem), ''), 'Без привязки')
        END
    """


def _problem_label_sql(alias: str = 'c') -> str:
    return f"COALESCE(NULLIF(TRIM({alias}.problem), ''), 'Без привязки')"


RESOLUTION_PROBLEM_GROUPS = (
    ('pid:1', '1. Проблемы с почтой', '1'),
    ('pid:2', '2. Настройка Тонкий VISA', '2'),
    ('pid:3', '3. Не работает наушник - звук/микрофон', '3'),
    ('pid:4', '4. Настройка прокси Windows', '4'),
    ('pid:5', '5. Монитор не включается', '5'),
    ('pid:6', '6. Проблемы с CISCO', '6'),
    ('pid:7', '7. Другая проблема', '7'),
)


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _resolution_problem_text_map() -> dict[str, set[str]]:
    """Названия мануалов/подпроблем для классификации старых заявок без problem_id."""
    result = {problem_id: set() for _, _, problem_id in RESOLUTION_PROBLEM_GROUPS}
    try:
        manuals = load_manuals()
    except Exception:
        manuals = {}

    for _, _, problem_id in RESOLUTION_PROBLEM_GROUPS:
        manual = manuals.get(problem_id, {}) if isinstance(manuals, dict) else {}
        title = str(manual.get('title', '') or '').strip()
        if title:
            result[problem_id].add(title.lower())
        for subproblem in (manual.get('subproblems') or {}).values():
            subtitle = str((subproblem or {}).get('title', '') or '').strip()
            if subtitle:
                result[problem_id].add(subtitle.lower())
    return result


def _resolution_legacy_text_cases(alias: str, value_by_problem_id: dict[str, str]) -> str:
    problem_text = f"LOWER(TRIM(COALESCE({alias}.problem, '')))"
    cases = []
    for problem_id, values in _resolution_problem_text_map().items():
        if problem_id not in value_by_problem_id:
            continue
        literals = sorted({_sql_literal(value) for value in values if value})
        if literals:
            cases.append(
                f"WHEN {problem_text} IN ({', '.join(literals)}) THEN {value_by_problem_id[problem_id]}"
            )
    return "\n            ".join(cases)


def _resolution_group_case(alias: str = 'c') -> str:
    """Группировка SLA по верхним категориям с главного экрана проблем."""
    problem_id = f"NULLIF(TRIM(COALESCE({alias}.problem_id, '')), '')"
    subproblem_id = f"NULLIF(TRIM(COALESCE({alias}.subproblem_id, '')), '')"
    legacy_cases = _resolution_legacy_text_cases(
        alias,
        {problem_id_value: _sql_literal(f'pid:{problem_id_value}')
         for _, _, problem_id_value in RESOLUTION_PROBLEM_GROUPS}
    )
    return f"""
        CASE
            WHEN COALESCE({alias}.is_cisco, 0) = 1 THEN 'pid:6'
            WHEN {problem_id} IN ('1', '2', '3', '4', '5', '6', '7') THEN 'pid:' || {problem_id}
            WHEN SUBSTR({subproblem_id}, 1, 2) = '1.' THEN 'pid:1'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '2.' THEN 'pid:2'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '3.' THEN 'pid:3'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '4.' THEN 'pid:4'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '5.' THEN 'pid:5'
            {legacy_cases}
            ELSE 'pid:7'
        END
    """


def _resolution_label_case(alias: str = 'c') -> str:
    problem_id = f"NULLIF(TRIM(COALESCE({alias}.problem_id, '')), '')"
    subproblem_id = f"NULLIF(TRIM(COALESCE({alias}.subproblem_id, '')), '')"
    legacy_cases = _resolution_legacy_text_cases(
        alias,
        {problem_id_value: _sql_literal(label)
         for _, label, problem_id_value in RESOLUTION_PROBLEM_GROUPS}
    )
    return f"""
        CASE
            WHEN COALESCE({alias}.is_cisco, 0) = 1 THEN '6. Проблемы с CISCO'
            WHEN {problem_id} = '1' THEN '1. Проблемы с почтой'
            WHEN {problem_id} = '2' THEN '2. Настройка Тонкий VISA'
            WHEN {problem_id} = '3' THEN '3. Не работает наушник - звук/микрофон'
            WHEN {problem_id} = '4' THEN '4. Настройка прокси Windows'
            WHEN {problem_id} = '5' THEN '5. Монитор не включается'
            WHEN {problem_id} = '6' THEN '6. Проблемы с CISCO'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '1.' THEN '1. Проблемы с почтой'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '2.' THEN '2. Настройка Тонкий VISA'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '3.' THEN '3. Не работает наушник - звук/микрофон'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '4.' THEN '4. Настройка прокси Windows'
            WHEN SUBSTR({subproblem_id}, 1, 2) = '5.' THEN '5. Монитор не включается'
            {legacy_cases}
            ELSE '7. Другая проблема'
        END
    """


def _parse_problem_filters() -> list[str]:
    values = []
    seen = set()
    for raw in request.args.getlist('problem_key'):
        val = (raw or '').strip()
        if val and val not in seen:
            seen.add(val)
            values.append(val[:300])
    return values


RESOLUTION_PROBLEM_LABEL_BY_ID = {
    problem_id: label for _, label, problem_id in RESOLUTION_PROBLEM_GROUPS
}
RESOLUTION_PROBLEM_KEY_BY_ID = {
    problem_id: key for key, _, problem_id in RESOLUTION_PROBLEM_GROUPS
}


def _resolution_problem_group_from_values(problem: str = '', problem_id: str = '',
                                          subproblem_id: str = '', is_cisco: bool = False,
                                          legacy_text_map: dict[str, set[str]] | None = None) -> tuple[str, str]:
    """Python mirror of _resolution_group_case/_resolution_label_case for in-memory ticket states."""
    if is_cisco:
        return 'pid:6', RESOLUTION_PROBLEM_LABEL_BY_ID['6']

    pid = str(problem_id or '').strip()
    if pid in RESOLUTION_PROBLEM_LABEL_BY_ID:
        return RESOLUTION_PROBLEM_KEY_BY_ID[pid], RESOLUTION_PROBLEM_LABEL_BY_ID[pid]

    sid = str(subproblem_id or '').strip()
    for prefix in ('1.', '2.', '3.', '4.', '5.'):
        if sid.startswith(prefix):
            group_id = prefix[0]
            return RESOLUTION_PROBLEM_KEY_BY_ID[group_id], RESOLUTION_PROBLEM_LABEL_BY_ID[group_id]

    normalized_problem = str(problem or '').strip().lower()
    if normalized_problem:
        legacy_text_map = legacy_text_map if legacy_text_map is not None else _resolution_problem_text_map()
        for legacy_problem_id, values in legacy_text_map.items():
            if normalized_problem in values and legacy_problem_id in RESOLUTION_PROBLEM_LABEL_BY_ID:
                return (
                    RESOLUTION_PROBLEM_KEY_BY_ID[legacy_problem_id],
                    RESOLUTION_PROBLEM_LABEL_BY_ID[legacy_problem_id]
                )

    return 'pid:7', RESOLUTION_PROBLEM_LABEL_BY_ID['7']


def _attach_resolution_problem_fields(state: dict, legacy_text_map: dict[str, set[str]] | None = None) -> dict:
    key, label = _resolution_problem_group_from_values(
        state.get('problem', ''),
        state.get('problem_id', ''),
        state.get('subproblem_id', ''),
        bool(state.get('is_cisco')),
        legacy_text_map
    )
    state['problem_key'] = key
    state['problem_label'] = label
    return state


def _state_matches_problem_filters(state: dict, problem_keys: list[str] | None) -> bool:
    if not problem_keys:
        return True
    key = state.get('problem_key')
    if not key:
        key, _ = _resolution_problem_group_from_values(
            state.get('problem', ''),
            state.get('problem_id', ''),
            state.get('subproblem_id', ''),
            bool(state.get('is_cisco'))
        )
    return key in set(problem_keys)


def _problem_filter_clause(alias: str, problem_keys: list[str] | None, postgres: bool) -> tuple[str, list[Any]]:
    problem_keys = problem_keys or []
    if not problem_keys:
        return '', []
    key_sql = _resolution_group_case(alias)
    if postgres:
        return f" AND {key_sql} = ANY(%s)", [problem_keys]
    placeholders = ",".join("?" * len(problem_keys))
    return f" AND {key_sql} IN ({placeholders})", list(problem_keys)


def _load_ticket_event_type_counts(start_at: str, end_at: str,
                                   problem_keys: list[str] | None = None) -> dict[str, int]:
    filter_sql, filter_params = _problem_filter_clause('e', problem_keys, ANALYTICS_USE_POSTGRES)
    counts: dict[str, int] = {}

    if ANALYTICS_USE_POSTGRES:
        query = f"""
            SELECT e.event_type, COUNT(*)::int as c
            FROM ticket_events e
            WHERE e.created_at BETWEEN %s AND %s
              {filter_sql}
            GROUP BY e.event_type
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [start_at, end_at, *filter_params])
                for row in cur.fetchall():
                    counts[row.get('event_type')] = int(row.get('c') or 0)
        return counts

    query = f"""
        SELECT e.event_type, COUNT(*) as c
        FROM ticket_events e
        WHERE e.created_at BETWEEN ? AND ?
          {filter_sql}
        GROUP BY e.event_type
    """
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, [start_at, end_at, *filter_params])
        for row in cur.fetchall():
            counts[row['event_type']] = int(row['c'] or 0)
    return counts


def _count_cisco_tickets_created(start_at: str, end_at: str,
                                 problem_keys: list[str] | None = None) -> int:
    filter_sql, filter_params = _problem_filter_clause('e', problem_keys, ANALYTICS_USE_POSTGRES)
    if ANALYTICS_USE_POSTGRES:
        query = f"""
            SELECT COUNT(*)::int as c
            FROM ticket_events e
            WHERE e.created_at BETWEEN %s AND %s
              AND e.event_type = 'ticket_created'
              AND e.is_cisco = 1
              {filter_sql}
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [start_at, end_at, *filter_params])
                return int((cur.fetchone() or {}).get('c') or 0)

    query = f"""
        SELECT COUNT(*) as c
        FROM ticket_events e
        WHERE e.created_at BETWEEN ? AND ?
          AND e.event_type = 'ticket_created'
          AND e.is_cisco = 1
          {filter_sql}
    """
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, [start_at, end_at, *filter_params])
        return int(cur.fetchone()['c'] or 0)


def _ticket_summary_from_counts(counts_by_type: dict[str, int]) -> dict:
    base_events = {'manual_opened_video', 'manual_opened_text', 'ticket_created'}
    helped_events = {'video_helped', 'manual_helped', 'ticket_solved_by_helper'}
    not_helped_events = {
        'video_not_helped',
        'manual_not_helped',
        'ticket_created',
        'ticket_not_relevant',
        'ticket_resolved_by_staff'
    }
    helped_total = sum(int(counts_by_type.get(et, 0) or 0) for et in helped_events)
    return {
        'total': sum(int(counts_by_type.get(et, 0) or 0) for et in base_events),
        'helped': helped_total,
        'not_helped': sum(int(counts_by_type.get(et, 0) or 0) for et in not_helped_events),
        'self_solved': helped_total,
        'rejected': int(counts_by_type.get('ticket_not_relevant', 0) or 0)
    }


def _load_resolution_rows(start_at: str, end_at: str, problem_keys: list[str] | None = None) -> list[dict]:
    """Загружает решённые заявки за период по дате решения."""
    problem_keys = problem_keys or []
    key_sql = _resolution_group_case('c')
    label_sql = _resolution_label_case('c')

    if ANALYTICS_USE_POSTGRES:
        filter_sql = ""
        if problem_keys:
            filter_sql = f" AND {key_sql} = ANY(%s)"

        query = f"""
            WITH created AS (
                SELECT
                    ticket_number,
                    MIN(created_at) AS created_at,
                    MAX(problem) AS problem,
                    MAX(problem_id) AS problem_id,
                    MAX(subproblem_id) AS subproblem_id,
                    MAX(department) AS department,
                    MAX(user_name) AS user_name,
                    MAX(workplace) AS workplace,
                    MAX(is_cisco) AS is_cisco
                FROM ticket_events
                WHERE event_type = 'ticket_created'
                  AND ticket_number IS NOT NULL
                GROUP BY ticket_number
            ),
            resolved AS (
                SELECT
                    ticket_number,
                    MIN(created_at) AS resolved_at
                FROM ticket_events
                WHERE event_type = 'ticket_resolved_by_staff'
                  AND ticket_number IS NOT NULL
                  AND created_at BETWEEN %s AND %s
                GROUP BY ticket_number
            ),
            resolved_actor AS (
                SELECT DISTINCT ON (ticket_number)
                    ticket_number,
                    actor_name
                FROM ticket_events
                WHERE event_type = 'ticket_resolved_by_staff'
                  AND ticket_number IS NOT NULL
                  AND created_at BETWEEN %s AND %s
                ORDER BY ticket_number, created_at ASC, id ASC
            )
            SELECT
                c.ticket_number,
                c.created_at::text AS created_at,
                r.resolved_at::text AS resolved_at,
                COALESCE(ra.actor_name, '') AS resolved_by,
                {label_sql} AS problem_label,
                COALESCE(NULLIF(TRIM(c.problem_id), ''), '') AS problem_id,
                COALESCE(NULLIF(TRIM(c.subproblem_id), ''), '') AS subproblem_id,
                {key_sql} AS problem_key,
                COALESCE(NULLIF(TRIM(c.department), ''), 'Не указан') AS department,
                COALESCE(NULLIF(TRIM(c.user_name), ''), 'Неизвестно') AS user_name,
                COALESCE(NULLIF(TRIM(c.workplace), ''), '') AS workplace,
                COALESCE(c.is_cisco, 0) AS is_cisco,
                EXTRACT(EPOCH FROM (r.resolved_at - c.created_at)) / 60.0 AS resolution_minutes
            FROM resolved r
            JOIN created c ON c.ticket_number = r.ticket_number
            LEFT JOIN resolved_actor ra ON ra.ticket_number = r.ticket_number
            WHERE c.created_at IS NOT NULL
              AND r.resolved_at >= c.created_at
              {filter_sql}
            ORDER BY r.resolved_at DESC
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                if problem_keys:
                    cur.execute(query, [start_at, end_at, start_at, end_at, problem_keys])
                else:
                    cur.execute(query, [start_at, end_at, start_at, end_at])
                return [dict(row) for row in cur.fetchall()]

    filter_sql = ""
    params = [start_at, end_at]
    if problem_keys:
        placeholders = ",".join("?" * len(problem_keys))
        filter_sql = f" AND {key_sql} IN ({placeholders})"
        params.extend(problem_keys)

    query = f"""
        WITH created AS (
            SELECT
                ticket_number,
                MIN(created_at) AS created_at,
                MAX(problem) AS problem,
                MAX(problem_id) AS problem_id,
                MAX(subproblem_id) AS subproblem_id,
                MAX(department) AS department,
                MAX(user_name) AS user_name,
                MAX(workplace) AS workplace,
                MAX(is_cisco) AS is_cisco
            FROM ticket_events
            WHERE event_type = 'ticket_created'
              AND ticket_number IS NOT NULL
            GROUP BY ticket_number
        ),
        resolved AS (
            SELECT
                ticket_number,
                MIN(created_at) AS resolved_at,
                MAX(actor_name) AS resolved_by
            FROM ticket_events
            WHERE event_type = 'ticket_resolved_by_staff'
              AND ticket_number IS NOT NULL
              AND created_at BETWEEN ? AND ?
            GROUP BY ticket_number
        )
        SELECT
            c.ticket_number,
            c.created_at AS created_at,
            r.resolved_at AS resolved_at,
            COALESCE(r.resolved_by, '') AS resolved_by,
            {label_sql} AS problem_label,
            COALESCE(NULLIF(TRIM(c.problem_id), ''), '') AS problem_id,
            COALESCE(NULLIF(TRIM(c.subproblem_id), ''), '') AS subproblem_id,
            {key_sql} AS problem_key,
            COALESCE(NULLIF(TRIM(c.department), ''), 'Не указан') AS department,
            COALESCE(NULLIF(TRIM(c.user_name), ''), 'Неизвестно') AS user_name,
            COALESCE(NULLIF(TRIM(c.workplace), ''), '') AS workplace,
            COALESCE(c.is_cisco, 0) AS is_cisco,
            (julianday(r.resolved_at) - julianday(c.created_at)) * 1440.0 AS resolution_minutes
        FROM resolved r
        JOIN created c ON c.ticket_number = r.ticket_number
        WHERE c.created_at IS NOT NULL
          AND r.resolved_at >= c.created_at
          {filter_sql}
        ORDER BY r.resolved_at DESC
    """
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def _load_resolution_problem_options(start_at: str, end_at: str) -> list[dict]:
    """Список проблем для фильтра. Берём из созданных заявок, а не только из решённых."""
    key_sql = _resolution_group_case('e')
    label_sql = _resolution_label_case('e')

    def _normalize(rows: list[dict]) -> list[dict]:
        groups = {
            key: {
                'key': key,
                'label': label,
                'problem_id': problem_id,
                'subproblem_id': '',
                'count': 0
            }
            for key, label, problem_id in RESOLUTION_PROBLEM_GROUPS
        }
        for row in rows:
            key = row.get('problem_key') or 'pid:7'
            if key not in groups:
                key = 'pid:7'
            groups[key]['count'] += int(row.get('count') or 0)
            if not groups[key]['subproblem_id']:
                groups[key]['subproblem_id'] = row.get('subproblem_id') or ''
        return [
            {
                'key': key,
                'label': label,
                'problem_id': problem_id,
                'subproblem_id': row.get('subproblem_id') or '',
                'count': groups[key]['count']
            }
            for key, label, problem_id in RESOLUTION_PROBLEM_GROUPS
            for row in (groups[key],)
        ]

    if ANALYTICS_USE_POSTGRES:
        query = f"""
            SELECT
                {key_sql} AS problem_key,
                {label_sql} AS problem_label,
                COALESCE(NULLIF(TRIM(MAX(e.problem_id)), ''), '') AS problem_id,
                COALESCE(NULLIF(TRIM(MAX(e.subproblem_id)), ''), '') AS subproblem_id,
                COUNT(*)::int AS count
            FROM ticket_events e
            WHERE e.event_type = 'ticket_created'
              AND e.created_at BETWEEN %s AND %s
            GROUP BY problem_key, problem_label
            ORDER BY count DESC, problem_label ASC
        """
        fallback_query = f"""
            SELECT
                {key_sql} AS problem_key,
                {label_sql} AS problem_label,
                COALESCE(NULLIF(TRIM(MAX(e.problem_id)), ''), '') AS problem_id,
                COALESCE(NULLIF(TRIM(MAX(e.subproblem_id)), ''), '') AS subproblem_id,
                COUNT(*)::int AS count
            FROM ticket_events e
            WHERE e.event_type = 'ticket_created'
            GROUP BY problem_key, problem_label
            ORDER BY count DESC, problem_label ASC
        """
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [start_at, end_at])
                rows = [dict(row) for row in cur.fetchall()]
                if not rows:
                    cur.execute(fallback_query)
                    rows = [dict(row) for row in cur.fetchall()]
                return _normalize(rows)

    query = f"""
        SELECT
            {key_sql} AS problem_key,
            {label_sql} AS problem_label,
            COALESCE(NULLIF(TRIM(MAX(e.problem_id)), ''), '') AS problem_id,
            COALESCE(NULLIF(TRIM(MAX(e.subproblem_id)), ''), '') AS subproblem_id,
            COUNT(*) AS count
        FROM ticket_events e
        WHERE e.event_type = 'ticket_created'
          AND e.created_at BETWEEN ? AND ?
        GROUP BY problem_key, problem_label
        ORDER BY count DESC, problem_label ASC
    """
    fallback_query = f"""
        SELECT
            {key_sql} AS problem_key,
            {label_sql} AS problem_label,
            COALESCE(NULLIF(TRIM(MAX(e.problem_id)), ''), '') AS problem_id,
            COALESCE(NULLIF(TRIM(MAX(e.subproblem_id)), ''), '') AS subproblem_id,
            COUNT(*) AS count
        FROM ticket_events e
        WHERE e.event_type = 'ticket_created'
        GROUP BY problem_key, problem_label
        ORDER BY count DESC, problem_label ASC
    """
    with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, [start_at, end_at])
        rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            cur.execute(fallback_query)
            rows = [dict(row) for row in cur.fetchall()]
        return _normalize(rows)


def _format_resolution_minutes(total_minutes: float | int | None) -> str:
    if total_minutes is None:
        return '—'
    minutes = max(0, int(round(float(total_minutes))))
    if minutes < 60:
        return f"{minutes}м"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}ч {mins}м"
    days, hours = divmod(hours, 24)
    return f"{days}д {hours}ч"


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

    def _bump_group(m: dict, key: str, total_inc: int, helped_inc: int, not_helped_inc: int, key_name: str, event_type: str = ''):
        if key not in m:
            m[key] = {key_name: key, 'count': 0, 'helped': 0, 'not_helped': 0,
                      'video_helped': 0, 'manual_helped': 0,
                      'video_not_helped': 0, 'ticket_created': 0}
        m[key]['count'] += total_inc
        m[key]['helped'] += helped_inc
        m[key]['not_helped'] += not_helped_inc
        if event_type == 'video_helped':
            m[key]['video_helped'] += int(total_inc or helped_inc)
        elif event_type in ('manual_helped', 'ticket_solved_by_helper'):
            m[key]['manual_helped'] += int(total_inc or helped_inc)
        elif event_type == 'video_not_helped':
            m[key]['video_not_helped'] += int(total_inc or not_helped_inc)
        elif event_type == 'ticket_created':
            m[key]['ticket_created'] += int(total_inc or not_helped_inc)

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
                        _bump_group(problems_map, p, total_inc, helped_inc, not_helped_inc, 'problem', et)

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
                    _bump_group(problems_map, p, total_inc, helped_inc, not_helped_inc, 'problem', et)

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
@AdminAuth.manuals_required
def admin_stats_dashboard():
    """Основная статистика теперь находится в новом admin dashboard."""
    return redirect(url_for('admin_dashboard') + '#stats')


@app.route('/admin/stats-legacy')
@AdminAuth.manuals_required
def admin_stats_legacy():
    """Старая страница статистики, оставлена как fallback."""
    return render_template('admin_stats_dashboard.html')


@app.route('/api/stats/manual_feedback')
@AdminAuth.manuals_required
def api_stats_manual_feedback():
    try:
        start_at, end_at = _resolve_period_range()
        limit = max(1, min(request.args.get('limit', 50, type=int) or 50, 200))
        feedback_events = ['video_helped', 'video_not_helped', 'manual_helped', 'manual_not_helped']
        labels = {
            'video_helped': 'Видео помогло',
            'video_not_helped': 'Видео не помогло',
            'manual_helped': 'Текст помог',
            'manual_not_helped': 'Текст не помог'
        }
        tones = {
            'video_helped': 'status-closed',
            'video_not_helped': 'status-danger',
            'manual_helped': 'status-ready',
            'manual_not_helped': 'status-work'
        }

        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_type, COUNT(*)::int AS c
                        FROM ticket_events
                        WHERE event_type = ANY(%s)
                          AND created_at BETWEEN %s AND %s
                        GROUP BY event_type
                    """, (feedback_events, start_at, end_at))
                    counts = {row.get('event_type'): int(row.get('c') or 0) for row in cur.fetchall()}

                    cur.execute("""
                        SELECT
                            created_at::text AS created_at,
                            event_type,
                            ticket_number,
                            problem,
                            problem_id,
                            subproblem_id,
                            department,
                            user_name,
                            workplace,
                            details_json::text AS details_json
                        FROM ticket_events
                        WHERE event_type = ANY(%s)
                          AND created_at BETWEEN %s AND %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                    """, (feedback_events, start_at, end_at, limit))
                    rows = [dict(row) for row in cur.fetchall()]
        else:
            placeholders = ",".join("?" * len(feedback_events))
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(f"""
                    SELECT event_type, COUNT(*) AS c
                    FROM ticket_events
                    WHERE event_type IN ({placeholders})
                      AND created_at BETWEEN ? AND ?
                    GROUP BY event_type
                """, [*feedback_events, start_at, end_at])
                counts = {row['event_type']: int(row['c'] or 0) for row in cur.fetchall()}

                cur.execute(f"""
                    SELECT
                        created_at,
                        event_type,
                        ticket_number,
                        problem,
                        problem_id,
                        subproblem_id,
                        department,
                        user_name,
                        workplace,
                        details_json
                    FROM ticket_events
                    WHERE event_type IN ({placeholders})
                      AND created_at BETWEEN ? AND ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                """, [*feedback_events, start_at, end_at, limit])
                rows = [dict(row) for row in cur.fetchall()]

        data = []
        for row in rows:
            event_type = row.get('event_type') or ''
            details = _parse_event_details(row.get('details_json'))
            data.append({
                'created_at': row.get('created_at') or '',
                'event_type': event_type,
                'label': labels.get(event_type, event_type),
                'tone': tones.get(event_type, 'status-work'),
                'ticket_number': row.get('ticket_number'),
                'problem': row.get('problem') or 'Без описания',
                'problem_id': row.get('problem_id') or '',
                'subproblem_id': row.get('subproblem_id') or '',
                'department': row.get('department') or 'Не указан',
                'user_name': row.get('user_name') or 'Неизвестно',
                'workplace': row.get('workplace') or '',
                'next_step': details.get('next_step') or '',
                'ticket_delivery': details.get('ticket_delivery') or ''
            })

        summary = {event_type: int(counts.get(event_type, 0) or 0) for event_type in feedback_events}
        summary['helped'] = summary['video_helped'] + summary['manual_helped']
        summary['not_helped'] = summary['video_not_helped'] + summary['manual_not_helped']

        return jsonify({'success': True, 'data': data, 'summary': summary})
    except Exception as e:
        print(f"[api_stats_manual_feedback] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения обратной связи по мануалам'}), 500


@app.route('/api/admin/duty-settings')
@AdminAuth.manuals_required
def api_admin_duty_settings_get():
    try:
        return jsonify({'success': True, 'data': _get_duty_settings()})
    except Exception as e:
        print(f"[api_admin_duty_settings_get] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения настроек дежурного'}), 500


@app.route('/api/admin/duty-settings', methods=['POST'])
@AdminAuth.manuals_required
def api_admin_duty_settings_save():
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('current_duty_name') or '').strip()[:120]
        username = str(data.get('current_duty_username') or '').strip().lstrip('@')[:120]
        telegram_id = _env_int_from_value(data.get('current_duty_telegram_id'), 0)
        overload_limit = max(1, min(_env_int_from_value(data.get('overload_ticket_limit'), OVERLOAD_TICKET_LIMIT), 100))
        alert_thread_id = _env_int_from_value(data.get('overload_alert_thread_id'), 0)

        if not name and not username and not telegram_id:
            return jsonify({'success': False, 'error': 'Укажите имя, username или Telegram ID дежурного'}), 400

        values = {
            'current_duty_name': name,
            'current_duty_username': username,
            'current_duty_telegram_id': str(telegram_id or ''),
            'overload_ticket_limit': str(overload_limit),
            'overload_alert_thread_id': str(alert_thread_id or ''),
        }
        actor = _current_actor()
        _set_app_settings(values, updated_by=actor.get('username') or actor.get('name') or '')
        write_audit_log('POST /api/admin/duty-settings', 200, values)
        return jsonify({'success': True, 'data': _get_duty_settings()})
    except Exception as e:
        print(f"[api_admin_duty_settings_save] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка сохранения настроек дежурного'}), 500


@app.route('/api/admin/ticket-schedule')
@AdminAuth.manuals_required
def api_admin_ticket_schedule_get():
    try:
        return jsonify({'success': True, 'data': _get_ticket_schedule_settings()})
    except Exception as e:
        print(f"[api_admin_ticket_schedule_get] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения графика заявок'}), 500


@app.route('/api/admin/ticket-schedule', methods=['POST'])
@AdminAuth.manuals_required
def api_admin_ticket_schedule_save():
    try:
        data = request.get_json(silent=True) or {}
        workdays = _normalize_ticket_workdays(data.get('workdays'))
        work_start = _normalize_time_text(data.get('work_start'), '08:30')
        work_end = _normalize_time_text(data.get('work_end'), '17:30')
        if work_start >= work_end:
            return jsonify({'success': False, 'error': 'Время начала должно быть меньше времени окончания'}), 400

        lunch_enabled = _bool_from_value(data.get('lunch_enabled'), True)
        lunch_start = _normalize_time_text(data.get('lunch_start'), '12:00')
        lunch_end = _normalize_time_text(data.get('lunch_end'), '13:00')
        if lunch_enabled and lunch_start >= lunch_end:
            return jsonify({'success': False, 'error': 'Начало обеда должно быть меньше окончания обеда'}), 400

        holidays = _normalize_ticket_holidays(data.get('holidays') if isinstance(data.get('holidays'), list) else [])
        workday_overrides = _normalize_ticket_workday_overrides(
            data.get('workday_overrides') if isinstance(data.get('workday_overrides'), list) else []
        )

        values = {
            'ticket_workdays': ','.join(str(day) for day in workdays),
            'ticket_work_start': work_start,
            'ticket_work_end': work_end,
            'ticket_lunch_enabled': 'true' if lunch_enabled else 'false',
            'ticket_lunch_start': lunch_start,
            'ticket_lunch_end': lunch_end,
            'ticket_holidays_json': json.dumps(holidays, ensure_ascii=False),
            'ticket_workday_overrides_json': json.dumps(workday_overrides, ensure_ascii=False),
        }
        actor = _current_actor()
        _set_app_settings(values, updated_by=actor.get('username') or actor.get('name') or '')
        write_audit_log('POST /api/admin/ticket-schedule', 200, {
            'workdays': workdays,
            'work_start': work_start,
            'work_end': work_end,
            'lunch_enabled': lunch_enabled,
            'holidays_count': len(holidays),
            'workday_overrides_count': len(workday_overrides)
        })
        return jsonify({'success': True, 'data': _get_ticket_schedule_settings()})
    except Exception as e:
        print(f"[api_admin_ticket_schedule_save] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка сохранения графика заявок'}), 500


@app.route('/api/stats/online')
@AdminAuth.super_admin_required
def api_stats_online():
    """API: онлайн-пользователи и статистика за сегодня (только super_admin)"""
    try:
        online = get_online_stats()

        # Уникальные пользователи за сегодня из audit_logs
        today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
        today_end = datetime.now().strftime('%Y-%m-%d 23:59:59')
        today_users = []
        total_requests_today = 0

        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    # Уникальные пользователи за сегодня
                    cur.execute("""
                        SELECT actor_username, actor_name, actor_type, ip_address,
                               MIN(created_at) as first_seen,
                               MAX(created_at) as last_seen,
                               COUNT(*)::int as requests
                        FROM audit_logs
                        WHERE created_at BETWEEN %s AND %s
                          AND actor_type != 'guest'
                          AND actor_username != ''
                        GROUP BY actor_username, actor_name, actor_type, ip_address
                        ORDER BY last_seen DESC
                    """, (today_start, today_end))
                    today_users = [dict(row) for row in cur.fetchall()]

                    # Общее количество запросов за сегодня
                    cur.execute("""
                        SELECT COUNT(*)::int as total
                        FROM audit_logs
                        WHERE created_at BETWEEN %s AND %s
                    """, (today_start, today_end))
                    total_requests_today = int((cur.fetchone() or {}).get('total', 0))
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT actor_username, actor_name, actor_type, ip_address,
                           MIN(created_at) as first_seen,
                           MAX(created_at) as last_seen,
                           COUNT(*) as requests
                    FROM audit_logs
                    WHERE created_at BETWEEN ? AND ?
                      AND actor_type != 'guest'
                      AND actor_username != ''
                    GROUP BY actor_username, actor_name, actor_type, ip_address
                    ORDER BY last_seen DESC
                """, (today_start, today_end))
                today_users = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT COUNT(*) as total
                    FROM audit_logs
                    WHERE created_at BETWEEN ? AND ?
                """, (today_start, today_end))
                total_requests_today = int((cur.fetchone() or {})['total'] or 0)

        return jsonify({
            'success': True,
            'online': online,
            'today': {
                'unique_users': len(today_users),
                'total_requests': total_requests_today,
                'users': today_users
            }
        })
    except Exception as e:
        print(f"[API] Ошибка получения online статистики: {e}")
        return jsonify({'success': False, 'error': 'Ошибка загрузки данных'}), 500


@app.route('/api/stats/summary')
@AdminAuth.manuals_required
def api_stats_summary():
    """API для получения общей статистики"""
    try:
        start_at, end_at = _resolve_period_range()
        problem_keys = _parse_problem_filters()
        counts_by_type = _load_ticket_event_type_counts(start_at, end_at, problem_keys)
        stats = _ticket_summary_from_counts(counts_by_type)

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

        for event_type, count in counts_by_type.items():
            if event_type in breakdown:
                breakdown[event_type] = int(count or 0)

        # Совместимость со старым названием (если было)
        breakdown['manual_helped'] += int(counts_by_type.get('ticket_solved_by_helper', 0) or 0)
        breakdown['tickets_created_cisco'] = _count_cisco_tickets_created(start_at, end_at, problem_keys)

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
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
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


@app.route('/api/stats/users')
@AdminAuth.manuals_required
def api_stats_users():
    """Статистика использования Helper по специалистам (конечным пользователям)."""
    try:
        start_at, end_at = _resolve_period_range()
        limit = request.args.get('limit', 30, type=int)
        limit = max(1, min(limit, 200))

        def _key(username: str, name: str) -> str:
            u = (username or '').strip()
            n = (name or '').strip()
            return u or n or 'Неизвестно'

        users: dict[str, dict] = {}

        # 1) ticket_events (мануалы/тикеты/фидбек)
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            actor_username,
                            actor_name,
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END)::int as tickets_created,
                            SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END)::int as manuals_opened,
                            SUM(CASE WHEN event_type IN ('video_helped','manual_helped','ticket_solved_by_helper') THEN 1 ELSE 0 END)::int as helped,
                            SUM(CASE WHEN event_type IN ('video_not_helped','manual_not_helped') THEN 1 ELSE 0 END)::int as not_helped,
                            COUNT(*)::int as actions
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN (
                            'ticket_created',
                            'manual_opened_video','manual_opened_text',
                            'video_helped','video_not_helped',
                            'manual_helped','manual_not_helped',
                            'ticket_solved_by_helper'
                          )
                        GROUP BY actor_username, actor_name, department
                    """, (start_at, end_at))
                    ticket_rows = cur.fetchall()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        actor_username,
                        actor_name,
                        COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                        SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END) as tickets_created,
                        SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END) as manuals_opened,
                        SUM(CASE WHEN event_type IN ('video_helped','manual_helped','ticket_solved_by_helper') THEN 1 ELSE 0 END) as helped,
                        SUM(CASE WHEN event_type IN ('video_not_helped','manual_not_helped') THEN 1 ELSE 0 END) as not_helped,
                        COUNT(*) as actions
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ?
                      AND event_type IN (
                        'ticket_created',
                        'manual_opened_video','manual_opened_text',
                        'video_helped','video_not_helped',
                        'manual_helped','manual_not_helped',
                        'ticket_solved_by_helper'
                      )
                    GROUP BY actor_username, actor_name, department
                """, (start_at, end_at))
                ticket_rows = cur.fetchall()

        for row in ticket_rows:
            username = row['actor_username'] if isinstance(row, sqlite3.Row) else row.get('actor_username')
            name = row['actor_name'] if isinstance(row, sqlite3.Row) else row.get('actor_name')
            department = row['department'] if isinstance(row, sqlite3.Row) else row.get('department')
            k = _key(username, name)
            if k not in users:
                users[k] = {
                    'username': (username or '').strip(),
                    'name': (name or '').strip() or (username or '').strip() or k,
                    'department': department or 'Не указан',
                    'searches': 0,
                    'manuals_opened': 0,
                    'tickets_created': 0,
                    'helped': 0,
                    'not_helped': 0,
                    'total_actions': 0
                }
            u = users[k]
            u['department'] = u['department'] if u['department'] != 'Не указан' else (department or u['department'])
            u['tickets_created'] += int(row['tickets_created'] or 0)
            u['manuals_opened'] += int(row['manuals_opened'] or 0)
            u['helped'] += int(row['helped'] or 0)
            u['not_helped'] += int(row['not_helped'] or 0)
            u['total_actions'] += int(row['actions'] or 0)

        # 2) topic_search_events (поиск тематик)
        search_rows = []
        try:
            if ANALYTICS_USE_POSTGRES:
                with _pg_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT
                                actor_username,
                                actor_name,
                                COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                                COUNT(*)::int as searches
                            FROM topic_search_events
                            WHERE created_at BETWEEN %s AND %s
                            GROUP BY actor_username, actor_name, department
                        """, (start_at, end_at))
                        search_rows = cur.fetchall()
            else:
                with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT
                            actor_username,
                            actor_name,
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            COUNT(*) as searches
                        FROM topic_search_events
                        WHERE created_at BETWEEN ? AND ?
                        GROUP BY actor_username, actor_name, department
                    """, (start_at, end_at))
                    search_rows = cur.fetchall()
        except Exception as e:
            # Не валим весь API если таблица/колонки topic_search_events еще не промигрированы.
            print(f"[api_stats_users] Warning: topic_search_events query failed: {e}")

        for row in search_rows:
            username = row['actor_username'] if isinstance(row, sqlite3.Row) else row.get('actor_username')
            name = row['actor_name'] if isinstance(row, sqlite3.Row) else row.get('actor_name')
            department = row['department'] if isinstance(row, sqlite3.Row) else row.get('department')
            k = _key(username, name)
            if k not in users:
                users[k] = {
                    'username': (username or '').strip(),
                    'name': (name or '').strip() or (username or '').strip() or k,
                    'department': department or 'Не указан',
                    'searches': 0,
                    'manuals_opened': 0,
                    'tickets_created': 0,
                    'helped': 0,
                    'not_helped': 0,
                    'total_actions': 0
                }
            u = users[k]
            u['department'] = u['department'] if u['department'] != 'Не указан' else (department or u['department'])
            u['searches'] += int(row['searches'] or 0)
            u['total_actions'] += int(row['searches'] or 0)

        data = sorted(users.values(), key=lambda x: x.get('total_actions', 0), reverse=True)[:limit]
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        print(f"[api_stats_users] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статистики по специалистам'}), 500


@app.route('/api/stats/departments_usage')
@AdminAuth.manuals_required
def api_stats_departments_usage():
    """Статистика использования Helper по отделам (поисki + мануалы + заявки)."""
    try:
        start_at, end_at = _resolve_period_range()
        limit = request.args.get('limit', 50, type=int)
        limit = max(1, min(limit, 200))

        departments: dict[str, dict] = {}

        def _ensure(dep: str) -> dict:
            dep = (dep or '').strip() or 'Не указан'
            if dep not in departments:
                departments[dep] = {
                    'department': dep,
                    'searches': 0,
                    'manuals_opened': 0,
                    'tickets_created': 0,
                    'total_actions': 0
                }
            return departments[dep]

        # ticket_events
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END)::int as tickets_created,
                            SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END)::int as manuals_opened,
                            COUNT(*)::int as actions
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN ('ticket_created','manual_opened_video','manual_opened_text')
                        GROUP BY department
                    """, (start_at, end_at))
                    rows = cur.fetchall()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                        SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END) as tickets_created,
                        SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END) as manuals_opened,
                        COUNT(*) as actions
                    FROM ticket_events
                    WHERE created_at BETWEEN ? AND ?
                      AND event_type IN ('ticket_created','manual_opened_video','manual_opened_text')
                    GROUP BY department
                """, (start_at, end_at))
                rows = cur.fetchall()

        for row in rows:
            dep = row['department']
            d = _ensure(dep)
            d['tickets_created'] += int(row['tickets_created'] or 0)
            d['manuals_opened'] += int(row['manuals_opened'] or 0)
            d['total_actions'] += int(row['actions'] or 0)

        # topic_search_events
        rows = []
        try:
            if ANALYTICS_USE_POSTGRES:
                with _pg_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT
                                COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                                COUNT(*)::int as searches
                            FROM topic_search_events
                            WHERE created_at BETWEEN %s AND %s
                            GROUP BY department
                        """, (start_at, end_at))
                        rows = cur.fetchall()
            else:
                with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            COUNT(*) as searches
                        FROM topic_search_events
                        WHERE created_at BETWEEN ? AND ?
                        GROUP BY department
                    """, (start_at, end_at))
                    rows = cur.fetchall()
        except Exception as e:
            print(f"[api_stats_departments_usage] Warning: topic_search_events query failed: {e}")

        for row in rows:
            dep = row['department']
            d = _ensure(dep)
            d['searches'] += int(row['searches'] or 0)
            d['total_actions'] += int(row['searches'] or 0)

        data = sorted(departments.values(), key=lambda x: x.get('total_actions', 0), reverse=True)[:limit]
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        print(f"[api_stats_departments_usage] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения статистики по использованию отделов'}), 500


@app.route('/api/stats/staff')
@AdminAuth.manuals_required
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


@app.route('/api/stats/resolution_problems')
@AdminAuth.manuals_required
def api_stats_resolution_problems():
    """Список проблем для фильтра среднего времени решения."""
    try:
        start_at, end_at = _resolve_period_range()
        data = _load_resolution_problem_options(start_at, end_at)
        return jsonify({'success': True, 'data': data, 'total': len(data)})
    except Exception as e:
        print(f"[api_stats_resolution_problems] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения списка проблем'}), 500


@app.route('/api/stats/avg_resolution')
@AdminAuth.manuals_required
def api_stats_avg_resolution():
    """Среднее время решения по решённым заявкам."""
    try:
        start_at, end_at = _resolve_period_range()
        problem_keys = _parse_problem_filters()
        rows = _load_resolution_rows(start_at, end_at, problem_keys)

        total_count = len(rows)
        avg_minutes = 0.0
        if total_count:
            avg_minutes = sum(float(row.get('resolution_minutes') or 0) for row in rows) / total_count

        problems_map = {}
        for row in rows:
            key = row.get('problem_key') or 'txt:Без привязки'
            item = problems_map.setdefault(key, {
                'key': key,
                'label': row.get('problem_label') or 'Без привязки',
                'problem_id': row.get('problem_id') or '',
                'subproblem_id': row.get('subproblem_id') or '',
                'resolved_count': 0,
                'avg_minutes': 0.0
            })
            item['resolved_count'] += 1
            item['avg_minutes'] += float(row.get('resolution_minutes') or 0)

        items = []
        for item in problems_map.values():
            avg_item = item['avg_minutes'] / item['resolved_count'] if item['resolved_count'] else 0.0
            items.append({
                'key': item['key'],
                'label': item['label'],
                'problem_id': item['problem_id'],
                'subproblem_id': item['subproblem_id'],
                'resolved_count': item['resolved_count'],
                'avg_minutes': round(avg_item, 1),
                'avg_time': _format_resolution_minutes(avg_item)
            })

        items.sort(key=lambda x: (-int(x['resolved_count']), (x['label'] or '').lower()))

        return jsonify({
            'success': True,
            'data': {
                'resolved_count': total_count,
                'avg_minutes': round(avg_minutes, 1),
                'avg_time': _format_resolution_minutes(avg_minutes),
                'filters_applied': len(problem_keys),
                'items': items
            }
        })
    except Exception as e:
        print(f"[api_stats_avg_resolution] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения среднего времени решения'}), 500


@app.route('/api/stats/topics/summary')
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
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
@AdminAuth.manuals_required
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


@app.route('/api/stats/pending_tickets')
@AdminAuth.manuals_required
def api_stats_pending_tickets():
    """API: заявки которые отправлены но ещё не решены."""
    try:
        problem_keys = _parse_problem_filters()
        active_statuses = {'in_work', 'ready_for_feedback', 'mass_incident'}
        rows = [
            state for state in _load_ticket_states().values()
            if state.get('status') in active_statuses
            and _state_matches_problem_filters(state, problem_keys)
        ]
        now = datetime.now()
        for row in rows:
            created = row.get('created_at', '')
            if created:
                try:
                    if isinstance(created, str):
                        dt = datetime.strptime(created[:19], '%Y-%m-%d %H:%M:%S')
                    else:
                        dt = created
                    delta = now - dt
                    total_hours = delta.total_seconds() / 3600
                    if total_hours >= 24:
                        days = int(total_hours // 24)
                        hours = int(total_hours % 24)
                        row['waiting_time'] = f"{days}д {hours}ч"
                    else:
                        row['waiting_time'] = f"{int(total_hours)}ч {int((total_hours % 1) * 60)}м"
                    row['waiting_hours'] = round(total_hours, 1)
                    row['created_at'] = str(created)[:19]
                except Exception:
                    row['waiting_time'] = '—'
                    row['waiting_hours'] = 0
        rows = sorted(rows, key=lambda x: x.get('created_at') or '')

        return jsonify({'success': True, 'data': rows, 'total': len(rows)})
    except Exception as e:
        print(f"[api_stats_pending_tickets] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения нерешённых заявок'}), 500


@app.route('/api/stats/tickets_journal')
@AdminAuth.manuals_required
def api_stats_tickets_journal():
    """API: полная сводка по всем заявкам — создание, решение, время ожидания."""
    try:
        start_at, end_at = _resolve_period_range()
        problem_keys = _parse_problem_filters()
        limit = max(1, min(request.args.get('limit', 100, type=int), 500))
        now = datetime.now()
        result = []
        states = [
            state for state in _load_ticket_states().values()
            if start_at <= (state.get('created_at') or '') <= end_at
            and _state_matches_problem_filters(state, problem_keys)
        ]
        states = sorted(states, key=lambda x: x.get('created_at') or '', reverse=True)[:limit]

        for state in states:
            tn = state['ticket_number']
            resolved_at = state.get('ready_at') or state.get('closed_at') or ''
            entry = {
                'ticket_number': tn,
                'problem': state.get('problem', ''),
                'problem_key': state.get('problem_key', ''),
                'problem_label': state.get('problem_label', ''),
                'department': state.get('department', ''),
                'user_name': state.get('user_name', ''),
                'workplace': state.get('workplace', ''),
                'is_cisco': bool(state.get('is_cisco')),
                'created_at': str(state.get('created_at', ''))[:19],
                'status': state.get('status') or 'unknown',
                'status_label': state.get('status_label') or 'Неизвестно',
                'resolved_by': (
                    state.get('transferred_by')
                    if state.get('status') == 'transferred_up'
                    else state.get('resolved_by') or state.get('assigned_name') or None
                ),
                'resolved_at': resolved_at or None,
                'resolution_time': None,
                'resolution_minutes': None
            }

            if resolved_at:
                try:
                    created_str = str(state.get('created_at', ''))[:19]
                    resolved_str = str(resolved_at)[:19]
                    dt_created = datetime.strptime(created_str, '%Y-%m-%d %H:%M:%S')
                    dt_resolved = datetime.strptime(resolved_str, '%Y-%m-%d %H:%M:%S')
                    delta = dt_resolved - dt_created
                    total_min = delta.total_seconds() / 60
                    entry['resolution_minutes'] = round(total_min, 1)
                    if total_min < 60:
                        entry['resolution_time'] = f"{int(total_min)}м"
                    elif total_min < 1440:
                        entry['resolution_time'] = f"{int(total_min // 60)}ч {int(total_min % 60)}м"
                    else:
                        d = int(total_min // 1440)
                        h = int((total_min % 1440) // 60)
                        entry['resolution_time'] = f"{d}д {h}ч"
                except Exception:
                    entry['resolution_time'] = '—'
            else:
                try:
                    created_str = str(state.get('created_at', ''))[:19]
                    dt_created = datetime.strptime(created_str, '%Y-%m-%d %H:%M:%S')
                    delta = now - dt_created
                    total_min = delta.total_seconds() / 60
                    entry['resolution_minutes'] = round(total_min, 1)
                    if total_min < 60:
                        entry['resolution_time'] = f"{int(total_min)}м ожидает"
                    elif total_min < 1440:
                        entry['resolution_time'] = f"{int(total_min // 60)}ч {int(total_min % 60)}м ожидает"
                    else:
                        d = int(total_min // 1440)
                        h = int((total_min % 1440) // 60)
                        entry['resolution_time'] = f"{d}д {h}ч ожидает"
                except Exception:
                    entry['resolution_time'] = '—'

            result.append(entry)

        return jsonify({'success': True, 'data': result, 'total': len(result)})
    except Exception as e:
        print(f"[api_stats_tickets_journal] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка получения журнала заявок'}), 500


@app.route('/admin/stats/export')
@AdminAuth.manuals_required
def admin_stats_export():
    """Экспорт статистики обращений в Excel."""
    try:
        import tempfile
        import pandas as pd
        from flask import send_file

        start_at, end_at = _resolve_period_range()

        # --- Используем _load_ticket_dashboard_data для основных данных ---
        stats_data, timeline, top_problems, departments = _load_ticket_dashboard_data(start_at, end_at)

        # Breakdown по event_type
        breakdown = {}
        users = []
        dept_usage = []
        staff_list = []
        pending = []

        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_type, COUNT(*)::int as c
                        FROM ticket_events WHERE created_at BETWEEN %s AND %s
                        GROUP BY event_type
                    """, (start_at, end_at))
                    for r in cur.fetchall():
                        breakdown[r['event_type']] = r['c']

                    # Users — используем actor_username/actor_name как в api_stats_users
                    cur.execute("""
                        SELECT
                            COALESCE(NULLIF(TRIM(actor_name), ''), COALESCE(NULLIF(TRIM(actor_username), ''), 'Неизвестно')) as name,
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END)::int as manuals_opened,
                            SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END)::int as tickets_created,
                            COUNT(*)::int as total_actions
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN ('ticket_created','manual_opened_video','manual_opened_text',
                                             'video_helped','video_not_helped','manual_helped','manual_not_helped',
                                             'ticket_solved_by_helper')
                        GROUP BY name, department
                        ORDER BY total_actions DESC LIMIT 50
                    """, (start_at, end_at))
                    users = [dict(r) for r in cur.fetchall()]

                    # Departments usage
                    cur.execute("""
                        SELECT
                            COALESCE(NULLIF(TRIM(department), ''), 'Не указан') as department,
                            SUM(CASE WHEN event_type IN ('manual_opened_video','manual_opened_text') THEN 1 ELSE 0 END)::int as manuals_opened,
                            SUM(CASE WHEN event_type = 'ticket_created' THEN 1 ELSE 0 END)::int as tickets_created,
                            COUNT(*)::int as total_actions
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN ('ticket_created','manual_opened_video','manual_opened_text',
                                             'video_helped','video_not_helped','manual_helped','manual_not_helped',
                                             'ticket_solved_by_helper')
                        GROUP BY department
                        ORDER BY total_actions DESC
                    """, (start_at, end_at))
                    dept_usage = [dict(r) for r in cur.fetchall()]

                    # Staff — используем actor_name как в api_stats_staff
                    cur.execute("""
                        SELECT actor_name, event_type, COUNT(*)::int as cnt
                        FROM ticket_events
                        WHERE created_at BETWEEN %s AND %s
                          AND event_type IN ('ticket_resolved_by_staff', 'ticket_not_relevant')
                        GROUP BY actor_name, event_type
                    """, (start_at, end_at))
                    staff_map = {}
                    for row in cur.fetchall():
                        name = row['actor_name'] or 'Неизвестно'
                        if name not in staff_map:
                            staff_map[name] = {'staff': name, 'resolved': 0, 'not_relevant': 0, 'total': 0}
                        if row['event_type'] == 'ticket_resolved_by_staff':
                            staff_map[name]['resolved'] += row['cnt']
                        elif row['event_type'] == 'ticket_not_relevant':
                            staff_map[name]['not_relevant'] += row['cnt']
                        staff_map[name]['total'] += row['cnt']
                    staff_list = sorted(staff_map.values(), key=lambda x: x['total'], reverse=True)

                    # Pending tickets
                    cur.execute("""
                        SELECT ticket_number, problem, department, user_name, is_cisco,
                               MIN(created_at)::text as created_at
                        FROM ticket_events
                        WHERE event_type = 'ticket_created'
                          AND ticket_number IS NOT NULL
                          AND ticket_number NOT IN (
                            SELECT ticket_number FROM ticket_events
                            WHERE event_type IN ('ticket_resolved_by_staff', 'ticket_not_relevant')
                              AND ticket_number IS NOT NULL
                          )
                        GROUP BY ticket_number, problem, department, user_name, is_cisco
                        ORDER BY MIN(created_at) ASC
                    """)
                    pending = [dict(r) for r in cur.fetchall()]
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT event_type, COUNT(*) as c FROM ticket_events WHERE created_at BETWEEN ? AND ? GROUP BY event_type", (start_at, end_at))
                breakdown = {r['event_type']: r['c'] for r in cur.fetchall()}

        # --- Формируем Excel ---
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        tmp_path = tmp_file.name
        tmp_file.close()

        now = datetime.now()
        with pd.ExcelWriter(tmp_path, engine='openpyxl') as writer:
            # 1. Сводка
            summary_df = pd.DataFrame([{
                'Период': f"{start_at[:10]} — {end_at[:10]}",
                'Всего обращений': stats_data.get('total', 0),
                'Видео помогло': breakdown.get('video_helped', 0),
                'Видео не помогло': breakdown.get('video_not_helped', 0),
                'Мануал помог': breakdown.get('manual_helped', 0),
                'Мануал не помог': breakdown.get('manual_not_helped', 0),
                'Заявки создано': breakdown.get('ticket_created', 0),
                'Решено персоналом': breakdown.get('ticket_resolved_by_staff', 0),
                'Не актуально': breakdown.get('ticket_not_relevant', 0),
                'Нерешённых заявок': len(pending)
            }])
            summary_df.to_excel(writer, sheet_name='Сводка', index=False)

            # 2. По дням
            if timeline:
                tl_df = pd.DataFrame(timeline)
                tl_df.rename(columns={'date': 'Дата', 'total': 'Всего', 'helped': 'Помогло', 'not_helped': 'Не помогло'}, inplace=True)
                tl_df.to_excel(writer, sheet_name='По дням', index=False)

            # 3. Топ проблем
            if top_problems:
                tp_df = pd.DataFrame(top_problems)
                tp_rename = {
                    'problem': 'Проблема',
                    'count': 'Всего',
                    'helped': 'Помогло',
                    'not_helped': 'Не помогло',
                    'video_helped': 'Видео помогло',
                    'manual_helped': 'Текст помог',
                    'video_not_helped': 'Видео не помогло',
                    'ticket_created': 'Заявка создана'
                }
                tp_df.rename(columns=tp_rename, inplace=True)
                tp_df.to_excel(writer, sheet_name='Топ проблем', index=False)

            # 4. По отделам
            if departments:
                dep_df = pd.DataFrame(departments)
                dep_df.rename(columns={'department': 'Отдел', 'total': 'Всего', 'helped': 'Помогло', 'not_helped': 'Не помогло'}, inplace=True)
                dep_df.to_excel(writer, sheet_name='По отделам', index=False)

            # 5. По специалистам
            if users:
                u_df = pd.DataFrame(users)
                u_df.rename(columns={
                    'name': 'Специалист', 'department': 'Отдел',
                    'manuals_opened': 'Мануалы открыто', 'tickets_created': 'Заявки создано',
                    'total_actions': 'Всего действий'
                }, inplace=True)
                u_df.to_excel(writer, sheet_name='По специалистам', index=False)

            # 6. Использование по отделам
            if dept_usage:
                du_df = pd.DataFrame(dept_usage)
                du_df.rename(columns={
                    'department': 'Отдел', 'manuals_opened': 'Мануалы открыто',
                    'tickets_created': 'Заявки создано', 'total_actions': 'Всего действий'
                }, inplace=True)
                du_df.to_excel(writer, sheet_name='Использование по отделам', index=False)

            # 7. Техподдержка
            if staff_list:
                st_df = pd.DataFrame(staff_list)
                st_df.columns = ['Сотрудник', 'Решено', 'Не актуально', 'Всего'][:len(st_df.columns)]
                st_df.to_excel(writer, sheet_name='Техподдержка', index=False)

            # 8. Нерешённые заявки
            if pending:
                for p in pending:
                    created = p.get('created_at', '')
                    try:
                        if isinstance(created, str):
                            dt = datetime.strptime(created[:19], '%Y-%m-%d %H:%M:%S')
                        else:
                            dt = created
                        delta = now - dt
                        total_hours = delta.total_seconds() / 3600
                        if total_hours >= 24:
                            d = int(total_hours // 24)
                            h = int(total_hours % 24)
                            p['waiting_time'] = f"{d}д {h}ч"
                        else:
                            p['waiting_time'] = f"{int(total_hours)}ч"
                    except Exception:
                        p['waiting_time'] = '—'
                pend_df = pd.DataFrame([{
                    '№ заявки': p.get('ticket_number', ''),
                    'Проблема': p.get('problem', ''),
                    'Отдел': p.get('department', ''),
                    'Пользователь': p.get('user_name', ''),
                    'Cisco': 'Да' if p.get('is_cisco') else 'Нет',
                    'Дата создания': str(p.get('created_at', ''))[:19],
                    'Время ожидания': p.get('waiting_time', '—')
                } for p in pending])
                pend_df.to_excel(writer, sheet_name='Нерешённые заявки', index=False)

            # 9. Журнал заявок (полная сводка)
            journal_rows = []
            try:
                if ANALYTICS_USE_POSTGRES:
                    with _pg_connect() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute("""
                                SELECT ticket_number, problem, department, user_name, workplace,
                                       is_cisco, created_at::text as created_at
                                FROM ticket_events
                                WHERE event_type = 'ticket_created'
                                  AND ticket_number IS NOT NULL
                                  AND created_at BETWEEN %s AND %s
                                ORDER BY created_at DESC LIMIT 500
                            """, (start_at, end_at))
                            j_created = [dict(r) for r in cur2.fetchall()]
                            j_nums = [r['ticket_number'] for r in j_created]
                            j_resolved = {}
                            if j_nums:
                                cur2.execute("""
                                    SELECT ticket_number, event_type, actor_name,
                                           created_at::text as resolved_at
                                    FROM ticket_events
                                    WHERE event_type IN ('ticket_resolved_by_staff','ticket_not_relevant')
                                      AND ticket_number = ANY(%s)
                                    ORDER BY created_at ASC
                                """, (j_nums,))
                                for r in cur2.fetchall():
                                    tn = r['ticket_number']
                                    if tn not in j_resolved:
                                        j_resolved[tn] = dict(r)
                            for row in j_created:
                                tn = row['ticket_number']
                                res = j_resolved.get(tn)
                                status = 'Ожидает'
                                resolved_by = ''
                                resolved_at = ''
                                resolution_time = ''
                                if res:
                                    status = 'Решена' if res['event_type'] == 'ticket_resolved_by_staff' else 'Не актуальна'
                                    resolved_by = res.get('actor_name', '')
                                    resolved_at = str(res.get('resolved_at', ''))[:19]
                                    try:
                                        dt_c = datetime.strptime(str(row['created_at'])[:19], '%Y-%m-%d %H:%M:%S')
                                        dt_r = datetime.strptime(resolved_at, '%Y-%m-%d %H:%M:%S')
                                        mins = (dt_r - dt_c).total_seconds() / 60
                                        if mins < 60:
                                            resolution_time = f"{int(mins)}м"
                                        elif mins < 1440:
                                            resolution_time = f"{int(mins // 60)}ч {int(mins % 60)}м"
                                        else:
                                            resolution_time = f"{int(mins // 1440)}д {int((mins % 1440) // 60)}ч"
                                    except Exception:
                                        resolution_time = '—'
                                else:
                                    try:
                                        dt_c = datetime.strptime(str(row['created_at'])[:19], '%Y-%m-%d %H:%M:%S')
                                        mins = (now - dt_c).total_seconds() / 60
                                        if mins < 60:
                                            resolution_time = f"{int(mins)}м ожидает"
                                        elif mins < 1440:
                                            resolution_time = f"{int(mins // 60)}ч ожидает"
                                        else:
                                            resolution_time = f"{int(mins // 1440)}д ожидает"
                                    except Exception:
                                        resolution_time = '—'
                                journal_rows.append({
                                    '№ заявки': tn,
                                    'Проблема': row.get('problem', ''),
                                    'Отдел': row.get('department', ''),
                                    'Пользователь': row.get('user_name', ''),
                                    'Рабочее место': row.get('workplace', ''),
                                    'Cisco': 'Да' if row.get('is_cisco') else 'Нет',
                                    'Дата создания': str(row.get('created_at', ''))[:19],
                                    'Статус': status,
                                    'Кто помог': resolved_by,
                                    'Дата решения': resolved_at,
                                    'Время решения': resolution_time
                                })
            except Exception as je:
                print(f"[admin_stats_export] journal error: {je}")
            if journal_rows:
                j_df = pd.DataFrame(journal_rows)
                j_df.to_excel(writer, sheet_name='Журнал заявок', index=False)

        return send_file(
            tmp_path,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'stats_{now.strftime("%Y-%m-%d")}.xlsx'
        )
    except Exception as e:
        print(f"[admin_stats_export] Ошибка: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Ошибка экспорта'}), 500


# ============================================
# УПРАВЛЕНИЕ УЧЕТНЫМИ ЗАПИСЯМИ АДМИНИСТРАТОРОВ
# ============================================

@app.route('/admin/users')
@AdminAuth.super_admin_required
def admin_users():
    """Список всех администраторов (только для супер-админа)"""
    admins = admins_manager.load_admins()
    permission_choices = [
        (ROLE_SUPER_ADMIN, ROLE_NAMES.get(ROLE_SUPER_ADMIN, ROLE_SUPER_ADMIN)),
        (ROLE_ADMIN_MANUALS, ROLE_NAMES.get(ROLE_ADMIN_MANUALS, ROLE_ADMIN_MANUALS)),
        (ROLE_ADMIN_TOPICS, ROLE_NAMES.get(ROLE_ADMIN_TOPICS, ROLE_ADMIN_TOPICS)),
        (ROLE_ADMIN_SCENARIOS, ROLE_NAMES.get(ROLE_ADMIN_SCENARIOS, ROLE_ADMIN_SCENARIOS)),
        (ROLE_ADMIN_TRAINER, ROLE_NAMES.get(ROLE_ADMIN_TRAINER, ROLE_ADMIN_TRAINER)),
        (ROLE_TRAINER_VIEWER, ROLE_NAMES.get(ROLE_TRAINER_VIEWER, ROLE_TRAINER_VIEWER)),
    ]
    normalized_admins = []
    for admin in admins:
        item = dict(admin)
        item['permissions'] = admins_manager.normalize_permissions(item.get('permissions'), item.get('role', ''))
        item['auth_type'] = item.get('auth_type') or ('local' if item.get('password_hash') else 'ad')
        normalized_admins.append(item)
    return render_template(
        'admin_users.html',
        admins=normalized_admins,
        role_names=ROLE_NAMES,
        permission_choices=permission_choices
    )


@app.route('/admin/users/add', methods=['GET', 'POST'])
@AdminAuth.super_admin_required
def admin_add_user():
    """Добавление нового администратора"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        auth_type = request.form.get('auth_type', 'ad').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        permissions = request.form.getlist('permissions')

        # Валидация
        if not username:
            flash('Логин обязателен для заполнения')
            return redirect(url_for('admin_add_user'))

        if auth_type != 'ad':
            auth_type = 'local'
            if not password:
                flash('Для локального администратора пароль обязателен')
                return redirect(url_for('admin_add_user'))
            if password != password_confirm:
                flash('Пароли не совпадают')
                return redirect(url_for('admin_add_user'))

        # Создаем администратора
        created_by = session.get('admin_username', 'system')
        result = admins_manager.create_admin(
            username=username,
            password=password,
            created_by=created_by,
            auth_type=auth_type,
            permissions=permissions
        )

        if result['success']:
            flash(f'Администратор {username} успешно создан')
            return redirect(url_for('admin_users'))
        else:
            flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    permission_choices = [
        (ROLE_SUPER_ADMIN, ROLE_NAMES.get(ROLE_SUPER_ADMIN, ROLE_SUPER_ADMIN)),
        (ROLE_ADMIN_MANUALS, ROLE_NAMES.get(ROLE_ADMIN_MANUALS, ROLE_ADMIN_MANUALS)),
        (ROLE_ADMIN_TOPICS, ROLE_NAMES.get(ROLE_ADMIN_TOPICS, ROLE_ADMIN_TOPICS)),
        (ROLE_ADMIN_SCENARIOS, ROLE_NAMES.get(ROLE_ADMIN_SCENARIOS, ROLE_ADMIN_SCENARIOS)),
        (ROLE_ADMIN_TRAINER, ROLE_NAMES.get(ROLE_ADMIN_TRAINER, ROLE_ADMIN_TRAINER)),
        (ROLE_TRAINER_VIEWER, ROLE_NAMES.get(ROLE_TRAINER_VIEWER, ROLE_TRAINER_VIEWER)),
    ]
    return render_template('admin_add_user.html', permission_choices=permission_choices, role_names=ROLE_NAMES)


@app.route('/admin/users/<string:username>/change_password', methods=['GET', 'POST'])
@AdminAuth.super_admin_required
def admin_change_user_password(username):
    """Изменение пароля администратора"""
    admin = admins_manager.get_admin_by_username(username)
    if not admin:
        flash('Администратор не найден')
        return redirect(url_for('admin_users'))
    if admins_manager.is_ad_admin(admin):
        flash('Для AD-администратора пароль меняется в Active Directory')
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


@app.route('/admin/users/<string:username>/change_permissions', methods=['POST'])
@AdminAuth.super_admin_required
def admin_change_user_permissions(username):
    """Изменение набора прав администратора."""
    permissions = request.form.getlist('permissions')
    result = admins_manager.update_admin_permissions(username, permissions)

    if result['success']:
        flash(f'Права для {username} обновлены')
    else:
        flash(f'Ошибка: {result.get("error", "Неизвестная ошибка")}')

    return redirect(url_for('admin_users'))


@app.route('/admin/users/<string:username>/change_segments', methods=['POST'])
@AdminAuth.super_admin_required
def admin_change_user_segments(username):
    """Изменение доступных сегментов тренажёра для администратора"""
    segments = request.form.getlist('trainer_segments')
    result = admins_manager.update_trainer_segments(username, segments)

    if result['success']:
        flash(f'Доступ к сегментам для {username} обновлён')
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


# ═══════════════════════════════════════════════════════════════
# МОДУЛЬ СЦЕНАРИИ КОНСУЛЬТАЦИЙ КЦ
# ═══════════════════════════════════════════════════════════════

# ─── Пользовательская часть ────────────────────────────────────

def _can_access_consultation_scenarios(permissions: list[str] | None = None) -> bool:
    normalized = admins_manager.normalize_permissions(permissions or session.get('admin_permissions', []))
    return ROLE_SUPER_ADMIN in normalized or ROLE_ADMIN_SCENARIOS in normalized


def _require_scenarios_admin_access(api: bool = False):
    if session.get('admin_logged_in'):
        if _can_access_consultation_scenarios():
            return None
        if api:
            return jsonify({'success': False, 'error': 'Недостаточно прав для сценариев'}), 403
        flash('Сценарии консультаций пока доступны только администраторам сценариев.', 'error')
        return redirect(url_for('choose_help_type'))

    if not session.get('authenticated'):
        if api:
            return jsonify({'success': False, 'error': 'Не авторизован'}), 401
        return redirect(url_for('user_login'))

    if api:
        return jsonify({'success': False, 'error': 'Сценарии доступны только администраторам сценариев'}), 403
    flash('Сценарии консультаций пока в подготовке.', 'error')
    return redirect(url_for('choose_help_type'))


@app.route('/scenarios')
def scenarios_list():
    """Список активных сценариев для операторов"""
    access_response = _require_scenarios_admin_access()
    if access_response:
        return access_response
    user = session.get('user_info', {})
    category_id = request.args.get('category_id', type=int)
    search = request.args.get('q', '').strip()

    scenarios = scenario_mgr.get_scenarios(status='active', category_id=category_id, search=search)
    categories = scenario_mgr.get_categories()
    top_scenarios = scenario_mgr.get_top_scenarios(limit=10)
    total_scenarios = len(scenario_mgr.get_scenarios(status='active'))

    return render_template('scenarios_list.html',
                           scenarios=scenarios,
                           categories=categories,
                           top_scenarios=top_scenarios,
                           total_scenarios=total_scenarios,
                           selected_category=category_id,
                           search=search,
                           user=user)


@app.route('/scenarios/<int:scenario_id>')
def scenario_play(scenario_id):
    """Прохождение сценария оператором"""
    access_response = _require_scenarios_admin_access()
    if access_response:
        return access_response
    user = session.get('user_info', {})

    scenario = scenario_mgr.get_scenario(scenario_id)
    if not scenario or scenario['status'] != 'active':
        flash('Сценарий не найден или недоступен', 'error')
        return redirect(url_for('scenarios_list'))

    root_node = scenario_mgr.get_root_node(scenario_id)
    total_nodes = len(scenario_mgr.get_nodes(scenario_id))

    # Логируем просмотр
    scenario_mgr.log_view(scenario_id, user.get('username', ''))

    return render_template('scenario_play.html',
                           scenario=scenario,
                           root_node=root_node,
                           total_nodes=total_nodes,
                           user=user)


@app.route('/api/scenarios/<int:scenario_id>/node/<int:node_id>')
def scenario_get_node(scenario_id, node_id):
    """API: получить узел с вариантами выбора"""
    access_response = _require_scenarios_admin_access(api=True)
    if access_response:
        return access_response

    # Проверяем, что сценарий активен (не черновик и не архив)
    scenario = scenario_mgr.get_scenario(scenario_id)
    if not scenario or scenario.get('status') != 'active':
        return jsonify({'success': False, 'error': 'Сценарий недоступен'}), 404

    node = scenario_mgr.get_node(node_id)
    if not node or node['scenario_id'] != scenario_id:
        return jsonify({'success': False, 'error': 'Узел не найден'}), 404

    choices = scenario_mgr.get_node_choices(node_id)

    return jsonify({
        'success': True,
        'node': dict(node),
        'choices': [dict(c) for c in choices]
    })


# ─── Админская часть ───────────────────────────────────────────

def _require_scenario_admin():
    """Проверка прав на управление сценариями"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    perms = session.get('admin_permissions', [])
    if not _can_access_consultation_scenarios(perms):
        flash('Недостаточно прав', 'error')
        return redirect(url_for('admin_dashboard'))
    return None


@app.route('/admin/scenarios')
def admin_scenarios():
    """Список всех сценариев в админке"""
    err = _require_scenario_admin()
    if err:
        return err
    scenarios = scenario_mgr.get_all_scenarios_admin()
    categories = scenario_mgr.get_categories()
    return render_template('admin_scenarios.html',
                           scenarios=scenarios,
                           categories=categories)


@app.route('/admin/scenarios/create', methods=['GET', 'POST'])
def admin_scenario_create():
    """Создание нового сценария"""
    err = _require_scenario_admin()
    if err:
        return err

    categories = scenario_mgr.get_categories()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category_id = request.form.get('category_id', type=int)
        tags = request.form.get('tags', '').strip()
        username = session.get('admin_username', '')

        if not title:
            flash('Название обязательно', 'error')
            return render_template('admin_scenario_edit.html',
                                   scenario=None, nodes=[], edges=[],
                                   categories=categories, is_new=True)

        scenario_id = scenario_mgr.create_scenario(
            title=title, description=description,
            category_id=category_id, tags=tags, created_by=username
        )
        flash('Сценарий создан', 'success')
        return redirect(url_for('admin_scenario_edit', scenario_id=scenario_id))

    return render_template('admin_scenario_edit.html',
                           scenario=None, nodes=[], edges=[],
                           categories=categories, is_new=True)


@app.route('/admin/scenarios/<int:scenario_id>/edit', methods=['GET', 'POST'])
def admin_scenario_edit(scenario_id):
    """Редактирование сценария"""
    err = _require_scenario_admin()
    if err:
        return err

    scenario = scenario_mgr.get_scenario(scenario_id)
    if not scenario:
        flash('Сценарий не найден', 'error')
        return redirect(url_for('admin_scenarios'))

    categories = scenario_mgr.get_categories()
    nodes = scenario_mgr.get_nodes(scenario_id)
    edges_raw = []
    with scenario_mgr._connect() as conn:
        edges_raw = [dict(r) for r in conn.execute(
            "SELECT * FROM cs_edges WHERE scenario_id=?", (scenario_id,)
        ).fetchall()]

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category_id = request.form.get('category_id', type=int)
        tags = request.form.get('tags', '').strip()
        username = session.get('admin_username', '')
        scenario_mgr.update_scenario(scenario_id, title, description,
                                     category_id, tags, username)
        flash('Сохранено', 'success')
        return redirect(url_for('admin_scenario_edit', scenario_id=scenario_id))

    return render_template('admin_scenario_edit.html',
                           scenario=scenario, nodes=nodes,
                           edges=edges_raw, categories=categories, is_new=False)


@app.route('/admin/scenarios/<int:scenario_id>/publish', methods=['POST'])
def admin_scenario_publish(scenario_id):
    err = _require_scenario_admin()
    if err:
        return err
    username = session.get('admin_username', '')
    scenario_mgr.publish_scenario(scenario_id, username)
    flash('Сценарий опубликован', 'success')
    return redirect(url_for('admin_scenarios'))


@app.route('/admin/scenarios/<int:scenario_id>/archive', methods=['POST'])
def admin_scenario_archive(scenario_id):
    err = _require_scenario_admin()
    if err:
        return err
    username = session.get('admin_username', '')
    scenario_mgr.archive_scenario(scenario_id, username)
    flash('Сценарий архивирован', 'success')
    return redirect(url_for('admin_scenarios'))


@app.route('/admin/scenarios/<int:scenario_id>/unarchive', methods=['POST'])
def admin_scenario_unarchive(scenario_id):
    err = _require_scenario_admin()
    if err:
        return err
    username = session.get('admin_username', '')
    scenario_mgr.unarchive_scenario(scenario_id, username)
    flash('Сценарий восстановлен', 'success')
    return redirect(url_for('admin_scenarios'))


@app.route('/admin/scenarios/<int:scenario_id>/duplicate', methods=['POST'])
def admin_scenario_duplicate(scenario_id):
    err = _require_scenario_admin()
    if err:
        return err
    username = session.get('admin_username', '')
    new_id = scenario_mgr.duplicate_scenario(scenario_id, username)
    if new_id:
        flash('Сценарий скопирован', 'success')
        return redirect(url_for('admin_scenario_edit', scenario_id=new_id))
    flash('Ошибка копирования', 'error')
    return redirect(url_for('admin_scenarios'))


@app.route('/admin/scenarios/<int:scenario_id>/delete', methods=['POST'])
def admin_scenario_delete(scenario_id):
    err = _require_scenario_admin()
    if err:
        return err
    scenario_mgr.delete_scenario(scenario_id)
    flash('Сценарий удалён', 'success')
    return redirect(url_for('admin_scenarios'))


# ─── API: Обновление мета-данных сценария ──────────────────────

@app.route('/api/admin/scenarios/<int:scenario_id>', methods=['PUT'])
def api_scenario_meta_update(scenario_id):
    """Сохранить заголовок, описание, категорию, теги сценария"""
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    data = request.get_json() or {}
    admin = session.get('admin_username', '')
    scenario_mgr.update_scenario(
        scenario_id=scenario_id,
        title=data.get('title', ''),
        description=data.get('description', ''),
        category_id=data.get('category_id') or None,
        tags=data.get('tags', ''),
        updated_by=admin
    )
    return jsonify({'success': True})


# ─── API для редактора узлов ────────────────────────────────────

@app.route('/api/admin/scenarios/<int:scenario_id>/nodes', methods=['GET'])
def api_scenario_nodes(scenario_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    nodes = scenario_mgr.get_nodes(scenario_id)
    with scenario_mgr._connect() as conn:
        edges = [dict(r) for r in conn.execute(
            "SELECT * FROM cs_edges WHERE scenario_id=?", (scenario_id,)
        ).fetchall()]
    return jsonify({'success': True, 'nodes': nodes, 'edges': edges})


@app.route('/api/admin/scenarios/<int:scenario_id>/nodes', methods=['POST'])
def api_scenario_node_create(scenario_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    data = request.get_json() or {}
    node_id = scenario_mgr.create_node(
        scenario_id=scenario_id,
        node_type=data.get('node_type', 'question'),
        title=data.get('title', 'Новый узел'),
        content=data.get('content', ''),
        is_root=data.get('is_root', False)
    )
    return jsonify({'success': True, 'node_id': node_id})


@app.route('/api/admin/scenarios/<int:scenario_id>/nodes/<int:node_id>', methods=['PUT'])
@app.route('/api/admin/scenarios/nodes/<int:node_id>', methods=['PUT'], defaults={'scenario_id': None})
def api_scenario_node_update(scenario_id, node_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    # Проверяем принадлежность узла сценарию
    if scenario_id is not None:
        node = scenario_mgr.get_node(node_id)
        if not node or node['scenario_id'] != scenario_id:
            return jsonify({'success': False, 'error': 'Узел не принадлежит сценарию'}), 403
    data = request.get_json() or {}
    scenario_mgr.update_node(node_id, data)
    return jsonify({'success': True})


@app.route('/api/admin/scenarios/<int:scenario_id>/nodes/<int:node_id>', methods=['DELETE'])
@app.route('/api/admin/scenarios/nodes/<int:node_id>', methods=['DELETE'], defaults={'scenario_id': None})
def api_scenario_node_delete(scenario_id, node_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    # Проверяем принадлежность узла сценарию
    if scenario_id is not None:
        node = scenario_mgr.get_node(node_id)
        if not node or node['scenario_id'] != scenario_id:
            return jsonify({'success': False, 'error': 'Узел не принадлежит сценарию'}), 403
    scenario_mgr.delete_node(node_id)
    return jsonify({'success': True})


@app.route('/api/admin/scenarios/<int:scenario_id>/edges', methods=['POST'])
def api_scenario_edge_create(scenario_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    data = request.get_json() or {}
    edge_id = scenario_mgr.create_edge(
        scenario_id=scenario_id,
        from_node_id=data['from_node_id'],
        to_node_id=data['to_node_id'],
        label=data.get('label', ''),
        sort_order=data.get('sort_order', 0)
    )
    return jsonify({'success': True, 'edge_id': edge_id})


@app.route('/api/admin/scenarios/<int:scenario_id>/edges/<int:edge_id>', methods=['PUT'])
@app.route('/api/admin/scenarios/edges/<int:edge_id>', methods=['PUT'], defaults={'scenario_id': None})
def api_scenario_edge_update(scenario_id, edge_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    # Проверяем принадлежность ребра сценарию
    if scenario_id is not None:
        edge = scenario_mgr.get_edge(edge_id)
        if not edge or edge['scenario_id'] != scenario_id:
            return jsonify({'success': False, 'error': 'Ребро не принадлежит сценарию'}), 403
    data = request.get_json() or {}
    scenario_mgr.update_edge(edge_id, data.get('label', ''), data.get('sort_order', 0))
    return jsonify({'success': True})


@app.route('/api/admin/scenarios/<int:scenario_id>/edges/<int:edge_id>', methods=['DELETE'])
@app.route('/api/admin/scenarios/edges/<int:edge_id>', methods=['DELETE'], defaults={'scenario_id': None})
def api_scenario_edge_delete(scenario_id, edge_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    # Проверяем принадлежность ребра сценарию
    if scenario_id is not None:
        edge = scenario_mgr.get_edge(edge_id)
        if not edge or edge['scenario_id'] != scenario_id:
            return jsonify({'success': False, 'error': 'Ребро не принадлежит сценарию'}), 403
    scenario_mgr.delete_edge(edge_id)
    return jsonify({'success': True})


# ─── Layout (позиции узлов на canvas) ──────────────────────────

@app.route('/api/admin/scenarios/<int:scenario_id>/layout', methods=['PUT'])
def api_scenario_layout(scenario_id):
    """Сохранить позиции узлов на canvas"""
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False, 'error': 'Нет прав'}), 403
    data = request.get_json() or {}
    positions = data.get('positions', [])
    scenario_mgr.update_layout(positions)
    return jsonify({'success': True})


# ─── Категории ─────────────────────────────────────────────────

@app.route('/api/admin/scenarios/categories', methods=['POST'])
def api_scenario_category_create():
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    data = request.get_json() or {}
    cat_id = scenario_mgr.create_category(data.get('name', ''), data.get('icon', '📁'))
    return jsonify({'success': True, 'id': cat_id})


@app.route('/api/admin/scenarios/categories/<int:cat_id>', methods=['PUT'])
def api_scenario_category_update(cat_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    data = request.get_json() or {}
    scenario_mgr.update_category(cat_id, data.get('name', ''), data.get('icon', '📁'))
    return jsonify({'success': True})


@app.route('/api/admin/scenarios/categories/<int:cat_id>', methods=['DELETE'])
def api_scenario_category_delete(cat_id):
    err = _require_scenario_admin()
    if err:
        return jsonify({'success': False}), 403
    scenario_mgr.delete_category(cat_id)
    return jsonify({'success': True})


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
    BOT_POLLING_UP.labels(*_metric_base_labels()).set(1)
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        BOT_POLLING_UP.labels(*_metric_base_labels()).set(0)
        BOT_POLLING_ERRORS_TOTAL.labels(*_metric_base_labels(), type(e).__name__).inc()
        BOT_ERRORS_TOTAL.labels(*_metric_base_labels(), 'infinity_polling', type(e).__name__).inc()
        print(f"❌ Ошибка в bot polling: {e}")
        traceback.print_exc()
    finally:
        BOT_POLLING_UP.labels(*_metric_base_labels()).set(0)


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
