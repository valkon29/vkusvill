"""Оффлайн-тесты проверок eval (evals/cases.py) и раннера.

Проверки — чистые функции над синтетическими логами событий; ключи и сеть
не нужны. Главное: проверка корзины обязана падать, когда корзина не собрана,
а не проходить циклом по пустому списку (замечание с защиты)."""

import json

from evals.cases import (
    check_answer_total_matches_cart,
    check_budget,
    check_cart_min_items,
    check_cart_present,
    check_carbs_total,
    check_composition_excludes,
    check_details_before_cart_when_dietary,
    check_replacement,
    turns,
)
from evals.runner import _last_answer


# ---------- конструкторы событий ----------

def turn(prompt):
    return ("turn", {"prompt": prompt})


def tool(name, args="{}"):
    return ("tool", {"name": name, "label": name, "args": args})


def tool_result(name, result):
    return ("tool_result", {"name": name, "result": result})


def token(text):
    return ("token", {"text": text})


def cart(items, url="https://vkusvill.ru/mobile?basket&share=1"):
    total = round(sum((i["price"] or 0) * i["quantity"] for i in items), 2)
    return ("cart", {"url": url, "total": total, "count": len(items), "items": items})


def item(pid, name, price, quantity=1):
    return {"id": pid, "name": name, "price": price, "quantity": quantity}


def details(pid, name, composition=None, nutrition=None, weight=None):
    d = {"id": pid, "name": name}
    if composition is not None:
        d["composition"] = composition
    if nutrition is not None:
        d["nutrition"] = nutrition
    if weight is not None:
        d["weight"] = weight
    return json.dumps(d, ensure_ascii=False)


MSGS = [{"role": "user", "content": "x"}]


# ---------- честность проверок: нет корзины — есть нарушение ----------

def test_cart_absent_is_failure_everywhere():
    """Замечание с защиты: без собранной корзины проверки проходили впустую."""
    events = [tool("search_products"), tool_result("search_products", "{}")]
    assert check_cart_present(events, MSGS)
    assert check_budget(500)(events, MSGS)
    assert check_composition_excludes("лактоз")(events, MSGS)
    assert check_cart_min_items(3)(events, MSGS)
    assert check_carbs_total()(events, MSGS)


def test_budget_over_limit_fails():
    events = [cart([item(1, "Суп", 300), item(2, "Хлеб", 250)])]
    assert check_budget(500)(events, MSGS)


def test_budget_within_limit_passes():
    events = [cart([item(1, "Суп", 300), item(2, "Хлеб", 150)])]
    assert check_budget(500)(events, MSGS) == []


def test_composition_unchecked_item_fails():
    """Товар в корзине без get_product_details — состав не проверен."""
    events = [
        tool("search_products"),
        tool_result("search_products", json.dumps({"items": [{"id": 1}, {"id": 2}]})),
        cart([item(1, "Каша", 100), item(2, "Печенье", 90)]),
    ]
    errors = check_composition_excludes("лактоз")(events, MSGS)
    assert any("не проверен" in e for e in errors)


def test_composition_milk_without_lactose_word_passes():
    """«молоко» без слова «лактоза» проверку не валит — известная гранца
    детерминированной проверки (распознавание скрытой лактозы — на LLM)."""
    events = [
        tool("get_product_details", '{"product_id": 1}'),
        tool_result("get_product_details", details(1, "Печенье", composition="мука, молоко")),
        cart([item(1, "Печенье", 90)]),
    ]
    errors = check_composition_excludes("лактоз")(events, MSGS)
    assert errors == []


def test_composition_direct_lactose_mention_fails():
    events = [
        tool_result("get_product_details", details(1, "Молоко", composition="молоко пастеризованное, лактоза")),
        cart([item(1, "Молоко", 90)]),
    ]
    assert check_composition_excludes("лактоз")(events, MSGS)


