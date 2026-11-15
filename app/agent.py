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
    ids_named_for_removal,
    last_shown_products_from_history,
    strip_context_blocks,
)

logger = logging.getLogger(__name__)

# Ответ утверждает изменение корзины («добавлено в корзину», «убрал из корзины»).
# Только утвердившиеся формы: инфинитивы («положить в корзину?» — вопрос) не считаются.
# Две ориентации: «добавил … в корзину» и «В корзину добавлен …».
_CART_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:добавлен(?:а|о|ы)?|добав(?:лю|им|ила?|ишь)"
    r"|положен(?:а|о|ы)?|полож(?:ил|у|им)"
    r"|собран(?:а|о|ы)?|собрал(?:а|о|и)?|собер(?:у|ёшь|ём|ет)"
    r"|убран(?:а|о|ы)?|убрал(?:а|о|и)?|убер(?:у|ёшь|ём|ет)"
    r"|удалён(?:а|о|ы)?|удален(?:а|о|ы)?|удал(?:ил|ю|им))"
    r"[^\n]{0,60}корзин"
    r"|корзин\w*[^\n]{0,20}"
    r"(?:добавлен(?:а|о|ы)?|добав(?:лю|им|ила?|ишь)"
    r"|положен(?:а|о|ы)?|убран(?:а|о|ы)?"
    r"|удалён(?:а|о|ы)?|удален(?:а|о|ы)?|удал(?:ил|ю|им)"
    r"|исключён(?:а|о|ы)?|исключен(?:а|о|ы)?)"
    r")",
    re.IGNORECASE,
)
# Обещание ссылки на корзину без вызова create_cart («не появится» — не обещание)
_CART_LINK_RE = re.compile(
    r"ссылк\w*\s+на\s+корзину[^\n]{0,40}(?<!не )появ",
    re.IGNORECASE,
)
# Прямой запрос корзины от пользователя («собери корзину», «положи в корзину») —
# повелительное наклонение; вопросы о состоянии («что в корзине?») не считаются
_CART_REQUEST_RE = re.compile(
    r"(?:собер[иё]|собират\w*|оформи\w*|созда\w*|сдела\w*"
    r"|полож[иь]|клад[иь]|добав[иь]|закаж[иь])\w{0,3}[^\n!?]{0,40}корзин"
    r"|корзин\w*[^\n!?]{0,20}(?:собер[иё]|оформи\w*)",
    re.IGNORECASE,
)
# Вопросительные зачины предложения: «Хотите, чтобы я собрал корзину?» —
# это предложение модели, а не утверждение об изменении
_QUESTION_START_RE = re.compile(
    r"^\s*(?:хочешь|хотите|хотим|хотели|хочется|давай|давайте|может|можем|могу"
    r"|могла|могли|будете|будешь|будем|готовы|готов|нужно|надо|стоит"
    r"|интересует|интересно)",
    re.IGNORECASE,
)


def _is_cart_claim(text: str) -> bool:
    """True, только если текст УТВЕРЖДАЕТ изменение корзины.

    «Суп добавлен в корзину» — утверждение (нужен вызов инструмента),
    «Хотите, чтобы я собрал корзину?» — вопрос модели, повтора не требуется.
    Проверяем предложение с совпадением: вопросительный зачин или
    сослагательное наклонение («чтобы я собрал») отменяют срабатывание.
    """
    for m in _CART_CLAIM_RE.finditer(text):
        # границы предложения, в которое попало совпадение
        start = max(text.rfind(sep, 0, m.start()) for sep in ".!?\n") + 1
        ends = [text.find(sep, m.end()) for sep in ".!?\n"]
        end = min((e for e in ends if e != -1), default=len(text))
        if _QUESTION_START_RE.match(text[start:end]):
            continue
        # «…, чтобы я собрал корзину» — глагол в придаточном цели/условия
        if re.search(r"чтобы\s+(?:я\s+)?\S*\s*$", text[start:m.start()]):
            continue
        return True
    return False


