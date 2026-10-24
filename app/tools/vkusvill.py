"""Обёртки над MCP-инструментами ВкусВилла.

Каждая обёртка: вызывает MCP-инструмент, отправляет богатое событие в UI
(products/cart) и возвращает LLM компактный текст (без картинок и лишних
полей — экономия токенов). Ошибки возвращаются строками TOOL_ERROR, чтобы
LLM могла извиниться и предложить альтернативу.

Конверт ответов сервера: {"ok": true, "data": {...}} / {"ok": false, ...}.
"""

import asyncio
import json
import logging
import re

from app.config import settings
from app.events import Emit
from app.mcp_client import MCPClientManager, MCPUnavailable

logger = logging.getLogger(__name__)

_SEARCH_FIELDS = ["id", "name", "price", "unit", "rating", "url", "images"]

# id аллергенов для vkusvill_recipes; заполняется лениво из ответа сервера
_allergen_ids: dict[str, int] = {}


def _clean(value) -> str | None:
    """Убирает HTML-сущности из полей выдачи (в названиях встречается &nbsp;)."""
    if not isinstance(value, str):
        return value
    return (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def _compact(items: list[dict], *, with_old_price: bool = False) -> list[dict]:
    """Компактное представление товара для LLM (без картинок)."""
    out = []
    for it in items:
        price = it.get("price") or {}
        rating = it.get("rating") or {}
        row = {
            "id": it.get("id"),
            "name": _clean(it.get("name")),
            "price": price.get("current"),
            "unit": it.get("unit"),
            "rating": round(rating.get("average") or 0, 1),
            "url": it.get("url"),
        }
        if with_old_price:
            row["old_price"] = price.get("old")
        out.append(row)
    return out


def _cards(items: list[dict]) -> list[dict]:
    """Карточки для UI: как _compact, но с картинкой."""
    cards = _compact(items, with_old_price=True)
    for card, it in zip(cards, items):
        images = it.get("images") or []
        card["image"] = images[0].get("small") if images else None
    return cards


def _parse_envelope(raw: str) -> dict:
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        return {"ok": True, "data": data}
    return data


def _payload(raw: str) -> dict | list:
    data = _parse_envelope(raw)
    if data.get("ok") is False:
        raise MCPUnavailable(str(data.get("error", {}).get("message", "ошибка")))
    return data.get("data", data)


def _clip(text: str) -> str:
    limit = settings.tool_result_max_chars
    return text if len(text) <= limit else text[:limit] + "…(обрезано)"


async def search_products(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "TOOL_ERROR: пустой поисковый запрос"
    limit = max(1, min(10, int(args.get("limit") or 5)))
    raw = await mcp.call_tool(
        "vkusvill_products_search",
        {
            "q": query,
            "mode": "custom",
            "fields": _SEARCH_FIELDS,
            "limit": limit,
            "page": max(1, int(args.get("page") or 1)),
            "sort": args.get("sort") or "popularity",
            "vvonly": 0,
        },
    )
    payload = _payload(raw)
    items = payload.get("items", [])
    meta = payload.get("meta", {})
    if items:
        await emit("products", {"items": _cards(items)})
    result = {
        "meta": {"total": meta.get("total"), "query": query},
        "items": _compact(items),
    }
    return _clip(json.dumps(result, ensure_ascii=False))


async def get_product_details(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    product_id = int(args.get("product_id") or 0)
    if product_id <= 0:
        return "TOOL_ERROR: не указан product_id"
    raw = await mcp.call_tool("vkusvill_product_details", {"id": product_id})
    d = _payload(raw)
    if not isinstance(d, dict) or "id" not in d:
        return "TOOL_ERROR: товар не найден"
    properties = {p.get("name", ""): p.get("value", "") for p in d.get("properties", [])}
    result = {
        "id": d.get("id"),
        "name": _clean(d.get("name")),
        "brand": _clean(d.get("brand")),
        "price": (d.get("price") or {}).get("current"),
        "unit": d.get("unit"),
        "weight": (d.get("weight") or {}).get("value"),
        "rating": round((d.get("rating") or {}).get("average") or 0, 1),
        "composition": properties.get("Состав"),
        "nutrition": properties.get("Пищевая и энергетическая ценность в 100 г"),
        "url": d.get("url"),
    }
    return _clip(json.dumps(result, ensure_ascii=False))


async def find_product_analogs(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    product_id = int(args.get("product_id") or 0)
    if product_id <= 0:
        return "TOOL_ERROR: не указан product_id"
    raw = await mcp.call_tool("vkusvill_product_analogs", {"id": product_id})
    payload = _payload(raw)
    items = payload.get("products", [])
    if items:
        await emit("products", {"items": _cards(items[:10])})
    return _clip(json.dumps(_compact(items[:10]), ensure_ascii=False))


async def search_discounted_products(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    raw = await mcp.call_tool(
        "vkusvill_products_discount",
        {
            "type": args.get("type") or "card",
            "page": max(1, int(args.get("page") or 1)),
            "sort": args.get("sort") or "popularity",
            "vvonly": 0,
        },
    )
    payload = _payload(raw)
    items = payload.get("items", [])
    if items:
        await emit("products", {"items": _cards(items[:10])})
    return _clip(json.dumps(_compact(items[:10], with_old_price=True), ensure_ascii=False))


async def find_recipes(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    query = str(args.get("query", "")).strip()
    exclude = [a.lower().strip() for a in args.get("exclude_allergens") or [] if str(a).strip()]
    base = {
        "sort": "popularity",
        "page": 1,
        "q": query,
        "id_feature_filter": 0,
        "id_cooking_time_filter": 0,
        "id_cooking_method_filter": 0,
        "id_complexity_filter": 0,
        "id_category_filter": 0,
        "id_exclude_allergens_filter": [],
    }
    raw = await mcp.call_tool("vkusvill_recipes", base)
    payload = _payload(raw)

    # кэшируем карту аллергенов (id) из ответа сервера
    for f in payload.get("meta", {}).get("filters", []):
        if f.get("param") == "exclude_allergens":
            _allergen_ids.update(
                {it["name"].lower(): it["id"] for it in f.get("items", [])}
            )

    unknown = []
    if exclude:
        ids = []
        for name in exclude:
            found = _allergen_ids.get(name) or _allergen_ids.get(name.rstrip("а"))
            if found:
                ids.append(found)
            else:
                unknown.append(name)
        if ids:
            base["id_exclude_allergens_filter"] = ids
            raw = await mcp.call_tool("vkusvill_recipes", base)
            payload = _payload(raw)

    items = payload.get("items", [])
    recipes = [
        {
            "id": r.get("id"),
            "name": _clean(r.get("name")),
            "url": r.get("url"),
            "cooking_time": (r.get("cooking_time") or {}).get("name"),
            "complexity": (r.get("complexity") or {}).get("name"),
            "portions": r.get("portions"),
            "ingredients": [
                {"name": i.get("name"), "quantity": i.get("quantity")}
                for i in (r.get("ingredients") or [])
            ],
        }
        for r in items[:5]
    ]
    result = {"recipes": recipes}
    if unknown:
        result["warning"] = f"неизвестные аллергены: {', '.join(unknown)}"
    return _clip(json.dumps(result, ensure_ascii=False))


async def find_shops(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    raw = await mcp.call_tool("vkusvill_shops", {"page": max(1, int(args.get("page") or 1))})
    payload = _payload(raw)
    shops = [
        {
            "city": (s.get("city") or {}).get("name"),
            "address": s.get("address"),
            "subway": (s.get("subway") or {}).get("name") if isinstance(s.get("subway"), dict) else s.get("subway"),
            "schedule": s.get("schedule"),
            "rating": s.get("rating"),
        }
        for s in payload.get("items", [])[:5]
    ]
    return _clip(json.dumps(shops, ensure_ascii=False))


def _clamp_q(q: float) -> float:
    return round(min(40.0, max(0.01, float(q))), 2)


_CART_BLOCK_RE = re.compile(r"\[В корзине:[^\]]*\]")
_CART_ITEM_RE = re.compile(r"id=(\d+)[^;]*?×\s*([\d.]+)")
_SHOWN_BLOCK_RE = re.compile(r"\[Показаны товары:[^\]]*\]")
# оба служебных блока (фронтенд дописывает их в историю как носитель состояния)
_CTX_BLOCKS_RE = re.compile(r"\s*\[(Показаны товары|В корзине)[^\]]*\]", re.IGNORECASE)


def strip_context_blocks(text):
    """Убирает служебные блоки из текста сообщения — LLM их не видит."""
    if not isinstance(text, str):
        return text
    return _CTX_BLOCKS_RE.sub("", text).strip()


def last_shown_products_from_history(messages: list[dict]) -> str | None:
    """Последний блок [Показаны товары: …] — по нему решается «этот товар»."""
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        block = _SHOWN_BLOCK_RE.search(content)
        if block:
            return block.group(0)
    return None


def current_cart_from_history(messages: list[dict]) -> list[dict]:
    """Текущая корзина из служебного блока [В корзине: …] в истории.

    Блок дописывает фронтенд после события cart — состояние переносится
    в системный промпт, LLM эти блоки в сообщениях не видит.
    """
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        block = _CART_BLOCK_RE.search(content)
        if block:
            return [
                {"id": int(m.group(1)), "quantity": float(m.group(2))}
                for m in _CART_ITEM_RE.finditer(block.group(0))
            ]
    return []


async def create_cart(args: dict, mcp: MCPClientManager, emit: Emit) -> str:
    # _current_cart подставляет агент из блока [В корзине: …] — текущий состав
    current = args.pop("_current_cart", None) or []
    replace = bool(args.get("replace"))
    products = args.get("products") or []

    # позиции запроса: дубликаты суммируются
    new_items: dict[int, float] = {}
    for p in products:
        pid = int(p.get("id") or 0)
        if pid <= 0:
            return f"TOOL_ERROR: некорректный id товара {p.get('id')!r}"
        new_items[pid] = round(min(40.0, new_items.get(pid, 0.0) + _clamp_q(p.get("quantity") or 1)), 2)

    # без replace объединяем с текущей корзиной; новое количество затирает старое
    merged: dict[int, float] = (
        {}
        if replace
        else {
            int(c["id"]): _clamp_q(c.get("quantity") or 1)
            for c in current
            if int(c.get("id") or 0) > 0
        }
    )
    merged.update(new_items)
    if not 1 <= len(merged) <= 20:
        return "TOOL_ERROR: в корзине должно быть от 1 до 20 позиций"

    cart_items = [{"id": pid, "q": q} for pid, q in merged.items()]
    raw = await mcp.call_tool("vkusvill_cart_link_create", {"products": cart_items})
    payload = _payload(raw)
    link = payload.get("link")
    if not link:
        return "TOOL_ERROR: сервер не вернул ссылку на корзину"

    # цены и названия — для суммы и карточек; запрашиваем параллельно,
    # семафор в MCPClientManager сам следит за rate limit
    async def fetch(pid: int):
        try:
            detail_raw = await mcp.call_tool("vkusvill_product_details", {"id": pid})
            d = _payload(detail_raw)
            return {
                "id": pid,
                "name": _clean(d.get("name")),
                "price": (d.get("price") or {}).get("current"),
                "quantity": merged[pid],
            }
        except Exception:  # noqa: BLE001 — корзина важнее идеальных цен
            return {"id": pid, "name": None, "price": None, "quantity": merged[pid]}

    details = await asyncio.gather(*(fetch(pid) for pid in merged))
    total = sum((d["price"] or 0) * d["quantity"] for d in details)
    await emit(
        "cart",
        {"url": link, "total": round(total, 2), "count": len(details), "items": details},
    )
    result = {
        "link": link,
        "total": round(total, 2),
        "items": [
            f"{d['name'] or d['id']} × {d['quantity']}" for d in details
        ],
    }
    return _clip(json.dumps(result, ensure_ascii=False))


TOOL_HANDLERS = {
    "search_products": search_products,
    "get_product_details": get_product_details,
    "find_product_analogs": find_product_analogs,
    "search_discounted_products": search_discounted_products,
    "find_recipes": find_recipes,
    "find_shops": find_shops,
    "create_cart": create_cart,
}
