#!/usr/bin/env python3
"""
Telegram Bot для Helper - отдельный процесс
Обрабатывает callback кнопки и сообщения в группах
"""

import os
import traceback
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Загружаем переменные окружения
load_dotenv()

import re
import sqlite3
from datetime import datetime

APP_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('TEST_BOT_TOKEN') if APP_TEST_MODE and os.getenv('TEST_BOT_TOKEN') else os.getenv('BOT_TOKEN')
TECH_SUPPORT_CHAT_ID = int(os.getenv('TECH_SUPPORT_CHAT_ID', '0'))
NEW_TICKETS_THREAD_ID = int(os.getenv('NEW_TICKETS_THREAD_ID', '0'))
IN_PROGRESS_THREAD_ID = int(os.getenv('IN_PROGRESS_THREAD_ID', '0'))
SOLVED_TICKETS_THREAD_ID = int(os.getenv('SOLVED_TICKETS_THREAD_ID', '0'))

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
                     actor_name='', actor_username=''):
    """Записывает событие заявки в БД."""
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        payload = (now, event_type, ticket_number, problem[:500],
                   department[:200], user_name[:200], workplace[:100],
                   '', '', 0, actor_name[:200], actor_username[:200], 'staff')
        if ANALYTICS_USE_POSTGRES:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ticket_events (
                            created_at, event_type, ticket_number, problem,
                            department, user_name, workplace,
                            channel, topic_name, is_cisco,
                            actor_name, actor_username, actor_role
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, payload)
                conn.commit()
        else:
            with sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=10.0) as conn:
                conn.execute("""
                    INSERT INTO ticket_events (
                        created_at, event_type, ticket_number, problem,
                        department, user_name, workplace,
                        channel, topic_name, is_cisco,
                        actor_name, actor_username, actor_role
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, payload)
                conn.commit()
        print(f"[analytics] Записано: {event_type} ticket_number={ticket_number}")
    except Exception as e:
        print(f"[analytics] Ошибка логирования: {e}")

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

        # Логируем в БД
        ticket_number = extract_ticket_number(original_message)
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

        bot.answer_callback_query(call.id, "✅ Заявка перемещена в 'В работе'")
        print("✅ Callback 'Готово' обработан успешно")

    except Exception as e:
        print(f"❌ Ошибка в handle_ticket_done: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, "❌ Ошибка при обработке")


# ============================================================================
# Обработчик кнопки "Не актуально"
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "ticket_not_relevant")
def handle_ticket_not_relevant(call):
    """Обработка нажатия кнопки 'Не актуально'"""
    print(f"🔔 Получен callback 'Не актуально'! User: {call.from_user.id}, Chat: {call.message.chat.id}")
    try:
        # Убираем кнопки с оригинального сообщения
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )

        # Отправляем отдельное сообщение о том, что заявка не актуальна
        original_message = call.message.text or call.message.caption or ""
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"❌ ЗАЯВКА НЕ АКТУАЛЬНА ❌\n\n"
            f"Заявка отмечена сотрудником {call.from_user.first_name} как не актуальная.\n"
            f"Никаких действий не требуется.",
            message_thread_id=NEW_TICKETS_THREAD_ID
        )

        # Логируем в БД
        ticket_number = extract_ticket_number(original_message)
        parsed = parse_ticket_fields(original_message)
        resolver_name = call.from_user.first_name or call.from_user.username or str(call.from_user.id)
        log_ticket_event(
            event_type='ticket_not_relevant',
            ticket_number=ticket_number,
            problem=parsed.get('problem', original_message),
            department=parsed.get('department', ''),
            user_name=parsed.get('name', ''),
            workplace=parsed.get('workplace', ''),
            actor_name=resolver_name,
            actor_username=call.from_user.username or str(call.from_user.id)
        )

        bot.answer_callback_query(call.id, "✅ Заявка отмечена как неактуальная")
        print("✅ Callback 'Не актуально' обработан успешно")

    except Exception as e:
        print(f"❌ Ошибка в handle_ticket_not_relevant: {e}")
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

        # Проверяем есть ли reply (ответ на сообщение)
        if message.reply_to_message:
            original_message_id = message.reply_to_message.message_id
            text = message.text.lower() if message.text else ""

            # Проверяем ключевые слова для перемещения заявки
            if "в работе" in text or "в процессе" in text or "решена" in text or "готово" in text:
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
