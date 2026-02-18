"""
Модуль для аутентификации через Active Directory
Двухэтапная схема:
  1. Service bind — подключение через сервисную учётку (svc_apo)
  2. Поиск пользователя в AD
  3. User bind — проверка пароля пользователя
"""
import os
import ssl
from typing import Optional, Dict, Any
from ldap3 import Server, Connection, ALL, NTLM, SIMPLE, Tls
from ldap3.core.exceptions import LDAPException, LDAPBindError
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

        # Сервисная учётка для bind
        self.bind_user = os.getenv('LDAP_BIND_USER', '')
        self.bind_password = os.getenv('LDAP_BIND_PASSWORD', '')

        # Списки администраторов по логинам
        admins_str = os.getenv('AD_ADMINS', '')
        super_admins_str = os.getenv('AD_SUPER_ADMINS', '')

        self.admin_logins = set(u.strip().lower() for u in admins_str.split(',') if u.strip())
        self.super_admin_logins = set(u.strip().lower() for u in super_admins_str.split(',') if u.strip())

    def _create_server(self) -> Server:
        """Создаёт объект LDAP-сервера с учётом SSL"""
        if self.use_ssl:
            # Для LDAPS — настраиваем TLS
            tls_config = Tls(
                validate=ssl.CERT_NONE,  # На этапе тестирования не проверяем сертификат
                version=ssl.PROTOCOL_TLSv1_2
            )
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
        Возвращает Connection или None
        """
        if not self.bind_user or not self.bind_password:
            print("[AD] Сервисная учётка не настроена (LDAP_BIND_USER / LDAP_BIND_PASSWORD)")
            return None

        try:
            server = self._create_server()
            conn = Connection(
                server,
                user=self.bind_user,
                password=self.bind_password,
                authentication=NTLM,
                auto_bind=True,
                read_only=True
            )
            print(f"[AD] Service bind успешен: {self.bind_user}")
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

        search_filter = f"(sAMAccountName={clean_username})"
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
                'role': 'editor'  # По умолчанию
            }

            # Проверка роли по логину (ПРИОРИТЕТ 1)
            lower_username = user_info['username'].lower()
            if lower_username in self.super_admin_logins:
                user_info['role'] = 'super_admin'
            elif lower_username in self.admin_logins:
                user_info['role'] = 'admin'
            # Проверяем принадлежность к группе администраторов (ПРИОРИТЕТ 2)
            elif self.admin_group and hasattr(entry, 'memberOf'):
                member_of = [str(group) for group in entry.memberOf]
                if self.admin_group in member_of:
                    user_info['role'] = 'super_admin'

            print(f"[AD] Пользователь найден: {user_info['display_name']} (роль: {user_info['role']})")
            return user_info

        except Exception as e:
            print(f"[AD] Ошибка поиска пользователя: {e}")
            return None

    def _user_bind(self, username: str, password: str) -> bool:
        """
        Этап 3: Проверка пароля пользователя через отдельный bind
        """
        # Формируем полный логин DOMAIN\\username
        clean_username = username.split('\\')[-1].split('@')[0]
        user_dn = f"{self.domain}\\{clean_username}"

        try:
            server = self._create_server()
            conn = Connection(
                server,
                user=user_dn,
                password=password,
                authentication=NTLM,
                auto_bind=True
            )
            print(f"[AD] User bind успешен: {clean_username}")
            conn.unbind()
            return True
        except LDAPBindError:
            print(f"[AD] User bind ОШИБКА: неверный пароль для {clean_username}")
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
