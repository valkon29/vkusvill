"""Управление MCP-сессией к серверу ВкусВилла.

Сервер https://mcp.vkusvill.ru/mcp stateless (streamable HTTP, протокол
2025-03-26), поэтому переоткрытие сессии всегда безопасно. Один менеджер
живёт всё время работы приложения (lifespan FastAPI); при сбое сессия
лениво переоткрывается на следующем вызове.

Ограничения сервера: 60 запросов за 3-секундное окно (по IP), поэтому
вызовы сериализуются семафором, а при 429 делается ретрай с паузой
на длительность окна сброса.
"""

import asyncio
import logging
from contextlib import AsyncExitStack

from mcp import ClientSession

try:  # разные имена в разных версиях официального SDK
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # pragma: no cover — fallback для новых версий SDK
    from mcp.client.streamable_http import (
        streamable_http_client as streamablehttp_client,
    )

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_CALLS = 3
_RETRIES = 3
_RATE_LIMIT_WINDOW = 3.0  # сек; X-RateLimit-Reset сервера


class MCPUnavailable(Exception):
    """Все попытки вызова инструмента исчерпаны."""


class MCPClientManager:
    def __init__(self, url: str) -> None:
        self._url = url
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()  # сериализует (пере)открытие сессии
        self._gate = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)

    async def start(self) -> None:
        await self._open()

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    async def _open(self) -> None:
        await self._teardown()
        stack = AsyncExitStack()
        try:
            transport = await stack.enter_async_context(
                streamablehttp_client(self._url)
            )
            read, write = transport[0], transport[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        logger.info("MCP-сессия открыта: %s", self._url)

    async def _teardown(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 — закрытие не должно ронять вызов
                logger.warning("Ошибка при закрытии MCP-сессии", exc_info=True)
        self._stack = None
        self._session = None

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Вызвать MCP-инструмент и вернуть текст результата.

        Ошибки сервера и сбои соединения после всех ретраев превращаются
        в MCPUnavailable — вызывающий код отдаёт их LLM как TOOL_ERROR.
        """
        last_error: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                async with self._gate:
                    if self._session is None:
                        async with self._lock:
                            if self._session is None:
                                await self._open()
                    result = await self._session.call_tool(name, arguments)
                is_error = getattr(result, "is_error", None)
                if is_error is None:
                    is_error = getattr(result, "isError", False)
                if is_error:
                    return f"TOOL_ERROR: {_result_text(result)}"
                return _result_text(result)
            except Exception as exc:  # noqa: BLE001 — любой сбой = переоткрытие
                last_error = exc
                # Ошибки валидации аргументов (-32602) не проходят от ретрая
                if getattr(exc, "code", None) in (-32602,):
                    raise MCPUnavailable(f"{name}: {exc}") from exc
                logger.warning(
                    "Сбой вызова %s (попытка %d/%d): %r",
                    name, attempt + 1, _RETRIES, exc,
                )
                async with self._lock:
                    await self._teardown()
                if attempt < _RETRIES - 1:
                    if _is_rate_limited(exc):
                        await asyncio.sleep(_RATE_LIMIT_WINDOW)
                    else:
                        await asyncio.sleep(0.5 * (attempt + 1))
        raise MCPUnavailable(f"{name}: {last_error!r}")


def _result_text(result) -> str:
    parts = [c.text for c in result.content if hasattr(c, "text")]
    return "\n".join(parts) if parts else ""


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate" in text or "too many" in text
