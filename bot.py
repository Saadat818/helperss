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

APP_TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('TEST_BOT_TOKEN') if APP_TEST_MODE and os.getenv('TEST_BOT_TOKEN') else os.getenv('BOT_TOKEN')
TECH_SUPPORT_CHAT_ID = int(os.getenv('TECH_SUPPORT_CHAT_ID', '0'))
NEW_TICKETS_THREAD_ID = int(os.getenv('NEW_TICKETS_THREAD_ID', '0'))
IN_PROGRESS_THREAD_ID = int(os.getenv('IN_PROGRESS_THREAD_ID', '0'))
SOLVED_TICKETS_THREAD_ID = int(os.getenv('SOLVED_TICKETS_THREAD_ID', '0'))

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
        bot.send_message(
            TECH_SUPPORT_CHAT_ID,
            f"❌ ЗАЯВКА НЕ АКТУАЛЬНА ❌\n\n"
            f"Заявка отмечена сотрудником {call.from_user.first_name} как не актуальная.\n"
            f"Никаких действий не требуется.",
            message_thread_id=NEW_TICKETS_THREAD_ID
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
