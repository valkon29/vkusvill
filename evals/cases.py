"""Определение eval-кейсов: промпт(ы) + детерминированные проверки.

Проверки получают лог событий (включая tool_result с полными результатами
инструментов) и финальную историю сообщений; возвращают список строк-
нарушений (пустой список = кейс пройден).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from app.mcp_client import MCPClientManager

Check = Callable[[list[tuple[str, dict]], list[dict]], list[str]]


@dataclass
class EvalCase:
    name: str
    description: str
    prompts: list[str]  # многоходовой кейс = несколько промптов подряд
    checks: list[Check]
    # опциональная подмена MCP (кейс «источник недоступен»)
    mcp_factory: Callable[[], MCPClientManager] | None = None


# ---------- вспомогательные выборки из лога событий ----------

def tool_results(events, name):
    return [d["result"] for t, d in events if t == "tool_result" and d["name"] == name]


def cart_events(events):
    return [d for t, d in events if t == "cart"]


def tool_names(events):
    return [d["name"] for t, d in events if t == "tool"]


def final_answer(events):
    return "".join(d["text"] for t, d in events if t == "token").strip()


def product_ids_from_tools(events):
    """Все id товаров, которые реально приходили из инструментов."""
    ids = set()
    for result in tool_results(events, "search_products"):
        try:
            ids.update(i["id"] for i in json.loads(result)["items"])
        except Exception:
            pass
    for result in tool_results(events, "find_product_analogs"):
        try:
            ids.update(i["id"] for i in json.loads(result))
        except Exception:
            pass
    return ids


# ---------- проверки ----------

def check_search_before_recommendation(events, messages):
    """LLM не называет товары, которых нет в результатах инструментов."""
    errors = []
    ids = product_ids_from_tools(events)
    for cart in cart_events(events):
        for item in cart.get("items", []):
            if item["id"] not in ids:
                errors.append(f"в корзине товар {item['id']} ({item['name']}), "
                              f"которого не было в результатах инструментов")
    return errors


def check_cart_url(events, messages):
    errors = []
    for cart in cart_events(events):
        if not re.match(r"^https://vkusvill\.ru/", cart.get("url", "")):
            errors.append(f"подозрительная ссылка на корзину: {cart.get('url')}")
    return errors


def check_budget(limit):
    def check(events, messages):
        errors = []
        for cart in cart_events(events):
            if (cart.get("total") or 0) > limit:
                errors.append(f"сумма корзины {cart['total']} ₽ превышает бюджет {limit} ₽")
        return errors
    return check


def check_composition_excludes(substr):
    """Ни один товар корзины не содержит запрещённый ингредиент в составе."""
    def check(events, messages):
        errors = []
        details = {}
        for result in tool_results(events, "get_product_details"):
            try:
                d = json.loads(result)
                details[d["id"]] = (d.get("composition") or "") + " " + (d.get("name") or "")
            except Exception:
                pass
        for cart in cart_events(events):
            for item in cart.get("items", []):
                text = details.get(item["id"], "").lower()
                if substr in text:
                    errors.append(f"товар «{item.get('name')}» содержит «{substr}», "
                                  f"хотя это запрещено по условию")
        return errors
    return check


def check_tool_called(tool):
    def check(events, messages):
        return [] if tool in tool_names(events) else [f"инструмент {tool} не вызывался"]
    return check


def check_answer_nonempty(events, messages):
    return [] if final_answer(events) else ["пустой финальный ответ"]


def check_no_cart_when_empty_result(events, messages):
    """На заведомо несуществующий товар не должно быть корзины."""
    return ([] if not cart_events(events)
            else ["собрана корзина на несуществующий товар"])


def check_graceful_failure(events, messages):
    """При недоступном MCP: без исключений, ответ есть, упомянута проблема."""
    errors = []
    if any(t == "error" for t, _ in events):
        errors.append("агент упал с ошибкой вместо вежливого ответа")
    answer = final_answer(events).lower()
    if not any(w in answer for w in ("извин", "не удалось", "недоступ", "сбой", "ошибк", "попробуй")):
        errors.append(f"ответ не признаёт проблему: {final_answer(events)[:200]!r}")
    return errors


def check_details_before_cart_when_dietary(events, messages):
    """При пищевом ограничении состав проверяется до сборки корзины."""
    names = tool_names(events)
    if "create_cart" in names:
        if "get_product_details" not in names:
            return ["корзина собрана без проверки составов при ограничении"]
    return []


# ---------- кейсы ----------

CASES = [
    EvalCase(
        name="budget_snack",
        description="«перекус на работу, бюджет 500 ₽, без лактозы»: поиск, "
                    "проверка состава, корзина в бюджете",
        prompts=["Собери перекус на работу, бюджет 500 ₽, без лактозы. Сразу собери корзину."],
        checks=[
            check_tool_called("search_products"),
            check_budget(500),
            check_composition_excludes("лактоз"),
            check_search_before_recommendation,
            check_cart_url,
            check_answer_nonempty,
        ],
    ),
    EvalCase(
        name="recipe_dinner",
        description="«ужин из курицы до 40 минут»: рецепт из результатов инструментов",
        prompts=["Что приготовить на ужин из курицы, максимум 40 минут?"],
        checks=[
            check_tool_called("find_recipes"),
            check_answer_nonempty,
        ],
    ),
    EvalCase(
        name="discounts",
        description="«молочное со скидками»: только товары со скидкой",
        prompts=["Покажи молочные товары со скидками"],
        checks=[
            check_tool_called("search_discounted_products"),
            check_answer_nonempty,
        ],
    ),
    EvalCase(
        name="empty_result",
        description="«драконье яйцо»: честный отказ, без выдуманных товаров",
        prompts=["Купи драконье яйцо"],
        checks=[
            check_no_cart_when_empty_result,
            check_answer_nonempty,
        ],
    ),
    EvalCase(
        name="dietary_breakfast",
        description="«завтрак без лактозы»: состав проверен до корзины",
        prompts=["Подбери мне завтрак без лактозы и собери корзину"],
        checks=[
            check_tool_called("search_products"),
            check_details_before_cart_when_dietary,
            check_composition_excludes("лактоз"),
            check_search_before_recommendation,
            check_cart_url,
            check_answer_nonempty,
        ],
    ),
    EvalCase(
        name="clarification",
        description="«хочу поесть»: уточняющий вопрос или разумное предложение",
        prompts=["Хочу поесть"],
        checks=[
            check_answer_nonempty,
            lambda events, messages: []
            if ("?" in final_answer(events) or any(t in ("products", "cart") for t, _ in events))
            else ["нет ни уточняющего вопроса, ни предложения товаров"],
        ],
    ),
]


class FailingMCP:
    """Заглушка для кейса «источник недоступен»: все вызовы падают."""

    async def call_tool(self, name, arguments):
        raise ConnectionError("MCP server unavailable ( simulated )")


SOURCE_DOWN_CASE = EvalCase(
    name="source_down",
    description="MCP недоступен: вежливое сообщение, без падения",
    prompts=["Найди сырники"],
    checks=[check_graceful_failure],
    mcp_factory=lambda: FailingMCP(),  # type: ignore[assignment]
)

ALL_CASES = CASES + [SOURCE_DOWN_CASE]