# Псевдо-вызов инструмента текстом: модель иногда выдаёт tool call
# как текст («[TOOL_CALL_START]create_cart {json}») вместо канала tool_calls —
# найдено живым прогоном eval
_PSEUDO_CALL_RE = re.compile(r"\[TOOL_CALL_START\]\s*(\w+)", re.IGNORECASE)


def _balance_json(s: str) -> str:
    """Дописывает недостающие закрывающие кавычки/скобки: модель иногда
    обрезает JSON псевдо-вызова (найдено живым прогоном — не хватало
    финальной «}»). Хвост после сбалансированной части игнорируется."""
    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_str:
        s += '"'
    return s + "".join(reversed(stack))


def _first_json_object(s: str) -> dict | None:
    """Первый JSON-объект в строке, или None.

    Объект может обрезаться (модель не дописала скобки) и за ним может идти
    посторонний текст. Сканируем до первой сбалансированной `}`; если до неё
    встретился символ, невозможный в JSON вне строк (кириллица) — объект
    обрезан: берём кусок до него и дописываем скобки по стеку.
    """
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    cut: int | None = None
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                cut = i + 1
                break
        elif not (ch.isascii() and (ch.isalnum() or ch in ":,-+ \t\r\n")):
            cut = i  # посторонний текст — JSON оборвался раньше
            break
    if cut is None:
        cut = len(s)
    try:
        obj, _ = json.JSONDecoder().raw_decode(_balance_json(s[start:cut]))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_pseudo_call(text: str, tool: str) -> str | None:
    """Аргументы псевдо-вызова инструмента из текста ответа, или None.

    Модель пишет «[TOOL_CALL_START]create_cart {"products": …}» текстом —
    аргументы при этом корректны (или обрезаны — восстанавливаются
    балансировкой скобок), вызов можно выполнить детерминированно.
    Конец псевдо-вызова — следующий маркер.
    """
    for m in _PSEUDO_CALL_RE.finditer(text):
        if m.group(1).lower() != tool:
            continue
        rest = text[m.end():]
        stop = rest.find("[TOOL_CALL")
        if stop != -1:
            rest = rest[:stop]
        obj = _first_json_object(rest)
        if obj is not None:
            return json.dumps(obj, ensure_ascii=False)
    return None


# Принудительная поправка, когда модель объявила изменение корзины или
# пообещала ссылку, не вызвав create_cart
_CART_FORCED_NUDGE = (
    "СИСТЕМНОЕ СООБЩЕНИЕ: в предыдущем ответе ты сообщил об изменении корзины "
    "или пообещал ссылку на неё, но не вызвал инструмент create_cart — ссылки "
    "у пользователя нет. Вызови create_cart сейчас: передай позиции текущей "
    "корзины из раздела контекста с их же количествами, а товары, которые "
    "пользователь просил убрать, — с quantity=0. НИКОГДА не передавай id, "
    "которых нет в разделе контекста или в результатах инструментов: "
    "выдуманный id приводит к ошибке всей сборки. Если нужного id в контексте "
    "нет — сначала вызови search_products. Если корзина пуста, а ты только что "
    "предлагал товары — собери корзину из них и вызови create_cart. Если и "
    "предложений нет — честно скажи, что корзина ещё не собрана, и предложи товары."
)

