# Аудит безопасности проекта Helper
**Дата:** 2026-02-18

## Критические (Critical) — 3

### 1. LDAP Injection (ad_auth.py:127)
- `search_filter = f"(sAMAccountName={clean_username})"` — username подставляется без экранирования
- Злоумышленник может ввести `*)(objectClass=*` для обхода фильтра
- **Исправление:** `ldap3.utils.conv.escape_filter_chars(clean_username)`

### 2. SSL CERT_NONE (ad_auth.py:63)
- `validate=ssl.CERT_NONE` — не проверяется сертификат AD сервера
- Возможна MiTM атака — перехват паролей
- **Исправление:** `validate=ssl.CERT_REQUIRED` + CA-сертификат AD сервера

### 3. TEST_MODE bypass (helper7.py: 3504-3518, 3588-3597)
- При `TEST_MODE=true` любой может войти с паролями `test`, `123`, `password`
- Админ может войти с паролями `admin`, `123`, `test` и получить SUPER_ADMIN
- **Исправление:** Запретить TEST_MODE на продакшене, добавить проверку окружения

---

## Высокая (High) — 1

### 4. admins.json в git
- Хеши паролей админов в истории коммитов
- Тестовый аккаунт `test_admin` от codex
- **Исправление:** `git rm --cached admins.json`, удалить тестовый аккаунт, сменить пароли

---

## Средние (Medium) — 7

### 5. DOM-XSS в trainer_play.html (3 места)
- `answer.feedback`, `answer.answer_text`, `answer.knowledge_link` вставляются через innerHTML без экранирования
- `client_info` label/value тоже без экранирования
- **Исправление:** экранировать через `escapeHtml()` или `textContent`

### 6. Open Redirect через request.referrer (helper7.py: 2663, 2685, 2699)
- `redirect(request.referrer)` — заголовок Referer контролируется клиентом
- **Исправление:** валидировать referrer через `urlparse`, проверять домен

### 7. Слабая парольная политика (admin_manager.py:418)
- Минимальная длина пароля — 6 символов, нет требований к сложности
- **Исправление:** минимум 12 символов, требовать заглавные/строчные/цифры/спецсимволы

### 8. Трассировки ошибок в API (helper7.py: 1824, 2845, 2937, 3468)
- `return jsonify({'error': str(e)})` — раскрывает пути, имена таблиц, параметры
- **Исправление:** возвращать общее сообщение, детали логировать серверно

### 9. Утечка bot token (bot.py:36)
- Выводит последние 10 символов токена в логи
- **Исправление:** заменить на `***REDACTED***`

### 10. api_admin_check_password без username (helper7.py:1811-1813)
- Принимает только пароль, username берёт из .env
- **Исправление:** требовать оба поля

---

## Низкие (Low) — 7

### 11. f-string в SQL UPDATE (topics_manager.py:481, trainer_manager.py:1121)
- Имена столбцов через f-string, но проверяются белым списком
- **Исправление:** словарь-маппинг для имён столбцов

### 12. CSRF exempt на GET /api/get_all_topics
- GET не меняет состояние, но потенциальная точка утечки
- **Исправление:** убрать csrf.exempt если возможно

### 13. SESSION_COOKIE_SECURE зависит от FLASK_ENV
- В development куки без шифрования
- **Исправление:** предупреждение при запуске если SECURE=False на не-localhost

### 14. Path Traversal в export (topics_manager.py:694)
- file_path без валидации, но используется через tempfile
- **Исправление:** валидация пути в функциях экспорта

### 15. redirect(request.url) при ошибках загрузки
- Менее уязвим чем referrer, но может содержать подставленный Host
- **Исправление:** использовать url_for()

### 16. Нет rate limiting на API статистики
- /api/stats/* без ограничений, тяжёлые SQL запросы
- **Исправление:** добавить @rate_limit()

### 17. check_same_thread=False для SQLite
- Race conditions при конкурентных записях
- **Исправление:** connection pooling или threading.Lock()

---

## Положительные аспекты
- CSRFProtect включен глобально
- Jinja2 auto-escaping работает
- Security headers настроены (X-Frame-Options, HSTS, CSP)
- Rate limiting на login маршрутах
- Параметризованные SQL-запросы в большинстве мест
- deep_escape() для XSS защиты
- Werkzeug scrypt для хеширования паролей
- MAX_CONTENT_LENGTH = 50MB
- Валидация длины username/password
