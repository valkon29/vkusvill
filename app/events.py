"""Протокол событий агентского цикла.

run_agent() и обёртки инструментов отправляют события через emit(type, data);
веб-слой превращает их в SSE, eval — в лог, CLI — в печать в терминал.

Типы событий:
  token    {text}                        — дельта текста ассистента
  tool     {name, label}                 — статус-чип «Ищу товары…»
  products {items: [ProductCard]}        — карточки товаров для UI
  cart     {url, total, count, items}    — готовая корзина ВкусВилла
  done     {}                            — цикл завершён
  error    {message}                     — фатальная ошибка
"""

from typing import Any, Awaitable, Callable

# ProductCard: {id, name, price, old_price, unit, rating, image, url}
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

TOOL_LABELS = {
    "search_products": "Ищу товары…",
    "get_product_details": "Проверяю состав…",
    "find_product_analogs": "Подбираю аналоги…",
    "search_discounted_products": "Смотрю скидки…",
    "find_recipes": "Ищу рецепты…",
    "find_shops": "Ищу магазины…",
    "create_cart": "Собираю корзину…",
}
