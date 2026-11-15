"""Определение eval-кейсов: промпт(ы) + детерминированные проверки.

Проверки получают лог событий (включая tool с аргументами вызова и
tool_result с полными результатами инструментов) и финальную историю
сообщений; возвращают список строк-нарушений (пустой список = кейс пройден).

Важное правило честности проверок: если кейс требует корзину, а корзина
не собрана — это НАРУШЕНИЕ, а не «цикл по пустому списку прошёл впустую».
Раннер размечает ходы диалога событиями ("turn", {"prompt": ...}).
"""

import json
import re
from dataclasses import dataclass
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


def turns(events) -> list[list[tuple[str, dict]]]:
    """Нарезает лог по ходам диалога: границы — события ("turn", ...),
    которые ставит раннер перед каждым промптом. Без маркеров весь
    лог считается одним ходом."""
    result: list[list[tuple[str, dict]]] = [[]]
    for ev in events:
        if ev[0] == "turn":
            result.append([])
        else:
            result[-1].append(ev)
    non_empty = [t for t in result if t]
    return non_empty or [[]]


def final_cart(events) -> dict | None:
    carts = cart_events(events)
    return carts[-1] if carts else None


def _details_parsed(events) -> dict[int, dict]:
    """id → распарсенный результат get_product_details (состав, КБЖУ, вес)."""
    details: dict[int, dict] = {}
    for result in tool_results(events, "get_product_details"):
        try:
            d = json.loads(result)
            if isinstance(d, dict) and "id" in d:
                details[d["id"]] = d
        except Exception:
            pass
    return details


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


check_search_before_recommendation.description = (
    "все id товаров корзины — из результатов search_products / "
    "find_product_analogs (анти-галлюцинации)"
)


def check_cart_url(events, messages):
    errors = []
    for cart in cart_events(events):
        if not re.match(r"^https://vkusvill\.ru/", cart.get("url", "")):
            errors.append(f"подозрительная ссылка на корзину: {cart.get('url')}")
    return errors


check_cart_url.description = "ссылка каждой корзины ведёт на vkusvill.ru"


def check_cart_present(events, messages):
    """Корзина обязана быть собрана — иначе остальные проверки корзины
    пройдут впустую (цикл по пустому списку)."""
    return [] if cart_events(events) else ["корзина не собрана (событие cart отсутствует)"]


check_cart_present.description = (
    "корзина собрана (событие cart есть) — без неё проверки корзины "
    "не имеют смысла"
)


def check_budget(limit):
    def check(events, messages):
        carts = cart_events(events)
        if not carts:
            return [f"корзина не собрана — бюджет {limit} ₽ не проверен"]
        errors = []
        for cart in carts:
            if (cart.get("total") or 0) > limit:
                errors.append(f"сумма корзины {cart['total']} ₽ превышает бюджет {limit} ₽")
        return errors
    check.description = f"сумма каждой собранной корзины ≤ {limit} ₽; без корзины — нарушение"
    return check


def check_composition_excludes(substr):
    """Ни один товар корзины не содержит запрещённый ингредиент в составе.
    Товар без вызова get_product_details — тоже нарушение: непроверенный
    состав при ограничении недопустим. Формулировки «без <ингредиента>»,
    «не содержит <ингредиента>», «<ингредиента> нет» — наоборот, означают,
    что продукт подходит (ложное срабатывание на «безлактозном молоке»
    найдено живым прогоном и закрыто тестом)."""
    def check(events, messages):
        carts = cart_events(events)
        if not carts:
            return [f"корзина не собрана — наличие «{substr}» не проверено"]
        details = _details_parsed(events)
        errors = []
        quoted = re.escape(substr)
        negation = re.compile(
            rf"(?:не\s+содерж[а-яё]*|без|нет)\s*[- ]?{quoted}\w*"
            rf"|{quoted}\w*\s+(?:нет|отсутств[а-яё]*|не\s+обнаружен[а-яё]*)",
            re.IGNORECASE,
        )
        for cart in carts:
            for item in cart.get("items", []):
                d = details.get(item["id"])
                if d is None:
                    errors.append(
                        f"состав товара «{item.get('name')}» (id={item['id']}) "
                        f"не проверен через get_product_details"
                    )
                    continue
                text = ((d.get("composition") or "") + " " + (d.get("name") or "")).lower()
                if substr in negation.sub(" ", text):
                    errors.append(f"товар «{item.get('name')}» содержит «{substr}», "
                                  f"хотя это запрещено по условию")
        return errors
    check.description = (
        f"ни один товар корзины не содержит «{substr}» в составе; состав каждого "
        f"товара проверен через get_product_details; «без {substr}» / «не содержит "
        f"{substr}» — подходит"
    )
    return check


