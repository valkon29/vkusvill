"""Агентный цикл: LLM через OpenAI-совместимый API + инструменты через MCP.

run_agent() не знает ни про FastAPI, ни про HTTP — только messages + emit.
Поэтому веб-эндпоинт, eval-раннер и CLI используют один и тот же цикл.

Специфика стриминга tool calls: аргументы приходят фрагментами, копятся
по index; невалидный JSON в аргументах возвращается модели как результат
инструмента, чтобы она самокорректировалась.
"""

import json
import logging
import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.events import Emit, TOOL_LABELS
from app.mcp_client import MCPClientManager
from app.prompts import SYSTEM_PROMPT
from app.tools.schemas import TOOL_SPECS
from app.tools.vkusvill import (
    TOOL_HANDLERS,
    cart_names_from_history,
    current_cart_from_history,
    last_shown_products_from_history,
    strip_context_blocks,
)

logger = logging.getLogger(__name__)

# Ответ утверждает изменение корзины («добавлено в корзину», «убрал из корзины»).
# Только утвердившиеся формы: инфинитивы («положить в корзину?» — вопрос) не считаются.
# Две ориентации: «добавил … в корзину» и «В корзину добавлен …».
_CART_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:добавлен(?:а|о|ы)?|добавл(?:ю|им|ила?|ишь)"
    r"|положен(?:а|о|ы)?|полож(?:ил|у|им)"
    r"|собран(?:а|о|ы)?|собрал(?:а|о|и)?|собер(?:у|ёшь|ём|ет)"
    r"|убран(?:а|о|ы)?|убрал(?:а|о|и)?|убер(?:у|ёшь|ём|ет)"
    r"|удалён(?:а|о|ы)?|удален(?:а|о|ы)?|удал(?:ил|ю|им))"
    r"[^\n]{0,60}корзин"
    r"|корзин\w*[^\n]{0,20}"
    r"(?:добавлен(?:а|о|ы)?|добавл(?:ю|им|ила?|ишь)"
    r"|положен(?:а|о|ы)?|убран(?:а|о|ы)?"
    r"|удалён(?:а|о|ы)?|удален(?:а|о|ы)?|удал(?:ил|ю|им)"
    r"|исключён(?:а|о|ы)?|исключен(?:а|о|ы)?)"
    r")",
    re.IGNORECASE,
)
# Обещание ссылки на корзину без вызова create_cart
_CART_LINK_RE = re.compile(
    r"ссылк\w*\s+на\s+корзину[^\n]{0,40}появ",
    re.IGNORECASE,
)
# Принудительная поправка, когда модель объявила изменение корзины или
# пообещала ссылку, не вызвав create_cart
_CART_FORCED_NUDGE = (
    "СИСТЕМНОЕ СООБЩЕНИЕ: в предыдущем ответе ты сообщил об изменении корзины "
    "или пообещал ссылку на неё, но не вызвал инструмент create_cart — ссылки "
    "у пользователя нет. Вызови create_cart сейчас: передай ВСЕ позиции "
    "текущей корзины из раздела контекста с их же количествами — тогда "
    "появится актуальная кнопка со ссылкой. НИКОГДА не передавай id, которых "
    "нет в разделе контекста или в результатах инструментов: выдуманный id "
    "приводит к ошибке всей сборки. Если нужного id в контексте нет — сначала "
    "вызови search_products. Если корзина пуста и добавить нечего — честно "
    "скажи, что корзина ещё не собрана, и предложи товары."
)


