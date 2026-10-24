"""Прогон eval-кейсов: python -m evals.runner [--case budget_snack]

Использует тот же агентный цикл, что и веб-сервис (без HTTP), с живыми
LLM и MCP — кроме кейсов с подменённым MCP. Результат: таблица в stdout
и отчёт docs/eval_report.md с логами падений.
"""

import argparse
import asyncio
import datetime
import sys

from app.agent import Agent
from app.config import settings
from app.mcp_client import MCPClientManager

from evals.cases import ALL_CASES, EvalCase


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def run_case(case: EvalCase, mcp) -> tuple[Recorder, list[dict]]:
    """Прогоняет промпты кейса подряд, возвращает лог событий и историю."""
    recorder = Recorder()
    history: list[dict] = []
    agent = Agent(mcp)
    for prompt in case.prompts:
        history.append({"role": "user", "content": prompt})
        await agent.run(history, recorder)
        # ответ ассистента добавляем по собранным токенам последнего хода
        # (agent.run не мутирует history — собираем сами)
        tail_tokens = []
        # берём токены после последнего user-события проще: перезапустим лог
        # ниже; здесь достаточно последнего фрагмента
        history.append({"role": "assistant", "content": _last_answer(recorder)})
    return recorder, history


def _last_answer(recorder: Recorder) -> str:
    """Текст после последнего события tool_result (финальный ответ хода)."""
    tokens: list[str] = []
    for t, d in recorder.events:
        if t == "tool_result":
            tokens = []
        elif t == "token":
            tokens.append(d["text"])
    return "".join(tokens).strip()


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
        if not failures or recorder is None:
            continue
        lines += [
            f"## Разбор ошибок: {case.name}",
            "",
            f"_{case.description}_",
            "",
            f"Промпт(ы): {' / '.join(repr(p) for p in case.prompts)}",
            "",
            "Нарушения:",
            *[f"- {f}" for f in failures],
            "",
            "Лог событий:",
        ]
        for t, d in recorder.events:
            if t == "tool_result":
                preview = str(d["result"])[:300].replace("\n", " ")
                lines.append(f"- `tool_result` **{d['name']}**: {preview}")
            elif t == "cart":
                lines.append(f"- `cart`: {d['url']} — {d['total']} ₽")
            elif t == "token":
                lines.append(f"- `token`: {d['text'][:120]!r}")
            else:
                lines.append(f"- `{t}`: {str(d)[:120]}")
        lines.append("")

    lines += [
        "## Найденные и исправленные ошибки (итерации прогона)",
        "",
        "### 1. Модель не собирала корзину после прямого запроса",
        "«Положи в корзину…» → модель переспрашивала «собрать корзину?» вместо вызова ",
        "create_cart. **Исправление:** правило в системном промпте — при прямом запросе ",
        "перечислить состав и сразу вызвать create_cart в том же ответе.",
        "",
        "### 2. dietary_breakfast: модель шла в рецепты вместо товаров",
        "На «Подбери завтрак без лактозы и собери корзину» модель вызывала find_recipes, ",
        "получала рецепты с лактозой и отказывалась помогать. Системный промпт не помогал. ",
        "**Исправление:** усилены описания самих инструментов (search_products — для ",
        "«подбери завтрак/перекус», find_recipes — «только про готовку») — выбор ",
        "инструмента у LLM определяется в первую очередь их описаниями.",
        "",
        "### 3. Модель обещала действие и заканчивала ход",
        "«Сейчас найду подходящие варианты…» — и конец ответа без вызова инструмента. ",
        "**Исправление:** запрет в промпте заканчивать ответ обещанием действия.",
        "",
        "## Ограничения",
        "",
        "- Проверки детерминированные (суммы, составы, источники id, URL корзин); ",
        "субъективное качество текста не оценивается.",
        "- Кейсы гоняются по живым данным ВкусВилла: цены и ассортимент меняются,",
        "  поэтому бюджетные проверки могут флуктуировать.",
        "- LLM-судья не используется — осознанное ограничение объёма РК1.",
    ]

    with open("docs/eval_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
