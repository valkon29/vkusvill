"""Схемы инструментов, которые видит LLM (формат OpenAI function calling).

Обёртки, а не прямой passthrough MCP-инструментов: контролируем объём
результатов (токены), прячем служебные параметры (например, 9 обязательных
аргументов vkusvill_recipes) и валидируем инварианты (корзина 1–20 позиций).
"""

SORT_OPTIONS = ["popularity", "rating", "price_asc", "price_desc", "new"]

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Поиск товаров ВкусВилл по текстовому запросу. Возвращает "
                "id, название, цену, рейтинг и ссылку. Вызывай перед тем, "
                "как рекомендовать или добавить любой товар. Подходит для "
                "запросов «подбери завтрак/перекус/ужин», «собери корзину», "
                "«что взять на работу» — ищи товары, а не рецепты."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос, например «сырники» или «кофе без кофеина»",
                    },
                    "sort": {
                        "type": "string",
                        "enum": SORT_OPTIONS,
                        "description": "Сортировка; price_asc полезна при ограниченном бюджете",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Сколько товаров вернуть (по умолчанию 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": (
                "Подробности о товаре по id из поиска: состав, КБЖУ, вес, "
                "цена, наличие. Обязательно проверяй состав, если у "
                "пользователя аллергия или пищевое ограничение."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "minimum": 1},
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_product_analogs",
            "description": (
                "Похожие товары по id из поиска. Используй, когда товар не "
                "подошёл (не устраивает цена, состав) и нужна замена."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "minimum": 1},
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_discounted_products",
            "description": (
                "Товары со скидками. type=card — скидка по карте лояльности, "
                "type=quantity — скидка при покупке нескольких штук. "
                "Полезно при ограниченном бюджете."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["card", "quantity"]},
                    "page": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_recipes",
            "description": (
                "Поиск рецептов ВкусВилл — ТОЛЬКО когда пользователь "
                "спрашивает про готовку («что приготовить», «рецепт», "
                "«как приготовить»). Не используй для подбора продуктов или "
                "сборки корзины. Возвращает ингредиенты (с id товаров для "
                "покупки), время приготовления, сложность, порции. Можно "
                "исключить аллергены."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Например «курица» или «завтрак»"},
                    "exclude_allergens": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Названия аллергенов для исключения: орехи, глютен, лактоза, лук, яйца, сахар, кунжут, горчица",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_shops",
            "description": (
                "Список магазинов ВкусВилл (адрес, часы работы, рейтинг). "
                "Вызывай, когда пользователь спрашивает, где забрать заказ."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_cart",
            "description": (
                "Собрать корзину ВкусВилл и получить ссылку на неё. Переданные "
                "товары автоматически ОБЪЕДИНЯЮТСЯ с текущей корзиной (см. раздел "
                "контекста): добавить товар — передай только его; изменить "
                "количество — тот же id с новым количеством; удалить товар — "
                "передай его id с quantity=0. Не упомянутые позиции всегда "
                "остаются в корзине. Перед вызовом кратко перечисли "
                "пользователю, что кладёшь, и назови сумму."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "products": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer", "minimum": 1},
                                "quantity": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 40,
                                    "description": "Количество (шт или кг). 0 — удалить товар из корзины",
                                },
                            },
                            "required": ["id", "quantity"],
                        },
                    },
                },
                "required": ["products"],
            },
        },
    },
]
