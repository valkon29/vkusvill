"""Прогон eval-кейсов: python -m evals.runner [--case budget_snack]

Использует тот же агентный цикл, что и веб-сервис (без HTTP), с живыми
LLM и MCP — кроме кейсов с подменённым MCP. Результаты:
  - таблица в stdout;
  - docs/eval_report.md — таблица + полный лог каждого кейса
    (промпты, вызовы инструментов с аргументами, ответы ВкусВилла,
    финальные ответы модели, корзины);
  - docs/showcase_lunch.md — эталонный разбор примера «от запроса до
    корзины» для кейса lunch_showcase.
"""

import argparse
import asyncio
import datetime
import json
import sys

from app.agent import Agent
from app.config import settings
from app.mcp_client import MCPClientManager

from evals.cases import ALL_CASES, EvalCase

# кейсы, для которых раннер пишет отдельный документ-разбор
SHOWCASE_DOCS = {"lunch_showcase": "docs/showcase_lunch.md"}

_SHOWCASE_INTRO = """# Эталонный пример: от запроса до корзины

Автогенерируется раннером eval из фактического лога прогона — всё ниже
реальные ответы модели и данные ВкусВилла на момент прогона.

**Запрос покупателя (3 хода):**

1. «Собери полноценный обед для одного взрослого человека. Сразу собери корзину.»
2. «Замени салат на винегрет»
3. «Сколько углеводов суммарно содержится в продуктах из корзины?»

**Что считается подходящим результатом** (проверяется детерминированно):

- после хода 1 собрана корзина (≥ 3 позиций: основное + гарнир/овощи + напиток),
  все id — из результатов инструментов, ссылка ведёт на vkusvill.ru;
- после хода 2 винегрет в корзине, салата нет (если был), остальные позиции
  хода 1 сохранились — корзина только накапливается;
- в ходе 3 по каждой позиции корзины запрошен свежий состав/КБЖУ
  (get_product_details), суммарные углеводы в ответе сходятся с пересчётом
  по весу упаковок из ответов ВкусВилла (допуск ±30% на округления).
"""


# Раздел про допустимые ошибки пишется в каждый отчёт — примеры из живых
# диалогов с прода, сознательно не исправляются (обоснование в тексте)
_KNOWN_ISSUES = """## Известные ошибки (пока не исправлены)

**1. Сахар назван «на 100 г», а не на упаковку.** Полный живой диалог с прода:

> **Пользователь:** Напиток травяной с мятой и лимоном витаминизированный
>
> **Ассистент:** Вот что я нашел по вашему запросу:
>
> > Напиток травяной с мятой и лимоном витаминизированный, 330 мл — 183 руб.
> > Напиток травяной с грушей и черной смородиной витаминизированный, 330 мл — 165 руб.
> > Напиток «Чай зеленый» со вкусом лимона, 1,2 л — 273 руб.
> > Напиток «Чай зеленый» со вкусом лимона, 450 мл — 123 руб.
> > Напиток «Чай зеленый с ромашкой и мятой», 500 мл — 146 руб.
>
> **Пользователь:** добавь в корзину 1ый
>
> **Ассистент:** Напиток травяной с мятой и лимоном витаминизированный, 330 мл,
> добавлен в корзину. Итоговая сумма: 183 рубля.
> Ссылка на корзину ВкусВилла появится под сообщением.
>
> **Пользователь:** сколько в нём сахара?
>
> **Ассистент:** В напитке травяном с мятой и лимоном витаминизированном,
> 330 мл содержится 2.7 г сахара.

2.7 г — сахар **на 100 мл** из строки КБЖУ («в том числе сахара — 2.7 г»),
а не на упаковку: в бутылке 330 мл сахара ≈ 8.9 г. Модель взяла значение
«на 100 г» без пересчёта на объём упаковки — тот же класс ошибки, что раньше
был у углеводов (тогда модель складывала «на 100 г» вместо пересчёта по весу).

Почему пока не исправляем: для углеводов обёртка уже предвычисляет
`carbs_in_package_g` (КБЖУ/100 г × вес упаковки) — это критерий эталонного
примера, и ошибка там была закрыта инвариантом в коде. Для сахара аналогичное
предвычисление не делается: вопрос о сахаре не входит в проверяемые сценарии,
а строка «в том числе сахара» есть не у всех производителей (см. допустимую
погрешность по нескольким производителям в README). Фикс — в roadmap:
`sugar_in_package_g` по аналогии с углеводами.
"""


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def run_case(case: EvalCase, mcp) -> tuple[Recorder, list[dict]]:
    """Прогоняет промпты кейса подряд, возвращает лог событий и историю.

    Перед каждым промптом в лог ставится маркер хода ("turn", ...) — по нему
    проверки и отчёт нарезают диалог на ходы. В ответ ассистента дописываются
    служебные блоки состояния — ровно то, что делает веб-фронтенд (app.js):
    [Показаны товары: …] и [В корзине: …]. Без них многоходовые кейсы бегут
    с пустой корзиной (состояние корзины живёт в истории клиента)."""
    recorder = Recorder()
    history: list[dict] = []
    agent = Agent(mcp)
    for prompt in case.prompts:
        recorder.events.append(("turn", {"prompt": prompt}))
        turn_start = len(recorder.events)
        history.append({"role": "user", "content": prompt})
        await agent.run(history, recorder)
        # agent.run не мутирует history — ответ ассистента собираем сами
        history.append({
            "role": "assistant",
            "content": _answer_with_context(
                recorder.events[turn_start:], _last_answer(recorder)
            ),
        })
    return recorder, history