class Agent:
    def __init__(self, mcp: MCPClientManager, llm: AsyncOpenAI | None = None):
        self._mcp = mcp
        self._llm = llm or AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )

    async def run(self, messages: list[dict], emit: Emit) -> None:
        """Прогоняет агентный цикл, отправляя события через emit.

        messages копируется — история вызывающего не мутируется. Служебные
        блоки (показанные товары, корзина) вырезаются из сообщений и
        переносятся в системный промпт: модель не должна их видеть в истории
        и копировать в свои ответы.
        """
        cart = current_cart_from_history(messages)
        shown = last_shown_products_from_history(messages)
        names = cart_names_from_history(messages)
        user_msg = next(
            (
                m["content"]
                for m in reversed(messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ),
            "",
        )
        convo = self._with_system_prompt(messages, cart, shown)
        try:
            await self._loop(convo, emit, cart, names, user_msg)
        except Exception as exc:  # noqa: BLE001 — пользователю нужен ответ, не 500
            logger.exception("Сбой агентного цикла")
            await emit("error", {"message": str(exc)})
        finally:
            await emit("done", {})

    def _with_system_prompt(
        self, messages: list[dict], cart: list[dict], shown: str | None
    ) -> list[dict]:
        clean = [
            (
                {**m, "content": strip_context_blocks(m.get("content"))}
                if isinstance(m.get("content"), str)
                else m
            )
            for m in messages
        ]
        context = ""
        if shown:
            context += f"\n\nПоказанные пользователю товары (последняя подборка): {shown}"
        if cart:
            context += "\n\nТекущая корзина: " + "; ".join(
                f"id={c['id']} ×{c['quantity']}" for c in cart
            )
        if messages and messages[0].get("role") == "system":
            head = {
                "role": "system",
                "content": str(messages[0].get("content", "")) + context,
            }
            return [head, *clean[1:]]
        return [{"role": "system", "content": SYSTEM_PROMPT + context}, *clean]

    async def _loop(
        self,
        convo: list[dict],
        emit: Emit,
        cart: list[dict],
        names: dict[int, str],
        user_msg: str,
    ) -> None:
        used_tools: set[str] = set()
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
                text = "".join(content_parts)
                # Гарантия: обещание корзины без вызова create_cart не проходит
                if text and "create_cart" not in used_tools:
                    if _CART_CLAIM_RE.search(text):
                        logger.info(
                            "Модель объявила изменение корзины без create_cart — "
                            "принудительный запрос вызова"
                        )
                        await emit("token", {"text": "\n\n_Оформляю корзину…_"})
                        convo.append({"role": "assistant", "content": text})
                        convo.append({"role": "user", "content": _CART_FORCED_NUDGE})
                        continue
                    if _CART_LINK_RE.search(text) and cart:
                        # обещана ссылка — пересобираем текущую корзину напрямую,
                        # без LLM: состав и количества детерминированы
                        logger.info("Пересборка корзины напрямую по обещанию ссылки")
                        await emit(
                            "tool",
                            {"name": "create_cart",
                             "label": TOOL_LABELS.get("create_cart", "create_cart")},
                        )
                        result, _ = await self._execute(
                            "create_cart",
                            json.dumps(
                                {
                                    "products": [
                                        {"id": c["id"], "quantity": c["quantity"]}
                                        for c in cart
                                    ]
                                }
                            ),
                            emit,
                            cart,
                            names,
                            user_msg,
                        )
                        await emit(
                            "tool_result", {"name": "create_cart", "result": result}
                        )
                        if not result.startswith("TOOL_ERROR"):
                            return
                        # прямая пересборка не удалась — пусть модель разбирается
                        await emit("token", {"text": "\n\n_Оформляю корзину…_"})
                        convo.append({"role": "assistant", "content": text})
                        convo.append({"role": "user", "content": _CART_FORCED_NUDGE})
                        continue
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
                used_tools.add(c["name"])
                logger.info("Вызов инструмента %s: %s", c["name"], c["args"][:300])
                await emit("tool", {"name": c["name"], "label": TOOL_LABELS.get(c["name"], c["name"])})
                result, cart = await self._execute(
                    c["name"], c["args"], emit, cart, names, user_msg
                )
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
        self,
        name: str,
        args_str: str,
        emit: Emit,
        cart: list[dict],
        names: dict[int, str] | None = None,
        user_msg: str = "",
    ) -> tuple[str, list[dict]]:
        """Выполняет инструмент; возвращает (текст для LLM, актуальная корзина).

        Корзина обновляется после успешного create_cart: второй вызов
        create_cart в этом же ходе объединяется с результатом первого,
        а не затирает его.
        """
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return f"TOOL_ERROR: неизвестный инструмент {name}", cart
        try:
            args = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError as exc:
            return f"TOOL_ERROR: невалидный JSON аргументов ({exc})", cart
        if not isinstance(args, dict):
            return "TOOL_ERROR: аргументы должны быть JSON-объектом", cart
        if name == "create_cart":
            # текущая корзина — из системного контекста; обёртка объединит состав
            args.setdefault("_current_cart", cart)
            args.setdefault("_cart_names", names or {})
            args.setdefault("_last_user_message", user_msg)
        try:
            result = await handler(args, self._mcp, emit)
        except Exception as exc:  # noqa: BLE001 — ошибка = текст для LLM
            logger.warning("Сбой инструмента %s: %r", name, exc)
            return f"TOOL_ERROR: {exc}", cart
        if name == "create_cart" and not result.startswith("TOOL_ERROR"):
            final = args.get("_final_cart")
            if final is not None:
                cart = final
        return result, cart