def check_tool_called(tool):
    def check(events, messages):
        return [] if tool in tool_names(events) else [f"инструмент {tool} не вызывался"]
    check.description = f"инструмент {tool} вызывался"
    return check


def check_answer_nonempty(events, messages):
    return [] if final_answer(events) else ["пустой финальный ответ"]


check_answer_nonempty.description = "финальный ответ не пуст"


def check_cart_min_items(n):
    def check(events, messages):
        cart = final_cart(events)
        if cart is None:
            return ["корзина не собрана — нечего проверять"]
        count = len(cart.get("items", []))
        return [] if count >= n else [
            f"в корзине {count} позиций — для «полноценного обеда» ожидалось не менее {n}"
        ]
    check.description = f"в финальной корзине не менее {n} позиций"
    return check


def check_replacement(turn_no, removed_sub, added_sub):
    """Ход-замена (1-индексный номер хода): added_sub появляется в корзине,
    removed_sub исчезает (если был), а все прочие позиции предыдущей корзины
    остаются — инвариант накопления проверяется на живых данных."""
    def check(events, messages):
        by_turn = turns(events)
        if len(by_turn) < turn_no:
            return [f"ходов меньше {turn_no} — замену проверить нельзя"]
        prev_carts = cart_events(by_turn[turn_no - 2])
        new_carts = cart_events(by_turn[turn_no - 1])
        if not prev_carts:
            return ["в предыдущем ходе корзина не собрана — замену проверить нельзя"]
        if not new_carts:
            return ["в ходе замены корзина не пересобрана"]
        prev, new = prev_carts[-1], new_carts[-1]
        errors = []
        # имена для сопоставления — в нижнем регистре, для сообщений — как есть
        prev_items = {i["id"]: (i.get("name") or "") for i in prev.get("items", [])}
        new_items = {i["id"]: (i.get("name") or "") for i in new.get("items", [])}
        prev_names = {pid: n.lower() for pid, n in prev_items.items()}
        new_names = {pid: n.lower() for pid, n in new_items.items()}
        if not any(added_sub in n for n in new_names.values()):
            errors.append(f"«{added_sub}» не появился в корзине после замены")
        # остаток ищем только среди позиций ПРЕДЫДУЩЕЙ корзины: новый товар
        # может сам содержать слово замены («Салат винегрет…») — это не остаток
        leftover = [prev_items[pid] for pid in prev_names
                    if removed_sub in prev_names[pid] and pid in new_names]
        if leftover:
            errors.append(f"«{removed_sub}» остался в корзине после замены: "
                          f"{'; '.join(leftover)}")
        for pid, name in prev_items.items():
            if pid not in new_names and removed_sub not in name.lower():
                errors.append(f"позиция «{name}» (id={pid}) пропала из корзины "
                              f"без запроса пользователя")
        return errors
    check.description = (
        f"ход {turn_no}: «{added_sub}» появился в корзине, «{removed_sub}» исчез "
        f"(если был), остальные позиции хода {turn_no - 1} сохранились"
    )
    return check


# «углеводы: 4,7» / «Углеводы 4.7 г» — число после слова, десятичная запятая
_CARBS_RE = re.compile(r"углевод[а-яё]*\D{0,15}?(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:г|гр|мл)\b", re.IGNORECASE)