def _answer_with_context(turn_events: list[tuple[str, dict]], answer: str) -> str:
    """Дописывает к ответу служебные блоки состояния — формат блока в точности
    как в app/web/static/app.js (фронтенд — носитель состояния корзины)."""
    notes = []
    shown = [p for t, d in turn_events if t == "products" for p in d.get("items", [])]
    if shown:
        notes.append("[Показаны товары: " + "; ".join(
            f"id={p['id']} {p.get('name') or ''}" for p in shown
        ) + "]")
    carts = [d for t, d in turn_events if t == "cart"]
    if carts:
        notes.append("[В корзине: " + "; ".join(
            f"id={i['id']} {i.get('name') or ''} ×{i.get('quantity', 1)}"
            for i in carts[-1].get("items", [])
        ) + "]")
    return answer + ("\n\n" + "\n".join(notes) if notes else "")


def _last_answer(recorder: Recorder) -> str:
    """Текст после последнего tool_result ИЛИ маркера хода — финальный
    ответ текущего хода (в ходе без вызовов инструментов не должен
    подтягивать ответ предыдущего хода)."""
    tokens: list[str] = []
    for t, d in recorder.events:
        if t in ("tool_result", "turn"):
            tokens = []
        elif t == "token":
            tokens.append(d["text"])
    return "".join(tokens).strip()


