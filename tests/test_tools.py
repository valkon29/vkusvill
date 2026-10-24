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


async def test_create_cart_replaces_when_flag(recorder, fake_mcp):
    await tv.create_cart(
        {
            "products": [{"id": 999, "quantity": 1}],
            "replace": True,
            "_current_cart": [{"id": 114976, "quantity": 2}],
        },
        fake_mcp,
        recorder,
    )
    # replace=true: старый состав отброшен
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 999, "q": 1}]
