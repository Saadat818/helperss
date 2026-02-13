"""
Модуль для управления тренажером операторов
Интерактивные сценарии для обучения работе с клиентами
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime


class TrainerManager:
    """Управление тренажером с сценариями и прогрессом"""

    def __init__(self, db_path: str = "topics.db"):
        self.db_path = db_path
        self.conn = None
        self._init_db()

    def _init_db(self):
        """Инициализация базы данных"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0,
                                   isolation_level='IMMEDIATE')
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()

        # Уровни сложности
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_levels (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                description TEXT,
                icon TEXT,
                color TEXT,
                required_level TEXT,
                required_percent INTEGER DEFAULT 80,
                order_num INTEGER DEFAULT 0
            )
        """)

        # Категории сценариев
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_categories (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT,
                color TEXT
            )
        """)

        # Сценарии
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_scenarios (
                id INTEGER PRIMARY KEY,
                level_id INTEGER NOT NULL,
                category_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                estimated_time INTEGER DEFAULT 5,
                total_points INTEGER DEFAULT 100,
                is_active BOOLEAN DEFAULT 1,
                order_num INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                timer_seconds INTEGER DEFAULT 15,
                initial_loyalty INTEGER DEFAULT 100,
                client_info_json TEXT,
                FOREIGN KEY (level_id) REFERENCES trainer_levels(id),
                FOREIGN KEY (category_id) REFERENCES trainer_categories(id)
            )
        """)

        # Шаги сценария (диалог)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_steps (
                id INTEGER PRIMARY KEY,
                scenario_id INTEGER NOT NULL,
                step_num INTEGER NOT NULL,
                client_message TEXT NOT NULL,
                client_avatar TEXT,
                client_name TEXT DEFAULT 'Клиент',
                initial_mood TEXT DEFAULT 'neutral',
                FOREIGN KEY (scenario_id) REFERENCES trainer_scenarios(id) ON DELETE CASCADE
            )
        """)

        # Варианты ответов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_answers (
                id INTEGER PRIMARY KEY,
                step_id INTEGER NOT NULL,
                answer_text TEXT NOT NULL,
                is_correct BOOLEAN DEFAULT 0,
                is_partial BOOLEAN DEFAULT 0,
                points INTEGER DEFAULT 0,
                feedback TEXT,
                order_num INTEGER DEFAULT 0,
                mood_impact INTEGER DEFAULT 0,
                knowledge_link TEXT,
                FOREIGN KEY (step_id) REFERENCES trainer_steps(id) ON DELETE CASCADE
            )
        """)

        # Прогресс пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_user_progress (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                level_code TEXT NOT NULL,
                scenarios_completed INTEGER DEFAULT 0,
                scenarios_total INTEGER DEFAULT 0,
                is_unlocked BOOLEAN DEFAULT 0,
                unlocked_at TIMESTAMP,
                UNIQUE(user_id, level_code)
            )
        """)

        # Результаты прохождений
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_results (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                scenario_id INTEGER NOT NULL,
                score INTEGER DEFAULT 0,
                max_score INTEGER DEFAULT 100,
                percent INTEGER DEFAULT 0,
                grade TEXT,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                answers_json TEXT,
                final_loyalty INTEGER,
                is_game_over BOOLEAN DEFAULT 0,
                timeout_count INTEGER DEFAULT 0,
                selected_topic_id INTEGER,
                selected_topic_name TEXT,
                FOREIGN KEY (scenario_id) REFERENCES trainer_scenarios(id)
            )
        """)

        # Журнал аудита (логирование изменений)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_audit_log (
                id INTEGER PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                entity_name TEXT,
                changes_json TEXT,
                ip_address TEXT
            )
        """)

        # Теги для сценариев (карты, кредиты, депозиты и т.д.)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                color TEXT DEFAULT '#607D8B',
                icon TEXT DEFAULT '🏷️'
            )
        """)

        # Связь многие-ко-многим: сценарий <-> теги
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_scenario_tags (
                scenario_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (scenario_id, tag_id),
                FOREIGN KEY (scenario_id) REFERENCES trainer_scenarios(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES trainer_tags(id) ON DELETE CASCADE
            )
        """)

        # Индексы
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_scenarios_level ON trainer_scenarios(level_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_steps_scenario ON trainer_steps(scenario_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_answers_step ON trainer_answers(step_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_results_user ON trainer_results(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_progress_user ON trainer_user_progress(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trainer_audit_timestamp ON trainer_audit_log(timestamp)")

        self.conn.commit()

        # Миграция: добавляем новые колонки к существующим таблицам
        self._migrate_gamification_fields()

        # Миграция: добавляем уровень Hard
        self._migrate_hard_level()

        # Миграция: убеждаемся, что все базовые уровни существуют
        self._ensure_default_levels()

        # Инициализация начальных данных если таблицы пустые
        self._init_default_data()

        # Инициализация тегов
        self._init_default_tags()

    def _migrate_gamification_fields(self):
        """Миграция: добавление полей геймификации к существующим таблицам"""
        cursor = self.conn.cursor()

        # Проверяем и добавляем новые колонки в trainer_scenarios
        try:
            cursor.execute("SELECT timer_seconds FROM trainer_scenarios LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_scenarios ADD COLUMN timer_seconds INTEGER DEFAULT 15")

        try:
            cursor.execute("SELECT initial_loyalty FROM trainer_scenarios LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_scenarios ADD COLUMN initial_loyalty INTEGER DEFAULT 100")

        try:
            cursor.execute("SELECT client_info_json FROM trainer_scenarios LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_scenarios ADD COLUMN client_info_json TEXT")

        # Проверяем и добавляем новые колонки в trainer_steps
        try:
            cursor.execute("SELECT initial_mood FROM trainer_steps LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_steps ADD COLUMN initial_mood TEXT DEFAULT 'neutral'")

        # Проверяем и добавляем новые колонки в trainer_answers
        try:
            cursor.execute("SELECT mood_impact FROM trainer_answers LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_answers ADD COLUMN mood_impact INTEGER DEFAULT 0")

        try:
            cursor.execute("SELECT knowledge_link FROM trainer_answers LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_answers ADD COLUMN knowledge_link TEXT")

        # Проверяем и добавляем новые колонки в trainer_results
        try:
            cursor.execute("SELECT final_loyalty FROM trainer_results LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_results ADD COLUMN final_loyalty INTEGER")

        try:
            cursor.execute("SELECT is_game_over FROM trainer_results LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_results ADD COLUMN is_game_over BOOLEAN DEFAULT 0")

        try:
            cursor.execute("SELECT timeout_count FROM trainer_results LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_results ADD COLUMN timeout_count INTEGER DEFAULT 0")

        try:
            cursor.execute("SELECT selected_topic_id FROM trainer_results LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_results ADD COLUMN selected_topic_id INTEGER")

        try:
            cursor.execute("SELECT selected_topic_name FROM trainer_results LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE trainer_results ADD COLUMN selected_topic_name TEXT")

        self.conn.commit()

    def _migrate_hard_level(self):
        """Миграция: добавление уровня Hard и переименование Высокий → advanced"""
        cursor = self.conn.cursor()

        # Проверяем есть ли уже уровень с кодом 'hard' и order_num=4
        cursor.execute("SELECT id, code FROM trainer_levels WHERE order_num = 4")
        existing_hard = cursor.fetchone()

        if not existing_hard:
            # Переименовываем текущий "hard" в "advanced" (Высокий)
            cursor.execute("""
                UPDATE trainer_levels
                SET code = 'advanced'
                WHERE code = 'hard' AND order_num = 3
            """)

            # Добавляем новый уровень Hard
            cursor.execute("""
                INSERT INTO trainer_levels (name, code, description, icon, color, required_level, required_percent, order_num)
                VALUES ('Hard', 'hard', 'Экстремальные ситуации. Максимальная сложность.', '💀', '#9C27B0', 'advanced', 80, 4)
            """)

            # Обновляем ссылки на required_level
            cursor.execute("""
                UPDATE trainer_levels
                SET required_level = 'advanced'
                WHERE required_level = 'hard' AND code != 'hard'
            """)

            self.conn.commit()

    def _ensure_default_levels(self):
        """Миграция: добавляем отсутствующие базовые уровни (basic/medium/advanced/hard)."""
        cursor = self.conn.cursor()
        default_levels = [
            ("Базовый", "basic", "Основы работы с клиентами. Простые ситуации.", "🌱", "#4CAF50", None, 0, 1),
            ("Средний", "medium", "Сложные ситуации и конфликтные клиенты.", "⚡", "#FF9800", "basic", 80, 2),
            ("Высокий", "advanced", "Нестандартные случаи и VIP-клиенты.", "🔥", "#F44336", "medium", 80, 3),
            ("Hard", "hard", "Экстремальные ситуации. Максимальная сложность.", "💀", "#9C27B0", "advanced", 80, 4),
        ]

        cursor.execute("SELECT code, order_num FROM trainer_levels")
        existing = {row[0]: row[1] for row in cursor.fetchall()}

        for name, code, description, icon, color, required_level, required_percent, order_num in default_levels:
            if code not in existing:
                cursor.execute(
                    """
                    INSERT INTO trainer_levels
                    (name, code, description, icon, color, required_level, required_percent, order_num)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, code, description, icon, color, required_level, required_percent, order_num),
                )
            elif not existing[code]:
                cursor.execute(
                    "UPDATE trainer_levels SET order_num = ? WHERE code = ?",
                    (order_num, code),
                )

        self.conn.commit()

    def _init_default_data(self):
        """Инициализация начальных данных (уровни, категории)"""
        cursor = self.conn.cursor()

        # Проверяем есть ли уровни
        cursor.execute("SELECT COUNT(*) FROM trainer_levels")
        if cursor.fetchone()[0] == 0:
            # Создаем уровни
            levels = [
                ("Базовый", "basic", "Основы работы с клиентами. Простые ситуации.", "🌱", "#4CAF50", None, 0, 1),
                ("Средний", "medium", "Сложные ситуации и конфликтные клиенты.", "⚡", "#FF9800", "basic", 80, 2),
                ("Высокий", "advanced", "Нестандартные случаи и VIP-клиенты.", "🔥", "#F44336", "medium", 80, 3),
                ("Hard", "hard", "Экстремальные ситуации. Максимальная сложность.", "💀", "#9C27B0", "advanced", 80, 4)
            ]
            cursor.executemany("""
                INSERT INTO trainer_levels (name, code, description, icon, color, required_level, required_percent, order_num)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, levels)

        # Проверяем есть ли категории
        cursor.execute("SELECT COUNT(*) FROM trainer_categories")
        if cursor.fetchone()[0] == 0:
            # Создаем категории
            categories = [
                ("Технические вопросы", "🔧", "#2196F3"),
                ("Финансовые операции", "💰", "#4CAF50"),
                ("Жалобы и претензии", "😤", "#F44336"),
                ("Информационные запросы", "ℹ️", "#9C27B0")
            ]
            cursor.executemany("""
                INSERT INTO trainer_categories (name, icon, color)
                VALUES (?, ?, ?)
            """, categories)

        self.conn.commit()

        # Создаем тестовые сценарии если их нет
        self._init_test_scenarios()

    def _init_default_tags(self):
        """Инициализация начальных тегов"""
        cursor = self.conn.cursor()

        # Проверяем есть ли теги
        cursor.execute("SELECT COUNT(*) FROM trainer_tags")
        if cursor.fetchone()[0] > 0:
            return

        # Создаем начальные теги
        default_tags = [
            ("Карты", "#2196F3", "💳"),
            ("Кредиты", "#4CAF50", "💰"),
            ("Депозиты", "#FF9800", "🏦"),
            ("Переводы", "#9C27B0", "💸"),
            ("Мобильное приложение", "#00BCD4", "📱"),
            ("Корпоративные", "#795548", "🏢"),
        ]

        cursor.executemany("""
            INSERT INTO trainer_tags (name, color, icon) VALUES (?, ?, ?)
        """, default_tags)

        self.conn.commit()

    def _init_test_scenarios(self):
        """Инициализация тестовых сценариев для базового уровня"""
        cursor = self.conn.cursor()

        # Проверяем есть ли сценарии
        cursor.execute("SELECT COUNT(*) FROM trainer_scenarios")
        if cursor.fetchone()[0] > 0:
            return

        # Получаем ID базового уровня и категорий
        cursor.execute("SELECT id FROM trainer_levels WHERE code = 'basic'")
        row = cursor.fetchone()
        if not row:
            # Если базового уровня нет, не создаем тестовые сценарии
            return
        basic_level_id = row[0]

        cursor.execute("SELECT id, name FROM trainer_categories")
        categories = {row[1]: row[0] for row in cursor.fetchall()}
        if not categories:
            return

        # Тестовые сценарии
        test_scenarios = [
            {
                'title': 'Клиент не может войти в личный кабинет',
                'description': 'Клиент звонит с проблемой входа в интернет-банк',
                'category': 'Технические вопросы',
                'estimated_time': 5,
                'total_points': 100,
                'steps': [
                    {
                        'client_message': 'Здравствуйте! Я не могу войти в личный кабинет. Пишет "неверный пароль", но я точно ввожу правильно!',
                        'client_name': 'Анна',
                        'client_avatar': '👩',
                        'answers': [
                            {'text': 'Добрый день! Давайте разберемся. Скажите, вы пробовали восстановить пароль через форму "Забыли пароль"?', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Вы вежливо поприветствовали клиента и задали уточняющий вопрос.'},
                            {'text': 'Пароль точно неверный, система не ошибается.', 'is_correct': False, 'points': 0, 'feedback': 'Не стоит сразу отвергать слова клиента. Нужно разобраться в ситуации.'},
                            {'text': 'Сейчас передам вашу заявку специалистам.', 'is_partial': True, 'points': 10, 'feedback': 'Можно попробовать решить проблему самостоятельно, прежде чем передавать другим.'},
                        ]
                    },
                    {
                        'client_message': 'Нет, не пробовала. А как это сделать?',
                        'client_name': 'Анна',
                        'client_avatar': '👩',
                        'answers': [
                            {'text': 'На странице входа нажмите "Забыли пароль", введите номер телефона или email, и вам придет ссылка для сброса.', 'is_correct': True, 'points': 25, 'feedback': 'Правильно! Четкая и понятная инструкция.'},
                            {'text': 'Погуглите, там все написано.', 'is_correct': False, 'points': 0, 'feedback': 'Это грубый и непрофессиональный ответ.'},
                            {'text': 'Я могу сбросить пароль за вас, только продиктуйте мне данные карты.', 'is_correct': False, 'points': 0, 'feedback': 'Никогда не запрашивайте данные карты! Это нарушение безопасности.'},
                        ]
                    },
                    {
                        'client_message': 'Получилось! Спасибо большое!',
                        'client_name': 'Анна',
                        'client_avatar': '👩',
                        'answers': [
                            {'text': 'Рада помочь! Если возникнут вопросы - обращайтесь. Хорошего дня!', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Вежливое завершение разговора.'},
                            {'text': 'Ок.', 'is_correct': False, 'points': 5, 'feedback': 'Слишком сухой ответ. Важно оставить положительное впечатление.'},
                            {'text': 'Не забудьте оценить мою работу!', 'is_correct': False, 'points': 0, 'feedback': 'Не стоит навязывать оценку.'},
                        ]
                    },
                    {
                        'client_message': 'А можете еще подсказать, как подключить смс-оповещения?',
                        'client_name': 'Анна',
                        'client_avatar': '👩',
                        'answers': [
                            {'text': 'Конечно! В личном кабинете зайдите в "Настройки" -> "Уведомления" и включите SMS-оповещения.', 'is_correct': True, 'points': 25, 'feedback': 'Правильно! Вы дали четкую инструкцию.'},
                            {'text': 'Это платная услуга, вам точно нужно?', 'is_correct': False, 'points': 5, 'feedback': 'Не стоит отговаривать клиента от услуги.'},
                            {'text': 'Для этого нужно обратиться в отделение банка.', 'is_correct': False, 'points': 0, 'feedback': 'Эту услугу можно подключить самостоятельно в личном кабинете.'},
                        ]
                    }
                ]
            },
            {
                'title': 'Вопрос о курсе валют',
                'description': 'Клиент интересуется курсом обмена валюты',
                'category': 'Информационные запросы',
                'estimated_time': 3,
                'total_points': 75,
                'steps': [
                    {
                        'client_message': 'Добрый день! Какой у вас курс доллара на сегодня?',
                        'client_name': 'Сергей',
                        'client_avatar': '👨',
                        'answers': [
                            {'text': 'Добрый день! Текущий курс: покупка - 89.50 руб, продажа - 92.00 руб. Курс актуален на данный момент и может измениться.', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Полная и точная информация.'},
                            {'text': 'Посмотрите на сайте.', 'is_correct': False, 'points': 0, 'feedback': 'Клиент обратился к вам за информацией, нужно её предоставить.'},
                            {'text': 'Примерно 90 рублей.', 'is_partial': True, 'points': 10, 'feedback': 'Лучше давать точную информацию о курсе покупки и продажи.'},
                        ]
                    },
                    {
                        'client_message': 'А можно обменять 1000 долларов?',
                        'client_name': 'Сергей',
                        'client_avatar': '👨',
                        'answers': [
                            {'text': 'Да, конечно! Вы можете обменять валюту в любом отделении банка или через мобильное приложение. В приложении курс обычно выгоднее.', 'is_correct': True, 'points': 25, 'feedback': 'Правильно! Вы предложили варианты и дали полезную рекомендацию.'},
                            {'text': 'Только в отделении банка.', 'is_partial': True, 'points': 10, 'feedback': 'Не забывайте про возможность обмена через приложение.'},
                            {'text': 'Такую сумму без комиссии не обменяем.', 'is_correct': False, 'points': 0, 'feedback': 'Это неверная информация.'},
                        ]
                    },
                    {
                        'client_message': 'Спасибо за информацию!',
                        'client_name': 'Сергей',
                        'client_avatar': '👨',
                        'answers': [
                            {'text': 'Пожалуйста! Обращайтесь, если будут вопросы. Удачного дня!', 'is_correct': True, 'points': 25, 'feedback': 'Отличное завершение разговора!'},
                            {'text': 'Угу.', 'is_correct': False, 'points': 0, 'feedback': 'Слишком неформальный ответ.'},
                            {'text': 'Ждем вас в нашем банке!', 'is_partial': True, 'points': 15, 'feedback': 'Хорошо, но можно сделать ответ более естественным.'},
                        ]
                    }
                ]
            },
            {
                'title': 'Блокировка карты',
                'description': 'Клиент потерял карту и хочет её заблокировать',
                'category': 'Финансовые операции',
                'estimated_time': 4,
                'total_points': 100,
                'steps': [
                    {
                        'client_message': 'Срочно! Я потерял карту! Нужно заблокировать!',
                        'client_name': 'Михаил',
                        'client_avatar': '👨‍💼',
                        'answers': [
                            {'text': 'Понял вас, сейчас поможем! Для блокировки карты, пожалуйста, назовите последние 4 цифры номера карты и кодовое слово.', 'is_correct': True, 'points': 30, 'feedback': 'Правильно! Вы быстро отреагировали и запросили минимально необходимую информацию.'},
                            {'text': 'Не переживайте, такое бывает. Расскажите подробнее, где потеряли.', 'is_correct': False, 'points': 5, 'feedback': 'В срочной ситуации нужно сначала заблокировать карту, а потом уточнять детали.'},
                            {'text': 'Назовите полный номер карты, CVV-код и срок действия.', 'is_correct': False, 'points': 0, 'feedback': 'Никогда не запрашивайте CVV-код и полный номер карты по телефону!'},
                        ]
                    },
                    {
                        'client_message': 'Последние цифры 4532, кодовое слово "Россия".',
                        'client_name': 'Михаил',
                        'client_avatar': '👨‍💼',
                        'answers': [
                            {'text': 'Спасибо. Карта заблокирована. Вы можете заказать перевыпуск в отделении или через приложение. Средства на счете в безопасности.', 'is_correct': True, 'points': 35, 'feedback': 'Отлично! Вы заблокировали карту, успокоили клиента и дали информацию о дальнейших действиях.'},
                            {'text': 'Готово, заблокировано.', 'is_partial': True, 'points': 20, 'feedback': 'Стоит дать больше информации о следующих шагах.'},
                            {'text': 'Кодовое слово неверное, блокировать не могу.', 'is_correct': False, 'points': 0, 'feedback': 'При блокировке можно использовать альтернативные способы верификации.'},
                        ]
                    },
                    {
                        'client_message': 'А сколько стоит перевыпуск и как быстро сделают?',
                        'client_name': 'Михаил',
                        'client_avatar': '👨‍💼',
                        'answers': [
                            {'text': 'Перевыпуск в связи с утерей стоит 500 рублей, карта будет готова через 5-7 рабочих дней. Можете оформить срочный выпуск за 1000 рублей - за 2 дня.', 'is_correct': True, 'points': 35, 'feedback': 'Правильно! Вы дали полную информацию с вариантами.'},
                            {'text': 'Точно не знаю, уточните в отделении.', 'is_correct': False, 'points': 5, 'feedback': 'Оператор должен знать базовые тарифы и сроки.'},
                            {'text': 'Бесплатно, но долго.', 'is_correct': False, 'points': 0, 'feedback': 'Это неверная информация о стоимости.'},
                        ]
                    }
                ]
            },
            {
                'title': 'Жалоба на обслуживание',
                'description': 'Клиент недоволен работой отделения',
                'category': 'Жалобы и претензии',
                'estimated_time': 5,
                'total_points': 100,
                'steps': [
                    {
                        'client_message': 'Я возмущен! Простоял в очереди 40 минут, а операционист был груб!',
                        'client_name': 'Виктор',
                        'client_avatar': '😠',
                        'answers': [
                            {'text': 'Приношу извинения за неудобства! Пожалуйста, расскажите подробнее - в каком отделении это произошло и что именно случилось? Мы обязательно разберемся.', 'is_correct': True, 'points': 30, 'feedback': 'Отлично! Вы извинились, проявили участие и начали собирать информацию.'},
                            {'text': 'В час пик всегда очереди, это нормально.', 'is_correct': False, 'points': 0, 'feedback': 'Нельзя обесценивать жалобу клиента!'},
                            {'text': 'Оставьте жалобу на сайте.', 'is_correct': False, 'points': 5, 'feedback': 'Клиент уже обратился к вам, нужно принять его обращение.'},
                        ]
                    },
                    {
                        'client_message': 'Отделение на Центральной, 15. Девушка-операционист закатывала глаза и отвечала односложно.',
                        'client_name': 'Виктор',
                        'client_avatar': '😠',
                        'answers': [
                            {'text': 'Спасибо за детали. Я зафиксировал ваше обращение. С сотрудником будет проведена беседа. Могу ли я чем-то ещё вам помочь по текущему вопросу?', 'is_correct': True, 'points': 35, 'feedback': 'Правильно! Вы приняли информацию, сообщили о мерах и предложили дополнительную помощь.'},
                            {'text': 'Ладно, передам руководству.', 'is_partial': True, 'points': 15, 'feedback': 'Ответ немного сухой. Важно показать клиенту, что его услышали.'},
                            {'text': 'Может, вы преувеличиваете?', 'is_correct': False, 'points': 0, 'feedback': 'Никогда не подвергайте сомнению слова клиента!'},
                        ]
                    },
                    {
                        'client_message': 'Да, мне нужно было оформить справку, но я так и не смог из-за этого всего.',
                        'client_name': 'Виктор',
                        'client_avatar': '😠',
                        'answers': [
                            {'text': 'Понимаю вас. Давайте я помогу оформить справку прямо сейчас по телефону, или могу записать вас на удобное время в любое отделение - без очереди.', 'is_correct': True, 'points': 35, 'feedback': 'Отлично! Вы предложили конкретное решение проблемы.'},
                            {'text': 'Приходите завтра, должно быть меньше народу.', 'is_partial': True, 'points': 10, 'feedback': 'Можно предложить более удобное решение.'},
                            {'text': 'К сожалению, справки только в отделении.', 'is_correct': False, 'points': 0, 'feedback': 'Многие справки можно оформить дистанционно или записаться без очереди.'},
                        ]
                    }
                ]
            },
            {
                'title': 'Подозрительная операция',
                'description': 'Клиент сообщает о незнакомой транзакции',
                'category': 'Финансовые операции',
                'estimated_time': 5,
                'total_points': 100,
                'steps': [
                    {
                        'client_message': 'У меня списали 5000 рублей, я ничего не покупал! Это мошенники?',
                        'client_name': 'Елена',
                        'client_avatar': '👩‍🦰',
                        'answers': [
                            {'text': 'Понимаю ваше беспокойство. Давайте проверим операцию. Назовите, пожалуйста, дату списания и последние 4 цифры карты.', 'is_correct': True, 'points': 25, 'feedback': 'Правильно! Вы проявили понимание и начали проверку.'},
                            {'text': 'Скорее всего, вы просто забыли о покупке.', 'is_correct': False, 'points': 0, 'feedback': 'Нельзя отвергать опасения клиента без проверки!'},
                            {'text': 'Срочно блокируйте карту в приложении!', 'is_correct': False, 'points': 10, 'feedback': 'Сначала нужно разобраться в ситуации.'},
                        ]
                    },
                    {
                        'client_message': 'Вчера, карта 7890. Написано "OZON" - я там ничего не заказывала!',
                        'client_name': 'Елена',
                        'client_avatar': '👩‍🦰',
                        'answers': [
                            {'text': 'Вижу операцию. Это может быть подписка или автоплатеж. Проверьте, не оформляли ли вы подписку OZON Premium? Также карта могла быть привязана к чужому аккаунту.', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Вы нашли операцию и предложили возможные объяснения.'},
                            {'text': 'Это точно мошенники, блокируем карту.', 'is_correct': False, 'points': 5, 'feedback': 'Не стоит делать поспешных выводов.'},
                            {'text': 'Разбирайтесь с OZON, мы тут ни при чем.', 'is_correct': False, 'points': 0, 'feedback': 'Банк должен помочь клиенту разобраться с операцией.'},
                        ]
                    },
                    {
                        'client_message': 'Точно! У меня была пробная подписка, я забыла отменить. Извините за беспокойство.',
                        'client_name': 'Елена',
                        'client_avatar': '👩‍🦰',
                        'answers': [
                            {'text': 'Ничего страшного, лучше проверить! Рекомендую настроить уведомления о списаниях - так вы всегда будете в курсе операций.', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Вы успокоили клиента и дали полезный совет.'},
                            {'text': 'Да, в следующий раз сначала проверяйте.', 'is_correct': False, 'points': 0, 'feedback': 'Не нужно упрекать клиента.'},
                            {'text': 'Хорошо, что разобрались.', 'is_partial': True, 'points': 15, 'feedback': 'Можно дать дополнительную полезную рекомендацию.'},
                        ]
                    },
                    {
                        'client_message': 'Как отменить эту подписку?',
                        'client_name': 'Елена',
                        'client_avatar': '👩‍🦰',
                        'answers': [
                            {'text': 'Зайдите в приложение OZON, раздел "Мой профиль" -> "Подписки" и отмените автопродление. Также можете отвязать карту от сервиса.', 'is_correct': True, 'points': 25, 'feedback': 'Отлично! Четкая инструкция по решению проблемы.'},
                            {'text': 'Это не к нам вопрос, звоните в OZON.', 'is_correct': False, 'points': 5, 'feedback': 'Если знаете ответ - помогите клиенту.'},
                            {'text': 'Заблокируйте карту, и списания прекратятся.', 'is_correct': False, 'points': 0, 'feedback': 'Это не решение проблемы, а создание новых неудобств.'},
                        ]
                    }
                ]
            }
        ]

        # Создаем сценарии с шагами и ответами
        for order_num, scenario_data in enumerate(test_scenarios, 1):
            category_id = categories.get(scenario_data['category'])

            cursor.execute("""
                INSERT INTO trainer_scenarios (level_id, category_id, title, description, estimated_time, total_points, order_num)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (basic_level_id, category_id, scenario_data['title'], scenario_data['description'],
                  scenario_data['estimated_time'], scenario_data['total_points'], order_num))
            scenario_id = cursor.lastrowid

            for step_num, step_data in enumerate(scenario_data['steps'], 1):
                cursor.execute("""
                    INSERT INTO trainer_steps (scenario_id, step_num, client_message, client_avatar, client_name)
                    VALUES (?, ?, ?, ?, ?)
                """, (scenario_id, step_num, step_data['client_message'],
                      step_data['client_avatar'], step_data['client_name']))
                step_id = cursor.lastrowid

                for answer_order, answer_data in enumerate(step_data['answers'], 1):
                    cursor.execute("""
                        INSERT INTO trainer_answers (step_id, answer_text, is_correct, is_partial, points, feedback, order_num)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (step_id, answer_data['text'], answer_data.get('is_correct', 0),
                          answer_data.get('is_partial', 0), answer_data['points'],
                          answer_data['feedback'], answer_order))

        self.conn.commit()
        # Тестовые сценарии тренажера созданы

    # ==================== УРОВНИ ====================

    def get_all_levels(self) -> List[Dict]:
        """Получить все уровни"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trainer_levels ORDER BY order_num
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_level_by_code(self, code: str) -> Optional[Dict]:
        """Получить уровень по коду"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_levels WHERE code = ?", (code,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_level_by_id(self, level_id: int) -> Optional[Dict]:
        """Получить уровень по ID"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_levels WHERE id = ?", (level_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def check_level_unlocked(self, user_id: str, level_code: str) -> bool:
        """Проверить разблокирован ли уровень для пользователя"""
        level = self.get_level_by_code(level_code)
        if not level:
            return False

        # Базовый уровень всегда открыт
        if not level['required_level']:
            return True

        # Проверяем прогресс по предыдущему уровню
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT is_unlocked FROM trainer_user_progress
            WHERE user_id = ? AND level_code = ?
        """, (user_id, level_code))
        row = cursor.fetchone()

        if row and row['is_unlocked']:
            return True

        # Проверяем выполнение условий разблокировки
        return self._check_unlock_condition(user_id, level)

    def _check_unlock_condition(self, user_id: str, level: Dict) -> bool:
        """Проверить условия разблокировки уровня"""
        required_level = level.get('required_level')
        required_percent = level.get('required_percent', 80)

        if not required_level:
            return True

        # Получаем средний процент прохождения предыдущего уровня
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT AVG(r.percent) as avg_percent, COUNT(DISTINCT r.scenario_id) as completed
            FROM trainer_results r
            JOIN trainer_scenarios s ON r.scenario_id = s.id
            JOIN trainer_levels l ON s.level_id = l.id
            WHERE r.user_id = ? AND l.code = ?
            GROUP BY r.user_id
        """, (user_id, required_level))
        row = cursor.fetchone()

        if row and row['avg_percent'] and row['avg_percent'] >= required_percent:
            # Разблокируем уровень
            self._unlock_level(user_id, level['code'])
            return True

        return False

    def _unlock_level(self, user_id: str, level_code: str):
        """Разблокировать уровень для пользователя"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO trainer_user_progress (user_id, level_code, is_unlocked, unlocked_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
        """, (user_id, level_code))
        self.conn.commit()

    # ==================== КАТЕГОРИИ ====================

    def get_all_categories(self) -> List[Dict]:
        """Получить все категории"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_categories ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]

    def get_category_by_id(self, category_id: int) -> Optional[Dict]:
        """Получить категорию по ID"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    # ==================== СЦЕНАРИИ ====================

    def get_scenarios_by_level(self, level_code: str, category_id: int = None) -> List[Dict]:
        """Получить сценарии по уровню"""
        cursor = self.conn.cursor()
        level = self.get_level_by_code(level_code)
        if not level:
            return []

        if category_id:
            cursor.execute("""
                SELECT s.*, l.name as level_name, l.code as level_code, c.name as category_name, c.icon as category_icon
                FROM trainer_scenarios s
                JOIN trainer_levels l ON s.level_id = l.id
                LEFT JOIN trainer_categories c ON s.category_id = c.id
                WHERE s.level_id = ? AND s.category_id = ? AND s.is_active = 1
                ORDER BY s.order_num
            """, (level['id'], category_id))
        else:
            cursor.execute("""
                SELECT s.*, l.name as level_name, l.code as level_code, c.name as category_name, c.icon as category_icon
                FROM trainer_scenarios s
                JOIN trainer_levels l ON s.level_id = l.id
                LEFT JOIN trainer_categories c ON s.category_id = c.id
                WHERE s.level_id = ? AND s.is_active = 1
                ORDER BY s.order_num
            """, (level['id'],))
        return [dict(row) for row in cursor.fetchall()]

    def get_scenario(self, scenario_id: int) -> Optional[Dict]:
        """Получить сценарий по ID"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT s.*, l.name as level_name, l.code as level_code, c.name as category_name, c.icon as category_icon
            FROM trainer_scenarios s
            JOIN trainer_levels l ON s.level_id = l.id
            LEFT JOIN trainer_categories c ON s.category_id = c.id
            WHERE s.id = ?
        """, (scenario_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_scenarios(self, include_inactive: bool = False) -> List[Dict]:
        """Получить все сценарии"""
        cursor = self.conn.cursor()
        if include_inactive:
            cursor.execute("""
                SELECT s.*, l.name as level_name, l.code as level_code, c.name as category_name, c.icon as category_icon
                FROM trainer_scenarios s
                JOIN trainer_levels l ON s.level_id = l.id
                LEFT JOIN trainer_categories c ON s.category_id = c.id
                ORDER BY l.order_num, s.order_num
            """)
        else:
            cursor.execute("""
                SELECT s.*, l.name as level_name, l.code as level_code, c.name as category_name, c.icon as category_icon
                FROM trainer_scenarios s
                JOIN trainer_levels l ON s.level_id = l.id
                LEFT JOIN trainer_categories c ON s.category_id = c.id
                WHERE s.is_active = 1
                ORDER BY l.order_num, s.order_num
            """)
        return [dict(row) for row in cursor.fetchall()]

    def get_scenario_steps(self, scenario_id: int) -> List[Dict]:
        """Получить шаги сценария"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trainer_steps
            WHERE scenario_id = ?
            ORDER BY step_num
        """, (scenario_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_step_answers(self, step_id: int) -> List[Dict]:
        """Получить варианты ответов для шага"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trainer_answers
            WHERE step_id = ?
            ORDER BY order_num
        """, (step_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_step_by_num(self, scenario_id: int, step_num: int) -> Optional[Dict]:
        """Получить шаг по номеру"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trainer_steps
            WHERE scenario_id = ? AND step_num = ?
        """, (scenario_id, step_num))
        row = cursor.fetchone()
        if row:
            step = dict(row)
            step['answers'] = self.get_step_answers(step['id'])
            return step
        return None

    def get_steps_count(self, scenario_id: int) -> int:
        """Получить количество шагов в сценарии"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trainer_steps WHERE scenario_id = ?", (scenario_id,))
        return cursor.fetchone()[0]

    # ==================== ПРОГРЕСС ====================

    def get_user_progress(self, user_id: str) -> Dict:
        """Получить общий прогресс пользователя"""
        cursor = self.conn.cursor()

        result = {
            'total_completed': 0,
            'total_scenarios': 0,
            'average_score': 0,
            'levels': {}
        }

        # Получаем все уровни
        levels = self.get_all_levels()

        for level in levels:
            level_code = level['code']

            # Считаем сценарии на уровне
            cursor.execute("""
                SELECT COUNT(*) FROM trainer_scenarios
                WHERE level_id = ? AND is_active = 1
            """, (level['id'],))
            total = cursor.fetchone()[0]

            # Считаем пройденные сценарии
            cursor.execute("""
                SELECT COUNT(DISTINCT scenario_id) FROM trainer_results r
                JOIN trainer_scenarios s ON r.scenario_id = s.id
                WHERE r.user_id = ? AND s.level_id = ?
            """, (user_id, level['id']))
            completed = cursor.fetchone()[0]

            # Средний процент
            cursor.execute("""
                SELECT AVG(percent) FROM trainer_results r
                JOIN trainer_scenarios s ON r.scenario_id = s.id
                WHERE r.user_id = ? AND s.level_id = ?
            """, (user_id, level['id']))
            avg_row = cursor.fetchone()
            avg_percent = round(avg_row[0] or 0, 1)

            # Проверка разблокировки
            is_unlocked = self.check_level_unlocked(user_id, level_code)

            result['levels'][level_code] = {
                'name': level['name'],
                'icon': level['icon'],
                'color': level['color'],
                'completed': completed,
                'total': total,
                'avg_percent': avg_percent,
                'is_unlocked': is_unlocked,
                'required_level': level['required_level'],
                'required_percent': level['required_percent']
            }

            result['total_completed'] += completed
            result['total_scenarios'] += total

        # Общий средний балл
        cursor.execute("""
            SELECT AVG(percent) FROM trainer_results WHERE user_id = ?
        """, (user_id,))
        avg_row = cursor.fetchone()
        result['average_score'] = round(avg_row[0] or 0, 1)

        return result

    def get_scenario_user_result(self, user_id: str, scenario_id: int) -> Optional[Dict]:
        """Получить лучший результат пользователя по сценарию"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trainer_results
            WHERE user_id = ? AND scenario_id = ?
            ORDER BY percent DESC, completed_at DESC
            LIMIT 1
        """, (user_id, scenario_id))
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_user_progress(self, user_id: str, level_code: str):
        """Обновить прогресс пользователя по уровню"""
        cursor = self.conn.cursor()
        level = self.get_level_by_code(level_code)
        if not level:
            return

        # Считаем сценарии
        cursor.execute("""
            SELECT COUNT(*) FROM trainer_scenarios WHERE level_id = ? AND is_active = 1
        """, (level['id'],))
        total = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(DISTINCT scenario_id) FROM trainer_results r
            JOIN trainer_scenarios s ON r.scenario_id = s.id
            WHERE r.user_id = ? AND s.level_id = ?
        """, (user_id, level['id']))
        completed = cursor.fetchone()[0]

        cursor.execute("""
            INSERT OR REPLACE INTO trainer_user_progress
            (user_id, level_code, scenarios_completed, scenarios_total, is_unlocked, unlocked_at)
            VALUES (?, ?, ?, ?,
                    COALESCE((SELECT is_unlocked FROM trainer_user_progress WHERE user_id = ? AND level_code = ?), 0),
                    COALESCE((SELECT unlocked_at FROM trainer_user_progress WHERE user_id = ? AND level_code = ?), NULL))
        """, (user_id, level_code, completed, total, user_id, level_code, user_id, level_code))
        self.conn.commit()

        # Проверяем разблокировку следующего уровня
        self.check_and_unlock_levels(user_id)

    def check_and_unlock_levels(self, user_id: str):
        """Проверить и разблокировать доступные уровни"""
        levels = self.get_all_levels()
        for level in levels:
            if level['required_level']:
                self._check_unlock_condition(user_id, level)

    # ==================== РЕЗУЛЬТАТЫ ====================

    def save_result(self, user_id: str, scenario_id: int, score: int, max_score: int, answers: List[Dict],
                    final_loyalty: int = None, is_game_over: bool = False, timeout_count: int = 0,
                    selected_topic_id: int = None, selected_topic_name: str = None) -> Dict:
        """Сохранить результат прохождения"""
        percent = round((score / max_score) * 100) if max_score > 0 else 0
        grade = self.calculate_grade(percent)

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trainer_results (user_id, scenario_id, score, max_score, percent, grade, answers_json,
                                        final_loyalty, is_game_over, timeout_count, selected_topic_id, selected_topic_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, scenario_id, score, max_score, percent, grade, json.dumps(answers, ensure_ascii=False),
              final_loyalty, 1 if is_game_over else 0, timeout_count, selected_topic_id, selected_topic_name))

        self.conn.commit()
        result_id = cursor.lastrowid

        # Обновляем прогресс
        scenario = self.get_scenario(scenario_id)
        if scenario:
            self.update_user_progress(user_id, scenario['level_code'])

        return {
            'id': result_id,
            'score': score,
            'max_score': max_score,
            'percent': percent,
            'grade': grade,
            'final_loyalty': final_loyalty,
            'is_game_over': is_game_over
        }

    def get_user_results(self, user_id: str, scenario_id: int = None) -> List[Dict]:
        """Получить результаты пользователя"""
        cursor = self.conn.cursor()
        if scenario_id:
            cursor.execute("""
                SELECT r.*, s.title as scenario_title
                FROM trainer_results r
                JOIN trainer_scenarios s ON r.scenario_id = s.id
                WHERE r.user_id = ? AND r.scenario_id = ?
                ORDER BY r.completed_at DESC
            """, (user_id, scenario_id))
        else:
            cursor.execute("""
                SELECT r.*, s.title as scenario_title
                FROM trainer_results r
                JOIN trainer_scenarios s ON r.scenario_id = s.id
                WHERE r.user_id = ?
                ORDER BY r.completed_at DESC
            """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_result_by_id(self, result_id: int) -> Optional[Dict]:
        """Получить результат по ID"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT r.*, s.title as scenario_title, s.description as scenario_description,
                   l.name as level_name, l.code as level_code
            FROM trainer_results r
            JOIN trainer_scenarios s ON r.scenario_id = s.id
            JOIN trainer_levels l ON s.level_id = l.id
            WHERE r.id = ?
        """, (result_id,))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result.get('answers_json'):
                result['answers'] = json.loads(result['answers_json'])
            return result
        return None

    def calculate_grade(self, percent: int) -> str:
        """Вычислить оценку по проценту"""
        if percent >= 90:
            return "excellent"
        elif percent >= 70:
            return "good"
        elif percent >= 50:
            return "partial"
        else:
            return "fail"

    def get_grade_info(self, grade: str) -> Dict:
        """Получить информацию об оценке"""
        grades = {
            "excellent": {"name": "Отлично", "icon": "🏆", "color": "#4CAF50", "message": "Превосходная работа!"},
            "good": {"name": "Хорошо", "icon": "👍", "color": "#8BC34A", "message": "Хороший результат!"},
            "partial": {"name": "Удовлетворительно", "icon": "📚", "color": "#FF9800", "message": "Есть над чем поработать."},
            "fail": {"name": "Нужно повторить", "icon": "📖", "color": "#F44336", "message": "Рекомендуем пройти ещё раз."}
        }
        return grades.get(grade, grades["fail"])

    # ==================== АДМИН CRUD ====================

    def create_scenario(self, data: Dict) -> Dict:
        """Создать новый сценарий"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO trainer_scenarios (level_id, category_id, title, description, estimated_time, total_points, is_active, order_num, timer_seconds, initial_loyalty, client_info_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('level_id'),
                data.get('category_id'),
                data.get('title', ''),
                data.get('description', ''),
                data.get('estimated_time', 5),
                data.get('total_points', 100),
                data.get('is_active', 1),
                data.get('order_num', 0),
                data.get('timer_seconds', 15),
                data.get('initial_loyalty', 100),
                data.get('client_info_json')
            ))
            self.conn.commit()
            return {"success": True, "id": cursor.lastrowid}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_scenario(self, scenario_id: int, data: Dict) -> Dict:
        """Обновить сценарий"""
        try:
            allowed_fields = ['level_id', 'category_id', 'title', 'description',
                            'estimated_time', 'total_points', 'is_active', 'order_num',
                            'timer_seconds', 'initial_loyalty', 'client_info_json']
            updates = {k: v for k, v in data.items() if k in allowed_fields}

            if not updates:
                return {"success": False, "error": "Нет полей для обновления"}

            set_parts = [f"{field} = ?" for field in updates.keys()]
            values = list(updates.values())
            values.append(scenario_id)

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE trainer_scenarios
                SET {', '.join(set_parts)}
                WHERE id = ?
            """, values)
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_scenario(self, scenario_id: int) -> Dict:
        """Удалить сценарий"""
        try:
            cursor = self.conn.cursor()
            # Удаляем связанные данные (каскадно)
            cursor.execute("DELETE FROM trainer_answers WHERE step_id IN (SELECT id FROM trainer_steps WHERE scenario_id = ?)", (scenario_id,))
            cursor.execute("DELETE FROM trainer_steps WHERE scenario_id = ?", (scenario_id,))
            cursor.execute("DELETE FROM trainer_results WHERE scenario_id = ?", (scenario_id,))
            cursor.execute("DELETE FROM trainer_scenarios WHERE id = ?", (scenario_id,))
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_step(self, scenario_id: int, data: Dict) -> Dict:
        """Создать шаг сценария"""
        try:
            # Определяем номер шага
            cursor = self.conn.cursor()
            cursor.execute("SELECT MAX(step_num) FROM trainer_steps WHERE scenario_id = ?", (scenario_id,))
            max_num = cursor.fetchone()[0] or 0

            cursor.execute("""
                INSERT INTO trainer_steps (scenario_id, step_num, client_message, client_avatar, client_name, initial_mood)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                scenario_id,
                max_num + 1,
                data.get('client_message', ''),
                data.get('client_avatar', '👤'),
                data.get('client_name', 'Клиент'),
                data.get('initial_mood', 'neutral')
            ))
            self.conn.commit()
            return {"success": True, "id": cursor.lastrowid, "step_num": max_num + 1}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_step(self, step_id: int, data: Dict) -> Dict:
        """Обновить шаг"""
        try:
            allowed_fields = ['client_message', 'client_avatar', 'client_name', 'step_num', 'initial_mood']
            updates = {k: v for k, v in data.items() if k in allowed_fields}

            if not updates:
                return {"success": False, "error": "Нет полей для обновления"}

            set_parts = [f"{field} = ?" for field in updates.keys()]
            values = list(updates.values())
            values.append(step_id)

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE trainer_steps
                SET {', '.join(set_parts)}
                WHERE id = ?
            """, values)
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_step(self, step_id: int) -> Dict:
        """Удалить шаг"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM trainer_answers WHERE step_id = ?", (step_id,))
            cursor.execute("DELETE FROM trainer_steps WHERE id = ?", (step_id,))
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_answer(self, step_id: int, data: Dict) -> Dict:
        """Создать вариант ответа"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO trainer_answers (step_id, answer_text, is_correct, is_partial, points, feedback, order_num, mood_impact, knowledge_link)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                step_id,
                data.get('answer_text', ''),
                data.get('is_correct', 0),
                data.get('is_partial', 0),
                data.get('points', 0),
                data.get('feedback', ''),
                data.get('order_num', 0),
                data.get('mood_impact', 0),
                data.get('knowledge_link')
            ))
            self.conn.commit()
            return {"success": True, "id": cursor.lastrowid}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_answer(self, answer_id: int, data: Dict) -> Dict:
        """Обновить вариант ответа"""
        try:
            allowed_fields = ['answer_text', 'is_correct', 'is_partial', 'points', 'feedback', 'order_num', 'mood_impact', 'knowledge_link']
            updates = {k: v for k, v in data.items() if k in allowed_fields}

            if not updates:
                return {"success": False, "error": "Нет полей для обновления"}

            set_parts = [f"{field} = ?" for field in updates.keys()]
            values = list(updates.values())
            values.append(answer_id)

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE trainer_answers
                SET {', '.join(set_parts)}
                WHERE id = ?
            """, values)
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_answer(self, answer_id: int) -> Dict:
        """Удалить вариант ответа"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM trainer_answers WHERE id = ?", (answer_id,))
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ==================== СТАТИСТИКА ====================

    def get_statistics(self) -> Dict:
        """Получить общую статистику тренажера"""
        cursor = self.conn.cursor()

        # Общее количество сценариев
        cursor.execute("SELECT COUNT(*) FROM trainer_scenarios WHERE is_active = 1")
        total_scenarios = cursor.fetchone()[0]

        # Количество прохождений
        cursor.execute("SELECT COUNT(*) FROM trainer_results")
        total_completions = cursor.fetchone()[0]

        # Уникальных пользователей
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM trainer_results")
        unique_users = cursor.fetchone()[0]

        # Средний балл
        cursor.execute("SELECT AVG(percent) FROM trainer_results")
        avg_row = cursor.fetchone()
        avg_score = round(avg_row[0] or 0, 1)

        # Статистика по уровням
        levels_stats = []
        for level in self.get_all_levels():
            cursor.execute("""
                SELECT COUNT(*) FROM trainer_scenarios WHERE level_id = ? AND is_active = 1
            """, (level['id'],))
            scenarios = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*), AVG(percent) FROM trainer_results r
                JOIN trainer_scenarios s ON r.scenario_id = s.id
                WHERE s.level_id = ?
            """, (level['id'],))
            row = cursor.fetchone()

            levels_stats.append({
                'name': level['name'],
                'code': level['code'],
                'scenarios': scenarios,
                'completions': row[0] or 0,
                'avg_percent': round(row[1] or 0, 1)
            })

        # Топ пользователей
        cursor.execute("""
            SELECT user_id, COUNT(*) as completions, AVG(percent) as avg_percent
            FROM trainer_results
            GROUP BY user_id
            ORDER BY avg_percent DESC, completions DESC
            LIMIT 10
        """)
        top_users = [dict(row) for row in cursor.fetchall()]

        return {
            'total_scenarios': total_scenarios,
            'total_completions': total_completions,
            'unique_users': unique_users,
            'avg_score': avg_score,
            'levels': levels_stats,
            'top_users': top_users
        }

    def get_scenario_statistics(self, scenario_id: int) -> Dict:
        """Получить статистику по конкретному сценарию"""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT COUNT(*) as completions, AVG(percent) as avg_percent,
                   MIN(percent) as min_percent, MAX(percent) as max_percent
            FROM trainer_results
            WHERE scenario_id = ?
        """, (scenario_id,))
        row = cursor.fetchone()

        return {
            'completions': row['completions'] or 0,
            'avg_percent': round(row['avg_percent'] or 0, 1),
            'min_percent': row['min_percent'] or 0,
            'max_percent': row['max_percent'] or 0
        }

    def get_all_users_progress(self) -> List[Dict]:
        """Получить прогресс всех пользователей для экспорта"""
        cursor = self.conn.cursor()

        # Получаем всех пользователей с их статистикой
        cursor.execute("""
            SELECT
                user_id,
                COUNT(*) as total_completions,
                COUNT(DISTINCT scenario_id) as unique_scenarios,
                AVG(percent) as avg_percent,
                MAX(percent) as best_percent,
                MIN(completed_at) as first_completion,
                MAX(completed_at) as last_completion,
                SUM(CASE WHEN percent >= 80 THEN 1 ELSE 0 END) as excellent_count,
                SUM(CASE WHEN percent >= 60 AND percent < 80 THEN 1 ELSE 0 END) as good_count,
                SUM(CASE WHEN percent < 60 THEN 1 ELSE 0 END) as needs_work_count
            FROM trainer_results
            GROUP BY user_id
            ORDER BY avg_percent DESC
        """)

        users = []
        for row in cursor.fetchall():
            users.append({
                'user_id': row['user_id'],
                'total_completions': row['total_completions'],
                'unique_scenarios': row['unique_scenarios'],
                'avg_percent': round(row['avg_percent'] or 0, 1),
                'best_percent': row['best_percent'] or 0,
                'first_completion': row['first_completion'],
                'last_completion': row['last_completion'],
                'excellent_count': row['excellent_count'],
                'good_count': row['good_count'],
                'needs_work_count': row['needs_work_count']
            })

        return users

    def get_detailed_results(self) -> List[Dict]:
        """Получить детальные результаты всех прохождений"""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                r.user_id,
                s.title as scenario_title,
                l.name as level_name,
                r.score,
                r.max_score,
                r.percent,
                r.completed_at,
                r.is_game_over,
                r.final_loyalty
            FROM trainer_results r
            JOIN trainer_scenarios s ON r.scenario_id = s.id
            JOIN trainer_levels l ON s.level_id = l.id
            ORDER BY r.completed_at DESC
        """)

        results = []
        for row in cursor.fetchall():
            results.append({
                'user_id': row['user_id'],
                'scenario_title': row['scenario_title'],
                'level_name': row['level_name'],
                'score': row['score'],
                'max_score': row['max_score'],
                'percent': row['percent'],
                'completed_at': row['completed_at'],
                'is_game_over': row['is_game_over'],
                'final_loyalty': row['final_loyalty']
            })

        return results

    def close(self):
        """Закрытие соединения с БД"""
        if self.conn:
            self.conn.close()

    # ==================== ТЕГИ ====================

    def create_tag(self, name: str, color: str = '#607D8B', icon: str = '🏷️') -> Dict:
        """Создать новый тег"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO trainer_tags (name, color, icon) VALUES (?, ?, ?)
            """, (name, color, icon))
            self.conn.commit()
            return {"success": True, "id": cursor.lastrowid}
        except sqlite3.IntegrityError:
            return {"success": False, "error": "Тег с таким названием уже существует"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_tag(self, tag_id: int, data: Dict) -> Dict:
        """Обновить тег"""
        try:
            allowed_fields = ['name', 'color', 'icon']
            updates = {k: v for k, v in data.items() if k in allowed_fields}

            if not updates:
                return {"success": False, "error": "Нет полей для обновления"}

            set_parts = [f"{field} = ?" for field in updates.keys()]
            values = list(updates.values())
            values.append(tag_id)

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE trainer_tags SET {', '.join(set_parts)} WHERE id = ?
            """, values)
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_tag(self, tag_id: int) -> Dict:
        """Удалить тег"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM trainer_scenario_tags WHERE tag_id = ?", (tag_id,))
            cursor.execute("DELETE FROM trainer_tags WHERE id = ?", (tag_id,))
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_all_tags(self) -> List[Dict]:
        """Получить все теги"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_tags ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]

    def get_tag_by_id(self, tag_id: int) -> Optional[Dict]:
        """Получить тег по ID"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_tags WHERE id = ?", (tag_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def set_scenario_tags(self, scenario_id: int, tag_ids: List[int]) -> Dict:
        """Установить теги сценарию (заменяет существующие)"""
        try:
            cursor = self.conn.cursor()
            # Удаляем старые связи
            cursor.execute("DELETE FROM trainer_scenario_tags WHERE scenario_id = ?", (scenario_id,))
            # Добавляем новые
            for tag_id in tag_ids:
                cursor.execute("""
                    INSERT INTO trainer_scenario_tags (scenario_id, tag_id) VALUES (?, ?)
                """, (scenario_id, tag_id))
            self.conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_scenario_tags(self, scenario_id: int) -> List[Dict]:
        """Получить теги сценария"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT t.* FROM trainer_tags t
            JOIN trainer_scenario_tags st ON t.id = st.tag_id
            WHERE st.scenario_id = ?
            ORDER BY t.name
        """, (scenario_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_scenarios_by_tag(self, tag_id: int) -> List[Dict]:
        """Получить сценарии по тегу"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT s.*, l.name as level_name, l.code as level_code,
                   c.name as category_name, c.icon as category_icon
            FROM trainer_scenarios s
            JOIN trainer_levels l ON s.level_id = l.id
            LEFT JOIN trainer_categories c ON s.category_id = c.id
            JOIN trainer_scenario_tags st ON s.id = st.scenario_id
            WHERE st.tag_id = ? AND s.is_active = 1
            ORDER BY l.order_num, s.order_num
        """, (tag_id,))
        return [dict(row) for row in cursor.fetchall()]

    # ==================== АУДИТ ====================

    def log_action(self, user_id: str, action: str, entity_type: str,
                   entity_id: int = None, entity_name: str = None,
                   changes: dict = None, ip_address: str = None):
        """Записать действие в журнал аудита"""
        cursor = self.conn.cursor()
        try:
            changes_json = json.dumps(changes, ensure_ascii=False) if changes else None
            cursor.execute("""
                INSERT INTO trainer_audit_log
                (user_id, action, entity_type, entity_id, entity_name, changes_json, ip_address)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, action, entity_type, entity_id, entity_name, changes_json, ip_address))
            self.conn.commit()
        except Exception as e:
            print(f"[log_action] Ошибка: {e}")

    def get_audit_log(self, limit: int = 100, offset: int = 0,
                      entity_type: str = None, user_id: str = None) -> List[Dict]:
        """Получить журнал аудита"""
        cursor = self.conn.cursor()

        query = "SELECT * FROM trainer_audit_log WHERE 1=1"
        params = []

        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        logs = []
        for row in cursor.fetchall():
            log_entry = dict(row)
            if log_entry.get('changes_json'):
                try:
                    log_entry['changes'] = json.loads(log_entry['changes_json'])
                except:
                    log_entry['changes'] = None
            logs.append(log_entry)

        return logs

    def get_audit_stats(self) -> Dict:
        """Получить статистику аудита"""
        cursor = self.conn.cursor()

        # Всего записей
        cursor.execute("SELECT COUNT(*) FROM trainer_audit_log")
        total = cursor.fetchone()[0]

        # По типам действий
        cursor.execute("""
            SELECT action, COUNT(*) as count
            FROM trainer_audit_log
            GROUP BY action
            ORDER BY count DESC
        """)
        by_action = {row['action']: row['count'] for row in cursor.fetchall()}

        # По пользователям
        cursor.execute("""
            SELECT user_id, COUNT(*) as count
            FROM trainer_audit_log
            GROUP BY user_id
            ORDER BY count DESC
            LIMIT 10
        """)
        by_user = [dict(row) for row in cursor.fetchall()]

        # За последние 7 дней
        cursor.execute("""
            SELECT DATE(timestamp) as date, COUNT(*) as count
            FROM trainer_audit_log
            WHERE timestamp >= datetime('now', '-7 days')
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
        """)
        by_date = [dict(row) for row in cursor.fetchall()]

        return {
            'total': total,
            'by_action': by_action,
            'by_user': by_user,
            'by_date': by_date
        }


# Пример использования
if __name__ == "__main__":
    tm = TrainerManager("topics.db")

    # Создаем тестовый сценарий
    result = tm.create_scenario({
        'level_id': 1,
        'category_id': 1,
        'title': 'Тестовый сценарий',
        'description': 'Описание тестового сценария'
    })
    print(f"Создан сценарий: {result}")

    # Статистика
    stats = tm.get_statistics()
    print(f"Статистика: {stats}")

    tm.close()
