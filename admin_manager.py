"""
Безопасный модуль для управления мануалами
Соответствует требованиям безопасности DerScanner
"""
import json
import os
import hashlib
import secrets
import re
from typing import Dict, Any, Optional, List
from functools import wraps
from flask import session, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash


class AdminManager:
    """Безопасное управление мануалами"""

    def __init__(self, data_file: str = 'manuals_data.json'):
        self.data_file = data_file
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """Создаёт файл данных если его нет"""
        if not os.path.exists(self.data_file):
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump({"manuals": {}}, f, ensure_ascii=False, indent=2)

    def load_manuals(self) -> Dict[str, Any]:
        """Загружает мануалы из файла"""
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('manuals', {})
        except (json.JSONDecodeError, IOError) as e:
            print(f"Ошибка загрузки мануалов: {e}")
            return {}

    def save_manuals(self, manuals: Dict[str, Any]) -> bool:
        """Сохраняет мануалы в файл"""
        try:
            # Валидация перед сохранением
            if not isinstance(manuals, dict):
                raise ValueError("Manuals must be a dictionary")

            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump({"manuals": manuals}, f, ensure_ascii=False, indent=2)
            return True
        except (IOError, ValueError) as e:
            print(f"Ошибка сохранения мануалов: {e}")
            return False

    @staticmethod
    def validate_manual_id(manual_id: str) -> bool:
        """Валидация ID мануала (только цифры)"""
        return bool(re.match(r'^\d+$', manual_id))

    @staticmethod
    def validate_subproblem_id(subproblem_id: str) -> bool:
        """Валидация ID подпроблемы (формат: цифра.цифра)"""
        return bool(re.match(r'^\d\.\d+$', subproblem_id))

    @staticmethod
    def sanitize_text(text: str, max_length: int = 500) -> str:
        """Очистка текста от потенциально опасных символов"""
        if not isinstance(text, str):
            return ""
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        # Ограничиваем длину
        text = text[:max_length]
        return text.strip()

    @staticmethod
    def validate_photo_id(photo_id: str) -> bool:
        """Валидация Telegram file_id"""
        # Telegram file_id может содержать различные символы
        if not isinstance(photo_id, str):
            return False
        if len(photo_id) == 0 or len(photo_id) > 300:
            return False
        # Проверяем что это printable ASCII символы
        return all(32 <= ord(c) <= 126 for c in photo_id)

    @staticmethod
    def validate_video_id(video_id: str) -> bool:
        """Валидация Telegram video file_id (аналогично photo_id)"""
        if not isinstance(video_id, str):
            return False
        if len(video_id) == 0 or len(video_id) > 300:
            return False
        # Проверяем что это printable ASCII символы
        return all(32 <= ord(c) <= 126 for c in video_id)

    def get_manual(self, manual_id: str) -> Optional[Dict[str, Any]]:
        """Получить конкретный мануал"""
        if not self.validate_manual_id(manual_id):
            return None
        manuals = self.load_manuals()
        return manuals.get(manual_id)

    def update_manual(self, manual_id: str, title: str, data: Dict[str, Any]) -> bool:
        """Обновить мануал"""
        if not self.validate_manual_id(manual_id):
            return False

        # Валидация заголовка
        title = self.sanitize_text(title, max_length=200)
        if not title:
            return False

        manuals = self.load_manuals()

        # Безопасное обновление
        if manual_id not in manuals:
            manuals[manual_id] = {}

        manuals[manual_id]['title'] = title

        # Обновляем остальные данные с валидацией
        if 'subproblems' in data and isinstance(data['subproblems'], dict):
            manuals[manual_id]['subproblems'] = data['subproblems']

        if 'version_hints' in data and isinstance(data['version_hints'], dict):
            manuals[manual_id]['version_hints'] = data['version_hints']

        if 'photos' in data and isinstance(data['photos'], list):
            manuals[manual_id]['photos'] = data['photos']

        if 'video' in data and isinstance(data['video'], dict):
            manuals[manual_id]['video'] = data['video']

        if 'can_add_screenshots' in data:
            manuals[manual_id]['can_add_screenshots'] = data['can_add_screenshots']

        return self.save_manuals(manuals)

    def delete_manual(self, manual_id: str) -> bool:
        """Удалить мануал"""
        if not self.validate_manual_id(manual_id):
            return False

        manuals = self.load_manuals()
        if manual_id in manuals:
            del manuals[manual_id]
            return self.save_manuals(manuals)
        return False

    def update_photo(self, manual_id: str, subproblem_id: str,
                     photo_index: int, new_photo_id: str, caption: str) -> bool:
        """Обновить фото в мануале"""
        if not self.validate_manual_id(manual_id):
            return False
        if not self.validate_subproblem_id(subproblem_id):
            return False
        if not self.validate_photo_id(new_photo_id):
            return False

        caption = self.sanitize_text(caption, max_length=300)

        manuals = self.load_manuals()

        if manual_id not in manuals:
            return False

        manual = manuals[manual_id]

        if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
            return False

        subproblem = manual['subproblems'][subproblem_id]

        if 'photos' not in subproblem or not isinstance(subproblem['photos'], list):
            subproblem['photos'] = []

        # Проверка индекса
        if photo_index < 0 or photo_index >= len(subproblem['photos']):
            return False

        # Обновляем фото
        subproblem['photos'][photo_index] = {
            'id': new_photo_id,
            'caption': caption
        }

        return self.save_manuals(manuals)

    def delete_photo(self, manual_id: str, subproblem_id: str, photo_index: int) -> bool:
        """Удалить фото из мануала (оставляет текст, удаляет только фото)"""
        if not self.validate_manual_id(manual_id):
            return False
        if not self.validate_subproblem_id(subproblem_id):
            return False

        manuals = self.load_manuals()

        if manual_id not in manuals:
            return False

        manual = manuals[manual_id]

        if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
            return False

        subproblem = manual['subproblems'][subproblem_id]

        if 'photos' not in subproblem or not isinstance(subproblem['photos'], list):
            return False

        # Проверка индекса
        if photo_index < 0 or photo_index >= len(subproblem['photos']):
            return False

        # Удаляем только id фото, оставляя caption (текст шага)
        subproblem['photos'][photo_index]['id'] = None

        return self.save_manuals(manuals)

    def add_video_to_subproblem(self, manual_id: str, subproblem_id: str,
                                video_file_id: str, caption: str) -> bool:
        """Добавить Telegram видео в подпроблему"""
        if not self.validate_manual_id(manual_id):
            return False
        if not self.validate_subproblem_id(subproblem_id):
            return False
        if not self.validate_video_id(video_file_id):
            return False

        caption = self.sanitize_text(caption, max_length=300)

        manuals = self.load_manuals()

        if manual_id not in manuals:
            return False

        manual = manuals[manual_id]

        if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
            return False

        subproblem = manual['subproblems'][subproblem_id]

        # Добавляем video_id в подпроблему (только один видео-мануал на подпроблему)
        subproblem['video'] = {
            'id': video_file_id,
            'caption': caption
        }

        return self.save_manuals(manuals)

    def delete_video(self, manual_id: str, subproblem_id: str) -> bool:
        """Удалить видео из подпроблемы"""
        if not self.validate_manual_id(manual_id):
            return False
        if not self.validate_subproblem_id(subproblem_id):
            return False

        manuals = self.load_manuals()

        if manual_id not in manuals:
            return False

        manual = manuals[manual_id]

        if 'subproblems' not in manual or subproblem_id not in manual['subproblems']:
            return False

        subproblem = manual['subproblems'][subproblem_id]

        # Удаляем видео если оно есть
        if 'video' in subproblem:
            del subproblem['video']
            return self.save_manuals(manuals)

        return False

    def add_new_step(self, manual_id: str, subproblem_id: str, caption: str, after_index: int = -1) -> bool:
        """Добавить новый пустой шаг в подпроблему

        Args:
            manual_id: ID мануала
            subproblem_id: ID подпроблемы
            caption: Описание шага
            after_index: Индекс, после которого вставить шаг.
                        -1 = в конец
                        -2 = в начало (перед первым шагом)
        """
        if not self.validate_manual_id(manual_id):
            return False

        caption = self.sanitize_text(caption, max_length=300)
        if not caption:
            return False

        manuals = self.load_manuals()

        if manual_id not in manuals:
            return False

        manual = manuals[manual_id]

        # Поддержка двух типов мануалов:
        # 1. Мануал с подпроблемами: subproblem_id должен быть в формате X.Y
        # 2. Простой мануал: subproblem_id = manual_id, работаем напрямую с manual
        if 'subproblems' in manual:
            # Мануал с подпроблемами
            if not self.validate_subproblem_id(subproblem_id):
                return False
            if subproblem_id not in manual['subproblems']:
                return False
            subproblem = manual['subproblems'][subproblem_id]
        else:
            # Простой мануал - работаем напрямую с manual
            subproblem = manual

        if 'photos' not in subproblem or not isinstance(subproblem['photos'], list):
            subproblem['photos'] = []

        # Создаем новый шаг
        new_step = {
            'id': None,
            'caption': caption
        }

        # Вставляем на нужную позицию
        if after_index == -2:
            # Вставляем в начало (перед первым шагом)
            subproblem['photos'].insert(0, new_step)
        elif after_index == -1 or after_index >= len(subproblem['photos']) - 1:
            # Добавляем в конец
            subproblem['photos'].append(new_step)
        else:
            # Вставляем после указанного индекса
            subproblem['photos'].insert(after_index + 1, new_step)

        return self.save_manuals(manuals)


