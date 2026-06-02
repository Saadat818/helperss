"""
Модуль для аутентификации через Active Directory
Двухэтапная схема:
  1. Service bind — подключение через сервисную учётку (svc_apo)
  2. Поиск пользователя в AD
  3. User bind — проверка пароля пользователя

Используем SIMPLE authentication через LDAPS (SSL) — не требует MD4
"""
import os
import ssl
from typing import Optional, Dict, Any
from ldap3 import Server, Connection, ALL, SIMPLE, Tls
from ldap3.core.exceptions import LDAPException, LDAPBindError
from ldap3.utils.conv import escape_filter_chars
from dotenv import load_dotenv

load_dotenv()


class ADAuth:
    """Аутентификация через Active Directory с сервисной учёткой"""

    def __init__(self):
        self.server_uri = os.getenv('AD_SERVER', 'ldap://localhost')
        self.port = int(os.getenv('AD_PORT', '636'))
        self.domain = os.getenv('AD_DOMAIN', 'cbk')
        self.base_dn = os.getenv('AD_BASE_DN', 'DC=cbk,DC=kg')
        self.use_ssl = os.getenv('AD_USE_SSL', 'true').lower() == 'true'
        self.admin_group = os.getenv('AD_ADMIN_GROUP', '')
        self.dev_mode = os.getenv('DEV_MODE', 'false').lower() == 'true'

        # Путь к CA-сертификату AD сервера для проверки SSL
        # Если указан — используется CERT_REQUIRED (безопасно)
        # Если не указан — CERT_NONE с предупреждением (MiTM уязвимость)
        self.ca_cert_file = os.getenv('AD_CA_CERT', '')

        # Сервисная учётка для bind
        self.bind_user = os.getenv('LDAP_BIND_USER', '')
        self.bind_password = os.getenv('LDAP_BIND_PASSWORD', '')

        # Гранулярные роли администраторов по логинам
        def _parse_logins(env_key):
            val = os.getenv(env_key, '')
            return set(u.strip().lower() for u in val.split(',') if u.strip())

        self.super_admin_logins = _parse_logins('AD_SUPER_ADMINS')
        self.admins_manuals = _parse_logins('AD_ADMINS_MANUALS')
        self.admins_topics = _parse_logins('AD_ADMINS_TOPICS')
        self.admins_scenarios = _parse_logins('AD_ADMINS_SCENARIOS')
        self.admins_trainer = _parse_logins('AD_ADMINS_TRAINER')
        self.trainer_viewers = _parse_logins('AD_TRAINER_VIEWERS')

    def _get_user_principal(self, username: str) -> str:
        """
        Формирует UPN (User Principal Name) для SIMPLE bind
        Формат: username@domain.suffix (например: svc_apo@cbk.kg)
        """
        clean_username = username.split('\\')[-1].split('@')[0]
        # Формируем UPN из base_dn: DC=cbk,DC=kg -> cbk.kg
        domain_parts = []
        for part in self.base_dn.split(','):
            part = part.strip()
            if part.upper().startswith('DC='):
                domain_parts.append(part[3:])
        domain_suffix = '.'.join(domain_parts)
        return f"{clean_username}@{domain_suffix}"

    def _create_server(self) -> Server:
        """Создаёт объект LDAP-сервера с учётом SSL"""
        if self.use_ssl:
            # Для LDAPS — настраиваем TLS
            if self.ca_cert_file and os.path.isfile(self.ca_cert_file):
                # Безопасный режим: проверяем сертификат AD сервера
                tls_config = Tls(
                    validate=ssl.CERT_REQUIRED,
                    ca_certs_file=self.ca_cert_file,
                    version=ssl.PROTOCOL_TLSv1_2
                )
                print("[AD] TLS: используется CA-сертификат, проверка включена (CERT_REQUIRED)")
            else:
                # Без проверки сертификата — допустимо во внутренней сети
                tls_config = Tls(
                    validate=ssl.CERT_NONE,
                    version=ssl.PROTOCOL_TLSv1_2
                )
                print("[AD] ⚠ SSL без проверки сертификата (CERT_NONE) — внутренняя сеть")
                print("[AD] ⚠ Для повышения безопасности укажите AD_CA_CERT=/path/to/ca.pem в .env")
            server = Server(
                self.server_uri,
                port=self.port,
                use_ssl=True,
                tls=tls_config,
                get_info=ALL,
                connect_timeout=10
            )
        else:
            server = Server(
                self.server_uri,
                port=self.port,
                get_info=ALL,
                use_ssl=False,
                connect_timeout=10
            )
        return server

    def _service_bind(self) -> Optional[Connection]:
        """
        Этап 1: Подключение через сервисную учётку (svc_apo)
        Используем SIMPLE bind с UPN (user@domain.kg) через LDAPS
        Возвращает Connection или None
        """
        if not self.bind_user or not self.bind_password:
            print("[AD] Сервисная учётка не настроена (LDAP_BIND_USER / LDAP_BIND_PASSWORD)")
            return None

        # Формируем UPN для SIMPLE auth
        bind_upn = self._get_user_principal(self.bind_user)
        print(f"[AD] Попытка service bind как: {bind_upn}")

        try:
            server = self._create_server()
            conn = Connection(
                server,
                user=bind_upn,
                password=self.bind_password,
                authentication=SIMPLE,
                auto_bind=True,
                read_only=True
            )
            print(f"[AD] Service bind успешен: {bind_upn}")
            return conn
        except LDAPBindError as e:
            print(f"[AD] Service bind ОШИБКА (неверные учётные данные svc_apo): {e}")
            return None
        except LDAPException as e:
            print(f"[AD] Service bind LDAP ошибка: {e}")
            return None
        except Exception as e:
            print(f"[AD] Service bind непредвиденная ошибка: {e}")
            return None

    def _search_user(self, conn: Connection, username: str) -> Optional[Dict[str, Any]]:
        """
        Этап 2: Поиск пользователя в AD через сервисное подключение
        """
        # Очищаем username от домена если есть
        clean_username = username.split('\\')[-1].split('@')[0]

        safe_username = escape_filter_chars(clean_username)
        search_filter = f"(sAMAccountName={safe_username})"
        try:
            conn.search(
                search_base=self.base_dn,
                search_filter=search_filter,
                attributes=['displayName', 'mail', 'memberOf', 'sAMAccountName',
                            'department', 'title', 'distinguishedName']
            )

            if not conn.entries:
                print(f"[AD] Пользователь не найден: {clean_username}")
                return None

            entry = conn.entries[0]
            user_info = {
                'username': str(entry.sAMAccountName) if hasattr(entry, 'sAMAccountName') else clean_username,
                'display_name': str(entry.displayName) if hasattr(entry, 'displayName') else clean_username,
                'email': str(entry.mail) if hasattr(entry, 'mail') else '',
                'department': str(entry.department) if hasattr(entry, 'department') else '',
                'title': str(entry.title) if hasattr(entry, 'title') else '',
                'dn': str(entry.distinguishedName) if hasattr(entry, 'distinguishedName') else '',
                'permissions': []  # Список разрешений
            }

            # Определяем разрешения по логину из .env
            lower_username = user_info['username'].lower()

            if lower_username in self.super_admin_logins:
                user_info['permissions'].append('super_admin')
            if lower_username in self.admins_manuals:
                user_info['permissions'].append('admin_manuals')
            if lower_username in self.admins_topics:
                user_info['permissions'].append('admin_topics')
            if lower_username in self.admins_scenarios:
                user_info['permissions'].append('admin_scenarios')
            if lower_username in self.admins_trainer:
                user_info['permissions'].append('admin_trainer')
            if lower_username in self.trainer_viewers:
                user_info['permissions'].append('trainer_viewer')

            # Проверяем принадлежность к группе AD (если настроена)
            if self.admin_group and hasattr(entry, 'memberOf'):
                member_of = [str(group) for group in entry.memberOf]
                if self.admin_group in member_of and 'super_admin' not in user_info['permissions']:
                    user_info['permissions'].append('super_admin')

            # Для обратной совместимости — определяем главную роль
            if 'super_admin' in user_info['permissions']:
                user_info['role'] = 'super_admin'
            elif user_info['permissions']:
                user_info['role'] = user_info['permissions'][0]
            else:
                user_info['role'] = 'user'  # Обычный пользователь, не админ

            is_admin = bool(user_info['permissions'])
            print(f"[AD] Пользователь найден: {user_info['display_name']} (права: {user_info['permissions']}, админ: {is_admin})")
            return user_info

        except Exception as e:
            print(f"[AD] Ошибка поиска пользователя: {e}")
            return None

    def _user_bind(self, username: str, password: str) -> bool:
        """
        Этап 3: Проверка пароля пользователя через отдельный SIMPLE bind
        Используем UPN формат: username@cbk.kg
        """
        user_upn = self._get_user_principal(username)
        print(f"[AD] Попытка user bind как: {user_upn}")

        try:
            server = self._create_server()
            conn = Connection(
                server,
                user=user_upn,
                password=password,
                authentication=SIMPLE,
                auto_bind=True
            )
            print(f"[AD] User bind успешен: {user_upn}")
            conn.unbind()
            return True
        except LDAPBindError:
            print(f"[AD] User bind ОШИБКА: неверный пароль для {user_upn}")
            return False
        except LDAPException as e:
            print(f"[AD] User bind LDAP ошибка: {e}")
            return False
        except Exception as e:
            print(f"[AD] User bind непредвиденная ошибка: {e}")
            return False

    def verify_credentials(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """
        Полная проверка учётных данных через AD (3 этапа)

        1. Service bind через svc_apo
        2. Поиск пользователя в AD
        3. User bind — проверка пароля пользователя

        Args:
            username: Логин пользователя (формат: r_koledin или cbk\\r_koledin)
            password: Пароль пользователя

        Returns:
            Dict с информацией о пользователе или None при ошибке
        """
        if not username or not password:
            return None

        # Ограничение длины для безопасности
        if len(username) > 100 or len(password) > 128:
            return None

        print(f"[AD] === Начало аутентификации для: {username} ===")

        # Этап 1: Service bind
        service_conn = self._service_bind()
        if not service_conn:
            print("[AD] Не удалось подключиться через сервисную учётку")
            return None

        try:
            # Этап 2: Поиск пользователя
            user_info = self._search_user(service_conn, username)
            if not user_info:
                return None

            # Этап 3: Проверка пароля пользователя
            if not self._user_bind(username, password):
                return None

            print(f"[AD] === Аутентификация УСПЕШНА: {user_info['display_name']} ===")
            return user_info

        finally:
            # Всегда закрываем сервисное подключение
            try:
                service_conn.unbind()
            except Exception:
                pass

    def is_configured(self) -> bool:
        """Проверка, настроен ли AD"""
        if not self.server_uri:
            return False

        normalized_uri = self.server_uri.strip().lower()
        placeholder_hosts = {
            'ldap://localhost',
            'ldap://your-ad-server.local',
            'ldaps://your-ad-server.local',
            'your-ad-server.local',
        }

        if normalized_uri in placeholder_hosts:
            return False

        # Дополнительно проверяем что сервисная учётка настроена
        if not self.bind_user or not self.bind_password or 'СЮДА_ВСТАВЬ' in self.bind_password:
            return False

        return True

    def test_connection(self) -> Dict[str, Any]:
        """
        Тестирование подключения к AD (для диагностики)
        Возвращает результат теста
        """
        result = {
            'server': self.server_uri,
            'port': self.port,
            'ssl': self.use_ssl,
            'bind_user': self.bind_user,
            'base_dn': self.base_dn,
            'configured': self.is_configured(),
            'service_bind': False,
            'search_works': False,
            'error': None
        }

        if not result['configured']:
            result['error'] = 'AD не настроен — проверь .env'
            return result

        # Тест service bind
        conn = self._service_bind()
        if conn:
            result['service_bind'] = True

            # Тест поиска
            try:
                conn.search(
                    search_base=self.base_dn,
                    search_filter='(objectClass=user)',
                    attributes=['sAMAccountName'],
                    size_limit=1
                )
                result['search_works'] = len(conn.entries) > 0
                if conn.entries:
                    result['sample_user'] = str(conn.entries[0].sAMAccountName)
            except Exception as e:
                result['error'] = f'Поиск не работает: {e}'
            finally:
                conn.unbind()
        else:
            result['error'] = 'Service bind не удался — проверь LDAP_BIND_USER и LDAP_BIND_PASSWORD'

        return result


# Глобальный экземпляр
ad_auth = ADAuth()
