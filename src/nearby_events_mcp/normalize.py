"""Turn raw `destination_event` objects into compact, LLM-friendly dicts."""

from __future__ import annotations

import html
import math
import re
from datetime import datetime
from typing import Any

try:  # zoneinfo needs tzdata on Windows; degrade gracefully
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def html_to_text(raw: str | None, limit: int | None = None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _money(block: dict | None) -> float | None:
    if not block:
        return None
    major = to_float(block.get("major_value"))
    if major is not None:
        return major
    value = to_float(block.get("value"))
    return None if value is None else value / 100.0


def _has_ended(start_date: str | None, end_date: str | None, end_time: str | None,
               tz_name: str | None) -> bool:
    if not (end_date and end_time and tz_name and ZoneInfo):
        return False
    try:
        end = datetime.fromisoformat(f"{end_date}T{end_time}").replace(tzinfo=ZoneInfo(tz_name))
    except Exception:
        return False
    return end < datetime.now(end.tzinfo)


def normalize_event(raw: dict, origin: tuple[float, float] | None = None) -> dict:
    ta = raw.get("ticket_availability") or {}
    venue = raw.get("primary_venue") or {}
    addr = venue.get("address") or {}
    org = raw.get("primary_organizer") or {}
    sales = raw.get("event_sales_status") or {}
    tags = raw.get("tags") or []

    lat, lng = to_float(addr.get("latitude")), to_float(addr.get("longitude"))
    distance = None
    if origin and lat is not None and lng is not None:
        distance = round(haversine_km(origin[0], origin[1], lat, lng), 2)

    price_min = _money(ta.get("minimum_ticket_price"))
    price_max = _money(ta.get("maximum_ticket_price"))
    currency = (ta.get("minimum_ticket_price") or ta.get("maximum_ticket_price") or {}).get("currency") \
        or sales.get("currency")
    is_free = bool(ta.get("is_free"))
    if is_free:
        display = "Free"
    elif price_min is not None and price_max is not None and price_min != price_max:
        display = f"{currency or ''} {price_min:.2f}–{price_max:.2f}".strip()
    elif price_min is not None:
        display = f"{currency or ''} {price_min:.2f}".strip()
    else:
        display = None

    def tag_names(prefix: str) -> list[str]:
        return [t.get("display_name") for t in tags if t.get("prefix") == prefix and t.get("display_name")]

    start_date, start_time = raw.get("start_date"), raw.get("start_time")
    end_date, end_time = raw.get("end_date"), raw.get("end_time")
    sales_status = sales.get("sales_status")

    return {
        "id": raw.get("id") or raw.get("eventbrite_event_id"),
        "name": raw.get("name"),
        "url": raw.get("url"),
        "summary": html_to_text(raw.get("summary"), 240),
        "start": f"{start_date}T{start_time}" if start_date and start_time else start_date,
        "end": f"{end_date}T{end_time}" if end_date and end_time else end_date,
        "timezone": raw.get("timezone"),
        "venue": venue.get("name"),
        "address": addr.get("localized_address_display"),
        "lat": lat,
        "lng": lng,
        "distance_km": distance,
        "is_online": bool(raw.get("is_online_event")),
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "currency": currency,
        "price": display,
        "categories": tag_names("EventbriteCategory"),
        "subcategories": tag_names("EventbriteSubCategory"),
        "formats": tag_names("EventbriteFormat"),
        "organizer": org.get("name"),
        "organizer_followers": org.get("num_followers"),
        "sales_status": sales_status,
        "sold_out": bool(ta.get("is_sold_out")),
        "has_tickets": ta.get("has_available_tickets"),
        "urgency": (raw.get("urgency_signals") or {}).get("messages") or [],
        "is_series": (raw.get("num_children") or 0) > 1,
        "capacity": None,  # filled by enrich_capacity() when requested
        "_ended": _has_ended(start_date, end_date, end_time, raw.get("timezone")),
        "_tags": [t.get("tag") for t in tags if t.get("tag")],
    }


def capacity_from_ticket_classes(payload: dict) -> int | None:
    """Sum of per-tier capacities, or None if the organizer hides them (usual case)."""
    caps = [t.get("capacity") for t in payload.get("ticket_classes", []) if isinstance(t.get("capacity"), (int, float))]
    return int(sum(caps)) if caps else None


def normalize_ticket_class(t: dict) -> dict:
    cost = t.get("cost") or {}
    return {
        "name": t.get("display_name") or t.get("name"),
        "free": bool(t.get("free")),
        "donation": bool(t.get("donation")),
        "price": cost.get("display"),
        "currency": cost.get("currency"),
        "capacity": t.get("capacity"),
        "on_sale_status": t.get("on_sale_status"),
        "min_per_order": t.get("minimum_quantity"),
        "max_per_order": t.get("maximum_quantity_per_order") or t.get("maximum_quantity"),
    }