def _render_events(events: list[tuple[str, dict]], clip: int | None = 500) -> list[str]:
    """Человекочитаемая трассировка хода: сохраняет порядок «модель сказала →
    вызвала инструмент с параметрами → ВкусВилл вернул → корзина».

    clip=None — полные ответы ВкусВилла (для разбора примера), иначе превью
    в clip символов (для eval-отчёта по всем кейсам)."""
    lines: list[str] = []
    text_buf: list[str] = []

    def flush_text():
        if text_buf:
            lines.append("")
            lines.append("**Модель:**")
            lines.append("")
            lines.append("> " + "".join(text_buf).replace("\n", "\n> "))
            lines.append("")
            text_buf.clear()

    def render_result(result: str) -> None:
        try:  # полный ответ — аккуратным JSON-блоком
            pretty = json.dumps(json.loads(result), ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 — не JSON, показываем как есть
            pretty = result
        if clip is not None and len(pretty) > clip:
            lines.append(f"  - ВкусВилл вернул (сокращено): `{pretty.replace(chr(10), ' ')[:clip]}…`")
        else:
            lines.append("  - ВкусВилл вернул:")
            lines.append("")
            lines.extend(["  ```json"] + ["  " + ln for ln in pretty.splitlines()] + ["  ```"])

    for t, d in events:
        if t == "token":
            text_buf.append(d["text"])
        elif t == "tool":
            flush_text()
            args = d.get("args", "")
            lines.append(f"- `{d['name']}` — параметры: `{args or '{}'}`")
        elif t == "tool_result":
            render_result(str(d["result"]))
        elif t == "products":
            names = "; ".join((i.get("name") or "?") for i in d.get("items", []))
            lines.append(f"  - в чат показаны карточки: {names}")
        elif t == "cart":
            items = "; ".join(
                f"{i.get('name') or i.get('id')} ×{i.get('quantity')} "
                f"({(i.get('price') or 0):.0f} ₽)"
                for i in d.get("items", [])
            )
            lines.append(f"  - **корзина**: {d.get('url')} — итого "
                         f"**{d.get('total')} ₽**, позиций: {d.get('count')}")
            lines.append(f"    - состав: {items}")
        elif t == "error":
            lines.append(f"- `error`: {d.get('message')}")
    flush_text()
    return lines


def _render_case(case: EvalCase, failures: list[str], events,
                 clip: int | None = 500) -> list[str]:
    lines = [
        f"## {case.name} — {'✅ PASS' if not failures else '❌ FAIL'}",
        "",
        f"_{case.description}_",
        "",
    ]
    descs = [getattr(c, "description", None) for c in case.checks]
    if any(descs):
        lines += [
            "Проверки кейса:",
            *[
                f"- {d}" if d else f"- _{getattr(c, '__name__', 'check')} — без описания_"
                for c, d in zip(case.checks, descs)
            ],
            "",
        ]
    if failures:
        lines += ["Нарушения:", *[f"- **{f}**" for f in failures], ""]
    for idx, turn in enumerate(_split_turns(events), start=1):
        prompt = turn[0][1].get("prompt", "")
        lines += [f"### Ход {idx} — пользователь: «{prompt}»", ""]
        body = turn[1:]
        lines += _render_events(body, clip=clip) or ["_(нет событий)_"]
        lines.append("")
    return lines


def _split_turns(events) -> list[list[tuple[str, dict]]]:
    turns: list[list[tuple[str, dict]]] = []
    current: list[tuple[str, dict]] = []
    for ev in events:
        if ev[0] == "turn":
            if current:
                turns.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        turns.append(current)
    return turns


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="имя одного кейса вместо всех")
    args = parser.parse_args()

    cases = [c for c in ALL_CASES if c.name == args.case] if args.case else ALL_CASES
    if not cases:
        print(f"Кейс {args.case!r} не найден. Доступны: {', '.join(c.name for c in ALL_CASES)}")
        return 2

    live_mcp = MCPClientManager(settings.mcp_url)
    await live_mcp.start()

    results = []  # (case, failures, recorder)
    try:
        for case in cases:
            mcp = case.mcp_factory() if case.mcp_factory else live_mcp
            print(f"▶ {case.name}: {case.description}", flush=True)
            try:
                recorder, history = await run_case(case, mcp)
                failures = []
                for check in case.checks:
                    try:
                        failures.extend(check(recorder.events, history))
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"проверка упала: {exc!r}")
                status = "PASS" if not failures else "FAIL"
                print(f"  {status}" + (f": {failures[0]}" if failures else ""), flush=True)
                results.append((case, failures, recorder))
            except Exception as exc:  # noqa: BLE001
                print(f"  CRASH: {exc!r}", flush=True)
                results.append((case, [f"агент упал: {exc!r}"], None))
    finally:
        await live_mcp.close()

    write_report(results)
    _write_showcase_docs(results)
    failed = sum(1 for _, f, _ in results if f)
    print(f"\n{len(results) - failed}/{len(results)} кейсов пройдено. Отчёт: docs/eval_report.md")
    return 0 if not failed else 1


def write_report(results) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Eval-отчёт: Вкусный ассистент",
        "",
        f"Дата прогона: {now} · модель: `{settings.model_name}` · MCP: `{settings.mcp_url}`",
        "",
        "| Кейс | Статус | Нарушения |",
        "|---|---|---|",
    ]
    for case, failures, _ in results:
        status = "✅ PASS" if not failures else "❌ FAIL"
        lines.append(f"| {case.name} | {status} | {'; '.join(failures) or '—'} |")
    lines.append("")
    for case, failures, recorder in results:
        if recorder is None:
            lines += [f"## {case.name} — ❌ CRASH", "", "Агент упал, лога нет.", ""]
            continue
        lines += _render_case(case, failures, recorder.events)
    lines += [_KNOWN_ISSUES.rstrip(), ""]
    lines += [
        "## Ограничения",
        "",
        "- Проверки детерминированные (суммы, составы, источники id, URL корзин); ",
        "субъективное качество текста не оценивается.",
        "- Кейсы гоняются по живым данным ВкусВилла: цены и ассортимент меняются,",
        "  поэтому бюджетные проверки могут флуктуировать.",
        "- LLM-судья не используется — осознанное ограничение объёма РК1.",
        "",
        "Исторический разбор найденных и исправленных ошибок — в README.md.",
    ]

    with open("docs/eval_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_showcase_docs(results) -> None:
    for case, failures, recorder in results:
        path = SHOWCASE_DOCS.get(case.name)
        if not path or recorder is None:
            continue
        status = "✅ Все проверки пройдены" if not failures else "❌ Есть нарушения"
        lines = [_SHOWCASE_INTRO, f"**Прогон:** {status}", ""]
        # полные ответы ВкусВилла — это разбор «что именно вернул MCP»,
        # сокращения только в eval-отчёте по всем кейсам
        lines += _render_case(case, failures, recorder.events, clip=None)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Разбор примера: {path}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
