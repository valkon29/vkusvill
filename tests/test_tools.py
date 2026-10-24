"""Тесты обёрток инструментов: зачистка, валидация корзины, события."""

import json

from app.tools import vkusvill as tv


async def test_search_products_compact_and_event(recorder, fake_mcp):
    result = await tv.search_products({"query": "сырники"}, fake_mcp, recorder)

    # событие для UI: с картинкой и old_price
    products_events = [d for t, d in recorder.events if t == "products"]
    assert len(products_events) == 1
    card = products_events[0]["items"][0]
    assert card["image"] == "https://img/1.webp"
    assert card["old_price"] is None

    # LLM получает компактный JSON: &nbsp; вычищен, картинки нет
    data = json.loads(result)
    assert data["items"][0]["name"] == "Сырники классические жареные"
    assert "image" not in data["items"][0]
    assert data["meta"]["total"] == 67


async def test_search_products_empty_query(recorder, fake_mcp):
    result = await tv.search_products({"query": "  "}, fake_mcp, recorder)
    assert result.startswith("TOOL_ERROR")
    assert fake_mcp.calls == []


async def test_get_product_details(recorder, fake_mcp):
    result = await tv.get_product_details({"product_id": 114976}, fake_mcp, recorder)
    data = json.loads(result)
    assert data["composition"] == "творог, мука, сахар"
    assert data["nutrition"] == "246 ккал"
    assert data["price"] == 306


async def test_create_cart_validates_and_dedupes(recorder, fake_mcp):
    result = await tv.create_cart(
        {
            "products": [
                {"id": 114976, "quantity": 2},
                {"id": 114976, "quantity": 1},  # дубликат — количества суммируются
            ]
        },
        fake_mcp,
        recorder,
    )
    data = json.loads(result)
    assert data["link"] == "https://vkusvill.ru/mobile?basket&share=1"

    # в MCP ушла одна позиция с суммой количеств
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 114976, "q": 3}]

    # событие корзины: цена подтянута из details (306 × 3)
    cart_events = [d for t, d in recorder.events if t == "cart"]
    assert cart_events[0]["total"] == 918
    assert cart_events[0]["count"] == 1


async def test_create_cart_rejects_empty(recorder, fake_mcp):
    result = await tv.create_cart({"products": []}, fake_mcp, recorder)
    assert result.startswith("TOOL_ERROR")
    assert fake_mcp.calls == []


async def test_create_cart_rejects_too_many(recorder, fake_mcp):
    products = [{"id": i, "quantity": 1} for i in range(1, 22)]  # 21 позиция
    result = await tv.create_cart({"products": products}, fake_mcp, recorder)
    assert result.startswith("TOOL_ERROR")


