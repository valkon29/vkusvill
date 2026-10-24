"""Тесты агентного цикла: накопление tool calls, невалидный JSON, лимит шагов."""

import json

from tests.conftest import FakeChunk, FakeTC, make_agent


async def test_plain_answer_no_tools(recorder, fake_mcp):
    agent = make_agent(
        [[FakeChunk(content="Привет!"), FakeChunk(content=" Чем помочь?")]],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "привет"}], recorder)

    types = [t for t, _ in recorder.events]
    assert "tool" not in types
    assert types[0] == "token" and types[-1] == "done"
    text = "".join(d["text"] for t, d in recorder.events if t == "token")
    assert text == "Привет! Чем помочь?"


async def test_tool_call_and_followup(recorder, fake_mcp):
    # 1-й ход LLM: вызывает search_products; 2-й: финальный текст
    agent = make_agent(
        [
            [
                FakeChunk(content="Сейчас найду."),
                FakeChunk(tool_calls=[FakeTC(0, "call_1", "search_products",
                                             '{"query": "сырники')]),
                FakeChunk(tool_calls=[FakeTC(0, None, None, '"}')]),
            ],
            [FakeChunk(content="Вот что нашёл.")],
        ],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "найди сырники"}], recorder)

    # MCP получили вызов с собранными из фрагментов аргументами
    assert fake_mcp.calls[0][0] == "vkusvill_products_search"
    assert fake_mcp.calls[0][1]["q"] == "сырники"

    # в историю LLM ушёл role=tool результат
    second_request = agent._llm.requests[1]
    tool_msgs = [m for m in second_request["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "Сырники" in tool_msgs[0]["content"]


async def test_invalid_json_args_returned_as_error(recorder, fake_mcp):
    agent = make_agent(
        [
            [FakeChunk(tool_calls=[FakeTC(0, "call_1", "search_products",
                                          '{broken json')])],
            [FakeChunk(content="Извините, попробую иначе.")],
        ],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "найди сырники"}], recorder)

    second_request = agent._llm.requests[1]
    tool_msgs = [m for m in second_request["messages"] if m["role"] == "tool"]
    assert "TOOL_ERROR" in tool_msgs[0]["content"]
    assert fake_mcp.calls == []  # до MCP невалидный вызов не дошёл


async def test_unknown_tool(recorder, fake_mcp):
    agent = make_agent(
        [
            [FakeChunk(tool_calls=[FakeTC(0, "call_1", "nonexistent_tool", "{}")])],
            [FakeChunk(content="Готово.")],
        ],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "x"}], recorder)
    second_request = agent._llm.requests[1]
    tool_msgs = [m for m in second_request["messages"] if m["role"] == "tool"]
    assert "TOOL_ERROR" in tool_msgs[0]["content"]


async def test_max_iterations_guard(recorder, fake_mcp, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_tool_iterations", 2)

    # LLM бесконечно зовёт инструменты — цикл должен оборваться
    looping_turn = [[FakeChunk(tool_calls=[FakeTC(0, "call_1", "search_products",
                                                  '{"query": "x"}')])]]
    agent = make_agent(looping_turn * 5, fake_mcp)
    await agent.run([{"role": "user", "content": "x"}], recorder)

    assert len(agent._llm.requests) == 2
    tokens = "".join(d["text"] for t, d in recorder.events if t == "token")
    assert "лимит шагов" in tokens


async def test_system_prompt_injected_once(recorder, fake_mcp):
    agent = make_agent([[FakeChunk(content="ок")]], fake_mcp)
    await agent.run([{"role": "user", "content": "привет"}], recorder)
    first = agent._llm.requests[0]["messages"]
    assert first[0]["role"] == "system"
    # исходная история вызывающего не мутируется
    assert len(agent._llm.requests[0]["messages"]) == 2


async def test_context_blocks_moved_to_system_prompt(recorder, fake_mcp):
    # история содержит служебные блоки — LLM их видеть не должна
    agent = make_agent([[FakeChunk(content="Готово.")]], fake_mcp)
    messages = [
        {"role": "user", "content": "покажи печенье"},
        {"role": "assistant", "content": (
            "Вот печенье.\n\n[Показаны товары: id=58289 Печенье сдобное ×1]\n\n"
            "[В корзине: id=58289 Печенье сдобное ×1]"
        )},
        {"role": "user", "content": "что в корзине?"},
    ]
    await agent.run(messages, recorder)

    request = agent._llm.requests[0]
    system = request["messages"][0]
    assert system["role"] == "system"
    assert "Текущая корзина: id=58289 ×1" in system["content"]
    assert "Показанные пользователю товары" in system["content"]
    # сами блоки из сообщений вырезаны
    assistant = [m for m in request["messages"] if m["role"] == "assistant"][0]
    assert "[" not in assistant["content"]


async def test_create_cart_merges_cart_from_context(recorder, fake_mcp):
    # модель передаёт только новый товар — обёртка объединяет с корзиной из истории
    agent = make_agent(
        [
            [
                FakeChunk(tool_calls=[FakeTC(0, "call_1", "create_cart",
                                             '{"products": [{"id": 999, "quantity": 1}]}')]),
            ],
            [FakeChunk(content="Добавил.")],
        ],
        fake_mcp,
    )
    messages = [
        {"role": "user", "content": "добавь суп"},
        {"role": "assistant", "content": (
            "Готово.\n\n[В корзине: id=114976 Сырники ×2]"
        )},
        {"role": "user", "content": "добавь ещё суп"},
    ]
    await agent.run(messages, recorder)

    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [
        {"id": 114976, "q": 2},
        {"id": 999, "q": 1},
    ]


async def test_cart_claim_without_tool_forces_retry(recorder, fake_mcp):
    # модель «обновила корзину словами» — агент обязан принудить к create_cart
    agent = make_agent(
        [
            [FakeChunk(content="Суп добавлен в корзину. Итоговая сумма: 653 рубля.")],
            [
                FakeChunk(tool_calls=[FakeTC(0, "call_1", "create_cart",
                                             '{"products": [{"id": 999, "quantity": 1}]}')]),
            ],
            [FakeChunk(content="Готово: корзина собрана.")],
        ],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "добавь суп"}], recorder)

    # create_cart реально вызван несмотря на «словесное» обновление
    cart_calls = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"]
    assert len(cart_calls) == 1

    # пользователю видна плашка о принудительном оформлении
    text = "".join(d["text"] for t, d in recorder.events if t == "token")
    assert "Оформляю корзину" in text

    # поправка ушла модели (один раз, осталась в истории последующих запросов)
    last_request = agent._llm.requests[-1]
    nudges = [
        m for m in last_request["messages"]
        if m.get("role") == "user" and "СИСТЕМНОЕ СООБЩЕНИЕ" in m.get("content", "")
    ]
    assert len(nudges) == 1