# Роли администраторов
ROLE_SUPER_ADMIN = 'super_admin'
ROLE_EDITOR = 'editor'

ROLE_NAMES = {
    ROLE_SUPER_ADMIN: 'Супер-администратор',
    ROLE_EDITOR: 'Редактор'
}


class AdminsManager:
    """Управление учетными записями администраторов"""

    def __init__(self, admins_file: str = 'admins.json'):
        self.admins_file = admins_file
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """Создаёт файл с учетными записями если его нет"""
        if not os.path.exists(self.admins_file):
            # Создаем файл с пустым списком администраторов
            with open(self.admins_file, 'w', encoding='utf-8') as f:
                json.dump({"admins": []}, f, ensure_ascii=False, indent=2)

    def load_admins(self) -> List[Dict[str, Any]]:
        """Загружает список администраторов из файла"""
        try:
            with open(self.admins_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('admins', [])
        except (json.JSONDecodeError, IOError) as e:
            print(f"Ошибка загрузки администраторов: {e}")
            return []

    def save_admins(self, admins: List[Dict[str, Any]]) -> bool:
        """Сохраняет список администраторов в файл"""
        try:
            if not isinstance(admins, list):
                raise ValueError("Admins must be a list")

            with open(self.admins_file, 'w', encoding='utf-8') as f:
                json.dump({"admins": admins}, f, ensure_ascii=False, indent=2)
            return True
        except (IOError, ValueError) as e:
            print(f"Ошибка сохранения администраторов: {e}")
            return False

    @staticmethod
    def validate_username(username: str) -> bool:
        """Валидация имени пользователя (только латиница, цифры, подчеркивание)"""
        if not isinstance(username, str):
            return False
        return bool(re.match(r'^[a-zA-Z0-9_]{3,30}$', username))

    @staticmethod
    def validate_role(role: str) -> bool:
        """Валидация роли"""
        return role in [ROLE_SUPER_ADMIN, ROLE_EDITOR]

    def get_admin_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Получить администратора по username"""
        if not self.validate_username(username):
            return None

        admins = self.load_admins()
        for admin in admins:
            if admin.get('username') == username:
                return admin
        return None

    def create_admin(self, username: str, password: str, role: str, created_by: str = 'system') -> Dict[str, Any]:
        """Создать нового администратора"""
        # Валидация
        if not self.validate_username(username):
            return {'success': False, 'error': 'Некорректное имя пользователя'}

        if not self.validate_role(role):
            return {'success': False, 'error': 'Некорректная роль'}

        if len(password) < 6 or len(password) > 128:
            return {'success': False, 'error': 'Пароль должен быть от 6 до 128 символов'}

        # Проверяем, что администратор с таким именем не существует
        if self.get_admin_by_username(username):
            return {'success': False, 'error': 'Администратор с таким именем уже существует'}

        admins = self.load_admins()

        # Создаем нового администратора
        new_admin = {
            'username': username,
            'password_hash': generate_password_hash(password),
            'role': role,
            'created_by': created_by,
            'created_at': self._get_current_timestamp(),
            'active': True
        }

        admins.append(new_admin)

        if self.save_admins(admins):
            return {'success': True, 'username': username}
        else:
            return {'success': False, 'error': 'Ошибка при сохранении'}

    def update_admin_password(self, username: str, new_password: str) -> Dict[str, Any]:
        """Обновить пароль администратора"""
        if not self.validate_username(username):
            return {'success': False, 'error': 'Некорректное имя пользователя'}

        if len(new_password) < 6 or len(new_password) > 128:
            return {'success': False, 'error': 'Пароль должен быть от 6 до 128 символов'}

        admins = self.load_admins()
        admin_found = False

        for admin in admins:
            if admin.get('username') == username:
                admin['password_hash'] = generate_password_hash(new_password)
                admin_found = True
                break

        if not admin_found:
            return {'success': False, 'error': 'Администратор не найден'}

        if self.save_admins(admins):
            return {'success': True}
        else:
            return {'success': False, 'error': 'Ошибка при сохранении'}

    def delete_admin(self, username: str) -> Dict[str, Any]:
        """Удалить администратора"""
        if not self.validate_username(username):
            return {'success': False, 'error': 'Некорректное имя пользователя'}

        admins = self.load_admins()

        # Проверяем, что это не последний супер-админ
        super_admins = [a for a in admins if a.get('role') == ROLE_SUPER_ADMIN]
        if len(super_admins) == 1 and super_admins[0].get('username') == username:
            return {'success': False, 'error': 'Нельзя удалить последнего супер-администратора'}

        # Удаляем администратора
        admins = [a for a in admins if a.get('username') != username]

        if self.save_admins(admins):
            return {'success': True}
        else:
            return {'success': False, 'error': 'Ошибка при сохранении'}

    def change_admin_role(self, username: str, new_role: str) -> Dict[str, Any]:
        """Изменить роль администратора"""
        if not self.validate_username(username):
            return {'success': False, 'error': 'Некорректное имя пользователя'}

        if not self.validate_role(new_role):
            return {'success': False, 'error': 'Некорректная роль'}

        admins = self.load_admins()

        # Проверяем, что это не последний супер-админ
        admin_to_change = None
        super_admins = [a for a in admins if a.get('role') == ROLE_SUPER_ADMIN]

        for admin in admins:
            if admin.get('username') == username:
                admin_to_change = admin
                break

        if not admin_to_change:
            return {'success': False, 'error': 'Администратор не найден'}

        # Если пытаемся понизить роль последнего супер-админа
        if (admin_to_change.get('role') == ROLE_SUPER_ADMIN and
            new_role != ROLE_SUPER_ADMIN and
            len(super_admins) == 1):
            return {'success': False, 'error': 'Нельзя понизить роль последнего супер-администратора'}

        # Изменяем роль
        admin_to_change['role'] = new_role

        if self.save_admins(admins):
            return {'success': True}
        else:
            return {'success': False, 'error': 'Ошибка при сохранении'}

    @staticmethod
    def _get_current_timestamp() -> str:
        """Получить текущую временную метку"""
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class AdminAuth:
    """Безопасная авторизация администраторов"""

    @staticmethod
    def _get_admin_credentials_from_env() -> Dict[str, Dict[str, str]]:
        """Получает учетные данные администраторов из переменных окружения (для обратной совместимости)"""
        admin_username = os.getenv('ADMIN_USERNAME', '')
        admin_password_hash = os.getenv('ADMIN_PASSWORD_HASH', '')

        if admin_username and admin_password_hash:
            return {
                admin_username: {
                    'password_hash': admin_password_hash,
                    'role': ROLE_SUPER_ADMIN  # По умолчанию супер-админ
                }
            }
        return {}

    @staticmethod
    def verify_admin(username: str, password: str) -> Optional[Dict[str, Any]]:
        """Проверка учётных данных администратора. Возвращает данные администратора или None"""
        if not isinstance(username, str) or not isinstance(password, str):
            return None

        # Ограничение длины для предотвращения DoS
        if len(username) > 100 or len(password) > 128:
            return None

        # ПРИОРИТЕТ 1: Проверяем через Active Directory (если настроен)
        try:
            from ad_auth import ad_auth
            if ad_auth.is_configured():
                ad_result = ad_auth.verify_credentials(username, password)
                if ad_result:
                    # Успешная авторизация через AD
                    return {
                        'username': ad_result.get('username', username),
                        'role': ad_result.get('role', ROLE_EDITOR),
                        'display_name': ad_result.get('display_name', username),
                        'email': ad_result.get('email', ''),
                        'auth_method': 'ad'
                    }
        except Exception as e:
            print(f"AD authentication error: {e}")
            # Продолжаем проверку локальных учетных записей

        # ПРИОРИТЕТ 2: Проверяем в файле admins.json (локальные учетные записи)
        admins_mgr = AdminsManager()
        admin = admins_mgr.get_admin_by_username(username)

        if admin and admin.get('active', True):
            password_hash = admin.get('password_hash', '')
            if check_password_hash(password_hash, password):
                return {
                    'username': username,
                    'role': admin.get('role', ROLE_EDITOR),
                    'auth_method': 'local'
                }

        # ПРИОРИТЕТ 3: Проверяем в переменных окружения (обратная совместимость)
        env_credentials = AdminAuth._get_admin_credentials_from_env()
        if username in env_credentials:
            hashed = env_credentials[username]['password_hash']
            if check_password_hash(hashed, password):
                return {
                    'username': username,
                    'role': env_credentials[username]['role'],
                    'auth_method': 'env'
                }

        return None

    @staticmethod
    def login_required(f):
        """Декоратор для защиты admin маршрутов"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get('admin_logged_in'):
                flash('Требуется авторизация')
                return redirect(url_for('admin_login'))
            return f(*args, **kwargs)
        return decorated_function

    @staticmethod
    def super_admin_required(f):
        """Декоратор для защиты маршрутов, доступных только супер-администраторам"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get('admin_logged_in'):
                flash('Требуется авторизация')
                return redirect(url_for('admin_login'))

            admin_role = session.get('admin_role')
            if admin_role != ROLE_SUPER_ADMIN:
                flash('Доступ запрещен. Требуются права супер-администратора.')
                return redirect(url_for('admin_dashboard'))

            return f(*args, **kwargs)
        return decorated_function

    @staticmethod
    def has_permission(permission: str) -> bool:
        """Проверить, есть ли у текущего администратора определенное разрешение"""
        if not session.get('admin_logged_in'):
            return False

        admin_role = session.get('admin_role')

        # Супер-админ может всё
        if admin_role == ROLE_SUPER_ADMIN:
            return True

        # Редактор может редактировать контент
        if admin_role == ROLE_EDITOR:
            return permission in ['edit_manuals', 'edit_topics', 'view_stats']

        return False

    @staticmethod
    def generate_session_token() -> str:
        """Генерация безопасного токена сессии"""
        return secrets.token_urlsafe(32)


# Глобальные экземпляры
admin_manager = AdminManager()
admins_manager = AdminsManager()
