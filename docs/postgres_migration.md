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

## 5. Что останется отдельной задачей

После копирования данных приложение все еще будет читать `topics.db`. Для полного перехода нужно отдельно перевести менеджеры с SQLite на PostgreSQL:

- `topics_manager.py`
- `trainer_manager.py`
- `scenario_manager.py`
- `contacts_manager.py`
- `myboard_manager.py`
- часть логов/аналитики в `helper7.py` и `bot.py`

До этого этапа PostgreSQL-схема `helper` используется как подготовленная копия и место для проверки миграции.
