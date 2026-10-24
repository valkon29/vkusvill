"""FastAPI-приложение: чат-эндпоинт (SSE) + статика.

Единственное состояние сервера — MCP-сессия из lifespan; история диалога
живёт в браузере (localStorage) и приходит в каждом запросе целиком.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.agent import Agent
from app.config import settings
from app.mcp_client import MCPClientManager

_WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=_WEB_DIR / "templates")

mcp_manager = MCPClientManager(settings.mcp_url)
agent = Agent(mcp_manager)

MAX_HISTORY_MESSAGES = 40  # защита от раздувания контекста


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mcp_manager.start()
    yield
    await mcp_manager.close()


app = FastAPI(title="Вкусный ассистент", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    messages = body.get("messages") or []
    # системный промпт добавляет сам агент; клиент шлёт только user/assistant
    messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    messages = messages[-MAX_HISTORY_MESSAGES:]
    if not messages:
        return StreamingResponse(
            iter([_sse("error", {"message": "пустая история"})]),
            media_type="text/event-stream",
        )

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(event_type: str, data: dict) -> None:
        if event_type == "done":
            await queue.put(None)  # конец потока
        else:
            await queue.put(_sse(event_type, data))

    task = asyncio.create_task(agent.run(messages, emit))

    async def event_stream():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
