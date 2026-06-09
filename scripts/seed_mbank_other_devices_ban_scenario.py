#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Создать/обновить сценарий консультации "Снятие запрета «Вход с других устройств»".

Запуск из корня проекта:
    python3 scripts/seed_mbank_other_devices_ban_scenario.py

Для другой базы:
    HELPER_SCENARIO_DB=/path/to/topics.db python3 scripts/seed_mbank_other_devices_ban_scenario.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from scenario_manager import ScenarioManager  # noqa: E402


TITLE = "Снятие запрета «Вход с других устройств»"
DESCRIPTION = (
    "Сценарий обработки обращения клиента по снятию запрета входа с других устройств "
    "в MBANK: идентификация, верификация, согласие клиента и фиксация результата в CRM."
)
TAGS = "MBANK, вход с других устройств, запрет, ARM, CRM, MJunior, верификация"


def _db_path() -> str:
    return os.environ.get("HELPER_SCENARIO_DB") or str(ROOT_DIR / "topics.db")


def ensure_category(manager: ScenarioManager) -> int:
    for category in manager.get_categories():
        if (category.get("name") or "").strip().lower() == "mbank":
            return int(category["id"])
    return manager.create_category("MBANK", "🔐")


def reset_or_create_scenario(manager: ScenarioManager, category_id: int) -> int:
    existing = None
    for scenario in manager.get_all_scenarios_admin():
        if (scenario.get("title") or "").strip().lower() == TITLE.lower():
            existing = scenario
            break

    if not existing:
        return manager.create_scenario(
            title=TITLE,
            description=DESCRIPTION,
            category_id=category_id,
            tags=TAGS,
            created_by="seed",
        )

    scenario_id = int(existing["id"])
    with manager._connect() as conn:
        conn.execute("DELETE FROM cs_edges WHERE scenario_id=?", (scenario_id,))
        conn.execute("DELETE FROM cs_nodes WHERE scenario_id=?", (scenario_id,))
        conn.execute(
            """
            UPDATE cs_scenarios
            SET title=?, description=?, category_id=?, tags=?,
                status='draft', updated_by='seed', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (TITLE, DESCRIPTION, category_id, TAGS, scenario_id),
        )
        conn.commit()
    return scenario_id


def build_scenario(manager: ScenarioManager, scenario_id: int) -> dict[str, int]:
    nodes: dict[str, int] = {}

    def add(
        key: str,
        node_type: str,
        title: str,
        content: str = "",
        *,
        final_answer: str = "",
        internal_note: str = "",
        x: int = 0,
        y: int = 0,
        root: bool = False,
    ) -> None:
        node_id = manager.create_node(
            scenario_id=scenario_id,
            node_type=node_type,
            title=title,
            content=content,
            is_root=root,
        )
        manager.update_node(
            node_id,
            {
                "final_answer": final_answer,
                "internal_note": internal_note,
                "pos_x": x,
                "pos_y": y,
            },
        )
        nodes[key] = node_id

    add(
        "start",
        "start",
        "Запрос клиента",
        "Запроси данные клиента. Найди клиента в ARM.",
        x=760,
        y=80,
        root=True,
    )
    add(
        "client_type",
        "question",
        "Какой тип клиента указан в ARM?",
        "Проверь тип клиента перед переходом к CRM.",
        x=760,
        y=250,
    )
    add(
        "mjunior_parent",
        "question",
        "По MJunior обращается родитель?",
        "Уточни, обращается ли родитель клиента MJunior.",
        x=290,
        y=430,
    )
    add(
        "mjunior_refuse",
        "final",
        "Отказать в обслуживании MJunior без родителя",
        "Обслуживание не выполнять. Оформить тематику в CRM:\n"
        "SR1: MJunior\n"
        "SR2: Обращение ребенка\n"
        "SR3: Отказать в обслуживании\n"
        "SR4: Просьба обратиться родителя",
        final_answer=(
            "По продукту MJunior обслуживание по этому вопросу возможно только при обращении "
            "родителя. Пожалуйста, попросите родителя обратиться к нам."
        ),
        internal_note="Исключение: MJunior без обращения родителя не проходит дальше к верификации.",
        x=40,
        y=610,
    )
    add(
        "find_crm",
        "info",
        "Найди клиента в CRM",
        "Найди карточку клиента в CRM. После этого переходи к процедуре верификации.",
        x=760,
        y=520,
    )
    add(
        "verify",
        "info",
        "Проведи верификацию клиента",
        "Проведи верификацию по доступным способам:\nкодовое слово;\nПД;\nТундук.",
        x=760,
        y=700,
    )
    add(
        "verification_passed",
        "question",
        "Верификация пройдена?",
        "Проверь результат верификации перед снятием запрета.",
        x=760,
        y=890,
    )
    add(
        "verification_failed",
        "final",
        "Отказать: идентификация не пройдена",
        "Отказать в обслуживании. Рекомендовать селф-сервис. Оформить тематику в CRM:\n"
        "SR1: MBANK\n"
        "SR2: Вход с других устройств\n"
        "SR3: Как снять запрет\n"
        "SR4: Идентификация не пройдена",
        final_answer=(
            "К сожалению, мы не можем снять запрет без успешной идентификации. "
            "Рекомендуем воспользоваться самостоятельным способом снятия запрета через селф-сервис."
        ),
        internal_note="Не снимай запрет в ARM, если клиент не прошел обязательную верификацию.",
        x=320,
        y=1080,
    )
    add(
        "warn_risks",
        "info",
        "Предупреди клиента о рисках",
        "Сообщи клиенту о рисках снятия запрета: после отключения вход с других устройств станет возможен.",
        x=960,
        y=1080,
    )
    add(
        "client_agrees",
        "question",
        "Клиент согласен снять запрет?",
        "Уточни согласие клиента после предупреждения о рисках.",
        x=960,
        y=1260,
    )
    add(
        "client_refused",
        "final",
        "Клиент не согласен снять запрет",
        "Завершить обращение без изменений. Рекомендовать селф-сервис. Оформить тематику в CRM:\n"
        "SR1: MBANK\n"
        "SR2: Вход с других устройств\n"
        "SR3: Как снять запрет\n"
        "SR4: Не знает как выключить",
        final_answer=(
            "Понимаю. Запрет оставляем включенным. Если захотите снять его самостоятельно, "
            "воспользуйтесь селф-сервисом."
        ),
        internal_note="Запрет в ARM не менять, если клиент не подтвердил согласие на снятие.",
        x=620,
        y=1450,
    )
    add(
        "remove_ban",
        "info",
        "Сними запрет в ARM",
        "Сними запрет «Вход с других устройств» в ARM.",
        x=1130,
        y=1450,
    )
    add(
        "success",
        "final",
        "Запрет успешно снят",
        "Оформить тематику в CRM:\n"
        "SR1: MBANK\n"
        "SR2: Вход с других устройств\n"
        "SR3: Как снять запрет\n"
        "SR4: Успешно снят",
        final_answer=(
            "Запрет на вход с других устройств успешно снят. В дальнейшем рекомендуем использовать "
            "самостоятельный способ снятия запрета через селф-сервис."
        ),
        internal_note="Перед снятием запрета обязательны успешная верификация и согласие клиента.",
        x=1130,
        y=1630,
    )

    return nodes


def build_edges(manager: ScenarioManager, scenario_id: int, n: dict[str, int]) -> None:
    def edge(src: str, dst: str, label: str = "") -> None:
        manager.create_edge(scenario_id, n[src], n[dst], label)

    edge("start", "client_type")
    edge("client_type", "find_crm", "Классический клиент")
    edge("client_type", "find_crm", "Идентифицированный по видео")
    edge("client_type", "mjunior_parent", "MJunior")
    edge("mjunior_parent", "find_crm", "Да, родитель")
    edge("mjunior_parent", "mjunior_refuse", "Нет, не родитель")
    edge("find_crm", "verify")
    edge("verify", "verification_passed")
    edge("verification_passed", "warn_risks", "Да")
    edge("verification_passed", "verification_failed", "Нет")
    edge("warn_risks", "client_agrees")
    edge("client_agrees", "remove_ban", "Да, согласен")
    edge("client_agrees", "client_refused", "Нет, не согласен")
    edge("remove_ban", "success")


def main() -> None:
    manager = ScenarioManager(_db_path())
    category_id = ensure_category(manager)
    scenario_id = reset_or_create_scenario(manager, category_id)
    nodes = build_scenario(manager, scenario_id)
    build_edges(manager, scenario_id, nodes)
    manager.publish_scenario(scenario_id, updated_by="seed")
    print(f"Сценарий обновлён: id={scenario_id}, title={TITLE!r}, nodes={len(nodes)}")


if __name__ == "__main__":
    main()
