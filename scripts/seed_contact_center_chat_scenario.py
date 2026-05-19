#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Создать/обновить сценарий консультации "Поступил новый чат".

Запуск из корня проекта:
    python3 scripts/seed_contact_center_chat_scenario.py

Для другой базы:
    HELPER_SCENARIO_DB=/path/to/topics.db python3 scripts/seed_contact_center_chat_scenario.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from scenario_manager import ScenarioManager  # noqa: E402


TITLE = "Поступил новый чат"
DESCRIPTION = "Операционный сценарий обработки нового чата: уточнение потребности, верификация, ответ и завершение консультации."
TAGS = "чат, консультация, верификация, ответ клиенту, КЦ"


def _db_path() -> str:
    return os.environ.get("HELPER_SCENARIO_DB") or str(ROOT_DIR / "topics.db")


def ensure_category(manager: ScenarioManager) -> int:
    for category in manager.get_categories():
        if (category.get("name") or "").strip().lower() == "чаты":
            return int(category["id"])
    return manager.create_category("Чаты", "💬")


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

    # Верх процесса.
    add("start", "start", "Поступил новый чат", "Начало обработки нового обращения клиента.", x=900, y=60, root=True)
    add("greet", "info", "Отправь приветствие", "Поприветствуй клиента по стандарту.", x=910, y=210)
    add("read_history", "info", "Ознакомься с историей чата", "Прочитай предыдущие сообщения и контекст обращения.", x=910, y=350)
    add("priority", "info", "Определи приоритет обращения", "Оцени срочность и влияние проблемы клиента.", x=910, y=490)
    add(
        "same_priority",
        "question",
        "В работе есть чат с таким же приоритетом или выше?",
        "Если да, вернись к обработке нового чата позже.",
        x=900,
        y=650,
    )
    add(
        "return_later",
        "final",
        "Вернись к новому чату позже",
        final_answer="Продолжи обработку текущего более приоритетного чата. К новому чату вернись сразу после освобождения.",
        x=1230,
        y=650,
    )
    add("need_clear", "question", "Потребность клиента ясна?", "Понятно, какую проблему нужно решить?", x=900, y=840)

    # Уточнение потребности.
    add("asked_question", "question", "Клиент задал вопрос?", "Есть ли в сообщении клиента конкретный вопрос?", x=620, y=1020)
    add("clarify_help", "info", "Уточни, чем можешь помочь", "Спроси клиента, с каким вопросом или проблемой он обратился.", x=350, y=1180)
    add("client_replied_1", "question", "Клиент ответил?", "Клиент дал новую информацию?", x=350, y=1340)
    add("request_feedback_1", "info", "Запроси ОС", "Запроси обратную связь и дождись ответа клиента.", x=120, y=1500)
    add("client_replied_1b", "question", "Клиент ответил?", "Клиент ответил после запроса обратной связи?", x=120, y=1660)

    add("question_clear", "question", "Вопрос понятен полностью?", "Достаточно ли информации, чтобы перейти к решению?", x=650, y=1220)
    add("ask_clarifying", "info", "Задай уточняющие вопросы", "Сформулируй уточняющие вопросы, чтобы полностью понять ситуацию.", x=570, y=1380)
    add("client_replied_2", "question", "Клиент ответил?", "Клиент ответил на уточняющие вопросы?", x=570, y=1540)
    add("request_feedback_2", "info", "Запроси ОС", "Запроси обратную связь и дождись ответа клиента.", x=350, y=1700)
    add("client_replied_2b", "question", "Клиент ответил?", "Клиент ответил после запроса обратной связи?", x=350, y=1860)
    add("partial_answer", "question", "Можно ли дать частичный ответ?", "Можно ли помочь клиенту частично, пока нет всех данных?", x=570, y=2020)

    # Верификация.
    add("verification_ok", "question", "Достаточный уровень верификации?", "Достаточно ли подтверждена личность/право клиента на обслуживание?", x=930, y=1180)
    add("verify_questions", "info", "Задай вопросы для верификации", "Задай вопросы, необходимые для подтверждения личности клиента.", x=930, y=1380)
    add("client_replied_ver", "question", "Клиент ответил?", "Клиент ответил на вопросы для верификации?", x=930, y=1540)
    add("request_feedback_ver", "info", "Запроси ОС", "Запроси обратную связь и дождись ответа клиента.", x=710, y=1700)
    add("client_replied_ver2", "question", "Клиент ответил?", "Клиент ответил после запроса обратной связи?", x=710, y=1860)
    add("answer_correct", "question", "Ответ правильный?", "Ответ клиента прошёл проверку?", x=930, y=1700)
    add("higher_verification", "question", "Есть более высокий уровень верификации?", "Можно ли продолжить верификацию на более высоком уровне?", x=930, y=1860)
    add(
        "refuse_service",
        "final",
        "Отказывай в обслуживании",
        final_answer="К сожалению, мы не можем продолжить обслуживание без достаточной верификации. Обратитесь повторно после подтверждения данных.",
        internal_note="Используй этот вариант только если клиент не прошёл обязательную верификацию.",
        x=700,
        y=2040,
    )

    # Подготовка и предоставление ответа.
    add("answer_5min", "question", "Успеваешь ответить за 5 минут?", "Сможешь подготовить корректный ответ за 5 минут?", x=1210, y=1380)
    add("ack_ready", "info", "Обозначь, что понял вопрос, и готовишь ответ", "Сообщи клиенту, что вопрос понятен и ты готовишь ответ.", x=1210, y=1540)
    add(
        "ack_10min",
        "info",
        "Обозначь, что понял вопрос, и готовишь ответ + обозначь срок",
        "Сообщи клиенту, что вопрос понятен, и обозначь срок подготовки ответа: максимум 10 минут.",
        x=1030,
        y=1540,
    )
    add("deadline_ok", "question", "Успеваешь ответить в срок выше?", "Успеваешь дать ответ в обозначенный срок?", x=1210, y=1720)
    add("ask_more_time", "info", "Извинись, попроси больше времени", "Извинись перед клиентом и попроси дополнительное время.", x=990, y=1880)
    add("solve_case", "info", "Реши вопрос/предоставь ответ/эскалируй кейс", "Дай решение, предоставь ответ или передай кейс на нужный уровень.", x=1210, y=1880)
    add("ask_more_help", "info", "Уточни, можешь ли ещё чем-то помочь", "Спроси клиента, остались ли дополнительные вопросы.", x=1210, y=2040)
    add("client_replied_after", "question", "Клиент ответил?", "Клиент ответил после уточнения?", x=1210, y=2200)
    add("remaining_questions", "question", "У клиента остались вопросы?", "Есть ли у клиента дополнительные вопросы?", x=1210, y=2360)

    # Финалы.
    add(
        "goodbye",
        "final",
        "Попрощайся с клиентом",
        final_answer="Спасибо за обращение! Если появятся вопросы — мы всегда готовы помочь.",
        x=930,
        y=2520,
    )

    return nodes


