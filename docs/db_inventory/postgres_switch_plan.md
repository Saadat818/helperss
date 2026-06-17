# PostgreSQL backend switch plan

## Goal

Helper keeps SQLite as the default backend. PostgreSQL is opt-in and is enabled
only through environment variables. The application does not run migrations,
delete `topics.db`, or modify backups on startup.

## Current data safety points

- SQLite database: `topics.db`
- SQLite backups:
  - `backups/pre_pg_migration/topics_after_inventory.db`
  - `backups/pre_pg_migration/topics_after_inventory_sqlite.db`
- PostgreSQL dump after migration:
  - `docs/db_inventory/helper_after_full_migration.sql`
- SQLite code references:
  - `docs/db_inventory/sqlite_code_references.txt`
- Mapping:
  - `docs/db_inventory/sqlite_to_postgres_mapping.md`

## Default mode

If `HELPER_DB_BACKEND` is not set, Helper uses SQLite.

```bash
unset HELPER_DB_BACKEND
sudo systemctl restart web.helper.service
sudo systemctl restart bot.helper.service
```

The same behavior is used when the variable is explicitly set to SQLite:

```bash
export HELPER_DB_BACKEND=sqlite
sudo systemctl restart web.helper.service
sudo systemctl restart bot.helper.service
```

## Enable PostgreSQL

Set the backend and PostgreSQL connection variables in the service environment.
The password must come only from environment variables.

```bash
export HELPER_DB_BACKEND=postgres
export HELPER_PG_HOST=10.10.90.57
export HELPER_PG_PORT=5432
export HELPER_PG_DB=apo
export HELPER_PG_USER=r_koledin
export HELPER_PG_SCHEMA=helper
export HELPER_PG_PASSWORD='<set in service env>'

sudo systemctl restart web.helper.service
sudo systemctl restart bot.helper.service
```

Do not commit `.env` files, passwords, DSNs, dumps with secrets, or production
runtime values.

## Health check

Run this before switching traffic and after restart:

```bash
HELPER_DB_BACKEND=postgres \
HELPER_PG_HOST=10.10.90.57 \
HELPER_PG_PORT=5432 \
HELPER_PG_DB=apo \
HELPER_PG_USER=r_koledin \
HELPER_PG_SCHEMA=helper \
HELPER_PG_PASSWORD='<set in shell only>' \
python3 -c "from db_backend import health_check; print(health_check())"
```

The check runs:

- `SELECT 1`
- `SELECT COUNT(*) FROM helper.topics`
- `SELECT COUNT(*) FROM helper.trainer_results`
- `SELECT COUNT(*) FROM helper.cc_contacts`

## Smoke tests after restart

After enabling PostgreSQL and restarting services, run these checks from the
application server:

```bash
sudo systemctl status web.helper.service --no-pager
sudo systemctl status bot.helper.service --no-pager
curl -I http://127.0.0.1:5000/login
curl -I https://helper.mbank.kg/login
```

Open the main migrated sections in the browser and verify that pages load
without 5xx responses:

- `/contacts_kc`
- `/trainer/kc`
- `/admin/trainer`
- `/admin/scenarios`
- `/employee_board`

Check recent service logs without printing environment variables:

```bash
sudo journalctl -u web.helper.service -n 100 --no-pager
sudo journalctl -u bot.helper.service -n 100 --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

If any check fails, rollback to SQLite immediately and inspect application logs.

## Rollback to SQLite

Rollback does not require code changes or data deletion. Switch the environment
back to SQLite and restart services:

```bash
export HELPER_DB_BACKEND=sqlite
sudo systemctl restart web.helper.service
sudo systemctl restart bot.helper.service
```

If needed, unset PostgreSQL variables after rollback. Keep `topics.db` and the
backup files in `backups/pre_pg_migration`.

## Scope of this patch

The shared adapter is `db_backend.py`. It routes connections to SQLite or
PostgreSQL based on `HELPER_DB_BACKEND`.

Covered modules:

- `contacts_manager.py`
- `trainer_manager.py`
- `topics_manager.py`
- `scenario_manager.py`
- `myboard_manager.py`
- `employee_board_manager.py`
- `helper7.py`
- `bot.py`

`reset_stats.py` remains a SQLite maintenance script. It should be updated only
when a separate PostgreSQL reset workflow is approved.

## Operational notes

- PostgreSQL mode assumes schema `helper` and migrated tables already exist.
- Startup initialization and SQLite `CREATE/ALTER` paths are skipped for the
  migrated managers in PostgreSQL mode.
- No automatic data migration runs during application startup.
- SQLite remains available for fast rollback.
