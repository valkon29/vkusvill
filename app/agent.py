"""Агентный цикл: LLM через OpenAI-совместимый API + инструменты через MCP.

run_agent() не знает ни про FastAPI, ни про HTTP — только messages + emit.
Поэтому веб-эндпоинт, eval-раннер и CLI используют один и тот же цикл.

Специфика стриминга tool calls: аргументы приходят фрагментами, копятся
по index; невалидный JSON в аргументах возвращается модели как результат
инструмента, чтобы она самокорректировалась.
"""

import json
import logging
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.events import Emit, TOOL_LABELS
from app.mcp_client import MCPClientManager
from app.prompts import SYSTEM_PROMPT
from app.tools.schemas import TOOL_SPECS
from app.tools.vkusvill import TOOL_HANDLERS, current_cart_from_history

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, mcp: MCPClientManager, llm: AsyncOpenAI | None = None):
        self._mcp = mcp
        self._llm = llm or AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )

    async def run(self, messages: list[dict], emit: Emit) -> None:
        """Прогоняет агентный цикл, отправляя события через emit.

        messages копируется — история вызывающего не мутируется.
        """
        convo = self._with_system_prompt(messages)
        try:
            await self._loop(convo, emit)
        except Exception as exc:  # noqa: BLE001 — пользователю нужен ответ, не 500
            logger.exception("Сбой агентного цикла")
            await emit("error", {"message": str(exc)})
        finally:
            await emit("done", {})

    def _with_system_prompt(self, messages: list[dict]) -> list[dict]:
        if messages and messages[0].get("role") == "system":
            return list(messages)
        return [{"role": "system", "content": SYSTEM_PROMPT}, *messages]

    async def _loop(self, convo: list[dict], emit: Emit) -> None:
        for _ in range(settings.max_tool_iterations):
            stream = await self._llm.chat.completions.create(
                model=settings.model_name,
                messages=convo,
                tools=TOOL_SPECS,
                stream=True,
            )
            content_parts: list[str] = []
            calls: dict[int, dict] = {}  # index -> {id, name, args}
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    await emit("token", {"text": delta.content})
                for tc in delta.tool_calls or []:
                    call = calls.setdefault(
                        tc.index, {"id": "", "name": "", "args": ""}
                    )
                    if tc.id:
                        call["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            call["name"] += tc.function.name
                        if tc.function.arguments:
                            call["args"] += tc.function.arguments

            if not calls:
                return  # финальный ответ — только текст

            convo.append(
                {
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"]},
                        }
                        for c in calls.values()
                    ],
                }
            )
            for c in calls.values():
                await emit("tool", {"name": c["name"], "label": TOOL_LABELS.get(c["name"], c["name"])})
                result = await self._execute(c["name"], c["args"], emit, convo)
                # полный результат инструмента — для eval и отладки (UI игнорирует)
                await emit("tool_result", {"name": c["name"], "result": result})
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": result,
                    }
                )

        await emit(
            "token",
            {"text": "\n\n_Достигнут лимит шагов — вот что удалось найти._"},
        )

    async def _execute(
        self, name: str, args_str: str, emit: Emit, convo: list[dict]
    ) -> str:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return f"TOOL_ERROR: неизвестный инструмент {name}"
        try:
            args = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError as exc:
            return f"TOOL_ERROR: невалидный JSON аргументов ({exc})"
        if not isinstance(args, dict):
            return "TOOL_ERROR: аргументы должны быть JSON-объектом"
        if name == "create_cart":
            # текущая корзина из блока [В корзине: …] — обёртка объединит состав
            args.setdefault("_current_cart", current_cart_from_history(convo))
        try:
            return await handler(args, self._mcp, emit)
        except Exception as exc:  # noqa: BLE001 — ошибка = текст для LLM
            logger.warning("Сбой инструмента %s: %r", name, exc)
            return f"TOOL_ERROR: {exc}"