def build_edges(manager: ScenarioManager, scenario_id: int, n: dict[str, int]) -> None:
    def edge(src: str, dst: str, label: str = "") -> None:
        manager.create_edge(scenario_id, n[src], n[dst], label)

    edge("start", "greet")
    edge("greet", "read_history")
    edge("read_history", "priority")
    edge("priority", "same_priority")
    edge("same_priority", "return_later", "Да")
    edge("same_priority", "need_clear", "Нет")

    edge("need_clear", "verification_ok", "Да")
    edge("need_clear", "asked_question", "Нет")

    edge("asked_question", "clarify_help", "Нет")
    edge("asked_question", "question_clear", "Да")
    edge("clarify_help", "client_replied_1")
    edge("client_replied_1", "question_clear", "Да")
    edge("client_replied_1", "request_feedback_1", "Нет")
    edge("request_feedback_1", "client_replied_1b")
    edge("client_replied_1b", "question_clear", "Да")
    edge("client_replied_1b", "goodbye", "Нет")

    edge("question_clear", "verification_ok", "Да")
    edge("question_clear", "ask_clarifying", "Нет")
    edge("ask_clarifying", "client_replied_2")
    edge("client_replied_2", "question_clear", "Да")
    edge("client_replied_2", "request_feedback_2", "Нет")
    edge("request_feedback_2", "client_replied_2b")
    edge("client_replied_2b", "question_clear", "Да")
    edge("client_replied_2b", "partial_answer", "Нет")
    edge("partial_answer", "verification_ok", "Да")
    edge("partial_answer", "goodbye", "Нет")

    edge("verification_ok", "answer_5min", "Да")
    edge("verification_ok", "verify_questions", "Нет")
    edge("verify_questions", "client_replied_ver")
    edge("client_replied_ver", "answer_correct", "Да")
    edge("client_replied_ver", "request_feedback_ver", "Нет")
    edge("request_feedback_ver", "client_replied_ver2")
    edge("client_replied_ver2", "answer_correct", "Да")
    edge("client_replied_ver2", "goodbye", "Нет")
    edge("answer_correct", "higher_verification", "Да")
    edge("answer_correct", "verification_ok", "Нет")
    edge("higher_verification", "ack_10min", "Да")
    edge("higher_verification", "refuse_service", "Нет")

    edge("answer_5min", "ack_ready", "Да")
    edge("answer_5min", "ack_10min", "Нет")
    edge("ack_ready", "deadline_ok")
    edge("ack_10min", "deadline_ok")
    edge("deadline_ok", "solve_case", "Да")
    edge("deadline_ok", "ask_more_time", "Нет")
    edge("ask_more_time", "deadline_ok")
    edge("solve_case", "ask_more_help")
    edge("ask_more_help", "client_replied_after")
    edge("client_replied_after", "remaining_questions", "Да")
    edge("client_replied_after", "goodbye", "Нет")
    edge("remaining_questions", "need_clear", "Да")
    edge("remaining_questions", "goodbye", "Нет")


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
