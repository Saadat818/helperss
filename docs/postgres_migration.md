# Подготовка переноса Helper в PostgreSQL

Цель первого этапа - перенести `topics.db` в PostgreSQL как копию в схему `helper`, не переключая приложение сразу.

## 1. Создать схему

Подключение:

```bash
psql -h 10.10.90.57 -p 5432 -U r_koledin -d apo
```

Выполнить:

```bash
psql -h 10.10.90.57 -p 5432 -U r_koledin -d apo -f scripts/prepare_helper_schema.sql
```

Проверить:

```sql
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name = 'helper';
```

## 2. Снять безопасную копию SQLite с сервера

Если приложение работает, у SQLite могут быть файлы `topics.db-wal` и `topics.db-shm`. Для точной копии лучше либо остановить Helper на минуту, либо сделать SQLite backup.

Предпочтительный live-вариант без остановки приложения:

```bash
BACKUP_DIR=~/helper_db_backup_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"
sqlite3 topics.db ".backup '$BACKUP_DIR/topics.db'"
sqlite3 "$BACKUP_DIR/topics.db" "PRAGMA integrity_check;"
ls -lh "$BACKUP_DIR"
```

Минимальный вариант при остановленном приложении:

```bash
cp topics.db topics.db.backup
```

Если приложение не останавливаем, копировать надо весь набор:

```bash
cp topics.db topics.db-wal topics.db-shm /tmp/helper-db-backup/
```

Файлы загрузок не входят в SQLite. Их переносить отдельно:

```bash
static/uploads/
static/videos/
.env
admins.json
```

## 3. Пробный перенос в PostgreSQL

Скрипт создаст таблицы в `apo.helper`, перенесет строки и выведет сверку количества.

```bash
POSTGRES_HOST=10.10.90.57 \
POSTGRES_PORT=5432 \
POSTGRES_DB=apo \
POSTGRES_USER=r_koledin \
POSTGRES_SCHEMA=helper \
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite topics.db \
  --ask-password \
  --drop-existing
```

Для первого теста без данных:

```bash
python scripts/migrate_sqlite_to_postgres.py --sqlite topics.db --ask-password --schema-only --drop-existing
```

## 4. Сверка после переноса

В PostgreSQL:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'helper'
ORDER BY table_name;
```

Пример проверки строк:

```sql
SELECT COUNT(*) FROM helper.cc_contacts;
SELECT COUNT(*) FROM helper.cs_scenarios;
SELECT COUNT(*) FROM helper.trainer_scenarios;
SELECT COUNT(*) FROM helper.topics;
```

## 5. Результат пробного переноса 2026-06-01

Пробный перенос с backup-файла `/home/fudo/helper_db_backup_20260601_105535/topics.db` завершился успешно:

```text
Done. Schema: helper
```

Проверенные значения:

```text
helper.topics              36572
helper.trainer_answers       500
helper.trainer_results     31757
helper.trainer_scenarios      36
helper.trainer_visits      33603
helper.search_cache          685
helper.ticket_sequence       280
```

В схеме `helper` создано 22 таблицы. Приложение не переключалось, `.env` не менялся, сервис не перезапускался.

## 6. Что останется отдельной задачей

После копирования данных приложение все еще будет читать `topics.db`. Для полного перехода нужно отдельно перевести менеджеры с SQLite на PostgreSQL:

- `topics_manager.py`
- `trainer_manager.py`
- `scenario_manager.py`
- `contacts_manager.py`
- `myboard_manager.py`
- часть логов/аналитики в `helper7.py` и `bot.py`

До этого этапа PostgreSQL-схема `helper` используется как подготовленная копия и место для проверки миграции.

## 7. Финальное окно перехода

Финальный переход нужно делать отдельным maintenance-окном:

1. Включить maintenance-страницу/отбойник на `helper.mbank.kg`.
2. Убедиться, что пользователи больше не пишут в SQLite.
3. Сделать свежий SQLite backup через `.backup` и проверить `PRAGMA integrity_check`.
4. Повторить миграцию в `apo.helper` с `--drop-existing`.
5. Сверить ключевые counts и выборочные записи.
6. Переключить приложение на PostgreSQL только после готовности кода.
7. Перезапустить сервис.
8. Выполнить smoke-test: логин, темы, тренажер, сценарии, контакты, создание/редактирование.
9. При проблемах вернуть SQLite-конфиг/код, перезапустить сервис, снять maintenance.
