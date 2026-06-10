import hashlib
import hmac
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MYBOARD_TYPE_MAP = {
    "start": "start",
    "text": "message",
    "message": "message",
    "question": "question",
    "condition": "condition",
    "action": "action",
    "end": "end",
    "final": "end",
    "media": "media",
    "note": "note",
    "group": "group",
    "script_step": "message",
}


@dataclass
class MyBoardConfig:
    api_url: str
    api_token: str
    webhook_secret: str
    enable_sync: bool
    timeout: int = 20
    verify_ssl: bool = True

    @property
    def ready_for_api(self) -> bool:
        return self.enable_sync and bool(self.api_url and self.api_token)

    @property
    def ready_for_webhook(self) -> bool:
        return self.enable_sync and bool(self.webhook_secret)


class MyBoardApiError(RuntimeError):
    def __init__(self, admin_message: str, status_code: int | None = None):
        super().__init__(admin_message)
        self.admin_message = admin_message
        self.status_code = status_code


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def get_myboard_config() -> MyBoardConfig:
    return MyBoardConfig(
        api_url=(os.getenv("MYBOARD_API_URL") or "").strip().rstrip("/"),
        api_token=(os.getenv("MYBOARD_API_TOKEN") or "").strip(),
        webhook_secret=(os.getenv("MYBOARD_WEBHOOK_SECRET") or "").strip(),
        enable_sync=_env_bool("MYBOARD_ENABLE_SYNC", False),
        timeout=_env_int("MYBOARD_API_TIMEOUT", 20),
        verify_ssl=_env_bool("MYBOARD_API_VERIFY_SSL", True),
    )


def masked_config_for_ui(config: MyBoardConfig | None = None) -> dict[str, Any]:
    cfg = config or get_myboard_config()
    return {
        "enabled": cfg.enable_sync,
        "api_url": cfg.api_url,
        "api_token_masked": "••••••••" if cfg.api_token else "не задан",
        "webhook_secret_masked": "••••••••" if cfg.webhook_secret else "не задан",
        "api_ready": cfg.ready_for_api,
        "webhook_ready": cfg.ready_for_webhook,
    }