def test_composition_lactose_free_wording_passes():
    """«безлактозное», «без лактозы», «не содержит лактозы» — продукт подходит:
    ложное срабатывание на «каше на безлактозном молоке» найдено живым прогоном."""
    for composition, name in [
        ("овсяная каша на безлактозном молоке", "Каша"),
        ("молоко без лактозы", "Молоко"),
        ("не содержит лактозы", "Печенье"),
        ("лактозы нет", "Йогурт"),
        ("лактоза отсутствует", "Кефир"),
    ]:
        events = [
            tool_result("get_product_details", details(1, name, composition=composition)),
            cart([item(1, name, 90)]),
        ]
        assert check_composition_excludes("лактоз")(events, MSGS) == [], (composition, name)


# ---------- названная сумма против фактической корзины ----------

def test_answer_total_matches_cart_passes():
    events = [turn("собери"), cart([item(1, "Суп", 228), item(2, "Хлеб", 60)]),
              token("Готово! Итого: 288 рублей.")]
    assert check_answer_total_matches_cart(events, MSGS) == []


def test_answer_total_mismatch_fails():
    events = [turn("замени"), cart([item(1, "Винегрет", 188)]),
              token("Итоговая сумма корзины: 850 рублей.")]
    errors = check_answer_total_matches_cart(events, MSGS)
    assert any("не сходится" in e for e in errors)


def test_answer_total_without_cart_fails():
    events = [turn("собери"), token("Собрал корзину, итого 500 руб.")]
    errors = check_answer_total_matches_cart(events, MSGS)
    assert any("не собрана" in e for e in errors)


# ---------- порядок проверок при пищевом ограничении ----------

def test_details_must_precede_cart():
    events = [
        turn("завтрак"),
        tool("search_products"),
        tool("create_cart"),
        tool("get_product_details"),
    ]
    errors = check_details_before_cart_when_dietary(events, MSGS)
    assert any("до проверки" in e for e in errors)


def test_details_before_cart_passes():
    events = [
        tool("get_product_details"),
        tool("create_cart"),
    ]
    assert check_details_before_cart_when_dietary(events, MSGS) == []


def test_cart_without_details_at_all_fails():
    events = [tool("search_products"), tool("create_cart")]
    assert check_details_before_cart_when_dietary(events, MSGS)


# ---------- замена салата на винегрет ----------

def _showcase_events():
    """Типовая трассировка эталонного кейса: обед → замена → углеводы."""
    return [
        turn("Собери обед"),
        tool("search_products", '{"query": "суп"}'),
        tool_result("search_products", json.dumps({"items": [{"id": 1}]})),
        cart([item(1, "Суп куриный", 250), item(2, "Салат овощной", 120), item(3, "Хлеб", 60)]),
        token("Собрал обед."),
        turn("Замени салат на винегрет"),
        tool("search_products", '{"query": "винегрет"}'),
        tool_result("search_products", json.dumps({"items": [{"id": 4}]})),
        # новый товар сам содержит слово «салат» — как в реальном каталоге
        # («Салат винегрет с маринованной капустой»); остатком не считается
        cart([item(1, "Суп куриный", 250), item(4, "Салат винегрет с маринованной капустой", 130), item(3, "Хлеб", 60)]),
        token("Заменил."),
        turn("Сколько углеводов?"),
        tool("get_product_details", '{"product_id": 1}'),
        tool_result("get_product_details",
                    details(1, "Суп куриный", nutrition="Углеводы: 8 г", weight={"value": 0.3, "unit": "кг"})),
        tool("get_product_details", '{"product_id": 4}'),
        tool_result("get_product_details",
                    details(4, "Салат винегрет с маринованной капустой", nutrition="Углеводы: 12,5 г", weight={"value": 300, "unit": "г"})),
        tool("get_product_details", '{"product_id": 3}'),
        tool_result("get_product_details",
                    details(3, "Хлеб", nutrition="Углеводы: 40 г", weight={"value": 0.4, "unit": "кг"})),
        token("Всего в обеде примерно 193 г углеводов."),
    ]