def _carbs_per_100g(detail: dict) -> float | None:
    """Углеводы на 100 г из строки КБЖУ; None — если разобрать не удалось."""
    for text in (detail.get("nutrition"), detail.get("composition")):
        if not text:
            continue
        m = _CARBS_RE.search(text)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def _weight_grams(detail: dict) -> float | None:
    """Вес упаковки в граммах; None — если не указан или не разобран."""
    w = detail.get("weight")
    if isinstance(w, dict):
        value, unit = w.get("value"), (w.get("unit") or "").lower()
        if isinstance(value, (int, float)) and value > 0:
            if unit in ("кг", "л"):
                return float(value) * 1000
            return float(value)  # г, мл или без единицы
        return None
    if isinstance(w, (int, float)) and w > 0:
        return float(w)
    if isinstance(w, str):
        m = _WEIGHT_RE.search(w)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def check_carbs_total(tolerance: float = 0.3):
    """Пересчёт суммарных углеводов по корзине из ответов ВкусВилла.

    Для каждой позиции требуем свежий get_product_details в последнем ходе;
    углеводы на 100 г парсим из КБЖУ. Сверяем с числами в финальном ответе
    два варианта: сумма «на 100 г» и сумма с учётом веса упаковок — модель
    могла считать любым из способов. Допуск покрывает округления."""
    def check(events, messages):
        cart = final_cart(events)
        if cart is None:
            return ["корзина не собрана — углеводы считать не по чему"]
        details = _details_parsed(turns(events)[-1])
        errors: list[str] = []
        s100 = 0.0
        weighted = 0.0
        weighted_ok = True
        for item in cart.get("items", []):
            name = item.get("name") or f"id={item['id']}"
            d = details.get(item["id"])
            if d is None:
                errors.append(f"КБЖУ товара «{name}» не запрошено через "
                              f"get_product_details в этом ходе")
                weighted_ok = False
                continue
            carbs = _carbs_per_100g(d)
            if carbs is None:
                errors.append(f"не удалось разобрать углеводы в КБЖУ товара «{name}»")
                weighted_ok = False
                continue
            qty = item.get("quantity") or 1
            s100 += carbs * qty
            w = _weight_grams(d)
            if w:
                weighted += carbs * (w / 100.0) * qty
            else:
                weighted_ok = False
        if errors:
            return errors
        answer = final_answer(events)
        numbers = [float(m.replace(",", ".")) for m in _NUM_RE.findall(answer)]

        def close(n: float, target: float) -> bool:
            return target * (1 - tolerance) <= n <= target * (1 + tolerance)

        # если веса упаковок известны — правильный ответ только взвешенный
        # (сколько углеводов реально в купленных упаковках); сумма «на 100 г» —
        # неверная интерпретация вопроса покупателя
        if weighted_ok and weighted > 0:
            targets, expected = [weighted], f"~{weighted:.1f} г (с учётом веса упаковок)"
        else:
            targets, expected = [s100], f"~{s100:.1f} г (на 100 г продукта — вес упаковок не указан)"
        matched = [n for n in numbers if any(close(n, t) for t in targets)]
        if not matched:
            found = ", ".join(f"{n:g}" for n in numbers) or "чисел нет"
            errors.append(f"в ответе нет суммарных углеводов, согласованных "
                          f"с пересчётом из КБЖУ: ожидалось {expected}, "
                          f"числа в ответе: {found}")
        return errors
    check.description = (
        f"по каждой позиции последней корзины запрошен свежий get_product_details; "
        f"суммарные углеводы в ответе сходятся с пересчётом (углеводы/100 г × вес "
        f"упаковки × количество, допуск ±{int(tolerance * 100)}%)"
    )
    return check


def check_no_cart_when_empty_result(events, messages):
    """На заведомо несуществующий товар не должно быть корзины."""
    return ([] if not cart_events(events)
            else ["собрана корзина на несуществующий товар"])


check_no_cart_when_empty_result.description = (
    "на несуществующий товар корзина не собирается"
)


def check_graceful_failure(events, messages):
    """При недоступном MCP: без исключений, ответ есть, упомянута проблема."""
    errors = []
    if any(t == "error" for t, _ in events):
        errors.append("агент упал с ошибкой вместо вежливого ответа")
    answer = final_answer(events).lower()
    if not any(w in answer for w in (
        "извин", "не удалось", "не удаётся", "не получается", "недоступ",
        "сбой", "ошибк", "попробуй",
    )):
        errors.append(f"ответ не признаёт проблему: {final_answer(events)[:200]!r}")
    return errors