class MyBoardClient:
    def __init__(self, config: MyBoardConfig | None = None):
        self.config = config or get_myboard_config()

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.config.ready_for_api:
            raise MyBoardApiError("Интеграция MyBoard выключена или не настроены MYBOARD_API_URL/MYBOARD_API_TOKEN")

        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = f"{self.config.api_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_token}",
            },
            method="GET",
        )
        context = None
        if not self.config.verify_ssl:
            context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout, context=context) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            messages = {
                401: "Ошибка авторизации, проверьте API Token",
                403: "Нет доступа, проверьте scope токена (нужен published_boards:read)",
                404: "Сценарий не найден в MyBoard",
            }
            raise MyBoardApiError(messages.get(e.code, f"MyBoard вернул HTTP {e.code}"), e.code) from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            raise MyBoardApiError("MyBoard недоступен, попробуйте позже") from e

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise MyBoardApiError("Неверный формат ответа от MyBoard") from e

    def list_boards(self, updated_after: str | None = None,
                    cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        payload = self._request_json(
            "/api/v1/boards",
            {
                "status": "published",
                "updated_after": updated_after,
                "cursor": cursor,
                "limit": limit,
            },
        )
        if not isinstance(payload, dict):
            raise MyBoardApiError("Неверный формат ответа от MyBoard")
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise MyBoardApiError("Неверный формат ответа от MyBoard")
        return {"items": items, "next_cursor": payload.get("next_cursor")}

    def list_all_boards(self, updated_after: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        cursor = None
        boards: list[dict[str, Any]] = []
        while True:
            page = self.list_boards(updated_after=updated_after, cursor=cursor, limit=limit)
            boards.extend([item for item in page["items"] if isinstance(item, dict)])
            cursor = page.get("next_cursor")
            if not cursor:
                return boards

    def export_board(self, board_id: str) -> dict[str, Any]:
        payload = self._request_json(f"/api/v1/boards/{urllib.parse.quote(str(board_id), safe='')}/export")
        if not isinstance(payload, dict):
            raise MyBoardApiError("Неверный формат ответа от MyBoard")
        return payload


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _node_source_type(raw_node: dict[str, Any]) -> str:
    if _truthy(raw_node.get("isStart")):
        return "start"
    for key in ("type", "helperType", "scenarioType", "scriptType"):
        value = _text(raw_node.get(key))
        if value:
            return value
    return "unknown"


def convert_myboard_export(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return {"ok": False, "errors": ["невозможно распарсить JSON"], "warnings": warnings}

    title = _text(payload.get("title"))
    if not title:
        title = "Без названия"
        warnings.append('У сценария не было title, использовано "Без названия"')

    description = _text(payload.get("description"))
    if "description" not in payload:
        warnings.append("У сценария не было description, оставлено пустое значение")

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("нет поля nodes или nodes пустой")
        raw_nodes = []

    node_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    start_count = 0
    nodes: list[dict[str, Any]] = []
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            errors.append(f"узел #{index + 1} имеет неверный формат")
            continue
        source_node_id = _text(raw_node.get("id"))
        if not source_node_id:
            source_node_id = f"generated_node_{index + 1}"
            warnings.append(f"У узла #{index + 1} не было id, сгенерирован {source_node_id}")
        if source_node_id in node_ids:
            duplicate_ids.add(source_node_id)
        node_ids.add(source_node_id)

        source_type = _node_source_type(raw_node)
        helper_type = MYBOARD_TYPE_MAP.get(source_type, "unknown")
        if helper_type == "unknown":
            warnings.append(f'Узел {source_node_id} имеет неизвестный тип "{source_type}"')
        if helper_type == "start":
            start_count += 1

        x = _number(raw_node.get("x"))
        y = _number(raw_node.get("y"))
        if x is None or y is None:
            warnings.append(f"У узла {source_node_id} некорректные координаты, использовано 0, 0")
            x = 0
            y = 0

        text = _text(raw_node.get("text") if "text" in raw_node else raw_node.get("content"))
        node_title = _text(raw_node.get("title")) or source_type
        nodes.append({
            "source_node_id": source_node_id,
            "node_type": helper_type,
            "title": node_title,
            "text": text,
            "x": x,
            "y": y,
            "is_root": helper_type == "start",
            "source_type": source_type if helper_type == "unknown" else "",
            "raw_data": _json_dumps(raw_node) if helper_type == "unknown" else "",
            "final_answer": text if helper_type == "end" else "",
        })

    if duplicate_ids:
        errors.append("дублируются node.id: " + ", ".join(sorted(duplicate_ids)))
    if start_count == 0:
        errors.append("нет узла с type = start")
    if start_count > 1:
        errors.append("должен быть ровно один узел с type = start")

    raw_edges = payload.get("edges") or []
    if not isinstance(raw_edges, list):
        errors.append("поле edges имеет неверный формат")
        raw_edges = []

    edge_ids: set[str] = set()
    edges: list[dict[str, Any]] = []
    for index, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, dict):
            warnings.append(f"Связь #{index + 1} имеет неверный формат и пропущена")
            continue
        source_edge_id = _text(raw_edge.get("id"))
        if not source_edge_id:
            source_edge_id = f"generated_edge_{index + 1}"
            warnings.append(f"У связи #{index + 1} не было edge.id, сгенерирован {source_edge_id}")
        if source_edge_id in edge_ids:
            source_edge_id = f"{source_edge_id}_{index + 1}"
            warnings.append(f"У связи #{index + 1} дублировался edge.id, использован {source_edge_id}")
        edge_ids.add(source_edge_id)

        from_id = _text(raw_edge.get("from"))
        to_id = _text(raw_edge.get("to"))
        if from_id not in node_ids or to_id not in node_ids:
            errors.append(f"edge.from или edge.to указывает на несуществующий node.id: {source_edge_id}")
            continue
        edges.append({
            "source_edge_id": source_edge_id,
            "from": from_id,
            "to": to_id,
            "label": _text(raw_edge.get("label")),
            "condition": _text(raw_edge.get("condition")),
        })

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "myboard_id": _text(payload.get("id")),
        "title": title,
        "description": description,
        "status": _text(payload.get("status")) or "published",
        "version": int(payload.get("version") or 0) if str(payload.get("version") or "").isdigit() else None,
        "updated_at": _text(payload.get("updated_at")),
        "nodes": nodes,
        "edges": edges,
        "raw": payload,
    }


def verify_webhook_signature(secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.strip(), f"sha256={expected}")


def parse_webhook_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_webhook_timestamp_fresh(value: str | None, max_age_seconds: int = 300) -> bool:
    parsed = parse_webhook_timestamp(value)
    if not parsed:
        return False
    delta = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    return delta <= max_age_seconds
