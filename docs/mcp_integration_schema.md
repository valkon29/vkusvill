# Схема интеграции с MCP

## Общая архитектура

```
Браузер (vanilla JS, fetch + ReadableStream для SSE)
   │  POST /api/chat {messages: [...]}   — вся история, сервер stateless
   ▼
FastAPI (app/main.py)
   │  lifespan: единый MCPClientManager
   ▼
Агентный цикл (app/agent.py), максимум 6 итераций
   │
   ├──► YandexGPT (OpenAI-совместимый API, стриминг + function calling)
   │        ├── content-дельты ──────────► SSE-событие token
   │        └── tool_calls ──► выбор обёртки
   │
   ├──► Обёртки инструментов (app/tools/vkusvill.py) — 7 шт.
   │        │   валидация аргументов, зачистка результатов (токены),
   │        │   эмит событий products / cart для UI
   │        ▼
   └──► MCPClientManager (app/mcp_client.py)
            │   Semaphore(3), ретраи, auto-heal сессии
            ▼
        MCP-сервер ВкусВилла — https://mcp.vkusvill.ru/mcp
        (streamable HTTP, протокол 2025-03-26, stateless,
         rate limit 60 запросов / 3 с)
```

## Почему обёртки, а не прямой passthrough

| Причина | Пример |
|---|---|
| Экономия токенов | поиск отдаёт LLM только id/название/цену/рейтинг; картинки уходят лишь в UI |
| Скрытие служебных параметров | `vkusvill_recipes` требует 9 обязательных аргументов — обёртка заполняет их, LLM видит только `query` и `exclude_allergens` |
| Инварианты в коде, а не в промпте | `create_cart` дедуплицирует позиции, ограничивает 1–20 и количество 0.01–40 |
| События для UI | обёртка эмитит `products`/`cart` — фронтенду не нужно парсить текст LLM |

## Соответствие инструментов

| Инструмент LLM | MCP-инструмент | Особенности обёртки |
|---|---|---|
| `search_products` | `vkusvill_products_search` | mode=custom (+images для карточек), limit ≤ 10, vvonly=0 |
| `get_product_details` | `vkusvill_product_details` | состав и КБЖУ — для проверки аллергенов |
| `find_product_analogs` | `vkusvill_product_analogs` | замены «на безлактозное» |
| `search_discounted_products` | `vkusvill_products_discount` | type: card / quantity |
| `find_recipes` | `vkusvill_recipes` | заполняет 9 обязательных параметров; кэширует id аллергенов |
| `find_shops` | `vkusvill_shops` | адреса и часы работы |
| `create_cart` | `vkusvill_cart_link_create` | дедуп, валидация, пересчёт суммы через product_details |

**Не используем** (осознанное ограничение РК1): `vkusvill_product_barcode` (нишевой) и
три OAuth-инструмента `vkusvill_orders_history`, `vkusvill_discounts_personal`,
`vkusvill_product_lp` — требуют OAuth 2.0 + PKCE; заявлены как roadmap.

## Отказоустойчивость

- **Rate limit 60/3с**: вызовы инструментов последовательны, в менеджере `Semaphore(3)`,
  при 429 — пауза 3 с и ретрай.
- **Сбой сессии/сервера**: `MCPClientManager` переоткрывает сессию (сервер stateless —
  это всегда безопасно), после 3 попыток ошибка возвращается LLM как `TOOL_ERROR: …` —
  модель извиняется и предлагает альтернативу (покрыто eval-кейсом `source_down`).
- **Невалидные аргументы tool call**: JSON-ошибка возвращается модели как результат
  инструмента — она самокорректируется на следующей итерации.