check_graceful_failure.description = (
    "при недоступном MCP: без исключений, ответ есть, проблема признана"
)


# «итого 854 ₽» / «Сумма корзины: 850 рублей» / «всего вышло 812.50 руб»
_TOTAL_RE = re.compile(
    r"(?:итого\w*|сумм\w*|всего)[^\n\d]{0,40}(\d+(?:[.,]\d+)?)\s*(?:руб|₽)",
    re.IGNORECASE,
)


def check_answer_total_matches_cart(events, messages):
    """Каждая названная в ответе итоговая сумма сходится с фактической суммой
    корзины из события cart (ловит «фальшивые суммы», названные без вызова
    create_cart или мимо его результата — найдено живым прогоном)."""
    errors = []
    for turn in turns(events):
        answer = "".join(d["text"] for t, d in turn if t == "token")
        mentions = [float(m.group(1).replace(",", "."))
                    for m in _TOTAL_RE.finditer(answer)]
        if not mentions:
            continue
        carts = cart_events(turn)
        if not carts:
            errors.append(f"в ответе названа сумма ({', '.join(f'{m:g}' for m in mentions)} ₽), "
                          f"но корзина в этом ходе не собрана")
            continue
        total = carts[-1].get("total") or 0
        for m in mentions:
            if abs(m - total) > max(1.0, total * 0.05):
                errors.append(f"названная сумма {m:g} ₽ не сходится "
                              f"с фактической суммой корзины {total:g} ₽")
    return errors


check_answer_total_matches_cart.description = (
    "каждая названная в ответе сумма сходится с фактической суммой корзины "
    "из события cart (±5%); сумма без вызова create_cart — нарушение"
)


def check_details_before_cart_when_dietary(events, messages):
    """При пищевом ограничении состав проверяется до сборки корзины:
    в каждом ходе с create_cart первый get_product_details был раньше."""
    errors = []
    for turn in turns(events):
        names = tool_names(turn)
        if "create_cart" not in names:
            continue
        if "get_product_details" not in names:
            errors.append("корзина собрана без проверки составов при ограничении")
        elif names.index("get_product_details") > names.index("create_cart"):
            errors.append("корзина собрана до проверки составов "
                          "(create_cart раньше get_product_details)")
    return errors


check_details_before_cart_when_dietary.description = (
    "в каждом ходе с create_cart составы уже проверены: первый "
    "get_product_details раньше первого create_cart"
)


# ---------- кейсы ----------

def check_clarification(events, messages):
    """На размытый запрос — уточняющий вопрос или конкретное предложение."""
    return [] if ("?" in final_answer(events)
                  or any(t in ("products", "cart") for t, _ in events)
                  ) else ["нет ни уточняющего вопроса, ни предложения товаров"]


check_clarification.description = (
    "на размытый запрос («хочу поесть») — уточняющий вопрос "
    "или подборка товаров"
)


CASES = [
    EvalCase(
        name="lunch_showcase",
        description="эталонный пример «от запроса до корзины»: обед → "
                    "замена салата на винегрет → суммарные углеводы",
        prompts=[
            "Собери полноценный обед для одного взрослого человека. Сразу собери корзину.",
            "Замени салат на винегрет",
            "Сколько углеводов суммарно содержится в продуктах из корзины?",
        ],
        checks=[
            check_tool_called("search_products"),
            check_cart_present,
            check_cart_min_items(3),
            check_search_before_recommendation,
            check_cart_url,
            check_answer_total_matches_cart,
            check_answer_nonempty,
            check_replacement(2, "салат", "винегрет"),
            check_carbs_total(),
        ],
    ),
    EvalCase(
        name="budget_snack",
        description="«перекус на работу, бюджет 500 ₽, без лактозы»: поиск, "
                    "проверка состава, корзина в бюджете",
        prompts=["Собери перекус на работу, бюджет 500 ₽, без лактозы. Сразу собери корзину."],
        checks=[
            check_tool_called("search_products"),
            check_cart_present,
            check_budget(500),
            check_composition_excludes("лактоз"),
            check_details_before_cart_when_dietary,
            check_search_before_recommendation,
            check_cart_url,
            check_answer_total_matches_cart,
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
            check_cart_present,
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
            check_clarification,
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