async def test_create_cart_clamps_quantity(recorder, fake_mcp):
    await tv.create_cart(
        {"products": [{"id": 114976, "quantity": 100}]}, fake_mcp, recorder
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"][0]["q"] == 40


def test_current_cart_from_history_block():
    msgs = [
        {"role": "user", "content": "добавь суп"},
        {"role": "assistant", "content": (
            "Готово!\n\n[В корзине: id=58289 Печенье сдобное, 300 г ×1; "
            "id=48315 Суп с курицей ×2]"
        )},
        {"role": "user", "content": "ещё сыр"},
    ]
    assert tv.current_cart_from_history(msgs) == [
        {"id": 58289, "quantity": 1.0},
        {"id": 48315, "quantity": 2.0},
    ]
    assert tv.current_cart_from_history([{"role": "user", "content": "нет корзины"}]) == []


async def test_create_cart_merges_with_current(recorder, fake_mcp):
    await tv.create_cart(
        {
            "products": [{"id": 999, "quantity": 1}],
            "_current_cart": [{"id": 114976, "quantity": 2}],
        },
        fake_mcp,
        recorder,
    )
    # в MCP ушёл объединённый состав: текущая корзина + новый товар
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [
        {"id": 114976, "q": 2},
        {"id": 999, "q": 1},
    ]


async def test_create_cart_new_quantity_overrides_current(recorder, fake_mcp):
    await tv.create_cart(
        {
            "products": [{"id": 114976, "quantity": 5}],
            "_current_cart": [{"id": 114976, "quantity": 2}],
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 114976, "q": 5}]


async def test_create_cart_replace_flag_is_ignored(recorder, fake_mcp):
    # replace больше не существует: корзина может только накапливаться,
    # молча потерять позицию модель не может
    await tv.create_cart(
        {
            "products": [{"id": 999, "quantity": 1}],
            "replace": True,
            "_current_cart": [{"id": 114976, "quantity": 2}],
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [
        {"id": 114976, "q": 2},
        {"id": 999, "q": 1},
    ]


async def test_create_cart_zero_quantity_removes(recorder, fake_mcp):
    await tv.create_cart(
        {
            "products": [
                {"id": 114976, "quantity": 0},  # удалить
                {"id": 999, "quantity": 1},     # добавить
            ],
            "_current_cart": [
                {"id": 114976, "quantity": 2},
                {"id": 555, "quantity": 1},
            ],
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    # 114976 удалён, 555 не упомянут — остался, 999 добавлен
    assert cart_call[1]["products"] == [
        {"id": 555, "q": 1},
        {"id": 999, "q": 1},
    ]


async def test_create_cart_reports_final_composition(recorder, fake_mcp):
    # обёртка отдаёт агенту финальный состав — для накопления внутри хода
    args = {
        "products": [{"id": 999, "quantity": 1}],
        "_current_cart": [{"id": 114976, "quantity": 2}],
    }
    await tv.create_cart(args, fake_mcp, recorder)
    assert args["_final_cart"] == [
        {"id": 114976, "quantity": 2},
        {"id": 999, "quantity": 1},
    ]


# --- защита от подмены удаляемого товара ---

_NAMES = {
    58289: "Печенье сдобное «Шоколадное», 300 г",
    60658: "Суп «Рассольник» с курицей, 1 кг",
}
_CURRENT = [
    {"id": 58289, "quantity": 1},
    {"id": 60658, "quantity": 1},
]


async def test_removal_of_wrong_item_rejected(recorder, fake_mcp):
    # пользователь просил убрать печенье, модель удаляет суп — подмена
    result = await tv.create_cart(
        {
            "products": [{"id": 60658, "quantity": 0}],
            "_current_cart": _CURRENT,
            "_cart_names": _NAMES,
            "_last_user_message": "убери печенье из корзины",
        },
        fake_mcp,
        recorder,
    )
    assert result.startswith("TOOL_ERROR")
    assert "не тот товар" in result
    assert fake_mcp.calls == []  # до MCP подмена не дошла


async def test_removal_of_named_item_allowed(recorder, fake_mcp):
    # удаляют именно печенье, которое просил пользователь
    await tv.create_cart(
        {
            "products": [{"id": 58289, "quantity": 0}],
            "_current_cart": _CURRENT,
            "_cart_names": _NAMES,
            "_last_user_message": "убери печенье из корзины",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 60658, "q": 1}]


async def test_removal_with_confirmation_passes(recorder, fake_mcp):
    # модель настаивает после подсказки — подтверждённый вызов проходит
    await tv.create_cart(
        {
            "products": [{"id": 60658, "quantity": 0}],
            "confirm_removals": True,
            "_current_cart": _CURRENT,
            "_cart_names": _NAMES,
            "_last_user_message": "убери печенье из корзины",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 58289, "q": 1}]


async def test_removal_without_named_mention_allowed(recorder, fake_mcp):
    # «номер 1 нужен» — товар не назван, сигнала подмены нет, не мешаем
    await tv.create_cart(
        {
            "products": [{"id": 60658, "quantity": 0}, {"id": 999, "quantity": 1}],
            "_current_cart": _CURRENT,
            "_cart_names": _NAMES,
            "_last_user_message": "номер 1 нужен",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [
        {"id": 58289, "q": 1},
        {"id": 999, "q": 1},
    ]


async def test_removal_keep_phrase_allowed(recorder, fake_mcp):
    # «оставь только суп» — упомянут оставляемый товар, это законно
    await tv.create_cart(
        {
            "products": [{"id": 58289, "quantity": 0}],
            "_current_cart": _CURRENT,
            "_cart_names": _NAMES,
            "_last_user_message": "оставь только суп",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 60658, "q": 1}]


def test_cart_names_from_history_block():
    msgs = [
        {"role": "assistant", "content": (
            "Готово!\n\n[В корзине: id=58289 Печенье сдобное ×1; "
            "id=48315 Суп с курицей ×2]"
        )},
        {"role": "user", "content": "ещё сыр"},
    ]
    assert tv.cart_names_from_history(msgs) == {
        58289: "Печенье сдобное",
        48315: "Суп с курицей",
    }


# --- удаление умолчанием (модель передала состав без удаляемого товара) ---

_NAMES3 = {
    58289: "Печенье сдобное «Шоколадное», 300 г",
    60658: "Суп «Рассольник» с курицей, 1 кг",
    555: "Салат винегрет, 180 г",
}
_CURRENT3 = [
    {"id": 58289, "quantity": 1},
    {"id": 60658, "quantity": 1},
    {"id": 555, "quantity": 1},
]


async def test_implicit_removal_by_omission(recorder, fake_mcp):
    # «убери винегрет», модель передала состав без винегрета — убираем сами
    await tv.create_cart(
        {
            "products": [
                {"id": 58289, "quantity": 1},
                {"id": 60658, "quantity": 1},
            ],
            "_current_cart": _CURRENT3,
            "_cart_names": _NAMES3,
            "_last_user_message": "убери из корзины винегрет",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [
        {"id": 58289, "q": 1},
        {"id": 60658, "q": 1},
    ]


async def test_implicit_removal_not_when_item_passed(recorder, fake_mcp):
    # модель явно передала винегрет с количеством — её выбор уважаем
    await tv.create_cart(
        {
            "products": [{"id": 555, "quantity": 2}],
            "_current_cart": _CURRENT3,
            "_cart_names": _NAMES3,
            "_last_user_message": "убери из корзины винегрет",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert {"id": 555, "q": 2} in cart_call[1]["products"]


async def test_no_implicit_removal_without_intent(recorder, fake_mcp):
    # «добавь суп» — намерения удалить нет, не упомянутые позиции остаются
    await tv.create_cart(
        {
            "products": [{"id": 60658, "quantity": 1}],
            "_current_cart": _CURRENT3,
            "_cart_names": _NAMES3,
            "_last_user_message": "добавь ещё суп",
        },
        fake_mcp,
        recorder,
    )
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert len(cart_call[1]["products"]) == 3  # всё сохранилось


def test_ids_named_for_removal():
    names = _NAMES3
    assert tv.ids_named_for_removal(names, "убери из корзины винегрет") == {555}
    assert tv.ids_named_for_removal(names, "суп не нужен") == {60658}
    assert tv.ids_named_for_removal(names, "выкинь печенье") == {58289}
    # вопрос о прошлом и фильтр — не намерение
    assert tv.ids_named_for_removal(names, "ты удалил винегрет?") == set()
    assert tv.ids_named_for_removal(names, "добавь кофе без лактозы") == set()
    assert tv.ids_named_for_removal(names, "добавь суп") == set()
