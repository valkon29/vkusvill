"""Общие фикстуры: фейковый LLM и фейковый MCP — тесты идут без сети и ключей."""

import json

import pytest

from app.agent import Agent
from app.mcp_client import MCPClientManager

# --- canned-ответы MCP-сервера (формы подтверждены живыми запросами) ---

SEARCH_RAW = json.dumps(
    {
        "ok": True,
        "data": {
            "meta": {"q": "сырники", "total": 67, "page": 1},
            "items": [
                {
                    "id": 114976,
                    "name": "Сырники классические&nbsp;жареные",
                    "price": {"current": 306, "old": None},
                    "unit": "шт",
                    "rating": {"average": 4.9, "count": 27338},
                    "url": "https://vkusvill.ru/goods/syrniki-1/",
                    "images": [{"small": "https://img/1.webp"}],
                },
                {
                    "id": 84636,
                    "name": 'Сырники "Нежные" зам.',
                    "price": {"current": 725, "old": 800},
                    "unit": "шт",
                    "rating": {"average": 4.9, "count": 4048},
                    "url": "https://vkusvill.ru/goods/syrniki-2/",
                    "images": [],
                },
            ],
        },
    },
    ensure_ascii=False,
)

DETAILS_RAW = json.dumps(
    {
        "ok": True,
        "data": {
            "id": 114976,
            "name": "Сырники классические жареные",
            "brand": "ВкусВилл",
            "price": {"current": 306},
            "unit": "шт",
            "weight": {"value": 0.2, "unit": "кг"},
            "rating": {"average": 4.9, "count": 1},
            "url": "https://vkusvill.ru/goods/syrniki-1/",
            "properties": [
                {"name": "Состав", "value": "творог, мука, сахар"},
                {"name": "Пищевая и энергетическая ценность в 100 г", "value": "246 ккал"},
            ],
        },
    },
    ensure_ascii=False,
)

CART_RAW = json.dumps(
    {"ok": True, "data": {"link": "https://vkusvill.ru/mobile?basket&share=1"}},
    ensure_ascii=False,
)


class FakeMCP:
    """Записывает вызовы и возвращает заготовленные ответы."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if name == "vkusvill_products_search":
            return SEARCH_RAW
        if name == "vkusvill_product_details":
            return DETAILS_RAW
        if name == "vkusvill_cart_link_create":
            return CART_RAW
        return json.dumps({"ok": True, "data": {"items": []}}, ensure_ascii=False)


class FakeChunk:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_FakeChoice(content, tool_calls)]


class _FakeChoice:
    def __init__(self, content, tool_calls):
        self.delta = _FakeDelta(content, tool_calls)


class _FakeDelta:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeTC:
    def __init__(self, index, id, name, arguments):
        self.index = index
        self.id = id
        self.function = _FakeFn(name, arguments)


class _FakeFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeStream:
    """Асинхронный итератор по чанкам (имитирует стриминг OpenAI-совместимого API)."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


class FakeLLM:
    """Скриптованный LLM: список «ответов», каждый — список чанков."""

    def __init__(self, scripted_turns):
        # scripted_turns: [[chunk, chunk, ...], [chunk, ...], ...]
        self._turns = iter(scripted_turns)
        self.requests: list[dict] = []
        self.chat = self._Chat(self)

    class _Chat:
        def __init__(self, parent):
            self._parent = parent
            self.completions = self

        async def create(self, **kwargs):
            self._parent.requests.append(kwargs)
            return FakeStream(next(self._parent._turns))


def make_agent(scripted_turns, mcp) -> Agent:
    return Agent(mcp, llm=FakeLLM(scripted_turns))


@pytest.fixture
def fake_mcp():
    return FakeMCP()


@pytest.fixture
def recorder():
    """Собирает события emit в список [(type, data), ...]."""

    async def _recorder(events):
        async def emit(event_type, data):
            events.append((event_type, data))

        return emit

    class Recorder:
        def __init__(self):
            self.events = []

        async def __call__(self, event_type, data):
            self.events.append((event_type, data))

    return Recorder()


def noop_lifespan_manager(monkeypatch):
    """Отключает сетевое подключение MCP-менеджера в app.main для тестов API."""
    monkeypatch.setattr(MCPClientManager, "start", lambda self: _async_noop())
    monkeypatch.setattr(MCPClientManager, "close", lambda self: _async_noop())


async def _async_noop():
    return None
