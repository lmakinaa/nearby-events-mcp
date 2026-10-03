"""Search orchestration: build the API request, page through results, apply client-side filters."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date as _date

from .client import EventsClient, EventsError
from .constants import (DATE_PRESETS, PAGE_SIZE, SIZE_BUCKETS, resolve_categories,
                        resolve_formats)
from .normalize import capacity_from_ticket_classes, normalize_event

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
MAX_ENRICH = 40


@dataclass
class SearchParams:
    date: str = "today"  # preset or YYYY-MM-DD
    date_to: str | None = None  # inclusive range end (YYYY-MM-DD)
    query: str | None = None
    radius_km: float = 10
    price: str = "any"  # any | free | paid
    categories: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    min_price: float | None = None  # most expensive ticket must be >= this
    max_price: float | None = None  # cheapest ticket must be <= this (free counts as 0)
    size: str | None = None  # small | medium | large (organizer-follower proxy)
    min_followers: int | None = None
    max_followers: int | None = None
    include_capacity: bool = False
    min_capacity: int | None = None
    max_capacity: int | None = None
    start_after: str | None = None  # HH:MM local
    start_before: str | None = None  # HH:MM local
    include_online: bool = False
    available_only: bool = False
    include_ended: bool = False
    sort: str = "distance"  # distance | date | price | popularity
    max_results: int = 20
    max_pages: int = 5


def validate(p: SearchParams) -> None:
    if p.date not in DATE_PRESETS:
        try:
            _date.fromisoformat(p.date)
        except ValueError:
            raise ValueError(
                f"date must be one of {sorted(DATE_PRESETS)} or YYYY-MM-DD, got '{p.date}'") from None
    if p.date_to:
        try:
            end = _date.fromisoformat(p.date_to)
            start = _date.fromisoformat(p.date)
        except ValueError:
            raise ValueError("date_to requires date and date_to both in YYYY-MM-DD format") from None
        if end < start:
            raise ValueError("date_to must be on or after date")
    for name in ("start_after", "start_before"):
        v = getattr(p, name)
        if v and not _HHMM.match(v):
            raise ValueError(f"{name} must be HH:MM (24h), got '{v}'")
    if p.price not in {"any", "free", "paid"}:
        raise ValueError("price must be any, free or paid")
    if p.size and p.size not in SIZE_BUCKETS:
        raise ValueError(f"size must be one of {sorted(SIZE_BUCKETS)}")
    if p.sort not in {"distance", "date", "price", "popularity"}:
        raise ValueError("sort must be distance, date, price or popularity")


def build_event_search(p: SearchParams, origin: tuple[float, float], page: int) -> dict:
    es: dict = {"page": page, "page_size": PAGE_SIZE, "dedup": True}
    if p.date in DATE_PRESETS and not p.date_to:
        es["dates"] = p.date
    elif p.date in DATE_PRESETS:
        raise ValueError("date_to can only be combined with a YYYY-MM-DD start date")
    else:
        es["date_range"] = {"from": p.date, "to": p.date_to or p.date}
    # Server only accepts an integer km string ("5km"); "5.0km" is rejected with 400.
    es["point_radius"] = {
        "latitude": origin[0],
        "longitude": origin[1],
        "radius": f"{max(1, round(p.radius_km))}km",
    }
    if p.query:
        es["q"] = p.query
    if p.price in {"free", "paid"}:
        es["price"] = p.price
    cats, fmts = resolve_categories(p.categories), resolve_formats(p.formats)
    # Multiple tags are OR-ed server-side. Categories and formats are different dimensions, so if both
    # are given only categories go to the server and formats are enforced client-side.
    server_tags = cats or fmts
    if server_tags:
        es["tags"] = server_tags
    # Only "date" and "distance" are valid server sorts.
    es["sort"] = "distance" if p.sort == "distance" else "date"
    return es


def _cheapest(ev: dict) -> float | None:
    if ev["is_free"]:
        return 0.0
    return ev["price_min"]


def passes(ev: dict, p: SearchParams, format_tags: list[str]) -> bool:
    if ev["is_online"] and not p.include_online:
        return False
    if ev["_ended"] and not p.include_ended:
        return False
    if p.available_only and (ev["sold_out"] or ev["sales_status"] == "sales_ended"):
        return False

    cheapest = _cheapest(ev)
    if p.max_price is not None and cheapest is not None and cheapest > p.max_price:
        return False
    top = 0.0 if ev["is_free"] else ev["price_max"]
    if p.min_price is not None and top is not None and top < p.min_price:
        return False

    lo, hi = p.min_followers, p.max_followers
    if p.size:
        blo, bhi = SIZE_BUCKETS[p.size]
        lo = blo if lo is None else lo
        hi = bhi if hi is None else hi
    if lo is not None or hi is not None:
        f = ev["organizer_followers"]
        if f is None:
            return False
        if lo is not None and f < lo:
            return False
        if hi is not None and f >= hi:
            return False

    if format_tags and not any(t in ev["_tags"] for t in format_tags):
        return False

    start = ev["start"] or ""
    hhmm = start[11:16] if len(start) >= 16 else None
    if hhmm:
        if p.start_after and hhmm < p.start_after.zfill(5):
            return False
        if p.start_before and hhmm > p.start_before.zfill(5):
            return False
    return True


def _sort_key(p: SearchParams):
    if p.sort == "distance":
        return lambda e: (e["distance_km"] is None, e["distance_km"] or 0, e["start"] or "")
    if p.sort == "price":
        return lambda e: (_cheapest(e) is None, _cheapest(e) or 0, e["start"] or "")
    if p.sort == "popularity":
        return lambda e: (-(e["organizer_followers"] or 0), e["start"] or "")
    return lambda e: (e["start"] or "", e["distance_km"] or 0)


async def enrich_capacity(client: EventsClient, events: list[dict]) -> int:
    """Fetch ticket classes and sum their capacities. Returns number of events with a known capacity."""
    sem = asyncio.Semaphore(4)

    async def one(ev: dict) -> None:
        async with sem:
            try:
                ev["capacity"] = capacity_from_ticket_classes(await client.get_ticket_classes(ev["id"]))
            except EventsError:
                ev["capacity"] = None

    await asyncio.gather(*(one(e) for e in events[:MAX_ENRICH]))
    return sum(1 for e in events if e["capacity"] is not None)


async def run_search(client: EventsClient, p: SearchParams, origin: tuple[float, float]) -> dict:
    validate(p)
    format_tags = resolve_formats(p.formats) if p.categories else []
    server_sorted = p.sort in {"distance", "date"}
    kept: list[dict] = []
    seen: set[str] = set()
    scanned = 0
    more = False

    for page in range(1, max(1, p.max_pages) + 1):
        data = await client.search(build_event_search(p, origin, page))
        block = data.get("events") or {}
        raw_events = block.get("results") or []
        if not raw_events:
            more = False
            break
        for raw in raw_events:
            ev = normalize_event(raw, origin)
            if not ev["id"] or ev["id"] in seen:
                continue
            seen.add(ev["id"])
            scanned += 1
            if passes(ev, p, format_tags):
                kept.append(ev)
        page_count = (block.get("pagination") or {}).get("page_count") or page
        more = page < page_count
        if not more:
            break
        if server_sorted and len(kept) >= p.max_results and p.min_capacity is None and p.max_capacity is None:
            break

    notes: list[str] = []
    if p.include_capacity or p.min_capacity is not None or p.max_capacity is not None:
        kept.sort(key=_sort_key(p))
        known = await enrich_capacity(client, kept)
        if p.min_capacity is not None or p.max_capacity is not None:
            def cap_ok(e: dict) -> bool:
                c = e["capacity"]
                if c is None:
                    return True  # unknown -> keep
                return (p.min_capacity is None or c >= p.min_capacity) and \
                       (p.max_capacity is None or c <= p.max_capacity)
            kept = [e for e in kept if cap_ok(e)]
        notes.append(
            f"Capacity is only published by some organizers: known for {known} of "
            f"{min(len(kept), MAX_ENRICH)} checked events; events with unknown capacity are kept.")
    if p.size or p.min_followers is not None or p.max_followers is not None:
        notes.append("Size filter uses organizer follower count as a proxy (Eventbrite does not expose "
                     "attendance). small<200, medium 200-2000, large>2000 followers.")

    kept.sort(key=_sort_key(p))
    total_kept = len(kept)
    out = kept[: max(1, p.max_results)]
    for e in out:
        e.pop("_ended", None)
        e.pop("_tags", None)
    return {
        "count": len(out),
        "matched": total_kept,
        "scanned": scanned,
        "more_available": more or total_kept > len(out),
        "notes": notes,
        "events": out,
    }