def test_replacement_ok():
    assert check_replacement(2, "салат", "винегрет")(_showcase_events(), MSGS) == []


def test_replacement_dropped_unrelated_item_fails():
    events = _showcase_events()
    # суп пропал из корзины замены без запроса
    events[8] = cart([item(4, "Винегрет", 130), item(3, "Хлеб", 60)])
    errors = check_replacement(2, "салат", "винегрет")(events, MSGS)
    assert any("Суп куриный" in e and "пропала" in e for e in errors)


def test_replacement_kept_old_item_fails():
    events = _showcase_events()
    events[8] = cart([item(1, "Суп куриный", 250), item(2, "Салат овощной", 120),
                      item(4, "Винегрет", 130), item(3, "Хлеб", 60)])
    errors = check_replacement(2, "салат", "винегрет")(events, MSGS)
    assert any("остался" in e for e in errors)


def test_replacement_missing_new_item_fails():
    events = _showcase_events()
    events[8] = cart([item(1, "Суп куриный", 250), item(3, "Хлеб", 60)])
    errors = check_replacement(2, "салат", "винегрет")(events, MSGS)
    assert any("не появился" in e for e in errors)


def test_replacement_without_salad_in_prev_cart_still_requires_new_item():
    """Если салата в корзине не было — проверяем только добавление и сохранность."""
    events = _showcase_events()
    events[4] = cart([item(1, "Суп куриный", 250), item(3, "Хлеб", 60)])
    assert check_replacement(2, "салат", "винегрет")(events, MSGS) == []


# ---------- пересчёт углеводов ----------

def test_carbs_total_matches_weighted_sum():
    # взвешенная сумма: 8*(300/100) + 12.5*3 + 40*4 = 24 + 37.5 + 160 = 221.5…
    # фактически 24 + 37.5 + 160 = 221.5; в примере выше — ход с 193 г
    # (30% допуск покрывает пересчёт «на 100 г»: 8+12.5+40 = 60.5 — не матчится,
    # поэтому проверим оба варианта отдельными событиями)
    events = _showcase_events()
    assert check_carbs_total()(events, MSGS) == []


def test_carbs_total_wrong_number_fails():
    events = _showcase_events()
    events[-1] = token("Всего примерно 3 г углеводов.")
    errors = check_carbs_total()(events, MSGS)
    assert any("с пересчётом" in e for e in errors)


def test_carbs_total_unchecked_item_fails():
    events = _showcase_events()
    # для хлеба не запросили состав в последнем ходе
    events = events[:-3] + [token("Всего примерно 50 г углеводов.")]
    errors = check_carbs_total()(events, MSGS)
    assert any("КБЖУ" in e and "Хлеб" in e for e in errors)


def test_carbs_total_unparseable_nutrition_fails():
    events = _showcase_events()
    # у хлеба не разобрать углеводы из КБЖУ
    events[-2] = tool_result(
        "get_product_details", details(3, "Хлеб", nutrition="пищевая ценность не указана")
    )
    errors = check_carbs_total()(events, MSGS)
    assert any("не удалось разобрать" in e for e in errors)


# ---------- нарезка ходов и ответ последнего хода ----------

def test_turns_split_by_markers():
    events = [turn("a"), token("1"), turn("b"), token("2"), token("3")]
    assert len(turns(events)) == 2


def test_turns_without_markers_is_single_turn():
    assert len(turns([token("x"), token("y")])) == 1


class _Rec:
    def __init__(self, events):
        self.events = events


def test_last_answer_resets_on_turn_marker():
    """Ответ хода без вызовов инструментов не должен подтягивать текст
    предыдущего хода."""
    events = [
        turn("a"), tool("search_products"), tool_result("search_products", "{}"),
        token("Ответ первого хода."),
        turn("b"), token("Ответ второго хода."),
    ]
    assert _last_answer(_Rec(events)) == "Ответ второго хода."
