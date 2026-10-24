# 🥑 Вкусный ассистент — AI-помощник по продуктам ВкусВилла

Универсальный чат-ассистент: описываете потребность свободно («перекус на работу,
бюджет 500 ₽, без лактозы») — ассистент ищет реальные товары, проверяет составы,
показывает карточки и собирает корзину ВкусВилла со ссылкой.

Личный проект для курса МФТИ · VK Education (РК1, октябрь 2026).

## Как это работает

- **LLM** — YandexGPT (OpenAI-совместимый API) ведёт диалог и решает, какие инструменты вызывать
- **MCP** — товары, рецепты и корзина получаются с официального MCP-сервера ВкусВилла
  `https://mcp.vkusvill.ru/mcp` через официальный SDK
- **Инструменты** — 7 обёрток над MCP-инструментами (экономия токенов, валидация,
  события для UI)
- **Веб** — FastAPI + SSE-стриминг, vanilla JS без сборки; история в localStorage

Подробная схема интеграции: [docs/mcp_integration_schema.md](docs/mcp_integration_schema.md).
Результаты eval: [docs/eval_report.md](docs/eval_report.md).

## Быстрый старт

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # заполните LLM_API_KEY (Yandex AI Studio)
.venv/bin/uvicorn app.main:app --reload
# откройте http://127.0.0.1:8000
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `LLM_API_KEY` | ключ Yandex AI Studio |
| `LLM_BASE_URL` | `https://ai.api.cloud.yandex.net/v1` |
| `MODEL_NAME` | `gpt://<folder_id>/yandexgpt/latest` |
| `MCP_URL` | `https://mcp.vkusvill.ru/mcp` |
| `MAX_TOOL_ITERATIONS` | лимит шагов агентного цикла (6) |
| `TOOL_RESULT_MAX_CHARS` | обрезка результатов инструментов (6000) |

## Тесты и eval

```bash
.venv/bin/python -m pytest tests/ -q        # оффлайн: fake LLM + fake MCP, без ключей
.venv/bin/python -m evals.runner            # 7 кейсов на живых LLM + MCP, отчёт в docs/
```

Оффлайн-тесты покрывают агентный цикл (накопление фрагментированных tool calls,
невалидный JSON, лимит итераций), обёртки (валидация корзины, дедуп, зачистка) и
SSE-эндпоинт. Eval-кейсы: бюджет, аллергены, скидки, пустой результат, недоступный
MCP, уточняющие вопросы — все проверки детерминированные.

## Деплой (Render)

Конфигурация в `render.yaml`: web-сервис, Python 3.12, uvicorn на `$PORT`.
`LLM_API_KEY` задаётся в дашборде Render. На free-тарифе инстанс засыпает после
15 минут простоя — на время демо сервис держится пингом `/health` (UptimeRobot,
раз в 10 минут).

## Структура

```
app/
├── main.py        # FastAPI: SSE-эндпоинт /api/chat, статика, lifespan
├── agent.py       # агентный цикл (LLM → инструменты → LLM → ответ)
├── mcp_client.py  # MCP-сессия: ретраи, auto-heal, троттлинг
├── prompts.py     # системный промпт
├── tools/         # обёртки MCP-инструментов + схемы для LLM
└── web/           # чат-UI (Jinja2 + vanilla JS)
evals/             # eval-кейсы и раннер (docs/eval_report.md)
tests/             # оффлайн-тесты (pytest)
docs/              # схема MCP-интеграции, eval-отчёт
scripts/           # spike-скрипты (smoke_mcp, smoke_agent)
```

## Ограничения (РК1)

- Цены и наличие — по данным MCP-сервера без региональной персонализации
  (персонализация требует OAuth; заявлено в roadmap вместе с историей заказов
  и личными скидками)
- Субъективное качество текстов не оценивается — eval только детерминированный
- Rate limit MCP-сервера: 60 запросов / 3 с — при высокой нагрузке возможны паузы
