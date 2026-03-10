#!/usr/bin/env python3
"""
Сброс статистики Helper перед новым деплоем.

Что делает:
  1. Очищает ticket_events в PostgreSQL (вся статистика обращений)
  2. Сбрасывает счётчик заявок в SQLite (topics.db) — нумерация с 1
  3. Устанавливает TICKET_NUMBER_START=1 в .env

Запуск:
  python3 reset_stats.py

⚠️  ВНИМАНИЕ: Все данные статистики будут удалены безвозвратно!
"""

import os
import sys
import sqlite3

# Загружаем .env
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
env_vars = {}
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                env_vars[key.strip()] = value.strip()

TICKET_COUNTER_DB = env_vars.get('TICKET_COUNTER_DB', 'topics.db')
PG_HOST = env_vars.get('POSTGRES_HOST', 'localhost')
PG_PORT = env_vars.get('POSTGRES_PORT', '5432')
PG_DB = env_vars.get('POSTGRES_DB', 'helper_analytics')
PG_USER = env_vars.get('POSTGRES_USER', 'ruslan')
PG_PASS = env_vars.get('POSTGRES_PASSWORD', '')


def confirm():
    print("=" * 55)
    print("  СБРОС СТАТИСТИКИ HELPER")
    print("=" * 55)
    print()
    print("  Будет выполнено:")
    print("  1. TRUNCATE ticket_events (PostgreSQL)")
    print(f"  2. Сброс ticket_sequence ({TICKET_COUNTER_DB})")
    print("  3. TICKET_NUMBER_START=1 в .env")
    print()
    print("  ⚠️  ВСЕ ДАННЫЕ СТАТИСТИКИ БУДУТ УДАЛЕНЫ!")
    print()
    ans = input("  Продолжить? (да/нет): ").strip().lower()
    if ans not in ('да', 'yes', 'y', 'д'):
        print("\n  Отменено.")
        sys.exit(0)
    print()


def reset_postgres():
    """Очистка ticket_events в PostgreSQL."""
    try:
        import psycopg2
    except ImportError:
        print("  [!] psycopg2 не установлен — пропуск PostgreSQL")
        return False

    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database=PG_DB, user=PG_USER, password=PG_PASS,
            connect_timeout=10
        )
        cur = conn.cursor()

        # Считаем записи перед удалением
        cur.execute("SELECT COUNT(*) FROM ticket_events")
        count = cur.fetchone()[0]

        cur.execute("TRUNCATE TABLE ticket_events RESTART IDENTITY")
        conn.commit()
        cur.close()
        conn.close()

        print(f"  ✓ PostgreSQL: удалено {count} записей из ticket_events")
        return True
    except Exception as e:
        print(f"  ✗ PostgreSQL ошибка: {e}")
        return False


def reset_sqlite_counter():
    """Сброс счётчика заявок в SQLite."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TICKET_COUNTER_DB)
    if not os.path.exists(db_path):
        print(f"  [!] {TICKET_COUNTER_DB} не найден — пропуск")
        return False

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.cursor()

        # Считаем записи
        cur.execute("SELECT COUNT(*) FROM ticket_sequence")
        count = cur.fetchone()[0]

        # Очищаем таблицу и сбрасываем автоинкремент
        cur.execute("DELETE FROM ticket_sequence")
        cur.execute("DELETE FROM sqlite_sequence WHERE name = 'ticket_sequence'")
        conn.commit()
        conn.close()

        print(f"  ✓ SQLite: сброшен ticket_sequence ({count} записей)")
        return True
    except Exception as e:
        print(f"  ✗ SQLite ошибка: {e}")
        return False


def update_env():
    """Установка TICKET_NUMBER_START=1 в .env."""
    if not os.path.exists(env_path):
        print(f"  [!] .env не найден")
        return False

    try:
        with open(env_path, 'r') as f:
            lines = f.readlines()

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith('TICKET_NUMBER_START='):
                old_val = line.strip().split('=', 1)[1]
                new_lines.append('TICKET_NUMBER_START=1\n')
                found = True
                print(f"  ✓ .env: TICKET_NUMBER_START={old_val} → 1")
            else:
                new_lines.append(line)

        if not found:
            new_lines.append('\nTICKET_NUMBER_START=1\n')
            print(f"  ✓ .env: добавлен TICKET_NUMBER_START=1")

        with open(env_path, 'w') as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        print(f"  ✗ .env ошибка: {e}")
        return False


if __name__ == '__main__':
    confirm()

    ok_pg = reset_postgres()
    ok_sq = reset_sqlite_counter()
    ok_env = update_env()

    print()
    print("=" * 55)
    if ok_pg and ok_sq and ok_env:
        print("  ✓ Сброс завершён. Первая заявка будет №1.")
    else:
        print("  ⚠ Сброс выполнен частично. Проверь ошибки выше.")
    print("=" * 55)
