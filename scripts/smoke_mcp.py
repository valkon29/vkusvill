"""Spike: проверка живого MCP-сервера ВкусВилла через официальный SDK.

Запуск: python -m scripts.smoke_mcp
Печатает результаты поиска и реальную ссылку на корзину.
"""

import asyncio
import json

from app.config import settings
from app.mcp_client import MCPClientManager


async def main() -> None:
    manager = MCPClientManager(settings.mcp_url)
    await manager.start()
    try:
        print("=== vkusvill_products_search: 'сырники' ===")
        raw = await manager.call_tool(
            "vkusvill_products_search",
            {"q": "сырники", "mode": "short", "limit": 3, "page": 1, "sort": "popularity"},
        )
        data = json.loads(raw)
        # конверт сервера: {"ok": true, "data": {...}} / {"ok": false, "error": {...}}
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        items = payload.get("items", [])
        print(json.dumps(items, ensure_ascii=False, indent=2)[:2000])

        # Берём первые два товара в корзину (id из результатов поиска)
        products = [{"id": item["id"], "q": 1} for item in items[:2]]
        if not products:
            products = [{"id": 114976, "q": 1}, {"id": 84636, "q": 1}]

        print("\n=== vkusvill_cart_link_create ===")
        cart_raw = await manager.call_tool(
            "vkusvill_cart_link_create", {"products": products}
        )
        print(cart_raw)
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
