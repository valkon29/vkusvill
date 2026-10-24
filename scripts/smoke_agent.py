"""CLI-прогон полного сценария: python -m scripts.smoke_agent "перекус на работу, бюджет 500 ₽, без лактозы"

Использует тот же агентный цикл, что и веб-сервис, печатает события в терминал.
"""

import asyncio
import sys

from app.agent import Agent
from app.config import settings
from app.mcp_client import MCPClientManager


async def printer(event_type: str, data: dict) -> None:
    if event_type == "token":
        print(data["text"], end="", flush=True)
    elif event_type == "tool":
        print(f"\n⚙ [{data['label']}]", file=sys.stderr)
    elif event_type == "products":
        names = ", ".join(f"{i['name']} ({i['price']} ₽)" for i in data["items"][:5])
        print(f"\n🛒 Товары: {names}", file=sys.stderr)
    elif event_type == "cart":
        print(
            f"\n🧺 Корзина: {data['count']} поз., {data['total']} ₽ — {data['url']}",
            file=sys.stderr,
        )
    elif event_type == "error":
        print(f"\n❌ {data['message']}", file=sys.stderr)


async def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Собери перекус на работу, бюджет 500 ₽, без лактозы"
    print(f"Пользователь: {prompt}\n", file=sys.stderr)

    mcp = MCPClientManager(settings.mcp_url)
    await mcp.start()
    try:
        agent = Agent(mcp)
        await agent.run([{"role": "user", "content": prompt}], printer)
        print(file=sys.stderr)
    finally:
        await mcp.close()


if __name__ == "__main__":
    asyncio.run(main())