# Поправка, когда пользователь ПРЯМО попросил собрать корзину, а модель
# переспросила («Собираем корзину?») или закончила ход без create_cart
_CART_REQUEST_NUDGE = (
    "СИСТЕМНОЕ СООБЩЕНИЕ: пользователь прямым текстом просил собрать корзину — "
    "уточняющий вопрос не нужен, просьбу нужно выполнить. Вызови create_cart "
    "сейчас: передай товары, которые ты предлагал в ответе (по 1 шт., если "
    "количества не оговаривались), плюс позиции текущей корзины из раздела "
    "контекста. НИКОГДА не передавай id, которых нет в контексте или в "
    "результатах инструментов. Если подходящих товаров в ответе не было — "
    "сначала вызови search_products и собери корзину из них."
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
                    # Гарантия: псевдо-вызов create_cart текстом выполняется
                    # напрямую — аргументы модель уже написала
                    pseudo_args = _extract_pseudo_call(text, "create_cart")
                    if pseudo_args is not None:
                        logger.info(
                            "Модель выдала create_cart текстом — выполняю напрямую"
                        )
                        await emit(
                            "tool",
                            {"name": "create_cart",
                             "label": TOOL_LABELS.get("create_cart", "create_cart"),
                             "args": pseudo_args},
                        )
                        result, cart = await self._execute(
                            "create_cart", pseudo_args, emit, cart, names, user_msg
                        )
                        await emit(
                            "tool_result", {"name": "create_cart", "result": result}
                        )
                        if not result.startswith("TOOL_ERROR"):
                            return
                        # не выполнилось (невалидные id и т.п.) — идём к поправкам
                    if _is_cart_claim(text):
                        logger.info(
                            "Модель объявила изменение корзины без create_cart — "
                            "принудительный запрос вызова"
                        )
                        await emit("token", {"text": "\n\n_Оформляю корзину…_"})
                        convo.append({"role": "assistant", "content": text})
                        convo.append({"role": "user", "content": _CART_FORCED_NUDGE})
                        continue
                    if _CART_LINK_RE.search(text):
                        # обещана ссылка — пересобираем текущую корзину напрямую,
                        # без LLM: состав и количества детерминированы.
                        # Товары, которые пользователь просил убрать,
                        # из пересбора исключаем. Пустая корзина детерминированно
                        # не пересобирается (нечего) — модель должна сама вызвать
                        # create_cart по поправке (найдено живым прогоном: модель
                        # перечисляла товары с суммой и обещала ссылку, не вызвав
                        # инструмент)
                        drop = ids_named_for_removal(names, user_msg)
                        rebuild = [c for c in cart if c["id"] not in drop]
                        if rebuild:
                            logger.info("Пересборка корзины напрямую по обещанию ссылки")
                            await emit(
                                "tool",
                                {"name": "create_cart",
                                 "label": TOOL_LABELS.get("create_cart", "create_cart"),
                                 "args": json.dumps(
                                     {"products": [
                                         {"id": c["id"], "quantity": c["quantity"]}
                                         for c in rebuild
                                     ]}, ensure_ascii=False
                                 )},
                            )
                            result, _ = await self._execute(
                                "create_cart",
                                json.dumps(
                                    {
                                        "products": [
                                            {"id": c["id"], "quantity": c["quantity"]}
                                            for c in rebuild
                                        ]
                                    }
                                ),
                                emit,
                                rebuild,
                                names,
                                user_msg,
                            )
                            await emit(
                                "tool_result", {"name": "create_cart", "result": result}
                            )
                            if not result.startswith("TOOL_ERROR"):
                                return
                        # пустая корзина или пересбор не удался — пусть модель
                        # разбирается, но не молча: поправка обязательна
                        await emit("token", {"text": "\n\n_Оформляю корзину…_"})
                        convo.append({"role": "assistant", "content": text})
                        convo.append({"role": "user", "content": _CART_FORCED_NUDGE})
                        continue
                    # Гарантия: прямой запрос корзины не заканчивается
                    # переспросом — найдено живым прогоном (модель отвечала
                    # «Собираем корзину?» на «…и собери корзину»)
                    if _CART_REQUEST_RE.search(user_msg):
                        logger.info(
                            "Пользователь просил корзину, create_cart не вызван — "
                            "принудительный запрос вызова"
                        )
                        await emit("token", {"text": "\n\n_Оформляю корзину…_"})
                        convo.append({"role": "assistant", "content": text})
                        convo.append({"role": "user", "content": _CART_REQUEST_NUDGE})
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
                await emit("tool", {"name": c["name"], "label": TOOL_LABELS.get(c["name"], c["name"]), "args": c["args"]})
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