async def test_cart_mention_without_claim_no_retry(recorder, fake_mcp):
    # вопрос о состоянии корзины — не утверждение об изменении, повтора быть не должно
    agent = make_agent(
        [[FakeChunk(content="Сейчас в корзине печенье и суп. Итого 651 рубль.")]],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "что в корзине?"}], recorder)

    assert not [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"]
    assert len(agent._llm.requests) == 1


def test_cart_claim_regexes():
    from app.agent import _CART_CLAIM_RE, _CART_LINK_RE

    assert _CART_CLAIM_RE.search("Суп добавлен в корзину. Итоговая сумма: 651 рубль.")
    assert _CART_CLAIM_RE.search("Я убрал печенье из корзины.")
    # обратный порядок слов: «В корзину добавлен …», «Из корзины удалён …»
    assert _CART_CLAIM_RE.search("В корзину добавлен суп «Рассольник» по цене 488 руб.")
    assert _CART_CLAIM_RE.search("Из корзины удалён сырники.")
    # вопросы о состоянии корзины — не утверждения
    assert not _CART_CLAIM_RE.search("Сейчас в корзине печенье и суп. Итого 651 рубль.")
    assert not _CART_CLAIM_RE.search("Что положить в корзину?")

    assert _CART_LINK_RE.search(
        "Ссылка на корзину ВкусВилла появится под моим сообщением."
    )
    assert not _CART_LINK_RE.search("Вот ссылка на корзину.")


async def test_sequential_create_cart_calls_accumulate(recorder, fake_mcp):
    # модель дважды вызвала create_cart за один ход — второй вызов обязан
    # объединиться с результатом первого, а не затереть его
    agent = make_agent(
        [
            [FakeChunk(tool_calls=[FakeTC(0, "call_1", "create_cart",
                                         '{"products": [{"id": 999, "quantity": 1}]}')])],
            [FakeChunk(tool_calls=[FakeTC(0, "call_2", "create_cart",
                                         '{"products": [{"id": 114976, "quantity": 2}]}')])],
            [FakeChunk(content="Готово.")],
        ],
        fake_mcp,
    )
    await agent.run([{"role": "user", "content": "добавь печенье и сырники"}], recorder)

    cart_calls = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"]
    assert len(cart_calls) == 2
    # первый вызов: только новый товар; второй: результат первого + новый товар
    assert cart_calls[0][1]["products"] == [{"id": 999, "q": 1}]
    assert cart_calls[1][1]["products"] == [
        {"id": 999, "q": 1},
        {"id": 114976, "q": 2},
    ]


async def test_link_promise_rebuilds_cart_directly(recorder, fake_mcp):
    # модель обещает ссылку без вызова — агент пересобирает корзину сам,
    # без второго захода к LLM, с точными количествами из контекста
    agent = make_agent(
        [[FakeChunk(content="Ссылка на корзину появится под сообщением.")]],
        fake_mcp,
    )
    messages = [
        {"role": "user", "content": "можно ссылку на корзину"},
        {"role": "assistant", "content": "Ранее собрано.\n\n[В корзине: id=114976 Сырники ×2]"},
    ]
    await agent.run(messages, recorder)

    # к LLM был только один запрос — пересборка прошла без модели
    assert len(agent._llm.requests) == 1
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 114976, "q": 2}]

    # событие корзины ушло в UI
    cart_events = [d for t, d in recorder.events if t == "cart"]
    assert len(cart_events) == 1
    assert cart_events[0]["total"] == 612  # 306 × 2


async def test_link_promise_rebuild_drops_removed_item(recorder, fake_mcp):
    # модель «удалила словами» и обещала ссылку — прямой пересбор обязан
    # исключить товар, который пользователь просил убрать
    agent = make_agent(
        [[FakeChunk(content="Ссылка на корзину появится под сообщением.")]],
        fake_mcp,
    )
    messages = [
        {"role": "user", "content": "собери корзину"},
        {"role": "assistant", "content": (
            "Готово.\n\n[В корзине: id=114976 Сырники ×2; id=555 Салат винегрет ×1]"
        )},
        {"role": "user", "content": "убери из корзины винегрет"},
    ]
    await agent.run(messages, recorder)

    assert len(agent._llm.requests) == 1
    cart_call = [c for c in fake_mcp.calls if c[0] == "vkusvill_cart_link_create"][0]
    assert cart_call[1]["products"] == [{"id": 114976, "q": 2}]
